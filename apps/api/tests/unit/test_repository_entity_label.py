"""``get_or_404`` names a record the way a person would, not the way a class is.

``NotFoundError`` renders ``"{entity} not found: {id}"``. The base repository
used to pass ``self.model.__name__`` there, so a missing physical count was
reported as "PhysicalCount not found" while the same module's own messages said
"Physical count". The default is now the class name split into words in
sentence case, and a repository whose split is wrong - an acronym, or a name a
service and a state machine already share - sets ``entity_label`` instead.
"""

from __future__ import annotations

import uuid
from itertools import pairwise
from typing import Any

import pytest

from src.common.repository import BaseRepository
from src.core.deps import TenantContext
from src.core.errors import NotFoundError
from src.modules.bom.repository import BomLineRepository
from src.modules.customers.repository import CustomerAddressRepository
from src.modules.inventory.repository import PhysicalCountRepository
from src.modules.products.repository import UnitOfMeasureRepository, UomConversionRepository
from src.modules.quality.repository import QcInspectionRepository

pytestmark = pytest.mark.unit


class _NoRowSession:
    async def execute(self, statement: Any) -> Any:
        class _Result:
            def scalar_one_or_none(self) -> None:
                return None

        return _Result()


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


def _repository_for(class_name: str, label: str | None = None) -> type[BaseRepository[Any]]:
    """A repository over a stand-in model with the given class name."""
    model = type(class_name, (), {})
    attributes: dict[str, Any] = {"model": model}
    if label is not None:
        attributes["entity_label"] = label
    return type(f"{class_name}Repository", (BaseRepository,), attributes)


class TestTheDefaultSplit:
    @pytest.mark.parametrize(
        ("class_name", "expected"),
        [
            ("PhysicalCount", "Physical count"),
            ("CustomerAddress", "Customer address"),
            ("Item", "Item"),
            ("ItemVendorLink", "Item vendor link"),
            ("StockSerial", "Stock serial"),
            ("UomConversion", "Uom conversion"),
        ],
    )
    def test_a_class_name_becomes_sentence_case_words(self, class_name: str, expected: str) -> None:
        assert _repository_for(class_name)._entity_label() == expected

    def test_an_explicit_label_wins(self) -> None:
        assert _repository_for("UomConversion", "Unit conversion")._entity_label() == (
            "Unit conversion"
        )

    def test_a_name_with_no_capital_is_returned_as_is(self) -> None:
        assert _repository_for("widget")._entity_label() == "widget"

    async def test_get_or_404_uses_the_label(self) -> None:
        missing = uuid.uuid4()

        class _Repository(_repository_for("PhysicalCount")):  # type: ignore[misc]
            # The stand-in model is not mapped, so the read is stubbed; what is
            # under test is the message the base ``get_or_404`` composes.
            async def get(self, entity_id: uuid.UUID, *, include_deleted: bool = False) -> None:
                return None

        with pytest.raises(NotFoundError) as excinfo:
            await _Repository(_NoRowSession(), _tenant()).get_or_404(missing)  # type: ignore[arg-type]
        assert excinfo.value.message == f"Physical count not found: {missing}"


class TestTheRealRepositories:
    """The labels the modules set, and the defaults they rely on."""

    @pytest.mark.parametrize(
        ("repository", "entity"),
        [
            (PhysicalCountRepository, "Physical count"),
            (CustomerAddressRepository, "Customer address"),
            (BomLineRepository, "BOM line"),
            (QcInspectionRepository, "QC inspection"),
            (UnitOfMeasureRepository, "Unit of measure"),
            (UomConversionRepository, "Unit conversion"),
        ],
        ids=["physical-count", "customer-address", "bom-line", "qc-inspection", "uom", "uom-conv"],
    )
    async def test_get_or_404_names_the_record_for_a_person(
        self, repository: Any, entity: str
    ) -> None:
        missing = uuid.uuid4()
        with pytest.raises(NotFoundError) as excinfo:
            await repository(_NoRowSession(), _tenant()).get_or_404(missing)
        assert excinfo.value.message == f"{entity} not found: {missing}"

    def test_no_repository_still_answers_with_a_class_name(self) -> None:
        """Every concrete repository's label reads as words, not as an identifier.

        The tell of a raw class name is a lower-case letter followed directly
        by a capital ("PhysicalCount"); a label built by the split, or set by
        hand, never has one.
        """
        import importlib
        import pkgutil

        import src.modules

        offenders: list[str] = []
        for info in pkgutil.iter_modules(src.modules.__path__):
            try:
                module = importlib.import_module(f"src.modules.{info.name}.repository")
            except ModuleNotFoundError:
                continue
            for name, value in vars(module).items():
                if (
                    isinstance(value, type)
                    and issubclass(value, BaseRepository)
                    and value is not BaseRepository
                    and "model" in vars(value)
                    and getattr(value, "__module__", "") == module.__name__
                ):
                    label = value._entity_label()
                    if any(a.islower() and b.isupper() for a, b in pairwise(label)):
                        offenders.append(f"{module.__name__}.{name}: {label!r}")
        assert offenders == []
