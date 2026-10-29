"""audio_tracks.to_bucket (a link imported from the Bucket tab: its audio goes
to the bucket, nothing is kept on this computer)

Revision ID: e7a3c5b9d2f4
Revises: d4b8e1f6a2c9
Create Date: 2026-10-29 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7a3c5b9d2f4"
down_revision: str | None = "d4b8e1f6a2c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audio_tracks",
        sa.Column("to_bucket", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("audio_tracks", "to_bucket")
