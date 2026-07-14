"""add active batch per project unique index

Revision ID: a8bd5476bd23
Revises: b73ada736f6e
Create Date: 2026-07-14 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a8bd5476bd23'
down_revision: Union[str, None] = 'b73ada736f6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Production incident: create_classification_batches created one
# ClassificationBatch per batch_size chunk of ALL eligible articles in a
# single request (~943 batches for one 37,700-article project), which the
# n8n workflow then tried to process sequentially in one execution --
# leading to 754 batches stuck pending/running. The app-level fix makes
# batch claiming atomic (row lock + at most one active batch returned per
# request); this index is the DB-level backstop against the same race
# (two concurrent "get next batch" calls both deciding to create a batch),
# mirroring ux_chat_runs_active_per_session.


def upgrade() -> None:
    op.create_index(
        'ux_classification_batches_active_per_project',
        'classification_batches',
        ['project_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(
        'ux_classification_batches_active_per_project',
        table_name='classification_batches',
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
