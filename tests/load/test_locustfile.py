"""The load harness refuses shared identities and incomplete write fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from load_support import LoadAccount, read_accounts, validate_account_count


def _write_fixtures(order_id: str = "order-1", adjustment_id: str = "adjustment-1") -> dict:
    return {
        "inventory_adjustment_id": adjustment_id,
        "reservation": {"item_id": "item-1", "warehouse_id": "warehouse-1"},
        "inspection": {"item_id": "item-1", "warehouse_id": "warehouse-1", "hold_stock": True},
        "inspection_decision": {"accepted_quantity": "1", "reason": "Load test acceptance"},
        "dispatch": {"sales_order_id": order_id, "lines": []},
        "dispatch_request": {"reason": "Load test dispatch"},
        "invoice": {"invoice_date": "2026-01-01"},
    }


def _write_file(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_account_file_requires_distinct_emails_and_hides_passwords(tmp_path: Path) -> None:
    path = _write_file(
        tmp_path / "accounts.json",
        [
            {"email": "load-1@example.test", "password": "secret-1"},
            {"email": "load-2@example.test", "password": "secret-2"},
        ],
    )

    accounts = read_accounts(path, require_write_fixtures=False)

    assert [account.email for account in accounts] == [
        "load-1@example.test",
        "load-2@example.test",
    ]
    assert "secret-1" not in repr(accounts[0])


def test_duplicate_emails_are_rejected_case_insensitively(tmp_path: Path) -> None:
    path = _write_file(
        tmp_path / "accounts.json",
        [
            {"email": "load@example.test", "password": "first"},
            {"email": "LOAD@example.test", "password": "second"},
        ],
    )

    with pytest.raises(ValueError, match="Duplicate email"):
        read_accounts(path, require_write_fixtures=False)


def test_write_profile_requires_distinct_inventory_and_order_fixtures(tmp_path: Path) -> None:
    path = _write_file(
        tmp_path / "accounts.json",
        [
            {
                "email": "load-1@example.test",
                "password": "secret-1",
                "write_fixtures": _write_fixtures(),
            },
            {
                "email": "load-2@example.test",
                "password": "secret-2",
                "write_fixtures": _write_fixtures(),
            },
        ],
    )

    with pytest.raises(ValueError, match="Inventory adjustment ids must be unique"):
        read_accounts(path, require_write_fixtures=True)


def test_write_profile_rejects_duplicate_sales_order_ids(tmp_path: Path) -> None:
    path = _write_file(
        tmp_path / "accounts.json",
        [
            {
                "email": "load-1@example.test",
                "password": "secret-1",
                "write_fixtures": _write_fixtures("order-1", "adjustment-1"),
            },
            {
                "email": "load-2@example.test",
                "password": "secret-2",
                "write_fixtures": _write_fixtures("ORDER-1", "adjustment-2"),
            },
        ],
    )

    with pytest.raises(ValueError, match="Sales order ids must be unique"):
        read_accounts(path, require_write_fixtures=True)


def test_write_profile_rejects_missing_or_non_holding_qc_fixture(tmp_path: Path) -> None:
    fixtures = _write_fixtures()
    fixtures["inspection"] = {"item_id": "item-1", "hold_stock": False}
    path = _write_file(
        tmp_path / "accounts.json",
        [
            {
                "email": "load@example.test",
                "password": "secret",
                "write_fixtures": fixtures,
            }
        ],
    )

    with pytest.raises(ValueError, match="must hold stock explicitly"):
        read_accounts(path, require_write_fixtures=True)


def test_account_pool_must_cover_all_local_virtual_users() -> None:
    accounts = [LoadAccount("load@example.test", "secret")]

    with pytest.raises(ValueError, match="2 virtual users need 2 distinct accounts"):
        validate_account_count(accounts, 2)


def test_load_account_password_is_not_in_its_representation() -> None:
    account = LoadAccount("load@example.test", "do-not-print")

    assert "do-not-print" not in repr(account)
