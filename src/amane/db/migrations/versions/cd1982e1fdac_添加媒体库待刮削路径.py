"""添加媒体库待刮削路径

Revision ID: cd1982e1fdac
Revises: 0b4c0aba7e89
Create Date: 2026-11-07 00:38:31.025297
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cd1982e1fdac"
down_revision: str | None = "0b4c0aba7e89"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("libraries", sa.Column("scan_path", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("libraries") as batch_op:
        batch_op.drop_column("scan_path")
