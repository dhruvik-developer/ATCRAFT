"""The one rule that keeps a self-referencing master a tree.

Departments, cost centres, warehouses and storage locations each point at a
parent of their own kind. Organisation and warehouses each carried a copy of
:func:`assert_parent_allowed`, emitting the same three refusals; the copies had
already drifted - only one counted the levels that move with the record. It is
deliberately pure: the repository walks the ancestors with one recursive query
and hands them in as a list, so every case is unit-testable without a database.
"""

from __future__ import annotations

import uuid

from src.core.errors import BusinessRuleError
from src.core.messages import hierarchy_too_deep, own_parent, parent_below


def assert_parent_allowed(
    *,
    entity: str,
    node_id: uuid.UUID | None,
    parent_id: uuid.UUID,
    parent_ancestor_ids: list[uuid.UUID],
    max_depth: int,
    subtree_height: int = 0,
) -> None:
    """Reject a parent assignment that would break the tree.

    ``parent_ancestor_ids`` is the chain from the proposed parent up to its root,
    *including the parent itself*, as returned by the repository's recursive
    walk. ``node_id`` is ``None`` on a create. ``subtree_height`` is how many
    levels sit *below* the record - zero for a leaf, and always zero on a create.
    Three failures are possible:

    * the record is its own parent;
    * the record already appears above the proposed parent, which would close a
      cycle - the case that turns a roll-up query into an infinite loop;
    * the resulting chain would be deeper than ``max_depth``, counted to the
      deepest descendant that moves with the record.
    """
    if node_id is not None and parent_id == node_id:
        raise BusinessRuleError(own_parent(entity))

    if node_id is not None and node_id in parent_ancestor_ids:
        raise BusinessRuleError(
            parent_below(entity),
            details={"parent_id": str(parent_id)},
        )

    # +1 for the record itself, which sits one level under the proposed parent,
    # and the levels beneath it, which move with it.
    depth = len(parent_ancestor_ids) + 1 + max(0, subtree_height)
    if depth > max_depth:
        raise BusinessRuleError(
            hierarchy_too_deep(entity, max_depth),
            details={"depth": depth, "max_depth": max_depth},
        )


__all__ = ["assert_parent_allowed"]
