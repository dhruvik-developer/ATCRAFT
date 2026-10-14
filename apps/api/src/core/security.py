"""Password hashing and JWT issuance/verification.

Master rules section 8 calls for removing hardcoded passwords, replacing weak
hashing, and adding JWT expiry. Concretely:

* Argon2id for new passwords, with a bcrypt verify-only path so an inherited
  user table can be migrated transparently on first successful sign-in.
* Short-lived access tokens plus rotating refresh tokens, each carrying a
  ``jti`` so a session can be revoked server-side through Redis.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type as Argon2Type
from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken as InvalidEncryptedToken

from src.core.clock import utcnow
from src.core.config import settings
from src.core.errors import AuthenticationError, ErrorCode, TokenExpiredError, ValidationError

# -----------------------------------------------------------------------------
# Password hashing
# -----------------------------------------------------------------------------

# Parameters follow the OWASP Argon2id guidance (19 MiB, t=2, p=1) - a balance
# between resistance to offline cracking and staying well under the latency
# budget of a login request on modest ERP hardware.
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Argon2Type.ID,
)

TokenType = Literal["access", "refresh", "mfa"]

MFA_CHALLENGE_TTL_MINUTES = 5
TOTP_PERIOD_SECONDS = 30


def hash_password(plain: str) -> str:
    """Hash a plaintext password with Argon2id."""
    if not plain:
        raise ValidationError("Password must not be empty.")
    return _hasher.hash(plain)


def verify_password(plain: str, stored_hash: str) -> bool:
    """Verify a password against an Argon2id or legacy bcrypt hash.

    Returns a plain bool rather than raising, because the caller must treat
    "unknown user" and "wrong password" identically to avoid enumeration.
    """
    if not stored_hash:
        return False

    if stored_hash.startswith("$argon2"):
        try:
            return _hasher.verify(stored_hash, plain)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    # Legacy path: an inherited table may still hold bcrypt hashes. Verify only -
    # never produce new ones - and let needs_rehash() trigger the upgrade.
    if stored_hash.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            import bcrypt

            return bcrypt.checkpw(plain.encode("utf-8"), stored_hash.encode("utf-8"))
        except (ValueError, TypeError):
            return False

    return False


# -----------------------------------------------------------------------------
# The async wrappers - what a request path must use
# -----------------------------------------------------------------------------
#
# Argon2id is deliberately expensive: measured at ~37 ms per hash and per verify
# with the parameters above. That is the right cost for resisting offline
# cracking, and exactly the wrong thing to run on the event loop - a login
# freezes the whole worker for 37 ms, so a shift change where fifty people sign
# in at once serialises into seconds during which no other request on that
# worker progresses, including ones that touch no password at all.
#
# `asyncio.to_thread` moves it to the default executor. Argon2 releases the GIL
# while it works, so the thread genuinely runs in parallel rather than merely
# deferring the stall.
#
# The synchronous forms stay, unchanged and exported: `src/core/seed.py` and the
# tests are not async, and a CLI blocking for 37 ms is not a problem.


async def hash_password_async(plain: str) -> str:
    """Hash off the event loop. Use this from any request path."""
    return await asyncio.to_thread(hash_password, plain)


async def verify_password_async(plain: str, stored_hash: str) -> bool:
    """Verify off the event loop. Use this from any request path."""
    return await asyncio.to_thread(verify_password, plain, stored_hash)


def needs_rehash(stored_hash: str) -> bool:
    """True when a verified password should be re-hashed with current settings.

    Covers both legacy bcrypt hashes and Argon2 hashes made with weaker
    parameters. Call after a successful verify and persist the new hash.
    """
    if not stored_hash.startswith("$argon2"):
        return True
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


# -----------------------------------------------------------------------------
# Password policy (section 38)
# -----------------------------------------------------------------------------

_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "12345678",
        "123456789",
        "qwerty123",
        "admin",
        "admin123",
        "administrator",
        "welcome1",
        "letmein1",
        "changeme",
        "atcraft",
        "atcraft123",
        "iloveyou",
        "monkey123",
        "dragon123",
        "sunshine",
    }
)


def validate_password_policy(
    password: str, *, email: str | None = None, field: str = "new_password"
) -> None:
    """Enforce the password policy, raising :class:`ValidationError` on failure.

    Length carries most of the strength; the character-class rules exist because
    business users otherwise pick a dictionary word padded to length.

    ``field`` is the name of the request field the password arrived in, so the
    error's ``details.fields`` lands on the control the user typed into: the
    self-service and reset flows send ``new_password``, an administrator's
    create-user form sends ``password``. ``details.requirements`` stays as the
    bare list for callers that render the policy as a checklist.
    """
    problems: list[str] = []
    minimum = settings.password_min_length

    if len(password) < minimum:
        problems.append(f"must be at least {minimum} characters")
    if len(password) > 128:
        problems.append("must be at most 128 characters")
    if not re.search(r"[a-z]", password):
        problems.append("must contain a lowercase letter")
    if not re.search(r"[A-Z]", password):
        problems.append("must contain an uppercase letter")
    if not re.search(r"\d", password):
        problems.append("must contain a digit")
    if not re.search(r"[^A-Za-z0-9]", password):
        problems.append("must contain a symbol")
    if password.lower() in _COMMON_PASSWORDS:
        problems.append("is too common")
    if email and email.split("@")[0].lower() in password.lower():
        problems.append("must not contain your email address")

    if problems:
        raise ValidationError(
            "Password " + "; ".join(problems) + ".",
            code=ErrorCode.PASSWORD_POLICY,
            details={
                "requirements": problems,
                "fields": {field: [f"Password {p}." for p in problems]},
            },
        )


# -----------------------------------------------------------------------------
# JWT
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """Decoded, verified token claims."""

    subject: uuid.UUID
    company_id: uuid.UUID
    token_type: TokenType
    jti: str
    issued_at: int
    expires_at: int
    session_id: str | None = None
    raw: dict[str, Any] | None = None


def _encode(
    subject: uuid.UUID,
    company_id: uuid.UUID,
    token_type: TokenType,
    expires_delta: timedelta,
    session_id: str | None,
    extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
    now = utcnow()
    jti = secrets.token_urlsafe(24)
    claims: dict[str, Any] = {
        "sub": str(subject),
        "cid": str(company_id),
        "typ": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    if session_id:
        claims["sid"] = session_id
    if extra:
        claims.update(extra)
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm), jti


def create_access_token(
    subject: uuid.UUID,
    company_id: uuid.UUID,
    *,
    session_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Issue a short-lived access token. Returns ``(token, jti)``.

    Permissions are deliberately NOT embedded: a role change must take effect
    immediately, and a 15-minute window of stale authority is unacceptable for
    approval rights (section 39). Permissions are loaded per request instead,
    from a Redis-cached, explicitly invalidated projection.
    """
    return _encode(
        subject,
        company_id,
        "access",
        timedelta(minutes=settings.jwt_access_token_expire_minutes),
        session_id,
        extra,
    )


def create_refresh_token(
    subject: uuid.UUID,
    company_id: uuid.UUID,
    *,
    session_id: str | None = None,
) -> tuple[str, str]:
    """Issue a refresh token. Returns ``(token, jti)``."""
    return _encode(
        subject,
        company_id,
        "refresh",
        timedelta(days=settings.jwt_refresh_token_expire_days),
        session_id,
    )


def create_mfa_challenge_token(
    subject: uuid.UUID,
    company_id: uuid.UUID,
    *,
    device_label: str | None = None,
) -> tuple[str, str]:
    """Issue a short-lived proof that the password stage already succeeded."""
    extra: dict[str, Any] = {"amr": ["pwd"]}
    if device_label:
        extra["device"] = device_label
    return _encode(
        subject,
        company_id,
        "mfa",
        timedelta(minutes=MFA_CHALLENGE_TTL_MINUTES),
        None,
        extra,
    )


def decode_token(token: str, *, expected_type: TokenType | None = None) -> TokenPayload:
    """Verify signature, expiry, issuer and audience, then return the claims."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError(
            "Invalid authentication token.", code=ErrorCode.TOKEN_INVALID
        ) from exc

    token_type = claims.get("typ")
    if expected_type and token_type != expected_type:
        # A refresh token presented as a bearer credential must be rejected -
        # it has a much longer lifetime than the access path assumes.
        raise AuthenticationError(
            "Wrong token type for this operation.", code=ErrorCode.TOKEN_INVALID
        )

    try:
        return TokenPayload(
            subject=uuid.UUID(claims["sub"]),
            company_id=uuid.UUID(claims["cid"]),
            token_type=token_type,  # type: ignore[arg-type]
            jti=claims["jti"],
            issued_at=int(claims["iat"]),
            expires_at=int(claims["exp"]),
            session_id=claims.get("sid"),
            raw=claims,
        )
    except (KeyError, ValueError) as exc:
        raise AuthenticationError(
            "Malformed authentication token.", code=ErrorCode.TOKEN_INVALID
        ) from exc


# -----------------------------------------------------------------------------
# Opaque secrets: password-reset links, API keys, vendor invitations
# -----------------------------------------------------------------------------


def generate_secret_token(length: int = 32) -> tuple[str, str]:
    """Return ``(plaintext, sha256_digest)``.

    Only the digest is stored, so a database leak does not hand over live reset
    links. Compared with :func:`constant_time_compare` on redemption.
    """
    plaintext = secrets.token_urlsafe(length)
    return plaintext, hashlib.sha256(plaintext.encode()).hexdigest()


def hash_secret_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# -----------------------------------------------------------------------------
# TOTP MFA
# -----------------------------------------------------------------------------


class CredentialDecryptionError(RuntimeError):
    """An encrypted database credential cannot be read with this deployment key."""


def _credential_fernet(purpose: str) -> Fernet:
    """Derive one purpose-specific Fernet key from the credential root key."""
    root_key = settings.credential_encryption_key.get_secret_value().encode()
    derived = hmac.new(root_key, f"atcraft:{purpose}:v1".encode(), hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def _encrypt_credential(value: str, *, purpose: str) -> str:
    return _credential_fernet(purpose).encrypt(value.encode()).decode()


def _decrypt_credential(ciphertext: str, *, purpose: str) -> str:
    try:
        return _credential_fernet(purpose).decrypt(ciphertext.encode()).decode()
    except (InvalidEncryptedToken, ValueError, TypeError) as exc:
        raise CredentialDecryptionError(
            f"Encrypted {purpose} credential cannot be decrypted with this deployment key."
        ) from exc


def generate_totp_secret() -> str:
    """Return a 160-bit RFC 6238 Base32 secret."""
    return pyotp.random_base32(length=32)


def encrypt_totp_secret(secret: str) -> str:
    return _encrypt_credential(secret, purpose="mfa")


def decrypt_totp_secret(ciphertext: str) -> str:
    # Rolling-upgrade compatibility for the original, unencrypted Base32
    # column. The service replaces this value with ciphertext immediately after
    # the next successful verification.
    if re.fullmatch(r"[A-Z2-7]{32}", ciphertext):
        return ciphertext
    try:
        return _decrypt_credential(ciphertext, purpose="mfa")
    except CredentialDecryptionError as exc:
        raise AuthenticationError(
            "Multi-factor authentication is not available for this account.",
            code=ErrorCode.MFA_INVALID,
        ) from exc


def encrypt_password_reset_delivery_token(token: str) -> str:
    """Encrypt the recoverable delivery copy of a password-reset credential."""
    return _encrypt_credential(token, purpose="password-reset-delivery")


def decrypt_password_reset_delivery_token(ciphertext: str) -> str:
    """Decrypt a reset credential inside the mail worker only."""
    return _decrypt_credential(ciphertext, purpose="password-reset-delivery")


def totp_provisioning_uri(secret: str, *, account_name: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(
        name=account_name,
        issuer_name=settings.app_name,
    )


def matching_totp_step(
    secret: str,
    code: str,
    *,
    at: datetime | None = None,
    valid_window: int = 1,
) -> int | None:
    """Return the accepted time-step, permitting modest clock drift.

    Returning the counter (rather than a bool) lets the service persist it and
    reject reuse of a code that was already accepted in the same 30-second
    window, including concurrent requests made through different workers.
    """
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    moment = at or utcnow()
    current_step = int(moment.timestamp()) // TOTP_PERIOD_SECONDS
    totp = pyotp.TOTP(secret, interval=TOTP_PERIOD_SECONDS)
    for offset in range(-valid_window, valid_window + 1):
        step = current_step + offset
        if step >= 0 and hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


__all__ = [
    "MFA_CHALLENGE_TTL_MINUTES",
    "CredentialDecryptionError",
    "TokenPayload",
    "TokenType",
    "constant_time_compare",
    "create_access_token",
    "create_mfa_challenge_token",
    "create_refresh_token",
    "decode_token",
    "decrypt_password_reset_delivery_token",
    "decrypt_totp_secret",
    "encrypt_password_reset_delivery_token",
    "encrypt_totp_secret",
    "generate_secret_token",
    "generate_totp_secret",
    "hash_password",
    "hash_password_async",
    "hash_secret_token",
    "matching_totp_step",
    "needs_rehash",
    "totp_provisioning_uri",
    "validate_password_policy",
    "verify_password",
    "verify_password_async",
]


def encrypt_push_subscription(endpoint: str) -> str:
    return _encrypt_credential(endpoint, purpose="push-subscription")


def decrypt_push_subscription(ciphertext: str) -> str:
    return _decrypt_credential(ciphertext, purpose="push-subscription")
