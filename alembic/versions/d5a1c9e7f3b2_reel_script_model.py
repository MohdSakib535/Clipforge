"""reels.script_model (the Ollama model that writes its script; null: the
default REEL_SCRIPT_MODEL)

Revision ID: d5a1c9e7f3b2
Revises: c8e4a2f6b1d9
Create Date: 2026-10-31 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5a1c9e7f3b2"
down_revision: str | None = "c8e4a2f6b1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reels", sa.Column("script_model", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("reels", "script_model")
