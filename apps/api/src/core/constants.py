"""Application-wide constants that are decisions, not configuration.

A value here is fixed by the business domain or the schema and must read the
same in every module: the currency a document is priced in when nobody says
otherwise, the width of a currency column. Anything an operator may legitimately
change per deployment belongs in :mod:`src.core.config` instead.

Model column defaults and Pydantic defaults both import from here, so the
database, the API contract and the demonstration data cannot disagree about
what "unspecified" means.
"""

from __future__ import annotations

# Bound named-unit creation and receipt posting consistently across documents.
MAX_SERIAL_BULK = 500

# Canonical demand identities carried from planning into one requisition line.
MAX_DEMAND_PROVENANCE_REFS = 50

#: INR fallback for model/schema defaults and callers that do not resolve a
#: company currency. Company-aware customer workflows use ``Company.currency``.
#: The compatibility ``DEFAULT_CURRENCY`` environment setting does not override
#: this constant or a company's saved currency.
DEFAULT_CURRENCY = "INR"

#: ISO 4217 codes are exactly three letters.
CURRENCY_CODE_LENGTH = 3

#: The audit trail persists the same correlation id that HTTP responses and
#: workers carry. Keep input validation aligned with ``audit_logs.request_id``.
MAX_REQUEST_ID_LENGTH = 64

# Specifications are bounded document-sized settings, not an unbounded catalogue.
MAX_SPECIFICATIONS_PER_ITEM = 200
MAX_SPECIFICATION_OPTIONS = 100
MAX_SPECIFICATION_FILTERS = 20
# Match one maximum-sized item page; inherited templates can amplify every item.
MAX_SPECIFICATION_BATCH = 200
MAX_ITEM_ACCESSORIES = 200

__all__ = [
    "CURRENCY_CODE_LENGTH",
    "DEFAULT_CURRENCY",
    "MAX_DEMAND_PROVENANCE_REFS",
    "MAX_ITEM_ACCESSORIES",
    "MAX_REQUEST_ID_LENGTH",
    "MAX_SERIAL_BULK",
    "MAX_SPECIFICATIONS_PER_ITEM",
    "MAX_SPECIFICATION_BATCH",
    "MAX_SPECIFICATION_FILTERS",
    "MAX_SPECIFICATION_OPTIONS",
]
