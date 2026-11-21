"""What may be packing material, and the three places that must agree on it.

The carton form's pick-list, opening a carton and saving a packing specification
each read "usable as packing material". The seal posts one untracked issue for
the box, so an item tracked by unit or by lot is none of those
(docs/assumptions.md §PACK-2). The pick-list filters on
``PACKING_MATERIAL_EXCLUDED_TRACKING``; these tests pin that every flag there is
refused by ``_require_untracked_material``, and that a packing specification -
which MRP turns into demand for its material (§E3b-6) - is refused on one too.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.errors import BusinessRuleError
from src.modules.packing import service
from src.modules.packing.repository import PACKING_MATERIAL_EXCLUDED_TRACKING


def _material(**flags: bool) -> Any:
    facts: dict[str, Any] = {flag.key: False for flag in PACKING_MATERIAL_EXCLUDED_TRACKING}
    facts.update(flags)
    return SimpleNamespace(
        id=uuid.uuid4(), code="BOX-1", is_active=True, can_purchase=True, **facts
    )


class _SpecRepo:
    def __init__(self, material: Any) -> None:
        self._material = material

    async def item(self, item_id: uuid.UUID, *, lock: bool = False) -> Any:
        return self._material


def test_an_untracked_material_is_accepted() -> None:
    service._require_untracked_material(_material())


@pytest.mark.parametrize("flag", [flag.key for flag in PACKING_MATERIAL_EXCLUDED_TRACKING])
def test_every_flag_the_pick_list_leaves_out_is_refused(flag: str) -> None:
    with pytest.raises(BusinessRuleError, match="cannot be used as packing material"):
        service._require_untracked_material(_material(**{flag: True}))


@pytest.mark.parametrize("flag", [flag.key for flag in PACKING_MATERIAL_EXCLUDED_TRACKING])
async def test_a_packing_spec_refuses_a_tracked_material(flag: str) -> None:
    material = _material(**{flag: True})
    with pytest.raises(BusinessRuleError, match=r"Item BOX-1 is \w+ tracked and cannot be used"):
        await service._validate_packing_spec_items(
            _SpecRepo(material),  # type: ignore[arg-type]
            uuid.uuid4(),
            material.id,
        )


async def test_a_packing_spec_accepts_an_untracked_material() -> None:
    material = _material()
    accepted = await service._validate_packing_spec_items(
        _SpecRepo(material),  # type: ignore[arg-type]
        uuid.uuid4(),
        material.id,
    )
    assert accepted is material
