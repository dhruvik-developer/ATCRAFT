"""A form states a length, a precision or a sign through the shared vocabulary.

``validation`` in ``apps/web/src/lib/messages.ts`` words the rules every form
shares: ``maxLength(64)`` reads "At most 64 characters", ``decimals('a
quantity', 4)`` reads "Enter a quantity with up to 4 decimal places", and
``positive('Quantity')`` reads "Quantity must be greater than zero.". Beside it,
339 messages were typed by hand in dozens of wordings - "Code is at most 64
characters", "At most 120 characters.", "Company code is too long", "Keep the
reason under 500 characters", "up to 4 decimals" against "up to 4 decimal
places" on two fields of one form, "must be more than zero" against "must be
greater than zero", "non-negative" - so one rule read differently per screen,
and every limit was typed twice: once in the schema and once in prose.

The rule blanks comments and fails, anywhere in ``apps/web/src`` outside the
catalogue, on the hand-typed shapes: "at most <n> characters", "under <n>
characters", "is too long", "up to <n> decimals" or "decimal places", "to <n>
decimal places", "must be more/greater than zero" closing a message, and
"non-negative". A rule that is not one of these - "Up to 12 digits and 6
decimal places", "Between 0 and 6 decimal places" - is left alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_frontend_copy_internals import GENERATED, blank_comments
from tests.unit.test_frontend_mutation_error_toasts import WEB_SRC, _line

pytestmark = pytest.mark.unit

MESSAGES = WEB_SRC / "lib" / "messages.ts"

_COUNT = r"(?:\d+|two|three|four|six|\$\{[^}]+\})"
_HAND_TYPED = re.compile(
    rf"\b[Aa]t most {_COUNT} characters\b"
    rf"|\bunder {_COUNT} characters\b"
    r"|\bis too long\b"
    rf"|\b[Uu]p to {_COUNT} decimal(?:s\b| places\b)"
    rf"|\bto {_COUNT} decimal places\b"
    # The whole sentence a builder says, closing its literal; a range such as
    # "greater than 0 and at most 100%" is a different rule.
    r"|\bmust be (?:more|greater) than (?:zero|0)\.?(?=['\"`])"
    r"|\bnon-negative\b"
)


def hand_typed_rules(source: str) -> list[str]:
    """``line N: 'wording'`` for each shared validation rule typed out by hand."""
    code = blank_comments(source)
    return [
        f"line {_line(code, match.start())}: {match.group(0)!r}"
        for match in _HAND_TYPED.finditer(code)
    ]


def _sources() -> list[Path]:
    return sorted(
        path
        for path in WEB_SRC.rglob("*.ts*")
        if path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".test." not in path.name
        and path.relative_to(WEB_SRC).parts[0] != "test"
        and path not in {MESSAGES, GENERATED}
    )


def test_the_builders_the_rule_points_to_exist() -> None:
    catalogue = MESSAGES.read_text(encoding="utf-8")
    assert "maxLength: (limit: number) => `At most ${limit} characters`" in catalogue
    assert "decimals: (what: string, places: number) =>" in catalogue
    assert "positive: (field: string) => `${field} must be greater than zero.`" in catalogue
    assert len(_sources()) > 300, "source discovery found too few files"


def test_no_form_types_a_shared_rule_by_hand() -> None:
    offenders = {
        path.relative_to(WEB_SRC).as_posix(): found
        for path in _sources()
        if (found := hand_typed_rules(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "These messages type a shared validation rule by hand. Say it through "
        "lib/messages.ts: validation.maxLength(n), validation.decimals(what, places) "
        f"or validation.positive(field):\n{offenders}"
    )


def test_the_rule_finds_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            "  code: z.string().max(64, 'Code is at most 64 characters'),",
            "  name: z.string().max(120, 'At most 120 characters.'),",
            "  company_code: z.string().max(32, 'Company code is too long').optional(),",
            "    `Keep the reason under ${REASON_MAX_LENGTH} characters - no more is stored.`,",
            "  cost: z.string().regex(DECIMAL, 'Enter an amount with up to 4 decimals'),",
            "  weight: z.string().regex(WEIGHT, 'Up to 3 decimal places'),",
            "    .refine(isPositive, 'Enter a quantity greater than zero, to 4 decimal places.'),",
            "    .refine((value) => value !== '0', 'Quantity per must be more than zero'),",
            "  discount: z.string().regex(MONEY, 'Enter a non-negative discount'),",
            "  problems.push('Every amount takes up to two decimal places.')",
            "  <FieldError message={`At most ${REASON_MAX_LENGTH} characters`} />",
        ]
    )

    assert [entry.split(":")[0] for entry in hand_typed_rules(source)] == [
        f"line {number}" for number in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
    ]


def test_the_rule_leaves_builders_other_rules_and_comments_alone() -> None:
    source = "\n".join(
        [
            "  code: z.string().max(64, validation.maxLength(64)),",
            "  quantity: z.string().regex(QUANTITY, validation.decimals('a quantity', 4)),",
            "  price: z.string().regex(PRICE, 'Up to 12 digits and 6 decimal places, no sign'),",
            "  places: z.string().refine(inRange, `Between ${MIN} and ${MAX} decimal places`),",
            "  throw new Error('Discount must be greater than 0 and at most 100%.')",
            "  hint: 'Debit-positive: negative means the account is in credit',",
            "  // A refusal used to say 'At most 64 characters.' here.",
            "  {/* up to 4 decimal places, as the API stores them */}",
        ]
    )

    assert hand_typed_rules(source) == []
