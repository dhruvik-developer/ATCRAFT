"""Identity, roles and permissions (master rules sections 38/39).

Model shape
-----------
``users`` -N:M- ``roles`` -N:M- permission strings

Permissions are stored as strings from :class:`~src.core.permissions.Permission`
rather than as a table of permission rows. They are a fixed vocabulary compiled
into the application - a permission that no route checks does not exist, so a
separate master table would only be able to drift out of sync with the code.

A user may also carry direct grants and direct denials, which is what makes
"this buyer may approve, that one may not" expressible without minting a role
per person. Denials win over grants, always.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.models import (
    Base,
    ImmutableLedgerEntity,
    TenantEntity,
    TenantMasterEntity,
)
from src.core.types import GUID, CIText

# Association table. A plain Table (not a model) because it carries no business
# data of its own beyond the pairing.
user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", GUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", GUID, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("assigned_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("assigned_by_id", GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    Index("ix_user_roles_role", "role_id"),
)


class Role(TenantMasterEntity):
    """A named bundle of permissions."""

    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ARRAY(text) rather than a join table: the set is small, always read in
    # full, and never queried by individual element in a hot path.
    permissions: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{}'::varchar[]")
    )

    # System roles are seeded and may not be deleted; their permission set can
    # still be edited by an administrator (section 81: prefer configurable).
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Vendor roles are only assignable to a user bound to a vendor (section 16).
    is_vendor_role: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Dealer roles are only assignable to a user bound to a dealer customer
    # (docs/assumptions.md §C3-1). Mirrors ``is_vendor_role``.
    is_dealer_role: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # user_roles carries TWO foreign keys to users (the member and whoever
    # assigned them), so the join has to be spelled out - SQLAlchemy cannot pick
    # between them and raises AmbiguousForeignKeysError otherwise.
    users: Mapped[list[User]] = relationship(
        secondary=user_roles,
        primaryjoin=lambda: Role.id == user_roles.c.role_id,
        secondaryjoin=lambda: User.id == user_roles.c.user_id,
        back_populates="roles",
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_roles_company_id_code"),
        Index("ix_roles_company_active", "company_id", "is_active"),
    )


class User(TenantMasterEntity):
    """An account that can authenticate.

    Internal staff, vendor-portal users and dealer-portal users share this
    table. A vendor user is one with ``vendor_id`` set; a dealer user is one
    with ``customer_id`` set - never both. Every query that serves such a
    principal is additionally filtered by that binding (section 16,
    docs/assumptions.md §C3-1).
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CIText(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    employee_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Argon2id. Never logged, never serialised - see core.logging.SENSITIVE_KEYS.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Fernet ciphertext, never the plaintext Base32 seed. 255 leaves room for
    # encryption/version overhead while keeping the credential tightly bounded.
    mfa_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The last accepted RFC 6238 time-step prevents a valid code being replayed
    # during its 30-second window, even through another API worker.
    mfa_last_used_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Account lockout (section 38). Counter is reset on any successful sign-in.
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(INET, nullable=True)

    # Assignment targets refer back to their creator/owner in users. Emit these
    # named foreign keys after table creation to break the metadata dependency
    # cycles without omitting them from Alembic's schema comparison.
    # Organisational placement drives default warehouse/plant and approval routing.
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID,
        ForeignKey("branches.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        index=True,
    )
    plant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID,
        ForeignKey("plants.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        index=True,
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID,
        ForeignKey("departments.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        index=True,
    )

    # Vendor-portal binding (section 16). NULL for internal staff.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID,
        ForeignKey("vendors.id", ondelete="CASCADE", use_alter=True),
        nullable=True,
        index=True,
    )
    # Dealer-portal binding (docs/assumptions.md §C3-1). NULL for internal staff
    # and for vendor-portal accounts; the CHECK below keeps the two bindings
    # exclusive, because one account must never see two principals' data.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID,
        ForeignKey("customers.id", ondelete="CASCADE", use_alter=True),
        nullable=True,
        index=True,
    )
    #: Item categories this vendor user may see. Empty means "all of this
    #: vendor's categories"; enforced in the repository layer, never trusted
    #: from the client.
    allowed_category_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(GUID), nullable=True)
    #: Warehouses a store user is restricted to. Empty means unrestricted.
    allowed_warehouse_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(GUID), nullable=True
    )

    # Per-user overrides layered on top of role permissions. Denials win.
    extra_permissions: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    denied_permissions: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)

    preferences: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)

    # Eager by design: every authenticated request resolves permissions from the
    # role set, so a lazy load here would be an N+1 on the hottest path.
    roles: Mapped[list[Role]] = relationship(
        secondary=user_roles,
        primaryjoin=lambda: User.id == user_roles.c.user_id,
        secondaryjoin=lambda: Role.id == user_roles.c.role_id,
        back_populates="users",
        lazy="selectin",
    )

    __table_args__ = (
        # Email is unique per tenant: the same person may hold accounts in two
        # companies, but never two accounts in one.
        UniqueConstraint("company_id", "email", name="uq_users_company_id_email"),
        Index("ix_users_company_active", "company_id", "is_active"),
        Index("ix_users_vendor", "company_id", "vendor_id"),
        Index("ix_users_customer", "company_id", "customer_id"),
        Index("ix_users_email_lookup", "email"),
        CheckConstraint(
            "vendor_id IS NULL OR customer_id IS NULL",
            name="portal_binding_exclusive",
        ),
    )

    @property
    def is_vendor_user(self) -> bool:
        return self.vendor_id is not None

    @property
    def is_dealer_user(self) -> bool:
        return self.customer_id is not None

    @property
    def is_locked(self) -> bool:
        from src.core.clock import utcnow

        return self.locked_until is not None and self.locked_until > utcnow()


class UserSession(TenantEntity):
    """A refresh-token session (section 38: session/device management).

    Refresh tokens rotate on every use. ``replaced_by_id`` links the chain, so a
    replayed old token identifies the whole family as compromised and every
    session in it can be revoked at once.
    """

    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    refresh_jti: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("user_sessions.id", ondelete="SET NULL"), nullable=True
    )

    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    device_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_user_sessions_active", "user_id", "revoked_at", "expires_at"),)


class PasswordResetToken(TenantEntity):
    """A single-use password reset grant.

    The redeemable lookup value is stored only as a SHA-256 digest. A separate
    encrypted copy exists solely until the mail worker accepts delivery; it
    makes the database row a durable outbox when the broker is unavailable.
    """

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # SMTP acceptance timestamp. A retried Celery task checks this before
    # sending so the same reset link is not normally delivered twice.
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_token_ciphertext: Mapped[str | None] = mapped_column(String(512), nullable=True)
    requested_ip: Mapped[str | None] = mapped_column(INET, nullable=True)

    __table_args__ = (Index("ix_password_reset_active", "user_id", "used_at", "expires_at"),)


class LoginAttempt(ImmutableLedgerEntity):
    """Append-only sign-in history.

    Separate from ``audit_logs`` because it is written on *failed* attempts too,
    including for e-mail addresses that do not exist - which must not create an
    audit row that implies a real account.
    """

    __tablename__ = "login_attempts"

    email: Mapped[str] = mapped_column(CIText(255), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        Index("ix_login_attempts_email_time", "email", "created_at"),
        Index("ix_login_attempts_ip_time", "ip_address", "created_at"),
    )


__all__ = [
    "LoginAttempt",
    "PasswordResetToken",
    "Role",
    "User",
    "UserSession",
    "user_roles",
]
