"""widen article text fields to unbounded text

Revision ID: b73ada736f6e
Revises: 1fbfc2db136b
Create Date: 2026-07-13 18:43:25.427913

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b73ada736f6e'
down_revision: Union[str, None] = '1fbfc2db136b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Production incident: a Carrefour Q2 2026 import failed with
# StringDataRightTruncation on `author` (VARCHAR(255)) — one row had a
# 405-character author value. `medium`/`source`/`sentiment_original`/
# `importance_original`/`author`/`county`/`subfolder_1`/`subfolder_2` are
# all uncontrolled source-workbook text (whatever a media-monitoring vendor
# put in that column), unlike `retailer`/`import_status`/
# `retailer_confidence`/`retailer_review_status`/`fingerprint`, which stay
# bounded VARCHAR because this application controls their exact value set.
# VARCHAR(n) -> TEXT is a metadata-only change in Postgres (both share the
# same on-disk representation; no table rewrite), safe on a large `articles`
# table.
_WIDENED_COLUMNS: tuple[tuple[str, int], ...] = (
    ("medium", 255),
    ("source", 500),
    ("sentiment_original", 64),
    ("importance_original", 64),
    ("author", 255),
    ("county", 128),
    ("subfolder_1", 255),
    ("subfolder_2", 255),
)


def upgrade() -> None:
    for column_name, _old_length in _WIDENED_COLUMNS:
        op.alter_column(
            "articles",
            column_name,
            existing_type=sa.String(length=_old_length),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    # Reverting to a bounded VARCHAR will fail if any existing row's value
    # now exceeds the original length -- expected and acceptable for a
    # downgrade path; the fix this migration ships is precisely that such
    # values are real and must not be truncated.
    for column_name, old_length in _WIDENED_COLUMNS:
        op.alter_column(
            "articles",
            column_name,
            existing_type=sa.Text(),
            type_=sa.String(length=old_length),
            existing_nullable=True,
        )
