"""background audio: audio_tracks table, clips.bg_audio_id / bg_audio_volume

Revision ID: d7b2e9c4a1f3
Revises: c41f7a2d9e10
Create Date: 2026-10-20 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7b2e9c4a1f3"
down_revision: str | None = "c41f7a2d9e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audio_tracks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audio_tracks_user_id", "audio_tracks", ["user_id"])

    op.add_column("clips", sa.Column("bg_audio_id", sa.String(length=36), nullable=True))
    op.add_column(
        "clips",
        sa.Column("bg_audio_volume", sa.Integer(), server_default="25", nullable=False),
    )
    op.create_index("ix_clips_bg_audio_id", "clips", ["bg_audio_id"])
    op.create_foreign_key("fk_clips_bg_audio_id", "clips", "audio_tracks", ["bg_audio_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_clips_bg_audio_id", "clips", type_="foreignkey")
    op.drop_index("ix_clips_bg_audio_id", table_name="clips")
    op.drop_column("clips", "bg_audio_volume")
    op.drop_column("clips", "bg_audio_id")
    op.drop_index("ix_audio_tracks_user_id", table_name="audio_tracks")
    op.drop_table("audio_tracks")
