"""The Postman collection, and the commands that regenerate it, stay current.

``make contract`` writes the collection. ``.\\make.ps1 contract`` claimed the
same thing and then stopped after the OpenAPI file and the frontend types, so
on Windows a regenerated contract left ``postman_collection.json`` behind and
``contract-check`` could not see it. The generator's own error told people to
run ``make postman``, which was listed as phony and had no recipe.

``docs/api.md`` also quoted a request count. That number is checked against
the file, because a sentence nobody compares is how 491 survived after the
API had moved on.
"""

from __future__ import annotations

import importlib.util
import json
import re

import pytest

from src.core.config import API_ROOT, REPO_ROOT

pytestmark = pytest.mark.unit

_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


def _load_exporter():
    path = API_ROOT / "scripts" / "export_postman.py"
    spec = importlib.util.spec_from_file_location("export_postman", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collection() -> dict:
    return json.loads((REPO_ROOT / "postman_collection.json").read_text(encoding="utf-8"))


def _request_count(collection: dict) -> int:
    return sum(len(folder["item"]) for folder in collection["item"])


def _ps_case(name: str, text: str) -> str:
    # The first closing brace ends the case: these blocks contain no nested ones.
    match = re.search(rf"'{name}'\s*\{{([^}}]*)\}}", text, re.S)
    assert match, f"make.ps1 has no '{name}' case"
    return match.group(1)


def test_the_collection_is_the_contract() -> None:
    spec = json.loads((REPO_ROOT / "openapi.json").read_text(encoding="utf-8"))
    exporter = _load_exporter()
    rendered = exporter.render(exporter.build_collection(spec))
    on_disk = (REPO_ROOT / "postman_collection.json").read_text(encoding="utf-8")
    assert on_disk == rendered, "postman_collection.json is stale. Run: make postman"


def test_every_operation_is_a_request() -> None:
    """A count match can hide a renamed path. Compare the operations themselves."""
    spec = json.loads((REPO_ROOT / "openapi.json").read_text(encoding="utf-8"))
    expected = {
        (method.upper(), path)
        for path, item in spec["paths"].items()
        for method in item
        if method in _METHODS
    }
    found: set[tuple[str, str]] = set()
    for folder in _collection()["item"]:
        for entry in folder["item"]:
            request = entry["request"]
            raw = request["url"]["raw"]
            path = raw.split("{{base_url}}", 1)[-1].split("?", 1)[0]
            found.add((request["method"], path))
    assert found == expected


def test_the_api_doc_states_the_collection_size() -> None:
    collection = _collection()
    needle = f"{_request_count(collection)} requests in {len(collection['item'])} folders"
    doc = re.sub(r"\s+", " ", (REPO_ROOT / "docs" / "api.md").read_text(encoding="utf-8"))
    assert needle in doc, f"docs/api.md should say {needle!r}"


def test_make_and_powershell_both_regenerate_the_collection() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^postman:.*\n\t.*scripts\.export_postman\s*$", makefile, re.M)
    for target in ("contract:", "contract-check:"):
        block = makefile.split(target, 1)[1].split("\n\n", 1)[0]
        assert "scripts.export_postman" in block, target

    ps1 = (REPO_ROOT / "make.ps1").read_text(encoding="utf-8")
    for name in ("contract", "contract-check", "postman"):
        assert "scripts.export_postman" in _ps_case(name, ps1), name
    assert "'postman'" in ps1.split("switch ($Target)", 1)[0]
