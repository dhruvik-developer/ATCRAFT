"""Channel selection and payload normalisation - no database involved.

These are the decisions ``notify`` makes before it touches a row: who actually
receives an alert once their preferences are applied, and what a caller is
allowed to put in one. Both are pure functions precisely so they can be pinned
down here rather than inferred from an integration test.
"""

from __future__ import annotations

import uuid

import pytest

from src.core.errors import ErrorCode, ValidationError
from src.modules.notifications.models import NotificationChannel, NotificationSeverity
from src.modules.notifications.service import (
    MAX_ACTION_URL_LENGTH,
    MAX_GROUP_KEY_LENGTH,
    MAX_TITLE_LENGTH,
    dedupe_recipients,
    normalise_action_url,
    normalise_entity_reference,
    normalise_group_key,
    normalise_title,
    resolve_channels,
)

pytestmark = pytest.mark.unit

IN_APP = NotificationChannel.IN_APP.value
EMAIL = NotificationChannel.EMAIL.value


class TestResolveChannels:
    def test_default_preference_delivers_in_app_only(self) -> None:
        assert resolve_channels(
            NotificationSeverity.INFO, in_app_enabled=True, email_enabled=False
        ) == (IN_APP,)

    def test_both_channels_list_in_app_first(self) -> None:
        # Order is part of the stored value and drives what the UI reports as the
        # primary delivery route.
        assert resolve_channels(
            NotificationSeverity.INFO, in_app_enabled=True, email_enabled=True
        ) == (IN_APP, EMAIL)

    def test_email_only_is_a_valid_choice(self) -> None:
        assert resolve_channels(
            NotificationSeverity.WARNING, in_app_enabled=False, email_enabled=True
        ) == (EMAIL,)

    def test_muting_everything_yields_no_channel_at_all(self) -> None:
        # The caller reads this as "write no row": a notification delivered
        # nowhere would be uncountable and unreadable for ever.
        assert (
            resolve_channels(
                NotificationSeverity.WARNING, in_app_enabled=False, email_enabled=False
            )
            == ()
        )

    def test_critical_severity_overrides_an_in_app_opt_out(self) -> None:
        assert resolve_channels(
            NotificationSeverity.CRITICAL, in_app_enabled=False, email_enabled=False
        ) == (IN_APP,)

    def test_critical_does_not_force_email(self) -> None:
        # The override exists so a silent failure is impossible, not so the
        # system can mail someone who asked it not to.
        assert EMAIL not in resolve_channels(
            NotificationSeverity.CRITICAL, in_app_enabled=False, email_enabled=False
        )

    @pytest.mark.parametrize(
        "severity",
        [NotificationSeverity.INFO, NotificationSeverity.SUCCESS, NotificationSeverity.WARNING],
    )
    def test_no_other_severity_overrides_a_mute(self, severity: NotificationSeverity) -> None:
        assert resolve_channels(severity, in_app_enabled=False, email_enabled=False) == ()


class TestDedupeRecipients:
    def test_removes_repeats_and_keeps_first_seen_order(self) -> None:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        assert dedupe_recipients([a, b, a, c, b]) == [a, b, c]

    def test_empty_input_is_empty_output(self) -> None:
        assert dedupe_recipients([]) == []


class TestNormaliseTitle:
    def test_collapses_whitespace(self) -> None:
        assert normalise_title("  PO-1042   needs\n approval ") == "PO-1042 needs approval"

    def test_trims_an_overlong_title_to_fit_the_column(self) -> None:
        result = normalise_title("x" * (MAX_TITLE_LENGTH + 50))
        assert len(result) == MAX_TITLE_LENGTH
        assert result.endswith("…")

    def test_a_title_at_the_limit_is_left_alone(self) -> None:
        exact = "y" * MAX_TITLE_LENGTH
        assert normalise_title(exact) == exact

    def test_blank_title_is_rejected(self) -> None:
        # Nothing sensible can be rendered, and no fallback exists.
        with pytest.raises(ValidationError):
            normalise_title("   \n ")


class TestNormaliseActionUrl:
    def test_accepts_a_site_relative_path(self) -> None:
        assert normalise_action_url("/purchase-orders/42") == "/purchase-orders/42"

    def test_blank_becomes_none(self) -> None:
        assert normalise_action_url("  ") is None
        assert normalise_action_url(None) is None

    @pytest.mark.parametrize(
        "hostile",
        [
            "https://evil.example/steal",
            "http://evil.example",
            "//evil.example/steal",  # protocol-relative: browsers go off-site
            "javascript:alert(1)",
            "purchase-orders/42",  # not anchored, resolves against the current page
        ],
    )
    def test_rejects_anything_that_can_leave_the_application(self, hostile: str) -> None:
        with pytest.raises(ValidationError) as exc:
            normalise_action_url(hostile)
        assert exc.value.code is ErrorCode.VALIDATION_ERROR

    def test_rejects_an_overlong_url_rather_than_truncating_it(self) -> None:
        # A truncated link points somewhere else, which is worse than no link.
        with pytest.raises(ValidationError):
            normalise_action_url("/" + "a" * MAX_ACTION_URL_LENGTH)


class TestNormaliseGroupKey:
    def test_trims_and_passes_through(self) -> None:
        assert normalise_group_key("  po-delay:42 ") == "po-delay:42"

    def test_blank_becomes_none_so_the_alert_never_collapses(self) -> None:
        assert normalise_group_key("") is None
        assert normalise_group_key(None) is None

    def test_rejects_an_overlong_key_rather_than_truncating_it(self) -> None:
        # Truncation would silently merge two distinct alerts into one, and a
        # suppressed notification is a lost one.
        with pytest.raises(ValidationError):
            normalise_group_key("k" * (MAX_GROUP_KEY_LENGTH + 1))


class TestNormaliseEntityReference:
    def test_lowercases_the_type(self) -> None:
        entity_id = uuid.uuid4()
        assert normalise_entity_reference("Purchase_Order", entity_id) == (
            "purchase_order",
            entity_id,
        )

    def test_no_reference_at_all_is_allowed(self) -> None:
        assert normalise_entity_reference(None, None) == (None, None)
        assert normalise_entity_reference("  ", None) == (None, None)

    def test_half_a_reference_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            normalise_entity_reference("purchase_order", None)
        with pytest.raises(ValidationError):
            normalise_entity_reference(None, uuid.uuid4())
