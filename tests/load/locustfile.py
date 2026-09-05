"""Load profile for the Atcraft ERP API (master rules sections 57, 66).

Purpose: replace the *engineering targets* in docs/performance.md with numbers
measured against realistic data. Targets that have never been load-tested are
aspirations.

Run against a populated database — an empty one measures nothing useful, because
every index lookup hits a table with no rows and every query looks fast::

    locust -f locustfile.py --host http://localhost \\
           --users 50 --spawn-rate 5 --run-time 10m

Headless smoke::

    locust -f locustfile.py --headless -u 50 -r 5 -t 2m --host http://localhost

The task weights approximate a real working day: people read stock and orders far
more than they write them, and the writes that do happen are concentrated in
receipts and issues.
"""

from __future__ import annotations

import os
import random
import threading
from datetime import date, timedelta

import gevent
from locust import HttpUser, between, events, task

API = "/api/v1"

# The same E2E_ADMIN_* variables the Playwright suite and the sweeps read
# (tests/e2e/.env.example documents them), so one credential serves every
# harness; LOAD_* is honoured for older invocations.
#
# Must match src.core.seed.DEFAULT_ADMIN_EMAIL. Not a .local address: Pydantic's
# EmailStr refuses special-use TLDs, so an account seeded at admin@atcraft.local
# is created and can then never log in - the login route rejects it at
# validation. Every run against that default failed at authentication.
ADMIN_EMAIL = os.getenv("E2E_ADMIN_EMAIL") or os.getenv("LOAD_EMAIL") or "admin@atcraft.example"
ADMIN_PASSWORD = (
    os.getenv("E2E_ADMIN_PASSWORD") or os.getenv("LOAD_PASSWORD") or "ChangeMe!Atcraft2026"
)


# --- one session, shared -----------------------------------------------------
# Every simulated user signing in separately does not model a working day: it
# models a brute-force attempt, and the API is right to refuse it.
# `LOGIN_PER_EMAIL` in src/core/ratelimit.py allows five attempts per email per
# five minutes, so from the sixth user onward every login returned 429 and every
# request after it 401 - and the reported p95 was the rate limiter's own response
# time, not the latency of any ERP endpoint.
#
# In production fifty users are fifty accounts whose sessions are already
# established. What this test measures is the steady state after that: read and
# write latency under concurrency. So the token is obtained once and shared.
# Login throughput is a separate question and needs distinct accounts to ask it.
_token_lock = threading.Lock()
_shared_token: str | None = None

#: How many times to wait out a 429 on the shared login before giving up.
LOGIN_RETRIES = 4
#: The per-email window is 300 s; a quarter of it per attempt covers a window
#: left open by a run that finished moments ago.
LOGIN_BACKOFF_SECONDS = 75


def _shared_login(client) -> str | None:
    """The session every simulated user works through. Fetched once.

    The lock is held across the retry deliberately. Without it a 429 - which is
    what a previous run inside the same five-minute window leaves behind - would
    let every waiting user attempt its own login, and thirty users would turn one
    rate-limited request into thirty, keeping the window open indefinitely. One
    caller waits; the rest queue behind it and get the token it obtains.
    """
    global _shared_token
    with _token_lock:
        if _shared_token is not None:
            return _shared_token

        for attempt in range(LOGIN_RETRIES):
            response = client.post(
                f"{API}/auth/login",
                json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                name="POST /auth/login",
            )
            if response.status_code == 200:
                data = response.json()["data"]
                if data.get("user", {}).get("must_change_password"):
                    # As seeded, the administrator may call nothing but
                    # /auth/me and /auth/change-password: every request the
                    # profile makes would answer 403 and be counted against
                    # the API. Say so instead of measuring a refusal.
                    response.failure("account is on a temporary password")
                    print(  # noqa: T201 - Locust reports through stdout
                        f"\n  {ADMIN_EMAIL} still has a temporary password, so the API"
                        " refuses every business route.\n"
                        "  Run the Playwright setup once (it completes the change), or"
                        " change the password by hand, then run again.\n"
                    )
                    return None
                _shared_token = data["access_token"]
                return _shared_token
            if response.status_code != 429:
                response.failure(f"login failed: {response.status_code}")
                return None
            # Rate limited. The per-email window is five minutes wide, so this
            # backs off rather than hammering; it is reported as a success
            # because a 429 here is the API defending itself correctly and
            # counting it as a failure would misreport the run.
            response.success()
            if attempt < LOGIN_RETRIES - 1:
                gevent.sleep(LOGIN_BACKOFF_SECONDS)

        print(  # noqa: T201 - Locust reports through stdout
            "\n  Login is rate limited (LOGIN_PER_EMAIL: 5 attempts per 5 minutes)"
            " and did not clear within the backoff.\n"
            "  Wait for the window to close and run again, or point LOAD_EMAIL at"
            " an account this machine has not just used.\n"
        )
        return None


@events.test_start.add_listener
def _reset_shared_token(environment, **_kwargs):
    """A fresh run must not reuse the previous run's token."""
    global _shared_token
    _shared_token = None


@events.test_start.add_listener
def _announce(environment, **_kwargs):
    print(  # noqa: T201 - Locust reports through stdout
        "\nAtcraft ERP load test\n"
        "  Reminder: run this against a database with realistic row counts.\n"
        "  An empty database will report latencies you cannot reproduce in production.\n"
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

        self.token = _shared_login(self.client)
        if self.token is None:
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
            f"{API}/products/items", params={"limit": 100}, headers=self.auth, name="GET /items [prime]"
        )
        if items.status_code == 200:
            self.item_ids = [row["id"] for row in items.json().get("data", [])]

        warehouses = self.client.get(
            f"{API}/warehouses", params={"limit": 50}, headers=self.auth, name="GET /warehouses [prime]"
        )
        if warehouses.status_code == 200:
            self.warehouse_ids = [row["id"] for row in warehouses.json().get("data", [])]

    # --- reads: the overwhelming majority of real traffic --------------------

    @task(30)
    def stock_balances(self) -> None:
        """The single most-opened screen in the system."""
        params: dict[str, object] = {"limit": 50, "page": random.randint(1, 5)}
        if self.warehouse_ids:
            params["warehouse_id"] = random.choice(self.warehouse_ids)
        self.client.get(
            f"{API}/inventory/balances",
            params=params,
            headers=self.auth,
            name="GET /inventory/balances",
        )

    @task(15)
    def item_search(self) -> None:
        """Search-as-you-type: short, unselective terms are the expensive case."""
        term = random.choice(["lock", "handle", "screw", "rose", "cyl", "kit", "pvd"])
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
            params={"limit": 50, "status": random.choice(["CONFIRMED", "ALLOCATED", "DRAFT"])},
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
        self.client.get(f"{API}/reports/dashboard", headers=self.auth, name="GET /reports/dashboard")

    @task(8)
    def inventory_ledger(self) -> None:
        """Keyset-paginated. Depth must not change the cost."""
        params: dict[str, object] = {"limit": 50}
        if self.item_ids:
            params["item_id"] = random.choice(self.item_ids)
        self.client.get(
            f"{API}/inventory/ledger", params=params, headers=self.auth, name="GET /inventory/ledger"
        )

    @task(6)
    def order_detail(self) -> None:
        if not self.order_ids:
            return
        order_id = random.choice(self.order_ids)
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
            params={"item_id": random.choice(self.item_ids), "limit": 5},
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
            f"{API}/dispatch/tracking", params={"limit": 50}, headers=self.auth, name="GET /dispatch/tracking"
        )

    @task(2)
    def audit_log(self) -> None:
        since = (date.today() - timedelta(days=7)).isoformat()
        self.client.get(
            f"{API}/audit/logs",
            params={"limit": 50, "date_from": since},
            headers=self.auth,
            name="GET /audit/logs",
        )

    @task(2)
    def notifications(self) -> None:
        self.client.get(
            f"{API}/notifications/unread-count", headers=self.auth, name="GET /notifications/unread-count"
        )


class PlannerUser(HttpUser):
    """A planner. Few in number, but their requests are the expensive ones."""

    wait_time = between(20, 60)
    weight = 1

    def on_start(self) -> None:
        self.token: str | None = _shared_login(self.client)
        # As ErpUser does. Without this, a rate-limited login left every planner
        # request carrying "Bearer None", and the 401s it collected were
        # reported as the API's failure rate rather than the harness's.
        if self.token is None:
            self.environment.runner.quit()
            return

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @task(3)
    def mrp_history(self) -> None:
        self.client.get(f"{API}/mrp/runs", params={"limit": 20}, headers=self.auth, name="GET /mrp/runs")

    @task(1)
    def start_mrp_run(self) -> None:
        """Must return promptly (202 for a large run) — never hold the request.

        A 200 here is fine for a small dataset; what would be a defect is this
        call blocking for minutes, so the assertion is on latency, not status.
        """
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
            f"{API}/mrp/shortage-report", params={"limit": 100}, headers=self.auth, name="GET /mrp/shortage-report"
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
        problems.append("95th percentile above 1 s — find the slow endpoint before tuning anything else")

    print("=" * 68)  # noqa: T201
    if problems:
        print("NEEDS ATTENTION:")  # noqa: T201
        for problem in problems:
            print(f"  - {problem}")  # noqa: T201
        print("\n  Next step: EXPLAIN ANALYZE the slowest endpoints. Do not guess.")  # noqa: T201
    else:
        print("Within the documented targets.")  # noqa: T201
    print("=" * 68 + "\n")  # noqa: T201
