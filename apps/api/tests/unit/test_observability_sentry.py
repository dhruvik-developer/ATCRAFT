"""Privacy boundary for optional backend exception reporting."""

from __future__ import annotations

from src.core.observability import _scrub_sentry_event


def test_sentry_event_drops_request_and_business_context() -> None:
    event = {
        "request": {
            "headers": {"authorization": "Bearer live-token"},
            "url": "https://erp.example.test/items?search=private-customer",
            "data": {"unit_price": "125.00"},
        },
        "user": {"id": "user-id", "email": "person@example.test"},
        "breadcrumbs": {"values": [{"message": "Opened customer ACME"}]},
        "extra": {"purchase_order": "PO-PRIVATE"},
        "exception": {"values": [{"type": "RuntimeError", "value": "failed"}]},
        "environment": "production",
    }

    scrubbed = _scrub_sentry_event(event, {})

    assert scrubbed is event
    assert "request" not in scrubbed
    assert "user" not in scrubbed
    assert "breadcrumbs" not in scrubbed
    assert "extra" not in scrubbed
    assert scrubbed["exception"] == {"values": [{"type": "RuntimeError", "value": "failed"}]}
    assert scrubbed["environment"] == "production"
