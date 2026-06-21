from datetime import datetime, timezone
import html
import re
from urllib.parse import quote

from app.adapters.base import (
    CollectionFetchResult,
    NormalizedListing,
    USER_AGENT,
    ensure_aware_utc,
    payload_content_hash,
)
from app.config import get_settings
from app.location import eures_location


def epoch_ms_to_datetime(value: int | float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)


def eures_description_text(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"</?(p|div|section|article|h[1-6]|ul|ol|li|br)\b[^>]*>", "\n", value, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text or None


def eures_portal_url(external_id: str) -> str:
    encoded_id = quote(external_id, safe="")
    return (
        "https://europa.eu/eures/portal/jv-se/jv-details/"
        f"{encoded_id}?jvDisplayLanguage=fi&lang=fi"
    )


class EuresAdapter:
    source_name = "eures_fi"
    source_method = "json_api"
    poll_interval_min = 15

    def __init__(self) -> None:
        settings = get_settings()
        self.url = settings.eures_url
        self.page_size = settings.eures_page_size
        self.max_pages = settings.eures_max_pages

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
    ) -> CollectionFetchResult:
        import httpx

        effective_page_size = page_size or self.page_size
        effective_max_pages = max_pages or self.max_pages
        listings: list[NormalizedListing] = []
        newest_watermark: datetime | None = None
        pages_fetched = 0

        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
            for page in range(1, effective_max_pages + 1):
                response = await client.post(
                    self.url,
                    json={
                        "resultsPerPage": effective_page_size,
                        "page": page,
                        "locationCodes": ["fi"],
                        "keywords": [],
                    },
                )
                response.raise_for_status()
                data = response.json()
                rows = list(data.get("jvs") or [])
                pages_fetched += 1
                if not rows:
                    break

                for payload in rows:
                    listing = self.normalize(payload)
                    published_at = ensure_aware_utc(listing.published_at)
                    watermark = ensure_aware_utc(watermark)
                    if watermark is not None and published_at is not None:
                        if published_at <= watermark:
                            continue
                    listings.append(listing)
                    if published_at is not None and (
                        newest_watermark is None or published_at > newest_watermark
                    ):
                        newest_watermark = published_at

                if len(rows) < effective_page_size:
                    break

        return CollectionFetchResult(
            listings=listings,
            newest_watermark=newest_watermark,
            pages_fetched=pages_fetched,
        )

    def normalize(self, payload: dict) -> NormalizedListing:
        external_id = str(payload["id"])
        employer = None
        employer_obj = payload.get("employer")
        if isinstance(employer_obj, dict):
            employer = employer_obj.get("name")
        published_at = epoch_ms_to_datetime(payload.get("creationDate"))
        canonical_source_url = eures_portal_url(external_id)

        return NormalizedListing(
            external_id=external_id,
            canonical_source_url=canonical_source_url,
            title=str(payload.get("title") or "").strip(),
            employer=employer,
            description=eures_description_text(payload.get("description")),
            location=eures_location(payload),
            published_at=published_at,
            content_hash=payload_content_hash(payload),
            payload=payload,
            application_url=canonical_source_url,
        )
