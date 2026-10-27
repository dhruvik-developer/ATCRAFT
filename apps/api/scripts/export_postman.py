"""Generate a Postman collection from the OpenAPI contract.

Generated, never hand-written. The operation count is whatever ``openapi.json``
currently describes, so a hand-maintained collection would be wrong within a
week and nobody would know which half. This reads the same ``openapi.json`` the
frontend's TypeScript types are generated from, so the collection describes
the server by construction; ``--check`` fails the build when it does not, exactly as
``export_openapi.py --check`` does for the contract itself.

What the collection gives you that the raw spec does not
-------------------------------------------------------
* **A working sign-in.** The refresh credential is an HttpOnly cookie, so a
  Postman user cannot read it out of the response body. The `Sign in` request
  therefore captures the access token into a collection variable from its own
  post-response script, and every other request inherits bearer auth from the
  collection root. Send `Sign in`, then send anything.
* **CSRF handled.** State-changing calls to ``/auth/*`` need the
  ``X-CSRF-Token`` header echoing the ``atcraft_csrf`` cookie. The sign-in
  script stores that cookie's value too, and the requests that need it carry
  the header.
* **Path parameters as variables**, so ``/items/{item_id}`` arrives as
  ``/items/:item_id`` with an editable value rather than a literal brace.
* **A request body worth sending**, built from each endpoint's own schema:
  required fields first, with types and enums as placeholder values.

Determinism
-----------
Keys sorted, two-space indent, trailing newline - the same rules as the
contract - so regenerating on an unchanged API produces a byte-identical file
and the diff means something.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONTRACT = REPO_ROOT / "openapi.json"
DEFAULT_OUTPUT = REPO_ROOT / "postman_collection.json"

#: Postman's schema for a v2.1 collection.
SCHEMA_URL = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

#: A stable id keeps the collection the *same* collection across regenerations,
#: so re-importing updates it in place instead of adding a duplicate.
COLLECTION_ID = "a7c1f0e2-4d3b-5a69-8b21-atcrafterp001"

#: Methods that carry a body worth generating.
BODY_METHODS = frozenset({"post", "put", "patch"})

#: The auth routes that require the CSRF double-submit header.
CSRF_PATHS = ("/auth/refresh", "/auth/logout", "/auth/logout-all")

SIGN_IN_SCRIPT = """// Capture what the rest of the collection needs.
//
// The refresh token is an HttpOnly cookie on purpose, so it is neither in this
// response nor readable here - and it does not need to be: Postman's cookie jar
// replays it on /auth/refresh. What the other requests need is the access
// token, which is in the body, and the CSRF value, which is in a readable
// cookie.
const body = pm.response.json()
const token = body && body.data && body.data.access_token

if (token) {
    pm.collectionVariables.set('access_token', token)
    console.log('Signed in. access_token stored.')
} else if (body && body.data && body.data.mfa_required) {
    console.log('Multi-factor challenge issued - send "Verify MFA" with the code.')
} else {
    console.log('No access token in the response; the other requests will answer 401.')
}

const csrf = pm.cookies.get('atcraft_csrf')
if (csrf) {
    pm.collectionVariables.set('csrf_token', csrf)
}
"""


def _example_for(schema: dict[str, Any], spec: dict[str, Any], depth: int = 0) -> Any:
    """A placeholder value that shows the shape and the type.

    Not valid business data - it cannot be, since the values are ids and codes
    from the caller's own database - but it names every required field, which is
    what stops somebody guessing the payload from the endpoint name.
    """
    if depth > 4:
        return {}

    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        resolved = spec.get("components", {}).get("schemas", {}).get(name)
        return _example_for(resolved, spec, depth + 1) if resolved else {}

    for key in ("allOf", "anyOf", "oneOf"):
        options = schema.get(key)
        if options:
            # Prefer the first non-null branch: `X | None` renders as the X.
            for option in options:
                if option.get("type") != "null":
                    return _example_for(option, spec, depth + 1)

    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]

    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        # Required fields first, then the rest: the required ones are what a
        # caller must supply, and Postman shows the body in insertion order.
        ordered = sorted(properties, key=lambda name: (name not in required, name))
        return {name: _example_for(properties[name], spec, depth + 1) for name in ordered}
    if kind == "array":
        item = _example_for(schema.get("items", {}), spec, depth + 1)
        return [item] if item != {} else []
    if kind == "integer":
        return 0
    if kind == "number":
        return 0
    if kind == "boolean":
        return False

    fmt = schema.get("format")
    if fmt == "uuid":
        return "00000000-0000-0000-0000-000000000000"
    if fmt == "date":
        return "2026-01-01"
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    if fmt == "email":
        return "user@example.com"
    return ""


def _postman_path(path: str, prefix: str) -> tuple[list[str], list[dict[str, str]]]:
    """`/api/v1/items/{item_id}` -> segments with `:item_id`, plus its variables."""
    trimmed = path[len(prefix) :] if prefix and path.startswith(prefix) else path
    segments: list[str] = []
    variables: list[dict[str, str]] = []
    for raw in (prefix + trimmed).strip("/").split("/"):
        if raw.startswith("{") and raw.endswith("}"):
            name = raw[1:-1]
            segments.append(f":{name}")
            variables.append({"key": name, "value": "", "description": f"{name} of the record"})
        else:
            segments.append(raw)
    return segments, variables


def _request(path: str, method: str, operation: dict[str, Any], spec: dict[str, Any]) -> dict:
    segments, path_variables = _postman_path(path, "")
    query: list[dict[str, Any]] = []

    for parameter in operation.get("parameters", []):
        if parameter.get("in") != "query":
            continue
        example = _example_for(parameter.get("schema", {}), spec)
        query.append(
            {
                "key": parameter["name"],
                "value": "" if example in ({}, []) else str(example),
                "description": parameter.get("description", ""),
                # Optional parameters arrive switched off, so a request sends
                # only what the caller turns on.
                "disabled": not parameter.get("required", False),
            }
        )

    headers: list[dict[str, str]] = []
    body: dict[str, Any] | None = None
    content = operation.get("requestBody", {}).get("content", {})
    if method in BODY_METHODS and "application/json" in content:
        headers.append({"key": "Content-Type", "value": "application/json"})
        example = _example_for(content["application/json"].get("schema", {}), spec)
        body = {
            "mode": "raw",
            "raw": json.dumps(example, indent=2, ensure_ascii=False),
            "options": {"raw": {"language": "json"}},
        }
    elif method in BODY_METHODS and "application/x-www-form-urlencoded" in content:
        example = _example_for(content["application/x-www-form-urlencoded"].get("schema", {}), spec)
        body = {
            "mode": "urlencoded",
            "urlencoded": [
                {"key": key, "value": str(value), "type": "text"}
                for key, value in (example or {}).items()
            ],
        }

    if any(path.endswith(suffix) for suffix in CSRF_PATHS):
        headers.append({"key": "X-CSRF-Token", "value": "{{csrf_token}}"})

    request: dict[str, Any] = {
        "method": method.upper(),
        "header": headers,
        "url": {
            "raw": "{{base_url}}" + path + ("?" if query else ""),
            "host": ["{{base_url}}"],
            "path": segments,
        },
        "description": operation.get("description") or operation.get("summary") or "",
    }
    if query:
        request["url"]["query"] = query
    if path_variables:
        request["url"]["variable"] = path_variables
    if body is not None:
        request["body"] = body
    return request


def _item_name(path: str, method: str, operation: dict[str, Any]) -> str:
    summary = operation.get("summary")
    return summary if summary else f"{method.upper()} {path}"


def build_collection(spec: dict[str, Any]) -> dict[str, Any]:
    """Group every operation under its OpenAPI tag."""
    folders: dict[str, list[dict[str, Any]]] = {}

    for path, item in sorted(spec.get("paths", {}).items()):
        for method, operation in sorted(item.items()):
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            tag = (operation.get("tags") or ["Other"])[0]
            entry: dict[str, Any] = {
                "name": _item_name(path, method, operation),
                "request": _request(path, method, operation, spec),
            }
            # The one request that has to do more than be sent.
            if path.endswith("/auth/login") and method == "post":
                entry["event"] = [
                    {
                        "listen": "test",
                        "script": {
                            "type": "text/javascript",
                            "exec": SIGN_IN_SCRIPT.splitlines(),
                        },
                    }
                ]
            folders.setdefault(tag, []).append(entry)

    info = spec.get("info", {})
    return {
        "info": {
            "_postman_id": COLLECTION_ID,
            "name": f"{info.get('title', 'API')} ({info.get('version', '0')})",
            "description": (
                "Generated from openapi.json by `make postman` - do not edit by hand, "
                "the next regeneration overwrites it.\n\n"
                "Getting started: set `base_url` (default http://127.0.0.1:8000/api/v1), "
                "send **Sign in** under Authentication, and every other request picks up "
                "the bearer token automatically.\n\n"
                "The refresh token is an HttpOnly cookie, so it never appears in a "
                "response body; Postman's cookie jar replays it on /auth/refresh."
            ),
            "schema": SCHEMA_URL,
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}],
        },
        "variable": [
            {"key": "base_url", "value": "http://127.0.0.1:8000/api/v1", "type": "string"},
            {"key": "access_token", "value": "", "type": "string"},
            {"key": "csrf_token", "value": "", "type": "string"},
        ],
        "item": [
            {"name": tag, "item": sorted(entries, key=lambda e: e["name"])}
            for tag, entries in sorted(folders.items())
        ],
    }


def render(collection: dict[str, Any]) -> str:
    return json.dumps(collection, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the collection on disk is stale",
    )
    args = parser.parse_args(argv)

    if not args.contract.exists():
        print(f"{args.contract} does not exist. Run: make contract", file=sys.stderr)
        return 1

    spec = json.loads(args.contract.read_text(encoding="utf-8"))
    rendered = render(build_collection(spec))

    if args.check:
        if not args.output.exists():
            print(f"{args.output} does not exist. Run: make postman", file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != rendered:
            print(
                f"{args.output} is out of date with {args.contract.name}.\nRun:  make postman",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} matches the contract.")
        return 0

    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    collection = json.loads(rendered)
    requests = sum(len(folder["item"]) for folder in collection["item"])
    print(f"Wrote {args.output} - {requests} requests in {len(collection['item'])} folders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
