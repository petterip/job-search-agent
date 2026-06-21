"""add source run event log

Revision ID: 20260620_0003
Revises: 20260620_0002
Create Date: 2026-06-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260620_0003"
down_revision: Union[str, None] = "20260620_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_run_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_run_id", sa.Integer(), sa.ForeignKey("source_runs.id"), nullable=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=True),
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_source_run_events_run_created",
        "source_run_events",
        ["source_run_id", "created_at"],
    )
    op.create_index(
        "ix_source_run_events_source_created",
        "source_run_events",
        ["source_name", "created_at"],
    )
    op.create_index(
        "ix_source_run_events_level_created",
        "source_run_events",
        ["level", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_run_events_level_created", table_name="source_run_events")
    op.drop_index("ix_source_run_events_source_created", table_name="source_run_events")
    op.drop_index("ix_source_run_events_run_created", table_name="source_run_events")
    op.drop_table("source_run_events")
