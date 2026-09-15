"""run ownership tokens for source and pipeline runs

Mutual exclusion itself is owned by PostgreSQL advisory locks held on a
dedicated connection for the whole run. The ``owner_token`` column records
which session created a ``running`` row so a completion update can match the
current owner instead of blindly overwriting whatever state the row is in.

Revision ID: 20260916_0017_run_ownership
Revises: 20260915_0016
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260916_0017_run_ownership"
down_revision: Union[str, None] = "20260915_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("source_runs", sa.Column("owner_token", sa.Text(), nullable=True))
    op.add_column("pipeline_runs", sa.Column("owner_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("pipeline_runs", "owner_token")
    op.drop_column("source_runs", "owner_token")
