"""HTTP surface for the ledger, vendor invoices, payments and finance reports.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: every literal segment below (``/accounts``, ``/ledger``,
``/vendor-invoices``, ``/payments``, ``/customer-adjustments``, ``/outstanding``,
``/ageing``, ``/inventory-valuation``, ``/gst-summary``) is distinct, and no path parameter
sits at the router root - so nothing here can capture a sibling's literal
segment and reject it as a malformed UUID.

Permission note: the chart of accounts is configuration rather than a finance
document, so its writes are gated on ``settings.manage`` while its reads sit
with the rest of the ledger on ``ledger.view``. The permission registry defines
no ``ledger.manage``, and inventing one would put a string in the API contract
that no role, seed or matrix knows about.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import (
    CurrentUser,
    DbSession,
    Tenant,
    forbid_dealer_users,
    forbid_vendor_users,
    require_permission,
)
from src.core.pagination import (
    PageParams,
    SortParams,
    bind_query_filters,
    page_params,
    sort_params,
)
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    AcceptedJobResponse,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.finance import credit_note_service, sales_invoice_service, service
from src.modules.finance.credit_note_schemas import (
    CreditNoteCancel,
    CreditNoteCreate,
    CreditNoteDetail,
    CreditNoteSummary,
)
from src.modules.finance.models import (
    LedgerAccountType,
    MatchStatus,
    PartyType,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
    SalesInvoiceStatus,
    VendorInvoiceStatus,
    VendorInvoiceType,
)
from src.modules.finance.sales_invoice_schemas import (
    SalesInvoiceCancel,
    SalesInvoiceCreate,
    SalesInvoiceDetail,
    SalesInvoiceFilters,
    SalesInvoicePreview,
    SalesInvoiceSummary,
)
from src.modules.finance.schemas import (
    AgeingReport,
    AllocationRequest,
    CustomerAdjustmentCreate,
    CustomerAdjustmentRead,
    DisputeRequest,
    GstSummary,
    InventoryValuationPage,
    InvoiceApproval,
    LedgerAccountCreate,
    LedgerAccountFilters,
    LedgerAccountRead,
    LedgerAccountUpdate,
    LedgerStatement,
    MatchResult,
    OutstandingRow,
    PaymentCreate,
    PaymentDetail,
    PaymentFilters,
    PaymentSummary,
    PaymentUpdate,
    ReasonRequest,
    StatementFilters,
    ValuationFilters,
    VendorInvoiceCreate,
    VendorInvoiceDetail,
    VendorInvoiceFilters,
    VendorInvoiceSummary,
    VendorInvoiceUpdate,
)
from src.modules.reports import service as reports_service

router = APIRouter(prefix="/finance", tags=["Finance"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Deletion carries its reason in the query string: a DELETE body is legal but
# several HTTP clients and proxies drop it, and the reason is not optional on a
# financial document.
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=500, description="Why this is being removed")
]


@router.get(
    "/credit-notes",
    response_model=PaginatedResponse[CreditNoteSummary],
    dependencies=[require_permission(Permission.CREDIT_NOTE_VIEW)],
)
async def list_credit_notes(
    session: DbSession, tenant: Tenant, params: PageDep, sort: SortDep
) -> PaginatedResponse[CreditNoteSummary]:
    rows, pagination = await credit_note_service.list_credit_notes(session, tenant, params, sort)
    return PaginatedResponse[CreditNoteSummary](data=rows, pagination=pagination)


@router.post(
    "/credit-notes",
    response_model=ApiResponse[CreditNoteDetail],
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission(Permission.CREDIT_NOTE_CREATE)],
)
async def create_credit_note(
    payload: CreditNoteCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[CreditNoteDetail]:
    note = await credit_note_service.issue_credit_note(session, tenant, payload)
    return ApiResponse[CreditNoteDetail].ok(note, "Credit note issued")


@router.get(
    "/credit-notes/{note_id}",
    response_model=ApiResponse[CreditNoteDetail],
    dependencies=[require_permission(Permission.CREDIT_NOTE_VIEW)],
)
async def get_credit_note(
    note_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[CreditNoteDetail]:
    return ApiResponse[CreditNoteDetail].ok(
        await credit_note_service.get_credit_note(session, tenant, note_id)
    )


@router.post(
    "/credit-notes/{note_id}/cancel",
    response_model=ApiResponse[CreditNoteDetail],
    dependencies=[require_permission(Permission.CREDIT_NOTE_CANCEL)],
)
async def cancel_credit_note(
    note_id: uuid.UUID,
    payload: CreditNoteCancel,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[CreditNoteDetail]:
    note = await credit_note_service.cancel_credit_note(session, tenant, note_id, payload.reason)
    return ApiResponse[CreditNoteDetail].ok(note, "Credit note cancelled")


# -----------------------------------------------------------------------------
# Chart of accounts
# -----------------------------------------------------------------------------


def account_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code or name")] = None,
    account_type: Annotated[LedgerAccountType | None, Query()] = None,
    parent_id: Annotated[uuid.UUID | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    is_system: Annotated[bool | None, Query()] = None,
) -> LedgerAccountFilters:
    return LedgerAccountFilters(
        search=search,
        account_type=account_type,
        parent_id=parent_id,
        is_active=is_active,
        is_system=is_system,
    )


@router.get(
    "/accounts",
    response_model=PaginatedResponse[LedgerAccountRead],
    summary="List the chart of accounts",
    dependencies=[require_permission(Permission.LEDGER_VIEW)],
)
async def list_accounts(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    sort: SortDep,
    filters: Annotated[LedgerAccountFilters, Depends(account_filters)],
) -> PaginatedResponse[LedgerAccountRead]:
    rows, pagination = await service.list_accounts(
        session, tenant, filters=filters, params=params, sort=sort
    )
    return PaginatedResponse[LedgerAccountRead](data=rows, pagination=pagination)


@router.post(
    "/accounts",
    response_model=ApiResponse[LedgerAccountRead],
    status_code=status.HTTP_201_CREATED,
    summary="Open an account in the chart",
    description=(
        "The control accounts the automatic postings depend on are provisioned by the module "
        "itself on first use and cannot be created here - a client that could claim a system "
        "code could redirect every payables posting in the company."
    ),
    dependencies=[require_permission(Permission.SETTINGS_MANAGE)],
)
async def create_account(
    session: DbSession, tenant: Tenant, payload: LedgerAccountCreate
) -> ApiResponse[LedgerAccountRead]:
    account = await service.create_account(session, tenant, payload)
    return ApiResponse[LedgerAccountRead].ok(account, "Ledger account created")


@router.get(
    "/accounts/{account_id}",
    response_model=ApiResponse[LedgerAccountRead],
    summary="One ledger account",
    dependencies=[require_permission(Permission.LEDGER_VIEW)],
)
async def get_account(
    session: DbSession, tenant: Tenant, account_id: uuid.UUID
) -> ApiResponse[LedgerAccountRead]:
    return ApiResponse[LedgerAccountRead].ok(await service.get_account(session, tenant, account_id))


@router.patch(
    "/accounts/{account_id}",
    response_model=ApiResponse[LedgerAccountRead],
    summary="Amend a ledger account",
    description=(
        "`account_type` is not amendable: re-typing an account with postings in it would "
        "silently restate every report ever run from it. A mis-typed account is retired and "
        "replaced."
    ),
    dependencies=[require_permission(Permission.SETTINGS_MANAGE)],
)
async def update_account(
    session: DbSession, tenant: Tenant, account_id: uuid.UUID, payload: LedgerAccountUpdate
) -> ApiResponse[LedgerAccountRead]:
    account = await service.update_account(session, tenant, account_id, payload)
    return ApiResponse[LedgerAccountRead].ok(account, "Ledger account updated")


@router.delete(
    "/accounts/{account_id}",
    response_model=ApiResponse[None],
    summary="Retire an account that has never been posted to",
    description=(
        "Refused for a control account, for an account with postings against it, and for one "
        "with accounts beneath it. An account with history is deactivated, never removed."
    ),
    dependencies=[require_permission(Permission.SETTINGS_MANAGE)],
)
async def delete_account(
    session: DbSession, tenant: Tenant, account_id: uuid.UUID, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.delete_account(session, tenant, account_id, reason=reason)
    return ApiResponse[None].ok(None, "Ledger account retired")


# -----------------------------------------------------------------------------
# Party statements
# -----------------------------------------------------------------------------


def statement_filters(
    date_from: Annotated[date | None, Query(description="Statement period start")] = None,
    date_to: Annotated[date | None, Query(description="Statement period end")] = None,
) -> StatementFilters:
    return bind_query_filters(StatementFilters, date_from=date_from, date_to=date_to)


StatementDep = Annotated[StatementFilters, Depends(statement_filters)]

_STATEMENT_DESCRIPTION = (
    "Every posting against the party in date order, each with the balance after it. The "
    "running balance is computed by PostgreSQL with a window function over the whole "
    "filtered set plus the opening balance carried in, so it is correct on every page - an "
    "accumulator in the application would restart at zero on page two. A positive balance "
    "always means money owed."
)


@router.get(
    "/ledger/customer/{customer_id}",
    response_model=ApiResponse[LedgerStatement],
    summary="A customer's ledger statement",
    description=_STATEMENT_DESCRIPTION,
    dependencies=[require_permission(Permission.LEDGER_VIEW)],
)
async def customer_statement(
    session: DbSession,
    tenant: Tenant,
    customer_id: uuid.UUID,
    params: PageDep,
    filters: StatementDep,
) -> ApiResponse[LedgerStatement]:
    statement, _ = await service.party_statement(
        session,
        tenant,
        party_type=PartyType.CUSTOMER,
        party_id=customer_id,
        filters=filters,
        params=params,
    )
    return ApiResponse[LedgerStatement].ok(statement)


@router.get(
    "/ledger/vendor/{vendor_id}",
    response_model=ApiResponse[LedgerStatement],
    summary="A vendor's ledger statement",
    description=_STATEMENT_DESCRIPTION,
    dependencies=[require_permission(Permission.LEDGER_VIEW)],
)
async def vendor_statement(
    session: DbSession,
    tenant: Tenant,
    vendor_id: uuid.UUID,
    params: PageDep,
    filters: StatementDep,
) -> ApiResponse[LedgerStatement]:
    statement, _ = await service.party_statement(
        session,
        tenant,
        party_type=PartyType.VENDOR,
        party_id=vendor_id,
        filters=filters,
        params=params,
    )
    return ApiResponse[LedgerStatement].ok(statement)


# -----------------------------------------------------------------------------
# Vendor invoices
# -----------------------------------------------------------------------------


def invoice_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Our code or the vendor's number")
    ] = None,
    invoice_status: Annotated[VendorInvoiceStatus | None, Query(alias="status")] = None,
    match_status: Annotated[MatchStatus | None, Query()] = None,
    invoice_type: Annotated[VendorInvoiceType | None, Query()] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    purchase_order_id: Annotated[uuid.UUID | None, Query()] = None,
    job_work_order_id: Annotated[uuid.UUID | None, Query()] = None,
    invoice_from: Annotated[date | None, Query()] = None,
    invoice_to: Annotated[date | None, Query()] = None,
    due_before: Annotated[date | None, Query()] = None,
    open_only: Annotated[bool, Query(description="Only invoices that still owe money")] = False,
    overdue_only: Annotated[bool, Query(description="Only invoices past their due date")] = False,
) -> VendorInvoiceFilters:
    return VendorInvoiceFilters(
        search=search,
        status=invoice_status,
        match_status=match_status,
        invoice_type=invoice_type,
        vendor_id=vendor_id,
        purchase_order_id=purchase_order_id,
        job_work_order_id=job_work_order_id,
        invoice_from=invoice_from,
        invoice_to=invoice_to,
        due_before=due_before,
        open_only=open_only,
        overdue_only=overdue_only,
    )


@router.get(
    "/vendor-invoices",
    response_model=PaginatedResponse[VendorInvoiceSummary],
    summary="List vendor invoices",
    dependencies=[require_permission(Permission.VENDOR_INVOICE_VIEW)],
)
async def list_vendor_invoices(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    sort: SortDep,
    filters: Annotated[VendorInvoiceFilters, Depends(invoice_filters)],
) -> PaginatedResponse[VendorInvoiceSummary]:
    rows, pagination = await service.list_invoices(
        session, tenant, filters=filters, params=params, sort=sort
    )
    return PaginatedResponse[VendorInvoiceSummary](data=rows, pagination=pagination)


@router.post(
    "/vendor-invoices",
    response_model=ApiResponse[VendorInvoiceDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Record a vendor invoice",
    description=(
        "Creates the bill in DRAFT; nothing reaches the ledger until it is approved. A "
        "vendor billing the same invoice number twice is refused - the duplicate is named in "
        "the error so the two can be compared."
    ),
    dependencies=[require_permission(Permission.VENDOR_INVOICE_CREATE)],
)
async def create_vendor_invoice(
    session: DbSession, tenant: Tenant, payload: VendorInvoiceCreate
) -> ApiResponse[VendorInvoiceDetail]:
    invoice = await service.create_invoice(session, tenant, payload)
    return ApiResponse[VendorInvoiceDetail].ok(invoice, "Vendor invoice recorded")


@router.get(
    "/vendor-invoices/{invoice_id}",
    response_model=ApiResponse[VendorInvoiceDetail],
    summary="One vendor invoice with its lines",
    dependencies=[require_permission(Permission.VENDOR_INVOICE_VIEW)],
)
async def get_vendor_invoice(
    session: DbSession, tenant: Tenant, invoice_id: uuid.UUID
) -> ApiResponse[VendorInvoiceDetail]:
    return ApiResponse[VendorInvoiceDetail].ok(
        await service.get_invoice_detail(session, tenant, invoice_id)
    )


@router.patch(
    "/vendor-invoices/{invoice_id}",
    response_model=ApiResponse[VendorInvoiceDetail],
    summary="Amend an invoice that has not been approved",
    description=(
        "Sending `lines` replaces the whole set. Any edit invalidates a previous three-way "
        "match, so the invoice has to be matched again before it can be approved."
    ),
    dependencies=[require_permission(Permission.VENDOR_INVOICE_CREATE)],
)
async def update_vendor_invoice(
    session: DbSession,
    tenant: Tenant,
    invoice_id: uuid.UUID,
    payload: VendorInvoiceUpdate,
) -> ApiResponse[VendorInvoiceDetail]:
    invoice = await service.update_invoice(session, tenant, invoice_id, payload)
    return ApiResponse[VendorInvoiceDetail].ok(invoice, "Vendor invoice updated")


@router.delete(
    "/vendor-invoices/{invoice_id}",
    response_model=ApiResponse[None],
    summary="Delete a draft invoice that was never used",
    description="Anything past DRAFT is cancelled instead, so the trail survives.",
    dependencies=[require_permission(Permission.VENDOR_INVOICE_CREATE)],
)
async def delete_vendor_invoice(
    session: DbSession, tenant: Tenant, invoice_id: uuid.UUID, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.delete_draft_invoice(session, tenant, invoice_id, reason=reason)
    return ApiResponse[None].ok(None, "Draft invoice deleted")


@router.post(
    "/vendor-invoices/{invoice_id}/submit",
    response_model=ApiResponse[VendorInvoiceDetail],
    summary="Send an invoice for approval",
    description=(
        "Runs the three-way match on the way in, so the approver sees the variance before "
        "opening the document rather than after approving it."
    ),
    dependencies=[require_permission(Permission.VENDOR_INVOICE_CREATE)],
)
async def submit_vendor_invoice(
    session: DbSession, tenant: Tenant, invoice_id: uuid.UUID, payload: ReasonRequest | None = None
) -> ApiResponse[VendorInvoiceDetail]:
    invoice = await service.submit_invoice(
        session, tenant, invoice_id, reason=payload.reason if payload else None
    )
    return ApiResponse[VendorInvoiceDetail].ok(invoice, "Submitted for approval")


@router.get(
    "/vendor-invoices/{invoice_id}/match",
    response_model=ApiResponse[MatchResult],
    summary="Three-way match against the purchase order and goods receipt",
    description=(
        "Compares the bill line by line: price against what was **ordered**, quantity against "
        "what was **accepted** at the gate. Reports the variance in money and as a proportion, "
        "and says exactly what blocks approval. Read-only - it decides nothing on its own."
    ),
    dependencies=[require_permission(Permission.VENDOR_INVOICE_VIEW)],
)
async def match_vendor_invoice(
    session: DbSession, tenant: Tenant, invoice_id: uuid.UUID
) -> ApiResponse[MatchResult]:
    return ApiResponse[MatchResult].ok(await service.match_invoice(session, tenant, invoice_id))


@router.post(
    "/vendor-invoices/{invoice_id}/approve",
    response_model=ApiResponse[VendorInvoiceDetail],
    summary="Approve an invoice for payment",
    description=(
        "Re-runs the match and refuses if it is outside tolerance - 2% on price, 0% on "
        "quantity - unless `override_reason` is supplied. The reason is stored on the invoice "
        "and in the audit log, so a bill passed over an objection can always be traced to a "
        "person and a justification. Approval posts the payable to the ledger."
    ),
    dependencies=[require_permission(Permission.VENDOR_INVOICE_APPROVE)],
)
async def approve_vendor_invoice(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    invoice_id: uuid.UUID,
    payload: InvoiceApproval,
) -> ApiResponse[VendorInvoiceDetail]:
    invoice = await service.approve_invoice(session, tenant, principal, invoice_id, payload)
    return ApiResponse[VendorInvoiceDetail].ok(invoice, "Vendor invoice approved")


@router.post(
    "/vendor-invoices/{invoice_id}/dispute",
    response_model=ApiResponse[VendorInvoiceDetail],
    summary="Hold an invoice while it is argued with the vendor",
    description=(
        "The money stays owed and the invoice stays on the ageing report: a dispute is not a "
        "write-off."
    ),
    dependencies=[require_permission(Permission.VENDOR_INVOICE_APPROVE)],
)
async def dispute_vendor_invoice(
    session: DbSession, tenant: Tenant, invoice_id: uuid.UUID, payload: DisputeRequest
) -> ApiResponse[VendorInvoiceDetail]:
    invoice = await service.dispute_invoice(session, tenant, invoice_id, reason=payload.reason)
    return ApiResponse[VendorInvoiceDetail].ok(invoice, "Vendor invoice disputed")


@router.post(
    "/vendor-invoices/{invoice_id}/resolve-dispute",
    response_model=ApiResponse[VendorInvoiceDetail],
    summary="Settle a dispute and return the invoice to the payment run",
    dependencies=[require_permission(Permission.VENDOR_INVOICE_APPROVE)],
)
async def resolve_vendor_invoice_dispute(
    session: DbSession, tenant: Tenant, invoice_id: uuid.UUID, payload: ReasonRequest
) -> ApiResponse[VendorInvoiceDetail]:
    invoice = await service.resolve_dispute(session, tenant, invoice_id, reason=payload.reason)
    return ApiResponse[VendorInvoiceDetail].ok(invoice, "Dispute resolved")


@router.post(
    "/vendor-invoices/{invoice_id}/cancel",
    response_model=ApiResponse[VendorInvoiceDetail],
    summary="Call off an invoice that will never be paid",
    description="Refused once anything has been allocated against it; reverse the payment first.",
    dependencies=[require_permission(Permission.VENDOR_INVOICE_APPROVE)],
)
async def cancel_vendor_invoice(
    session: DbSession, tenant: Tenant, invoice_id: uuid.UUID, payload: ReasonRequest
) -> ApiResponse[VendorInvoiceDetail]:
    invoice = await service.cancel_invoice(session, tenant, invoice_id, reason=payload.reason)
    return ApiResponse[VendorInvoiceDetail].ok(invoice, "Vendor invoice cancelled")


# -----------------------------------------------------------------------------
# Payments
# -----------------------------------------------------------------------------


def payment_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Our code or the instrument reference")
    ] = None,
    payment_status: Annotated[PaymentStatus | None, Query(alias="status")] = None,
    payment_type: Annotated[PaymentType | None, Query()] = None,
    party_type: Annotated[PartyType | None, Query()] = None,
    party_id: Annotated[uuid.UUID | None, Query()] = None,
    payment_method: Annotated[PaymentMethod | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    unallocated_only: Annotated[
        bool, Query(description="Approved money not yet matched to invoices")
    ] = False,
) -> PaymentFilters:
    return PaymentFilters(
        search=search,
        status=payment_status,
        payment_type=payment_type,
        party_type=party_type,
        party_id=party_id,
        payment_method=payment_method,
        date_from=date_from,
        date_to=date_to,
        unallocated_only=unallocated_only,
    )


@router.get(
    "/payments",
    response_model=PaginatedResponse[PaymentSummary],
    summary="List payments and receipts",
    dependencies=[require_permission(Permission.PAYMENT_VIEW)],
)
async def list_payments(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    sort: SortDep,
    filters: Annotated[PaymentFilters, Depends(payment_filters)],
) -> PaginatedResponse[PaymentSummary]:
    rows, pagination = await service.list_payments(
        session, tenant, filters=filters, params=params, sort=sort
    )
    return PaginatedResponse[PaymentSummary](data=rows, pagination=pagination)


@router.post(
    "/payments",
    response_model=ApiResponse[PaymentDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Record a payment or receipt",
    description=(
        "Creates the document in DRAFT. Money moves - and the ledger moves - at approval, and "
        "invoices are settled afterwards through `/allocate`."
    ),
    dependencies=[require_permission(Permission.PAYMENT_RECORD)],
)
async def create_payment(
    session: DbSession, tenant: Tenant, payload: PaymentCreate
) -> ApiResponse[PaymentDetail]:
    payment = await service.create_payment(session, tenant, payload)
    return ApiResponse[PaymentDetail].ok(payment, "Payment recorded")


@router.get(
    "/payments/{payment_id}",
    response_model=ApiResponse[PaymentDetail],
    summary="One payment with what it has settled",
    dependencies=[require_permission(Permission.PAYMENT_VIEW)],
)
async def get_payment(
    session: DbSession, tenant: Tenant, payment_id: uuid.UUID
) -> ApiResponse[PaymentDetail]:
    return ApiResponse[PaymentDetail].ok(
        await service.get_payment_detail(session, tenant, payment_id)
    )


@router.patch(
    "/payments/{payment_id}",
    response_model=ApiResponse[PaymentDetail],
    summary="Amend a payment that has not been approved",
    dependencies=[require_permission(Permission.PAYMENT_RECORD)],
)
async def update_payment(
    session: DbSession, tenant: Tenant, payment_id: uuid.UUID, payload: PaymentUpdate
) -> ApiResponse[PaymentDetail]:
    payment = await service.update_payment(session, tenant, payment_id, payload)
    return ApiResponse[PaymentDetail].ok(payment, "Payment updated")


@router.delete(
    "/payments/{payment_id}",
    response_model=ApiResponse[None],
    summary="Delete a draft payment",
    dependencies=[require_permission(Permission.PAYMENT_RECORD)],
)
async def delete_payment(
    session: DbSession, tenant: Tenant, payment_id: uuid.UUID, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.delete_draft_payment(session, tenant, payment_id, reason=reason)
    return ApiResponse[None].ok(None, "Draft payment deleted")


@router.post(
    "/payments/{payment_id}/approve",
    response_model=ApiResponse[PaymentDetail],
    summary="Approve a payment - the money moves",
    description=(
        "Posts the settlement: a vendor payment debits the payable and credits the bank, a "
        "customer receipt debits the bank, credits the receivable and reduces the customer's "
        "outstanding balance - all in one transaction."
    ),
    dependencies=[require_permission(Permission.PAYMENT_APPROVE)],
)
async def approve_payment(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    payment_id: uuid.UUID,
    payload: ReasonRequest | None = None,
) -> ApiResponse[PaymentDetail]:
    payment = await service.approve_payment(
        session, tenant, principal, payment_id, reason=payload.reason if payload else None
    )
    return ApiResponse[PaymentDetail].ok(payment, "Payment approved")


@router.post(
    "/payments/{payment_id}/cancel",
    response_model=ApiResponse[PaymentDetail],
    summary="Call off a payment, reversing its posting",
    description=(
        "An approved payment is reversed rather than deleted - the original journal stays and "
        "its mirror image is posted beside it. Refused while allocations stand."
    ),
    dependencies=[require_permission(Permission.PAYMENT_APPROVE)],
)
async def cancel_payment(
    session: DbSession, tenant: Tenant, payment_id: uuid.UUID, payload: ReasonRequest
) -> ApiResponse[PaymentDetail]:
    payment = await service.cancel_payment(session, tenant, payment_id, reason=payload.reason)
    return ApiResponse[PaymentDetail].ok(payment, "Payment cancelled and reversed")


@router.post(
    "/payments/{payment_id}/allocate",
    response_model=ApiResponse[PaymentDetail],
    summary="Settle invoices from an approved payment",
    description=(
        "An allocation may exceed neither what the payment is worth nor what the invoice "
        "still owes; both are read under a row lock. `replace` discards what the payment "
        "previously settled, otherwise naming an invoice again amends that allocation rather "
        "than adding a second one."
    ),
    dependencies=[require_permission(Permission.PAYMENT_RECORD)],
)
async def allocate_payment(
    session: DbSession, tenant: Tenant, payment_id: uuid.UUID, payload: AllocationRequest
) -> ApiResponse[PaymentDetail]:
    payment = await service.allocate_payment(session, tenant, payment_id, payload)
    return ApiResponse[PaymentDetail].ok(payment, "Payment allocated")


# -----------------------------------------------------------------------------
# Customer adjustments
# -----------------------------------------------------------------------------


@router.post(
    "/customer-adjustments",
    response_model=ApiResponse[CustomerAdjustmentRead],
    status_code=status.HTTP_201_CREATED,
    summary="Post a debit or credit note against a customer",
    description=(
        "A charge raises what the customer owes; a credit lowers it. The receivable journal, "
        "the customer's cached outstanding balance and the audit row are written in one "
        "transaction, so the balance a credit check reads can always be explained by the "
        "ledger - the journal carries the same `reference_code` the balance change records. "
        "Gated on `payment.approve` rather than `payment.record`: a credit note is money the "
        "company agrees not to collect."
    ),
    dependencies=[require_permission(Permission.PAYMENT_APPROVE)],
)
async def post_customer_adjustment(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    payload: CustomerAdjustmentCreate,
) -> ApiResponse[CustomerAdjustmentRead]:
    adjustment = await service.post_customer_adjustment(
        session, tenant, principal, payload.customer_id, payload
    )
    return ApiResponse[CustomerAdjustmentRead].ok(adjustment, "Customer adjustment posted")


# -----------------------------------------------------------------------------
# Reports
# -----------------------------------------------------------------------------


@router.get(
    "/outstanding",
    response_model=PaginatedResponse[OutstandingRow],
    summary="Open payables, oldest due first",
    description=(
        "Every vendor invoice that still owes money, with what is left, how many days it is "
        "overdue and which ageing bucket it falls in. One query, and the bucket boundaries are "
        "the same constant `/finance/ageing` aggregates on.\n\n"
        "This is a list of documents rather than an aggregate, so every denomination is "
        "returned and each row states its own. `currency` narrows it to one, which is how a "
        "row-level list is reconciled against one block of the ageing report."
    ),
    dependencies=[require_permission(Permission.FINANCE_VIEW)],
)
async def outstanding(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    vendor_id: Annotated[uuid.UUID | None, Query(description="Restrict to one vendor")] = None,
    overdue_only: Annotated[bool, Query(description="Only invoices past their due date")] = False,
    as_of: Annotated[date | None, Query(description="Report date; defaults to today")] = None,
    currency: Annotated[
        str | None,
        Query(
            min_length=3,
            max_length=3,
            description="ISO 4217 code, e.g. INR. Omit for every denomination.",
        ),
    ] = None,
) -> PaginatedResponse[OutstandingRow]:
    rows, pagination = await service.list_outstanding(
        session,
        tenant,
        params=params,
        vendor_id=vendor_id,
        overdue_only=overdue_only,
        as_of=as_of,
        currency=currency,
    )
    return PaginatedResponse[OutstandingRow](data=rows, pagination=pagination)


@router.get(
    "/ageing",
    response_model=ApiResponse[AgeingReport],
    summary="Ageing analysis bucketed 0-30 / 31-60 / 61-90 / 90+, per currency",
    description=(
        "One row per party and denomination in one aggregate query. Payables are aged from "
        "the invoice due date. Receivables are FIFO-netted per customer and currency; sales "
        "invoice debits age from their due dates, legacy journals from posting dates.\n\n"
        "There is no exchange-rate table in this schema, so nothing here is summed across "
        "denominations. The response carries one block per currency the company has an open "
        "item in, and `currencies[0]` - the block whose figures are also restated at the top "
        "level - is `currency` if you asked for one, otherwise the company's own currency if "
        "anything is open in it, and failing that the currency with the most open documents. "
        "Blocks are ordered by open-document count, never by amount: two denominations cannot "
        "be compared by size without a rate this schema does not hold. "
        "Asking for nothing therefore returns the whole book, never an empty page.\n\n"
        "`limit` is parties per denomination; each block reports `party_count` so a capped "
        "block is visible as capped, and its totals are the whole block's either way."
    ),
    dependencies=[require_permission(Permission.FINANCE_VIEW)],
)
async def ageing(
    session: DbSession,
    tenant: Tenant,
    party_type: Annotated[PartyType, Query(description="CUSTOMER or VENDOR")] = PartyType.VENDOR,
    party_id: Annotated[uuid.UUID | None, Query(description="Restrict to one party")] = None,
    as_of: Annotated[date | None, Query(description="Report date; defaults to today")] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=service.MAX_AGEING_PARTIES,
            description="Parties to return per denomination",
        ),
    ] = 200,
    currency: Annotated[
        str | None,
        Query(
            min_length=3,
            max_length=3,
            description=(
                "ISO 4217 code, e.g. INR. Chooses which denomination's block is restated at "
                "the top level; every denomination is reported either way."
            ),
        ),
    ] = None,
) -> ApiResponse[AgeingReport]:
    report = await service.ageing_report(
        session,
        tenant,
        party_type=party_type,
        as_of=as_of,
        party_id=party_id,
        limit=limit,
        currency=currency,
    )
    return ApiResponse[AgeingReport].ok(report)


@router.get(
    "/inventory-valuation",
    response_model=ApiResponse[InventoryValuationPage],
    summary="Value on-hand stock at FIFO cost",
    description=(
        "Grouped to item and warehouse, highest value first, with the totals for the whole "
        "filtered set beside the page. The full-company run is queued instead - see "
        "`POST /finance/inventory-valuation/export`."
    ),
    dependencies=[require_permission(Permission.INVENTORY_VALUATION_VIEW)],
)
async def inventory_valuation(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    category_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ApiResponse[InventoryValuationPage]:
    filters = ValuationFilters(warehouse_id=warehouse_id, item_id=item_id, category_id=category_id)
    rows, summary, pagination = await service.inventory_valuation(
        session, tenant, filters=filters, params=params
    )
    return ApiResponse[InventoryValuationPage].ok(
        InventoryValuationPage(summary=summary, pagination=pagination, rows=rows)
    )


@router.post(
    "/inventory-valuation/export",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue the full-company inventory valuation",
    description=(
        "A company-wide valuation is thousands of positions and a spreadsheet nobody waits "
        "for. It goes to the reports queue and the workbook appears in the document store "
        "when it is ready (sections 22, 35)."
    ),
    dependencies=[
        require_permission(Permission.INVENTORY_VALUATION_VIEW, Permission.REPORT_EXPORT),
        Depends(forbid_vendor_users),
        Depends(forbid_dealer_users),
    ],
)
async def export_inventory_valuation(
    session: DbSession,
    tenant: Tenant,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    category_id: Annotated[uuid.UUID | None, Query()] = None,
) -> AcceptedJobResponse:
    job_id = await service.queue_inventory_valuation(
        session, tenant, ValuationFilters(warehouse_id=warehouse_id, category_id=category_id)
    )
    return AcceptedJobResponse.create(
        job_id=job_id,
        status_url=reports_service.job_status_url(job_id),
        message="The inventory valuation is being generated",
    )


@router.get(
    "/gst-summary",
    response_model=ApiResponse[GstSummary],
    summary="Output and input GST for a period",
    description=(
        "Output tax comes from issued, non-cancelled sales invoice lines by invoice date, "
        "using the snapshotted HSN. Uninvoiced dispatches are reported separately. "
        "Input tax comes from vendor invoices "
        "accepted into the books. Both are grouped by HSN and rate slab, which is the shape a "
        "return is read from."
    ),
    dependencies=[require_permission(Permission.GST_REPORT_VIEW)],
)
async def gst_summary(
    session: DbSession,
    tenant: Tenant,
    date_from: Annotated[date, Query(description="Period start, inclusive")],
    date_to: Annotated[date, Query(description="Period end, inclusive")],
) -> ApiResponse[GstSummary]:
    summary = await service.gst_summary(session, tenant, date_from=date_from, date_to=date_to)
    return ApiResponse[GstSummary].ok(summary)


__all__ = ["router"]


# Sales invoices are issued directly; preview never allocates a document number.


def sales_invoice_filters(
    search: Annotated[str | None, Query(max_length=120)] = None,
    invoice_status: Annotated[SalesInvoiceStatus | None, Query(alias="status")] = None,
    customer_id: uuid.UUID | None = None,
    sales_order_id: uuid.UUID | None = None,
    dispatch_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    overdue_only: bool = False,
    open_only: bool = False,
) -> SalesInvoiceFilters:
    return SalesInvoiceFilters(
        search=search,
        status=invoice_status,
        customer_id=customer_id,
        sales_order_id=sales_order_id,
        dispatch_id=dispatch_id,
        date_from=date_from,
        date_to=date_to,
        overdue_only=overdue_only,
        open_only=open_only,
    )


@router.get(
    "/sales-invoices",
    response_model=PaginatedResponse[SalesInvoiceSummary],
    summary="List issued sales invoices",
    dependencies=[require_permission(Permission.SALES_INVOICE_VIEW)],
)
async def list_sales_invoices(
    session: DbSession,
    tenant: Tenant,
    filters: Annotated[SalesInvoiceFilters, Depends(sales_invoice_filters)],
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[SalesInvoiceSummary]:
    rows, pagination = await sales_invoice_service.list_sales_invoices(
        session, tenant, filters, params, sort
    )
    return PaginatedResponse[SalesInvoiceSummary](data=rows, pagination=pagination)


@router.get(
    "/sales-invoices/preview",
    response_model=ApiResponse[SalesInvoicePreview],
    summary="Preview a tax invoice without issuing it",
    dependencies=[require_permission(Permission.SALES_INVOICE_VIEW)],
)
async def preview_sales_invoice(
    dispatch_id: uuid.UUID, session: DbSession, tenant: Tenant, invoice_date: date | None = None
) -> ApiResponse[SalesInvoicePreview]:
    return ApiResponse[SalesInvoicePreview].ok(
        await sales_invoice_service.preview_sales_invoice(
            session, tenant, SalesInvoiceCreate(dispatch_id=dispatch_id, invoice_date=invoice_date)
        )
    )


@router.post(
    "/sales-invoices",
    response_model=ApiResponse[SalesInvoiceDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Issue and post a GST sales invoice",
    dependencies=[require_permission(Permission.SALES_INVOICE_CREATE)],
)
async def issue_sales_invoice(
    payload: SalesInvoiceCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesInvoiceDetail]:
    return ApiResponse[SalesInvoiceDetail].ok(
        await sales_invoice_service.issue_sales_invoice(session, tenant, payload),
        "Sales invoice issued",
    )


@router.get(
    "/sales-invoices/{invoice_id}",
    response_model=ApiResponse[SalesInvoiceDetail],
    summary="Read an issued sales invoice",
    dependencies=[require_permission(Permission.SALES_INVOICE_VIEW)],
)
async def get_sales_invoice(
    invoice_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesInvoiceDetail]:
    return ApiResponse[SalesInvoiceDetail].ok(
        await sales_invoice_service.get_sales_invoice(session, tenant, invoice_id)
    )


@router.post(
    "/sales-invoices/{invoice_id}/cancel",
    response_model=ApiResponse[SalesInvoiceDetail],
    summary="Cancel an unpaid invoice within its cancellation window",
    dependencies=[require_permission(Permission.SALES_INVOICE_CANCEL)],
)
async def cancel_sales_invoice(
    invoice_id: uuid.UUID, payload: SalesInvoiceCancel, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesInvoiceDetail]:
    return ApiResponse[SalesInvoiceDetail].ok(
        await sales_invoice_service.cancel_sales_invoice(
            session, tenant, invoice_id, reason=payload.reason
        ),
        "Sales invoice cancelled",
    )


@router.post(
    "/sales-invoices/{invoice_id}/pdf",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate a permanent GST sales invoice PDF",
    dependencies=[require_permission(Permission.SALES_INVOICE_VIEW)],
)
async def sales_invoice_pdf(
    invoice_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> AcceptedJobResponse:
    job_id = await sales_invoice_service.queue_sales_invoice_pdf(session, tenant, invoice_id)
    return AcceptedJobResponse.create(
        job_id=job_id,
        status_url=reports_service.job_status_url(job_id),
        message="Sales invoice PDF queued",
    )
