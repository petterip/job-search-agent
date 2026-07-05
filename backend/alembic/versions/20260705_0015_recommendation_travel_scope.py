"""recommendation travel scope columns

Revision ID: 20260705_0015
Revises: 20260705_0014
Create Date: 2026-07-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260705_0015"
down_revision: Union[str, None] = "20260705_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "recommendations",
        sa.Column("commutable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "recommendations",
        sa.Column("full_remote", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "recommendations",
        sa.Column(
            "commutable_or_full_remote",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("recommendations", sa.Column("commutable_rank", sa.Integer(), nullable=True))
    op.add_column(
        "recommendations",
        sa.Column("commutable_or_full_remote_rank", sa.Integer(), nullable=True),
    )
    op.add_column("recommendations", sa.Column("nationwide_rank", sa.Integer(), nullable=True))
    op.add_column("recommendations", sa.Column("travel_status", sa.Text(), nullable=True))
    op.add_column("recommendations", sa.Column("travel_reason_code", sa.Text(), nullable=True))
    op.add_column("recommendations", sa.Column("travel_duration_seconds", sa.Integer(), nullable=True))
    op.add_column("recommendations", sa.Column("travel_distance_km", sa.Integer(), nullable=True))
    op.add_column("recommendations", sa.Column("travel_origin_address", sa.Text(), nullable=True))
    op.add_column("recommendations", sa.Column("travel_commute_limit_minutes", sa.Integer(), nullable=True))
    op.add_column("recommendations", sa.Column("travel_routing_profile", sa.Text(), nullable=True))
    op.create_index(
        "ix_recommendations_profile_active_commutable_rank",
        "recommendations",
        ["profile_id", "is_active", "commutable", "commutable_rank"],
    )
    op.create_index(
        "ix_recommendations_profile_active_nationwide_rank",
        "recommendations",
        ["profile_id", "is_active", "nationwide_rank"],
    )
    op.create_index(
        "ix_recommendations_profile_active_commutable_or_remote_rank",
        "recommendations",
        ["profile_id", "is_active", "commutable_or_full_remote", "commutable_or_full_remote_rank"],
    )
    op.execute(
        """
        update recommendations
        set nationwide_rank = rank,
            commutable = false
        where rank is not null
        """
    )
    op.execute(
        """
        update recommendations r
        set commutable = true,
            commutable_or_full_remote = true,
            commutable_rank = r.rank,
            commutable_or_full_remote_rank = r.rank,
            travel_status = coalesce(r.travel_status, 'exact_home_city'),
            travel_reason_code = coalesce(r.travel_reason_code, 'exact_home_city')
        from jobs j, job_seeker_profiles p
        where j.id = r.job_id
          and p.id = r.profile_id
          and r.rank is not null
          and nullif(trim(p.profile->'location'->>'home_city'), '') is not null
          and trim(
                split_part(
                    split_part(lower(coalesce(j.location, '')), '/', 1),
                    ',',
                    1
                )
              ) = lower(trim(p.profile->'location'->>'home_city'))
        """
    )
    op.execute(
        """
        update recommendations r
        set full_remote = true,
            commutable_or_full_remote = true,
            commutable_or_full_remote_rank = coalesce(r.commutable_or_full_remote_rank, r.rank),
            travel_status = coalesce(r.travel_status, 'full_remote'),
            travel_reason_code = coalesce(r.travel_reason_code, 'full_remote')
        from jobs j
        where j.id = r.job_id
          and r.rank is not null
          and (
              lower(coalesce(j.location, '')) in (
                  'etä',
                  'etätyö',
                  'kokopäiväinen etätyö',
                  'remote',
                  'etätyö koko työaika'
              )
              or (
                  lower(coalesce(j.location, '')) like '%/ etä%'
                  and lower(coalesce(j.location, '')) not like '%/ hybridi%'
                  and lower(coalesce(j.location, '')) not like '%hybrid%'
                  and lower(coalesce(j.location, '')) not like '%etätyömahdollisuus%'
                  and lower(coalesce(j.location, '')) not like '%mahdollisuus etä%'
                  and lower(coalesce(j.location, '')) not like '%osittain etä%'
                  and lower(coalesce(j.location, '')) not like '%osittainen etä%'
              )
          )
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recommendations_profile_active_commutable_or_remote_rank",
        table_name="recommendations",
    )
    op.drop_index("ix_recommendations_profile_active_nationwide_rank", table_name="recommendations")
    op.drop_index("ix_recommendations_profile_active_commutable_rank", table_name="recommendations")
    op.drop_column("recommendations", "travel_routing_profile")
    op.drop_column("recommendations", "travel_commute_limit_minutes")
    op.drop_column("recommendations", "travel_origin_address")
    op.drop_column("recommendations", "travel_distance_km")
    op.drop_column("recommendations", "travel_duration_seconds")
    op.drop_column("recommendations", "travel_reason_code")
    op.drop_column("recommendations", "travel_status")
    op.drop_column("recommendations", "nationwide_rank")
    op.drop_column("recommendations", "commutable_or_full_remote_rank")
    op.drop_column("recommendations", "commutable_rank")
    op.drop_column("recommendations", "commutable_or_full_remote")
    op.drop_column("recommendations", "full_remote")
    op.drop_column("recommendations", "commutable")
