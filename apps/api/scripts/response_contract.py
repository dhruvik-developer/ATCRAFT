"""Reject API contracts that bypass the shared JSON response envelopes."""

from __future__ import annotations

from typing import Any

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
ERROR_SCHEMA = "#/components/schemas/ErrorResponse"


def _success_variants(schema: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve an envelope reference or every member of a documented union."""
    reference = schema.get("$ref")
    if reference:
        prefix = "#/components/schemas/"
        if not reference.startswith(prefix):
            raise ValueError(f"unsupported response reference {reference}")
        schema = spec.get("components", {}).get("schemas", {}).get(reference[len(prefix) :], {})
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            return [
                variant for member in schema[keyword] for variant in _success_variants(member, spec)
            ]
    return [schema]


def validate_response_contract(spec: dict[str, Any]) -> None:
    """Require a typed shared envelope for every documented JSON response.

    Binary downloads and bodyless HTTP responses retain their native formats.
    This runs during both contract generation and CI's freshness check, so an
    untyped route cannot silently become an untyped frontend API response.
    """
    issues: list[str] = []
    for path, item in spec.get("paths", {}).items():
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            for status, response in operation.get("responses", {}).items():
                label = f"{method.upper()} {path} ({status})"
                if (
                    status.startswith(("4", "5")) or status == "default"
                ) and "application/json" not in response.get("content", {}):
                    issues.append(f"{label}: errors must document ErrorResponse JSON")
                for media_type, content in response.get("content", {}).items():
                    if media_type != "application/json" and not media_type.endswith("+json"):
                        continue
                    schema = content.get("schema", {})
                    if status.startswith("2"):
                        try:
                            variants = _success_variants(schema, spec)
                        except ValueError as exc:
                            issues.append(f"{label}: {exc}")
                            continue
                        for variant in variants:
                            properties = variant.get("properties", {})
                            if (
                                variant.get("type") != "object"
                                or properties.get("success", {}).get("const") is not True
                                or "data" not in properties
                                or properties.get("message", {}).get("type") != "string"
                            ):
                                issues.append(
                                    f"{label}: success JSON must declare a shared envelope "
                                    "with success=true, data and a string message"
                                )
                    elif status.startswith(("4", "5")) or status == "default":
                        if schema.get("$ref") != ERROR_SCHEMA:
                            issues.append(f"{label}: errors must use ErrorResponse")
    if issues:
        raise ValueError("Inconsistent API response contract:\n" + "\n".join(issues))
