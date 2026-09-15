"""separate missing-attempt accounting for scan closure

Revision ID: 20260916_0024
Revises: 20260916_0023
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260916_0024"
down_revision: Union[str, None] = "20260916_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_scan_members",
        sa.Column("missing_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("source_scan_members", "missing_attempts")
