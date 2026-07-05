"""five-point recommendation feedback with upsert

Revision ID: 20260705_0011
Revises: 20260705_0010
Create Date: 2026-07-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260705_0011"
down_revision: Union[str, None] = "20260705_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("recommendation_feedback", sa.Column("rating", sa.SmallInteger(), nullable=True))
    op.add_column(
        "recommendation_feedback",
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("recommendation_feedback", sa.Column("job_id", sa.Integer(), nullable=True))
    op.add_column(
        "recommendation_feedback",
        sa.Column(
            "scoring_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "recommendation_feedback",
        sa.Column("analysis_status", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "recommendation_feedback",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        """
        delete from recommendation_feedback rf
        where rf.id not in (
            select distinct on (recommendation_id) id
            from recommendation_feedback
            order by recommendation_id, created_at desc, id desc
        )
        """
    )
    op.execute(
        """
        update recommendation_feedback rf
        set
            rating = case rf.action
                when 'not_relevant' then 1
                when 'good_match' then 4
                when 'applied' then 5
            end,
            applied = (rf.action = 'applied'),
            job_id = r.job_id,
            updated_at = rf.created_at
        from recommendations r
        where r.id = rf.recommendation_id
        """
    )
    op.execute(
        """
        delete from recommendation_feedback
        where job_id is null
        """
    )

    op.alter_column("recommendation_feedback", "rating", nullable=False)
    op.alter_column("recommendation_feedback", "job_id", nullable=False)
    op.alter_column("recommendation_feedback", "updated_at", nullable=False, server_default=sa.func.now())
    op.alter_column("recommendation_feedback", "action", nullable=True)

    op.create_foreign_key(
        "fk_recommendation_feedback_job_id",
        "recommendation_feedback",
        "jobs",
        ["job_id"],
        ["id"],
    )
    op.drop_constraint(
        "recommendation_feedback_recommendation_id_fkey",
        "recommendation_feedback",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "recommendation_feedback_recommendation_id_fkey",
        "recommendation_feedback",
        "recommendations",
        ["recommendation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_recommendation_feedback_recommendation_id",
        "recommendation_feedback",
        ["recommendation_id"],
    )
    op.create_check_constraint(
        "ck_recommendation_feedback_rating",
        "recommendation_feedback",
        "rating BETWEEN 1 AND 5",
    )
    op.create_check_constraint(
        "ck_recommendation_feedback_analysis_status",
        "recommendation_feedback",
        "analysis_status IN ('pending', 'completed', 'failed', 'skipped')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_recommendation_feedback_analysis_status", "recommendation_feedback", type_="check")
    op.drop_constraint("ck_recommendation_feedback_rating", "recommendation_feedback", type_="check")
    op.drop_constraint("uq_recommendation_feedback_recommendation_id", "recommendation_feedback", type_="unique")
    op.drop_constraint(
        "recommendation_feedback_recommendation_id_fkey",
        "recommendation_feedback",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "recommendation_feedback_recommendation_id_fkey",
        "recommendation_feedback",
        "recommendations",
        ["recommendation_id"],
        ["id"],
    )
    op.drop_constraint("fk_recommendation_feedback_job_id", "recommendation_feedback", type_="foreignkey")
    op.alter_column("recommendation_feedback", "action", nullable=False)
    op.drop_column("recommendation_feedback", "updated_at")
    op.drop_column("recommendation_feedback", "analysis_status")
    op.drop_column("recommendation_feedback", "scoring_snapshot")
    op.drop_column("recommendation_feedback", "job_id")
    op.drop_column("recommendation_feedback", "applied")
    op.drop_column("recommendation_feedback", "rating")
