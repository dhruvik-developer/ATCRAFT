"""Report stored GSTIN and PAN values the current validation would refuse.

Run this against a real database **before** deploying:

    cd apps/api
    .venv/Scripts/python.exe -m scripts.check_tax_identifiers     # Windows
    python -m scripts.check_tax_identifiers                       # elsewhere

Why it exists
-------------
GSTIN validation now verifies the check digit, which earlier releases did not.
That is the correct rule - the tax authority rejects a mistyped number too - but
it is enforced when a record is *saved*, and the customer and vendor edit forms
resend the value they loaded. So a row holding a number that was accepted before
this release cannot be saved again until it is corrected, and the person editing
it sees a validation error on a field they did not touch.

Nothing breaks on read: lists, reports and documents are unaffected, and the
demonstration database has no such rows. This script is how you find out whether
a real one does, before somebody meets it mid-edit.

Read-only: it issues SELECTs and changes nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from sqlalchemy import Select, null, select

from src.common.tax_ids import validate_gstin, validate_pan
from src.core.database import session_scope
from src.modules.customers.models import Customer
from src.modules.organization.models import Branch, Company
from src.modules.vendors.models import Vendor

#: Every table that stores a tax identifier, as an explicit statement per table.
#: Spelled out rather than built from ``information_schema`` so there is no
#: string-composed SQL here at all.
SOURCES: tuple[tuple[str, Select[tuple[str, str | None, str | None]]], ...] = (
    ("customer", select(Customer.code, Customer.gstin, Customer.pan)),
    ("vendor", select(Vendor.code, Vendor.gstin, Vendor.pan)),
    ("company", select(Company.code, Company.gstin, Company.pan)),
    # A branch is a place of business under the company's PAN, so it carries a
    # GSTIN of its own but no PAN; the literal keeps the row shape uniform.
    ("branch", select(Branch.code, Branch.gstin, null().label("pan"))),
)


def _problems(gstin: str | None, pan: str | None) -> Sequence[str]:
    """Every reason this row could not be saved unchanged."""
    found: list[str] = []
    for label, value, check in (("GSTIN", gstin, validate_gstin), ("PAN", pan, validate_pan)):
        if not value:
            continue
        try:
            check(value)
        except ValueError as exc:
            found.append(f"{label} {value}: {exc}")
    return found


async def main() -> int:
    total = 0
    async with session_scope() as session:
        for entity, statement in SOURCES:
            for code, gstin, pan in (await session.execute(statement)).all():
                for problem in _problems(gstin, pan):
                    total += 1
                    print(f"  {entity:9} {code or '(no code)':18} {problem}")

    if total:
        print(
            f"\n{total} stored value(s) would be refused on the next save.\n"
            "Correct them before deploying, or the person who next edits one of these\n"
            "records is stopped by a field they did not change."
        )
    else:
        print("Every stored GSTIN and PAN is valid. Nothing to do.")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
