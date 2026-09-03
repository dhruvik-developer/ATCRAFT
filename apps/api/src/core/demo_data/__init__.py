"""Realistic demonstration data for a development or evaluation database.

This is **not** :mod:`src.core.seed`. That one loads the baseline every install
needs — roles, permissions, units, warehouse types, one administrator — and is
safe to run against any environment. This one fabricates a working business:
items with stock, orders in flight, production part-finished, invoices part-paid.
It exists so that every screen has something to show, and so an end-to-end run
has something to walk through.

Why it goes through the services
--------------------------------
Every row here is created by calling the module's own service function, never by
INSERT. That is slower and it is the point:

* stock arrives through ``inventory.service.post_movement``, so the ledger, the
  balance and the audit row are written together and the demo database is
  internally consistent rather than merely populated;
* status changes go through the state machines, so nothing lands in a state its
  own module considers impossible;
* Python-side column defaults apply. ``CLAUDE.md`` records what hand-written
  INSERTs cost this codebase twice — a column list goes stale the moment a
  module adds or removes a field, silently.

A side effect worth having: this file exercises 25 modules in one pass, so it
fails loudly when a service signature changes under it.

Idempotence
-----------
Everything is keyed by a deterministic code carrying :data:`DEMO_PREFIX`.
Re-running finds what exists and skips it, so this can be run repeatedly against
a database that is already populated, and it will only fill in what is missing.
It never edits or deletes a row it did not create.

Ordering
--------
The stages run in dependency order and must stay that way: engineering needs
items, procurement needs vendors and items, operations needs BOMs and stock,
finance needs the receipts and invoices operations produced.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import session_scope
from src.core.deps import Principal, TenantContext
from src.core.logging import configure_logging, get_logger
from src.core.permissions import Permission
from src.core.registry import import_all_models

log = get_logger(__name__)

#: Every code this module creates starts with it, so demonstration rows are
#: recognisable at a glance and can never collide with real data.
DEMO_PREFIX = "DM"


class DemoDataError(RuntimeError):
    """A stage could not complete. Carries the stage name for the report."""


@dataclass
class DemoContext:
    """Everything a stage needs, plus the register of what earlier stages made.

    Stages address each other's output by *code*, never by position or by a
    freshly-issued id: ``ctx.id_of("item", "DM-RM-BRASS")`` is stable across
    runs, which is what makes the whole thing re-runnable.
    """

    session: AsyncSession
    tenant: TenantContext
    principal: Principal
    company_id: uuid.UUID
    user_id: uuid.UUID

    #: ``kind -> code -> id`` for everything created or found so far.
    refs: dict[str, dict[str, uuid.UUID]] = field(default_factory=dict)
    #: ``stage -> {"created": n, "existing": n}``.
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Stages that were skipped, and why. Reported at the end.
    skipped: list[tuple[str, str]] = field(default_factory=list)

    # --- the register ------------------------------------------------------

    def remember(self, kind: str, code: str, row_id: uuid.UUID) -> uuid.UUID:
        self.refs.setdefault(kind, {})[code] = row_id
        return row_id

    def id_of(self, kind: str, code: str) -> uuid.UUID:
        """The id an earlier stage registered under this code.

        Raises rather than returning ``None``: a stage that cannot find its
        dependency has a bug in the ordering, and continuing would write a row
        with a dangling reference that only fails much later.
        """
        try:
            return self.refs[kind][code]
        except KeyError as exc:
            known = sorted(self.refs.get(kind, {}))
            raise DemoDataError(
                f"no {kind} registered as {code!r}; "
                f"{'known: ' + ', '.join(known[:8]) if known else f'no {kind} registered at all'}"
            ) from exc

    def maybe_id(self, kind: str, code: str) -> uuid.UUID | None:
        """Like :meth:`id_of`, but for a genuinely optional reference."""
        return self.refs.get(kind, {}).get(code)

    def has(self, kind: str, code: str) -> bool:
        return code in self.refs.get(kind, {})

    # --- reporting ---------------------------------------------------------

    def note(self, stage: str, *, created: int = 0, existing: int = 0) -> None:
        bucket = self.counts.setdefault(stage, {"created": 0, "existing": 0})
        bucket["created"] += created
        bucket["existing"] += existing

    def code(self, *parts: str) -> str:
        """A deterministic demo code: ``DM-RM-BRASS``."""
        return "-".join((DEMO_PREFIX, *(p.upper() for p in parts)))


#: One stage. Takes the context, fills in its slice, registers what it made.
Stage = Callable[[DemoContext], Awaitable[None]]


async def find_by_code(
    session: AsyncSession, model: type[Any], company_id: uuid.UUID, code: str
) -> Any | None:
    """The idempotence primitive: has this demo row already been made?

    Codes are unique per company by convention throughout this schema
    (``UniqueConstraint("company_id", "code")``), so this is the same lookup the
    services use to reject a duplicate.
    """
    stmt = select(model).where(model.company_id == company_id, model.code == code)
    return (await session.execute(stmt)).scalars().first()


async def _resolve_principal(session: AsyncSession, admin_email: str) -> Principal:
    """Act as the seeded administrator, with every permission the code defines.

    Demonstration data has to cross every module, so it runs with the full set
    rather than a role's subset. This is a developer tool operating on a
    developer database; it is never wired into the application.
    """
    from src.modules.users.models import User

    user = (await session.execute(select(User).where(User.email == admin_email))).scalars().first()
    if user is None:
        raise DemoDataError(
            f"no user {admin_email!r} exists. Run `python -m src.core.seed` first — "
            "this tool populates a seeded database, it does not replace the seed."
        )

    return Principal(
        user_id=user.id,
        company_id=user.company_id,
        email=user.email,
        full_name=user.full_name,
        is_superuser=True,
        permissions=frozenset(str(p) for p in Permission),
    )


#: Dependency order. Later stages resolve earlier stages' output by code, so
#: this sequence is a contract, not a preference.
STAGE_ORDER: tuple[str, ...] = (
    "masters",
    "engineering",
    "procurement",
    "operations",
    "finance",
    # Last, because a document is filed against a record: every other stage has
    # to have made its documents before there is anything to attach a file to.
    "documents",
)


def _load_stage(name: str) -> Stage:
    """Import one stage on demand.

    Deliberately not a module-level import of all five: ``--only masters`` has
    to work while a sibling stage is missing or mid-edit, which is exactly the
    situation when one is being developed or debugged. An eager import turns an
    unrelated stage's syntax error into "nothing runs at all".
    """
    module = import_module(f"{__name__}.{name}")
    seed_fn = getattr(module, "seed", None)
    if seed_fn is None:
        raise DemoDataError(f"stage {name!r} defines no seed(ctx) coroutine")
    return cast("Stage", seed_fn)


async def run(admin_email: str, *, only: tuple[str, ...] = ()) -> DemoContext:
    """Run every stage in dependency order and return the filled context."""
    import_all_models()

    unknown = sorted(set(only) - set(STAGE_ORDER))
    if unknown:
        raise DemoDataError(
            f"unknown stage(s): {', '.join(unknown)}. Known: {', '.join(STAGE_ORDER)}"
        )

    async with session_scope() as session:
        principal = await _resolve_principal(session, admin_email)
        ctx = DemoContext(
            session=session,
            tenant=TenantContext(
                company_id=principal.company_id,
                user_id=principal.user_id,
                is_superuser=True,
            ),
            principal=principal,
            company_id=principal.company_id,
            user_id=principal.user_id,
        )

        for name in STAGE_ORDER:
            if only and name not in only:
                ctx.skipped.append((name, "not selected"))
                continue
            log.info("demo.stage_start", stage=name)
            await _load_stage(name)(ctx)
            # Flush rather than commit: the whole run is one transaction, so a
            # stage that fails halfway leaves no half-built business behind.
            await session.flush()
            log.info("demo.stage_done", stage=name, **ctx.counts.get(name, {}))

    return ctx


def _report(ctx: DemoContext) -> None:
    print("\n" + "=" * 68)
    print("  Demonstration data")
    print("=" * 68)
    width = max((len(s) for s in ctx.counts), default=12)
    for stage, counts in ctx.counts.items():
        print(
            f"  {stage:<{width}}  created {counts['created']:>4}"
            f"   already present {counts['existing']:>4}"
        )
    for stage, reason in ctx.skipped:
        print(f"  {stage:<{width}}  skipped - {reason}")
    total = sum(len(v) for v in ctx.refs.values())
    print("-" * 68)
    print(f"  {total} records registered across {len(ctx.refs)} kinds")
    print("=" * 68 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Populate a development database with a working business."
    )
    parser.add_argument(
        "--admin-email",
        default="admin@atcraft.example",
        help="The seeded administrator to act as (default: %(default)s)",
    )
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated stages to run (masters, engineering, procurement, "
        "operations, finance). Default: all, in order.",
    )
    args = parser.parse_args()

    configure_logging()
    only = tuple(s.strip() for s in args.only.split(",") if s.strip())
    try:
        ctx = asyncio.run(run(args.admin_email, only=only))
    except DemoDataError as exc:
        print(f"\n  Demo data failed: {exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - developer tool
        log.error("demo.failed", error=str(exc), exc_info=exc)
        return 1
    _report(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
