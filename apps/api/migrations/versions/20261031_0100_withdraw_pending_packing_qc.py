"""Preserve an explicit, undecided withdrawal of an unstarted packing check.

Revision ID: 20261031_0100
Revises: 20261030_0100
Create Date: 2026-10-25
"""

from __future__ import annotations

from alembic import op

revision = "20261031_0100"
down_revision = "20261030_0100"
branch_labels = None
depends_on = None

_OPEN = "'PENDING', 'IN_PROGRESS'"
_STATES = "'PENDING', 'IN_PROGRESS', 'ACCEPTED', 'REWORK', 'REJECTED', 'HOLD', 'SCRAP', 'CLOSED'"


def _replace(name: str, expression: str) -> None:
    constraint = op.f(f"ck_qc_inspections_{name}")
    op.drop_constraint(constraint, "qc_inspections", type_="check")
    op.create_check_constraint(constraint, "qc_inspections", expression)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _replace("status_vocabulary", f"status IN ({_STATES}, 'WITHDRAWN')")
    _replace(
        "status_and_result_agree",
        f"(status IN ({_OPEN}, 'WITHDRAWN') AND result = 'PENDING') "
        f"OR (status NOT IN ({_OPEN}, 'WITHDRAWN') AND result <> 'PENDING')",
    )
    _replace(
        "decision_is_attributed",
        f"status IN ({_OPEN}, 'WITHDRAWN') OR (inspector_user_id IS NOT NULL AND inspected_at IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_qc_inspections_withdrawal_has_no_verdict"),
        "qc_inspections",
        "status <> 'WITHDRAWN' OR (qc_stage = 'FINAL' AND reference_type = 'packing_order' "
        "AND accepted_quantity + rejected_quantity + rework_quantity + hold_quantity "
        "+ scrap_quantity = 0 AND serial_dispositions = '{}'::jsonb)",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM qc_inspections WHERE status = 'WITHDRAWN') THEN
            RAISE EXCEPTION 'Withdrawn packing inspections exist; downgrade would discard their audit meaning';
        END IF;
    END $$""")
    op.drop_constraint(
        op.f("ck_qc_inspections_withdrawal_has_no_verdict"), "qc_inspections", type_="check"
    )
    _replace(
        "decision_is_attributed",
        f"status IN ({_OPEN}) OR (inspector_user_id IS NOT NULL AND inspected_at IS NOT NULL)",
    )
    _replace(
        "status_and_result_agree",
        f"(status IN ({_OPEN}) AND result = 'PENDING') OR (status NOT IN ({_OPEN}) AND result <> 'PENDING')",
    )
    _replace("status_vocabulary", f"status IN ({_STATES})")
