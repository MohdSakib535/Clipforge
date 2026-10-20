"""remove the LLM features: clip scores/reasons, job token counts, highlights stage

Revision ID: 9e1a4c7b2d58
Revises: 7c2e5b9d0f14
Create Date: 2026-10-20 21:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9e1a4c7b2d58"
down_revision: str | None = "7c2e5b9d0f14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("score", "hook_strength", "standalone", "reason"):
        op.drop_column("clips", column)
    op.drop_column("jobs", "llm_tokens_in")
    op.drop_column("jobs", "llm_tokens_out")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE jobstage RENAME VALUE 'finding_highlights' TO 'planning_clips'")


def downgrade() -> None:
    # The columns come back empty: the values dropped by upgrade() are gone.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE jobstage RENAME VALUE 'planning_clips' TO 'finding_highlights'")
    op.add_column(
        "jobs", sa.Column("llm_tokens_out", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "jobs", sa.Column("llm_tokens_in", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("clips", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column("clips", sa.Column("standalone", sa.Boolean(), nullable=True))
    op.add_column("clips", sa.Column("hook_strength", sa.Float(), nullable=True))
    op.add_column("clips", sa.Column("score", sa.Float(), nullable=True))
