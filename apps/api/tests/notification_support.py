"""The people notifications reach, and the device a push goes to.

``_make_user`` builds a user through the ORM with its roles and permission
exceptions attached before the first flush - appending to the roles of a
flushed user would lazy-load from a synchronous context. ``_payload`` is a push
subscription a browser would send: a fresh P-256 key, an auth secret and an
endpoint no other test shares.

The notification service, push delivery, web-push and notification route
suites all use them; they live here so none of those files is another's
library. They keep the names they had there.
"""

from __future__ import annotations

import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.webpush import b64url


async def _make_user(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    is_active: bool = True,
    roles: tuple = (),
    customer_id: uuid.UUID | None = None,
    extra_permissions: list[str] | None = None,
    denied_permissions: list[str] | None = None,
):
    from src.core.security import hash_password
    from src.modules.users.models import User

    record = User(
        company_id=company_id,
        email=f"n-{uuid.uuid4().hex[:10]}@test.local",
        full_name="Notified Person",
        password_hash=hash_password("TestPassword!2026"),
        is_active=is_active,
        customer_id=customer_id,
        extra_permissions=extra_permissions,
        denied_permissions=denied_permissions,
    )
    # Roles are attached before the object is persisted: appending to the
    # collection of an already-flushed instance would trigger a lazy load from a
    # synchronous context.
    for role in roles:
        record.roles.append(role)
    session.add(record)
    await session.flush()
    return record


def _payload() -> dict:
    key = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return {
        "endpoint": f"https://fcm.googleapis.com/send/{uuid.uuid4()}",
        "keys": {"p256dh": b64url(key), "auth": b64url(b"0123456789abcdef")},
        "device_label": "Plant PC",
    }


__all__ = ["_make_user", "_payload"]
