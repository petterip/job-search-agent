"""index job_sources.job_id

The recommendations/jobs API resolves each job's display source with a lateral
subquery filtered on ``job_sources.job_id``. Without an index that turns into a
sequential scan of ``job_sources`` per returned row, which makes the home page
recommendation feed seconds slow once the table grows.

Revision ID: 20260915_0016
Revises: 20260705_0015
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260915_0016"
down_revision: Union[str, None] = "20260705_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "create index if not exists ix_job_sources_job_id on job_sources (job_id)"
    )


def downgrade() -> None:
    op.execute("drop index if exists ix_job_sources_job_id")
