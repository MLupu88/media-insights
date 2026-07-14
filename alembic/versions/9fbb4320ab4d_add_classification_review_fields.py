"""add classification review fields

Revision ID: 9fbb4320ab4d
Revises: a8bd5476bd23
Create Date: 2026-07-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '9fbb4320ab4d'
down_revision: Union[str, None] = 'a8bd5476bd23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Existing classification rows predate the human-review workflow and are
# backfilled to 'approved' -- mirroring the exact precedent set by
# 1fbfc2db136b (article retailer_review_status backfilled to 'confirmed'):
# historical/already-used data is treated as already-resolved, never
# dumped into a brand-new review queue retroactively. New classifications
# inserted from this point on get the ORM-level default ('pending')
# instead, since they never pass through this ALTER TABLE at all.


def upgrade() -> None:
    op.add_column(
        "classifications",
        sa.Column("review_status", sa.String(length=16), nullable=False, server_default="approved"),
    )
    op.add_column(
        "classifications", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "classifications",
        sa.Column("original_ai_labels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.create_index(
        "ix_classifications_project_id_review_status",
        "classifications",
        ["project_id", "review_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_classifications_project_id_review_status", table_name="classifications")
    op.drop_column("classifications", "original_ai_labels")
    op.drop_column("classifications", "reviewed_at")
    op.drop_column("classifications", "review_status")
