"""videos: import from a link (source_url, download_progress, 'downloading' status)

Revision ID: c41f7a2d9e10
Revises: 88c6a9ee667f
Create Date: 2026-10-20 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c41f7a2d9e10"
down_revision: str | None = "88c6a9ee667f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE videostatus ADD VALUE IF NOT EXISTS 'downloading'")
    op.add_column("videos", sa.Column("source_url", sa.String(length=2048), nullable=True))
    op.add_column("videos", sa.Column("download_progress", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("videos", "download_progress")
    op.drop_column("videos", "source_url")
    # Postgres can't drop a value from an enum type; an unused 'downloading'
    # left in videostatus is harmless.
