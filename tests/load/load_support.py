"""Account and fixture validation shared by the Locust runner and unit tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LoadAccount:
    email: str
    password: str = field(repr=False)
    write_fixtures: dict[str, object] | None = None


_WRITE_FIXTURE_KEYS = {
    "inventory_adjustment_id",
    "reservation",
    "inspection",
    "inspection_decision",
    "dispatch",
    "dispatch_request",
    "invoice",
}


def read_accounts(path: Path, *, require_write_fixtures: bool) -> list[LoadAccount]:
    try:
        # Windows editors commonly save JSON with a BOM; utf-8-sig accepts both.
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError("LOAD_ACCOUNTS_FILE is not valid JSON") from exc
    return validate_account_payload(payload, require_write_fixtures=require_write_fixtures)


def validate_account_payload(payload: object, *, require_write_fixtures: bool) -> list[LoadAccount]:
    """Validate an already-parsed account array.

    Split out from :func:`read_accounts` so the fixture generator can check the
    rows it is about to rewrite without first writing them to a temporary file.
    """
    if not isinstance(payload, list):
        raise ValueError("LOAD_ACCOUNTS_FILE must contain a JSON array")

    accounts: list[LoadAccount] = []
    seen_emails: set[str] = set()
    seen_adjustments: set[str] = set()
    seen_orders: set[str] = set()
    for index, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Account entry {index} must be an object")
        email = row.get("email")
        password = row.get("password")
        if not isinstance(email, str) or not email.strip():
            raise ValueError(f"Account entry {index} needs an email")
        if not isinstance(password, str) or not password:
            raise ValueError(f"Account entry {index} needs a password")
        normalized_email = email.strip().casefold()
        if normalized_email in seen_emails:
            raise ValueError(f"Duplicate email in LOAD_ACCOUNTS_FILE at entry {index}")
        seen_emails.add(normalized_email)

        fixtures = row.get("write_fixtures")
        if fixtures is not None and not isinstance(fixtures, dict):
            raise ValueError(f"write_fixtures for account {index} must be an object")
        if require_write_fixtures:
            if not isinstance(fixtures, dict):
                raise ValueError(f"Account entry {index} has no write_fixtures object")
            missing = _WRITE_FIXTURE_KEYS - fixtures.keys()
            if missing:
                raise ValueError(
                    f"Account entry {index} is missing write fixtures: {', '.join(sorted(missing))}"
                )
            adjustment_id = fixtures["inventory_adjustment_id"]
            dispatch = fixtures["dispatch"]
            if not isinstance(adjustment_id, str) or not adjustment_id:
                raise ValueError(f"Account entry {index} needs an inventory adjustment id")
            if adjustment_id.casefold() in seen_adjustments:
                raise ValueError("Inventory adjustment ids must be unique per load account")
            seen_adjustments.add(adjustment_id.casefold())
            if (
                not isinstance(dispatch, dict)
                or not isinstance(dispatch.get("sales_order_id"), str)
                or not dispatch["sales_order_id"]
            ):
                raise ValueError(f"Account entry {index} needs a distinct sales_order_id")
            order_id = dispatch["sales_order_id"].casefold()
            if order_id in seen_orders:
                raise ValueError("Sales order ids must be unique per load account")
            seen_orders.add(order_id)
            if not isinstance(fixtures["reservation"], dict):
                raise ValueError(f"Reservation fixture for account {index} must be an object")
            inspection = fixtures["inspection"]
            if not isinstance(inspection, dict) or inspection.get("hold_stock") is not True:
                raise ValueError(
                    f"Inspection fixture for account {index} must hold stock explicitly"
                )
            if not isinstance(fixtures["inspection_decision"], dict):
                raise ValueError(f"Inspection decision for account {index} must be an object")
            if not isinstance(fixtures["dispatch_request"], dict):
                raise ValueError(f"Dispatch request for account {index} must be an object")
            if not isinstance(fixtures["invoice"], dict):
                raise ValueError(f"Invoice fixture for account {index} must be an object")

        accounts.append(
            LoadAccount(
                email=email.strip(),
                password=password,
                write_fixtures=fixtures if isinstance(fixtures, dict) else None,
            )
        )

    if not accounts:
        raise ValueError("LOAD_ACCOUNTS_FILE must contain at least one account")
    return accounts


def validate_account_count(accounts: list[LoadAccount], target: int) -> None:
    if target > len(accounts):
        raise ValueError(
            f"{target} virtual users need {target} distinct accounts; "
            f"LOAD_ACCOUNTS_FILE contains {len(accounts)}"
        )
