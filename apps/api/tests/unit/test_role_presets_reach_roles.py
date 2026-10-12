"""A permission added to the enum must actually reach the roles that hold it.

This exists because adding one did not, and nothing caught it.

`roles.permissions` is a stored text array, written when a company is seeded.
`_seed_roles` used to skip any role that already existed, so a permission added
in a later release reached the enum, the preset, the API and the frontend
catalogue - and never reached a single deployed role. The screen it gated was
invisible to everyone, the superuser included, whose preset is literally
`frozenset(Permission)`.

Every existing test stayed green. `test_frontend_permission_catalogue.py`
compares the backend enum with the frontend constant and both were right; the
endpoint returned its rows; the tab simply was not there. It was found by
opening the page.

The two assertions below are the general form of that bug rather than the one
instance of it.
"""

from __future__ import annotations

import pytest

from src.core.permissions import PERMISSION_GROUPS, ROLE_PRESETS, Permission

pytestmark = pytest.mark.unit


def test_the_superuser_preset_holds_every_permission() -> None:
    """It is defined as every permission, so a new one is in it by construction.

    Asserted anyway: if this ever became an explicit list, a permission added
    later would silently not be in it, and the failure would look like a
    permissions bug on a screen rather than a stale list here.
    """
    superuser = ROLE_PRESETS["SUPER_ADMIN"]["permissions"]

    missing = sorted(str(p) for p in Permission if p not in superuser)

    assert missing == [], f"SUPER_ADMIN cannot use: {missing}"


def test_every_permission_belongs_to_a_named_group() -> None:
    """An ungrouped permission lands in "Other" on the role editor.

    Which is where a new one goes unnoticed: the administrator who needs to
    grant it cannot find it under the heading they would look for. Grouping is
    by resource - the part before the dot.
    """
    grouped = {resource for resources in PERMISSION_GROUPS.values() for resource in resources}

    ungrouped = sorted({str(p).split(".")[0] for p in Permission} - grouped)

    assert ungrouped == [], (
        "these resources have no PERMISSION_GROUPS entry and would appear under "
        f'"Other" in the role editor: {ungrouped}'
    )


def test_a_system_role_is_one_whose_permissions_the_client_does_not_own() -> None:
    """The boundary that decides what `_seed_roles` may rewrite.

    A system role's permission list is ours: the seeder refreshes it on every
    run so a new release's permissions arrive. A non-system role is the
    client's - `is_system` is false on the operational presets precisely so an
    administrator can tune what a planner or a storekeeper may do - and
    rewriting one from a preset would silently undo that.

    So the set of system roles must stay small and deliberate. This asserts what
    it currently is; changing it is a decision, not a detail.
    """
    system = sorted(code for code, spec in ROLE_PRESETS.items() if spec.get("is_system"))

    assert system == [
        "ADMIN",
        "SUPER_ADMIN",
        "VENDOR_ACCOUNT_MANAGER",
        "VENDOR_DISPATCH_MANAGER",
        "VENDOR_MANAGER",
        "VENDOR_OWNER",
        "VENDOR_QC_CONTACT",
    ]


def test_vendor_system_presets_have_only_their_portal_rights() -> None:
    base = {Permission.VENDOR_PORTAL_ACCESS, Permission.NOTIFICATION_VIEW}
    expected = {
        "VENDOR_OWNER": base
        | {
            Permission.VENDOR_PORTAL_RESPOND,
            Permission.VENDOR_PORTAL_SHIP,
            Permission.VENDOR_PORTAL_INVOICE,
            Permission.VENDOR_PORTAL_QUALITY,
        },
        "VENDOR_MANAGER": base | {Permission.VENDOR_PORTAL_RESPOND, Permission.VENDOR_PORTAL_SHIP},
        "VENDOR_DISPATCH_MANAGER": base | {Permission.VENDOR_PORTAL_SHIP},
        "VENDOR_QC_CONTACT": base | {Permission.VENDOR_PORTAL_QUALITY},
        "VENDOR_ACCOUNT_MANAGER": base | {Permission.VENDOR_PORTAL_INVOICE},
    }
    for code, permissions in expected.items():
        assert ROLE_PRESETS[code]["permissions"] == permissions
