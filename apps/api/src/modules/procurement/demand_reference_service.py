"""Validate real demand ownership before procurement stores or trusts a reference."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.demand_provenance import DemandReference
from src.core.deps import TenantContext
from src.core.errors import NotFoundError, ValidationError
from src.modules.procurement.demand_reference_repository import DemandReferenceRepository

# The canonical schema remains open for readable historical/future provenance.
# Writes may only claim identities whose actual ownership this release resolves.
# BOM contributions retain SALES_ORDER/MRP_DEMAND roots; an item or BOM UUID is
# never substituted for a nonexistent BOM-demand document.
SUPPORTED_DEMAND_REFERENCE_TYPES = frozenset({"SALES_ORDER", "MRP_DEMAND"})


def _canonical_references(
    refs: Sequence[DemandReference | Mapping[str, object]],
) -> tuple[DemandReference, ...]:
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
        raise ValidationError(
            "Demand references must be a list of document identities.",
            details={"field": "source_demand_refs"},
        )
    try:
        # Do not use the legacy MRP display reader: a validated PR list of
        # exactly fifty references is complete. A document may validate several
        # such lines together, so the per-line schema owns the reference bound.
        canonical: dict[tuple[str, uuid.UUID, uuid.UUID | None], DemandReference] = {}
        for raw in refs:
            reference = DemandReference.model_validate(raw)
            canonical.setdefault(
                (reference.demand_type, reference.demand_id, reference.demand_line_id), reference
            )
        return tuple(canonical.values())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "Each demand reference must identify a demand type, document and optional line.",
            details={"field": "source_demand_refs"},
        ) from exc


async def validate_source_demand_refs(
    session: AsyncSession,
    tenant: TenantContext,
    refs: Sequence[DemandReference | Mapping[str, object]],
) -> None:
    """Resolve every supplied triple in this tenant and check line ownership.

    Missing and foreign IDs receive the same not-found response. Source
    cancellation does not erase causal history, so validation concerns identity
    and ownership rather than the source's current business status.
    """
    canonical = _canonical_references(refs)
    if not canonical:
        return
    unsupported = sorted({ref.demand_type for ref in canonical} - SUPPORTED_DEMAND_REFERENCE_TYPES)
    if unsupported:
        raise ValidationError(
            "These demand reference types cannot be verified: "
            + ", ".join(unsupported)
            + ". Use an existing sales order or a manual planning demand.",
            details={"field": "source_demand_refs", "demand_types": unsupported},
        )
    if any(ref.demand_type == "MRP_DEMAND" and ref.demand_line_id is not None for ref in canonical):
        raise ValidationError(
            "A manual planning demand is a single demand row and cannot name a separate line.",
            details={"field": "source_demand_refs.demand_line_id"},
        )

    sales_refs = tuple(ref for ref in canonical if ref.demand_type == "SALES_ORDER")
    order_ids = {ref.demand_id for ref in sales_refs}
    line_ids = {ref.demand_line_id for ref in sales_refs if ref.demand_line_id is not None}
    mrp_ids = {ref.demand_id for ref in canonical if ref.demand_type == "MRP_DEMAND"}
    repo = DemandReferenceRepository(session, tenant)
    # Fixed batched reads per entity type. None of the per-reference checks
    # below performs I/O, even when one document references many sales orders.
    known_orders = await repo.sales_orders(order_ids)
    known_lines = await repo.sales_order_lines(line_ids)
    known_demands = await repo.manual_mrp_demands(mrp_ids)

    missing_orders = order_ids - known_orders
    if missing_orders:
        raise NotFoundError("Sales order demand", _ids(missing_orders))
    missing_lines = line_ids - known_lines.keys()
    if missing_lines:
        raise NotFoundError("Sales order demand line", _ids(missing_lines))
    missing_demands = mrp_ids - known_demands
    if missing_demands:
        raise NotFoundError("Manual planning demand", _ids(missing_demands))
    if any(
        ref.demand_line_id is not None and known_lines[ref.demand_line_id] != ref.demand_id
        for ref in sales_refs
    ):
        raise ValidationError(
            "A demand line must belong to the sales order named in the same reference.",
            details={"field": "source_demand_refs.demand_line_id"},
        )


def _ids(values: set[uuid.UUID]) -> str:
    return ", ".join(str(value) for value in sorted(values))
