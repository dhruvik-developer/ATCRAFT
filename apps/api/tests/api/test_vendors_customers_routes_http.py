"""Vendors, transporters and customers over HTTP.

The service rules - maker-checker approval, preferred-category demotion, the
single-default address rule, credit-hold state - are pinned at service level.
These drive the twenty-five write routes no backend test had reached through
HTTP: the status code and envelope each answers with, the row it leaves behind
and the audit entry written with it, the 422 a payload or a record's state can
earn, and the 404 another company's record answers with - never a 403, which
would confirm that it exists.

The caller is the ``api_client`` principal - every permission, not a superuser
- except where maker-checker needs a second actor. Prerequisites are built
through the vendors and customers services, so every fixture row has the shape
production gives it. Ids are held as plain values: an expected failure rolls
its savepoint back and expires what was loaded before it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.customers.models import (
    AddressType,
    Customer,
    CustomerAddress,
    CustomerContact,
)
from src.modules.customers.schemas import (
    AddressCreate,
    ContactCreate,
    CustomerCreate,
    CustomerDetail,
)
from src.modules.customers.service import CustomerService
from src.modules.users.models import User
from src.modules.vendors.models import (
    Transporter,
    TransporterType,
    Vendor,
    VendorCategory,
    VendorContact,
    VendorContactRole,
    VendorType,
)
from src.modules.vendors.schemas import (
    TransporterCreate,
    TransporterRead,
    VendorCategoryAssign,
    VendorCategoryRead,
    VendorContactCreate,
    VendorContactRead,
    VendorCreate,
    VendorRead,
)
from src.modules.vendors.service import (
    TransporterService,
    VendorCategoryService,
    VendorContactService,
    VendorService,
)
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]

#: A real GSTIN shape with a valid check digit, and the same with a wrong one.
GSTIN = "27AAPFU0939F1ZV"
GSTIN_BAD_CHECK_DIGIT = "27AAPFU0939F1ZX"
PAN = "AAPFU0939F"
REASON = "Documents verified"


# -----------------------------------------------------------------------------
# Callers, builders and readers
# -----------------------------------------------------------------------------


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    """Scope for building another company's masters, through its own actor."""
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


@pytest.fixture
async def foreign_principal(
    session: AsyncSession, other_company, foreign: TenantContext
) -> Principal:
    user = await session.get(User, foreign.user_id)
    assert user is not None
    return Principal(
        user_id=user.id,
        company_id=other_company.id,
        email=user.email,
        full_name=user.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("FOREIGN_ADMIN",),
    )


def vendor_body(code: str, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "name": f"Vendor {code}",
        "vendor_types": [VendorType.RAW_MATERIAL_SUPPLIER],
    }
    body.update(fields)
    return body


def approvable_body(code: str, **fields: Any) -> dict[str, Any]:
    body = vendor_body(
        code,
        gstin=GSTIN,
        pan=PAN,
        state_code="27",
        bank_account_name="Sharma Metals Pvt Ltd",
        bank_account_number="50100123456789",
        bank_ifsc="HDFC0001234",
    )
    body.update(fields)
    return body


def customer_body(code: str, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "name": f"Customer {code}"}
    body.update(fields)
    return body


def address_body(**fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "address_type": AddressType.SHIPPING,
        "address_line1": "1 Industrial Estate",
        "city": "Pune",
        "state": "Maharashtra",
        "state_code": "27",
    }
    body.update(fields)
    return body


def transporter_body(code: str, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "name": f"Transporter {code}",
        "transporter_type": TransporterType.TRANSPORT,
    }
    body.update(fields)
    return body


async def make_vendor(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> VendorRead:
    return await VendorService(session, tenant).create_vendor(
        VendorCreate(**vendor_body(code, **fields))
    )


async def make_approvable_vendor(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> VendorRead:
    return await VendorService(session, tenant).create_vendor(
        VendorCreate(**approvable_body(code, **fields))
    )


async def make_approved_vendor(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    code: str,
) -> VendorRead:
    """Create under a second user, approve as ``principal`` - maker-checker."""
    maker = await seed_support.seed_user(session, company_id=tenant.company_id)
    maker_tenant = TenantContext(company_id=tenant.company_id, user_id=maker.id)
    created = await make_approvable_vendor(session, maker_tenant, code)
    return await VendorService(session, tenant).approve_vendor(created.id, reason=REASON)


async def make_transporter(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> TransporterRead:
    return await TransporterService(session, tenant).create_transporter(
        TransporterCreate(**transporter_body(code, **fields))
    )


async def make_vendor_contact(
    session: AsyncSession, tenant: TenantContext, vendor_id: uuid.UUID, **fields: Any
) -> VendorContactRead:
    payload: dict[str, Any] = {
        "name": "Rajesh Kumar",
        "contact_role": VendorContactRole.OWNER,
        "email": f"contact-{uuid.uuid4().hex[:8]}@vendor-qa.com",
    }
    payload.update(fields)
    return await VendorContactService(session, tenant).create_contact(
        vendor_id, VendorContactCreate(**payload)
    )


async def make_category_link(
    session: AsyncSession,
    tenant: TenantContext,
    vendor_id: uuid.UUID,
    *,
    is_preferred: bool = False,
) -> VendorCategoryRead:
    category = await seed_support.seed_category(session, company_id=tenant.company_id)
    return await VendorCategoryService(session, tenant).assign(
        vendor_id,
        VendorCategoryAssign(item_category_id=category.id, is_preferred=is_preferred),
    )


async def make_customer(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    code: str,
    **fields: Any,
) -> CustomerDetail:
    return await CustomerService(session, tenant).create_customer(
        principal, CustomerCreate(**customer_body(code, **fields))
    )


async def make_address(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    customer_id: uuid.UUID,
    **fields: Any,
):
    return await CustomerService(session, tenant).create_address(
        principal, customer_id, AddressCreate(**address_body(**fields))
    )


async def make_customer_contact(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    customer_id: uuid.UUID,
    **fields: Any,
):
    payload: dict[str, Any] = {
        "name": "Asha Rao",
        "phone": "9800000000",
    }
    payload.update(fields)
    return await CustomerService(session, tenant).create_contact(
        principal, customer_id, ContactCreate(**payload)
    )


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


# -----------------------------------------------------------------------------
# POST /vendors
# -----------------------------------------------------------------------------


class TestCreateVendor:
    async def test_a_vendor_is_created_unapproved_normalised_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("vnd").lower()

        response = await api_client.post(
            "/vendors",
            json={
                "code": f"  {raw} ",
                "name": " Sharma   Metals ",
                "vendor_types": [VendorType.RAW_MATERIAL_SUPPLIER, VendorType.SERVICE],
                "gstin": GSTIN.lower(),
                "pan": PAN.lower(),
                "state_code": "27",
                "city": " Pune ",
            },
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "Sharma Metals"
        assert data["gstin"] == GSTIN
        assert data["city"] == "Pune"
        assert data["is_approved"] is False
        assert data["rating"] is None
        assert set(data["vendor_types"]) == {
            VendorType.RAW_MATERIAL_SUPPLIER,
            VendorType.SERVICE,
        }
        vendor_id = uuid.UUID(data["id"])
        assert await column(session, Vendor.company_id, vendor_id) == tenant.company_id
        (created,) = await audit_rows(session, tenant.company_id, vendor_id, AuditAction.CREATE)
        assert created.entity_type == "vendor"
        assert created.created_by_id == tenant.user_id
        assert created.new_values["gstin"] == GSTIN
        assert ok(await api_client.get(f"/vendors/{vendor_id}"))["code"] == data["code"]

    async def test_a_code_already_in_use_is_a_conflict_after_normalisation(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_vendor(session, tenant, unique_code("VND"))

        response = await api_client.post(
            "/vendors",
            json=vendor_body(existing.code.lower(), name="Twin"),
        )

        refused(response, 409, "DUPLICATE_DOCUMENT")
        assert await count_codes(session, Vendor, tenant.company_id, existing.code) == 1

    async def test_a_gstin_with_a_wrong_check_digit_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        code = unique_code("VND")

        response = await api_client.post(
            "/vendors",
            json=vendor_body(code, gstin=GSTIN_BAD_CHECK_DIGIT, state_code="27"),
        )

        assert "gstin" in field_errors(response)
        assert await count_codes(session, Vendor, tenant.company_id, code) == 0

    async def test_vendor_types_are_required(self, api_client: AsyncClient, unique_code) -> None:
        response = await api_client.post(
            "/vendors", json={"code": unique_code("VND"), "name": "No types"}
        )

        assert "vendor_types" in field_errors(response)


# -----------------------------------------------------------------------------
# PATCH /vendors/{vendor_id}
# -----------------------------------------------------------------------------


class TestUpdateVendor:
    async def test_an_edit_applies_only_what_was_sent_and_audits_the_difference(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"), city="Pune")
        vendor_id = vendor.id

        response = await api_client.patch(
            f"/vendors/{vendor_id}",
            json={"name": "Sharma Metals (Main)", "city": " Nashik "},
        )

        data = ok(response)
        assert data["name"] == "Sharma Metals (Main)"
        assert data["city"] == "Nashik"
        assert await column(session, Vendor.city, vendor_id) == "Nashik"
        (update,) = await audit_rows(session, tenant.company_id, vendor_id, AuditAction.UPDATE)
        assert "city" in update.changed_fields
        assert "name" in update.changed_fields

    async def test_an_empty_patch_and_a_code_change_are_both_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))
        vendor_id, code = vendor.id, vendor.code

        empty = await api_client.patch(f"/vendors/{vendor_id}", json={})
        refused(empty, 422, "BUSINESS_RULE_VIOLATION")

        recoded = await api_client.patch(f"/vendors/{vendor_id}", json={"code": "RENAMED"})
        assert "code" in field_errors(recoded)
        assert await column(session, Vendor.code, vendor_id) == code

    async def test_another_companys_vendor_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_vendor(session, foreign, unique_code("VND"))
        theirs_id = theirs.id

        response = await api_client.patch(f"/vendors/{theirs_id}", json={"name": "Taken over"})

        refused(response, 404, "NOT_FOUND")
        assert await column(session, Vendor.name, theirs_id) == theirs.name


# -----------------------------------------------------------------------------
# POST /vendors/{vendor_id}/approve|withdraw-approval|activate|deactivate
# -----------------------------------------------------------------------------


class TestVendorLifecycle:
    async def test_a_second_person_can_approve_an_approvable_vendor(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        maker = await seed_support.seed_user(session, company_id=tenant.company_id)
        maker_tenant = TenantContext(company_id=tenant.company_id, user_id=maker.id)
        created = await make_approvable_vendor(session, maker_tenant, unique_code("VND"))
        vendor_id = created.id

        response = await api_client.post(f"/vendors/{vendor_id}/approve", json={"reason": REASON})

        data = ok(response)
        assert data["is_approved"] is True
        assert data["approved_by_id"] == str(principal.user_id)
        assert await column(session, Vendor.is_approved, vendor_id) is True
        (approve,) = await audit_rows(session, tenant.company_id, vendor_id, AuditAction.APPROVE)
        assert approve.created_by_id == tenant.user_id
        assert (approve.context or {}).get("reason") == REASON

    async def test_the_creator_cannot_approve_their_own_vendor(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        created = await make_approvable_vendor(session, tenant, unique_code("VND"))
        vendor_id = created.id

        response = await api_client.post(f"/vendors/{vendor_id}/approve", json={"reason": REASON})

        refused(response, 403, "APPROVAL_REQUIRED")
        assert await column(session, Vendor.is_approved, vendor_id) is False

    async def test_approving_without_bank_details_is_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        unique_code,
    ) -> None:
        maker = await seed_support.seed_user(session, company_id=tenant.company_id)
        maker_tenant = TenantContext(company_id=tenant.company_id, user_id=maker.id)
        created = await make_vendor(
            session, maker_tenant, unique_code("VND"), gstin=GSTIN, pan=PAN, state_code="27"
        )
        vendor_id = created.id

        response = await api_client.post(f"/vendors/{vendor_id}/approve", json={"reason": REASON})

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, Vendor.is_approved, vendor_id) is False

    async def test_withdraw_activate_and_deactivate_round_trip(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        vendor = await make_approved_vendor(session, tenant, principal, unique_code("VND"))
        vendor_id = vendor.id
        path = f"/vendors/{vendor_id}"

        withdrawn = ok(
            await api_client.post(f"{path}/withdraw-approval", json={"reason": "GST lapsed"})
        )
        assert withdrawn["is_approved"] is False
        assert withdrawn["approved_by_id"] is None
        assert await column(session, Vendor.is_approved, vendor_id) is False
        (reject,) = await audit_rows(session, tenant.company_id, vendor_id, AuditAction.REJECT)
        assert (reject.context or {}).get("reason") == "GST lapsed"

        suspended = ok(
            await api_client.post(f"{path}/deactivate", json={"reason": "Quality escalation"})
        )
        assert suspended["is_active"] is False
        assert await column(session, Vendor.is_active, vendor_id) is False

        reactivated = ok(
            await api_client.post(f"{path}/activate", json={"reason": "Issue resolved"})
        )
        assert reactivated["is_active"] is True
        assert reactivated["is_approved"] is False
        assert await column(session, Vendor.is_active, vendor_id) is True

        changes = await audit_rows(session, tenant.company_id, vendor_id, AuditAction.STATUS_CHANGE)
        assert len(changes) >= 2

    async def test_a_short_reason_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))

        response = await api_client.post(f"/vendors/{vendor.id}/deactivate", json={"reason": "no"})

        assert "reason" in field_errors(response)

    async def test_another_companys_lifecycle_routes_are_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_approvable_vendor(session, foreign, unique_code("VND"))
        theirs_id = theirs.id

        for suffix in ("approve", "withdraw-approval", "activate", "deactivate"):
            response = await api_client.post(
                f"/vendors/{theirs_id}/{suffix}", json={"reason": REASON}
            )
            refused(response, 404, "NOT_FOUND")

        assert await column(session, Vendor.is_approved, theirs_id) is False
        assert await column(session, Vendor.is_active, theirs_id) is True


# -----------------------------------------------------------------------------
# POST /vendors/{vendor_id}/rating
# -----------------------------------------------------------------------------


class TestRecomputeVendorRating:
    async def test_insufficient_history_answers_without_storing_a_rating(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))
        vendor_id = vendor.id

        response = await api_client.post(f"/vendors/{vendor_id}/rating", json={})

        data = ok(response)
        assert data["vendor_id"] == str(vendor_id)
        assert data["applied"] is False
        assert data["is_sufficient"] is False
        assert data["rating"] is None
        assert await column(session, Vendor.rating, vendor_id) is None

    async def test_an_out_of_range_window_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))

        response = await api_client.post(f"/vendors/{vendor.id}/rating", json={"window_days": 1})

        assert "window_days" in field_errors(response)

    async def test_another_companys_vendor_rating_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_vendor(session, foreign, unique_code("VND"))

        response = await api_client.post(f"/vendors/{theirs.id}/rating", json={})

        refused(response, 404, "NOT_FOUND")


# -----------------------------------------------------------------------------
# POST /vendors/{vendor_id}/contacts and PATCH /vendors/contacts/{contact_id}
# -----------------------------------------------------------------------------


class TestVendorContacts:
    async def test_a_contact_is_added_normalised_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))
        vendor_id = vendor.id

        response = await api_client.post(
            f"/vendors/{vendor_id}/contacts",
            json={
                "name": "  Rajesh   Kumar ",
                "contact_role": VendorContactRole.ACCOUNTS,
                "email": "accounts@vendor-qa.com",
                "is_primary": True,
            },
        )

        data = ok(response, 201)
        assert data["name"] == "Rajesh Kumar"
        assert data["vendor_id"] == str(vendor_id)
        assert data["is_primary"] is True
        contact_id = uuid.UUID(data["id"])
        assert await column(session, VendorContact.company_id, contact_id) == tenant.company_id
        (created,) = await audit_rows(session, tenant.company_id, contact_id, AuditAction.CREATE)
        assert created.entity_type == "vendor_contact"

    async def test_a_contact_needs_a_reachable_channel(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))

        response = await api_client.post(
            f"/vendors/{vendor.id}/contacts",
            json={"name": "Nobody", "contact_role": VendorContactRole.OWNER},
        )

        refused(response, 422, "VALIDATION_ERROR")

    async def test_an_edit_updates_the_contact_and_refuses_an_empty_patch(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))
        contact = await make_vendor_contact(session, tenant, vendor.id)
        contact_id = contact.id

        updated = ok(
            await api_client.patch(
                f"/vendors/contacts/{contact_id}",
                json={"designation": "Accounts head", "phone": "020-1234567"},
            )
        )
        assert updated["designation"] == "Accounts head"
        assert updated["phone"] == "020-1234567"
        (edit,) = await audit_rows(session, tenant.company_id, contact_id, AuditAction.UPDATE)
        assert "designation" in edit.changed_fields

        empty = await api_client.patch(f"/vendors/contacts/{contact_id}", json={})
        refused(empty, 422, "BUSINESS_RULE_VIOLATION")

    async def test_another_companys_contact_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs_vendor = await make_vendor(session, foreign, unique_code("VND"))
        theirs = await make_vendor_contact(session, foreign, theirs_vendor.id)
        theirs_id = theirs.id

        response = await api_client.patch(f"/vendors/contacts/{theirs_id}", json={"name": "Taken"})

        refused(response, 404, "NOT_FOUND")
        assert await column(session, VendorContact.name, theirs_id) == theirs.name


# -----------------------------------------------------------------------------
# POST /vendors/{vendor_id}/categories and PATCH /vendors/categories/{link_id}
# -----------------------------------------------------------------------------


class TestVendorCategories:
    async def test_a_category_qualification_is_assigned_and_updated(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))
        category = await seed_support.seed_category(session, company_id=tenant.company_id)
        vendor_id, category_id = vendor.id, category.id

        created = ok(
            await api_client.post(
                f"/vendors/{vendor_id}/categories",
                json={"item_category_id": str(category_id), "notes": "Preferred source"},
            ),
            201,
        )
        assert created["vendor_id"] == str(vendor_id)
        assert created["item_category_id"] == str(category_id)
        assert created["is_preferred"] is False
        link_id = uuid.UUID(created["id"])
        (create_audit,) = await audit_rows(session, tenant.company_id, link_id, AuditAction.CREATE)
        assert create_audit.entity_type == "vendor_category"

        preferred = ok(
            await api_client.patch(
                f"/vendors/categories/{link_id}",
                json={"is_preferred": True, "notes": "Sole source"},
            )
        )
        assert preferred["is_preferred"] is True
        assert preferred["notes"] == "Sole source"
        assert await column(session, VendorCategory.is_preferred, link_id) is True
        (update,) = await audit_rows(session, tenant.company_id, link_id, AuditAction.UPDATE)
        assert "is_preferred" in update.changed_fields

    async def test_a_second_qualification_for_the_same_category_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))
        link = await make_category_link(session, tenant, vendor.id)

        response = await api_client.post(
            f"/vendors/{vendor.id}/categories",
            json={"item_category_id": str(link.item_category_id)},
        )

        refused(response, 409, "DUPLICATE_DOCUMENT")

    async def test_another_companys_category_is_not_found_on_assign(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))
        theirs = await seed_support.seed_category(session, company_id=foreign.company_id)

        response = await api_client.post(
            f"/vendors/{vendor.id}/categories",
            json={"item_category_id": str(theirs.id)},
        )

        refused(response, 404, "NOT_FOUND")

    async def test_another_companys_link_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs_vendor = await make_vendor(session, foreign, unique_code("VND"))
        theirs = await make_category_link(session, foreign, theirs_vendor.id)
        theirs_id = theirs.id

        response = await api_client.patch(
            f"/vendors/categories/{theirs_id}", json={"notes": "Taken"}
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, VendorCategory.notes, theirs_id) is None

    async def test_an_empty_category_patch_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        vendor = await make_vendor(session, tenant, unique_code("VND"))
        link = await make_category_link(session, tenant, vendor.id)

        response = await api_client.patch(f"/vendors/categories/{link.id}", json={})

        refused(response, 422, "BUSINESS_RULE_VIOLATION")


# -----------------------------------------------------------------------------
# POST /transporters and PATCH /transporters/{transporter_id}
# -----------------------------------------------------------------------------


class TestCreateTransporter:
    async def test_a_transporter_is_created_normalised_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("tr").lower()

        response = await api_client.post(
            "/transporters",
            json={
                "code": f"  {raw} ",
                "name": " Blue   Dart ",
                "transporter_type": TransporterType.COURIER,
                "tracking_url_template": "https://track.example/{tracking_id}",
                "city": " Mumbai ",
            },
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "Blue Dart"
        assert data["transporter_type"] == TransporterType.COURIER
        assert data["is_active"] is True
        transporter_id = uuid.UUID(data["id"])
        assert await column(session, Transporter.company_id, transporter_id) == tenant.company_id
        (created,) = await audit_rows(
            session, tenant.company_id, transporter_id, AuditAction.CREATE
        )
        assert created.entity_type == "transporter"
        assert ok(await api_client.get(f"/transporters/{transporter_id}"))["code"] == data["code"]

    async def test_a_duplicate_code_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_transporter(session, tenant, unique_code("TR"))

        response = await api_client.post(
            "/transporters", json=transporter_body(existing.code.lower())
        )

        refused(response, 409, "DUPLICATE_DOCUMENT")

    async def test_a_tracking_template_without_the_placeholder_is_a_field_error(
        self, api_client: AsyncClient, unique_code
    ) -> None:
        response = await api_client.post(
            "/transporters",
            json=transporter_body(
                unique_code("TR"),
                tracking_url_template="https://track.example/static",
            ),
        )

        assert "tracking_url_template" in field_errors(response)


class TestUpdateTransporter:
    async def test_an_edit_applies_and_refuses_an_empty_or_code_patch(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        transporter = await make_transporter(session, tenant, unique_code("TR"), city="Pune")
        transporter_id, code = transporter.id, transporter.code

        data = ok(
            await api_client.patch(
                f"/transporters/{transporter_id}",
                json={"name": "Blue Dart Express", "city": " Nashik "},
            )
        )
        assert data["name"] == "Blue Dart Express"
        assert data["city"] == "Nashik"
        (update,) = await audit_rows(session, tenant.company_id, transporter_id, AuditAction.UPDATE)
        assert "name" in update.changed_fields

        refused(
            await api_client.patch(f"/transporters/{transporter_id}", json={}),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert "code" in field_errors(
            await api_client.patch(f"/transporters/{transporter_id}", json={"code": "MOVED"})
        )
        assert await column(session, Transporter.code, transporter_id) == code

    async def test_another_companys_transporter_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_transporter(session, foreign, unique_code("TR"))
        theirs_id = theirs.id

        response = await api_client.patch(f"/transporters/{theirs_id}", json={"name": "Taken"})

        refused(response, 404, "NOT_FOUND")
        assert await column(session, Transporter.name, theirs_id) == theirs.name


# -----------------------------------------------------------------------------
# POST /transporters/{id}/activate|deactivate
# -----------------------------------------------------------------------------


class TestTransporterLifecycle:
    async def test_deactivate_then_activate_writes_status_changes(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        transporter = await make_transporter(session, tenant, unique_code("TR"))
        transporter_id = transporter.id
        path = f"/transporters/{transporter_id}"

        off = ok(await api_client.post(f"{path}/deactivate", json={"reason": "Contract ended"}))
        assert off["is_active"] is False
        assert await column(session, Transporter.is_active, transporter_id) is False

        on = ok(await api_client.post(f"{path}/activate", json={"reason": "Contract renewed"}))
        assert on["is_active"] is True
        assert await column(session, Transporter.is_active, transporter_id) is True

        changes = await audit_rows(
            session, tenant.company_id, transporter_id, AuditAction.STATUS_CHANGE
        )
        assert [(c.old_values["status"], c.new_values["status"]) for c in changes] == [
            ("ACTIVE", "INACTIVE"),
            ("INACTIVE", "ACTIVE"),
        ]

    async def test_activating_an_already_active_transporter_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        transporter = await make_transporter(session, tenant, unique_code("TR"))

        response = await api_client.post(
            f"/transporters/{transporter.id}/activate", json={"reason": REASON}
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")

    async def test_another_companys_transporter_lifecycle_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_transporter(session, foreign, unique_code("TR"))
        theirs_id = theirs.id

        for suffix in ("activate", "deactivate"):
            refused(
                await api_client.post(
                    f"/transporters/{theirs_id}/{suffix}", json={"reason": REASON}
                ),
                404,
                "NOT_FOUND",
            )
        assert await column(session, Transporter.is_active, theirs_id) is True


# -----------------------------------------------------------------------------
# POST /customers and PATCH /customers/{customer_id}
# -----------------------------------------------------------------------------


class TestCreateCustomer:
    async def test_a_customer_is_created_normalised_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("ac").lower()

        response = await api_client.post(
            "/customers",
            json={
                "code": f"  {raw} ",
                "name": "  Apex   Hardware ",
                "credit_days": 45,
                "credit_limit": "10000.0000",
            },
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "Apex Hardware"
        assert data["credit_days"] == 45
        assert Decimal(data["credit_limit"]) == Decimal("10000.0000")
        assert Decimal(data["outstanding_amount"]) == Decimal("0.0000")
        assert data["addresses"] == []
        customer_id = uuid.UUID(data["id"])
        assert await column(session, Customer.company_id, customer_id) == tenant.company_id
        (created,) = await audit_rows(session, tenant.company_id, customer_id, AuditAction.CREATE)
        assert created.entity_type == "customer"
        assert created.created_by_id == tenant.user_id
        assert ok(await api_client.get(f"/customers/{customer_id}"))["code"] == data["code"]

    async def test_a_duplicate_code_is_a_conflict(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        existing = await make_customer(session, tenant, principal, unique_code("AC"))

        response = await api_client.post("/customers", json=customer_body(existing.code.lower()))

        refused(response, 409, "DUPLICATE_DOCUMENT")
        assert await count_codes(session, Customer, tenant.company_id, existing.code) == 1

    async def test_a_customer_needs_a_code_and_a_name(self, api_client: AsyncClient) -> None:
        response = await api_client.post("/customers", json={"credit_days": 30})

        assert {"code", "name"} <= set(field_errors(response))


class TestUpdateCustomer:
    async def test_an_edit_applies_only_what_was_sent_and_audits_the_difference(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(
            session, tenant, principal, unique_code("AC"), credit_days=15
        )
        customer_id = customer.id

        response = await api_client.patch(
            f"/customers/{customer_id}",
            json={"name": "Apex Hardware (West)", "credit_days": 30},
        )

        data = ok(response)
        assert data["name"] == "Apex Hardware (West)"
        assert data["credit_days"] == 30
        assert await column(session, Customer.credit_days, customer_id) == 30
        (update,) = await audit_rows(session, tenant.company_id, customer_id, AuditAction.UPDATE)
        assert "name" in update.changed_fields
        assert "credit_days" in update.changed_fields

    async def test_clear_credit_limit_nulls_the_stored_limit(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(
            session,
            tenant,
            principal,
            unique_code("AC"),
            credit_limit=Decimal("1000.0000"),
        )
        customer_id = customer.id

        data = ok(
            await api_client.patch(f"/customers/{customer_id}", json={"clear_credit_limit": True})
        )
        assert data["credit_limit"] is None
        assert await column(session, Customer.credit_limit, customer_id) is None

    async def test_an_empty_patch_and_a_code_change_are_both_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(session, tenant, principal, unique_code("AC"))
        customer_id, code = customer.id, customer.code

        refused(
            await api_client.patch(f"/customers/{customer_id}", json={}),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert "code" in field_errors(
            await api_client.patch(f"/customers/{customer_id}", json={"code": "MOVED"})
        )
        assert await column(session, Customer.code, customer_id) == code

    async def test_another_companys_customer_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        foreign_principal: Principal,
        unique_code,
    ) -> None:
        theirs = await make_customer(session, foreign, foreign_principal, unique_code("AC"))
        theirs_id = theirs.id

        response = await api_client.patch(f"/customers/{theirs_id}", json={"name": "Taken over"})

        refused(response, 404, "NOT_FOUND")
        assert await column(session, Customer.name, theirs_id) == theirs.name


# -----------------------------------------------------------------------------
# POST /customers/{id}/activate|deactivate|block|unblock
# -----------------------------------------------------------------------------


class TestCustomerLifecycle:
    async def test_deactivate_activate_block_and_unblock_round_trip(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(session, tenant, principal, unique_code("AC"))
        customer_id = customer.id
        path = f"/customers/{customer_id}"

        off = ok(await api_client.post(f"{path}/deactivate", json={"reason": "Seasonal close"}))
        assert off["is_active"] is False
        assert await column(session, Customer.is_active, customer_id) is False

        on = ok(await api_client.post(f"{path}/activate", json={"reason": "Season reopened"}))
        assert on["is_active"] is True

        blocked = ok(
            await api_client.post(f"{path}/block", json={"reason": "Cheque bounced twice"})
        )
        assert blocked["is_blocked"] is True
        assert blocked["block_reason"] == "Cheque bounced twice"
        assert await column(session, Customer.is_blocked, customer_id) is True

        released = ok(await api_client.post(f"{path}/unblock", json={"reason": "Payment received"}))
        assert released["is_blocked"] is False
        assert released["block_reason"] is None
        assert await column(session, Customer.is_blocked, customer_id) is False

        changes = await audit_rows(
            session, tenant.company_id, customer_id, AuditAction.STATUS_CHANGE
        )
        assert len(changes) == 4

    async def test_blocking_twice_is_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(session, tenant, principal, unique_code("AC"))
        customer_id = customer.id
        await CustomerService(session, tenant).block(principal, customer_id, reason="First hold")

        response = await api_client.post(
            f"/customers/{customer_id}/block", json={"reason": "Second hold"}
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await column(session, Customer.block_reason, customer_id) == "First hold"

    async def test_a_short_reason_is_a_field_error(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(session, tenant, principal, unique_code("AC"))

        response = await api_client.post(
            f"/customers/{customer.id}/deactivate", json={"reason": "no"}
        )

        assert "reason" in field_errors(response)

    async def test_another_companys_lifecycle_routes_are_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        foreign_principal: Principal,
        unique_code,
    ) -> None:
        theirs = await make_customer(session, foreign, foreign_principal, unique_code("AC"))
        theirs_id = theirs.id

        for suffix in ("activate", "deactivate", "block", "unblock"):
            refused(
                await api_client.post(f"/customers/{theirs_id}/{suffix}", json={"reason": REASON}),
                404,
                "NOT_FOUND",
            )
        assert await column(session, Customer.is_active, theirs_id) is True
        assert await column(session, Customer.is_blocked, theirs_id) is False


# -----------------------------------------------------------------------------
# POST|PATCH /customers/{id}/addresses/...
# -----------------------------------------------------------------------------


class TestCustomerAddresses:
    async def test_an_address_is_added_and_updated(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(session, tenant, principal, unique_code("AC"))
        customer_id = customer.id

        created = ok(
            await api_client.post(
                f"/customers/{customer_id}/addresses",
                json=address_body(label="  Godown A ", is_default=True),
            ),
            201,
        )
        assert created["label"] == "Godown A"
        assert created["is_default"] is True
        assert created["city"] == "Pune"
        address_id = uuid.UUID(created["id"])
        assert await column(session, CustomerAddress.company_id, address_id) == tenant.company_id
        (create_audit,) = await audit_rows(
            session, tenant.company_id, address_id, AuditAction.CREATE
        )
        assert create_audit.entity_type == "customer_address"

        updated = ok(
            await api_client.patch(
                f"/customers/{customer_id}/addresses/{address_id}",
                json={"city": " Nashik ", "label": "Godown West"},
            )
        )
        assert updated["city"] == "Nashik"
        assert updated["label"] == "Godown West"
        assert await column(session, CustomerAddress.city, address_id) == "Nashik"
        (edit,) = await audit_rows(session, tenant.company_id, address_id, AuditAction.UPDATE)
        assert "city" in edit.changed_fields

    async def test_a_domestic_address_without_state_code_is_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(session, tenant, principal, unique_code("AC"))

        response = await api_client.post(
            f"/customers/{customer.id}/addresses",
            json=address_body(state_code=None),
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_an_empty_address_patch_is_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(session, tenant, principal, unique_code("AC"))
        address = await make_address(session, tenant, principal, customer.id)

        response = await api_client.patch(
            f"/customers/{customer.id}/addresses/{address.id}", json={}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_another_companys_address_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        foreign_principal: Principal,
        unique_code,
    ) -> None:
        theirs_customer = await make_customer(
            session, foreign, foreign_principal, unique_code("AC")
        )
        theirs = await make_address(session, foreign, foreign_principal, theirs_customer.id)
        theirs_id = theirs.id
        customer_id = theirs_customer.id

        response = await api_client.patch(
            f"/customers/{customer_id}/addresses/{theirs_id}",
            json={"city": "Taken"},
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, CustomerAddress.city, theirs_id) == "Pune"


# -----------------------------------------------------------------------------
# POST|PATCH /customers/{id}/contacts/...
# -----------------------------------------------------------------------------


class TestCustomerContacts:
    async def test_a_contact_is_added_and_updated(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(session, tenant, principal, unique_code("AC"))
        customer_id = customer.id

        created = ok(
            await api_client.post(
                f"/customers/{customer_id}/contacts",
                json={"name": "  Asha   Rao ", "phone": "9800000000", "is_primary": True},
            ),
            201,
        )
        assert created["name"] == "Asha Rao"
        assert created["is_primary"] is True
        contact_id = uuid.UUID(created["id"])
        assert await column(session, CustomerContact.company_id, contact_id) == tenant.company_id
        (create_audit,) = await audit_rows(
            session, tenant.company_id, contact_id, AuditAction.CREATE
        )
        assert create_audit.entity_type == "customer_contact"

        updated = ok(
            await api_client.patch(
                f"/customers/{customer_id}/contacts/{contact_id}",
                json={"designation": "Buyer", "email": "asha@apex-qa.com"},
            )
        )
        assert updated["designation"] == "Buyer"
        assert updated["email"] == "asha@apex-qa.com"
        (edit,) = await audit_rows(session, tenant.company_id, contact_id, AuditAction.UPDATE)
        assert "designation" in edit.changed_fields

    async def test_a_contact_without_email_or_phone_is_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(session, tenant, principal, unique_code("AC"))

        response = await api_client.post(
            f"/customers/{customer.id}/contacts", json={"name": "Nobody"}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_an_empty_contact_patch_is_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        customer = await make_customer(session, tenant, principal, unique_code("AC"))
        contact = await make_customer_contact(session, tenant, principal, customer.id)

        response = await api_client.patch(
            f"/customers/{customer.id}/contacts/{contact.id}", json={}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_another_companys_contact_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        foreign_principal: Principal,
        unique_code,
    ) -> None:
        theirs_customer = await make_customer(
            session, foreign, foreign_principal, unique_code("AC")
        )
        theirs = await make_customer_contact(
            session, foreign, foreign_principal, theirs_customer.id
        )
        theirs_id = theirs.id
        customer_id = theirs_customer.id

        response = await api_client.patch(
            f"/customers/{customer_id}/contacts/{theirs_id}",
            json={"name": "Taken"},
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, CustomerContact.name, theirs_id) == "Asha Rao"
