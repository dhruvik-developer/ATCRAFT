"""Warehouse and storage-location master data (master rules section 9).

Two levels, deliberately separate:

``warehouses``          a stock-holding entity. Every inventory movement names
                        one, and valuation, availability and reservation are all
                        computed per warehouse.
``storage_locations``   a physical position *inside* a warehouse (rack, bin,
                        shelf, floor area, staging lane). Optional on a movement
                        - a company that does not run bin-level control simply
                        never creates any.

Both are hierarchical. A warehouse may have a parent so a plant store can own
logical sub-stores (line-side WIP, QC hold) that roll up for reporting while
still being addressable individually by a movement. A location may have a parent
so ``RACK-A`` can own ``RACK-A/BIN-03``.

Neither table stores a quantity. On-hand, reserved and available live in
``inventory_balances`` (section 4 of the database contract) and are derived from
the ledger; duplicating them here would create a second, silently diverging
truth.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.models import TenantMasterEntity
from src.core.types import GUID, Quantity


class WarehouseType(StrEnum):
    """The stock-holding roles a warehouse can play (master rules section 9).

    This is a *classification*, not a permission: no rule reads it to decide
    whether a movement is legal - that is what the item capability flags and the
    stock-state vocabulary are for. It drives defaults (where a GRN proposes to
    receive), reporting groups, and the shop-floor pickers.
    """

    RAW_MATERIAL = "RAW_MATERIAL"
    COMPONENT = "COMPONENT"
    WIP = "WIP"
    ASSEMBLY = "ASSEMBLY"
    FINISHED_GOODS = "FINISHED_GOODS"
    PACKING = "PACKING"
    SCRAP = "SCRAP"
    RETURN = "RETURN"
    QC_HOLD = "QC_HOLD"
    JOB_WORK = "JOB_WORK"
    IN_TRANSIT = "IN_TRANSIT"
    CONSUMABLES = "CONSUMABLES"


class StorageLocationType(StrEnum):
    """Physical shape of a storage position."""

    RACK = "RACK"
    BIN = "BIN"
    SHELF = "SHELF"
    FLOOR = "FLOOR"
    STAGING = "STAGING"


class Warehouse(TenantMasterEntity):
    """A stock-holding entity within one company."""

    __tablename__ = "warehouses"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    warehouse_type: Mapped[WarehouseType] = mapped_column(String(24), nullable=False)

    # Organisational placement. RESTRICT rather than CASCADE: removing a branch
    # must never silently take its stock-holding warehouses with it.
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    plant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("plants.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # Logical sub-stores roll up to a parent for reporting while remaining
    # individually addressable by a movement.
    parent_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # SET NULL: a manager who leaves must not block deletion of their user row,
    # and an unmanaged warehouse is a reportable condition, not a broken one.
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # GST place-of-supply code: a stock transfer between two states is an
    # invoiceable movement in India, so the state must be known per warehouse.
    state_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    country: Mapped[str] = mapped_column(String(80), nullable=False, default="India")
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Proposed by documents that do not name a warehouse explicitly. At most one
    #: per company - enforced by a partial unique index below.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Defaults to FALSE, and turning it on is an explicit business decision that
    # someone signs off on, because it weakens a safety net.
    #
    # What it does NOT do: it does not permit negative on-hand stock. The check
    # constraint on inventory_balances (on_hand_quantity >= 0, database contract
    # section 4.3) still forbids that, and no flag on this table can relax a
    # database constraint on another one.
    #
    # What it does: it relaxes the inventory engine's *pre-flight* service check
    # for this warehouse, so an issue may be posted before the matching receipt
    # has been keyed. That is the real shop-floor sequence for line-side WIP -
    # material is consumed at the machine and the receipt from the previous
    # operation is booked minutes later. Everywhere else the pre-flight check is
    # the thing that stops a typo becoming a stock-out at 2am, so it stays on.
    allow_negative_stock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # lazy="raise": a list endpoint that touches these per row is an N+1, and it
    # must fail in development rather than quietly issue 100 queries (section 32).
    parent: Mapped[Warehouse | None] = relationship(
        remote_side="Warehouse.id", back_populates="children", lazy="raise"
    )
    children: Mapped[list[Warehouse]] = relationship(back_populates="parent", lazy="raise")
    locations: Mapped[list[StorageLocation]] = relationship(
        back_populates="warehouse", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_warehouses_company_id_code"),
        # Covers the default list ("active warehouses of this company") and the
        # type-filtered picker used by receipt and issue screens.
        Index("ix_warehouses_company_active", "company_id", "is_active"),
        Index("ix_warehouses_company_type", "company_id", "warehouse_type"),
        # Partial unique: exactly one live default per company. Expressed in the
        # database because "we always clear the old one first" is a promise the
        # application can break under concurrency, and this cannot.
        Index(
            "uq_warehouses_company_default",
            "company_id",
            unique=True,
            postgresql_where=text("is_default AND deleted_at IS NULL"),
        ),
        CheckConstraint(
            "parent_warehouse_id IS NULL OR parent_warehouse_id <> id",
            name="parent_not_self",
        ),
    )


class StorageLocation(TenantMasterEntity):
    """A position inside a warehouse: rack, bin, shelf, floor area, staging lane."""

    __tablename__ = "storage_locations"

    # CASCADE is wrong here and RESTRICT is right: a warehouse that still has
    # locations is retired, never erased, so its bins keep pointing at it.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    location_type: Mapped[StorageLocationType] = mapped_column(String(16), nullable=False)
    parent_location_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("storage_locations.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Capacity is advisory: it feeds put-away suggestions and an over-capacity
    # report, and is never a hard block on a movement - refusing to receive
    # material that has physically arrived would make the system disagree with
    # the floor, which is the one thing stock control must never do.
    capacity_quantity: Mapped[Decimal | None] = mapped_column(Quantity, nullable=True)
    #: UoM symbol the capacity is expressed in. A plain string rather than an FK
    #: to units_of_measure: capacity is a shelf-labelling aid ("40 boxes") that
    #: is never converted or arithmetically combined with item quantities.
    capacity_uom: Mapped[str | None] = mapped_column(String(16), nullable=True)
    max_weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    max_volume_m3: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    warehouse: Mapped[Warehouse] = relationship(back_populates="locations", lazy="raise")
    parent: Mapped[StorageLocation | None] = relationship(
        remote_side="StorageLocation.id", back_populates="children", lazy="raise"
    )
    children: Mapped[list[StorageLocation]] = relationship(back_populates="parent", lazy="raise")

    __table_args__ = (
        # Scoped to the warehouse, not the company: "BIN-01" exists in every
        # warehouse and forcing globally distinct bin codes would be unusable.
        UniqueConstraint(
            "company_id",
            "warehouse_id",
            "code",
            name="uq_storage_locations_company_id_warehouse_id_code",
        ),
        Index("ix_storage_locations_warehouse_active", "company_id", "warehouse_id", "is_active"),
        Index("ix_storage_locations_barcode", "company_id", "barcode"),
        CheckConstraint(
            "parent_location_id IS NULL OR parent_location_id <> id",
            name="parent_not_self",
        ),
        CheckConstraint(
            "capacity_quantity IS NULL OR capacity_quantity >= 0", name="capacity_non_negative"
        ),
        CheckConstraint(
            "max_weight_kg IS NULL OR max_weight_kg >= 0", name="max_weight_non_negative"
        ),
        CheckConstraint(
            "max_volume_m3 IS NULL OR max_volume_m3 >= 0", name="max_volume_non_negative"
        ),
    )


__all__ = ["StorageLocation", "StorageLocationType", "Warehouse", "WarehouseType"]
