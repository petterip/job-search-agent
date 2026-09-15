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

logger = logging.getLogger("collector.talentech")


@dataclass(frozen=True)
class TalentechRegionalConfig:
    source_name: str
    source_method: str
    base_url: str
    regional_paths: tuple[str, ...]
    poll_interval_min: int = 60
    detail_fetch_delay_s: float = 0.75


class TalentechRegionalAdapter:
    def __init__(self, config: TalentechRegionalConfig) -> None:
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
        first = self.config.regional_paths[0]
        return f"{self.config.base_url}/fi/tyopaikat/{first}?format=json"

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
            partial = False
            partial_warnings: list[str] = []
            for regional_path in self.config.regional_paths:
                shard_url = f"{self.config.base_url}/fi/tyopaikat/{regional_path}?format=json"
                response = None
                for attempt in range(4):
                    response = await client.get(shard_url, headers={"Accept": "application/json"})
                    if response.status_code == 429:
                        await asyncio.sleep(self.config.detail_fetch_delay_s * (2**attempt))
                        continue
                    response.raise_for_status()
                    break
                if response is None:
                    raise RuntimeError(f"failed to fetch shard for {shard_url}")
                if response.status_code == 429:
                    partial = True
                    partial_warnings.append("shard_rate_limited")
                    logger.warning("event=talentech_shard_rate_limited url=%s", shard_url)
                    continue
                response.raise_for_status()
                pages_fetched += 1
                payload = response.json()
                if not isinstance(payload, list):
                    if self.config.detail_fetch_delay_s > 0:
                        await asyncio.sleep(self.config.detail_fetch_delay_s)
                    continue
                for row in payload:
                    if isinstance(row, dict) and row.get("id") is not None:
                        summaries_by_id[int(row["id"])] = row
                if self.config.detail_fetch_delay_s > 0:
                    await asyncio.sleep(self.config.detail_fetch_delay_s)

            listings = []
            newest_watermark = watermark

            for summary in summaries_by_id.values():
                published_at = normalize_talentech_summary(
                    summary,
                    source_name=self.source_name,
                    base_url=self.config.base_url,
                ).published_at
                if not is_newer_than_watermark(published_at, watermark):
                    continue

                detail_url = talentech_canonical_url(
                    self.config.base_url,
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
                        await asyncio.sleep(self.config.detail_fetch_delay_s * (2**attempt))
                        continue
                    detail_response.raise_for_status()
                    break
                if detail_response is None:
                    raise RuntimeError(f"failed to fetch detail for {detail_url}")
                if detail_response.status_code == 429:
                    partial = True
                    partial_warnings.append("detail_rate_limited")
                    logger.warning("event=talentech_detail_rate_limited url=%s", detail_url)
                    continue
                if self.config.detail_fetch_delay_s > 0:
                    await asyncio.sleep(self.config.detail_fetch_delay_s)
                description = extract_talentech_description(detail_response.text)
                listing = normalize_talentech_summary(
                    summary,
                    source_name=self.source_name,
                    base_url=self.config.base_url,
                    description=description,
                )
                listings.append(listing)
                published_at = ensure_aware_utc(listing.published_at)
                if published_at is not None and (
                    newest_watermark is None or published_at > newest_watermark
                ):
                    newest_watermark = published_at

        return CollectionFetchResult(
            listings=listings,
            newest_watermark=newest_watermark,
            pages_fetched=pages_fetched,
            outcome="partial" if partial else "delta",
            warnings=sorted(set(partial_warnings)),
        )
