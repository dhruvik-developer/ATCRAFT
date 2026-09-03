"""The guard that keeps ``departments.parent_id`` a tree.

``assert_parent_allowed`` is deliberately pure - the recursive ancestor walk
happens in the repository and is handed in as a list - so the interesting cases
can be exercised exactly, including the ones that are awkward to set up against
a database (a cycle that already exists, a chain sitting on the depth limit).
"""

from __future__ import annotations

import uuid

import pytest

from src.core.errors import BusinessRuleError
from src.modules.organization.service import MAX_DEPARTMENT_DEPTH, assert_parent_allowed

pytestmark = pytest.mark.unit


def _ids(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


class TestParentAssignment:
    def test_accepts_an_unrelated_parent(self) -> None:
        node, parent = _ids(2)
        assert_parent_allowed(
            entity="department",
            node_id=node,
            parent_id=parent,
            parent_ancestor_ids=[parent],
        )

    def test_accepts_a_parent_on_create_when_the_node_has_no_id_yet(self) -> None:
        """A create has no node id, so the cycle checks cannot apply - only depth."""
        ancestors = _ids(2)
        assert_parent_allowed(
            entity="department",
            node_id=None,
            parent_id=ancestors[0],
            parent_ancestor_ids=ancestors,
        )

    def test_rejects_self_parenting(self) -> None:
        node = uuid.uuid4()
        with pytest.raises(BusinessRuleError, match="cannot be its own parent"):
            assert_parent_allowed(
                entity="department",
                node_id=node,
                parent_id=node,
                parent_ancestor_ids=[node],
            )

    def test_rejects_a_direct_cycle(self) -> None:
        """A -> B, then B's parent set to A's child. The chain above already holds A."""
        node, child = _ids(2)
        with pytest.raises(BusinessRuleError, match="must stay a tree"):
            assert_parent_allowed(
                entity="department",
                node_id=node,
                parent_id=child,
                parent_ancestor_ids=[child, node],
            )

    def test_rejects_an_indirect_cycle_and_names_the_parent(self) -> None:
        """A -> B -> C, then A's parent set to C: the loop closes three levels up."""
        node, middle, grandchild = _ids(3)
        with pytest.raises(BusinessRuleError) as exc:
            assert_parent_allowed(
                entity="department",
                node_id=node,
                parent_id=grandchild,
                parent_ancestor_ids=[grandchild, middle, node],
            )
        assert exc.value.details["parent_id"] == str(grandchild)


class TestDepthLimit:
    def test_allows_a_chain_exactly_at_the_limit(self) -> None:
        # The record itself occupies the last level, so the parent chain may be
        # one shorter than the limit.
        ancestors = _ids(MAX_DEPARTMENT_DEPTH - 1)
        assert_parent_allowed(
            entity="department",
            node_id=uuid.uuid4(),
            parent_id=ancestors[0],
            parent_ancestor_ids=ancestors,
        )

    def test_rejects_one_level_past_the_limit(self) -> None:
        ancestors = _ids(MAX_DEPARTMENT_DEPTH)
        with pytest.raises(BusinessRuleError) as exc:
            assert_parent_allowed(
                entity="department",
                node_id=uuid.uuid4(),
                parent_id=ancestors[0],
                parent_ancestor_ids=ancestors,
            )
        assert exc.value.details == {
            "depth": MAX_DEPARTMENT_DEPTH + 1,
            "max_depth": MAX_DEPARTMENT_DEPTH,
        }

    def test_the_cycle_check_runs_before_the_depth_check(self) -> None:
        """A cycle in a chain that is also too deep reports the cycle.

        Order matters for the message the user sees: "you would create a loop"
        is actionable, "too deep" sends them off to shorten a hierarchy that is
        not actually the problem.
        """
        node = uuid.uuid4()
        ancestors = [*_ids(MAX_DEPARTMENT_DEPTH), node]
        with pytest.raises(BusinessRuleError, match="must stay a tree"):
            assert_parent_allowed(
                entity="department",
                node_id=node,
                parent_id=ancestors[0],
                parent_ancestor_ids=ancestors,
            )

    def test_a_custom_limit_is_honoured(self) -> None:
        ancestors = _ids(2)
        with pytest.raises(BusinessRuleError, match="deeper than 2 levels"):
            assert_parent_allowed(
                entity="department",
                node_id=uuid.uuid4(),
                parent_id=ancestors[0],
                parent_ancestor_ids=ancestors,
                max_depth=2,
            )
