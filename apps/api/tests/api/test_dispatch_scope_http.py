"""What dispatch will carry, and what the packing hand-over will load, over HTTP.

Two defects are pinned at the boundary here. ``POST /dispatch`` accepted a
TRANSFER, JOB_WORK or PURCHASE_RETURN consignment whose release posted a plain
DISPATCH outflow - the goods left the books whatever the type said - and
``POST /packing/{id}/hand-over`` stored whatever dispatch id it was sent. The
service-level behaviour is covered in ``tests/integration/test_dispatch_service``;
this file checks the status, the envelope, the safe message and the permission
gate a caller actually sees.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from src.core.clock import today
from src.core.deps import Principal, get_current_principal
from src.core.permissions import Permission

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _principal_without(base: Principal, *missing: Permission) -> Principal:
    """The same person holding every right except ``missing``."""
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission if p not in missing),
        role_codes=base.role_codes,
    )


@pytest.mark.parametrize(
    ("dispatch_type", "owner"),
    [("TRANSFER", "Inventory"), ("JOB_WORK", "Job Work"), ("PURCHASE_RETURN", "Procurement")],
)
async def test_a_non_sales_consignment_is_refused_naming_the_module_that_owns_it(
    api_client: AsyncClient, dispatch_type: str, owner: str
) -> None:
    response = await api_client.post(
        "/dispatch",
        json={
            "dispatch_type": dispatch_type,
            "warehouse_id": str(uuid.uuid4()),
            "vendor_id": None if dispatch_type == "TRANSFER" else str(uuid.uuid4()),
            "dispatch_date": today().isoformat(),
            "lr_number": "LR-9001",
            "lines": [{"item_id": str(uuid.uuid4()), "quantity": "1"}],
        },
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "BUSINESS_RULE_VIOLATION"
    assert owner in body["error"]["message"]
    assert body["error"]["details"]["dispatch_type"] == dispatch_type
    assert "Traceback" not in response.text


async def test_a_hand_over_to_a_consignment_that_does_not_exist_is_not_found(
    api_client: AsyncClient,
) -> None:
    dispatch_id = uuid.uuid4()
    response = await api_client.post(
        f"/packing/{uuid.uuid4()}/hand-over",
        json={"dispatch_id": str(dispatch_id), "reason": "On the 4pm truck"},
    )

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"
    # The consignment is read first, under its own lock - it is what the
    # boxes are being loaded onto.
    assert error["message"].startswith("Dispatch not found")
    assert "Traceback" not in response.text


async def test_the_hand_over_needs_the_right_to_send_goods(
    api_client: AsyncClient, principal: Principal
) -> None:
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: _principal_without(
        principal, Permission.DISPATCH_CREATE
    )
    refused = await api_client.post(
        f"/packing/{uuid.uuid4()}/hand-over",
        json={"dispatch_id": str(uuid.uuid4()), "reason": "On the 4pm truck"},
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "PERMISSION_DENIED"
