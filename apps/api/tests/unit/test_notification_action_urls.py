from __future__ import annotations

import ast
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.unit

API_ROOT = Path(__file__).resolve().parents[2]
MODULES = API_ROOT / "src" / "modules"
ROUTE_LOADERS = API_ROOT.parent / "web" / "src" / "app" / "route-loaders.ts"
NOTIFICATION_CALLS = {
    "notify",
    "notify_customer",
    "notify_vendor",
    "NotificationDraft",
    "AlertCandidate",
}


def _function_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _paths(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.JoinedStr):
        value = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                value += part.value
            elif isinstance(part, ast.FormattedValue):
                value += ":dynamic"
            else:
                return set()
        return {value}
    if isinstance(node, ast.IfExp):
        return _paths(node.body) | _paths(node.orelse)
    return set()


def _route_paths() -> list[str]:
    source = ROUTE_LOADERS.read_text(encoding="utf-8")
    routes = re.findall(r"^\s*'([^']+)':\s*\(\)\s*=>", source, re.MULTILINE)
    assert routes, "route-loaders.ts contains no route keys"
    return routes


def _matches_route(path: str, routes: list[str]) -> bool:
    pathname = urlsplit(path).path
    return any(
        re.fullmatch(re.sub(r":[^/]+", r"[^/]+", re.escape(route)), pathname) for route in routes
    )


def test_notification_action_urls_resolve_to_frontend_routes() -> None:
    routes = _route_paths()
    invalid: list[str] = []

    for source_path in MODULES.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _function_name(node) not in NOTIFICATION_CALLS:
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            action_url = keywords.get("action_url")
            entity_id = keywords.get("entity_id")
            if entity_id is not None and not (
                isinstance(entity_id, ast.Constant) and entity_id.value is None
            ):
                assert action_url is not None, (
                    f"{source_path.relative_to(API_ROOT)}:{node.lineno} sets entity_id "
                    "without an action_url"
                )
            if action_url is None:
                continue
            for path in _paths(action_url):
                if path.startswith("/") and not _matches_route(path, routes):
                    invalid.append(f"{source_path.relative_to(API_ROOT)}:{node.lineno}: {path}")

    assert not invalid, "Notification action URLs without frontend routes:\n" + "\n".join(invalid)
