"""Planning business rules: starting a run, executing it, acting on its output.

The rules that matter here, and why each one lives in the service rather than in
a route or in the engine:

1. **The service decides how a run executes, never the client.** A run whose
   demand exceeds ``settings.mrp_sync_threshold_demand_lines`` is handed to
   Celery and the endpoint answers ``202`` with a status URL; anything smaller
   runs inline. Master rules section 14 is explicit that a normal HTTP request is
   never held open for a long calculation, and letting a request ask for "run it
   inline" would be a way around that.

2. **Demand is snapshotted before the run is dispatched.** ``mrp_demands`` is
   written in the same transaction that creates the run, and the engine - whether
   it runs inline or in a worker ten minutes later - plans against that snapshot,
   never against the live order book. Two things follow: the plan is reproducible,
   and ``demand_line_count`` describes what was actually planned rather than what
   happened to be open when a worker got round to it.

3. **One run per company at a time**, guarded in two layers. A Redis lock
   (``acquire_lock``/``release_lock``) is the fast, cross-process gate and covers
   the whole life of an asynchronous run; a PostgreSQL advisory lock plus a check
   for an active run row is the correctness floor underneath it, so a Redis
   outage degrades to "still correct, one extra round-trip" rather than to two
   concurrent plans. Both raise ``MRP_RUN_IN_PROGRESS``.

4. **And a budget on top of the lock.** The lock only stops runs *overlapping*;
   it does nothing about a client that starts a fresh run the moment the last
   one ends. ``RateLimitPolicy.MRP_RUN`` bounds that to five starts per company
   per five minutes, keyed on the tenant so no company can spend another's.

5. **Every status change goes through a state machine**, and every one writes an
   audit row in the same transaction as the change it describes.

6. **Conversion is a seam, not a stub.** Procurement, production and job work are
   built independently of planning. When one of them is deployed, the suggestion
   is converted through its named service hook inside this transaction; when it
   is not, the caller receives the typed :class:`ConversionRequest` MRP would
   have sent, and the result says plainly that no document exists yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.cache import acquire_lock, release_lock
from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.database import advisory_xact_lock, transaction
from src.core.deps import TenantContext
from src.core.errors import AppError, BusinessRuleError, ErrorCode, NotFoundError
from src.core.logging import get_logger
from src.core.observability import mrp_run_duration, mrp_runs_total
from src.core.pagination import PageParams, SortParams
from src.core.ratelimit import RateLimitPolicy, enforce_policy
from src.core.responses import OffsetPagination, api_path
from src.core.state_machine import StateMachine, Transition
from src.core.types import to_quantity
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.mrp import engine
from src.modules.mrp.models import (
    MAX_DEMAND_LINES,
    MrpDemand,
    MrpDemandSource,
    MrpRequirement,
    MrpRun,
    MrpRunMode,
    MrpRunStatus,
    MrpSuggestion,
    MrpSuggestionStatus,
    MrpSuggestionType,
)
from src.modules.mrp.repository import (
    MRP_RUN_ENTITY,
    MRP_SUGGESTION_ENTITY,
    DatabaseMrpSource,
    MrpDemandRepository,
    MrpRequirementRepository,
    MrpRunRepository,
    MrpSuggestionRepository,
    SalesDemandRepository,
)
from src.modules.mrp.schemas import (
    DEFAULT_CONVERSION_TARGET,
    BulkIgnoreRequest,
    BulkIgnoreResult,
    ConversionRequest,
    ConversionTarget,
    MrpDemandRead,
    MrpRequirementRead,
    MrpRunFilters,
    MrpRunRead,
    MrpRunRequest,
    MrpRunSummary,
    MrpSuggestionRead,
    SalesOrderAvailabilityRead,
    SalesOrderLineAvailabilityRead,
    ShortageReportFilters,
    SuggestionConversionResult,
    SuggestionConvertRequest,
    SuggestionFilters,
)

log = get_logger(__name__)

#: Where a client polls for a run's progress, relative to the mounted prefix.
#: Kept as one constant so the value returned in a 202 body cannot drift away
#: from the route that serves it. The prefix is applied by :func:`status_url` at
#: call time rather than baked in here: an import-time f-string over
#: ``settings.api_v1_prefix`` would ignore a prefix bound later - by a test, or
#: by an operator who changes ``API_V1_PREFIX`` - and point at a route that no
#: longer exists.
STATUS_URL_TEMPLATE = "/mrp/runs/{run_id}"


def status_url(run_id: uuid.UUID | str) -> str:
    """Absolute path a client polls for one run: ``GET /mrp/runs/{id}``."""
    return api_path(STATUS_URL_TEMPLATE.format(run_id=run_id))


#: Celery task name. Must match the ``@async_task(name=...)`` in
#: ``src.workers.tasks.mrp``; the API dispatches by name so it never has to
#: import the worker module.
MRP_TASK_NAME = "mrp.run"

#: Redis lock lifetime. Comfortably longer than Celery's own hard task limit
#: (3600 s) so a genuinely long run cannot have its lock expire underneath it,
#: and short enough that a worker killed mid-run does not block planning for a day.
RUN_LOCK_TTL_SECONDS = 4200

#: ASSUMPTION: a run that has been QUEUED or RUNNING for longer than this was
#: abandoned - the worker died, or the broker lost the task. It is closed off as
#: FAILED so planning is not blocked forever by a process that no longer exists.
#: Deliberately longer than the Celery hard time limit, so a task that is merely
#: slow is never mistaken for a dead one.
RUN_STALE_AFTER_SECONDS = 7200

_STALE_RUN_MESSAGE = (
    "The run was still marked in progress after "
    f"{RUN_STALE_AFTER_SECONDS // 3600} hours and is presumed abandoned. "
    "No plan was produced."
)


class MrpRunInProgressError(AppError):
    """Another plan is already being calculated for this company.

    Two concurrent runs would both write suggestions a buyer could act on, and
    the second would be netting against stock the first had already committed in
    its own output.
    """

    status_code = 409
    code = ErrorCode.MRP_RUN_IN_PROGRESS
    message = "A planning run is already in progress for this company."


# -----------------------------------------------------------------------------
# State machines - the only thing permitted to move a status column
# -----------------------------------------------------------------------------

RUN_LIFECYCLE: StateMachine[MrpRunStatus] = StateMachine(
    entity="MRP run",
    initial=MrpRunStatus.QUEUED,
    transitions=(
        Transition(MrpRunStatus.QUEUED, MrpRunStatus.RUNNING, label="Start"),
        Transition(MrpRunStatus.QUEUED, MrpRunStatus.FAILED, label="Fail"),
        Transition(MrpRunStatus.QUEUED, MrpRunStatus.CANCELLED, label="Cancel"),
        Transition(MrpRunStatus.RUNNING, MrpRunStatus.COMPLETED, label="Complete"),
        Transition(MrpRunStatus.RUNNING, MrpRunStatus.FAILED, label="Fail"),
        Transition(MrpRunStatus.RUNNING, MrpRunStatus.CANCELLED, label="Cancel"),
    ),
    terminal=frozenset({MrpRunStatus.COMPLETED, MrpRunStatus.FAILED, MrpRunStatus.CANCELLED}),
)

SUGGESTION_LIFECYCLE: StateMachine[MrpSuggestionStatus] = StateMachine(
    entity="MRP suggestion",
    initial=MrpSuggestionStatus.OPEN,
    transitions=(
        Transition(
            MrpSuggestionStatus.OPEN,
            MrpSuggestionStatus.CONVERTED,
            label="Convert",
            is_irreversible=True,
        ),
        Transition(MrpSuggestionStatus.OPEN, MrpSuggestionStatus.IGNORED, label="Ignore"),
    ),
    # Both ends are terminal on purpose. Re-opening a converted suggestion would
    # invite a second document for a shortage that has already been ordered, and
    # re-opening an ignored one hides that somebody rejected it - the next run
    # raises a fresh suggestion anyway.
    terminal=frozenset({MrpSuggestionStatus.CONVERTED, MrpSuggestionStatus.IGNORED}),
)


#: Service hooks the owning modules expose when they are deployed. Resolved by
#: name at call time - see the module docstring. Each takes the session, the
#: tenant and the typed :class:`ConversionRequest`, creates its document inside
#: the caller's transaction, and returns ``(document_id, document_code)``.
CONVERSION_HOOKS: dict[ConversionTarget, tuple[str, str]] = {
    ConversionTarget.PURCHASE_REQUISITION: (
        "src.modules.procurement.service",
        "create_requisition_from_mrp",
    ),
    ConversionTarget.PRODUCTION_ORDER: (
        "src.modules.production.service",
        "create_order_from_mrp",
    ),
    ConversionTarget.JOB_WORK_ORDER: (
        "src.modules.job_work.service",
        "create_order_from_mrp",
    ),
    ConversionTarget.STOCK_TRANSFER: (
        "src.modules.inventory.service",
        "create_transfer_from_mrp",
    ),
    ConversionTarget.REALLOCATION_REQUEST: (
        "src.modules.allocation.service",
        "create_reallocation_from_mrp",
    ),
}


def _target_label(target: ConversionTarget) -> str:
    """``JOB_WORK_ORDER`` -> ``job work order``, for messages."""
    return target.value.lower().replace("_", " ")


#: Why a deployed module cannot create the document from a planning request.
#: Read when a module is present but exposes no hook, so the planner is told
#: the real reason rather than "module not available".
MANUAL_CONVERSION_REASONS: dict[ConversionTarget, str] = {}


# -----------------------------------------------------------------------------
# Serialisation helpers
# -----------------------------------------------------------------------------


def _run_summary(run: MrpRun) -> MrpRunSummary:
    return MrpRunSummary.model_validate(run)


def _run_read(run: MrpRun, *, status_counts: dict[str, int] | None = None) -> MrpRunRead:
    """Serialise a run, lifting the caveats out of the summary blob.

    ``warnings`` lives inside ``result_summary`` in the database because it is
    part of the run's own record, but a client must not have to know the shape of
    a JSONB column to display "this plan did not net against incoming orders".
    """
    summary = run.result_summary or {}
    warnings = summary.get("warnings") or []
    return MrpRunRead(
        id=run.id,
        code=run.code,
        status=MrpRunStatus(run.status),
        mode=MrpRunMode(run.mode),
        horizon_days=run.horizon_days,
        warehouse_ids=list(run.warehouse_ids) if run.warehouse_ids else None,
        started_at=run.started_at,
        completed_at=run.completed_at,
        demand_line_count=run.demand_line_count,
        suggestion_count=run.suggestion_count,
        created_at=run.created_at,
        celery_task_id=run.celery_task_id,
        error_message=run.error_message,
        parameters=run.parameters,
        result_summary=summary or None,
        warnings=[str(w) for w in warnings],
        suggestion_counts_by_status=status_counts or {},
        status_url=status_url(run.id),
    )


def _safe_error(exc: BaseException) -> str:
    """A failure message safe to persist and show.

    An :class:`AppError` was raised deliberately and its message is written for a
    user. Anything else is a bug: the type name is recorded so the log line can be
    found, and nothing else - a driver error string can contain SQL and row data
    (section 53).
    """
    if isinstance(exc, AppError):
        return exc.message
    return (
        f"The planning run failed with an unexpected {type(exc).__name__}. "
        "The full error is in the server log against this run's request id."
    )


def _observe_run_outcome(run: MrpRun) -> None:
    """Count the run and time it, once its terminal status is on the row.

    Labelled on mode and outcome only - both are closed vocabularies, so the
    series count is bounded. The duration is measured from ``started_at``; a run
    that failed before it started has no runtime to report.
    """
    mrp_runs_total.labels(mode=str(run.mode), status=str(run.status)).inc()
    if run.started_at is not None and run.completed_at is not None:
        mrp_run_duration.observe((run.completed_at - run.started_at).total_seconds())


# -----------------------------------------------------------------------------
# Concurrency guard
# -----------------------------------------------------------------------------


def _lock_name(company_id: uuid.UUID) -> str:
    return f"mrp:run:{company_id}"


async def _acquire_planning_lock(company_id: uuid.UUID) -> str | None:
    """Take the company's planning lock, or refuse.

    A Redis outage degrades to ``None`` - "no lock held" - rather than to a 500.
    That is safe because the advisory lock and the active-run check inside the
    creating transaction are the actual correctness floor; the Redis lock exists
    to fail fast and to keep covering an asynchronous run after that transaction
    has committed.
    """
    try:
        token = await acquire_lock(_lock_name(company_id), ttl_seconds=RUN_LOCK_TTL_SECONDS)
    except Exception as exc:
        log.warning("mrp.lock_unavailable", company_id=str(company_id), error=str(exc))
        return None
    if token is None:
        raise MrpRunInProgressError()
    return token


async def release_planning_lock(company_id: uuid.UUID, token: str | None) -> None:
    """Release the planning lock if we still hold it.

    Public because the Celery task owns the lock for the life of an asynchronous
    run and has to release it when the run ends.
    """
    if not token:
        return
    try:
        await release_lock(_lock_name(company_id), token)
    except Exception as exc:  # pragma: no cover - Redis outage, already degraded
        log.warning("mrp.lock_release_failed", company_id=str(company_id), error=str(exc))


async def _close_abandoned_runs(session: AsyncSession, tenant: TenantContext) -> None:
    """Refuse if a run is genuinely in flight; close it off if it is not.

    Called inside the creating transaction and after the advisory lock, so two
    requests arriving together serialise here and the second one sees the first
    one's row.
    """
    runs = await MrpRunRepository(session, tenant).active_runs()
    cutoff = utcnow() - timedelta(seconds=RUN_STALE_AFTER_SECONDS)

    for run in runs:
        started = run.started_at or run.created_at
        if started > cutoff:
            log.info("mrp.run_already_in_progress", run_code=run.code, status=str(run.status))
            raise MrpRunInProgressError(
                details={
                    "run_id": str(run.id),
                    "code": run.code,
                    "status": str(run.status),
                    "started_at": started.isoformat(),
                }
            )

        previous = str(run.status)
        RUN_LIFECYCLE.assert_transition(MrpRunStatus(run.status), MrpRunStatus.FAILED)
        run.status = MrpRunStatus.FAILED
        run.error_message = _STALE_RUN_MESSAGE
        run.completed_at = utcnow()
        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type="mrp_run",
            entity_id=run.id,
            entity_code=run.code,
            from_status=previous,
            to_status=str(MrpRunStatus.FAILED),
            user_id=tenant.user_id,
            reason=_STALE_RUN_MESSAGE,
        )
        log.warning("mrp.abandoned_run_closed", run_code=run.code, previous_status=previous)


# -----------------------------------------------------------------------------
# Starting a run
# -----------------------------------------------------------------------------


def _manual_demand_lines(payload: MrpRunRequest) -> list[engine.DemandLine]:
    return [
        engine.DemandLine(
            item_id=line.item_id,
            quantity=to_quantity(line.quantity),
            required_date=line.required_date,
            source=MrpDemandSource.MANUAL,
            warehouse_id=line.warehouse_id,
            notes=line.notes,
        )
        for line in payload.manual_demand
    ]


async def _collect_demand(
    session: AsyncSession,
    tenant: TenantContext,
    payload: MrpRunRequest,
    *,
    horizon_end: date,
) -> list[engine.DemandLine]:
    """The order book plus any manual lines, in one statement plus arithmetic.

    Read *before* the run row is created so the mode decision - inline or Celery -
    is taken on the real line count rather than on a guess.
    """
    demand = await SalesDemandRepository(session, tenant).open_demand(
        horizon_end=horizon_end, warehouse_ids=payload.warehouse_ids or None
    )
    demand.extend(_manual_demand_lines(payload))
    if len(demand) > MAX_DEMAND_LINES:
        raise engine.MrpDemandTooLargeError(len(demand), MAX_DEMAND_LINES)
    return demand


def _demand_rows(
    run: MrpRun, tenant: TenantContext, demand: Sequence[engine.DemandLine]
) -> list[MrpDemand]:
    return [
        MrpDemand(
            company_id=tenant.company_id,
            created_by_id=tenant.user_id,
            run_id=run.id,
            demand_source=line.source,
            source_id=line.source_id,
            source_line_id=line.source_line_id,
            source_code=line.source_code,
            item_id=line.item_id,
            warehouse_id=line.warehouse_id,
            quantity=to_quantity(line.quantity),
            required_date=line.required_date,
            priority=line.priority,
            notes=line.notes,
        )
        for line in demand
    ]


async def start_run(
    session: AsyncSession, tenant: TenantContext, payload: MrpRunRequest
) -> tuple[MrpRunRead, bool]:
    """Create a run, snapshot its demand, and either execute it or dispatch it.

    Returns the run and whether it was handed to a worker. The router turns the
    second element into ``202`` plus a status URL rather than deciding for itself
    - the execution mode is a business rule, not a transport detail.

    Raises :class:`~src.core.errors.RateLimitedError` (429, with ``Retry-After``)
    when the company has already started ``RateLimitPolicy.MRP_RUN``'s allowance
    of runs inside the window, and :class:`MrpRunInProgressError` (409) when one
    is still in flight.
    """
    # The most expensive operation in the product: every BOM exploded for every
    # demand line across the horizon. The planning lock below stops two runs
    # overlapping, but nothing in it stops a client firing runs back to back for
    # ever and keeping a worker permanently busy, so the budget is spent here -
    # before the lock is taken, before a row is read - and a refusal costs a
    # single Redis round-trip.
    #
    # Keyed on the company, and only ever on the company: it comes from the
    # TenantContext derived from the verified token, so one tenant cannot spend
    # another's budget and no client-supplied value can move the key. A company's
    # planners deliberately share one budget - what is being rationed is the cost
    # of planning the company's order book, which is the same cost whoever asks.
    #
    # ASSUMPTION: an attempt that is refused further down - a run already in
    # progress (409), demand over MAX_DEMAND_LINES - still spends a token. That
    # is the same rule login follows, and it is what makes the limit bite on a
    # client retrying in a tight loop, which is the behaviour being bounded.
    await enforce_policy(RateLimitPolicy.MRP_RUN, str(tenant.company_id))

    as_of = today()
    horizon_end = as_of + timedelta(days=payload.horizon_days)

    token = await _acquire_planning_lock(tenant.company_id)
    hand_over_lock = False
    try:
        demand = await _collect_demand(session, tenant, payload, horizon_end=horizon_end)
        mode = (
            MrpRunMode.ASYNC
            if len(demand) > settings.mrp_sync_threshold_demand_lines
            else MrpRunMode.SYNC
        )

        async with transaction(session):
            # Serialises two simultaneous starts so the active-run check below
            # cannot be passed by both. Released at COMMIT; there is no unlock
            # path to forget.
            await advisory_xact_lock(session, "mrp-run", tenant.company_id)
            await _close_abandoned_runs(session, tenant)

            repository = MrpRunRepository(session, tenant)
            run = MrpRun(
                code=await next_document_number(session, tenant.company_id, DocTypes.MRP_RUN),
                status=MrpRunStatus.QUEUED,
                mode=mode,
                horizon_days=payload.horizon_days,
                warehouse_ids=list(payload.warehouse_ids) or None,
                demand_line_count=len(demand),
                parameters={
                    "horizon_days": payload.horizon_days,
                    "horizon_end": horizon_end.isoformat(),
                    "as_of": as_of.isoformat(),
                    "warehouse_ids": [str(w) for w in payload.warehouse_ids],
                    "respect_safety_stock": payload.respect_safety_stock,
                    "manual_demand_lines": len(payload.manual_demand),
                    "notes": payload.notes,
                },
            )
            repository.add(run)
            await session.flush()

            session.add_all(_demand_rows(run, tenant, demand))
            await session.flush()

            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.CREATE,
                entity_type="mrp_run",
                entity_id=run.id,
                entity_code=run.code,
                summary=(
                    f"MRP run {run.code} created over {payload.horizon_days} days "
                    f"with {len(demand)} demand line(s), mode {mode}"
                ),
                user_id=tenant.user_id,
                new_values={
                    "horizon_days": payload.horizon_days,
                    "mode": str(mode),
                    "demand_line_count": len(demand),
                    "warehouse_ids": [str(w) for w in payload.warehouse_ids],
                },
            )

        run_id = run.id
        if mode is MrpRunMode.ASYNC:
            await _dispatch(session, tenant, run, token)
            # The worker owns the lock now and releases it when the run ends; if
            # the worker never starts, the Redis TTL clears it.
            hand_over_lock = True
        else:
            await _run_inline(session, tenant, run_id)

        refreshed = await MrpRunRepository(session, tenant).get_or_404(run_id)
        return _run_read(refreshed), mode is MrpRunMode.ASYNC
    finally:
        if not hand_over_lock:
            await release_planning_lock(tenant.company_id, token)


async def _dispatch(
    session: AsyncSession, tenant: TenantContext, run: MrpRun, token: str | None
) -> None:
    """Hand the run to Celery and record the task id.

    Imported here rather than at module scope: the API process does not need a
    broker connection to serve a read, and a broker that is unreachable must fail
    *this* request rather than application startup.
    """
    from src.core.logging import get_request_id
    from src.workers.celery_app import celery_app

    # The kwargs are evaluated here, on the event loop, so the request id comes
    # from this request's context; only the blocking broker round-trip is moved
    # off the loop, where it would otherwise stall every other request for as
    # long as the broker takes to answer.
    payload = {
        "run_id": str(run.id),
        "company_id": str(tenant.company_id),
        "user_id": str(tenant.user_id),
        "lock_token": token,
        "request_id": get_request_id(),
    }
    try:
        task = await asyncio.to_thread(
            celery_app.send_task, MRP_TASK_NAME, kwargs=payload, queue="mrp"
        )
    except Exception as exc:
        log.error("mrp.dispatch_failed", run_code=run.code, error=str(exc))
        await fail_run(session, tenant, run_id=run.id, error=_safe_error(exc))
        raise AppError(
            "The planning run could not be queued for background processing.",
            code=ErrorCode.SERVICE_UNAVAILABLE,
            status_code=503,
        ) from exc

    async with transaction(session):
        run.celery_task_id = task.id
        MrpRunRepository(session, tenant).touch(run)
    log.info("mrp.run_dispatched", run_code=run.code, task_id=task.id)


async def _run_inline(session: AsyncSession, tenant: TenantContext, run_id: uuid.UUID) -> None:
    """Execute a small run on the request's own session.

    The failed calculation is unwound by :func:`~src.core.database.transaction`
    itself - every statement :func:`execute_run` issues is inside one of its
    scopes, and an exception leaving a scope rolls that scope back and leaves the
    connection able to accept the next statement. So the failure record can be
    written straight away.

    This deliberately does **not** call ``session.rollback()``. That unwinds the
    *outermost* transaction, not the failed scope: on a request session the run
    row and its demand snapshot were written in ``start_run``'s enclosing scope,
    and rolling the session back destroys them - leaving no run, no snapshot and
    no FAILED record for the 500 the caller just received, which is the opposite
    of what this function is for.
    """
    try:
        await execute_run(session, tenant, run_id=run_id)
    except Exception as exc:
        try:
            await fail_run(session, tenant, run_id=run_id, error=_safe_error(exc))
        except Exception:
            # The original failure is re-raised below and logged by the unhandled
            # handler. This one is different: the FAILED record could not be
            # written, so the run stays RUNNING until ``_close_abandoned_runs``
            # declares it dead and every ``start_run`` until then answers 409.
            # An operator asking "why is planning blocked" needs this line.
            log.exception("mrp.fail_run_failed", run_id=str(run_id))
        raise


# -----------------------------------------------------------------------------
# Executing a run
# -----------------------------------------------------------------------------


async def _snapshot_demand(
    session: AsyncSession, tenant: TenantContext, run_id: uuid.UUID
) -> list[engine.DemandLine]:
    """Replay the demand the run was created with.

    One statement, bounded by the same limit the engine enforces. Reading the
    snapshot rather than the live order book is what makes a plan reproducible:
    a worker picking the task up ten minutes later must plan the same thing the
    API counted, not whatever the sales desk has entered since.
    """
    demands = await MrpDemandRepository(session, tenant).all_for_run(run_id)
    return [
        engine.DemandLine(
            item_id=row.item_id,
            quantity=Decimal(str(row.quantity)),
            required_date=row.required_date,
            source=MrpDemandSource(row.demand_source),
            demand_id=row.id,
            source_id=row.source_id,
            source_line_id=row.source_line_id,
            source_code=row.source_code,
            warehouse_id=row.warehouse_id,
            priority=row.priority,
            notes=row.notes,
        )
        for row in demands
    ]


def _requirement_rows(
    run: MrpRun, tenant: TenantContext, plan: engine.MrpPlan
) -> list[MrpRequirement]:
    return [
        MrpRequirement(
            company_id=tenant.company_id,
            created_by_id=tenant.user_id,
            run_id=run.id,
            item_id=requirement.item_id,
            item_code=requirement.item_code,
            level=requirement.level,
            low_level_code=requirement.low_level_code,
            direct_demand_quantity=requirement.direct_demand_quantity,
            dependent_demand_quantity=requirement.dependent_demand_quantity,
            gross_requirement=requirement.gross_requirement,
            on_hand_quantity=requirement.on_hand_quantity,
            reserved_quantity=requirement.reserved_quantity,
            available_quantity=requirement.available_quantity,
            incoming_quantity=requirement.incoming_quantity,
            in_production_quantity=requirement.in_production_quantity,
            job_work_quantity=requirement.job_work_quantity,
            late_incoming_quantity=requirement.late_incoming_quantity,
            late_in_production_quantity=requirement.late_in_production_quantity,
            late_job_work_quantity=requirement.late_job_work_quantity,
            first_late_incoming_date=requirement.first_late_incoming_date,
            first_late_in_production_date=requirement.first_late_in_production_date,
            first_late_job_work_date=requirement.first_late_job_work_date,
            # The closing half of each interval. Persisting the opening date
            # alone republishes the defect the pair exists to fix: the quantity
            # is a SUM over that feed's late tranches, so one date beside it
            # attributes the whole amount to the first tranche.
            last_late_incoming_date=requirement.last_late_incoming_date,
            last_late_in_production_date=requirement.last_late_in_production_date,
            last_late_job_work_date=requirement.last_late_job_work_date,
            # Derived on the engine's requirement from the per-feed dates above, so
            # the persisted summary cannot disagree with the pair it summarises
            # - which is also what the row's CHECK constraint asserts.
            first_late_supply_date=requirement.first_late_supply_date,
            net_requirement=requirement.net_requirement,
            planned_reallocation_quantity=requirement.planned_reallocation_quantity,
            shortage_quantity=requirement.shortage_quantity,
            earliest_required_date=requirement.earliest_required_date,
            expected_available_date=requirement.expected_available_date,
            source_demand_count=requirement.source_demand_count,
            source_demands=list(requirement.source_demands) or None,
        )
        for requirement in plan.requirements
    ]


def _suggestion_rows(
    run: MrpRun, tenant: TenantContext, plan: engine.MrpPlan
) -> list[MrpSuggestion]:
    return [
        MrpSuggestion(
            company_id=tenant.company_id,
            created_by_id=tenant.user_id,
            run_id=run.id,
            item_id=suggestion.item_id,
            item_code=suggestion.item_code,
            suggestion_type=suggestion.suggestion_type,
            required_quantity=suggestion.required_quantity,
            suggested_quantity=suggestion.suggested_quantity,
            shortage_quantity=suggestion.shortage_quantity,
            available_quantity=suggestion.available_quantity,
            incoming_quantity=suggestion.incoming_quantity,
            required_date=suggestion.required_date,
            suggested_order_date=suggestion.suggested_order_date,
            is_late=suggestion.is_late,
            lead_time_days=suggestion.lead_time_days,
            low_level_code=suggestion.low_level_code,
            expected_available_date=suggestion.expected_available_date,
            from_warehouse_id=suggestion.from_warehouse_id,
            to_warehouse_id=suggestion.to_warehouse_id,
            from_reservation_id=suggestion.from_reservation_id,
            from_demand_type=suggestion.from_demand_type,
            from_demand_id=suggestion.from_demand_id,
            from_demand_code=suggestion.from_demand_code,
            to_demand_type=suggestion.to_demand_type,
            to_demand_id=suggestion.to_demand_id,
            to_demand_line_id=suggestion.to_demand_line_id,
            to_demand_code=suggestion.to_demand_code,
            to_demand_priority=suggestion.to_demand_priority,
            source_demands=list(suggestion.source_demands) or None,
            status=MrpSuggestionStatus.OPEN,
        )
        for suggestion in plan.suggestions
    ]


async def execute_run(
    session: AsyncSession, tenant: TenantContext, *, run_id: uuid.UUID
) -> MrpRunRead:
    """Calculate the plan for a QUEUED run and persist everything it produced.

    Called by the Celery task and, for a small run, directly by
    :func:`start_run`. Three scopes: the QUEUED -> RUNNING transition, the
    calculation, and then the whole of the output plus the COMPLETED transition
    plus the audit entry in one transaction, so a plan is never half-saved. The
    run row is locked and re-read at each transition rather than trusted from the
    copy loaded before it - see :meth:`MrpRunRepository.lock_for_update`.
    """
    repository = MrpRunRepository(session, tenant)

    async with transaction(session):
        # Locked, not merely read. ``task_acks_late`` means this task can be
        # delivered twice; without the lock both deliveries read QUEUED, both
        # pass the transition check and both compute and write a full plan - the
        # second one dying on the (run_id, item_id) unique index as an
        # unexplained integrity error rather than the clean refusal the worker
        # documents. The lock makes the second delivery see RUNNING.
        run = await repository.lock_for_update(run_id)
        if run is None:
            raise NotFoundError(MRP_RUN_ENTITY, run_id)
        previous = str(run.status)
        RUN_LIFECYCLE.assert_transition(MrpRunStatus(run.status), MrpRunStatus.RUNNING)
        run.status = MrpRunStatus.RUNNING
        run.started_at = utcnow()
        repository.touch(run)
        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type="mrp_run",
            entity_id=run.id,
            entity_code=run.code,
            from_status=previous,
            to_status=str(MrpRunStatus.RUNNING),
            user_id=tenant.user_id,
        )

    parameters = run.parameters or {}
    as_of = _parse_date(parameters.get("as_of")) or (
        run.started_at.date() if run.started_at else today()
    )
    horizon_end = _parse_date(parameters.get("horizon_end")) or (
        as_of + timedelta(days=run.horizon_days)
    )
    respect_safety_stock = bool(parameters.get("respect_safety_stock", True))

    # The calculation is reads only, but it is still bracketed: a database error
    # in the middle of it - a statement timeout on a large stock aggregate, say -
    # leaves the connection refusing further statements until *something* rolls
    # back, and the only rollback that must not happen is the one that would take
    # the run row and its demand snapshot with it. Scoping the reads means the
    # failure is unwound here and :func:`fail_run` can still record it.
    async with transaction(session):
        demand = await _snapshot_demand(session, tenant, run.id)
        source = DatabaseMrpSource(session, tenant, warehouse_ids=run.warehouse_ids)

        async def reallocation_provider(
            targets: Sequence[engine.ReallocationTarget],
        ) -> Sequence[engine.ReallocationRoute]:
            from src.modules.allocation.models import DemandType
            from src.modules.allocation.service import (
                ReallocationBatchTarget,
                suggest_reallocations_batch,
            )

            targets_by_line = {
                (target.demand_id, target.demand_line_id): target for target in targets
            }
            ranked = await suggest_reallocations_batch(
                session,
                tenant,
                [
                    ReallocationBatchTarget(
                        item_id=target.item_id,
                        warehouse_id=target.warehouse_id,
                        demand_type=DemandType.SALES_ORDER,
                        demand_id=target.demand_id,
                        demand_line_id=target.demand_line_id,
                        demand_code=target.demand_code,
                        quantity=target.quantity,
                        priority=target.priority,
                        required_date=target.required_date,
                    )
                    for target in targets
                ],
                as_of=as_of,
            )
            return [
                engine.ReallocationRoute(
                    target=targets_by_line[(result.target.demand_id, result.target.demand_line_id)],
                    reservation_id=result.candidate.reservation_id,
                    donor_demand_type=result.candidate.demand_type.value,
                    donor_demand_id=result.candidate.demand_id,
                    donor_demand_code=result.candidate.demand_code,
                    quantity=result.candidate.takeable_quantity,
                )
                for result in ranked
            ]

        plan = await engine.plan(
            source,
            demands=demand,
            as_of=as_of,
            horizon_end=horizon_end,
            respect_safety_stock=respect_safety_stock,
            reallocation_provider=reallocation_provider,
        )

    async with transaction(session):
        # Re-locked and re-read: the calculation above can take minutes, the row
        # lock taken for the RUNNING transition was released at the end of that
        # transaction, and ``_close_abandoned_runs`` may have declared this run
        # dead in the meantime. Writing COMPLETED over that FAILED would resurrect
        # a run somebody was told had produced nothing, and would do it without
        # any legal edge in the state machine.
        run = await repository.lock_for_update(run_id)
        if run is None:
            raise NotFoundError(MRP_RUN_ENTITY, run_id)

        session.add_all(_requirement_rows(run, tenant, plan))
        session.add_all(_suggestion_rows(run, tenant, plan))
        # The demand snapshot was written when the run was *created*; the date
        # each line is expected to be covered is only known now, so it is filled
        # in here - one statement, in the same transaction as the requirement
        # rows it is copied from, so the two can never disagree.
        await MrpDemandRepository(session, tenant).set_expected_availability(
            run.id,
            {
                requirement.item_id: requirement.expected_available_date
                for requirement in plan.requirements
                if requirement.expected_available_date is not None
            },
        )

        previous = str(run.status)
        RUN_LIFECYCLE.assert_transition(MrpRunStatus(run.status), MrpRunStatus.COMPLETED)
        run.status = MrpRunStatus.COMPLETED
        run.completed_at = utcnow()
        _observe_run_outcome(run)
        run.suggestion_count = len(plan.suggestions)
        run.result_summary = {**plan.stats, "warnings": list(plan.warnings)}
        repository.touch(run)

        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type="mrp_run",
            entity_id=run.id,
            entity_code=run.code,
            from_status=previous,
            to_status=str(MrpRunStatus.COMPLETED),
            user_id=tenant.user_id,
            action=AuditAction.MRP_RUN,
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.MRP_RUN,
            entity_type="mrp_run",
            entity_id=run.id,
            entity_code=run.code,
            summary=(
                f"MRP run {run.code} produced {len(plan.suggestions)} suggestion(s) "
                f"from {len(plan.requirements)} requirement(s)"
            ),
            user_id=tenant.user_id,
            new_values=dict(plan.stats),
            context={"warnings": list(plan.warnings)} if plan.warnings else None,
        )

    log.info(
        "mrp.run_completed",
        run_code=run.code,
        requirements=len(plan.requirements),
        suggestions=len(plan.suggestions),
    )
    return _run_read(run)


async def fail_run(
    session: AsyncSession, tenant: TenantContext, *, run_id: uuid.UUID, error: str
) -> None:
    """Record that a run did not produce a plan.

    Separate from :func:`execute_run` so a worker can call it on a *fresh*
    session after the one that failed has been discarded. A run left sitting in
    RUNNING would block the next one until it aged out.
    """
    repository = MrpRunRepository(session, tenant)

    async with transaction(session):
        # Same read-check-write as every other transition here, and the same
        # lock: a worker recording its failure races the API closing the run off
        # as abandoned, and whichever read first would otherwise overwrite a
        # terminal status it never saw.
        run = await repository.lock_for_update(run_id)
        if run is None:
            log.error("mrp.fail_run_missing", run_id=str(run_id))
            return
        if MrpRunStatus(run.status) in RUN_LIFECYCLE.terminal:
            return

        previous = str(run.status)
        RUN_LIFECYCLE.assert_transition(MrpRunStatus(run.status), MrpRunStatus.FAILED)
        run.status = MrpRunStatus.FAILED
        run.error_message = error[:2000]
        run.completed_at = utcnow()
        _observe_run_outcome(run)
        repository.touch(run)
        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type="mrp_run",
            entity_id=run.id,
            entity_code=run.code,
            from_status=previous,
            to_status=str(MrpRunStatus.FAILED),
            user_id=tenant.user_id,
            action=AuditAction.JOB_FAILED,
            reason=error[:255],
        )
    log.error("mrp.run_failed", run_code=run.code, error=error)


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    with contextlib.suppress(ValueError):
        return date.fromisoformat(value)
    return None


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


async def list_runs(
    session: AsyncSession,
    tenant: TenantContext,
    filters: MrpRunFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[MrpRunSummary], OffsetPagination]:
    rows, pagination = await MrpRunRepository(session, tenant).list_page_filtered(
        params,
        sort,
        status=filters.status,
        created_from=filters.created_from,
        created_to=filters.created_to,
        search=filters.search,
    )
    return [_run_summary(row) for row in rows], pagination


async def get_run(session: AsyncSession, tenant: TenantContext, run_id: uuid.UUID) -> MrpRunRead:
    """One run with its status, its caveats and its suggestion counts.

    Two statements: the run and one grouped count. The counts are what a planner
    actually looks at on this screen - "180 suggestions, 40 already converted" -
    and computing them by loading the suggestions would be the N+1 in disguise.
    """
    run = await MrpRunRepository(session, tenant).get_or_404(run_id)
    counts = await MrpSuggestionRepository(session, tenant).count_by_status(run.id)
    return _run_read(run, status_counts=counts)


async def list_run_demand(
    session: AsyncSession, tenant: TenantContext, run_id: uuid.UUID, params: PageParams
) -> tuple[list[MrpDemandRead], OffsetPagination]:
    await MrpRunRepository(session, tenant).get_or_404(run_id)
    rows, pagination = await MrpDemandRepository(session, tenant).list_for_run(run_id, params)
    return [MrpDemandRead.model_validate(row) for row in rows], pagination


async def sales_order_availability(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> SalesOrderAvailabilityRead:
    """When the last plan that saw this order expects each line to be covered.

    Two statements: the newest completed run whose snapshot contained the order,
    then that run's rows for the order. The question is deliberately historical.
    "Is there stock now?" is the allocation endpoint's job; a promised date has
    to be defensible against the plan it was taken from, so it comes from the
    stamped demand row rather than a fresh calculation.
    """
    demands = MrpDemandRepository(session, tenant)
    run = await demands.latest_completed_run_for_sales_order(order_id)
    if run is None:
        return SalesOrderAvailabilityRead()
    rows = await demands.sales_order_lines_for_run(run.id, order_id)
    return SalesOrderAvailabilityRead(
        run_id=run.id,
        run_code=run.code,
        completed_at=run.completed_at,
        lines=[
            SalesOrderLineAvailabilityRead(
                sales_order_line_id=row.source_line_id,
                item_id=row.item_id,
                expected_available_date=row.expected_available_date,
            )
            for row in rows
            if row.source_line_id is not None
        ],
    )


async def list_suggestions(
    session: AsyncSession,
    tenant: TenantContext,
    filters: SuggestionFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[MrpSuggestionRead], OffsetPagination]:
    rows, pagination = await MrpSuggestionRepository(session, tenant).list_page_filtered(
        params,
        sort,
        run_id=filters.run_id,
        suggestion_type=filters.suggestion_type,
        status=filters.status,
        item_id=filters.item_id,
        late_only=filters.late_only,
        search=filters.search,
    )
    return [MrpSuggestionRead.model_validate(row) for row in rows], pagination


async def shortage_report(
    session: AsyncSession,
    tenant: TenantContext,
    filters: ShortageReportFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[MrpRequirementRead], OffsetPagination]:
    """Short items from one run, defaulting to the latest completed plan.

    A company that has never completed a run has no shortages, so an empty page
    is the honest answer rather than a 404 - the screen is a report, not a
    document.
    """
    runs = MrpRunRepository(session, tenant)
    run = await runs.get_or_404(filters.run_id) if filters.run_id else await runs.latest_completed()
    if run is None:
        return [], OffsetPagination(page=params.page, limit=params.limit, total=0, total_pages=1)

    rows, pagination = await MrpRequirementRepository(session, tenant).list_page_filtered(
        run.id,
        params,
        sort,
        item_id=filters.item_id,
        shortage_only=filters.shortage_only,
        search=filters.search,
    )
    return [MrpRequirementRead.model_validate(row) for row in rows], pagination


# -----------------------------------------------------------------------------
# Acting on suggestions
# -----------------------------------------------------------------------------


def _conversion_target(
    suggestion: MrpSuggestion, requested: ConversionTarget | None
) -> ConversionTarget:
    suggestion_type = MrpSuggestionType(suggestion.suggestion_type)
    target = requested or DEFAULT_CONVERSION_TARGET.get(suggestion_type)
    if target is None:
        raise BusinessRuleError(
            f"A {suggestion.suggestion_type} suggestion has no default conversion target.",
            details={"suggestion_type": str(suggestion.suggestion_type)},
        )
    if (suggestion_type is MrpSuggestionType.TRANSFER) != (
        target is ConversionTarget.STOCK_TRANSFER
    ) or (suggestion_type is MrpSuggestionType.REALLOCATION) != (
        target is ConversionTarget.REALLOCATION_REQUEST
    ):
        raise BusinessRuleError(
            "Transfer and reallocation suggestions must use their matching document "
            "types, and no other suggestion can create those documents.",
            details={"suggestion_type": suggestion_type.value, "target": target.value},
        )
    if suggestion_type is MrpSuggestionType.TRANSFER and (
        suggestion.from_warehouse_id is None or suggestion.to_warehouse_id is None
    ):
        raise BusinessRuleError(
            "This transfer suggestion has no source and destination warehouse; "
            "create the transfer manually.",
            details={"suggestion_id": str(suggestion.id)},
        )
    if suggestion_type is MrpSuggestionType.REALLOCATION and any(
        value is None
        for value in (
            suggestion.from_reservation_id,
            suggestion.from_demand_type,
            suggestion.from_demand_id,
            suggestion.to_demand_type,
            suggestion.to_demand_id,
            suggestion.to_demand_line_id,
            suggestion.to_demand_priority,
            suggestion.from_warehouse_id,
        )
    ):
        raise BusinessRuleError(
            "This reallocation suggestion is missing its donor or sales-order target; "
            "create the reallocation manually.",
            details={"suggestion_id": str(suggestion.id)},
        )
    return target


def _conversion_request(
    run: MrpRun,
    suggestion: MrpSuggestion,
    target: ConversionTarget,
    payload: SuggestionConvertRequest,
) -> ConversionRequest:
    return ConversionRequest(
        suggestion_id=suggestion.id,
        run_id=suggestion.run_id,
        target=target,
        item_id=suggestion.item_id,
        item_code=suggestion.item_code,
        quantity=to_quantity(payload.quantity or suggestion.suggested_quantity),
        required_date=payload.required_date or suggestion.required_date,
        suggested_order_date=suggestion.suggested_order_date,
        warehouse_ids=list(run.warehouse_ids) if run.warehouse_ids else [],
        source_demands=list(suggestion.source_demands or []),
        vendor_id=payload.vendor_id,
        process_type=payload.process_type,
        expected_return_date=payload.expected_return_date,
        warehouse_id=payload.warehouse_id,
        from_warehouse_id=suggestion.from_warehouse_id,
        to_warehouse_id=suggestion.to_warehouse_id,
        from_reservation_id=suggestion.from_reservation_id,
        from_demand_type=suggestion.from_demand_type,
        from_demand_id=suggestion.from_demand_id,
        from_demand_code=suggestion.from_demand_code,
        to_demand_type=suggestion.to_demand_type,
        to_demand_id=suggestion.to_demand_id,
        to_demand_line_id=suggestion.to_demand_line_id,
        to_demand_code=suggestion.to_demand_code,
        to_demand_priority=suggestion.to_demand_priority,
        notes=payload.notes,
    )


async def _invoke_conversion_hook(
    session: AsyncSession,
    tenant: TenantContext,
    target: ConversionTarget,
    request: ConversionRequest,
) -> tuple[uuid.UUID, str] | None:
    """Create the document through the owning module, if that module is deployed.

    THE SEAM. Resolved by name at call time so this module never imports
    procurement, production or job work - all three are built independently of
    planning and any of them may be absent. The hook runs inside the caller's
    transaction, so a document and the suggestion that became it are committed
    together or not at all.

    Contract::

        async def create_..._from_mrp(
            session, tenant, request: ConversionRequest
        ) -> tuple[uuid.UUID, str]      # (document_id, document_code)

    Returns ``None`` when there is nothing to call. The caller then hands the
    typed request back to the API client, which is the difference between a
    documented seam and a silent no-op.
    """
    module_name, function_name = CONVERSION_HOOKS[target]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return None

    hook = getattr(module, function_name, None)
    if hook is None:
        # The module is deployed but does not take conversions from planning.
        # That is not the "module absent" case the seam tolerates: closing the
        # suggestion here would tell the planner a deployment gap explained a
        # document that was never going to be created. Refuse instead, with the
        # reason, and leave the suggestion OPEN for the planner to action.
        raise BusinessRuleError(
            MANUAL_CONVERSION_REASONS.get(
                target,
                f"The {_target_label(target)} module does not accept conversions from "
                "planning; raise the document by hand and then ignore the suggestion.",
            ),
            details={"target": target.value, "item_code": request.item_code},
        )

    created = await hook(session, tenant, request)
    if created is None:
        return None
    document_id, document_code = created
    return uuid.UUID(str(document_id)), str(document_code)


async def convert_suggestion(
    session: AsyncSession,
    tenant: TenantContext,
    suggestion_id: uuid.UUID,
    payload: SuggestionConvertRequest,
) -> SuggestionConversionResult:
    """Turn one open suggestion into a purchase requisition or a production order.

    The status change, the document (where one can be created) and the audit
    entry are one transaction. A suggestion that says CONVERTED while its
    requisition rolled back would send a buyer looking for a document that does
    not exist.
    """
    suggestions = MrpSuggestionRepository(session, tenant)

    async with transaction(session):
        # The row is locked *before* its status is read, and read only through
        # the lock. Two planners converting the same suggestion at the same
        # moment would otherwise both see OPEN, both pass the transition check
        # and both run the conversion hook - two purchase requisitions for one
        # shortage, which is precisely the duplicate order this module exists to
        # avoid. With the lock the second one waits, re-reads CONVERTED and is
        # refused by the state machine.
        suggestion = await suggestions.lock_for_update(suggestion_id)
        if suggestion is None:
            raise NotFoundError(MRP_SUGGESTION_ENTITY, suggestion_id)
        run = await MrpRunRepository(session, tenant).get_or_404(suggestion.run_id)

        target = _conversion_target(suggestion, payload.target)
        request = _conversion_request(run, suggestion, target, payload)

        previous = str(suggestion.status)
        SUGGESTION_LIFECYCLE.assert_transition(
            MrpSuggestionStatus(suggestion.status), MrpSuggestionStatus.CONVERTED
        )

        created = await _invoke_conversion_hook(session, tenant, target, request)

        suggestion.status = MrpSuggestionStatus.CONVERTED
        # ASSUMPTION: the suggestion is marked CONVERTED even when the owning
        # module is absent and no document was created. The planner has made the
        # decision, and leaving it OPEN would have the next screen offer it again.
        # `handled` on the result - and a NULL converted_to_id - are what say
        # whether a document actually exists, so the gap is visible rather than
        # implied by a status.
        suggestion.converted_to_type = target.value.lower()
        suggestion.converted_to_id = created[0] if created else None
        suggestion.converted_at = utcnow()
        suggestions.touch(suggestion)

        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type="mrp_suggestion",
            entity_id=suggestion.id,
            entity_code=suggestion.item_code,
            from_status=previous,
            to_status=str(MrpSuggestionStatus.CONVERTED),
            user_id=tenant.user_id,
            reason=(
                f"Converted to {target.value}"
                + (f" {created[1]}" if created else " (document creation deferred)")
            ),
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type="mrp_suggestion",
            entity_id=suggestion.id,
            entity_code=suggestion.item_code,
            summary=f"MRP suggestion for {suggestion.item_code} converted to {target.value}",
            user_id=tenant.user_id,
            new_values={
                "target": target.value,
                "quantity": request.quantity,
                "required_date": request.required_date,
                "document_id": created[0] if created else None,
                "document_code": created[1] if created else None,
                # Recorded because a planner overriding the engine's advice - buying
                # something the item master says is made here - is a decision worth
                # being able to find later.
                "overrode_suggestion_type": payload.target is not None,
            },
        )

    message = (
        f"Converted to {target.value} {created[1]}."
        if created
        else (
            f"The {_target_label(target)} module is not available, so no "
            "document was created. The suggestion is closed and the conversion request "
            "is returned for the caller to action."
        )
    )
    return SuggestionConversionResult(
        suggestion=MrpSuggestionRead.model_validate(suggestion),
        request=request,
        handled=created is not None,
        created_document_type=target.value.lower() if created else None,
        created_document_id=created[0] if created else None,
        created_document_code=created[1] if created else None,
        message=message,
    )


async def bulk_ignore_suggestions(
    session: AsyncSession, tenant: TenantContext, payload: BulkIgnoreRequest
) -> BulkIgnoreResult:
    """Dismiss several suggestions with one shared reason.

    Suggestions that are not open - already converted, already ignored, or not in
    this tenant - are reported as skipped rather than failing the whole call: a
    planner selecting a page should not have the action refused because one row
    moved underneath them, but must be told which ones were left alone.

    Every dismissal still writes its own audit row. The batch is bounded so that
    stays a transaction that finishes inside a request.
    """
    repository = MrpSuggestionRepository(session, tenant)

    ignored: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []

    async with transaction(session):
        # Loaded *inside* the transaction and locked, in id order. Read outside
        # it, the statuses this loop decides on are already stale by the time it
        # writes: a conversion committing in between leaves the row IGNORED with
        # a converted_at still set, which the conversion_timestamp_implies_
        # converted check rejects as an integrity error the planner cannot act on.
        found = await repository.load_for_bulk_decision(payload.suggestion_ids)
        by_id = {row.id: row for row in found}

        for suggestion_id in payload.suggestion_ids:
            suggestion = by_id.get(suggestion_id)
            if suggestion is None or not SUGGESTION_LIFECYCLE.can_transition(
                MrpSuggestionStatus(suggestion.status), MrpSuggestionStatus.IGNORED
            ):
                skipped.append(suggestion_id)
                continue

            previous = str(suggestion.status)
            suggestion.status = MrpSuggestionStatus.IGNORED
            suggestion.ignored_reason = payload.reason
            repository.touch(suggestion)
            await record_status_change(
                session,
                company_id=tenant.company_id,
                entity_type="mrp_suggestion",
                entity_id=suggestion.id,
                entity_code=suggestion.item_code,
                from_status=previous,
                to_status=str(MrpSuggestionStatus.IGNORED),
                user_id=tenant.user_id,
                reason=payload.reason,
            )
            ignored.append(suggestion_id)

    log.info("mrp.suggestions_ignored", ignored=len(ignored), skipped=len(skipped))
    return BulkIgnoreResult(
        ignored_count=len(ignored),
        ignored_ids=ignored,
        skipped_ids=skipped,
        message=(
            f"{len(ignored)} suggestion(s) ignored"
            + (f", {len(skipped)} skipped because they were no longer open" if skipped else "")
        ),
    )


async def get_suggestion(
    session: AsyncSession, tenant: TenantContext, suggestion_id: uuid.UUID
) -> MrpSuggestionRead:
    found = await MrpSuggestionRepository(session, tenant).get_or_404(suggestion_id)
    return MrpSuggestionRead.model_validate(found)


__all__ = [
    "CONVERSION_HOOKS",
    "MRP_TASK_NAME",
    "RUN_LIFECYCLE",
    "RUN_LOCK_TTL_SECONDS",
    "RUN_STALE_AFTER_SECONDS",
    "STATUS_URL_TEMPLATE",
    "SUGGESTION_LIFECYCLE",
    "MrpRunInProgressError",
    "bulk_ignore_suggestions",
    "convert_suggestion",
    "execute_run",
    "fail_run",
    "get_run",
    "get_suggestion",
    "list_run_demand",
    "list_runs",
    "list_suggestions",
    "release_planning_lock",
    "shortage_report",
    "start_run",
    "status_url",
]
