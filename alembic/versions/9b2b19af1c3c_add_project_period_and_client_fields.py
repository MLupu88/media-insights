"""add project period and client fields

Revision ID: 9b2b19af1c3c
Revises: 93fab4c29c23
Create Date: 2026-07-13 07:47:10.410313

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9b2b19af1c3c'
down_revision: Union[str, None] = '93fab4c29c23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `quarter` becomes optional: a project must supply a valid quarter OR
    # a complete period_start/period_end range. Every existing row already
    # has `quarter` set, so relaxing the constraint touches no data.
    op.alter_column("projects", "quarter", existing_type=sa.String(length=16), nullable=True)

    op.add_column("projects", sa.Column("period_start", sa.Date(), nullable=True))
    op.add_column("projects", sa.Column("period_end", sa.Date(), nullable=True))
    op.add_column("projects", sa.Column("client_name", sa.String(length=255), nullable=True))

    # DB-level backstop behind the Pydantic validator (ProjectCreate) — safe
    # to add with zero pre-migration cleanup: every existing row has
    # `quarter` set and both new date columns NULL, satisfying every clause
    # trivially. `projects` is a small table, so the brief ACCESS EXCLUSIVE
    # lock needed to validate the constraint is not a practical concern.
    op.create_check_constraint(
        "ck_projects_period_integrity",
        "projects",
        "(quarter IS NOT NULL OR (period_start IS NOT NULL AND period_end IS NOT NULL)) "
        "AND (period_start IS NULL) = (period_end IS NULL) "
        "AND (period_start IS NULL OR period_end IS NULL OR period_end >= period_start)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_projects_period_integrity", "projects", type_="check")
    op.drop_column("projects", "client_name")
    op.drop_column("projects", "period_end")
    op.drop_column("projects", "period_start")
    op.alter_column("projects", "quarter", existing_type=sa.String(length=16), nullable=False)
