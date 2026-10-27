"""Root demand identities survive consolidation, BOM explosion and display caps."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.demand_provenance import (
    DemandReference,
    RootDemandProvenance,
    project_demand_provenance,
)
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError
from src.modules.mrp import engine, service
from src.modules.mrp.models import MAX_SOURCE_DEMANDS_LISTED, MrpDemandSource
from src.modules.mrp.schemas import MrpSuggestionRead
from src.modules.procurement.demand_provenance import canonical_demand_refs
from src.modules.procurement.models import MAX_SOURCE_DEMAND_REFS
from src.modules.procurement.schemas import SourceDemandRef
from tests.unit.test_mrp_engine_merge import (
    AS_OF,
    HORIZON_END,
    REQUIRED,
    FakeSource,
    make_item,
    make_product,
    sales_line,
)

pytestmark = pytest.mark.unit


def _ref(*, demand_type: str = "SALES_ORDER") -> dict[str, Any]:
    return {
        "demand_type": demand_type,
        "demand_id": str(uuid.uuid4()),
        "demand_line_id": str(uuid.uuid4()),
    }


def _metadata(refs: list[dict[str, Any]], *, complete: bool = True) -> list[dict[str, Any]]:
    return [
        {
            "source": "BOM",
            "via_item_id": str(uuid.uuid4()),
            "quantity": "12.0000",
            "root_demand_refs": refs,
            "root_demand_refs_complete": complete,
        }
    ]


def test_legacy_direct_demands_normalize_and_deduplicate_real_triples() -> None:
    ref = _ref(demand_type="sales_order")
    source = {
        "source": ref["demand_type"],
        "source_id": ref["demand_id"],
        "source_line_id": ref["demand_line_id"],
        "quantity": "1.0000",
    }

    result = canonical_demand_refs([source, source])

    assert len(result) == 1
    assert result[0].model_dump(mode="json") == {**ref, "demand_type": "SALES_ORDER"}


def test_metadata_is_authoritative_and_never_makes_a_parent_item_a_demand() -> None:
    ref = _ref()
    sources = _metadata([ref, ref])

    result = canonical_demand_refs(sources)

    assert [entry.model_dump(mode="json") for entry in result] == [ref]
    assert str(result[0].demand_id) != sources[0]["via_item_id"]


def test_shared_reference_subclasses_deduplicate_by_identity() -> None:
    raw = _ref()
    provenance = RootDemandProvenance(
        root_demand_refs=[DemandReference.model_validate(raw), SourceDemandRef.model_validate(raw)],
        root_demand_refs_complete=True,
    )
    assert len(provenance.root_demand_refs) == 1


@pytest.mark.parametrize("sources", ["invalid", {"source": "BOM"}, [None], [1], [["BOM"]]])
def test_malformed_fact_lists_refuse_conversion_but_remain_readable(sources: Any) -> None:
    with pytest.raises(BusinessRuleError):
        canonical_demand_refs(sources)
    projection = project_demand_provenance(sources)
    assert projection is not None and projection.root_demand_refs_complete is False


@pytest.mark.parametrize(
    "sources",
    [
        _metadata([_ref()], complete=False),
        _metadata([]),
        [{"source": "BOM", "via_item_id": str(uuid.uuid4()), "quantity": "1"}],
        [{"source": "SALES_ORDER", "source_id": "invalid", "source_line_id": None}],
        [{"source": "MANUAL", "source_id": None, "source_line_id": None}],
        [{"source": "SALES_ORDER", "source_id": None}],
        [{"source": "BOM", "root_demand_refs": [_ref()]}],
        [{"source": "BOM", "root_demand_refs": [_ref()], "root_demand_refs_complete": "true"}],
        _metadata([{"demand_type": "SALES_ORDER", "demand_id": "invalid"}]),
    ],
)
def test_incomplete_or_invalid_provenance_refuses_conversion(sources: list[dict[str, Any]]) -> None:
    with pytest.raises(BusinessRuleError, match=r"(?i)(provenance|demand)"):
        canonical_demand_refs(sources)


def test_legacy_full_display_cap_cannot_be_assumed_complete() -> None:
    sources = [
        {"source": "SALES_ORDER", "source_id": str(uuid.uuid4()), "source_line_id": None}
        for _ in range(MAX_SOURCE_DEMANDS_LISTED)
    ]

    with pytest.raises(BusinessRuleError, match=r"(?i)(rerun|run|scope|split)"):
        canonical_demand_refs(sources)


def test_more_than_fifty_distinct_roots_is_refused_without_silent_truncation() -> None:
    with pytest.raises(BusinessRuleError):
        canonical_demand_refs(_metadata([_ref() for _ in range(MAX_SOURCE_DEMAND_REFS + 1)]))


def test_empty_legacy_source_list_stays_compatible_with_old_hook_callers() -> None:
    assert canonical_demand_refs(None) == []
    assert canonical_demand_refs([]) == []


def test_fifty_distinct_canonical_roots_are_accepted() -> None:
    assert len(canonical_demand_refs(_metadata([_ref() for _ in range(50)]))) == 50


def test_many_repeated_canonical_roots_are_deduplicated_before_the_limit() -> None:
    ref = _ref()
    assert len(canonical_demand_refs(_metadata([ref] * 75))) == 1


def test_open_demand_type_vocabulary_is_preserved_and_normalized() -> None:
    result = canonical_demand_refs(_metadata([_ref(demand_type="  packing_order  ")]))
    assert result[0].demand_type == "PACKING_ORDER"


def test_malformed_canonical_metadata_never_falls_back_to_a_valid_direct_fact() -> None:
    ref = _ref()
    sources = [
        {
            "source": "SALES_ORDER",
            "source_id": ref["demand_id"],
            "source_line_id": ref["demand_line_id"],
            "root_demand_refs": "invalid",
            "root_demand_refs_complete": True,
        }
    ]
    with pytest.raises(BusinessRuleError):
        canonical_demand_refs(sources)
    projection = project_demand_provenance(sources)
    assert projection is not None
    assert projection.root_demand_refs == []
    assert projection.root_demand_refs_complete is False


def test_multiple_canonical_aggregates_are_refused() -> None:
    with pytest.raises(BusinessRuleError):
        canonical_demand_refs(_metadata([_ref()]) + _metadata([_ref()]))


@pytest.mark.parametrize(
    "sources",
    [
        [{"source": "BOM", "via_item_id": str(uuid.uuid4())}],
        _metadata([]),
        [{"source": "BOM", "root_demand_refs": [], "root_demand_refs_complete": "true"}],
    ],
)
def test_read_dto_exposes_historical_incomplete_provenance_without_failing(
    sources: list[dict[str, Any]],
) -> None:
    suggestion = MrpSuggestionRead.model_validate(
        {
            "id": uuid.uuid4(),
            "run_id": uuid.uuid4(),
            "item_id": uuid.uuid4(),
            "item_code": "LEGACY-ROOTS",
            "suggestion_type": "PURCHASE",
            "required_quantity": Decimal("5"),
            "suggested_quantity": Decimal("5"),
            "shortage_quantity": Decimal("5"),
            "available_quantity": Decimal("0"),
            "incoming_quantity": Decimal("0"),
            "required_date": REQUIRED,
            "suggested_order_date": AS_OF,
            "is_late": False,
            "lead_time_days": 0,
            "status": "OPEN",
            "source_demands": sources,
        }
    )

    data = suggestion.model_dump(mode="json")

    assert data["demand_provenance"] == {
        "root_demand_refs": [],
        "root_demand_refs_complete": False,
    }


async def test_common_part_keeps_root_sales_ids_through_multiple_levels() -> None:
    kit = make_product("PROVENANCE-KIT")
    assembly = make_product("PROVENANCE-ASSEMBLY")
    raw = make_item("PROVENANCE-RAW")
    source = FakeSource(
        items={item.id: item for item in (kit, assembly, raw)},
        structure={kit.id: [(assembly, "2"), (raw, "3")], assembly.id: [(raw, "4")]},
        default_bom={kit.id, assembly.id},
    )
    demands = [sales_line(kit, "5", code="SO-ROOT-A"), sales_line(kit, "2", code="SO-ROOT-B")]

    plan = await engine.plan(source, demands=demands, as_of=AS_OF, horizon_end=HORIZON_END)

    suggestion = plan.suggestion_for(raw.id)
    assert suggestion is not None
    assert suggestion.suggested_quantity == Decimal("77.0000")
    refs = canonical_demand_refs(suggestion.source_demands)
    assert {(ref.demand_id, ref.demand_line_id) for ref in refs} == {
        (demand.source_id, demand.source_line_id) for demand in demands
    }
    assert len(refs) == 2, "The same root reached through two BOM branches appears once."
    assert all(ref.demand_type == "SALES_ORDER" for ref in refs)
    assert all(ref.demand_id not in {kit.id, assembly.id} for ref in refs)


async def test_display_truncation_does_not_drop_a_small_set_of_distinct_roots() -> None:
    raw = make_item("REPEATED-ROOT")
    demand = sales_line(raw, "1", code="SO-ONE-ROOT")
    source = FakeSource(items={raw.id: raw})

    plan = await engine.plan(
        source,
        demands=[demand] * (MAX_SOURCE_DEMANDS_LISTED + 4),
        as_of=AS_OF,
        horizon_end=HORIZON_END,
    )

    requirement = plan.requirement_for(raw.id)
    suggestion = plan.suggestion_for(raw.id)
    assert requirement is not None and suggestion is not None
    assert requirement.source_demand_count == MAX_SOURCE_DEMANDS_LISTED + 4
    assert len(suggestion.source_demands) == MAX_SOURCE_DEMANDS_LISTED
    assert suggestion.source_demands[0]["root_demand_refs_complete"] is True
    assert len(canonical_demand_refs(suggestion.source_demands)) == 1


async def test_a_root_introduced_after_the_display_cap_is_still_retained() -> None:
    raw = make_item("LATE-ROOT")
    first = sales_line(raw, "1", code="SO-REPEATED")
    last = sales_line(raw, "1", code="SO-AFTER-CAP")
    plan = await engine.plan(
        FakeSource(items={raw.id: raw}),
        demands=[first] * (MAX_SOURCE_DEMANDS_LISTED + 1) + [last],
        as_of=AS_OF,
        horizon_end=HORIZON_END,
    )

    suggestion = plan.suggestion_for(raw.id)
    assert suggestion is not None
    assert not any(
        source["source_id"] == str(last.source_id) for source in suggestion.source_demands
    )
    refs = canonical_demand_refs(suggestion.source_demands)
    assert {ref.demand_id for ref in refs} == {first.source_id, last.source_id}


async def test_large_root_set_finishes_mrp_but_cannot_create_a_partial_requisition() -> None:
    kit = make_product("MANY-ROOTS-KIT")
    raw = make_item("MANY-ROOTS-RAW")
    source = FakeSource(
        items={kit.id: kit, raw.id: raw},
        structure={kit.id: [(raw, "2")]},
        default_bom={kit.id},
    )
    demands = [
        sales_line(kit, "1", code=f"SO-ROOT-{index}") for index in range(MAX_SOURCE_DEMAND_REFS + 1)
    ]

    plan = await engine.plan(source, demands=demands, as_of=AS_OF, horizon_end=HORIZON_END)

    suggestion = plan.suggestion_for(raw.id)
    assert suggestion is not None
    assert suggestion.suggested_quantity == Decimal("102.0000")
    assert suggestion.source_demands[0]["root_demand_refs_complete"] is False
    assert len(suggestion.source_demands[0]["root_demand_refs"]) == MAX_SOURCE_DEMAND_REFS
    with pytest.raises(BusinessRuleError):
        canonical_demand_refs(suggestion.source_demands)


async def test_same_order_different_lines_remain_distinct_roots() -> None:
    raw = make_item("SAME-ORDER-DIFFERENT-LINES")
    first = sales_line(raw, "1", code="SO-ROOT")
    second = replace(first, source_line_id=uuid.uuid4())

    plan = await engine.plan(
        FakeSource(items={raw.id: raw}),
        demands=[first, second],
        as_of=AS_OF,
        horizon_end=HORIZON_END,
    )

    suggestion = plan.suggestion_for(raw.id)
    assert suggestion is not None
    assert len(canonical_demand_refs(suggestion.source_demands)) == 2


async def test_manual_snapshot_identity_survives_bom_explosion() -> None:
    kit = make_product("MANUAL-KIT")
    raw = make_item("MANUAL-RAW")
    demand_id = uuid.uuid4()
    demand = engine.DemandLine(
        item_id=kit.id,
        quantity=Decimal("3"),
        required_date=REQUIRED,
        source=MrpDemandSource.MANUAL,
        demand_id=demand_id,
    )

    plan = await engine.plan(
        FakeSource(
            items={kit.id: kit, raw.id: raw},
            structure={kit.id: [(raw, "2")]},
            default_bom={kit.id},
        ),
        demands=[demand],
        as_of=AS_OF,
        horizon_end=HORIZON_END,
    )

    suggestion = plan.suggestion_for(raw.id)
    assert suggestion is not None
    refs = canonical_demand_refs(suggestion.source_demands)
    assert len(refs) == 1
    assert refs[0].demand_type == "MRP_DEMAND"
    assert refs[0].demand_id == demand_id
    assert refs[0].demand_line_id is None


async def test_snapshot_replay_carries_the_stable_demand_row_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demand_id = uuid.uuid4()
    row = SimpleNamespace(
        id=demand_id,
        item_id=uuid.uuid4(),
        quantity=Decimal("2"),
        required_date=REQUIRED,
        demand_source=MrpDemandSource.MANUAL,
        source_id=None,
        source_line_id=None,
        source_code=None,
        warehouse_id=None,
        priority=5,
        notes=None,
    )

    async def all_for_run(self: Any, run_id: uuid.UUID) -> list[Any]:
        return [row]

    monkeypatch.setattr(service.MrpDemandRepository, "all_for_run", all_for_run)
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())

    result = await service._snapshot_demand(None, tenant, uuid.uuid4())  # type: ignore[arg-type]

    assert len(result) == 1
    assert result[0].demand_id == demand_id
