"""Preserve verifiable planning demand identities when a suggestion becomes a PR."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.core.demand_provenance import MAX_ROOT_DEMAND_REFS, canonical_demand_provenance
from src.core.errors import BusinessRuleError
from src.modules.procurement.schemas import SourceDemandRef


def canonical_demand_refs(
    source_demands: Sequence[Mapping[str, Any]] | None,
) -> list[SourceDemandRef]:
    """Require complete roots, deduplicate triples and preserve the PR schema.

    ASSUMPTION: a planning aggregate with more than fifty distinct roots must
    be replanned in a narrower scope. Silently retaining its first fifty would
    make duplicate prevention claim an incomplete history was complete.
    """
    try:
        provenance = canonical_demand_provenance(source_demands)
        if not provenance.root_demand_refs_complete:
            raise ValueError("The recorded demand provenance is incomplete.")
    except (TypeError, ValueError) as exc:
        raise BusinessRuleError(
            "Planning demand links are incomplete or exceed the fifty-reference limit. "
            "Rerun the plan with a narrower scope before creating a purchase requisition.",
            details={"field": "source_demands", "limit": MAX_ROOT_DEMAND_REFS},
        ) from exc
    return [SourceDemandRef.model_validate(ref.model_dump()) for ref in provenance.root_demand_refs]
