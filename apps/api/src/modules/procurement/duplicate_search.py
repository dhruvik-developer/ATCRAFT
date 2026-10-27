"""Duplicate discovery at physical PO-line grain, with causal demand links.

The pure matcher remains in ``service_rules``. This module loads its facts in
fixed batches, derives the same procurement category as conversion and keeps
allocation/provenance multiplicity from multiplying physical order quantities.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.warehouse_scope import assert_warehouse_scope
from src.core.demand_provenance import DemandReference
from src.core.deps import TenantContext
from src.core.errors import AuthorizationError, NotFoundError, ValidationError
from src.modules.procurement.duplicate_search_repository import DuplicateSearchRepository
from src.modules.procurement.models import DUPLICATE_REQUIRED_DATE_WINDOW_DAYS, PurchaseSourceType
from src.modules.procurement.service_rules import (
    CandidateInput,
    DuplicateCandidate,
    DuplicateConfidence,
    ProposedLine,
    category_lineage,
    classify_candidates,
    demand_ref_keys,
    resolve_procurement_category,
)


@dataclass(frozen=True, slots=True)
class DuplicateSearchGroup:
    vendor_id: uuid.UUID
    warehouse_id: uuid.UUID
    lines: Sequence[ProposedLine]
    source_type: PurchaseSourceType | None = None
    source_id: uuid.UUID | None = None
    exclude_order_id: uuid.UUID | None = None


def _canonical_refs(
    refs: Iterable[Mapping[str, Any] | DemandReference],
) -> tuple[DemandReference, ...]:
    # This is the validated PR reference list, not MRP's capped display facts.
    # Exactly fifty roots remain complete, and pooled lines may contain more.
    identities = demand_ref_keys(tuple(refs))
    return tuple(
        DemandReference(demand_type=kind, demand_id=header_id, demand_line_id=line_id)
        for kind, header_id, line_id in sorted(
            identities, key=lambda value: (value[0], str(value[1]), str(value[2] or ""))
        )
    )


def _source_ids(line: ProposedLine) -> frozenset[uuid.UUID]:
    return line.requisition_line_ids | (
        frozenset({line.requisition_line_id})
        if line.requisition_line_id is not None
        else frozenset()
    )


def _date_envelope(
    lines: Sequence[ProposedLine], window_days: int
) -> tuple[date | None, date | None, bool]:
    if window_days < 0:
        raise ValueError("The duplicate date window cannot be negative.")
    # Unknown dates overlap every date in the pure rule. One unknown line must
    # therefore widen the SQL envelope even when other proposed dates are known.
    if not lines or any(line.required_date is None for line in lines):
        return None, None, True
    known = [line.required_date for line in lines if line.required_date is not None]
    window = timedelta(days=window_days)
    return min(known) - window, max(known) + window, False


def _classify_group(
    group: DuplicateSearchGroup,
    candidates: Sequence[CandidateInput],
    *,
    window_days: int,
) -> list[DuplicateCandidate]:
    confidence_rank = {confidence: index for index, confidence in enumerate(DuplicateConfidence)}
    found: dict[uuid.UUID, tuple[tuple[int, int], DuplicateCandidate]] = {}
    for line in group.lines:
        possible = [
            candidate
            for candidate in candidates
            if candidate.purchase_order_id != group.exclude_order_id
            and candidate.vendor_id == group.vendor_id
            and candidate.warehouse_id == group.warehouse_id
            and (
                candidate.item_id == line.item_id
                or (
                    line.item_category_id is not None
                    and candidate.item_category_id == line.item_category_id
                )
            )
        ]
        for match in classify_candidates(
            possible,
            vendor_id=group.vendor_id,
            item_id=line.item_id,
            warehouse_id=group.warehouse_id,
            required_date=line.required_date,
            source_type=group.source_type,
            source_id=group.source_id,
            window_days=window_days,
            requisition_line_id=line.requisition_line_id,
            requisition_line_ids=line.requisition_line_ids,
            item_category_id=line.item_category_id,
            source_demand_refs=line.source_demand_refs,
        ):
            # A category match from an earlier proposed line must not obscure a
            # later EXACT/same-SKU match for this same physical candidate line.
            priority = (confidence_rank[match.confidence], int(match.item_id != line.item_id))
            previous = found.get(match.purchase_order_line_id)
            if previous is None or priority < previous[0]:
                found[match.purchase_order_line_id] = priority, match
    return sorted(
        (entry[1] for entry in found.values()),
        key=lambda match: (
            confidence_rank[match.confidence],
            match.required_date or date.max,
            match.purchase_order_code,
            str(match.purchase_order_line_id),
        ),
    )


async def find_duplicates_for_groups(
    session: AsyncSession,
    tenant: TenantContext,
    groups: Sequence[DuplicateSearchGroup],
    *,
    window_days: int = DUPLICATE_REQUIRED_DATE_WINDOW_DAYS,
) -> list[list[DuplicateCandidate]]:
    """Discover all conversion groups in fixed query batches, without N+1 reads."""
    if not groups:
        return []
    if tenant.vendor_id is not None or tenant.customer_id is not None:
        raise AuthorizationError(
            "Duplicate purchase-order checks are for internal purchasing users."
        )
    assert_warehouse_scope(tenant, *(group.warehouse_id for group in groups))
    incoming = [line for group in groups for line in group.lines]
    if not incoming:
        return [[] for _ in groups]
    repo = DuplicateSearchRepository(session, tenant)
    paths = await repo.item_lineages([line.item_id for line in incoming])
    missing_items = {line.item_id for line in incoming} - set(paths)
    if missing_items:
        raise NotFoundError("Item", ", ".join(sorted(map(str, missing_items))))
    qualifications = await repo.vendor_categories([group.vendor_id for group in groups])
    requested_sources = {source_id for line in incoming for source_id in _source_ids(line)}
    source_facts = await repo.requisition_demands(sorted(requested_sources))
    missing_sources = requested_sources - set(source_facts)
    if missing_sources:
        raise NotFoundError(
            "Purchase requisition line", ", ".join(sorted(map(str, missing_sources)))
        )
    enriched: list[DuplicateSearchGroup] = []
    for group in groups:
        lines: list[ProposedLine] = []
        for line in group.lines:
            source_ids = _source_ids(line)
            if any(source_facts[source_id].item_id != line.item_id for source_id in source_ids):
                raise ValidationError(
                    "An order line must match the item on every linked requisition line."
                )
            refs = _canonical_refs(
                [
                    *line.source_demand_refs,
                    *(
                        ref
                        for source_id in source_ids
                        for ref in source_facts[source_id].source_demand_refs
                    ),
                ]
            )
            lines.append(
                replace(
                    line,
                    item_category_id=resolve_procurement_category(
                        category_lineage(paths[line.item_id]),
                        qualifications.get(group.vendor_id, frozenset()),
                    ),
                    source_demand_refs=refs,
                    requisition_line_ids=source_ids,
                )
            )
        enriched.append(replace(group, lines=lines))
    every = [line for group in enriched for line in group.lines]
    date_from, date_to, unrestricted = _date_envelope(every, window_days)
    refs = _canonical_refs(ref for line in every for ref in line.source_demand_refs)
    # Exclude a header in SQL only if every group excludes it. Otherwise apply
    # each group's exclusion during classification to avoid cross-group loss.
    exclusions = set.intersection(
        *[
            {group.exclude_order_id} if group.exclude_order_id is not None else set()
            for group in enriched
        ]
    )
    rows = await repo.open_lines(
        vendor_warehouses=[(group.vendor_id, group.warehouse_id) for group in enriched],
        item_ids=[line.item_id for line in every],
        category_ids=[line.item_category_id for line in every if line.item_category_id is not None],
        date_from=date_from,
        date_to=date_to,
        unrestricted_dates=unrestricted,
        header_sources=[
            (group.source_type, group.source_id)
            for group in enriched
            if group.source_type not in {None, PurchaseSourceType.MANUAL}
            and group.source_id is not None
        ],
        requisition_line_ids=sorted(requested_sources),
        demand_refs=[ref.model_dump(mode="json", exclude_none=True) for ref in refs],
        group_count=len(enriched),
        exclude_order_ids=sorted(exclusions),
    )
    sources = await repo.candidate_sources([row.purchase_order_line_id for row in rows])
    candidates: list[CandidateInput] = []
    for row in rows:
        allocated = sources.get(row.purchase_order_line_id, [])
        candidates.append(
            CandidateInput(
                purchase_order_id=row.purchase_order_id,
                purchase_order_code=row.purchase_order_code,
                purchase_order_line_id=row.purchase_order_line_id,
                purchase_order_version=row.purchase_order_version,
                status=row.status,
                vendor_id=row.vendor_id,
                warehouse_id=row.warehouse_id,
                item_id=row.item_id,
                ordered_quantity=row.quantity,
                received_quantity=row.received_quantity,
                required_date=row.required_date,
                source_type=row.source_type,
                source_id=row.source_id,
                has_receipt_history=row.has_receipt_history,
                item_category_id=resolve_procurement_category(
                    category_lineage(row.category_path),
                    qualifications.get(row.vendor_id, frozenset()),
                ),
                requisition_line_ids=frozenset(source_id for source_id, _ in allocated),
                source_demand_refs=_canonical_refs(ref for _, refs in allocated for ref in refs),
            )
        )
    return [_classify_group(group, candidates, window_days=window_days) for group in enriched]


async def find_duplicates_for_lines(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    vendor_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    lines: Sequence[ProposedLine],
    source_type: PurchaseSourceType | None,
    source_id: uuid.UUID | None,
    exclude_order_id: uuid.UUID | None = None,
    window_days: int = DUPLICATE_REQUIRED_DATE_WINDOW_DAYS,
) -> list[DuplicateCandidate]:
    """Drop-in whole-document search, enriched with active demand allocations."""
    if not lines:
        return []
    results = await find_duplicates_for_groups(
        session,
        tenant,
        [
            DuplicateSearchGroup(
                vendor_id, warehouse_id, lines, source_type, source_id, exclude_order_id
            )
        ],
        window_days=window_days,
    )
    return results[0]
