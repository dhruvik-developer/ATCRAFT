"""Password hashing and JWT issuance/verification.

Master rules section 8 calls for removing hardcoded passwords, replacing weak
hashing, and adding JWT expiry. Concretely:

* Argon2id for new passwords, with a bcrypt verify-only path so an inherited
  user table can be migrated transparently on first successful sign-in.
* Short-lived access tokens plus rotating refresh tokens, each carrying a
  ``jti`` so a session can be revoked server-side through Redis.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type as Argon2Type

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

TokenType = Literal["access", "refresh"]


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


def validate_password_policy(password: str, *, email: str | None = None) -> None:
    """Enforce the password policy, raising :class:`ValidationError` on failure.

    Length carries most of the strength; the character-class rules exist because
    business users otherwise pick a dictionary word padded to length.
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
            details={"requirements": problems},
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


__all__ = [
    "TokenPayload",
    "TokenType",
    "constant_time_compare",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "generate_secret_token",
    "hash_password",
    "hash_secret_token",
    "needs_rehash",
    "validate_password_policy",
    "verify_password",
]
