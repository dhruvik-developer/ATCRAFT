"""Tenant-scoped credit-note queries."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.modules.finance.models import CreditNote


class CreditNoteRepository(BaseRepository[CreditNote]):
    model = CreditNote
    entity_label = "Credit note"
    searchable_fields = ("code", "buyer_name")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": CreditNote.code,
        "note_date": CreditNote.note_date,
        "total_amount": CreditNote.total_amount,
        "status": CreditNote.status,
    }
