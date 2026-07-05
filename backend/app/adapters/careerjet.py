from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
import logging

import httpx

from app.adapters.base import CollectionFetchResult, NormalizedListing, USER_AGENT, payload_content_hash
from app.config import get_settings

logger = logging.getLogger("collector.careerjet")


def parse_careerjet_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


class CareerjetAdapter:
    source_name = "careerjet"
    source_method = "publisher_api_v4"
    poll_interval_min = 360

    def __init__(self) -> None:
        settings = get_settings()
        self.url = "https://search.api.careerjet.net/v4/query"
        self.api_key = settings.careerjet_api_key
        self.max_results = settings.careerjet_max_results_per_run
        self.user_ip = settings.careerjet_user_ip

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
    ) -> CollectionFetchResult:
        if not self.api_key.strip():
            logger.warning("event=careerjet_skipped reason=missing_api_key")
            return CollectionFetchResult(listings=[], newest_watermark=watermark, pages_fetched=0)

        effective_page_size = min(page_size or self.max_results, 100)
        max_results = max_urls or self.max_results
        max_pages = max_pages or max(1, (max_results + effective_page_size - 1) // effective_page_size)
        listings: list[NormalizedListing] = []
        newest_watermark = watermark
        pages_fetched = 0

        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
            for page in range(1, max_pages + 1):
                if len(listings) >= max_results:
                    break
                response = await client.get(
                    self.url,
                    params={
                        "locale_code": "fi_FI",
                        "location": "Finland",
                        "sort": "date",
                        "page": page,
                        "page_size": min(effective_page_size, max_results - len(listings)),
                        "fragment_size": 600,
                        "user_ip": self.user_ip,
                        "user_agent": USER_AGENT,
                    },
                    auth=(self.api_key, ""),
                )
                response.raise_for_status()
                payload = response.json()
                pages_fetched += 1
                if payload.get("type") != "JOBS":
                    logger.warning("event=careerjet_location_mode message=%s", payload.get("message"))
                    break
                rows = [row for row in payload.get("jobs", []) if isinstance(row, dict)]
                if not rows:
                    break
                for row in rows:
                    listing = self.normalize(row)
                    if watermark is not None and listing.published_at is not None:
                        if listing.published_at <= watermark:
                            continue
                    listings.append(listing)
                    if listing.published_at is not None and (
                        newest_watermark is None or listing.published_at > newest_watermark
                    ):
                        newest_watermark = listing.published_at
                    if len(listings) >= max_results:
                        break
                if page >= int(payload.get("pages") or page):
                    break

        return CollectionFetchResult(
            listings=listings,
            newest_watermark=newest_watermark,
            pages_fetched=pages_fetched,
        )

    def normalize(self, payload: dict) -> NormalizedListing:
        url = str(payload.get("url") or "")
        external_id = url or f"{payload.get('site', '')}:{payload.get('title', '')}:{payload.get('date', '')}"
        stored_payload = {"source": self.source_name, **payload}
        return NormalizedListing(
            external_id=external_id,
            canonical_source_url=url,
            title=str(payload.get("title") or "").strip(),
            employer=str(payload.get("company") or "").strip() or None,
            description=str(payload.get("description") or "").strip() or None,
            location=str(payload.get("locations") or "").strip() or None,
            published_at=parse_careerjet_date(payload.get("date")),
            content_hash=payload_content_hash(stored_payload),
            payload=stored_payload,
            application_url=url,
        )
