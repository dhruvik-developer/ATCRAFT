"""Uploads, document PDFs and exports spend their own per-user budget.

``REPORT_EXPORT``, ``FILE_UPLOAD`` and ``BULK_IMPORT`` were declared and never
spent, while ``docs/security.md`` said exports and uploads had their own
budget. Only the general 300-a-minute API allowance applied, so one vendor or
dealer account could queue about 300 permanent PDFs and reports-queue jobs a
minute, or store 300 files of 25 MB.

The rules this file enforces, over every mounted route:

* a route that takes a file spends ``FILE_UPLOAD``;
* a route that queues a background job (it answers ``AcceptedJobResponse``)
  spends ``DOCUMENT_PDF`` or ``REPORT_EXPORT``;
* every ``RateLimitPolicy`` is spent somewhere in ``src`` - a limit nothing
  enforces is a claim, not a control.

And, through the real application with the database replaced by a session
that fails on any use: an exhausted budget answers 429 before anything is
read, and it is that user's budget - another user's request is not refused.
"""

from __future__ import annotations

import ast
import re
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, File, UploadFile
from fastapi.dependencies.models import Dependant
from httpx import ASGITransport, AsyncClient

from src.core import ratelimit
from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import get_db
from src.core.deps import Principal, get_current_principal, spend_budget
from src.core.errors import ErrorCode
from src.core.permissions import Permission
from src.core.ratelimit import KEY_PREFIX, RateLimitPolicy
from src.core.responses import AcceptedJobResponse
from tests.redis_support import FakeRedis
from tests.route_support import (
    MountedRoute,
    UntouchableSession,
    iter_api_routes,
    module_routes,
    mounted_application,
)

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

Policy = tuple[str, int, int]

#: The budgets a queued job may spend: one document's PDF, or an export.
JOB_BUDGETS: frozenset[Policy] = frozenset(
    {RateLimitPolicy.DOCUMENT_PDF, RateLimitPolicy.REPORT_EXPORT}
)


# -----------------------------------------------------------------------------
# Reading routes
# -----------------------------------------------------------------------------


def _dependency_calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    pending = list(dependant.dependencies)
    while pending:
        dependency = pending.pop()
        if dependency.call is not None:
            yield dependency.call
        pending.extend(dependency.dependencies)


def budgets_of(mounted: MountedRoute) -> set[Policy]:
    """The policies a route's ``spend_budget`` dependencies spend."""
    return {
        policy
        for call in _dependency_calls(mounted.route.dependant)
        if (policy := getattr(call, "budget_policy", None)) is not None
    }


def takes_a_file(mounted: MountedRoute) -> bool:
    return any(
        "UploadFile" in str(param.field_info.annotation)
        for param in mounted.route.dependant.body_params
    )


def queues_a_job(mounted: MountedRoute) -> bool:
    return mounted.route.response_model is AcceptedJobResponse


def budget_violations(routes: Iterable[MountedRoute]) -> list[str]:
    found: list[str] = []
    for mounted in routes:
        spent = budgets_of(mounted)
        if takes_a_file(mounted) and RateLimitPolicy.FILE_UPLOAD not in spent:
            found.append(f"{mounted}: takes a file but does not spend FILE_UPLOAD")
        if queues_a_job(mounted) and not spent & JOB_BUDGETS:
            found.append(f"{mounted}: queues a job but spends neither DOCUMENT_PDF nor EXPORT")
    return sorted(found)


def spent_policy_names(source: str) -> set[str]:
    """``X`` for every ``enforce_policy(RateLimitPolicy.X, ...)`` or ``spend_budget(...)``."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func: Any = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        policy = node.args[0]
        if (
            called in {"enforce_policy", "spend_budget"}
            and isinstance(policy, ast.Attribute)
            and isinstance(policy.value, ast.Name)
            and policy.value.id == "RateLimitPolicy"
        ):
            names.add(policy.attr)
    return names


def declared_policy_names() -> set[str]:
    return {
        name
        for name, value in vars(RateLimitPolicy).items()
        if name.isupper() and isinstance(value, tuple)
    }


def _source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.fixture(scope="module")
def routes() -> tuple[MountedRoute, ...]:
    return module_routes()


def _budgeted(routes: Iterable[MountedRoute]) -> list[MountedRoute]:
    return [mounted for mounted in routes if takes_a_file(mounted) or queues_a_job(mounted)]


# -----------------------------------------------------------------------------
# The rules over the application
# -----------------------------------------------------------------------------


def test_the_scan_sees_the_uploads_and_the_jobs(routes: tuple[MountedRoute, ...]) -> None:
    assert sum(takes_a_file(mounted) for mounted in routes) >= 3
    assert sum(queues_a_job(mounted) for mounted in routes) >= 13


def test_every_upload_and_every_queued_job_spends_its_budget(
    routes: tuple[MountedRoute, ...],
) -> None:
    offenders = budget_violations(routes)
    assert not offenders, (
        "Add spend_budget(RateLimitPolicy.FILE_UPLOAD) to a route that takes a file, "
        "and spend_budget(RateLimitPolicy.DOCUMENT_PDF) or REPORT_EXPORT to one that "
        f"queues a job, after its permission gate: {offenders}"
    )


def test_every_named_policy_is_spent_somewhere() -> None:
    spent: set[str] = set()
    for path in _source_files():
        spent |= spent_policy_names(path.read_text(encoding="utf-8"))

    unused = declared_policy_names() - spent
    assert not unused, (
        f"RateLimitPolicy declares {sorted(unused)} and nothing spends them. Enforce "
        "each where its operation happens, or delete it - docs/security.md section 8 "
        "describes what is enforced, and a declared budget reads as a control."
    )


# -----------------------------------------------------------------------------
# The rules reject what they exist to catch
# -----------------------------------------------------------------------------


def test_the_budget_rule_rejects_an_unspent_upload_and_a_job_on_the_wrong_budget() -> None:
    router = APIRouter(prefix="/files")

    @router.post("/unbudgeted")
    async def unbudgeted(file: UploadFile = File()) -> None: ...

    @router.post("/budgeted", dependencies=[spend_budget(RateLimitPolicy.FILE_UPLOAD)])
    async def budgeted(file: UploadFile = File()) -> None: ...

    @router.post(
        "/pdf",
        response_model=AcceptedJobResponse,
        dependencies=[spend_budget(RateLimitPolicy.FILE_UPLOAD)],
    )
    async def wrong_budget() -> None: ...

    @router.post("/report", response_model=AcceptedJobResponse)
    async def no_budget() -> None: ...

    mounted = [MountedRoute("files", path, route) for path, route in iter_api_routes(router)]

    assert budget_violations(mounted) == [
        "POST /files/pdf: queues a job but spends neither DOCUMENT_PDF nor EXPORT",
        "POST /files/report: queues a job but spends neither DOCUMENT_PDF nor EXPORT",
        "POST /files/unbudgeted: takes a file but does not spend FILE_UPLOAD",
    ]


def test_the_usage_rule_sees_both_ways_of_spending_and_nothing_else() -> None:
    source = (
        "await enforce_policy(RateLimitPolicy.MRP_RUN, str(tenant.company_id))\n"
        "deps = [spend_budget(RateLimitPolicy.FILE_UPLOAD)]\n"
        "scope = RateLimitPolicy.LOGIN_PER_EMAIL[0]\n"
        "await reset_rate_limit(email, scope=RateLimitPolicy.REFRESH[0])\n"
    )

    assert spent_policy_names(source) == {"MRP_RUN", "FILE_UPLOAD"}


# -----------------------------------------------------------------------------
# HTTP: an exhausted budget refuses before anything is read
# -----------------------------------------------------------------------------


def _principal(path: str) -> Principal:
    """Whoever may call ``path`` at all: a vendor, a dealer or staff, holding everything."""
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="budget@example.com",
        full_name="Budget Holder",
        is_superuser=False,
        permissions=frozenset(str(permission) for permission in Permission),
        role_codes=("TEST_ADMIN",),
        vendor_id=uuid.uuid4() if path.startswith("/vendor-portal/") else None,
        customer_id=uuid.uuid4() if path.startswith("/dealer-portal/") else None,
    )


def _url(path: str) -> str:
    return settings.api_v1_prefix + re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return mounted_application()


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    stub = FakeRedis()

    async def _get_redis() -> FakeRedis:
        return stub

    monkeypatch.setattr(ratelimit, "get_redis", _get_redis)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    return stub


def _exhaust(redis: FakeRedis, policy: Policy, principal: Principal) -> None:
    scope, limit, _ = policy
    now_ms = int(utcnow().timestamp() * 1000)
    redis.windows[f"{KEY_PREFIX}:rl:{scope}:{principal.user_id}"] = [
        (now_ms, f"spent-{n}") for n in range(limit)
    ]


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[tuple[AsyncClient, list[str]]]:
    touched: list[str] = []

    async def _session() -> AsyncIterator[UntouchableSession]:
        yield UntouchableSession(touched)

    app.dependency_overrides[get_db] = _session
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, touched
    finally:
        app.dependency_overrides.clear()


def _cases() -> list[Any]:
    return [
        pytest.param(mounted, id=str(mounted))
        for mounted in _budgeted(module_routes())
        if budgets_of(mounted)
    ]


@pytest.mark.parametrize("mounted", _cases())
async def test_an_exhausted_budget_is_refused_before_anything_is_read(
    mounted: MountedRoute,
    app: FastAPI,
    client: tuple[AsyncClient, list[str]],
    redis: FakeRedis,
) -> None:
    http, touched = client
    (method,) = mounted.methods
    holder, other = _principal(mounted.path), _principal(mounted.path)
    for policy in budgets_of(mounted):
        _exhaust(redis, policy, holder)

    app.dependency_overrides[get_current_principal] = lambda: holder
    refused = await http.request(method, _url(mounted.path))

    assert refused.status_code == 429, refused.text[:300]
    assert refused.json()["error"]["code"] == ErrorCode.RATE_LIMITED
    assert int(refused.headers["Retry-After"]) >= 1
    assert not touched, f"the database was used before the budget was checked: {touched}"

    # It is the holder's budget: someone else's request gets past it - to the
    # request's own validation, or to the database.
    app.dependency_overrides[get_current_principal] = lambda: other
    allowed = await http.request(method, _url(mounted.path))
    assert allowed.status_code in {422, 503}, allowed.text[:300]


def test_the_budgets_are_sized_as_documented() -> None:
    """docs/assumptions.md §RATE-1 states these; quietly loosening one is caught here."""
    assert RateLimitPolicy.FILE_UPLOAD == ("upload", 60, 300)
    assert RateLimitPolicy.DOCUMENT_PDF == ("document_pdf", 60, 600)
    assert RateLimitPolicy.REPORT_EXPORT == ("report_export", 10, 600)


def test_a_budget_dependency_names_its_policy() -> None:
    dependency: Any = spend_budget(RateLimitPolicy.DOCUMENT_PDF)

    assert isinstance(dependency, type(Depends()))
    assert dependency.dependency.budget_policy == RateLimitPolicy.DOCUMENT_PDF
