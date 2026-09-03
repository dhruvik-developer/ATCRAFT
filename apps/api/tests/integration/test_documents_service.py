"""Service-level tests for the documents module, against a real PostgreSQL.

These cover what only the database and a real filesystem can prove: that the
bytes and the row are written together or not at all, that an oversized upload
leaves nothing behind, that another tenant's document is a 404 rather than a
403, and that deleting removes the file as well as the row.

``settings.storage_path`` is redirected to a per-test temporary directory, so
nothing here can touch the repository's real storage tree.

Fixtures (``session``, ``tenant``, ``company``, ``other_company``, ``user``)
come from ``tests/conftest.py``; the session's outer transaction is rolled back
after every test.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers, UploadFile

from src.core.clock import utcnow
from src.core.config import settings
from src.core.deps import TenantContext
from src.core.errors import ErrorCode, FileValidationError, NotFoundError
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.schemas import DocumentFilters, DocumentUploadRequest
from src.modules.documents.service import DocumentService, stream_file

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.7\n" + b"payload bytes " * 100 + b"\n%%EOF\n"
CSV = b"code,name\nRM-001,Steel rod\n"
WINDOWS_EXECUTABLE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 512


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    """Redirect the storage tree at a throwaway directory."""
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_path", root)
    return root


def stored_files(root) -> list:
    return sorted(p for p in root.rglob("*") if p.is_file())


def upload_file(
    content: bytes,
    *,
    file_name: str = "quarterly-report.pdf",
    content_type: str = "application/pdf",
) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        size=len(content),
        filename=file_name,
        headers=Headers({"content-type": content_type}),
    )


async def audit_rows(session: AsyncSession, entity_id: uuid.UUID) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == "document", AuditLog.entity_id == entity_id)
        # (created_at, id), not created_at alone: audit rows written in one
        # transaction all carry the same now(), and without the UUIDv7
        # tiebreaker this assertion depends on which row PostgreSQL happens to
        # return first.
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def document_count(session: AsyncSession, company_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(Document).where(Document.company_id == company_id)
    return int((await session.execute(stmt)).scalar_one())


# -----------------------------------------------------------------------------
# Upload
# -----------------------------------------------------------------------------


class TestUpload:
    async def test_writes_the_file_the_row_and_the_audit_entry(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        service = DocumentService(session, tenant)

        document = await service.upload(
            upload_file(PDF), DocumentUploadRequest(category=DocumentCategory.DOCUMENT)
        )

        assert document.size_bytes == len(PDF)
        assert document.checksum_sha256 == hashlib.sha256(PDF).hexdigest()
        assert document.mime_type == "application/pdf"
        assert document.file_name == "quarterly-report.pdf"
        assert document.expires_at is None

        files = stored_files(storage_root)
        assert len(files) == 1
        assert files[0].read_bytes() == PDF

        row = (
            await session.execute(select(Document).where(Document.id == document.id))
        ).scalar_one()
        # Key is server-generated: tenant-partitioned, dated, and carrying none
        # of the submitted file name.
        assert row.storage_key.startswith(f"documents/{tenant.company_id}/")
        assert "quarterly-report" not in row.storage_key
        assert row.uploaded_by_id == tenant.user_id
        assert row.is_public is False

        entries = await audit_rows(session, document.id)
        assert [e.action for e in entries] == [AuditAction.DOCUMENT_UPLOAD]
        assert entries[0].new_values["checksum_sha256"] == document.checksum_sha256

    async def test_a_traversal_filename_is_stored_for_display_only(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        document = await DocumentService(session, tenant).upload(
            upload_file(PDF, file_name="../../../etc/passwd"), DocumentUploadRequest()
        )

        assert document.file_name == "passwd"
        files = stored_files(storage_root)
        assert len(files) == 1
        # Everything landed inside the storage root, and nothing is named after
        # the submitted path.
        assert files[0].is_relative_to(storage_root)
        assert files[0].suffix == ".pdf"

    async def test_an_executable_renamed_as_a_pdf_never_reaches_disk(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        with pytest.raises(FileValidationError, match="executable or script"):
            await DocumentService(session, tenant).upload(
                upload_file(WINDOWS_EXECUTABLE, file_name="invoice.pdf"),
                DocumentUploadRequest(),
            )

        # Rejected before the destination was even opened.
        assert stored_files(storage_root) == []
        assert await document_count(session, tenant.company_id) == 0

    async def test_a_disallowed_content_type_is_refused(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        with pytest.raises(FileValidationError) as exc:
            await DocumentService(session, tenant).upload(
                upload_file(b"<svg/>", file_name="logo.svg", content_type="image/svg+xml"),
                DocumentUploadRequest(),
            )

        assert exc.value.code is ErrorCode.FILE_TYPE_NOT_ALLOWED
        assert stored_files(storage_root) == []

    async def test_the_size_limit_is_enforced_during_the_copy(
        self, session: AsyncSession, tenant: TenantContext, storage_root, monkeypatch
    ) -> None:
        """An oversized upload is abandoned mid-copy and leaves nothing behind."""
        monkeypatch.setattr(settings, "max_upload_size_mb", 1)
        oversized = b"%PDF-1.7\n" + b"x" * (2 * 1024 * 1024)

        part = upload_file(oversized)
        # The declared size is cleared so the pre-flight shortcut cannot fire and
        # the streaming check is the thing actually under test.
        part.size = None

        with pytest.raises(FileValidationError) as exc:
            await DocumentService(session, tenant).upload(part, DocumentUploadRequest())

        assert exc.value.code is ErrorCode.FILE_TOO_LARGE
        assert exc.value.status_code == 413
        assert stored_files(storage_root) == []
        assert await document_count(session, tenant.company_id) == 0

    async def test_transient_categories_get_a_default_retention(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        before = utcnow()
        document = await DocumentService(session, tenant).upload(
            upload_file(CSV, file_name="stock.csv", content_type="text/csv"),
            DocumentUploadRequest(category=DocumentCategory.EXPORT),
        )

        assert document.expires_at is not None
        assert timedelta(days=6) < document.expires_at - before < timedelta(days=8)

    async def test_an_entity_reference_is_persisted_and_filterable(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        receipt_id = uuid.uuid4()
        service = DocumentService(session, tenant)

        attached = await service.upload(
            upload_file(PDF, file_name="qc-cert.pdf"),
            DocumentUploadRequest(
                category=DocumentCategory.ATTACHMENT,
                entity_type="goods_receipt",
                entity_id=receipt_id,
            ),
        )
        await service.upload(upload_file(PDF, file_name="unrelated.pdf"), DocumentUploadRequest())

        items, pagination = await service.list_documents(
            DocumentFilters(entity_type="goods_receipt", entity_id=receipt_id),
            PageParams(page=1, limit=50),
            SortParams(),
        )

        assert [d.id for d in items] == [attached.id]
        assert pagination.total == 1


# -----------------------------------------------------------------------------
# Reads, download and tenant isolation
# -----------------------------------------------------------------------------


class TestReads:
    async def test_download_streams_the_exact_bytes(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        service = DocumentService(session, tenant)
        document = await service.upload(upload_file(PDF), DocumentUploadRequest())

        download = await service.open_download(document.id)
        streamed = b"".join([chunk async for chunk in stream_file(download.path)])

        assert streamed == PDF
        assert hashlib.sha256(streamed).hexdigest() == document.checksum_sha256

    async def test_another_tenants_document_is_not_found_rather_than_forbidden(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        other_company,
        storage_root,
    ) -> None:
        """A 403 would confirm the id exists; a 404 tells the caller nothing."""
        document = await DocumentService(session, tenant).upload(
            upload_file(PDF), DocumentUploadRequest()
        )
        intruder = TenantContext(company_id=other_company.id, user_id=tenant.user_id)
        service = DocumentService(session, intruder)

        for call in (service.get_document, service.open_download):
            with pytest.raises(NotFoundError) as exc:
                await call(document.id)
            assert exc.value.status_code == 404

        items, _ = await service.list_documents(
            DocumentFilters(), PageParams(page=1, limit=50), SortParams()
        )
        assert items == []

    async def test_a_missing_file_reads_as_not_found(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        # A restore that missed the storage volume: the row survives, the bytes
        # do not. Metadata still resolves; the download must not.
        service = DocumentService(session, tenant)
        document = await service.upload(upload_file(PDF), DocumentUploadRequest())
        stored_files(storage_root)[0].unlink()

        assert (await service.get_document(document.id)).id == document.id
        with pytest.raises(NotFoundError) as exc:
            await service.open_download(document.id)
        assert exc.value.code is ErrorCode.FILE_NOT_FOUND

    async def test_expired_documents_are_hidden_but_still_addressable_by_the_sweep(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        service = DocumentService(session, tenant)
        document = await service.upload(
            upload_file(CSV, file_name="export.csv", content_type="text/csv"),
            DocumentUploadRequest(category=DocumentCategory.EXPORT),
        )
        # Backdate past retention. The schema refuses a past expires_at on the
        # way in, so this is done on the row - which is what the passage of time
        # would do anyway.
        row = (
            await session.execute(select(Document).where(Document.id == document.id))
        ).scalar_one()
        row.expires_at = utcnow() - timedelta(minutes=1)
        await session.flush()

        with pytest.raises(NotFoundError):
            await service.get_document(document.id)

        hidden, _ = await service.list_documents(
            DocumentFilters(), PageParams(page=1, limit=50), SortParams()
        )
        assert hidden == []

        shown, _ = await service.list_documents(
            DocumentFilters(include_expired=True), PageParams(page=1, limit=50), SortParams()
        )
        assert [d.id for d in shown] == [document.id]

        assert await service.purge_expired() == 1
        assert stored_files(storage_root) == []
        assert await document_count(session, tenant.company_id) == 0


# -----------------------------------------------------------------------------
# Deletion
# -----------------------------------------------------------------------------


class TestDeletion:
    async def test_removes_row_and_file_and_audits_it(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        service = DocumentService(session, tenant)
        document = await service.upload(upload_file(PDF), DocumentUploadRequest())
        path = stored_files(storage_root)[0]

        await service.delete_document(document.id, reason="Superseded by revision B")

        assert not path.exists()
        assert await document_count(session, tenant.company_id) == 0

        entries = await audit_rows(session, document.id)
        assert [e.action for e in entries] == [
            AuditAction.DOCUMENT_UPLOAD,
            AuditAction.DOCUMENT_DELETE,
        ]
        deletion = entries[-1]
        # The audit row is the only surviving record of what was removed.
        assert deletion.old_values["file_name"] == "quarterly-report.pdf"
        assert deletion.old_values["checksum_sha256"] == document.checksum_sha256
        assert deletion.new_values["status"] == "DELETED"
        assert deletion.context["reason"] == "Superseded by revision B"

    async def test_another_tenant_cannot_delete(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        other_company,
        storage_root,
    ) -> None:
        document = await DocumentService(session, tenant).upload(
            upload_file(PDF), DocumentUploadRequest()
        )
        intruder = TenantContext(company_id=other_company.id, user_id=tenant.user_id)

        with pytest.raises(NotFoundError):
            await DocumentService(session, intruder).delete_document(
                document.id, reason="Not mine to delete"
            )

        assert len(stored_files(storage_root)) == 1
        assert await document_count(session, tenant.company_id) == 1

    async def test_deleting_twice_is_not_found_the_second_time(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        service = DocumentService(session, tenant)
        document = await service.upload(upload_file(PDF), DocumentUploadRequest())

        await service.delete_document(document.id, reason="First removal")
        with pytest.raises(NotFoundError):
            await service.delete_document(document.id, reason="Second removal")
