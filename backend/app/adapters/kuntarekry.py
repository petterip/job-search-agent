from app.adapters.talentech_org_shard import TalentechOrgShardAdapter, TalentechOrgShardConfig
from app.adapters.talentech_regional import TalentechRegionalAdapter, TalentechRegionalConfig
from app.config import get_settings

KUNTAREKRY_BASE = "https://kuntarekry.fi"
KUNTAREKRY_REGIONAL_PATHS = (
    "oulu",
    "pohjois-pohjanmaa",
    "lappi",
    "rovaniemi",
    "oulun-kaupunki",
    "kemijarvi",
)


class KuntarekryAdapter:
    def __init__(self) -> None:
        settings = get_settings()
        mode = settings.kuntarekry_collection_mode.strip().lower()
        if mode == "org_shard":
            self._adapter: TalentechRegionalAdapter | TalentechOrgShardAdapter = (
                TalentechOrgShardAdapter(
                    TalentechOrgShardConfig(
                        source_name="kuntarekry",
                        source_method="processwire_format_json_org_shard",
                        site_root=KUNTAREKRY_BASE,
                        poll_interval_min=60,
                    )
                )
            )
        else:
            self._adapter = TalentechRegionalAdapter(
                TalentechRegionalConfig(
                    source_name="kuntarekry",
                    source_method="processwire_format_json",
                    base_url=KUNTAREKRY_BASE,
                    regional_paths=KUNTAREKRY_REGIONAL_PATHS,
                    poll_interval_min=60,
                )
            )

    @property
    def source_name(self) -> str:
        return self._adapter.source_name

    @property
    def source_method(self) -> str:
        return self._adapter.source_method

    @property
    def poll_interval_min(self) -> int:
        return self._adapter.poll_interval_min

    @property
    def url(self) -> str:
        return self._adapter.url

    async def collect(self, **kwargs):
        return await self._adapter.collect(**kwargs)
