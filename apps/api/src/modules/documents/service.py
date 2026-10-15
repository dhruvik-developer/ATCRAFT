"""Secure file storage: validation, streaming, retrieval and removal.

Master rules sections 43 and 44. Everything defensive about this module is here;
the router only binds parameters and the repository only builds queries.

The threat model, and what answers each part of it
--------------------------------------------------

*A ``.exe`` renamed ``report.pdf``.* The declared content type must be on
``settings.allowed_mime_type_set`` **and** the leading bytes of the content must
match that type's signature. A short list of executable and script signatures is
refused outright whatever the declaration says, so a mislabelled binary cannot
reach disk even if a future configuration change widens the allow-list.

*A file large enough to fill the disk.* The size ceiling is enforced against a
running total while the content is copied, and the copy is abandoned the moment
it is crossed. Checking afterwards would mean the damage is already done.

*A filename that is really a path.* The storage key is built entirely from
server-side values - category, tenant, date, a fresh UUIDv7 and the extension
implied by the *verified* type. The client's filename is kept for display and
never touches a filesystem call. Every path is then resolved and asserted to be
inside ``settings.storage_path`` before any read or write, so a key that somehow
escaped - a hand-edited row, a future caller passing its own key - still cannot
address a byte outside the storage root.

*Another company's document.* Reads go through the tenant-scoped repository and
a miss raises ``NotFoundError``, never ``AuthorizationError``: a 403 would
confirm the id exists.

*A browser talked into executing what it downloaded.* Responses carry
``Content-Disposition: attachment`` and ``X-Content-Type-Options: nosniff`` (set
by the router from :func:`content_disposition` and :data:`DOWNLOAD_HEADERS`).

Ordering of the write and the commit
------------------------------------
The bytes are written first, then the metadata row is committed. If the
transaction fails, the orphaned file is unlinked before the error propagates.
The reverse order would leave a committed row whose bytes never arrived, and a
row pointing at nothing is worse than a file nothing points at: the first breaks
every reader, the second is invisible until a housekeeping sweep tidies it.

Deletion is the mirror image, for the same reason: the row is removed and the
audit entry written in one transaction, and only once that has committed is the
file unlinked. A failed unlink is logged and leaves an unreferenced file rather
than resurrecting a document the audit log already says was deleted.
"""

from __future__ import annotations

import codecs
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO
from urllib.parse import quote

import anyio
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import ErrorCode, FileValidationError, NotFoundError
from src.core.ids import new_id
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, snapshot
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.repository import MAX_PURGE_BATCH, DocumentRepository
from src.modules.documents.schemas import (
    DocumentFilters,
    DocumentRead,
    DocumentUploadRequest,
)

log = get_logger(__name__)

AUDIT_ENTITY = "document"

#: Reason written to the audit log by the retention sweep, so a purge is
#: distinguishable from a person deleting a file.
PURGE_REASON = "Retention period elapsed"

#: What a caller is told when a stored row's key cannot be resolved to a file.
#: Deliberately says nothing about keys, roots or layout: the key is generated
#: server-side and the caller only ever sent a document id.
STORAGE_KEY_UNUSABLE = "File not found."

#: Metadata captured into the audit entry before a row is destroyed. Explicit
#: rather than "every column": the actor and timestamp columns are already on the
#: audit row itself, and the storage key is internal.
_AUDITED_FIELDS = [
    "file_name",
    "mime_type",
    "size_bytes",
    "checksum_sha256",
    "category",
    "entity_type",
    "entity_id",
    "uploaded_by_id",
    "is_public",
    "expires_at",
]

#: Copy granularity. Large enough that the per-chunk thread hop is amortised,
#: small enough that a rejected oversized upload is abandoned promptly.
CHUNK_SIZE = 64 * 1024

#: How much of the leading content is inspected for a type signature. Every
#: signature this module knows lives in the first 16 bytes; 512 gives the text
#: heuristic enough to work with and still costs one read.
HEADER_BYTES = 512

#: Headers every download carries.
#:
#: ``nosniff`` is the one that matters: without it a browser may ignore the
#: declared type and execute an HTML or script payload that was uploaded under
#: an image content type. ``no-store`` keeps tenant data out of shared caches -
#: a response is authorised for one principal, not for whatever proxy sees it.
DOWNLOAD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
}


# -----------------------------------------------------------------------------
# Content type verification
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MagicSignature:
    """One acceptable byte pattern, expressed as ``(offset, bytes)`` parts.

    Several parts are ANDed: WebP, for instance, is ``RIFF`` at 0 *and* ``WEBP``
    at 8, and checking only the first would accept any RIFF container - a WAV or
    an AVI - as an image.
    """

    parts: tuple[tuple[int, bytes], ...]

    def matches(self, header: bytes) -> bool:
        return all(header[at : at + len(magic)] == magic for at, magic in self.parts)


def _sig(*parts: tuple[int, bytes]) -> MagicSignature:
    return MagicSignature(parts=parts)


@dataclass(frozen=True, slots=True)
class FileTypeRule:
    """How one MIME type is recognised and named on disk."""

    #: Extension used for the storage key. Derived from the verified type, never
    #: from the client's filename, so ``invoice.pdf.exe`` cannot land as ``.exe``.
    extension: str
    signatures: tuple[MagicSignature, ...] = ()
    #: True for formats with no magic number, verified by the text heuristic.
    is_text: bool = False


#: The types this module can *prove*. A type outside this table is refused even
#: if an operator adds it to ``ALLOWED_UPLOAD_MIME_TYPES``, because accepting it
#: would mean trusting the client's declaration - see ``assert_upload_allowed``.
KNOWN_TYPES: dict[str, FileTypeRule] = {
    "image/jpeg": FileTypeRule(".jpg", (_sig((0, b"\xff\xd8\xff")),)),
    "image/png": FileTypeRule(".png", (_sig((0, b"\x89PNG\r\n\x1a\n")),)),
    "image/webp": FileTypeRule(".webp", (_sig((0, b"RIFF"), (8, b"WEBP")),)),
    "application/pdf": FileTypeRule(".pdf", (_sig((0, b"%PDF-")),)),
    "text/csv": FileTypeRule(".csv", is_text=True),
    # XLSX is a ZIP container. The three signatures are a normal archive, an
    # empty one and a spanned one - a writer that emits the latter two is
    # unusual but legal, and rejecting them would look like random corruption.
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": FileTypeRule(
        ".xlsx",
        (
            _sig((0, b"PK\x03\x04")),
            _sig((0, b"PK\x05\x06")),
            _sig((0, b"PK\x07\x08")),
        ),
    ),
}

#: Refused whatever the declared type is. These are the payloads that turn a
#: file store into a delivery mechanism, and none of them can legitimately be
#: any allowed type - so the check is unconditional rather than per-type.
BLOCKED_SIGNATURES: tuple[MagicSignature, ...] = (
    _sig((0, b"MZ")),  # DOS/PE executable, .exe and .dll
    _sig((0, b"\x7fELF")),  # ELF executable
    _sig((0, b"#!")),  # shebang script
    _sig((0, b"\xca\xfe\xba\xbe")),  # Java class / Mach-O fat binary
    _sig((0, b"\xfe\xed\xfa\xce")),  # Mach-O 32-bit
    _sig((0, b"\xfe\xed\xfa\xcf")),  # Mach-O 64-bit
    _sig((0, b"\xcf\xfa\xed\xfe")),  # Mach-O 64-bit, little endian
    _sig((0, b"\x00asm")),  # WebAssembly
)


def normalise_mime_type(raw: str | None) -> str:
    """Reduce a Content-Type header to its bare type.

    ``text/csv; charset=utf-8`` and ``TEXT/CSV`` are the same type, and comparing
    the raw header against the allow-list would reject both.
    """
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip().lower()


def assert_upload_allowed(mime_type: str) -> FileTypeRule:
    """Check the declared type against configuration and against what we can verify.

    Two gates, and the second is the interesting one. A type an operator has
    allowed but that this module cannot recognise is still refused: without a
    signature the only evidence of what the file contains would be the client's
    own claim, which is precisely what this whole path exists not to trust.
    """
    if mime_type not in settings.allowed_mime_type_set:
        raise FileValidationError(
            f"Files of type '{mime_type or 'unknown'}' are not accepted.",
            details={"allowed_mime_types": sorted(settings.allowed_mime_type_set)},
        )

    rule = KNOWN_TYPES.get(mime_type)
    if rule is None:
        # ASSUMPTION: an allowed-but-unverifiable type is rejected rather than
        # waved through. Adding a type to ALLOWED_UPLOAD_MIME_TYPES therefore
        # also requires adding its signature to KNOWN_TYPES - a deliberate
        # coupling, so widening the allow-list cannot silently disable the
        # content check.
        log.warning("documents.unverifiable_mime_type", mime_type=mime_type)
        raise FileValidationError(
            f"Files of type '{mime_type}' cannot be content-verified and are not accepted.",
        )
    return rule


def _looks_like_text(header: bytes) -> bool:
    """Is this leading chunk plausibly UTF-8 text?

    An incremental decoder is used rather than ``bytes.decode`` because the chunk
    is cut at a fixed offset and may end mid-character; a plain decode would
    reject a perfectly good CSV for the crime of having an accented name near
    byte 512. A NUL byte, by contrast, never appears in text and is the clearest
    single signal that something binary is being passed off as a CSV.
    """
    if b"\x00" in header:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(header, False)
    except UnicodeDecodeError:
        return False
    return True


def assert_content_matches(mime_type: str, header: bytes, rule: FileTypeRule) -> None:
    """Verify the actual leading bytes against the declared type.

    This is the check that catches an executable renamed to ``.pdf``: the
    declaration says ``application/pdf``, the bytes start ``MZ``, and no amount
    of renaming changes the second.
    """
    if not header:
        raise FileValidationError("The uploaded file is empty.")

    for blocked in BLOCKED_SIGNATURES:
        if blocked.matches(header):
            log.warning(
                "documents.executable_content_rejected",
                declared_mime_type=mime_type,
                leading_bytes=header[:8].hex(),
            )
            raise FileValidationError(
                "The file content is an executable or script and cannot be stored."
            )

    if rule.is_text:
        if not _looks_like_text(header):
            raise FileValidationError(
                f"The file content is not valid text and does not match '{mime_type}'."
            )
        return

    if not any(signature.matches(header) for signature in rule.signatures):
        raise FileValidationError(
            f"The file content does not match the declared type '{mime_type}'.",
            details={"declared_mime_type": mime_type},
        )


# -----------------------------------------------------------------------------
# Names and paths
# -----------------------------------------------------------------------------

#: Storage sub-directory per category. Matches the tree
#: ``Settings.ensure_storage_dirs`` creates, so nothing has to be provisioned
#: separately when a new category starts being used.
CATEGORY_DIRECTORY: dict[DocumentCategory, str] = {
    DocumentCategory.DOCUMENT: "documents",
    DocumentCategory.REPORT: "reports",
    DocumentCategory.ATTACHMENT: "attachments",
    DocumentCategory.EXPORT: "exports",
    DocumentCategory.TEMP: "temp",
}

#: ASSUMPTION: generated artefacts are transient and are given a default
#: retention when the caller does not set one, so an export nobody collects does
#: not sit on disk for ever. Uploaded business documents have no default expiry -
#: silently deleting a supplier's QC certificate would be a data-loss bug, not
#: housekeeping.
DEFAULT_RETENTION: dict[DocumentCategory, timedelta] = {
    DocumentCategory.TEMP: timedelta(hours=24),
    DocumentCategory.EXPORT: timedelta(days=7),
}

_FILENAME_FALLBACK = "upload"


def sanitise_file_name(raw: str | None) -> str:
    """Reduce a client-supplied filename to something safe to store and display.

    It is display metadata, not a path - but it is echoed into a
    ``Content-Disposition`` header, so control characters (a CR or LF would end
    the header and start another) and directory components are stripped here
    rather than trusted to whatever renders it later.
    """
    candidate = (raw or "").strip()
    # Both flavours: a Windows client sends ``C:\Users\a\b.pdf``, a POSIX one
    # ``/home/a/b.pdf``, and each parser ignores the other's separator.
    candidate = PureWindowsPath(PurePosixPath(candidate).name).name
    candidate = "".join(ch for ch in candidate if ch.isprintable()).strip(" .")
    if not candidate:
        return _FILENAME_FALLBACK
    return candidate[:255]


def build_storage_key(
    *,
    category: DocumentCategory,
    company_id: uuid.UUID,
    rule: FileTypeRule,
    moment: datetime,
    file_id: uuid.UUID,
) -> str:
    """``<dir>/<company_id>/<yyyy>/<mm>/<uuid7><ext>`` - entirely server-side.

    The date partition keeps any one directory to a month of one tenant's
    uploads, which matters on filesystems that degrade with very wide
    directories, and makes an age-based retention sweep a directory walk rather
    than a full table scan.
    """
    return (
        f"{CATEGORY_DIRECTORY[category]}/{company_id}/"
        f"{moment:%Y}/{moment:%m}/{file_id}{rule.extension}"
    )


def resolve_storage_path(storage_key: str) -> Path:
    """Resolve a storage key to an absolute path inside the storage root.

    The containment check is lexical, on a normalised path, and it runs before
    any filesystem call - so a traversal attempt never becomes an ``open`` that
    the operating system then has to be trusted to refuse. ``normpath`` collapses
    ``..`` textually, and the ``is_relative_to`` assertion is what actually
    rejects the result.

    Symlinks are deliberately not followed: resolving them would require the path
    to exist (it does not yet, on the write path), and anyone able to plant a
    symlink inside the storage root already has filesystem access - which is a
    different incident from the one this guard defends against.
    """
    root = settings.storage_path
    key = (storage_key or "").strip()

    # Every refusal below answers 404 FILE_NOT_FOUND, not 400. The key is
    # server-generated - a caller only ever sent a document id - so a row whose
    # key fails containment is a missing file from the caller's side, not a
    # malformed request; and the message names no internal concept, because the
    # storage layout is not something a response should describe. The row that
    # carries the bad key is logged so an operator can find it.
    if not key:
        raise FileValidationError(
            STORAGE_KEY_UNUSABLE, code=ErrorCode.FILE_NOT_FOUND, status_code=404
        )

    # Rejected before joining, because ``Path("/root") / "/etc/passwd"`` is
    # ``/etc/passwd``: an absolute component silently discards the base.
    if (
        PurePosixPath(key).is_absolute()
        or PureWindowsPath(key).is_absolute()
        or key.startswith(("/", "\\"))
        or "\\" in key
        or "\x00" in key
    ):
        log.error("documents.invalid_storage_key", storage_key=storage_key)
        raise FileValidationError(
            STORAGE_KEY_UNUSABLE, code=ErrorCode.FILE_NOT_FOUND, status_code=404
        )

    # Split by hand rather than through PurePosixPath: that class silently
    # normalises away "." and empty segments, so "./x" and "a//x" would come
    # back clean. A key this module generated never contains either, and a key
    # that does has been through something that should be looked at, not tidied.
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        log.error("documents.invalid_storage_key", storage_key=storage_key)
        raise FileValidationError(
            STORAGE_KEY_UNUSABLE, code=ErrorCode.FILE_NOT_FOUND, status_code=404
        )

    candidate = Path(os.path.normpath(os.path.join(root, *parts)))
    if candidate == root or not candidate.is_relative_to(root):
        log.error("documents.path_traversal_blocked", storage_key=storage_key)
        raise FileValidationError(
            STORAGE_KEY_UNUSABLE, code=ErrorCode.FILE_NOT_FOUND, status_code=404
        )
    return candidate


def content_disposition(file_name: str) -> str:
    """``attachment`` disposition carrying both an ASCII and a UTF-8 filename.

    ``attachment`` rather than ``inline`` is not cosmetic: an inline HTML or SVG
    payload would run in the application's own origin. The two filename forms are
    RFC 6266: the quoted one for clients that predate ``filename*``, the
    percent-encoded one so a non-ASCII name survives intact.
    """
    safe = sanitise_file_name(file_name)
    ascii_name = safe.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe, safe='')}"


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


class DocumentState(StrEnum):
    """Derived lifecycle of a stored file.

    There is no ``status`` column: the state follows from ``expires_at`` and from
    whether the row exists at all, and a stored copy could only ever disagree
    with them. Routing removal through a :class:`StateMachine` still gives one
    declared, testable place where the legal moves live (section 18/73).
    """

    AVAILABLE = "AVAILABLE"
    EXPIRED = "EXPIRED"
    DELETED = "DELETED"


DOCUMENT_LIFECYCLE: StateMachine[DocumentState] = StateMachine(
    entity="Document",
    initial=DocumentState.AVAILABLE,
    transitions=(
        # Not an action anyone takes - time takes it - but declaring the edge is
        # what makes "purge an expired file" a legal move rather than a special
        # case bolted onto delete.
        Transition(DocumentState.AVAILABLE, DocumentState.EXPIRED, label="Expire"),
        Transition(
            DocumentState.AVAILABLE,
            DocumentState.DELETED,
            permission=Permission.DOCUMENT_DELETE,
            label="Delete",
            is_irreversible=True,
        ),
        Transition(
            DocumentState.EXPIRED,
            DocumentState.DELETED,
            permission=Permission.DOCUMENT_DELETE,
            label="Purge",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({DocumentState.DELETED}),
)


def document_state(document: Document, now: datetime | None = None) -> DocumentState:
    """Current state of a persisted document. A deleted row has none - it is gone."""
    moment = now or utcnow()
    if document.expires_at is not None and document.expires_at <= moment:
        return DocumentState.EXPIRED
    return DocumentState.AVAILABLE


def default_expiry(category: DocumentCategory, moment: datetime) -> datetime | None:
    retention = DEFAULT_RETENTION.get(category)
    return moment + retention if retention is not None else None


# -----------------------------------------------------------------------------
# Filesystem primitives
# -----------------------------------------------------------------------------
#
# Every one of these blocks. They are plain synchronous functions handed to
# ``anyio.to_thread.run_sync`` so a slow disk cannot stall the event loop, and so
# no blocking call is ever written inline in an async body.


def _make_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _open_write(path: Path) -> BinaryIO:
    # "xb" - exclusive create. The key ends in a fresh UUIDv7, so an existing
    # file at that path means something is badly wrong (a replayed key, a clock
    # or entropy failure) and must not be silently overwritten.
    return path.open("xb")


def _open_read(path: Path) -> BinaryIO:
    return path.open("rb")


def _exists(path: Path) -> bool:
    return path.is_file()


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


async def _discard(path: Path) -> None:
    """Remove a partially written or orphaned file, never raising.

    Called on the failure path, where the caller already has an error worth
    reporting; masking it with an unlink failure would hide the real cause.
    """
    try:
        await anyio.to_thread.run_sync(_unlink, path)
    except OSError as exc:  # pragma: no cover - depends on filesystem state
        log.error("documents.orphan_file_left", path=str(path), error=str(exc))


async def stream_file(path: Path) -> AsyncIterator[bytes]:
    """Yield a file in chunks, without ever holding it all in memory."""
    handle = await anyio.to_thread.run_sync(_open_read, path)
    try:
        while True:
            chunk = await anyio.to_thread.run_sync(handle.read, CHUNK_SIZE)
            if not chunk:
                return
            yield chunk
    finally:
        await anyio.to_thread.run_sync(handle.close)


@dataclass(frozen=True, slots=True)
class DocumentDownload:
    """Everything the router needs to stream one document back."""

    document: DocumentRead
    path: Path


# -----------------------------------------------------------------------------
# Service
# -----------------------------------------------------------------------------


class DocumentService:
    """Business rules for stored files."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = DocumentRepository(session, tenant)

    # --- upload -------------------------------------------------------------

    async def upload(self, upload_file: UploadFile, payload: DocumentUploadRequest) -> DocumentRead:
        """Validate, store and register one uploaded file."""
        moment = utcnow()
        mime_type = normalise_mime_type(upload_file.content_type)
        rule = assert_upload_allowed(mime_type)
        file_name = sanitise_file_name(upload_file.filename)

        # A declared size over the limit is refused before a byte is copied. It
        # is only a hint - the streaming check below is the enforcement - but
        # believing it when it says "too large" costs nothing and saves the copy.
        declared_size = getattr(upload_file, "size", None)
        if declared_size is not None and declared_size > settings.max_upload_size_bytes:
            raise self._too_large(declared_size)

        header = await upload_file.read(HEADER_BYTES)
        assert_content_matches(mime_type, header, rule)

        # UUIDv7: the key is time-ordered, so a directory listing of one month's
        # uploads comes back in upload order without a database round trip.
        file_id = new_id()
        storage_key = build_storage_key(
            category=payload.category,
            company_id=self.tenant.company_id,
            rule=rule,
            moment=moment,
            file_id=file_id,
        )
        destination = resolve_storage_path(storage_key)

        size_bytes, checksum = await self._write_stream(
            upload_file, destination, first_chunk=header
        )

        try:
            async with transaction(self.session):
                document = Document(
                    file_name=file_name,
                    storage_key=storage_key,
                    mime_type=mime_type,
                    size_bytes=size_bytes,
                    checksum_sha256=checksum,
                    category=payload.category,
                    entity_type=payload.entity_type,
                    entity_id=payload.entity_id,
                    uploaded_by_id=self.tenant.user_id,
                    is_public=payload.is_public,
                    expires_at=payload.expires_at or default_expiry(payload.category, moment),
                )
                self.repo.add(document)
                await self.session.flush()

                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.DOCUMENT_UPLOAD,
                    entity_type=AUDIT_ENTITY,
                    entity_id=document.id,
                    entity_code=file_name[:64],
                    summary=f"Uploaded {file_name} ({size_bytes} bytes)",
                    user_id=self.tenant.user_id,
                    new_values={
                        "file_name": file_name,
                        "mime_type": mime_type,
                        "size_bytes": size_bytes,
                        "checksum_sha256": checksum,
                        "category": payload.category,
                        "is_public": payload.is_public,
                        "expires_at": document.expires_at,
                    },
                    context={
                        "entity_type": payload.entity_type,
                        "entity_id": payload.entity_id,
                    },
                )
        except Exception:
            # The row never committed, so nothing will ever reference these
            # bytes. Removing them here is the only chance to do so cheaply.
            await _discard(destination)
            raise

        log.info(
            "documents.uploaded",
            document_id=str(document.id),
            category=str(payload.category),
            size_bytes=size_bytes,
            mime_type=mime_type,
        )
        return DocumentRead.model_validate(document)

    async def _write_stream(
        self, upload_file: UploadFile, destination: Path, *, first_chunk: bytes
    ) -> tuple[int, str]:
        """Copy the upload to disk, hashing and measuring as it goes.

        The size ceiling is checked against the running total on every chunk and
        the copy is abandoned the moment it is crossed, so an oversized upload
        costs one chunk beyond the limit rather than the whole file. The
        SHA-256 is computed from the same pass - re-reading the file afterwards
        would double the I/O for a value we already have in hand.
        """
        limit = settings.max_upload_size_bytes
        digest = hashlib.sha256()
        total = 0

        await anyio.to_thread.run_sync(_make_parent, destination)
        handle = await anyio.to_thread.run_sync(_open_write, destination)
        try:
            try:
                chunk = first_chunk
                while chunk:
                    total += len(chunk)
                    if total > limit:
                        raise self._too_large(total)
                    digest.update(chunk)
                    await anyio.to_thread.run_sync(handle.write, chunk)
                    chunk = await upload_file.read(CHUNK_SIZE)
            finally:
                await anyio.to_thread.run_sync(handle.close)
        except BaseException:
            # BaseException, not Exception: a cancelled request must not leave a
            # half-written file behind either.
            await _discard(destination)
            raise

        return total, digest.hexdigest()

    @staticmethod
    def _too_large(observed: int) -> FileValidationError:
        limit = settings.max_upload_size_bytes
        return FileValidationError(
            f"The file exceeds the {settings.max_upload_size_mb} MB upload limit.",
            code=ErrorCode.FILE_TOO_LARGE,
            status_code=413,
            details={"max_bytes": limit, "observed_bytes_at_least": observed},
        )

    # --- reads --------------------------------------------------------------

    async def list_documents(
        self,
        filters: DocumentFilters,
        params: PageParams,
        sort: SortParams,
        *,
        principal: Principal | None = None,
    ) -> tuple[list[DocumentRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort, now=utcnow(), principal=principal)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [DocumentRead.model_validate(row) for row in rows], pagination

    async def get_document(
        self, document_id: uuid.UUID, *, principal: Principal | None = None
    ) -> DocumentRead:
        return DocumentRead.model_validate(await self._load_live(document_id, principal=principal))

    async def _load_live(
        self, document_id: uuid.UUID, *, principal: Principal | None = None
    ) -> Document:
        document = await self.repo.get_live(document_id, utcnow(), principal=principal)
        if document is None:
            # Identical for "does not exist", "belongs to another company" and
            # "has expired". Distinguishing them would let a caller enumerate
            # another tenant's ids by watching which error comes back.
            raise NotFoundError("Document", document_id)
        return document

    async def open_download(
        self, document_id: uuid.UUID, *, principal: Principal | None = None
    ) -> DocumentDownload:
        """Locate the bytes of one document for streaming.

        No audit entry: this is a read, and writing a row on every GET would put
        an insert on the hottest path in the module. The request log already
        carries who fetched what.
        """
        document = await self._load_live(document_id, principal=principal)
        path = resolve_storage_path(document.storage_key)

        if not await anyio.to_thread.run_sync(_exists, path):
            # The row survived but the bytes did not - a restore that missed the
            # storage volume, or a manual deletion. Loud in the log, plainly
            # "not found" to the caller.
            log.error(
                "documents.file_missing",
                document_id=str(document.id),
                storage_key=document.storage_key,
            )
            raise NotFoundError("Document content", document_id, code=ErrorCode.FILE_NOT_FOUND)

        return DocumentDownload(document=DocumentRead.model_validate(document), path=path)

    # --- deletion -----------------------------------------------------------

    async def delete_document(
        self,
        document_id: uuid.UUID,
        *,
        reason: str,
        principal: Principal | None = None,
    ) -> None:
        """Remove one document by id."""
        document = await self.repo.get_for_delete(document_id, principal=principal)
        if document is None:
            raise NotFoundError("Document", document_id)
        await self._delete(document, reason=reason)

    async def _delete(self, document: Document, *, reason: str) -> None:
        """Audit and drop the row, then unlink the file.

        Takes a loaded instance rather than an id so the purge sweep does not
        re-select every row it has already read (section 32).

        Deleting an expired document is legal on purpose - a lapsed export must
        still be removable - which is why the lifecycle declares an edge from
        EXPIRED as well as from AVAILABLE.
        """
        current = document_state(document)
        DOCUMENT_LIFECYCLE.assert_transition(current, DocumentState.DELETED)

        document_id = document.id
        file_name = document.file_name
        # Resolved *before* the row disappears, and through the same containment
        # guard as every other path in this module.
        path = resolve_storage_path(document.storage_key)
        before = snapshot(document, _AUDITED_FIELDS)

        async with transaction(self.session):
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DOCUMENT_DELETE,
                entity_type=AUDIT_ENTITY,
                entity_id=document_id,
                entity_code=file_name[:64],
                summary=f"Deleted document {file_name}",
                user_id=self.tenant.user_id,
                # The metadata is about to stop existing, so the audit row is the
                # only remaining record of what was removed.
                old_values={"status": str(current), **before},
                new_values={"status": str(DocumentState.DELETED)},
                context={"reason": reason},
            )
            await self.repo.hard_delete(document)

        # Only now, with the metadata gone for good. A failure here leaves an
        # unreferenced file, which housekeeping can find; unlinking earlier would
        # risk a surviving row that points at nothing, which nothing can fix.
        await _discard(path)
        log.info("documents.deleted", document_id=str(document_id), reason=reason)

    async def purge_expired(self, *, limit: int = MAX_PURGE_BATCH) -> int:
        """Delete one batch of lapsed documents. Returns how many were removed.

        Written for a scheduled sweep rather than a request: the batch is bounded
        so no single transaction grows long, and the caller loops until it
        returns 0.
        """
        expired = await self.repo.list_expired(utcnow(), limit=limit)
        for document in expired:
            await self._delete(document, reason=PURGE_REASON)
        if expired:
            log.info("documents.purged_expired", count=len(expired))
        return len(expired)


__all__ = [
    "AUDIT_ENTITY",
    "BLOCKED_SIGNATURES",
    "CATEGORY_DIRECTORY",
    "CHUNK_SIZE",
    "DEFAULT_RETENTION",
    "DOCUMENT_LIFECYCLE",
    "DOWNLOAD_HEADERS",
    "HEADER_BYTES",
    "KNOWN_TYPES",
    "PURGE_REASON",
    "DocumentDownload",
    "DocumentService",
    "DocumentState",
    "FileTypeRule",
    "MagicSignature",
    "assert_content_matches",
    "assert_upload_allowed",
    "build_storage_key",
    "content_disposition",
    "default_expiry",
    "document_state",
    "normalise_mime_type",
    "resolve_storage_path",
    "sanitise_file_name",
    "stream_file",
]
