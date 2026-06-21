"""add recommendation llm fields

Revision ID: 20260620_0005
Revises: 20260620_0004
Create Date: 2026-06-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260620_0005"
down_revision: Union[str, None] = "20260620_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("recommendations", sa.Column("llm_score", sa.Integer(), nullable=True))
    op.add_column("recommendations", sa.Column("fit_tier", sa.String(length=64), nullable=True))
    op.add_column("recommendations", sa.Column("suggested_action", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("recommendations", "suggested_action")
    op.drop_column("recommendations", "fit_tier")
    op.drop_column("recommendations", "llm_score")
