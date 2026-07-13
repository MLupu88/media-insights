"""add article retailer review fields

Revision ID: 1fbfc2db136b
Revises: bb80f449c7d8
Create Date: 2026-07-13 07:47:10.858065

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1fbfc2db136b'
down_revision: Union[str, None] = 'bb80f449c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows backfill to 'legacy'/'confirmed' — treated as
    # already-resolved, never dumped into the Review tab. server_default
    # backfills every current row at ALTER TABLE time (metadata-only on
    # Postgres 11+, no table rewrite even on a large `articles` table).
    op.add_column(
        "articles",
        sa.Column(
            "retailer_confidence", sa.String(length=24), nullable=False, server_default="legacy"
        ),
    )
    op.add_column(
        "articles",
        sa.Column(
            "retailer_review_status", sa.String(length=16), nullable=False, server_default="confirmed"
        ),
    )
    op.add_column("articles", sa.Column("retailer_raw_value", sa.Text(), nullable=True))

    op.create_index(
        "ix_articles_project_id_retailer_review_status",
        "articles",
        ["project_id", "retailer_review_status"],
        unique=False,
        postgresql_where=sa.text("retailer_review_status = 'needs_review'"),
    )


def downgrade() -> None:
    op.drop_index("ix_articles_project_id_retailer_review_status", table_name="articles")
    op.drop_column("articles", "retailer_raw_value")
    op.drop_column("articles", "retailer_review_status")
    op.drop_column("articles", "retailer_confidence")
