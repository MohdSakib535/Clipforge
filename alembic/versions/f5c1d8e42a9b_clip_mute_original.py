"""clips.bg_audio_mute_original (music-only: drop the clip's own audio)

Revision ID: f5c1d8e42a9b
Revises: e3a9f1c7b5d2
Create Date: 2026-10-20 17:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5c1d8e42a9b"
down_revision: str | None = "e3a9f1c7b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clips",
        sa.Column(
            "bg_audio_mute_original",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("clips", "bg_audio_mute_original")
