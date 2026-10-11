"""ORM builders for the master data every module's integration tests need.

A test that seeds another module's table with a hand-written ``INSERT`` states a
row shape it does not own. Most of the NOT NULL columns here default in Python
rather than in the database (``companies.country``, ``users.is_active``, every
``items.can_*`` flag), so the column list has to repeat them - and it silently
goes stale the moment the owning module adds one. Building the row through the
model asks that module what its rows look like instead.

Each builder flushes, so the returned instance has its identity, and takes
``**overrides`` for the columns a particular test actually cares about.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.ids import new_id
from src.modules.customers.models import Customer, CustomerType
from src.modules.inventory.models import StockSerial
from src.modules.organization.models import Company
from src.modules.products.models import (
    Item,
    ItemCategory,
    ItemGroup,
    UnitOfMeasure,
    UomCategory,
)
from src.modules.products.service import build_category_path
from src.modules.users.models import User
from src.modules.vendors.models import Vendor, VendorType
from src.modules.warehouses.models import Warehouse, WarehouseType


def _suffix() -> str:
    return uuid.uuid4().hex[:6].upper()


async def seed_company(
    session: AsyncSession,
    *,
    company_id: uuid.UUID | None = None,
    code: str | None = None,
    prefix: str = "ATC",
    name: str = "Atcraft",
    **overrides: Any,
) -> Company:
    """One tenant. ``code`` is unique across the database, so it is randomised."""
    record = Company(
        code=code or f"{prefix}{_suffix()}",
        name=name,
        **overrides,
    )
    if company_id is not None:
        record.id = company_id
    session.add(record)
    await session.flush()
    return record


async def seed_user(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    email: str | None = None,
    full_name: str = "Tester",
    **overrides: Any,
) -> User:
    """An actor for ``created_by_id``.

    The password hash is a placeholder: no module outside auth authenticates
    against these rows, and a real Argon2 hash per fixture user is measurable.
    """
    record = User(
        company_id=company_id,
        email=email or f"user-{uuid.uuid4().hex[:8]}@atcraft.test",
        full_name=full_name,
        password_hash="x",
        **overrides,
    )
    if user_id is not None:
        record.id = user_id
    session.add(record)
    await session.flush()
    return record


async def seed_uom(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    code: str | None = None,
    name: str = "Numbers",
    uom_category: UomCategory = UomCategory.COUNT,
    **overrides: Any,
) -> UnitOfMeasure:
    record = UnitOfMeasure(
        company_id=company_id,
        code=code or f"U{_suffix()}",
        name=name,
        uom_category=uom_category,
        **overrides,
    )
    session.add(record)
    await session.flush()
    return record


async def seed_item(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str | None = None,
    base_uom_id: uuid.UUID | None = None,
    item_group: ItemGroup = ItemGroup.RAW_MATERIAL,
    **overrides: Any,
) -> Item:
    """An item, with its own base unit unless one is supplied.

    A fresh unit per item is deliberate: ``units_of_measure`` allows one base
    unit per (company, category), and these fixtures never mark one, so nothing
    contends.
    """
    if base_uom_id is None:
        base_uom_id = (await seed_uom(session, company_id=company_id)).id
    record = Item(
        company_id=company_id,
        code=code,
        name=name or f"Item {code}",
        item_group=item_group,
        base_uom_id=base_uom_id,
        **overrides,
    )
    session.add(record)
    await session.flush()
    return record


async def seed_warehouse(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str | None = None,
    warehouse_type: WarehouseType = WarehouseType.RAW_MATERIAL,
    **overrides: Any,
) -> Warehouse:
    record = Warehouse(
        company_id=company_id,
        code=code,
        name=name or f"Store {code}",
        warehouse_type=warehouse_type,
        **overrides,
    )
    session.add(record)
    await session.flush()
    return record


async def seed_category(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    code: str | None = None,
    name: str | None = None,
    parent: ItemCategory | None = None,
    **overrides: Any,
) -> ItemCategory:
    """A category - a root, or a child of ``parent``.

    The materialised ``path`` is built by the products module's own rule rather
    than spelled here, so a test cannot seed a shape the ancestor queries would
    never produce. The id is minted up front because the path embeds it.
    """
    category_id = new_id()
    code = code or f"CAT-{_suffix()}"
    record = ItemCategory(
        id=category_id,
        company_id=company_id,
        code=code,
        name=name or code.title(),
        parent_id=parent.id if parent is not None else None,
        path=build_category_path(parent.path if parent is not None else None, category_id),
        depth=parent.depth + 1 if parent is not None else 0,
        **overrides,
    )
    session.add(record)
    await session.flush()
    return record


async def seed_customer(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    code: str | None = None,
    name: str | None = None,
    customer_type: CustomerType = CustomerType.CUSTOMER,
    blocked: bool = False,
    **overrides: Any,
) -> Customer:
    """A customer, optionally on credit hold.

    ``blocked`` writes the reason and the timestamp together: the table's own
    CHECK constraints refuse a hold with either missing, so a test that wants
    one cannot flip the flag and patch the rest in afterwards.
    """
    code = code or f"CUST-{_suffix()}"
    if blocked:
        overrides.setdefault("is_blocked", True)
        overrides.setdefault("block_reason", "Credit hold")
        overrides.setdefault("blocked_at", utcnow())
    record = Customer(
        company_id=company_id,
        code=code,
        name=name or f"Customer {code}",
        customer_type=customer_type,
        **overrides,
    )
    session.add(record)
    await session.flush()
    return record


async def seed_vendor(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    code: str | None = None,
    name: str | None = None,
    vendor_types: tuple[VendorType, ...] = (VendorType.RAW_MATERIAL_SUPPLIER,),
    **overrides: Any,
) -> Vendor:
    code = code or f"VEND-{_suffix()}"
    record = Vendor(
        company_id=company_id,
        code=code,
        name=name or f"Vendor {code}",
        vendor_types=[str(kind) for kind in vendor_types],
        **overrides,
    )
    session.add(record)
    await session.flush()
    return record


async def seed_serial(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID | None = None,
    serial_number: str | None = None,
    **overrides: Any,
) -> StockSerial:
    """One serial number, in stock at ``warehouse_id`` unless told otherwise.

    This is a *fixture* for modules that consume serials (packing, dispatch);
    inventory's own tests mint theirs through ``post_movement`` because that is
    the only path production takes.
    """
    record = StockSerial(
        company_id=company_id,
        item_id=item_id,
        warehouse_id=warehouse_id,
        serial_number=serial_number or f"SN-{_suffix()}",
        **overrides,
    )
    session.add(record)
    await session.flush()
    return record
