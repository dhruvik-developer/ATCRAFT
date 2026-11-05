"""Approval-threshold persistence against a real PostgreSQL.

The table is sparse: no row and a NULL column both mean "use the code default".
The interesting cases are therefore the ones an in-memory fake would not show -
the first save creating the row, an explicit null withdrawing an override, the
audit row carrying both sides of the change, and two administrators saving for
the first time at the same moment.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.deps import TenantContext
from src.modules.audit.models import AuditAction, AuditLog, CompanyApprovalSettings
from src.modules.audit.schemas import ApprovalSettingsUpdate
from src.modules.audit.service import (
    DEFAULT_PO_DUAL_APPROVAL_AMOUNT,
    DEFAULT_REQUISITION_AUTO_APPROVE_LIMIT,
    get_company_approval_settings,
    update_company_approval_settings,
)
from src.modules.organization.models import Company
from src.modules.users.models import User

pytestmark = pytest.mark.integration


def _payload(**values: str | None) -> ApprovalSettingsUpdate:
    return ApprovalSettingsUpdate.model_validate(values)


async def _config_rows(session: AsyncSession, company_id: uuid.UUID) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.company_id == company_id,
                    AuditLog.entity_type == "company_approval_settings",
                )
            )
        ).all()
    )


async def test_the_first_save_stores_only_the_overrides_and_audits_the_change(
    session: AsyncSession, company: Company, tenant: TenantContext
) -> None:
    before = await get_company_approval_settings(session, tenant)
    assert before.is_default is True
    assert before.po_dual_approval_amount == DEFAULT_PO_DUAL_APPROVAL_AMOUNT
    assert before.requisition_auto_approve_limit == DEFAULT_REQUISITION_AUTO_APPROVE_LIMIT

    saved = await update_company_approval_settings(
        session, tenant, _payload(requisition_auto_approve_limit="0.0001")
    )

    assert saved.is_default is False
    assert saved.requisition_auto_approve_limit == Decimal("0.0001")
    assert saved.po_dual_approval_amount == DEFAULT_PO_DUAL_APPROVAL_AMOUNT

    row = await session.scalar(
        select(CompanyApprovalSettings).where(CompanyApprovalSettings.company_id == company.id)
    )
    assert row is not None
    assert row.po_dual_approval_amount is None
    assert row.requisition_auto_approve_limit == Decimal("0.0001")

    entries = await _config_rows(session, company.id)
    assert len(entries) == 1
    assert entries[0].action == AuditAction.CONFIG_CHANGE.value
    assert entries[0].old_values == {"requisition_auto_approve_limit": None}
    assert entries[0].new_values == {"requisition_auto_approve_limit": "0.0001"}


async def test_precision_is_stored_and_read_back_without_going_through_a_float(
    session: AsyncSession, company: Company, tenant: TenantContext
) -> None:
    await update_company_approval_settings(
        session,
        tenant,
        _payload(
            po_dual_approval_amount="1234567890.1234",
            adjustment_approval_threshold="0.0001",
        ),
    )

    stored = await session.scalar(
        select(CompanyApprovalSettings).where(CompanyApprovalSettings.company_id == company.id)
    )
    assert stored is not None
    assert stored.po_dual_approval_amount == Decimal("1234567890.1234")
    assert stored.adjustment_approval_threshold == Decimal("0.0001")

    read = await get_company_approval_settings(session, tenant)
    assert read.po_dual_approval_amount == Decimal("1234567890.1234")
    assert read.adjustment_approval_threshold == Decimal("0.0001")


async def test_restoring_defaults_on_a_company_without_an_override_writes_nothing(
    session: AsyncSession, company: Company, tenant: TenantContext
) -> None:
    restored = await update_company_approval_settings(
        session,
        tenant,
        _payload(
            po_dual_approval_amount=None,
            adjustment_approval_threshold=None,
            internal_issue_approval_threshold=None,
            requisition_auto_approve_limit=None,
        ),
    )

    assert restored.is_default is True
    count = await session.scalar(
        select(func.count())
        .select_from(CompanyApprovalSettings)
        .where(CompanyApprovalSettings.company_id == company.id)
    )
    assert count == 0
    assert await _config_rows(session, company.id) == []


async def test_an_explicit_null_withdraws_one_stored_override(
    session: AsyncSession, company: Company, tenant: TenantContext
) -> None:
    await update_company_approval_settings(
        session, tenant, _payload(po_dual_approval_amount="100", adjustment_approval_threshold="50")
    )
    saved = await update_company_approval_settings(
        session, tenant, _payload(po_dual_approval_amount=None)
    )

    assert saved.po_dual_approval_amount == DEFAULT_PO_DUAL_APPROVAL_AMOUNT
    # The untouched override survives the sparse update.
    assert saved.adjustment_approval_threshold == Decimal("50")
    assert saved.is_default is False

    entries = await _config_rows(session, company.id)
    assert len(entries) == 2
    assert entries[-1].old_values["po_dual_approval_amount"] == "100.0000"
    assert entries[-1].new_values["po_dual_approval_amount"] is None


async def test_two_concurrent_first_saves_do_not_collide_on_the_unique_company(
    engine,
) -> None:
    """The advisory lock serialises "no row yet, insert one" for one company."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with maker() as setup:
        setup.add(
            Company(
                id=company_id,
                code=f"APPR{uuid.uuid4().hex[:6].upper()}",
                name="Concurrent Approval Co",
                country="India",
                currency="INR",
                is_active=True,
            )
        )
        setup.add(
            User(
                id=user_id,
                company_id=company_id,
                email=f"approver-{uuid.uuid4().hex[:8]}@test.local",
                full_name="Concurrent Approver",
                password_hash="x",
                is_active=True,
            )
        )
        await setup.commit()

    tenant = TenantContext(company_id=company_id, user_id=user_id)

    async def save(field: str, value: str):
        async with maker() as saving:
            return await update_company_approval_settings(
                saving, tenant, _payload(**{field: value})
            )

    first, second = await asyncio.gather(
        save("po_dual_approval_amount", "100.0001"),
        save("adjustment_approval_threshold", "200.0002"),
    )

    # Both calls succeed: the loser of the insert race sees the winner's row
    # after the advisory lock is released, rather than failing on the unique
    # company constraint.
    assert first.is_default is False
    assert second.is_default is False
    async with maker() as check:
        rows = list(
            (
                await check.scalars(
                    select(CompanyApprovalSettings).where(
                        CompanyApprovalSettings.company_id == company_id
                    )
                )
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].po_dual_approval_amount == Decimal("100.0001")
        assert rows[0].adjustment_approval_threshold == Decimal("200.0002")
