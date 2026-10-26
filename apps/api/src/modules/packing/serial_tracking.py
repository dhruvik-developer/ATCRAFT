"""Physical-unit selection and FINAL-QC membership rules for the packing bench."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from src.core.errors import BusinessRuleError, NotFoundError
from src.modules.packing.models import PackingOrderLine
from src.modules.packing.repository import PackingItem
from src.modules.packing.schemas import PackingSerialSelection


@dataclass(frozen=True, slots=True)
class SerialContent:
    line_id: uuid.UUID
    batch_id: uuid.UUID | None
    serial_ids: Sequence[uuid.UUID]


def requires_named_final(item: PackingItem | None) -> bool:
    return item is not None and item.is_serial_tracked and item.requires_final_qc


def select_final_units(
    lines: Sequence[PackingOrderLine],
    items: Mapping[uuid.UUID, PackingItem],
    selections: Sequence[PackingSerialSelection],
) -> None:
    """Persist an exact, disjoint selection; the quality factory validates custody."""
    by_id = {line.id: line for line in lines}
    seen: set[uuid.UUID] = set()
    for selection in selections:
        if selection.line_id in seen:
            raise BusinessRuleError("Provide one serial selection per packing line.")
        seen.add(selection.line_id)
        line = by_id.get(selection.line_id)
        if line is None:
            raise NotFoundError("Packing order line", selection.line_id)
        if not requires_named_final(items.get(line.item_id)):
            raise BusinessRuleError(
                "Serial selections belong only to lines requiring serial FINAL QC."
            )
        ids = list(selection.serial_ids)
        if len(ids) != len(set(ids)) or Decimal(len(ids)) != line.quantity_to_pack:
            raise BusinessRuleError(
                "Select one distinct serial for every unit on the packing line."
            )
        line.qc_serial_ids = ids
        line.qc_batch_id = selection.batch_id
    selected: set[uuid.UUID] = set()
    for line in lines:
        if not requires_named_final(items.get(line.item_id)):
            continue
        ids = getattr(line, "qc_serial_ids", None) or []
        if not ids or len(ids) != len(set(ids)) or Decimal(len(ids)) != line.quantity_to_pack:
            raise BusinessRuleError(
                f"Select every serial unit for FINAL QC on packing line {line.line_number}.",
                details={"line_id": str(line.id), "line_number": line.line_number},
            )
        if selected.intersection(ids):
            raise BusinessRuleError("A serial cannot be selected for two packing lines.")
        selected.update(ids)


def assert_selected_contents(
    contents: Sequence[SerialContent],
    lines: Sequence[PackingOrderLine],
    items: Mapping[uuid.UUID, PackingItem],
) -> None:
    """A carton may contain only the physical units the line submitted to FINAL QC."""
    by_id = {line.id: line for line in lines}
    packed: set[uuid.UUID] = set()
    for content in contents:
        line = by_id.get(content.line_id)
        if line is None or not requires_named_final(items.get(line.item_id)):
            continue
        selected = set(getattr(line, "qc_serial_ids", None) or [])
        if (
            not selected
            or len(content.serial_ids) != len(set(content.serial_ids))
            or packed.intersection(content.serial_ids)
            or not set(content.serial_ids).issubset(selected)
            or content.batch_id != line.qc_batch_id
        ):
            raise BusinessRuleError(
                f"Carton contents on line {line.line_number} must match its selected "
                "FINAL-QC serials and batch.",
                details={"line_id": str(line.id), "line_number": line.line_number},
            )
        packed.update(content.serial_ids)


def assert_named_verdicts(
    lines: Sequence[PackingOrderLine],
    items: Mapping[uuid.UUID, PackingItem],
    verdicts: Mapping[uuid.UUID | None, Any],
) -> None:
    """A quantity-only acceptance cannot approve a substituted or uninspected unit."""
    for line in lines:
        if not requires_named_final(items.get(line.item_id)):
            continue
        selected = set(getattr(line, "qc_serial_ids", None) or [])
        inspection = verdicts.get(line.qc_inspection_id)
        inspected = set(getattr(inspection, "serial_ids", None) or [])
        dispositions = getattr(inspection, "serial_dispositions", None) or {}
        if (
            not selected
            or Decimal(len(selected)) != line.quantity_to_pack
            or selected != inspected
            or getattr(inspection, "item_id", None) != line.item_id
            or getattr(inspection, "reference_type", None) != "packing_order"
            or getattr(inspection, "reference_id", None) != line.id
            or str(getattr(inspection, "qc_stage", "")) != "FINAL"
            or getattr(inspection, "inspected_quantity", None) != line.quantity_to_pack
            or getattr(inspection, "batch_id", None) != line.qc_batch_id
            or any(dispositions.get(str(serial_id)) != "ACCEPTED" for serial_id in selected)
        ):
            raise BusinessRuleError(
                f"Line {line.line_number} needs an ACCEPTED FINAL inspection for its "
                "exact selected serial units.",
                details={"line_id": str(line.id), "inspection_id": str(line.qc_inspection_id)},
            )
