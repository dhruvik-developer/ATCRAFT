"""``POST /finance/customer-adjustments`` over HTTP.

The replacement for ``POST /customers/{id}/outstanding``, which moved the
cached balance and nothing else - a figure the ledger could not explain. Here
the receivable journal moves and the cached balance follows it, in one
transaction, and the customer's own detail reads the result. Three things are
settled at the boundary and tested there:

* a charge raises what the customer owes and a credit lowers it, and the
  customer detail - the figure every credit check reads - agrees with the
  ledger's answer to the rupee;
* the route is gated on ``payment.approve``, not ``payment.record``: a credit
  note is money the company agrees not to collect;
* the receivable control account is refused as the contra leg, as a field error,
  so a caller cannot post a "receivable" adjustment that the ageing report would
  never see.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.customers.models import Customer, CustomerType
from src.modules.finance import service as finance_service
from src.modules.finance.models import LedgerAccount, LedgerEntry, PartyType, SystemAccount
from src.modules.products.models import PriceType

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _money(value: object) -> Decimal:
    """JSON carries money as a string; compare by value, not by spelling."""
    return Decimal(str(value))


def _principal_holding(base: Principal, *permissions: Permission) -> Principal:
    """The same person, a superuser no longer, holding exactly these rights."""
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
async def customer(session: AsyncSession, tenant: TenantContext) -> Customer:
    record = Customer(
        company_id=tenant.company_id,
        code=f"CU-{uuid.uuid4().hex[:8].upper()}",
        name="Hardware Distributors",
        customer_type=CustomerType.CUSTOMER,
        price_list_type=PriceType.SALE,
        credit_limit=Decimal("1000"),
        credit_days=30,
        is_active=True,
        is_blocked=False,
    )
    session.add(record)
    await session.flush()
    return record


@pytest.fixture
async def accounts(session: AsyncSession, tenant: TenantContext) -> dict[str, LedgerAccount]:
    """The chart's control accounts, provisioned the way the first posting would."""
    return await finance_service.ensure_system_accounts(session, tenant)


async def _journal_legs(
    session: AsyncSession, tenant: TenantContext, reference_code: str
) -> list[LedgerEntry]:
    """The ledger rows an adjustment posted, by the code the response carries.

    Read by ``reference_type`` + ``reference_code`` - the pair the customer's
    balance history records - so this proves the balance can be reconciled to
    the ledger by exactly the key the service promises.
    """
    result = await session.execute(
        select(LedgerEntry)
        .where(
            LedgerEntry.company_id == tenant.company_id,
            LedgerEntry.reference_type == "customer_adjustment",
            LedgerEntry.reference_code == reference_code,
        )
        .order_by(LedgerEntry.line_number)
    )
    return list(result.scalars().all())


def _leg_shape(legs: list[LedgerEntry]) -> list[tuple]:
    return [
        (leg.account_id, leg.debit_amount, leg.credit_amount, leg.party_type, leg.party_id)
        for leg in legs
    ]


def _payload(customer: Customer, contra: LedgerAccount, kind: str, amount: str) -> dict:
    return {
        "customer_id": str(customer.id),
        "adjustment_type": kind,
        "amount": amount,
        "account_id": str(contra.id),
        "reason": f"{kind.title()} note raised by hand for a test",
    }


class TestPostingAnAdjustment:
    async def test_a_charge_and_a_credit_move_the_balance_the_customer_detail_reads(
        self, api_client, customer: Customer, accounts: dict[str, LedgerAccount]
    ) -> None:
        contra = accounts[str(SystemAccount.BANK)]

        charged = await api_client.post(
            "/finance/customer-adjustments", json=_payload(customer, contra, "CHARGE", "750.50")
        )
        assert charged.status_code == 201, charged.text
        charge = charged.json()["data"]
        assert charge["customer_id"] == str(customer.id)
        assert charge["adjustment_type"] == "CHARGE"
        assert charge["reference_code"]
        assert _money(charge["delta"]) == Decimal("750.50")
        assert _money(charge["outstanding_amount"]) == Decimal("750.50")

        credited = await api_client.post(
            "/finance/customer-adjustments", json=_payload(customer, contra, "CREDIT", "250.50")
        )
        assert credited.status_code == 201, credited.text
        credit = credited.json()["data"]
        assert _money(credit["delta"]) == Decimal("-250.50")
        assert _money(credit["outstanding_amount"]) == Decimal("500.00")
        assert credit["reference_code"] != charge["reference_code"]

        detail = await api_client.get(f"/customers/{customer.id}")
        assert detail.status_code == 200, detail.text
        read = detail.json()["data"]
        assert _money(read["outstanding_amount"]) == Decimal("500.00")
        assert _money(read["credit_headroom"]) == Decimal("500.00")

    async def test_the_journal_carries_the_customer_on_the_receivable_leg_only(
        self,
        api_client,
        session: AsyncSession,
        tenant: TenantContext,
        customer: Customer,
        accounts: dict[str, LedgerAccount],
    ) -> None:
        """A charge debits the receivable against the customer and credits the
        named account with no party; a credit note posts the mirror image."""
        contra = accounts[str(SystemAccount.BANK)]
        receivable = accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)]

        charged = await api_client.post(
            "/finance/customer-adjustments", json=_payload(customer, contra, "CHARGE", "150.00")
        )
        assert charged.status_code == 201, charged.text
        charge = charged.json()["data"]

        detail = await api_client.get(f"/customers/{customer.id}")
        assert detail.status_code == 200, detail.text
        assert _money(detail.json()["data"]["outstanding_amount"]) == Decimal("150.0000")

        legs = await _journal_legs(session, tenant, charge["reference_code"])
        assert len(legs) == 2
        assert {str(leg.journal_id) for leg in legs} == {charge["journal_id"]}
        assert _leg_shape(legs) == [
            (receivable.id, Decimal("150"), Decimal("0"), PartyType.CUSTOMER, customer.id),
            (contra.id, Decimal("0"), Decimal("150"), PartyType.NONE, None),
        ]

        credited = await api_client.post(
            "/finance/customer-adjustments", json=_payload(customer, contra, "CREDIT", "150.00")
        )
        assert credited.status_code == 201, credited.text
        credit = credited.json()["data"]
        assert credit["reference_code"] != charge["reference_code"]
        assert _money(credit["outstanding_amount"]) == Decimal("0")

        detail = await api_client.get(f"/customers/{customer.id}")
        assert _money(detail.json()["data"]["outstanding_amount"]) == Decimal("0")

        legs = await _journal_legs(session, tenant, credit["reference_code"])
        assert _leg_shape(legs) == [
            (receivable.id, Decimal("0"), Decimal("150"), PartyType.CUSTOMER, customer.id),
            (contra.id, Decimal("150"), Decimal("0"), PartyType.NONE, None),
        ]

    async def test_the_receivable_control_account_is_refused_as_the_contra_leg(
        self, api_client, customer: Customer, accounts: dict[str, LedgerAccount]
    ) -> None:
        receivable = accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)]

        response = await api_client.post(
            "/finance/customer-adjustments",
            json=_payload(customer, receivable, "CHARGE", "100.00"),
        )
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert "account_id" in error["details"]["fields"]

        detail = await api_client.get(f"/customers/{customer.id}")
        assert _money(detail.json()["data"]["outstanding_amount"]) == Decimal("0")


class TestAuthority:
    async def test_recording_payments_is_not_enough_to_post_a_credit_note(
        self,
        api_client,
        principal: Principal,
        customer: Customer,
        accounts: dict[str, LedgerAccount],
    ) -> None:
        from src.core.deps import get_current_principal
        from src.main import app

        app.dependency_overrides[get_current_principal] = lambda: _principal_holding(
            principal, Permission.PAYMENT_RECORD, Permission.LEDGER_VIEW, Permission.CUSTOMER_VIEW
        )
        contra = accounts[str(SystemAccount.BANK)]

        response = await api_client.post(
            "/finance/customer-adjustments", json=_payload(customer, contra, "CREDIT", "100.00")
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"

        detail = await api_client.get(f"/customers/{customer.id}")
        assert detail.status_code == 200, detail.text
        assert _money(detail.json()["data"]["outstanding_amount"]) == Decimal("0")
