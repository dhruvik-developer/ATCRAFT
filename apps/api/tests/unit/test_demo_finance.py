"""Demo documents must resume safely and agree with their real source documents."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.demo_data import DemoContext
from src.core.demo_data import finance as demo_finance
from src.core.deps import Principal, TenantContext
from src.core.errors import BusinessRuleError
from src.modules.finance.models import VendorInvoiceStatus
from src.modules.finance.schemas import VendorInvoiceLineIn
from src.modules.sales_returns import service as returns_service
from src.modules.sales_returns.models import SalesReturnStatus

pytestmark = pytest.mark.unit


@pytest.fixture
def ctx() -> DemoContext:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    tenant = TenantContext(company_id=company_id, user_id=user_id, is_superuser=True)
    principal = Principal(
        company_id=company_id,
        user_id=user_id,
        email="seed@example.test",
        full_name="Seed operator",
        is_superuser=True,
        permissions=frozenset(),
    )
    return DemoContext(
        cast(AsyncSession, Mock(spec=AsyncSession)), tenant, principal, company_id, user_id
    )


def receipt_pair(*, accepted: str, returned: str, received: str = "800") -> tuple:
    return (
        SimpleNamespace(
            id=uuid.uuid4(),
            item_id=uuid.uuid4(),
            uom_id=uuid.uuid4(),
            accepted_quantity=Decimal(accepted),
            returned_quantity=Decimal(returned),
            received_quantity=Decimal(received),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            quantity=Decimal("800"),
            unit_price=Decimal("10"),
            discount=Decimal("800"),
            tax_rate=Decimal("18"),
        ),
    )


@pytest.mark.parametrize(
    ("bill_accepted", "expected"), [(True, Decimal("720")), (False, Decimal("800"))]
)
async def test_receipt_bill_uses_net_accepted_quantity_but_preserves_overbill_example(
    ctx: DemoContext, bill_accepted: bool, expected: Decimal
) -> None:
    result = Mock()
    result.all.return_value = [receipt_pair(accepted="780", returned="60")]
    ctx.session.execute = AsyncMock(return_value=result)

    lines = await demo_finance._lines_from_receipt(
        ctx,
        order_id=uuid.uuid4(),
        receipt_id=uuid.uuid4(),
        accepted=bill_accepted,
        interstate=False,
    )

    assert len(lines) == 1
    assert lines[0].quantity == expected
    assert lines[0].unit_price == Decimal("9")
    assert lines[0].discount_amount == Decimal("0")


async def test_receipt_bill_omits_fully_returned_or_rejected_lines(ctx: DemoContext) -> None:
    result = Mock()
    result.all.return_value = [
        receipt_pair(accepted="60", returned="60"),
        receipt_pair(accepted="0", returned="0"),
    ]
    ctx.session.execute = AsyncMock(return_value=result)

    assert (
        await demo_finance._lines_from_receipt(
            ctx,
            order_id=uuid.uuid4(),
            receipt_id=uuid.uuid4(),
            accepted=True,
            interstate=True,
        )
        == []
    )


def invoice_line(quantity: str = "720") -> VendorInvoiceLineIn:
    return VendorInvoiceLineIn(
        item_id=uuid.uuid4(),
        purchase_order_line_id=uuid.uuid4(),
        goods_receipt_line_id=uuid.uuid4(),
        uom_id=uuid.uuid4(),
        quantity=Decimal(quantity),
        unit_price=Decimal("9"),
        tax_rate=Decimal("18"),
    )


@pytest.mark.parametrize("stale", [True, False])
async def test_existing_matched_bill_is_amended_only_when_source_figures_changed(
    ctx: DemoContext, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    spec = demo_finance.ORDERED_INVOICES[0]
    marker = ctx.code(*spec.marker)
    invoice_id = uuid.uuid4()
    ctx.remember("purchase_order", ctx.code(*spec.order), uuid.uuid4())
    ctx.remember("goods_receipt", ctx.code(*spec.receipt), uuid.uuid4())
    ctx.remember("vendor", ctx.code(spec.vendor), uuid.uuid4())
    expected = invoice_line()
    existing = expected.model_copy(update={"quantity": Decimal("780")}) if stale else expected
    get_detail = AsyncMock(return_value=SimpleNamespace(lines=[existing]))
    update = AsyncMock(return_value=SimpleNamespace(status=VendorInvoiceStatus.PENDING_APPROVAL))
    walk = AsyncMock()
    monkeypatch.setattr(demo_finance, "_lines_from_receipt", AsyncMock(return_value=[expected]))
    monkeypatch.setattr(demo_finance.finance, "get_invoice_detail", get_detail)
    monkeypatch.setattr(demo_finance.finance, "update_invoice", update)
    monkeypatch.setattr(demo_finance, "_walk_invoice", walk)

    await demo_finance._seed_ordered_invoice(
        ctx, spec, {marker: (invoice_id, VendorInvoiceStatus.PENDING_APPROVAL)}, set()
    )

    assert update.await_count == int(stale)
    if stale:
        payload = update.await_args.args[3]
        assert payload.lines == [expected]
    walk.assert_awaited_once_with(
        ctx,
        marker,
        invoice_id,
        VendorInvoiceStatus.PENDING_APPROVAL,
        VendorInvoiceStatus.APPROVED,
        override_reason=None,
    )
    assert ctx.counts["finance"] == {"created": 0, "existing": 1}


@pytest.mark.parametrize(
    ("spec_index", "status"),
    [(0, VendorInvoiceStatus.PARTIALLY_PAID), (1, VendorInvoiceStatus.PENDING_APPROVAL)],
)
async def test_invoice_repair_preserves_posted_bills_and_intentional_variance(
    ctx: DemoContext,
    monkeypatch: pytest.MonkeyPatch,
    spec_index: int,
    status: VendorInvoiceStatus,
) -> None:
    spec = demo_finance.ORDERED_INVOICES[spec_index]
    lines = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr(demo_finance, "_lines_from_receipt", lines)
    monkeypatch.setattr(demo_finance.finance, "update_invoice", update)
    monkeypatch.setattr(demo_finance, "_walk_invoice", AsyncMock())

    await demo_finance._seed_ordered_invoice(
        ctx, spec, {ctx.code(*spec.marker): (uuid.uuid4(), status)}, set()
    )

    lines.assert_not_awaited()
    update.assert_not_awaited()


async def test_new_sales_returns_use_services_and_register_automatic_credit_notes(
    ctx: DemoContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = demo_finance._SalesReturnSource(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    source_lookup = AsyncMock(return_value=source)
    created_ids: list[uuid.UUID] = []

    async def create(*args: object) -> SimpleNamespace:
        row_id = uuid.uuid4()
        created_ids.append(row_id)
        return SimpleNamespace(id=row_id, status=SalesReturnStatus.DRAFT)

    async def transition(*args: object) -> SimpleNamespace:
        return SimpleNamespace(id=args[2], status=args[3])

    create_return = AsyncMock(side_effect=create)
    transition_return = AsyncMock(side_effect=transition)
    credits = [(uuid.uuid4(), "CN-2026-00001"), (uuid.uuid4(), "CN-2026-00002")]
    monkeypatch.setattr(demo_finance, "_existing", AsyncMock(return_value={}))
    monkeypatch.setattr(demo_finance, "_sales_return_source", source_lookup)
    monkeypatch.setattr(demo_finance, "_return_credit_notes", AsyncMock(side_effect=[[], credits]))
    monkeypatch.setattr(returns_service, "create_return", create_return)
    monkeypatch.setattr(returns_service, "transition_return", transition_return)

    await demo_finance._seed_sales_returns(ctx)

    source_lookup.assert_awaited_once_with(ctx, Decimal("3"))
    assert create_return.await_count == 3
    for call, suffix in zip(
        create_return.await_args_list, ("DRAFT", "RECEIVED", "CLOSED"), strict=True
    ):
        payload = call.args[2]
        assert payload.dispatch_id == source.dispatch_id
        assert payload.return_warehouse_id == source.warehouse_id
        assert payload.notes.startswith(f"[DM-SR-{suffix}]")
        assert payload.lines[0].dispatch_line_id == source.dispatch_line_id
        assert payload.lines[0].quantity == Decimal("1")
        assert payload.lines[0].requires_qc is False
    assert [(call.args[2], call.args[3]) for call in transition_return.await_args_list] == [
        (created_ids[1], SalesReturnStatus.RECEIVED),
        (created_ids[2], SalesReturnStatus.RECEIVED),
        (created_ids[2], SalesReturnStatus.CLOSED),
    ]
    assert ctx.counts["finance"] == {"created": 5, "existing": 0}
    assert ctx.refs["credit_note"] == {code: row_id for row_id, code in credits}


@pytest.mark.parametrize("interrupted", [False, True])
async def test_sales_return_rerun_reuses_ids_and_resumes_only_unfinished_transitions(
    ctx: DemoContext, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    known = {
        ctx.code("SR", suffix): SimpleNamespace(id=uuid.uuid4())
        for suffix, _ in demo_finance.SALES_RETURN_EXAMPLES
    }
    states = {
        known[ctx.code("SR", suffix)].id: SalesReturnStatus.DRAFT if interrupted else target
        for suffix, target in demo_finance.SALES_RETURN_EXAMPLES
    }
    credits = [(uuid.uuid4(), "CN-2026-00001"), (uuid.uuid4(), "CN-2026-00002")]

    async def get_return(*args: object) -> SimpleNamespace:
        return SimpleNamespace(id=args[2], status=states[args[2]])

    async def transition(*args: object) -> SimpleNamespace:
        return SimpleNamespace(id=args[2], status=args[3])

    create = AsyncMock()
    source = AsyncMock()
    transition_return = AsyncMock(side_effect=transition)
    monkeypatch.setattr(demo_finance, "_existing", AsyncMock(return_value=known))
    monkeypatch.setattr(demo_finance, "_sales_return_source", source)
    monkeypatch.setattr(demo_finance, "_return_credit_notes", AsyncMock(return_value=credits))
    monkeypatch.setattr(returns_service, "create_return", create)
    monkeypatch.setattr(returns_service, "get_return", AsyncMock(side_effect=get_return))
    monkeypatch.setattr(returns_service, "transition_return", transition_return)

    await demo_finance._seed_sales_returns(ctx)

    create.assert_not_awaited()
    source.assert_not_awaited()
    assert transition_return.await_count == (3 if interrupted else 0)
    assert ctx.counts["finance"] == {"created": 0, "existing": 5}
    assert ctx.refs["sales_return"] == {marker: row.id for marker, row in known.items()}


async def test_sales_return_seed_without_eligible_delivery_reports_skips_without_writes(
    ctx: DemoContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = AsyncMock()
    monkeypatch.setattr(demo_finance, "_existing", AsyncMock(return_value={}))
    monkeypatch.setattr(demo_finance, "_sales_return_source", AsyncMock(return_value=None))
    monkeypatch.setattr(demo_finance, "_return_credit_notes", AsyncMock(return_value=[]))
    monkeypatch.setattr(returns_service, "create_return", create)

    await demo_finance._seed_sales_returns(ctx)

    create.assert_not_awaited()
    assert len(ctx.skipped) == 3
    assert all("no invoiced demo delivery" in reason for _, reason in ctx.skipped)


async def test_return_receipt_refusal_preserves_created_ids_for_the_next_run(
    ctx: DemoContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = demo_finance._SalesReturnSource(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    monkeypatch.setattr(demo_finance, "_existing", AsyncMock(return_value={}))
    monkeypatch.setattr(demo_finance, "_sales_return_source", AsyncMock(return_value=source))
    monkeypatch.setattr(demo_finance, "_return_credit_notes", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        returns_service,
        "create_return",
        AsyncMock(
            side_effect=[
                SimpleNamespace(id=row_id, status=SalesReturnStatus.DRAFT) for row_id in ids
            ]
        ),
    )
    receive = AsyncMock(side_effect=BusinessRuleError("Shipment is no longer returnable"))
    monkeypatch.setattr(returns_service, "transition_return", receive)

    await demo_finance._seed_sales_returns(ctx)

    assert list(ctx.refs["sales_return"].values()) == ids
    assert ctx.counts["finance"] == {"created": 3, "existing": 0}
    assert len(ctx.skipped) == 2
    assert [call.args[3] for call in receive.await_args_list] == [
        SalesReturnStatus.RECEIVED,
        SalesReturnStatus.RECEIVED,
    ]


@pytest.mark.parametrize("expected_failure", [True, False])
async def test_sales_return_seed_reports_business_refusals_but_exposes_implementation_errors(
    ctx: DemoContext, monkeypatch: pytest.MonkeyPatch, expected_failure: bool
) -> None:
    failure = BusinessRuleError("Source changed") if expected_failure else RuntimeError("Defect")
    source = demo_finance._SalesReturnSource(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    monkeypatch.setattr(demo_finance, "_existing", AsyncMock(return_value={}))
    monkeypatch.setattr(demo_finance, "_sales_return_source", AsyncMock(return_value=source))
    monkeypatch.setattr(demo_finance, "_return_credit_notes", AsyncMock(return_value=[]))
    monkeypatch.setattr(returns_service, "create_return", AsyncMock(side_effect=failure))

    if expected_failure:
        await demo_finance._seed_sales_returns(ctx)
        assert len(ctx.skipped) == 3
        assert all("Source changed" in reason for _, reason in ctx.skipped)
    else:
        with pytest.raises(RuntimeError, match="Defect"):
            await demo_finance._seed_sales_returns(ctx)
