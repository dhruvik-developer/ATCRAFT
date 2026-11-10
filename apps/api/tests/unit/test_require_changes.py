"""A partial update that names no field is refused, the one way, on every route.

Four outcomes had grown up for ``PATCH {}``: half the update routes refused it
with ``NO_FIELDS_TO_UPDATE``, the QC template update refused it in other words,
production answered 200 and did nothing, and the rest ran the whole update -
200, a bumped version, and an UPDATE audit row whose before and after were the
same (docs/assumptions.md §PATCH-1). ``require_changes`` is the one refusal.

Two guards hold it. The HTTP one sends ``{}`` to every PATCH route through the
real application with a session that refuses to be used, so it sees every
update path whatever its shape, and proves the refusal comes before anything
is read. The source one names the service function that applies a payload's
sent fields without the call, which is the line to fix.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import BaseModel

from src.core.config import API_ROOT, settings
from src.core.errors import BusinessRuleError, ErrorCode
from src.core.messages import NO_FIELDS_TO_UPDATE
from src.core.validation import require_changes
from tests.route_support import (
    PORTAL_PREFIXES,
    MountedRoute,
    module_routes,
    mounted_application,
    route_url,
    superuser_without_database,
)

pytestmark = pytest.mark.unit

MODULES = API_ROOT / "src" / "modules"

#: Update paths that apply a payload without refusing an empty one, and why.
#: Keyed "<path below src/modules>:<function>".
EXEMPT = {
    # PUT /sales/settings replaces the settings record; {} keeps every default.
    "sales/service.py:update_sales_settings": "PUT-style settings",
    # PUT /audit/approval-settings: {} reads the current settings back unchanged.
    "audit/service.py:update_company_approval_settings": "PUT-style settings",
    # Called only by update_order, which calls require_changes first.
    "sales/service.py:_update_draft": "helper of update_order",
}


class _Probe(BaseModel):
    name: str | None = None
    notes: str | None = None


def test_an_empty_payload_is_refused_as_every_update_route_refuses_it() -> None:
    with pytest.raises(BusinessRuleError) as refused:
        require_changes(_Probe())

    assert refused.value.status_code == 422
    assert refused.value.code is ErrorCode.BUSINESS_RULE_VIOLATION
    assert refused.value.message == NO_FIELDS_TO_UPDATE


@pytest.mark.parametrize("sent", [{"name": "Renamed"}, {"notes": None}])
def test_a_payload_that_names_a_field_passes_even_with_a_null(sent: dict[str, str | None]) -> None:
    # What an explicit null means is the field's own rule, not this one's.
    require_changes(_Probe(**sent))


def _annotation_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _applies_unset_fields_of(call: ast.Call) -> str | None:
    """The name ``X`` when ``call`` is ``X.model_dump(..., exclude_unset=True)``."""
    if (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "model_dump"
        and isinstance(call.func.value, ast.Name)
        and any(
            keyword.arg == "exclude_unset"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in call.keywords
        )
    ):
        return call.func.value.id
    return None


def unguarded_updates(source: str, path: str) -> list[str]:
    """Functions applying an ``*Update`` parameter's sent fields without refusing ``{}``.

    A function passes when it calls ``require_changes`` on that parameter, or
    names ``NO_FIELDS_TO_UPDATE`` itself - the few that refuse a body with no
    *effective* change (a clear flag set to false, a filtered null) rather than
    only an empty one.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        updates = {
            argument.arg
            for argument in [*node.args.args, *node.args.kwonlyargs]
            if _annotation_name(argument.annotation).endswith("Update")
        }
        applied = {
            name
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            for name in [_applies_unset_fields_of(inner)]
            if name in updates
        }
        if not applied:
            continue
        guarded = {
            inner.args[0].id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "require_changes"
            and inner.args
            and isinstance(inner.args[0], ast.Name)
        }
        names_refusal = any(
            isinstance(inner, ast.Name) and inner.id == "NO_FIELDS_TO_UPDATE"
            for inner in ast.walk(node)
        )
        if applied - guarded and not names_refusal:
            found.append(f"{path}:{node.name}")
    return found


def test_every_update_path_refuses_an_empty_payload() -> None:
    found: list[str] = []
    for file in sorted(MODULES.rglob("*.py")):
        path = file.relative_to(MODULES).as_posix()
        found.extend(unguarded_updates(file.read_text(encoding="utf-8"), path))
    assert sorted(set(found) - set(EXEMPT)) == [], (
        "Call require_changes(payload) first, or list the function in EXEMPT with its reason."
    )
    assert sorted(set(EXEMPT) - set(found)) == [], "An exemption no longer needed: remove it."


def test_the_rule_finds_an_update_path_that_applies_an_empty_payload() -> None:
    source = """
async def applies(session, payload: ThingUpdate):
    changes = payload.model_dump(exclude_unset=True)

async def refuses(session, payload: ThingUpdate):
    require_changes(payload)
    changes = payload.model_dump(exclude_unset=True)

async def refuses_in_its_own_words(session, payload: ThingUpdate):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

async def guards_another(session, payload: ThingUpdate, other: OtherUpdate):
    require_changes(other)
    changes = payload.model_dump(exclude_unset=True)

def not_an_update(session, payload: ThingCreate):
    return payload.model_dump(exclude_unset=True)
"""
    assert unguarded_updates(source, "probe.py") == [
        "probe.py:applies",
        "probe.py:guards_another",
    ]


def test_the_quality_template_schema_leaves_the_empty_body_to_the_service() -> None:
    from src.modules.quality.schemas import TemplateUpdate

    # Refused by update_template through require_changes, in the shared words -
    # not as a schema error in words of its own.
    assert TemplateUpdate().model_fields_set == set()
    with pytest.raises(BusinessRuleError):
        require_changes(TemplateUpdate())


def test_the_guard_reads_real_files() -> None:
    assert any(Path(MODULES).rglob("service.py"))


# -----------------------------------------------------------------------------
# HTTP: every PATCH route, through the real application
# -----------------------------------------------------------------------------


def _patch_routes() -> list[MountedRoute]:
    return [
        mounted
        for mounted in module_routes()
        if "PATCH" in mounted.methods and not mounted.path.startswith(PORTAL_PREFIXES)
    ]


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[AsyncClient, list[str]]]:
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    async with superuser_without_database(mounted_application()) as pair:
        yield pair


def test_the_http_guard_sees_the_update_routes() -> None:
    paths = {mounted.path for mounted in _patch_routes()}
    assert {
        "/products/items/{item_id}",
        "/production/{order_id}",
        "/promotions/offers/{identity}",
    } <= paths


@pytest.mark.parametrize("mounted", [pytest.param(m, id=m.path) for m in _patch_routes()])
async def test_an_empty_patch_is_refused_before_anything_is_read(
    mounted: MountedRoute, client: tuple[AsyncClient, list[str]]
) -> None:
    http, touched = client
    response = await http.patch(settings.api_v1_prefix + route_url(mounted), json={})

    assert not touched, f"the database was used before an empty body was refused: {touched}"
    assert response.status_code == 422, response.text[:300]
    error = response.json()["error"]
    # A body with required fields - a full replacement - fails on its fields
    # first; every partial update refuses the empty body in the shared words.
    if error["code"] != ErrorCode.VALIDATION_ERROR:
        assert (error["code"], error["message"]) == (
            ErrorCode.BUSINESS_RULE_VIOLATION,
            NO_FIELDS_TO_UPDATE,
        )
