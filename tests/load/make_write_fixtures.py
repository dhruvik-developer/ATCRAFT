"""Create one-use fixtures for the Locust write profile.

``LOAD_WRITE_ENABLED=true`` makes every load account post a stock adjustment,
cycle a reservation, raise and decide a QC inspection, dispatch a prepared
sales order and invoice it. Each of those steps consumes records that cannot be
reused, so a load run needs its own fixture set and an isolated target.

This script creates that set against a running API:

* one shared warehouse and unit of measure for the whole run;
* per account, a dedicated item, customer, QC template and opening stock;
* a draft adjustment the load profile posts;
* a confirmed, fully allocated sales order the profile raises its consignment
  against; and
* the inspection, reservation, dispatch and invoice payloads the profile sends.

It is destructive by design. Without ``--apply`` it only prints the codes it
would create and makes no HTTP calls. With ``--apply`` it logs in, verifies
every load account first, then creates the fixtures and rewrites the account
file (or ``--output``) with a ``write_fixtures`` object per account.

The account file contains passwords, so both it and the output must live
outside this repository. Keep ``LOAD_ACCOUNTS_FILE`` pointing at the file this
script writes.

Typical isolated-target use::

    $env:LOAD_ADMIN_EMAIL = 'admin@atcraft.test'
    $env:LOAD_ADMIN_PASSWORD = '...'
    python tests/load/make_write_fixtures.py \
        --accounts C:\\secure\\load-accounts.json --apply

Then, in the same shell::

    $env:LOAD_ACCOUNTS_FILE = 'C:\\secure\\load-accounts.json'
    $env:LOAD_WRITE_ENABLED = 'true'
    locust -f tests/load/locustfile.py --host http://load-target

Never point this script at ``atcraft_erp`` or ``atcraft_erp_test``. It posts
stock, confirms orders and raises QC holds; use a throwaway database or the
client's isolated load target.
"""

from __future__ import annotations

# A command-line tool reports through stdout; that is its interface.
# ruff: noqa: T201
import argparse
import getpass
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from load_support import validate_account_payload

API_PREFIX = "/api/v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_TAG_PATTERN = re.compile(r"^[A-Z0-9]{1,8}$")
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_LOGIN_DELAY_SECONDS = 0.1


class FixtureError(Exception):
    """A fixture could not be built safely."""


class ApiError(FixtureError):
    """The API refused a request."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: object = None,
        request_id: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.request_id = request_id
        suffix = f" (request {request_id})" if request_id else ""
        super().__init__(f"HTTP {status} {code}: {message}{suffix}")


@dataclass(frozen=True)
class FixturePlan:
    """The codes one generator run will use."""

    run_tag: str
    warehouse_code: str
    warehouse_name: str
    uom_code: str
    uom_name: str
    item_codes: tuple[str, ...]
    customer_codes: tuple[str, ...]
    template_codes: tuple[str, ...]

    @classmethod
    def create(cls, account_count: int, run_tag: str) -> FixturePlan:
        if account_count < 1:
            raise FixtureError("At least one load account is required.")
        tag = normalise_run_tag(run_tag)
        return cls(
            run_tag=tag,
            warehouse_code=f"LF-{tag}-WH",
            warehouse_name=f"Load fixtures {tag} warehouse",
            uom_code=f"LF{tag}U",
            uom_name=f"Load unit {tag}",
            item_codes=tuple(f"LF-{tag}-I{index:03d}" for index in range(1, account_count + 1)),
            customer_codes=tuple(f"LF-{tag}-C{index:03d}" for index in range(1, account_count + 1)),
            template_codes=tuple(f"LF-{tag}-T{index:03d}" for index in range(1, account_count + 1)),
        )


# -----------------------------------------------------------------------------
# Input and path guards
# -----------------------------------------------------------------------------


def normalise_run_tag(value: str) -> str:
    tag = value.strip().upper()
    if not RUN_TAG_PATTERN.fullmatch(tag):
        raise FixtureError(
            "The run tag must be 1-8 letters or digits, with no punctuation, so it can prefix "
            "warehouse, unit, item, customer and template codes."
        )
    return tag


def new_run_tag() -> str:
    return secrets.token_hex(3).upper()


def assert_outside_repository(path: Path, *, label: str) -> Path:
    """Refuse credential-bearing files under the repository."""
    resolved = path.expanduser().resolve()
    root = REPO_ROOT.resolve()
    resolved_text = os.path.normcase(str(resolved))
    root_text = os.path.normcase(str(root))
    if resolved_text == root_text or resolved_text.startswith(root_text + os.sep):
        raise FixtureError(
            f"The {label} must stay outside the repository: {resolved}. "
            "Account files contain passwords and must never be committed."
        )
    return resolved


def load_account_rows(path: Path) -> list[dict[str, Any]]:
    try:
        # Accept the BOM that Windows editors add to UTF-8 JSON.
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise FixtureError("The account file is not valid JSON.") from exc
    except OSError as exc:
        raise FixtureError(f"Could not read the account file: {exc}") from exc
    if not isinstance(payload, list):
        raise FixtureError("The account file must contain a JSON array.")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            raise FixtureError(f"Account entry {index} must be an object.")
        if row.get("write_fixtures") is not None:
            raise FixtureError(
                f"Account entry {index} already has write_fixtures. They are one-use records: "
                "use a fresh account file, or remove the old fixtures deliberately if you are "
                "discarding the previous load run."
            )
        rows.append(row)
    return rows


# -----------------------------------------------------------------------------
# Decimal and threshold helpers
# -----------------------------------------------------------------------------


def parse_decimal(value: object, *, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FixtureError(f"{label} is not a number: {value!r}") from exc
    if not parsed.is_finite():
        raise FixtureError(f"{label} must be finite.")
    return parsed


def format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def ensure_below_threshold(value: Decimal, threshold: Decimal, *, label: str) -> None:
    """Refuse a posting that would need the approval workflow.

    The write profile posts its prepared adjustment directly. It cannot approve
    one, and the opening stock must land before an order can be confirmed, so a
    value strictly above the company's threshold would leave half-built
    fixtures behind.
    """
    if value > threshold:
        raise FixtureError(
            f"{label} is {value}, above the company's adjustment approval threshold "
            f"{threshold}. The load profile cannot run the approval workflow. Lower the "
            "fixture quantities or cost, or raise the threshold on the isolated target."
        )


def read_adjustment_threshold(client: ApiClient, token: str) -> Decimal:
    data = client.get("/audit/approval-settings", token=token)
    if not isinstance(data, Mapping) or "adjustment_approval_threshold" not in data:
        raise FixtureError(
            "The approval-settings response did not contain the adjustment threshold."
        )
    return parse_decimal(data["adjustment_approval_threshold"], label="adjustment threshold")


# -----------------------------------------------------------------------------
# Payload builders (pure, so they can be unit tested without a database)
# -----------------------------------------------------------------------------


def build_warehouse_payload(code: str, name: str) -> dict[str, object]:
    return {
        "code": code,
        "name": name,
        "warehouse_type": "FINISHED_GOODS",
        "is_active": True,
    }


def build_uom_payload(code: str, name: str) -> dict[str, object]:
    # A non-base unit avoids the one-base-per-category rule while still being a
    # valid stocking unit for the item.
    return {
        "code": code,
        "name": name,
        "uom_category": "COUNT",
        "is_base": False,
        "decimal_places": 0,
        "is_active": True,
    }


def build_item_payload(
    code: str,
    name: str,
    *,
    uom_id: str,
    standard_cost: Decimal,
    sale_price: Decimal,
) -> dict[str, object]:
    return {
        "code": code,
        "name": name,
        "item_group": "FINISHED_GOOD",
        "base_uom_id": uom_id,
        "production_type": "BUY",
        "can_purchase": True,
        "can_sell": True,
        # The write profile reserves the item as SPARE demand; the allocation
        # engine refuses SPARE for an item without this flag.
        "can_be_spare": True,
        "gst_rate": "0",
        "standard_cost": format_decimal(standard_cost),
        "sale_price": format_decimal(sale_price),
        "is_active": True,
    }


def build_customer_payload(code: str, name: str) -> dict[str, object]:
    return {
        "code": code,
        "name": name,
        "customer_type": "CUSTOMER",
        "credit_limit": None,
        "credit_days": 0,
        "is_active": True,
    }


def build_template_payload(code: str, name: str, item_id: str) -> dict[str, object]:
    # One optional TEXT parameter: the inspection is decidable without sending
    # readings, which keeps the write profile to the documents it means to test.
    return {
        "code": code,
        "name": name,
        "item_id": item_id,
        "qc_stage": "FINAL",
        "parameters": [
            {
                "sequence": 1,
                "parameter_name": "Appearance",
                "parameter_type": "TEXT",
                "is_critical": False,
                "is_mandatory": False,
            }
        ],
    }


def build_adjustment_payload(
    *,
    warehouse_id: str,
    item_id: str,
    quantity: Decimal,
    reason_code: str,
    reason: str,
    unit_cost: Decimal | None = None,
    adjustment_date: str | None = None,
) -> dict[str, object]:
    line: dict[str, object] = {
        "item_id": item_id,
        "quantity": format_decimal(quantity),
    }
    if unit_cost is not None:
        line["unit_cost"] = format_decimal(unit_cost)
    payload: dict[str, object] = {
        "warehouse_id": warehouse_id,
        "reason_code": reason_code,
        "reason": reason,
        "lines": [line],
    }
    if adjustment_date is not None:
        payload["adjustment_date"] = adjustment_date
    return payload


def build_order_payload(
    *,
    customer_id: str,
    warehouse_id: str,
    item_id: str,
    quantity: Decimal,
    required_date: str,
) -> dict[str, object]:
    return {
        "customer_id": customer_id,
        "required_date": required_date,
        "warehouse_id": warehouse_id,
        "lines": [
            {
                "line_number": 1,
                "item_id": item_id,
                "quantity": format_decimal(quantity),
            }
        ],
    }


def build_write_fixtures(
    *,
    inventory_adjustment_id: str,
    reservation: dict[str, object],
    inspection: dict[str, object],
    inspection_decision: dict[str, object],
    dispatch: dict[str, object],
    dispatch_request: dict[str, object],
    invoice: dict[str, object],
) -> dict[str, object]:
    """Assemble the contract ``locustfile.WriteUser`` consumes."""
    return {
        "inventory_adjustment_id": inventory_adjustment_id,
        "reservation": reservation,
        "inspection": inspection,
        "inspection_decision": inspection_decision,
        "dispatch": dispatch,
        "dispatch_request": dispatch_request,
        "invoice": invoice,
    }


def build_reservation_fixture(
    *, item_id: str, warehouse_id: str, quantity: Decimal
) -> dict[str, object]:
    # The harness injects a fresh demand_id per reservation; the load profile
    # only needs a valid payload it can replay.
    return {
        "item_id": item_id,
        "warehouse_id": warehouse_id,
        "quantity": format_decimal(quantity),
        "demand_type": "SPARE",
        "reason": "Load profile reservation cycle",
    }


def build_inspection_fixture(
    *,
    item_id: str,
    warehouse_id: str,
    template_id: str,
    quantity: Decimal,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "warehouse_id": warehouse_id,
        "template_id": template_id,
        "qc_stage": "FINAL",
        "inspected_quantity": format_decimal(quantity),
        "hold_stock": True,
        "reason": "Load profile final inspection",
    }


def build_inspection_decision_fixture(*, quantity: Decimal) -> dict[str, object]:
    return {
        "accepted_quantity": format_decimal(quantity),
        "reason": "Load profile accepts the inspected quantity",
    }


def build_dispatch_fixture(
    *,
    sales_order_id: str,
    customer_id: str,
    warehouse_id: str,
    item_id: str,
    sales_order_line_id: str,
    quantity: Decimal,
    dispatch_date: str,
) -> dict[str, object]:
    return {
        "warehouse_id": warehouse_id,
        "dispatch_date": dispatch_date,
        "customer_id": customer_id,
        "sales_order_id": sales_order_id,
        "lines": [
            {
                "item_id": item_id,
                "quantity": format_decimal(quantity),
                "sales_order_line_id": sales_order_line_id,
            }
        ],
    }


def build_dispatch_request_fixture() -> dict[str, object]:
    return {"reason": "Load profile dispatches the prepared order"}


def build_invoice_fixture(*, invoice_date: str) -> dict[str, object]:
    return {"invoice_date": invoice_date}


# -----------------------------------------------------------------------------
# HTTP client
# -----------------------------------------------------------------------------


class ApiClient:
    """Small stdlib JSON client with 429 retry and no credential logging."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def get(self, path: str, *, token: str) -> dict[str, Any]:
        return self._request("GET", path, token=token, expected=(200,))

    def post(
        self,
        path: str,
        body: Mapping[str, object] | None,
        *,
        token: str,
        expected: tuple[int, ...] = (200, 201),
    ) -> dict[str, Any]:
        return self._request("POST", path, token=token, body=body, expected=expected)

    def login(
        self,
        email: str,
        password: str,
        *,
        company_code: str | None = None,
    ) -> str:
        body: dict[str, object] = {"email": email, "password": password}
        if company_code:
            body["company_code"] = company_code
        data = self._request("POST", "/auth/login", body=body, expected=(200,))
        user = data.get("user")
        token = data.get("access_token")
        if not isinstance(user, Mapping):
            raise FixtureError("The login response did not contain a user.")
        if user.get("must_change_password"):
            raise FixtureError(
                f"{email} is on a temporary password; change it before using the account for "
                "load generation."
            )
        if not isinstance(token, str) or not token:
            if data.get("mfa_required"):
                raise FixtureError(f"{email} requires MFA; the load harness cannot complete it.")
            raise FixtureError("The login response did not contain an access token.")
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: Mapping[str, object] | None = None,
        expected: tuple[int, ...],
    ) -> dict[str, Any]:
        url = f"{self.base_url}{API_PREFIX}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        attempt = 0
        while True:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status = response.status
                    request_id = response.headers.get("X-Request-ID")
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                status = exc.code
                request_id = exc.headers.get("X-Request-ID")
                raw = exc.read()
                if status == 429 and attempt < self.max_retries:
                    attempt += 1
                    delay = _retry_after_seconds(exc.headers.get("Retry-After"), attempt)
                    print(f"Rate limited by the API; retrying in {delay:.1f}s ...")
                    time.sleep(delay)
                    continue
                raise _api_error(status, raw, request_id=request_id) from exc
            except urllib.error.URLError as exc:
                raise FixtureError(f"Could not reach {url}: {exc.reason}") from exc
            break

        if status not in expected:
            raise _api_error(status, raw, request_id=request_id)
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{method} {path} returned a non-JSON response.") from exc
        if not isinstance(envelope, Mapping) or envelope.get("success") is not True:
            raise FixtureError(f"{method} {path} did not return a success envelope.")
        payload = envelope.get("data")
        if not isinstance(payload, dict):
            raise FixtureError(f"{method} {path} returned no data object.")
        return payload


def _retry_after_seconds(value: str | None, attempt: int) -> float:
    if value is not None:
        try:
            return min(max(float(value), 0.0), 30.0)
        except ValueError:
            pass
    return min(2.0**attempt, 30.0)


def _api_error(status: int, raw: bytes, *, request_id: str | None) -> ApiError:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            return ApiError(
                status,
                str(error.get("code") or "UNKNOWN"),
                str(error.get("message") or "The API refused the request."),
                details=error.get("details"),
                request_id=request_id or _optional_str(payload.get("request_id")),
            )
    return ApiError(
        status,
        "UNKNOWN",
        "The API returned an error without a readable envelope.",
        request_id=request_id,
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------


def _require_id(data: Mapping[str, object], resource: str) -> str:
    value = data.get("id")
    if not isinstance(value, str) or not value:
        raise FixtureError(f"The {resource} response did not contain an id.")
    return value


def _require_bool(data: Mapping[str, object], key: str, resource: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise FixtureError(f"The {resource} response did not contain {key!r}.")
    return value


def _post_adjustment(
    client: ApiClient,
    token: str,
    payload: Mapping[str, object],
    *,
    label: str,
    threshold: Decimal,
) -> dict[str, Any]:
    created = client.post("/inventory/adjustments", payload, token=token)
    if _require_bool(created, "requires_approval", label):
        total = created.get("total_value", "an unknown value")
        raise FixtureError(
            f"{label} totals {total}, above the adjustment approval threshold {threshold}. "
            "The load profile posts directly and cannot approve."
        )
    return created


def create_shared_masters(client: ApiClient, token: str, plan: FixturePlan) -> tuple[str, str]:
    warehouse = client.post(
        "/warehouses",
        build_warehouse_payload(plan.warehouse_code, plan.warehouse_name),
        token=token,
    )
    warehouse_id = _require_id(warehouse, "warehouse")
    uom = client.post(
        "/products/uoms",
        build_uom_payload(plan.uom_code, plan.uom_name),
        token=token,
    )
    uom_id = _require_id(uom, "unit of measure")
    return warehouse_id, uom_id


def create_account_fixtures(
    client: ApiClient,
    token: str,
    *,
    plan: FixturePlan,
    index: int,
    warehouse_id: str,
    uom_id: str,
    opening_quantity: Decimal,
    order_quantity: Decimal,
    write_down_quantity: Decimal,
    inspection_quantity: Decimal,
    reservation_quantity: Decimal,
    item_cost: Decimal,
    sale_price: Decimal,
    threshold: Decimal,
) -> tuple[dict[str, object], dict[str, str]]:
    """Create one account's dedicated records and return its write fixtures."""
    item_code = plan.item_codes[index - 1]
    customer_code = plan.customer_codes[index - 1]
    template_code = plan.template_codes[index - 1]
    today = datetime.now(tz=UTC).date().isoformat()
    created: dict[str, str] = {"item": item_code, "customer": customer_code}

    item = client.post(
        "/products/items",
        build_item_payload(
            item_code,
            f"Load item {item_code}",
            uom_id=uom_id,
            standard_cost=item_cost,
            sale_price=sale_price,
        ),
        token=token,
        expected=(201,),
    )
    item_id = _require_id(item, "item")

    customer = client.post(
        "/customers",
        build_customer_payload(customer_code, f"Load customer {customer_code}"),
        token=token,
        expected=(201,),
    )
    customer_id = _require_id(customer, "customer")

    template = client.post(
        "/quality/templates",
        build_template_payload(template_code, f"Load template {template_code}", item_id),
        token=token,
        expected=(201,),
    )
    template_id = _require_id(template, "quality template")

    opening = _post_adjustment(
        client,
        token,
        build_adjustment_payload(
            warehouse_id=warehouse_id,
            item_id=item_id,
            quantity=opening_quantity,
            reason_code="OPENING",
            reason=f"Load fixture opening stock for {item_code}",
            unit_cost=item_cost,
            adjustment_date=today,
        ),
        label="Opening stock adjustment",
        threshold=threshold,
    )
    opening_id = _require_id(opening, "opening adjustment")
    posted = client.post(f"/inventory/adjustments/{opening_id}/post", None, token=token)
    if posted.get("status") != "POSTED":
        raise FixtureError(f"Opening adjustment {opening_id} did not reach POSTED.")

    write_adjustment = _post_adjustment(
        client,
        token,
        build_adjustment_payload(
            warehouse_id=warehouse_id,
            item_id=item_id,
            quantity=-write_down_quantity,
            reason_code="COUNT_VARIANCE",
            reason=f"Load profile write-down for {item_code}",
            adjustment_date=today,
        ),
        label="Load write-down adjustment",
        threshold=threshold,
    )
    write_adjustment_id = _require_id(write_adjustment, "load adjustment")

    order = client.post(
        "/sales",
        build_order_payload(
            customer_id=customer_id,
            warehouse_id=warehouse_id,
            item_id=item_id,
            quantity=order_quantity,
            required_date=today,
        ),
        token=token,
        expected=(201,),
    )
    order_id = _require_id(order, "sales order")
    if _require_bool(order, "requires_approval", "sales order"):
        raise FixtureError(
            f"Sales order {order_id} needs below-cost approval. Raise --sale-price above "
            "--item-cost."
        )

    confirmation = client.post(
        f"/sales/{order_id}/confirm",
        {"reason": "Load fixture confirms the order"},
        token=token,
    )
    allocation = confirmation.get("allocation")
    if not isinstance(allocation, Mapping):
        raise FixtureError(f"Sales order {order_id} confirmation returned no allocation.")
    if allocation.get("fully_allocated") is not True:
        allocation = client.post(
            f"/sales/{order_id}/allocate",
            {"reason": "Load fixture tops up the order"},
            token=token,
        )
    if allocation.get("fully_allocated") is not True:
        raise FixtureError(
            f"Sales order {order_id} is not fully allocated; raise --opening-quantity."
        )
    allocation_lines = allocation.get("lines")
    if not isinstance(allocation_lines, list) or not allocation_lines:
        raise FixtureError(f"Sales order {order_id} has no allocation lines.")
    line = allocation_lines[0]
    if not isinstance(line, Mapping):
        raise FixtureError(f"Sales order {order_id} returned a malformed allocation line.")
    order_line_id = line.get("line_id")
    if not isinstance(order_line_id, str) or not order_line_id:
        raise FixtureError(f"Sales order {order_id} allocation did not contain a line id.")
    dispatch_quantity = parse_decimal(line.get("dispatchable_quantity"), label="dispatch quantity")
    if dispatch_quantity <= 0:
        raise FixtureError(f"Sales order {order_id} has nothing dispatchable.")

    order_status = allocation.get("status")
    if order_status not in ("ALLOCATED", "PARTIALLY_DISPATCHED"):
        raise FixtureError(f"Sales order {order_id} is {order_status}, not ready to dispatch.")
    # The load profile raises its own consignment from the payload below. A plan
    # created here would count against the order line's planned quantity and
    # leave the load-time POST /dispatch with nothing to ship.
    created.update({"sales_order": order_id, "adjustment": write_adjustment_id})

    fixtures = build_write_fixtures(
        inventory_adjustment_id=write_adjustment_id,
        reservation=build_reservation_fixture(
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=reservation_quantity,
        ),
        inspection=build_inspection_fixture(
            item_id=item_id,
            warehouse_id=warehouse_id,
            template_id=template_id,
            quantity=inspection_quantity,
        ),
        inspection_decision=build_inspection_decision_fixture(quantity=inspection_quantity),
        dispatch=build_dispatch_fixture(
            sales_order_id=order_id,
            customer_id=customer_id,
            warehouse_id=warehouse_id,
            item_id=item_id,
            sales_order_line_id=order_line_id,
            quantity=dispatch_quantity,
            dispatch_date=today,
        ),
        dispatch_request=build_dispatch_request_fixture(),
        invoice=build_invoice_fixture(invoice_date=today),
    )
    return fixtures, created


def _apply(
    args: argparse.Namespace,
    rows: list[dict[str, Any]],
    plan: FixturePlan,
    *,
    base_url: str,
) -> None:
    run_tag = plan.run_tag
    opening_quantity = parse_decimal(args.opening_quantity, label="opening quantity")
    order_quantity = parse_decimal(args.order_quantity, label="order quantity")
    write_down_quantity = parse_decimal(args.write_down_quantity, label="write-down quantity")
    item_cost = parse_decimal(args.item_cost, label="item cost")
    sale_price = parse_decimal(args.sale_price, label="sale price")
    inspection_quantity = Decimal("1")
    reservation_quantity = Decimal("1")

    if opening_quantity <= 0 or order_quantity <= 0 or write_down_quantity <= 0:
        raise FixtureError("Opening, order and write-down quantities must all be positive.")
    if item_cost < 0 or sale_price <= 0:
        raise FixtureError("Item cost cannot be negative and sale price must be positive.")
    if sale_price <= item_cost:
        raise FixtureError("--sale-price must be above --item-cost.")
    committed = order_quantity + write_down_quantity + inspection_quantity + reservation_quantity
    if opening_quantity < committed:
        raise FixtureError(
            f"--opening-quantity must be at least {committed}: the order reserves "
            f"{order_quantity}, the load adjustment writes down {write_down_quantity}, and the "
            "inspection and reservation each hold one unit."
        )

    admin_email = args.admin_email or os.getenv("LOAD_ADMIN_EMAIL")
    if not admin_email:
        raise FixtureError("Set --admin-email or LOAD_ADMIN_EMAIL.")
    admin_password = os.getenv("LOAD_ADMIN_PASSWORD")
    if not admin_password:
        if not sys.stdin.isatty():
            raise FixtureError("Set LOAD_ADMIN_PASSWORD when no terminal is available.")
        admin_password = getpass.getpass(f"Password for {admin_email}: ")

    client = ApiClient(base_url, timeout=args.timeout)
    print(f"Logging in as {admin_email} ...")
    admin_token = client.login(admin_email, admin_password, company_code=args.company_code)
    threshold = read_adjustment_threshold(client, admin_token)
    opening_value = opening_quantity * item_cost
    write_down_value = write_down_quantity * item_cost
    ensure_below_threshold(write_down_value, threshold, label="The load write-down")
    ensure_below_threshold(opening_value, threshold, label="The opening stock")
    print(
        f"Adjustment threshold {threshold}; opening value {opening_value}, "
        f"write-down value {write_down_value}."
    )

    if not args.skip_login_check:
        print(f"Verifying {len(rows)} load account login(s) before creating anything ...")
        for index, row in enumerate(rows, start=1):
            email = str(row["email"])
            client.login(
                email,
                str(row["password"]),
                company_code=args.company_code,
            )
            if args.login_delay > 0 and index < len(rows):
                time.sleep(args.login_delay)
        print("All load accounts can log in.")

    print(f"Creating shared warehouse {plan.warehouse_code} and unit {plan.uom_code} ...")
    warehouse_id, uom_id = create_shared_masters(client, admin_token, plan)

    for index, row in enumerate(rows, start=1):
        email = str(row["email"])
        print(
            f"[{index}/{len(rows)}] {email}: "
            f"{plan.item_codes[index - 1]}, {plan.customer_codes[index - 1]}, "
            f"{plan.template_codes[index - 1]} ..."
        )
        fixtures, created = create_account_fixtures(
            client,
            admin_token,
            plan=plan,
            index=index,
            warehouse_id=warehouse_id,
            uom_id=uom_id,
            opening_quantity=opening_quantity,
            order_quantity=order_quantity,
            write_down_quantity=write_down_quantity,
            inspection_quantity=inspection_quantity,
            reservation_quantity=reservation_quantity,
            item_cost=item_cost,
            sale_price=sale_price,
            threshold=threshold,
        )
        row["write_fixtures"] = fixtures
        print(
            "    created "
            + ", ".join(f"{key} {value}" for key, value in created.items())
            + f" (threshold {threshold})"
        )

    validate_account_payload(rows, require_write_fixtures=True)
    output = Path(args.output or args.accounts)
    _write_rows(output, rows)
    print(
        f"\nWrote write fixtures for {len(rows)} accounts to {output}.\n"
        f"Run tag {run_tag}: every record created by this run is identifiable by its code.\n"
        "Point LOAD_ACCOUNTS_FILE at this file and set LOAD_WRITE_ENABLED=true."
    )


def _write_rows(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    payload = json.dumps(rows, indent=2) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _dry_run(rows: Sequence[Mapping[str, object]], plan: FixturePlan) -> None:
    print("Dry run: no API calls will be made and no file will be written.")
    print(f"Accounts: {len(rows)}")
    print(f"Run tag: {plan.run_tag}")
    print(f"Warehouse: {plan.warehouse_code}")
    print(f"Unit of measure: {plan.uom_code}")
    for index, row in enumerate(rows, start=1):
        print(
            f"  {row['email']}: item {plan.item_codes[index - 1]}, "
            f"customer {plan.customer_codes[index - 1]}, "
            f"template {plan.template_codes[index - 1]}"
        )
    print("Re-run with --apply to create these fixtures.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create one-use fixtures for the Locust write profile. The script is destructive: "
            "use an isolated target, and only add --apply when you mean it."
        )
    )
    parser.add_argument(
        "--accounts",
        required=True,
        type=Path,
        help="JSON array of {email, password} load accounts, outside the repository.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Where to write the enriched account file. Defaults to --accounts.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("LOAD_BASE_URL", "http://127.0.0.1:8000"),
        help="API origin, without the /api/v1 suffix.",
    )
    parser.add_argument(
        "--admin-email",
        default=os.getenv("LOAD_ADMIN_EMAIL"),
        help="Account that may create masters and fixtures. Defaults to LOAD_ADMIN_EMAIL.",
    )
    parser.add_argument(
        "--company-code",
        default=None,
        help="Company code, when the login needs one.",
    )
    parser.add_argument("--run-tag", default=None, help="1-8 letters/digits; random by default.")
    parser.add_argument("--opening-quantity", default="100")
    parser.add_argument("--order-quantity", default="5")
    parser.add_argument("--write-down-quantity", default="1")
    parser.add_argument("--item-cost", default="10")
    parser.add_argument("--sale-price", default="20")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--login-delay",
        type=float,
        default=DEFAULT_LOGIN_DELAY_SECONDS,
        help="Seconds between load-account login checks, to stay clear of rate limits.",
    )
    parser.add_argument(
        "--skip-login-check",
        action="store_true",
        help="Do not verify every load account before creating fixtures. Not recommended.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually create records. Without it the script only prints its plan.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        accounts_path = assert_outside_repository(args.accounts, label="account file")
        output = args.output or args.accounts
        output_path = assert_outside_repository(output, label="output file")
        rows = load_account_rows(accounts_path)
        validate_account_payload(rows, require_write_fixtures=False)
        plan = FixturePlan.create(len(rows), args.run_tag or new_run_tag())
        if not args.apply:
            _dry_run(rows, plan)
            return 0
        args.output = output_path
        base_url = str(args.base_url).rstrip("/")
        _apply(args, rows, plan, base_url=base_url)
        return 0
    except (FixtureError, OSError, ValueError) as exc:
        print(f"\nFixture generation failed: {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
