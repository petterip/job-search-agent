"""key enrichment state by source occurrence

Revision ID: 20260705_0010
Revises: 20260705_0009
Create Date: 2026-07-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260705_0010"
down_revision: Union[str, None] = "20260705_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("enrichment_queue", sa.Column("job_source_id", sa.Integer(), nullable=True))
    op.add_column("enrichment_queue", sa.Column("raw_listing_id", sa.Integer(), nullable=True))
    op.add_column("job_enrichments", sa.Column("job_source_id", sa.Integer(), nullable=True))
    op.add_column("job_enrichments", sa.Column("raw_listing_id", sa.Integer(), nullable=True))
    op.add_column("job_description_state", sa.Column("job_source_id", sa.Integer(), nullable=True))
    op.add_column("job_description_state", sa.Column("job_enrichment_id", sa.Integer(), nullable=True))

    op.execute(
        """
        update enrichment_queue eq
        set job_source_id = (
                select js.id
                from job_sources js
                where js.job_id = eq.job_id
                order by js.last_seen_at desc nulls last, js.id desc
                limit 1
            ),
            raw_listing_id = (
                select js.raw_listing_id
                from job_sources js
                where js.job_id = eq.job_id
                order by js.last_seen_at desc nulls last, js.id desc
                limit 1
            )
        """
    )
    op.execute(
        """
        update enrichment_queue
        set status = 'cancelled',
            last_error = coalesce(last_error, 'cancelled by source-occurrence migration: no job_source row'),
            updated_at = now()
        where job_source_id is null
        """
    )
    op.execute("delete from enrichment_queue where job_source_id is null")

    op.execute(
        """
        update job_enrichments je
        set job_source_id = (
                select js.id
                from job_sources js
                where js.job_id = je.job_id
                order by js.last_seen_at desc nulls last, js.id desc
                limit 1
            ),
            raw_listing_id = (
                select js.raw_listing_id
                from job_sources js
                where js.job_id = je.job_id
                order by js.last_seen_at desc nulls last, js.id desc
                limit 1
            )
        """
    )
    op.execute("delete from job_enrichments where job_source_id is null")

    op.drop_index("uq_enrichment_queue_active", table_name="enrichment_queue")
    op.create_foreign_key(
        "fk_enrichment_queue_job_source_id",
        "enrichment_queue",
        "job_sources",
        ["job_source_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_enrichment_queue_raw_listing_id",
        "enrichment_queue",
        "raw_listings",
        ["raw_listing_id"],
        ["id"],
    )
    op.alter_column("enrichment_queue", "job_source_id", nullable=False)
    op.alter_column("enrichment_queue", "raw_listing_id", nullable=False)
    op.create_index(
        "uq_enrichment_queue_active",
        "enrichment_queue",
        ["job_source_id", "enricher", "input_hash"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'retry')"),
    )

    op.drop_constraint("uq_job_enrichments_latest", "job_enrichments", type_="unique")
    op.create_foreign_key(
        "fk_job_enrichments_job_source_id",
        "job_enrichments",
        "job_sources",
        ["job_source_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_job_enrichments_raw_listing_id",
        "job_enrichments",
        "raw_listings",
        ["raw_listing_id"],
        ["id"],
    )
    op.alter_column("job_enrichments", "job_source_id", nullable=False)
    op.alter_column("job_enrichments", "raw_listing_id", nullable=False)
    op.create_unique_constraint(
        "uq_job_enrichments_latest",
        "job_enrichments",
        ["job_source_id", "enricher", "input_hash"],
    )

    op.execute(
        """
        update job_description_state jds
        set job_source_id = (
                select js.id
                from job_sources js
                where js.job_id = jds.job_id
                  and js.source_id = jds.source_id
                order by js.last_seen_at desc nulls last, js.id desc
                limit 1
            )
        where jds.provenance = 'source'
        """
    )
    op.execute(
        """
        update job_description_state jds
        set job_enrichment_id = (
                select je.id
                from job_enrichments je
                where je.job_id = jds.job_id
                  and je.enricher = jds.enricher
                  and je.input_hash = jds.input_hash
                order by je.created_at desc nulls last, je.id desc
                limit 1
            )
        where jds.provenance = 'enriched'
        """
    )
    op.execute("delete from job_description_state where provenance = 'source' and job_source_id is null")
    op.execute("delete from job_description_state where provenance = 'enriched' and job_enrichment_id is null")

    op.create_foreign_key(
        "fk_job_description_state_job_source_id",
        "job_description_state",
        "job_sources",
        ["job_source_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_job_description_state_job_enrichment_id",
        "job_description_state",
        "job_enrichments",
        ["job_enrichment_id"],
        ["id"],
    )
    op.drop_constraint(
        "ck_job_description_state_provenance",
        "job_description_state",
        type_="check",
    )
    op.drop_column("job_description_state", "source_id")
    op.drop_column("job_description_state", "enricher")
    op.drop_column("job_description_state", "input_hash")
    op.create_check_constraint(
        "ck_job_description_state_provenance",
        "job_description_state",
        """
        (
            provenance = 'source'
            and job_source_id is not null
            and job_enrichment_id is null
        ) or (
            provenance = 'enriched'
            and job_enrichment_id is not null
            and job_source_id is null
        )
        """,
    )


def downgrade() -> None:
    op.add_column("job_description_state", sa.Column("input_hash", sa.String(length=64), nullable=True))
    op.add_column("job_description_state", sa.Column("enricher", sa.String(length=64), nullable=True))
    op.add_column("job_description_state", sa.Column("source_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "job_description_state_source_id_fkey",
        "job_description_state",
        "sources",
        ["source_id"],
        ["id"],
    )
    op.execute(
        """
        update job_description_state jds
        set source_id = js.source_id
        from job_sources js
        where jds.job_source_id = js.id
          and jds.provenance = 'source'
        """
    )
    op.execute(
        """
        update job_description_state jds
        set enricher = je.enricher,
            input_hash = je.input_hash
        from job_enrichments je
        where jds.job_enrichment_id = je.id
          and jds.provenance = 'enriched'
        """
    )
    op.drop_constraint("ck_job_description_state_provenance", "job_description_state", type_="check")
    op.create_check_constraint(
        "ck_job_description_state_provenance",
        "job_description_state",
        "provenance IN ('source', 'enriched')",
    )
    op.drop_constraint("fk_job_description_state_job_enrichment_id", "job_description_state", type_="foreignkey")
    op.drop_constraint("fk_job_description_state_job_source_id", "job_description_state", type_="foreignkey")
    op.drop_column("job_description_state", "job_enrichment_id")
    op.drop_column("job_description_state", "job_source_id")

    op.drop_constraint("uq_job_enrichments_latest", "job_enrichments", type_="unique")
    op.execute(
        """
        delete from job_enrichments old
        using job_enrichments keep
        where old.job_id = keep.job_id
          and old.enricher = keep.enricher
          and old.input_hash = keep.input_hash
          and old.id < keep.id
        """
    )
    op.create_unique_constraint(
        "uq_job_enrichments_latest",
        "job_enrichments",
        ["job_id", "enricher", "input_hash"],
    )
    op.drop_constraint("fk_job_enrichments_raw_listing_id", "job_enrichments", type_="foreignkey")
    op.drop_constraint("fk_job_enrichments_job_source_id", "job_enrichments", type_="foreignkey")
    op.drop_column("job_enrichments", "raw_listing_id")
    op.drop_column("job_enrichments", "job_source_id")

    op.drop_index("uq_enrichment_queue_active", table_name="enrichment_queue")
    op.execute(
        """
        update enrichment_queue old
        set status = 'cancelled',
            last_error = coalesce(
                old.last_error,
                'cancelled by source-occurrence migration downgrade: duplicate job-level queue item'
            ),
            updated_at = now()
        from enrichment_queue keep
        where old.job_id = keep.job_id
          and old.enricher = keep.enricher
          and old.input_hash = keep.input_hash
          and old.status in ('queued', 'running', 'retry')
          and keep.status in ('queued', 'running', 'retry')
          and old.id > keep.id
        """
    )
    op.create_index(
        "uq_enrichment_queue_active",
        "enrichment_queue",
        ["job_id", "enricher", "input_hash"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'retry')"),
    )
    op.drop_constraint("fk_enrichment_queue_raw_listing_id", "enrichment_queue", type_="foreignkey")
    op.drop_constraint("fk_enrichment_queue_job_source_id", "enrichment_queue", type_="foreignkey")
    op.drop_column("enrichment_queue", "raw_listing_id")
    op.drop_column("enrichment_queue", "job_source_id")
