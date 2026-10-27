"""The duplicate discovery layer preserves physical grain and complete PR roots."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from src.core.deps import TenantContext
from src.core.errors import AuthorizationError, BusinessRuleError, NotFoundError, ValidationError
from src.modules.procurement import duplicate_search as search
from src.modules.procurement.duplicate_search_repository import (
    DuplicateSearchRepository,
    OpenDuplicateLine,
    RequisitionDemandFacts,
)
from src.modules.procurement.models import PurchaseOrderStatus, PurchaseSourceType
from src.modules.procurement.repository import DUPLICATE_SCAN_LIMIT
from src.modules.procurement.service_rules import DuplicateAction, DuplicateConfidence, ProposedLine

pytestmark = pytest.mark.unit
DAY = date(2026, 10, 27)
VENDOR, OTHER_VENDOR, WAREHOUSE, ITEM, OTHER_ITEM, ROOT, LEAF = (uuid.uuid4() for _ in range(7))
PATH = f"/{ROOT}/{LEAF}/"


def _ref(*, header_id=None, line_id=None):
    return {
        "demand_type": "SALES_ORDER",
        "demand_id": str(header_id or uuid.uuid4()),
        "demand_line_id": str(line_id) if line_id else None,
    }


def _row(**overrides):
    values = {
        "purchase_order_id": uuid.uuid4(),
        "purchase_order_code": "PO-TEST-001",
        "purchase_order_version": 7,
        "purchase_order_line_id": uuid.uuid4(),
        "status": PurchaseOrderStatus.DRAFT,
        "vendor_id": VENDOR,
        "warehouse_id": WAREHOUSE,
        "item_id": ITEM,
        "category_path": PATH,
        "quantity": Decimal("16"),
        "received_quantity": Decimal("0"),
        "required_date": DAY,
        "source_type": PurchaseSourceType.MANUAL,
        "source_id": None,
        "has_receipt_history": False,
    }
    values.update(overrides)
    return OpenDuplicateLine(**values)


@pytest.fixture
def tenant():
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


@pytest.fixture
def repository(monkeypatch):
    repo = SimpleNamespace(
        item_lineages=AsyncMock(return_value={ITEM: PATH, OTHER_ITEM: PATH}),
        vendor_categories=AsyncMock(return_value={VENDOR: frozenset({ROOT})}),
        requisition_demands=AsyncMock(return_value={}),
        open_lines=AsyncMock(return_value=[]),
        candidate_sources=AsyncMock(return_value={}),
    )
    monkeypatch.setattr(search, "DuplicateSearchRepository", lambda *_: repo)
    return repo


async def _find(tenant, lines, **kwargs):
    return await search.find_duplicates_for_lines(
        None,
        tenant,
        vendor_id=VENDOR,
        warehouse_id=WAREHOUSE,
        lines=lines,
        source_type=None,
        source_id=None,
        **kwargs,
    )


async def test_category_signal_uses_vendor_qualified_ancestor_not_client_category(
    repository, tenant
):
    repository.open_lines.return_value = [_row(item_id=OTHER_ITEM)]
    matches = await _find(tenant, [ProposedLine(ITEM, DAY, item_category_id=uuid.uuid4())])
    assert matches[0].confidence is DuplicateConfidence.POSSIBLE
    assert matches[0].item_category_id == ROOT
    assert DuplicateAction.INCREASE_EXISTING_QUANTITY not in matches[0].available_actions
    assert repository.open_lines.await_args.kwargs["category_ids"] == [ROOT]


async def test_active_allocation_roots_are_exact_across_different_requisitions_and_dates(
    repository, tenant
):
    incoming_source, candidate_source = uuid.uuid4(), uuid.uuid4()
    ref = _ref(line_id=uuid.uuid4())
    row = _row(required_date=DAY + timedelta(days=40))
    repository.requisition_demands.return_value = {
        incoming_source: RequisitionDemandFacts(ITEM, (ref,))
    }
    repository.open_lines.return_value = [row]
    repository.candidate_sources.return_value = {
        row.purchase_order_line_id: [(candidate_source, (ref,))]
    }
    matches = await _find(tenant, [ProposedLine(ITEM, DAY, requisition_line_id=incoming_source)])
    assert matches[0].confidence is DuplicateConfidence.EXACT
    assert "Both cover the same sales order / BOM demand" in matches[0].match_reasons
    assert repository.open_lines.await_args.kwargs["demand_refs"] == [ref]


async def test_complete_fifty_reference_pr_keeps_last_identity(repository, tenant):
    source_id = uuid.uuid4()
    refs = tuple(_ref(line_id=uuid.uuid4()) for _ in range(50))
    row = _row(required_date=DAY + timedelta(days=40))
    repository.requisition_demands.return_value = {source_id: RequisitionDemandFacts(ITEM, refs)}
    repository.open_lines.return_value = [row]
    repository.candidate_sources.return_value = {
        row.purchase_order_line_id: [(uuid.uuid4(), (refs[-1],))]
    }
    matches = await _find(tenant, [ProposedLine(ITEM, DAY, requisition_line_id=source_id)])
    assert matches[0].confidence is DuplicateConfidence.EXACT
    assert len(repository.open_lines.await_args.kwargs["demand_refs"]) == 50


async def test_pooled_sources_do_not_multiply_order_quantity_or_candidates(repository, tenant):
    source_id, other_source = uuid.uuid4(), uuid.uuid4()
    ref = _ref()
    row = _row()
    repository.requisition_demands.return_value = {source_id: RequisitionDemandFacts(ITEM, (ref,))}
    repository.open_lines.return_value = [row]
    repository.candidate_sources.return_value = {
        row.purchase_order_line_id: [(source_id, (ref,)), (other_source, (ref,))]
    }
    matches = await _find(
        tenant, [ProposedLine(ITEM, DAY, requisition_line_id=source_id), ProposedLine(ITEM, DAY)]
    )
    assert len(matches) == 1
    assert matches[0].ordered_quantity == Decimal("16")
    assert matches[0].confidence is DuplicateConfidence.EXACT


async def test_receipt_history_and_current_version_reach_action_predicates(repository, tenant):
    repository.open_lines.return_value = [
        _row(status=PurchaseOrderStatus.SENT, has_receipt_history=True)
    ]
    matches = await _find(tenant, [ProposedLine(ITEM, DAY)])
    assert matches[0].purchase_order_version == 7
    assert matches[0].has_receipt_history is True
    assert DuplicateAction.MERGE_WITH_EXISTING not in matches[0].available_actions
    assert DuplicateAction.INCREASE_EXISTING_QUANTITY not in matches[0].available_actions


async def test_unknown_proposed_date_does_not_narrow_other_candidate_dates(repository, tenant):
    repository.open_lines.return_value = [_row(required_date=DAY + timedelta(days=100))]
    matches = await _find(tenant, [ProposedLine(ITEM, DAY), ProposedLine(ITEM, None)])
    assert matches
    arguments = repository.open_lines.await_args.kwargs
    assert arguments["unrestricted_dates"] is True
    assert arguments["date_from"] is None
    assert arguments["date_to"] is None


async def test_later_same_item_signal_replaces_first_weaker_category_match(repository, tenant):
    repository.open_lines.return_value = [_row(item_id=OTHER_ITEM)]
    matches = await _find(tenant, [ProposedLine(ITEM, DAY), ProposedLine(OTHER_ITEM, DAY)])
    assert len(matches) == 1
    assert matches[0].confidence is DuplicateConfidence.STRONG
    assert DuplicateAction.INCREASE_EXISTING_QUANTITY in matches[0].available_actions


async def test_inactive_sources_cannot_create_exact_match(repository, tenant):
    source_id = uuid.uuid4()
    ref = _ref()
    repository.requisition_demands.return_value = {source_id: RequisitionDemandFacts(ITEM, (ref,))}
    repository.open_lines.return_value = [_row(required_date=DAY + timedelta(days=40))]
    # The repository returns no roots when only inactive mapping rows exist.
    assert await _find(tenant, [ProposedLine(ITEM, DAY, requisition_line_id=source_id)]) == []


async def test_forged_or_other_tenant_pr_id_is_not_found(repository, tenant):
    with pytest.raises(NotFoundError):
        await _find(tenant, [ProposedLine(ITEM, DAY, requisition_line_id=uuid.uuid4())])
    repository.open_lines.assert_not_awaited()


async def test_requisition_item_mismatch_is_refused(repository, tenant):
    source_id = uuid.uuid4()
    repository.requisition_demands.return_value = {
        source_id: RequisitionDemandFacts(OTHER_ITEM, ())
    }
    with pytest.raises(ValidationError, match="match the item"):
        await _find(tenant, [ProposedLine(ITEM, DAY, requisition_line_id=source_id)])


async def test_missing_item_is_not_found_before_search(repository, tenant):
    with pytest.raises(NotFoundError):
        await _find(tenant, [ProposedLine(uuid.uuid4(), DAY)])


@pytest.mark.parametrize("binding", ["vendor_id", "customer_id"])
async def test_portal_context_cannot_use_internal_duplicate_search(repository, tenant, binding):
    with pytest.raises(AuthorizationError):
        await _find(
            dataclasses.replace(tenant, **{binding: uuid.uuid4()}), [ProposedLine(ITEM, DAY)]
        )
    repository.item_lineages.assert_not_awaited()


async def test_warehouse_assignment_is_checked_before_query(repository, tenant):
    with pytest.raises(AuthorizationError):
        await _find(
            dataclasses.replace(tenant, allowed_warehouse_ids=(uuid.uuid4(),)),
            [ProposedLine(ITEM, DAY)],
        )
    repository.item_lineages.assert_not_awaited()


async def test_empty_input_uses_no_queries(repository, tenant):
    assert await _find(tenant, []) == []
    repository.item_lineages.assert_not_awaited()


async def test_group_search_batches_facts_and_keeps_vendor_category_resolution(repository, tenant):
    repository.vendor_categories.return_value = {
        VENDOR: frozenset({ROOT}),
        OTHER_VENDOR: frozenset({LEAF}),
    }
    first, second = _row(), _row(vendor_id=OTHER_VENDOR)
    repository.open_lines.return_value = [first, second]
    groups = [
        search.DuplicateSearchGroup(VENDOR, WAREHOUSE, [ProposedLine(ITEM, DAY)]),
        search.DuplicateSearchGroup(OTHER_VENDOR, WAREHOUSE, [ProposedLine(ITEM, DAY)]),
    ]
    results = await search.find_duplicates_for_groups(None, tenant, groups)
    assert results[0][0].item_category_id == ROOT
    assert results[1][0].item_category_id == LEAF
    for method in vars(repository).values():
        method.assert_awaited_once()


async def test_exclusion_from_one_group_does_not_hide_candidate_from_another(repository, tenant):
    row = _row()
    repository.open_lines.return_value = [row]
    groups = [
        search.DuplicateSearchGroup(
            VENDOR, WAREHOUSE, [ProposedLine(ITEM, DAY)], exclude_order_id=row.purchase_order_id
        ),
        search.DuplicateSearchGroup(VENDOR, WAREHOUSE, [ProposedLine(ITEM, DAY)]),
    ]
    results = await search.find_duplicates_for_groups(None, tenant, groups)
    assert results[0] == []
    assert results[1][0].purchase_order_id == row.purchase_order_id
    assert repository.open_lines.await_args.kwargs["exclude_order_ids"] == []


def test_custom_window_controls_sql_envelope():
    assert search._date_envelope([ProposedLine(ITEM, DAY)], 14) == (
        DAY - timedelta(days=14),
        DAY + timedelta(days=14),
        False,
    )
    with pytest.raises(ValueError):
        search._date_envelope([ProposedLine(ITEM, DAY)], -1)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


def _query_arguments():
    return {
        "vendor_warehouses": [(VENDOR, WAREHOUSE)],
        "item_ids": [ITEM],
        "category_ids": [ROOT],
        "date_from": DAY - timedelta(days=7),
        "date_to": DAY + timedelta(days=7),
        "unrestricted_dates": False,
        "header_sources": [(PurchaseSourceType.PURCHASE_REQUISITION, uuid.uuid4())],
        "requisition_line_ids": [uuid.uuid4()],
        "demand_refs": [_ref()],
        "group_count": 1,
    }


async def test_candidate_query_compiles_tenant_scoped_indexable_root_and_receipt_predicates(tenant):
    session = SimpleNamespace(execute=AsyncMock(return_value=_Rows([])))
    await DuplicateSearchRepository(session, tenant).open_lines(**_query_arguments())
    statement = session.execute.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "source_demand_refs @> jsonb_build_array" in sql
    assert "jsonb_array_elements" in sql
    assert "purchase_order_source_allocations.is_active IS true" in sql
    assert "goods_receipts.company_id" in sql
    assert "purchase_orders.version" in sql
    assert DUPLICATE_SCAN_LIMIT + 1 in compiled.params.values()
    assert tenant.company_id in compiled.params.values()


async def test_candidate_query_refuses_overflow_instead_of_silently_truncating(tenant):
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_Rows([object()] * (DUPLICATE_SCAN_LIMIT + 1)))
    )
    with pytest.raises(BusinessRuleError, match="Too many open"):
        await DuplicateSearchRepository(session, tenant).open_lines(**_query_arguments())


async def test_source_query_uses_active_mappings_and_legacy_only_without_any_mapping(tenant):
    session = SimpleNamespace(execute=AsyncMock(return_value=_Rows([])))
    await DuplicateSearchRepository(session, tenant).candidate_sources([uuid.uuid4()])
    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "UNION ALL" in sql
    assert "purchase_order_source_allocations.is_active IS true" in sql
    assert "NOT (EXISTS" in sql
    assert "purchase_requisition_lines.company_id = purchase_order_lines.company_id" in sql
