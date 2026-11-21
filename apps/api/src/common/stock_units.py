"""A document line is stated in its item's stock unit.

ASSUMPTION (docs/assumptions.md §UOM-1): until documents convert units, every
purchase and sales document line is in its item's stock unit. The inventory
engine has no unit of its own - a ``MovementRequest`` carries a bare quantity,
and every balance and ledger row holds it in the item's stock (base) unit - so a
line keyed in a purchase or sale unit would post its number as stock: five
packets of ten received as five screws, each at a packet's price. A line naming
any other unit is refused here, never converted, and so is a source line a
posting copies from a document written before the rule.

Every caller already holds its items' stock units in the item facts it loaded,
so a document in the stock unit costs no read here; the unit codes are read only
to word a refusal.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.repository import labels_by_id, labels_in_order
from src.core.deps import TenantContext
from src.core.errors import NotFoundError, ValidationError
from src.core.messages import not_in_stock_unit
from src.modules.products.models import UnitOfMeasure

#: Advice for a refused line, as a fixed sentence or built from the stock
#: unit's code (``"add it again as a new line in NOS"``).
Remedy = str | Callable[[str], str]


@dataclass(frozen=True, slots=True)
class LineUnit:
    """One document line's unit, beside its item's stock unit.

    ``subject`` is how a refusal names the line: ``"Line 2"`` on the document
    being written, ``"Purchase order PO-7 line 2"`` for a source line a posting
    copies. ``uom_id`` is ``None`` for a line that names no unit, which the
    document then writes in the stock unit.
    """

    subject: str
    item_code: str
    stock_uom_id: uuid.UUID
    uom_id: uuid.UUID | None


async def require_stock_units(
    session: AsyncSession,
    tenant: TenantContext,
    lines: Iterable[LineUnit],
    *,
    remedy: Remedy | None = None,
) -> None:
    """Refuse the first line whose unit is not its item's stock unit.

    A unit this company does not have is "not found", as any foreign reference
    is. ``remedy`` replaces the advice to enter the quantity in the stock unit,
    for a line the caller copied from a source document, or one saved before the
    rule, where re-keying the number on the same line is not what fixes it.
    """
    refused = next(
        (line for line in lines if line.uom_id not in (None, line.stock_uom_id)),
        None,
    )
    if refused is None or refused.uom_id is None:
        return
    pair = (refused.stock_uom_id, refused.uom_id)
    codes = await labels_by_id(session, tenant, pair, UnitOfMeasure.code)
    if refused.uom_id not in codes:
        raise NotFoundError("Unit of measure", refused.uom_id)
    stock_unit, unit = labels_in_order(pair, codes, "code")
    raise ValidationError(
        not_in_stock_unit(
            refused.subject,
            refused.item_code,
            stock_unit,
            unit,
            remedy(stock_unit) if callable(remedy) else remedy,
        ),
        details={
            "item_code": refused.item_code,
            "uom_id": str(refused.uom_id),
            "stock_uom_id": str(refused.stock_uom_id),
        },
    )


__all__ = ["LineUnit", "Remedy", "require_stock_units"]
