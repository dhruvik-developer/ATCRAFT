"""No identifier may be longer than PostgreSQL will store.

PostgreSQL's ``NAMEDATALEN`` is 64, so an identifier is truncated to 63 bytes -
silently, with no error and no warning. Nothing about the running system looks
wrong afterwards: the constraint exists, it is enforced, it works. What breaks is
the *name*. The model says ``ck_purchase_order_lines_something_rather_long``, the
catalogue says ``ck_purchase_order_lines_something_rather_l_4f2a``, and the two
never agree again.

That divergence is not cosmetic, because Alembic matches some constraints by
name. ``alembic/ddl/_autogen.py`` builds a check constraint's signature from the
*raw* metadata name (``_ck_constraint_sig.__init__``: ``self._sig =
(self.name,)``), while the connection side of the comparison carries the
truncated name PostgreSQL actually stored. Autogenerate therefore sees one
constraint to drop and one to add, on every run, for ever - and the generated
migration does not fix it, because applying it re-truncates to the same value.
Three check constraint names in this schema hit exactly that and had to be
shortened by hand. Nothing stopped the fourth. This module is what stops it.

The guard walks the whole of ``Base.metadata`` - every module's models, loaded
through :func:`tests.schema_support.prepare_metadata` - and checks the name that
will actually be emitted, not the short label written in the model. Those differ:
the ``ck`` entry in :data:`src.core.models.NAMING_CONVENTION` is
``ck_%(table_name)s_%(constraint_name)s``, so a model that writes
``CheckConstraint(..., name="quantity_positive")`` on a table called
``production_order_components`` is really declaring a 44-character identifier.
SQLAlchemy renders the convention when the constraint is attached to its table,
so the name held in the metadata is already the final one;
``test_no_identifier_is_truncated_by_the_dialect`` pins that down against the
PostgreSQL dialect itself rather than taking it on trust.

No database is needed and none is opened - this is metadata arithmetic, and it
belongs in the fast loop.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.compiler import IdentifierPreparer
from sqlalchemy.sql.schema import Constraint, Index

from src.core.models import Base
from tests.schema_support import prepare_metadata

pytestmark = pytest.mark.unit

#: PostgreSQL stores identifiers in a ``name`` column of ``NAMEDATALEN`` (64)
#: bytes including the terminator, so 63 bytes is the whole budget. Bytes, not
#: characters: the limit is on the encoded form, and a non-ASCII identifier
#: spends more than one byte per character.
MAX_IDENTIFIER_BYTES = 63

# Foreign keys reach into modules that land in later waves, and the metadata has
# to be complete before it can be walked. ``prepare_metadata`` imports every
# module that exists and registers a one-column stand-in for whatever foreign
# key targets are still missing.
prepare_metadata()


# --- The pre-existing exceptions ---------------------------------------------
#
# Twelve foreign key names were already over the limit when this guard was
# written. Every one of them is generated, not authored: the ``fk`` convention is
# ``fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s``, and two long
# table names either side of a long column name overflow it without anybody
# typing a long string. They are recorded here rather than silently skipped, so
# that a *thirteenth* still fails the test.
#
# They are survivable where a check constraint is not, and the difference is
# worth knowing before anyone decides how urgent this is. Alembic compares
# foreign keys structurally - ``_compare_foreign_keys`` keys both sides on
# ``_fk_constraint_sig.unnamed`` / ``unnamed_no_options``, which is
# (source table, source columns, target table, target columns, options) with the
# name left out - so a truncated foreign key name does not produce the endless
# drop-and-recreate that a truncated check constraint name does. Indexes and
# unique constraints are compared by their *final* name on both sides
# (``md_name_to_sql_name`` -> ``_get_constraint_final_name``, which truncates),
# so they are safe from that failure too. Check constraints are the ones matched
# on the raw metadata name, and there are none over the limit.
#
# This ledger may only ever shrink. Fixing an entry means giving the constraint
# an explicit shorter name in its model and renaming it in a migration, then
# deleting the line here - ``test_the_ledger_has_no_stale_entries`` fails if a
# name listed here is no longer an over-long identifier, so it cannot rot.
# Do not add to it. An over-long identifier in new work is a defect to fix
# before merge, and this test is the thing that tells you about it.
KNOWN_OVERLONG_FOREIGN_KEYS = frozenset(
    {
        "fk_goods_receipt_lines_purchase_order_line_id_purchase_order_lines",
        "fk_job_work_challan_lines_job_work_order_line_id_job_work_order_lines",
        "fk_job_work_receipt_lines_job_work_order_line_id_job_work_order_lines",
        "fk_material_issue_lines_component_id_production_order_components",
        "fk_production_order_components_production_order_id_production_orders",
        "fk_purchase_order_lines_quotation_line_id_vendor_quotation_lines",
        "fk_purchase_order_lines_requisition_line_id_purchase_requisition_lines",
        "fk_purchase_requisition_lines_requisition_id_purchase_requisitions",
        "fk_purchase_return_lines_goods_receipt_line_id_goods_receipt_lines",
        "fk_vendor_invoice_lines_goods_receipt_line_id_goods_receipt_lines",
        "fk_vendor_invoice_lines_purchase_order_line_id_purchase_order_lines",
        "fk_vendor_quotations_converted_purchase_order_id_purchase_orders",
    }
)


def _identifiers() -> Iterator[tuple[str, str, str]]:
    """Every identifier the schema declares, as ``(table, kind, name)``.

    ``name`` is the rendered name - the one PostgreSQL will be asked to store.
    SQLAlchemy applies :data:`~src.core.models.NAMING_CONVENTION` in an
    ``after_parent_attach`` listener (``sqlalchemy/sql/naming.py``), so by the
    time a constraint is in the metadata its ``.name`` is already the convention
    expanded against the table and its columns. Reading it back here is
    therefore reading the real identifier, not the label in the model.
    """
    for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        yield table.name, "table", table.name
        for column in table.columns:
            yield table.name, "column", column.name
        for constraint in sorted(table.constraints, key=lambda c: str(c.name or "")):
            # A None name is a different defect - the ``ck`` convention needs an
            # explicit name and SQLAlchemy raises at DDL time without one - and
            # is not this test's business. There are none today.
            if constraint.name is not None:
                yield table.name, _kind_of(constraint), str(constraint.name)
        for index in sorted(table.indexes, key=lambda i: str(i.name or "")):
            if index.name is not None:
                yield table.name, "index", str(index.name)


def _kind_of(constraint: object) -> str:
    """``primary key`` / ``foreign key`` / ``unique`` / ``check`` for a message."""
    name = type(constraint).__name__.removesuffix("Constraint")
    return "".join(f" {ch.lower()}" if ch.isupper() else ch for ch in name).strip()


def _too_long(name: str) -> bool:
    return len(name.encode("utf-8")) > MAX_IDENTIFIER_BYTES


def _emitted_name(preparer: IdentifierPreparer, obj: Constraint | Index) -> str:
    """The identifier the PostgreSQL dialect would put in the DDL.

    Both entry points truncate over-long names to ``max_identifier_length`` and
    append a hash of the original, which is why the emitted name is the right
    thing to compare against rather than the length on its own.
    """
    if isinstance(obj, Index):
        return str(preparer.format_index(obj))
    return str(preparer.format_constraint(obj, _alembic_quote=False))


class TestIdentifierLength:
    def test_the_metadata_is_actually_loaded(self) -> None:
        """A guard over an empty metadata passes and protects nothing.

        ``prepare_metadata`` imports every module that exists; if that import
        ever silently stops finding them, the two tests below go green while
        checking nothing at all. The exact count is deliberately not pinned -
        modules land continuously - but the schema is large and cannot collapse
        to a handful of tables without something being badly wrong.
        """
        assert len(Base.metadata.tables) > 50, (
            f"only {len(Base.metadata.tables)} tables were loaded - "
            "prepare_metadata() is not importing the modules, so this guard "
            "is inspecting an almost-empty schema"
        )
        assert sum(1 for _ in _identifiers()) > 1000

    def test_every_identifier_fits_in_a_postgresql_name(self) -> None:
        """Tables, columns, and every constraint and index name, in bytes."""
        violations = [
            f"{name!r} ({len(name.encode('utf-8'))} bytes, "
            f"limit {MAX_IDENTIFIER_BYTES}) - {kind} on table {table!r}"
            for table, kind, name in _identifiers()
            if _too_long(name) and name not in KNOWN_OVERLONG_FOREIGN_KEYS
        ]
        assert not violations, (
            f"{len(violations)} identifier(s) exceed PostgreSQL's "
            f"{MAX_IDENTIFIER_BYTES}-byte limit and will be silently truncated, "
            "leaving the model and the catalogue permanently disagreeing:\n  "
            + "\n  ".join(sorted(violations))
        )

    def test_no_identifier_is_truncated_by_the_dialect(self) -> None:
        """The same rule, stated as the consequence rather than the count.

        This is the check that keeps :data:`MAX_IDENTIFIER_BYTES` honest: rather
        than trusting the number, it asks the PostgreSQL dialect what name it
        would emit and requires that to be the name the model declares. It is
        also how Alembic asks the question
        (``sqla_compat._get_constraint_final_name``), so a disagreement here is
        exactly a disagreement autogenerate would go on to report.
        """
        preparer = postgresql.dialect().identifier_preparer
        truncated: list[str] = []
        for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
            named: list[Constraint | Index] = [*table.constraints, *table.indexes]
            for obj in named:
                if obj.name is None:
                    continue
                declared = str(obj.name)
                if declared in KNOWN_OVERLONG_FOREIGN_KEYS:
                    continue
                emitted = _emitted_name(preparer, obj)
                if emitted != declared:
                    truncated.append(
                        f"{declared!r} ({len(declared.encode('utf-8'))} bytes) on table "
                        f"{table.name!r} reaches PostgreSQL as {emitted!r}"
                    )
        assert not truncated, (
            "the model and the database would disagree about these names:\n  "
            + "\n  ".join(sorted(truncated))
        )

    def test_the_ledger_has_no_stale_entries(self) -> None:
        """Every exception listed must still be a real, still-over-long name.

        Without this the ledger silently becomes a list of names nobody can find,
        and a future identifier that happens to match one of them is waved
        through. Shortening a name means deleting its line here.
        """
        overlong = {name for _table, _kind, name in _identifiers() if _too_long(name)}
        stale = sorted(KNOWN_OVERLONG_FOREIGN_KEYS - overlong)
        assert not stale, (
            "KNOWN_OVERLONG_FOREIGN_KEYS lists identifiers that are no longer "
            "over the limit (or no longer exist). Delete these lines - the "
            "ledger may only ever shrink:\n  " + "\n  ".join(stale)
        )
