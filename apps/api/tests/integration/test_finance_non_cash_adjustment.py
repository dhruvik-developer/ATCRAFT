"""Legacy adjustment drafts can be repaired/cancelled, but cannot post bank money."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.errors import BusinessRuleError
from src.modules.finance import service
from src.modules.finance.models import (
    LedgerEntry,
    PartyType,
    Payment,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
)
from src.modules.finance.schemas import PaymentUpdate
from src.modules.vendors.models import Vendor, VendorType

pytestmark = pytest.mark.integration


async def legacy_draft(session, tenant):
    vendor = Vendor(
        company_id=tenant.company_id,
        code=f"V-{uuid.uuid4().hex[:10]}",
        name="Legacy Vendor",
        vendor_types=[VendorType.RAW_MATERIAL_SUPPLIER],
    )
    session.add(vendor)
    await session.flush()
    row = Payment(
        company_id=tenant.company_id,
        code=f"P-{uuid.uuid4().hex[:10]}",
        payment_type=PaymentType.PAYMENT,
        party_type=PartyType.VENDOR,
        party_id=vendor.id,
        payment_date=date(2026, 10, 14),
        amount=Decimal("100"),
        allocated_amount=Decimal("0"),
        currency="INR",
        payment_method=PaymentMethod.ADJUSTMENT,
        status=PaymentStatus.DRAFT,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.parametrize("operation", ["approve", "update"])
async def test_unsupported_draft_cannot_write_money(session, tenant, principal, operation):
    row = await legacy_draft(session, tenant)
    row_id = row.id
    with pytest.raises(BusinessRuleError, match="Noncash adjustments"):
        if operation == "approve":
            await service.approve_payment(session, tenant, principal, row_id)
        else:
            await service.update_payment(
                session, tenant, row_id, PaymentUpdate(notes="Unchanged method")
            )
    await session.refresh(row)
    assert row.status == PaymentStatus.DRAFT and row.approved_at is None
    assert (
        await session.scalar(
            select(func.count()).select_from(LedgerEntry).where(LedgerEntry.reference_id == row_id)
        )
        == 0
    )


async def test_legacy_draft_can_be_cancelled_without_any_posting(session, tenant):
    row = await legacy_draft(session, tenant)
    result = await service.cancel_payment(
        session, tenant, row.id, reason="Retire unsupported draft"
    )
    assert result.status == PaymentStatus.CANCELLED
    assert result.payment_method == PaymentMethod.ADJUSTMENT


async def test_legacy_draft_can_be_repaired_to_a_real_payment_method(session, tenant, principal):
    row = await legacy_draft(session, tenant)
    changed = await service.update_payment(
        session,
        tenant,
        row.id,
        PaymentUpdate(payment_method=PaymentMethod.BANK_TRANSFER),
    )
    assert changed.payment_method == PaymentMethod.BANK_TRANSFER
    approved = await service.approve_payment(session, tenant, principal, row.id)
    assert approved.status == PaymentStatus.APPROVED
    cancelled = await service.cancel_payment(
        session, tenant, row.id, reason="Reverse actual payment"
    )
    assert cancelled.status == PaymentStatus.CANCELLED


async def test_historical_adjustment_bank_posting_can_be_reversed(session, tenant, principal):
    row = await legacy_draft(session, tenant)
    await service.update_payment(
        session, tenant, row.id, PaymentUpdate(payment_method=PaymentMethod.BANK_TRANSFER)
    )
    await service.approve_payment(session, tenant, principal, row.id)
    # Reproduce the persisted shape made by the old ADJUSTMENT posting path:
    # its bank journal is identical to BANK_TRANSFER, with the unsupported label.
    row.payment_method = PaymentMethod.ADJUSTMENT
    await session.flush()
    row_id = row.id
    cancelled = await service.cancel_payment(
        session, tenant, row_id, reason="Reverse historical noncash bank posting"
    )
    assert cancelled.status == PaymentStatus.CANCELLED
    legs = list(
        (await session.scalars(select(LedgerEntry).where(LedgerEntry.reference_id == row_id))).all()
    )
    assert len(legs) == 4
    assert sum(leg.debit_amount for leg in legs) == sum(leg.credit_amount for leg in legs)
    original = [leg for leg in legs if not leg.is_reversal]
    reversed_ids = {leg.reverses_entry_id for leg in legs if leg.is_reversal}
    assert reversed_ids == {leg.id for leg in original}
