"""Background execution of a planning run (master rules sections 14, 35).

The API creates the run row, snapshots its demand and hands the id to this task;
the calculation itself happens here, on the ``mrp`` queue, so a four-minute plan
never occupies an HTTP worker.

Three properties this task has to hold, and how each is achieved:

**It must be safe to re-deliver.** ``task_acks_late`` is on, so a worker killed
mid-run has its task delivered again. The state machine is what makes that
survivable: the second delivery finds the run no longer ``QUEUED`` and
:class:`~src.core.errors.InvalidStateTransitionError` stops it before it can
write a second set of suggestions for the same run. That is a deliberate refusal,
not a crash, and it is logged as such.

**A failure must be recorded, not just logged.** The engine runs inside one
session; if it fails, that session is discarded and the run is marked ``FAILED``
on a *fresh* one. A run left sitting in ``RUNNING`` would block the company's
next plan until it aged out.

**The lock must be released exactly once.** The API takes the company's planning
lock and hands the token over with the task, because the lock has to outlive the
request that started the run. This task owns it from that moment and releases it
in a ``finally`` - and because ``release_lock`` is a compare-and-delete, a token
that has already expired cannot delete a lock some later run legitimately holds.
"""

from __future__ import annotations

import uuid
from typing import Any

from src.core.database import session_scope
from src.core.deps import TenantContext, system_tenant
from src.core.errors import AppError, InvalidStateTransitionError, RateLimitedError
from src.core.logging import get_logger
from src.modules.mrp import service
from src.modules.mrp.schemas import MrpRunRequest
from src.workers.celery_app import async_task

log = get_logger("worker.mrp")


@async_task(name="mrp.scheduled_run", queue="mrp")
async def scheduled_run(request_id: str | None = None, **_: Any) -> dict[str, int]:
    """Queue one asynchronous planning run for each enabled company."""
    async with session_scope() as session:
        configurations = await service.scheduled_configurations(session)

    queued = 0
    skipped = 0
    failed = 0
    for configuration in configurations:
        tenant = system_tenant(configuration.company_id)
        try:
            async with session_scope() as session:
                await service.start_run(
                    session,
                    tenant,
                    MrpRunRequest(
                        horizon_days=configuration.horizon_days,
                        warehouse_ids=list(configuration.warehouse_ids or ()),
                        respect_safety_stock=configuration.respect_safety_stock,
                    ),
                    force_async=True,
                )
        except (service.MrpRunInProgressError, RateLimitedError) as exc:
            skipped += 1
            log.info(
                "mrp.scheduled_run_skipped",
                company_id=str(configuration.company_id),
                reason=exc.message,
            )
            continue
        except Exception as exc:
            failed += 1
            log.error(
                "mrp.scheduled_run_failed",
                company_id=str(configuration.company_id),
                error=str(exc),
                exc_info=exc,
            )
            continue
        queued += 1

    outcome = {
        "companies": len(configurations),
        "queued": queued,
        "skipped": skipped,
        "failed": failed,
    }
    log.info("mrp.scheduled_runs_dispatched", **outcome)
    return outcome


def _failure_message(exc: BaseException) -> str:
    """A message safe to persist on the run row.

    A deliberate :class:`AppError` carries text written for a user. Anything else
    is a bug, and a driver's exception string can contain SQL and row data, so
    only the type name is recorded and the traceback stays in the log (section 53).
    """
    if isinstance(exc, AppError):
        return exc.message
    return (
        f"The planning run failed with an unexpected {type(exc).__name__}. "
        "The full error is in the worker log against this task id."
    )


@async_task(name="mrp.run", queue="mrp")
async def run_mrp(
    run_id: str,
    company_id: str,
    user_id: str,
    lock_token: str | None = None,
    request_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Execute one queued MRP run and record what it produced.

    Arguments are plain strings because Celery serialises task payloads as JSON;
    ``request_id`` is not used here but is read by the ``task_prerun`` signal so
    the API's correlation id follows the work into the worker's logs (section 55).
    """
    tenant = TenantContext(company_id=uuid.UUID(company_id), user_id=uuid.UUID(user_id))
    identifier = uuid.UUID(run_id)

    try:
        async with session_scope() as session:
            result = await service.execute_run(session, tenant, run_id=identifier)
        log.info(
            "mrp.task_completed",
            run_id=run_id,
            code=result.code,
            suggestions=result.suggestion_count,
        )
        return {
            "run_id": run_id,
            "code": result.code,
            "status": str(result.status),
            "suggestion_count": result.suggestion_count,
            "warnings": result.warnings,
        }

    except InvalidStateTransitionError:
        # A re-delivery of a task whose run has already moved on. Nothing to do,
        # and nothing wrong: acknowledging it is the correct outcome, and raising
        # would have Celery retry forever against a run that is already finished.
        log.warning("mrp.task_run_not_queued", run_id=run_id)
        return {"run_id": run_id, "status": "SKIPPED", "reason": "run is no longer queued"}

    except Exception as exc:
        # A separate session: the one the failure happened on cannot be used to
        # write anything, so the run would otherwise stay RUNNING for ever.
        async with session_scope() as session:
            await service.fail_run(session, tenant, run_id=identifier, error=_failure_message(exc))
        log.error("mrp.task_failed", run_id=run_id, error=str(exc), exc_info=exc)
        raise

    finally:
        await service.release_planning_lock(tenant.company_id, lock_token)


__all__ = ["run_mrp", "scheduled_run"]
