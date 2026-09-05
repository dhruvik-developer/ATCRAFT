"""Storage-key generation and the path-traversal guard.

The guard is lexical and runs before any filesystem call, so these tests need no
temporary directory: they assert on the decision, not on what the OS would have
done with it. That is the point of writing it lexically - the check is provable
here rather than dependent on a particular filesystem's behaviour.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import pytest

from src.core.config import settings
from src.core.errors import ErrorCode, FileValidationError
from src.modules.documents.models import DocumentCategory
from src.modules.documents.service import (
    CATEGORY_DIRECTORY,
    KNOWN_TYPES,
    STORAGE_KEY_UNUSABLE,
    build_storage_key,
    resolve_storage_path,
)

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 3, 9, 11, 30, tzinfo=UTC)
COMPANY_ID = uuid.UUID("018f0000-0000-7000-8000-000000000001")

STORAGE_KEY_PATTERN = re.compile(
    r"^(documents|reports|attachments|exports|temp)/"
    r"[0-9a-f-]{36}/\d{4}/\d{2}/[0-9a-f-]{36}\.[a-z0-9]+$"
)


class TestStorageKeyGeneration:
    def test_shape(self) -> None:
        key = build_storage_key(
            category=DocumentCategory.ATTACHMENT,
            company_id=COMPANY_ID,
            rule=KNOWN_TYPES["application/pdf"],
            moment=MOMENT,
            file_id=uuid.UUID("018f0000-0000-7000-8000-0000000000aa"),
        )
        assert key == (
            "attachments/018f0000-0000-7000-8000-000000000001/2026/03/"
            "018f0000-0000-7000-8000-0000000000aa.pdf"
        )
        assert STORAGE_KEY_PATTERN.match(key)

    @pytest.mark.parametrize("category", list(DocumentCategory))
    def test_every_category_maps_to_a_provisioned_directory(
        self, category: DocumentCategory
    ) -> None:
        # ``Settings.ensure_storage_dirs`` creates exactly this set; a category
        # pointing anywhere else would write into an unmanaged directory.
        assert CATEGORY_DIRECTORY[category] in {
            "documents",
            "reports",
            "attachments",
            "exports",
            "temp",
        }

    def test_extension_comes_from_the_verified_type_not_the_upload_name(self) -> None:
        # There is no filename parameter at all: an ``invoice.pdf.exe`` upload
        # verified as a PDF is stored as ``.pdf``, because the extension is a
        # function of the content type and nothing else.
        key = build_storage_key(
            category=DocumentCategory.DOCUMENT,
            company_id=COMPANY_ID,
            rule=KNOWN_TYPES["image/png"],
            moment=MOMENT,
            file_id=uuid.uuid4(),
        )
        assert key.endswith(".png")

    def test_two_uploads_in_the_same_millisecond_get_different_keys(self) -> None:
        keys = {
            build_storage_key(
                category=DocumentCategory.DOCUMENT,
                company_id=COMPANY_ID,
                rule=KNOWN_TYPES["application/pdf"],
                moment=MOMENT,
                file_id=uuid.uuid4(),
            )
            for _ in range(100)
        }
        assert len(keys) == 100

    def test_key_is_partitioned_by_tenant(self) -> None:
        other = uuid.uuid4()
        mine = build_storage_key(
            category=DocumentCategory.DOCUMENT,
            company_id=COMPANY_ID,
            rule=KNOWN_TYPES["application/pdf"],
            moment=MOMENT,
            file_id=uuid.uuid4(),
        )
        theirs = build_storage_key(
            category=DocumentCategory.DOCUMENT,
            company_id=other,
            rule=KNOWN_TYPES["application/pdf"],
            moment=MOMENT,
            file_id=uuid.uuid4(),
        )
        assert str(COMPANY_ID) in mine
        assert str(COMPANY_ID) not in theirs


class TestPathTraversalGuard:
    @pytest.mark.parametrize(
        "key",
        [
            "../../etc/passwd",
            "documents/../../../etc/passwd",
            "/etc/passwd",
            "//etc/passwd",
            "C:\\Windows\\win.ini",
            "\\\\server\\share\\secret",
            "documents\\..\\..\\secret",
            "documents/..",
            "..",
            ".",
            "./documents/x.pdf",
            "documents//x.pdf",
            "documents/x.pdf\x00.png",
            "",
            "   ",
        ],
    )
    def test_escaping_or_malformed_keys_are_refused(self, key: str) -> None:
        with pytest.raises(FileValidationError) as exc:
            resolve_storage_path(key)
        # FILE_NOT_FOUND, not a "type not allowed": from the caller's side an
        # unusable key is a missing file, and saying more describes the layout.
        assert exc.value.code is ErrorCode.FILE_NOT_FOUND
        # And 404 to match: the key is server-generated, so a caller who only
        # sent a document id has not made a malformed request. The message
        # must not name the internal concept either.
        assert exc.value.status_code == 404
        assert exc.value.message == STORAGE_KEY_UNUSABLE
        assert "key" not in exc.value.message.lower()

    def test_a_generated_key_resolves_inside_the_storage_root(self) -> None:
        key = build_storage_key(
            category=DocumentCategory.EXPORT,
            company_id=COMPANY_ID,
            rule=KNOWN_TYPES["text/csv"],
            moment=MOMENT,
            file_id=uuid.uuid4(),
        )
        resolved = resolve_storage_path(key)
        assert resolved.is_relative_to(settings.storage_path)
        assert resolved.suffix == ".csv"
        assert resolved.parent.name == "03"

    def test_a_sibling_directory_sharing_the_root_prefix_is_refused(self) -> None:
        # ``/srv/storage-evil`` starts with ``/srv/storage`` as a string but is
        # not inside it - a containment check written with startswith would let
        # this through.
        sibling = f"../{settings.storage_path.name}-evil/x.pdf"
        with pytest.raises(FileValidationError):
            resolve_storage_path(sibling)

    def test_the_root_itself_is_not_a_valid_target(self) -> None:
        with pytest.raises(FileValidationError):
            resolve_storage_path(str(settings.storage_path))
