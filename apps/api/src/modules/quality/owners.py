"""Who owns the stock an inspection decides, and how they are told the verdict.

THE RULE THIS FILE EXISTS FOR: one hold, one release path. A goods receipt, an
assembly completion and a job-work receipt each put their output on QC hold and
raise an inspection. Deciding that inspection is what moves the held stock
(``service.decide_inspection``), so the document that raised it learns the
verdict from the decision itself, in the same transaction, instead of offering a
second endpoint that releases the same hold again. Two release paths for one hold
is the defect this replaces: the receipt line stayed PENDING and the purchase
order sat in QC_PENDING for ever, an assembly order could never leave QC_PENDING,
and whichever of a job-work verdict and an inspection decision ran second either
failed for want of held stock or released another lot held at the same grain.

Quality may call the owners: it sits downstream of procurement, assembly and job
work (docs/architecture.md). The owners are imported lazily, by name, through
:data:`OWNER_BINDINGS` and resolved once per reference type, so nothing here is
imported at start-up and a module that is not deployed is simply skipped. A
failure *inside* an owner is never swallowed: it rolls the decision back with
everything else, exactly as the owners' own ports to quality behave.

Three hooks per owner, each optional:

``lock``      takes the owner's own row lock before quality touches a balance
              row. A decision then locks inspection -> owner -> balances, the
              same order the owner's posting takes owner -> balances in, so a
              decision and a second delivery against the same purchase order
              cannot deadlock each other.
``handler``   applies the verdict to the owner's document and reports whether it
              changed anything. It never moves stock: quality already did, and
              moving it again is the whole defect.
``awaiting``  pages through the owner's documents still waiting on a verdict,
              for :func:`reconcile_owners` - the sweep that repairs rows a
              decision stranded before this callback existed.

Every handler is idempotent: told about a verdict it has already applied, it
changes nothing. That is what makes the reconciliation sweep safe to run on every
beat start and every day.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Any, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.logging import get_logger
from src.core.types import ZERO, to_quantity
from src.modules.quality.models import InspectionResult, InspectionStatus, QcInspection
from src.modules.quality.repository import QcInspectionRepository

log = get_logger(__name__)

#: Documents asked for per ``awaiting`` page, and the cap on pages one sweep reads
#: per owner. A mature tenant can hold thousands of receipt lines stranded before
#: the callback existed; the bound keeps one tick from becoming an unbounded job,
#: and whatever is left is taken on the next.
RECONCILE_BATCH = 200
RECONCILE_MAX_BATCHES = 50

#: The reason an owner records when the sweep, rather than a person's decision,
#: is what applied a verdict.
RECONCILE_REASON = "Applied the verdict already recorded on the inspection"


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    """What an owner is told when one of its inspections is decided.

    Quantities are the inspection's cumulative disposition, not the delta of the
    latest decision: an owner applies the outcome, and a lot first sent for
    rework and later accepted is told the final split.
    """

    inspection_id: uuid.UUID
    inspection_code: str
    reference_type: str
    reference_id: uuid.UUID | None
    reference_code: str | None
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    #: The document's status after the decision - an ``InspectionStatus`` value.
    status: str
    #: The verdict on the goods - an ``InspectionResult`` value.
    result: str
    inspected_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    rework_quantity: Decimal
    hold_quantity: Decimal
    scrap_quantity: Decimal
    #: Rework plus hold: quantity the decision left in the QC hold pool.
    still_held_quantity: Decimal
    reason: str

    @property
    def is_settled(self) -> bool:
        """A verdict exists and nothing it covers is still on hold."""
        return self.result != InspectionResult.PENDING and self.still_held_quantity <= ZERO

    @property
    def refused_quantity(self) -> Decimal:
        """Rejected plus scrapped - everything the decision refused."""
        return to_quantity(self.rejected_quantity + self.scrap_quantity)

    @classmethod
    def of(cls, inspection: QcInspection, *, reason: str) -> QualityVerdict:
        rework = to_quantity(Decimal(inspection.rework_quantity))
        hold = to_quantity(Decimal(inspection.hold_quantity))
        return cls(
            inspection_id=inspection.id,
            inspection_code=inspection.code,
            reference_type=inspection.reference_type,
            reference_id=inspection.reference_id,
            reference_code=inspection.reference_code,
            item_id=inspection.item_id,
            warehouse_id=inspection.warehouse_id,
            status=str(InspectionStatus(inspection.status)),
            result=str(InspectionResult(inspection.result)),
            inspected_quantity=to_quantity(Decimal(inspection.inspected_quantity)),
            accepted_quantity=to_quantity(Decimal(inspection.accepted_quantity)),
            rejected_quantity=to_quantity(Decimal(inspection.rejected_quantity)),
            rework_quantity=rework,
            hold_quantity=hold,
            scrap_quantity=to_quantity(Decimal(inspection.scrap_quantity)),
            still_held_quantity=to_quantity(rework + hold),
            reason=reason,
        )


class OwnerHandler(Protocol):
    """Apply a verdict to the owning document. Returns whether anything changed."""

    async def __call__(
        self, session: AsyncSession, tenant: TenantContext, verdict: QualityVerdict
    ) -> bool: ...


class OwnerLock(Protocol):
    """Take the owner's row lock for the document an inspection references."""

    async def __call__(
        self, session: AsyncSession, tenant: TenantContext, reference_id: uuid.UUID
    ) -> None: ...


class OwnerAwaiting(Protocol):
    """One page of the owner's documents still waiting on a verdict.

    Returns ``(reference_type, reference_id)`` pairs in ascending id order, after
    ``after`` when it is given - a keyset page, so a sweep walks every waiting
    document once even when most of them are still under inspection.
    """

    async def __call__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        after: uuid.UUID | None,
        limit: int,
    ) -> Sequence[tuple[str, uuid.UUID]]: ...


@dataclass(frozen=True, slots=True)
class OwnerBinding:
    """Where one owning module's hooks live, by name. ``None`` means "not wired"."""

    module: str
    handler: str | None
    lock: str | None = None
    awaiting: str | None = None


#: ``reference_type`` -> the module that owns the held stock an inspection with
#: that reference decides. Names, not imports: see the module docstring.
OWNER_BINDINGS: dict[str, OwnerBinding] = {
    "goods_receipt": OwnerBinding(
        module="src.modules.procurement.service",
        handler="record_incoming_inspection_outcome",
        lock="lock_for_incoming_inspection",
        awaiting="awaiting_incoming_inspections",
    ),
    "assembly_order": OwnerBinding(
        module="src.modules.assembly.service",
        handler="record_quality_verdict",
        lock="lock_for_quality_verdict",
        awaiting="awaiting_quality_verdicts",
    ),
    # A job-work inspection names the *receipt* it holds. Rows raised before the
    # receipt had a reference type of its own say "job_work_order" with the
    # receipt's id, so both keys reach the same owner.
    "job_work_receipt": OwnerBinding(
        module="src.modules.job_work.service",
        handler="record_quality_verdict",
        lock="lock_for_quality_verdict",
        awaiting="awaiting_quality_verdicts",
    ),
    "job_work_order": OwnerBinding(
        module="src.modules.job_work.service",
        handler="record_quality_verdict",
        lock="lock_for_quality_verdict",
        awaiting="awaiting_quality_verdicts",
    ),
    # Production output is booked into QC hold with an in-process inspection
    # per booking (WP E5). A booking at a QC-gated routing operation names the
    # operation, any other booking names itself - and the handler finds its
    # booking by the inspection either way, so both keys reach the same owner.
    "production_entry": OwnerBinding(
        module="src.modules.production.service",
        handler="record_quality_verdict",
        lock="lock_for_quality_verdict",
        awaiting="awaiting_quality_verdicts",
    ),
    "work_order": OwnerBinding(
        module="src.modules.production.service",
        handler="record_quality_verdict",
        lock="lock_for_quality_verdict",
        awaiting="awaiting_quality_verdicts",
    ),
    # "sales_return" is deliberately absent: the customer-return document pulls
    # its verdict with ``inspections_for_reference`` (WP A3) rather than being
    # called back.
}


@dataclass(frozen=True, slots=True)
class OwnerHooks:
    """An owner's hooks, resolved to callables."""

    handler: OwnerHandler | None
    lock: OwnerLock | None
    awaiting: OwnerAwaiting | None


def _attribute(module: Any, name: str | None, binding: OwnerBinding) -> Any:
    if name is None:
        return None
    hook = getattr(module, name, None)
    if not callable(hook):
        # A binding that names a function its module does not have is a defect
        # in this table, not an absent module - say so instead of skipping the
        # owner and stranding its documents again.
        raise RuntimeError(f"{binding.module} has no callable {name!r} for quality to call.")
    return hook


@cache
def resolve_owner(reference_type: str) -> OwnerHooks | None:
    """The owner of ``reference_type``'s held stock, resolved once.

    ``None`` when nobody owns that reference type, or when the owning module is
    not deployed. Only the module's *absence* is tolerated: an import that fails
    for any other reason is raised.
    """
    binding = OWNER_BINDINGS.get(reference_type)
    if binding is None:
        return None
    try:
        module = importlib.import_module(binding.module)
    except ModuleNotFoundError as exc:
        if exc.name is not None and binding.module.startswith(exc.name):
            return None
        raise
    return OwnerHooks(
        handler=cast(OwnerHandler | None, _attribute(module, binding.handler, binding)),
        lock=cast(OwnerLock | None, _attribute(module, binding.lock, binding)),
        awaiting=cast(OwnerAwaiting | None, _attribute(module, binding.awaiting, binding)),
    )


async def lock_owner(
    session: AsyncSession, tenant: TenantContext, inspection: QcInspection
) -> None:
    """Take the owner's row lock before the decision moves any stock."""
    if inspection.reference_id is None:
        return
    hooks = resolve_owner(inspection.reference_type)
    if hooks is None or hooks.lock is None:
        return
    await hooks.lock(session, tenant, inspection.reference_id)


async def notify_owner(
    session: AsyncSession, tenant: TenantContext, inspection: QcInspection, *, reason: str
) -> bool:
    """Tell the owning document what was decided. Returns whether it changed.

    Called inside the deciding transaction, after the stock has moved and the
    inspection carries its new disposition. An owner that refuses rolls the
    whole decision back - the stock does not move without its owner agreeing.
    """
    if inspection.reference_id is None:
        return False
    hooks = resolve_owner(inspection.reference_type)
    if hooks is None or hooks.handler is None:
        return False
    return bool(await hooks.handler(session, tenant, QualityVerdict.of(inspection, reason=reason)))


@dataclass(frozen=True, slots=True)
class OwnerReconciliation:
    """What one company's reconciliation sweep did."""

    #: Owner documents still waiting on a verdict that the sweep looked at.
    documents_examined: int
    #: Decided inspections whose owner the sweep told again.
    verdicts_applied: int
    #: Of those, how many actually changed the owner - the stranded rows repaired.
    owners_repaired: int
    #: Inspections whose owner refused; logged, skipped, retried next sweep.
    failed: int


def _awaiting_hooks() -> list[OwnerAwaiting]:
    """Every distinct ``awaiting`` hook, once each, in binding order.

    Two reference types can share an owner (job work's receipt and its legacy
    order key), and walking its documents twice would repair nothing more.
    """
    seen: set[tuple[str, str]] = set()
    hooks: list[OwnerAwaiting] = []
    for reference_type, binding in OWNER_BINDINGS.items():
        if binding.awaiting is None:
            continue
        key = (binding.module, binding.awaiting)
        if key in seen:
            continue
        seen.add(key)
        resolved = resolve_owner(reference_type)
        if resolved is not None and resolved.awaiting is not None:
            hooks.append(resolved.awaiting)
    return hooks


async def reconcile_owners(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    batch: int = RECONCILE_BATCH,
    max_batches: int = RECONCILE_MAX_BATCHES,
) -> OwnerReconciliation:
    """Re-tell every waiting owner the verdicts its inspections already carry.

    ASSUMPTION: rows a decision stranded before the owner callback existed are
    repaired by this idempotent sweep, run as the system actor with audit, rather
    than by a data migration - SQL in a migration cannot write the audit rows and
    status-change history a repair owes (docs/assumptions.md §E4-1).

    Each inspection is repaired in its own transaction, so one owner that refuses
    is logged and skipped rather than undoing every other repair, and is tried
    again on the next sweep. Each takes the same locks a live decision takes, in
    the same order: inspection, then owner.
    """
    repo = QcInspectionRepository(session, tenant)
    examined = applied = repaired = failed = 0

    for awaiting in _awaiting_hooks():
        after: uuid.UUID | None = None
        for _ in range(max_batches):
            references = list(await awaiting(session, tenant, after=after, limit=batch))
            if not references:
                break
            examined += len(references)
            after = references[-1][1]

            # Plain ids: a repair that fails rolls its savepoint back and expires
            # the identity map, so no ORM row is carried across iterations.
            for inspection_id in await repo.settled_ids_for_references(references):
                try:
                    async with transaction(session):
                        inspection = await repo.lock_for_update(inspection_id)
                        if inspection is None:
                            continue
                        await lock_owner(session, tenant, inspection)
                        changed = await notify_owner(
                            session, tenant, inspection, reason=RECONCILE_REASON
                        )
                    applied += 1
                    repaired += int(changed)
                except Exception as exc:  # one stranded row must not stop the rest
                    failed += 1
                    log.error(
                        "quality.owner_reconcile_failed",
                        company_id=str(tenant.company_id),
                        inspection_id=str(inspection_id),
                        error=str(exc),
                        exc_info=exc,
                    )

            if len(references) < batch:
                break

    result = OwnerReconciliation(
        documents_examined=examined,
        verdicts_applied=applied,
        owners_repaired=repaired,
        failed=failed,
    )
    log.info(
        "quality.owners_reconciled",
        company_id=str(tenant.company_id),
        documents_examined=examined,
        verdicts_applied=applied,
        owners_repaired=repaired,
        failed=failed,
    )
    return result


__all__ = [
    "OWNER_BINDINGS",
    "RECONCILE_BATCH",
    "RECONCILE_MAX_BATCHES",
    "RECONCILE_REASON",
    "OwnerAwaiting",
    "OwnerBinding",
    "OwnerHandler",
    "OwnerHooks",
    "OwnerLock",
    "OwnerReconciliation",
    "QualityVerdict",
    "lock_owner",
    "notify_owner",
    "reconcile_owners",
    "resolve_owner",
]
