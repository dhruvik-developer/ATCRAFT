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

import asyncio
import hashlib
import io
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers, UploadFile

from src.core.actors import SYSTEM_ACTOR_LABEL
from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    ErrorCode,
    FileValidationError,
    MalwareDetectedError,
    MalwareScanUnavailableError,
    NotFoundError,
)
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.documents import service as documents_service
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.repository import DocumentRepository
from src.modules.documents.schemas import DocumentFilters, DocumentUploadRequest
from src.modules.documents.service import (
    PURGE_REASON,
    DocumentService,
    resolve_storage_path,
    stream_file,
)
from src.workers.tasks import maintenance

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

    async def test_a_detected_payload_leaves_no_file_and_no_row(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        storage_root,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def infected(path):
            raise MalwareDetectedError("Eicar-Test-Signature")

        monkeypatch.setattr(documents_service, "scan_upload", infected)

        with pytest.raises(MalwareDetectedError):
            await DocumentService(session, tenant).upload(
                upload_file(PDF), DocumentUploadRequest(category=DocumentCategory.DOCUMENT)
            )

        assert stored_files(storage_root) == []
        assert await document_count(session, tenant.company_id) == 0

    async def test_an_unscannable_upload_is_refused_under_fail_closed(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        storage_root,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def unavailable(path):
            raise MalwareScanUnavailableError()

        monkeypatch.setattr(documents_service, "scan_upload", unavailable)

        with pytest.raises(MalwareScanUnavailableError):
            await DocumentService(session, tenant).upload(
                upload_file(PDF), DocumentUploadRequest(category=DocumentCategory.DOCUMENT)
            )

        assert stored_files(storage_root) == []
        assert await document_count(session, tenant.company_id) == 0

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

    async def test_a_request_cancelled_during_the_audit_leaves_no_file_and_no_row(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        storage_root,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cancellation is a BaseException, not an Exception: the handler
        that removed the bytes on a failed row used to let it through."""

        async def cancelled(*_args: object, **_kwargs: object) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(documents_service, "record_audit", cancelled)

        with pytest.raises(asyncio.CancelledError):
            await DocumentService(session, tenant).upload(upload_file(PDF), DocumentUploadRequest())

        assert stored_files(storage_root) == []
        assert await document_count(session, tenant.company_id) == 0

    async def test_a_cancellation_while_committing_keeps_the_bytes_of_a_row_that_may_exist(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        storage_root,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancelled after the body, with the COMMIT in flight, the row may have
        landed - removing its bytes then would leave a document nobody can open.
        A caller whose own transaction is known to have failed still can."""

        @asynccontextmanager
        async def commit_cancelled(_session: AsyncSession) -> AsyncIterator[AsyncSession]:
            yield _session
            raise asyncio.CancelledError

        monkeypatch.setattr(documents_service, "transaction", commit_cancelled)
        service = DocumentService(session, tenant)

        with pytest.raises(asyncio.CancelledError):
            await service.upload(upload_file(PDF), DocumentUploadRequest())

        assert len(stored_files(storage_root)) == 1
        await service.discard_stored(asyncio.CancelledError(), committing=False)
        assert stored_files(storage_root) == []

    async def test_a_failed_enclosing_transaction_removes_only_what_its_service_stored(
        self, session: AsyncSession, tenant: TenantContext, storage_root
    ) -> None:
        """``upload`` inside a caller's transaction: the caller's next step fails,
        the row rolls back with it, and discard_stored takes the bytes too -
        but not a file another upload already committed."""
        committed = await DocumentService(session, tenant).upload(
            upload_file(PDF, file_name="committed.pdf"), DocumentUploadRequest()
        )
        committed_id = committed.id
        service = DocumentService(session, tenant)

        with pytest.raises(BusinessRuleError):
            try:
                async with transaction(session):
                    await service.upload(
                        upload_file(CSV, file_name="rolled-back.csv", content_type="text/csv"),
                        DocumentUploadRequest(),
                    )
                    raise BusinessRuleError("The caller's next step failed.")
            except BusinessRuleError as failure:
                await service.discard_stored(failure)
                raise

        files = stored_files(storage_root)
        assert [path.read_bytes() for path in files] == [PDF]
        assert await document_count(session, tenant.company_id) == 1
        assert (await service.get_document(committed_id)).file_name == "committed.pdf"

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

        assert (await service.purge_expired()).removed == 1
        assert stored_files(storage_root) == []
        assert await document_count(session, tenant.company_id) == 0


# -----------------------------------------------------------------------------
# Deletion
# -----------------------------------------------------------------------------


class TestDeletion:
    async def test_a_document_the_purge_deleted_first_answers_404_and_is_not_audited_twice(
        self, session: AsyncSession, tenant: TenantContext, storage_root, monkeypatch
    ) -> None:
        service = DocumentService(session, tenant)
        document = await service.upload(upload_file(PDF), DocumentUploadRequest())
        get_for_delete = DocumentRepository.get_for_delete

        async def raced(self, *args, **kwargs):
            row = await get_for_delete(self, *args, **kwargs)
            # The purge's delete, committed between the person's read and theirs.
            await self.session.execute(
                delete(Document.__table__).where(Document.__table__.c.id == document.id)
            )
            return row

        monkeypatch.setattr(DocumentRepository, "get_for_delete", raced)

        with pytest.raises(NotFoundError):
            await service.delete_document(document.id, reason="Superseded")
        assert await purge_audits(session, [document.id]) == []

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


# -----------------------------------------------------------------------------
# The nightly purge
# -----------------------------------------------------------------------------


def run_in(session: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """``session_scope`` for the sweep: every session it opens is the test's own."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield session

    return scope


async def stored(
    service: DocumentService,
    file_name: str,
    category: DocumentCategory = DocumentCategory.EXPORT,
    *,
    expires_at: datetime | None = None,
    lapsed_minutes_ago: int | None = None,
) -> tuple[uuid.UUID, Path]:
    """Upload a CSV and return its id and its file; ``lapsed_minutes_ago`` expires it."""
    document = await service.upload(
        upload_file(CSV, file_name=file_name, content_type="text/csv"),
        DocumentUploadRequest(category=category, expires_at=expires_at),
    )
    row = (
        await service.session.execute(select(Document).where(Document.id == document.id))
    ).scalar_one()
    if lapsed_minutes_ago is not None:
        # The schema refuses a past expires_at, so the passage of time is done
        # on the row.
        row.expires_at = utcnow() - timedelta(minutes=lapsed_minutes_ago)
        await service.session.flush()
    return document.id, resolve_storage_path(row.storage_key)


async def purge_audits(session: AsyncSession, ids: list[uuid.UUID]) -> list[AuditLog]:
    stmt = select(AuditLog).where(
        AuditLog.action == AuditAction.DOCUMENT_DELETE, AuditLog.entity_id.in_(ids)
    )
    return list((await session.execute(stmt)).scalars().all())


async def surviving(session: AsyncSession, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    stmt = select(Document.id).where(Document.id.in_(ids))
    return set((await session.execute(stmt)).scalars().all())


class TestNightlyPurge:
    """``maintenance.sweep_expired_documents``, the work of the nightly beat entry."""

    async def test_every_company_loses_its_lapsed_files_and_keeps_everything_else(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        other_company,
        storage_root,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ours = DocumentService(session, tenant)
        theirs = DocumentService(
            session, TenantContext(company_id=other_company.id, user_id=tenant.user_id)
        )
        lapsed = {
            # An export whose default retention has run out ...
            await stored(ours, "stock.csv", lapsed_minutes_ago=5): tenant.company_id,
            # ... a business document whose uploader chose a date ...
            await stored(
                ours,
                "drawing.csv",
                DocumentCategory.DOCUMENT,
                expires_at=utcnow() + timedelta(days=1),
                lapsed_minutes_ago=4,
            ): tenant.company_id,
            # ... a scratch file whose bytes the temp sweep took first ...
            await stored(
                ours, "scratch.csv", DocumentCategory.TEMP, lapsed_minutes_ago=3
            ): tenant.company_id,
            # ... and another company's lapsed export.
            await stored(theirs, "their-stock.csv", lapsed_minutes_ago=2): other_company.id,
        }
        kept = [
            await stored(ours, "fresh-export.csv"),
            await stored(ours, "contract.csv", DocumentCategory.DOCUMENT),
            await stored(ours, "certificate.csv", DocumentCategory.ATTACHMENT),
            await stored(ours, "report.csv", DocumentCategory.REPORT),
            await stored(theirs, "their-contract.csv", DocumentCategory.DOCUMENT),
        ]
        lapsed_ids = [document_id for document_id, _ in lapsed]
        kept_ids = [document_id for document_id, _ in kept]
        scratch_file = list(lapsed)[2][1]
        scratch_file.unlink()
        monkeypatch.setattr(maintenance, "session_scope", run_in(session))

        result = await maintenance.sweep_expired_documents()

        assert result == {"companies": 2, "removed": 4, "failed": 0, "companies_failed": 0}
        assert await surviving(session, lapsed_ids + kept_ids) == set(kept_ids)
        assert [path.exists() for _, path in lapsed] == [False] * 4
        assert [path.exists() for _, path in kept] == [True] * 5

        # One deletion each, recorded in its own company, by the system and
        # with the sweep's reason - the only record left of what went.
        audits = await purge_audits(session, lapsed_ids + kept_ids)
        assert sorted((a.entity_id, a.company_id) for a in audits) == sorted(
            (document_id, company_id) for (document_id, _), company_id in lapsed.items()
        )
        for audit in audits:
            assert audit.context["reason"] == PURGE_REASON
            assert audit.created_by_id is None
            assert audit.actor_label == SYSTEM_ACTOR_LABEL
            assert audit.old_values["status"] == "EXPIRED"

        # The next night finds nothing: no row goes and no deletion is audited.
        assert await maintenance.sweep_expired_documents() == {
            "companies": 0,
            "removed": 0,
            "failed": 0,
            "companies_failed": 0,
        }
        assert len(await purge_audits(session, lapsed_ids + kept_ids)) == 4
        assert await surviving(session, kept_ids) == set(kept_ids)

    async def test_a_file_goes_only_once_the_delete_of_its_row_has_committed(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        storage_root,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unlinking first would leave, on a failed commit, a row whose bytes are
        gone - which nothing can repair; the other way round leaves a stray file."""
        service = DocumentService(session, tenant)
        for name in ("first.csv", "second.csv"):
            await stored(service, name, lapsed_minutes_ago=1)
        events: list[str] = []
        commit, discard = session.commit, documents_service._discard

        async def recorded_commit() -> None:
            await commit()
            events.append("commit")

        async def recorded_discard(path: Path) -> None:
            events.append("unlink")
            await discard(path)

        monkeypatch.setattr(session, "commit", recorded_commit)
        monkeypatch.setattr(documents_service, "_discard", recorded_discard)
        monkeypatch.setattr(maintenance, "session_scope", run_in(session))

        assert (await maintenance.sweep_expired_documents())["removed"] == 2
        assert events == ["commit", "unlink", "commit", "unlink"]

    async def test_a_backlog_goes_in_bounded_batches_oldest_first_over_several_nights(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        storage_root,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = DocumentService(session, tenant)
        backlog = [
            await stored(service, f"export-{age}.csv", lapsed_minutes_ago=age)
            for age in (5, 4, 3, 2, 1)
        ]
        ids = [document_id for document_id, _ in backlog]
        monkeypatch.setattr(maintenance, "DOCUMENT_PURGE_BATCH", 2)
        monkeypatch.setattr(maintenance, "MAX_PURGE_BATCHES", 2)
        monkeypatch.setattr(maintenance, "session_scope", run_in(session))

        # Two batches of two: the four that lapsed first, and no more.
        assert (await maintenance.sweep_expired_documents())["removed"] == 4
        assert await surviving(session, ids) == {ids[-1]}
        assert [path.exists() for _, path in backlog] == [False, False, False, False, True]

        assert (await maintenance.sweep_expired_documents())["removed"] == 1
        assert await surviving(session, ids) == set()
        assert not backlog[-1][1].exists()

    async def test_a_row_that_cannot_be_removed_is_skipped_and_the_rest_still_go(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        storage_root,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The oldest rows are read first, so a row that always fails once stopped
        its company's purge at that row every night."""
        service = DocumentService(session, tenant)
        backlog = [
            await stored(service, f"export-{age}.csv", lapsed_minutes_ago=age)
            for age in (5, 4, 3, 2)
        ]
        ids = [document_id for document_id, _ in backlog]
        bad_key, refused_audit = ids[0], ids[1]
        # A storage key resolve_storage_path refuses: refused before anything is written.
        await session.execute(
            update(Document).where(Document.id == bad_key).values(storage_key="exports//escape.csv")
        )
        # A write refused inside the row's own transaction, which rolls its
        # savepoint back and expires every instance the batch loaded.
        record = documents_service.record_audit

        async def refusing(*args, **kwargs):
            if kwargs.get("entity_id") == refused_audit:
                raise RuntimeError("the audit insert failed")
            return await record(*args, **kwargs)

        monkeypatch.setattr(documents_service, "record_audit", refusing)
        monkeypatch.setattr(maintenance, "DOCUMENT_PURGE_BATCH", 2)
        monkeypatch.setattr(maintenance, "session_scope", run_in(session))

        result = await maintenance.sweep_expired_documents()

        # Both failures fill the first batch, and the second batch still runs.
        assert result == {"companies": 1, "removed": 2, "failed": 2, "companies_failed": 0}
        assert await surviving(session, ids) == {bad_key, refused_audit}
        assert [path.exists() for _, path in backlog] == [True, True, False, False]
        assert {audit.entity_id for audit in await purge_audits(session, ids)} == set(ids[2:])

    async def test_a_row_someone_deleted_meanwhile_is_not_deleted_or_audited_again(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        storage_root,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A person deleting a document the purge has already read: one audit row."""
        service = DocumentService(session, tenant)
        document_id, path = await stored(service, "export.csv", lapsed_minutes_ago=1)
        list_expired = DocumentRepository.list_expired

        async def raced(self, *args, **kwargs):
            rows = await list_expired(self, *args, **kwargs)
            # The other deleter's statement, committed between the read and the purge.
            await self.session.execute(
                delete(Document.__table__).where(Document.__table__.c.id == document_id)
            )
            return rows

        monkeypatch.setattr(DocumentRepository, "list_expired", raced)

        batch = await service.purge_expired()

        assert (batch.read, batch.removed, batch.failed) == (1, 0, 0)
        assert await purge_audits(session, [document_id]) == []
        # The file is the other deleter's to unlink, not the purge's.
        assert path.exists()
