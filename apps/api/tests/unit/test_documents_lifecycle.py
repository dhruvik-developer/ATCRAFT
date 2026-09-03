"""Document lifecycle and retention defaults - no database involved.

The state is derived from ``expires_at`` rather than stored, so these tests pin
down the derivation as well as the transition graph: a wrong derivation would
make an expired export undeletable, or make a live document look purgeable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core.clock import frozen_time
from src.core.errors import ErrorCode, InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.documents.models import DocumentCategory
from src.modules.documents.service import (
    DEFAULT_RETENTION,
    DOCUMENT_LIFECYCLE,
    DocumentState,
    default_expiry,
    document_state,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _document(expires_at: datetime | None = None) -> SimpleNamespace:
    """Minimal stand-in: the derivation reads one attribute and nothing else."""
    return SimpleNamespace(expires_at=expires_at)


class TestStateDerivation:
    def test_a_document_without_an_expiry_is_available(self) -> None:
        assert document_state(_document(), NOW) is DocumentState.AVAILABLE

    def test_a_future_expiry_is_still_available(self) -> None:
        assert document_state(_document(NOW + timedelta(seconds=1)), NOW) is DocumentState.AVAILABLE

    def test_a_past_expiry_is_expired(self) -> None:
        assert document_state(_document(NOW - timedelta(seconds=1)), NOW) is DocumentState.EXPIRED

    def test_the_expiry_instant_itself_counts_as_expired(self) -> None:
        # ``expires_at`` is the moment retention ends, not the last moment of
        # validity; the repository's ``expires_at > now`` filter agrees.
        assert document_state(_document(NOW), NOW) is DocumentState.EXPIRED

    def test_defaults_to_the_project_clock(self) -> None:
        with frozen_time(NOW):
            assert document_state(_document(NOW - timedelta(days=1))) is DocumentState.EXPIRED


class TestLifecycleGraph:
    def test_graph_is_structurally_sound(self) -> None:
        # No unreachable state, no non-terminal state without an exit.
        DOCUMENT_LIFECYCLE.validate()

    @pytest.mark.parametrize("source", [DocumentState.AVAILABLE, DocumentState.EXPIRED])
    def test_deletion_is_reachable_from_both_live_states(self, source: DocumentState) -> None:
        # An expired export must remain removable, or the retention sweep has no
        # legal move and the backlog never drains.
        transition = DOCUMENT_LIFECYCLE.assert_transition(source, DocumentState.DELETED)
        assert transition.permission is Permission.DOCUMENT_DELETE
        assert transition.is_irreversible

    def test_deletion_is_terminal(self) -> None:
        assert DOCUMENT_LIFECYCLE.is_terminal(DocumentState.DELETED)
        with pytest.raises(InvalidStateTransitionError) as exc:
            DOCUMENT_LIFECYCLE.assert_transition(DocumentState.DELETED, DocumentState.AVAILABLE)
        assert exc.value.code is ErrorCode.INVALID_STATE_TRANSITION

    def test_an_expired_document_cannot_be_revived(self) -> None:
        # Restoring one would need a new expiry, which is an update this module
        # deliberately does not offer: document metadata is write-once.
        with pytest.raises(InvalidStateTransitionError):
            DOCUMENT_LIFECYCLE.assert_transition(DocumentState.EXPIRED, DocumentState.AVAILABLE)

    def test_available_documents_expose_expiry_and_deletion_only(self) -> None:
        assert set(DOCUMENT_LIFECYCLE.allowed_targets(DocumentState.AVAILABLE)) == {
            DocumentState.EXPIRED,
            DocumentState.DELETED,
        }

    def test_a_viewer_is_offered_no_destructive_action(self) -> None:
        offered = DOCUMENT_LIFECYCLE.available_actions(
            DocumentState.AVAILABLE, [Permission.DOCUMENT_VIEW]
        )
        assert [t.target for t in offered] == [DocumentState.EXPIRED]


class TestRetentionDefaults:
    def test_generated_artefacts_expire_by_default(self) -> None:
        assert default_expiry(DocumentCategory.TEMP, NOW) == NOW + timedelta(hours=24)
        assert default_expiry(DocumentCategory.EXPORT, NOW) == NOW + timedelta(days=7)

    @pytest.mark.parametrize(
        "category",
        [DocumentCategory.DOCUMENT, DocumentCategory.REPORT, DocumentCategory.ATTACHMENT],
    )
    def test_business_documents_are_kept_indefinitely(self, category: DocumentCategory) -> None:
        # Silently deleting a supplier's QC certificate would be data loss, not
        # housekeeping - so only the transient categories carry a default.
        assert default_expiry(category, NOW) is None
        assert category not in DEFAULT_RETENTION
