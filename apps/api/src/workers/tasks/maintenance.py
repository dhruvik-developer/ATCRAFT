"""Scheduled housekeeping (master rules sections 25, 33, 35, 43).

Four beat jobs live here, and they are the four the schedule in
``src.workers.celery_app`` already names. Each is an *adapter*: the rules belong
to the module that owns the data, and this file finds the tenants and hands each
one to that module's service.

``maintenance.refresh_summary_views``
    Rebuilds ``inventory_summary`` so dashboards read a pre-aggregated position
    instead of grouping the hot balance table on every request (section 25).

``maintenance.release_stale_reservations``
    Frees stock promised to demand that never came. A stale reservation is
    expensive in a specific way: it hides stock from availability, so MRP orders
    material the company already owns.

``maintenance.purge_expired_sessions``
    Removes refresh-session rows that can no longer authenticate anything.

``maintenance.purge_temp_files``
    Clears the scratch directory and the lapsed report snapshots that point at
    artefacts nobody will fetch again.

Three properties every task here holds.

**Beat has no principal.** There is no request, no user and therefore no
:class:`~src.core.deps.TenantContext`. Each sweep begins with a deliberately
cross-tenant read to discover which companies have work, then partitions by
company and scopes every write again - the same shape ``dispatch.scan_delays``
uses. The nil actor id is only ever used for stamping; nothing here writes it to
a foreign key.

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
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import affected_rows, session_scope, transaction
from src.core.deps import TenantContext
from src.core.logging import get_logger
from src.modules.allocation import service as allocation_service
from src.modules.allocation.models import OPEN_RESERVATION_STATUSES, StockReservation
from src.modules.notifications.repository import SYSTEM_ACTOR_ID
from src.modules.reports import service as reports_service
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


def _system_tenant(company_id: uuid.UUID) -> TenantContext:
    """Scope for a sweep that has no principal.

    The nil actor id is a placeholder for stamping only. The services called from
    here pass ``user_id=None`` to the audit writer explicitly, so it never
    reaches ``created_by_id`` and cannot violate the foreign key to ``users``.
    """
    return TenantContext(company_id=company_id, user_id=SYSTEM_ACTOR_ID)


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


__all__ = [
    "MAX_PURGE_BATCHES",
    "PURGE_BATCH",
    "SESSION_RETENTION_DAYS",
    "TEMP_FILE_MAX_AGE_HOURS",
    "purge_expired_sessions",
    "purge_temp_files",
    "refresh_summary_views",
    "release_stale_reservations",
]
