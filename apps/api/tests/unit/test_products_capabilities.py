"""Capability flags: the "one item, many roles" rule (master rules section 1).

The property these tests protect is that ``item_group`` is a *classification*.
It may seed defaults at creation, and it may never decide what an item is
allowed to do afterwards.
"""

from __future__ import annotations

import pytest

from src.core.errors import BusinessRuleError
from src.modules.products.models import ItemGroup, ProductionType
from src.modules.products.service import (
    CAPABILITY_FIELDS,
    default_capabilities,
    resolve_capabilities,
    validate_capabilities,
)


@pytest.mark.parametrize("group", list(ItemGroup))
def test_every_group_yields_a_complete_flag_set(group: ItemGroup) -> None:
    flags = default_capabilities(group)
    assert set(flags) == set(CAPABILITY_FIELDS)
    assert all(isinstance(value, bool) for value in flags.values())


def test_defaults_reflect_the_usual_role_of_each_group() -> None:
    raw = default_capabilities(ItemGroup.RAW_MATERIAL)
    assert raw["can_purchase"] is True
    assert raw["can_be_bom_component"] is True
    assert raw["can_sell"] is False

    finished = default_capabilities(ItemGroup.FINISHED_GOOD)
    assert finished["can_sell"] is True
    assert finished["can_manufacture"] is True


def test_assembly_groups_start_with_the_defaults_of_what_they_are() -> None:
    """docs/assumptions.md §E4-6: an assembly is a finished good built on the
    bench, a sub-assembly is a semi-finished item - so each starts with exactly
    that group's flags, and nothing else reads the group afterwards."""
    assert default_capabilities(ItemGroup.ASSEMBLY) == default_capabilities(ItemGroup.FINISHED_GOOD)
    assert default_capabilities(ItemGroup.SUB_ASSEMBLY) == default_capabilities(
        ItemGroup.SEMI_FINISHED
    )
    sub_assembly = default_capabilities(ItemGroup.SUB_ASSEMBLY)
    assert sub_assembly["can_manufacture"] is True
    assert sub_assembly["can_be_assembly_component"] is True
    assert sub_assembly["can_sell"] is False


def test_a_raw_material_can_be_made_sellable_and_stays_sellable() -> None:
    # The scenario the whole design exists for: the same part is bought as raw
    # material, consumed in a BOM, and also sold over the counter as a spare.
    flags = resolve_capabilities(ItemGroup.RAW_MATERIAL, {"can_sell": True, "can_be_spare": True})

    assert flags["can_sell"] is True
    assert flags["can_be_spare"] is True
    assert flags["can_purchase"] is True
    assert flags["can_be_bom_component"] is True


def test_explicit_false_overrides_a_default_true() -> None:
    flags = resolve_capabilities(ItemGroup.FINISHED_GOOD, {"can_sell": False})
    assert flags["can_sell"] is False


def test_unset_flag_keeps_the_group_default() -> None:
    flags = resolve_capabilities(ItemGroup.FINISHED_GOOD, {"can_sell": None})
    assert flags["can_sell"] is True


@pytest.mark.parametrize("group", list(ItemGroup))
def test_item_group_never_constrains_an_explicit_capability(group: ItemGroup) -> None:
    """Whatever the group, an explicitly requested capability is honoured.

    This is the rule that stops ``item_group`` becoming a behaviour switch: a
    SERVICE item the business decides to stock, or SCRAP it wants to send for
    job work, must both be expressible.
    """
    requested = dict.fromkeys(CAPABILITY_FIELDS, True)
    assert resolve_capabilities(group, requested) == dict.fromkeys(CAPABILITY_FIELDS, True)


def test_an_item_with_no_capability_at_all_is_rejected() -> None:
    with pytest.raises(BusinessRuleError):
        validate_capabilities(ProductionType.BUY, dict.fromkeys(CAPABILITY_FIELDS, False))


def test_make_requires_the_manufacture_capability() -> None:
    flags = dict.fromkeys(CAPABILITY_FIELDS, False)
    flags["can_purchase"] = True

    with pytest.raises(BusinessRuleError) as excinfo:
        validate_capabilities(ProductionType.MAKE, flags)

    assert excinfo.value.details["missing_capabilities"] == ["can_manufacture"]


def test_make_or_buy_requires_both_capabilities() -> None:
    flags = dict.fromkeys(CAPABILITY_FIELDS, False)
    flags["can_manufacture"] = True

    with pytest.raises(BusinessRuleError) as excinfo:
        validate_capabilities(ProductionType.MAKE_OR_BUY, flags)

    assert excinfo.value.details["missing_capabilities"] == ["can_purchase"]


def test_job_work_requires_the_job_work_capability() -> None:
    flags = dict.fromkeys(CAPABILITY_FIELDS, False)
    flags["can_manufacture"] = True

    with pytest.raises(BusinessRuleError):
        validate_capabilities(ProductionType.JOB_WORK, flags)


def test_a_coherent_capability_set_passes() -> None:
    flags = dict.fromkeys(CAPABILITY_FIELDS, False)
    flags.update(can_manufacture=True, can_purchase=True, can_sell=True)

    validate_capabilities(ProductionType.MAKE_OR_BUY, flags)
