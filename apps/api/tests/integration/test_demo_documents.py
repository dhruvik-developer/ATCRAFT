"""A repeated local seed must retain document identities and stored files."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.demo_data import DemoContext
from src.core.demo_data import documents as demo_documents
from src.core.deps import Principal, TenantContext
from src.modules.documents.models import Document, DocumentCategory

pytestmark = pytest.mark.integration


def stored_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


async def test_document_seed_reuses_the_document_identity_and_file(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    spec = demo_documents._FileSpec(
        key="LOCAL-NOTE",
        file_name="local-seed-note.pdf",
        mime_type="application/pdf",
        category=DocumentCategory.DOCUMENT,
        entity_kind="company",
        entity_code="LOCAL-COMPANY",
        entity_type="company",
        payload=demo_documents._pdf("Local seed", "Document identity regression."),
    )
    monkeypatch.setattr(demo_documents, "_specs", lambda ctx: (spec,))

    def context() -> DemoContext:
        ctx = DemoContext(session, tenant, principal, tenant.company_id, tenant.user_id)
        ctx.remember("company", "LOCAL-COMPANY", tenant.company_id)
        return ctx

    first = context()
    await demo_documents.seed(first)
    first_files = await asyncio.to_thread(stored_files, tmp_path)
    assert len(first_files) == 1
    document_id = first.id_of("document", spec.key)
    assert document_id != tenant.company_id

    second = context()
    await demo_documents.seed(second)
    assert second.id_of("document", spec.key) == document_id
    assert second.counts["documents"] == {"created": 0, "existing": 1}
    assert await asyncio.to_thread(stored_files, tmp_path) == first_files
    assert (
        await session.scalar(
            select(func.count(Document.id)).where(Document.company_id == tenant.company_id)
        )
        == 1
    )
