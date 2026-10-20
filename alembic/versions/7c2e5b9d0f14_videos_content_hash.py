"""videos.content_hash (reuse a transcript for the same file); size_bytes -> bigint

Revision ID: 7c2e5b9d0f14
Revises: a9d3f6b1c204
Create Date: 2026-10-20 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7c2e5b9d0f14"
down_revision: str | None = "a9d3f6b1c204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("content_hash", sa.String(80), nullable=True))
    op.create_index("ix_videos_content_hash", "videos", ["content_hash"])
    # A 32-bit integer stops at 2.1 GB, below the 5 GB upload limit.
    op.alter_column("videos", "size_bytes", type_=sa.BigInteger(), existing_nullable=True)


def downgrade() -> None:
    op.alter_column("videos", "size_bytes", type_=sa.Integer(), existing_nullable=True)
    op.drop_index("ix_videos_content_hash", table_name="videos")
    op.drop_column("videos", "content_hash")
