"""remove the rights check (internal content matching): drop its tables, if a
database still has them from before the feature was removed

Revision ID: c2d4e6f8a0b1
Revises: 9e1a4c7b2d58
Create Date: 2026-10-20 23:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c2d4e6f8a0b1"
down_revision: str | None = "9e1a4c7b2d58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Indexes go with their tables. IF EXISTS: safe on a database that never had them.
    op.execute("DROP TABLE IF EXISTS content_checks")
    op.execute("DROP TABLE IF EXISTS reference_contents")


def downgrade() -> None:
    # Nothing to restore: the feature and the migration that created these
    # tables are gone, so a fresh database never had them.
    pass
