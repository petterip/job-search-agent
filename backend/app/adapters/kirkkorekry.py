from app.adapters.talentech_regional import TalentechRegionalAdapter, TalentechRegionalConfig

KIRKKOREKRY_BASE = "https://kirkkorekry.fi"
KIRKKOREKRY_REGIONAL_PATHS = (
    "oulu",
    "pohjois-pohjanmaa",
    "lappi",
)


class KirkkorekryAdapter(TalentechRegionalAdapter):
    def __init__(self) -> None:
        super().__init__(
            TalentechRegionalConfig(
                source_name="kirkkorekry",
                source_method="processwire_format_json",
                base_url=KIRKKOREKRY_BASE,
                regional_paths=KIRKKOREKRY_REGIONAL_PATHS,
                poll_interval_min=60,
            )
        )
