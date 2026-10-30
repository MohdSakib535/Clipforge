"""reels.script_type (story | tips | facts) and reels.description (the
creator's own idea for the script: plot, flow, tone)

Revision ID: c8e4a2f6b1d9
Revises: b6d2f8a4c1e7
Create Date: 2026-10-30 18:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8e4a2f6b1d9"
down_revision: str | None = "b6d2f8a4c1e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Reels written before were all "tips" (hook, one point per scene, payoff);
    # new ones default to a story.
    op.add_column(
        "reels",
        sa.Column("script_type", sa.String(length=16), server_default="tips", nullable=False),
    )
    op.alter_column("reels", "script_type", server_default="story")
    op.add_column("reels", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("reels", "description")
    op.drop_column("reels", "script_type")
