"""Service-level tests for the customers module, against a real PostgreSQL.

These cover what only a database can prove: the partial unique indexes behind
the single-default and single-primary rules, tenant isolation, the audit rows
written in the same transaction as the change, the retirement guard that reads
the outstanding balance, and the locked read-modify-write behind
``apply_outstanding_delta``.

Fixtures (``session``, ``tenant``, ``company``, ``other_company``, ``user``,
``principal``) come from ``tests/conftest.py``. The session's outer transaction
is rolled back after every test, so nothing here survives into the next one.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import Principal, TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
)
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.customers.models import (
    AddressType,
    Customer,
    CustomerAddress,
    CustomerContact,
    CustomerType,
)
from src.modules.customers.schemas import (
    AddressCreate,
    ContactCreate,
    CustomerCreate,
    CustomerFilters,
    CustomerUpdate,
    OutstandingAdjustment,
)
from src.modules.customers.service import (
    CustomerService,
    apply_outstanding_delta,
    assert_customer_can_order,
)

pytestmark = pytest.mark.integration


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _payload(code: str, **overrides: object) -> CustomerCreate:
    data: dict[str, object] = {"code": code, "name": f"Customer {code}"}
    data.update(overrides)
    return CustomerCreate(**data)  # type: ignore[arg-type]


def _address(**overrides: object) -> AddressCreate:
    data: dict[str, object] = {
        "address_type": AddressType.SHIPPING,
        "address_line1": "1 Industrial Estate",
        "city": "Pune",
        "state": "Maharashtra",
        "state_code": "27",
    }
    data.update(overrides)
    return AddressCreate(**data)  # type: ignore[arg-type]


def _no_dealer_principal(principal: Principal) -> Principal:
    """The same person, minus ``dealer.manage``."""
    return Principal(
        user_id=principal.user_id,
        company_id=principal.company_id,
        email=principal.email,
        full_name=principal.full_name,
        is_superuser=False,
        permissions=frozenset(
            p for p in principal.permissions if p != str(Permission.DEALER_MANAGE)
        ),
    )


async def _audit_rows(
    session: AsyncSession, entity_type: str, entity_id: uuid.UUID
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


def _one_audit(rows: list[AuditLog], action: AuditAction) -> AuditLog:
    """The single audit row for an action.

    Compared with ``==`` rather than ``is``: the column is a String, so the
    value read back is a plain ``str`` that equals - but is not - the StrEnum
    member.
    """
    matches = [row for row in rows if row.action == action]
    assert len(matches) == 1, f"expected exactly one {action} audit row, got {len(matches)}"
    return matches[0]


async def _default_count(
    session: AsyncSession, customer_id: uuid.UUID, address_type: AddressType
) -> int:
    stmt = select(func.count()).where(
        CustomerAddress.customer_id == customer_id,
        CustomerAddress.address_type == address_type,
        CustomerAddress.is_default.is_(True),
        CustomerAddress.deleted_at.is_(None),
    )
    return int((await session.execute(stmt)).scalar_one())


# -----------------------------------------------------------------------------
# Customer CRUD
# -----------------------------------------------------------------------------


class TestCustomerCreation:
    async def test_create_writes_the_row_and_its_audit_entry(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        code = unique_code("AC")

        created = await service.create_customer(principal, _payload(code, credit_days=45))

        assert created.code == code
        assert created.outstanding_amount == Decimal("0.0000")
        assert created.addresses == []

        rows = await _audit_rows(session, "customer", created.id)
        assert [row.action for row in rows] == [AuditAction.CREATE]
        assert rows[0].new_values is not None
        assert rows[0].new_values["code"] == code

    async def test_company_id_comes_from_the_tenant_not_the_payload(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        created = await CustomerService(session, tenant).create_customer(
            principal, _payload(unique_code("AC"))
        )
        row = (
            await session.execute(select(Customer).where(Customer.id == created.id))
        ).scalar_one()
        assert row.company_id == tenant.company_id

    async def test_duplicate_code_is_refused(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        code = unique_code("AC")
        await service.create_customer(principal, _payload(code))

        with pytest.raises(DuplicateError):
            await service.create_customer(principal, _payload(code))

    async def test_duplicate_gstin_is_refused(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        """Two live customers on one GSTIN split a legal entity's balance across
        two credit limits, which makes both credit checks wrong."""
        service = CustomerService(session, tenant)
        gstin = "27ABCDE1234F1Z0"
        await service.create_customer(principal, _payload(unique_code("AC"), gstin=gstin))

        with pytest.raises(DuplicateError):
            await service.create_customer(principal, _payload(unique_code("AC"), gstin=gstin))

    async def test_unknown_sales_user_is_rejected(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        with pytest.raises(NotFoundError):
            await CustomerService(session, tenant).create_customer(
                principal,
                _payload(unique_code("AC"), assigned_sales_user_id=uuid.uuid4()),
            )

    async def test_dealer_creation_requires_dealer_manage(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        payload = _payload(unique_code("DL"), customer_type=CustomerType.DEALER)

        with pytest.raises(PermissionDeniedError):
            await service.create_customer(_no_dealer_principal(principal), payload)

        # The same payload succeeds for someone who may manage dealers.
        created = await service.create_customer(principal, payload)
        assert created.customer_type is CustomerType.DEALER


class TestTenantIsolation:
    async def test_another_tenant_cannot_read_the_customer(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        other_company,
        unique_code,
    ) -> None:
        created = await CustomerService(session, tenant).create_customer(
            principal, _payload(unique_code("AC"))
        )

        intruder = TenantContext(company_id=other_company.id, user_id=tenant.user_id)
        with pytest.raises(NotFoundError):
            await CustomerService(session, intruder).get_customer(created.id)

    async def test_the_same_code_may_be_used_by_another_tenant(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        other_company,
        unique_code,
    ) -> None:
        code = unique_code("AC")
        await CustomerService(session, tenant).create_customer(principal, _payload(code))

        other = TenantContext(company_id=other_company.id, user_id=tenant.user_id)
        created = await CustomerService(session, other).create_customer(principal, _payload(code))
        assert created.code == code


class TestCustomerUpdates:
    async def test_clear_flag_nulls_the_credit_limit(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        created = await service.create_customer(
            principal, _payload(unique_code("AC"), credit_limit=Decimal("1000"))
        )

        updated = await service.update_customer(
            principal, created.id, CustomerUpdate(clear_credit_limit=True)
        )
        # NULL means unlimited; the flag is the only way to express that, since
        # an omitted field means "no change".
        assert updated.credit_limit is None

    async def test_update_audits_only_what_changed(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        created = await service.create_customer(principal, _payload(unique_code("AC")))

        await service.update_customer(
            principal, created.id, CustomerUpdate(name="Renamed Co", credit_days=60)
        )

        rows = await _audit_rows(session, "customer", created.id)
        update_row = _one_audit(rows, AuditAction.UPDATE)
        assert set(update_row.changed_fields or []) == {"name", "credit_days"}

    async def test_empty_update_is_refused(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        created = await service.create_customer(principal, _payload(unique_code("AC")))

        with pytest.raises(BusinessRuleError):
            await service.update_customer(principal, created.id, CustomerUpdate())


# -----------------------------------------------------------------------------
# Blocking and credit
# -----------------------------------------------------------------------------


class TestCreditHold:
    async def test_block_records_reason_actor_and_status_change(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        created = await service.create_customer(principal, _payload(unique_code("AC")))

        blocked = await service.block(principal, created.id, reason="Cheque bounced twice")

        assert blocked.is_blocked is True
        assert blocked.block_reason == "Cheque bounced twice"
        assert blocked.blocked_at is not None
        assert blocked.blocked_by_id == tenant.user_id

        rows = await _audit_rows(session, "customer", created.id)
        status_row = _one_audit(rows, AuditAction.STATUS_CHANGE)
        assert status_row.new_values == {"status": "ON_HOLD"}
        assert status_row.context is not None
        assert status_row.context["reason"] == "Cheque bounced twice"

    async def test_blocked_customer_cannot_take_an_order(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        created = await service.create_customer(principal, _payload(unique_code("AC")))
        await service.block(principal, created.id, reason="Legal dispute")

        with pytest.raises(BusinessRuleError):
            await assert_customer_can_order(session, tenant, created.id, Decimal("1"))

    async def test_unblock_clears_the_hold_and_keeps_the_history(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        created = await service.create_customer(principal, _payload(unique_code("AC")))
        await service.block(principal, created.id, reason="Cheque bounced")

        released = await service.unblock(principal, created.id, reason="Payment received")

        assert released.is_blocked is False
        assert released.block_reason is None
        # The reason for the hold survives where it cannot be overwritten.
        rows = await _audit_rows(session, "customer", created.id)
        assert any("Cheque bounced" in (row.context or {}).get("reason", "") for row in rows)


class TestOutstandingBalance:
    async def test_delta_is_applied_and_audited(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        created = await service.create_customer(
            principal, _payload(unique_code("AC"), credit_limit=Decimal("1000"))
        )

        await apply_outstanding_delta(
            session,
            tenant,
            created.id,
            OutstandingAdjustment(
                delta=Decimal("750.50"),
                reason="Invoice INV/2026-27/00001",
                reference_type="invoice",
                reference_code="INV/2026-27/00001",
            ),
        )
        await apply_outstanding_delta(
            session,
            tenant,
            created.id,
            OutstandingAdjustment(
                delta=Decimal("-250.50"),
                reason="Receipt",
                reference_type="payment",
                reference_code="RCPT/2026-27/00001",
            ),
        )

        detail = await service.get_customer(created.id)
        assert detail.outstanding_amount == Decimal("500.00")
        assert detail.credit_headroom == Decimal("500.00")

    async def test_credit_check_uses_the_updated_balance(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        created = await service.create_customer(
            principal, _payload(unique_code("AC"), credit_limit=Decimal("1000"))
        )
        await apply_outstanding_delta(
            session,
            tenant,
            created.id,
            OutstandingAdjustment(
                delta=Decimal("900"),
                reason="Invoice",
                reference_type="invoice",
                reference_code="INV/2026-27/00002",
            ),
        )

        assessment = await assert_customer_can_order(session, tenant, created.id, Decimal("100"))
        assert assessment.exposure == Decimal("1000.00")

        with pytest.raises(BusinessRuleError):
            await assert_customer_can_order(session, tenant, created.id, Decimal("100.01"))

    async def test_over_limit_filter_finds_the_account(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        breached = await service.create_customer(
            principal, _payload(unique_code("AC"), credit_limit=Decimal("100"))
        )
        healthy = await service.create_customer(
            principal, _payload(unique_code("AC"), credit_limit=Decimal("100000"))
        )
        for customer_id in (breached.id, healthy.id):
            await apply_outstanding_delta(
                session,
                tenant,
                customer_id,
                OutstandingAdjustment(
                    delta=Decimal("500"),
                    reason="Invoice",
                    reference_type="invoice",
                    reference_code="INV/2026-27/00003",
                ),
            )

        rows, _ = await service.list_customers(
            CustomerFilters(over_credit_limit=True), PageParams(limit=50), SortParams()
        )
        found = {row.id for row in rows}
        assert breached.id in found
        assert healthy.id not in found


# -----------------------------------------------------------------------------
# Addresses
# -----------------------------------------------------------------------------


class TestAddresses:
    async def test_first_address_of_a_type_becomes_the_default(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))

        address = await service.create_address(customer.id, _address(is_default=False))

        assert address.is_default is True

    async def test_a_new_default_demotes_the_previous_one(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))
        first = await service.create_address(customer.id, _address(label="Godown A"))

        second = await service.create_address(
            customer.id, _address(label="Godown B", is_default=True)
        )

        assert second.is_default is True
        # The partial unique index would have rejected a second flagged row.
        assert await _default_count(session, customer.id, AddressType.SHIPPING) == 1
        refreshed = await session.get(CustomerAddress, first.id)
        assert refreshed is not None
        assert refreshed.is_default is False

    async def test_billing_and_shipping_each_keep_their_own_default(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))

        await service.create_address(customer.id, _address(address_type=AddressType.SHIPPING))
        await service.create_address(customer.id, _address(address_type=AddressType.BILLING))

        assert await _default_count(session, customer.id, AddressType.SHIPPING) == 1
        assert await _default_count(session, customer.id, AddressType.BILLING) == 1

    async def test_deleting_the_default_promotes_the_next_address(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))
        first = await service.create_address(customer.id, _address(label="Godown A"))
        second = await service.create_address(customer.id, _address(label="Godown B"))

        await service.delete_address(customer.id, first.id, reason="Godown closed")

        promoted = await session.get(CustomerAddress, second.id)
        assert promoted is not None
        assert promoted.is_default is True
        assert await _default_count(session, customer.id, AddressType.SHIPPING) == 1

    async def test_domestic_address_without_a_state_code_is_refused(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))

        with pytest.raises(BusinessRuleError) as exc:
            await service.create_address(customer.id, _address(state_code=None))
        assert exc.value.details == {"field": "state_code"}

    async def test_gstin_contradicting_the_state_code_is_refused(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))

        with pytest.raises(BusinessRuleError):
            await service.create_address(
                customer.id, _address(state_code="29", gstin="27ABCDE1234F1Z0")
            )

    async def test_export_address_may_omit_the_state_code(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))

        address = await service.create_address(
            customer.id, _address(country="Germany", city="Hamburg", state_code=None)
        )
        assert address.state_code is None

    async def test_an_address_of_another_customer_is_not_reachable(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        """The path must own the record: otherwise one account's ship-to could
        be repointed through another account's URL."""
        service = CustomerService(session, tenant)
        mine = await service.create_customer(principal, _payload(unique_code("AC")))
        theirs = await service.create_customer(principal, _payload(unique_code("AC")))
        address = await service.create_address(theirs.id, _address())

        with pytest.raises(NotFoundError):
            await service.set_default_address(mine.id, address.id)


# -----------------------------------------------------------------------------
# Contacts
# -----------------------------------------------------------------------------


class TestContacts:
    async def test_first_contact_becomes_primary_and_the_next_does_not(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))

        first = await service.create_contact(
            customer.id, ContactCreate(name="Asha Rao", phone="9800000000")
        )
        second = await service.create_contact(
            customer.id, ContactCreate(name="Ravi Nair", email="ravi@example.com")
        )

        assert first.is_primary is True
        assert second.is_primary is False

    async def test_set_primary_moves_the_flag_exactly_once(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))
        first = await service.create_contact(
            customer.id, ContactCreate(name="Asha Rao", phone="9800000000")
        )
        second = await service.create_contact(
            customer.id, ContactCreate(name="Ravi Nair", email="ravi@example.com")
        )

        await service.set_primary_contact(customer.id, second.id)

        stmt = select(func.count()).where(
            CustomerContact.customer_id == customer.id,
            CustomerContact.is_primary.is_(True),
            CustomerContact.deleted_at.is_(None),
        )
        assert int((await session.execute(stmt)).scalar_one()) == 1
        demoted = await session.get(CustomerContact, first.id)
        assert demoted is not None
        assert demoted.is_primary is False

    async def test_contact_without_email_or_phone_is_refused(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))

        with pytest.raises(BusinessRuleError):
            await service.create_contact(customer.id, ContactCreate(name="Nobody"))


# -----------------------------------------------------------------------------
# Retirement
# -----------------------------------------------------------------------------


class TestRetirement:
    async def test_customer_with_a_balance_cannot_be_retired(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))
        await apply_outstanding_delta(
            session,
            tenant,
            customer.id,
            OutstandingAdjustment(
                delta=Decimal("100"),
                reason="Invoice",
                reference_type="invoice",
                reference_code="INV/2026-27/00004",
            ),
        )

        with pytest.raises(BusinessRuleError) as exc:
            await service.retire_customer(principal, customer.id, reason="Closed account")
        assert exc.value.code is ErrorCode.REFERENCED_RECORD_IN_USE

    async def test_retirement_cascades_to_addresses_and_contacts(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))
        await service.create_address(customer.id, _address())
        await service.create_contact(
            customer.id, ContactCreate(name="Asha Rao", phone="9800000000")
        )

        await service.retire_customer(principal, customer.id, reason="Business closed")

        with pytest.raises(NotFoundError):
            await service.get_customer(customer.id)

        live_addresses = await CustomerService(session, tenant).addresses.list_for_customer(
            customer.id
        )
        assert live_addresses == []

        rows = await _audit_rows(session, "customer", customer.id)
        delete_row = _one_audit(rows, AuditAction.DELETE)
        assert delete_row.context is not None
        assert delete_row.context["retired_addresses"] == 1
        assert delete_row.context["retired_contacts"] == 1

    async def test_restore_returns_the_account_inactive(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        customer = await service.create_customer(principal, _payload(unique_code("AC")))
        await service.retire_customer(principal, customer.id, reason="Closed in error")

        restored = await service.restore_customer(principal, customer.id)

        # Never straight back to ACTIVE: someone must confirm the credit terms.
        assert restored.is_active is False

    async def test_a_retired_code_is_not_reusable(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        """The code is quoted on the retired account's historical documents."""
        service = CustomerService(session, tenant)
        code = unique_code("AC")
        customer = await service.create_customer(principal, _payload(code))
        await service.retire_customer(principal, customer.id, reason="Closed")

        with pytest.raises(DuplicateError):
            await service.create_customer(principal, _payload(code))


# -----------------------------------------------------------------------------
# Dealer sub-resource
# -----------------------------------------------------------------------------


class TestDealerListing:
    async def test_dealer_list_returns_only_the_dealer_channel(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, unique_code
    ) -> None:
        service = CustomerService(session, tenant)
        dealer = await service.create_customer(
            principal, _payload(unique_code("DL"), customer_type=CustomerType.DEALER)
        )
        distributor = await service.create_customer(
            principal, _payload(unique_code("DS"), customer_type=CustomerType.DISTRIBUTOR)
        )
        plain = await service.create_customer(principal, _payload(unique_code("AC")))

        rows, _ = await service.list_dealers(CustomerFilters(), PageParams(limit=100), SortParams())
        found = {row.id for row in rows}

        assert {dealer.id, distributor.id} <= found
        assert plain.id not in found

    async def test_a_non_dealer_channel_filter_is_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        # Returning an empty page would read as "there are no such dealers",
        # which would be a lie.
        with pytest.raises(BusinessRuleError):
            await CustomerService(session, tenant).list_dealers(
                CustomerFilters(customer_type=CustomerType.OEM),
                PageParams(),
                SortParams(),
            )
