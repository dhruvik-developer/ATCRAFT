"""The accepted physical quantity that can support a customer-return credit."""

from __future__ import annotations

from decimal import Decimal

from src.core.errors import NotFoundError
from src.core.types import ZERO
from src.modules.quality.models import QcInspection
from src.modules.sales_returns.models import SalesReturnLine


def accepted_return_quantity(line: SalesReturnLine, inspection: QcInspection | None) -> Decimal:
    if line.serial_ids:
        return Decimal(len(line.accepted_serial_ids))
    if line.qc_inspection_id is not None:
        if inspection is None:
            raise NotFoundError("QC inspection", line.qc_inspection_id)
        return Decimal(inspection.accepted_quantity)
    return ZERO if line.requires_qc else Decimal(line.quantity)
