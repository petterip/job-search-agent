"""add recommendation feedback

Revision ID: 20260620_0006
Revises: 20260620_0005
Create Date: 2026-06-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260620_0006"
down_revision: Union[str, None] = "20260620_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recommendation_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("recommendation_id", sa.Integer(), sa.ForeignKey("recommendations.id"), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "action IN ('good_match', 'not_relevant', 'applied')",
            name="ck_recommendation_feedback_action",
        ),
    )
    op.create_index(
        "ix_recommendation_feedback_recommendation_created",
        "recommendation_feedback",
        ["recommendation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recommendation_feedback_recommendation_created",
        table_name="recommendation_feedback",
    )
    op.drop_table("recommendation_feedback")
