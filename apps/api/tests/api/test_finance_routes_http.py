"""Finance write routes over HTTP, end to end on real rows.

``tests/unit/test_route_authorization.py`` and ``tests/api/test_route_contract_http.py``
already prove that every route refuses an anonymous or unentitled caller and
answers an unknown id with 404. Neither runs a handler body to completion, and
eighteen finance write routes had never been executed by any backend test. This
file walks each one: the status code and envelope it answers with, the rows it
leaves behind - the document, its ledger legs, the customer's cached balance,
the job row - and the audit entry written in the same transaction, the 422 a
payload or a document's state earns, and the 404 a caller from another company
receives for a row that is not theirs.

Two benches, both the integration suites' own builders:

``purchase``
    ``test_finance_service.seed``: a vendor, an item, a purchase order for 100
    at 25 and the goods receipt that took all of it in. A matching bill is
    2 500 + 18% GST = 2 950.
``bench``
    ``test_sales_invoice_service.bench``: a confirmed sales order with 200 units
    reserved, which ``make_dispatch`` ships and ``issue`` invoices.

Celery's ``send_task`` is stubbed for every test - it is the one door to the
broker - and records what it was handed. Ids are held as plain values: an
expected failure rolls its savepoint back and expires whatever was loaded before
it (CLAUDE.md), so no test reads an attribute off a fixture row after a refusal.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import frozen_time, today, utcnow
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.audit.models import ApprovalRequest, AuditAction, AuditLog
from src.modules.audit.schemas import ApprovalStatus
from src.modules.customers.models import Customer
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.schemas import DispatchCreate
from src.modules.finance import credit_note_service
from src.modules.finance import service as finance_service
from src.modules.finance.credit_note_schemas import CreditNoteCreate, CreditNoteLineIn
from src.modules.finance.models import (
    CreditNote,
    CreditNoteReason,
    LedgerAccount,
    LedgerAccountType,
    LedgerEntry,
    MatchStatus,
    PartyType,
    Payment,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
    PurchaseReturnDebitAllocation,
    SalesInvoice,
    SalesInvoiceLine,
    SalesInvoiceStatus,
    SystemAccount,
    VendorInvoice,
    VendorInvoiceStatus,
)
from src.modules.finance.schemas import (
    AllocationRequest,
    LedgerAccountCreate,
    PaymentAllocationIn,
    PaymentCreate,
)
from src.modules.procurement.models import PurchaseReturn, PurchaseReturnStatus
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportJobStatus, ReportKey, ReportSnapshot
from src.modules.sales.models import SalesOrderLine
from src.workers import celery_app as celery_module
from tests import seed_support
from tests.api.dealer_portal_support import sign_in
from tests.integration.test_finance_service import (
    approved_invoice,
    invoice_payload,
    posted_purchase_return,
    seed,
)
from tests.integration.test_sales_invoice_service import bench as bench
from tests.integration.test_sales_invoice_service import issue, make_dispatch

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "Recorded by the finance route suite"
#: The invoice date ``invoice_payload`` bills on, and so the payable's entry date.
BILL_DATE = date(2026, 4, 6)
#: A matching bill: 100 at 25 plus 18% GST.
BILL_TOTAL = Decimal("2950")


# -----------------------------------------------------------------------------
# Envelope and row helpers
# -----------------------------------------------------------------------------


def ok(response: Response, status: int = 200) -> Any:
    """The success envelope's ``data``, after checking the status and the flag."""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
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


def money(value: object) -> Decimal:
    """JSON carries money as a string; compare by value, not by spelling."""
    return Decimal(str(value))


def by_value(amounts: dict[str, Any]) -> dict[str, Decimal]:
    return {key: money(amount) for key, amount in amounts.items()}


async def value(session: AsyncSession, attribute: Any, row_id: uuid.UUID) -> Any:
    """One column of one row, read from the database rather than the identity map."""
    model = attribute.class_
    return await session.scalar(select(attribute).where(model.id == row_id))


async def audit_trail(
    session: AsyncSession,
    entity_id: uuid.UUID,
    *,
    action: AuditAction | None = None,
    entity_type: str | None = None,
) -> list[AuditLog]:
    # (created_at, id): rows written in one transaction share now().
    stmt = select(AuditLog).where(AuditLog.entity_id == entity_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    return list((await session.scalars(stmt.order_by(AuditLog.created_at, AuditLog.id))).all())


def status_moves(trail: list[AuditLog]) -> list[tuple[Any, Any]]:
    return [
        ((entry.old_values or {}).get("status"), (entry.new_values or {}).get("status"))
        for entry in trail
    ]


async def ledger_legs(session: AsyncSession, reference_id: uuid.UUID) -> list[LedgerEntry]:
    stmt = (
        select(LedgerEntry)
        .where(LedgerEntry.reference_id == reference_id)
        .order_by(LedgerEntry.created_at, LedgerEntry.id)
    )
    return list((await session.scalars(stmt)).all())


async def journal_legs(session: AsyncSession, journal_id: uuid.UUID) -> list[LedgerEntry]:
    """A journal and every leg that reverses one of its legs."""
    originals = select(LedgerEntry.id).where(LedgerEntry.journal_id == journal_id)
    stmt = (
        select(LedgerEntry)
        .where(
            (LedgerEntry.journal_id == journal_id) | LedgerEntry.reverses_entry_id.in_(originals)
        )
        .order_by(LedgerEntry.created_at, LedgerEntry.id)
    )
    return list((await session.scalars(stmt)).all())


def balanced(legs: list[LedgerEntry]) -> Decimal:
    """The journal's size, after checking that its debits equal its credits."""
    debit = sum((leg.debit_amount for leg in legs), Decimal("0"))
    credit = sum((leg.credit_amount for leg in legs), Decimal("0"))
    assert debit == credit, (debit, credit)
    return debit


def party_net(legs: list[LedgerEntry], party_type: PartyType) -> Decimal:
    """Credits less debits on the legs naming a party: what the ledger says is owed."""
    return sum(
        (leg.credit_amount - leg.debit_amount for leg in legs if leg.party_type == party_type),
        Decimal("0"),
    )


async def snapshot(session: AsyncSession, job_id: uuid.UUID) -> ReportSnapshot:
    return (
        await session.execute(select(ReportSnapshot).where(ReportSnapshot.id == job_id))
    ).scalar_one()


def as_tenant(principal: Principal) -> TenantContext:
    return TenantContext(company_id=principal.company_id, user_id=principal.user_id)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def broker(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every task handed to Celery, with its kwargs. Nothing reaches a broker."""
    sent: list[tuple[str, dict[str, Any]]] = []

    def accepted(name: str, *_: Any, kwargs: dict[str, Any] | None = None, **__: Any) -> Any:
        sent.append((name, dict(kwargs or {})))
        return SimpleNamespace(id=uuid.uuid4().hex)

    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted)
    return sent


@pytest.fixture
async def outsider(session: AsyncSession, other_company) -> Principal:
    """A fully entitled user of another company."""
    user = await seed_support.seed_user(session, company_id=other_company.id)
    return Principal(
        user_id=user.id,
        company_id=other_company.id,
        email=user.email,
        full_name=user.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("OUTSIDER",),
    )


@pytest.fixture
async def purchase(session: AsyncSession, tenant: TenantContext) -> SimpleNamespace:
    """The purchase bench, as plain ids plus the builder object for setup calls."""
    fixture = await seed(session, tenant.company_id)
    return SimpleNamespace(
        bench=fixture,
        vendor_id=fixture.vendor.id,
        item_id=fixture.item.id,
        uom_id=fixture.uom.id,
        order_id=fixture.order.id,
        order_line_id=fixture.order_line.id,
        receipt_id=fixture.receipt.id,
        receipt_line_id=fixture.receipt_line.id,
        warehouse_id=fixture.receipt.warehouse_id,
    )


# -----------------------------------------------------------------------------
# Builders - every one through the finance service, every one returning an id
# -----------------------------------------------------------------------------


def bill_line(p: SimpleNamespace, *, quantity: str = "100", unit_price: str = "25") -> dict:
    return {
        "item_id": str(p.item_id),
        "purchase_order_line_id": str(p.order_line_id),
        "goods_receipt_line_id": str(p.receipt_line_id),
        "quantity": quantity,
        "uom_id": str(p.uom_id),
        "unit_price": unit_price,
        "tax_rate": "18",
    }


async def draft_bill(
    session: AsyncSession, tenant: TenantContext, p: SimpleNamespace, **kwargs: Any
) -> uuid.UUID:
    detail = await finance_service.create_invoice(
        session, tenant, invoice_payload(p.bench, **kwargs)
    )
    return detail.id


async def submitted_bill(
    session: AsyncSession, tenant: TenantContext, p: SimpleNamespace, **kwargs: Any
) -> uuid.UUID:
    invoice_id = await draft_bill(session, tenant, p, **kwargs)
    await finance_service.submit_invoice(session, tenant, invoice_id, reason=None)
    return invoice_id


async def approved_bill(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    p: SimpleNamespace,
    **kwargs: Any,
) -> uuid.UUID:
    return (await approved_invoice(session, tenant, principal, p.bench, **kwargs)).id


async def vendor_payment(
    session: AsyncSession,
    tenant: TenantContext,
    vendor_id: uuid.UUID,
    *,
    amount: str = "1000",
    principal: Principal | None = None,
) -> uuid.UUID:
    """A DRAFT payment to the vendor, approved when ``principal`` is given."""
    payment = await finance_service.create_payment(
        session,
        tenant,
        PaymentCreate(
            payment_type=PaymentType.PAYMENT,
            party_type=PartyType.VENDOR,
            party_id=vendor_id,
            payment_date=date(2026, 5, 6),
            amount=Decimal(amount),
            payment_method=PaymentMethod.BANK_TRANSFER,
        ),
    )
    if principal is not None:
        await finance_service.approve_payment(session, tenant, principal, payment.id)
    return payment.id


async def settle(
    session: AsyncSession,
    tenant: TenantContext,
    payment_id: uuid.UUID,
    invoice_id: uuid.UUID,
    amount: str,
) -> None:
    await finance_service.allocate_payment(
        session,
        tenant,
        payment_id,
        AllocationRequest(
            allocations=[PaymentAllocationIn(vendor_invoice_id=invoice_id, amount=Decimal(amount))]
        ),
    )


async def chart_account(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    code: str,
    name: str = "Freight inward",
    parent_id: uuid.UUID | None = None,
) -> uuid.UUID:
    account = await finance_service.create_account(
        session,
        tenant,
        LedgerAccountCreate(
            code=code, name=name, account_type=LedgerAccountType.EXPENSE, parent_id=parent_id
        ),
    )
    return account.id


async def credit_note(b: SimpleNamespace, invoice_id: uuid.UUID, line_id: uuid.UUID) -> uuid.UUID:
    note = await credit_note_service.issue_credit_note(
        b.session,
        b.tenant,
        CreditNoteCreate(
            sales_invoice_id=invoice_id,
            note_date=today(),
            reason_type=CreditNoteReason.PRICE_ADJUSTMENT,
            lines=[CreditNoteLineIn(sales_invoice_line_id=line_id, taxable_amount=Decimal("100"))],
        ),
    )
    return note.id


async def issued(b: SimpleNamespace) -> SimpleNamespace:
    """Ship the bench's 200 units and invoice them. Plain ids and the total."""
    invoice = await issue(b, await make_dispatch(b))
    return SimpleNamespace(
        id=invoice.id,
        line_id=invoice.lines[0].id,
        total=invoice.total_amount,
        customer_id=b.customer.id,
        order_line_id=b.line.id,
    )


# -----------------------------------------------------------------------------
# POST /finance/accounts, PATCH /finance/accounts/{account_id}
# -----------------------------------------------------------------------------


class TestChartOfAccounts:
    async def test_an_account_opens_under_its_parent_and_is_audited(
        self, api_client, session, tenant, unique_code
    ):
        parent_id = await chart_account(
            session, tenant, code=unique_code("5"), name="Direct expenses"
        )
        code = unique_code("51")

        response = await api_client.post(
            "/finance/accounts",
            json={
                "code": code,
                "name": "Freight inward",
                "account_type": "EXPENSE",
                "parent_id": str(parent_id),
                "description": "Carriage on purchases",
            },
        )

        data = ok(response, 201)
        assert data["code"] == code
        assert data["account_type"] == "EXPENSE"
        assert data["parent_id"] == str(parent_id)
        assert data["is_active"] is True
        assert data["is_system"] is False
        assert data["system_code"] is None
        account_id = uuid.UUID(data["id"])
        assert await value(session, LedgerAccount.parent_id, account_id) == parent_id
        [entry] = await audit_trail(session, account_id, action=AuditAction.CREATE)
        assert entry.entity_type == "ledger_account"
        assert entry.entity_code == code
        assert entry.created_by_id == tenant.user_id
        assert entry.new_values["parent_id"] == str(parent_id)

    async def test_a_code_already_in_the_chart_is_a_conflict(
        self, api_client, session, tenant, unique_code
    ):
        code = unique_code("52")
        await chart_account(session, tenant, code=code)

        response = await api_client.post(
            "/finance/accounts", json={"code": code, "name": "Second", "account_type": "EXPENSE"}
        )

        assert refused(response, 409, "DUPLICATE_DOCUMENT")["details"]["code"] == code
        count = select(func.count()).where(
            LedgerAccount.company_id == tenant.company_id, LedgerAccount.code == code
        )
        assert await session.scalar(count) == 1

    @pytest.mark.parametrize(
        ("change", "field"),
        [
            ({"code": ""}, "code"),
            ({"account_type": "REVENUE"}, "account_type"),
            ({"system_code": "ACCOUNTS_PAYABLE"}, "system_code"),
        ],
        ids=["empty-code", "unknown-type", "system-code-claimed"],
    )
    async def test_a_malformed_account_is_a_field_error(self, api_client, change, field):
        payload = {"code": "5300", "name": "Packing material", "account_type": "EXPENSE", **change}

        assert field in field_errors(await api_client.post("/finance/accounts", json=payload))

    async def test_a_parent_in_another_company_is_not_found(
        self, api_client, session, tenant, outsider, unique_code
    ):
        theirs = await chart_account(session, as_tenant(outsider), code=unique_code("6"))
        code = unique_code("61")

        response = await api_client.post(
            "/finance/accounts",
            json={
                "code": code,
                "name": "Borrowed",
                "account_type": "EXPENSE",
                "parent_id": str(theirs),
            },
        )

        refused(response, 404, "NOT_FOUND")
        count = select(func.count()).where(
            LedgerAccount.company_id == tenant.company_id, LedgerAccount.code == code
        )
        assert await session.scalar(count) == 0

    async def test_an_amendment_renames_reparents_and_retires_with_a_diff(
        self, api_client, session, tenant, unique_code
    ):
        parent_id = await chart_account(session, tenant, code=unique_code("7"), name="Overheads")
        account_id = await chart_account(session, tenant, code=unique_code("71"))

        response = await api_client.patch(
            f"/finance/accounts/{account_id}",
            json={"name": "Freight and cartage", "parent_id": str(parent_id), "is_active": False},
        )

        data = ok(response)
        assert data["name"] == "Freight and cartage"
        assert data["parent_id"] == str(parent_id)
        assert data["is_active"] is False
        assert data["account_type"] == "EXPENSE"
        assert await value(session, LedgerAccount.is_active, account_id) is False
        [entry] = await audit_trail(session, account_id, action=AuditAction.UPDATE)
        assert entry.changed_fields == ["is_active", "name", "parent_id"]
        assert entry.old_values["name"] == "Freight inward"
        assert entry.new_values["name"] == "Freight and cartage"

    async def test_an_account_cannot_be_its_own_parent(
        self, api_client, session, tenant, unique_code
    ):
        account_id = await chart_account(session, tenant, code=unique_code("72"))

        response = await api_client.patch(
            f"/finance/accounts/{account_id}", json={"parent_id": str(account_id)}
        )

        refused(response, 422, "VALIDATION_ERROR")
        assert await value(session, LedgerAccount.parent_id, account_id) is None

    async def test_reparenting_under_a_descendant_is_refused_as_a_cycle(
        self, api_client, session, tenant, unique_code
    ):
        root_id = await chart_account(session, tenant, code=unique_code("8"), name="Expenses")
        child_id = await chart_account(session, tenant, code=unique_code("81"), parent_id=root_id)

        response = await api_client.patch(
            f"/finance/accounts/{root_id}", json={"parent_id": str(child_id)}
        )

        error = refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert error["details"] == {"account_id": str(root_id), "parent_id": str(child_id)}
        assert await value(session, LedgerAccount.parent_id, root_id) is None

    async def test_a_control_account_cannot_be_deactivated(self, api_client, session, tenant):
        accounts = await finance_service.ensure_system_accounts(session, tenant)
        bank_id = accounts[str(SystemAccount.BANK)].id

        response = await api_client.patch(f"/finance/accounts/{bank_id}", json={"is_active": False})

        error = refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert error["details"]["system_code"] == str(SystemAccount.BANK)
        assert await value(session, LedgerAccount.is_active, bank_id) is True

    async def test_the_account_type_is_not_amendable(
        self, api_client, session, tenant, unique_code
    ):
        account_id = await chart_account(session, tenant, code=unique_code("73"))

        response = await api_client.patch(
            f"/finance/accounts/{account_id}", json={"account_type": "ASSET"}
        )

        assert "account_type" in field_errors(response)
        assert await value(session, LedgerAccount.account_type, account_id) == "EXPENSE"

    async def test_another_companys_account_is_not_found(
        self, api_client, session, tenant, outsider, unique_code
    ):
        account_id = await chart_account(session, tenant, code=unique_code("74"))
        sign_in(outsider)

        response = await api_client.patch(
            f"/finance/accounts/{account_id}", json={"name": "Renamed from outside"}
        )

        refused(response, 404, "NOT_FOUND")
        assert await value(session, LedgerAccount.name, account_id) == "Freight inward"


# -----------------------------------------------------------------------------
# POST /finance/trial-balance/export, POST /finance/gstr-1/export
# -----------------------------------------------------------------------------


class TestWorkbookExports:
    async def test_the_trial_balance_export_is_a_job_handed_to_the_broker(
        self, api_client, session, tenant, broker
    ):
        response = await api_client.post(
            "/finance/trial-balance/export", params={"as_of": "2026-09-30"}
        )

        data = ok(response, 202)
        job_id = uuid.UUID(data["job_id"])
        assert data["status"] == "queued"
        assert data["status_url"] == reports_service.job_status_url(job_id)
        job = await snapshot(session, job_id)
        assert job.report_key == ReportKey.TRIAL_BALANCE
        assert job.status == ReportJobStatus.QUEUED
        assert job.parameters["as_of"] == "2026-09-30"
        assert job.celery_task_id is not None
        [kwargs] = [
            sent for name, sent in broker if name == finance_service.TRIAL_BALANCE_TASK_NAME
        ]
        assert kwargs["snapshot_id"] == str(job_id)
        assert kwargs["company_id"] == str(tenant.company_id)
        [entry] = await audit_trail(session, job_id, action=AuditAction.JOB_STARTED)
        assert entry.entity_type == "trial_balance"

    async def test_the_trial_balance_is_drawn_to_today_by_default(self, api_client, session):
        data = ok(await api_client.post("/finance/trial-balance/export"), 202)

        job = await snapshot(session, uuid.UUID(data["job_id"]))
        assert job.parameters["as_of"] == today().isoformat()

    async def test_a_broker_that_refuses_the_task_fails_the_job_instead_of_orphaning_it(
        self, api_client, session, tenant, monkeypatch
    ):
        def unreachable(*_: Any, **__: Any) -> Any:
            raise ConnectionError("broker unreachable")

        monkeypatch.setattr(celery_module.celery_app, "send_task", unreachable)

        response = await api_client.post(
            "/finance/trial-balance/export", params={"as_of": "2026-09-30"}
        )

        refused(response, 503, "SERVICE_UNAVAILABLE")
        jobs = select(ReportSnapshot).where(
            ReportSnapshot.company_id == tenant.company_id,
            ReportSnapshot.report_key == ReportKey.TRIAL_BALANCE,
        )
        [job] = (await session.scalars(jobs)).all()
        assert job.status == ReportJobStatus.FAILED
        assert job.error_message
        assert await audit_trail(session, job.id, action=AuditAction.JOB_FAILED)

    async def test_the_gstr1_export_is_a_job_for_its_return_period(
        self, api_client, session, broker
    ):
        response = await api_client.post(
            "/finance/gstr-1/export", params={"date_from": "2026-04-01", "date_to": "2026-04-30"}
        )

        data = ok(response, 202)
        job_id = uuid.UUID(data["job_id"])
        assert data["status_url"] == reports_service.job_status_url(job_id)
        job = await snapshot(session, job_id)
        assert job.report_key == ReportKey.GSTR1
        assert job.status == ReportJobStatus.QUEUED
        assert job.parameters["period"] == {"date_from": "2026-04-01", "date_to": "2026-04-30"}
        [kwargs] = [sent for name, sent in broker if name == finance_service.GSTR1_TASK_NAME]
        assert kwargs["snapshot_id"] == str(job_id)
        [entry] = await audit_trail(session, job_id, action=AuditAction.JOB_STARTED)
        assert entry.entity_type == "gstr1"

    async def test_a_period_that_ends_before_it_starts_queues_nothing(
        self, api_client, session, tenant, broker
    ):
        response = await api_client.post(
            "/finance/gstr-1/export", params={"date_from": "2026-04-30", "date_to": "2026-04-01"}
        )

        refused(response, 422, "VALIDATION_ERROR")
        jobs = select(func.count()).where(ReportSnapshot.company_id == tenant.company_id)
        assert await session.scalar(jobs) == 0
        assert broker == []

    @pytest.mark.parametrize(
        ("path", "params", "field"),
        [
            ("/finance/trial-balance/export", {"as_of": "30/09/2026"}, "query.as_of"),
            ("/finance/gstr-1/export", {"date_from": "2026-04-01"}, "query.date_to"),
            (
                "/finance/gstr-1/export",
                {"date_from": "April", "date_to": "2026-04-30"},
                "query.date_from",
            ),
        ],
        ids=["trial-balance-date", "gstr1-missing-end", "gstr1-unparsable-start"],
    )
    async def test_export_parameters_are_validated_field_by_field(
        self, api_client, broker, path, params, field
    ):
        assert field in field_errors(await api_client.post(path, params=params))
        assert broker == []


# -----------------------------------------------------------------------------
# PATCH /finance/vendor-invoices/{invoice_id}
# -----------------------------------------------------------------------------


class TestVendorInvoiceAmendment:
    async def test_a_submitted_bill_takes_new_lines_and_must_be_matched_again(
        self, api_client, session, tenant, purchase, unique_code
    ):
        invoice_id = await submitted_bill(session, tenant, purchase)
        assert await value(session, VendorInvoice.match_status, invoice_id) == MatchStatus.MATCHED
        number = unique_code("INV")

        response = await api_client.patch(
            f"/finance/vendor-invoices/{invoice_id}",
            json={
                "vendor_invoice_number": number,
                "notes": "Re-keyed from the vendor's corrected copy",
                "lines": [bill_line(purchase, quantity="80")],
            },
        )

        data = ok(response)
        assert data["vendor_invoice_number"] == number
        assert data["status"] == "PENDING_APPROVAL"
        assert data["match_status"] == "NOT_MATCHED"
        assert money(data["subtotal"]) == Decimal("2000")
        assert money(data["tax_amount"]) == Decimal("360")
        assert money(data["total_amount"]) == Decimal("2360")
        assert [money(line["quantity"]) for line in data["lines"]] == [Decimal("80")]
        assert await value(session, VendorInvoice.total_amount, invoice_id) == Decimal("2360")
        [entry] = await audit_trail(session, invoice_id, action=AuditAction.UPDATE)
        assert {"vendor_invoice_number", "total_amount", "match_status", "notes"} <= set(
            entry.changed_fields
        )
        assert entry.new_values["vendor_invoice_number"] == number

    async def test_an_approved_bill_is_no_longer_editable(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)

        response = await api_client.patch(
            f"/finance/vendor-invoices/{invoice_id}", json={"notes": "Too late to change"}
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")["details"]["status"] == "APPROVED"
        assert await value(session, VendorInvoice.notes, invoice_id) is None
        assert await audit_trail(session, invoice_id, action=AuditAction.UPDATE) == []

    async def test_a_number_the_vendor_already_billed_is_a_conflict(
        self, api_client, session, tenant, purchase, unique_code
    ):
        number = unique_code("INV")
        first_id = await draft_bill(session, tenant, purchase, number=number)
        first_code = await value(session, VendorInvoice.code, first_id)
        second_id = await draft_bill(session, tenant, purchase)

        response = await api_client.patch(
            f"/finance/vendor-invoices/{second_id}", json={"vendor_invoice_number": number}
        )

        error = refused(response, 409, "DUPLICATE_DOCUMENT")
        assert error["details"]["existing_invoice_code"] == first_code
        assert await value(session, VendorInvoice.vendor_invoice_number, second_id) != number

    async def test_a_due_date_before_the_invoice_date_is_refused(
        self, api_client, session, tenant, purchase
    ):
        invoice_id = await draft_bill(session, tenant, purchase)
        due_before = await value(session, VendorInvoice.due_date, invoice_id)

        response = await api_client.patch(
            f"/finance/vendor-invoices/{invoice_id}",
            json={"due_date": (BILL_DATE - timedelta(days=5)).isoformat()},
        )

        refused(response, 422, "VALIDATION_ERROR")
        assert await value(session, VendorInvoice.due_date, invoice_id) == due_before

    @pytest.mark.parametrize(
        ("line_change", "field"),
        [
            ({"quantity": "0"}, "lines.0.quantity"),
            ({"unit_price": "-1"}, "lines.0.unit_price"),
            ({"line_total": "2950"}, "lines.0.line_total"),
        ],
        ids=["zero-quantity", "negative-price", "client-total"],
    )
    async def test_a_malformed_line_is_a_field_error(
        self, api_client, session, tenant, purchase, line_change, field
    ):
        invoice_id = await draft_bill(session, tenant, purchase)

        response = await api_client.patch(
            f"/finance/vendor-invoices/{invoice_id}",
            json={"lines": [{**bill_line(purchase), **line_change}]},
        )

        assert field in field_errors(response)
        assert await value(session, VendorInvoice.total_amount, invoice_id) == BILL_TOTAL

    async def test_another_companys_bill_is_not_found(
        self, api_client, session, tenant, purchase, outsider
    ):
        invoice_id = await draft_bill(session, tenant, purchase)
        sign_in(outsider)

        response = await api_client.patch(
            f"/finance/vendor-invoices/{invoice_id}", json={"notes": "Edited from outside"}
        )

        refused(response, 404, "NOT_FOUND")
        assert await value(session, VendorInvoice.notes, invoice_id) is None


# -----------------------------------------------------------------------------
# POST /finance/vendor-invoices/{invoice_id}/approve
# -----------------------------------------------------------------------------


class TestVendorInvoiceApproval:
    async def test_a_matched_bill_is_approved_and_posts_its_payable(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await submitted_bill(session, tenant, purchase)

        data = ok(await api_client.post(f"/finance/vendor-invoices/{invoice_id}/approve", json={}))

        assert data["status"] == "APPROVED"
        assert data["match_status"] == "MATCHED"
        assert data["approved_by_id"] == str(principal.user_id)
        assert data["approved_at"] is not None
        legs = await ledger_legs(session, invoice_id)
        assert balanced(legs) == BILL_TOTAL
        assert party_net(legs, PartyType.VENDOR) == BILL_TOTAL
        approvals = await audit_trail(session, invoice_id, action=AuditAction.APPROVE)
        assert ("PENDING_APPROVAL", "APPROVED") in status_moves(approvals)
        request = select(ApprovalRequest.status).where(ApprovalRequest.entity_id == invoice_id)
        assert await session.scalar(request) == ApprovalStatus.APPROVED.value

    async def test_a_price_variance_is_refused_until_an_override_reason_is_given(
        self, api_client, session, tenant, purchase
    ):
        invoice_id = await submitted_bill(session, tenant, purchase, unit_price="30")
        path = f"/finance/vendor-invoices/{invoice_id}/approve"

        error = refused(await api_client.post(path, json={}), 422, "APPROVAL_REQUIRED")
        assert error["details"]["blocking_reasons"]
        assert await value(session, VendorInvoice.status, invoice_id) == (
            VendorInvoiceStatus.PENDING_APPROVAL
        )
        assert await ledger_legs(session, invoice_id) == []

        override = "Price rise agreed with the vendor by phone"
        data = ok(await api_client.post(path, json={"override_reason": override}))
        assert data["status"] == "APPROVED"
        assert data["match_status"] == "OVERRIDDEN"
        assert data["variance_override_reason"] == override
        assert party_net(await ledger_legs(session, invoice_id), PartyType.VENDOR) == Decimal(
            "3540"
        )
        approvals = await audit_trail(session, invoice_id, action=AuditAction.APPROVE)
        assert any((entry.context or {}).get("override_reason") == override for entry in approvals)

    async def test_a_draft_cannot_be_approved(self, api_client, session, tenant, purchase):
        invoice_id = await draft_bill(session, tenant, purchase)

        response = await api_client.post(f"/finance/vendor-invoices/{invoice_id}/approve", json={})

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")["details"]["status"] == "DRAFT"
        assert await value(session, VendorInvoice.status, invoice_id) == VendorInvoiceStatus.DRAFT
        assert await ledger_legs(session, invoice_id) == []

    async def test_an_unknown_field_is_a_field_error(self, api_client, session, tenant, purchase):
        invoice_id = await submitted_bill(session, tenant, purchase)

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/approve",
            json={"override_reason": "Agreed", "approved_by_id": str(uuid.uuid4())},
        )

        assert "approved_by_id" in field_errors(response)
        assert await value(session, VendorInvoice.status, invoice_id) == (
            VendorInvoiceStatus.PENDING_APPROVAL
        )

    async def test_another_companys_bill_is_not_found(
        self, api_client, session, tenant, purchase, outsider
    ):
        invoice_id = await submitted_bill(session, tenant, purchase)
        sign_in(outsider)

        response = await api_client.post(f"/finance/vendor-invoices/{invoice_id}/approve", json={})

        refused(response, 404, "NOT_FOUND")
        assert await value(session, VendorInvoice.status, invoice_id) == (
            VendorInvoiceStatus.PENDING_APPROVAL
        )


# -----------------------------------------------------------------------------
# POST /finance/vendor-invoices/{invoice_id}/submit
# -----------------------------------------------------------------------------


class TestVendorInvoiceSubmit:
    async def test_a_draft_is_matched_and_queued_for_approval(
        self, api_client, session, tenant, purchase
    ):
        invoice_id = await draft_bill(session, tenant, purchase)

        data = ok(
            await api_client.post(
                f"/finance/vendor-invoices/{invoice_id}/submit", json={"reason": REASON}
            )
        )

        assert (data["status"], data["match_status"]) == ("PENDING_APPROVAL", "MATCHED")
        moves = await audit_trail(session, invoice_id, action=AuditAction.STATUS_CHANGE)
        assert status_moves(moves) == [("DRAFT", "PENDING_APPROVAL")]
        requests = select(ApprovalRequest.status).where(ApprovalRequest.entity_id == invoice_id)
        assert list(await session.scalars(requests)) == [ApprovalStatus.PENDING.value]

    async def test_a_bill_already_awaiting_approval_is_not_submitted_again(
        self, api_client, session, tenant, purchase
    ):
        invoice_id = await submitted_bill(session, tenant, purchase)
        matched_at = await value(session, VendorInvoice.matched_at, invoice_id)

        # Frozen past the first match, so a re-run would leave a later matched_at.
        with frozen_time(matched_at + timedelta(seconds=1)):
            response = await api_client.post(f"/finance/vendor-invoices/{invoice_id}/submit")

        error = refused(response, 422, "INVALID_STATE_TRANSITION")
        assert error["details"]["current_status"] == "PENDING_APPROVAL"
        assert await value(session, VendorInvoice.matched_at, invoice_id) == matched_at
        moves = await audit_trail(session, invoice_id, action=AuditAction.STATUS_CHANGE)
        assert status_moves(moves) == [("DRAFT", "PENDING_APPROVAL")]
        requests = select(ApprovalRequest.status).where(ApprovalRequest.entity_id == invoice_id)
        assert list(await session.scalars(requests)) == [ApprovalStatus.PENDING.value]

    async def test_a_bill_disputed_after_approval_is_not_sent_back_to_the_queue(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        await finance_service.dispute_invoice(session, tenant, invoice_id, reason="Short supply")

        response = await api_client.post(f"/finance/vendor-invoices/{invoice_id}/submit")

        error = refused(response, 422, "INVALID_STATE_TRANSITION")
        assert error["details"]["current_status"] == "DISPUTED"
        assert await value(session, VendorInvoice.status, invoice_id) == (
            VendorInvoiceStatus.DISPUTED
        )
        assert await value(session, VendorInvoice.dispute_reason, invoice_id) == "Short supply"
        # One step further: no approval can reach it now, so the payable stays posted once.
        refused(
            await api_client.post(f"/finance/vendor-invoices/{invoice_id}/approve", json={}),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert party_net(await ledger_legs(session, invoice_id), PartyType.VENDOR) == BILL_TOTAL


# -----------------------------------------------------------------------------
# POST /finance/vendor-invoices/{invoice_id}/dispute and /resolve-dispute
# -----------------------------------------------------------------------------


class TestVendorInvoiceDispute:
    async def test_an_approved_bill_is_held_and_still_owed(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        reason = "Four units short against the delivery note"

        data = ok(
            await api_client.post(
                f"/finance/vendor-invoices/{invoice_id}/dispute", json={"reason": reason}
            )
        )

        assert data["status"] == "DISPUTED"
        assert data["dispute_reason"] == reason
        assert money(data["outstanding_amount"]) == BILL_TOTAL
        assert await value(session, VendorInvoice.dispute_reason, invoice_id) == reason
        moves = await audit_trail(session, invoice_id, action=AuditAction.STATUS_CHANGE)
        [entry] = [e for e in moves if (e.new_values or {}).get("status") == "DISPUTED"]
        assert entry.old_values == {"status": "APPROVED"}
        assert entry.context["reason"] == reason

    async def test_a_draft_cannot_be_disputed(self, api_client, session, tenant, purchase):
        invoice_id = await draft_bill(session, tenant, purchase)

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/dispute", json={"reason": REASON}
        )

        error = refused(response, 422, "INVALID_STATE_TRANSITION")
        assert error["details"]["current_status"] == "DRAFT"
        assert error["details"]["requested_status"] == "DISPUTED"
        assert await value(session, VendorInvoice.dispute_reason, invoice_id) is None

    async def test_a_dispute_needs_a_reason(self, api_client, session, tenant, principal, purchase):
        invoice_id = await approved_bill(session, tenant, principal, purchase)

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/dispute", json={"reason": "no"}
        )

        assert "reason" in field_errors(response)
        assert await value(session, VendorInvoice.status, invoice_id) == (
            VendorInvoiceStatus.APPROVED
        )

    async def test_resolving_returns_the_bill_to_the_payment_run(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        await finance_service.dispute_invoice(session, tenant, invoice_id, reason="Short supply")
        reason = "Vendor delivered the missing units"

        data = ok(
            await api_client.post(
                f"/finance/vendor-invoices/{invoice_id}/resolve-dispute", json={"reason": reason}
            )
        )

        assert data["status"] == "APPROVED"
        assert data["dispute_reason"] is None
        assert money(data["outstanding_amount"]) == BILL_TOTAL
        moves = await audit_trail(session, invoice_id, action=AuditAction.STATUS_CHANGE)
        [entry] = [e for e in moves if (e.old_values or {}).get("status") == "DISPUTED"]
        assert entry.new_values == {"status": "APPROVED"}
        assert entry.context["reason"] == reason
        # Resolving re-posts nothing: the payable posted at approval still stands alone.
        assert party_net(await ledger_legs(session, invoice_id), PartyType.VENDOR) == BILL_TOTAL

    async def test_a_part_paid_bill_returns_from_dispute_part_paid(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id, principal=principal)
        await settle(session, tenant, payment_id, invoice_id, "1000")
        await finance_service.dispute_invoice(session, tenant, invoice_id, reason="Short supply")

        data = ok(
            await api_client.post(
                f"/finance/vendor-invoices/{invoice_id}/resolve-dispute", json={"reason": REASON}
            )
        )

        assert data["status"] == "PARTIALLY_PAID"
        assert money(data["outstanding_amount"]) == BILL_TOTAL - Decimal("1000")

    async def test_only_a_disputed_bill_can_be_resolved(
        self, api_client, session, tenant, purchase
    ):
        invoice_id = await draft_bill(session, tenant, purchase)

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/resolve-dispute", json={"reason": REASON}
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await value(session, VendorInvoice.status, invoice_id) == VendorInvoiceStatus.DRAFT

    async def test_a_bill_awaiting_approval_cannot_be_approved_by_resolving_a_dispute(
        self, api_client, session, tenant, purchase
    ):
        invoice_id = await submitted_bill(session, tenant, purchase)

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/resolve-dispute", json={"reason": REASON}
        )

        assert response.status_code == 422, (
            f"resolved a bill nobody disputed: {response.status_code}, now "
            f"{await value(session, VendorInvoice.status, invoice_id)} with "
            f"{len(await ledger_legs(session, invoice_id))} ledger legs"
        )
        assert await value(session, VendorInvoice.status, invoice_id) == (
            VendorInvoiceStatus.PENDING_APPROVAL
        )

    async def test_a_bill_disputed_before_approval_does_not_resolve_into_an_unposted_approval(
        self, api_client, session, tenant, purchase
    ):
        invoice_id = await submitted_bill(session, tenant, purchase)
        await finance_service.dispute_invoice(session, tenant, invoice_id, reason="Price query")

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/resolve-dispute", json={"reason": REASON}
        )

        assert response.status_code in (200, 422), response.text
        status = await value(session, VendorInvoice.status, invoice_id)
        posted = party_net(await ledger_legs(session, invoice_id), PartyType.VENDOR)
        approver = await value(session, VendorInvoice.approved_by_id, invoice_id)
        assert status != VendorInvoiceStatus.APPROVED or (
            posted == BILL_TOTAL and approver is not None
        ), f"{status} with {posted} payable posted and approver {approver}"

    async def test_a_second_dispute_is_refused_or_recorded(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        await finance_service.dispute_invoice(session, tenant, invoice_id, reason="Short supply")
        second = "The price disagrees with the order as well"

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/dispute", json={"reason": second}
        )

        recorded = [
            entry
            for entry in await audit_trail(session, invoice_id)
            if (entry.context or {}).get("reason") == second
            or (entry.new_values or {}).get("dispute_reason") == second
        ]
        assert response.status_code == 422 or recorded, (
            f"answered {response.status_code}; dispute reason is now "
            f"{await value(session, VendorInvoice.dispute_reason, invoice_id)!r} with no audit row"
        )

    async def test_resolving_a_bill_nobody_disputed_leaves_its_approval_open(
        self, api_client, session, tenant, purchase
    ):
        invoice_id = await submitted_bill(session, tenant, purchase)

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/resolve-dispute", json={"reason": REASON}
        )

        error = refused(response, 422, "INVALID_STATE_TRANSITION")
        assert error["details"]["status"] == "PENDING_APPROVAL"
        assert await value(session, VendorInvoice.approved_by_id, invoice_id) is None
        assert await ledger_legs(session, invoice_id) == []
        request = select(ApprovalRequest.status).where(ApprovalRequest.entity_id == invoice_id)
        assert await session.scalar(request) == ApprovalStatus.PENDING.value

    async def test_a_bill_disputed_before_approval_returns_to_the_approval_queue(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await submitted_bill(session, tenant, purchase)
        matched_at = await value(session, VendorInvoice.matched_at, invoice_id)
        await finance_service.dispute_invoice(session, tenant, invoice_id, reason="Price query")
        reason = "Vendor confirmed the ordered price"

        # Frozen past the submission's match: the Windows clock ticks every 15.6 ms.
        with frozen_time(matched_at + timedelta(seconds=1)) as resolved_at:
            data = ok(
                await api_client.post(
                    f"/finance/vendor-invoices/{invoice_id}/resolve-dispute",
                    json={"reason": reason},
                )
            )

        assert data["status"] == "PENDING_APPROVAL"
        assert data["dispute_reason"] is None
        assert data["approved_by_id"] is None
        assert await ledger_legs(session, invoice_id) == []
        # The approver sees a match run on resolution, not the one from before the dispute.
        assert await value(session, VendorInvoice.matched_at, invoice_id) == resolved_at
        moves = await audit_trail(session, invoice_id, action=AuditAction.STATUS_CHANGE)
        [entry] = [e for e in moves if (e.old_values or {}).get("status") == "DISPUTED"]
        assert entry.new_values == {"status": "PENDING_APPROVAL"}
        assert entry.context["reason"] == reason
        requests = select(ApprovalRequest.status).where(ApprovalRequest.entity_id == invoice_id)
        assert list(
            await session.scalars(requests.order_by(ApprovalRequest.created_at, ApprovalRequest.id))
        ) == [ApprovalStatus.REJECTED.value, ApprovalStatus.PENDING.value]

        # One step further: the queue's own approval posts the payable and names the approver.
        approved = ok(
            await api_client.post(f"/finance/vendor-invoices/{invoice_id}/approve", json={})
        )
        assert approved["status"] == "APPROVED"
        assert approved["approved_by_id"] == str(principal.user_id)
        assert party_net(await ledger_legs(session, invoice_id), PartyType.VENDOR) == BILL_TOTAL

    async def test_a_second_dispute_keeps_the_first_reason(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        first = "Short supply"
        await finance_service.dispute_invoice(session, tenant, invoice_id, reason=first)

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/dispute",
            json={"reason": "The price disagrees with the order as well"},
        )

        error = refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert error["details"]["dispute_reason"] == first
        assert await value(session, VendorInvoice.dispute_reason, invoice_id) == first
        moves = status_moves(await audit_trail(session, invoice_id))
        assert [move for move in moves if move[1] == "DISPUTED"] == [("APPROVED", "DISPUTED")]


# -----------------------------------------------------------------------------
# POST /finance/vendor-invoices/{invoice_id}/cancel
# -----------------------------------------------------------------------------


class TestVendorInvoiceCancel:
    async def test_a_draft_is_called_off_with_nothing_to_reverse(
        self, api_client, session, tenant, purchase
    ):
        invoice_id = await draft_bill(session, tenant, purchase)
        reason = "Keyed against the wrong vendor"

        data = ok(
            await api_client.post(
                f"/finance/vendor-invoices/{invoice_id}/cancel", json={"reason": reason}
            )
        )

        assert data["status"] == "CANCELLED"
        assert data["cancellation_reason"] == reason
        assert await ledger_legs(session, invoice_id) == []
        [entry] = await audit_trail(session, invoice_id, action=AuditAction.CANCEL)
        assert status_moves([entry]) == [("DRAFT", "CANCELLED")]
        assert entry.context["reason"] == reason

    async def test_cancelling_an_approved_bill_reverses_its_journal(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        posted = len(await ledger_legs(session, invoice_id))

        data = ok(
            await api_client.post(
                f"/finance/vendor-invoices/{invoice_id}/cancel", json={"reason": REASON}
            )
        )

        assert data["status"] == "CANCELLED"
        legs = await ledger_legs(session, invoice_id)
        reversals = [leg for leg in legs if leg.is_reversal]
        assert len(reversals) == posted
        assert {leg.reverses_entry_id for leg in reversals} == {
            leg.id for leg in legs if not leg.is_reversal
        }
        balanced(legs)
        assert party_net(legs, PartyType.VENDOR) == Decimal("0")
        [entry] = await audit_trail(session, invoice_id, action=AuditAction.CANCEL)
        assert status_moves([entry]) == [("APPROVED", "CANCELLED")]

    async def test_a_bill_with_money_against_it_cannot_be_cancelled(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id, principal=principal)
        await settle(session, tenant, payment_id, invoice_id, "1000")

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/cancel", json={"reason": REASON}
        )

        error = refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert money(error["details"]["paid_amount"]) == Decimal("1000")
        assert await value(session, VendorInvoice.status, invoice_id) == (
            VendorInvoiceStatus.PARTIALLY_PAID
        )
        assert not any(leg.is_reversal for leg in await ledger_legs(session, invoice_id))

    async def test_a_cancellation_needs_a_reason(self, api_client, session, tenant, purchase):
        invoice_id = await draft_bill(session, tenant, purchase)

        response = await api_client.post(f"/finance/vendor-invoices/{invoice_id}/cancel", json={})

        assert "reason" in field_errors(response)
        assert await value(session, VendorInvoice.status, invoice_id) == VendorInvoiceStatus.DRAFT

    async def test_a_cancelled_bill_cannot_be_cancelled_again(
        self, api_client, session, tenant, purchase
    ):
        invoice_id = await draft_bill(session, tenant, purchase)
        first = "Keyed against the wrong vendor"
        await finance_service.cancel_invoice(session, tenant, invoice_id, reason=first)

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/cancel", json={"reason": "A second story"}
        )

        assert response.status_code == 422, (
            f"re-cancelled with {response.status_code}; recorded reason is now "
            f"{await value(session, VendorInvoice.cancellation_reason, invoice_id)!r}"
        )
        assert await value(session, VendorInvoice.cancellation_reason, invoice_id) == first

    async def test_a_second_cancellation_leaves_the_first_on_record(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        first = "Billed against the wrong order"
        await finance_service.cancel_invoice(session, tenant, invoice_id, reason=first)
        legs = len(await ledger_legs(session, invoice_id))

        response = await api_client.post(
            f"/finance/vendor-invoices/{invoice_id}/cancel", json={"reason": REASON}
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")["details"]["status"] == (
            "CANCELLED"
        )
        [entry] = await audit_trail(session, invoice_id, action=AuditAction.CANCEL)
        assert entry.context["reason"] == first
        assert len(await ledger_legs(session, invoice_id)) == legs


# -----------------------------------------------------------------------------
# POST /finance/payments, PATCH /finance/payments/{payment_id}
# -----------------------------------------------------------------------------


def payment_json(party_id: uuid.UUID, **change: Any) -> dict[str, Any]:
    return {
        "payment_type": "PAYMENT",
        "party_type": "VENDOR",
        "party_id": str(party_id),
        "payment_date": "2026-05-06",
        "amount": "1000",
        "payment_method": "BANK_TRANSFER",
        **change,
    }


async def payments_in(session: AsyncSession, company_id: uuid.UUID) -> int:
    return await session.scalar(select(func.count()).where(Payment.company_id == company_id))


class TestPaymentCreate:
    async def test_a_vendor_payment_is_recorded_in_draft_and_queued_for_approval(
        self, api_client, session, tenant, purchase
    ):
        response = await api_client.post(
            "/finance/payments",
            json=payment_json(
                purchase.vendor_id, reference_number="UTR-0001", bank_name="State Bank"
            ),
        )

        data = ok(response, 201)
        assert data["status"] == "DRAFT"
        assert data["code"]
        assert data["party_id"] == str(purchase.vendor_id)
        assert data["currency"] == "INR"
        assert money(data["amount"]) == Decimal("1000")
        assert money(data["allocated_amount"]) == Decimal("0")
        assert data["allocations"] == []
        payment_id = uuid.UUID(data["id"])
        assert await value(session, Payment.reference_number, payment_id) == "UTR-0001"
        assert await ledger_legs(session, payment_id) == []
        [entry] = await audit_trail(session, payment_id, action=AuditAction.CREATE)
        assert entry.entity_type == "payment"
        assert entry.new_values["payment_method"] == "BANK_TRANSFER"
        request = await session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.entity_id == payment_id)
        )
        assert request.status == ApprovalStatus.PENDING.value
        assert money(request.payload["amount"]) == Decimal("1000")

    async def test_a_customer_receipt_is_recorded_against_the_customer(
        self, api_client, session, tenant, unique_code
    ):
        customer = await seed_support.seed_customer(
            session, company_id=tenant.company_id, code=unique_code("CU")
        )
        customer_id = customer.id

        response = await api_client.post(
            "/finance/payments",
            json=payment_json(
                customer_id,
                payment_type="RECEIPT",
                party_type="CUSTOMER",
                payment_method="UPI",
                amount="500",
            ),
        )

        data = ok(response, 201)
        assert data["payment_type"] == "RECEIPT"
        assert data["party_type"] == "CUSTOMER"
        assert data["status"] == "DRAFT"
        assert await value(session, Payment.party_id, uuid.UUID(data["id"])) == customer_id

    @pytest.mark.parametrize(
        ("change", "field"),
        [
            ({"payment_type": "RECEIPT"}, "_root"),
            ({"payment_method": "ADJUSTMENT"}, "payment_method"),
            ({"amount": "0"}, "amount"),
            ({"currency": "inr"}, "currency"),
        ],
        ids=["receipt-from-a-vendor", "noncash-method", "zero-amount", "lower-case-currency"],
    )
    async def test_a_malformed_payment_is_a_field_error(
        self, api_client, session, tenant, purchase, change, field
    ):
        response = await api_client.post(
            "/finance/payments", json=payment_json(purchase.vendor_id, **change)
        )

        assert field in field_errors(response)
        assert await payments_in(session, tenant.company_id) == 0

    async def test_a_vendor_of_another_company_is_not_found(
        self, api_client, session, tenant, other_company, unique_code
    ):
        vendor = await seed_support.seed_vendor(
            session, company_id=other_company.id, code=unique_code("V")
        )

        response = await api_client.post("/finance/payments", json=payment_json(vendor.id))

        refused(response, 404, "NOT_FOUND")
        assert await payments_in(session, tenant.company_id) == 0


class TestPaymentAmendment:
    async def test_a_draft_is_amended_and_its_approval_request_follows(
        self, api_client, session, tenant, purchase
    ):
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id)

        response = await api_client.patch(
            f"/finance/payments/{payment_id}",
            json={"amount": "1250", "reference_number": "UTR-0002"},
        )

        data = ok(response)
        assert money(data["amount"]) == Decimal("1250")
        assert data["reference_number"] == "UTR-0002"
        assert data["status"] == "DRAFT"
        assert await value(session, Payment.amount, payment_id) == Decimal("1250")
        [entry] = await audit_trail(session, payment_id, action=AuditAction.UPDATE)
        assert entry.changed_fields == ["amount", "reference_number"]
        assert money(entry.old_values["amount"]) == Decimal("1000")
        [request] = (
            await session.scalars(
                select(ApprovalRequest).where(ApprovalRequest.entity_id == payment_id)
            )
        ).all()
        assert request.status == ApprovalStatus.PENDING.value
        assert money(request.payload["amount"]) == Decimal("1250")

    async def test_an_approved_payment_cannot_be_amended(
        self, api_client, session, tenant, principal, purchase
    ):
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id, principal=principal)

        response = await api_client.patch(f"/finance/payments/{payment_id}", json={"amount": "1"})

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")["details"]["status"] == "APPROVED"
        assert await value(session, Payment.amount, payment_id) == Decimal("1000")

    @pytest.mark.parametrize(
        ("change", "field"),
        [
            ({"payment_method": "ADJUSTMENT"}, "payment_method"),
            ({"amount": "-5"}, "amount"),
            ({"status": "APPROVED"}, "status"),
        ],
        ids=["noncash-method", "negative-amount", "status-is-not-a-field"],
    )
    async def test_a_malformed_amendment_is_a_field_error(
        self, api_client, session, tenant, purchase, change, field
    ):
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id)

        response = await api_client.patch(f"/finance/payments/{payment_id}", json=change)

        assert field in field_errors(response)
        assert await value(session, Payment.status, payment_id) == PaymentStatus.DRAFT
        assert await value(session, Payment.amount, payment_id) == Decimal("1000")

    async def test_another_companys_payment_is_not_found(
        self, api_client, session, tenant, purchase, outsider
    ):
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id)
        sign_in(outsider)

        response = await api_client.patch(f"/finance/payments/{payment_id}", json={"amount": "1"})

        refused(response, 404, "NOT_FOUND")
        assert await value(session, Payment.amount, payment_id) == Decimal("1000")


# -----------------------------------------------------------------------------
# POST /finance/payments/{payment_id}/cancel
# -----------------------------------------------------------------------------


class TestPaymentCancel:
    async def test_a_draft_payment_is_called_off_and_its_approval_rejected(
        self, api_client, session, tenant, purchase
    ):
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id)
        reason = "Raised twice for one cheque"

        data = ok(
            await api_client.post(f"/finance/payments/{payment_id}/cancel", json={"reason": reason})
        )

        assert data["status"] == "CANCELLED"
        assert data["cancellation_reason"] == reason
        assert await ledger_legs(session, payment_id) == []
        request = select(ApprovalRequest.status).where(ApprovalRequest.entity_id == payment_id)
        assert await session.scalar(request) == ApprovalStatus.REJECTED.value
        [entry] = await audit_trail(session, payment_id, action=AuditAction.CANCEL)
        assert status_moves([entry]) == [("DRAFT", "CANCELLED")]
        assert entry.context["reason"] == reason

    async def test_an_approved_payment_is_reversed_on_the_ledger(
        self, api_client, session, tenant, principal, purchase
    ):
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id, principal=principal)
        posted = await ledger_legs(session, payment_id)
        assert balanced(posted) == Decimal("1000")

        data = ok(
            await api_client.post(f"/finance/payments/{payment_id}/cancel", json={"reason": REASON})
        )

        assert data["status"] == "CANCELLED"
        legs = await ledger_legs(session, payment_id)
        reversals = [leg for leg in legs if leg.is_reversal]
        assert {leg.reverses_entry_id for leg in reversals} == {leg.id for leg in posted}
        assert balanced(legs) == Decimal("2000")
        assert party_net(legs, PartyType.VENDOR) == Decimal("0")
        [entry] = await audit_trail(session, payment_id, action=AuditAction.CANCEL)
        assert status_moves([entry]) == [("APPROVED", "CANCELLED")]

    async def test_a_payment_still_allocated_cannot_be_cancelled(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id, principal=principal)
        await settle(session, tenant, payment_id, invoice_id, "1000")

        response = await api_client.post(
            f"/finance/payments/{payment_id}/cancel", json={"reason": REASON}
        )

        error = refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert money(error["details"]["allocated_amount"]) == Decimal("1000")
        assert await value(session, Payment.status, payment_id) == PaymentStatus.ALLOCATED
        assert not any(leg.is_reversal for leg in await ledger_legs(session, payment_id))

    async def test_a_cancelled_payment_cannot_be_cancelled_again(
        self, api_client, session, tenant, purchase
    ):
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id)
        first = "Raised twice for one cheque"
        await finance_service.cancel_payment(session, tenant, payment_id, reason=first)

        response = await api_client.post(
            f"/finance/payments/{payment_id}/cancel", json={"reason": "A second story"}
        )

        error = refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert error["details"]["status"] == "CANCELLED"
        assert await value(session, Payment.cancellation_reason, payment_id) == first

    async def test_a_cancellation_needs_a_reason(self, api_client, session, tenant, purchase):
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id)

        response = await api_client.post(
            f"/finance/payments/{payment_id}/cancel", json={"reason": "ok"}
        )

        assert "reason" in field_errors(response)
        assert await value(session, Payment.status, payment_id) == PaymentStatus.DRAFT


# -----------------------------------------------------------------------------
# POST /finance/payments/{payment_id}/allocate
# -----------------------------------------------------------------------------


class TestPaymentAllocation:
    async def test_an_approved_payment_settles_part_of_a_bill(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        invoice_code = await value(session, VendorInvoice.code, invoice_id)
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id, principal=principal)

        response = await api_client.post(
            f"/finance/payments/{payment_id}/allocate",
            json={
                "allocations": [
                    {"vendor_invoice_id": str(invoice_id), "amount": "1000", "notes": "Tranche 1"}
                ]
            },
        )

        data = ok(response)
        assert data["status"] == "ALLOCATED"
        assert money(data["allocated_amount"]) == Decimal("1000")
        assert money(data["unallocated_amount"]) == Decimal("0")
        [allocation] = data["allocations"]
        assert allocation["vendor_invoice_id"] == str(invoice_id)
        assert allocation["invoice_code"] == invoice_code
        assert allocation["notes"] == "Tranche 1"
        assert await value(session, VendorInvoice.paid_amount, invoice_id) == Decimal("1000")
        assert await value(session, VendorInvoice.status, invoice_id) == (
            VendorInvoiceStatus.PARTIALLY_PAID
        )
        [entry] = [
            e
            for e in await audit_trail(session, payment_id, action=AuditAction.PAYMENT)
            if "allocations" in (e.new_values or {})
        ]
        assert by_value(entry.new_values["allocations"]) == {str(invoice_id): Decimal("1000")}

    async def test_replacing_with_nothing_releases_the_bill(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id, principal=principal)
        await settle(session, tenant, payment_id, invoice_id, "1000")

        data = ok(
            await api_client.post(
                f"/finance/payments/{payment_id}/allocate",
                json={"allocations": [], "replace": True},
            )
        )

        assert data["status"] == "APPROVED"
        assert money(data["allocated_amount"]) == Decimal("0")
        assert data["allocations"] == []
        assert await value(session, VendorInvoice.paid_amount, invoice_id) == Decimal("0")
        assert await value(session, VendorInvoice.status, invoice_id) == (
            VendorInvoiceStatus.APPROVED
        )

    @pytest.mark.parametrize(
        ("payment_amount", "allocated", "detail"),
        [("5000", "3000", "outstanding_amount"), ("1000", "2000", "payment_amount")],
        ids=["beyond-what-the-bill-owes", "beyond-what-the-payment-is-worth"],
    )
    async def test_an_allocation_is_bounded_by_the_bill_and_the_payment(
        self, api_client, session, tenant, principal, purchase, payment_amount, allocated, detail
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        payment_id = await vendor_payment(
            session, tenant, purchase.vendor_id, amount=payment_amount, principal=principal
        )

        response = await api_client.post(
            f"/finance/payments/{payment_id}/allocate",
            json={"allocations": [{"vendor_invoice_id": str(invoice_id), "amount": allocated}]},
        )

        assert detail in refused(response, 422, "BUSINESS_RULE_VIOLATION")["details"]
        assert await value(session, VendorInvoice.paid_amount, invoice_id) == Decimal("0")
        assert await value(session, Payment.allocated_amount, payment_id) == Decimal("0")

    async def test_a_draft_payment_cannot_settle_anything(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id)

        response = await api_client.post(
            f"/finance/payments/{payment_id}/allocate",
            json={"allocations": [{"vendor_invoice_id": str(invoice_id), "amount": "100"}]},
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")["details"]["status"] == "DRAFT"
        assert await value(session, VendorInvoice.paid_amount, invoice_id) == Decimal("0")

    async def test_a_vendor_payment_cannot_settle_a_sales_invoice(
        self, api_client, session, tenant, principal, purchase
    ):
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id, principal=principal)

        response = await api_client.post(
            f"/finance/payments/{payment_id}/allocate",
            json={"allocations": [{"sales_invoice_id": str(uuid.uuid4()), "amount": "100"}]},
        )

        refused(response, 422, "VALIDATION_ERROR")
        assert await value(session, Payment.allocated_amount, payment_id) == Decimal("0")

    @pytest.mark.parametrize(
        ("allocations", "field"),
        [
            ([], "_root"),
            ([{"amount": "100"}], "allocations.0"),
            ([{"vendor_invoice_id": str(uuid.UUID(int=1)), "amount": "0"}], "allocations.0.amount"),
        ],
        ids=["empty-without-replace", "no-invoice-named", "zero-amount"],
    )
    async def test_a_malformed_allocation_is_a_field_error(
        self, api_client, session, tenant, principal, purchase, allocations, field
    ):
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id, principal=principal)

        response = await api_client.post(
            f"/finance/payments/{payment_id}/allocate", json={"allocations": allocations}
        )

        assert field in field_errors(response)
        assert await value(session, Payment.allocated_amount, payment_id) == Decimal("0")

    async def test_another_companys_payment_is_not_found(
        self, api_client, session, tenant, principal, purchase, outsider
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        payment_id = await vendor_payment(session, tenant, purchase.vendor_id, principal=principal)
        sign_in(outsider)

        response = await api_client.post(
            f"/finance/payments/{payment_id}/allocate",
            json={"allocations": [{"vendor_invoice_id": str(invoice_id), "amount": "100"}]},
        )

        refused(response, 404, "NOT_FOUND")
        assert await value(session, VendorInvoice.paid_amount, invoice_id) == Decimal("0")

    async def test_a_customer_receipt_settles_a_sales_invoice(
        self, api_client, session, tenant, principal, bench
    ):
        invoice = await issued(bench)
        receipt = await finance_service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.RECEIPT,
                party_type=PartyType.CUSTOMER,
                party_id=invoice.customer_id,
                payment_date=today(),
                amount=Decimal("1000"),
                payment_method=PaymentMethod.CASH,
            ),
        )
        await finance_service.approve_payment(session, tenant, principal, receipt.id)

        data = ok(
            await api_client.post(
                f"/finance/payments/{receipt.id}/allocate",
                json={"allocations": [{"sales_invoice_id": str(invoice.id), "amount": "1000"}]},
            )
        )

        assert data["status"] == "ALLOCATED"
        assert [row["sales_invoice_id"] for row in data["allocations"]] == [str(invoice.id)]
        assert await value(session, SalesInvoice.paid_amount, invoice.id) == Decimal("1000")
        assert await value(session, SalesInvoice.status, invoice.id) == (
            SalesInvoiceStatus.PARTIALLY_PAID
        )
        [entry] = [
            e
            for e in await audit_trail(session, receipt.id, action=AuditAction.PAYMENT)
            if "allocated_amount" in (e.new_values or {})
        ]
        assert money(entry.new_values["allocated_amount"]) == Decimal("1000")


# -----------------------------------------------------------------------------
# POST /finance/purchase-returns/{purchase_return_id}/debit-allocations
# -----------------------------------------------------------------------------


async def draft_return(
    session: AsyncSession, tenant: TenantContext, p: SimpleNamespace
) -> uuid.UUID:
    """A purchase return still in DRAFT: no debit note, nothing posted."""
    row = PurchaseReturn(
        company_id=tenant.company_id,
        code=f"PRTN-{uuid.uuid4().hex[:8].upper()}",
        vendor_id=p.vendor_id,
        goods_receipt_id=p.receipt_id,
        purchase_order_id=p.order_id,
        warehouse_id=p.warehouse_id,
        return_date=date(2026, 5, 6),
        status=PurchaseReturnStatus.DRAFT,
        reason="Damaged goods to go back",
        currency="INR",
        subtotal=Decimal("500"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("500"),
    )
    session.add(row)
    await session.flush()
    return row.id


def debit_path(return_id: uuid.UUID) -> str:
    return f"/finance/purchase-returns/{return_id}/debit-allocations"


async def debit_rows(session: AsyncSession, return_id: uuid.UUID) -> int:
    return await session.scalar(
        select(func.count()).where(PurchaseReturnDebitAllocation.purchase_return_id == return_id)
    )


class TestPurchaseReturnDebitAllocation:
    async def test_a_posted_return_settles_a_bill_of_its_vendor(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        invoice_code = await value(session, VendorInvoice.code, invoice_id)
        returned = await posted_purchase_return(session, tenant, purchase.bench, total_amount="500")
        return_id = returned.id

        response = await api_client.post(
            debit_path(return_id),
            json={
                "allocations": [
                    {"vendor_invoice_id": str(invoice_id), "amount": "500", "notes": "Damaged"}
                ]
            },
        )

        data = ok(response)
        assert data["purchase_return_id"] == str(return_id)
        assert money(data["total_amount"]) == Decimal("500")
        assert money(data["allocated_amount"]) == Decimal("500")
        assert money(data["unallocated_amount"]) == Decimal("0")
        [allocation] = data["allocations"]
        assert allocation["invoice_code"] == invoice_code
        assert allocation["notes"] == "Damaged"
        assert await value(session, VendorInvoice.debit_allocated_amount, invoice_id) == (
            Decimal("500")
        )
        assert await value(session, VendorInvoice.status, invoice_id) == (
            VendorInvoiceStatus.PARTIALLY_PAID
        )
        assert await debit_rows(session, return_id) == 1
        [entry] = await audit_trail(
            session, return_id, entity_type="purchase_return_debit_allocation"
        )
        assert entry.action == AuditAction.PAYMENT
        assert by_value(entry.new_values["allocations"]) == {str(invoice_id): Decimal("500")}

    async def test_more_than_the_return_is_worth_is_refused(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        return_id = (
            await posted_purchase_return(session, tenant, purchase.bench, total_amount="500")
        ).id

        response = await api_client.post(
            debit_path(return_id),
            json={"allocations": [{"vendor_invoice_id": str(invoice_id), "amount": "501"}]},
        )

        error = refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert money(error["details"]["return_total"]) == Decimal("500")
        assert await value(session, VendorInvoice.debit_allocated_amount, invoice_id) == 0
        assert await debit_rows(session, return_id) == 0

    async def test_a_return_that_was_never_posted_cannot_settle_a_bill(
        self, api_client, session, tenant, principal, purchase
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        return_id = await draft_return(session, tenant, purchase)

        response = await api_client.post(
            debit_path(return_id),
            json={"allocations": [{"vendor_invoice_id": str(invoice_id), "amount": "100"}]},
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")["details"]["status"] == "DRAFT"
        assert await debit_rows(session, return_id) == 0

    async def test_the_same_bill_twice_is_a_field_error(
        self, api_client, session, tenant, purchase
    ):
        return_id = (
            await posted_purchase_return(session, tenant, purchase.bench, total_amount="500")
        ).id
        twice = {"vendor_invoice_id": str(uuid.uuid4()), "amount": "100"}

        response = await api_client.post(
            debit_path(return_id), json={"allocations": [twice, twice]}
        )

        assert "_root" in field_errors(response)
        assert await debit_rows(session, return_id) == 0

    async def test_another_companys_return_is_not_found(
        self, api_client, session, tenant, principal, purchase, outsider
    ):
        invoice_id = await approved_bill(session, tenant, principal, purchase)
        return_id = (
            await posted_purchase_return(session, tenant, purchase.bench, total_amount="500")
        ).id
        sign_in(outsider)

        response = await api_client.post(
            debit_path(return_id),
            json={"allocations": [{"vendor_invoice_id": str(invoice_id), "amount": "100"}]},
        )

        refused(response, 404, "NOT_FOUND")
        assert await debit_rows(session, return_id) == 0


# -----------------------------------------------------------------------------
# POST /finance/credit-notes, POST /finance/credit-notes/{note_id}/cancel
# -----------------------------------------------------------------------------


def credit_json(invoice_id: uuid.UUID, line: dict[str, Any], **change: Any) -> dict[str, Any]:
    return {
        "sales_invoice_id": str(invoice_id),
        "note_date": today().isoformat(),
        "reason_type": "PRICE_ADJUSTMENT",
        "lines": [line],
        **change,
    }


async def notes_against(session: AsyncSession, invoice_id: uuid.UUID) -> int:
    return await session.scalar(
        select(func.count()).where(CreditNote.sales_invoice_id == invoice_id)
    )


class TestCreditNotes:
    async def test_a_price_adjustment_credits_the_invoice_and_the_customer(
        self, api_client, session, tenant, bench
    ):
        invoice = await issued(bench)
        owed = await value(session, Customer.outstanding_amount, invoice.customer_id)

        response = await api_client.post(
            "/finance/credit-notes",
            json=credit_json(
                invoice.id, {"sales_invoice_line_id": str(invoice.line_id), "taxable_amount": "100"}
            ),
        )

        data = ok(response, 201)
        assert data["status"] == "ISSUED"
        assert data["code"]
        assert data["sales_invoice_id"] == str(invoice.id)
        assert data["customer_id"] == str(invoice.customer_id)
        assert money(data["taxable_amount"]) == Decimal("100")
        assert money(data["tax_amount"]) == Decimal("18")
        assert [money(line["taxable_amount"]) for line in data["lines"]] == [Decimal("100")]
        note_id, total = uuid.UUID(data["id"]), money(data["total_amount"])
        assert await value(session, SalesInvoiceLine.credited_amount, invoice.line_id) == 100
        assert await value(session, Customer.outstanding_amount, invoice.customer_id) == (
            owed - total
        )
        legs = await journal_legs(session, await value(session, CreditNote.journal_id, note_id))
        balanced(legs)
        assert party_net(legs, PartyType.CUSTOMER) == total
        [entry] = await audit_trail(session, note_id, action=AuditAction.CREATE)
        assert entry.entity_type == "credit_note"
        assert entry.created_by_id == tenant.user_id

    async def test_the_customer_statement_names_the_credit_note(self, api_client, bench):
        invoice = await issued(bench)
        note = ok(
            await api_client.post(
                "/finance/credit-notes",
                json=credit_json(
                    invoice.id,
                    {"sales_invoice_line_id": str(invoice.line_id), "taxable_amount": "100"},
                ),
            ),
            201,
        )

        statement = ok(await api_client.get(f"/finance/ledger/customer/{invoice.customer_id}"))

        credits = [row for row in statement["rows"] if money(row["credit_amount"]) > 0]
        assert [row["reference_code"] for row in credits] == [note["code"]], credits

    async def test_the_note_and_its_reversal_are_traced_to_the_note(
        self, api_client, session, bench
    ):
        invoice = await issued(bench)
        note_id = await credit_note(bench, invoice.id, invoice.line_id)
        code = await value(session, CreditNote.code, note_id)
        posted = await ledger_legs(session, note_id)
        assert {leg.journal_id for leg in posted} == {
            await value(session, CreditNote.journal_id, note_id)
        }
        assert {leg.reference_code for leg in posted} == {code}

        ok(
            await api_client.post(
                f"/finance/credit-notes/{note_id}/cancel", json={"reason": REASON}
            )
        )

        legs = await ledger_legs(session, note_id)
        assert len([leg for leg in legs if leg.is_reversal]) == len(posted)
        assert {leg.reference_code for leg in legs} == {code}
        balanced(legs)
        assert party_net(legs, PartyType.CUSTOMER) == Decimal("0")

    @pytest.mark.parametrize(
        "line",
        [{"quantity": "201"}, {"taxable_amount": "5000"}],
        ids=["more-units-than-invoiced", "more-value-than-invoiced"],
    )
    async def test_a_credit_beyond_the_invoice_line_is_refused(
        self, api_client, session, bench, line
    ):
        invoice = await issued(bench)

        response = await api_client.post(
            "/finance/credit-notes",
            json=credit_json(invoice.id, {"sales_invoice_line_id": str(invoice.line_id), **line}),
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await notes_against(session, invoice.id) == 0
        assert await value(session, SalesInvoiceLine.credited_amount, invoice.line_id) == 0

    async def test_a_line_from_another_invoice_is_refused(self, api_client, session, bench):
        invoice = await issued(bench)

        response = await api_client.post(
            "/finance/credit-notes",
            json=credit_json(
                invoice.id, {"sales_invoice_line_id": str(uuid.uuid4()), "taxable_amount": "10"}
            ),
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await notes_against(session, invoice.id) == 0

    @pytest.mark.parametrize(
        ("line", "change", "field"),
        [
            ({"quantity": "1", "taxable_amount": "10"}, {}, "lines.0"),
            ({"quantity": "0"}, {}, "lines.0.quantity"),
            ({"quantity": "1"}, {"reason_type": "RETURN"}, "_root"),
            ({"quantity": "1"}, {"reason_type": "GOODWILL"}, "reason_type"),
        ],
        ids=["two-credit-bases", "zero-quantity", "return-without-a-return", "unknown-reason"],
    )
    async def test_a_malformed_credit_note_is_a_field_error(self, api_client, line, change, field):
        body = credit_json(
            uuid.uuid4(), {"sales_invoice_line_id": str(uuid.uuid4()), **line}, **change
        )

        assert field in field_errors(await api_client.post("/finance/credit-notes", json=body))

    async def test_another_companys_invoice_is_not_found(
        self, api_client, session, bench, outsider
    ):
        invoice = await issued(bench)
        sign_in(outsider)

        response = await api_client.post(
            "/finance/credit-notes",
            json=credit_json(
                invoice.id, {"sales_invoice_line_id": str(invoice.line_id), "taxable_amount": "10"}
            ),
        )

        refused(response, 404, "NOT_FOUND")
        assert await notes_against(session, invoice.id) == 0

    async def test_cancelling_restores_the_invoice_and_the_customer(
        self, api_client, session, bench
    ):
        invoice = await issued(bench)
        note_id = await credit_note(bench, invoice.id, invoice.line_id)
        total = await value(session, CreditNote.total_amount, note_id)
        owed = await value(session, Customer.outstanding_amount, invoice.customer_id)
        reason = "Credit raised against the wrong price"

        data = ok(
            await api_client.post(
                f"/finance/credit-notes/{note_id}/cancel", json={"reason": reason}
            )
        )

        assert data["status"] == "CANCELLED"
        assert data["cancellation_reason"] == reason
        assert data["cancelled_at"] is not None
        assert await value(session, SalesInvoiceLine.credited_amount, invoice.line_id) == 0
        assert await value(session, Customer.outstanding_amount, invoice.customer_id) == (
            owed + total
        )
        legs = await journal_legs(session, await value(session, CreditNote.journal_id, note_id))
        assert any(leg.is_reversal for leg in legs)
        balanced(legs)
        assert party_net(legs, PartyType.CUSTOMER) == Decimal("0")
        [entry] = await audit_trail(session, note_id, action=AuditAction.STATUS_CHANGE)
        assert status_moves([entry]) == [("ISSUED", "CANCELLED")]
        assert entry.context["reason"] == reason

    async def test_a_cancelled_note_cannot_be_cancelled_again(self, api_client, session, bench):
        invoice = await issued(bench)
        note_id = await credit_note(bench, invoice.id, invoice.line_id)
        await credit_note_service.cancel_credit_note(
            bench.session, bench.tenant, note_id, "First cancellation"
        )
        owed = await value(session, Customer.outstanding_amount, invoice.customer_id)

        response = await api_client.post(
            f"/finance/credit-notes/{note_id}/cancel", json={"reason": "A second story"}
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await value(session, CreditNote.cancellation_reason, note_id) == (
            "First cancellation"
        )
        assert await value(session, Customer.outstanding_amount, invoice.customer_id) == owed

    async def test_another_companys_note_is_not_found(self, api_client, session, bench, outsider):
        invoice = await issued(bench)
        note_id = await credit_note(bench, invoice.id, invoice.line_id)
        sign_in(outsider)

        response = await api_client.post(
            f"/finance/credit-notes/{note_id}/cancel", json={"reason": "Cancelled from outside"}
        )

        refused(response, 404, "NOT_FOUND")
        assert await value(session, CreditNote.status, note_id) == "ISSUED"

    async def test_a_cancellation_needs_a_reason(self, api_client):
        response = await api_client.post(
            f"/finance/credit-notes/{uuid.uuid4()}/cancel", json={"reason": "no"}
        )

        assert "reason" in field_errors(response)


# -----------------------------------------------------------------------------
# POST /finance/sales-invoices, POST /finance/sales-invoices/{invoice_id}/cancel
# -----------------------------------------------------------------------------


async def staged_dispatch(b: SimpleNamespace) -> uuid.UUID:
    """A consignment created but not yet marked ready: NOT_DISPATCHED."""
    body = DispatchCreate.model_validate(
        {
            "dispatch_type": "SALES",
            "customer_id": str(b.customer.id),
            "warehouse_id": str(b.warehouse.id),
            "sales_order_id": str(b.order.id),
            "dispatch_date": today().isoformat(),
            "lines": [
                {
                    "item_id": str(b.item.id),
                    "sales_order_line_id": str(b.line.id),
                    "quantity": "200",
                    "serial_ids": [],
                    "carton_ids": [],
                }
            ],
        }
    )
    return (await dispatch_service.create_dispatch(b.session, b.tenant, body)).id


async def invoices_for(session: AsyncSession, dispatch_id: uuid.UUID) -> int:
    return await session.scalar(select(func.count()).where(SalesInvoice.dispatch_id == dispatch_id))


class TestSalesInvoices:
    async def test_issuing_posts_the_receivable_and_invoices_the_order_line(
        self, api_client, session, tenant, bench
    ):
        dispatch_id = (await make_dispatch(bench)).id
        customer_id, order_line_id = bench.customer.id, bench.line.id

        response = await api_client.post(
            "/finance/sales-invoices",
            json={"dispatch_id": str(dispatch_id), "vehicle_number": "GJ01AB1234"},
        )

        data = ok(response, 201)
        assert data["status"] == "ISSUED"
        assert data["code"]
        assert data["dispatch_id"] == str(dispatch_id)
        assert data["customer_id"] == str(customer_id)
        assert data["vehicle_number"] == "GJ01AB1234"
        assert [money(line["quantity"]) for line in data["lines"]] == [Decimal("200")]
        invoice_id, total = uuid.UUID(data["id"]), money(data["total_amount"])
        assert await value(session, SalesOrderLine.invoiced_quantity, order_line_id) == 200
        assert await value(session, Customer.outstanding_amount, customer_id) == total
        legs = await ledger_legs(session, invoice_id)
        balanced(legs)
        assert party_net(legs, PartyType.CUSTOMER) == -total
        [entry] = await audit_trail(session, invoice_id, action=AuditAction.CREATE)
        assert entry.entity_type == "sales_invoice"
        assert entry.new_values["dispatch_id"] == str(dispatch_id)
        assert entry.created_by_id == tenant.user_id

    async def test_a_dispatch_is_invoiced_once(self, api_client, session, bench):
        dispatch = await make_dispatch(bench)
        dispatch_id = dispatch.id
        await issue(bench, dispatch)

        response = await api_client.post(
            "/finance/sales-invoices", json={"dispatch_id": str(dispatch_id)}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await invoices_for(session, dispatch_id) == 1

    async def test_a_consignment_still_being_staged_cannot_be_invoiced(
        self, api_client, session, bench
    ):
        dispatch_id = await staged_dispatch(bench)

        response = await api_client.post(
            "/finance/sales-invoices", json={"dispatch_id": str(dispatch_id)}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await invoices_for(session, dispatch_id) == 0

    async def test_another_companys_dispatch_is_not_found(
        self, api_client, session, bench, outsider
    ):
        dispatch_id = (await make_dispatch(bench)).id
        sign_in(outsider)

        response = await api_client.post(
            "/finance/sales-invoices", json={"dispatch_id": str(dispatch_id)}
        )

        refused(response, 404, "NOT_FOUND")
        assert await invoices_for(session, dispatch_id) == 0

    @pytest.mark.parametrize(
        ("change", "field"),
        [
            ({"total_amount": "1"}, "total_amount"),
            ({"vehicle_number": "V" * 25}, "vehicle_number"),
            ({"dispatch_id": "DSP-1"}, "dispatch_id"),
        ],
        ids=["client-total", "over-long-vehicle", "not-a-uuid"],
    )
    async def test_a_malformed_issue_is_a_field_error(self, api_client, change, field):
        body = {"dispatch_id": str(uuid.uuid4()), **change}

        assert field in field_errors(await api_client.post("/finance/sales-invoices", json=body))

    async def test_cancelling_inside_the_window_reverses_the_receivable(
        self, api_client, session, bench
    ):
        invoice = await issued(bench)
        reason = "Invoice raised on the wrong consignment"

        data = ok(
            await api_client.post(
                f"/finance/sales-invoices/{invoice.id}/cancel", json={"reason": reason}
            )
        )

        assert data["status"] == "CANCELLED"
        assert data["cancellation_reason"] == reason
        assert data["cancelled_at"] is not None
        legs = await ledger_legs(session, invoice.id)
        assert any(leg.is_reversal for leg in legs)
        balanced(legs)
        assert party_net(legs, PartyType.CUSTOMER) == Decimal("0")
        assert await value(session, Customer.outstanding_amount, invoice.customer_id) == 0
        assert await value(session, SalesOrderLine.invoiced_quantity, invoice.order_line_id) == 0
        [entry] = await audit_trail(session, invoice.id, action=AuditAction.CANCEL)
        assert entry.entity_type == "sales_invoice"
        assert entry.context["reason"] == reason

    async def test_an_invoice_past_its_cancellation_window_is_refused(
        self, api_client, session, bench
    ):
        invoice = await issued(bench)
        row = await session.get(SalesInvoice, invoice.id)
        row.issued_at = utcnow() - timedelta(hours=25)
        await session.flush()

        response = await api_client.post(
            f"/finance/sales-invoices/{invoice.id}/cancel", json={"reason": REASON}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await value(session, SalesInvoice.status, invoice.id) == SalesInvoiceStatus.ISSUED
        assert await value(session, Customer.outstanding_amount, invoice.customer_id) == (
            invoice.total
        )

    async def test_a_credited_invoice_cannot_be_cancelled(self, api_client, session, bench):
        invoice = await issued(bench)
        await credit_note(bench, invoice.id, invoice.line_id)

        response = await api_client.post(
            f"/finance/sales-invoices/{invoice.id}/cancel", json={"reason": REASON}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await value(session, SalesInvoice.status, invoice.id) == SalesInvoiceStatus.ISSUED
        assert not any(leg.is_reversal for leg in await ledger_legs(session, invoice.id))

    async def test_another_companys_invoice_is_not_found(
        self, api_client, session, bench, outsider
    ):
        invoice = await issued(bench)
        sign_in(outsider)

        response = await api_client.post(
            f"/finance/sales-invoices/{invoice.id}/cancel", json={"reason": REASON}
        )

        refused(response, 404, "NOT_FOUND")
        assert await value(session, SalesInvoice.status, invoice.id) == SalesInvoiceStatus.ISSUED

    async def test_a_cancellation_needs_a_reason(self, api_client):
        response = await api_client.post(
            f"/finance/sales-invoices/{uuid.uuid4()}/cancel", json={"reason": "no"}
        )

        assert "reason" in field_errors(response)
