"""audio_tracks.bucket_copy (the track's copy in the bucket: "Upload to bucket")

Revision ID: d4b8e1f6a2c9
Revises: c8f2a4e6b1d3
Create Date: 2026-10-29 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4b8e1f6a2c9"
down_revision: str | None = "c8f2a4e6b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audio_tracks", sa.Column("bucket_copy", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("audio_tracks", "bucket_copy")
