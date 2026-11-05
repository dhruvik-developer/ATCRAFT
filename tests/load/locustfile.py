"""Load profile for the Atcraft ERP API (master rules sections 57, 66).

Purpose: replace the *engineering targets* in docs/performance.md with numbers
measured against realistic data. Targets that have never been load-tested are
aspirations.

Run against a populated, isolated load environment. Every simulated user needs
its own account in ``LOAD_ACCOUNTS_FILE``; the file must stay outside the
repository. See docs/performance.md for setup and write-fixture requirements::

    locust -f tests/load/locustfile.py --host http://localhost \\
           --users 50 --spawn-rate 5 --run-time 10m

Headless smoke::

    locust -f tests/load/locustfile.py --headless -u 50 -r 5 -t 2m --host http://localhost

The task weights approximate a real working day: people read stock and orders far
more than they write them, and the writes that do happen are concentrated in
receipts and issues. Set ``LOAD_WRITE_ENABLED=true`` to include the isolated
write workflow; it requires dedicated, one-use fixtures per account.
"""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
from uuid import uuid4

from load_support import LoadAccount, read_accounts, validate_account_count
from locust import HttpUser, between, events, task
from locust.runners import MasterRunner, WorkerRunner

API = "/api/v1"
_random = random.Random()  # noqa: S311 - request sampling does not need cryptographic randomness


_accounts: Queue[LoadAccount] = Queue()


def _write_profile_enabled() -> bool:
    value = os.getenv("LOAD_WRITE_ENABLED", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("LOAD_WRITE_ENABLED must be true or false")
    return value == "true"


WRITE_ENABLED = _write_profile_enabled()


def _target_user_count(environment) -> int:
    target = getattr(environment.runner, "target_user_count", 0)
    if target:
        return int(target)
    options = getattr(environment, "parsed_options", None)
    return int(getattr(options, "num_users", 0) or 0)


@events.test_start.add_listener
def _prepare_accounts(environment, **_kwargs):
    """Give every simulated user a different account in this Locust process."""
    global _accounts
    _accounts = Queue()
    if isinstance(environment.runner, MasterRunner):
        return
    try:
        account_path = os.getenv("LOAD_ACCOUNTS_FILE")
        if not account_path:
            raise ValueError("Set LOAD_ACCOUNTS_FILE to a JSON array of dedicated test accounts")
        accounts = read_accounts(Path(account_path), require_write_fixtures=WRITE_ENABLED)
        if not isinstance(environment.runner, WorkerRunner):
            target = _target_user_count(environment)
            if target:
                validate_account_count(accounts, target)
    except (OSError, ValueError) as exc:
        print(f"\nLoad profile setup failed: {exc}\n")  # noqa: T201
        environment.process_exit_code = 1
        environment.runner.quit()
        return

    _accounts = Queue()
    for account in accounts:
        _accounts.put(account)


def _claim_account(environment) -> LoadAccount:
    try:
        return _accounts.get_nowait()
    except Empty as exc:
        message = (
            "This Locust process ran out of unique accounts. Increase its account pool "
            "or reduce the users assigned to this worker."
        )
        environment.process_exit_code = 1
        environment.runner.quit()
        raise RuntimeError(message) from exc


def _login(client, account: LoadAccount) -> str | None:
    with client.post(
        f"{API}/auth/login",
        json={"email": account.email, "password": account.password},
        name="POST /auth/login",
        catch_response=True,
    ) as response:
        if response.status_code != 200:
            response.failure(f"login failed: {response.status_code}")
            return None
        try:
            data = response.json()["data"]
        except (KeyError, TypeError, ValueError):
            response.failure("login response did not contain a valid access token")
            return None
        if not isinstance(data, dict):
            response.failure("login response did not contain a valid access token")
            return None
        user = data.get("user")
        token = data.get("access_token")
        if not isinstance(user, dict):
            response.failure("login response did not contain a valid user")
            return None
        if user.get("must_change_password"):
            response.failure("account is on a temporary password")
            return None
        if not isinstance(token, str) or not token:
            response.failure("login response did not contain a valid access token")
            return None
        response.success()
        return token


@events.test_start.add_listener
def _announce(environment, **_kwargs):
    mode = "write-enabled" if WRITE_ENABLED else "read-only"
    print(  # noqa: T201 - Locust reports through stdout
        "\nAtcraft ERP load test "
        f"({mode})\n"
        "  Every simulated user needs a dedicated account from LOAD_ACCOUNTS_FILE.\n"
        "  Use a populated, isolated load environment; never target the shared "
        "development database.\n"
    )


class ErpUser(HttpUser):
    """A signed-in internal user working through a normal shift."""

    # Think time. Zero delay measures how fast the server can be hammered, which
    # is not a question anybody needs answered about an ERP.
    wait_time = between(1, 5)

    def on_start(self) -> None:
        self.token: str | None = None
        self.item_ids: list[str] = []
        self.warehouse_ids: list[str] = []
        self.order_ids: list[str] = []

        self.account = _claim_account(self.environment)
        self.token = _login(self.client, self.account)
        if self.token is None:
            self.environment.process_exit_code = 1
            self.environment.runner.quit()
            return

        self._prime_reference_data()

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _prime_reference_data(self) -> None:
        """Fetch a pool of real ids so requests hit varied rows.

        Hammering one item id would sit entirely in PostgreSQL's shared buffers
        and measure the cache rather than the query.
        """
        items = self.client.get(
            f"{API}/products/items",
            params={"limit": 100},
            headers=self.auth,
            name="GET /items [prime]",
        )
        if items.status_code == 200:
            self.item_ids = [row["id"] for row in items.json().get("data", [])]

        warehouses = self.client.get(
            f"{API}/warehouses",
            params={"limit": 50},
            headers=self.auth,
            name="GET /warehouses [prime]",
        )
        if warehouses.status_code == 200:
            self.warehouse_ids = [row["id"] for row in warehouses.json().get("data", [])]

    # --- reads: the overwhelming majority of real traffic --------------------

    @task(30)
    def stock_balances(self) -> None:
        """The single most-opened screen in the system."""
        params: dict[str, object] = {"limit": 50, "page": _random.randint(1, 5)}
        if self.warehouse_ids:
            params["warehouse_id"] = _random.choice(self.warehouse_ids)
        self.client.get(
            f"{API}/inventory/balances",
            params=params,
            headers=self.auth,
            name="GET /inventory/balances",
        )

    @task(15)
    def item_search(self) -> None:
        """Search-as-you-type: short, unselective terms are the expensive case."""
        term = _random.choice(["lock", "handle", "screw", "rose", "cyl", "kit", "pvd"])
        self.client.get(
            f"{API}/products/items",
            params={"search": term, "limit": 25, "with_total": "false"},
            headers=self.auth,
            name="GET /items?search",
        )

    @task(12)
    def sales_orders(self) -> None:
        response = self.client.get(
            f"{API}/sales",
            params={"limit": 50, "status": _random.choice(["CONFIRMED", "ALLOCATED", "DRAFT"])},
            headers=self.auth,
            name="GET /sales",
        )
        if response.status_code == 200:
            rows = response.json().get("data", [])
            self.order_ids = [row["id"] for row in rows][:20] or self.order_ids

    @task(10)
    def purchase_orders(self) -> None:
        self.client.get(
            f"{API}/procurement/purchase-orders",
            params={"limit": 50},
            headers=self.auth,
            name="GET /purchase-orders",
        )

    @task(8)
    def dashboard(self) -> None:
        """Served from summary tables — should be well inside 500 ms."""
        self.client.get(
            f"{API}/reports/dashboard", headers=self.auth, name="GET /reports/dashboard"
        )

    @task(8)
    def inventory_ledger(self) -> None:
        """Keyset-paginated. Depth must not change the cost."""
        params: dict[str, object] = {"limit": 50}
        if self.item_ids:
            params["item_id"] = _random.choice(self.item_ids)
        self.client.get(
            f"{API}/inventory/ledger",
            params=params,
            headers=self.auth,
            name="GET /inventory/ledger",
        )

    @task(6)
    def order_detail(self) -> None:
        if not self.order_ids:
            return
        order_id = _random.choice(self.order_ids)
        self.client.get(f"{API}/sales/{order_id}", headers=self.auth, name="GET /sales/{id}")
        self.client.get(
            f"{API}/sales/{order_id}/allocation-status",
            headers=self.auth,
            name="GET /sales/{id}/allocation-status",
        )

    @task(5)
    def bom_explosion(self) -> None:
        """CPU- and query-heavy. The interesting tail of the latency curve."""
        if not self.item_ids:
            return
        self.client.get(
            f"{API}/bom",
            params={"item_id": _random.choice(self.item_ids), "limit": 5},
            headers=self.auth,
            name="GET /bom?item_id",
        )

    @task(4)
    def reservations(self) -> None:
        self.client.get(
            f"{API}/allocation/reservations",
            params={"limit": 50, "status": "ACTIVE"},
            headers=self.auth,
            name="GET /allocation/reservations",
        )

    @task(3)
    def production_orders(self) -> None:
        self.client.get(
            f"{API}/production", params={"limit": 50}, headers=self.auth, name="GET /production"
        )

    @task(3)
    def qc_pending(self) -> None:
        self.client.get(f"{API}/quality/pending", headers=self.auth, name="GET /quality/pending")

    @task(3)
    def dispatch_tracking(self) -> None:
        """A wide join across dispatch, PO, vendor and QC — watch this one."""
        self.client.get(
            f"{API}/dispatch/tracking",
            params={"limit": 50},
            headers=self.auth,
            name="GET /dispatch/tracking",
        )

    @task(2)
    def audit_log(self) -> None:
        since = (datetime.now(UTC).date() - timedelta(days=7)).isoformat()
        self.client.get(
            f"{API}/audit/logs",
            params={"limit": 50, "date_from": since},
            headers=self.auth,
            name="GET /audit/logs",
        )

    @task(2)
    def notifications(self) -> None:
        self.client.get(
            f"{API}/notifications/unread-count",
            headers=self.auth,
            name="GET /notifications/unread-count",
        )


class WriteUser(HttpUser):
    """Run one isolated write cycle through inventory, allocation, QC and sales."""

    weight = 1 if WRITE_ENABLED else 0
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.account = _claim_account(self.environment)
        self.token = _login(self.client, self.account)
        self.completed = False
        if self.token is None:
            self.environment.process_exit_code = 1
            self.environment.runner.quit()
            return

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _post(
        self,
        path: str,
        body: dict[str, object] | None,
        name: str,
        *,
        expected_statuses: tuple[int, ...] = (200, 201),
    ) -> dict[str, object] | None:
        with self.client.post(
            f"{API}{path}",
            json=body,
            headers=self.auth,
            name=name,
            catch_response=True,
        ) as response:
            if response.status_code not in expected_statuses:
                response.failure(f"unexpected status {response.status_code}")
                self.environment.process_exit_code = 1
                return None
            try:
                envelope = response.json()
                data = envelope["data"]
            except (KeyError, TypeError, ValueError):
                response.failure("response did not contain a valid data envelope")
                self.environment.process_exit_code = 1
                return None
            if not isinstance(data, dict):
                response.failure("response data was not an object")
                self.environment.process_exit_code = 1
                return None
            response.success()
            return data

    def _required_id(self, data: dict[str, object], resource: str) -> str | None:
        resource_id = data.get("id")
        if isinstance(resource_id, str) and resource_id:
            return resource_id
        message = f"{resource} response did not contain an id"
        print(f"\nWrite profile failed: {message}\n")  # noqa: T201
        self.environment.process_exit_code = 1
        self.environment.runner.quit()
        return None

    @task
    def concurrent_write_cycle(self) -> None:
        if self.completed:
            return
        self.completed = True
        fixtures = self.account.write_fixtures
        if fixtures is None:
            self.environment.runner.quit()
            raise RuntimeError("WriteUser requires write_fixtures for its account")

        adjustment_id = fixtures["inventory_adjustment_id"]
        adjustment = self._post(
            f"/inventory/adjustments/{adjustment_id}/post",
            None,
            "POST /inventory/adjustments/{id}/post",
        )
        if adjustment is None:
            return

        reservation_payload = dict(fixtures["reservation"])
        reservation_payload["demand_id"] = str(uuid4())
        reservation = self._post(
            "/allocation/reservations",
            reservation_payload,
            "POST /allocation/reservations",
        )
        if reservation is None:
            return
        reservation_id = self._required_id(reservation, "Reservation")
        if reservation_id is None:
            return
        released = self._post(
            f"/allocation/reservations/{reservation_id}/release",
            {"reason": "Load profile releases its reserved quantity."},
            "POST /allocation/reservations/{id}/release",
        )
        if released is None:
            return

        inspection_payload = dict(fixtures["inspection"])
        inspection_payload.update({"reference_type": "load_test", "reference_id": str(uuid4())})
        inspection = self._post(
            "/quality/inspections",
            inspection_payload,
            "POST /quality/inspections",
        )
        if inspection is None:
            return
        inspection_id = self._required_id(inspection, "Inspection")
        if inspection_id is None:
            return
        decision = self._post(
            f"/quality/inspections/{inspection_id}/decide",
            dict(fixtures["inspection_decision"]),
            "POST /quality/inspections/{id}/decide",
        )
        if decision is None:
            return

        dispatch = self._post(
            "/dispatch",
            dict(fixtures["dispatch"]),
            "POST /dispatch",
        )
        if dispatch is None:
            return
        dispatch_id = self._required_id(dispatch, "Dispatch")
        if dispatch_id is None:
            return
        sent = self._post(
            f"/dispatch/{dispatch_id}/dispatch",
            dict(fixtures["dispatch_request"]),
            "POST /dispatch/{id}/dispatch",
        )
        if sent is None:
            return

        invoice_payload = dict(fixtures["invoice"])
        invoice_payload["dispatch_id"] = dispatch_id
        self._post(
            "/finance/sales-invoices",
            invoice_payload,
            "POST /finance/sales-invoices",
            expected_statuses=(201,),
        )


class PlannerUser(HttpUser):
    """A planner. Few in number, but their requests are the expensive ones."""

    wait_time = between(20, 60)
    weight = 1

    def on_start(self) -> None:
        self.account = _claim_account(self.environment)
        self.token: str | None = _login(self.client, self.account)
        # As ErpUser does. Without this, a rate-limited login left every planner
        # request carrying "Bearer None", and the 401s it collected were
        # reported as the API's failure rate rather than the harness's.
        if self.token is None:
            self.environment.process_exit_code = 1
            self.environment.runner.quit()
            return

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @task(3)
    def mrp_history(self) -> None:
        self.client.get(
            f"{API}/mrp/runs", params={"limit": 20}, headers=self.auth, name="GET /mrp/runs"
        )

    @task(1)
    def start_mrp_run(self) -> None:
        """Must return promptly (202 for a large run) — never hold the request.

        A 200 here is fine for a small dataset; what would be a defect is this
        call blocking for minutes, so the assertion is on latency, not status.
        """
        if not WRITE_ENABLED:
            return
        with self.client.post(
            f"{API}/mrp/runs",
            json={"horizon_days": 60},
            headers=self.auth,
            name="POST /mrp/runs",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 202):
                if response.elapsed.total_seconds() > 5:
                    response.failure(
                        f"MRP kickoff took {response.elapsed.total_seconds():.1f}s — "
                        "long runs must be dispatched to Celery, not held open"
                    )
                else:
                    response.success()
            elif response.status_code == 409:
                # Another run is already in progress: correct behaviour, not a failure.
                response.success()
            elif response.status_code == 429:
                # Over the per-company run budget. The route documents this as a
                # normal answer (`RateLimitedError`, with a `Retry-After`), and a
                # planner profile that keeps asking is exactly what earns it -
                # counting it as an API failure blames the service for the
                # harness's own request rate, which is the mistake the verdict at
                # the end of this file then repeats as "failure rate exceeds 1%".
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")

    @task(2)
    def shortage_report(self) -> None:
        self.client.get(
            f"{API}/mrp/shortage-report",
            params={"limit": 100},
            headers=self.auth,
            name="GET /mrp/shortage-report",
        )


@events.test_stop.add_listener
def _report(environment, **_kwargs):
    """Check the measured result against the documented targets."""
    stats = environment.stats.total
    print("\n" + "=" * 68)  # noqa: T201
    print("Result vs the targets in docs/performance.md")  # noqa: T201
    print("=" * 68)  # noqa: T201
    print(f"  requests        {stats.num_requests}")  # noqa: T201
    print(f"  failures        {stats.num_failures} ({stats.fail_ratio:.2%})")  # noqa: T201
    print(f"  median          {stats.median_response_time} ms   (target < 200)")  # noqa: T201
    print(f"  95th percentile {stats.get_response_time_percentile(0.95):.0f} ms")  # noqa: T201
    print(f"  99th percentile {stats.get_response_time_percentile(0.99):.0f} ms")  # noqa: T201
    print(f"  throughput      {stats.total_rps:.1f} req/s")  # noqa: T201

    problems = []
    if stats.fail_ratio > 0.01:
        problems.append(f"failure rate {stats.fail_ratio:.2%} exceeds 1%")
    if stats.median_response_time > 200:
        problems.append(f"median {stats.median_response_time} ms exceeds the 200 ms target")
    if stats.get_response_time_percentile(0.95) > 1000:
        problems.append(
            "95th percentile above 1 s — find the slow endpoint before tuning anything else"
        )

    print("=" * 68)  # noqa: T201
    if problems:
        environment.process_exit_code = 1
        print("NEEDS ATTENTION:")  # noqa: T201
        for problem in problems:
            print(f"  - {problem}")  # noqa: T201
        print("\n  Next step: EXPLAIN ANALYZE the slowest endpoints. Do not guess.")  # noqa: T201
    else:
        print("Within the documented targets.")  # noqa: T201
    print("=" * 68 + "\n")  # noqa: T201
