"""feedback analysis retry accounting

Revision ID: 20260916_0019
Revises: 20260916_0018
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260916_0019"
down_revision: Union[str, None] = "20260916_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "recommendation_feedback",
        sa.Column("analysis_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "recommendation_feedback",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "recommendation_feedback",
        sa.Column("analysis_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recommendation_feedback", "analysis_reason")
    op.drop_column("recommendation_feedback", "next_attempt_at")
    op.drop_column("recommendation_feedback", "analysis_attempts")
