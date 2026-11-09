"""Dealer portal and promotion routes over HTTP, end to end on real rows.

``tests/unit/test_route_authorization.py`` and ``tests/api/test_route_contract_http.py``
already prove that every route refuses an anonymous or unentitled caller and
answers an unknown id with 404. They stop at the door: a portal route refuses
the staff principal those suites use before its handler runs, so none of the
bodies below had been executed by any test. This file walks each route's
success path and the failures the route owns itself - a malformed payload (422
with field errors), a document in the wrong state, another dealer's or another
company's row (404) - and reads every write back through the API or the session.

Two worlds, both built through services or the ORM:

``shop``
    A dealer, its buyer account and a sellable item with a DEALER price list
    (90 from one unit, 80 from ten), two item schemes (10% from one unit, 15%
    from ten), a priced variant (+15), a retired variant and a default
    finished-goods warehouse. The catalogue, cart, wishlist and checkout run here.
``account``
    The invoicing bench of ``test_sales_invoice_service`` (a confirmed order with
    200 units reserved), its customer turned dealer. Consignments, tracking,
    invoices, credit notes, PDF jobs, downloads and returns run here.

Celery's ``send_task`` is stubbed for every test - it is the one door to the
broker, so PDF jobs and any mail, push or messaging fan-out stay in-process -
and files land in a throwaway storage root.

Fixtures hand back plain ids, never ORM rows: an expected failure rolls its
savepoint back and expires what was loaded before it (CLAUDE.md), so no test
reads an attribute off a fixture row after a refusal.
"""

from __future__ import annotations

import io
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers, UploadFile

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.customers.models import AddressType, CustomerAddress, CustomerType
from src.modules.dealer_portal.models import (
    DealerBanner,
    DealerBannerStatus,
    DealerCart,
    DealerCartLine,
    DealerOffer,
    DealerWishlistItem,
)
from src.modules.dealer_portal.promotion_schemas import DealerBannerCreate, DealerOfferCreate
from src.modules.dealer_portal.promotion_service import PromotionService
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.schemas import DispatchCreate, DispatchRequest, InTransitRequest
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.schemas import DocumentUploadRequest
from src.modules.documents.service import DocumentService
from src.modules.finance import credit_note_service, sales_invoice_service
from src.modules.finance.credit_note_schemas import CreditNoteCreate, CreditNoteLineIn
from src.modules.finance.models import CreditNoteReason, SalesInvoice
from src.modules.finance.sales_invoice_schemas import SalesInvoiceCreate
from src.modules.inventory import service as inventory_service
from src.modules.inventory.models import TransactionType
from src.modules.notifications.models import Notification, NotificationType
from src.modules.products import service as products_service
from src.modules.products.models import Item, ItemAttributeType, ItemGroup, PriceType
from src.modules.products.schemas import ItemAttributeCreate, PriceCreate, VariantCreate
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportSnapshot
from src.modules.sales.models import SalesOrder
from src.modules.sales_returns.models import SalesReturn
from src.modules.warehouses.models import Warehouse, WarehouseType
from src.workers import celery_app as celery_module
from tests import seed_support
from tests.api.dealer_portal_support import (
    dealer_customer,
    dealer_principal,
    portal_user,
    sign_in,
)
from tests.integration.test_sales_invoice_service import bench as bench
from tests.statement_support import counted

pytestmark = [pytest.mark.api, pytest.mark.integration]

#: The real PDF queue. ``bench`` replaces it with a stub that files no job, so
#: the tests that follow a job from queue to download put this one back.
REAL_QUEUE_INVOICE_PDF = sales_invoice_service.queue_sales_invoice_pdf

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(48)
PDF = b"%PDF-1.7\n" + b"dealer statutory copy " * 40 + b"\n%%EOF\n"
WEEK = timedelta(days=7)


# -----------------------------------------------------------------------------
# Envelope helpers
# -----------------------------------------------------------------------------


def ok(response: Response, status: int = 200, *, message: str | None = None) -> Any:
    """The success envelope's ``data``, after checking the status, the flag and any message."""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    if message is not None:
        assert body["message"] == message, body
    return body["data"]


def refused(response: Response, status: int, code: str) -> dict[str, Any]:
    """The error envelope's ``error``, after checking the status and the code."""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code, body["error"]
    return body["error"]


def field_errors(response: Response) -> dict[str, list[str]]:
    """The per-field messages of a 422 request-validation refusal."""
    return refused(response, 422, "VALIDATION_ERROR")["details"]["fields"]


# -----------------------------------------------------------------------------
# Row helpers
# -----------------------------------------------------------------------------


async def audit_rows(
    session: AsyncSession,
    *,
    entity_id: uuid.UUID,
    entity_type: str | None = None,
    action: AuditAction | None = None,
) -> list[AuditLog]:
    # (created_at, id): rows written in one transaction share now().
    stmt = select(AuditLog).where(AuditLog.entity_id == entity_id)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list((await session.scalars(stmt.order_by(AuditLog.created_at, AuditLog.id))).all())


def reasons(trail: list[AuditLog]) -> list[tuple[str, str | None]]:
    return [(str(row.action), (row.context or {}).get("reason")) for row in trail]


async def stored(
    session: AsyncSession,
    tenant: TenantContext,
    content: bytes,
    mime_type: str,
    file_name: str,
    *,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    category: DocumentCategory = DocumentCategory.ATTACHMENT,
) -> uuid.UUID:
    """File bytes through the documents module's own upload path. Returns the id."""
    upload = UploadFile(
        file=io.BytesIO(content),
        size=len(content),
        filename=file_name,
        headers=Headers({"content-type": mime_type}),
    )
    document = await DocumentService(session, tenant).upload(
        upload,
        DocumentUploadRequest(category=category, entity_type=entity_type, entity_id=entity_id),
    )
    return document.id


async def colleague_of(
    session: AsyncSession, tenant: TenantContext, staff: Principal, customer_id: uuid.UUID
) -> Principal:
    """A second buyer account of the same dealer: same customer, its own cart."""
    user = await portal_user(session, tenant, customer_id, preset="DEALER_BUYER")
    return dealer_principal(staff, customer_id, user_id=user.id, preset="DEALER_BUYER")


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def broker(monkeypatch) -> list[str]:
    """Every task name handed to Celery. Nothing reaches a broker."""
    sent: list[str] = []

    def accepted(name: str, **_: Any) -> Any:
        sent.append(name)
        return SimpleNamespace(id=uuid.uuid4().hex)

    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted)
    return sent


@pytest.fixture
def storage(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_path", root)
    return root


@pytest.fixture
async def shop(session, tenant, company, principal, unique_code) -> SimpleNamespace:
    company.state, company.state_code = "Gujarat", "24"
    await session.flush()
    ours = await dealer_customer(session, tenant, "Our Hardware Mart")
    theirs = await dealer_customer(session, tenant, "Somebody Else's Mart")
    buyer = await portal_user(session, tenant, ours.id)
    category = await seed_support.seed_category(session, company_id=tenant.company_id)
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=unique_code("FG"),
        name="Lever handle",
        item_group=ItemGroup.FINISHED_GOOD,
        category_id=category.id,
        can_sell=True,
        is_active=True,
        sale_price=Decimal("120"),
        dealer_price=Decimal("100"),
        gst_rate=Decimal("18"),
        hsn_code="8302",
        description="Satin brass lever handle on a round rose",
    )
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=unique_code("FGW"),
        warehouse_type=WarehouseType.FINISHED_GOODS,
        is_default=True,
    )
    for minimum, price in (("0", "90"), ("10", "80")):
        await products_service.create_price(
            session,
            tenant,
            item.id,
            PriceCreate(
                price_type=PriceType.DEALER,
                price=Decimal(price),
                min_quantity=Decimal(minimum),
                valid_from=today() - timedelta(days=1),
            ),
        )
    satin = await products_service.create_item_attribute(
        session,
        tenant,
        ItemAttributeCreate(
            attribute=ItemAttributeType.FINISH, code=unique_code("F"), name="Satin"
        ),
    )
    black = await products_service.create_item_attribute(
        session,
        tenant,
        ItemAttributeCreate(
            attribute=ItemAttributeType.FINISH, code=unique_code("F"), name="Matt black"
        ),
    )
    variant = await products_service.create_variant(
        session,
        tenant,
        item.id,
        VariantCreate(
            code=unique_code("V"), name="Satin", finish_id=satin.id, price_delta=Decimal("15")
        ),
    )
    retired = await products_service.create_variant(
        session,
        tenant,
        item.id,
        VariantCreate(
            code=unique_code("V"),
            name="Matt black",
            finish_id=black.id,
            price_delta=Decimal("5"),
            is_active=False,
        ),
    )
    promotions = PromotionService(session, tenant)
    ten = await promotions.create(
        "offers",
        DealerOfferCreate(
            code=unique_code("TEN"),
            title="Ten off the lever",
            scope_kind="ITEM",
            item_id=item.id,
            discount_percentage=Decimal("10"),
            is_active=True,
        ),
    )
    bulk = await promotions.create(
        "offers",
        DealerOfferCreate(
            code=unique_code("BULK"),
            title="Fifteen off ten or more",
            scope_kind="ITEM",
            item_id=item.id,
            min_quantity=Decimal("10"),
            discount_percentage=Decimal("15"),
            is_active=True,
        ),
    )
    return SimpleNamespace(
        ours_id=ours.id,
        theirs_id=theirs.id,
        buyer_id=buyer.id,
        item_id=item.id,
        item_code=item.code,
        warehouse_id=warehouse.id,
        variant_id=variant.id,
        retired_variant_id=retired.id,
        ten_id=ten.id,
        bulk_id=bulk.id,
        dealer=dealer_principal(principal, ours.id, user_id=buyer.id),
    )


@pytest.fixture
async def account(bench, session, tenant, principal) -> SimpleNamespace:
    """The invoicing bench's customer as a dealer with its own buyer account."""
    customer = bench.customer
    customer.customer_type = CustomerType.DEALER
    customer.price_list_type = PriceType.DEALER
    await session.flush()
    buyer = await portal_user(session, tenant, customer.id)
    other = await dealer_customer(session, tenant, "Another dealer")
    stranger = await portal_user(session, tenant, other.id)
    return SimpleNamespace(
        customer_id=customer.id,
        buyer_id=buyer.id,
        item_id=bench.item.id,
        warehouse_id=bench.warehouse.id,
        order_id=bench.order.id,
        order_line_id=bench.line.id,
        dealer=dealer_principal(principal, customer.id, user_id=buyer.id),
        stranger=dealer_principal(principal, other.id, user_id=stranger.id),
    )


async def ship(
    session: AsyncSession,
    tenant: TenantContext,
    world: SimpleNamespace,
    quantity: str = "200",
    **transport: Any,
) -> uuid.UUID:
    """Raise, load and send a consignment for the bench order. Returns its id."""
    body = DispatchCreate.model_validate(
        {
            "dispatch_type": "SALES",
            "customer_id": str(world.customer_id),
            "warehouse_id": str(world.warehouse_id),
            "sales_order_id": str(world.order_id),
            "dispatch_date": today().isoformat(),
            "lines": [
                {
                    "item_id": str(world.item_id),
                    "sales_order_line_id": str(world.order_line_id),
                    "quantity": quantity,
                }
            ],
            **transport,
        }
    )
    detail = await dispatch_service.create_dispatch(session, tenant, body)
    await dispatch_service.mark_ready(session, tenant, detail.id, reason="Loaded and ready")
    await dispatch_service.dispatch_consignment(
        session, tenant, detail.id, DispatchRequest(reason="Goods leaving")
    )
    return detail.id


@pytest.fixture
async def invoiced(account, session, tenant) -> SimpleNamespace:
    dispatch_id = await ship(session, tenant, account)
    invoice = await sales_invoice_service.issue_sales_invoice(
        session, tenant, SalesInvoiceCreate(dispatch_id=dispatch_id)
    )
    return SimpleNamespace(
        dispatch_id=dispatch_id,
        invoice_id=invoice.id,
        invoice_code=invoice.code,
        invoice_total=invoice.total_amount,
        invoice_line_id=invoice.lines[0].id,
    )


@pytest.fixture
async def credited(invoiced, session, tenant) -> SimpleNamespace:
    note = await credit_note_service.issue_credit_note(
        session,
        tenant,
        CreditNoteCreate(
            sales_invoice_id=invoiced.invoice_id,
            note_date=today(),
            reason_type=CreditNoteReason.DISCOUNT,
            lines=[
                CreditNoteLineIn(
                    sales_invoice_line_id=invoiced.invoice_line_id, taxable_amount=Decimal("100")
                )
            ],
        ),
    )
    return SimpleNamespace(note_id=note.id, note_code=note.code, note_total=note.total_amount)


def return_body(dispatch_id: uuid.UUID | str, *lines: tuple[str, str]) -> dict[str, Any]:
    return {
        "dispatch_id": str(dispatch_id),
        "lines": [
            {"dispatch_line_id": line_id, "quantity": quantity, "reason_code": "DAMAGED"}
            for line_id, quantity in lines
        ],
    }


# -----------------------------------------------------------------------------
# Catalogue: item detail and price quote
# -----------------------------------------------------------------------------


async def test_the_item_detail_projects_variants_slabs_schemes_and_images(
    api_client, session, tenant, shop, storage, decimal_equal
):
    front = await stored(
        session, tenant, PNG, "image/png", "front.png", entity_type="item", entity_id=shop.item_id
    )
    side = await stored(
        session, tenant, PNG, "image/png", "side.png", entity_type="item", entity_id=shop.item_id
    )
    await products_service.set_item_image(session, tenant, shop.item_id, side)
    await inventory_service.post_movement(
        session,
        tenant,
        item_id=shop.item_id,
        warehouse_id=shop.warehouse_id,
        quantity=Decimal("3"),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="opening",
        reference_id=None,
        reference_code="OPEN",
        reason="Opening stock for the catalogue",
        unit_cost=Decimal("40"),
    )
    sign_in(shop.dealer)
    ok(await api_client.post("/dealer-portal/wishlist", json={"item_id": str(shop.item_id)}), 201)
    ok(
        await api_client.post(
            "/dealer-portal/cart/lines", json={"item_id": str(shop.item_id), "quantity": "3"}
        ),
        201,
    )

    detail = ok(await api_client.get(f"/dealer-portal/catalog/items/{shop.item_id}"))

    assert (detail["id"], detail["code"]) == (str(shop.item_id), shop.item_code)
    decimal_equal(detail["price"], "90")
    assert detail["price_on_request"] is False and detail["orderable"] is True
    # Three free units in a sellable warehouse: a band, never the quantity.
    assert detail["availability_band"] == "LIMITED"
    assert detail["in_wishlist"] is True
    decimal_equal(detail["in_cart_quantity"], "3")
    # At one unit only the 10% scheme applies; the 15% one starts at ten.
    assert detail["best_offer"]["id"] == str(shop.ten_id)
    assert [offer["id"] for offer in detail["offers"]] == [str(shop.ten_id)]
    assert detail["primary_image_id"] == str(side)
    assert detail["image_ids"] == [str(side), str(front)]
    assert detail["hsn_code"] == "8302" and detail["can_return"] is True
    decimal_equal(detail["gst_rate"], "18")
    assert [(Decimal(row["min_quantity"]), Decimal(row["price"])) for row in detail["slabs"]] == [
        (Decimal("0"), Decimal("90")),
        (Decimal("10"), Decimal("80")),
    ]
    variants = {row["id"]: row for row in detail["variants"]}
    decimal_equal(variants[str(shop.variant_id)]["price"], "105")
    assert variants[str(shop.variant_id)]["is_active"] is True
    assert variants[str(shop.retired_variant_id)]["is_active"] is False
    for internal in ("standard_cost", "sale_price", "dealer_price", "available_quantity"):
        assert internal not in detail, internal


async def test_items_the_dealer_cannot_buy_have_no_detail_and_no_quote(
    api_client, session, tenant, shop, other_company, unique_code
):
    unsellable = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=unique_code("RM"), can_sell=False
    )
    foreign = await seed_support.seed_item(
        session,
        company_id=other_company.id,
        code=unique_code("FG"),
        can_sell=True,
        dealer_price=Decimal("50"),
    )
    hidden = (unsellable.id, foreign.id, uuid.uuid4())
    sign_in(shop.dealer)
    for item_id in hidden:
        refused(await api_client.get(f"/dealer-portal/catalog/items/{item_id}"), 404, "NOT_FOUND")
        refused(
            await api_client.get(f"/dealer-portal/catalog/items/{item_id}/price"),
            404,
            "NOT_FOUND",
        )


async def test_the_price_quote_follows_the_slab_the_scheme_and_the_variant(
    api_client, session, tenant, shop, unique_code, decimal_equal
):
    other = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=unique_code("FG"),
        can_sell=True,
        dealer_price=Decimal("70"),
    )
    finish = await products_service.create_item_attribute(
        session,
        tenant,
        ItemAttributeCreate(attribute=ItemAttributeType.FINISH, code=unique_code("F"), name="Gold"),
    )
    stray = await products_service.create_variant(
        session,
        tenant,
        other.id,
        VariantCreate(code=unique_code("V"), name="Gold", finish_id=finish.id),
    )
    stray_id = stray.id
    url = f"/dealer-portal/catalog/items/{shop.item_id}/price"
    sign_in(shop.dealer)

    single = ok(await api_client.get(url))
    assert single["price_on_request"] is False
    assert single["offer"]["id"] == str(shop.ten_id)
    for field, expected in (
        ("quantity", "1"),
        ("gross_unit", "90"),
        ("discount_percentage", "10"),
        ("net_unit", "81"),
        ("tax_rate", "18"),
        ("tax_estimate", "14.58"),
    ):
        decimal_equal(single[field], expected, field)

    # Ten units reach the 80 slab, and the 15% scheme outranks the 10% one.
    slab = ok(await api_client.get(url, params={"quantity": "10"}))
    assert slab["offer"]["id"] == str(shop.bulk_id)
    for field, expected in (
        ("gross_unit", "80"),
        ("discount_percentage", "15"),
        ("net_unit", "68"),
        ("tax_estimate", "122.40"),
    ):
        decimal_equal(slab[field], expected, field)

    # The variant carries its delta on top of the item-wide slab.
    variant = ok(
        await api_client.get(url, params={"quantity": "10", "variant_id": str(shop.variant_id)})
    )
    assert variant["variant_id"] == str(shop.variant_id)
    for field, expected in (
        ("gross_unit", "95"),
        ("net_unit", "80.75"),
        ("tax_estimate", "145.35"),
    ):
        decimal_equal(variant[field], expected, field)

    assert "query.quantity" in field_errors(await api_client.get(url, params={"quantity": "0"}))
    refused(await api_client.get(url, params={"variant_id": str(stray_id)}), 404, "NOT_FOUND")


async def test_a_channel_without_a_price_is_quoted_on_request(
    api_client, session, tenant, principal, shop, decimal_equal
):
    distributor = await dealer_customer(
        session,
        tenant,
        "Regional Distributor",
        customer_type=CustomerType.DISTRIBUTOR,
        price_type=PriceType.DISTRIBUTOR,
    )
    account_user = await portal_user(session, tenant, distributor.id)
    sign_in(dealer_principal(principal, distributor.id, user_id=account_user.id))

    quote = ok(
        await api_client.get(
            f"/dealer-portal/catalog/items/{shop.item_id}/price", params={"quantity": "5"}
        )
    )

    assert quote["price_on_request"] is True
    assert quote["gross_unit"] is None and quote["net_unit"] is None
    assert quote["tax_estimate"] is None and quote["offer"] is None
    decimal_equal(quote["discount_percentage"], "0")


# -----------------------------------------------------------------------------
# Cart
# -----------------------------------------------------------------------------


async def test_cart_lines_are_added_repriced_removed_and_cleared(
    api_client, session, shop, decimal_equal
):
    sign_in(shop.dealer)
    lines_url = "/dealer-portal/cart/lines"

    cart = ok(
        await api_client.post(lines_url, json={"item_id": str(shop.item_id), "quantity": "2"}),
        201,
    )
    (line,) = cart["lines"]
    assert line["item_id"] == str(shop.item_id) and line["offer"]["id"] == str(shop.ten_id)
    # 2 x 90 = 180, 10% off = 162, 18% tax = 29.16.
    for field, expected in (
        ("quantity", "2"),
        ("unit_price", "90"),
        ("discount_percentage", "10"),
        ("net_amount", "162"),
        ("tax_amount", "29.16"),
        ("line_total", "191.16"),
    ):
        decimal_equal(line[field], expected, field)
    for field, expected in (
        ("subtotal", "180"),
        ("discount_amount", "18"),
        ("tax_amount", "29.16"),
        ("total_amount", "191.16"),
    ):
        decimal_equal(cart[field], expected, field)
    assert cart["warnings"] == []
    cart_id = uuid.UUID(cart["cart_id"])
    owner = await session.scalar(select(DealerCart).where(DealerCart.id == cart_id))
    assert owner is not None
    assert (owner.user_id, owner.customer_id) == (shop.buyer_id, shop.ours_id)

    # Twelve units reach the 80 slab and the 15% scheme: 960 - 144 = 816, tax 146.88.
    repriced = ok(await api_client.patch(f"{lines_url}/{line['id']}", json={"quantity": "12"}))
    (line,) = repriced["lines"]
    assert line["offer"]["id"] == str(shop.bulk_id)
    for field, expected in (
        ("unit_price", "80"),
        ("discount_percentage", "15"),
        ("net_amount", "816"),
        ("tax_amount", "146.88"),
        ("line_total", "962.88"),
    ):
        decimal_equal(line[field], expected, field)
    decimal_equal(
        await session.scalar(
            select(DealerCartLine.quantity).where(DealerCartLine.id == uuid.UUID(line["id"]))
        ),
        "12",
    )

    with_variant = ok(
        await api_client.post(
            lines_url,
            json={
                "item_id": str(shop.item_id),
                "item_variant_id": str(shop.variant_id),
                "quantity": "1",
            },
        ),
        201,
    )
    (variant_line,) = [
        row for row in with_variant["lines"] if row["item_variant_id"] == str(shop.variant_id)
    ]
    decimal_equal(variant_line["unit_price"], "105")

    removed = ok(await api_client.delete(f"{lines_url}/{line['id']}"))
    assert [row["id"] for row in removed["lines"]] == [variant_line["id"]]
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DealerCartLine)
            .where(DealerCartLine.id == uuid.UUID(line["id"]))
        )
        == 0
    )

    cleared = ok(await api_client.delete("/dealer-portal/cart"))
    assert cleared["lines"] == [] and cleared["cart_id"] == str(cart_id)
    decimal_equal(cleared["total_amount"], "0")
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DealerCartLine)
            .where(DealerCartLine.cart_id == cart_id)
        )
        == 0
    )


async def test_cart_changes_the_dealer_may_not_make_are_refused(
    api_client, session, tenant, principal, shop, other_company, unique_code
):
    foreign = await seed_support.seed_item(
        session,
        company_id=other_company.id,
        code=unique_code("FG"),
        can_sell=True,
        dealer_price=Decimal("50"),
    )
    foreign_id = foreign.id
    lines_url = "/dealer-portal/cart/lines"
    colleague = await colleague_of(session, tenant, principal, shop.ours_id)
    sign_in(colleague)
    theirs = ok(
        await api_client.post(lines_url, json={"item_id": str(shop.item_id), "quantity": "1"}),
        201,
    )
    colleague_line = theirs["lines"][0]["id"]

    sign_in(shop.dealer)
    # This account has no cart yet: there is nothing to empty.
    refused(await api_client.delete("/dealer-portal/cart"), 404, "NOT_FOUND")
    assert "quantity" in field_errors(
        await api_client.post(lines_url, json={"item_id": str(shop.item_id), "quantity": "0"})
    )
    refused(
        await api_client.post(lines_url, json={"item_id": str(foreign_id), "quantity": "1"}),
        404,
        "NOT_FOUND",
    )
    refused(
        await api_client.post(
            lines_url,
            json={
                "item_id": str(shop.item_id),
                "item_variant_id": str(shop.retired_variant_id),
                "quantity": "1",
            },
        ),
        422,
        "BUSINESS_RULE_VIOLATION",
    )
    ok(
        await api_client.post(lines_url, json={"item_id": str(shop.item_id), "quantity": "1"}),
        201,
    )
    refused(
        await api_client.post(lines_url, json={"item_id": str(shop.item_id), "quantity": "4"}),
        409,
        "DUPLICATE_DOCUMENT",
    )
    # The colleague's line sits on another cart: not found, never "forbidden".
    refused(
        await api_client.patch(f"{lines_url}/{colleague_line}", json={"quantity": "2"}),
        404,
        "NOT_FOUND",
    )
    refused(await api_client.delete(f"{lines_url}/{colleague_line}"), 404, "NOT_FOUND")
    refused(await api_client.delete(f"{lines_url}/{uuid.uuid4()}"), 404, "NOT_FOUND")
    assert "quantity" in field_errors(
        await api_client.patch(f"{lines_url}/{colleague_line}", json={"quantity": "-1"})
    )

    sign_in(colleague)
    untouched = ok(await api_client.get("/dealer-portal/cart"))
    assert [(row["id"], Decimal(row["quantity"])) for row in untouched["lines"]] == [
        (colleague_line, Decimal("1"))
    ]


def trail(rows: list[AuditLog]) -> list[tuple[str, dict | None, dict | None]]:
    """Each row's action and the values it recorded, in the order written.

    ``record_audit`` stores only what changed, so a side with nothing to say
    (a create's before, a delete's after) and a null value read as absent.
    """

    def said(values: dict | None) -> dict | None:
        return {key: value for key, value in (values or {}).items() if value is not None} or None

    return [(str(row.action), said(row.old_values), said(row.new_values)) for row in rows]


async def test_every_cart_and_wishlist_write_is_audited_by_the_portal_user(
    api_client, session, shop
):
    """What a dealer added, changed and removed before checkout can be read back.

    Each write audits in its own transaction under the buyer's account; a write
    that changed nothing, and one that was refused, leaves no row.
    """
    sign_in(shop.dealer)
    lines_url = "/dealer-portal/cart/lines"
    item, variant = str(shop.item_id), str(shop.variant_id)

    cart = ok(await api_client.post(lines_url, json={"item_id": item, "quantity": "2"}), 201)
    cart_id, line_id = uuid.UUID(cart["cart_id"]), uuid.UUID(cart["lines"][0]["id"])
    ok(await api_client.patch(f"{lines_url}/{line_id}", json={"quantity": "12"}))
    ok(await api_client.patch(f"{lines_url}/{line_id}", json={"quantity": "12"}))
    refused(
        await api_client.post(lines_url, json={"item_id": item, "quantity": "1"}),
        409,
        "DUPLICATE_DOCUMENT",
    )

    (opened,) = await audit_rows(session, entity_id=cart_id, entity_type="dealer_cart")
    assert trail([opened]) == [("CREATE", None, {"customer_id": str(shop.ours_id)})]
    line_trail = await audit_rows(session, entity_id=line_id, entity_type="dealer_cart_line")
    assert trail(line_trail) == [
        ("CREATE", None, {"item_id": item, "quantity": "2.0000"}),
        ("UPDATE", {"quantity": "2.0000"}, {"quantity": "12.0000"}),
    ]
    assert {(row.created_by_id, row.entity_code) for row in [opened, *line_trail]} == {
        (shop.buyer_id, None),
        (shop.buyer_id, shop.item_code),
    }

    saved = ok(
        await api_client.post(
            "/dealer-portal/wishlist", json={"item_id": item, "item_variant_id": variant}
        ),
        201,
    )
    moved = ok(
        await api_client.post(
            f"/dealer-portal/wishlist/{saved['id']}/move-to-cart", params={"quantity": "3"}
        )
    )
    (variant_line,) = [row for row in moved["lines"] if row["item_variant_id"] == variant]
    assert trail(
        await audit_rows(
            session, entity_id=uuid.UUID(saved["id"]), entity_type="dealer_wishlist_item"
        )
    ) == [
        ("CREATE", None, {"item_id": item, "item_variant_id": variant}),
        ("DELETE", {"item_id": item, "item_variant_id": variant}, None),
    ]
    assert trail(
        await audit_rows(
            session, entity_id=uuid.UUID(variant_line["id"]), entity_type="dealer_cart_line"
        )
    ) == [("CREATE", None, {"item_id": item, "item_variant_id": variant, "quantity": "3.0000"})]

    ok(await api_client.delete(f"{lines_url}/{line_id}"))
    (removed,) = await audit_rows(
        session, entity_id=line_id, entity_type="dealer_cart_line", action=AuditAction.DELETE
    )
    assert removed.entity_code == shop.item_code
    assert trail([removed]) == [("DELETE", {"item_id": item, "quantity": "12.0000"}, None)]

    ok(await api_client.delete("/dealer-portal/cart"))
    ok(await api_client.delete("/dealer-portal/cart"))
    assert trail(await audit_rows(session, entity_id=cart_id, entity_type="dealer_cart")) == [
        ("CREATE", None, {"customer_id": str(shop.ours_id)}),
        ("UPDATE", {"line_count": 1}, {"line_count": 0}),
    ]

    again = ok(await api_client.post("/dealer-portal/wishlist", json={"item_id": item}), 201)
    ok(await api_client.delete(f"/dealer-portal/wishlist/{again['id']}"))
    assert trail(
        await audit_rows(
            session, entity_id=uuid.UUID(again["id"]), entity_type="dealer_wishlist_item"
        )
    ) == [("CREATE", None, {"item_id": item}), ("DELETE", {"item_id": item}, None)]


async def test_checkout_refuses_a_cart_changed_since_it_was_read(api_client, shop):
    sign_in(shop.dealer)
    cart = ok(
        await api_client.post(
            "/dealer-portal/cart/lines", json={"item_id": str(shop.item_id), "quantity": "2"}
        ),
        201,
    )
    read_version = cart["cart_version"]
    # Another tab of the same account changes the cart after this one read it.
    ok(
        await api_client.patch(
            f"/dealer-portal/cart/lines/{cart['lines'][0]['id']}", json={"quantity": "12"}
        )
    )

    placed = await api_client.post(
        "/dealer-portal/checkout",
        json={"cart_version": read_version, "required_date": (today() + WEEK).isoformat()},
    )

    refused(placed, 409, "CONFLICT")
    (line,) = ok(await api_client.get("/dealer-portal/cart"))["lines"]
    assert Decimal(line["quantity"]) == Decimal("12")


# -----------------------------------------------------------------------------
# Wishlist
# -----------------------------------------------------------------------------


async def test_the_wishlist_saves_moves_and_removes_items(api_client, session, shop, decimal_equal):
    sign_in(shop.dealer)
    saved = ok(
        await api_client.post("/dealer-portal/wishlist", json={"item_id": str(shop.item_id)}),
        201,
    )
    assert (saved["item_id"], saved["item_code"]) == (str(shop.item_id), shop.item_code)
    decimal_equal(saved["price"], "90")
    assert saved["price_on_request"] is False
    assert saved["availability_band"] == "MADE_TO_ORDER"
    entry = await session.scalar(
        select(DealerWishlistItem).where(DealerWishlistItem.id == uuid.UUID(saved["id"]))
    )
    assert entry is not None
    assert (entry.user_id, entry.customer_id) == (shop.buyer_id, shop.ours_id)
    refused(
        await api_client.post("/dealer-portal/wishlist", json={"item_id": str(shop.item_id)}),
        409,
        "DUPLICATE_DOCUMENT",
    )

    moved = ok(
        await api_client.post(
            f"/dealer-portal/wishlist/{saved['id']}/move-to-cart", params={"quantity": "4"}
        )
    )
    assert [(row["item_id"], Decimal(row["quantity"])) for row in moved["lines"]] == [
        (str(shop.item_id), Decimal("4"))
    ]
    assert ok(await api_client.get("/dealer-portal/wishlist")) == []

    again = ok(
        await api_client.post(
            "/dealer-portal/wishlist",
            json={"item_id": str(shop.item_id), "item_variant_id": str(shop.variant_id)},
        ),
        201,
    )
    assert again["item_variant_id"] == str(shop.variant_id)
    assert ok(await api_client.delete(f"/dealer-portal/wishlist/{again['id']}")) is None
    assert ok(await api_client.get("/dealer-portal/wishlist")) == []
    assert (
        await session.scalar(
            select(func.count())
            .select_from(DealerWishlistItem)
            .where(DealerWishlistItem.user_id == shop.buyer_id)
        )
        == 0
    )


def wishlist_reads(statements: list[str]) -> list[str]:
    """The statements that read the wishlist table."""
    return [
        text
        for text in statements
        if text.lstrip().upper().startswith("SELECT") and "dealer_wishlist_items" in text
    ]


async def test_the_wishlist_is_read_a_page_at_a_time(api_client, session, engine, shop):
    """GET /wishlist pages in SQL, and an add answers with its own row only.

    The list used to be one unbounded read, re-run in full by every add to find
    the row it had just saved.
    """
    sign_in(shop.dealer)
    first = ok(
        await api_client.post("/dealer-portal/wishlist", json={"item_id": str(shop.item_id)}),
        201,
    )
    with counted(engine) as adding:
        second = ok(
            await api_client.post(
                "/dealer-portal/wishlist",
                json={"item_id": str(shop.item_id), "item_variant_id": str(shop.variant_id)},
            ),
            201,
        )
    # One read of the table: the duplicate-grain check. The answer is the saved row.
    assert len(wishlist_reads(adding)) == 1, wishlist_reads(adding)
    assert (second["item_variant_id"], second["item_code"]) == (
        str(shop.variant_id),
        shop.item_code,
    )

    response = await api_client.get("/dealer-portal/wishlist", params={"limit": 1})
    assert [row["id"] for row in ok(response)] == [first["id"]]
    paging = response.json()["pagination"]
    assert (paging["total"], paging["has_next"], paging["has_previous"]) == (2, True, False)

    response = await api_client.get("/dealer-portal/wishlist", params={"limit": 1, "page": 2})
    assert [row["id"] for row in ok(response)] == [second["id"]]
    paging = response.json()["pagination"]
    assert (paging["total"], paging["has_next"], paging["has_previous"]) == (2, False, True)
    assert "query.limit" in field_errors(
        await api_client.get("/dealer-portal/wishlist", params={"limit": 0})
    )

    # A saved item that stops being sellable leaves the page and the count
    # together: the filter is in the page statement, never applied to a page
    # already cut (which would answer a short page under a total of two).
    await session.execute(update(Item).where(Item.id == shop.item_id).values(can_sell=False))
    response = await api_client.get("/dealer-portal/wishlist", params={"limit": 1})
    assert ok(response) == []
    paging = response.json()["pagination"]
    assert (paging["total"], paging["has_next"]) == (0, False)


async def test_a_catalogue_page_asks_only_about_its_own_items(api_client, engine, shop):
    """The ``in_wishlist`` flags come from the page's ids, not the whole wishlist."""
    sign_in(shop.dealer)
    ok(await api_client.post("/dealer-portal/wishlist", json={"item_id": str(shop.item_id)}), 201)

    with counted(engine) as browsing:
        page = ok(
            await api_client.get("/dealer-portal/catalog/items", params={"search": shop.item_code})
        )

    assert [row["in_wishlist"] for row in page if row["id"] == str(shop.item_id)] == [True]
    (read,) = wishlist_reads(browsing)
    assert "dealer_wishlist_items.item_id IN (" in read, read


async def test_wishlist_changes_the_dealer_may_not_make_are_refused(
    api_client, session, tenant, principal, shop, other_company, unique_code
):
    foreign = await seed_support.seed_item(
        session,
        company_id=other_company.id,
        code=unique_code("FG"),
        can_sell=True,
        dealer_price=Decimal("50"),
    )
    foreign_id = foreign.id
    colleague = await colleague_of(session, tenant, principal, shop.ours_id)
    sign_in(colleague)
    theirs = ok(
        await api_client.post("/dealer-portal/wishlist", json={"item_id": str(shop.item_id)}),
        201,
    )

    sign_in(shop.dealer)
    assert "price" in field_errors(
        await api_client.post(
            "/dealer-portal/wishlist", json={"item_id": str(shop.item_id), "price": "1"}
        )
    )
    refused(
        await api_client.post("/dealer-portal/wishlist", json={"item_id": str(foreign_id)}),
        404,
        "NOT_FOUND",
    )
    refused(await api_client.delete(f"/dealer-portal/wishlist/{theirs['id']}"), 404, "NOT_FOUND")
    refused(
        await api_client.post(f"/dealer-portal/wishlist/{theirs['id']}/move-to-cart"),
        404,
        "NOT_FOUND",
    )
    assert "query.quantity" in field_errors(
        await api_client.post(
            f"/dealer-portal/wishlist/{theirs['id']}/move-to-cart", params={"quantity": "0"}
        )
    )

    sign_in(colleague)
    assert [row["id"] for row in ok(await api_client.get("/dealer-portal/wishlist"))] == [
        theirs["id"]
    ]


async def test_a_quantity_beyond_the_stored_scale_is_a_field_error(api_client, shop):
    """NUMERIC(18,4) is the contract, so the door states it.

    ``1e25`` used to overflow ``Decimal.quantize`` in ``to_quantity`` and fifteen
    integer digits to overflow the column - both a 500; a fifth decimal place
    would have been rounded into a quantity nobody keyed.
    """
    sign_in(shop.dealer)
    lines_url = "/dealer-portal/cart/lines"
    price_url = f"/dealer-portal/catalog/items/{shop.item_id}/price"
    saved = ok(
        await api_client.post("/dealer-portal/wishlist", json={"item_id": str(shop.item_id)}),
        201,
    )
    move_url = f"/dealer-portal/wishlist/{saved['id']}/move-to-cart"
    added = ok(
        await api_client.post(lines_url, json={"item_id": str(shop.item_id), "quantity": "1.2345"}),
        201,
    )
    (line,) = added["lines"]
    assert Decimal(line["quantity"]) == Decimal("1.2345")

    for quantity in ("1e25", "100000000000000", "1.00001"):
        body = {"item_id": str(shop.item_id), "quantity": quantity}
        assert "quantity" in field_errors(await api_client.post(lines_url, json=body))
        assert "quantity" in field_errors(
            await api_client.patch(f"{lines_url}/{line['id']}", json={"quantity": quantity})
        )
        assert "query.quantity" in field_errors(
            await api_client.get(price_url, params={"quantity": quantity})
        )
        assert "query.quantity" in field_errors(
            await api_client.post(move_url, params={"quantity": quantity})
        )

    largest = ok(await api_client.get(price_url, params={"quantity": "99999999999999.9999"}))
    assert Decimal(largest["quantity"]) == Decimal("99999999999999.9999")
    (unchanged,) = ok(await api_client.get("/dealer-portal/cart"))["lines"]
    assert Decimal(unchanged["quantity"]) == Decimal("1.2345")


# -----------------------------------------------------------------------------
# Checkout
# -----------------------------------------------------------------------------


async def test_checkout_places_a_draft_portal_order_and_audits_it(
    api_client, session, tenant, principal, shop, decimal_equal
):
    address = CustomerAddress(
        company_id=tenant.company_id,
        customer_id=shop.ours_id,
        address_type=AddressType.SHIPPING,
        label="Shop",
        address_line1="12 Station Road",
        city="Rajkot",
        state="Gujarat",
        state_code="24",
        country="India",
        is_active=True,
        is_default=True,
    )
    session.add(address)
    await session.flush()
    address_id = address.id
    sign_in(shop.dealer)
    ok(
        await api_client.post(
            "/dealer-portal/cart/lines", json={"item_id": str(shop.item_id), "quantity": "2"}
        ),
        201,
    )
    preview = ok(await api_client.get("/dealer-portal/cart"))
    required = (today() + WEEK).isoformat()

    order = ok(
        await api_client.post(
            "/dealer-portal/checkout",
            json={
                "cart_version": preview["cart_version"],
                "required_date": required,
                "shipping_address_id": str(address_id),
                "customer_po_number": "PO-778",
                "notes": "Deliver before the festival",
            },
        ),
        201,
    )

    assert (order["status"], order["source"]) == ("DRAFT", "DEALER_PORTAL")
    assert (order["customer_po_number"], order["required_date"]) == ("PO-778", required)
    (line,) = order["lines"]
    assert line["item_id"] == str(shop.item_id) and line["offer_id"] == str(shop.ten_id)
    decimal_equal(line["quantity"], "2")
    decimal_equal(line["unit_price"], "90")
    decimal_equal(line["discount_percentage"], "10")
    # The server prices the order with the matcher the preview used.
    decimal_equal(order["total_amount"], preview["total_amount"])
    order_id = uuid.UUID(order["id"])
    placed = await session.scalar(select(SalesOrder).where(SalesOrder.id == order_id))
    assert placed is not None
    assert (placed.customer_id, placed.shipping_address_id) == (shop.ours_id, address_id)
    assert (str(placed.status), str(placed.source)) == ("DRAFT", "DEALER_PORTAL")
    (audit,) = await audit_rows(session, entity_id=order_id, entity_type="dealer_checkout")
    assert audit.action == AuditAction.CREATE
    assert (audit.created_by_id, audit.entity_code) == (shop.buyer_id, order["code"])
    assert ok(await api_client.get("/dealer-portal/cart"))["lines"] == []
    assert ok(await api_client.get(f"/dealer-portal/orders/{order_id}"))["code"] == order["code"]

    # The desk finds the request by its source, through the same two values the
    # source_known CHECK admits; anything else is a field error, not an empty page.
    sign_in(principal)
    portal = ok(await api_client.get("/sales", params={"source": "DEALER_PORTAL"}))
    assert order["id"] in [row["id"] for row in portal]
    assert {row["source"] for row in portal} == {"DEALER_PORTAL"}
    desk = ok(await api_client.get("/sales", params={"source": "ERP"}))
    assert order["id"] not in [row["id"] for row in desk]
    assert "query.source" in field_errors(
        await api_client.get("/sales", params={"source": "dealer_portal"})
    )


async def test_checkout_refusals_leave_the_cart_as_it_was(api_client, session, shop):
    sign_in(shop.dealer)
    ok(
        await api_client.post(
            "/dealer-portal/cart/lines", json={"item_id": str(shop.item_id), "quantity": "1"}
        ),
        201,
    )
    version = ok(await api_client.get("/dealer-portal/cart"))["cart_version"]
    required = (today() + WEEK).isoformat()
    checkout = "/dealer-portal/checkout"

    assert "required_date" in field_errors(
        await api_client.post(checkout, json={"cart_version": version})
    )
    assert "cart_version" in field_errors(
        await api_client.post(checkout, json={"cart_version": 0, "required_date": required})
    )
    stale = refused(
        await api_client.post(
            checkout, json={"cart_version": version + 1, "required_date": required}
        ),
        409,
        "CONFLICT",
    )
    assert stale["details"]["cart_version"] == version
    refused(
        await api_client.post(
            checkout,
            json={
                "cart_version": version,
                "required_date": (today() - timedelta(days=1)).isoformat(),
            },
        ),
        422,
        "VALIDATION_ERROR",
    )
    warehouse = await session.get(Warehouse, shop.warehouse_id)
    assert warehouse is not None
    warehouse.is_default = False
    await session.flush()
    nowhere = refused(
        await api_client.post(checkout, json={"cart_version": version, "required_date": required}),
        422,
        "BUSINESS_RULE_VIOLATION",
    )
    assert nowhere["details"]["missing"] == "warehouses.is_default"

    cart = ok(await api_client.get("/dealer-portal/cart"))
    assert len(cart["lines"]) == 1 and cart["cart_version"] == version
    assert (
        await session.scalar(
            select(func.count())
            .select_from(SalesOrder)
            .where(SalesOrder.customer_id == shop.ours_id)
        )
        == 0
    )


# -----------------------------------------------------------------------------
# Tracking
# -----------------------------------------------------------------------------


async def test_tracking_shows_the_consignment_without_the_driver_or_the_freight(
    api_client, session, tenant, account
):
    dispatch_id = await ship(
        session,
        tenant,
        account,
        courier_name="Gati Express",
        tracking_id="GATI-42-7781",
        lr_number="LR-9001",
        driver_name="Ramesh",
        driver_mobile="9876543210",
        freight_amount="450",
    )
    await dispatch_service.mark_in_transit(
        session,
        tenant,
        dispatch_id,
        InTransitRequest(reason="Left the Vadodara hub", location="Vadodara hub"),
    )
    sign_in(account.dealer)
    url = f"/dealer-portal/dispatches/{dispatch_id}/tracking"

    response = await api_client.get(url)
    tracking = ok(response)

    assert (tracking["id"], tracking["status"]) == (str(dispatch_id), "IN_TRANSIT")
    assert (tracking["courier_name"], tracking["tracking_id"], tracking["lr_number"]) == (
        "Gati Express",
        "GATI-42-7781",
        "LR-9001",
    )
    assert tracking["dispatched_at"] is not None and tracking["next_cursor"] is None
    assert [event["event_status"] for event in tracking["events"]] == [
        "READY_FOR_DISPATCH",
        "DISPATCHED",
        "IN_TRANSIT",
    ]
    assert tracking["events"][-1]["location"] == "Vadodara hub"
    for private in ("driver_name", "driver_mobile", "vehicle_number", "freight_amount"):
        assert private not in tracking, private
    assert "9876543210" not in response.text and "Ramesh" not in response.text

    refused(
        await api_client.get(f"/dealer-portal/dispatches/{uuid.uuid4()}/tracking"),
        404,
        "NOT_FOUND",
    )
    sign_in(account.stranger)
    refused(await api_client.get(url), 404, "NOT_FOUND")


# -----------------------------------------------------------------------------
# Invoices, PDF jobs and downloads
# -----------------------------------------------------------------------------


async def test_the_dealer_reads_its_own_invoices(api_client, account, invoiced, decimal_equal):
    sign_in(account.dealer)
    listed = await api_client.get("/dealer-portal/invoices")
    (row,) = ok(listed)
    assert listed.json()["pagination"]["total"] == 1
    assert (row["id"], row["dispatch_id"], row["status"]) == (
        str(invoiced.invoice_id),
        str(invoiced.dispatch_id),
        "ISSUED",
    )
    decimal_equal(row["credited_amount"], "0")
    decimal_equal(row["balance_amount"], str(invoiced.invoice_total))
    found = ok(
        await api_client.get("/dealer-portal/invoices", params={"search": invoiced.invoice_code})
    )
    assert [match["id"] for match in found] == [str(invoiced.invoice_id)]
    assert (
        ok(await api_client.get("/dealer-portal/invoices", params={"search": "NO-SUCH-BILL"})) == []
    )

    detail = ok(await api_client.get(f"/dealer-portal/invoices/{invoiced.invoice_id}"))
    assert detail["code"] == invoiced.invoice_code and len(detail["lines"]) == 1
    assert detail["payments"] == [] and detail["credit_notes"] == []
    decimal_equal(detail["total_amount"], str(invoiced.invoice_total))

    sign_in(account.stranger)
    refused(
        await api_client.get(f"/dealer-portal/invoices/{invoiced.invoice_id}"), 404, "NOT_FOUND"
    )
    assert ok(await api_client.get("/dealer-portal/invoices")) == []


async def test_an_invoice_pdf_is_queued_polled_and_downloaded(
    api_client, session, tenant, monkeypatch, broker, storage, account, invoiced
):
    monkeypatch.setattr(sales_invoice_service, "queue_sales_invoice_pdf", REAL_QUEUE_INVOICE_PDF)
    sign_in(account.dealer)

    job = ok(await api_client.post(f"/dealer-portal/invoices/{invoiced.invoice_id}/pdf"), 202)
    job_id = job["job_id"]
    assert job["status"] == "queued"
    assert job["status_url"] == f"/api/v1/dealer-portal/jobs/{job_id}"
    assert broker.count("reports.generate_sales_invoice") == 1
    snapshot = await session.scalar(
        select(ReportSnapshot).where(ReportSnapshot.id == uuid.UUID(job_id))
    )
    assert snapshot is not None
    assert (snapshot.created_by_id, str(snapshot.status)) == (account.buyer_id, "QUEUED")
    (audit,) = await audit_rows(
        session,
        entity_id=invoiced.invoice_id,
        entity_type="sales_invoice",
        action=AuditAction.JOB_STARTED,
    )
    assert audit.created_by_id == account.buyer_id
    assert str(audit.context["job_id"]) == job_id
    polled = ok(await api_client.get(f"/dealer-portal/jobs/{job_id}"))
    assert (polled["status"], polled["report_key"]) == ("QUEUED", "SALES_INVOICE")
    assert polled["download_url"] is None

    # The worker's part, through the calls it makes: claim the job, file the PDF
    # against the invoice, then complete the job with that document.
    worker = TenantContext(company_id=tenant.company_id, user_id=account.buyer_id)
    await reports_service.start_snapshot(session, worker, uuid.UUID(job_id))
    document_id = await stored(
        session,
        worker,
        PDF,
        "application/pdf",
        "sales-invoice.pdf",
        entity_type="sales_invoice",
        entity_id=invoiced.invoice_id,
        category=DocumentCategory.REPORT,
    )
    invoice_row = await session.get(SalesInvoice, invoiced.invoice_id)
    assert invoice_row is not None
    invoice_row.pdf_document_id = document_id
    await session.flush()
    await reports_service.complete_snapshot(
        session, worker, snapshot_id=uuid.UUID(job_id), row_count=1, document_id=document_id
    )

    done = ok(await api_client.get(f"/dealer-portal/jobs/{job_id}"))
    assert (done["status"], done["document_id"]) == ("COMPLETED", str(document_id))
    assert done["download_url"] == f"/api/v1/dealer-portal/documents/{document_id}/download"
    downloaded = await api_client.get(done["download_url"].removeprefix("/api/v1"))
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == PDF
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"

    sign_in(account.stranger)
    refused(await api_client.get(f"/dealer-portal/jobs/{job_id}"), 404, "NOT_FOUND")
    refused(
        await api_client.get(f"/dealer-portal/documents/{document_id}/download"), 404, "NOT_FOUND"
    )
    refused(
        await api_client.post(f"/dealer-portal/invoices/{invoiced.invoice_id}/pdf"),
        404,
        "NOT_FOUND",
    )


async def test_jobs_and_files_the_dealer_does_not_own_are_not_found(
    api_client, session, tenant, storage, account, invoiced
):
    # Queued by the sales desk for the dealer's own invoice: still not the dealer's job.
    desk_job = await REAL_QUEUE_INVOICE_PDF(session, tenant, invoiced.invoice_id)
    # Filed against the invoice, but not the PDF the invoice points at.
    draft = await stored(
        session,
        tenant,
        PDF,
        "application/pdf",
        "draft.pdf",
        entity_type="sales_invoice",
        entity_id=invoiced.invoice_id,
    )
    photo = await stored(
        session,
        tenant,
        PNG,
        "image/png",
        "photo.png",
        entity_type="item",
        entity_id=account.item_id,
    )
    sign_in(account.dealer)

    refused(await api_client.get(f"/dealer-portal/jobs/{desk_job}"), 404, "NOT_FOUND")
    refused(await api_client.get(f"/dealer-portal/jobs/{uuid.uuid4()}"), 404, "NOT_FOUND")
    for document_id in (draft, photo, uuid.uuid4()):
        refused(
            await api_client.get(f"/dealer-portal/documents/{document_id}/download"),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# Credit notes
# -----------------------------------------------------------------------------


async def test_the_dealer_reads_its_credit_notes(
    api_client, account, invoiced, credited, decimal_equal
):
    sign_in(account.dealer)
    listed = await api_client.get("/dealer-portal/credit-notes")
    (row,) = ok(listed)
    assert listed.json()["pagination"]["total"] == 1
    assert (row["id"], row["sales_invoice_id"], row["status"]) == (
        str(credited.note_id),
        str(invoiced.invoice_id),
        "ISSUED",
    )
    filtered = ok(
        await api_client.get(
            "/dealer-portal/credit-notes", params={"invoice_id": str(invoiced.invoice_id)}
        )
    )
    assert [match["id"] for match in filtered] == [str(credited.note_id)]
    detail = ok(await api_client.get(f"/dealer-portal/credit-notes/{credited.note_id}"))
    assert detail["code"] == credited.note_code and len(detail["lines"]) == 1
    decimal_equal(detail["total_amount"], str(credited.note_total))
    invoice = ok(await api_client.get(f"/dealer-portal/invoices/{invoiced.invoice_id}"))
    assert [note["id"] for note in invoice["credit_notes"]] == [str(credited.note_id)]
    decimal_equal(invoice["credited_amount"], str(credited.note_total))
    refused(
        await api_client.get(
            "/dealer-portal/credit-notes", params={"invoice_id": str(uuid.uuid4())}
        ),
        404,
        "NOT_FOUND",
    )

    sign_in(account.stranger)
    refused(
        await api_client.get(f"/dealer-portal/credit-notes/{credited.note_id}"), 404, "NOT_FOUND"
    )
    refused(
        await api_client.get(
            "/dealer-portal/credit-notes", params={"invoice_id": str(invoiced.invoice_id)}
        ),
        404,
        "NOT_FOUND",
    )
    assert ok(await api_client.get("/dealer-portal/credit-notes")) == []


async def test_a_credit_note_pdf_is_queued_for_the_dealer(
    api_client, session, broker, account, credited
):
    sign_in(account.dealer)

    job = ok(await api_client.post(f"/dealer-portal/credit-notes/{credited.note_id}/pdf"), 202)

    job_id = job["job_id"]
    assert job["status_url"] == f"/api/v1/dealer-portal/jobs/{job_id}"
    assert broker.count("reports.generate_credit_note") == 1
    snapshot = await session.scalar(
        select(ReportSnapshot).where(ReportSnapshot.id == uuid.UUID(job_id))
    )
    assert snapshot is not None and snapshot.created_by_id == account.buyer_id
    assert snapshot.parameters == {"credit_note_id": str(credited.note_id)}
    polled = ok(await api_client.get(f"/dealer-portal/jobs/{job_id}"))
    assert (polled["status"], polled["report_key"]) == ("QUEUED", "CREDIT_NOTE")

    sign_in(account.stranger)
    refused(
        await api_client.post(f"/dealer-portal/credit-notes/{credited.note_id}/pdf"),
        404,
        "NOT_FOUND",
    )


async def test_queueing_a_credit_note_pdf_is_audited(api_client, session, account, credited):
    sign_in(account.dealer)
    job = ok(await api_client.post(f"/dealer-portal/credit-notes/{credited.note_id}/pdf"), 202)

    trail = await audit_rows(session, entity_id=credited.note_id, action=AuditAction.JOB_STARTED)

    assert [str(row.context["job_id"]) for row in trail] == [job["job_id"]]
    assert trail[0].created_by_id == account.buyer_id


# -----------------------------------------------------------------------------
# Returns
# -----------------------------------------------------------------------------


async def test_the_dealer_requests_a_return_from_an_eligible_consignment(
    api_client, session, tenant, user, account, decimal_equal
):
    dispatch_id = await ship(session, tenant, account)
    sign_in(account.dealer)
    eligible = await api_client.get("/dealer-portal/returns/eligible-dispatches")
    (consignment,) = ok(eligible)
    assert consignment["id"] == str(dispatch_id) and consignment["dispatched_at"] is not None
    (line,) = consignment["lines"]
    assert line["item_id"] == str(account.item_id)
    decimal_equal(line["returnable_quantity"], "200")

    created = ok(
        await api_client.post(
            "/dealer-portal/returns",
            json={
                "dispatch_id": str(dispatch_id),
                "notes": "Two cartons crushed",
                "lines": [
                    {
                        "dispatch_line_id": line["dispatch_line_id"],
                        "quantity": "5",
                        "reason_code": "DAMAGED",
                        "note": "Crushed in transit",
                    }
                ],
            },
        ),
        201,
    )

    assert (created["status"], created["source"]) == ("DRAFT", "DEALER_PORTAL")
    assert (created["dispatch_id"], created["customer_id"]) == (
        str(dispatch_id),
        str(account.customer_id),
    )
    assert created["received_at"] is None
    # The request names the consignment and the item it returns, not their ids.
    assert created["dispatch_code"] == consignment["code"]
    (returned,) = created["lines"]
    assert (returned["item_code"], returned["item_name"]) == (line["item_code"], line["item_name"])
    assert returned["requires_qc"] is True and returned["reason_code"] == "DAMAGED"
    decimal_equal(returned["quantity"], "5")
    decimal_equal(created["credited_amount"], "0")
    return_id = uuid.UUID(created["id"])
    row = await session.scalar(select(SalesReturn).where(SalesReturn.id == return_id))
    assert row is not None and row.requested_by_user_id == account.buyer_id
    notified = await session.scalars(
        select(Notification.user_id).where(
            Notification.entity_id == return_id,
            Notification.notification_type == NotificationType.DEALER_RETURN_REQUEST_RECEIVED,
        )
    )
    # The desk hears of the request; the dealer's own accounts are never told.
    assert set(notified.all()) == {user.id}

    listed = await api_client.get("/dealer-portal/returns")
    assert [(match["id"], match["dispatch_code"]) for match in ok(listed)] == [
        (created["id"], consignment["code"])
    ]
    assert listed.json()["pagination"]["total"] == 1
    detail = ok(await api_client.get(f"/dealer-portal/returns/{return_id}"))
    assert detail["code"] == created["code"] and len(detail["lines"]) == 1
    assert detail["dispatch_code"] == consignment["code"]
    (after,) = ok(await api_client.get("/dealer-portal/returns/eligible-dispatches"))
    decimal_equal(after["lines"][0]["returnable_quantity"], "195")

    sign_in(account.stranger)
    refused(await api_client.get(f"/dealer-portal/returns/{return_id}"), 404, "NOT_FOUND")
    assert ok(await api_client.get("/dealer-portal/returns")) == []
    assert ok(await api_client.get("/dealer-portal/returns/eligible-dispatches")) == []


async def test_return_requests_outside_the_rules_are_refused(api_client, session, tenant, account):
    first = await ship(session, tenant, account, "100")
    second = await ship(session, tenant, account, "100")
    sign_in(account.dealer)
    eligible = {
        row["id"]: row["lines"][0]["dispatch_line_id"]
        for row in ok(await api_client.get("/dealer-portal/returns/eligible-dispatches"))
    }
    first_line, second_line = eligible[str(first)], eligible[str(second)]
    url = "/dealer-portal/returns"

    assert "_root" in field_errors(
        await api_client.post(url, json=return_body(first, (first_line, "1"), (first_line, "2")))
    )
    assert "lines.0.quantity" in field_errors(
        await api_client.post(url, json=return_body(first, (first_line, "0")))
    )
    assert "lines" in field_errors(await api_client.post(url, json=return_body(first)))
    # A line of another consignment is not returnable against this one.
    refused(
        await api_client.post(url, json=return_body(first, (second_line, "1"))),
        422,
        "BUSINESS_RULE_VIOLATION",
    )
    sign_in(account.stranger)
    refused(
        await api_client.post(url, json=return_body(first, (first_line, "1"))), 404, "NOT_FOUND"
    )

    sign_in(account.dealer)
    assert ok(await api_client.get(url)) == []


async def test_a_return_request_is_audited(api_client, session, tenant, account):
    dispatch_id = await ship(session, tenant, account)
    sign_in(account.dealer)
    (consignment,) = ok(await api_client.get("/dealer-portal/returns/eligible-dispatches"))
    line_id = consignment["lines"][0]["dispatch_line_id"]

    created = ok(
        await api_client.post(
            "/dealer-portal/returns", json=return_body(dispatch_id, (line_id, "2"))
        ),
        201,
    )

    trail = await audit_rows(session, entity_id=uuid.UUID(created["id"]), action=AuditAction.CREATE)
    assert [row.created_by_id for row in trail] == [account.buyer_id]


async def test_a_second_request_cannot_claim_more_than_is_still_returnable(
    api_client, session, tenant, account, decimal_equal
):
    dispatch_id = await ship(session, tenant, account)
    sign_in(account.dealer)
    (consignment,) = ok(await api_client.get("/dealer-portal/returns/eligible-dispatches"))
    line_id = consignment["lines"][0]["dispatch_line_id"]
    ok(
        await api_client.post(
            "/dealer-portal/returns", json=return_body(dispatch_id, (line_id, "150"))
        ),
        201,
    )
    (after,) = ok(await api_client.get("/dealer-portal/returns/eligible-dispatches"))
    decimal_equal(after["lines"][0]["returnable_quantity"], "50")

    over = await api_client.post(
        "/dealer-portal/returns", json=return_body(dispatch_id, (line_id, "100"))
    )

    refused(over, 422, "BUSINESS_RULE_VIOLATION")


# -----------------------------------------------------------------------------
# What a dealer sees of promotions
# -----------------------------------------------------------------------------


async def test_the_dealer_sees_only_live_promotions_meant_for_it(
    api_client, session, tenant, shop, storage, unique_code
):
    promotions = PromotionService(session, tenant)
    image_id = await stored(session, tenant, PNG, "image/png", "banner.png")

    def banner(**extra: Any) -> DealerBannerCreate:
        label = unique_code("B")
        return DealerBannerCreate(code=label, title=label, image_document_id=image_id, **extra)

    later = await promotions.create(
        "banners", banner(status=DealerBannerStatus.ACTIVE, sort_order=2)
    )
    first = await promotions.create(
        "banners", banner(status=DealerBannerStatus.ACTIVE, sort_order=1)
    )
    await promotions.create("banners", banner())
    await promotions.create(
        "banners", banner(status=DealerBannerStatus.ACTIVE, audience_price_type=PriceType.SALE)
    )
    negotiated = await promotions.create(
        "offers",
        DealerOfferCreate(
            code=unique_code("NEG"),
            title="Our negotiated scheme",
            scope_kind="ALL",
            customer_id=shop.ours_id,
            discount_percentage=Decimal("3"),
            is_active=True,
        ),
    )
    for customer_id, active in ((shop.theirs_id, True), (None, False)):
        await promotions.create(
            "offers",
            DealerOfferCreate(
                code=unique_code("HID"),
                title="Not for this dealer",
                scope_kind="ALL",
                customer_id=customer_id,
                discount_percentage=Decimal("4"),
                is_active=active,
            ),
        )
    expected_banners = [str(first.id), str(later.id)]
    expected_offers = {str(shop.ten_id), str(shop.bulk_id), str(negotiated.id)}
    negotiated_id = str(negotiated.id)
    sign_in(shop.dealer)

    banners = await api_client.get("/dealer-portal/banners")
    assert [row["id"] for row in ok(banners)] == expected_banners
    assert banners.json()["pagination"]["total"] == 2
    offers = {row["id"]: row for row in ok(await api_client.get("/dealer-portal/offers"))}
    assert set(offers) == expected_offers
    assert offers[negotiated_id]["customer_id"] == str(shop.ours_id)


async def test_the_image_route_serves_only_images_the_dealer_may_see(
    api_client, session, tenant, shop, storage, unique_code
):
    promotions = PromotionService(session, tenant)
    unsellable = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=unique_code("RM"), can_sell=False
    )
    banner_png = PNG + b"festive banner"
    product = await stored(
        session, tenant, PNG, "image/png", "front.png", entity_type="item", entity_id=shop.item_id
    )
    promo = await stored(session, tenant, banner_png, "image/png", "banner.png")
    internal = await stored(
        session, tenant, PNG, "image/png", "jig.png", entity_type="item", entity_id=unsellable.id
    )
    leaflet = await stored(
        session,
        tenant,
        PDF,
        "application/pdf",
        "leaflet.pdf",
        entity_type="item",
        entity_id=shop.item_id,
    )
    banner = await promotions.create(
        "banners",
        DealerBannerCreate(code=unique_code("B"), title="Festive range", image_document_id=promo),
    )
    banner_id = banner.id
    sign_in(shop.dealer)

    served = await api_client.get(f"/dealer-portal/images/{product}")
    assert served.status_code == 200, served.text
    assert served.content == PNG
    assert served.headers["content-type"] == "image/png"
    assert served.headers["content-disposition"].startswith("inline;")
    assert served.headers["cache-control"] == "private, max-age=300"
    assert served.headers["x-content-type-options"] == "nosniff"
    # An inactive banner's image, an unsellable item's photo, a PDF, nothing at all.
    for hidden in (promo, internal, leaflet, uuid.uuid4()):
        refused(await api_client.get(f"/dealer-portal/images/{hidden}"), 404, "NOT_FOUND")

    await promotions.set_status("banners", banner_id, DealerBannerStatus.ACTIVE, "Campaign live")
    live = await api_client.get(f"/dealer-portal/images/{promo}")
    assert live.status_code == 200, live.text
    assert live.content == banner_png


async def test_an_image_past_its_retention_date_is_neither_listed_nor_served(
    api_client, session, tenant, shop, storage
):
    # Between an image's retention date and the purge sweep the row is still
    # there, and the image route already answers 404 for it. The catalogue kept
    # listing the id, so the dealer saw a broken image - in the list as well,
    # when the lapsed file was the item's chosen primary image.
    front = await stored(
        session, tenant, PNG, "image/png", "front.png", entity_type="item", entity_id=shop.item_id
    )
    lapsed = await stored(
        session,
        tenant,
        PNG + b"last season",
        "image/png",
        "old.png",
        entity_type="item",
        entity_id=shop.item_id,
    )
    await products_service.set_item_image(session, tenant, shop.item_id, lapsed)
    await session.execute(
        update(Document)
        .where(Document.id == lapsed)
        .values(expires_at=utcnow() - timedelta(minutes=1))
    )
    sign_in(shop.dealer)

    detail = ok(await api_client.get(f"/dealer-portal/catalog/items/{shop.item_id}"))
    assert detail["image_ids"] == [str(front)]
    assert detail["primary_image_id"] == str(front)
    page = ok(
        await api_client.get("/dealer-portal/catalog/items", params={"search": shop.item_code})
    )
    assert [row["primary_image_id"] for row in page if row["id"] == str(shop.item_id)] == [
        str(front)
    ]
    refused(await api_client.get(f"/dealer-portal/images/{lapsed}"), 404, "NOT_FOUND")
    served = await api_client.get(f"/dealer-portal/images/{front}")
    assert served.status_code == 200, served.text
    assert served.content == PNG


async def test_a_banner_whose_image_has_lapsed_is_withdrawn_from_the_dealer(
    api_client, session, tenant, shop, storage, unique_code
):
    # The image route answers 404 for an image past its retention date, and for
    # one the purge sweep has since removed, while the banner list kept naming
    # both: every dealer saw a broken banner. A banner has no next image to take
    # its place, so it is withdrawn instead (docs/assumptions.md §PROMO-1).
    promotions = PromotionService(session, tenant)
    live_png = PNG + b"monsoon range"
    images = {
        "live": await stored(session, tenant, live_png, "image/png", "live.png"),
        "lapsed": await stored(session, tenant, PNG + b"last season", "image/png", "old.png"),
        "purged": await stored(session, tenant, PNG + b"swept", "image/png", "gone.png"),
    }
    banners: dict[str, uuid.UUID] = {}
    for order, (name, image_id) in enumerate(images.items()):
        created = await promotions.create(
            "banners",
            DealerBannerCreate(
                code=unique_code("B"),
                title=f"The {name} banner",
                image_document_id=image_id,
                status=DealerBannerStatus.ACTIVE,
                sort_order=order,
            ),
        )
        banners[name] = created.id
    await session.execute(
        update(Document)
        .where(Document.id == images["lapsed"])
        .values(expires_at=utcnow() - timedelta(minutes=1))
    )
    await session.execute(delete(Document).where(Document.id == images["purged"]))
    sign_in(shop.dealer)

    listed = await api_client.get("/dealer-portal/banners")
    assert [row["id"] for row in ok(listed)] == [str(banners["live"])]
    assert listed.json()["pagination"]["total"] == 1
    for gone in (images["lapsed"], images["purged"]):
        refused(await api_client.get(f"/dealer-portal/images/{gone}"), 404, "NOT_FOUND")
    served = await api_client.get(f"/dealer-portal/images/{images['live']}")
    assert served.status_code == 200, served.text
    assert served.content == live_png

    # Withdrawn from dealers, not retired: nothing moved a status.
    statuses = await session.scalars(
        select(DealerBanner.status).where(DealerBanner.id.in_(list(banners.values())))
    )
    assert sorted(statuses.all()) == [DealerBannerStatus.ACTIVE] * 3


# -----------------------------------------------------------------------------
# Staff promotion management: banners
# -----------------------------------------------------------------------------


async def test_a_banner_is_created_read_listed_and_edited(
    api_client, session, tenant, user, storage, unique_code
):
    item = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=unique_code("FG"), can_sell=True
    )
    item_id = item.id
    image_id = await stored(session, tenant, PNG, "image/png", "monsoon.png")
    code = unique_code("BNR")

    created = ok(
        await api_client.post(
            "/promotions/banners",
            json={
                "code": code,
                "title": "Monsoon range",
                "subtitle": "Rust-proof hinges",
                "image_document_id": str(image_id),
                "sort_order": 5,
            },
        ),
        201,
        message="Banner created",
    )

    assert (created["code"], created["status"], created["link_kind"]) == (code, "INACTIVE", "NONE")
    banner_id = uuid.UUID(created["id"])
    (audit,) = await audit_rows(session, entity_id=banner_id, entity_type="dealer_banner")
    assert audit.action == AuditAction.CREATE
    assert (audit.created_by_id, audit.entity_code) == (user.id, code)
    assert ok(await api_client.get(f"/promotions/banners/{banner_id}"))["title"] == "Monsoon range"
    listed = await api_client.get("/promotions/banners", params={"search": code})
    assert [row["id"] for row in ok(listed)] == [str(banner_id)]
    assert listed.json()["pagination"]["total"] == 1
    assert ok(await api_client.get("/promotions/banners", params={"search": "NO-SUCH-BNR"})) == []

    edited = ok(
        await api_client.patch(
            f"/promotions/banners/{banner_id}",
            json={
                "title": "Monsoon range 2026",
                "link_kind": "ITEM",
                "link_id": str(item_id),
                "sort_order": 1,
            },
        ),
        message="Banner updated",
    )

    assert (edited["title"], edited["link_kind"], edited["link_id"]) == (
        "Monsoon range 2026",
        "ITEM",
        str(item_id),
    )
    row = await session.scalar(select(DealerBanner).where(DealerBanner.id == banner_id))
    assert row is not None
    assert (row.title, row.link_id, row.sort_order) == ("Monsoon range 2026", item_id, 1)
    trail = await audit_rows(session, entity_id=banner_id, entity_type="dealer_banner")
    assert [entry.action for entry in trail] == [AuditAction.CREATE, AuditAction.UPDATE]


async def test_banner_payloads_and_edits_outside_the_rules_are_refused(
    api_client, session, tenant, storage, other_company, unique_code
):
    foreign = await seed_support.seed_item(
        session, company_id=other_company.id, code=unique_code("FG"), can_sell=True
    )
    foreign_id = foreign.id
    image_id = await stored(session, tenant, PNG, "image/png", "festive.png")
    base = {
        "code": unique_code("BNR"),
        "title": "Festive range",
        "image_document_id": str(image_id),
    }
    url = "/promotions/banners"

    assert "_root" in field_errors(await api_client.post(url, json={**base, "link_kind": "ITEM"}))
    assert "_root" in field_errors(await api_client.post(url, json={**base, "status": "ARCHIVED"}))
    assert "code" in field_errors(await api_client.post(url, json={**base, "code": "has spaces"}))
    # Another company's item is no target at all.
    refused(
        await api_client.post(url, json={**base, "link_kind": "ITEM", "link_id": str(foreign_id)}),
        404,
        "NOT_FOUND",
    )
    banner_id = ok(await api_client.post(url, json=base), 201)["id"]
    refused(await api_client.post(url, json=base), 409, "DUPLICATE_DOCUMENT")

    edit = f"{url}/{banner_id}"
    refused(await api_client.patch(edit, json={"status": "ACTIVE"}), 422, "BUSINESS_RULE_VIOLATION")
    # A rule across fields is checked on the merged banner, and still names a path.
    assert "_root" in field_errors(
        await api_client.patch(
            edit,
            json={
                "valid_from": today().isoformat(),
                "valid_to": (today() - timedelta(days=1)).isoformat(),
            },
        )
    )
    assert "code" in field_errors(await api_client.patch(edit, json={"code": "RENAMED"}))
    # An edit is held to the create's limits when it is parsed.
    for change, field in (
        ({"title": "x" * 161}, "title"),
        ({"title": None}, "title"),
        ({"subtitle": "x" * 2001}, "subtitle"),
        ({"sort_order": 100001}, "sort_order"),
    ):
        assert field in field_errors(await api_client.patch(edit, json=change)), change
    row = await session.scalar(select(DealerBanner).where(DealerBanner.id == uuid.UUID(banner_id)))
    assert row is not None
    assert (row.status, row.valid_to, row.code) == ("INACTIVE", None, base["code"])


async def test_a_banner_moves_through_its_lifecycle_with_a_reason_each_time(
    api_client, session, tenant, storage, unique_code
):
    image_id = await stored(session, tenant, PNG, "image/png", "season.png")
    code = unique_code("BNR")
    banner_id = ok(
        await api_client.post(
            "/promotions/banners",
            json={"code": code, "title": "Season", "image_document_id": str(image_id)},
        ),
        201,
    )["id"]
    base = f"/promotions/banners/{banner_id}"

    assert "reason" in field_errors(
        await api_client.post(f"{base}/activate", json={"reason": "  "})
    )
    active = ok(
        await api_client.post(f"{base}/activate", json={"reason": "Launch approved"}),
        message="Banner activated",
    )
    assert active["status"] == "ACTIVE"
    again = refused(
        await api_client.post(f"{base}/activate", json={"reason": "Launch approved"}),
        422,
        "INVALID_STATE_TRANSITION",
    )
    assert again["details"]["current_status"] == "ACTIVE"
    paused = ok(
        await api_client.post(f"{base}/deactivate", json={"reason": "Paused for stock"}),
        message="Banner deactivated",
    )
    assert paused["status"] == "INACTIVE"
    refused(
        await api_client.post(f"{base}/deactivate", json={"reason": "Paused again"}),
        422,
        "INVALID_STATE_TRANSITION",
    )
    retired = ok(
        await api_client.post(f"{base}/retire", json={"reason": "Season over"}),
        message="Banner retired",
    )
    assert retired["status"] == "ARCHIVED"

    row = await session.scalar(select(DealerBanner).where(DealerBanner.id == uuid.UUID(banner_id)))
    assert row is not None and row.status == "ARCHIVED" and row.deleted_at is not None
    trail = await audit_rows(session, entity_id=uuid.UUID(banner_id), entity_type="dealer_banner")
    assert reasons(trail) == [
        ("CREATE", None),
        ("UPDATE", "Launch approved"),
        ("UPDATE", "Paused for stock"),
        ("DELETE", "Season over"),
    ]
    # A retired banner is gone from management: no read, no list entry, no revival.
    refused(await api_client.get(base), 404, "NOT_FOUND")
    assert ok(await api_client.get("/promotions/banners", params={"search": code})) == []
    refused(
        await api_client.post(f"{base}/activate", json={"reason": "Bring it back"}),
        404,
        "NOT_FOUND",
    )


async def test_deleting_a_banner_or_an_offer_retires_it_with_a_reason(
    api_client, session, tenant, storage, unique_code
):
    image_id = await stored(session, tenant, PNG, "image/png", "old.png")
    banner_id = ok(
        await api_client.post(
            "/promotions/banners",
            json={"code": unique_code("BNR"), "title": "Old", "image_document_id": str(image_id)},
        ),
        201,
    )["id"]
    offer_id = ok(
        await api_client.post(
            "/promotions/offers",
            json={
                "code": unique_code("SCH"),
                "title": "Old scheme",
                "scope_kind": "ALL",
                "discount_percentage": "5",
                "is_active": True,
            },
        ),
        201,
    )["id"]

    assert "reason" in field_errors(
        await api_client.request("DELETE", f"/promotions/banners/{banner_id}", json={})
    )
    banner = ok(
        await api_client.request(
            "DELETE", f"/promotions/banners/{banner_id}", json={"reason": "Replaced by festive"}
        ),
        message="Banner retired",
    )
    offer = ok(
        await api_client.request(
            "DELETE", f"/promotions/offers/{offer_id}", json={"reason": "Scheme withdrawn"}
        ),
        message="Offer retired",
    )

    assert banner["status"] == "ARCHIVED"
    assert (offer["status"], offer["is_active"]) == ("ARCHIVED", False)
    for resource, identity, reason in (
        ("banners", banner_id, "Replaced by festive"),
        ("offers", offer_id, "Scheme withdrawn"),
    ):
        (audit,) = await audit_rows(
            session, entity_id=uuid.UUID(identity), action=AuditAction.DELETE
        )
        assert audit.context == {"reason": reason}
        refused(
            await api_client.request(
                "DELETE", f"/promotions/{resource}/{identity}", json={"reason": "Twice over"}
            ),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# Staff promotion management: offers
# -----------------------------------------------------------------------------


async def test_an_offer_is_created_read_listed_edited_and_previewed(
    api_client, session, tenant, user, unique_code, decimal_equal
):
    parent = await seed_support.seed_category(session, company_id=tenant.company_id)
    child = await seed_support.seed_category(session, company_id=tenant.company_id, parent=parent)
    elsewhere = await seed_support.seed_category(session, company_id=tenant.company_id)
    for category, can_sell in ((parent, True), (child, True), (child, False), (elsewhere, True)):
        await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("FG"),
            category_id=category.id,
            can_sell=can_sell,
            is_active=True,
        )
    parent_id = parent.id
    code = unique_code("SCH")

    created = ok(
        await api_client.post(
            "/promotions/offers",
            json={
                "code": code,
                "title": "Handles festival",
                "scope_kind": "CATEGORY",
                "category_id": str(parent_id),
                "discount_percentage": "12.5",
                "min_quantity": "5",
            },
        ),
        201,
        message="Offer created",
    )

    assert (created["status"], created["is_active"]) == ("INACTIVE", False)
    assert created["category_id"] == str(parent_id)
    decimal_equal(created["discount_percentage"], "12.5")
    decimal_equal(created["min_quantity"], "5")
    offer_id = uuid.UUID(created["id"])
    (audit,) = await audit_rows(session, entity_id=offer_id, entity_type="dealer_offer")
    assert (audit.action, audit.created_by_id) == (AuditAction.CREATE, user.id)
    assert ok(await api_client.get(f"/promotions/offers/{offer_id}"))["code"] == code
    listed = ok(await api_client.get("/promotions/offers", params={"search": code}))
    assert [row["id"] for row in listed] == [str(offer_id)]
    # The parent's sellable item and the child's: the subtree, sellable only.
    assert ok(await api_client.get(f"/promotions/offers/{offer_id}/preview")) == {"item_count": 2}

    edited = ok(
        await api_client.patch(
            f"/promotions/offers/{offer_id}",
            json={"title": "Handles festival - extended", "discount_percentage": "15"},
        ),
        message="Offer updated",
    )

    decimal_equal(edited["discount_percentage"], "15")
    row = await session.scalar(select(DealerOffer).where(DealerOffer.id == offer_id))
    assert row is not None and row.title == "Handles festival - extended"
    decimal_equal(row.discount_percentage, "15")
    trail = await audit_rows(session, entity_id=offer_id, entity_type="dealer_offer")
    assert [entry.action for entry in trail] == [AuditAction.CREATE, AuditAction.UPDATE]


async def test_offer_payloads_and_edits_outside_the_rules_are_refused(
    api_client, session, tenant, other_company, unique_code
):
    item = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=unique_code("FG"), can_sell=True
    )
    foreign = await seed_support.seed_item(
        session, company_id=other_company.id, code=unique_code("FG"), can_sell=True
    )
    item_id, foreign_id = item.id, foreign.id
    url = "/promotions/offers"
    base = {
        "code": unique_code("SCH"),
        "title": "Ten off the lever",
        "scope_kind": "ITEM",
        "item_id": str(item_id),
        "discount_percentage": "10",
    }

    assert "_root" in field_errors(await api_client.post(url, json={**base, "scope_kind": "BRAND"}))
    assert "discount_percentage" in field_errors(
        await api_client.post(url, json={**base, "discount_percentage": "150"})
    )
    refused(await api_client.post(url, json={**base, "item_id": str(foreign_id)}), 404, "NOT_FOUND")
    offer_id = ok(await api_client.post(url, json=base), 201)["id"]

    edit = f"{url}/{offer_id}"
    # ALL with the item still named is no longer a consistent scope.
    assert "_root" in field_errors(await api_client.patch(edit, json={"scope_kind": "ALL"}))
    refused(await api_client.patch(edit, json={"item_id": str(foreign_id)}), 404, "NOT_FOUND")
    assert "unknown" in field_errors(await api_client.patch(edit, json={"unknown": "1"}))
    for change, field in (
        ({"discount_percentage": "150"}, "discount_percentage"),
        ({"discount_percentage": "0"}, "discount_percentage"),
        ({"min_quantity": "1e25"}, "min_quantity"),
        ({"title": "x" * 161}, "title"),
        ({"description": "x" * 2001}, "description"),
        ({"sort_order": -1}, "sort_order"),
    ):
        assert field in field_errors(await api_client.patch(edit, json=change)), change
    row = await session.scalar(select(DealerOffer).where(DealerOffer.id == uuid.UUID(offer_id)))
    assert row is not None and (row.scope_kind, row.item_id) == ("ITEM", item_id)


async def test_an_offer_moves_through_its_lifecycle_with_a_reason_each_time(
    api_client, session, unique_code
):
    offer_id = ok(
        await api_client.post(
            "/promotions/offers",
            json={
                "code": unique_code("SCH"),
                "title": "Year-end scheme",
                "scope_kind": "ALL",
                "discount_percentage": "7.5",
            },
        ),
        201,
    )["id"]
    base = f"/promotions/offers/{offer_id}"

    active = ok(
        await api_client.post(f"{base}/activate", json={"reason": "Approved by sales"}),
        message="Offer activated",
    )
    assert (active["status"], active["is_active"]) == ("ACTIVE", True)
    refused(
        await api_client.post(f"{base}/activate", json={"reason": "Approved twice"}),
        422,
        "INVALID_STATE_TRANSITION",
    )
    paused = ok(
        await api_client.post(f"{base}/deactivate", json={"reason": "Margin review"}),
        message="Offer deactivated",
    )
    assert (paused["status"], paused["is_active"]) == ("INACTIVE", False)
    refused(
        await api_client.post(f"{base}/deactivate", json={"reason": "Paused twice"}),
        422,
        "INVALID_STATE_TRANSITION",
    )
    retired = ok(
        await api_client.post(f"{base}/retire", json={"reason": "Scheme closed"}),
        message="Offer retired",
    )
    assert retired["status"] == "ARCHIVED"

    row = await session.scalar(select(DealerOffer).where(DealerOffer.id == uuid.UUID(offer_id)))
    assert row is not None and row.is_active is False and row.deleted_at is not None
    trail = await audit_rows(session, entity_id=uuid.UUID(offer_id), entity_type="dealer_offer")
    assert reasons(trail) == [
        ("CREATE", None),
        ("UPDATE", "Approved by sales"),
        ("UPDATE", "Margin review"),
        ("DELETE", "Scheme closed"),
    ]
    refused(await api_client.get(base), 404, "NOT_FOUND")
    refused(await api_client.get(f"{base}/preview"), 404, "NOT_FOUND")


async def test_another_companys_promotions_are_not_found(
    api_client, session, user, other_company, unique_code
):
    theirs = await PromotionService(
        session, TenantContext(company_id=other_company.id, user_id=user.id)
    ).create(
        "offers",
        DealerOfferCreate(
            code=unique_code("SCH"),
            title="Their scheme",
            scope_kind="ALL",
            discount_percentage=Decimal("5"),
        ),
    )
    offer_id = theirs.id
    base = f"/promotions/offers/{offer_id}"

    refused(await api_client.get(base), 404, "NOT_FOUND")
    refused(await api_client.get(f"{base}/preview"), 404, "NOT_FOUND")
    refused(await api_client.patch(base, json={"title": "Hijacked"}), 404, "NOT_FOUND")
    refused(
        await api_client.post(f"{base}/activate", json={"reason": "Not ours"}), 404, "NOT_FOUND"
    )
    refused(await api_client.request("DELETE", base, json={"reason": "Not ours"}), 404, "NOT_FOUND")
    assert ok(await api_client.get("/promotions/offers")) == []

    row = await session.scalar(select(DealerOffer).where(DealerOffer.id == offer_id))
    assert row is not None
    assert (row.title, row.is_active, row.deleted_at) == ("Their scheme", False, None)
