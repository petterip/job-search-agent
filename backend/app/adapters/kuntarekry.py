from app.adapters.talentech_regional import TalentechRegionalAdapter, TalentechRegionalConfig

KUNTAREKRY_BASE = "https://kuntarekry.fi"
KUNTAREKRY_REGIONAL_PATHS = (
    "oulu",
    "pohjois-pohjanmaa",
    "lappi",
    "rovaniemi",
    "oulun-kaupunki",
    "kemijarvi",
)


class KuntarekryAdapter(TalentechRegionalAdapter):
    def __init__(self) -> None:
        super().__init__(
            TalentechRegionalConfig(
                source_name="kuntarekry",
                source_method="processwire_format_json",
                base_url=KUNTAREKRY_BASE,
                regional_paths=KUNTAREKRY_REGIONAL_PATHS,
                poll_interval_min=60,
            )
        )
