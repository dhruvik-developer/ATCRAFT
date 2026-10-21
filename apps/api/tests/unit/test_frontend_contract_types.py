"""A hand-written frontend type must not declare a field the API does not send.

``apps/web/src/types/openapi.ts`` is generated and hook-checked, so anything
derived from it (``SchemaOf<'ItemAttributeOut'>``) cannot drift. Some screens
declare the response shape by hand instead, and the compiler compares those
against nothing at all.

That is not hypothetical. ``items.valuation_method`` was dropped from the API
when every item became FIFO - migration ``7e8eb4bdd0ed`` removed the column and
no schema has carried it since. ``ItemDetail`` in ``item-model.ts`` went on
declaring it, ``item-detail-page.tsx`` went on drawing a "Valuation" row from it,
and ``humanise(undefined)`` rendered that row as a dash. Every item in the ERP
showed a labelled field with nothing in it, for months, on a screen people read
daily, with a green build throughout - because nothing compared the two.

Why the Pydantic model and not ``openapi.json``
-----------------------------------------------
Twenty-seven endpoints declare ``response_model=None``, the item master among
them, and a response with no model produces an empty ``schema: {}`` in the
contract. That is a deliberate choice where the shape depends on the caller -
``get_item`` returns ``ItemDetailWithCost`` to a principal who may see costs and
``ItemDetail`` to one who may not, and a declared model would strip the costs
from the first. The consequence is that the contract cannot describe those
endpoints, which is precisely why the frontend hand-wrote the shape in the first
place, and precisely why nothing caught the drift.

So this test anchors on the Pydantic model, which exists whatever the router
declares. It is the authority in either case: the contract is generated from it.

Only extra fields fail. A mirror that declares a subset is normal and often
deliberate - a screen states what it uses - so a missing field is reported by
the second test as information, not as an error.

A backend test on purpose, for the same reason as the permission catalogue
beside it: the backend owns the shape and the frontend copies it, so the copy is
what must be checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.modules.auth.schemas import MeResponse
from src.modules.inventory.schemas import ItemStockSummaryWithCost, WarehouseStockRowWithCost
from src.modules.production.schemas import ProductionOrderDetail, ProductionOrderSummary
from src.modules.products.schemas import (
    CategoryOut,
    ItemDetail,
    ItemDetailWithCost,
    ItemSummary,
    ItemSummaryWithCost,
    UomOut,
)

pytestmark = pytest.mark.unit

WEB = Path(__file__).resolve().parents[4] / "apps" / "web" / "src"

#: Hand-written mirrors: the file, the interface, and the Pydantic model it
#: mirrors. Register a pair here when a screen declares a response shape by hand
#: - or better, derive it from the generated types and do not need an entry.
#: Each pair names the WIDEST shape the endpoint can return. Several of these
#: responses vary by permission - a principal entitled to see costs receives more
#: - and comparing against the narrow one would flag a field that some people do
#: legitimately receive.
MIRRORS: tuple[tuple[str, str, type[BaseModel]], ...] = (
    # Every page load reads this one, and it is where company_name sat
    # declared-but-never-sent, rendering a blank line in the sidebar.
    ("features/auth/auth-context.tsx", "MeResponse", MeResponse),
    ("features/items/item-model.ts", "ItemDetail", ItemDetailWithCost),
    ("features/items/item-model.ts", "ItemRow", ItemSummaryWithCost),
    ("features/items/item-model.ts", "ItemWarehouseStockRow", WarehouseStockRowWithCost),
    ("features/items/item-model.ts", "ItemStockSummary", ItemStockSummaryWithCost),
    ("features/items/item-model.ts", "CategoryOption", CategoryOut),
    ("features/items/item-model.ts", "UomOption", UomOut),
    ("features/inventory/types.ts", "ItemOption", ItemSummary),
    # Registered when the cost centre was added to both: a field typed by hand
    # into a shape the contract does not check is exactly how valuation_method
    # outlived its column. The detail interface extends the row; only its own
    # body is read here, and the row's fields are checked through the row.
    ("features/production/production-model.ts", "ProductionOrderRow", ProductionOrderSummary),
    ("features/production/production-model.ts", "ProductionOrderDetail", ProductionOrderDetail),
)

#: Fields a mirror may declare that the model does not carry, with the reason.
#: Deliberately empty - every entry would be a screen showing something the
#: server does not send, which is the defect this test exists to catch.
ALLOWED_EXTRA: dict[str, set[str]] = {}


def _interface_fields(source: str, name: str) -> set[str]:
    """Property names declared in ``interface <name> { ... }``.

    Brace-matched rather than regex-matched to a closing brace: a nested object
    type inside a field would end the match early and silently shorten the field
    list, which would make this test pass by looking at less.
    """
    start = source.index(f"interface {name}")
    open_brace = source.index("{", start)
    depth, index = 0, open_brace
    while index < len(source):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                break
        index += 1
    body = source[open_brace + 1 : index]

    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"^\s*([a-z_][a-z0-9_]*)\??\s*:", body, re.M))


@pytest.mark.parametrize(("path", "interface", "model"), MIRRORS)
def test_a_hand_written_type_declares_no_field_the_api_dropped(
    path: str, interface: str, model: type[BaseModel]
) -> None:
    source = (WEB / path).read_text(encoding="utf-8")

    declared = _interface_fields(source, interface)
    extra = declared - set(model.model_fields) - ALLOWED_EXTRA.get(interface, set())

    assert extra == set(), (
        f"{path} declares {sorted(extra)} on {interface}, which {model.__name__} does not "
        f"carry. A screen reading one of those draws an empty value - see valuation_method, "
        f"which sat on the item screen for months. Remove the field and whatever renders it."
    )


@pytest.mark.parametrize(("path", "interface", "model"), MIRRORS)
def test_what_a_hand_written_type_is_missing_is_recorded_not_failed(
    path: str, interface: str, model: type[BaseModel], record_property: object
) -> None:
    """Information, not an error: declaring a subset is a legitimate choice.

    Recorded as a test property rather than asserted, so a field the API
    gained and no screen can reach stays visible to whoever looks, without
    failing a build over a decision that is usually deliberate.
    """
    source = (WEB / path).read_text(encoding="utf-8")
    missing = sorted(set(model.model_fields) - _interface_fields(source, interface))

    record_property("undeclared_fields", ", ".join(missing) or "none")


def test_every_hand_written_mirror_of_an_item_shape_is_registered() -> None:
    """The failure that matters is a NEW hand-written mirror nobody registered.

    Scoped to the item master rather than the whole application, because that is
    the shape this test knows how to check and a list of thirty-four unrelated
    interfaces would be noise nobody acts on. The broader exposure - thirty-odd
    hand-written response types across the features - is recorded in
    docs/architecture.md rather than asserted here, because most of them mirror
    endpoints that DO declare a response model and should simply be derived from
    the generated types instead.
    """
    item_model = (WEB / "features/items/item-model.ts").read_text(encoding="utf-8")
    registered = {interface for _, interface, _ in MIRRORS}

    #: Shapes that mirror no single response and so cannot be compared here.
    #: Named rather than skipped by a pattern, so adding one is a decision.
    not_a_response_mirror = {
        # The where-used answer is assembled by the BOM module from several
        # queries; there is no one model that is its shape.
        "WhereUsedRow",
        # A line of a larger response rather than a response, and the response
        # that carries it is already checked through its own mirror.
        "ItemPrice",
    }

    declared = set(re.findall(r"^export interface ([A-Z]\w+)", item_model, re.M))
    unregistered = sorted(declared - registered - not_a_response_mirror)

    assert unregistered == [], (
        f"these interfaces in item-model.ts are not registered in MIRRORS, so nothing "
        f"compares them with the API: {unregistered}. Add them, or derive them from the "
        "generated types."
    )


def test_the_narrow_shape_is_a_subset_of_the_wide_one() -> None:
    """The cost-bearing shapes must stay supersets of the ones they widen.

    The mirrors above are checked against the wide shape, so if the two ever
    diverged the other way this test would be permitting a field that the narrow
    response - the one most people actually receive - never carries.
    """
    narrow = set(ItemDetail.model_fields)
    wide = set(ItemDetailWithCost.model_fields)

    assert narrow <= wide, (
        f"ItemDetail carries {sorted(narrow - wide)} that ItemDetailWithCost does not"
    )
