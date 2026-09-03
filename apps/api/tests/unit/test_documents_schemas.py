"""Schema-level rules for document upload and listing.

These are the constraints that keep an attachment findable and a retention date
meaningful, and they are enforced on the model rather than in the route so a
future caller that constructs the payload directly gets the same answers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.core.clock import utcnow
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.schemas import (
    MAX_RETENTION_DAYS,
    DocumentFilters,
    DocumentRead,
    DocumentUploadRequest,
)

pytestmark = pytest.mark.unit


class TestUploadRequest:
    def test_defaults_are_the_conservative_ones(self) -> None:
        payload = DocumentUploadRequest()
        assert payload.category is DocumentCategory.DOCUMENT
        # Not public unless someone says so (master rules section 44).
        assert payload.is_public is False
        assert payload.expires_at is None

    def test_entity_reference_is_normalised(self) -> None:
        entity_id = uuid.uuid4()
        payload = DocumentUploadRequest(entity_type="  Goods_Receipt  ", entity_id=entity_id)
        assert payload.entity_type == "goods_receipt"
        assert payload.entity_id == entity_id

    @pytest.mark.parametrize(
        ("entity_type", "entity_id"),
        [("goods_receipt", None), (None, uuid.uuid4()), ("   ", uuid.uuid4())],
    )
    def test_half_an_entity_reference_is_refused(
        self, entity_type: str | None, entity_id: uuid.UUID | None
    ) -> None:
        # A file attached to a type but no id is filed against nothing findable.
        with pytest.raises(ValidationError, match="must be supplied together"):
            DocumentUploadRequest(entity_type=entity_type, entity_id=entity_id)

    @pytest.mark.parametrize(
        "entity_type", ["Goods Receipt", "goods-receipt", "1_receipt", "goods.receipt", "../etc"]
    )
    def test_entity_type_must_be_a_module_noun(self, entity_type: str) -> None:
        with pytest.raises(ValidationError, match="snake_case"):
            DocumentUploadRequest(entity_type=entity_type, entity_id=uuid.uuid4())

    def test_naive_expiry_is_refused(self) -> None:
        # A naive timestamp would be compared against UTC and silently shift the
        # retention date by the server's offset (section 28).
        with pytest.raises(ValidationError, match="timezone"):
            DocumentUploadRequest(expires_at=datetime(2030, 1, 1))  # noqa: DTZ001

    def test_past_expiry_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="future"):
            DocumentUploadRequest(expires_at=utcnow() - timedelta(minutes=1))

    def test_absurd_retention_is_refused(self) -> None:
        with pytest.raises(ValidationError, match=str(MAX_RETENTION_DAYS)):
            DocumentUploadRequest(expires_at=utcnow() + timedelta(days=MAX_RETENTION_DAYS + 2))

    def test_a_reasonable_retention_is_accepted(self) -> None:
        moment = utcnow() + timedelta(days=30)
        assert DocumentUploadRequest(expires_at=moment).expires_at == moment


class TestFilters:
    def test_expired_documents_are_hidden_by_default(self) -> None:
        assert DocumentFilters().include_expired is False

    def test_entity_id_alone_is_refused(self) -> None:
        # It would ignore the leading columns of the (company_id, entity_type,
        # entity_id) index and scan the table.
        with pytest.raises(ValidationError, match="together with entity_type"):
            DocumentFilters(entity_id=uuid.uuid4())

    def test_entity_type_alone_is_a_valid_filter(self) -> None:
        assert DocumentFilters(entity_type="goods_receipt").entity_id is None


class TestReadSchema:
    def test_the_storage_key_is_never_serialised(self) -> None:
        """The on-disk layout is not part of the API contract.

        Built from a real (unpersisted) ORM instance rather than a dict, because
        the risk being tested is exactly that ``from_attributes`` picks up a
        column nobody meant to publish.
        """
        document = Document(
            id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            file_name="qc-certificate.pdf",
            storage_key="attachments/tenant/2026/06/secret-layout.pdf",
            mime_type="application/pdf",
            size_bytes=2048,
            checksum_sha256="a" * 64,
            category=DocumentCategory.ATTACHMENT,
            entity_type="goods_receipt",
            entity_id=uuid.uuid4(),
            uploaded_by_id=uuid.uuid4(),
            is_public=False,
            expires_at=None,
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
            updated_at=datetime(2026, 6, 1, tzinfo=UTC),
        )

        serialised = DocumentRead.model_validate(document).model_dump()

        assert "storage_key" not in serialised
        assert "secret-layout" not in str(serialised)
        assert serialised["file_name"] == "qc-certificate.pdf"
        assert serialised["checksum_sha256"] == "a" * 64
