"""Item lifecycle state machine and category path materialisation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.products.models import MAX_CATEGORY_DEPTH, ItemLifecycleState
from src.modules.products.service import (
    ITEM_LIFECYCLE,
    build_category_path,
    category_depth,
    lifecycle_state,
)


@dataclass(frozen=True)
class ItemRow:
    """The two columns ``lifecycle_state`` projects. Not an ORM instance: the
    projection is pure logic and must be testable without a mapper registry."""

    is_active: bool
    deleted_at: datetime | None = None


def item(is_active: bool, deleted_at: datetime | None = None) -> Any:
    return cast(Any, ItemRow(is_active=is_active, deleted_at=deleted_at))


def test_the_state_graph_is_structurally_sound() -> None:
    # Catches an unreachable state or a non-terminal dead end.
    ITEM_LIFECYCLE.validate()


def test_active_item_can_be_deactivated_or_retired() -> None:
    targets = set(ITEM_LIFECYCLE.allowed_targets(ItemLifecycleState.ACTIVE))
    assert targets == {ItemLifecycleState.INACTIVE, ItemLifecycleState.ARCHIVED}


def test_retirement_is_terminal() -> None:
    assert ITEM_LIFECYCLE.is_terminal(ItemLifecycleState.ARCHIVED)
    assert ITEM_LIFECYCLE.allowed_targets(ItemLifecycleState.ARCHIVED) == []


def test_reactivating_an_archived_item_is_refused() -> None:
    with pytest.raises(InvalidStateTransitionError):
        ITEM_LIFECYCLE.assert_transition(ItemLifecycleState.ARCHIVED, ItemLifecycleState.ACTIVE)


def test_a_no_op_transition_is_refused() -> None:
    # Matters because a double-clicked "deactivate" must not write a second
    # audit row claiming a status change that did not happen.
    with pytest.raises(InvalidStateTransitionError):
        ITEM_LIFECYCLE.assert_transition(ItemLifecycleState.ACTIVE, ItemLifecycleState.ACTIVE)


def test_retirement_requires_the_delete_permission_not_merely_update() -> None:
    transition = ITEM_LIFECYCLE.find(ItemLifecycleState.ACTIVE, ItemLifecycleState.ARCHIVED)
    assert transition is not None
    assert transition.permission is Permission.ITEM_DELETE
    assert transition.is_irreversible is True


def test_a_user_with_only_update_rights_cannot_see_the_retire_action() -> None:
    actions = ITEM_LIFECYCLE.available_actions(ItemLifecycleState.ACTIVE, [Permission.ITEM_UPDATE])
    assert [t.target for t in actions] == [ItemLifecycleState.INACTIVE]


def test_lifecycle_state_is_derived_from_the_two_stored_columns() -> None:
    assert lifecycle_state(item(True)) is ItemLifecycleState.ACTIVE
    assert lifecycle_state(item(False)) is ItemLifecycleState.INACTIVE
    # A retired row reports ARCHIVED whatever is_active says, so a stale active
    # flag on a deleted item cannot make it look live.
    retired = item(True, datetime(2026, 8, 1, tzinfo=UTC))
    assert lifecycle_state(retired) is ItemLifecycleState.ARCHIVED


# --- category paths ---------------------------------------------------------


def test_root_path_is_bounded_by_separators() -> None:
    node = uuid.uuid4()
    path = build_category_path(None, node)
    assert path == f"/{node}/"
    assert category_depth(path) == 0


def test_child_path_extends_the_parent_and_increases_depth() -> None:
    root = uuid.uuid4()
    child = uuid.uuid4()
    root_path = build_category_path(None, root)
    child_path = build_category_path(root_path, child)

    assert child_path == f"/{root}/{child}/"
    assert child_path.startswith(root_path)
    assert category_depth(child_path) == 1


def test_a_sibling_path_is_not_a_prefix_match_of_another() -> None:
    # The trailing separator is what stops "everything under A" from also
    # matching an unrelated node whose id happens to share a prefix.
    root = uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    path_a = build_category_path(build_category_path(None, root), a)
    path_b = build_category_path(build_category_path(None, root), b)

    assert not path_b.startswith(path_a)


def test_the_deepest_permitted_path_fits_the_column() -> None:
    from src.modules.products.models import CATEGORY_PATH_LENGTH

    path: str | None = None
    for _ in range(MAX_CATEGORY_DEPTH):
        path = build_category_path(path, uuid.uuid4())

    assert path is not None
    assert category_depth(path) == MAX_CATEGORY_DEPTH - 1
    assert len(path) <= CATEGORY_PATH_LENGTH
