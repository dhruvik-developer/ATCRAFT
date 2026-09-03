"""Read lifecycle and effective preferences - no database involved.

The read lifecycle has exactly one edge, which makes it easy to under-test and
easy to break: the property that matters is not "a notification can be marked
read" but "nothing can ever mark it unread again", because ``read_at`` is the
evidence of when an approver first saw a request.

The preference resolution is here for a related reason: it is the code that
decides what a user who has never opened the settings screen receives, and a
silent change to it would change delivery for every existing account.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.errors import ErrorCode, InvalidStateTransitionError
from src.modules.notifications.models import NotificationPreference, NotificationType
from src.modules.notifications.service import (
    DEFAULT_EMAIL_ENABLED,
    DEFAULT_IN_APP_ENABLED,
    NOTIFICATION_EMAIL_TASK,
    NOTIFICATION_READ_MACHINE,
    NotificationReadState,
    effective_preferences,
    read_state,
)

pytestmark = pytest.mark.unit


class TestReadStateMachine:
    def test_graph_is_structurally_sound(self) -> None:
        NOTIFICATION_READ_MACHINE.validate()

    def test_acknowledgement_is_the_only_edge(self) -> None:
        assert NOTIFICATION_READ_MACHINE.to_graph() == {"UNREAD": ["READ"]}

    def test_unread_may_become_read(self) -> None:
        transition = NOTIFICATION_READ_MACHINE.assert_transition(
            NotificationReadState.UNREAD, NotificationReadState.READ
        )
        # No permission gate: acknowledging one's own notification is not a
        # privileged action.
        assert transition.permission is None

    def test_read_can_never_go_back_to_unread(self) -> None:
        with pytest.raises(InvalidStateTransitionError) as exc:
            NOTIFICATION_READ_MACHINE.assert_transition(
                NotificationReadState.READ, NotificationReadState.UNREAD
            )
        assert exc.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert exc.value.details["allowed"] == []

    def test_read_is_terminal(self) -> None:
        assert NOTIFICATION_READ_MACHINE.is_terminal(NotificationReadState.READ)
        assert not NOTIFICATION_READ_MACHINE.is_terminal(NotificationReadState.UNREAD)

    def test_state_is_derived_from_the_flag(self) -> None:
        assert read_state(SimpleNamespace(is_read=False)) is NotificationReadState.UNREAD
        assert read_state(SimpleNamespace(is_read=True)) is NotificationReadState.READ


class TestEffectivePreferences:
    def test_every_type_is_reported_even_with_nothing_stored(self) -> None:
        result = effective_preferences([])

        assert [p.notification_type for p in result] == list(NotificationType)
        assert all(p.in_app_enabled is DEFAULT_IN_APP_ENABLED for p in result)
        assert all(p.email_enabled is DEFAULT_EMAIL_ENABLED for p in result)
        assert not any(p.is_customised for p in result)

    def test_a_stored_row_overrides_the_default_for_that_type_only(self) -> None:
        stored = NotificationPreference(
            user_id=None,
            notification_type=NotificationType.PURCHASE_ORDER_DELAYED,
            in_app_enabled=False,
            email_enabled=True,
        )

        result = {p.notification_type: p for p in effective_preferences([stored])}

        overridden = result[NotificationType.PURCHASE_ORDER_DELAYED]
        assert (overridden.in_app_enabled, overridden.email_enabled) == (False, True)
        assert overridden.is_customised is True

        untouched = result[NotificationType.APPROVAL_PENDING]
        assert untouched.in_app_enabled is DEFAULT_IN_APP_ENABLED
        assert untouched.is_customised is False

    def test_defaults_make_email_opt_in(self) -> None:
        # Pinned deliberately: flipping this would start mailing every user of
        # every existing account without anyone asking for it.
        assert DEFAULT_IN_APP_ENABLED is True
        assert DEFAULT_EMAIL_ENABLED is False


def test_email_task_name_is_the_published_contract() -> None:
    # The worker module is written separately and registers a task under exactly
    # this name; a rename on either side silently stops all e-mail.
    assert NOTIFICATION_EMAIL_TASK == "notifications.send_email"
    # It must stay on the notifications queue - the ``notifications.*`` routing
    # rule in celery_app is what keeps a 200 ms alert out from behind an MRP run.
    assert NOTIFICATION_EMAIL_TASK.startswith("notifications.")
