"""cache google routes transit distances

Revision ID: 20260705_0008
Revises: 20260620_0007
Create Date: 2026-07-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260705_0008"
down_revision: Union[str, None] = "20260620_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "transit_distance_cache",
        sa.Column("destination_query", sa.Text(), nullable=False),
        sa.Column("origin_address", sa.Text(), nullable=False),
        sa.Column("distance_meters", sa.Integer(), nullable=False),
        sa.Column("distance_km", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("duration_text", sa.Text(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("destination_query", "origin_address"),
    )
    op.create_table(
        "transit_distance_failures",
        sa.Column("destination_query", sa.Text(), nullable=False),
        sa.Column("origin_address", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("destination_query", "origin_address"),
    )


def downgrade() -> None:
    op.drop_table("transit_distance_failures")
    op.drop_table("transit_distance_cache")
