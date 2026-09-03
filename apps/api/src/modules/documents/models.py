"""Secure file-storage metadata (master rules sections 43, 44).

One table. The bytes live on disk under ``settings.storage_path``; this row is
the only index into them, and it is what makes a file tenant-owned, findable and
auditable.

Two columns carry the whole security design, and the split between them is the
point:

``file_name``   the *original* name, kept only so a download can be presented to
                a human under the name they recognise. It is never used to build
                a path, never trusted, and never round-tripped into a filesystem
                call. A file called ``../../etc/passwd`` is stored, displayed and
                served harmlessly because nothing ever concatenates it.

``storage_key`` the server-generated relative location,
                ``<dir>/<company_id>/<yyyy>/<mm>/<uuid7><ext>``. Every component
                is produced by the server: the directory comes from the category
                enum, the tenant from the verified principal, the date from the
                clock, the name from a fresh UUIDv7 and the extension from the
                *sniffed* content type. No byte of client input reaches it, which
                is why path traversal is structurally impossible here rather than
                merely filtered out.

``entity_id`` is a plain UUID with no foreign key, deliberately. A QC certificate
attached to a goods receipt must survive that receipt being archived or purged,
and a document that vanishes when its subject does is exactly the kind of
evidence loss an audit trail exists to prevent (the same reasoning as
``inventory_transactions.reference_id``, database contract section 4.2).

There is no soft delete. A retained row whose bytes have been unlinked is a lie:
every read of it 404s while the list still advertises it. Deletion removes the
row and the file together, and the audit log is what remembers it happened.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import TenantEntity
from src.core.types import GUID


class DocumentCategory(StrEnum):
    """What the file is for, and therefore where it is stored.

    A classification, not a permission: nothing reads this to decide whether a
    caller may see the file - that is what the tenant filter and the
    ``document.*`` permissions do. It selects the storage sub-tree and drives the
    default retention applied in :mod:`src.modules.documents.service`.
    """

    DOCUMENT = "DOCUMENT"
    REPORT = "REPORT"
    ATTACHMENT = "ATTACHMENT"
    EXPORT = "EXPORT"
    TEMP = "TEMP"


class Document(TenantEntity):
    """One stored file belonging to one company."""

    __tablename__ = "documents"

    #: Display only. Sanitised on upload (path components and control characters
    #: stripped) because it is echoed back in a ``Content-Disposition`` header.
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Server-generated relative path under ``settings.storage_path``. Never
    #: returned to a client - the read schema omits it - because the on-disk
    #: layout is not part of the API contract and leaking it invites probing.
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)

    #: The type the content was *verified* to be, not merely the type the client
    #: declared: an upload is rejected unless the leading magic bytes agree.
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)

    #: BigInteger rather than Integer: the 2 GB ceiling of a 32-bit column is a
    #: silent overflow waiting for the day someone raises the upload limit.
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: Lowercase hex SHA-256 of the stored bytes, computed while streaming. Lets
    #: a restored backup be proved identical to what was uploaded.
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    category: Mapped[DocumentCategory] = mapped_column(String(16), nullable=False)

    #: What the file is attached to, e.g. ``goods_receipt`` plus its id. Both
    #: NULL for a standalone upload; the check constraint below forbids half a
    #: pair, which would be an attachment nothing can ever find.
    entity_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    #: The domain-meaningful uploader, mandated by the database contract and
    #: indexed for "my uploads". It duplicates ``created_by_id`` from
    #: ``ActorMixin`` by design: both are written once at insert and document
    #: metadata is never edited afterwards, so the two cannot drift apart.
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Marks a file whose content is not confidential within the company. It does
    #: NOT relax authentication: every download served by this module requires a
    #: valid principal and is scoped to the tenant. See the service docstring.
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    #: Retention horizon for generated artefacts (exports, temp files). Past it
    #: the document reads as absent, and the purge sweep removes row and file.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Two rows pointing at one file would let deleting either one unlink the
        # bytes out from under the other.
        UniqueConstraint("company_id", "storage_key", name="uq_documents_company_id_storage_key"),
        # The attachment lookup: "everything filed against this goods receipt",
        # newest first. Also serves the entity_type-only prefix.
        Index(
            "ix_documents_company_entity",
            "company_id",
            "entity_type",
            "entity_id",
            text("created_at DESC"),
        ),
        Index("ix_documents_company_category", "company_id", "category", text("created_at DESC")),
        Index("ix_documents_company_uploaded_by", "company_id", "uploaded_by_id"),
        # Partial: only a minority of rows ever expire, so the sweep should scan
        # an index of just those rather than the whole table.
        Index(
            "ix_documents_expiring",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
        CheckConstraint(
            "(entity_type IS NULL) = (entity_id IS NULL)",
            name="entity_reference_complete",
        ),
        # Zero-byte uploads are rejected by the service; the constraint makes the
        # promise hold for any writer.
        CheckConstraint("size_bytes > 0", name="size_positive"),
        # A value that is not 64 lowercase hex characters is not a SHA-256, and a
        # wrong-shaped one silently defeats every integrity comparison.
        CheckConstraint("checksum_sha256 ~ '^[0-9a-f]{64}$'", name="checksum_is_sha256_hex"),
        # Belt and braces behind the service's path guard: even a direct SQL
        # insert cannot plant a key that escapes the storage root. chr(92) is a
        # backslash - written this way so no escaping convention has to be
        # trusted between Python, SQLAlchemy and PostgreSQL.
        CheckConstraint(
            "storage_key <> '' "
            "AND storage_key NOT LIKE '/%' "
            "AND position('..' in storage_key) = 0 "
            "AND position(chr(92) in storage_key) = 0",
            name="storage_key_is_relative",
        ),
    )


__all__ = ["Document", "DocumentCategory"]
