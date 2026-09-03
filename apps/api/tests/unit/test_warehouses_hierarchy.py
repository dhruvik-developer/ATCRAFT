"""Hierarchy rules for warehouses and storage locations.

``assert_parent_allowed`` is the guard that keeps ``parent_warehouse_id`` a tree.
It is deliberately pure - the recursive ancestor walk happens in the repository
and is handed in as a list - so the interesting cases can be exercised exactly,
including the ones that are awkward to set up against a database.
"""

from __future__ import annotations

import uuid

import pytest

from src.core.errors import BusinessRuleError
from src.modules.warehouses.service import MAX_HIERARCHY_DEPTH, assert_parent_allowed

pytestmark = pytest.mark.unit


def _ids(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


class TestParentAssignment:
    def test_accepts_a_parent_that_is_not_related(self) -> None:
        node, parent = _ids(2)
        assert_parent_allowed(
            entity="warehouse",
            node_id=node,
            parent_id=parent,
            parent_ancestor_ids=[parent],
        )

    def test_rejects_self_parenting(self) -> None:
        node = uuid.uuid4()
        with pytest.raises(BusinessRuleError, match="cannot be its own parent"):
            assert_parent_allowed(
                entity="warehouse",
                node_id=node,
                parent_id=node,
                parent_ancestor_ids=[node],
            )

    def test_rejects_a_direct_cycle(self) -> None:
        """A -> B, then B -> A. The chain above B already contains A."""
        node, child = _ids(2)
        with pytest.raises(BusinessRuleError, match="must stay a tree"):
            assert_parent_allowed(
                entity="warehouse",
                node_id=node,
                parent_id=child,
                parent_ancestor_ids=[child, node],
            )

    def test_rejects_an_indirect_cycle(self) -> None:
        """A -> B -> C, then A's parent set to C: the loop closes three levels up."""
        node, middle, grandchild = _ids(3)
        with pytest.raises(BusinessRuleError) as exc:
            assert_parent_allowed(
                entity="warehouse",
                node_id=node,
                parent_id=grandchild,
                parent_ancestor_ids=[grandchild, middle, node],
            )
        assert exc.value.details["parent_id"] == str(grandchild)


class TestDepthLimit:
    def test_allows_a_chain_exactly_at_the_limit(self) -> None:
        # The record itself occupies the last level, so the parent chain may be
        # one shorter than the limit.
        ancestors = _ids(MAX_HIERARCHY_DEPTH - 1)
        assert_parent_allowed(
            entity="warehouse",
            node_id=uuid.uuid4(),
            parent_id=ancestors[0],
            parent_ancestor_ids=ancestors,
        )

    def test_rejects_one_level_too_deep(self) -> None:
        ancestors = _ids(MAX_HIERARCHY_DEPTH)
        with pytest.raises(BusinessRuleError) as exc:
            assert_parent_allowed(
                entity="warehouse",
                node_id=uuid.uuid4(),
                parent_id=ancestors[0],
                parent_ancestor_ids=ancestors,
            )
        assert exc.value.details == {
            "depth": MAX_HIERARCHY_DEPTH + 1,
            "max_depth": MAX_HIERARCHY_DEPTH,
        }

    def test_depth_applies_to_a_record_that_does_not_exist_yet(self) -> None:
        """A create has no id, and must still not be nested below the limit."""
        ancestors = _ids(MAX_HIERARCHY_DEPTH)
        with pytest.raises(BusinessRuleError, match="deeper than"):
            assert_parent_allowed(
                entity="storage location",
                node_id=None,
                parent_id=ancestors[0],
                parent_ancestor_ids=ancestors,
            )

    def test_limit_is_overridable_for_a_configured_site(self) -> None:
        ancestors = _ids(6)
        assert_parent_allowed(
            entity="warehouse",
            node_id=uuid.uuid4(),
            parent_id=ancestors[0],
            parent_ancestor_ids=ancestors,
            max_depth=7,
        )


class TestMessageWording:
    def test_entity_name_reaches_the_user(self) -> None:
        node = uuid.uuid4()
        with pytest.raises(BusinessRuleError, match="storage location cannot be its own parent"):
            assert_parent_allowed(
                entity="storage location",
                node_id=node,
                parent_id=node,
                parent_ancestor_ids=[node],
            )
