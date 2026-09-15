"""occurrence-level closure evidence

Revision ID: 20260916_0023
Revises: 20260916_0022
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260916_0023"
down_revision: Union[str, None] = "20260916_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "job_sources",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_job_sources_open",
        "job_sources",
        ["job_id", "closed_at"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_job_sources_open", table_name="job_sources")
    op.drop_column("job_sources", "closed_at")
