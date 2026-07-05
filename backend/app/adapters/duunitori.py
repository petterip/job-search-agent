from dataclasses import dataclass
from datetime import datetime

import httpx
from dateutil.parser import isoparse

from app.adapters.base import (
    CollectionFetchResult,
    NormalizedListing,
    USER_AGENT,
    collect_page_results,
    payload_content_hash,
)
from app.config import get_settings
from app.location import append_work_mode, detect_work_mode, infer_city_from_text


def duunitori_job_url(slug: str) -> str:
    return f"https://duunitori.fi/tyopaikat/tyo/{slug}"


@dataclass(frozen=True)
class DuunitoriFetchResult:
    payloads: list[dict]
    newest_published_at: datetime | None
    pages_fetched: int
    stopped_at_watermark: bool


class DuunitoriAdapter:
    source_name = "duunitori"
    source_method = "json_api"
    poll_interval_min = 5

    def __init__(self) -> None:
        settings = get_settings()
        self.url = settings.duunitori_url
        self.page_size = settings.duunitori_page_size
        self.max_pages = settings.duunitori_max_pages

    async def fetch_page(self, *, page_size: int = 20, page: int = 1) -> list[dict]:
        params = {
            "ordering": "-date_posted",
            "page_size": page_size,
            "page": page,
        }
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(self.url, params=params)
            response.raise_for_status()
            data = response.json()
            return list(data.get("results", []))

    async def fetch_search(self, *, query: str, page_size: int = 20) -> list[dict]:
        params = {
            "ordering": "-date_posted",
            "page_size": page_size,
            "page": 1,
            "search": query,
        }
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(self.url, params=params)
            response.raise_for_status()
            data = response.json()
            return list(data.get("results", []))

    async def fetch_since_watermark(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
    ) -> DuunitoriFetchResult:
        effective_page_size = page_size or self.page_size
        effective_max_pages = max_pages or self.max_pages
        payloads: list[dict] = []
        newest_published_at: datetime | None = None
        pages_fetched = 0
        stopped_at_watermark = False

        for page in range(1, effective_max_pages + 1):
            results = await self.fetch_page(page_size=effective_page_size, page=page)
            if not results:
                break

            page_payloads, page_newest, page_stopped = collect_page_results(
                results,
                watermark=watermark,
            )
            payloads.extend(page_payloads)
            if page_newest is not None and (
                newest_published_at is None or page_newest > newest_published_at
            ):
                newest_published_at = page_newest

            pages_fetched += 1
            if page_stopped:
                stopped_at_watermark = True
                break

            if len(results) < effective_page_size:
                break

        return DuunitoriFetchResult(
            payloads=payloads,
            newest_published_at=newest_published_at,
            pages_fetched=pages_fetched,
            stopped_at_watermark=stopped_at_watermark,
        )

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
    ) -> CollectionFetchResult:
        fetch_result = await self.fetch_since_watermark(
            watermark=watermark,
            page_size=page_size,
            max_pages=max_pages,
        )
        payloads_by_slug = {str(payload["slug"]): payload for payload in fetch_result.payloads}
        pages_fetched = fetch_result.pages_fetched
        for query in get_settings().discovery_search_queries:
            for payload in await self.fetch_search(query=query, page_size=20):
                payloads_by_slug[str(payload["slug"])] = payload
            pages_fetched += 1
        return CollectionFetchResult(
            listings=[self.normalize(payload) for payload in payloads_by_slug.values()],
            newest_watermark=fetch_result.newest_published_at,
            pages_fetched=pages_fetched,
            stopped_at_watermark=fetch_result.stopped_at_watermark,
        )

    def normalize(self, payload: dict) -> NormalizedListing:
        slug = str(payload["slug"])
        published_raw = payload.get("date_posted")
        published_at = isoparse(published_raw) if published_raw else None

        job_url = duunitori_job_url(slug)

        return NormalizedListing(
            external_id=slug,
            canonical_source_url=job_url,
            title=str(payload.get("heading") or "").strip(),
            employer=(payload.get("company_name") or None),
            description=(payload.get("descr") or None),
            location=append_work_mode(
                payload.get("municipality_name")
                or infer_city_from_text(payload.get("heading"), payload.get("descr"))
                or "Suomi",
                detect_work_mode(payload.get("heading"), payload.get("descr")),
            ),
            published_at=published_at,
            content_hash=payload_content_hash(payload),
            payload=payload,
            application_url=job_url,
        )
