"""Carton barcode generation.

A barcode is printed on a physical box and then read back by a scanner, by a
camera, or out loud over a telephone. Two properties have to hold or the label is
worse than useless: two boxes must never answer to one value, and a value that
has been corrupted in transit must be detectable rather than silently resolving
to somebody else's carton.

These tests are the evidence for both.
"""

from __future__ import annotations

import pytest

from src.modules.packing.models import BARCODE_LENGTH
from src.modules.packing.service import (
    BARCODE_ALPHABET,
    barcode_body,
    barcode_check_character,
    build_barcode,
    verify_barcode,
)

ORDER_CODE = "PK/2026-27/00042"


# -----------------------------------------------------------------------------
# Shape
# -----------------------------------------------------------------------------


def test_the_body_keeps_only_scannable_characters() -> None:
    assert barcode_body(ORDER_CODE) == "PK20262700042"


def test_the_body_upper_cases_a_lower_case_code() -> None:
    assert barcode_body("pk/2026-27/00042") == barcode_body(ORDER_CODE)


def test_a_barcode_is_the_body_the_sequence_and_one_check_character() -> None:
    value = build_barcode(order_code=ORDER_CODE, carton_number=3)
    assert value.startswith("PK202627000420003")
    assert len(value) == len("PK202627000420003") + 1


def test_every_generated_barcode_is_within_the_column_width() -> None:
    for number in (1, 42, 999, 1000):
        assert len(build_barcode(order_code=ORDER_CODE, carton_number=number)) <= BARCODE_LENGTH


def test_every_character_is_in_the_declared_alphabet() -> None:
    # The database CHECK only allows [0-9A-Z-]; a generator that emitted anything
    # else would fail at INSERT, which is a bad place to find out.
    value = build_barcode(order_code=ORDER_CODE, carton_number=7)
    assert set(value) <= set(BARCODE_ALPHABET)


def test_generation_is_deterministic() -> None:
    # The barcode has to be reproducible from the order code and the carton
    # number alone: a reprinted label must carry the same value as the first one.
    first = build_barcode(order_code=ORDER_CODE, carton_number=5)
    second = build_barcode(order_code=ORDER_CODE, carton_number=5)
    assert first == second


# -----------------------------------------------------------------------------
# Uniqueness
# -----------------------------------------------------------------------------


def test_two_cartons_of_one_order_never_share_a_barcode() -> None:
    values = {build_barcode(order_code=ORDER_CODE, carton_number=n) for n in range(1, 1001)}
    assert len(values) == 1000


def test_two_orders_never_share_a_barcode_for_the_same_carton_number() -> None:
    a = build_barcode(order_code="PK/2026-27/00042", carton_number=1)
    b = build_barcode(order_code="PK/2026-27/00043", carton_number=1)
    assert a != b


def test_the_sequence_is_zero_padded_so_ten_cannot_collide_with_one() -> None:
    # Without padding, order "PK1" carton 11 and order "PK11" carton 1 would both
    # encode as "PK111" - two different boxes, one barcode.
    assert build_barcode(order_code="PK1", carton_number=11) != build_barcode(
        order_code="PK11", carton_number=1
    )


# -----------------------------------------------------------------------------
# Error detection - the point of the check character
# -----------------------------------------------------------------------------


def test_a_generated_barcode_verifies() -> None:
    assert verify_barcode(build_barcode(order_code=ORDER_CODE, carton_number=12))


def test_verification_is_case_insensitive_because_scanners_are_not_typists() -> None:
    value = build_barcode(order_code=ORDER_CODE, carton_number=12)
    assert verify_barcode(value.lower())


def test_every_single_character_substitution_is_caught() -> None:
    value = build_barcode(order_code=ORDER_CODE, carton_number=8)
    for position, original in enumerate(value):
        for replacement in BARCODE_ALPHABET:
            if replacement == original:
                continue
            corrupted = value[:position] + replacement + value[position + 1 :]
            assert not verify_barcode(corrupted), corrupted


def test_transposing_two_different_adjacent_characters_is_caught() -> None:
    value = build_barcode(order_code=ORDER_CODE, carton_number=8)
    for position in range(len(value) - 1):
        pair = {value[position], value[position + 1]}
        if len(pair) == 1:
            # Swapping two identical characters is not a corruption at all.
            continue
        if pair == {"0", "Z"}:
            continue  # the documented blind spot - see the test below
        swapped = value[:position] + value[position + 1] + value[position] + value[position + 2 :]
        assert not verify_barcode(swapped), swapped


def test_the_one_transposition_luhn_cannot_see_is_zero_beside_z() -> None:
    # Documented, not a defect. Every Luhn mod N has exactly one pair whose
    # doubling maps collide - 0 and 9 in decimal, 0 and Z in base 36 - so a
    # "0Z" read back as "Z0" checksums identically. Recorded here so the limit
    # is known rather than discovered during an incident.
    assert barcode_check_character("0Z") == barcode_check_character("Z0")


def test_a_truncated_barcode_does_not_verify() -> None:
    value = build_barcode(order_code=ORDER_CODE, carton_number=8)
    assert not verify_barcode(value[:-1])


def test_a_hand_written_value_does_not_verify() -> None:
    assert not verify_barcode("CARTON-1")
    assert not verify_barcode("")
    assert not verify_barcode("   ")


def test_a_value_outside_the_alphabet_does_not_verify() -> None:
    # Lower-case is normalised, but punctuation is not: it fails the shape check
    # rather than reaching the checksum.
    assert not verify_barcode("PK/2026-27/00042*1")


def test_the_check_character_folds_overflow_back_in() -> None:
    # "Z" is index 35 and is in a doubled position, so it contributes
    # 70 // 36 + 70 % 36 = 35 and the check character is 1. An implementation
    # that fed the raw 70 straight into the modulus would produce 2 - and would
    # stop detecting substitutions in every doubled position.
    assert barcode_check_character("Z") == "1"


# -----------------------------------------------------------------------------
# Refusals
# -----------------------------------------------------------------------------


def test_a_non_positive_carton_number_is_refused() -> None:
    with pytest.raises(ValueError, match="carton_number"):
        build_barcode(order_code=ORDER_CODE, carton_number=0)


def test_an_order_code_with_nothing_to_encode_is_refused() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        build_barcode(order_code="///", carton_number=1)


def test_a_barcode_that_would_overflow_the_column_is_refused_not_truncated() -> None:
    # Truncating the body would let two orders share a barcode, which is the one
    # failure this value exists to prevent.
    with pytest.raises(ValueError, match="exceed"):
        build_barcode(order_code="X" * BARCODE_LENGTH, carton_number=1)


def test_a_character_outside_the_alphabet_cannot_be_checksummed() -> None:
    with pytest.raises(ValueError, match="alphabet"):
        barcode_check_character("PK-1")
