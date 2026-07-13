"""add import batches table

Revision ID: bb80f449c7d8
Revises: 9b2b19af1c3c
Create Date: 2026-07-13 07:47:10.660739

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bb80f449c7d8'
down_revision: Union[str, None] = '9b2b19af1c3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("files_processed", sa.Integer(), nullable=False),
        sa.Column("files_accepted", sa.Integer(), nullable=False),
        sa.Column("files_rejected", sa.Integer(), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("valid_rows", sa.Integer(), nullable=False),
        sa.Column("invalid_rows", sa.Integer(), nullable=False),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False),
        sa.Column("needs_review_rows", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_batches_project_id", "import_batches", ["project_id"], unique=False)

    op.add_column("uploaded_files", sa.Column("import_batch_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_uploaded_files_import_batch_id",
        "uploaded_files",
        "import_batches",
        ["import_batch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_uploaded_files_import_batch_id", "uploaded_files", ["import_batch_id"], unique=False
    )

    # Existing rows must not become trustable tier-2 mappings retroactively
    # — server_default='false' backfills every current row to unconfirmed.
    op.add_column(
        "uploaded_files",
        sa.Column(
            "retailer_hint_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    op.drop_column("uploaded_files", "retailer_hint_confirmed")
    op.drop_index("ix_uploaded_files_import_batch_id", table_name="uploaded_files")
    op.drop_constraint("fk_uploaded_files_import_batch_id", "uploaded_files", type_="foreignkey")
    op.drop_column("uploaded_files", "import_batch_id")
    op.drop_index("ix_import_batches_project_id", table_name="import_batches")
    op.drop_table("import_batches")
