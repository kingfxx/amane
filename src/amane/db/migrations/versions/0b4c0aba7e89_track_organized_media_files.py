"""track organized media files

Revision ID: 0b4c0aba7e89
Revises: a9c01801c865
Create Date: 2026-11-06 18:55:18.127467
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0b4c0aba7e89"
down_revision: str | None = "a9c01801c865"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_files", sa.Column("organized", sa.Boolean(), nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    op.drop_column("media_files", "organized")
