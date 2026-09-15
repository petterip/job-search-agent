"""resumable source sitemap scan state

Revision ID: 20260916_0022
Revises: 20260916_0021
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260916_0022"
down_revision: Union[str, None] = "20260916_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_scan_members",
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("lastmod", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sitemap_present", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'classified', 'retry', 'closed')",
            name="ck_source_scan_members_state",
        ),
        sa.PrimaryKeyConstraint("source_id", "external_id"),
    )
    op.create_index(
        "ix_source_scan_members_claim",
        "source_scan_members",
        ["source_id", "state", "next_attempt_at", "lastmod", "external_id"],
    )
    op.create_index(
        "ix_source_scan_members_absent",
        "source_scan_members",
        ["source_id", "sitemap_present", "state", "updated_at"],
    )
    op.create_table(
        "source_scan_progress",
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), primary_key=True),
        sa.Column("frontier_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("classified_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("frontier_drained", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("source_scan_progress")
    op.drop_index("ix_source_scan_members_absent", table_name="source_scan_members")
    op.drop_index("ix_source_scan_members_claim", table_name="source_scan_members")
    op.drop_table("source_scan_members")
