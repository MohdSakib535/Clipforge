"""reels.bucket (the reel's copy in the bucket: "Upload to bucket")

Revision ID: c8f2a4e6b1d3
Revises: b7e3c9a5d1f2
Create Date: 2026-10-28 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8f2a4e6b1d3"
down_revision: str | None = "b7e3c9a5d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reels", sa.Column("bucket", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("reels", "bucket")
