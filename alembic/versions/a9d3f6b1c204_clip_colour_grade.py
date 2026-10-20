"""clips: colour grade (contrast / brightness / saturation)

Revision ID: a9d3f6b1c204
Revises: f5c1d8e42a9b
Create Date: 2026-10-20 18:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9d3f6b1c204"
down_revision: str | None = "f5c1d8e42a9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLS = ("color_contrast", "color_brightness", "color_saturation")


def upgrade() -> None:
    for col in _COLS:
        op.add_column("clips", sa.Column(col, sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    for col in _COLS:
        op.drop_column("clips", col)
