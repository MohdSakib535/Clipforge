"""briefs (the "Brief reels" tab) and reels.brief_id / reels.custom_style

Revision ID: a4e8c2f6d1b9
Revises: d5a1c9e7f3b2
Create Date: 2026-11-02 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4e8c2f6d1b9"
down_revision: str | None = "d5a1c9e7f3b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "briefs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("niche", sa.Text(), server_default="", nullable=False),
        sa.Column("rules", sa.Text(), server_default="", nullable=False),
        sa.Column("visual_style", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_briefs_user_id", "briefs", ["user_id"])
    op.add_column("reels", sa.Column("brief_id", sa.String(length=36), nullable=True))
    op.add_column("reels", sa.Column("custom_style", sa.Text(), nullable=True))
    op.create_index("ix_reels_brief_id", "reels", ["brief_id"])


def downgrade() -> None:
    op.drop_index("ix_reels_brief_id", table_name="reels")
    op.drop_column("reels", "custom_style")
    op.drop_column("reels", "brief_id")
    op.drop_index("ix_briefs_user_id", table_name="briefs")
    op.drop_table("briefs")
