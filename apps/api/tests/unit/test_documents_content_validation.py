"""Content-type verification: the check that stops a renamed executable.

No database, no filesystem. Every function under test takes bytes and returns a
decision, which is exactly why this logic was kept pure - the interesting cases
are byte patterns, and they should be exercisable without an upload.
"""

from __future__ import annotations

import pytest

from src.core.config import settings
from src.core.errors import ErrorCode, FileValidationError
from src.modules.documents.service import (
    KNOWN_TYPES,
    assert_content_matches,
    assert_upload_allowed,
    content_disposition,
    normalise_mime_type,
    sanitise_file_name,
)

pytestmark = pytest.mark.unit

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 16
WEBP = b"RIFF" + (100).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 16
XLSX = b"PK\x03\x04" + b"\x00" * 32
CSV = b"code,name,quantity\nRM-001,Steel rod,10\n"
# A Windows PE header. This is the payload the whole module exists to refuse.
WINDOWS_EXECUTABLE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _rule(mime_type: str):
    return KNOWN_TYPES[mime_type]


class TestMimeNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("application/pdf", "application/pdf"),
            ("APPLICATION/PDF", "application/pdf"),
            ("text/csv; charset=utf-8", "text/csv"),
            ("  image/png  ", "image/png"),
            (None, ""),
            ("", ""),
        ],
    )
    def test_normalises(self, raw: str | None, expected: str) -> None:
        assert normalise_mime_type(raw) == expected

    def test_parameterised_type_still_matches_the_allow_list(self) -> None:
        # A browser sending `text/csv; charset=utf-8` must not be rejected for
        # being more precise than the allow-list entry.
        assert normalise_mime_type("text/csv; charset=utf-8") in settings.allowed_mime_type_set


class TestAllowList:
    def test_disallowed_type_is_refused(self) -> None:
        with pytest.raises(FileValidationError) as exc:
            assert_upload_allowed("application/x-msdownload")
        assert exc.value.code is ErrorCode.FILE_TYPE_NOT_ALLOWED

    def test_missing_content_type_is_refused(self) -> None:
        with pytest.raises(FileValidationError):
            assert_upload_allowed("")

    def test_allowed_but_unverifiable_type_is_refused(self, monkeypatch) -> None:
        """Widening the allow-list must not silently disable the content check.

        An operator adding a type with no signature in KNOWN_TYPES would
        otherwise get a type that is accepted purely on the client's word.
        """
        monkeypatch.setattr(
            settings,
            "allowed_upload_mime_types",
            f"{settings.allowed_upload_mime_types},application/msword",
        )
        assert "application/msword" in settings.allowed_mime_type_set

        with pytest.raises(FileValidationError, match="cannot be content-verified"):
            assert_upload_allowed("application/msword")

    @pytest.mark.parametrize("mime_type", sorted(KNOWN_TYPES))
    def test_every_known_type_is_shipped_in_the_default_allow_list(self, mime_type: str) -> None:
        # The two tables are coupled on purpose; this catches a signature added
        # for a type nobody can actually upload, and vice versa.
        assert mime_type in settings.allowed_mime_type_set


class TestMagicByteVerification:
    @pytest.mark.parametrize(
        ("mime_type", "content"),
        [
            ("application/pdf", PDF),
            ("image/png", PNG),
            ("image/jpeg", JPEG),
            ("image/webp", WEBP),
            ("text/csv", CSV),
            (XLSX_MIME, XLSX),
        ],
    )
    def test_genuine_content_passes(self, mime_type: str, content: bytes) -> None:
        assert_content_matches(mime_type, content, _rule(mime_type))

    def test_executable_renamed_as_pdf_is_rejected(self) -> None:
        with pytest.raises(FileValidationError, match="executable or script"):
            assert_content_matches("application/pdf", WINDOWS_EXECUTABLE, _rule("application/pdf"))

    def test_executable_declared_as_csv_is_rejected(self) -> None:
        # The blocked-signature check runs before the per-type check, so a
        # binary cannot slip through on a type that has no magic number.
        with pytest.raises(FileValidationError, match="executable or script"):
            assert_content_matches("text/csv", WINDOWS_EXECUTABLE, _rule("text/csv"))

    @pytest.mark.parametrize(
        "content",
        [
            b"\x7fELF\x02\x01\x01\x00",
            b"#!/bin/sh\nrm -rf /\n",
            b"\xcf\xfa\xed\xfe\x07\x00\x00\x01",
            b"\x00asm\x01\x00\x00\x00",
        ],
    )
    def test_other_executable_formats_are_rejected(self, content: bytes) -> None:
        with pytest.raises(FileValidationError, match="executable or script"):
            assert_content_matches("application/pdf", content, _rule("application/pdf"))

    def test_png_declared_as_jpeg_is_rejected(self) -> None:
        with pytest.raises(FileValidationError, match="does not match the declared type"):
            assert_content_matches("image/jpeg", PNG, _rule("image/jpeg"))

    def test_riff_container_that_is_not_webp_is_rejected(self) -> None:
        # A WAV is also RIFF: checking only the first four bytes would accept it
        # as an image, which is precisely the mismatch this module refuses.
        wav = b"RIFF" + (100).to_bytes(4, "little") + b"WAVEfmt "
        with pytest.raises(FileValidationError):
            assert_content_matches("image/webp", wav, _rule("image/webp"))

    def test_empty_content_is_rejected(self) -> None:
        with pytest.raises(FileValidationError, match="empty"):
            assert_content_matches("application/pdf", b"", _rule("application/pdf"))

    def test_zip_that_is_not_office_still_passes_as_xlsx(self) -> None:
        # Honest about the limit of signature checking: XLSX is a ZIP, so any
        # ZIP satisfies it. That is why downloads are served as attachments with
        # nosniff rather than trusted to be safe to open inline.
        assert_content_matches(XLSX_MIME, b"PK\x03\x04arbitrary zip", _rule(XLSX_MIME))


class TestTextHeuristic:
    def test_utf8_csv_passes(self) -> None:
        assert_content_matches("text/csv", "código,quantité\n".encode(), _rule("text/csv"))

    def test_nul_byte_rejects(self) -> None:
        with pytest.raises(FileValidationError, match="not valid text"):
            assert_content_matches("text/csv", b"a,b\n\x00\x01binary", _rule("text/csv"))

    def test_invalid_utf8_rejects(self) -> None:
        with pytest.raises(FileValidationError, match="not valid text"):
            assert_content_matches("text/csv", b"a,b\n\xc3\x28rubbish", _rule("text/csv"))

    def test_multibyte_character_split_by_the_header_boundary_is_tolerated(self) -> None:
        # The header is cut at a fixed offset, so the last character may be
        # incomplete. Rejecting that would fail valid files at random.
        header = ("x" * 511).encode() + "é".encode()[:1]
        assert_content_matches("text/csv", header, _rule("text/csv"))


class TestFileNameSanitisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("/etc/shadow", "shadow"),
            ("C:\\Windows\\System32\\cmd.exe", "cmd.exe"),
            ("..\\..\\boot.ini", "boot.ini"),
            ("  quarterly report.pdf  ", "quarterly report.pdf"),
            ("résumé.pdf", "résumé.pdf"),
        ],
    )
    def test_strips_path_components(self, raw: str, expected: str) -> None:
        assert sanitise_file_name(raw) == expected

    def test_control_characters_are_removed(self) -> None:
        # A CR/LF would terminate the Content-Disposition header and let the
        # rest of the name be read as headers of its own.
        assert sanitise_file_name("invoice\r\nX-Injected: 1.pdf") == "invoiceX-Injected: 1.pdf"

    @pytest.mark.parametrize("raw", [None, "", "   ", "...", "/", "../.."])
    def test_empty_or_path_only_names_fall_back(self, raw: str | None) -> None:
        assert sanitise_file_name(raw) == "upload"

    def test_length_is_bounded_to_the_column(self) -> None:
        assert len(sanitise_file_name("a" * 5000)) == 255


class TestContentDisposition:
    def test_always_attachment(self) -> None:
        assert content_disposition("report.pdf").startswith('attachment; filename="report.pdf"')

    def test_non_ascii_name_is_carried_in_the_rfc6266_form(self) -> None:
        header = content_disposition("résumé.pdf")
        assert "filename*=UTF-8''r%C3%A9sum%C3%A9.pdf" in header
        # The plain form must stay ASCII or the header cannot be encoded.
        header.encode("ascii")

    def test_header_cannot_be_split_by_a_crafted_name(self) -> None:
        header = content_disposition('evil"\r\nSet-Cookie: a=b.pdf')
        assert "\r" not in header
        assert "\n" not in header
        # The quote is neutralised too, or it would close the quoted string
        # early and let the remainder be parsed as further parameters.
        assert header.count('"') == 2
