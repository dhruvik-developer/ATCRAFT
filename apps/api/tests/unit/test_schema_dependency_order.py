"""Schema comparison must inspect every foreign key without unresolved cycles."""

from __future__ import annotations

import warnings

from sqlalchemy.exc import SAWarning
from sqlalchemy.schema import sort_tables_and_constraints

from src.core.models import Base
from src.core.registry import import_all_models


def test_metadata_dependency_order_keeps_all_foreign_keys() -> None:
    import_all_models()
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        ordered = Base.metadata.sorted_tables
        ddl = sort_tables_and_constraints(ordered, _warn_for_cycles=True)

    assert {table.name for table in ordered} == set(Base.metadata.tables)
    emitted = {constraint for _, constraints in ddl for constraint in constraints}
    declared = {
        constraint
        for table in Base.metadata.tables.values()
        for constraint in table.foreign_key_constraints
    }
    assert emitted == declared
    assert all(constraint.name for constraint in emitted)
