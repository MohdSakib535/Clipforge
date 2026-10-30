"""reels.style (how the pictures are drawn: cinematic, realistic, anime, 3d)

Revision ID: b6d2f8a4c1e7
Revises: e7a3c5b9d2f4
Create Date: 2026-10-30 17:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6d2f8a4c1e7"
down_revision: str | None = "e7a3c5b9d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reels",
        sa.Column("style", sa.String(length=16), server_default="cinematic", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("reels", "style")
