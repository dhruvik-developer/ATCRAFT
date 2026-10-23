"""RFC 8030/8291/8292 Web Push, using the existing cryptography and PyJWT stack.

https://www.rfc-editor.org/rfc/rfc8291.html
https://www.rfc-editor.org/rfc/rfc8292.html
Endpoint URLs and transport response bodies are capabilities and never logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import struct
from enum import StrEnum
from functools import lru_cache
from typing import Any, Protocol
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.core.clock import utcnow
from src.core.config import settings
from src.core.security import decrypt_push_subscription

P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
MAX_PUSH_PAYLOAD_BYTES = 3072


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_b64url(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


@lru_cache(maxsize=1)
def vapid_keypair() -> ec.EllipticCurvePrivateKey:
    raw = settings.web_push_vapid_private_key.get_secret_value()
    if raw:
        scalar = int.from_bytes(decode_b64url(raw), "big")
        if not 1 <= scalar < P256_ORDER:
            raise ValueError("VAPID private key must be a valid base64url P-256 scalar.")
    else:
        # ASSUMPTION: independent purpose derivation avoids another mandatory secret.
        # Root-key rotation requires device re-subscription. docs/assumptions.md §C1-13.
        digest = hmac.new(
            settings.credential_encryption_key.get_secret_value().encode(),
            b"web-push-vapid",
            hashlib.sha256,
        ).digest()
        scalar = int.from_bytes(digest, "big") % (P256_ORDER - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def vapid_public_key_b64url() -> str:
    return b64url(_point(vapid_keypair().public_key()))


def vapid_authorization(endpoint: str) -> str:
    assert_allowed_endpoint(endpoint)
    parsed = urlsplit(endpoint)
    subject = (
        settings.web_push_subject or f"mailto:{settings.email_from_address or 'no-reply@localhost'}"
    )
    token = jwt.encode(
        {
            "aud": f"https://{parsed.hostname}",
            "exp": int(utcnow().timestamp()) + 12 * 3600,
            "sub": subject,
        },
        vapid_keypair(),
        algorithm="ES256",
    )
    return f"vapid t={token}, k={vapid_public_key_b64url()}"


def encrypt_aes128gcm(
    p256dh_b64url: str,
    auth_b64url: str,
    plaintext: bytes,
    *,
    salt: bytes | None = None,
    ephemeral_private: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    if len(plaintext) > 3993:
        raise ValueError("Push payload is too large.")
    receiver_point = decode_b64url(p256dh_b64url)
    auth = decode_b64url(auth_b64url)
    if len(receiver_point) != 65 or len(auth) != 16:
        raise ValueError("Invalid push subscription key lengths.")
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver_point)
    private = ephemeral_private or ec.generate_private_key(ec.SECP256R1())
    sender_point = _point(private.public_key())
    shared = private.exchange(ec.ECDH(), receiver)
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth,
        info=b"WebPush: info\x00" + receiver_point + sender_point,
    ).derive(shared)
    salt = salt if salt is not None else secrets.token_bytes(16)
    if len(salt) != 16:
        raise ValueError("Push salt must contain 16 bytes.")
    key = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00"
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00"
    ).derive(ikm)
    return (
        salt
        + struct.pack("!IB", 4096, 65)
        + sender_point
        + AESGCM(key).encrypt(nonce, plaintext + b"\x02", None)
    )


def assert_allowed_endpoint(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid push endpoint.") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or any(ord(c) < 33 for c in url)
    ):
        raise ValueError("Push endpoint must be HTTPS on the default port without credentials.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Push endpoints cannot use IP addresses.")
    hosts = [h.strip().lower() for h in settings.web_push_allowed_hosts.split(",") if h.strip()]
    if not any(
        host.lower() == allowed or host.lower().endswith("." + allowed) for allowed in hosts
    ):
        raise ValueError("Push endpoint host is not allowed.")


class PushOutcome(StrEnum):
    DELIVERED = "DELIVERED"
    GONE = "GONE"
    TOO_LARGE = "TOO_LARGE"
    RETRY = "RETRY"
    FAILED = "FAILED"


class PushRetryableError(OSError):
    """No provider data in the exception; Celery retries this task."""


class PushDevice(Protocol):
    endpoint_ciphertext: str
    p256dh: str
    auth: str


def outcome_for_status(status: int) -> PushOutcome:
    if status in (201, 202):
        return PushOutcome.DELIVERED
    if status in (404, 410):
        return PushOutcome.GONE
    if status == 413:
        return PushOutcome.TOO_LARGE
    if status == 429 or status >= 500:
        return PushOutcome.RETRY
    return PushOutcome.FAILED


def _deliver(endpoint: str, encrypted: bytes, headers: dict[str, str]) -> PushOutcome:
    parsed = urlsplit(endpoint)
    connection = http.client.HTTPSConnection(
        parsed.hostname or "", timeout=settings.web_push_timeout_seconds
    )
    try:
        connection.request(
            "POST",
            (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
            body=encrypted,
            headers=headers,
        )
        response = connection.getresponse()
        return outcome_for_status(response.status)
    except (OSError, http.client.HTTPException):
        return PushOutcome.RETRY
    finally:
        connection.close()


async def deliver_push(
    sub: PushDevice, payload: bytes, *, ttl: int, urgency: str, topic: str
) -> PushOutcome:
    if not settings.web_push_enabled:
        return PushOutcome.FAILED
    endpoint = decrypt_push_subscription(sub.endpoint_ciphertext)
    assert_allowed_endpoint(endpoint)
    encrypted = encrypt_aes128gcm(sub.p256dh, sub.auth, payload)
    headers = {
        "Authorization": vapid_authorization(endpoint),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": urgency,
        "Topic": topic,
    }
    return await asyncio.to_thread(_deliver, endpoint, encrypted, headers)


def push_payload(notification: Any) -> bytes:
    """Fit the complete JSON payload, including Unicode metadata, in one record.

    Character limits on the database columns do not bound their UTF-8 or JSON
    byte lengths. Use one stable compact collapse key and fall back to the
    notification feed before shortening a headline; never truncate a URL.
    """
    data = {
        "id": str(notification.id),
        "title": notification.title,
        "body": notification.body,
        "url": notification.action_url or "/notifications",
        "tag": push_topic(notification),
        "severity": str(notification.severity),
        "count": notification.occurrence_count,
    }

    def encode() -> bytes:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()

    payload = encode()
    if len(payload) <= MAX_PUSH_PAYLOAD_BYTES:
        return payload

    body = data["body"]
    data["body"] = ""
    if len(encode()) > MAX_PUSH_PAYLOAD_BYTES:
        data["url"] = "/notifications"

    def fit_text(field: str, text: str) -> None:
        # JSON escapes also consume bytes. Binary search the serialized prefix,
        # with a strictly shrinking interval so even malformed input terminates.
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            data[field] = text[:middle]
            if len(encode()) <= MAX_PUSH_PAYLOAD_BYTES:
                low = middle
            else:
                high = middle - 1
        data[field] = text[:low]

    if len(encode()) > MAX_PUSH_PAYLOAD_BYTES:
        fit_text("title", data["title"])
    if len(encode()) > MAX_PUSH_PAYLOAD_BYTES:
        raise ValueError("Push notification metadata exceeds the payload limit.")
    fit_text("body", body)
    return encode()


def push_topic(notification: Any) -> str:
    return b64url(
        hashlib.sha256((notification.group_key or str(notification.id)).encode()).digest()
    )[:32]
