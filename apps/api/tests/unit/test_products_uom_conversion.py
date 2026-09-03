"""Unit-of-measure conversion resolution.

The rules under test are the ones a wrong answer silently corrupts stock with:
an inverted factor, a conversion invented for an undefined pair, or a
weight-to-count "conversion" that is really an item-specific property.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import pytest

from src.core.errors import BusinessRuleError
from src.modules.products.models import UomCategory
from src.modules.products.service import UomGraph


@dataclass(frozen=True)
class UomRow:
    """What ``UomGraph.build`` reads off a unit row. Plain data, so the graph
    can be tested without configuring SQLAlchemy's mapper registry."""

    id: uuid.UUID
    uom_category: UomCategory
    is_base: bool


@dataclass(frozen=True)
class ConversionRow:
    from_uom_id: uuid.UUID
    to_uom_id: uuid.UUID
    factor: Decimal


NOS = uuid.uuid4()
BOX = uuid.uuid4()
DOZEN = uuid.uuid4()
KG = uuid.uuid4()
GRAM = uuid.uuid4()
LITRE = uuid.uuid4()
UNKNOWN = uuid.uuid4()


def make_graph() -> UomGraph:
    """NOS is the COUNT base, KG the WEIGHT base, and LITRE is deliberately
    orphaned in VOLUME so "no path" can be asserted."""
    return UomGraph(
        category_by_uom={
            NOS: "COUNT",
            BOX: "COUNT",
            DOZEN: "COUNT",
            KG: "WEIGHT",
            GRAM: "WEIGHT",
            LITRE: "VOLUME",
        },
        base_by_category={"COUNT": NOS, "WEIGHT": KG},
        factors={
            (BOX, NOS): Decimal("24"),
            (DOZEN, NOS): Decimal("12"),
            (KG, GRAM): Decimal("1000"),
        },
    )


def test_identity_conversion_is_one() -> None:
    assert make_graph().factor(BOX, BOX) == Decimal(1)


def test_direct_stored_factor_is_used_verbatim() -> None:
    assert make_graph().factor(BOX, NOS) == Decimal("24")


def test_inverse_of_a_stored_row_is_derived_not_demanded() -> None:
    # Only 1 BOX = 24 NOS is stored; the other direction must be derived so the
    # two halves cannot be maintained inconsistently.
    factor = make_graph().factor(NOS, BOX)
    assert factor * Decimal("24") == pytest.approx(Decimal(1))


def test_conversion_between_two_non_base_units_goes_through_the_base() -> None:
    # 1 BOX = 24 NOS, 1 DOZEN = 12 NOS, therefore 1 BOX = 2 DOZEN. Neither the
    # (BOX, DOZEN) row nor its inverse exists.
    graph = make_graph()
    assert graph.factor(BOX, DOZEN) == Decimal("2")
    assert graph.factor(DOZEN, BOX) == Decimal("0.5")


def test_convert_rounds_to_the_stored_quantity_scale() -> None:
    graph = make_graph()
    assert graph.convert(Decimal("5"), BOX, NOS) == Decimal("120.0000")
    # 1/24 = 0.041666..., quantised to the 4-place quantity scale.
    assert graph.convert(Decimal("1"), NOS, BOX) == Decimal("0.0417")


def test_round_trip_through_the_base_does_not_drift() -> None:
    graph = make_graph()
    grams = graph.convert(Decimal("2.5"), KG, GRAM)
    assert grams == Decimal("2500.0000")
    assert graph.convert(grams, GRAM, KG) == Decimal("2.5000")


def test_cross_category_conversion_is_refused_with_both_categories_named() -> None:
    with pytest.raises(BusinessRuleError) as excinfo:
        make_graph().factor(KG, NOS)

    assert excinfo.value.details["from_category"] == "WEIGHT"
    assert excinfo.value.details["to_category"] == "COUNT"


def test_undefined_pair_inside_one_category_raises_rather_than_assuming_one_to_one() -> None:
    graph = UomGraph(
        category_by_uom={LITRE: "VOLUME", UNKNOWN: "VOLUME"},
        base_by_category={"VOLUME": LITRE},
        factors={},
    )
    with pytest.raises(BusinessRuleError) as excinfo:
        graph.factor(UNKNOWN, LITRE)

    assert "conversion" in str(excinfo.value).lower()
    assert excinfo.value.details["from_uom_id"] == str(UNKNOWN)


def test_missing_base_unit_for_the_category_is_reported_clearly() -> None:
    graph = UomGraph(
        category_by_uom={LITRE: "VOLUME", UNKNOWN: "VOLUME"},
        base_by_category={},
        factors={},
    )
    with pytest.raises(BusinessRuleError) as excinfo:
        graph.factor(LITRE, UNKNOWN)

    assert excinfo.value.details["uom_category"] == "VOLUME"


def test_unknown_unit_is_rejected() -> None:
    with pytest.raises(BusinessRuleError):
        make_graph().factor(UNKNOWN, NOS)


def test_has_path_reports_reachability_without_raising() -> None:
    graph = make_graph()
    assert graph.has_path(BOX, DOZEN) is True
    assert graph.has_path(KG, NOS) is False


def test_build_reads_units_and_conversions() -> None:
    units = [
        UomRow(id=NOS, uom_category=UomCategory.COUNT, is_base=True),
        UomRow(id=BOX, uom_category=UomCategory.COUNT, is_base=False),
    ]
    conversions = [ConversionRow(from_uom_id=BOX, to_uom_id=NOS, factor=Decimal("24"))]

    graph = UomGraph.build(cast(Any, units), cast(Any, conversions))

    assert graph.base_by_category["COUNT"] == NOS
    assert graph.factor(BOX, NOS) == Decimal("24")


def test_cache_payload_round_trip_preserves_decimal_factors() -> None:
    # The graph is cached in Redis as JSON; a float round-trip here would put a
    # binary-floating-point factor into every stock conversion (section 84).
    graph = make_graph()
    restored = UomGraph.from_payload(graph.to_payload())

    assert restored.factors == graph.factors
    assert isinstance(restored.factors[(BOX, NOS)], Decimal)
    assert restored.factor(BOX, DOZEN) == Decimal("2")
