import html
import re
from datetime import datetime
from urllib.parse import urlparse

from dateutil.parser import isoparse

from app.adapters.base import (
    CollectionFetchResult,
    NormalizedListing,
    USER_AGENT,
    payload_content_hash,
)
from app.config import get_settings
from app.location import laura_location


def laura_rendered(field: dict | str | None) -> str:
    if isinstance(field, dict):
        value = str(field.get("rendered") or "").strip()
    else:
        value = str(field or "").strip()
    return html.unescape(re.sub(r"<[^>]+>", " ", value)).strip()


def laura_content_text(field: dict | str | None) -> str:
    if isinstance(field, dict):
        value = str(field.get("rendered") or "")
    else:
        value = str(field or "")
    value = re.sub(r"</?(p|div|section|article|h[1-6]|ul|ol|li|br)\b[^>]*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n\n", value)
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def laura_employer_from_link(link: str) -> str | None:
    parts = [part for part in urlparse(link).path.split("/") if part]
    try:
        company_slug = parts[parts.index("avoimet-tyopaikat") + 1]
    except (ValueError, IndexError):
        return None
    words = []
    for part in company_slug.split("-"):
        lower = part.casefold()
        if lower in {"oy", "ab", "ry"}:
            words.append(lower.capitalize())
        else:
            words.append(part.capitalize())
    return " ".join(words) if words else None


class LauraAdapter:
    source_name = "laura"
    source_method = "wordpress_rest"
    poll_interval_min = 5

    def __init__(self) -> None:
        settings = get_settings()
        self.url = settings.laura_url
        self.page_size = settings.laura_page_size
        self.max_pages = settings.laura_max_pages

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
        seen_ids: set[int] = set()
        listings: list[NormalizedListing] = []
        newest_watermark: datetime | None = None
        total_pages_fetched = 0

        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
            payloads_by_id: dict[int, dict] = {}
            for orderby, watermark_param in (
                ("date", "after"),
                ("modified", "modified_after"),
            ):
                for page in range(1, effective_max_pages + 1):
                    params: dict[str, str | int] = {
                        "per_page": effective_page_size,
                        "orderby": orderby,
                        "order": "desc",
                        "page": page,
                    }
                    if watermark is not None:
                        params[watermark_param] = watermark.strftime("%Y-%m-%dT%H:%M:%S")

                    response = await client.get(self.url, params=params)
                    response.raise_for_status()
                    rows = response.json()
                    total_pages_fetched += 1
                    if not isinstance(rows, list) or not rows:
                        break

                    for payload in rows:
                        listing_id = int(payload["id"])
                        if listing_id in seen_ids:
                            continue
                        seen_ids.add(listing_id)
                        payloads_by_id[listing_id] = payload
                        listing = self.normalize(payload)
                        candidate = listing.published_at
                        modified_raw = payload.get("modified_gmt")
                        if modified_raw:
                            modified_at = isoparse(modified_raw)
                            if candidate is None or modified_at > candidate:
                                candidate = modified_at
                        if candidate is not None and (
                            newest_watermark is None or candidate > newest_watermark
                        ):
                            newest_watermark = candidate

                    if len(rows) < effective_page_size:
                        break

            for query in get_settings().discovery_search_queries:
                params: dict[str, str | int] = {
                    "search": query,
                    "per_page": min(effective_page_size, 20),
                    "orderby": "date",
                    "order": "desc",
                    "page": 1,
                }
                response = await client.get(self.url, params=params)
                response.raise_for_status()
                rows = response.json()
                total_pages_fetched += 1
                if not isinstance(rows, list):
                    continue
                for payload in rows:
                    listing_id = int(payload["id"])
                    payloads_by_id[listing_id] = payload
                    listing = self.normalize(payload)
                    candidate = listing.published_at
                    modified_raw = payload.get("modified_gmt")
                    if modified_raw:
                        modified_at = isoparse(modified_raw)
                        if candidate is None or modified_at > candidate:
                            candidate = modified_at
                    if candidate is not None and (
                        newest_watermark is None or candidate > newest_watermark
                    ):
                        newest_watermark = candidate

        listings = [self.normalize(payload) for payload in payloads_by_id.values()]

        return CollectionFetchResult(
            listings=listings,
            newest_watermark=newest_watermark,
            pages_fetched=total_pages_fetched,
        )

    def normalize(self, payload: dict) -> NormalizedListing:
        external_id = str(payload["id"])
        published_raw = payload.get("date_gmt") or payload.get("date")
        published_at = isoparse(published_raw) if published_raw else None
        link = str(payload.get("link") or "").strip()
        content = laura_content_text(payload.get("content"))

        return NormalizedListing(
            external_id=external_id,
            canonical_source_url=link or f"https://laura.fi/?p={external_id}",
            title=laura_rendered(payload.get("title")),
            employer=laura_employer_from_link(link),
            description=content or None,
            location=laura_location(payload),
            published_at=published_at,
            content_hash=payload_content_hash(payload),
            payload=payload,
            application_url=link or None,
        )
