from app.adapters.talentech_org_shard import TalentechOrgShardAdapter, TalentechOrgShardConfig

VALTIOLLE_SITE_ROOT = "https://valtiolle.fi"


class ValtiolleAdapter(TalentechOrgShardAdapter):
    def __init__(self) -> None:
        super().__init__(
            TalentechOrgShardConfig(
                source_name="valtiolle",
                source_method="processwire_format_json_org_shard",
                site_root=VALTIOLLE_SITE_ROOT,
                poll_interval_min=360,
            )
        )
