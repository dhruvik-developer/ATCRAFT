"""shift master, shift roster, and the shift references on production

Revision ID: 0ac18597894f
Revises: 948902066228
Created: 2026-10-12 09:00:00.000000+00:00

A shift was free text typed onto a work order or a booking ("A", "a", "A "), with
no times, no roster and no way to say who was meant to be where. This revision
adds the two routing tables and points production at the first of them:

* ``shifts`` - a tenant master of named wall-clock windows (code, name, start and
  end time, a derived crosses-midnight flag, break minutes, ordering, active).
* ``shift_assignments`` - the roster: one operator on one shift at one work
  centre, optionally one machine, on one local date. The UTC window is stored as
  a snapshot, and two partial ``EXCLUDE USING gist`` constraints refuse a second
  PLANNED window for the same operator, or the same machine, that overlaps it.
  ``btree_gist`` supplies the ``uuid WITH =`` operator class; it is created here
  if absent, as the initial schema and ``7f3a91c4e5d2`` already do.
* ``work_orders.shift_id`` and ``production_entries.shift_id`` - nullable
  references to the master. The existing ``shift`` text columns stay, as a code
  snapshot the service writes whenever an id is set.

There is deliberately no backfill. A shift master row needs times, and the text
on existing rows carries none; inventing them would be inventing a business
rule. Legacy rows keep their text and a NULL ``shift_id``, and reports group by
the text, so old and new rows sit together (docs/assumptions.md §B2-3).

Review (master rules section 67):

* Locking impact:  the two ``CREATE TABLE`` statements take no lock anything
                   else contends for. ``ADD COLUMN ... NULL`` on ``work_orders``
                   and ``production_entries`` is catalogue-only in PostgreSQL
                   11+ - no rewrite, no scan - but takes a brief ACCESS
                   EXCLUSIVE lock that queues behind a long transaction on the
                   table. The two foreign keys are added ``NOT VALID`` and
                   validated separately: ``VALIDATE CONSTRAINT`` takes only SHARE
                   UPDATE EXCLUSIVE and blocks neither reads nor writes, where a
                   validating ``ADD CONSTRAINT`` would hold SHARE ROW EXCLUSIVE
                   on both tables for a full scan. Every new column is NULL, so
                   the validation finds nothing to check.
* Data migration:  none, on purpose - see above.
* Rollback:        ``downgrade()`` drops the two indexes, the two foreign keys
                   and the two ``shift_id`` columns, then both new tables. The
                   ``shift`` text columns are untouched by this revision, so the
                   code snapshots written while it was applied survive the
                   rollback; the roster rows and the master do not.
* Production size: the new tables start empty. The two index builds on the
                   existing tables read every row of ``work_orders`` and
                   ``production_entries`` once - both are plant-sized, tens of
                   thousands of rows, so seconds - and block writes to them for
                   that time; ``CREATE INDEX CONCURRENTLY`` cannot run inside the
                   migration transaction.
* Index creation:  on ``shifts`` - the primary key, ``uq_shifts_company_id_code``,
                   the base-class indexes and the partial picker index; on
                   ``shift_assignments`` - the primary key, the base-class
                   indexes, the two GiST exclusion indexes, the day-board grid
                   index and the per-operator index; ``ix_work_orders_shift`` and
                   ``ix_production_entries_shift`` on the existing tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0ac18597894f"
down_revision: str | None = "948902066228"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base_columns() -> list[sa.Column[object]]:
    """``id`` / ``company_id`` / timestamps / actors, as ``TenantEntity`` declares them."""
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
    ]


def _base_constraints(table: str) -> list[sa.SchemaItem]:
    return [
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f(f"fk_{table}_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f(f"fk_{table}_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f(f"fk_{table}_updated_by_id_users"),
            ondelete="SET NULL",
        ),
    ]


def _window() -> sa.ColumnElement[object]:
    return sa.func.tstzrange(
        sa.column("starts_at"), sa.column("ends_at"), sa.literal_column("'[)'")
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "shifts",
        *_base_columns(),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column(
            "crosses_midnight", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("break_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_base_constraints("shifts"),
        sa.UniqueConstraint("company_id", "code", name="uq_shifts_company_id_code"),
        sa.CheckConstraint(
            "(crosses_midnight AND end_time <= start_time) "
            "OR (NOT crosses_midnight AND end_time > start_time)",
            name=op.f("ck_shifts_midnight_flag_matches_times"),
        ),
        sa.CheckConstraint(
            "break_minutes >= 0 AND break_minutes < 1440",
            name=op.f("ck_shifts_break_minutes_bounded"),
        ),
    )
    op.create_index("ix_shifts_company_id", "shifts", ["company_id"])
    op.create_index("ix_shifts_created_at", "shifts", ["created_at"])
    op.create_index("ix_shifts_deleted_at", "shifts", ["deleted_at"])
    op.create_index(
        "ix_shifts_company_picker",
        "shifts",
        ["company_id", "sort_order"],
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )

    op.create_table(
        "shift_assignments",
        *_base_columns(),
        sa.Column("roster_date", sa.Date(), nullable=False),
        sa.Column("shift_id", sa.Uuid(), nullable=False),
        sa.Column("work_center_id", sa.Uuid(), nullable=False),
        sa.Column("machine_id", sa.Uuid(), nullable=True),
        sa.Column("operator_user_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="PLANNED"),
        sa.Column("notes", sa.Text(), nullable=True),
        *_base_constraints("shift_assignments"),
        sa.ForeignKeyConstraint(
            ["shift_id"],
            ["shifts.id"],
            name=op.f("fk_shift_assignments_shift_id_shifts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["work_center_id"],
            ["work_centers.id"],
            name=op.f("fk_shift_assignments_work_center_id_work_centers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operator_user_id"],
            ["users.id"],
            name=op.f("fk_shift_assignments_operator_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["machine_id", "work_center_id"],
            ["machines.id", "machines.work_center_id"],
            name="fk_shift_assignments_machine_id_work_center_id_machines",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_shift_assignments_window_ordered")),
        postgresql.ExcludeConstraint(
            (sa.column("company_id"), "="),
            (sa.column("operator_user_id"), "="),
            (_window(), "&&"),
            using="gist",
            where=sa.text("status = 'PLANNED'"),
            name="ex_shift_assignments_operator_window",
        ),
        postgresql.ExcludeConstraint(
            (sa.column("company_id"), "="),
            (sa.column("machine_id"), "="),
            (_window(), "&&"),
            using="gist",
            where=sa.text("status = 'PLANNED' AND machine_id IS NOT NULL"),
            name="ex_shift_assignments_machine_window",
        ),
    )
    op.create_index("ix_shift_assignments_company_id", "shift_assignments", ["company_id"])
    op.create_index("ix_shift_assignments_created_at", "shift_assignments", ["created_at"])
    op.create_index(
        "ix_shift_assignments_grid",
        "shift_assignments",
        ["company_id", "roster_date", "work_center_id", "shift_id"],
    )
    op.create_index(
        "ix_shift_assignments_operator",
        "shift_assignments",
        ["company_id", "operator_user_id", "starts_at"],
    )

    for table, index_columns in (
        ("work_orders", ["company_id", "shift_id", "status"]),
        ("production_entries", ["company_id", "shift_id", "entry_date"]),
    ):
        op.add_column(table, sa.Column("shift_id", sa.Uuid(), nullable=True))
        # NOT VALID, then validated below: see the locking note above.
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_shift_id_shifts "
            "FOREIGN KEY (shift_id) REFERENCES shifts (id) ON DELETE RESTRICT NOT VALID"
        )
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT fk_{table}_shift_id_shifts")
        op.create_index(f"ix_{table}_shift", table, index_columns)


def downgrade() -> None:
    for table in ("production_entries", "work_orders"):
        op.drop_index(f"ix_{table}_shift", table_name=table)
        op.drop_constraint(f"fk_{table}_shift_id_shifts", table, type_="foreignkey")
        op.drop_column(table, "shift_id")

    op.drop_table("shift_assignments")
    op.drop_table("shifts")
