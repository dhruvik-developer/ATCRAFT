"""Request and response schemas for warehouse and storage-location master data.

Two conventions applied throughout:

* **Codes are normalised on the way in** (``src.common.codes.normalise_code``)
  so ``rm 001``, ``RM-001`` and ``rm001`` cannot become three warehouses that a
  storekeeper reads as one.
* **Updates use explicit "unset" semantics.** Every field on an update schema is
  optional, and the service applies only the keys the client actually sent
  (``model_dump(exclude_unset=True)``). Without that, a PATCH omitting
  ``manager_user_id`` would silently null it out.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.common.codes import strip_optional, validate_code
from src.modules.warehouses.models import StorageLocationType, WarehouseType

Code = Annotated[str, Field(min_length=1, max_length=32)]
Name = Annotated[str, Field(min_length=1, max_length=255)]


# -----------------------------------------------------------------------------
# Warehouse
# -----------------------------------------------------------------------------


class WarehouseAddress(BaseModel):
    """Address block, shared by create and update."""

    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4, description="GST state code")
    postal_code: str | None = Field(default=None, max_length=16)
    country: str = Field(default="India", max_length=80)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None

    _strip = field_validator(
        "address_line1", "address_line2", "city", "state", "state_code", "postal_code", "phone"
    )(strip_optional)


class WarehouseCreate(WarehouseAddress):
    """Payload for creating a warehouse.

    ``company_id`` is absent by design: it comes from the authenticated
    principal's tenant context and is never accepted from a client (section 40).
    """

    code: Code
    name: Name
    warehouse_type: WarehouseType
    branch_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    parent_warehouse_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    is_active: bool = True
    is_default: bool = False
    allow_negative_stock: bool = Field(
        default=False,
        description=(
            "Relaxes the inventory pre-flight availability check for this warehouse only. "
            "The database still forbids negative on-hand stock. Enable only for warehouses "
            "where receipts are legitimately posted after issues, such as line-side WIP."
        ),
    )
    notes: str | None = None

    _code = field_validator("code")(validate_code)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Name cannot be blank.")
        return cleaned


class WarehouseUpdate(BaseModel):
    """Partial update. Only the keys present in the request are applied.

    ``code`` is intentionally not updatable: it is printed on labels, quoted in
    document references and used by integrations, so renaming it in place would
    invalidate history that has already left the building. Retire the warehouse
    and create the replacement instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    warehouse_type: WarehouseType | None = None
    branch_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    parent_warehouse_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    allow_negative_stock: bool | None = None
    notes: str | None = None

    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4)
    postal_code: str | None = Field(default=None, max_length=16)
    country: str | None = Field(default=None, max_length=80)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None

    _strip = field_validator(
        "address_line1", "address_line2", "city", "state", "state_code", "postal_code", "phone"
    )(strip_optional)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Name cannot be blank.")
        return cleaned


class WarehouseRead(BaseModel):
    """Full warehouse representation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    warehouse_type: WarehouseType
    branch_id: uuid.UUID | None
    plant_id: uuid.UUID | None
    parent_warehouse_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None

    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    state_code: str | None
    postal_code: str | None
    country: str
    phone: str | None
    email: str | None

    is_active: bool
    is_default: bool
    allow_negative_stock: bool
    notes: str | None

    #: Live storage locations, batched in one query for the whole page - never a
    #: per-row lookup.
    location_count: int = 0

    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class WarehouseOption(BaseModel):
    """Slim shape for pickers and dropdowns.

    A dedicated projection rather than ``WarehouseRead``: the picker is called on
    nearly every document screen and does not need the address block.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    warehouse_type: WarehouseType
    is_default: bool


# -----------------------------------------------------------------------------
# Storage location
# -----------------------------------------------------------------------------


class StorageLocationCreate(BaseModel):
    """Payload for creating a storage location.

    ``warehouse_id`` comes from the path, not the body - a bin cannot be created
    into a warehouse other than the one addressed by the URL.
    """

    code: Code
    name: Name
    location_type: StorageLocationType
    parent_location_id: uuid.UUID | None = None
    is_active: bool = True
    capacity_quantity: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    capacity_uom: str | None = Field(default=None, max_length=16)
    max_weight_kg: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    max_volume_m3: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    barcode: str | None = Field(default=None, max_length=64)
    notes: str | None = None

    _code = field_validator("code")(validate_code)
    _strip = field_validator("capacity_uom", "barcode")(strip_optional)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Name cannot be blank.")
        return cleaned


class StorageLocationUpdate(BaseModel):
    """Partial update of a storage location. ``code`` is immutable, as for warehouses."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    location_type: StorageLocationType | None = None
    parent_location_id: uuid.UUID | None = None
    capacity_quantity: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    capacity_uom: str | None = Field(default=None, max_length=16)
    max_weight_kg: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    max_volume_m3: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    barcode: str | None = Field(default=None, max_length=64)
    notes: str | None = None

    _strip = field_validator("capacity_uom", "barcode")(strip_optional)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Name cannot be blank.")
        return cleaned


class StorageLocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    warehouse_id: uuid.UUID
    code: str
    name: str
    location_type: StorageLocationType
    parent_location_id: uuid.UUID | None
    is_active: bool
    capacity_quantity: Decimal | None
    capacity_uom: str | None
    max_weight_kg: Decimal | None
    max_volume_m3: Decimal | None
    barcode: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


# -----------------------------------------------------------------------------
# Filters and small command payloads
# -----------------------------------------------------------------------------


class WarehouseFilters(BaseModel):
    """List filters. Validated here so the repository never sees free text."""

    search: str | None = None
    warehouse_type: WarehouseType | None = None
    branch_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    parent_warehouse_id: uuid.UUID | None = None
    is_active: bool | None = None
    allow_negative_stock: bool | None = None
    include_deleted: bool = False


class StorageLocationFilters(BaseModel):
    search: str | None = None
    warehouse_id: uuid.UUID | None = None
    location_type: StorageLocationType | None = None
    parent_location_id: uuid.UUID | None = None
    is_active: bool | None = None
    include_deleted: bool = False


class StatusChangeRequest(BaseModel):
    """Reason capture for a lifecycle change.

    Required, not optional: section 83 is explicit that a change of state
    without a recorded reason is what makes an audit trail useless six months
    later, when nobody remembers why a warehouse was switched off.
    """

    reason: str = Field(min_length=3, max_length=255)


__all__ = [
    "StatusChangeRequest",
    "StorageLocationCreate",
    "StorageLocationFilters",
    "StorageLocationRead",
    "StorageLocationUpdate",
    "WarehouseAddress",
    "WarehouseCreate",
    "WarehouseFilters",
    "WarehouseOption",
    "WarehouseRead",
    "WarehouseUpdate",
]
