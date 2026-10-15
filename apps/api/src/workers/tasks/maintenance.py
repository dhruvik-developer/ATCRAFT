"""Scheduled housekeeping (master rules sections 25, 33, 35, 43).

Five beat jobs live here, and they are the five the schedule in
``src.workers.celery_app`` names. Each is an *adapter*: the rules belong to the
module that owns the data, and this file finds the tenants and hands each one to
that module's service.

``maintenance.refresh_summary_views``
    Rebuilds ``inventory_summary`` so dashboards read a pre-aggregated position
    instead of grouping the hot balance table on every request (section 25).

``maintenance.release_stale_reservations``
    Frees stock promised to demand that never came. A stale reservation is
    expensive in a specific way: it hides stock from availability, so MRP orders
    material the company already owns.

``maintenance.allocate_short_orders``
    The allocation top-up: hands each short sales order to
    ``sales.service.allocate_order``, so stock that arrives from a goods
    receipt, a production or assembly output or a transfer reaches the orders
    waiting for it without any of those modules knowing about sales.

``maintenance.purge_expired_sessions``
    Removes refresh-session rows that can no longer authenticate anything.

``maintenance.purge_temp_files``
    Clears the scratch directory and the lapsed report snapshots that point at
    artefacts nobody will fetch again.

``maintenance.reconcile_quality_owners``
    Re-tells every document still waiting on a quality verdict the verdict its
    inspection already carries - the purchase orders, receipt lines, assembly
    orders and job-work lots a decision stranded before quality called its
    owners back (``quality.owners``). Run once when beat starts and then daily;
    it does nothing when nothing is waiting.

Three properties every task here holds.

**Beat has no principal.** There is no request, no user and therefore no
:class:`~src.core.deps.TenantContext`. Each sweep begins with a deliberately
cross-tenant read to discover which companies have work, then partitions by
company and scopes every write again - the same shape ``dispatch.scan_delays``
uses. The nil actor id (``src.core.ids.SYSTEM_ACTOR_ID``) is only ever used for
stamping: every actor column is written through ``persisted_actor``, which
stores it as NULL rather than as a foreign key to a user that does not exist.

**One tenant's failure must not stop the others.** Each company runs in its own
session, and a failure is logged and skipped. A single company with a corrupt row
cannot silence every other tenant's housekeeping.

**Re-delivery is harmless.** ``task_acks_late`` is on, so a worker killed
mid-sweep has its task delivered again. Every sweep here is idempotent by
construction: it re-derives its work set from the clock and from current state,
so running it twice does the same thing as running it once, and each is bounded
so a backlog is worked off over several ticks rather than in one transaction that
holds locks for minutes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from sqlalchemy import Exists, and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import affected_rows, session_scope, transaction
from src.core.deps import TenantContext, system_tenant
from src.core.errors import BusinessRuleError
from src.core.logging import get_logger
from src.modules.allocation import service as allocation_service
from src.modules.allocation.models import OPEN_RESERVATION_STATUSES, StockReservation
from src.modules.quality import service as quality_service
from src.modules.reports import service as reports_service
from src.modules.sales import service as sales_service
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderLineStatus
from src.modules.users.models import UserSession
from src.workers.celery_app import async_task

log = get_logger("worker.maintenance")

#: How long a dead session row is kept before deletion. ASSUMPTION: the master
#: rules fix no retention for expired sessions. Thirty days is the conservative
#: choice - long enough that "which device was this signed in from last month?"
#: is still answerable during a security investigation, short enough that the
#: table does not grow without bound. The security-relevant trail of *attempts*
#: lives in ``login_attempts`` and is untouched by this sweep.
SESSION_RETENTION_DAYS = 30

#: Age at which a file left in the scratch directory is removed. ASSUMPTION: no
#: retention is specified for ``storage/temp``; a day is long enough that an
#: in-flight upload or export is never deleted underneath itself, and short
#: enough that a crashed job does not leak disk for a week.
TEMP_FILE_MAX_AGE_HOURS = 24

#: Rows deleted per statement. Bounded so no single transaction holds locks for
#: long; the sweep loops, and whatever is left is taken on the next tick.
PURGE_BATCH = 500

#: Ceiling on the loop, so a pathological backlog cannot turn one scheduled tick
#: into an unbounded job that overruns the next one.
MAX_PURGE_BATCHES = 40

#: The scratch directory, from the storage tree section 43 defines.
TEMP_SUBDIRECTORY = "temp"

#: Most short sales orders one company's top-up handles per tick. ASSUMPTION:
#: 200 orders every ``SO_TOPUP_INTERVAL_SECONDS`` (900 by default) keeps a tick
#: well inside its interval while still reaching a large order book within the
#: hour; the most urgent orders are taken first, so a backlog delays the least
#: urgent ones rather than random ones (docs/assumptions.md section E1-2).
MAX_TOPUP_ORDERS = 200


def _system_tenant(company_id: uuid.UUID) -> TenantContext:
    """Scope for a sweep that has no principal.

    The nil actor id is a placeholder, never a user. The repository base class
    and the audit writer stamp it as NULL and label the audit row "System"
    (``src.core.actors``), so it cannot violate the foreign key to ``users``
    whichever service a sweep calls.
    """
    return system_tenant(company_id)


# -----------------------------------------------------------------------------
# Summary refresh
# -----------------------------------------------------------------------------


@async_task(name="maintenance.refresh_summary_views", queue="default")
async def refresh_summary_views(request_id: str | None = None, **_: Any) -> dict[str, Any]:
    """Rebuild every tenant's inventory summary.

    Runs on its own schedule rather than on every stock movement on purpose: a
    trigger firing per movement would put summary maintenance on the critical
    path of the row-locked write that section 10 protects, which is the one place
    in the system that must stay as short as possible.
    """
    async with session_scope() as session:
        companies = await reports_service.companies_with_stock(session)

    refreshed = 0
    positions = 0
    failed = 0

    for company_id in companies:
        try:
            async with session_scope() as session:
                result = await reports_service.refresh_inventory_summary(
                    session, _system_tenant(company_id)
                )
        except Exception as exc:
            failed += 1
            log.error(
                "maintenance.summary_refresh_failed",
                company_id=str(company_id),
                error=str(exc),
                exc_info=exc,
            )
            continue
        refreshed += 1
        positions += result.position_count

    log.info(
        "maintenance.summary_views_refreshed",
        companies=refreshed,
        positions=positions,
        failed=failed,
    )
    return {"companies": refreshed, "positions": positions, "failed": failed}


# -----------------------------------------------------------------------------
# Stale reservations
# -----------------------------------------------------------------------------


async def _companies_with_stale_reservations(
    session: AsyncSession, moment: datetime
) -> list[uuid.UUID]:
    """Tenants holding at least one reservation past its expiry."""
    stmt = (
        select(StockReservation.company_id)
        .where(
            StockReservation.status.in_(sorted(OPEN_RESERVATION_STATUSES)),
            StockReservation.expires_at.is_not(None),
            StockReservation.expires_at <= moment,
        )
        .distinct()
    )
    return list((await session.execute(stmt)).scalars().all())


@async_task(name="maintenance.release_stale_reservations", queue="default")
async def release_stale_reservations(request_id: str | None = None, **_: Any) -> dict[str, Any]:
    """Give back stock promised to demand that never materialised.

    The releasing itself belongs to the allocation engine, which does it under
    the same balance row locks a live order takes - this task only decides *when*
    and *for whom*. Doing the arithmetic here would be a second implementation of
    the rule that decides how much stock exists.
    """
    moment = utcnow()
    async with session_scope() as session:
        companies = await _companies_with_stale_reservations(session, moment)

    released = 0
    failed = 0

    for company_id in companies:
        try:
            async with session_scope() as session:
                expired = await allocation_service.expire_stale_reservations(
                    session, _system_tenant(company_id)
                )
        except Exception as exc:
            failed += 1
            log.error(
                "maintenance.reservation_expiry_failed",
                company_id=str(company_id),
                error=str(exc),
                exc_info=exc,
            )
            continue
        released += len(expired)

    log.info(
        "maintenance.stale_reservations_released",
        companies=len(companies),
        released=released,
        failed=failed,
    )
    return {"companies": len(companies), "released": released, "failed": failed}


# -----------------------------------------------------------------------------
# Allocation top-up for short sales orders
# -----------------------------------------------------------------------------


def _has_short_line() -> Exists:
    """Correlated on ``sales_orders``: a live line whose open demand its reservation does not cover.

    "Covered" reads the reservation's live remaining quantity. A line whose
    reservation was released, expired or consumed joins nothing and counts as
    holding none - exactly how ``sales.service`` reads it.
    """
    return (
        select(SalesOrderLine.id)
        .outerjoin(
            StockReservation,
            and_(
                StockReservation.id == SalesOrderLine.reservation_id,
                StockReservation.company_id == SalesOrderLine.company_id,
                StockReservation.status.in_(sorted(OPEN_RESERVATION_STATUSES)),
            ),
        )
        .where(
            SalesOrderLine.company_id == SalesOrder.company_id,
            SalesOrderLine.sales_order_id == SalesOrder.id,
            SalesOrderLine.status != SalesOrderLineStatus.CANCELLED,
            SalesOrderLine.quantity - SalesOrderLine.dispatched_quantity
            > func.coalesce(StockReservation.remaining_quantity, 0),
        )
        .exists()
    )


async def _companies_with_short_orders(session: AsyncSession) -> list[uuid.UUID]:
    """Tenants with at least one open order (confirmed, allocated or part-shipped) still short."""
    stmt = (
        select(SalesOrder.company_id)
        .where(SalesOrder.status.in_(sales_service.TOP_UP_STATUSES), _has_short_line())
        .distinct()
    )
    return list((await session.execute(stmt)).scalars().all())


async def _short_orders(session: AsyncSession, company_id: uuid.UUID) -> list[uuid.UUID]:
    """One company's short orders, most urgent first, at most ``MAX_TOPUP_ORDERS``.

    Priority first (lower is more urgent), then the date the customer needs the
    goods, then the date they ordered; the id breaks the remaining ties so the
    order is stable from one tick to the next.
    """
    stmt = (
        select(SalesOrder.id)
        .where(
            SalesOrder.company_id == company_id,
            SalesOrder.status.in_(sales_service.TOP_UP_STATUSES),
            _has_short_line(),
        )
        .order_by(
            SalesOrder.priority,
            SalesOrder.required_date,
            SalesOrder.order_date,
            SalesOrder.id,
        )
        .limit(MAX_TOPUP_ORDERS)
    )
    return list((await session.execute(stmt)).scalars().all())


@async_task(name="maintenance.allocate_short_orders", queue="default")
async def allocate_short_orders(request_id: str | None = None, **_: Any) -> dict[str, Any]:
    """Reserve newly available stock for every short sales order (K02, K08).

    The Celery entry point; the work is :func:`top_up_short_orders`.
    """
    return await top_up_short_orders()


async def top_up_short_orders() -> dict[str, Any]:
    """One tick of the allocation top-up, over every tenant.

    The rule belongs to ``sales.service.allocate_order``; this only decides
    *when* and *for whom*, which is what keeps goods receipts, production and
    transfers free of any knowledge of sales (docs/assumptions.md section E1-2).
    Each order runs in its own session and transaction, so one that cannot be
    topped up - a lock timeout, a reservation the engine refuses - is logged and
    skipped without undoing the orders before it. An order that stopped being
    eligible between the read and its lock (shipped, fully covered, cancelled)
    is a race the lock resolved, not a failure.

    The service writes its audit row only when a reservation changed, so a tick
    over orders whose items are still out of stock leaves no trace beyond the
    log line below. A plain coroutine, apart from the task, so it can be awaited
    on a caller's own event loop.
    """
    async with session_scope() as session:
        companies = await _companies_with_short_orders(session)

    examined = 0
    skipped = 0
    failed = 0
    for company_id in companies:
        try:
            async with session_scope() as session:
                order_ids = await _short_orders(session, company_id)
        except Exception as exc:
            failed += 1
            log.error(
                "maintenance.topup_scan_failed",
                company_id=str(company_id),
                error=str(exc),
                exc_info=exc,
            )
            continue
        for order_id in order_ids:
            examined += 1
            try:
                async with session_scope() as session:
                    await sales_service.allocate_order(
                        session,
                        _system_tenant(company_id),
                        order_id,
                        reason="Scheduled allocation of newly available stock",
                    )
            except BusinessRuleError as exc:
                skipped += 1
                log.info(
                    "maintenance.topup_skipped",
                    company_id=str(company_id),
                    order_id=str(order_id),
                    reason=exc.message,
                )
            except Exception as exc:
                failed += 1
                log.error(
                    "maintenance.topup_failed",
                    company_id=str(company_id),
                    order_id=str(order_id),
                    error=str(exc),
                    exc_info=exc,
                )

    log.info(
        "maintenance.short_orders_topped_up",
        companies=len(companies),
        examined=examined,
        skipped=skipped,
        failed=failed,
    )
    return {
        "companies": len(companies),
        "examined": examined,
        "skipped": skipped,
        "failed": failed,
    }


# -----------------------------------------------------------------------------
# Session retention
# -----------------------------------------------------------------------------


@async_task(name="maintenance.purge_expired_sessions", queue="default")
async def purge_expired_sessions(request_id: str | None = None, **_: Any) -> dict[str, Any]:
    """Delete refresh sessions that expired or were revoked long enough ago.

    Deliberately cross-tenant and deliberately a hard delete: a ``user_sessions``
    row is a credential, not a business record. Once it can no longer
    authenticate anything, keeping it is a liability rather than an audit trail -
    the trail of who signed in and from where lives in ``login_attempts``, which
    this sweep does not touch (section 28: soft deletion is for master data, and
    never for hiding history that matters).

    The delete is written here rather than in the users module because retention
    is a scheduling concern with no business rule attached: it is one bounded
    statement with a date predicate, and giving the users module a purge API for
    it would be a wider surface than the job needs.
    """
    cutoff = utcnow() - timedelta(days=SESSION_RETENTION_DAYS)
    removed = 0

    async with session_scope() as session:
        for _batch in range(MAX_PURGE_BATCHES):
            stale = (
                select(UserSession.id)
                .where(UserSession.expires_at <= cutoff)
                .limit(PURGE_BATCH)
                .scalar_subquery()
            )
            async with transaction(session):
                result = await session.execute(delete(UserSession).where(UserSession.id.in_(stale)))
            deleted = affected_rows(result)
            removed += deleted
            if deleted < PURGE_BATCH:
                break

    log.info("maintenance.expired_sessions_purged", removed=removed, cutoff=cutoff.isoformat())
    return {"removed": removed, "cutoff": cutoff.isoformat()}


# -----------------------------------------------------------------------------
# Temporary files
# -----------------------------------------------------------------------------


def _sweep_temp_directory(root: Path, cutoff: float) -> tuple[int, int]:
    """Delete files under ``root`` last modified before ``cutoff``.

    Synchronous, and handed to a worker thread by the caller: ``stat`` and
    ``unlink`` on a directory of thousands of files is blocking I/O and has no
    business on the event loop.

    Only files are removed, never directories - a directory here is part of the
    storage layout section 43 defines, and a sweep that pruned empty ones would
    race with a job that had just created one for a file it was about to write.
    A file that vanishes between the scan and the unlink is another worker doing
    the same job, which is why the miss is ignored rather than raised.
    """
    removed = 0
    reclaimed = 0
    if not root.exists():
        return 0, 0

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                continue
            size = stat.st_size
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            # A file held open by another process is not an error worth failing
            # the sweep over; the next tick will find it again.
            continue
        removed += 1
        reclaimed += size
    return removed, reclaimed


@async_task(name="maintenance.purge_temp_files", queue="default")
async def purge_temp_files(request_id: str | None = None, **_: Any) -> dict[str, Any]:
    """Clear the scratch directory and the report snapshots that have lapsed.

    Two halves of the same fact. A generated report is a temporary artefact with
    a stated expiry; past it, the snapshot row is a pointer nobody will follow.
    The file it names is a ``documents`` row with its own retention and is left to
    that module - deleting bytes out from under another module's bookkeeping
    would leave it with a row pointing at nothing.
    """
    moment = utcnow()
    cutoff = (moment - timedelta(hours=TEMP_FILE_MAX_AGE_HOURS)).timestamp()
    root = settings.storage_path / TEMP_SUBDIRECTORY

    removed_files, reclaimed_bytes = await anyio.to_thread.run_sync(
        _sweep_temp_directory, root, cutoff
    )

    async with session_scope() as session:
        companies = await reports_service.companies_with_expired_snapshots(session)

    removed_snapshots = 0
    failed = 0
    for company_id in companies:
        try:
            async with session_scope() as session:
                removed_snapshots += await reports_service.purge_expired_snapshots(
                    session, _system_tenant(company_id)
                )
        except Exception as exc:
            failed += 1
            log.error(
                "maintenance.snapshot_purge_failed",
                company_id=str(company_id),
                error=str(exc),
                exc_info=exc,
            )

    log.info(
        "maintenance.temp_files_purged",
        files=removed_files,
        bytes=reclaimed_bytes,
        snapshots=removed_snapshots,
        failed=failed,
    )
    return {
        "files_removed": removed_files,
        "bytes_reclaimed": reclaimed_bytes,
        "snapshots_removed": removed_snapshots,
        "failed": failed,
    }


# -----------------------------------------------------------------------------
# Quality owners
# -----------------------------------------------------------------------------


@async_task(name="maintenance.reconcile_quality_owners", queue="default")
async def reconcile_quality_owners(request_id: str | None = None, **_: Any) -> dict[str, Any]:
    """Repair documents a quality decision left waiting on its verdict.

    The rules - which documents wait, and what a verdict does to each - belong to
    ``quality.owners`` and the owners it calls; this finds the companies and
    runs that sweep for each as the system actor, so every repair is audited
    and status-changed exactly as a person's decision would have been.
    Idempotent: a document already brought up to date is left alone, and a
    company with nothing waiting costs three indexed reads.
    """
    async with session_scope() as session:
        companies = await quality_service.companies_with_decided_inspections(session)

    examined = applied = repaired = failed = companies_failed = 0
    for company_id in companies:
        try:
            async with session_scope() as session:
                result = await quality_service.reconcile_quality_owners(
                    session, _system_tenant(company_id)
                )
        except Exception as exc:
            companies_failed += 1
            log.error(
                "maintenance.quality_owner_reconcile_failed",
                company_id=str(company_id),
                error=str(exc),
                exc_info=exc,
            )
            continue
        examined += result.documents_examined
        applied += result.verdicts_applied
        repaired += result.owners_repaired
        failed += result.failed

    log.info(
        "maintenance.quality_owners_reconciled",
        companies=len(companies),
        documents_examined=examined,
        verdicts_applied=applied,
        owners_repaired=repaired,
        failed=failed,
        companies_failed=companies_failed,
    )
    return {
        "companies": len(companies),
        "documents_examined": examined,
        "verdicts_applied": applied,
        "owners_repaired": repaired,
        "failed": failed,
        "companies_failed": companies_failed,
    }


__all__ = [
    "MAX_PURGE_BATCHES",
    "MAX_TOPUP_ORDERS",
    "PURGE_BATCH",
    "SESSION_RETENTION_DAYS",
    "TEMP_FILE_MAX_AGE_HOURS",
    "allocate_short_orders",
    "purge_expired_sessions",
    "purge_temp_files",
    "reconcile_quality_owners",
    "refresh_summary_views",
    "release_stale_reservations",
    "top_up_short_orders",
]
