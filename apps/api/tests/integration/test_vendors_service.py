"""Service-level tests for the vendors module, against a real PostgreSQL.

These cover the behaviour only a database can prove: the tenant and
vendor-portal scoping, the partial unique indexes behind "one primary contact"
and "one preferred vendor per category", the approval workflow and its
maker-checker rule, the retirement guards that read tables owned by other
modules through a ``to_regclass`` probe, and the audit rows written in the same
transaction as each change.

Isolation strategy: every test opens a connection, starts one transaction,
creates the schema it needs *inside* that transaction, and rolls the whole thing
back at the end. PostgreSQL's DDL is transactional, so nothing - not even the
tables - survives the test, and the target database is left untouched.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.core.clock import utcnow
from src.core.config import settings
from src.core.deps import TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
)
from src.core.ids import new_id
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditLog
from src.modules.dispatch.models import Dispatch, DispatchType
from src.modules.organization.models import Branch, Company, Department, Plant
from src.modules.procurement.models import GoodsReceipt, GoodsReceiptStatus, PurchaseOrder
from src.modules.products.models import Item, ItemCategory, ItemVendorLink, UnitOfMeasure
from src.modules.quality.models import InspectionResult, InspectionStatus, QcInspection, QcStage
from src.modules.users.models import Role, User, user_roles
from src.modules.vendors.models import (
    Transporter,
    Vendor,
    VendorCategory,
    VendorContact,
    VendorContactRole,
    VendorType,
)
from src.modules.vendors.schemas import (
    TransporterCreate,
    TransporterFilters,
    VendorCategoryAssign,
    VendorCategoryFilters,
    VendorCategoryUpdate,
    VendorContactCreate,
    VendorContactFilters,
    VendorContactUpdate,
    VendorCreate,
    VendorFilters,
    VendorUpdate,
)
from src.modules.vendors.service import (
    TransporterService,
    VendorCategoryService,
    VendorContactService,
    VendorPerformanceWindow,
    VendorService,
    VendorState,
    apply_vendor_rating,
    load_performance,
    vendor_state,
)
from src.modules.warehouses.models import StorageLocation, Warehouse
from tests import seed_support
from tests.schema_support import report_database_setup_failure

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Only the tables this module needs, plus everything they reference. Creating
# the whole metadata would pull in modules that are not under test here.
_TABLES = [
    Company.__table__,
    Branch.__table__,
    Plant.__table__,
    Department.__table__,
    Role.__table__,
    Vendor.__table__,
    User.__table__,
    user_roles,
    AuditLog.__table__,
    ItemCategory.__table__,
    UnitOfMeasure.__table__,
    Warehouse.__table__,
    StorageLocation.__table__,
    Item.__table__,
    ItemVendorLink.__table__,
    VendorContact.__table__,
    VendorCategory.__table__,
    Transporter.__table__,
]

# -----------------------------------------------------------------------------
# Tables owned by other modules
# -----------------------------------------------------------------------------
#
# The retirement guards and the rating adapter read tables belonging to
# procurement, quality and dispatch. The service still probes for each with
# ``to_regclass`` rather than importing the module, because a deployment can
# carry vendors without them - and that branch is exercised here by *hiding*
# the real table (see :func:`_hide_table`), never by standing in a copy.
#
# All three modules have landed, and ``tests/integration/conftest.py`` builds
# every table before this file runs, so the rows below go into the real tables
# through the owning models. A ``CREATE TABLE`` of another module's table in a
# test is a row shape that module has not agreed to, which is why the four
# stand-ins that used to live here were removed the moment they became dead.

#: Statuses the real ``dispatches`` table will not accept without an arrival
#: date (``delivered_dispatch_has_arrival_date``): the consignment is at the far
#: end, so somebody has taken it.
_ARRIVED_DISPATCH_STATUSES = frozenset(
    {"DELIVERED", "QC_PENDING", "RECEIVED", "QC_COMPLETED", "GRN_COMPLETED"}
)

VALID_GSTIN = "27AAPFU0939F1ZV"
VALID_PAN = "AAPFU0939F"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session on a connection whose transaction is rolled back afterwards."""
    engine = create_async_engine(settings.database_url, poolclass=None)
    try:
        connection = await engine.connect()
    except Exception as exc:  # pragma: no cover - environment problem
        await engine.dispose()
        # Not a blanket skip: the shared reporter skips only when the server is
        # genuinely unreachable (and fails outright under CI), and re-raises a
        # refused CREATE, a missing extension or a model that will not create.
        report_database_setup_failure(exc)

    transaction = await connection.begin()
    await connection.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))

    db = AsyncSession(bind=connection, expire_on_commit=False, autoflush=False)
    try:
        yield db
    finally:
        await db.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


async def _seed_tenant(db: AsyncSession, *, users: int = 2) -> tuple[TenantContext, uuid.UUID]:
    """One company and two users - a maker and a checker.

    Two by default because vendor approval is maker-checker: a single-user
    fixture could only ever exercise the refusal path.
    """
    company_id = new_id()
    # Random, not derived from the id: UUIDv7 is time-ordered, so two ids minted
    # in the same millisecond share their leading characters.
    suffix = uuid.uuid4().hex[:8]

    await db.execute(
        insert(Company.__table__).values(
            id=company_id, code=f"CO{suffix}", name="Atcraft Test Manufacturing"
        )
    )
    ids = []
    for index in range(users):
        user_id = new_id()
        ids.append(user_id)
        await db.execute(
            insert(User.__table__).values(
                id=user_id,
                company_id=company_id,
                email=f"buyer{index}.{suffix}@example.com",
                full_name=f"Buyer {index}",
                password_hash="argon2-placeholder",
            )
        )
    await db.flush()
    return TenantContext(company_id=company_id, user_id=ids[0]), ids[1]


async def _seed_category(db: AsyncSession, company_id: uuid.UUID, code: str) -> uuid.UUID:
    category_id = new_id()
    await db.execute(
        insert(ItemCategory.__table__).values(
            id=category_id,
            company_id=company_id,
            code=code,
            name=code.title(),
            path=f"/{category_id}/",
            depth=0,
        )
    )
    await db.flush()
    return category_id


async def _table_exists(db: AsyncSession, name: str) -> bool:
    found = await db.execute(
        text("SELECT to_regclass(:qualified)"), {"qualified": f"public.{name}"}
    )
    return found.scalar() is not None


async def _hide_table(db: AsyncSession, name: str) -> None:
    """Make ``name`` invisible for the rest of this test's transaction.

    Several tests assert what the service does when a source table does not
    exist at all. Once the owning module has landed that branch is unreachable
    unless the table is taken away, and parking a differently-shaped copy in
    another schema would not do it: the service probes for ``public.<name>`` by
    name. Renaming is transactional like every other DDL here, so the table is
    back, with its rows, the moment the test rolls back.
    """
    if await _table_exists(db, name):
        await db.execute(text(f"ALTER TABLE public.{name} RENAME TO {name}_hidden_by_test"))


async def _seed_warehouse(db: AsyncSession, tenant: TenantContext) -> uuid.UUID:
    """A warehouse for purchase orders and receipts to be delivered into.

    Through the ORM, not an INSERT: ``country`` and ``is_active`` are NOT NULL
    with Python-side defaults, which a raw statement would leave to the column.
    """
    warehouse = Warehouse(
        company_id=tenant.company_id,
        code=f"WH{uuid.uuid4().hex[:8].upper()}",
        name="Receiving Store",
        warehouse_type="RAW_MATERIAL",
    )
    db.add(warehouse)
    await db.flush()
    return warehouse.id


async def _seed_purchase_order(
    db: AsyncSession,
    tenant: TenantContext,
    vendor_id: uuid.UUID,
    *,
    status: str,
    warehouse_id: uuid.UUID,
    expected_days: int | None = None,
) -> uuid.UUID:
    """One purchase order, in procurement's own table.

    ``expected_days`` is how many days ago the vendor promised delivery; None
    means the order promised nothing. The order is dated well before that, since
    the table refuses a promise made before the order existed.

    A Core insert against the model's table rather than a hand-written column
    list: the Python-side defaults (status, priority, currency, the money
    columns) come from the model, so a column procurement adds later cannot
    leave this helper writing a row the module would never produce.
    """
    today = utcnow().date()
    order_id = new_id()
    await db.execute(
        insert(PurchaseOrder.__table__).values(
            id=order_id,
            company_id=tenant.company_id,
            code=f"PO/{uuid.uuid4().hex[:10].upper()}",
            vendor_id=vendor_id,
            po_date=today - timedelta(days=60),
            expected_delivery_date=(
                None if expected_days is None else today - timedelta(days=expected_days)
            ),
            warehouse_id=warehouse_id,
            status=status,
        )
    )
    return order_id


async def _seed_goods_receipt(
    db: AsyncSession,
    tenant: TenantContext,
    vendor_id: uuid.UUID,
    *,
    purchase_order_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    received_days: int,
) -> None:
    """One posted delivery, ``received_days`` days ago.

    POSTED rather than DRAFT because only a posted receipt is evidence that the
    goods actually arrived - and the table insists a posted one names who
    posted it and when.
    """
    await db.execute(
        insert(GoodsReceipt.__table__).values(
            id=new_id(),
            company_id=tenant.company_id,
            code=f"GRN/{uuid.uuid4().hex[:10].upper()}",
            purchase_order_id=purchase_order_id,
            vendor_id=vendor_id,
            warehouse_id=warehouse_id,
            receipt_date=utcnow().date() - timedelta(days=received_days),
            status=GoodsReceiptStatus.POSTED,
            posted_at=utcnow(),
            posted_by_id=tenant.user_id,
        )
    )


async def _seed_qc_inspection(
    db: AsyncSession,
    tenant: TenantContext,
    vendor_id: uuid.UUID,
    *,
    warehouse_id: uuid.UUID,
    inspected: str,
    rejected: str,
) -> None:
    """One decided incoming inspection of this vendor's goods.

    Decided rather than pending: the rating aggregates a rejected quantity, and
    the table refuses to record one nobody signed for - a rejection needs a
    reason (``refusal_states_a_reason``) and a decision needs an inspector and a
    time (``decision_is_attributed``).
    """
    # Through the shared ORM builder: every ``items.can_*`` capability flag is
    # NOT NULL with a Python-side default, so a hand-written column list here
    # would go stale the moment products adds another one.
    item = await seed_support.seed_item(
        db, company_id=tenant.company_id, code=f"RM-{uuid.uuid4().hex[:8].upper()}"
    )
    inspected_quantity = Decimal(inspected)
    rejected_quantity = Decimal(rejected)
    refused = rejected_quantity > 0
    await db.execute(
        insert(QcInspection.__table__).values(
            id=new_id(),
            company_id=tenant.company_id,
            code=f"QC/{uuid.uuid4().hex[:10].upper()}",
            qc_stage=QcStage.INCOMING,
            item_id=item.id,
            warehouse_id=warehouse_id,
            vendor_id=vendor_id,
            # What procurement stamps on an inspection a goods receipt raised.
            reference_type="goods_receipt",
            inspected_quantity=inspected_quantity,
            # The whole lot was measured, so the sample is the lot - which is
            # also the only way to satisfy ``sample_within_lot`` without
            # inventing a sampling plan this test does not care about.
            sample_size=inspected_quantity,
            accepted_quantity=inspected_quantity - rejected_quantity,
            rejected_quantity=rejected_quantity,
            rejection_reason="Thread gauge out of tolerance" if refused else None,
            result=InspectionResult.REJECTED if refused else InspectionResult.ACCEPTED,
            status=InspectionStatus.REJECTED if refused else InspectionStatus.ACCEPTED,
            inspector_user_id=tenant.user_id,
            inspected_at=utcnow(),
        )
    )


async def _seed_dispatch(
    db: AsyncSession,
    tenant: TenantContext,
    transporter_id: uuid.UUID,
    *,
    status: str,
) -> None:
    """One consignment carried by ``transporter_id``.

    A stock transfer rather than the default sales consignment: a sales dispatch
    must name a customer, and customers belong to a module this file does not
    seed.
    """
    dispatch_date = utcnow().date() - timedelta(days=2)
    await db.execute(
        insert(Dispatch.__table__).values(
            id=new_id(),
            company_id=tenant.company_id,
            code=f"DSP/{uuid.uuid4().hex[:10].upper()}",
            dispatch_type=DispatchType.TRANSFER,
            warehouse_id=await _seed_warehouse(db, tenant),
            transporter_id=transporter_id,
            status=status,
            dispatch_date=dispatch_date,
            actual_delivery_date=(dispatch_date if status in _ARRIVED_DISPATCH_STATUSES else None),
        )
    )


def _payload(code: str, **overrides: object) -> VendorCreate:
    data: dict[str, object] = {
        "code": code,
        "name": f"Vendor {code}",
        "vendor_types": [VendorType.RAW_MATERIAL_SUPPLIER],
    }
    data.update(overrides)
    return VendorCreate(**data)  # type: ignore[arg-type]


def _approvable(code: str, **overrides: object) -> VendorCreate:
    """A payload carrying everything the approval gate insists on."""
    data: dict[str, object] = {
        "gstin": VALID_GSTIN,
        "pan": VALID_PAN,
        "state_code": "27",
        "bank_account_name": "Sharma Metals Pvt Ltd",
        "bank_account_number": "50100123456789",
        "bank_ifsc": "HDFC0001234",
    }
    data.update(overrides)
    return _payload(code, **data)


async def _audit_actions(db: AsyncSession, entity_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        # created_at is server-side now(), which is identical for every row
        # written in one transaction; the UUIDv7 primary key breaks the tie in
        # true insertion order.
        select(AuditLog.action)
        .where(AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return [str(a) for a in rows.scalars().all()]


# -----------------------------------------------------------------------------
# Creation and editing
# -----------------------------------------------------------------------------


class TestCreateVendor:
    async def test_stamps_tenant_and_writes_one_audit_entry(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        created = await VendorService(session, tenant).create_vendor(_payload("vend 001"))

        assert created.code == "VEND 001"
        # A new vendor is never born approved, whatever the client sent.
        assert created.is_approved is False
        assert created.rating is None

        stored = await session.get(Vendor, created.id)
        assert stored is not None
        # company_id comes from the tenant context, never from the payload.
        assert stored.company_id == tenant.company_id
        assert stored.created_by_id == tenant.user_id
        assert vendor_state(stored) is VendorState.PENDING_APPROVAL

        assert await _audit_actions(session, created.id) == ["CREATE"]

    async def test_duplicate_code_is_refused(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        await service.create_vendor(_payload("VEND-001"))

        with pytest.raises(DuplicateError):
            # normalise_code upper-cases and collapses whitespace, so this is
            # the same code to the buyer and to the unique constraint.
            await service.create_vendor(_payload("  vend-001  "))

    async def test_duplicate_gstin_is_refused(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        await service.create_vendor(_payload("VEND-001", gstin=VALID_GSTIN, state_code="27"))

        with pytest.raises(DuplicateError, match="GSTIN"):
            await service.create_vendor(_payload("VEND-002", gstin=VALID_GSTIN, state_code="27"))

    async def test_several_vendor_types_are_stored_and_queryable(
        self, session: AsyncSession
    ) -> None:
        """The point of the array: one legal entity, several roles."""
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        await service.create_vendor(
            _payload("VEND-001", vendor_types=[VendorType.JOB_WORK, VendorType.SERVICE])
        )
        await service.create_vendor(
            _payload("VEND-002", vendor_types=[VendorType.RAW_MATERIAL_SUPPLIER])
        )

        job_workers, _ = await service.list_vendors(
            VendorFilters(vendor_type=VendorType.JOB_WORK), PageParams(), SortParams()
        )
        assert [v.code for v in job_workers] == ["VEND-001"]
        assert set(job_workers[0].vendor_types) == {"JOB_WORK", "SERVICE"}

    async def test_empty_type_array_is_refused_by_the_database(self, session: AsyncSession) -> None:
        """The check constraint is the floor, independent of schema validation."""
        tenant, _ = await _seed_tenant(session)
        # Inside a SAVEPOINT so the failed flush unwinds only this statement and
        # leaves the surrounding test transaction usable.
        with pytest.raises(Exception, match="vendor_types_not_empty"):
            async with session.begin_nested():
                session.add(
                    Vendor(
                        company_id=tenant.company_id,
                        code="RAW-001",
                        name="Raw",
                        vendor_types=[],
                    )
                )
                await session.flush()


class TestUpdateVendor:
    async def test_partial_update_leaves_untouched_fields_alone(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(
            _payload("VEND-001", credit_limit=Decimal("100000.0000"), lead_time_days=14)
        )

        updated = await service.update_vendor(created.id, VendorUpdate(name="Renamed Metals"))

        assert updated.name == "Renamed Metals"
        assert updated.credit_limit == Decimal("100000.0000")
        assert updated.lead_time_days == 14

    async def test_pan_that_contradicts_the_stored_gstin_is_refused(
        self, session: AsyncSession
    ) -> None:
        """The cross-check must run against the merged record, not the payload.

        A PATCH carrying only a PAN is internally consistent on its own; only the
        combination with what is already stored is wrong.
        """
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(
            _payload("VEND-001", gstin=VALID_GSTIN, state_code="27")
        )

        with pytest.raises(Exception, match="does not match"):
            await service.update_vendor(created.id, VendorUpdate(pan="ZZZZZ1111Z"))

    async def test_bank_change_is_audited_with_the_account_number_masked(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_payload("VEND-001"))

        await service.update_vendor(
            created.id,
            VendorUpdate(bank_account_number="50100123456789", bank_account_name="Sharma Metals"),
        )

        rows = await session.execute(
            select(AuditLog.new_values).where(
                AuditLog.entity_id == created.id, AuditLog.action == "UPDATE"
            )
        )
        new_values = rows.scalars().one()
        # The change is provable, but the number itself never reaches the log.
        assert new_values["bank_account_number"] == "**********6789"
        assert "50100123456789" not in str(new_values)

    async def test_masked_number_is_what_the_api_returns(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(
            _payload("VEND-001", bank_account_number="50100123456789")
        )
        assert created.bank_account_number_masked == "**********6789"


# -----------------------------------------------------------------------------
# Approval
# -----------------------------------------------------------------------------


class TestApproval:
    async def test_a_second_person_may_approve(self, session: AsyncSession) -> None:
        tenant, checker_id = await _seed_tenant(session)
        created = await VendorService(session, tenant).create_vendor(_approvable("VEND-001"))

        checker = TenantContext(company_id=tenant.company_id, user_id=checker_id)
        approved = await VendorService(session, checker).approve_vendor(
            created.id, reason="Documents verified"
        )

        assert approved.is_approved is True
        assert approved.approved_by_id == checker_id
        assert approved.approved_at is not None
        assert "APPROVE" in await _audit_actions(session, created.id)

    async def test_the_creator_cannot_approve_their_own_vendor(self, session: AsyncSession) -> None:
        """Maker-checker: approval unlocks payments to a bank account the same
        person entered, so it must not be a solo act."""
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_approvable("VEND-001"))

        with pytest.raises(AuthorizationError) as excinfo:
            await service.approve_vendor(created.id, reason="Looks fine to me")
        assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED

        stored = await session.get(Vendor, created.id)
        assert stored is not None and stored.is_approved is False

    async def test_approval_requires_tax_and_bank_details(self, session: AsyncSession) -> None:
        tenant, checker_id = await _seed_tenant(session)
        created = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))

        checker = TenantContext(company_id=tenant.company_id, user_id=checker_id)
        with pytest.raises(BusinessRuleError) as excinfo:
            await VendorService(session, checker).approve_vendor(created.id, reason="Approve")
        missing = excinfo.value.details["missing_fields"]
        assert "bank_account_number" in missing
        assert "gstin or msme_number" in missing

    async def test_withdrawing_approval_clears_the_approver(self, session: AsyncSession) -> None:
        tenant, checker_id = await _seed_tenant(session)
        created = await VendorService(session, tenant).create_vendor(_approvable("VEND-001"))
        checker = TenantContext(company_id=tenant.company_id, user_id=checker_id)
        service = VendorService(session, checker)
        await service.approve_vendor(created.id, reason="Documents verified")

        withdrawn = await service.withdraw_approval(created.id, reason="GST registration lapsed")

        assert withdrawn.is_approved is False
        # A stale approver on an unapproved record would read as a live approval.
        assert withdrawn.approved_by_id is None
        assert withdrawn.approved_at is None

    async def test_approving_twice_is_refused_by_the_state_machine(
        self, session: AsyncSession
    ) -> None:
        tenant, checker_id = await _seed_tenant(session)
        created = await VendorService(session, tenant).create_vendor(_approvable("VEND-001"))
        checker = TenantContext(company_id=tenant.company_id, user_id=checker_id)
        service = VendorService(session, checker)
        await service.approve_vendor(created.id, reason="Documents verified")

        with pytest.raises(InvalidStateTransitionError):
            await service.approve_vendor(created.id, reason="Again")

    async def test_suspension_and_reactivation_preserve_approval(
        self, session: AsyncSession
    ) -> None:
        tenant, checker_id = await _seed_tenant(session)
        created = await VendorService(session, tenant).create_vendor(_approvable("VEND-001"))
        checker = TenantContext(company_id=tenant.company_id, user_id=checker_id)
        service = VendorService(session, checker)
        await service.approve_vendor(created.id, reason="Documents verified")

        await service.set_active(created.id, active=False, reason="Quality escalation")
        reactivated = await service.set_active(created.id, active=True, reason="Issue resolved")

        assert reactivated.is_active is True
        assert reactivated.is_approved is True

    async def test_options_exclude_unapproved_vendors_by_default(
        self, session: AsyncSession
    ) -> None:
        tenant, checker_id = await _seed_tenant(session)
        service = VendorService(session, tenant)
        approved = await service.create_vendor(_approvable("VEND-001"))
        await service.create_vendor(_payload("VEND-002"))

        checker = VendorService(
            session, TenantContext(company_id=tenant.company_id, user_id=checker_id)
        )
        await checker.approve_vendor(approved.id, reason="Documents verified")

        options = await service.list_options(None)
        assert [o.code for o in options] == ["VEND-001"]

        everything = await service.list_options(None, approved_only=False)
        assert {o.code for o in everything} == {"VEND-001", "VEND-002"}


# -----------------------------------------------------------------------------
# Retirement and restore
# -----------------------------------------------------------------------------


class TestRetirement:
    async def test_retiring_removes_contacts_and_categories(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_payload("VEND-001"))
        category_id = await _seed_category(session, tenant.company_id, "LOCK-BODY")

        await VendorContactService(session, tenant).create_contact(
            created.id,
            VendorContactCreate(
                name="Rajesh Kumar", contact_role=VendorContactRole.OWNER, email="r@example.com"
            ),
        )
        await VendorCategoryService(session, tenant).assign(
            created.id, VendorCategoryAssign(item_category_id=category_id)
        )

        await service.delete_vendor(created.id, reason="Relationship ended")

        stored = await session.get(Vendor, created.id)
        assert stored is not None and stored.deleted_at is not None
        # A qualification that outlived its vendor would keep it in sourcing.
        remaining = await session.execute(
            select(VendorCategory).where(VendorCategory.vendor_id == created.id)
        )
        assert remaining.scalars().all() == []
        live_contacts = await session.execute(
            select(VendorContact).where(
                VendorContact.vendor_id == created.id, VendorContact.deleted_at.is_(None)
            )
        )
        assert live_contacts.scalars().all() == []

    async def test_open_purchase_order_blocks_retirement(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_payload("VEND-001"))

        await _seed_purchase_order(
            session,
            tenant,
            created.id,
            status="APPROVED",
            warehouse_id=await _seed_warehouse(session, tenant),
        )

        with pytest.raises(BusinessRuleError) as excinfo:
            await service.delete_vendor(created.id, reason="Cleanup")
        assert excinfo.value.code is ErrorCode.REFERENCED_RECORD_IN_USE

    async def test_closed_purchase_order_does_not_block_retirement(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_payload("VEND-001"))

        await _seed_purchase_order(
            session,
            tenant,
            created.id,
            status="CLOSED",
            warehouse_id=await _seed_warehouse(session, tenant),
        )

        await service.delete_vendor(created.id, reason="Relationship ended")
        stored = await session.get(Vendor, created.id)
        assert stored is not None and stored.deleted_at is not None

    async def test_missing_purchase_orders_table_does_not_break_retirement(
        self, session: AsyncSession
    ) -> None:
        """The to_regclass guard: a table that is not there holds no orders."""
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_payload("VEND-001"))
        # Taken away rather than merely left empty: an empty table proves only
        # that the query found nothing, not that the guard in front of it works.
        await _hide_table(session, "purchase_orders")

        await service.delete_vendor(created.id, reason="Relationship ended")
        stored = await session.get(Vendor, created.id)
        assert stored is not None and stored.deleted_at is not None

    async def test_active_portal_user_blocks_retirement(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_payload("VEND-001"))

        await session.execute(
            insert(User.__table__).values(
                id=new_id(),
                company_id=tenant.company_id,
                email=f"portal.{uuid.uuid4().hex[:8]}@vendor.example",
                full_name="Vendor Owner",
                password_hash="argon2-placeholder",
                vendor_id=created.id,
                is_active=True,
            )
        )
        await session.flush()

        with pytest.raises(BusinessRuleError, match="portal users"):
            await service.delete_vendor(created.id, reason="Cleanup")

    async def test_restore_returns_the_vendor_suspended_and_unapproved(
        self, session: AsyncSession
    ) -> None:
        tenant, checker_id = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_approvable("VEND-001"))
        checker = VendorService(
            session, TenantContext(company_id=tenant.company_id, user_id=checker_id)
        )
        await checker.approve_vendor(created.id, reason="Documents verified")
        await service.delete_vendor(created.id, reason="Paused")

        restored = await service.restore_vendor(created.id)

        assert restored.deleted_at is None
        assert restored.is_active is False
        # Restoring must not silently re-approve: someone has to look again.
        assert restored.is_approved is False

    async def test_restoring_a_live_vendor_is_refused(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_payload("VEND-001"))

        with pytest.raises(InvalidStateTransitionError):
            await service.restore_vendor(created.id)

    async def test_a_retired_vendor_still_owns_its_code(self, session: AsyncSession) -> None:
        """The code unique constraint is not partial, and deliberately so.

        A retired vendor's code is still printed on historical purchase orders,
        so reusing it would make two different suppliers read as one in the
        archive.
        """
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_payload("VEND-001"))
        await service.delete_vendor(created.id, reason="Paused")

        with pytest.raises(DuplicateError):
            await service.create_vendor(_payload("VEND-001"))

    async def test_restore_is_refused_when_the_gstin_was_re_registered(
        self, session: AsyncSession
    ) -> None:
        """The GSTIN index *is* partial, so the number can legitimately be taken
        while a vendor is retired - and restoring must then be refused rather
        than violating it."""
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(
            _payload("VEND-001", gstin=VALID_GSTIN, state_code="27")
        )
        await service.delete_vendor(created.id, reason="Paused")

        await service.create_vendor(_payload("VEND-002", gstin=VALID_GSTIN, state_code="27"))

        with pytest.raises(DuplicateError, match="GSTIN"):
            await service.restore_vendor(created.id)

    async def test_item_vendor_link_blocks_retirement(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        created = await service.create_vendor(_payload("VEND-001"))
        category_id = await _seed_category(session, tenant.company_id, "COMPONENTS")

        uom_id = new_id()
        await session.execute(
            insert(UnitOfMeasure.__table__).values(
                id=uom_id,
                company_id=tenant.company_id,
                code="NOS",
                name="Numbers",
                uom_category="COUNT",
                is_base=True,
            )
        )
        item_id = new_id()
        await session.execute(
            insert(Item.__table__).values(
                id=item_id,
                company_id=tenant.company_id,
                code="RM-001",
                name="Brass Rod",
                item_group="RAW_MATERIAL",
                category_id=category_id,
                base_uom_id=uom_id,
            )
        )
        await session.execute(
            insert(ItemVendorLink.__table__).values(
                id=new_id(),
                company_id=tenant.company_id,
                item_id=item_id,
                vendor_id=created.id,
            )
        )
        await session.flush()

        with pytest.raises(BusinessRuleError, match="named as a source"):
            await service.delete_vendor(created.id, reason="Cleanup")


# -----------------------------------------------------------------------------
# Contacts
# -----------------------------------------------------------------------------


class TestContacts:
    async def test_promoting_a_primary_demotes_the_previous_one(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        service = VendorContactService(session, tenant)

        first = await service.create_contact(
            vendor.id,
            VendorContactCreate(
                name="Rajesh Kumar",
                contact_role=VendorContactRole.OWNER,
                email="rajesh@example.com",
                is_primary=True,
            ),
        )
        second = await service.create_contact(
            vendor.id,
            VendorContactCreate(
                name="Priya Nair",
                contact_role=VendorContactRole.ACCOUNTS,
                email="priya@example.com",
            ),
        )

        promoted = await service.set_primary(second.id)
        assert promoted.is_primary is True

        demoted = await session.get(VendorContact, first.id)
        assert demoted is not None
        await session.refresh(demoted)
        assert demoted.is_primary is False

    async def test_two_primaries_are_impossible_even_by_direct_write(
        self, session: AsyncSession
    ) -> None:
        """The partial unique index is the guarantee; the service is convenience."""
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))

        with pytest.raises(Exception, match="uq_vendor_contacts_one_primary"):
            async with session.begin_nested():
                for name in ("A Person", "B Person"):
                    session.add(
                        VendorContact(
                            company_id=tenant.company_id,
                            vendor_id=vendor.id,
                            name=name,
                            contact_role=VendorContactRole.MANAGER,
                            email=f"{name.split()[0].lower()}@example.com",
                            is_primary=True,
                        )
                    )
                await session.flush()

    async def test_the_same_person_cannot_be_added_twice_in_one_role(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        service = VendorContactService(session, tenant)
        payload = VendorContactCreate(
            name="Rajesh Kumar", contact_role=VendorContactRole.QC, phone="+912212345678"
        )
        await service.create_contact(vendor.id, payload)

        with pytest.raises(DuplicateError):
            await service.create_contact(
                vendor.id,
                VendorContactCreate(
                    name="rajesh kumar",  # same person, different capitalisation
                    contact_role=VendorContactRole.QC,
                    phone="+912212345678",
                ),
            )

    async def test_clearing_every_contact_channel_is_refused(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        service = VendorContactService(session, tenant)
        contact = await service.create_contact(
            vendor.id,
            VendorContactCreate(
                name="Rajesh Kumar", contact_role=VendorContactRole.OWNER, email="r@example.com"
            ),
        )

        with pytest.raises(Exception, match="e-mail, a phone or a mobile"):
            await service.update_contact(
                # Clearing every channel leaves a contact nobody can reach.
                contact.id,
                VendorContactUpdate(email=None, phone=None, mobile=None),
            )

    async def test_the_primary_contact_cannot_be_removed(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        service = VendorContactService(session, tenant)
        contact = await service.create_contact(
            vendor.id,
            VendorContactCreate(
                name="Rajesh Kumar",
                contact_role=VendorContactRole.OWNER,
                email="r@example.com",
                is_primary=True,
            ),
        )

        with pytest.raises(BusinessRuleError, match="primary contact"):
            await service.delete_contact(contact.id, reason="Left the company")

    async def test_contacts_list_is_scoped_to_the_vendor(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        vendors = VendorService(session, tenant)
        first = await vendors.create_vendor(_payload("VEND-001"))
        second = await vendors.create_vendor(_payload("VEND-002"))
        service = VendorContactService(session, tenant)
        for vendor_id, name in ((first.id, "Rajesh"), (second.id, "Priya")):
            await service.create_contact(
                vendor_id,
                VendorContactCreate(
                    name=name,
                    contact_role=VendorContactRole.MANAGER,
                    email=f"{name.lower()}@example.com",
                ),
            )

        items, _ = await service.list_contacts(
            VendorContactFilters(vendor_id=first.id), PageParams(), SortParams()
        )
        assert [c.name for c in items] == ["Rajesh"]


# -----------------------------------------------------------------------------
# Category qualifications
# -----------------------------------------------------------------------------


class TestCategories:
    async def test_assigning_and_listing_resolves_the_category_label(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        category_id = await _seed_category(session, tenant.company_id, "LOCK-BODY")
        service = VendorCategoryService(session, tenant)

        link = await service.assign(
            vendor.id, VendorCategoryAssign(item_category_id=category_id, is_preferred=True)
        )
        assert link.item_category_code == "LOCK-BODY"

        items, _ = await service.list_categories(
            VendorCategoryFilters(vendor_id=vendor.id), PageParams(), SortParams()
        )
        assert [c.item_category_code for c in items] == ["LOCK-BODY"]

    async def test_a_category_from_another_tenant_is_not_found(self, session: AsyncSession) -> None:
        """A foreign key only proves the row exists somewhere."""
        tenant, _ = await _seed_tenant(session)
        other_tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        foreign_category = await _seed_category(session, other_tenant.company_id, "HANDLES")

        with pytest.raises(NotFoundError):
            await VendorCategoryService(session, tenant).assign(
                vendor.id, VendorCategoryAssign(item_category_id=foreign_category)
            )

    async def test_the_same_category_cannot_be_assigned_twice(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        category_id = await _seed_category(session, tenant.company_id, "LOCK-BODY")
        service = VendorCategoryService(session, tenant)
        await service.assign(vendor.id, VendorCategoryAssign(item_category_id=category_id))

        with pytest.raises(DuplicateError):
            await service.assign(vendor.id, VendorCategoryAssign(item_category_id=category_id))

    async def test_preferring_a_vendor_demotes_the_previous_preferred_one(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        vendors = VendorService(session, tenant)
        first = await vendors.create_vendor(_payload("VEND-001"))
        second = await vendors.create_vendor(_payload("VEND-002"))
        category_id = await _seed_category(session, tenant.company_id, "LOCK-BODY")
        service = VendorCategoryService(session, tenant)

        link_one = await service.assign(
            first.id, VendorCategoryAssign(item_category_id=category_id, is_preferred=True)
        )
        link_two = await service.assign(
            second.id, VendorCategoryAssign(item_category_id=category_id, is_preferred=True)
        )

        assert link_two.is_preferred is True
        demoted = await session.get(VendorCategory, link_one.id)
        assert demoted is not None
        await session.refresh(demoted)
        assert demoted.is_preferred is False

    async def test_updating_to_preferred_demotes_the_incumbent(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        vendors = VendorService(session, tenant)
        first = await vendors.create_vendor(_payload("VEND-001"))
        second = await vendors.create_vendor(_payload("VEND-002"))
        category_id = await _seed_category(session, tenant.company_id, "LOCK-BODY")
        service = VendorCategoryService(session, tenant)
        link_one = await service.assign(
            first.id, VendorCategoryAssign(item_category_id=category_id, is_preferred=True)
        )
        link_two = await service.assign(
            second.id, VendorCategoryAssign(item_category_id=category_id)
        )

        await service.update(link_two.id, VendorCategoryUpdate(is_preferred=True))

        demoted = await session.get(VendorCategory, link_one.id)
        assert demoted is not None
        await session.refresh(demoted)
        assert demoted.is_preferred is False

    async def test_revoking_a_qualification_is_audited(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        category_id = await _seed_category(session, tenant.company_id, "LOCK-BODY")
        service = VendorCategoryService(session, tenant)
        link = await service.assign(vendor.id, VendorCategoryAssign(item_category_id=category_id))

        await service.remove(link.id, reason="No longer qualified")

        assert await session.get(VendorCategory, link.id) is None
        # The row is gone; the fact that it existed and was revoked is not.
        assert await _audit_actions(session, link.id) == ["CREATE", "DELETE"]


# -----------------------------------------------------------------------------
# Vendor-portal scoping and tenant isolation
# -----------------------------------------------------------------------------


class TestScoping:
    async def test_another_tenants_vendor_is_invisible(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        other_tenant, _ = await _seed_tenant(session)
        theirs = await VendorService(session, other_tenant).create_vendor(_payload("VEND-001"))

        with pytest.raises(NotFoundError):
            await VendorService(session, tenant).get_vendor(theirs.id)

        items, _ = await VendorService(session, tenant).list_vendors(
            VendorFilters(), PageParams(), SortParams()
        )
        assert items == []

    async def test_a_vendor_principal_sees_only_its_own_record(self, session: AsyncSession) -> None:
        """Section 16: a vendor user is scoped to its vendor, server-side."""
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        mine = await service.create_vendor(_payload("VEND-001"))
        theirs = await service.create_vendor(_payload("VEND-002"))

        portal = TenantContext(
            company_id=tenant.company_id, user_id=tenant.user_id, vendor_id=mine.id
        )
        portal_service = VendorService(session, portal)

        items, _ = await portal_service.list_vendors(VendorFilters(), PageParams(), SortParams())
        assert [v.code for v in items] == ["VEND-001"]

        with pytest.raises(NotFoundError):
            await portal_service.get_vendor(theirs.id)

    async def test_a_category_restricted_vendor_user_sees_only_those_categories(
        self, session: AsyncSession
    ) -> None:
        """The "Manager 1 -> Lock Body, Manager 2 -> Handles" restriction."""
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        lock_body = await _seed_category(session, tenant.company_id, "LOCK-BODY")
        handles = await _seed_category(session, tenant.company_id, "HANDLES")
        service = VendorCategoryService(session, tenant)
        await service.assign(vendor.id, VendorCategoryAssign(item_category_id=lock_body))
        await service.assign(vendor.id, VendorCategoryAssign(item_category_id=handles))

        restricted = TenantContext(
            company_id=tenant.company_id,
            user_id=tenant.user_id,
            vendor_id=vendor.id,
            allowed_category_ids=(lock_body,),
        )
        items, _ = await VendorCategoryService(session, restricted).list_categories(
            VendorCategoryFilters(), PageParams(), SortParams()
        )
        assert [c.item_category_code for c in items] == ["LOCK-BODY"]


# -----------------------------------------------------------------------------
# Rating adapter
# -----------------------------------------------------------------------------


class TestRatingAdapter:
    async def test_missing_source_tables_degrade_to_insufficient_data(
        self, session: AsyncSession
    ) -> None:
        """The to_regclass guard is the whole point: no crash before procurement
        exists, just an honest "cannot rate this yet"."""
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        # Procurement has landed since this was written, so the sources have to
        # be taken away for the "not built yet" branch to be reachable at all.
        for source in ("goods_receipts", "purchase_orders", "qc_inspections"):
            await _hide_table(session, source)

        assert await load_performance(session, tenant, vendor.id) is None

        preview = await VendorService(session, tenant).preview_rating(vendor.id)
        assert preview.rating is None
        assert preview.is_sufficient is False
        assert preview.applied is False

    async def test_reads_receipts_and_inspections_when_they_exist(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))

        warehouse_id = await _seed_warehouse(session, tenant)
        # Six deliveries against a promised date, five of them on time. The
        # seventh was ordered without one and must be counted in neither figure:
        # a delivery nobody promised cannot be late. The promise is recorded on
        # the order, so each receipt is given one of its own.
        on_time_offsets = [(9, 7), (8, 6), (7, 5), (6, 4), (5, 3)]
        late_offsets = [(3, 6)]  # received 3 days ago, was due 6 days ago
        for received_days, expected_days in [*on_time_offsets, *late_offsets, (0, None)]:
            order_id = await _seed_purchase_order(
                session,
                tenant,
                vendor.id,
                status="GRN_COMPLETED",
                warehouse_id=warehouse_id,
                expected_days=expected_days,
            )
            await _seed_goods_receipt(
                session,
                tenant,
                vendor.id,
                purchase_order_id=order_id,
                warehouse_id=warehouse_id,
                received_days=received_days,
            )

        await _seed_qc_inspection(
            session,
            tenant,
            vendor.id,
            warehouse_id=warehouse_id,
            inspected="1000",
            rejected="50",
        )

        window = await load_performance(session, tenant, vendor.id)
        assert window is not None
        # The receipt with no promised date is excluded from both counts.
        assert window.receipt_count == 6
        assert window.on_time_receipt_count == 5
        assert window.inspected_quantity == Decimal("1000.0000")
        assert window.rejected_quantity == Decimal("50.0000")

        rating = await VendorService(session, tenant).recompute_rating(vendor.id)
        assert rating.applied is True
        stored = await session.get(Vendor, vendor.id)
        assert stored is not None
        await session.refresh(stored)
        assert stored.rating == rating.rating
        assert stored.rating_sample_size == 6
        assert stored.rating_updated_at is not None

    async def test_a_schema_mismatch_degrades_rather_than_raising(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))
        # The table exists but does not carry the columns the adapter reads.
        # The real one is parked out of the way first, so this deliberately
        # wrong shape stands in for it instead of colliding with it.
        await _hide_table(session, "goods_receipts")
        await session.execute(text("CREATE TABLE goods_receipts (id uuid PRIMARY KEY, note text)"))

        assert await load_performance(session, tenant, vendor.id) is None

    async def test_an_insufficient_window_leaves_a_stored_rating_alone(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        vendor = await VendorService(session, tenant).create_vendor(_payload("VEND-001"))

        good = VendorPerformanceWindow(
            vendor_id=vendor.id,
            window_days=180,
            receipt_count=10,
            on_time_receipt_count=10,
            inspected_quantity=Decimal("100"),
            rejected_quantity=Decimal("0"),
        )
        await apply_vendor_rating(session, tenant, window=good)
        stored = await session.get(Vendor, vendor.id)
        assert stored is not None
        await session.refresh(stored)
        assert stored.rating == Decimal("5.00")

        thin = VendorPerformanceWindow(
            vendor_id=vendor.id,
            window_days=30,
            receipt_count=1,
            on_time_receipt_count=0,
            inspected_quantity=Decimal("0"),
            rejected_quantity=Decimal("0"),
        )
        result = await apply_vendor_rating(session, tenant, window=thin)

        assert result.rating is None
        await session.refresh(stored)
        # A measured score is not thrown away because the latest window is empty.
        assert stored.rating == Decimal("5.00")

    async def test_rating_filter_excludes_unrated_vendors(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = VendorService(session, tenant)
        rated = await service.create_vendor(_payload("VEND-001"))
        await service.create_vendor(_payload("VEND-002"))

        await apply_vendor_rating(
            session,
            tenant,
            window=VendorPerformanceWindow(
                vendor_id=rated.id,
                window_days=180,
                receipt_count=10,
                on_time_receipt_count=8,
                inspected_quantity=Decimal("0"),
                rejected_quantity=Decimal("0"),
            ),
        )

        items, _ = await service.list_vendors(
            VendorFilters(min_rating=Decimal("3.00")), PageParams(), SortParams()
        )
        # The unrated vendor is absent rather than treated as a zero score.
        assert [v.code for v in items] == ["VEND-001"]


# -----------------------------------------------------------------------------
# Transporters
# -----------------------------------------------------------------------------


class TestTransporters:
    async def test_create_and_resolve_a_tracking_link(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = TransporterService(session, tenant)
        created = await service.create_transporter(
            TransporterCreate(
                code="trn 01",
                name="Blue Freight",
                transporter_type="COURIER",
                tracking_url_template="https://carrier.example/t?awb={tracking_id}",
            )
        )

        assert created.code == "TRN 01"
        link = await service.tracking_link(created.id, "AWB/2026/001")
        assert link.is_resolvable is True
        assert link.url == "https://carrier.example/t?awb=AWB%2F2026%2F001"

    async def test_transporter_without_a_template_yields_no_link(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        service = TransporterService(session, tenant)
        created = await service.create_transporter(
            TransporterCreate(code="TRN-02", name="Local Cartage", transporter_type="TRANSPORT")
        )

        link = await service.tracking_link(created.id, "MH12AB1234")
        assert link.is_resolvable is False
        assert link.url is None

    async def test_open_dispatch_blocks_retirement(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        service = TransporterService(session, tenant)
        created = await service.create_transporter(
            TransporterCreate(code="TRN-01", name="Blue Freight", transporter_type="TRANSPORT")
        )

        await _seed_dispatch(session, tenant, created.id, status="IN_TRANSIT")

        with pytest.raises(BusinessRuleError, match="undelivered"):
            await service.delete_transporter(created.id, reason="Contract ended")

    async def test_delivered_dispatch_does_not_block_retirement(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        service = TransporterService(session, tenant)
        created = await service.create_transporter(
            TransporterCreate(code="TRN-01", name="Blue Freight", transporter_type="TRANSPORT")
        )

        await _seed_dispatch(session, tenant, created.id, status="DELIVERED")

        await service.delete_transporter(created.id, reason="Contract ended")
        stored = await session.get(Transporter, created.id)
        assert stored is not None and stored.deleted_at is not None

    async def test_filters_and_isolation(self, session: AsyncSession) -> None:
        tenant, _ = await _seed_tenant(session)
        other_tenant, _ = await _seed_tenant(session)
        service = TransporterService(session, tenant)
        await service.create_transporter(
            TransporterCreate(code="TRN-01", name="Blue Freight", transporter_type="TRANSPORT")
        )
        await service.create_transporter(
            TransporterCreate(code="CUR-01", name="Swift Courier", transporter_type="COURIER")
        )
        await TransporterService(session, other_tenant).create_transporter(
            TransporterCreate(code="TRN-01", name="Their Carrier", transporter_type="TRANSPORT")
        )

        couriers, _ = await service.list_transporters(
            TransporterFilters(transporter_type="COURIER"), PageParams(), SortParams()
        )
        assert [t.code for t in couriers] == ["CUR-01"]

        mine, _ = await service.list_transporters(TransporterFilters(), PageParams(), SortParams())
        assert {t.name for t in mine} == {"Blue Freight", "Swift Courier"}

    async def test_deactivate_then_retire_is_audited_as_a_status_change(
        self, session: AsyncSession
    ) -> None:
        tenant, _ = await _seed_tenant(session)
        service = TransporterService(session, tenant)
        created = await service.create_transporter(
            TransporterCreate(code="TRN-01", name="Blue Freight", transporter_type="TRANSPORT")
        )

        await service.set_active(created.id, active=False, reason="Rates under review")
        await service.delete_transporter(created.id, reason="Contract ended")

        assert await _audit_actions(session, created.id) == ["CREATE", "STATUS_CHANGE", "DELETE"]
