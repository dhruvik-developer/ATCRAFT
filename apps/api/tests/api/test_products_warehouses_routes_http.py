"""Products and warehouses write routes over HTTP.

Service rules for categories, attributes, UOMs, items, variants, prices, vendor
links, warehouses and bins are pinned in the integration suites listed in the
products-warehouses campaign brief. These drive the twenty-six write routes no
backend test had reached through HTTP: the status and envelope each answers
with, the row and audit entry left behind, the 422 a payload or a record's
state can earn, and the 404 another company's record answers with - never a
403, which would confirm that it exists.

Permission gates on the route itself are covered by
``tests/unit/test_route_authorization.py``. Brand create/delete, the
``code``-smuggling PATCH, and the in-use retirement shape live in
``test_brand_manufacturer_http.py`` - this file covers the PATCH success path
those suites leave untouched, and every other write listed in the brief.

The caller is the ``api_client`` principal - every permission, not a
superuser - except where a nested right (``price.manage`` on a channel-price
edit) must be stripped. Prerequisites are built through the products and
warehouses services (or ``seed_support`` for plain item/vendor rows), so every
fixture has the shape production gives it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext, get_current_principal
from src.core.permissions import Permission
from src.main import app
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.documents.models import Document, DocumentCategory
from src.modules.organization.models import Branch
from src.modules.products import service as products_service
from src.modules.products.models import (
    Brand,
    Item,
    ItemAttribute,
    ItemAttributeType,
    ItemCategory,
    ItemGroup,
    ItemPrice,
    ItemVariant,
    ItemVendorLink,
    Manufacturer,
    PriceType,
    UnitOfMeasure,
    UomCategory,
    UomConversion,
)
from src.modules.products.schemas import (
    BrandCreate,
    BrandOut,
    CategoryCreate,
    CategoryOut,
    ItemAttributeCreate,
    ItemAttributeOut,
    ItemCreate,
    ManufacturerCreate,
    ManufacturerOut,
    PriceCreate,
    PriceOut,
    UomConversionCreate,
    UomCreate,
    UomOut,
    VariantCreate,
    VariantOut,
    VendorLinkCreate,
    VendorLinkOut,
)
from src.modules.warehouses.models import StorageLocation, Warehouse
from src.modules.warehouses.schemas import (
    StorageLocationCreate,
    StorageLocationRead,
    WarehouseCreate,
    WarehouseRead,
)
from src.modules.warehouses.service import StorageLocationService, WarehouseService
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(48)


# -----------------------------------------------------------------------------
# Callers, builders and readers
# -----------------------------------------------------------------------------


def act_as(principal: Principal) -> None:
    app.dependency_overrides[get_current_principal] = lambda: principal


def holding(base: Principal, *permissions: Permission) -> Principal:
    """The same person holding exactly these permissions."""
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
        role_codes=base.role_codes,
    )


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    """Scope for building another company's masters, through its own actor."""
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


def ok(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def refused(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code, body["error"]
    return body["error"]


def field_errors(response: Response) -> dict[str, list[str]]:
    return refused(response, 422, "VALIDATION_ERROR")["details"]["fields"]


async def audit_rows(
    session: AsyncSession,
    company_id: uuid.UUID,
    entity_id: uuid.UUID,
    action: AuditAction,
) -> list[Row[Any]]:
    stmt = (
        select(
            AuditLog.entity_type,
            AuditLog.created_by_id,
            AuditLog.old_values,
            AuditLog.new_values,
            AuditLog.changed_fields,
            AuditLog.context,
        )
        .where(
            AuditLog.company_id == company_id,
            AuditLog.entity_id == entity_id,
            AuditLog.action == action,
        )
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.execute(stmt)).all())


async def column(session: AsyncSession, attribute: Any, row_id: uuid.UUID) -> Any:
    """One stored value, read past the identity map."""
    model = attribute.class_
    return await session.scalar(select(attribute).where(model.id == row_id))


async def count_codes(session: AsyncSession, model: Any, company_id: uuid.UUID, code: str) -> int:
    stmt = (
        select(func.count())
        .select_from(model)
        .where(model.company_id == company_id, model.code == code)
    )
    return int((await session.execute(stmt)).scalar_one())


async def make_category(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> CategoryOut:
    return await products_service.create_category(
        session, tenant, CategoryCreate(code=code, name="Category", **fields)
    )


async def make_attribute(
    session: AsyncSession,
    tenant: TenantContext,
    code: str,
    *,
    attribute: ItemAttributeType = ItemAttributeType.FINISH,
    **fields: Any,
) -> ItemAttributeOut:
    return await products_service.create_item_attribute(
        session,
        tenant,
        ItemAttributeCreate(attribute=attribute, code=code, name=code.title(), **fields),
    )


async def make_manufacturer(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> ManufacturerOut:
    return await products_service.create_manufacturer(
        session, tenant, ManufacturerCreate(code=code, name="Maker", **fields)
    )


async def make_brand(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> BrandOut:
    return await products_service.create_brand(
        session, tenant, BrandCreate(code=code, name="Brand", **fields)
    )


async def make_uom(
    session: AsyncSession,
    tenant: TenantContext,
    code: str,
    *,
    uom_category: UomCategory = UomCategory.COUNT,
    **fields: Any,
) -> UomOut:
    return await products_service.create_uom(
        session,
        tenant,
        UomCreate(code=code, name=code.title(), uom_category=uom_category, **fields),
    )


async def make_item(session: AsyncSession, tenant: TenantContext, code: str, **fields: Any) -> Item:
    base_uom_id = fields.pop("base_uom_id", None)
    if base_uom_id is None:
        base_uom_id = (await make_uom(session, tenant, f"U{uuid.uuid4().hex[:6].upper()}")).id
    return await products_service.create_item(
        session,
        tenant,
        ItemCreate(
            code=code,
            name=fields.pop("name", f"Item {code}"),
            item_group=fields.pop("item_group", ItemGroup.COMPONENT),
            base_uom_id=base_uom_id,
            **fields,
        ),
    )


async def make_variant(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    code: str,
    **fields: Any,
) -> VariantOut:
    if "finish_id" not in fields and "size_id" not in fields and "colour_id" not in fields:
        finish = await make_attribute(session, tenant, f"F{uuid.uuid4().hex[:6].upper()}")
        fields["finish_id"] = finish.id
    return await products_service.create_variant(
        session,
        tenant,
        item_id,
        VariantCreate(code=code, name=fields.pop("name", code), **fields),
        include_cost=True,
    )


async def make_price(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    *,
    price: str = "100",
    price_type: PriceType = PriceType.SALE,
    **fields: Any,
) -> PriceOut:
    return await products_service.create_price(
        session,
        tenant,
        item_id,
        PriceCreate(
            price_type=price_type,
            price=Decimal(price),
            valid_from=fields.pop("valid_from", today()),
            **fields,
        ),
    )


async def make_vendor_link(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    vendor_id: uuid.UUID,
    **fields: Any,
) -> VendorLinkOut:
    return await products_service.link_vendor(
        session, tenant, item_id, VendorLinkCreate(vendor_id=vendor_id, **fields)
    )


async def make_warehouse(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> WarehouseRead:
    return await WarehouseService(session, tenant).create_warehouse(
        WarehouseCreate(
            code=code,
            name=fields.pop("name", f"Store {code}"),
            warehouse_type=fields.pop("warehouse_type", "RAW_MATERIAL"),
            **fields,
        )
    )


async def make_location(
    session: AsyncSession,
    tenant: TenantContext,
    warehouse_id: uuid.UUID,
    code: str,
    **fields: Any,
) -> StorageLocationRead:
    return await StorageLocationService(session, tenant).create_location(
        warehouse_id,
        StorageLocationCreate(
            code=code,
            name=fields.pop("name", f"Bin {code}"),
            location_type=fields.pop("location_type", "BIN"),
            **fields,
        ),
    )


async def item_image(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, *, mime: str = "image/png"
) -> Document:
    document = Document(
        company_id=tenant.company_id,
        file_name="cover.png",
        storage_key=f"test/{uuid.uuid4()}.png",
        mime_type=mime,
        size_bytes=len(PNG),
        checksum_sha256="0" * 64,
        category=DocumentCategory.ATTACHMENT,
        uploaded_by_id=tenant.user_id,
        entity_type="item",
        entity_id=item_id,
    )
    session.add(document)
    await session.flush()
    return document


# -----------------------------------------------------------------------------
# POST /products/categories
# -----------------------------------------------------------------------------


class TestCreateCategory:
    async def test_a_category_is_created_normalised_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("cat").lower()

        response = await api_client.post(
            "/products/categories",
            json={"code": f"  {raw} ", "name": " Door Hardware ", "description": "Locks"},
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "Door Hardware"
        assert data["depth"] == 0
        assert data["parent_id"] is None
        category_id = uuid.UUID(data["id"])
        assert await column(session, ItemCategory.company_id, category_id) == tenant.company_id
        (created,) = await audit_rows(session, tenant.company_id, category_id, AuditAction.CREATE)
        assert created.entity_type == "item_category"
        assert created.created_by_id == tenant.user_id
        assert created.new_values["code"] == data["code"]
        read = ok(await api_client.get(f"/products/categories/{category_id}"))
        assert read["code"] == data["code"]

    async def test_a_child_under_another_companys_parent_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_category(session, foreign, unique_code("CAT"))
        code = unique_code("CAT")

        response = await api_client.post(
            "/products/categories",
            json={"code": code, "name": "Borrowed", "parent_id": str(theirs.id)},
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await count_codes(session, ItemCategory, tenant.company_id, code) == 0

    async def test_a_duplicate_code_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_category(session, tenant, unique_code("CAT"))

        response = await api_client.post(
            "/products/categories", json={"code": existing.code.lower(), "name": "Twin"}
        )

        assert refused(response, 409, "DUPLICATE_DOCUMENT")
        assert await count_codes(session, ItemCategory, tenant.company_id, existing.code) == 1

    async def test_a_category_needs_a_code_and_a_name(self, api_client: AsyncClient) -> None:
        response = await api_client.post("/products/categories", json={"description": "x"})

        assert {"code", "name"} <= set(field_errors(response))


# -----------------------------------------------------------------------------
# PATCH /products/categories/{category_id}
# -----------------------------------------------------------------------------


class TestUpdateCategory:
    async def test_an_edit_applies_and_audits_the_difference(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        category = await make_category(session, tenant, unique_code("CAT"), description="Old")

        response = await api_client.patch(
            f"/products/categories/{category.id}",
            json={"name": "Hinges", "description": "Butt hinges"},
        )

        data = ok(response)
        assert data["name"] == "Hinges"
        assert data["description"] == "Butt hinges"
        assert await column(session, ItemCategory.name, category.id) == "Hinges"
        (update,) = await audit_rows(session, tenant.company_id, category.id, AuditAction.UPDATE)
        assert update.entity_type == "item_category"
        assert update.new_values["name"] == "Hinges"

    async def test_a_category_cannot_move_under_its_descendant(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_category(session, tenant, unique_code("CAT"))
        child = await make_category(session, tenant, unique_code("CAT"), parent_id=parent.id)

        response = await api_client.patch(
            f"/products/categories/{parent.id}", json={"parent_id": str(child.id)}
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, ItemCategory.parent_id, parent.id) is None

    async def test_another_companys_category_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_category(session, foreign, unique_code("CAT"))

        response = await api_client.patch(
            f"/products/categories/{theirs.id}", json={"name": "Taken over"}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, ItemCategory.name, theirs.id) == "Category"


# -----------------------------------------------------------------------------
# POST /products/attributes
# -----------------------------------------------------------------------------


class TestCreateItemAttribute:
    async def test_an_attribute_is_created_normalised_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("fin").lower()

        response = await api_client.post(
            "/products/attributes",
            json={
                "attribute": "FINISH",
                "code": f"  {raw} ",
                "name": " Satin Brass ",
                "sort_order": 10,
            },
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "Satin Brass"
        assert data["attribute"] == "FINISH"
        attribute_id = uuid.UUID(data["id"])
        assert await column(session, ItemAttribute.company_id, attribute_id) == tenant.company_id
        (created,) = await audit_rows(session, tenant.company_id, attribute_id, AuditAction.CREATE)
        assert created.entity_type == "item_attribute"
        assert created.new_values["code"] == data["code"]

    async def test_the_same_code_twice_for_one_kind_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_attribute(session, tenant, unique_code("FIN"))

        response = await api_client.post(
            "/products/attributes",
            json={"attribute": "FINISH", "code": existing.code.lower(), "name": "Twin"},
        )

        assert refused(response, 409, "DUPLICATE_DOCUMENT")

    async def test_a_finish_and_a_colour_may_share_a_code(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        code = unique_code("CLR")
        await make_attribute(session, tenant, code, attribute=ItemAttributeType.FINISH)

        response = await api_client.post(
            "/products/attributes",
            json={"attribute": "COLOUR", "code": code, "name": "Shared"},
        )

        data = ok(response, 201)
        assert data["attribute"] == "COLOUR"
        assert data["code"] == code

    async def test_attribute_kind_and_code_are_required(self, api_client: AsyncClient) -> None:
        response = await api_client.post("/products/attributes", json={"name": "Missing"})

        assert {"attribute", "code"} <= set(field_errors(response))


# -----------------------------------------------------------------------------
# PATCH /products/attributes/{attribute_id}
# -----------------------------------------------------------------------------


class TestUpdateItemAttribute:
    async def test_renaming_is_free_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        attribute = await make_attribute(session, tenant, unique_code("FIN"))

        response = await api_client.patch(
            f"/products/attributes/{attribute.id}",
            json={"name": "Antique Brass", "sort_order": 5},
        )

        data = ok(response)
        assert data["name"] == "Antique Brass"
        assert data["sort_order"] == 5
        (update,) = await audit_rows(session, tenant.company_id, attribute.id, AuditAction.UPDATE)
        assert update.entity_type == "item_attribute"
        assert update.new_values["name"] == "Antique Brass"

    async def test_retiring_an_attribute_an_item_carries_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        attribute = await make_attribute(session, tenant, unique_code("FIN"))
        await make_item(session, tenant, unique_code("IT"), finish_id=attribute.id)

        response = await api_client.patch(
            f"/products/attributes/{attribute.id}", json={"is_active": False}
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, ItemAttribute.is_active, attribute.id) is True

    async def test_another_companys_attribute_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_attribute(session, foreign, unique_code("FIN"))

        response = await api_client.patch(
            f"/products/attributes/{theirs.id}", json={"name": "Taken"}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, ItemAttribute.name, theirs.id) == theirs.name


# -----------------------------------------------------------------------------
# PATCH /products/brands/{brand_id}
# -----------------------------------------------------------------------------


class TestUpdateBrand:
    async def test_an_edit_applies_only_what_was_sent_and_audits(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        brand = await make_brand(
            session, tenant, unique_code("BR"), description="Line one", sort_order=1
        )

        response = await api_client.patch(
            f"/products/brands/{brand.id}",
            json={"name": "Atcraft Pro", "description": "Flagship"},
        )

        data = ok(response)
        assert data["name"] == "Atcraft Pro"
        assert data["description"] == "Flagship"
        assert data["sort_order"] == 1
        assert await column(session, Brand.name, brand.id) == "Atcraft Pro"
        (update,) = await audit_rows(session, tenant.company_id, brand.id, AuditAction.UPDATE)
        assert update.entity_type == "brand"
        assert update.new_values["name"] == "Atcraft Pro"

    async def test_switching_a_brand_off_is_also_a_status_change(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        brand = await make_brand(session, tenant, unique_code("BR"))

        response = await api_client.patch(f"/products/brands/{brand.id}", json={"is_active": False})

        assert ok(response)["is_active"] is False
        assert await column(session, Brand.is_active, brand.id) is False
        (change,) = await audit_rows(
            session, tenant.company_id, brand.id, AuditAction.STATUS_CHANGE
        )
        assert change.old_values == {"status": "ACTIVE"}
        assert change.new_values == {"status": "INACTIVE"}

    async def test_a_foreign_manufacturer_cannot_own_the_brand(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        brand = await make_brand(session, tenant, unique_code("BR"))
        theirs = await make_manufacturer(session, foreign, unique_code("MK"))

        response = await api_client.patch(
            f"/products/brands/{brand.id}", json={"manufacturer_id": str(theirs.id)}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, Brand.manufacturer_id, brand.id) is None

    async def test_an_empty_patch_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        brand = await make_brand(session, tenant, unique_code("BR"))

        response = await api_client.patch(f"/products/brands/{brand.id}", json={})

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert not await audit_rows(session, tenant.company_id, brand.id, AuditAction.UPDATE)

    async def test_another_companys_brand_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_brand(session, foreign, unique_code("BR"))

        response = await api_client.patch(
            f"/products/brands/{theirs.id}", json={"name": "Taken over"}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, Brand.name, theirs.id) == "Brand"


# -----------------------------------------------------------------------------
# PATCH /products/manufacturers/{manufacturer_id}
# -----------------------------------------------------------------------------


class TestUpdateManufacturer:
    async def test_an_edit_applies_and_audits(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        maker = await make_manufacturer(
            session, tenant, unique_code("MK"), website="https://a.test"
        )

        response = await api_client.patch(
            f"/products/manufacturers/{maker.id}",
            json={"name": "Spring Works", "website": "https://springs.test", "legal_name": None},
        )

        data = ok(response)
        assert data["name"] == "Spring Works"
        assert data["website"] == "https://springs.test"
        assert data["legal_name"] is None
        (update,) = await audit_rows(session, tenant.company_id, maker.id, AuditAction.UPDATE)
        assert update.entity_type == "manufacturer"
        assert update.new_values["name"] == "Spring Works"

    async def test_an_empty_patch_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        maker = await make_manufacturer(session, tenant, unique_code("MK"))

        response = await api_client.patch(f"/products/manufacturers/{maker.id}", json={})

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_another_companys_manufacturer_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_manufacturer(session, foreign, unique_code("MK"))

        response = await api_client.patch(
            f"/products/manufacturers/{theirs.id}", json={"name": "Taken"}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, Manufacturer.name, theirs.id) == "Maker"


# -----------------------------------------------------------------------------
# POST /products/uoms and PATCH /products/uoms/{uom_id}
# -----------------------------------------------------------------------------


class TestCreateUom:
    async def test_a_unit_is_created_normalised_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("nos").lower()

        response = await api_client.post(
            "/products/uoms",
            json={
                "code": f"  {raw} ",
                "name": " Numbers ",
                "uom_category": "COUNT",
                "is_base": True,
            },
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "Numbers"
        assert data["is_base"] is True
        uom_id = uuid.UUID(data["id"])
        assert await column(session, UnitOfMeasure.company_id, uom_id) == tenant.company_id
        (created,) = await audit_rows(session, tenant.company_id, uom_id, AuditAction.CREATE)
        assert created.entity_type == "unit_of_measure"
        assert created.new_values["is_base"] is True

    async def test_a_second_base_unit_for_a_category_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        await make_uom(session, tenant, unique_code("NOS"), is_base=True)

        response = await api_client.post(
            "/products/uoms",
            json={
                "code": unique_code("PCS"),
                "name": "Pieces",
                "uom_category": "COUNT",
                "is_base": True,
            },
        )

        error = refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert "existing_base_uom_id" in error["details"]

    async def test_a_duplicate_code_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_uom(session, tenant, unique_code("BOX"))

        response = await api_client.post(
            "/products/uoms",
            json={"code": existing.code.lower(), "name": "Twin", "uom_category": "COUNT"},
        )

        assert refused(response, 409, "DUPLICATE_DOCUMENT")

    async def test_category_and_code_are_required(self, api_client: AsyncClient) -> None:
        response = await api_client.post("/products/uoms", json={"name": "Loose"})

        assert {"code", "uom_category"} <= set(field_errors(response))


class TestUpdateUom:
    async def test_a_rename_is_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        uom = await make_uom(session, tenant, unique_code("BOX"))

        response = await api_client.patch(
            f"/products/uoms/{uom.id}", json={"name": "Carton", "decimal_places": 0}
        )

        data = ok(response)
        assert data["name"] == "Carton"
        assert data["decimal_places"] == 0
        (update,) = await audit_rows(session, tenant.company_id, uom.id, AuditAction.UPDATE)
        assert update.entity_type == "unit_of_measure"
        assert update.new_values["name"] == "Carton"

    async def test_a_unit_used_by_items_cannot_be_switched_off(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        uom = await make_uom(session, tenant, unique_code("NOS"))
        await make_item(session, tenant, unique_code("IT"), base_uom_id=uom.id)

        response = await api_client.patch(f"/products/uoms/{uom.id}", json={"is_active": False})

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, UnitOfMeasure.is_active, uom.id) is True

    async def test_clearing_the_base_flag_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        uom = await make_uom(session, tenant, unique_code("NOS"), is_base=True)

        response = await api_client.patch(f"/products/uoms/{uom.id}", json={"is_base": False})

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, UnitOfMeasure.is_base, uom.id) is True

    async def test_another_companys_uom_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_uom(session, foreign, unique_code("NOS"))

        response = await api_client.patch(f"/products/uoms/{theirs.id}", json={"name": "Taken"})

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, UnitOfMeasure.name, theirs.id) == theirs.name


# -----------------------------------------------------------------------------
# POST /products/uom-conversions
# -----------------------------------------------------------------------------


class TestCreateConversion:
    async def test_a_conversion_is_created_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        nos = await make_uom(session, tenant, unique_code("NOS"), is_base=True)
        box = await make_uom(session, tenant, unique_code("BOX"))

        response = await api_client.post(
            "/products/uom-conversions",
            json={"from_uom_id": str(box.id), "to_uom_id": str(nos.id), "factor": "24"},
        )

        data = ok(response, 201)
        assert data["from_uom_id"] == str(box.id)
        assert data["to_uom_id"] == str(nos.id)
        assert Decimal(data["factor"]) == Decimal("24")
        conversion_id = uuid.UUID(data["id"])
        (created,) = await audit_rows(session, tenant.company_id, conversion_id, AuditAction.CREATE)
        assert created.entity_type == "uom_conversion"
        assert Decimal(str(created.new_values["factor"])) == Decimal("24")

    async def test_cross_category_and_self_conversions_are_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        nos = await make_uom(session, tenant, unique_code("NOS"), is_base=True)
        kg = await make_uom(
            session, tenant, unique_code("KG"), uom_category=UomCategory.WEIGHT, is_base=True
        )

        cross = await api_client.post(
            "/products/uom-conversions",
            json={"from_uom_id": str(nos.id), "to_uom_id": str(kg.id), "factor": "1"},
        )
        assert refused(cross, 422, "BUSINESS_RULE_VIOLATION")

        same = await api_client.post(
            "/products/uom-conversions",
            json={"from_uom_id": str(nos.id), "to_uom_id": str(nos.id), "factor": "1"},
        )
        assert "from_uom_id" in field_errors(same) or field_errors(same)

    async def test_another_companys_unit_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        ours = await make_uom(session, tenant, unique_code("NOS"), is_base=True)
        theirs = await make_uom(session, foreign, unique_code("BOX"))

        response = await api_client.post(
            "/products/uom-conversions",
            json={"from_uom_id": str(theirs.id), "to_uom_id": str(ours.id), "factor": "12"},
        )

        assert refused(response, 404, "NOT_FOUND")
        assert (
            await session.scalar(
                select(func.count())
                .select_from(UomConversion)
                .where(UomConversion.company_id == tenant.company_id)
            )
        ) == 0

    async def test_a_reverse_pair_is_a_duplicate(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        nos = await make_uom(session, tenant, unique_code("NOS"), is_base=True)
        box = await make_uom(session, tenant, unique_code("BOX"))
        await products_service.create_conversion(
            session,
            tenant,
            UomConversionCreate(from_uom_id=box.id, to_uom_id=nos.id, factor=Decimal("12")),
        )

        response = await api_client.post(
            "/products/uom-conversions",
            json={"from_uom_id": str(nos.id), "to_uom_id": str(box.id), "factor": "0.0833"},
        )

        assert refused(response, 409, "DUPLICATE_DOCUMENT")


# -----------------------------------------------------------------------------
# PATCH /products/items/{item_id}
# -----------------------------------------------------------------------------


class TestUpdateItem:
    async def test_an_edit_applies_and_audits(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"), description="Old")

        response = await api_client.patch(
            f"/products/items/{item.id}",
            json={"name": "Lever handle", "description": "Satin brass", "lead_time_days": 7},
        )

        data = ok(response)
        assert data["name"] == "Lever handle"
        assert data["description"] == "Satin brass"
        assert data["lead_time_days"] == 7
        assert "standard_cost" in data  # api_client principal holds item.cost_view
        assert await column(session, Item.name, item.id) == "Lever handle"
        (update,) = await audit_rows(session, tenant.company_id, item.id, AuditAction.UPDATE)
        assert update.entity_type == "item"
        assert update.new_values["name"] == "Lever handle"

    async def test_a_channel_price_move_needs_price_manage(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"), sale_price=Decimal("10"))
        act_as(holding(principal, Permission.ITEM_UPDATE, Permission.ITEM_VIEW))

        response = await api_client.patch(
            f"/products/items/{item.id}", json={"sale_price": "12.50"}
        )

        assert refused(response, 403, "PERMISSION_DENIED")
        assert await column(session, Item.sale_price, item.id) == Decimal("10")

    async def test_another_companys_finish_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        theirs = await make_attribute(session, foreign, unique_code("FIN"))

        response = await api_client.patch(
            f"/products/items/{item.id}", json={"finish_id": str(theirs.id)}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, Item.finish_id, item.id) is None

    async def test_another_companys_item_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_item(session, foreign, unique_code("IT"))

        response = await api_client.patch(
            f"/products/items/{theirs.id}", json={"name": "Taken over"}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, Item.name, theirs.id) == theirs.name


# -----------------------------------------------------------------------------
# POST /products/items/{item_id}/status
# -----------------------------------------------------------------------------


class TestChangeItemStatus:
    async def test_deactivating_is_a_status_change(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))

        response = await api_client.post(
            f"/products/items/{item.id}/status",
            json={"is_active": False, "reason": "Discontinued line"},
        )

        data = ok(response)
        assert data["is_active"] is False
        assert data["lifecycle_state"] == "INACTIVE"
        assert await column(session, Item.is_active, item.id) is False
        (change,) = await audit_rows(session, tenant.company_id, item.id, AuditAction.STATUS_CHANGE)
        assert change.entity_type == "item"
        assert change.old_values == {"status": "ACTIVE"}
        assert change.new_values == {"status": "INACTIVE"}
        assert change.context["reason"] == "Discontinued line"

    async def test_a_no_op_status_change_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))

        response = await api_client.post(
            f"/products/items/{item.id}/status",
            json={"is_active": True, "reason": "Already active"},
        )

        assert refused(response, 422, "INVALID_STATE_TRANSITION")
        assert not await audit_rows(session, tenant.company_id, item.id, AuditAction.STATUS_CHANGE)

    async def test_a_short_reason_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))

        response = await api_client.post(
            f"/products/items/{item.id}/status", json={"is_active": False, "reason": "no"}
        )

        assert "reason" in field_errors(response)

    async def test_another_companys_item_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_item(session, foreign, unique_code("IT"))

        response = await api_client.post(
            f"/products/items/{theirs.id}/status",
            json={"is_active": False, "reason": "Taken over"},
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, Item.is_active, theirs.id) is True


# -----------------------------------------------------------------------------
# POST /products/items/{item_id}/variants and PATCH /products/variants/{id}
# -----------------------------------------------------------------------------


class TestCreateVariant:
    async def test_a_variant_is_created_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        finish = await make_attribute(session, tenant, unique_code("FIN"))
        raw = unique_code("var").lower()

        response = await api_client.post(
            f"/products/items/{item.id}/variants",
            json={
                "code": f"  {raw} ",
                "name": " Satin Brass ",
                "finish_id": str(finish.id),
                "price_delta": "15",
            },
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "Satin Brass"
        assert data["finish_id"] == str(finish.id)
        assert Decimal(data["price_delta"]) == Decimal("15")
        variant_id = uuid.UUID(data["id"])
        (created,) = await audit_rows(session, tenant.company_id, variant_id, AuditAction.CREATE)
        assert created.entity_type == "item_variant"
        assert created.new_values["finish_id"] == str(finish.id)

    async def test_a_variant_without_an_attribute_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))

        response = await api_client.post(
            f"/products/items/{item.id}/variants",
            json={"code": unique_code("VAR"), "name": "Blank"},
        )

        errors = field_errors(response)
        assert errors  # model validator lands on the body

    async def test_a_duplicate_combination_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        finish = await make_attribute(session, tenant, unique_code("FIN"))
        await make_variant(session, tenant, item.id, unique_code("VAR"), finish_id=finish.id)

        response = await api_client.post(
            f"/products/items/{item.id}/variants",
            json={
                "code": unique_code("VAR"),
                "name": "Twin",
                "finish_id": str(finish.id),
            },
        )

        assert refused(response, 409, "DUPLICATE_DOCUMENT")

    async def test_another_companys_item_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_item(session, foreign, unique_code("IT"))
        finish = await make_attribute(session, foreign, unique_code("FIN"))

        response = await api_client.post(
            f"/products/items/{theirs.id}/variants",
            json={
                "code": unique_code("VAR"),
                "name": "Borrowed",
                "finish_id": str(finish.id),
            },
        )

        assert refused(response, 404, "NOT_FOUND")


class TestUpdateVariant:
    async def test_a_rename_is_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        variant = await make_variant(session, tenant, item.id, unique_code("VAR"))

        response = await api_client.patch(
            f"/products/variants/{variant.id}",
            json={"name": "Chrome polish", "price_delta": "20"},
        )

        data = ok(response)
        assert data["name"] == "Chrome polish"
        assert Decimal(data["price_delta"]) == Decimal("20")
        (update,) = await audit_rows(session, tenant.company_id, variant.id, AuditAction.UPDATE)
        assert update.entity_type == "item_variant"
        assert update.new_values["name"] == "Chrome polish"

    async def test_clearing_every_attribute_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        finish = await make_attribute(session, tenant, unique_code("FIN"))
        variant = await make_variant(
            session, tenant, item.id, unique_code("VAR"), finish_id=finish.id
        )

        response = await api_client.patch(
            f"/products/variants/{variant.id}", json={"finish_id": None}
        )

        assert refused(response, 422, "VALIDATION_ERROR")
        assert await column(session, ItemVariant.finish_id, variant.id) == finish.id

    async def test_another_companys_variant_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, foreign, unique_code("IT"))
        theirs = await make_variant(session, foreign, item.id, unique_code("VAR"))

        response = await api_client.patch(f"/products/variants/{theirs.id}", json={"name": "Taken"})

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, ItemVariant.name, theirs.id) == theirs.name


# -----------------------------------------------------------------------------
# POST /products/items/{item_id}/prices and PATCH /products/prices/{price_id}
# -----------------------------------------------------------------------------


class TestCreatePrice:
    async def test_a_price_is_created_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        start = today()

        response = await api_client.post(
            f"/products/items/{item.id}/prices",
            json={
                "price_type": "SALE",
                "price": "125.50",
                "min_quantity": "10",
                "valid_from": start.isoformat(),
                "notes": "Volume slab",
            },
        )

        data = ok(response, 201)
        assert data["price_type"] == "SALE"
        assert Decimal(data["price"]) == Decimal("125.50")
        assert Decimal(data["min_quantity"]) == Decimal("10")
        price_id = uuid.UUID(data["id"])
        assert await column(session, ItemPrice.item_id, price_id) == item.id
        (created,) = await audit_rows(session, tenant.company_id, price_id, AuditAction.CREATE)
        assert created.entity_type == "item_price"
        assert Decimal(str(created.new_values["price"])) == Decimal("125.50")

    async def test_an_overlapping_window_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        start = today()
        existing = await make_price(session, tenant, item.id, valid_from=start)

        response = await api_client.post(
            f"/products/items/{item.id}/prices",
            json={
                "price_type": "SALE",
                "price": "90",
                "valid_from": start.isoformat(),
            },
        )

        error = refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert str(existing.id) in error["details"]["conflicting_price_ids"]

    async def test_a_backwards_window_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))

        response = await api_client.post(
            f"/products/items/{item.id}/prices",
            json={
                "price_type": "SALE",
                "price": "10",
                "valid_from": "2030-06-01",
                "valid_to": "2030-01-01",
            },
        )

        assert field_errors(response)

    async def test_another_companys_item_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_item(session, foreign, unique_code("IT"))

        response = await api_client.post(
            f"/products/items/{theirs.id}/prices",
            json={"price_type": "SALE", "price": "10", "valid_from": today().isoformat()},
        )

        assert refused(response, 404, "NOT_FOUND")


class TestUpdatePrice:
    async def test_a_price_change_is_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        price = await make_price(session, tenant, item.id, price="100")

        response = await api_client.patch(
            f"/products/prices/{price.id}", json={"price": "110.25", "notes": "Revised"}
        )

        data = ok(response)
        assert Decimal(data["price"]) == Decimal("110.25")
        assert data["notes"] == "Revised"
        (update,) = await audit_rows(session, tenant.company_id, price.id, AuditAction.UPDATE)
        assert update.entity_type == "item_price"
        assert Decimal(str(update.new_values["price"])) == Decimal("110.25")

    async def test_extending_into_another_window_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        start = today()
        first = await make_price(
            session,
            tenant,
            item.id,
            price="100",
            valid_from=start,
            valid_to=start + timedelta(days=30),
        )
        await make_price(
            session,
            tenant,
            item.id,
            price="90",
            valid_from=start + timedelta(days=31),
        )

        response = await api_client.patch(
            f"/products/prices/{first.id}",
            json={"valid_to": (start + timedelta(days=60)).isoformat()},
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, ItemPrice.valid_to, first.id) == start + timedelta(days=30)

    async def test_another_companys_price_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, foreign, unique_code("IT"))
        theirs = await make_price(session, foreign, item.id)

        response = await api_client.patch(f"/products/prices/{theirs.id}", json={"price": "1"})

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, ItemPrice.price, theirs.id) == theirs.price


# -----------------------------------------------------------------------------
# POST /products/items/{item_id}/vendors and PATCH /products/item-vendors/{id}
# -----------------------------------------------------------------------------


class TestLinkVendor:
    async def test_a_vendor_is_linked_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"), can_purchase=True)
        vendor = await seed_support.seed_vendor(
            session, company_id=tenant.company_id, code=unique_code("V")
        )

        response = await api_client.post(
            f"/products/items/{item.id}/vendors",
            json={
                "vendor_id": str(vendor.id),
                "vendor_item_code": "V-100",
                "is_preferred": True,
                "lead_time_days": 14,
            },
        )

        data = ok(response, 201)
        assert data["vendor_id"] == str(vendor.id)
        assert data["vendor_item_code"] == "V-100"
        assert data["is_preferred"] is True
        link_id = uuid.UUID(data["id"])
        (created,) = await audit_rows(session, tenant.company_id, link_id, AuditAction.CREATE)
        assert created.entity_type == "item_vendor_link"

    async def test_a_non_purchasable_item_refuses_a_vendor(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(
            session,
            tenant,
            unique_code("IT"),
            item_group=ItemGroup.FINISHED_GOOD,
            production_type="MAKE",
            can_manufacture=True,
            can_purchase=False,
        )
        vendor = await seed_support.seed_vendor(
            session, company_id=tenant.company_id, code=unique_code("V")
        )

        response = await api_client.post(
            f"/products/items/{item.id}/vendors", json={"vendor_id": str(vendor.id)}
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_another_companys_vendor_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"), can_purchase=True)
        theirs = await seed_support.seed_vendor(
            session, company_id=foreign.company_id, code=unique_code("V")
        )

        response = await api_client.post(
            f"/products/items/{item.id}/vendors", json={"vendor_id": str(theirs.id)}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ItemVendorLink)
                .where(ItemVendorLink.company_id == tenant.company_id)
            )
        ) == 0


class TestUpdateVendorLink:
    async def test_preferring_one_vendor_demotes_the_other(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"), can_purchase=True)
        first_vendor = await seed_support.seed_vendor(
            session, company_id=tenant.company_id, code=unique_code("V")
        )
        second_vendor = await seed_support.seed_vendor(
            session, company_id=tenant.company_id, code=unique_code("V")
        )
        preferred = await make_vendor_link(
            session, tenant, item.id, first_vendor.id, is_preferred=True
        )
        other = await make_vendor_link(session, tenant, item.id, second_vendor.id)

        response = await api_client.patch(
            f"/products/item-vendors/{other.id}",
            json={"is_preferred": True, "lead_time_days": 21},
        )

        data = ok(response)
        assert data["is_preferred"] is True
        assert data["lead_time_days"] == 21
        assert await column(session, ItemVendorLink.is_preferred, preferred.id) is False
        assert await column(session, ItemVendorLink.is_preferred, other.id) is True
        (update,) = await audit_rows(session, tenant.company_id, other.id, AuditAction.UPDATE)
        assert update.entity_type == "item_vendor_link"

    async def test_another_companys_link_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, foreign, unique_code("IT"), can_purchase=True)
        vendor = await seed_support.seed_vendor(
            session, company_id=foreign.company_id, code=unique_code("V")
        )
        theirs = await make_vendor_link(session, foreign, item.id, vendor.id)

        response = await api_client.patch(
            f"/products/item-vendors/{theirs.id}", json={"lead_time_days": 99}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, ItemVendorLink.lead_time_days, theirs.id) == 0


# -----------------------------------------------------------------------------
# PUT /products/items/{item_id}/image
# -----------------------------------------------------------------------------


class TestSetItemImage:
    async def test_an_image_filed_on_the_item_can_be_set_and_cleared(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        document = await item_image(session, tenant, item.id)

        set_response = await api_client.put(
            f"/products/items/{item.id}/image", json={"document_id": str(document.id)}
        )
        set_data = ok(set_response)
        assert set_data["document_id"] == str(document.id)
        assert await column(session, Item.image_document_id, item.id) == document.id
        (updated,) = await audit_rows(session, tenant.company_id, item.id, AuditAction.UPDATE)
        assert updated.new_values["image_document_id"] == str(document.id)

        clear_response = await api_client.put(
            f"/products/items/{item.id}/image", json={"document_id": None}
        )
        assert ok(clear_response)["document_id"] is None
        assert await column(session, Item.image_document_id, item.id) is None

    async def test_an_image_filed_on_another_item_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        item = await make_item(session, tenant, unique_code("IT"))
        other = await make_item(session, tenant, unique_code("IT"))
        document = await item_image(session, tenant, other.id)

        response = await api_client.put(
            f"/products/items/{item.id}/image", json={"document_id": str(document.id)}
        )

        assert refused(response, 422, "VALIDATION_ERROR")
        assert await column(session, Item.image_document_id, item.id) is None

    async def test_another_companys_item_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_item(session, foreign, unique_code("IT"))
        document = await item_image(session, foreign, theirs.id)

        response = await api_client.put(
            f"/products/items/{theirs.id}/image", json={"document_id": str(document.id)}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, Item.image_document_id, theirs.id) is None


# -----------------------------------------------------------------------------
# Warehouses
# -----------------------------------------------------------------------------


class TestCreateWarehouse:
    async def test_a_warehouse_is_created_normalised_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("rm").lower()

        response = await api_client.post(
            "/warehouses",
            json={
                "code": f"  {raw} ",
                "name": " Raw   Material ",
                "warehouse_type": "RAW_MATERIAL",
                "city": " Rajkot ",
            },
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "Raw Material"
        assert data["city"] == "Rajkot"
        assert data["is_active"] is True
        assert data["is_default"] is False
        warehouse_id = uuid.UUID(data["id"])
        assert await column(session, Warehouse.company_id, warehouse_id) == tenant.company_id
        (created,) = await audit_rows(session, tenant.company_id, warehouse_id, AuditAction.CREATE)
        assert created.entity_type == "warehouse"
        assert created.created_by_id == tenant.user_id
        read = ok(await api_client.get(f"/warehouses/{warehouse_id}"))
        assert read["code"] == data["code"]

    async def test_a_duplicate_code_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_warehouse(session, tenant, unique_code("RM"))

        response = await api_client.post(
            "/warehouses",
            json={
                "code": existing.code.lower(),
                "name": "Twin",
                "warehouse_type": "RAW_MATERIAL",
            },
        )

        assert refused(response, 409, "DUPLICATE_DOCUMENT")
        assert await count_codes(session, Warehouse, tenant.company_id, existing.code) == 1

    async def test_a_branch_of_another_company_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = Branch(company_id=foreign.company_id, code=unique_code("BR"), name="Theirs")
        session.add(theirs)
        await session.flush()
        code = unique_code("RM")

        response = await api_client.post(
            "/warehouses",
            json={
                "code": code,
                "name": "Borrowed",
                "warehouse_type": "RAW_MATERIAL",
                "branch_id": str(theirs.id),
            },
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await count_codes(session, Warehouse, tenant.company_id, code) == 0

    async def test_code_name_and_type_are_required(self, api_client: AsyncClient) -> None:
        response = await api_client.post("/warehouses", json={"city": "Rajkot"})

        assert {"code", "name", "warehouse_type"} <= set(field_errors(response))


class TestUpdateWarehouse:
    async def test_an_edit_applies_only_what_was_sent_and_audits(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"), city="Rajkot")

        response = await api_client.patch(
            f"/warehouses/{warehouse.id}",
            json={"name": "Main RM store", "city": " Surat "},
        )

        data = ok(response)
        assert data["name"] == "Main RM store"
        assert data["city"] == "Surat"
        assert await column(session, Warehouse.city, warehouse.id) == "Surat"
        (update,) = await audit_rows(session, tenant.company_id, warehouse.id, AuditAction.UPDATE)
        assert update.entity_type == "warehouse"
        assert set(update.changed_fields or []) >= {"city", "name"}

    async def test_an_empty_patch_and_a_code_change_are_both_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))

        empty = await api_client.patch(f"/warehouses/{warehouse.id}", json={})
        assert refused(empty, 422, "BUSINESS_RULE_VIOLATION")

        recoded = await api_client.patch(f"/warehouses/{warehouse.id}", json={"code": "RENAMED"})
        assert "code" in field_errors(recoded)
        assert await column(session, Warehouse.code, warehouse.id) == warehouse.code

    async def test_another_companys_warehouse_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_warehouse(session, foreign, unique_code("RM"))

        response = await api_client.patch(f"/warehouses/{theirs.id}", json={"name": "Taken over"})

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, Warehouse.name, theirs.id) == theirs.name


class TestWarehouseLifecycle:
    async def test_deactivate_and_activate_are_status_changes(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))

        off = await api_client.post(
            f"/warehouses/{warehouse.id}/deactivate",
            json={"reason": "Under renovation"},
        )
        assert ok(off)["is_active"] is False
        assert await column(session, Warehouse.is_active, warehouse.id) is False

        on = await api_client.post(
            f"/warehouses/{warehouse.id}/activate",
            json={"reason": "Renovation complete"},
        )
        assert ok(on)["is_active"] is True
        changes = await audit_rows(
            session, tenant.company_id, warehouse.id, AuditAction.STATUS_CHANGE
        )
        assert [(c.old_values["status"], c.new_values["status"]) for c in changes] == [
            ("ACTIVE", "INACTIVE"),
            ("INACTIVE", "ACTIVE"),
        ]
        assert changes[0].context["reason"] == "Under renovation"

    async def test_the_default_warehouse_cannot_be_deactivated(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"), is_default=True)

        response = await api_client.post(
            f"/warehouses/{warehouse.id}/deactivate",
            json={"reason": "Trying to switch off the default"},
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, Warehouse.is_active, warehouse.id) is True

    async def test_a_short_reason_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))

        response = await api_client.post(
            f"/warehouses/{warehouse.id}/deactivate", json={"reason": "no"}
        )

        assert "reason" in field_errors(response)

    async def test_another_companys_warehouse_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_warehouse(session, foreign, unique_code("RM"))

        response = await api_client.post(
            f"/warehouses/{theirs.id}/deactivate", json={"reason": "Taken over"}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, Warehouse.is_active, theirs.id) is True

    async def test_activating_an_already_active_warehouse_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))

        response = await api_client.post(
            f"/warehouses/{warehouse.id}/activate", json={"reason": "Already live"}
        )

        assert refused(response, 422, "INVALID_STATE_TRANSITION")


class TestCreateLocation:
    async def test_a_location_is_created_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))
        raw = unique_code("bin").lower()

        response = await api_client.post(
            f"/warehouses/{warehouse.id}/locations",
            json={
                "code": f"  {raw} ",
                "name": " Bin   A1 ",
                "location_type": "BIN",
                "barcode": "BC-001",
            },
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "Bin A1"
        assert data["warehouse_id"] == str(warehouse.id)
        location_id = uuid.UUID(data["id"])
        assert await column(session, StorageLocation.warehouse_id, location_id) == warehouse.id
        (created,) = await audit_rows(session, tenant.company_id, location_id, AuditAction.CREATE)
        assert created.entity_type == "storage_location"

    async def test_a_duplicate_code_in_the_same_warehouse_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))
        existing = await make_location(session, tenant, warehouse.id, unique_code("BIN"))

        response = await api_client.post(
            f"/warehouses/{warehouse.id}/locations",
            json={
                "code": existing.code.lower(),
                "name": "Twin",
                "location_type": "BIN",
            },
        )

        assert refused(response, 409, "DUPLICATE_DOCUMENT")

    async def test_an_inactive_warehouse_takes_no_new_locations(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))
        await WarehouseService(session, tenant).set_active(
            warehouse.id, active=False, reason="Closed for stock take"
        )

        response = await api_client.post(
            f"/warehouses/{warehouse.id}/locations",
            json={"code": unique_code("BIN"), "name": "Late", "location_type": "BIN"},
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_another_companys_warehouse_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_warehouse(session, foreign, unique_code("RM"))

        response = await api_client.post(
            f"/warehouses/{theirs.id}/locations",
            json={"code": unique_code("BIN"), "name": "Borrowed", "location_type": "BIN"},
        )

        assert refused(response, 404, "NOT_FOUND")


class TestUpdateLocation:
    async def test_an_edit_applies_and_audits(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))
        location = await make_location(session, tenant, warehouse.id, unique_code("BIN"))

        response = await api_client.patch(
            f"/warehouses/locations/{location.id}",
            json={"name": "Aisle 1 bin 2", "barcode": "BC-22"},
        )

        data = ok(response)
        assert data["name"] == "Aisle 1 bin 2"
        assert data["barcode"] == "BC-22"
        (update,) = await audit_rows(session, tenant.company_id, location.id, AuditAction.UPDATE)
        assert update.entity_type == "storage_location"
        assert update.new_values["name"] == "Aisle 1 bin 2"

    async def test_an_empty_patch_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))
        location = await make_location(session, tenant, warehouse.id, unique_code("BIN"))

        response = await api_client.patch(f"/warehouses/locations/{location.id}", json={})

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert not await audit_rows(session, tenant.company_id, location.id, AuditAction.UPDATE)

    async def test_another_companys_location_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, foreign, unique_code("RM"))
        theirs = await make_location(session, foreign, warehouse.id, unique_code("BIN"))

        response = await api_client.patch(
            f"/warehouses/locations/{theirs.id}", json={"name": "Taken"}
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, StorageLocation.name, theirs.id) == theirs.name


class TestLocationLifecycle:
    async def test_deactivate_and_activate_are_status_changes(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))
        location = await make_location(session, tenant, warehouse.id, unique_code("BIN"))

        off = await api_client.post(
            f"/warehouses/locations/{location.id}/deactivate",
            json={"reason": "Rack damaged"},
        )
        assert ok(off)["is_active"] is False

        on = await api_client.post(
            f"/warehouses/locations/{location.id}/activate",
            json={"reason": "Rack repaired"},
        )
        assert ok(on)["is_active"] is True
        changes = await audit_rows(
            session, tenant.company_id, location.id, AuditAction.STATUS_CHANGE
        )
        assert [(c.old_values["status"], c.new_values["status"]) for c in changes] == [
            ("ACTIVE", "INACTIVE"),
            ("INACTIVE", "ACTIVE"),
        ]

    async def test_another_companys_location_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, foreign, unique_code("RM"))
        theirs = await make_location(session, foreign, warehouse.id, unique_code("BIN"))

        response = await api_client.post(
            f"/warehouses/locations/{theirs.id}/deactivate",
            json={"reason": "Taken over"},
        )

        assert refused(response, 404, "NOT_FOUND")
        assert await column(session, StorageLocation.is_active, theirs.id) is True

    async def test_activating_an_already_active_location_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        warehouse = await make_warehouse(session, tenant, unique_code("RM"))
        location = await make_location(session, tenant, warehouse.id, unique_code("BIN"))

        response = await api_client.post(
            f"/warehouses/locations/{location.id}/activate",
            json={"reason": "Already live"},
        )

        assert refused(response, 422, "INVALID_STATE_TRANSITION")
