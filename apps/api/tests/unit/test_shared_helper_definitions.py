"""A shared helper is defined once, and the copies it replaced do not come back.

Each of these was copied into module after module, every copy's docstring
promising "one constructor" or "one predicate, in one place": six
``referenced_in_use``, two ``assert_parent_allowed`` that had already drifted
(only one counted the levels that move with a record), five ``can_approve`` and
four ``_clean_name`` validators in warehouses - two of which returned ``None``,
so a PATCH of ``{"name": null}`` reached a NOT NULL column and came back as a
422 naming no field. The warehouse reference checks went the same way: five
stock documents each refused an unknown or inactive store in code of their own,
and procurement and production each kept the scoped destination check.

A copy is found under the helper's own name and under a name it replaced
(:data:`REPLACED`): assembly and job work each defined ``_assert_warehouses``,
procurement and production ``_assert_warehouse``, and a copy restored from
history comes back with that name rather than the new one. Packing, quality and
dispatch wrote their check inline, which no definition guard can see.

A definition elsewhere that delegates to the owner - ``mrp``'s
``_failure_message`` binds the shared sentence to the planning run - is a call,
not a copy, and passes. A re-implementation does not.
"""

from __future__ import annotations

import ast

import pytest

from tests.source_scan_support import source_texts

pytestmark = pytest.mark.unit


#: helper -> the one module that defines it.
OWNERS: dict[str, str] = {
    "referenced_in_use": "core/errors.py",
    "failure_message": "core/errors.py",
    "assert_parent_allowed": "common/hierarchy.py",
    "can_approve": "common/approvals.py",
    "clean_name": "common/codes.py",
    "reject_null": "core/validation.py",
    "require_changes": "core/validation.py",
    "date_before": "core/messages.py",
    "inactive_record": "core/messages.py",
    "duplicate_code": "core/errors.py",
    "code_already_used": "core/messages.py",
    "not_tracked": "core/messages.py",
    "belongs_elsewhere": "core/messages.py",
    "not_for_this_item": "core/messages.py",
    "no_longer_editable": "core/messages.py",
    "draft_only_delete": "core/messages.py",
    "currency_code": "core/fields.py",
    "normalise_serial_labels": "core/fields.py",
    "require_active_warehouses": "modules/warehouses/service.py",
    "require_warehouse_in_scope": "modules/warehouses/service.py",
}

#: A name a copy had before its helper was shared -> the helper in :data:`OWNERS`
#: that replaced it. A copy is restored under its old name, not its new one.
REPLACED: dict[str, str] = {
    "assert_warehouses": "require_active_warehouses",
    "assert_warehouse": "require_warehouse_in_scope",
}

#: The error code only :func:`src.core.errors.referenced_in_use` attaches by hand
#: (the integrity handler maps a foreign-key violation to it from a table).
_IN_USE_CODE = "REFERENCED_RECORD_IN_USE"
_IN_USE_OWNERS = frozenset({"core/errors.py"})


def _calls(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
    return names


def copies(source: str, relative: str) -> list[str]:
    """Definitions in ``source`` that re-implement a helper :data:`OWNERS` names."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        name = node.name.lstrip("_")
        helper = REPLACED.get(name, name)
        if helper not in OWNERS or OWNERS[helper] == relative:
            continue
        if helper in _calls(node):
            continue  # delegates to the shared helper
        where = OWNERS[helper] if helper == name else f"{OWNERS[helper]} as {helper}"
        found.append((node.lineno, f"line {node.lineno}: {node.name} (defined in {where})"))
    # ast.walk is breadth-first; report in source order so a failure reads top-down.
    return [finding for _, finding in sorted(found)]


def hand_built_in_use_errors(source: str) -> list[str]:
    """Calls that attach ``code=ErrorCode.REFERENCED_RECORD_IN_USE`` themselves."""
    return [
        f"line {node.lineno}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and any(
            keyword.arg == "code"
            and isinstance(keyword.value, ast.Attribute)
            and keyword.value.attr == _IN_USE_CODE
            for keyword in node.keywords
        )
    ]


def test_the_guard_finds_a_copied_helper() -> None:
    source = (
        "from pydantic import BaseModel, field_validator\n"
        "class WarehouseUpdate(BaseModel):\n"
        "    @field_validator('name')\n"
        "    @classmethod\n"
        "    def _clean_name(cls, value):\n"
        "        return None if value is None else ' '.join(value.split())\n"
        "def referenced_in_use(message, **details):\n"
        "    return BusinessRuleError(message, code=ErrorCode.REFERENCED_RECORD_IN_USE)\n"
    )

    assert copies(source, "modules/warehouses/schemas.py") == [
        "line 5: _clean_name (defined in common/codes.py)",
        "line 7: referenced_in_use (defined in core/errors.py)",
    ]
    assert hand_built_in_use_errors(source) == ["line 8"]


def test_the_guard_finds_a_renamed_warehouse_check() -> None:
    """The copies 2b7ccca2 replaced, as they stood before it, under their own names."""
    assembly = (
        "async def _assert_warehouses(session, tenant, *ids):\n"
        "    found = await WarehouseLookupRepository(session, tenant).load(list(ids))\n"
        "    for warehouse_id in ids:\n"
        "        row = found.get(warehouse_id)\n"
        "        if row is None:\n"
        "            raise NotFoundError('Warehouse', warehouse_id)\n"
        "        code, is_active = row\n"
        "        if not is_active:\n"
        "            raise BusinessRuleError(\n"
        "                inactive_record('Warehouse', code, 'cannot be used by an assembly'),\n"
        "                details={'warehouse_id': str(warehouse_id), 'warehouse_code': code},\n"
        "            )\n"
    )
    procurement = (
        "async def _assert_warehouse(session, tenant, warehouse_id):\n"
        "    if not await WarehouseLookupRepository(session, tenant).exists_in_tenant(\n"
        "        warehouse_id\n"
        "    ):\n"
        "        raise NotFoundError('Warehouse', warehouse_id)\n"
    )

    assert copies(assembly, "modules/assembly/service.py") == [
        "line 1: _assert_warehouses "
        "(defined in modules/warehouses/service.py as require_active_warehouses)",
    ]
    assert copies(procurement, "modules/procurement/service.py") == [
        "line 1: _assert_warehouse "
        "(defined in modules/warehouses/service.py as require_warehouse_in_scope)",
    ]


def test_the_guard_lets_a_delegating_wrapper_through() -> None:
    source = (
        "def _failure_message(exc):\n"
        "    return failure_message(exc, 'The planning run', logged_against='worker log')\n"
        "async def _assert_warehouses(session, tenant, *ids):\n"
        "    await require_active_warehouses(session, tenant, ids, consequence='cannot')\n"
    )

    assert copies(source, "workers/tasks/mrp.py") == []


def test_every_replaced_name_points_at_a_shared_helper() -> None:
    """A map entry naming no owned helper would silently check nothing."""
    assert set(REPLACED.values()) <= set(OWNERS)
    assert not set(REPLACED) & set(OWNERS)


def test_every_owner_defines_its_helper() -> None:
    sources = source_texts()
    for helper, owner in OWNERS.items():
        defined = {
            node.name
            for node in ast.walk(ast.parse(sources[owner]))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert helper in defined, f"{owner} no longer defines {helper}"


def test_no_module_redefines_a_shared_helper() -> None:
    offenders = {
        relative: found
        for relative, source in source_texts().items()
        if (found := copies(source, relative))
    }

    assert not offenders, (
        f"Import the shared helper instead of defining another copy of it: {offenders}"
    )


def test_only_referenced_in_use_attaches_the_in_use_code() -> None:
    offenders = {
        relative: found
        for relative, source in source_texts().items()
        if relative not in _IN_USE_OWNERS and (found := hand_built_in_use_errors(source))
    }

    assert not offenders, (
        "Raise src.core.errors.referenced_in_use(message, **details) rather than "
        f"attaching REFERENCED_RECORD_IN_USE by hand: {offenders}"
    )
