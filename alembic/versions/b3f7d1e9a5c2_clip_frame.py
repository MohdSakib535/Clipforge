"""clips.frame (1080x1920 with the user's text above/below the picture)

Revision ID: b3f7d1e9a5c2
Revises: a4e8c2f6d1b9
Create Date: 2026-11-05 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3f7d1e9a5c2"
down_revision: str | None = "a4e8c2f6d1b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("clips", sa.Column("frame", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("clips", "frame")
