"""mrp requirement late supply

Revision ID: ad9f573e5e59
Revises: b7cc02046b2d
Created: 2026-09-02 00:00:00.000000+00:00

Persist the supply the planner deliberately did *not* net.

``mrp`` time-phases: a requirement is netted only against inbound supply landing
on or before its own ``earliest_required_date``, and the rest is carried on the
engine's ``Requirement`` as ``late_incoming_quantity`` /
``late_in_production_quantity``. Those two figures were computed on every run
and then dropped on the floor - ``mrp_requirements`` had no column for them, so
the only surviving trace was a count in ``mrp_runs.result_summary`` and an
English sentence in the run's warnings:

    "1 short item(s) have inbound supply inside the horizon that arrives AFTER
     it is needed ... E2E-DOOR-HANDLE (100.0000 arriving 2026-12-01, needed
     2026-09-12)"

A requirements table cannot render "100 arriving, six weeks late" per row by
parsing that. Three columns fix it, and the third - the date - is the one that
makes the other two actionable: "late" without "how late" tells a planner
nothing about whether to expedite the order or raise a new one. The engine
already knew the date; it was computed for the warning text and discarded with
the rest.

Review checklist (master rules section 67)
------------------------------------------

* Locking impact:      one ALTER TABLE on ``mrp_requirements`` taking ACCESS
                       EXCLUSIVE. The three ADD COLUMNs are metadata-only:
                       PostgreSQL 11+ stores a non-volatile column default in
                       ``pg_attribute`` instead of rewriting the heap, and
                       ``'0'`` and NULL are both constants. The four CHECKs each
                       cost one sequential scan.

                       Deliberately NOT split into ADD CONSTRAINT ... NOT VALID
                       plus VALIDATE CONSTRAINT. That pattern is worth having
                       only when the two halves commit separately: Alembic runs
                       this file in a single transaction (``env.py`` sets
                       ``transaction_per_migration=True``), and the ACCESS
                       EXCLUSIVE lock the ADD COLUMN takes is held until that
                       transaction commits regardless, so validating inside it
                       under a weaker lock buys nothing. If this table ever
                       grows to where the scan itself is the problem, the
                       constraints belong in their own revision using an
                       autocommit block - not in a NOT VALID here that would
                       read like protection while providing none.

* Data migration:      none, and this is a real limitation rather than a clean
                       bill of health. Rows written by runs that completed
                       before this revision get 0 / 0 / NULL, which is not
                       necessarily what those runs computed - the figures were
                       never stored and the purchase orders and jobs they were
                       derived from have moved since, so there is nothing to
                       backfill from. A plan is a snapshot document; the way to
                       get a current answer is to run the plan again. Note the
                       consequence: on a historic row, "no late supply" and "we
                       did not record it" both read as zero with a NULL date.
                       The alternative - nullable quantity columns, so the two
                       stay distinguishable - was rejected because every other
                       quantity on this table is NOT NULL DEFAULT 0 and pushing
                       a three-state figure into every consumer to describe runs
                       that are already history is the worse trade.

* Rollback:            downgrade() drops the four constraints and the three
                       columns. It loses the late-supply figures for every run
                       planned while the revision was applied; nothing else in
                       the schema references them, and the engine recomputes
                       them on the next run either way. Verified by
                       upgrade / downgrade / upgrade in
                       tests/integration/test_mrp_late_supply_migration.py.

* Production size:     one ALTER TABLE and four sequential scans of
                       ``mrp_requirements``, which holds one row per item per
                       planning run. Sub-second on any realistic history.

* Index creation:      none. No query filters on these columns - they are read
                       back with the row a shortage report already selected, and
                       the run-level count that *is* filtered on lives in
                       ``mrp_runs.result_summary``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ad9f573e5e59"
down_revision: str | None = "b7cc02046b2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mrp_requirements"

#: (name, expression) for each guarantee the columns carry. Written out rather
#: than reflected from the model so this revision keeps describing the schema it
#: built after the model moves on. The names are the *final* ones and are passed
#: through ``op.f`` at both call sites: ``env.py`` hands Alembic a
#: ``target_metadata`` that carries ``ck_%(table_name)s_%(constraint_name)s``,
#: so a bare name here would be emitted as
#: ``ck_mrp_requirements_ck_mrp_requirements_...`` - double-prefixed, then
#: silently truncated at 63 bytes, and never matchable again.
CHECKS: tuple[tuple[str, str], ...] = (
    ("ck_mrp_requirements_late_incoming_non_negative", "late_incoming_quantity >= 0"),
    (
        "ck_mrp_requirements_late_in_production_non_negative",
        "late_in_production_quantity >= 0",
    ),
    # "Late" means exactly one thing: it lands after the date this requirement
    # needs it.
    (
        "ck_mrp_requirements_late_supply_arrives_after_required",
        "first_late_supply_date IS NULL OR first_late_supply_date > earliest_required_date",
    ),
    # A quantity with no date is the half-fact these columns exist to replace.
    # The converse is deliberately not asserted: a tranche smaller than the 4dp
    # quantum rounds to zero while still being a real, dated arrival.
    (
        "ck_mrp_requirements_late_quantity_names_its_date",
        "late_incoming_quantity + late_in_production_quantity = 0 "
        "OR first_late_supply_date IS NOT NULL",
    ),
)


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "late_incoming_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "late_in_production_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(TABLE, sa.Column("first_late_supply_date", sa.Date(), nullable=True))
    for name, expression in CHECKS:
        op.create_check_constraint(op.f(name), TABLE, expression)


def downgrade() -> None:
    for name, _expression in reversed(CHECKS):
        op.drop_constraint(op.f(name), TABLE, type_="check")
    op.drop_column(TABLE, "first_late_supply_date")
    op.drop_column(TABLE, "late_in_production_quantity")
    op.drop_column(TABLE, "late_incoming_quantity")
