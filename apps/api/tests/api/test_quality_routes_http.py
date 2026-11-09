"""Quality template and inspection write routes over HTTP.

The disposition arithmetic, the critical override and the owner callbacks are
pinned at service level in ``tests/integration/test_quality_service.py`` and its
siblings; ``test_route_authorization`` and ``test_route_contract_http`` cover the
anonymous, unentitled and unknown-id answers of every route. This file walks the
four handlers no test had executed through HTTP - amending a template, forking a
new version, recording measurements and closing an inspection - with real rows:
the status and envelope, the persisted effect, the audit entry, the 422 a
payload or the document's state earns, and the 404 another company's record
answers with.

The lab: a finished good with 100 pieces on hand and an item-scoped IN_PROCESS
check sheet (a critical NUMERIC bore of 9.9-10.1 mm and a BOOLEAN finish check),
plus a second item that no template covers - an inspection of that one has no
parameters, so it can be decided without readings. Everything is built through
the quality, inventory and documents services. Fixtures hand back plain ids: an
expected failure rolls its savepoint back and expires what was loaded before it.
"""

from __future__ import annotations

import io
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers, UploadFile

from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.schemas import DocumentUploadRequest
from src.modules.documents.service import DocumentService
from src.modules.inventory import service as inventory
from src.modules.inventory.models import TransactionType
from src.modules.products.models import ItemGroup, ProductionType
from src.modules.quality import service as quality
from src.modules.quality.models import (
    InspectionStatus,
    QcInspection,
    QcInspectionResult,
    QcStage,
    QcTemplate,
    QcTemplateParameter,
)
from src.modules.quality.schemas import (
    DecideRequest,
    InspectionCreate,
    TemplateCreate,
    TemplateParameterIn,
)
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "Quality HTTP coverage"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(48)


# -----------------------------------------------------------------------------
# Envelope and row helpers
# -----------------------------------------------------------------------------


def ok(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def refused(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code, body["error"]
    return body["error"]


def field_errors(response: Response) -> dict[str, list[str]]:
    return refused(response, 422, "VALIDATION_ERROR")["details"]["fields"]


async def audit_trail(
    session: AsyncSession, entity_type: str, entity_id: uuid.UUID
) -> list[AuditLog]:
    # (created_at, id): every row written in one transaction shares now().
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.scalars(stmt)).all())


def sheet() -> list[dict[str, Any]]:
    return [
        {
            "sequence": 1,
            "parameter_name": "Bore diameter",
            "parameter_type": "NUMERIC",
            "unit": "mm",
            "min_value": "9.9",
            "max_value": "10.1",
            "is_critical": True,
        },
        {"sequence": 2, "parameter_name": "Finish free of scratches", "parameter_type": "BOOLEAN"},
    ]


async def parameter_ids(session: AsyncSession, template_id: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = await session.execute(
        select(QcTemplateParameter.parameter_name, QcTemplateParameter.id).where(
            QcTemplateParameter.template_id == template_id
        )
    )
    return dict(rows.tuples().all())


# -----------------------------------------------------------------------------
# The lab
# -----------------------------------------------------------------------------


async def stocked_item(
    session: AsyncSession, tenant: TenantContext, store_id: uuid.UUID, code: str
):
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=code,
        base_uom_id=uom.id,
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.BUY,
        can_sell=True,
        can_scrap=True,
        standard_cost=Decimal("120"),
    )
    async with transaction(session):
        await inventory.post_movement(
            session,
            tenant,
            item_id=item.id,
            warehouse_id=store_id,
            quantity=Decimal("100"),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="Quality routes fixture stock",
            unit_cost=Decimal("120"),
        )
    return item.id


async def template_for(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, code: str
) -> uuid.UUID:
    template, _ = await quality.create_template(
        session,
        tenant,
        TemplateCreate(
            code=code,
            name="Lever handle in-process check",
            item_id=item_id,
            qc_stage=QcStage.IN_PROCESS,
            parameters=[TemplateParameterIn(**row) for row in sheet()],
        ),
    )
    return template.id


async def raise_inspection(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, store_id: uuid.UUID
) -> uuid.UUID:
    inspection = await quality.create_inspection(
        session,
        tenant,
        InspectionCreate(
            item_id=item_id,
            warehouse_id=store_id,
            qc_stage=QcStage.IN_PROCESS,
            inspected_quantity=Decimal("100"),
            hold_stock=True,
            reason="Supervisor pulled the lot",
        ),
    )
    return inspection.id


async def build_lab(session: AsyncSession, tenant: TenantContext, unique_code) -> SimpleNamespace:
    store = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=unique_code("QCW")
    )
    checked = await stocked_item(session, tenant, store.id, unique_code("FG"))
    plain = await stocked_item(session, tenant, store.id, unique_code("FGP"))
    return SimpleNamespace(
        store_id=store.id,
        item_id=checked,
        plain_item_id=plain,
        template_id=await template_for(session, tenant, checked, unique_code("QCT")),
    )


@pytest.fixture
async def lab(session, tenant, unique_code) -> SimpleNamespace:
    return await build_lab(session, tenant, unique_code)


@pytest.fixture
async def foreign_lab(session, other_company, unique_code) -> SimpleNamespace:
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    tenant = TenantContext(company_id=other_company.id, user_id=owner.id)
    world = await build_lab(session, tenant, unique_code)
    world.inspection_id = await raise_inspection(session, tenant, world.item_id, world.store_id)
    return world


async def checked_inspection(session, tenant, lab) -> uuid.UUID:
    """A PENDING inspection that resolved the lab's check sheet."""
    return await raise_inspection(session, tenant, lab.item_id, lab.store_id)


async def plain_inspection(session, tenant, lab) -> uuid.UUID:
    """A PENDING inspection with no check sheet: decidable without readings."""
    return await raise_inspection(session, tenant, lab.plain_item_id, lab.store_id)


async def decide(
    session: AsyncSession, tenant: TenantContext, inspection_id: uuid.UUID, **quantities: str
) -> None:
    await quality.decide_inspection(
        session,
        tenant,
        inspection_id,
        DecideRequest(
            accepted_quantity=Decimal(quantities.get("accepted", "100")),
            rework_quantity=Decimal(quantities.get("rework", "0")),
            reason="Lot verdict",
        ),
    )


def readings(ids: dict[str, uuid.UUID], *, bore: str = "10.0", finish: bool = True) -> dict:
    return {
        "measurements": [
            {"parameter_id": str(ids["Bore diameter"]), "measured_value_numeric": bore},
            {"parameter_id": str(ids["Finish free of scratches"]), "measured_value_bool": finish},
        ]
    }


# -----------------------------------------------------------------------------
# PATCH /quality/templates/{template_id}
# -----------------------------------------------------------------------------


async def test_an_unused_template_is_amended_in_place_and_audited(api_client, session, lab):
    body = {
        "name": "Lever handle check, rev B",
        "sampling_plan": "FIXED_SAMPLE",
        "sample_size": 5,
        "parameters": [
            *sheet(),
            {"sequence": 3, "parameter_name": "Plating colour", "parameter_type": "TEXT"},
        ],
    }

    data = ok(await api_client.patch(f"/quality/templates/{lab.template_id}", json=body))

    assert (data["name"], data["version"], data["sampling_plan"], data["sample_size"]) == (
        "Lever handle check, rev B",
        1,
        "FIXED_SAMPLE",
        5,
    )
    assert [row["parameter_name"] for row in data["parameters"]] == [
        "Bore diameter",
        "Finish free of scratches",
        "Plating colour",
    ]
    assert set(await parameter_ids(session, lab.template_id)) == {
        "Bore diameter",
        "Finish free of scratches",
        "Plating colour",
    }
    [updated] = [
        row
        for row in await audit_trail(session, quality.AUDIT_QC_TEMPLATE, lab.template_id)
        if row.action == AuditAction.UPDATE
    ]
    assert updated.old_values["name"] == "Lever handle in-process check"
    assert updated.new_values["name"] == "Lever handle check, rev B"


async def test_a_template_an_inspection_applied_keeps_its_specification_frozen(
    api_client, session, tenant, lab
):
    await checked_inspection(session, tenant, lab)
    url = f"/quality/templates/{lab.template_id}"

    error = refused(
        await api_client.patch(url, json={"sampling_plan": "AQL"}), 422, "REFERENCED_RECORD_IN_USE"
    )
    assert error["details"]["inspection_count"] == 1
    renamed = ok(await api_client.patch(url, json={"name": "Renamed for the floor"}))
    assert (renamed["name"], renamed["sampling_plan"]) == ("Renamed for the floor", "ALL")


async def test_a_sampling_plan_without_its_number_is_refused(api_client, session, lab):
    refused(
        await api_client.patch(
            f"/quality/templates/{lab.template_id}", json={"sampling_plan": "PERCENTAGE"}
        ),
        422,
        "BUSINESS_RULE_VIOLATION",
    )
    stored = await session.scalar(
        select(QcTemplate.sampling_plan).where(QcTemplate.id == lab.template_id)
    )
    assert stored == "ALL"


async def test_moving_a_template_onto_a_scope_another_one_owns_names_the_owner(
    api_client, session, tenant, lab, unique_code
):
    rival_id = await template_for(session, tenant, lab.plain_item_id, unique_code("QCR"))

    error = refused(
        await api_client.patch(
            f"/quality/templates/{lab.template_id}", json={"item_id": str(lab.plain_item_id)}
        ),
        409,
        "DUPLICATE_DOCUMENT",
    )
    assert error["details"]["template_id"] == str(rival_id)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "_root"),
        ({"parameters": []}, "_root"),
        ({"sample_size": 0}, "sample_size"),
        ({"code": "RENAMED"}, "code"),
        (
            {
                "parameters": [
                    {
                        "sequence": 1,
                        "parameter_name": "Gap",
                        "parameter_type": "NUMERIC",
                        "min_value": "5",
                        "max_value": "1",
                    }
                ]
            },
            "parameters.0",
        ),
    ],
    ids=["nothing", "empty-sheet", "zero-sample", "unknown-field", "min-above-max"],
)
async def test_a_malformed_amendment_is_a_field_error(api_client, session, lab, body, field):
    fields = field_errors(
        await api_client.patch(f"/quality/templates/{lab.template_id}", json=body)
    )

    assert field in fields, fields
    assert len(await parameter_ids(session, lab.template_id)) == 2


# -----------------------------------------------------------------------------
# POST /quality/templates/{template_id}/versions
# -----------------------------------------------------------------------------


async def test_a_new_version_copies_the_sheet_and_retires_the_one_it_replaces(
    api_client, session, lab
):
    data = ok(
        await api_client.post(
            f"/quality/templates/{lab.template_id}/versions",
            json={"reason": "Tolerance review after field returns"},
        ),
        201,
    )

    new_id = uuid.UUID(data["id"])
    assert new_id != lab.template_id
    assert (data["version"], data["is_active"]) == (2, True)
    assert [row["parameter_name"] for row in data["parameters"]] == [
        "Bore diameter",
        "Finish free of scratches",
    ]
    assert set(await parameter_ids(session, new_id)) == {
        "Bore diameter",
        "Finish free of scratches",
    }
    previous = ok(await api_client.get(f"/quality/templates/{lab.template_id}"))
    assert (previous["version"], previous["is_active"]) == (1, False)

    [created] = await audit_trail(session, quality.AUDIT_QC_TEMPLATE, new_id)
    assert created.action == AuditAction.CREATE
    assert created.context == {
        "superseded_template_id": str(lab.template_id),
        "reason": "Tolerance review after field returns",
    }


async def test_a_new_version_may_replace_the_sheet_and_the_plan(api_client, session, lab):
    data = ok(
        await api_client.post(
            f"/quality/templates/{lab.template_id}/versions",
            json={
                "reason": "Sampling agreed with the customer",
                "name": "Lever handle check, sampled",
                "sampling_plan": "PERCENTAGE",
                "sample_percentage": "10",
                "parameters": [sheet()[0]],
            },
        ),
        201,
    )

    assert (data["name"], data["sampling_plan"]) == ("Lever handle check, sampled", "PERCENTAGE")
    assert Decimal(data["sample_percentage"]) == Decimal("10")
    assert [row["parameter_name"] for row in data["parameters"]] == ["Bore diameter"]


async def test_a_version_can_follow_a_template_that_is_no_longer_selected(api_client, session, lab):
    ok(await api_client.patch(f"/quality/templates/{lab.template_id}", json={"is_active": False}))

    data = ok(
        await api_client.post(
            f"/quality/templates/{lab.template_id}/versions",
            json={"reason": "Reinstated for the new line", "retire_previous": False},
        ),
        201,
    )

    assert (data["version"], data["is_active"]) == (2, True)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: create_template_version flushes the new active row before "
        "_assert_scope_is_free, so the partial unique index answers with no owning template"
    ),
)
async def test_keeping_the_previous_version_live_names_the_template_owning_the_scope(
    api_client, session, lab
):
    error = refused(
        await api_client.post(
            f"/quality/templates/{lab.template_id}/versions",
            json={"reason": "Parallel run during changeover", "retire_previous": False},
        ),
        409,
        "DUPLICATE_DOCUMENT",
    )
    assert (error.get("details") or {}).get("template_id") == str(lab.template_id), error


async def test_a_versioned_percentage_plan_needs_its_percentage(api_client, session, lab):
    template_code = await session.scalar(
        select(QcTemplate.code).where(QcTemplate.id == lab.template_id)
    )

    refused(
        await api_client.post(
            f"/quality/templates/{lab.template_id}/versions",
            json={"reason": "Sampling agreed", "sampling_plan": "PERCENTAGE"},
        ),
        422,
        "BUSINESS_RULE_VIOLATION",
    )
    versions = await session.scalar(
        select(func.count()).select_from(QcTemplate).where(QcTemplate.code == template_code)
    )
    assert versions == 1


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "reason"),
        ({"reason": "ok"}, "reason"),
        ({"reason": "Empty sheet", "parameters": []}, "parameters"),
        ({"reason": "Moved", "qc_stage": "FINAL"}, "qc_stage"),
    ],
    ids=["no-reason", "short-reason", "empty-sheet", "scope-is-not-versioned"],
)
async def test_a_malformed_version_request_is_a_field_error(api_client, lab, body, field):
    fields = field_errors(
        await api_client.post(f"/quality/templates/{lab.template_id}/versions", json=body)
    )

    assert field in fields, fields


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: TemplateUpdate and TemplateVersionRequest skip TemplateCreate's repeated "
        "sequence/name checks (a repeated name is stored; a repeated sequence is a 409)"
    ),
)
@pytest.mark.parametrize("route", ["amend", "version"])
@pytest.mark.parametrize("repeated", ["parameter_name", "sequence"])
async def test_a_sheet_the_create_refuses_is_refused_by_amend_and_version(
    api_client, session, lab, route, repeated
):
    rows = sheet()
    rows[1][repeated] = rows[0][repeated]
    if route == "amend":
        response = await api_client.patch(
            f"/quality/templates/{lab.template_id}", json={"parameters": rows}
        )
    else:
        response = await api_client.post(
            f"/quality/templates/{lab.template_id}/versions",
            json={"reason": "Re-sequenced sheet", "parameters": rows},
        )

    assert field_errors(response)


# -----------------------------------------------------------------------------
# POST /quality/inspections/{inspection_id}/results
# -----------------------------------------------------------------------------


async def test_readings_are_judged_on_the_server_and_start_the_inspection(
    api_client, session, tenant, lab
):
    inspection_id = await checked_inspection(session, tenant, lab)
    ids = await parameter_ids(session, lab.template_id)

    data = ok(
        await api_client.post(
            f"/quality/inspections/{inspection_id}/results",
            json={**readings(ids), "remarks": "Measured on the bench gauge"},
        )
    )

    assert data["inspection"]["status"] == "IN_PROGRESS"
    assert data["inspection"]["remarks"] == "Measured on the bench gauge"
    assert {row["is_within_spec"] for row in data["inspection"]["results"]} == {True}
    evaluation = data["evaluation"]
    assert (evaluation["recommended_result"], evaluation["is_conclusive"]) == ("ACCEPTED", True)
    assert (evaluation["measured_count"], evaluation["in_spec_count"]) == (2, 2)
    stored = await session.scalar(
        select(func.count())
        .select_from(QcInspectionResult)
        .where(QcInspectionResult.inspection_id == inspection_id)
    )
    assert stored == 2

    trail = await audit_trail(session, quality.AUDIT_QC_INSPECTION, inspection_id)
    assert [
        (row.old_values or {}).get("status")
        for row in trail
        if row.action == AuditAction.STATUS_CHANGE
    ] == ["PENDING"]
    [recorded] = [row for row in trail if row.action == AuditAction.UPDATE]
    assert sorted(recorded.new_values["parameters"]) == [
        "Bore diameter",
        "Finish free of scratches",
    ]


async def test_a_critical_reading_out_of_spec_settles_the_verdict_until_corrected(
    api_client, session, tenant, lab
):
    inspection_id = await checked_inspection(session, tenant, lab)
    ids = await parameter_ids(session, lab.template_id)
    url = f"/quality/inspections/{inspection_id}/results"

    failed = ok(await api_client.post(url, json=readings(ids, bore="10.5")))
    assert failed["evaluation"]["recommended_result"] == "REJECTED"
    assert failed["evaluation"]["critical_failures"] == ["Bore diameter"]

    corrected = ok(
        await api_client.post(
            url,
            json={
                "measurements": [
                    {"parameter_id": str(ids["Bore diameter"]), "measured_value_numeric": "10.05"}
                ]
            },
        )
    )
    assert corrected["evaluation"]["recommended_result"] == "ACCEPTED"
    bore = (
        await session.execute(
            select(
                QcInspectionResult.measured_value_numeric, QcInspectionResult.is_within_spec
            ).where(
                QcInspectionResult.inspection_id == inspection_id,
                QcInspectionResult.parameter_id == ids["Bore diameter"],
            )
        )
    ).all()
    assert [(Decimal(value), within) for value, within in bore] == [(Decimal("10.05"), True)]


@pytest.mark.parametrize(
    "case", ["foreign-parameter", "text-for-numeric", "two-values", "repeated-parameter", "empty"]
)
async def test_readings_that_do_not_fit_the_sheet_are_refused(
    api_client, session, tenant, lab, case
):
    inspection_id = await checked_inspection(session, tenant, lab)
    ids = await parameter_ids(session, lab.template_id)
    bore = str(ids["Bore diameter"])
    bodies: dict[str, list[dict[str, Any]]] = {
        "foreign-parameter": [{"parameter_id": str(uuid.uuid4()), "measured_value_bool": True}],
        "text-for-numeric": [{"parameter_id": bore, "measured_value_text": "10.0"}],
        "two-values": [
            {"parameter_id": bore, "measured_value_numeric": "10", "measured_value_text": "10"}
        ],
        "repeated-parameter": [
            {"parameter_id": bore, "measured_value_numeric": "10"},
            {"parameter_id": bore, "measured_value_numeric": "10.01"},
        ],
        "empty": [],
    }

    error = refused(
        await api_client.post(
            f"/quality/inspections/{inspection_id}/results", json={"measurements": bodies[case]}
        ),
        422,
        "VALIDATION_ERROR",
    )

    if case == "foreign-parameter":
        assert error["details"]["unknown_parameter_ids"] == [bodies[case][0]["parameter_id"]]
    elif case == "text-for-numeric":
        assert error["details"]["parameter_id"] == bore
    else:
        assert error["details"]["fields"]
    status = await session.scalar(
        select(QcInspection.status).where(QcInspection.id == inspection_id)
    )
    assert status == InspectionStatus.PENDING


async def uploaded_photo(session: AsyncSession, owner: TenantContext) -> uuid.UUID:
    """A small PNG filed by ``owner``'s company. Patch ``settings.storage_path`` first."""
    upload = UploadFile(
        file=io.BytesIO(PNG),
        size=len(PNG),
        filename="bore.png",
        headers=Headers({"content-type": "image/png"}),
    )
    document = await DocumentService(session, owner).upload(
        upload, DocumentUploadRequest(category=DocumentCategory.ATTACHMENT)
    )
    return document.id


@pytest.mark.parametrize("photo", ["another-companys-document", "no-such-document"])
async def test_a_reading_cannot_attach_a_photo_this_company_does_not_hold(
    api_client, session, tenant, other_company, lab, tmp_path, monkeypatch, photo
):
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    theirs = await uploaded_photo(
        session, TenantContext(company_id=other_company.id, user_id=owner.id)
    )
    photo_id = theirs if photo == "another-companys-document" else uuid.uuid4()
    inspection_id = await checked_inspection(session, tenant, lab)
    ids = await parameter_ids(session, lab.template_id)
    body = readings(ids)
    body["measurements"][0]["photo_document_id"] = str(photo_id)

    response = await api_client.post(f"/quality/inspections/{inspection_id}/results", json=body)

    refused(response, 404, "NOT_FOUND")
    stored = await session.scalar(
        select(func.count())
        .select_from(QcInspectionResult)
        .where(QcInspectionResult.inspection_id == inspection_id)
    )
    assert stored == 0


async def test_a_reading_keeps_a_photo_this_company_holds_through_a_correction(
    api_client, session, tenant, lab, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    photo_id = await uploaded_photo(session, tenant)
    inspection_id = await checked_inspection(session, tenant, lab)
    ids = await parameter_ids(session, lab.template_id)
    url = f"/quality/inspections/{inspection_id}/results"
    body = readings(ids)
    body["measurements"][0]["photo_document_id"] = str(photo_id)
    stored_photo = (
        select(QcInspectionResult.photo_document_id)
        .where(
            QcInspectionResult.inspection_id == inspection_id,
            QcInspectionResult.parameter_id == ids["Bore diameter"],
        )
        .scalar_subquery()
    )

    ok(await api_client.post(url, json=body))
    assert await session.scalar(select(stored_photo)) == photo_id

    # The file lapses; correcting the value it evidences must not lose the reading.
    await session.execute(
        update(Document)
        .where(Document.id == photo_id)
        .values(expires_at=utcnow() - timedelta(days=1))
    )
    body["measurements"][0]["measured_value_numeric"] = "10.02"
    ok(await api_client.post(url, json=body))
    assert await session.scalar(select(stored_photo)) == photo_id


async def test_an_inspection_raised_without_a_sheet_has_nothing_to_measure(
    api_client, session, tenant, lab
):
    inspection_id = await plain_inspection(session, tenant, lab)

    refused(
        await api_client.post(
            f"/quality/inspections/{inspection_id}/results",
            json={
                "measurements": [{"parameter_id": str(uuid.uuid4()), "measured_value_bool": True}]
            },
        ),
        422,
        "BUSINESS_RULE_VIOLATION",
    )


async def test_readings_behind_a_decision_can_no_longer_change(api_client, session, tenant, lab):
    inspection_id = await checked_inspection(session, tenant, lab)
    ids = await parameter_ids(session, lab.template_id)
    url = f"/quality/inspections/{inspection_id}/results"
    ok(await api_client.post(url, json=readings(ids)))
    await decide(session, tenant, inspection_id)

    error = refused(
        await api_client.post(url, json=readings(ids, bore="10.5")), 422, "INVALID_STATE_TRANSITION"
    )
    assert error["details"]["status"] == "ACCEPTED"


# -----------------------------------------------------------------------------
# POST /quality/inspections/{inspection_id}/close
# -----------------------------------------------------------------------------


async def test_a_decided_inspection_with_nothing_held_is_closed(api_client, session, tenant, lab):
    inspection_id = await plain_inspection(session, tenant, lab)
    await decide(session, tenant, inspection_id)

    data = ok(
        await api_client.post(
            f"/quality/inspections/{inspection_id}/close", json={"reason": "Lot released to stores"}
        )
    )

    assert (data["status"], data["result"]) == ("CLOSED", "ACCEPTED")
    stored = await session.get(QcInspection, inspection_id)
    assert stored is not None and stored.closed_at is not None
    closing = [
        row
        for row in await audit_trail(session, quality.AUDIT_QC_INSPECTION, inspection_id)
        if (row.new_values or {}).get("status") == "CLOSED"
    ]
    assert [(row.action, row.old_values["status"], row.context["reason"]) for row in closing] == [
        (AuditAction.QC_DECISION, "ACCEPTED", "Lot released to stores")
    ]


async def test_an_inspection_still_holding_rework_cannot_be_closed(
    api_client, session, tenant, lab, decimal_equal
):
    inspection_id = await plain_inspection(session, tenant, lab)
    await decide(session, tenant, inspection_id, accepted="60", rework="40")

    error = refused(
        await api_client.post(
            f"/quality/inspections/{inspection_id}/close", json={"reason": REASON}
        ),
        422,
        "BUSINESS_RULE_VIOLATION",
    )
    decimal_equal(error["details"]["still_held_quantity"], "40")
    stored = await session.get(QcInspection, inspection_id)
    assert stored is not None and stored.closed_at is None


async def test_a_closed_inspection_cannot_be_closed_again(api_client, session, tenant, lab):
    inspection_id = await plain_inspection(session, tenant, lab)
    await decide(session, tenant, inspection_id)
    url = f"/quality/inspections/{inspection_id}/close"
    ok(await api_client.post(url, json={"reason": REASON}))

    refused(await api_client.post(url, json={"reason": REASON}), 422, "INVALID_STATE_TRANSITION")


async def test_closing_needs_a_reason(api_client, session, tenant, lab):
    inspection_id = await plain_inspection(session, tenant, lab)
    await decide(session, tenant, inspection_id)

    assert "reason" in field_errors(
        await api_client.post(f"/quality/inspections/{inspection_id}/close", json={})
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: quality._close validates the disposition before the state machine, so "
        "closing an undecided inspection is a quantity complaint, not INVALID_STATE_TRANSITION"
    ),
)
async def test_an_undecided_inspection_cannot_be_closed(api_client, session, tenant, lab):
    inspection_id = await plain_inspection(session, tenant, lab)

    refused(
        await api_client.post(
            f"/quality/inspections/{inspection_id}/close", json={"reason": REASON}
        ),
        422,
        "INVALID_STATE_TRANSITION",
    )


# -----------------------------------------------------------------------------
# Tenant isolation
# -----------------------------------------------------------------------------


async def test_another_companys_templates_and_inspections_are_not_found(
    api_client, session, lab, foreign_lab
):
    template = f"/quality/templates/{foreign_lab.template_id}"
    inspection = f"/quality/inspections/{foreign_lab.inspection_id}"

    refused(await api_client.patch(template, json={"name": "Hijacked"}), 404, "NOT_FOUND")
    refused(
        await api_client.post(f"{template}/versions", json={"reason": "Not ours"}), 404, "NOT_FOUND"
    )
    refused(
        await api_client.post(
            f"{inspection}/results",
            json={
                "measurements": [{"parameter_id": str(uuid.uuid4()), "measured_value_bool": True}]
            },
        ),
        404,
        "NOT_FOUND",
    )
    refused(await api_client.post(f"{inspection}/close", json={"reason": REASON}), 404, "NOT_FOUND")

    stored = await session.get(QcTemplate, foreign_lab.template_id)
    assert stored is not None and (stored.name, stored.version) == (
        "Lever handle in-process check",
        1,
    )
    status = await session.scalar(
        select(QcInspection.status).where(QcInspection.id == foreign_lab.inspection_id)
    )
    assert status == InspectionStatus.PENDING
