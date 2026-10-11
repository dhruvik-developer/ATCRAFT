"""Requisition to purchase orders: the category-wise split, as pure rules.

Each rule here decides something a buyer can argue with: which vendor a line is
bought from and why, at what price, under which category, as part of which
order, and which of the vendor's people the order is handed to. None of them
needs a database, so each is pinned with hand-built cases.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from src.core.clock import utcnow
from src.core.errors import BusinessRuleError
from src.core.permissions import Permission
from src.modules.procurement.repository import RoleGrant, VendorUserFacts
from src.modules.procurement.service import _as_assignee_candidate
from src.modules.procurement.service_rules import (
    MAX_ORDERS_PER_CONVERSION,
    VENDOR_PO_RESPOND_PERMISSION,
    AssigneeCandidate,
    PlannedConversionLine,
    RequisitionPriceSource,
    RequisitionVendorSource,
    VendorAssigneeReason,
    assert_within_order_limit,
    category_lineage,
    choose_vendor_assignee,
    conversion_delivery_date,
    group_conversion_lines,
    lineage_from,
    may_take_order,
    preferred_category_vendor,
    resolve_line_price,
    resolve_line_vendor,
    resolve_procurement_category,
    scope_covers_category,
    single_order_category,
)

pytestmark = pytest.mark.unit

# A small category tree: HARDWARE > LOCKS > MORTISE, and HARDWARE > HANDLES.
HARDWARE = uuid.uuid4()
LOCKS = uuid.uuid4()
MORTISE = uuid.uuid4()
HANDLES = uuid.uuid4()
PACKING = uuid.uuid4()

MORTISE_PATH = f"/{HARDWARE}/{LOCKS}/{MORTISE}/"
MORTISE_LINEAGE = (MORTISE, LOCKS, HARDWARE)

VENDOR = uuid.uuid4()
OTHER_VENDOR = uuid.uuid4()
WAREHOUSE = uuid.uuid4()
OTHER_WAREHOUSE = uuid.uuid4()


# -----------------------------------------------------------------------------
# Category lineage from the materialised path
# -----------------------------------------------------------------------------


class TestCategoryLineage:
    def test_the_lineage_is_the_path_read_backwards_nearest_first(self) -> None:
        assert category_lineage(MORTISE_PATH) == MORTISE_LINEAGE

    def test_a_root_is_its_own_lineage(self) -> None:
        assert category_lineage(f"/{PACKING}/") == (PACKING,)

    def test_no_path_is_no_lineage(self) -> None:
        assert category_lineage(None) == ()
        assert category_lineage("") == ()

    def test_lineage_from_an_ancestor_keeps_what_is_above_it(self) -> None:
        assert lineage_from(MORTISE_LINEAGE, LOCKS) == (LOCKS, HARDWARE)
        assert lineage_from(MORTISE_LINEAGE, MORTISE) == MORTISE_LINEAGE

    def test_lineage_from_a_stranger_or_nothing_is_empty(self) -> None:
        assert lineage_from(MORTISE_LINEAGE, PACKING) == ()
        assert lineage_from(MORTISE_LINEAGE, None) == ()


# -----------------------------------------------------------------------------
# Procurement category (docs/assumptions.md §50)
# -----------------------------------------------------------------------------


class TestProcurementCategory:
    def test_the_items_own_category_when_the_vendor_is_qualified_for_it(self) -> None:
        assert resolve_procurement_category(MORTISE_LINEAGE, {MORTISE, LOCKS}) == MORTISE

    def test_the_nearest_qualified_ancestor_otherwise(self) -> None:
        """Engaged for Locks, the vendor's mortise-lock line is a Locks line."""
        assert resolve_procurement_category(MORTISE_LINEAGE, {LOCKS}) == LOCKS

    def test_the_nearest_ancestor_wins_over_a_more_distant_one(self) -> None:
        assert resolve_procurement_category(MORTISE_LINEAGE, {HARDWARE, LOCKS}) == LOCKS

    def test_the_items_own_category_when_the_vendor_is_qualified_for_nothing_above_it(
        self,
    ) -> None:
        assert resolve_procurement_category(MORTISE_LINEAGE, {PACKING}) == MORTISE
        assert resolve_procurement_category(MORTISE_LINEAGE, set()) == MORTISE

    def test_an_uncategorised_item_has_no_procurement_category(self) -> None:
        assert resolve_procurement_category((), {PACKING}) is None

    def test_the_category_preferred_vendor_is_the_nearest_ancestor_that_has_one(self) -> None:
        assert preferred_category_vendor(MORTISE_LINEAGE, {LOCKS: VENDOR}) == VENDOR
        assert (
            preferred_category_vendor(MORTISE_LINEAGE, {HARDWARE: OTHER_VENDOR, LOCKS: VENDOR})
            == VENDOR
        )
        assert preferred_category_vendor(MORTISE_LINEAGE, {PACKING: VENDOR}) is None
        assert preferred_category_vendor((), {PACKING: VENDOR}) is None


# -----------------------------------------------------------------------------
# Vendor resolution (§51) and price resolution (§52)
# -----------------------------------------------------------------------------


class TestVendorResolution:
    def test_the_buyers_override_wins_over_everything(self) -> None:
        override = uuid.uuid4()
        assert resolve_line_vendor(
            override=override,
            suggested=uuid.uuid4(),
            item_preferred=uuid.uuid4(),
            category_preferred=uuid.uuid4(),
        ) == (override, RequisitionVendorSource.OVERRIDE)

    def test_then_the_requesters_suggestion(self) -> None:
        suggested = uuid.uuid4()
        assert resolve_line_vendor(
            override=None,
            suggested=suggested,
            item_preferred=uuid.uuid4(),
            category_preferred=uuid.uuid4(),
        ) == (suggested, RequisitionVendorSource.SUGGESTED)

    def test_then_the_items_preferred_vendor(self) -> None:
        preferred = uuid.uuid4()
        assert resolve_line_vendor(
            override=None,
            suggested=None,
            item_preferred=preferred,
            category_preferred=uuid.uuid4(),
        ) == (preferred, RequisitionVendorSource.ITEM_PREFERRED)

    def test_then_the_categorys_preferred_vendor(self) -> None:
        assert resolve_line_vendor(
            override=None, suggested=None, item_preferred=None, category_preferred=VENDOR
        ) == (VENDOR, RequisitionVendorSource.CATEGORY_PREFERRED)

    def test_no_source_is_an_unresolved_line(self) -> None:
        assert resolve_line_vendor(
            override=None, suggested=None, item_preferred=None, category_preferred=None
        ) == (None, None)


class TestPriceResolution:
    def test_the_override_wins(self) -> None:
        assert resolve_line_price(
            override=Decimal("9"), estimated=Decimal("10"), last_purchase=Decimal("11")
        ) == (Decimal("9"), RequisitionPriceSource.OVERRIDE)

    def test_then_the_requesters_estimate(self) -> None:
        assert resolve_line_price(
            override=None, estimated=Decimal("10"), last_purchase=Decimal("11")
        ) == (Decimal("10"), RequisitionPriceSource.ESTIMATE)

    def test_then_the_last_price_paid_to_that_vendor(self) -> None:
        assert resolve_line_price(override=None, estimated=None, last_purchase=Decimal("11")) == (
            Decimal("11"),
            RequisitionPriceSource.LAST_PURCHASE,
        )

    def test_a_zero_is_a_price(self) -> None:
        """A free sample is priced at zero; that is not the same as no price."""
        assert resolve_line_price(
            override=Decimal("0"), estimated=Decimal("10"), last_purchase=None
        ) == (Decimal("0"), RequisitionPriceSource.OVERRIDE)

    def test_no_source_is_an_unresolved_line_never_the_standard_cost(self) -> None:
        # The rule takes no standard cost at all: there is nothing to fall back to.
        assert resolve_line_price(override=None, estimated=None, last_purchase=None) == (
            None,
            None,
        )


# -----------------------------------------------------------------------------
# Dates and the order's own category
# -----------------------------------------------------------------------------


class TestDeliveryDate:
    def test_an_overdue_need_is_asked_for_on_the_order_date(self) -> None:
        """§57: the header cannot promise a delivery before the order exists."""
        assert conversion_delivery_date(date(2026, 9, 1), date(2026, 10, 11)) == date(2026, 10, 11)

    def test_a_future_need_is_kept(self) -> None:
        assert conversion_delivery_date(date(2026, 11, 1), date(2026, 10, 11)) == date(2026, 11, 1)

    def test_no_need_date_stays_unknown(self) -> None:
        assert conversion_delivery_date(None, date(2026, 10, 11)) is None


class TestOrderCategory:
    def test_one_shared_category_is_the_orders(self) -> None:
        assert single_order_category([LOCKS, LOCKS]) == LOCKS

    def test_lines_in_several_categories_give_none(self) -> None:
        assert single_order_category([LOCKS, HANDLES]) is None

    def test_an_uncategorised_line_counts_as_a_category_of_its_own(self) -> None:
        assert single_order_category([LOCKS, None]) is None
        assert single_order_category([None, None]) is None


# -----------------------------------------------------------------------------
# Grouping (one order per vendor, category and warehouse)
# -----------------------------------------------------------------------------


def _planned(
    *,
    vendor_id: uuid.UUID = VENDOR,
    category_id: uuid.UUID | None = LOCKS,
    warehouse_id: uuid.UUID = WAREHOUSE,
    requisition_code: str = "PR/2026-27/00001",
    requisition_id: uuid.UUID | None = None,
    line_number: int = 1,
    priority: int = 5,
) -> PlannedConversionLine:
    return PlannedConversionLine(
        requisition_id=requisition_id or uuid.uuid5(uuid.NAMESPACE_OID, requisition_code),
        requisition_code=requisition_code,
        requisition_line_id=uuid.uuid4(),
        line_number=line_number,
        item_id=uuid.uuid4(),
        description=None,
        quantity=Decimal("10"),
        uom_id=uuid.uuid4(),
        required_date=None,
        unit_price=Decimal("5"),
        price_source=RequisitionPriceSource.ESTIMATE,
        vendor_id=vendor_id,
        vendor_source=RequisitionVendorSource.SUGGESTED,
        item_category_id=category_id,
        category_lineage=lineage_from(MORTISE_LINEAGE, category_id) if category_id else (),
        warehouse_id=warehouse_id,
        priority=priority,
    )


class TestGrouping:
    def test_three_categories_make_three_orders(self) -> None:
        """The proposal's own case: lock bodies, handles and boxes split three ways."""
        groups = group_conversion_lines(
            [
                _planned(category_id=LOCKS, line_number=1),
                _planned(category_id=HANDLES, line_number=2),
                _planned(vendor_id=OTHER_VENDOR, category_id=PACKING, line_number=3),
            ]
        )
        assert len(groups) == 3
        assert {(g.vendor_id, g.item_category_id) for g in groups} == {
            (VENDOR, LOCKS),
            (VENDOR, HANDLES),
            (OTHER_VENDOR, PACKING),
        }

    def test_the_same_vendor_and_category_in_two_warehouses_is_two_orders(self) -> None:
        groups = group_conversion_lines(
            [_planned(warehouse_id=WAREHOUSE), _planned(warehouse_id=OTHER_WAREHOUSE)]
        )
        assert len(groups) == 2

    def test_groups_are_sorted_by_vendor_then_category_then_warehouse(self) -> None:
        lines = [
            _planned(vendor_id=v, category_id=c, warehouse_id=w)
            for v in (VENDOR, OTHER_VENDOR)
            for c in (LOCKS, HANDLES, None)
            for w in (WAREHOUSE, OTHER_WAREHOUSE)
        ]
        keys = [g.sort_key for g in group_conversion_lines(list(reversed(lines)))]
        assert keys == sorted(keys)
        assert keys[0][0] == min(str(VENDOR), str(OTHER_VENDOR))

    def test_a_category_less_group_sorts_as_an_empty_string(self) -> None:
        groups = group_conversion_lines([_planned(category_id=None), _planned(category_id=LOCKS)])
        assert groups[0].item_category_id is None

    def test_lines_inside_a_group_follow_requisition_then_line_number(self) -> None:
        group = group_conversion_lines(
            [
                _planned(requisition_code="PR-2", line_number=1),
                _planned(requisition_code="PR-1", line_number=2),
                _planned(requisition_code="PR-1", line_number=1),
            ]
        )[0]
        assert [(line.requisition_code, line.line_number) for line in group.lines] == [
            ("PR-1", 1),
            ("PR-1", 2),
            ("PR-2", 1),
        ]

    def test_a_group_from_one_requisition_names_it_as_the_source(self) -> None:
        requisition = uuid.uuid4()
        group = group_conversion_lines(
            [
                _planned(requisition_id=requisition, line_number=1),
                _planned(requisition_id=requisition, line_number=2),
            ]
        )[0]
        assert group.source_requisition_id == requisition

    def test_a_group_pooling_two_requisitions_names_no_source(self) -> None:
        group = group_conversion_lines(
            [_planned(requisition_code="PR-1"), _planned(requisition_code="PR-2")]
        )[0]
        assert group.source_requisition_id is None
        assert len(group.requisition_ids) == 2

    def test_the_group_carries_its_categorys_lineage(self) -> None:
        group = group_conversion_lines([_planned(category_id=LOCKS)])[0]
        assert group.category_lineage == (LOCKS, HARDWARE)

    def test_the_order_limit_is_a_named_bound(self) -> None:
        assert MAX_ORDERS_PER_CONVERSION == 20
        assert_within_order_limit(MAX_ORDERS_PER_CONVERSION)
        with pytest.raises(BusinessRuleError) as refused:
            assert_within_order_limit(MAX_ORDERS_PER_CONVERSION + 1)
        assert refused.value.details["order_count"] == MAX_ORDERS_PER_CONVERSION + 1
        assert refused.value.details["max_orders_per_conversion"] == MAX_ORDERS_PER_CONVERSION


# -----------------------------------------------------------------------------
# Who the order goes to (§55)
# -----------------------------------------------------------------------------


def _user(
    *scope: uuid.UUID,
    vendor_id: uuid.UUID | None = VENDOR,
    is_active: bool = True,
    is_deleted: bool = False,
    can_respond: bool = True,
) -> AssigneeCandidate:
    return AssigneeCandidate(
        user_id=uuid.uuid4(),
        vendor_id=vendor_id,
        is_active=is_active,
        is_deleted=is_deleted,
        can_respond=can_respond,
        allowed_category_ids=tuple(scope),
    )


def _choose(users: list[AssigneeCandidate], lineage: tuple[uuid.UUID, ...] = (LOCKS, HARDWARE)):
    return choose_vendor_assignee(users, vendor_id=VENDOR, category_lineage=lineage)


class TestAssigneeChoice:
    def test_the_one_manager_scoped_to_the_category_is_assigned(self) -> None:
        lock_manager = _user(LOCKS)
        chosen = _choose([lock_manager, _user(HANDLES), _user()])
        assert chosen == (lock_manager.user_id, VendorAssigneeReason.CATEGORY_MATCH)

    def test_a_scope_on_an_ancestor_covers_the_category(self) -> None:
        """Subtree semantics: scoped to Hardware covers a Locks order."""
        hardware_manager = _user(HARDWARE)
        assert _choose([hardware_manager]) == (
            hardware_manager.user_id,
            VendorAssigneeReason.CATEGORY_MATCH,
        )

    def test_a_scope_on_a_descendant_does_not_cover_the_parent(self) -> None:
        mortise_only = _user(MORTISE)
        assert _choose([mortise_only]) == (None, VendorAssigneeReason.NO_ELIGIBLE_USER)

    def test_two_managers_for_the_category_leave_it_unassigned(self) -> None:
        assert _choose([_user(LOCKS), _user(LOCKS)]) == (None, VendorAssigneeReason.AMBIGUOUS)

    def test_a_scoped_match_wins_over_an_unrestricted_user(self) -> None:
        lock_manager = _user(LOCKS)
        assert _choose([_user(), lock_manager])[0] == lock_manager.user_id

    def test_with_no_scoped_match_the_only_unrestricted_user_is_assigned(self) -> None:
        owner = _user()
        assert _choose([owner, _user(HANDLES)]) == (
            owner.user_id,
            VendorAssigneeReason.UNRESTRICTED,
        )

    def test_several_unrestricted_users_leave_it_unassigned(self) -> None:
        assert _choose([_user(), _user()]) == (None, VendorAssigneeReason.AMBIGUOUS)

    def test_nobody_eligible_is_unassigned(self) -> None:
        assert _choose([]) == (None, VendorAssigneeReason.NO_ELIGIBLE_USER)

    def test_a_category_less_order_goes_only_to_an_unrestricted_user(self) -> None:
        owner = _user()
        assert _choose([_user(LOCKS), owner], lineage=()) == (
            owner.user_id,
            VendorAssigneeReason.UNRESTRICTED,
        )
        assert _choose([_user(LOCKS)], lineage=()) == (None, VendorAssigneeReason.NO_ELIGIBLE_USER)

    @pytest.mark.parametrize(
        "ineligible",
        [
            {"vendor_id": OTHER_VENDOR},
            {"vendor_id": None},
            {"is_active": False},
            {"is_deleted": True},
            {"can_respond": False},
        ],
    )
    def test_an_ineligible_user_is_never_chosen_or_counted(self, ineligible: dict) -> None:
        lock_manager = _user(LOCKS)
        stranger = _user(LOCKS, **ineligible)
        # Had the stranger counted, two scoped matches would be AMBIGUOUS.
        assert _choose([lock_manager, stranger]) == (
            lock_manager.user_id,
            VendorAssigneeReason.CATEGORY_MATCH,
        )
        assert not may_take_order(stranger, vendor_id=VENDOR, category_lineage=(LOCKS,))

    def test_may_take_order_is_what_send_accepts(self) -> None:
        assert may_take_order(_user(), vendor_id=VENDOR, category_lineage=())
        assert may_take_order(_user(HARDWARE), vendor_id=VENDOR, category_lineage=(LOCKS, HARDWARE))
        assert not may_take_order(_user(HANDLES), vendor_id=VENDOR, category_lineage=(LOCKS,))

    def test_an_empty_scope_is_not_a_scoped_match(self) -> None:
        assert not scope_covers_category((), (LOCKS,))


class TestTheRespondRight:
    """Eligibility folds roles, grants and denials exactly as the request path does."""

    def _facts(self, *roles: RoleGrant, denied: tuple[str, ...] = (), superuser: bool = False):
        return VendorUserFacts(
            id=uuid.uuid4(),
            full_name="Vendor Manager",
            email="manager@vendor.example",
            vendor_id=VENDOR,
            is_active=True,
            is_superuser=superuser,
            allowed_category_ids=(),
            extra_permissions=(),
            denied_permissions=denied,
            roles=roles,
        )

    def _role(self, *permissions: str, active: bool = True) -> RoleGrant:
        return RoleGrant(
            code="VENDOR_MANAGER", permissions=permissions, is_active=active, deleted_at=None
        )

    def test_the_right_is_purchase_order_update_for_now(self) -> None:
        assert VENDOR_PO_RESPOND_PERMISSION is Permission.PURCHASE_ORDER_UPDATE

    def test_a_role_granting_the_right_makes_the_user_eligible(self) -> None:
        facts = self._facts(self._role(str(VENDOR_PO_RESPOND_PERMISSION)))
        assert _as_assignee_candidate(facts).can_respond

    def test_a_denial_beats_the_role(self) -> None:
        facts = self._facts(
            self._role(str(VENDOR_PO_RESPOND_PERMISSION)),
            denied=(str(VENDOR_PO_RESPOND_PERMISSION),),
        )
        assert not _as_assignee_candidate(facts).can_respond

    def test_a_suspended_role_grants_nothing(self) -> None:
        facts = self._facts(self._role(str(VENDOR_PO_RESPOND_PERMISSION), active=False))
        assert not _as_assignee_candidate(facts).can_respond

    def test_a_retired_role_grants_nothing(self) -> None:
        retired = RoleGrant(
            code="OLD",
            permissions=(str(VENDOR_PO_RESPOND_PERMISSION),),
            is_active=True,
            deleted_at=utcnow(),
        )
        assert not _as_assignee_candidate(self._facts(retired)).can_respond

    def test_a_view_only_role_is_not_enough(self) -> None:
        facts = self._facts(self._role(str(Permission.PURCHASE_ORDER_VIEW)))
        assert not _as_assignee_candidate(facts).can_respond
