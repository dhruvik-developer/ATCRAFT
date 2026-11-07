"""The write-fixture generator builds safe, one-use documents without a database."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from load_support import read_accounts
from make_write_fixtures import (
    REPO_ROOT,
    FixtureError,
    FixturePlan,
    assert_outside_repository,
    build_adjustment_payload,
    build_customer_payload,
    build_dispatch_fixture,
    build_dispatch_request_fixture,
    build_inspection_decision_fixture,
    build_inspection_fixture,
    build_invoice_fixture,
    build_item_payload,
    build_order_payload,
    build_reservation_fixture,
    build_template_payload,
    build_uom_payload,
    build_warehouse_payload,
    build_write_fixtures,
    ensure_below_threshold,
    format_decimal,
    load_account_rows,
    normalise_run_tag,
    parse_decimal,
)


def _fixtures(*, index: int = 1) -> dict[str, object]:
    item_id = f"item-{index}"
    warehouse_id = "warehouse-1"
    template_id = f"template-{index}"
    order_id = f"order-{index}"
    order_line_id = f"order-line-{index}"
    customer_id = f"customer-{index}"
    return build_write_fixtures(
        inventory_adjustment_id=f"adjustment-{index}",
        reservation=build_reservation_fixture(
            item_id=item_id, warehouse_id=warehouse_id, quantity=Decimal("1")
        ),
        inspection=build_inspection_fixture(
            item_id=item_id,
            warehouse_id=warehouse_id,
            template_id=template_id,
            quantity=Decimal("1"),
        ),
        inspection_decision=build_inspection_decision_fixture(quantity=Decimal("1")),
        dispatch=build_dispatch_fixture(
            sales_order_id=order_id,
            customer_id=customer_id,
            warehouse_id=warehouse_id,
            item_id=item_id,
            sales_order_line_id=order_line_id,
            quantity=Decimal("5"),
            dispatch_date="2026-11-07",
        ),
        dispatch_request=build_dispatch_request_fixture(),
        invoice=build_invoice_fixture(invoice_date="2026-11-07"),
    )


def test_plan_codes_fit_every_api_length_and_stay_unique() -> None:
    plan = FixturePlan.create(3, "ab12")

    assert plan.run_tag == "AB12"
    assert plan.warehouse_code == "LF-AB12-WH"
    assert len(plan.warehouse_code) <= 32
    assert len(plan.uom_code) <= 16
    assert len(set(plan.item_codes)) == 3
    assert len(set(plan.customer_codes)) == 3
    assert len(set(plan.template_codes)) == 3
    assert all(len(code) <= 64 for code in plan.item_codes)
    assert all(len(code) <= 32 for code in plan.customer_codes)
    assert all(len(code) <= 64 for code in plan.template_codes)


@pytest.mark.parametrize("tag", ["", "A-B", "TOOLONG99", "tag!"])
def test_run_tag_rejects_anything_that_cannot_prefix_a_code(tag: str) -> None:
    with pytest.raises(FixtureError, match="run tag"):
        normalise_run_tag(tag)


def test_plan_refuses_an_empty_account_list() -> None:
    with pytest.raises(FixtureError, match="At least one"):
        FixturePlan.create(0, "AB12")


def test_repository_paths_are_refused_for_credential_files(tmp_path: Path) -> None:
    with pytest.raises(FixtureError, match="outside the repository"):
        assert_outside_repository(REPO_ROOT / "accounts.json", label="account file")

    outside = assert_outside_repository(tmp_path / "accounts.json", label="account file")
    assert outside == (tmp_path / "accounts.json").resolve()


def test_threshold_guard_allows_the_threshold_but_not_a_cent_above() -> None:
    ensure_below_threshold(Decimal("10"), Decimal("10"), label="write-down")

    with pytest.raises(FixtureError, match="above the company's adjustment approval threshold"):
        ensure_below_threshold(Decimal("10.01"), Decimal("10"), label="write-down")


def test_decimal_helpers_accept_json_strings_and_numbers() -> None:
    assert parse_decimal("10.5", label="cost") == Decimal("10.5")
    assert parse_decimal(10, label="cost") == Decimal("10")
    assert format_decimal(Decimal("1.000")) == "1"
    assert format_decimal(Decimal("-1.500")) == "-1.5"

    with pytest.raises(FixtureError, match="not a number"):
        parse_decimal("not-a-number", label="cost")


def test_item_payload_is_sellable_and_priced_above_cost() -> None:
    payload = build_item_payload(
        "LF-AB12-I001",
        "Load item",
        uom_id="uom-1",
        standard_cost=Decimal("10"),
        sale_price=Decimal("20"),
    )

    assert payload["gst_rate"] == "0"
    assert payload["production_type"] == "BUY"
    assert payload["can_purchase"] is True
    assert payload["can_sell"] is True
    assert payload["can_be_spare"] is True
    assert payload["standard_cost"] == "10"
    assert payload["sale_price"] == "20"


def test_shared_master_payloads_satisfy_the_creation_contracts() -> None:
    warehouse = build_warehouse_payload("LF-AB12-WH", "Load warehouse")
    uom = build_uom_payload("LFAB12U", "Load unit")
    customer = build_customer_payload("LF-AB12-C001", "Load customer")
    template = build_template_payload("LF-AB12-T001", "Load template", "item-1")

    assert warehouse["warehouse_type"] == "FINISHED_GOODS"
    assert uom["uom_category"] == "COUNT"
    assert uom["is_base"] is False
    assert customer["credit_limit"] is None
    assert template["qc_stage"] == "FINAL"
    parameter = template["parameters"][0]
    assert isinstance(parameter, dict)
    assert parameter["is_mandatory"] is False


def test_adjustments_write_opening_stock_up_and_the_load_down() -> None:
    opening = build_adjustment_payload(
        warehouse_id="warehouse-1",
        item_id="item-1",
        quantity=Decimal("100"),
        reason_code="OPENING",
        reason="Opening stock",
        unit_cost=Decimal("10"),
    )
    load = build_adjustment_payload(
        warehouse_id="warehouse-1",
        item_id="item-1",
        quantity=Decimal("-1"),
        reason_code="COUNT_VARIANCE",
        reason="Load write-down",
    )

    opening_line = opening["lines"][0]
    load_line = load["lines"][0]
    assert isinstance(opening_line, dict)
    assert isinstance(load_line, dict)
    assert opening_line["quantity"] == "100"
    assert opening_line["unit_cost"] == "10"
    assert load_line["quantity"] == "-1"
    assert "unit_cost" not in load_line


def test_order_and_dispatch_name_the_same_sales_order_line() -> None:
    order = build_order_payload(
        customer_id="customer-1",
        warehouse_id="warehouse-1",
        item_id="item-1",
        quantity=Decimal("5"),
        required_date="2026-11-07",
    )
    order_line = order["lines"][0]
    assert isinstance(order_line, dict)
    assert order_line["quantity"] == "5"

    dispatch = build_dispatch_fixture(
        sales_order_id="order-1",
        customer_id="customer-1",
        warehouse_id="warehouse-1",
        item_id="item-1",
        sales_order_line_id="order-line-1",
        quantity=Decimal("5"),
        dispatch_date="2026-11-07",
    )
    dispatch_line = dispatch["lines"][0]
    assert isinstance(dispatch_line, dict)
    assert dispatch_line["sales_order_line_id"] == "order-line-1"
    assert dispatch_line["quantity"] == "5"


def test_inspection_holds_stock_and_needs_no_readings() -> None:
    inspection = build_inspection_fixture(
        item_id="item-1",
        warehouse_id="warehouse-1",
        template_id="template-1",
        quantity=Decimal("1"),
    )
    decision = build_inspection_decision_fixture(quantity=Decimal("1"))

    assert inspection["hold_stock"] is True
    assert inspection["qc_stage"] == "FINAL"
    assert decision["accepted_quantity"] == "1"


def test_generated_fixtures_round_trip_through_the_harness_validator(tmp_path: Path) -> None:
    rows = [
        {
            "email": f"load-{index}@example.test",
            "password": f"secret-{index}",
            "company_code": "ACME",
            "write_fixtures": _fixtures(index=index),
        }
        for index in (1, 2)
    ]
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(rows), encoding="utf-8")

    accounts = read_accounts(path, require_write_fixtures=True)

    assert [account.email for account in accounts] == [
        "load-1@example.test",
        "load-2@example.test",
    ]
    assert "secret-1" not in repr(accounts[0])
    assert [account.write_fixtures["inventory_adjustment_id"] for account in accounts] == [
        "adjustment-1",
        "adjustment-2",
    ]
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten[0]["company_code"] == "ACME"


def test_account_rows_with_existing_fixtures_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            [
                {
                    "email": "load@example.test",
                    "password": "secret",
                    "write_fixtures": _fixtures(),
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(FixtureError, match="already has write_fixtures"):
        load_account_rows(path)


def test_account_file_with_a_utf8_bom_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps([{"email": "load@example.test", "password": "secret"}]),
        encoding="utf-8-sig",
    )

    rows = load_account_rows(path)

    assert rows[0]["email"] == "load@example.test"
