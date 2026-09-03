"""mrp per feed late supply dates

Revision ID: 66b1c0e2d1f4
Revises: 65bcadcc236b
Created: 2026-09-02 12:10:00.000000+00:00

Give each late-supply quantity its own arrival date.

Revision ``ad9f573e5e59`` persisted the supply the planner deliberately did not
net as three columns: ``late_incoming_quantity``,
``late_in_production_quantity`` and one ``first_late_supply_date``. One date for
two independent schedules. The engine computed that date as the earliest across
*both* feeds, so the column beside a quantity routinely described the other
feed's arrival:

    item BOLT-M8, needed 2026-07-15
      one purchase order    5000.0000 due 2026-11-12
      one released job         0.0001 due 2026-07-16

    late_incoming_quantity      = 5000.0000
    late_in_production_quantity =    0.0001
    first_late_supply_date      = 2026-07-16   <- the job's date

A requirements table rendering "5000 arriving 2026-07-16" states that five
thousand bolts land the day after they are needed, when they land four months
later. That is worse than the warning prose these columns replaced, because it
looks structured and authoritative - and the whole reason a client was moved off
parsing the prose was so the structured fields could be trusted individually.

Two nullable date columns fix it: ``first_late_incoming_date`` beside
``late_incoming_quantity`` and ``first_late_in_production_date`` beside
``late_in_production_quantity``. NULL means that feed excluded nothing - "no
late supply" and "late supply arriving today" are different facts and a sentinel
date would merge them, which is the same reasoning ``ad9f573e5e59`` gave for its
own nullable date.

``first_late_supply_date`` is **kept**, unchanged in meaning: the earliest date
anything at all lands, which is the run-level "could this be relieved without a
new order, and how soon" question and is what existing consumers already read.
The engine now derives it from the two per-feed dates rather than storing an
independent third answer, and ``ck_..._first_late_supply_is_the_earlier`` holds
the row to that. Purely additive: nothing that reads the three existing columns
changes behaviour.

Review checklist (master rules section 67)
------------------------------------------

* Locking impact:      one ALTER TABLE on ``mrp_requirements`` taking ACCESS
                       EXCLUSIVE. Both ADD COLUMNs are metadata-only - nullable
                       with no default rewrites no heap on any supported
                       PostgreSQL. The three CHECKs cost one sequential scan
                       each.

                       Not split into ADD CONSTRAINT ... NOT VALID plus
                       VALIDATE CONSTRAINT, for the reason ``ad9f573e5e59``
                       gives at length: ``env.py`` sets
                       ``transaction_per_migration=True``, so the ACCESS
                       EXCLUSIVE lock is held to the end of this transaction
                       regardless and validating inside it under a weaker lock
                       buys nothing.

* Data migration:      none, deliberately, and this is the interesting decision
                       in the revision. Rows written by earlier runs carry a
                       summary date and no per-feed dates.

                       Backfilling the ones with exactly one non-zero late
                       quantity - copy ``first_late_supply_date`` into that
                       feed's column - was considered and rejected. It is
                       *almost* a derivation: the summary is the earliest date
                       over the feeds that had a late tranche, and the other
                       feed's quantity being zero usually means it had none. But
                       ``ad9f573e5e59`` deliberately allows a real, dated
                       tranche whose quantity rounds to zero at four decimal
                       places, and in that case the summary date belongs to the
                       feed showing 0.0000. The backfill would then write a
                       date onto the wrong feed - inventing exactly the false
                       structured fact this revision exists to end, on a
                       historic row nobody can check. A NULL that reads "not
                       recorded" is the honest answer for a run planned before
                       the engine computed this, and a plan is a snapshot
                       document: re-running it is how a current answer is
                       obtained.

                       Consequence, stated rather than hidden: on a row planned
                       before this revision, a per-feed date of NULL does not
                       mean that feed is on time. Only rows from this revision
                       onward guarantee a date beside every late quantity, and
                       the CHECK below cannot forbid the older shape for the
                       same reason it could not be backfilled.

* Rollback:            downgrade() drops the three constraints and the two
                       columns, leaving the table exactly as ``ad9f573e5e59``
                       built it - including its own four constraints, which this
                       revision does not touch. It loses the per-feed dates for
                       every run planned while this revision was applied; the
                       engine recomputes them on the next run. Verified by
                       upgrade / downgrade / upgrade in
                       tests/integration/test_mrp_per_feed_late_dates_migration.py.

* Production size:     one ALTER TABLE and three sequential scans of
                       ``mrp_requirements`` - one row per item per planning run.
                       Sub-second on any realistic history.

* Index creation:      none. Nothing filters on these columns: they are read
                       back with a row the shortage report already selected, and
                       the run-level count that is filtered on lives in
                       ``mrp_runs.result_summary``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "66b1c0e2d1f4"
down_revision: str | None = "65bcadcc236b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mrp_requirements"

#: (name, expression) per guarantee. Written out rather than reflected from the
#: model so this revision keeps describing the schema it built after the model
#: moves on. The names are the *final* ones and go through ``op.f`` at both call
#: sites: ``env.py`` hands Alembic a ``target_metadata`` carrying
#: ``ck_%(table_name)s_%(constraint_name)s``, so a bare name here would be
#: emitted double-prefixed, truncated at 63 bytes, and never matchable again.
CHECKS: tuple[tuple[str, str], ...] = (
    # "Late" means one thing per feed too: it lands after the date this
    # requirement needs it. The summary date already asserts this, but it is the
    # *earlier* of the pair - it passing does not prove the later one did.
    (
        "ck_mrp_requirements_late_incoming_arrives_after_required",
        "first_late_incoming_date IS NULL "
        "OR first_late_incoming_date > earliest_required_date",
    ),
    (
        "ck_mrp_requirements_late_in_production_arrives_after_required",
        "first_late_in_production_date IS NULL "
        "OR first_late_in_production_date > earliest_required_date",
    ),
    # The summary is the earlier of the two feeds. Two descriptions of one fact
    # that can disagree is the defect this revision exists to fix, one level up;
    # this is that statement made unbreakable rather than merely intended.
    # LEAST ignores NULLs, so a single late feed reduces to that feed's date.
    # The first branch admits the pre-existing rows - see "Data migration".
    (
        "ck_mrp_requirements_first_late_supply_is_the_earlier",
        "(first_late_incoming_date IS NULL AND first_late_in_production_date IS NULL) "
        "OR first_late_supply_date "
        "= LEAST(first_late_incoming_date, first_late_in_production_date)",
    ),
)


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("first_late_incoming_date", sa.Date(), nullable=True))
    op.add_column(
        TABLE, sa.Column("first_late_in_production_date", sa.Date(), nullable=True)
    )
    for name, expression in CHECKS:
        op.create_check_constraint(op.f(name), TABLE, expression)


def downgrade() -> None:
    for name, _expression in reversed(CHECKS):
        op.drop_constraint(op.f(name), TABLE, type_="check")
    op.drop_column(TABLE, "first_late_in_production_date")
    op.drop_column(TABLE, "first_late_incoming_date")
