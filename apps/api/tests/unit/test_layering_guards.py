"""Mechanical guards for three CLAUDE.md layering rules that review alone kept.

* A router is thin: parse, authorise, delegate, return the envelope. It imports
  nothing from SQLAlchemy or from a repository and never drives the session
  itself - "Raw database access in a route is a review rejection." The products
  router imported ``ItemRepository`` to resolve the item list's sort column,
  which is the repository's job behind the service, as in every other module.
* Only ``src/core/database.py`` commits or rolls back. ``transaction()`` runs
  its body in a SAVEPOINT so a failure unwinds that operation and nothing
  else; a bare ``session.rollback()`` in a service discards the caller's work
  too (see the CLAUDE.md trap of the same name).
* No ``float`` in the business modules. Money and quantities are ``Decimal``;
  the few floats that are not business values are listed below with the
  reason, so a new one is a decision someone has to write down.
* A budget is a named ``RateLimitPolicy``, spent through ``enforce_policy`` or
  ``spend_budget``. Only the limiter itself and the general per-principal
  middleware call ``enforce_rate_limit`` / ``check_rate_limit`` directly. The
  courier webhook spent a literal 60-a-minute from its route handler, where
  ``tests/unit/test_route_budgets.py`` - which checks every *named* policy is
  spent - could not see it, and no document listed it.
* A statement is built in a repository - "repository.py: queries and
  persistence, always tenant-scoped". Built in a service, it re-types the tenant
  filter and the lock order at each site, and the repository method it copies
  rots unused beside it: four such twins were found dead. The files that still
  build statements outside a repository are listed with what they hold; the list
  only shrinks.

Every rule is a function over source text, and each has a self-test that feeds
it the violation it exists to catch - a guard that cannot fail is worse than
none.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

#: Session methods a router must leave to the service layer.
_SESSION_METHODS = frozenset(
    {"execute", "scalar", "scalars", "get", "add", "add_all", "flush", "commit"}
    | {"rollback", "refresh", "delete", "merge", "stream", "stream_scalars"}
)

#: The one module allowed to end a transaction.
_TRANSACTION_OWNER = "core/database.py"

#: The limiter's raw entry points, which take a limit and a window as literals.
_RAW_LIMITERS = frozenset({"enforce_rate_limit", "check_rate_limit"})

#: The modules allowed to call them, each with the reason.
RAW_LIMITER_OWNERS: dict[str, str] = {
    "core/ratelimit.py": "the limiter itself; enforce_policy spends a named policy through it",
    "core/middleware.py": "the general per-principal API budget, sized by settings",
}

#: SQLAlchemy's statement builders, by the names ``sqlalchemy`` exports them under.
_STATEMENT_BUILDERS = frozenset({"select", "update", "delete", "insert", "exists", "text"})

#: Module files outside a repository that still build statements, each with what
#: it holds. Move them into the module's repository and delete the entry; add one
#: only for a documented decision, as sales/fulfilment.py is.
SQL_OUTSIDE_REPOSITORIES: dict[str, str] = {
    "modules/sales/fulfilment.py": (
        "documented reporting surface: its module docstring reads every owning "
        "module's tables directly to trace one order"
    ),
    "modules/vendors/service.py": (
        "load_performance's raw-SQL probe of tables that may not exist yet in a "
        "partially migrated database; _soft_delete_contacts not yet moved"
    ),
    "modules/collections/service.py": "_customer and _assert_user reads, not yet moved",
    "modules/courier_tracking/service.py": (
        "_replace_transporter_links and _apply_inbound_delivery, not yet moved"
    ),
    "modules/dealer_portal/service.py": "_live_internal_users, not yet moved",
    "modules/finance/sales_invoice_service.py": (
        "issued_credit_total, list_sales_invoices and _set_sales_receipt_allocations, not yet moved"
    ),
    "modules/finance/service.py": (
        "the purchase-return debit allocation reads and two alert-candidate queries, not yet moved"
    ),
    "modules/inventory/service.py": (
        "_job_work_serials, get_serial and below_reorder_alert_candidates, not yet moved"
    ),
    "modules/job_work/serial_tracking.py": (
        "validate_receipt_units and prepare_batch_conversions, not yet moved"
    ),
    "modules/job_work/service.py": (
        "the rework-source locks, the order detail, receive, the challan PDF data and "
        "overdue_alert_candidates, not yet moved"
    ),
    "modules/mrp/shortfall.py": "the production and requisition document reads, not yet moved",
    "modules/procurement/inbound_service.py": "five alert-candidate queries, not yet moved",
    "modules/procurement/purchase_order_changes.py": "_apply_lines' line delete, not yet moved",
    "modules/production/service.py": (
        "_movements_for_document and delayed_alert_candidates, not yet moved"
    ),
    "modules/quality/serial_tracking.py": (
        "_locked_positions and prepare_serial_inspection, not yet moved"
    ),
    "modules/quality/service.py": "pending_alert_candidates, not yet moved",
    "modules/reports/service.py": "the sweeps' company lists, not yet moved",
    "modules/sales/service.py": "at_risk_alert_candidates, not yet moved",
}

#: ``float`` in src/modules, each a non-business value.
FLOAT_ALLOWED: dict[str, str] = {
    "modules/audit/schemas.py": "a reporting window's length in days, not a stored value",
    "modules/courier_tracking/provider.py": "epoch seconds compared for webhook clock skew",
    "modules/vendors/router.py": "the min_rating query filter; the filter model holds a Decimal",
}


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _router_files() -> list[Path]:
    return sorted(path for path in (SRC / "modules").glob("*/*.py") if "router" in path.stem)


def _is_repository(path: Path) -> bool:
    return path.stem == "repository" or path.stem.endswith("_repository")


def _module_files_outside_repositories() -> list[Path]:
    return [path for path in _python_files(SRC / "modules") if not _is_repository(path)]


def _line_number(finding: str) -> int:
    # ast.walk is breadth-first; report in source order so a failure reads top-down.
    return int(finding.split(":")[0].removeprefix("line "))


def _router_may_not_import(module: str) -> bool:
    """SQLAlchemy, or a repository - ``repository``, ``inbound_repository`` and the like."""
    return module.split(".")[0] == "sqlalchemy" or module.split(".")[-1].endswith("repository")


def router_violations(source: str) -> list[str]:
    """SQLAlchemy and repository imports, and direct session calls, in a router module."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and _router_may_not_import(node.module or ""):
            found.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            found.extend(
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if _router_may_not_import(alias.name)
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SESSION_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"session", "db"}
        ):
            found.append(f"line {node.lineno}: {node.func.value.id}.{node.func.attr}()")
    return sorted(found, key=_line_number)


def transaction_end_violations(source: str) -> list[str]:
    """``.commit()`` / ``.rollback()`` calls, wherever they appear."""
    found = [
        f"line {node.lineno}: .{node.func.attr}()"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback"}
    ]
    return sorted(found, key=_line_number)


def raw_limiter_calls(source: str) -> list[str]:
    """Calls of ``enforce_rate_limit`` / ``check_rate_limit``, bare or as an attribute."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in _RAW_LIMITERS:
            found.append(f"line {node.lineno}: {name}()")
    return sorted(found, key=_line_number)


def statement_builds(source: str, *, allowed: frozenset[str] = frozenset()) -> list[str]:
    """Calls of a SQLAlchemy statement builder, imported by name or through the module.

    ``allowed`` exempts builders by name: a models file states CHECK constraints
    and partial-index predicates through ``text()``, which is schema, not a query.
    """
    builders = _STATEMENT_BUILDERS - allowed
    tree = ast.parse(source)
    imported: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "sqlalchemy":
            imported.update(
                (alias.asname or alias.name, alias.name)
                for alias in node.names
                if alias.name in builders
            )
        elif isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name.split(".")[0]
                for alias in node.names
                if alias.name.split(".")[0] == "sqlalchemy"
            )
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in imported:
            found.append(f"line {node.lineno}: {imported[func.id]}()")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in builders
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ):
            found.append(f"line {node.lineno}: {func.value.id}.{func.attr}()")
    return sorted(found, key=_line_number)


def _module_statement_builds(path: Path) -> list[str]:
    allowed = frozenset({"text"}) if path.stem == "models" else frozenset()
    return statement_builds(path.read_text(encoding="utf-8"), allowed=allowed)


def float_uses(source: str) -> list[str]:
    found = [
        f"line {node.lineno}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name) and node.id == "float"
    ]
    return sorted(found, key=_line_number)


def test_the_scans_find_the_code_they_police() -> None:
    assert len(_router_files()) >= 30, "router discovery found too few files"
    assert (SRC / _TRANSACTION_OWNER).is_file()


def test_routers_do_not_touch_the_database() -> None:
    offenders = {
        _relative(path): found
        for path in _router_files()
        if (found := router_violations(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "Routers parse, authorise, delegate and return the envelope; move the query, "
        "or the use of the repository, behind the module's service and call that: "
        f"{offenders}"
    )


def test_only_the_transaction_helper_commits_or_rolls_back() -> None:
    offenders = {
        _relative(path): found
        for path in _python_files(SRC)
        if _relative(path) != _TRANSACTION_OWNER
        and (found := transaction_end_violations(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "Use `async with transaction(session):` from src.core.database; a direct "
        f"commit or rollback ends work that belongs to the caller: {offenders}"
    )


def test_only_the_limiter_and_the_general_budget_call_the_raw_limiter() -> None:
    offenders = {
        _relative(path): found
        for path in _python_files(SRC)
        if _relative(path) not in RAW_LIMITER_OWNERS
        and (found := raw_limiter_calls(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "Declare the budget as a RateLimitPolicy in src/core/ratelimit.py and spend it "
        "with enforce_policy (or spend_budget on a route), so test_route_budgets sees "
        f"it and docs/security.md section 8 can name it: {offenders}"
    )


def test_every_raw_limiter_owner_still_calls_it() -> None:
    stale = [
        name
        for name in RAW_LIMITER_OWNERS
        if not (SRC / name).is_file()
        or not raw_limiter_calls((SRC / name).read_text(encoding="utf-8"))
    ]
    assert not stale, f"remove these RAW_LIMITER_OWNERS entries, the call is gone: {stale}"


def test_statements_are_built_in_repositories() -> None:
    offenders = {
        _relative(path): found
        for path in _module_files_outside_repositories()
        if _relative(path) not in SQL_OUTSIDE_REPOSITORIES
        and (found := _module_statement_builds(path))
    }
    assert not offenders, (
        "Build the statement in the module's repository (a BaseRepository method, so "
        "the tenant filter and the lock order are written once) and call that from "
        f"the service: {offenders}"
    )


def test_every_sql_allowance_is_still_needed() -> None:
    stale = [
        name
        for name in SQL_OUTSIDE_REPOSITORIES
        if not (SRC / name).is_file() or not _module_statement_builds(SRC / name)
    ]
    assert not stale, (
        f"remove these SQL_OUTSIDE_REPOSITORIES entries, the statements are gone: {stale}"
    )


def test_business_modules_hold_no_float() -> None:
    offenders = {
        _relative(path): found
        for path in _python_files(SRC / "modules")
        if _relative(path) not in FLOAT_ALLOWED
        and (found := float_uses(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "Money and quantities are Decimal (src.core.types). If this float is not a "
        f"business value, add it to FLOAT_ALLOWED with the reason: {offenders}"
    )


def test_every_float_allowance_is_still_needed() -> None:
    stale = [
        name
        for name in FLOAT_ALLOWED
        if not (SRC / name).is_file() or not float_uses((SRC / name).read_text(encoding="utf-8"))
    ]
    assert not stale, f"remove these FLOAT_ALLOWED entries, the float is gone: {stale}"


# -- the rules reject what they exist to catch --------------------------------


def test_the_router_rule_rejects_queries_session_calls_and_repositories() -> None:
    source = (
        "from sqlalchemy import select\n"
        "import sqlalchemy.orm\n"
        "async def handler(session):\n"
        "    await session.execute(select(1))\n"
        "    session.add(object())\n"
        "    await service(session).run()\n"
        "from src.modules.products.repository import ItemRepository\n"
        "from src.modules.procurement.inbound_repository import ShipmentRepository\n"
        "import src.common.repository\n"
        "from src.modules.products import service\n"
        "from src.modules.products.schemas import ItemFilters\n"
    )
    assert router_violations(source) == [
        "line 1: from sqlalchemy import ...",
        "line 2: import sqlalchemy.orm",
        "line 4: session.execute()",
        "line 5: session.add()",
        "line 7: from src.modules.products.repository import ...",
        "line 8: from src.modules.procurement.inbound_repository import ...",
        "line 9: import src.common.repository",
    ]


def test_the_transaction_rule_rejects_commit_and_rollback() -> None:
    source = "async def f(session):\n    await session.rollback()\n    await self.db.commit()\n"
    assert transaction_end_violations(source) == ["line 2: .rollback()", "line 3: .commit()"]


def test_the_limiter_rule_rejects_a_literal_budget_and_passes_a_named_one() -> None:
    source = (
        "from src.core import ratelimit\n"
        "async def handler(ip):\n"
        "    await enforce_rate_limit(f'hook:{ip}', limit=60, window_seconds=60)\n"
        "    await ratelimit.check_rate_limit(ip, limit=5)\n"
        "    await enforce_policy(RateLimitPolicy.COURIER_WEBHOOK_PER_IP, ip)\n"
    )
    assert raw_limiter_calls(source) == [
        "line 3: enforce_rate_limit()",
        "line 4: check_rate_limit()",
    ]


def test_the_statement_rule_rejects_builders_by_name_and_through_the_module() -> None:
    source = (
        "import sqlalchemy as sa\n"
        "from sqlalchemy import func, select, text as sql\n"
        "from sqlalchemy.dialects.postgresql import insert\n"
        "async def f(session, repo, stmt):\n"
        "    await session.execute(select(Item).where(func.lower(Item.code) == 'x'))\n"
        "    await session.execute(sql('select 1'))\n"
        "    insert(Item).values(code='x')\n"
        "    sa.update(Item)\n"
        "    stmt.exists()\n"
        "    await repo.select()\n"
    )
    assert statement_builds(source) == [
        "line 5: select()",
        "line 6: text()",
        "line 7: insert()",
        "line 8: sa.update()",
    ]


def test_the_statement_rule_lets_a_model_state_a_constraint_in_text() -> None:
    source = (
        "from sqlalchemy import CheckConstraint, select, text\n"
        "CheckConstraint(text('quantity >= 0'), name='quantity_non_negative')\n"
        "subquery = select(Item.id)\n"
    )
    assert statement_builds(source, allowed=frozenset({"text"})) == ["line 3: select()"]


def test_the_float_rule_rejects_annotations_and_conversions() -> None:
    source = "def total(x: float) -> str:\n    return str(float(x))\n"
    assert float_uses(source) == ["line 1", "line 2"]
