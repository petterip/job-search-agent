from app.adapters.tmt import TmtAdapter
from app.config import get_settings


class TmtOuluAdapter(TmtAdapter):
    source_name = "tmt_oulu"
    poll_interval_min = 15

    def __init__(self) -> None:
        super().__init__()
        settings = get_settings()
        self.municipality_codes = settings.tmt_oulu_municipality_codes
        self.max_pages = settings.tmt_oulu_max_pages

    def extra_filters(self) -> dict:
        return {"municipalities": list(self.municipality_codes)}
