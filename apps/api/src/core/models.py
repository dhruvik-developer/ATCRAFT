"""Declarative base and the mixins every ERP table is assembled from.

The constraint naming convention is deliberate: Alembic autogenerate can only
produce stable, reversible migrations when it can name an existing constraint,
and PostgreSQL's default names are not deterministic across environments.

Mixin vocabulary
----------------
``UUIDPrimaryKeyMixin``  time-ordered UUIDv7 primary key
``TimestampMixin``       created_at / updated_at, always UTC
``SoftDeleteMixin``      deleted_at - ONLY for records with no audit or
                         financial obligation (section 28)
``TenantMixin``          company_id, the tenant isolation boundary (section 40)
``ActorMixin``           who created/last changed the row
``OptimisticLockMixin``  version counter for lost-update protection
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from src.core.clock import utcnow
from src.core.ids import new_id
from src.core.types import GUID

# Deterministic constraint names. %(column_0_N_name)s truncates composite names
# so PostgreSQL's 63-character identifier limit is never silently hit.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Root declarative class. Every model in every module inherits this."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy API expects a plain dict
        uuid.UUID: GUID,
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


class UUIDPrimaryKeyMixin:
    """Time-ordered UUID primary key (section 28)."""

    id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=new_id,
        sort_order=-100,
    )


class TimestampMixin:
    """UTC creation/modification stamps maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
        sort_order=100,
    )
    # onupdate is deliberately PYTHON-side, not `func.now()`.
    #
    # A server-side onupdate makes SQLAlchemy expire the attribute after every
    # UPDATE, because only the database knows the new value. The next read of
    # `updated_at` - typically serialising the object into a response - then
    # needs a SELECT, and under asyncio that lazy load raises MissingGreenlet
    # instead of quietly issuing a query. Generating the value in the
    # application keeps it available immediately, and makes it freezable in
    # tests through src.core.clock.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
        sort_order=101,
    )


class SoftDeleteMixin:
    """Reversible deletion.

    Section 28 is explicit: soft deletion must never be used to hide an audit or
    financial record. Apply this mixin to master data (items, partners, users)
    where a row is referenced by history and therefore cannot be hard-deleted -
    never to ledger entries, audit rows or posted financial documents.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        index=True,
        sort_order=102,
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class TenantMixin:
    """Tenant isolation column (section 40).

    ``company_id`` is populated from the authenticated principal on the server.
    A client-supplied company id is never trusted; see
    :func:`src.core.deps.get_tenant_context`.
    """

    @declared_attr
    @classmethod
    def company_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            GUID,
            ForeignKey("companies.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            sort_order=-90,
        )


class ActorMixin:
    """Who created and last modified the row.

    ``ondelete="SET NULL"`` because users are deactivated rather than removed,
    but a hard delete must never cascade into business documents.
    """

    @declared_attr
    @classmethod
    def created_by_id(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(
            GUID,
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            sort_order=103,
        )

    @declared_attr
    @classmethod
    def updated_by_id(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(
            GUID,
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            sort_order=104,
        )


class OptimisticLockMixin:
    """Lost-update protection for records edited by several users.

    SQLAlchemy raises ``StaleDataError`` when the version in the UPDATE's WHERE
    clause no longer matches, which the error handler converts to HTTP 409.
    Use on documents people edit concurrently (BOM, purchase orders) - inventory
    correctness relies on row locks instead (see section 10).
    """

    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1", sort_order=105
    )

    @declared_attr.directive
    @classmethod
    def __mapper_args__(cls) -> dict[str, Any]:
        return {"version_id_col": cls.__table__.c.version}  # type: ignore[attr-defined]


# --- Convenience composites --------------------------------------------------


class TenantEntity(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, ActorMixin):
    """Standard tenant-owned business entity: id + company + stamps + actors."""

    __abstract__ = True


class TenantMasterEntity(TenantEntity, SoftDeleteMixin):
    """Tenant-owned master data that may be retired but never hard-deleted."""

    __abstract__ = True


class ImmutableLedgerEntity(Base, UUIDPrimaryKeyMixin, TenantMixin):
    """Append-only history row.

    No ``updated_at``, no ``deleted_at``, no version column - by construction
    these rows are written once and never mutated (sections 61, 82, 83).
    Corrections are expressed as a new, offsetting entry.
    """

    __abstract__ = True

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), sort_order=100
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, sort_order=101
    )


def tenant_unique(table_name: str, *columns: str, name: str | None = None) -> Index:
    """Build a tenant-scoped unique index.

    Business codes (item code, PO number, warehouse code) are unique *within a
    company*, never globally - a second tenant must be able to use "RM-001" too.
    """
    index_name = name or f"uq_{table_name}_company_{'_'.join(columns)}"
    return Index(index_name, "company_id", *columns, unique=True)


__all__ = [
    "NAMING_CONVENTION",
    "ActorMixin",
    "Base",
    "ImmutableLedgerEntity",
    "OptimisticLockMixin",
    "SoftDeleteMixin",
    "TenantEntity",
    "TenantMasterEntity",
    "TenantMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "tenant_unique",
]
