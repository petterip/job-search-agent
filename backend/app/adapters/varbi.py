import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from dateutil.parser import isoparse

from app.adapters.base import (
    CollectionFetchResult,
    NormalizedListing,
    USER_AGENT,
    is_newer_than_watermark,
    payload_content_hash,
)
from app.config import get_settings


def varbi_job_id_from_link(link: str) -> str | None:
    match = re.search(r"jobID:(\d+)", link)
    return match.group(1) if match else None


def varbi_fi_job_url(base_url: str, job_id: str) -> str:
    return f"{base_url.rstrip('/')}/fi/what:job/jobID:{job_id}/"


def parse_varbi_rss_items(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    items: list[dict] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_raw = item.findtext("pubDate")
        published_at = None
        if pub_raw:
            try:
                published_at = parsedate_to_datetime(pub_raw)
            except (TypeError, ValueError, OverflowError):
                published_at = None
        job_id = varbi_job_id_from_link(link)
        if not job_id:
            continue
        items.append(
            {
                "job_id": job_id,
                "title": title,
                "link": link,
                "published_at": published_at,
            }
        )
    return items


def extract_varbi_description(html: str) -> str | None:
    match = re.search(r'class="job-desc mb"[^>]*>(.*?)</div>', html, re.S | re.I)
    if not match:
        return None
    text = re.sub(r"<[^>]+>", " ", match.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


class OuluVarbiAdapter:
    source_name = "oulu_varbi"
    source_method = "rss_then_detail_html"
    poll_interval_min = 60

    def __init__(self) -> None:
        settings = get_settings()
        self.rss_url = settings.oulu_varbi_rss_url
        self.base_url = settings.oulu_varbi_base_url

    @property
    def url(self) -> str:
        return self.rss_url

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
    ) -> CollectionFetchResult:
        import httpx

        listings: list[NormalizedListing] = []
        newest_watermark: datetime | None = watermark

        async with httpx.AsyncClient(timeout=60, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(self.rss_url)
            response.raise_for_status()
            rss_items = parse_varbi_rss_items(response.text)

            for item in rss_items:
                published_at = item.get("published_at")
                if isinstance(published_at, datetime) and published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                if not is_newer_than_watermark(published_at, watermark):
                    continue

                job_id = str(item["job_id"])
                detail_url = varbi_fi_job_url(self.base_url, job_id)
                detail_response = await client.get(detail_url)
                detail_response.raise_for_status()
                description = extract_varbi_description(detail_response.text)
                listing = self.normalize(
                    {
                        **item,
                        "description": description,
                        "detail_url": detail_url,
                    }
                )
                listings.append(listing)
                if listing.published_at is not None and (
                    newest_watermark is None or listing.published_at > newest_watermark
                ):
                    newest_watermark = listing.published_at

        return CollectionFetchResult(
            listings=listings,
            newest_watermark=newest_watermark,
            pages_fetched=1,
        )

    def normalize(self, payload: dict) -> NormalizedListing:
        job_id = str(payload["job_id"])
        detail_url = str(payload["detail_url"])
        title = str(payload.get("title") or "").strip()
        published_at = payload.get("published_at")
        if isinstance(published_at, str):
            published_at = isoparse(published_at)
        description = payload.get("description")
        stored_payload = {
            "source": self.source_name,
            "rss_link": payload.get("link"),
            "job_id": job_id,
            "title": title,
            "detail_url": detail_url,
            "description": description,
        }
        if isinstance(published_at, datetime):
            stored_payload["published_at"] = published_at.isoformat()

        return NormalizedListing(
            external_id=job_id,
            canonical_source_url=detail_url,
            title=title,
            employer="Oulun yliopisto",
            description=description,
            location="Oulu",
            published_at=published_at,
            content_hash=payload_content_hash(stored_payload),
            payload=stored_payload,
            application_url=detail_url,
        )
