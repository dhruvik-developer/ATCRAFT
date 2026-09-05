"""The ready-for-packing feed is readable by either side of the hand-over.

``require_permission`` defaults to ``mode="all"``. Declared with two rights,
the gate demanded both - and no preset role holds ``packing.view`` (a dispatch
right) together with ``assembly_order.view`` (a production right), so the
Dispatch Manager this feed exists for was answered 403 while the companion
``/pack`` endpoint needed ``packing.manage`` alone. The gate is "any"; these
tests pin it, and pin that a shipped preset can actually read the feed.

No database: the dependency is pulled straight off the route and called with a
hand-built principal.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from fastapi.routing import APIRoute

from src.core.deps import Principal
from src.core.errors import PermissionDeniedError
from src.core.permissions import ROLE_PRESETS, Permission
from src.modules.assembly.router import router

pytestmark = pytest.mark.unit

FEED_PATH = "/assembly/ready-for-packing"


def _principal(*permissions: Permission | str) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="packer@test.local",
        full_name="Packer",
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
    )


def _gate() -> Callable[[Principal], Awaitable[Principal]]:
    route = next(
        r
        for r in router.routes
        if isinstance(r, APIRoute) and r.path == FEED_PATH and "GET" in r.methods
    )
    [dependency] = route.dependencies
    return cast(Callable[[Principal], Awaitable[Principal]], dependency.dependency)


@pytest.mark.parametrize(
    "held",
    [
        pytest.param((Permission.PACKING_VIEW,), id="packer"),
        pytest.param((Permission.ASSEMBLY_ORDER_VIEW,), id="assembly-planner"),
        pytest.param((Permission.PACKING_VIEW, Permission.ASSEMBLY_ORDER_VIEW), id="both"),
    ],
)
async def test_either_right_alone_reads_the_feed(held: tuple[Permission, ...]) -> None:
    principal = _principal(*held)
    assert await _gate()(principal) is principal


async def test_a_principal_holding_neither_right_is_refused() -> None:
    with pytest.raises(PermissionDeniedError) as exc:
        await _gate()(_principal(Permission.PACKING_MANAGE))

    required = exc.value.details["required_permission"]
    assert str(Permission.PACKING_VIEW) in required
    assert str(Permission.ASSEMBLY_ORDER_VIEW) in required


async def test_the_dispatch_manager_preset_reads_the_feed() -> None:
    """The role the feed was written for, exactly as it ships.

    A future rearrangement of the presets that takes ``packing.view`` off the
    dispatch role, or an "all" gate creeping back, fails here rather than in a
    packer's browser.
    """
    permissions = cast(frozenset[Permission], ROLE_PRESETS["DISPATCH_MANAGER"]["permissions"])
    assert Permission.PACKING_VIEW in permissions
    assert Permission.ASSEMBLY_ORDER_VIEW not in permissions

    principal = _principal(*permissions)
    assert await _gate()(principal) is principal
