from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Protocol

from dateutil.parser import isoparse

USER_AGENT = "job-search-agent-collector/0.1 (+local research project)"


@dataclass(frozen=True)
class NormalizedListing:
    external_id: str
    canonical_source_url: str
    title: str
    employer: str | None
    description: str | None
    location: str | None
    published_at: datetime | None
    content_hash: str
    payload: dict
    attribution: str | None = None
    application_url: str | None = None


@dataclass(frozen=True)
class CollectionFetchResult:
    listings: list[NormalizedListing]
    newest_watermark: datetime | None = None
    pages_fetched: int = 0
    stopped_at_watermark: bool = False


def payload_content_hash(payload: dict) -> str:
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return sha256(normalized.encode("utf-8")).hexdigest()


def ensure_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_newer_than_watermark(published_at: datetime | None, watermark: datetime | None) -> bool:
    published_at = ensure_aware_utc(published_at)
    watermark = ensure_aware_utc(watermark)
    if watermark is None:
        return True
    if published_at is None:
        return True
    return published_at > watermark


def collect_page_results(
    results: list[dict],
    *,
    watermark: datetime | None,
) -> tuple[list[dict], datetime | None, bool]:
    collected: list[dict] = []
    newest: datetime | None = None
    stopped_at_watermark = False

    for row in results:
        published_raw = row.get("date_posted")
        published_at = isoparse(published_raw) if published_raw else None
        if not is_newer_than_watermark(published_at, watermark):
            stopped_at_watermark = True
            break
        collected.append(row)
        if published_at is not None and (newest is None or published_at > newest):
            newest = published_at

    return collected, newest, stopped_at_watermark


class SourceAdapter(Protocol):
    source_name: str
    source_method: str
    poll_interval_min: int
    url: str

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
    ) -> CollectionFetchResult: ...
