"""Every figure the dashboard returns is withheld unless its permission is held.

``reports.service.dashboard`` nulls exactly the fields named in
``DASHBOARD_TILE_PERMISSIONS`` and hands every other column of its select to
``DashboardRead`` as it is. Before 713cb953 nine tiles - open sales orders to
overdue deliveries - had no entry in that map, so every holder of
``dashboard.view`` saw them; and the HTTP test that checks a bare reader gets
nothing read a list of tile names written out by hand, which the next tile would
not be on. So the map is held to the schema here: every figure but the time the
payload was generated names its permission, and every entry names a figure.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from pydantic import BaseModel, create_model

from src.core.permissions import Permission
from src.modules.reports.schemas import DashboardRead
from src.modules.reports.service import DASHBOARD_TILE_PERMISSIONS

pytestmark = pytest.mark.unit

#: What every reader of the dashboard gets: when the payload was built, not a figure.
UNGATED = frozenset({"generated_at"})


def gating_gaps(model: type[BaseModel], gates: Mapping[str, Permission]) -> dict[str, list[str]]:
    """The figures no permission withholds, and the gates that name no figure."""
    figures = set(model.model_fields) - UNGATED
    return {
        "figures without a permission": sorted(figures - set(gates)),
        "permissions without a figure": sorted(set(gates) - figures),
    }


def test_every_dashboard_figure_is_withheld_by_a_permission() -> None:
    gaps = gating_gaps(DashboardRead, DASHBOARD_TILE_PERMISSIONS)

    assert gaps == {"figures without a permission": [], "permissions without a figure": []}, (
        f"{gaps}\n\nName the permission that owns each tile's documents in "
        "src.modules.reports.service.DASHBOARD_TILE_PERMISSIONS; a tile missing from it "
        "is shown to everyone who can open the dashboard."
    )


def test_the_rule_finds_a_tile_shipped_without_a_permission() -> None:
    extended = create_model(
        "DashboardWithComplaints", __base__=DashboardRead, open_complaints=(int | None, None)
    )

    assert gating_gaps(extended, DASHBOARD_TILE_PERMISSIONS) == {
        "figures without a permission": ["open_complaints"],
        "permissions without a figure": [],
    }


def test_the_rule_finds_a_permission_left_behind_by_a_removed_tile() -> None:
    stale = {**DASHBOARD_TILE_PERMISSIONS, "open_complaints": Permission.QC_VIEW}

    assert gating_gaps(DashboardRead, stale) == {
        "figures without a permission": [],
        "permissions without a figure": ["open_complaints"],
    }
