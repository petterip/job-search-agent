"""feedback llm analyses table

Revision ID: 20260705_0012
Revises: 20260705_0011
Create Date: 2026-07-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260705_0012"
down_revision: Union[str, None] = "20260705_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feedback_llm_analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "feedback_id",
            sa.Integer(),
            sa.ForeignKey("recommendation_feedback.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("configured_model", sa.Text(), nullable=False),
        sa.Column("returned_model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("analysis", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("feedback_id", name="uq_feedback_llm_analyses_feedback_id"),
    )
    op.create_index(
        "ix_feedback_llm_analyses_request_hash",
        "feedback_llm_analyses",
        ["request_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_feedback_llm_analyses_request_hash", table_name="feedback_llm_analyses")
    op.drop_table("feedback_llm_analyses")
