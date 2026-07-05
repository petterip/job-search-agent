"""learned preference and anti-preference centroids

Revision ID: 20260705_0014
Revises: 20260705_0013
Create Date: 2026-07-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260705_0014"
down_revision: Union[str, None] = "20260705_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE learned_preference_embeddings (
            id SERIAL PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES job_seeker_profiles(id) ON DELETE CASCADE,
            kind VARCHAR(32) NOT NULL,
            model TEXT NOT NULL,
            dimension INTEGER NOT NULL DEFAULT 1536,
            job_count INTEGER NOT NULL DEFAULT 0,
            embedding vector(1536) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_learned_preference_embeddings_kind
                CHECK (kind IN ('preference', 'anti_preference')),
            CONSTRAINT ck_learned_preference_embeddings_dimension
                CHECK (dimension = 1536),
            CONSTRAINT uq_learned_preference_embeddings_profile_kind_model
                UNIQUE (profile_id, kind, model, dimension)
        )
        """
    )


def downgrade() -> None:
    op.drop_table("learned_preference_embeddings")
