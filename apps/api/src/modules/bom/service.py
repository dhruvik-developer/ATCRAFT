"""Bill-of-materials business rules.

The rules that matter, and why each one is here rather than left to the caller:

1. **An approved structure is immutable.** Editing is only possible in ``DRAFT``.
   A change to an approved BOM is a *new version* created by
   :func:`copy_as_new_version`, because production orders, costings and MRP runs
   already reference the old one and must keep resolving to what they were
   planned against.
2. **Approval is a second person's judgement.** The approver may not be the
   creator unless they hold ``SUPER_ADMIN`` - see :func:`can_approve` and
   ``docs/assumptions.md`` section 2.
3. **One default approved structure per item**, demoted *before* the new one is
   promoted. The partial unique index is checked per statement, so
   promote-then-demote is a valid end state that still fails mid-transaction.
4. **Quantities are stated in the component's stock unit.** Multi-level
   consolidation adds quantities keyed on ``item_id``; a line in BOX under one
   parent and NOS under another would produce a total that looks plausible and
   is wrong. The service refuses the second unit rather than converting behind
   the engineer's back.
5. **Every write is one transaction with its audit row**, and every status
   change goes through :data:`BOM_LIFECYCLE`.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import today, utcnow
from src.core.database import advisory_xact_lock, transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import AuthorizationError, BusinessRuleError, NotFoundError
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.bom import engine
from src.modules.bom.models import (
    DEFAULT_MAX_EXPLOSION_DEPTH,
    Bom,
    BomLine,
    BomOperation,
    BomStatus,
    BomType,
)
from src.modules.bom.repository import (
    LIVE_BOM_STATUSES,
    BomLineRepository,
    BomOperationRepository,
    BomRepository,
    ItemInfo,
    ItemLookupRepository,
)
from src.modules.bom.schemas import (
    BomCostComponentRead,
    BomCostRead,
    BomCreate,
    BomDetail,
    BomDetailWithCost,
    BomExplosionRead,
    BomFilters,
    BomLineIn,
    BomLineRead,
    BomOperationIn,
    BomOperationRead,
    BomSummary,
    BomUpdate,
    CopyAsNewVersionRequest,
    ExplodedComponentRead,
    WhereUsedRead,
)

log = get_logger(__name__)

AUDIT_BOM = "bom"

#: Fields snapshotted before and after a header change so the audit diff names
#: what actually moved rather than the whole row.
BOM_AUDITED_FIELDS = [
    "code",
    "item_id",
    "version",
    "revision",
    "status",
    "bom_type",
    "is_default",
    "output_quantity",
    "base_uom_id",
    "effective_from",
    "effective_to",
    "parent_bom_id",
    "notes",
]

#: Columns on a BOM that cannot be nulled by a partial update.
NON_NULLABLE_UPDATE_FIELDS = frozenset(
    {"bom_type", "output_quantity", "base_uom_id", "effective_from", "is_default"}
)

#: The role that may approve its own work. Named rather than inlined so the
#: segregation-of-duties rule is one editable decision (docs/assumptions.md §2).
SUPER_ADMIN_ROLE_CODE = "SUPER_ADMIN"


# -----------------------------------------------------------------------------
# State machine - the only thing permitted to move boms.status
# -----------------------------------------------------------------------------

BOM_LIFECYCLE: StateMachine[BomStatus] = StateMachine(
    entity="BOM",
    initial=BomStatus.DRAFT,
    transitions=(
        Transition(
            BomStatus.DRAFT,
            BomStatus.PENDING_APPROVAL,
            permission=Permission.BOM_UPDATE,
            label="Submit for approval",
        ),
        # Rejection returns the structure to its author rather than deleting it:
        # the review comments are worth more than the empty slate.
        Transition(
            BomStatus.PENDING_APPROVAL,
            BomStatus.DRAFT,
            permission=Permission.BOM_APPROVE,
            label="Return to draft",
        ),
        Transition(
            BomStatus.PENDING_APPROVAL,
            BomStatus.APPROVED,
            permission=Permission.BOM_APPROVE,
            label="Approve",
            # Approval releases the structure to planning and production, and
            # is the point from which it can no longer be edited.
            is_irreversible=True,
        ),
        Transition(
            BomStatus.APPROVED,
            BomStatus.OBSOLETE,
            permission=Permission.BOM_OBSOLETE,
            label="Make obsolete",
            is_irreversible=True,
        ),
        # A draft that was never submitted can be abandoned without leaving a
        # half-finished structure in the engineering list forever.
        Transition(
            BomStatus.DRAFT,
            BomStatus.OBSOLETE,
            permission=Permission.BOM_OBSOLETE,
            label="Abandon",
        ),
        Transition(
            BomStatus.PENDING_APPROVAL,
            BomStatus.OBSOLETE,
            permission=Permission.BOM_OBSOLETE,
            label="Abandon",
        ),
    ),
    terminal=frozenset({BomStatus.OBSOLETE}),
)


# -----------------------------------------------------------------------------
# Pure rule helpers - unit tested without a database
# -----------------------------------------------------------------------------


def can_approve(principal: Principal, created_by_id: uuid.UUID | None) -> bool:
    """Segregation of duties on approval.

    ASSUMPTION: the approver of a BOM may not be the person who created it,
    unless they hold ``SUPER_ADMIN`` (or are a platform superuser). Approval is
    evidence of a second person's judgement (master rules section 42), and a
    document approved by its own author is not evidence of anything. Recorded in
    ``docs/assumptions.md`` section 2 because a two-person engineering team may
    reasonably want it relaxed - it is one predicate, in one place.
    """
    if principal.is_superuser or SUPER_ADMIN_ROLE_CODE in principal.role_codes:
        return True
    return created_by_id is None or created_by_id != principal.user_id


def is_editable(status: BomStatus) -> bool:
    """Only a draft may be edited in place; everything else is a new version."""
    return status is BomStatus.DRAFT


def effectivity_contains(bom: Bom, on: date) -> bool:
    """Whether ``on`` falls inside the structure's effectivity window."""
    if on < bom.effective_from:
        return False
    return bom.effective_to is None or on <= bom.effective_to


# -----------------------------------------------------------------------------
# Validation helpers
# -----------------------------------------------------------------------------


async def _load_parent_item(items: ItemLookupRepository, item_id: uuid.UUID) -> ItemInfo:
    info = (await items.load([item_id])).get(item_id)
    if info is None:
        raise NotFoundError("Item", item_id)
    # Master rules section 1: read the capability flag, never item_group. An
    # item nobody can manufacture has nothing to describe with a structure.
    if not info.can_manufacture:
        raise BusinessRuleError(
            f"{info.code} is not flagged as manufacturable, so it cannot have a "
            "bill of materials. Enable 'can manufacture' on the item first.",
            details={"item_id": str(item_id), "item_code": info.code},
        )
    return info


def _assert_operations_exist(
    line_refs: Sequence[tuple[int, int | None]], sequences: Collection[int]
) -> None:
    """Every line that names an operation must name one this BOM defines.

    Takes ``(line_number, operation_sequence)`` pairs rather than a payload type
    so the same rule covers a line set being replaced and a line set already
    persisted - a partial update may change either side of the reference.
    """
    for line_number, operation_sequence in line_refs:
        if operation_sequence is not None and operation_sequence not in sequences:
            raise BusinessRuleError(
                f"Line {line_number} is assigned to operation {operation_sequence}, "
                "which this BOM does not define.",
                details={
                    "line_number": line_number,
                    "operation_sequence": operation_sequence,
                },
            )


async def _validate_lines(
    items: ItemLookupRepository,
    *,
    parent: ItemInfo,
    lines: Sequence[BomLineIn],
    operation_sequences: Collection[int] | None,
) -> dict[uuid.UUID, ItemInfo]:
    """Check every component in one batched read.

    A loop of single lookups here would be an N+1 on the write path, which is
    where a 200-line structure is submitted in one request.
    """
    component_ids = [line.component_item_id for line in lines]
    infos = await items.load(component_ids)

    missing = sorted({str(i) for i in component_ids if i not in infos})
    if missing:
        raise NotFoundError("BOM component item", ", ".join(missing))

    for line in lines:
        info = infos[line.component_item_id]
        if info.id == parent.id:
            # Also blocked by a CHECK constraint; caught here so the message
            # names the line instead of surfacing an integrity error.
            raise BusinessRuleError(
                f"Line {line.line_number}: {info.code} cannot be a component of itself.",
                details={"line_number": line.line_number, "item_code": info.code},
            )
        if not info.can_be_bom_component:
            raise BusinessRuleError(
                f"Line {line.line_number}: {info.code} is not flagged as usable as a "
                "BOM component.",
                details={"line_number": line.line_number, "item_code": info.code},
            )
        if not info.is_active:
            raise BusinessRuleError(
                f"Line {line.line_number}: {info.code} is inactive and cannot be added "
                "to a structure.",
                details={"line_number": line.line_number, "item_code": info.code},
            )
        # ASSUMPTION: a BOM line is stated in the component's stock unit and no
        # other. Consolidation across levels sums quantities keyed on item_id
        # (database.md section 5), so a line in BOX under one parent and NOS
        # under another would produce a total that looks plausible and is wrong.
        # Converting silently would hide the data-entry error instead.
        if line.uom_id is not None and line.uom_id != info.base_uom_id:
            raise BusinessRuleError(
                f"Line {line.line_number}: quantities for {info.code} must be stated in "
                "its stock unit, because multi-level consolidation adds quantities "
                "across structures.",
                details={
                    "line_number": line.line_number,
                    "item_code": info.code,
                    "expected_uom_id": str(info.base_uom_id),
                },
            )

    if operation_sequences is not None:
        _assert_operations_exist(
            [(line.line_number, line.operation_sequence) for line in lines],
            operation_sequences,
        )

    return infos


async def _next_version(session: AsyncSession, repo: BomRepository, item_id: uuid.UUID) -> int:
    """Claim the next version number for an item's structure.

    ``MAX(version) + 1`` is only safe under a lock: two engineers creating a
    revision at the same moment would otherwise both read the same maximum and
    the second insert would fail on the unique constraint. The advisory lock is
    transaction-scoped, so it is released on commit or rollback with no unlock
    path to forget (see ``src.core.database.advisory_xact_lock``).
    """
    await advisory_xact_lock(session, "bom_version", repo.tenant.company_id, item_id)
    return await repo.max_version_for_item(item_id) + 1


def _build_lines(
    payload_lines: Sequence[BomLineIn],
    *,
    bom: Bom,
    infos: dict[uuid.UUID, ItemInfo],
) -> list[BomLine]:
    return [
        BomLine(
            bom_id=bom.id,
            parent_item_id=bom.item_id,
            line_number=line.line_number,
            component_item_id=line.component_item_id,
            quantity_per=line.quantity_per,
            uom_id=line.uom_id or infos[line.component_item_id].base_uom_id,
            scrap_percentage=line.scrap_percentage,
            is_optional=line.is_optional,
            operation_sequence=line.operation_sequence,
            notes=line.notes,
        )
        for line in payload_lines
    ]


async def _persist_lines(
    session: AsyncSession,
    repo: BomLineRepository,
    *,
    bom: Bom,
    payload_lines: Sequence[BomLineIn],
    infos: dict[uuid.UUID, ItemInfo],
) -> list[BomLine]:
    """Insert the line set and wire up substitutes.

    Two passes because a substitute points at another line's primary key, which
    does not exist until the first pass is flushed. The alternative - asking the
    client for ids it cannot know - would make creating a structure with
    substitutes a two-request dance.
    """
    records = _build_lines(payload_lines, bom=bom, infos=infos)
    for record in records:
        repo.add(record)
    await session.flush()

    id_by_number = {record.line_number: record.id for record in records}
    linked = False
    for payload, record in zip(payload_lines, records, strict=True):
        if payload.substitute_for_line_number is not None:
            record.substitute_for_line_id = id_by_number[payload.substitute_for_line_number]
            linked = True
    if linked:
        await session.flush()
    return records


async def _persist_operations(
    session: AsyncSession,
    repo: BomOperationRepository,
    *,
    bom: Bom,
    payload_operations: Sequence[BomOperationIn],
) -> list[BomOperation]:
    records = [
        BomOperation(
            bom_id=bom.id,
            operation_sequence=operation.operation_sequence,
            name=operation.name,
            description=operation.description,
            routing_operation_id=operation.routing_operation_id,
            work_center_id=operation.work_center_id,
            setup_time_minutes=operation.setup_time_minutes,
            run_time_minutes_per_unit=operation.run_time_minutes_per_unit,
            cost_per_hour=operation.cost_per_hour,
            fixed_cost=operation.fixed_cost,
            is_outsourced=operation.is_outsourced,
            notes=operation.notes,
        )
        for operation in payload_operations
    ]
    for record in records:
        repo.add(record)
    if records:
        await session.flush()
    return records


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


async def get_bom(session: AsyncSession, tenant: TenantContext, bom_id: uuid.UUID) -> Bom:
    return await BomRepository(session, tenant).get_or_404(bom_id)


async def list_boms(
    session: AsyncSession,
    tenant: TenantContext,
    filters: BomFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[BomSummary], OffsetPagination]:
    rows, pagination = await BomRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [BomSummary.model_validate(row) for row in rows], pagination


async def list_versions(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    params: PageParams,
) -> tuple[list[BomSummary], OffsetPagination]:
    rows, pagination = await BomRepository(session, tenant).list_versions(item_id, params)
    return [BomSummary.model_validate(row) for row in rows], pagination


async def get_bom_detail(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    bom_id: uuid.UUID,
) -> BomDetail:
    """One structure with its lines and operations, in three queries."""
    bom = await BomRepository(session, tenant).get_or_404(bom_id)
    lines = await BomLineRepository(session, tenant).list_for_bom(bom_id)
    operations = await BomOperationRepository(session, tenant).list_for_bom(bom_id)
    return bom_detail_response(bom, lines, operations, principal)


def bom_detail_response(
    bom: Bom,
    lines: Sequence[BomLine],
    operations: Sequence[BomOperation],
    principal: Principal,
) -> BomDetail:
    """Project a BOM, choosing the model the caller is allowed to see.

    The costed model is a different class rather than nulled-out fields, so a
    principal without ``bom.cost_view`` receives a payload in which the roll-up
    simply does not exist (the same pattern the item master uses for
    ``standard_cost``).
    """
    payload = {
        **{field: getattr(bom, field) for field in BomSummary.model_fields},
        "notes": bom.notes,
        "lines": [BomLineRead.model_validate(line) for line in lines],
        "operations": [BomOperationRead.model_validate(op) for op in operations],
    }
    if principal.has(Permission.BOM_COST_VIEW):
        return BomDetailWithCost(
            **payload,
            total_material_cost=bom.total_material_cost,
            total_operation_cost=bom.total_operation_cost,
            cost_rolled_up_at=bom.cost_rolled_up_at,
        )
    return BomDetail(**payload)


# -----------------------------------------------------------------------------
# Writes
# -----------------------------------------------------------------------------


async def create_bom(
    session: AsyncSession, tenant: TenantContext, payload: BomCreate
) -> tuple[Bom, list[BomLine], list[BomOperation]]:
    """Create a DRAFT structure with its lines and operations."""
    repo = BomRepository(session, tenant)
    items = ItemLookupRepository(session, tenant)

    async with transaction(session):
        parent = await _load_parent_item(items, payload.item_id)

        if payload.base_uom_id is not None and payload.base_uom_id != parent.base_uom_id:
            raise BusinessRuleError(
                f"A structure for {parent.code} must yield its output in the item's stock unit.",
                details={"expected_uom_id": str(parent.base_uom_id)},
            )

        infos = await _validate_lines(
            items,
            parent=parent,
            lines=payload.lines,
            operation_sequences={operation.operation_sequence for operation in payload.operations},
        )

        if payload.parent_bom_id is not None:
            alternate_of = await repo.get_or_404(payload.parent_bom_id)
            if alternate_of.item_id != payload.item_id:
                raise BusinessRuleError(
                    "An alternate structure must be an alternative for the same item.",
                    details={"parent_bom_id": str(payload.parent_bom_id)},
                )

        version = await _next_version(session, repo, payload.item_id)
        code = await next_document_number(session, tenant.company_id, DocTypes.BOM)

        bom = Bom(
            code=code,
            item_id=payload.item_id,
            version=version,
            revision=payload.revision,
            status=BomStatus.DRAFT,
            bom_type=payload.bom_type,
            is_default=payload.is_default,
            output_quantity=payload.output_quantity,
            base_uom_id=parent.base_uom_id,
            effective_from=payload.effective_from or today(),
            effective_to=payload.effective_to,
            parent_bom_id=payload.parent_bom_id,
            notes=payload.notes,
        )
        repo.add(bom)
        await session.flush()

        lines = await _persist_lines(
            session,
            BomLineRepository(session, tenant),
            bom=bom,
            payload_lines=payload.lines,
            infos=infos,
        )
        operations = await _persist_operations(
            session,
            BomOperationRepository(session, tenant),
            bom=bom,
            payload_operations=payload.operations,
        )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_BOM,
            entity_id=bom.id,
            entity_code=bom.code,
            summary=f"Created {bom.code} v{bom.version} for {parent.code} "
            f"with {len(lines)} component line(s)",
            user_id=tenant.user_id,
            new_values={
                **snapshot(bom, BOM_AUDITED_FIELDS),
                "line_count": len(lines),
                "operation_count": len(operations),
            },
        )

    return bom, lines, operations


async def update_bom(
    session: AsyncSession, tenant: TenantContext, bom_id: uuid.UUID, payload: BomUpdate
) -> tuple[Bom, list[BomLine], list[BomOperation]]:
    """Edit a DRAFT structure. Lines and operations are replaced wholesale."""
    repo = BomRepository(session, tenant)
    items = ItemLookupRepository(session, tenant)
    line_repo = BomLineRepository(session, tenant)
    operation_repo = BomOperationRepository(session, tenant)

    async with transaction(session):
        bom = await repo.get_or_404(bom_id)
        if not is_editable(BomStatus(bom.status)):
            raise BusinessRuleError(
                f"{bom.code} is {bom.status} and can no longer be edited. Create a new "
                "version instead, so documents planned against this one still resolve.",
                details={"bom_id": str(bom.id), "status": str(bom.status)},
            )

        before = snapshot(bom, BOM_AUDITED_FIELDS)
        parent = await _load_parent_item(items, bom.item_id)

        if payload.base_uom_id is not None and payload.base_uom_id != parent.base_uom_id:
            raise BusinessRuleError(
                f"A structure for {parent.code} must yield its output in the item's stock unit.",
                details={"expected_uom_id": str(parent.base_uom_id)},
            )
        if payload.parent_bom_id is not None:
            alternate_of = await repo.get_or_404(payload.parent_bom_id)
            if alternate_of.item_id != bom.item_id or alternate_of.id == bom.id:
                raise BusinessRuleError(
                    "An alternate structure must be an alternative for the same item.",
                    details={"parent_bom_id": str(payload.parent_bom_id)},
                )

        # ``exclude_unset`` applies only the keys the client actually sent, so a
        # PATCH that omits ``notes`` does not blank it. An explicit null for a
        # NOT NULL column is dropped rather than turned into an IntegrityError.
        sent = payload.model_dump(exclude_unset=True, exclude={"lines", "operations"})
        changes = {
            key: value
            for key, value in sent.items()
            if value is not None or key not in NON_NULLABLE_UPDATE_FIELDS
        }
        for field, value in changes.items():
            setattr(bom, field, value)
        repo.touch(bom)

        if bom.effective_to is not None and bom.effective_to < bom.effective_from:
            raise BusinessRuleError(
                "The effectivity window would end before it starts.",
                details={
                    "effective_from": bom.effective_from.isoformat(),
                    "effective_to": bom.effective_to.isoformat(),
                },
            )

        # Enforced on create by ``BomCreate``; a rule that only holds on POST is
        # a rule a client bypasses by using PATCH.
        if BomType(bom.bom_type) is BomType.ALTERNATE and bom.parent_bom_id is None:
            raise BusinessRuleError(
                "An alternate structure must name the bill of materials it is an alternative to.",
                details={"bom_id": str(bom.id)},
            )

        lines = await line_repo.list_for_bom(bom.id)
        operations = await operation_repo.list_for_bom(bom.id)
        line_count_before = len(lines)

        # Lines and operations are replaced independently, so the reference
        # between them has to be checked against what the BOM will actually hold
        # once this patch lands - the replacement where one was sent, what is
        # already persisted where one was not. Checking only the payload against
        # itself lets a patch of one side orphan the other: replacing operations
        # [10, 20] with [10] leaves a persisted line staged at operation 20, and
        # material issue by operation then never stages that component.
        operation_sequences = (
            {operation.operation_sequence for operation in payload.operations}
            if payload.operations is not None
            else {operation.operation_sequence for operation in operations}
        )

        if payload.lines is not None:
            infos = await _validate_lines(
                items,
                parent=parent,
                lines=payload.lines,
                operation_sequences=operation_sequences,
            )
            await line_repo.delete_for_bom(bom.id)
            lines = await _persist_lines(
                session, line_repo, bom=bom, payload_lines=payload.lines, infos=infos
            )
        elif payload.operations is not None:
            _assert_operations_exist(
                [(line.line_number, line.operation_sequence) for line in lines],
                operation_sequences,
            )

        if payload.operations is not None:
            await operation_repo.delete_for_bom(bom.id)
            operations = await _persist_operations(
                session, operation_repo, bom=bom, payload_operations=payload.operations
            )

        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_BOM,
            entity_id=bom.id,
            entity_code=bom.code,
            summary=f"Updated {bom.code} v{bom.version}",
            user_id=tenant.user_id,
            # ``line_count_before`` is captured before the replacement: reading
            # ``len(lines)`` for both sides reports the new count as the old one,
            # so an audit trail asked "when did nine components leave this
            # structure?" shows a line count that never moved.
            old_values={**before, "line_count": line_count_before},
            new_values={**snapshot(bom, BOM_AUDITED_FIELDS), "line_count": len(lines)},
        )

    return bom, lines, operations


async def _transition(
    session: AsyncSession,
    tenant: TenantContext,
    bom: Bom,
    target: BomStatus,
    *,
    reason: str,
    action: AuditAction = AuditAction.STATUS_CHANGE,
) -> Bom:
    """Move a BOM's status through the state machine and record it.

    Every status change in this module funnels through here, so there is exactly
    one place that can write ``boms.status`` and exactly one place that writes
    the matching audit row.
    """
    current = BomStatus(bom.status)
    BOM_LIFECYCLE.assert_transition(current, target)
    bom.status = target
    BomRepository(session, tenant).touch(bom)
    await session.flush()

    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=AUDIT_BOM,
        entity_id=bom.id,
        entity_code=bom.code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )
    return bom


async def submit_bom(
    session: AsyncSession, tenant: TenantContext, bom_id: uuid.UUID, *, reason: str
) -> Bom:
    """Send a draft for approval. Refuses a structure with nothing in it."""
    repo = BomRepository(session, tenant)

    async with transaction(session):
        bom = await repo.get_or_404(bom_id)
        lines = await BomLineRepository(session, tenant).list_for_bom(bom.id)
        if not any(engine.is_explodable(line) for line in lines):
            raise BusinessRuleError(
                f"{bom.code} has no mandatory component lines, so approving it would "
                "release a structure that produces nothing.",
                details={"bom_id": str(bom.id)},
            )
        await _transition(session, tenant, bom, BomStatus.PENDING_APPROVAL, reason=reason)

    return bom


async def approve_bom(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    bom_id: uuid.UUID,
    *,
    reason: str,
) -> Bom:
    """Approve a structure, refresh its stored roll-up and make it the default.

    Everything happens in one transaction: the status change, the demotion of
    the previous default, the cost refresh and the audit row. A partial result -
    approved but not default, or default twice - is exactly what the partial
    unique index and section 29 exist to prevent.

    Approvals for one item are serialised on a per-item advisory lock, the same
    key discipline :func:`_next_version` uses. "Read the current defaults, then
    demote them, then promote mine" is a check-then-act: two reviewers approving
    two revisions of the same item at the same moment both read the *old*
    default, both demote it, and both promote their own. The partial unique
    index does stop the second one committing, but it does so as a raw duplicate
    key - the reviewer is told "a record with these details already exists"
    about a structure they have never seen, and the cost roll-up that ran before
    it is thrown away. Taking the lock before the read makes the second approval
    wait, re-read a default that is now the first reviewer's revision, and
    demote *that* - which is the outcome the sequential case produces.
    """
    repo = BomRepository(session, tenant)

    async with transaction(session):
        bom = await repo.get_or_404(bom_id)

        if not can_approve(principal, bom.created_by_id):
            raise AuthorizationError(
                "A bill of materials must be approved by someone other than its author.",
                details={"bom_id": str(bom.id), "created_by_id": str(bom.created_by_id)},
            )

        # Validate the transition before doing any work, so a wrong-state
        # request costs one comparison rather than a cost roll-up.
        BOM_LIFECYCLE.assert_transition(BomStatus(bom.status), BomStatus.APPROVED)

        if bom.is_default:
            # Taken before the candidates are read, so the read cannot be stale
            # by the time it is acted on. Transaction-scoped: released on commit
            # or rollback with no unlock path to forget.
            await advisory_xact_lock(session, "bom_default", tenant.company_id, bom.item_id)

            # Demote first. The partial unique index is evaluated per statement,
            # so promoting before demoting is a valid end state that still fails
            # in the middle of the transaction (see CLAUDE.md, "traps").
            for existing in await repo.list_default_candidates(bom.item_id):
                if existing.id == bom.id:
                    continue
                existing.is_default = False
                repo.touch(existing)
                await record_audit(
                    session,
                    company_id=tenant.company_id,
                    action=AuditAction.UPDATE,
                    entity_type=AUDIT_BOM,
                    entity_id=existing.id,
                    entity_code=existing.code,
                    summary=f"{existing.code} is no longer the default structure; "
                    f"superseded by {bom.code} v{bom.version}",
                    user_id=tenant.user_id,
                    old_values={"is_default": True},
                    new_values={"is_default": False},
                )
            await session.flush()

        bom.approved_by_id = tenant.user_id
        bom.approved_at = utcnow()
        await _transition(
            session,
            tenant,
            bom,
            BomStatus.APPROVED,
            reason=reason,
            action=AuditAction.APPROVE,
        )

        # The structure is APPROVED inside this transaction, so the roll-up sees
        # the same graph planning will see a moment from now.
        cost = await engine.cost_roll_up(session, tenant, bom.item_id, bom_id=bom.id)
        bom.total_material_cost = cost.material_cost
        bom.total_operation_cost = cost.operation_cost
        bom.cost_rolled_up_at = utcnow()
        await session.flush()

        if cost.items_without_cost:
            log.info(
                "bom.approved_with_uncosted_items",
                bom_id=str(bom.id),
                item_codes=list(cost.items_without_cost),
            )

    return bom


async def reject_bom(
    session: AsyncSession, tenant: TenantContext, bom_id: uuid.UUID, *, reason: str
) -> Bom:
    """Return a submitted structure to its author with the reason recorded."""
    repo = BomRepository(session, tenant)
    async with transaction(session):
        bom = await repo.get_or_404(bom_id)
        await _transition(
            session, tenant, bom, BomStatus.DRAFT, reason=reason, action=AuditAction.REJECT
        )
    return bom


async def obsolete_bom(
    session: AsyncSession, tenant: TenantContext, bom_id: uuid.UUID, *, reason: str
) -> Bom:
    """Retire a structure.

    The default flag is cleared at the same time: an obsolete structure that is
    still flagged default would be invisible to the effective-BOM query (which
    only looks at APPROVED rows) while still occupying the partial unique index
    slot, so the replacement could never be approved.
    """
    repo = BomRepository(session, tenant)
    async with transaction(session):
        bom = await repo.get_or_404(bom_id)
        bom.is_default = False
        await _transition(
            session, tenant, bom, BomStatus.OBSOLETE, reason=reason, action=AuditAction.CANCEL
        )
    return bom


async def delete_draft_bom(
    session: AsyncSession, tenant: TenantContext, bom_id: uuid.UUID, *, reason: str
) -> None:
    """Physically remove a draft that was never released.

    Hard deletion is safe here and only here: a DRAFT has never been approved,
    so nothing outside engineering can reference it. Anything that has been
    approved is retired with :func:`obsolete_bom` instead - section 28 forbids
    destroying a record other documents point at.

    ASSUMPTION: the route is gated on ``bom.obsolete`` rather than ``bom.update``.
    There is no ``bom.delete`` permission in the registry, and destroying a
    structure is closer to retiring one than to editing it - an engineer who may
    change a draft should not necessarily be able to erase it.
    """
    repo = BomRepository(session, tenant)

    async with transaction(session):
        bom = await repo.get_or_404(bom_id)
        if BomStatus(bom.status) is not BomStatus.DRAFT:
            raise BusinessRuleError(
                f"{bom.code} is {bom.status}. Only a draft can be deleted; make it "
                "obsolete instead.",
                details={"bom_id": str(bom.id), "status": str(bom.status)},
            )
        if await repo.has_alternates(bom.id):
            raise BusinessRuleError(
                f"{bom.code} has alternate structures pointing at it.",
                details={"bom_id": str(bom.id)},
            )

        bom_code, bom_version = bom.code, bom.version
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_BOM,
            entity_id=bom.id,
            entity_code=bom_code,
            summary=f"Deleted draft {bom_code} v{bom_version}",
            user_id=tenant.user_id,
            old_values=snapshot(bom, BOM_AUDITED_FIELDS),
            context={"reason": reason},
        )
        # Lines and operations go with it through ON DELETE CASCADE rather than
        # a second round-trip per child table.
        await repo.hard_delete(bom)


async def copy_as_new_version(
    session: AsyncSession,
    tenant: TenantContext,
    bom_id: uuid.UUID,
    payload: CopyAsNewVersionRequest,
) -> tuple[Bom, list[BomLine], list[BomOperation]]:
    """Create the next DRAFT version from an existing structure.

    This is how an approved BOM is "edited": the old version stays exactly as
    the documents raised against it remember it, and the new one starts as a
    draft that has to be reviewed again.

    ASSUMPTION: the copy inherits ``is_default`` from its source. A revision is
    normally intended to replace the structure it came from, and approving it
    then demotes the old default in the same transaction. It has no effect until
    approval - the unique index only covers APPROVED rows.
    """
    repo = BomRepository(session, tenant)
    line_repo = BomLineRepository(session, tenant)
    operation_repo = BomOperationRepository(session, tenant)

    async with transaction(session):
        source = await repo.get_or_404(bom_id)
        source_lines = await line_repo.list_for_bom(source.id)
        source_operations = (
            await operation_repo.list_for_bom(source.id) if payload.copy_operations else []
        )

        effective_from = payload.effective_from or today()
        if payload.effective_to is not None and payload.effective_to < effective_from:
            raise BusinessRuleError(
                "The effectivity window would end before it starts.",
                details={"effective_from": effective_from.isoformat()},
            )

        version = await _next_version(session, repo, source.item_id)
        code = await next_document_number(session, tenant.company_id, DocTypes.BOM)

        copy = Bom(
            code=code,
            item_id=source.item_id,
            version=version,
            revision=payload.revision or source.revision,
            status=BomStatus.DRAFT,
            bom_type=source.bom_type,
            is_default=source.is_default,
            output_quantity=source.output_quantity,
            base_uom_id=source.base_uom_id,
            effective_from=effective_from,
            effective_to=payload.effective_to,
            parent_bom_id=source.parent_bom_id,
            notes=payload.notes if payload.notes is not None else source.notes,
        )
        repo.add(copy)
        await session.flush()

        # Substitute links are rebuilt against the *new* line ids: copying the
        # source ids would leave the new structure pointing at the old version's
        # rows, which is a cross-version reference nobody would ever notice.
        new_lines = [
            BomLine(
                bom_id=copy.id,
                parent_item_id=copy.item_id,
                line_number=line.line_number,
                component_item_id=line.component_item_id,
                quantity_per=line.quantity_per,
                uom_id=line.uom_id,
                scrap_percentage=line.scrap_percentage,
                is_optional=line.is_optional,
                operation_sequence=line.operation_sequence,
                notes=line.notes,
            )
            for line in source_lines
        ]
        for record in new_lines:
            line_repo.add(record)
        await session.flush()

        new_id_by_number = {record.line_number: record.id for record in new_lines}
        old_number_by_id = {line.id: line.line_number for line in source_lines}
        relinked = False
        for source_line, new_line in zip(source_lines, new_lines, strict=True):
            if source_line.substitute_for_line_id is None:
                continue
            number = old_number_by_id.get(source_line.substitute_for_line_id)
            if number is not None:
                new_line.substitute_for_line_id = new_id_by_number[number]
                relinked = True
        if relinked:
            await session.flush()

        new_operations = [
            BomOperation(
                bom_id=copy.id,
                operation_sequence=operation.operation_sequence,
                name=operation.name,
                description=operation.description,
                routing_operation_id=operation.routing_operation_id,
                work_center_id=operation.work_center_id,
                setup_time_minutes=operation.setup_time_minutes,
                run_time_minutes_per_unit=operation.run_time_minutes_per_unit,
                cost_per_hour=operation.cost_per_hour,
                fixed_cost=operation.fixed_cost,
                is_outsourced=operation.is_outsourced,
                notes=operation.notes,
            )
            for operation in source_operations
        ]
        for new_operation in new_operations:
            operation_repo.add(new_operation)
        if new_operations:
            await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_BOM,
            entity_id=copy.id,
            entity_code=copy.code,
            summary=f"Created {copy.code} v{copy.version} as a copy of "
            f"{source.code} v{source.version}",
            user_id=tenant.user_id,
            new_values={
                **snapshot(copy, BOM_AUDITED_FIELDS),
                "copied_from_bom_id": source.id,
                "copied_from_version": source.version,
                "line_count": len(new_lines),
            },
            context={"reason": payload.reason},
        )

    return copy, new_lines, new_operations


# -----------------------------------------------------------------------------
# Engine-backed reads
# -----------------------------------------------------------------------------


async def explode_bom(
    session: AsyncSession,
    tenant: TenantContext,
    bom_id: uuid.UUID,
    *,
    quantity: Decimal,
    as_of: date | None = None,
    max_depth: int | None = None,
) -> BomExplosionRead:
    """Explode a named structure for a quantity and project it for the API."""
    bom = await BomRepository(session, tenant).get_or_404(bom_id)
    result = await engine.explode(
        session,
        tenant,
        bom.item_id,
        quantity,
        bom_id=bom.id,
        as_of=as_of,
        max_depth=max_depth if max_depth is not None else DEFAULT_MAX_EXPLOSION_DEPTH,
    )
    return explosion_response(result)


def explosion_response(result: engine.BomExplosion) -> BomExplosionRead:
    return BomExplosionRead(
        root_item_id=result.root_item_id,
        root_item_code=result.root_item_code,
        bom_id=result.bom_id,
        bom_code=result.bom_code,
        root_bom_status=BomStatus(result.root_bom_status),
        quantity=result.quantity,
        as_of=result.as_of,
        max_level_reached=result.max_level_reached,
        phantom_item_codes=list(result.phantom_item_codes),
        components=[
            ExplodedComponentRead(
                item_id=component.item_id,
                item_code=component.item_code,
                item_name=component.item_name,
                total_quantity=component.total_quantity,
                scrap_adjusted_quantity=component.scrap_adjusted_quantity,
                uom_id=component.uom_id,
                level=component.level,
                is_purchased=component.is_purchased,
                is_manufactured=component.is_manufactured,
                source_paths=[list(path) for path in component.source_paths],
                source_path_count=component.source_path_count,
            )
            for component in result.components
        ],
    )


async def bom_cost(
    session: AsyncSession, tenant: TenantContext, bom_id: uuid.UUID, *, as_of: date | None = None
) -> BomCostRead:
    """Recompute the multi-level roll-up for a structure.

    Read-only: the stored ``total_material_cost`` / ``total_operation_cost`` are
    refreshed on approval, not by a GET, so browsing a costing screen cannot
    silently rewrite an approved document.
    """
    bom = await BomRepository(session, tenant).get_or_404(bom_id)
    result = await engine.cost_roll_up(session, tenant, bom.item_id, bom_id=bom.id, as_of=as_of)
    return BomCostRead(
        item_id=result.item_id,
        item_code=result.item_code,
        bom_id=result.bom_id,
        bom_code=result.bom_code,
        output_quantity=result.output_quantity,
        material_cost=result.material_cost,
        operation_cost=result.operation_cost,
        total_cost=result.total_cost,
        unit_cost=result.unit_cost,
        components=[
            BomCostComponentRead(
                item_id=component.item_id,
                item_code=component.item_code,
                quantity_per_unit=component.quantity_per_unit,
                unit_cost=component.unit_cost,
                extended_cost=component.extended_cost,
                cost_source=component.cost_source,
            )
            for component in result.components
        ],
        items_without_cost=list(result.items_without_cost),
    )


async def where_used_page(
    session: AsyncSession,
    tenant: TenantContext,
    component_item_id: uuid.UUID,
    *,
    levels: int,
    params: PageParams,
) -> tuple[list[WhereUsedRead], OffsetPagination]:
    """Which structures consume this component, paginated.

    Single-level is paginated in SQL: a common screw legitimately appears in
    thousands of structures, and materialising all of them to return fifty would
    be exactly the unbounded read section 31 forbids. Multi-level has no stable
    sort key spanning levels, so the engine walks it under a hard row budget and
    the bounded result is paged in memory.
    """
    if levels == 1:
        return await _direct_where_used_page(session, tenant, component_item_id, params)

    entries = await engine.where_used(session, tenant, component_item_id, levels=levels)
    total = len(entries)
    window = entries[params.offset : params.offset + params.limit]
    return (
        [
            WhereUsedRead(
                parent_item_id=entry.parent_item_id,
                parent_item_code=entry.parent_item_code,
                parent_item_name=entry.parent_item_name,
                bom_id=entry.bom_id,
                bom_code=entry.bom_code,
                bom_version=entry.bom_version,
                bom_status=BomStatus(entry.bom_status),
                is_default=entry.is_default,
                line_number=entry.line_number,
                quantity_per=entry.quantity_per,
                level=entry.level,
            )
            for entry in window
        ],
        OffsetPagination(
            page=params.page,
            limit=params.limit,
            total=total,
            total_pages=max(1, -(-total // params.limit)),
            has_next=params.offset + params.limit < total,
            has_previous=params.page > 1,
        ),
    )


async def _direct_where_used_page(
    session: AsyncSession,
    tenant: TenantContext,
    component_item_id: uuid.UUID,
    params: PageParams,
) -> tuple[list[WhereUsedRead], OffsetPagination]:
    lines, pagination = await BomLineRepository(session, tenant).direct_parents_page(
        component_item_id, params, statuses=LIVE_BOM_STATUSES
    )
    if not lines:
        return [], pagination

    # Two batched lookups for the whole page - never one per row (section 32).
    bom_repo = BomRepository(session, tenant)
    boms = {
        bom.id: bom
        for bom in await bom_repo.get_many(sorted({line.bom_id for line in lines}, key=str))
    }
    infos = await ItemLookupRepository(session, tenant).load(
        sorted({bom.item_id for bom in boms.values()}, key=str)
    )

    rows: list[WhereUsedRead] = []
    for line in lines:
        bom = boms.get(line.bom_id)
        info = infos.get(bom.item_id) if bom is not None else None
        if bom is None or info is None:
            continue
        rows.append(
            WhereUsedRead(
                parent_item_id=bom.item_id,
                parent_item_code=info.code,
                parent_item_name=info.name,
                bom_id=bom.id,
                bom_code=bom.code,
                bom_version=bom.version,
                bom_status=BomStatus(bom.status),
                is_default=bom.is_default,
                line_number=line.line_number,
                quantity_per=line.quantity_per,
                level=1,
            )
        )
    return rows, pagination


__all__ = [
    "AUDIT_BOM",
    "BOM_AUDITED_FIELDS",
    "BOM_LIFECYCLE",
    "NON_NULLABLE_UPDATE_FIELDS",
    "SUPER_ADMIN_ROLE_CODE",
    "approve_bom",
    "bom_cost",
    "bom_detail_response",
    "can_approve",
    "copy_as_new_version",
    "create_bom",
    "delete_draft_bom",
    "effectivity_contains",
    "explode_bom",
    "explosion_response",
    "get_bom",
    "get_bom_detail",
    "is_editable",
    "list_boms",
    "list_versions",
    "obsolete_bom",
    "reject_bom",
    "submit_bom",
    "update_bom",
    "where_used_page",
]
