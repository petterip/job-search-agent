from __future__ import annotations

import asyncio
from datetime import datetime
import html
import math
import logging
import re
from urllib.parse import urljoin

import httpx
from dateutil.parser import isoparse

from app.adapters.base import CollectionFetchResult, NormalizedListing, USER_AGENT, payload_content_hash
from app.config import get_settings

logger = logging.getLogger("collector.linkedin")

LINKEDIN_GUEST_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def _clean_html_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_linkedin_guest_jobs(markup: str) -> list[dict]:
    jobs: list[dict] = []
    cards = re.findall(r'<li\b[^>]*>(.*?)</li>', markup, re.I | re.S)
    for card in cards:
        href_match = re.search(r'<a[^>]+href=["\']([^"\']+)["\']', card, re.I | re.S)
        if not href_match:
            continue
        url = href_match.group(1).split("?")[0]
        job_id_match = re.search(r"-(\d+)(?:/)?$", url)
        title_match = re.search(
            r'class=["\'][^"\']*base-search-card__title[^"\']*["\'][^>]*>(.*?)</',
            card,
            re.I | re.S,
        )
        company_match = re.search(
            r'class=["\'][^"\']*base-search-card__subtitle[^"\']*["\'][^>]*>(.*?)</',
            card,
            re.I | re.S,
        )
        location_match = re.search(
            r'class=["\'][^"\']*job-search-card__location[^"\']*["\'][^>]*>(.*?)</',
            card,
            re.I | re.S,
        )
        time_match = re.search(r'<time[^>]+datetime=["\']([^"\']+)["\']', card, re.I)
        title = _clean_html_text(title_match.group(1)) if title_match else ""
        if not title:
            continue
        jobs.append(
            {
                "id": job_id_match.group(1) if job_id_match else url,
                "url": urljoin("https://www.linkedin.com", url),
                "title": title,
                "company": _clean_html_text(company_match.group(1)) if company_match else None,
                "location": _clean_html_text(location_match.group(1)) if location_match else None,
                "date": time_match.group(1) if time_match else None,
            }
        )
    return jobs


class LinkedinAdapter:
    source_name = "linkedin"
    source_method = "guest_http_search"
    poll_interval_min = 360

    def __init__(self) -> None:
        settings = get_settings()
        self.enabled = settings.linkedin_enabled
        self.max_results = settings.linkedin_max_results_per_run
        self.request_delay_seconds = settings.linkedin_request_delay_seconds
        self.search_queries = settings.linkedin_search_queries
        self.url = LINKEDIN_GUEST_SEARCH_URL

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
    ) -> CollectionFetchResult:
        if not self.enabled:
            logger.warning("event=linkedin_skipped reason=LINKEDIN_ENABLED=false")
            return CollectionFetchResult(listings=[], newest_watermark=watermark, pages_fetched=0)

        effective_page_size = page_size or 25
        max_results = max_urls or self.max_results
        search_queries = [query for query in self.search_queries if query.strip()]
        if not search_queries:
            logger.warning("event=linkedin_skipped reason=no_search_queries")
            return CollectionFetchResult(listings=[], newest_watermark=watermark, pages_fetched=0)
        max_pages = max_pages or max(
            len(search_queries),
            math.ceil(max_results / effective_page_size),
        )
        listings_by_id: dict[str, NormalizedListing] = {}
        newest_watermark = watermark
        pages_fetched = 0

        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
            for page in range(max_pages):
                if len(listings_by_id) >= max_results:
                    break
                query = search_queries[page % len(search_queries)]
                page_index = page // len(search_queries)
                response = await client.get(
                    self.url,
                    params={
                        "keywords": query,
                        "location": "Finland",
                        "f_TPR": "r604800",
                        "start": page_index * effective_page_size,
                    },
                )
                if response.status_code in {401, 403, 429}:
                    logger.warning("event=linkedin_skipped status_code=%s", response.status_code)
                    break
                response.raise_for_status()
                pages_fetched += 1
                rows = parse_linkedin_guest_jobs(response.text)
                if not rows:
                    break
                for row in rows:
                    listing = self.normalize(row)
                    if watermark is not None and listing.published_at is not None:
                        if listing.published_at <= watermark:
                            continue
                    listings_by_id[listing.external_id] = listing
                    if listing.published_at is not None and (
                        newest_watermark is None or listing.published_at > newest_watermark
                    ):
                        newest_watermark = listing.published_at
                    if len(listings_by_id) >= max_results:
                        break
                if page + 1 < max_pages and self.request_delay_seconds > 0:
                    await asyncio.sleep(self.request_delay_seconds)

        return CollectionFetchResult(
            listings=list(listings_by_id.values()),
            newest_watermark=newest_watermark,
            pages_fetched=pages_fetched,
        )

    def normalize(self, payload: dict) -> NormalizedListing:
        published_at = isoparse(payload["date"]) if payload.get("date") else None
        stored_payload = {"source": self.source_name, **payload}
        return NormalizedListing(
            external_id=str(payload["id"]),
            canonical_source_url=str(payload["url"]),
            title=str(payload["title"]).strip(),
            employer=payload.get("company"),
            description=None,
            location=payload.get("location"),
            published_at=published_at,
            content_hash=payload_content_hash(stored_payload),
            payload=stored_payload,
            application_url=str(payload["url"]),
        )
