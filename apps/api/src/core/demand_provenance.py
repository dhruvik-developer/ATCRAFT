"""Typed causal demand identities shared by planning and procurement.

References identify the demand that contributed to a plan. They do not assign
part of a shared shortage to a sales line: MRP nets quantities in aggregate.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from src.core.constants import MAX_DEMAND_PROVENANCE_REFS as MAX_ROOT_DEMAND_REFS

# This is the historical listing cap, not a count of unique root demands. Old
# rows at this bound may have discarded further facts and need to be replanned.
LEGACY_MRP_SOURCE_LIST_LIMIT = 50


class DemandReference(BaseModel):
    """One canonical, open-vocabulary demand identity; never an item identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    demand_type: str = Field(min_length=1, max_length=32)
    demand_id: uuid.UUID
    demand_line_id: uuid.UUID | None = None

    @field_validator("demand_type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value


class RootDemandProvenance(BaseModel):
    """Bounded canonical roots with an explicit completeness claim.

    New MRP facts store this metadata on their first listed contribution. The
    display listing and the canonical roots have separate bounds: repeated
    contributions can exceed the former without losing any distinct root.
    """

    model_config = ConfigDict(extra="forbid")

    root_demand_refs: list[DemandReference] = Field(max_length=MAX_ROOT_DEMAND_REFS)
    root_demand_refs_complete: StrictBool

    @field_validator("root_demand_refs", mode="before")
    @classmethod
    def _deduplicate_refs(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            return value
        seen: set[tuple[str, uuid.UUID, uuid.UUID | None]] = set()
        refs: list[DemandReference] = []
        for raw in value:
            ref = DemandReference.model_validate(raw)
            identity = (ref.demand_type, ref.demand_id, ref.demand_line_id)
            if identity not in seen:
                seen.add(identity)
                refs.append(ref)
        return refs


def recorded_demand_provenance(
    sources: Sequence[Mapping[str, Any]] | None,
) -> RootDemandProvenance | None:
    """Read explicit metadata strictly; malformed metadata is never legacy.

    Only the first fact can contain the aggregate. Accepting several aggregates
    would make it unclear which completeness claim the consumer should trust.
    """
    if sources is not None and (
        not isinstance(sources, Sequence) or isinstance(sources, (str, bytes))
    ):
        raise ValueError("Demand provenance must be a source fact list.")
    if any(not isinstance(source, Mapping) for source in sources or ()):
        raise ValueError("Each demand provenance fact must be an object.")
    metadata = [
        (index, source)
        for index, source in enumerate(sources or ())
        if "root_demand_refs" in source or "root_demand_refs_complete" in source
    ]
    if not metadata:
        return None
    if len(metadata) != 1 or metadata[0][0] != 0:
        raise ValueError("Demand provenance must be recorded on the first source fact only.")
    source = metadata[0][1]
    provenance = RootDemandProvenance.model_validate(
        {
            key: source[key]
            for key in ("root_demand_refs", "root_demand_refs_complete")
            if key in source
        }
    )
    if provenance.root_demand_refs_complete and not provenance.root_demand_refs:
        raise ValueError("Nonempty demand facts cannot claim complete roots without an identity.")
    return provenance


def canonical_demand_provenance(
    sources: Sequence[Mapping[str, Any]] | None,
) -> RootDemandProvenance:
    """Read new metadata or reconstruct provable, uncapped legacy direct facts.

    Legacy BOM entries name only a parent item. That is not a demand, so this
    function refuses them instead of fabricating an EXACT duplicate identity.
    """
    recorded = recorded_demand_provenance(sources)
    if recorded is not None:
        return recorded
    if sources and len(sources) >= LEGACY_MRP_SOURCE_LIST_LIMIT:
        raise ValueError("Legacy demand provenance may be truncated; rerun a narrower plan.")
    refs: list[DemandReference] = []
    for source in sources or ():
        if "demand_type" in source or "demand_id" in source:
            raw = {
                key: source[key]
                for key in ("demand_type", "demand_id", "demand_line_id")
                if key in source
            }
        else:
            raw = {
                "demand_type": source.get("source"),
                "demand_id": source.get("source_id"),
                "demand_line_id": source.get("source_line_id"),
            }
        # In particular, via_item_id is deliberately absent from this mapping.
        refs.append(DemandReference.model_validate(raw))
    return RootDemandProvenance(root_demand_refs=refs, root_demand_refs_complete=True)


def project_demand_provenance(
    sources: Sequence[Mapping[str, Any]] | None,
) -> RootDemandProvenance | None:
    """Expose old or damaged metadata safely without inventing missing roots.

    Read endpoints remain available for historical plans. Conversion applies
    the strict reader and refuses incomplete provenance before creating a PR.
    """
    if not sources:
        return None
    try:
        return canonical_demand_provenance(sources)
    except (TypeError, ValueError):
        return RootDemandProvenance(root_demand_refs=[], root_demand_refs_complete=False)
