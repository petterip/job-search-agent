"""add normalized job location

Revision ID: 20260620_0004
Revises: 20260620_0003
Create Date: 2026-06-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260620_0004"
down_revision: Union[str, None] = "20260620_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("location", sa.Text(), nullable=True))
    op.create_index("ix_jobs_location", "jobs", ["location"])


def downgrade() -> None:
    op.drop_index("ix_jobs_location", table_name="jobs")
    op.drop_column("jobs", "location")
