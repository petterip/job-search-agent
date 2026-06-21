"""track active recommendation set

Revision ID: 20260620_0007
Revises: 20260620_0006
Create Date: 2026-06-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260620_0007"
down_revision: Union[str, None] = "20260620_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        with survivors as (
            select profile_id, job_id, min(id) as survivor_id
            from recommendations
            group by profile_id, job_id
        )
        update recommendation_feedback rf
        set recommendation_id = survivors.survivor_id
        from recommendations r
        join survivors
          on survivors.profile_id = r.profile_id
         and survivors.job_id = r.job_id
        where rf.recommendation_id = r.id
          and r.id <> survivors.survivor_id
        """
    )
    op.execute(
        """
        with survivors as (
            select profile_id, job_id, min(id) as survivor_id
            from recommendations
            group by profile_id, job_id
        )
        delete from recommendations r
        using survivors
        where r.profile_id = survivors.profile_id
          and r.job_id = survivors.job_id
          and r.id <> survivors.survivor_id
        """
    )
    op.add_column(
        "recommendations",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_unique_constraint(
        "uq_recommendations_profile_job",
        "recommendations",
        ["profile_id", "job_id"],
    )
    op.create_index(
        "ix_recommendations_profile_active_rank",
        "recommendations",
        ["profile_id", "is_active", "rank"],
    )


def downgrade() -> None:
    op.drop_index("ix_recommendations_profile_active_rank", table_name="recommendations")
    op.drop_constraint("uq_recommendations_profile_job", "recommendations", type_="unique")
    op.drop_column("recommendations", "is_active")
