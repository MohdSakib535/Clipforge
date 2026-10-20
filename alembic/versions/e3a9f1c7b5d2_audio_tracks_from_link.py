"""audio_tracks: status, error, source_url, progress (grab audio from a link)

Revision ID: e3a9f1c7b5d2
Revises: d7b2e9c4a1f3
Create Date: 2026-10-20 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3a9f1c7b5d2"
down_revision: str | None = "d7b2e9c4a1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audio_tracks",
        sa.Column("status", sa.String(length=16), server_default="ready", nullable=False),
    )
    op.add_column("audio_tracks", sa.Column("error", sa.String(length=2048), nullable=True))
    op.add_column("audio_tracks", sa.Column("source_url", sa.String(length=2048), nullable=True))
    op.add_column("audio_tracks", sa.Column("progress", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("audio_tracks", "progress")
    op.drop_column("audio_tracks", "source_url")
    op.drop_column("audio_tracks", "error")
    op.drop_column("audio_tracks", "status")
