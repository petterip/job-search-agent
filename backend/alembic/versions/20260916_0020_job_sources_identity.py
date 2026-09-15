"""partial uniqueness for source occurrence identity

Revision ID: 20260916_0020
Revises: 20260916_0019
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260916_0020"
down_revision: Union[str, None] = "20260916_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_job_sources_source_external_id"


def upgrade() -> None:
    # CONCURRENTLY so a live catalogue is not locked; IF NOT EXISTS makes the
    # migration safe on a database that already has the index.
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            "ON job_sources (source_id, external_id) WHERE external_id IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
