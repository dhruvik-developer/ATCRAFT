"""Join the supply and quality migration histories.

This revision changes no schema or data. Downgrading it is also a no-op; each
parent migration remains independently reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "10f21bb9339c"
down_revision: str | Sequence[str] | None = ("d4e10a02bb02", "e5b240120002")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
