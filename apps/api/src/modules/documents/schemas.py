"""Request and response schemas for stored documents.

Three conventions carry security weight here rather than mere tidiness:

* **The read schema has no ``storage_key``.** The on-disk layout is an internal
  detail; publishing it hands an attacker the exact shape of the paths they
  would need to guess, and gains a client nothing - every byte is fetched
  through ``/documents/{id}/download``.
* **Nothing in the upload payload can influence a path.** ``category`` is a
  closed enum and everything else is metadata. The filename arrives on the
  multipart part, not in this model, and is sanitised for display only.
* **No ``company_id``.** It comes from the verified principal (section 40).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.clock import utcnow
from src.modules.documents.models import DocumentCategory

# ``entity_type`` is a stable snake_case module noun (``goods_receipt``,
# ``purchase_order``) that other modules pass when attaching a file. Constrained
# so it stays a usable index key and cannot become a free-text dumping ground.
ENTITY_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

#: Retention beyond this is not a temporary artefact any more; a caller wanting
#: permanent storage should not be setting an expiry at all.
MAX_RETENTION_DAYS = 365


class DocumentUploadRequest(BaseModel):
    """Multipart form fields accompanying the uploaded part.

    The file itself is bound separately by the route; this model validates only
    what the client asserts *about* it.
    """

    category: DocumentCategory = DocumentCategory.DOCUMENT
    entity_type: Annotated[str | None, Field(default=None, max_length=48)] = None
    entity_id: uuid.UUID | None = None
    is_public: bool = False
    #: Optional explicit retention. When omitted, the service applies the default
    #: for the category - see ``DEFAULT_RETENTION`` in the service module.
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> DocumentUploadRequest:
        entity_type = self.entity_type.strip().lower() if self.entity_type else None
        if entity_type == "":
            entity_type = None

        # Half a reference is worse than none: the file is filed against
        # something no query can express, so it is invisible for ever.
        if (entity_type is None) != (self.entity_id is None):
            raise ValueError("entity_type and entity_id must be supplied together.")
        if entity_type is not None and not ENTITY_TYPE_PATTERN.match(entity_type):
            raise ValueError("entity_type must be lowercase snake_case, e.g. 'goods_receipt'.")
        object.__setattr__(self, "entity_type", entity_type)

        if self.expires_at is not None:
            if self.expires_at.tzinfo is None:
                raise ValueError("expires_at must include a timezone offset.")
            now = utcnow()
            if self.expires_at <= now:
                raise ValueError("expires_at must be in the future.")
            if (self.expires_at - now).days > MAX_RETENTION_DAYS:
                raise ValueError(
                    f"expires_at cannot be more than {MAX_RETENTION_DAYS} days away; "
                    "omit it for a document that is kept indefinitely."
                )
        return self


class DocumentRead(BaseModel):
    """Document metadata as returned to a client.

    ``storage_key`` is absent on purpose - see the module docstring.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_name: str
    mime_type: str
    size_bytes: int
    checksum_sha256: str
    category: DocumentCategory
    entity_type: str | None
    entity_id: uuid.UUID | None
    uploaded_by_id: uuid.UUID | None
    is_public: bool
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DocumentFilters(BaseModel):
    """List filters.

    ``include_expired`` defaults to False so a lapsed export artefact disappears
    from every ordinary listing on its own, without waiting for the purge sweep
    to run.
    """

    search: Annotated[str | None, Field(default=None, max_length=255)] = None
    category: DocumentCategory | None = None
    entity_type: Annotated[str | None, Field(default=None, max_length=48)] = None
    entity_id: uuid.UUID | None = None
    mime_type: Annotated[str | None, Field(default=None, max_length=128)] = None
    uploaded_by_id: uuid.UUID | None = None
    is_public: bool | None = None
    include_expired: bool = False

    @model_validator(mode="after")
    def _validate(self) -> DocumentFilters:
        # Filtering by entity_id alone would ignore the leading columns of the
        # (company_id, entity_type, entity_id) index and scan the table, and it
        # is ambiguous besides: ids are unique but the pair is the addressable
        # unit everywhere else in the API.
        if self.entity_id is not None and not self.entity_type:
            raise ValueError("entity_id can only be used together with entity_type.")
        return self


__all__ = [
    "ENTITY_TYPE_PATTERN",
    "MAX_RETENTION_DAYS",
    "DocumentFilters",
    "DocumentRead",
    "DocumentUploadRequest",
]
