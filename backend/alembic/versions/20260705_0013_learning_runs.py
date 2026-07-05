"""learning run audit table

Revision ID: 20260705_0013
Revises: 20260705_0012
Create Date: 2026-07-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260705_0013"
down_revision: Union[str, None] = "20260705_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "learning_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feedback_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("analysis_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changes_applied", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("learned_version", sa.Integer(), nullable=True),
        sa.Column("feedback_analysis_prompt_version", sa.Integer(), nullable=True),
        sa.Column("job_fit_prompt_version", sa.Integer(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("learning_runs")
