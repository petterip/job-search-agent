from __future__ import annotations

from dataclasses import dataclass
import logging

from app.adapters.base import (
    CollectionFetchResult,
    URL_REASON_REJECTED,
    async_source_redirect_guard,
    ensure_aware_utc,
    is_newer_than_watermark,
    log_url_rejection,
)
from app.adapters.talentech import (
    TALENTECH_USER_AGENT,
    extract_talentech_description,
    normalize_talentech_summary,
    talentech_canonical_url,
)

logger = logging.getLogger("collector.talentech_org_shard")


@dataclass(frozen=True)
class TalentechOrgShardConfig:
    source_name: str
    source_method: str
    site_root: str
    poll_interval_min: int = 360
    shard_fetch_delay_s: float = 0.15


def parse_filters_organisation_ids(filters_payload: dict) -> list[str]:
    organisation_ids: list[str] = []
    seen: set[str] = set()
    for row in filters_payload.get("organisations", []):
        if not isinstance(row, dict):
            continue
        organisation_id = str(row.get("id", "")).strip()
        if not organisation_id or organisation_id in seen:
            continue
        seen.add(organisation_id)
        organisation_ids.append(organisation_id)
    return organisation_ids


class TalentechOrgShardAdapter:
    def __init__(self, config: TalentechOrgShardConfig) -> None:
        self.config = config

    @property
    def source_name(self) -> str:
        return self.config.source_name

    @property
    def source_method(self) -> str:
        return self.config.source_method

    @property
    def poll_interval_min(self) -> int:
        return self.config.poll_interval_min

    @property
    def url(self) -> str:
        return f"{self.config.site_root.rstrip('/')}/fi/api/filters-data/"

    async def collect(
        self,
        *,
        watermark,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
    ) -> CollectionFetchResult:
        import asyncio

        import httpx

        summaries_by_id: dict[int, dict] = {}
        pages_fetched = 0

        async with httpx.AsyncClient(
            timeout=60,
            headers={"User-Agent": TALENTECH_USER_AGENT},
            follow_redirects=True,
            event_hooks={"response": [async_source_redirect_guard(self.source_name)]},
        ) as client:
            filters_response = await client.get(
                self.url,
                headers={"Accept": "application/json"},
            )
            filters_response.raise_for_status()
            pages_fetched += 1
            organisation_ids = parse_filters_organisation_ids(filters_response.json())
            if max_pages is not None:
                organisation_ids = organisation_ids[:max_pages]

            for organisation_id in organisation_ids:
                shard_url = (
                    f"{self.config.site_root.rstrip('/')}/fi/tyopaikat/"
                    f"?format=json&limit=500&sort=-changetime&organisation={organisation_id}"
                )
                shard_response = None
                for attempt in range(4):
                    shard_response = await client.get(
                        shard_url,
                        headers={"Accept": "application/json"},
                    )
                    if shard_response.status_code == 429:
                        await asyncio.sleep(self.config.shard_fetch_delay_s * (2**attempt))
                        continue
                    break
                if shard_response is None:
                    raise RuntimeError(f"failed to fetch shard for {shard_url}")
                if shard_response.status_code == 429:
                    logger.warning(
                        "event=talentech_org_shard_rate_limited organisation=%s",
                        organisation_id,
                    )
                    break
                shard_response.raise_for_status()
                pages_fetched += 1
                payload = shard_response.json()
                if isinstance(payload, list):
                    for row in payload:
                        if isinstance(row, dict) and row.get("id") is not None:
                            summaries_by_id[int(row["id"])] = row
                if self.config.shard_fetch_delay_s > 0:
                    await asyncio.sleep(self.config.shard_fetch_delay_s)

            listings = []
            newest_watermark = watermark
            detail_budget = max_urls

            for summary in summaries_by_id.values():
                published_at = normalize_talentech_summary(
                    summary,
                    source_name=self.source_name,
                    base_url=self.config.site_root,
                ).published_at
                if not is_newer_than_watermark(published_at, watermark):
                    continue
                if detail_budget is not None and detail_budget <= 0:
                    break

                detail_url = talentech_canonical_url(
                    self.config.site_root,
                    str(summary["url"]),
                )
                if detail_url is None:
                    log_url_rejection(
                        logger,
                        source_name=self.source_name,
                        reason=URL_REASON_REJECTED,
                        value=summary.get("url"),
                    )
                    continue
                detail_response = None
                for attempt in range(4):
                    detail_response = await client.get(detail_url)
                    if detail_response.status_code == 429:
                        await asyncio.sleep(self.config.shard_fetch_delay_s * (2**attempt))
                        continue
                    detail_response.raise_for_status()
                    break
                if detail_response is None:
                    raise RuntimeError(f"failed to fetch detail for {detail_url}")
                if detail_response.status_code == 429:
                    logger.warning("event=talentech_detail_rate_limited url=%s", detail_url)
                    continue
                if self.config.shard_fetch_delay_s > 0:
                    await asyncio.sleep(self.config.shard_fetch_delay_s)
                description = extract_talentech_description(detail_response.text)
                listing = normalize_talentech_summary(
                    summary,
                    source_name=self.source_name,
                    base_url=self.config.site_root,
                    description=description,
                )
                listings.append(listing)
                if detail_budget is not None:
                    detail_budget -= 1
                published_at = ensure_aware_utc(listing.published_at)
                if published_at is not None and (
                    newest_watermark is None or published_at > newest_watermark
                ):
                    newest_watermark = published_at

        return CollectionFetchResult(
            listings=listings,
            newest_watermark=newest_watermark,
            pages_fetched=pages_fetched,
        )
