"""normalized dedupe expression index

Revision ID: 20260916_0021
Revises: 20260916_0020
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260916_0021"
down_revision: Union[str, None] = "20260916_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_jobs_dedupe_normalized"

# Only immutable expressions can be indexed; `published_at::date` is stable
# (session timezone dependent) and is therefore filtered, not indexed.
NORMALIZE_TITLE = "trim(regexp_replace(lower(coalesce(title, '')), '[^0-9a-zåäö]+', ' ', 'g'))"
NORMALIZE_EMPLOYER = "trim(regexp_replace(lower(coalesce(employer, '')), '[^0-9a-zåäö]+', ' ', 'g'))"
NORMALIZE_LOCATION = "trim(regexp_replace(lower(coalesce(location, '')), '[^0-9a-zåäö]+', ' ', 'g'))"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} ON jobs "
            f"(({NORMALIZE_TITLE}), ({NORMALIZE_EMPLOYER}), ({NORMALIZE_LOCATION})) "
            "WHERE status = 'active'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
