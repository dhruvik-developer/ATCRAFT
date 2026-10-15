"""The cost-centre master's pure rules: lifecycle, tree depth, payloads, probes.

Everything here runs without a database. The tree walk, the retire guards and
the seam are exercised against PostgreSQL in
``tests/integration/test_cost_centres_service.py``; what is pinned here is the
part that is arithmetic or vocabulary, and one guard that is about the schema
itself: every table that carries a cost centre must be one the retire guard
probes, so a carrier added later cannot be forgotten silently.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.common.banking import IFSC_SHAPE_MESSAGE, mask_account_number
from src.core.errors import BusinessRuleError, InvalidStateTransitionError
from src.core.models import Base
from src.core.registry import import_all_models
from src.modules.organization.models import CostCentreType
from src.modules.organization.repository import (
    COST_CENTRE_DEFAULT_PROBES,
    COST_CENTRE_REFERENCE_PROBES,
    DEPARTMENT_REFERENCE_PROBES,
    PLANT_REFERENCE_PROBES,
    CostCentreFacts,
    ReferenceProbe,
    reference_labels,
)
from src.modules.organization.schemas import (
    CompanyUpdate,
    CostCentreCreate,
    CostCentreUpdate,
    DepartmentCreate,
)
from src.modules.organization.service import (
    COST_CENTRE_LIFECYCLE,
    MAX_COST_CENTRE_DEPTH,
    MAX_DEPARTMENT_DEPTH,
    MasterLifecycleState,
    assert_parent_allowed,
)

pytestmark = pytest.mark.unit

ACTIVE = MasterLifecycleState.ACTIVE
INACTIVE = MasterLifecycleState.INACTIVE
ARCHIVED = MasterLifecycleState.ARCHIVED


def _ids(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


class TestLifecycle:
    def test_the_machine_is_well_formed(self) -> None:
        COST_CENTRE_LIFECYCLE.validate()

    def test_it_is_named_the_way_a_person_says_it(self) -> None:
        assert COST_CENTRE_LIFECYCLE.entity == "Cost centre"

    @pytest.mark.parametrize(
        ("source", "target"),
        [(ACTIVE, INACTIVE), (INACTIVE, ACTIVE), (ACTIVE, ARCHIVED), (INACTIVE, ARCHIVED)],
    )
    def test_the_permitted_moves(
        self, source: MasterLifecycleState, target: MasterLifecycleState
    ) -> None:
        COST_CENTRE_LIFECYCLE.assert_transition(source, target)

    @pytest.mark.parametrize("target", [ACTIVE, INACTIVE])
    def test_retirement_is_terminal(self, target: MasterLifecycleState) -> None:
        assert COST_CENTRE_LIFECYCLE.is_terminal(ARCHIVED)
        with pytest.raises(InvalidStateTransitionError):
            COST_CENTRE_LIFECYCLE.assert_transition(ARCHIVED, target)


# -----------------------------------------------------------------------------
# The tree
# -----------------------------------------------------------------------------


class TestDepth:
    def test_the_limit_is_the_department_limit(self) -> None:
        assert MAX_COST_CENTRE_DEPTH == MAX_DEPARTMENT_DEPTH

    def test_a_leaf_may_go_to_the_bottom_of_the_tree(self) -> None:
        node = uuid.uuid4()
        chain = _ids(MAX_COST_CENTRE_DEPTH - 1)
        assert_parent_allowed(
            entity="cost centre",
            node_id=node,
            parent_id=chain[0],
            parent_ancestor_ids=chain,
            max_depth=MAX_COST_CENTRE_DEPTH,
            subtree_height=0,
        )

    def test_a_subtree_is_counted_to_its_deepest_descendant(self) -> None:
        """Moving a centre moves its children: a parent at depth four leaves no
        room for a centre with two levels beneath it when the limit is six."""
        node = uuid.uuid4()
        chain = _ids(MAX_COST_CENTRE_DEPTH - 2)
        with pytest.raises(BusinessRuleError, match="may not be deeper") as raised:
            assert_parent_allowed(
                entity="cost centre",
                node_id=node,
                parent_id=chain[0],
                parent_ancestor_ids=chain,
                max_depth=MAX_COST_CENTRE_DEPTH,
                subtree_height=2,
            )
        assert raised.value.details["depth"] == MAX_COST_CENTRE_DEPTH + 1

    def test_the_same_subtree_fits_one_level_higher(self) -> None:
        node = uuid.uuid4()
        chain = _ids(MAX_COST_CENTRE_DEPTH - 3)
        assert_parent_allowed(
            entity="cost centre",
            node_id=node,
            parent_id=chain[0],
            parent_ancestor_ids=chain,
            max_depth=MAX_COST_CENTRE_DEPTH,
            subtree_height=2,
        )

    def test_the_department_rule_is_unchanged_by_default(self) -> None:
        """``subtree_height`` defaults to zero, so the department guard reads as before."""
        node = uuid.uuid4()
        chain = _ids(MAX_DEPARTMENT_DEPTH - 1)
        assert_parent_allowed(
            entity="department", node_id=node, parent_id=chain[0], parent_ancestor_ids=chain
        )

    def test_the_messages_name_a_cost_centre(self) -> None:
        node = uuid.uuid4()
        with pytest.raises(BusinessRuleError, match="A cost centre cannot be its own parent"):
            assert_parent_allowed(
                entity="cost centre",
                node_id=node,
                parent_id=node,
                parent_ancestor_ids=[node],
                max_depth=MAX_COST_CENTRE_DEPTH,
            )


# -----------------------------------------------------------------------------
# Payloads
# -----------------------------------------------------------------------------


class TestCostCentrePayloads:
    def test_the_code_is_normalised_and_the_name_cleaned(self) -> None:
        payload = CostCentreCreate(
            code=" cc  prod ",
            name="  Die   casting ",
            cost_centre_type=CostCentreType.PRODUCTION,
        )
        assert payload.code == "CC PROD"
        assert payload.name == "Die casting"

    def test_the_classification_is_required(self) -> None:
        """A silent default would misclassify every centre nobody remembered to set."""
        with pytest.raises(PydanticValidationError):
            CostCentreCreate.model_validate({"code": "CC-1", "name": "Assembly"})

    def test_an_unknown_classification_is_refused(self) -> None:
        with pytest.raises(PydanticValidationError):
            CostCentreCreate.model_validate(
                {"code": "CC-1", "name": "Assembly", "cost_centre_type": "OVERHEAD"}
            )

    def test_the_code_cannot_be_changed_by_an_update(self) -> None:
        """It is printed on posted ledger history."""
        with pytest.raises(PydanticValidationError):
            CostCentreUpdate.model_validate({"code": "CC-NEW"})

    def test_an_update_distinguishes_clearing_from_omitting(self) -> None:
        cleared = CostCentreUpdate.model_validate({"parent_id": None})
        assert cleared.model_dump(exclude_unset=True) == {"parent_id": None}
        assert CostCentreUpdate.model_validate({}).model_dump(exclude_unset=True) == {}

    def test_a_department_may_name_a_default(self) -> None:
        centre = uuid.uuid4()
        payload = DepartmentCreate(code="PROD", name="Production", cost_centre_id=centre)
        assert payload.cost_centre_id == centre


class TestCompanyBankDetails:
    def test_an_ifsc_is_upper_cased_and_despaced(self) -> None:
        payload = CompanyUpdate(bank_ifsc=" hdfc 0001204 ")
        assert payload.bank_ifsc == "HDFC0001204"

    @pytest.mark.parametrize("bad", ["HDFC1001204", "HDF00001204", "HDFC000120", "HDFC00012045"])
    def test_a_malformed_ifsc_is_refused_with_a_usable_message(self, bad: str) -> None:
        with pytest.raises(PydanticValidationError) as raised:
            CompanyUpdate(bank_ifsc=bad)
        assert IFSC_SHAPE_MESSAGE in str(raised.value)

    def test_blank_bank_fields_become_null(self) -> None:
        payload = CompanyUpdate(bank_ifsc="  ", bank_name="   ", invoice_terms=" ")
        assert payload.bank_ifsc is None
        assert payload.bank_name is None
        assert payload.invoice_terms is None

    def test_an_account_number_loses_its_spaces(self) -> None:
        assert CompanyUpdate(bank_account_number="5010 0123 4567 89").bank_account_number == (
            "50100123456789"
        )

    def test_terms_keep_their_line_breaks(self) -> None:
        terms = "Payment within 30 days.\nGoods once sold will not be taken back."
        assert CompanyUpdate(invoice_terms=f"  {terms}  ").invoice_terms == terms

    def test_the_shared_mask_keeps_the_last_four(self) -> None:
        assert mask_account_number("50100123456789") == "**********6789"


# -----------------------------------------------------------------------------
# The seam's facts
# -----------------------------------------------------------------------------


class TestFacts:
    @pytest.mark.parametrize(
        ("is_active", "is_archived", "assignable"),
        [(True, False, True), (False, False, False), (True, True, False), (False, True, False)],
    )
    def test_only_a_live_active_centre_is_assignable(
        self, is_active: bool, is_archived: bool, assignable: bool
    ) -> None:
        facts = CostCentreFacts(
            id=uuid.uuid4(),
            code="CC",
            name="Centre",
            is_active=is_active,
            is_archived=is_archived,
        )
        assert facts.is_assignable is assignable


# -----------------------------------------------------------------------------
# Retire-guard probes
# -----------------------------------------------------------------------------


class TestReferenceProbes:
    def test_a_master_table_is_referenced_by_its_live_rows_only(self) -> None:
        sql = ReferenceProbe("work_centers", "plant_id", "work centres", soft_deleted=True)
        assert "deleted_at IS NULL" in sql.exists_sql()

    def test_a_document_table_is_referenced_by_every_row(self) -> None:
        sql = ReferenceProbe("production_orders", "plant_id", "production orders").exists_sql()
        assert "deleted_at" not in sql
        assert "company_id = :company_id" in sql
        assert "plant_id = :entity_id" in sql

    def test_labels_follow_probe_order_and_do_not_repeat(self) -> None:
        probes = (
            ReferenceProbe("b_table", "x_id", "bees"),
            ReferenceProbe("a_table", "x_id", "ays"),
            ReferenceProbe("b_table", "y_id", "bees"),
        )
        assert reference_labels(probes, ["a_table", "b_table"]) == ["bees", "ays"]
        assert reference_labels(probes, []) == []

    def test_the_department_guard_probes_every_carrier(self) -> None:
        assert {probe.table for probe in DEPARTMENT_REFERENCE_PROBES} == {
            "work_centers",
            "purchase_requisitions",
            "internal_issues",
        }

    def test_the_plant_guard_probes_work_centres_and_production_orders(self) -> None:
        assert {probe.table for probe in PLANT_REFERENCE_PROBES} == {
            "work_centers",
            "production_orders",
        }

    def test_the_deactivation_guard_is_a_subset_of_the_archive_guard(self) -> None:
        assert set(COST_CENTRE_DEFAULT_PROBES) <= set(COST_CENTRE_REFERENCE_PROBES)

    def test_posted_history_never_blocks_retirement(self) -> None:
        assert "ledger_entries" not in {probe.table for probe in COST_CENTRE_REFERENCE_PROBES}

    def test_every_table_that_carries_a_cost_centre_is_probed(self) -> None:
        """A carrier added later must join the archive guard, or say why not.

        Read from the schema rather than listed by hand: the failure this
        prevents is a module adding ``cost_centre_id`` to its own table and a
        cost centre then being archived out from under its documents, with no
        error anywhere. Three exemptions, each checked another way:

        * ``ledger_entries`` - posted history keeps its centre (§43);
        * ``departments`` - organisation's own table, checked through the ORM;
        * ``cost_centres`` - the parent link, checked as "live children".
        """
        import_all_models()
        exempt = {"ledger_entries", "departments", "cost_centres"}
        probed = {(probe.table, probe.column) for probe in COST_CENTRE_REFERENCE_PROBES}

        carriers = {
            (table.name, column.name)
            for table in Base.metadata.tables.values()
            for column in table.columns
            for key in column.foreign_keys
            if key.column.table.name == "cost_centres"
        }

        unprobed = sorted(
            (table, column)
            for table, column in carriers
            if table not in exempt and (table, column) not in probed
        )
        assert unprobed == [], (
            f"these columns reference cost_centres but the archive guard never probes them: "
            f"{unprobed}. Add each to COST_CENTRE_REFERENCE_PROBES in "
            "src/modules/organization/repository.py."
        )
        stale = sorted(probed - carriers)
        assert stale == [], f"probes for columns that no longer reference cost_centres: {stale}"
