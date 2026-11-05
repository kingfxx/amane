"""新增插件导入来源记录

Revision ID: a9c01801c865
Revises: 56fe451426ef
Create Date: 2026-11-06 00:17:00.684264
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9c01801c865"
down_revision: str | None = "56fe451426ef"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("metadata_id", sa.Integer(), sa.ForeignKey("metadata.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("source_path", sa.String(), nullable=False),
        sa.Column("source_hash", sa.String(), nullable=False),
        sa.Column("local_trailer_paths", sa.JSON(), nullable=False),
        sa.UniqueConstraint("source_id", "source_path", name="uq_import_source_path"),
    )
    op.create_index("ix_import_records_metadata_id", "import_records", ["metadata_id"])


def downgrade() -> None:
    op.drop_index("ix_import_records_metadata_id", table_name="import_records")
    op.drop_table("import_records")
