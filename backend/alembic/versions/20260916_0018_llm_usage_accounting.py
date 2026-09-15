"""hosted llm usage accounting

Revision ID: 20260916_0018
Revises: 20260916_0017
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260916_0018"
down_revision: Union[str, None] = "20260916_0017_run_ownership"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACCOUNTING_COLUMNS = (
    ("input_tokens", sa.Integer(), True),
    ("output_tokens", sa.Integer(), True),
    ("cached_tokens", sa.Integer(), True),
    ("latency_ms", sa.Integer(), True),
    ("attempts", sa.Integer(), False, sa.text("1")),
    ("outcome", sa.Text(), True),
    ("provider_request_id", sa.Text(), True),
    ("usage_present", sa.Boolean(), False, sa.text("false")),
)


def _add_columns(table: str) -> None:
    for column in ACCOUNTING_COLUMNS:
        name, column_type, nullable = column[0], column[1], column[2]
        server_default = column[3] if len(column) > 3 else None
        op.add_column(
            table,
            sa.Column(name, column_type, nullable=nullable, server_default=server_default),
        )


def _drop_columns(table: str) -> None:
    for column in reversed(ACCOUNTING_COLUMNS):
        op.drop_column(table, column[0])


def upgrade() -> None:
    _add_columns("llm_evaluations")
    _add_columns("feedback_llm_analyses")


def downgrade() -> None:
    _drop_columns("feedback_llm_analyses")
    _drop_columns("llm_evaluations")
