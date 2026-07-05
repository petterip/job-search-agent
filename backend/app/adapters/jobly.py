import json
import html as html_lib
import logging
import re
from datetime import datetime, timezone

from dateutil.parser import isoparse

from app.adapters.base import (
    CollectionFetchResult,
    NormalizedListing,
    USER_AGENT,
    ensure_aware_utc,
    payload_content_hash,
)
from app.config import get_settings
from app.location import jobly_location

logger = logging.getLogger("collector.jobly")


def parse_sitemap_urls(xml: str, *, watermark: datetime | None) -> list[tuple[str, datetime | None]]:
    rows: list[tuple[str, datetime | None]] = []
    for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
        if "/tyopaikka/" not in block:
            continue
        loc_match = re.search(r"<loc>([^<]+)</loc>", block)
        if not loc_match:
            continue
        url = loc_match.group(1).strip()
        mod_match = re.search(r"<lastmod>([^<]+)</lastmod>", block)
        lastmod = isoparse(mod_match.group(1)) if mod_match else None
        if watermark is not None and lastmod is not None and lastmod <= watermark:
            continue
        rows.append((url, lastmod))
    rows.sort(key=lambda item: item[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows


def extract_jobposting_jsonld(html: str) -> dict | None:
    for script in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(script.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        schema_type = data.get("@type")
        if schema_type == "JobPosting" or (
            isinstance(schema_type, list) and "JobPosting" in schema_type
        ):
            return data
    return None


def _meta_content(html: str, *, name: str | None = None, prop: str | None = None) -> str | None:
    if name is not None:
        patterns = (
            rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(name)}["\']',
        )
    else:
        patterns = (
            rf'<meta[^>]+property=["\']{re.escape(prop or "")}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop or "")}["\']',
        )
    for pattern in patterns:
        match = re.search(pattern, html, re.I | re.S)
        if match:
            text = html_lib.unescape(match.group(1).strip())
            if text:
                return text
    return None


def extract_jobly_static_description(html: str) -> str | None:
    for extractor in (
        lambda: _meta_content(html, name="description"),
        lambda: _meta_content(html, prop="og:description"),
        lambda: _meta_content(html, prop="twitter:description"),
    ):
        text = extractor()
        if text and len(text) >= 40:
            return text

    itemprop = re.search(
        r'itemprop=["\']description["\'][^>]*>(.*?)</',
        html,
        re.I | re.S,
    )
    if itemprop:
        text = re.sub(r"<[^>]+>", " ", itemprop.group(1))
        text = html_lib.unescape(re.sub(r"\s+", " ", text).strip())
        if len(text) >= 40:
            return text

    body_match = re.search(
        r'<(?:div|section)[^>]+class=["\'][^"\']*(?:job-description|job__description|description)[^"\']*["\'][^>]*>(.*?)</(?:div|section)>',
        html,
        re.I | re.S,
    )
    if body_match:
        text = re.sub(r"<[^>]+>", " ", body_match.group(1))
        text = html_lib.unescape(re.sub(r"\s+", " ", text).strip())
        if len(text) >= 40:
            return text
    return None


def extract_jobly_detail_payload(html: str, *, source_url: str) -> dict:
    payload = extract_jobposting_jsonld(html)
    if payload is not None:
        return payload

    title_match = re.search(r"<title>([^<]+)</title>", html)
    payload = {
        "url": source_url,
        "title": title_match.group(1).strip() if title_match else source_url,
    }
    description = extract_jobly_static_description(html)
    if description:
        payload["description"] = description
    return payload


def jobly_external_id(url: str) -> str:
    match = re.search(r"-(\d+)(?:/)?$", url.rstrip("/"))
    return match.group(1) if match else url


class JoblyAdapter:
    source_name = "jobly"
    source_method = "sitemap_jsonld"
    poll_interval_min = 60

    def __init__(self) -> None:
        settings = get_settings()
        self.sitemap_urls = settings.jobly_sitemap_urls
        self.max_urls = settings.jobly_max_urls_per_run

    @property
    def url(self) -> str:
        return self.sitemap_urls[0] if self.sitemap_urls else "https://www.jobly.fi/sitemap.xml?page=1"

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
    ) -> CollectionFetchResult:
        import httpx

        effective_max_urls = max_urls or self.max_urls
        candidate_urls: list[tuple[str, datetime | None]] = []

        async with httpx.AsyncClient(timeout=60, headers={"User-Agent": USER_AGENT}) as client:
            for sitemap_url in self.sitemap_urls:
                response = await client.get(sitemap_url)
                response.raise_for_status()
                candidate_urls.extend(parse_sitemap_urls(response.text, watermark=watermark))

            listings: list[NormalizedListing] = []
            newest_watermark = watermark
            fetched = 0

            for url, lastmod in candidate_urls[:effective_max_urls]:
                page_response = await client.get(url)
                if page_response.status_code == 404:
                    logger.warning("event=jobly_listing_missing url=%s", url)
                    continue
                page_response.raise_for_status()
                payload = extract_jobly_detail_payload(page_response.text, source_url=url)
                listing = self.normalize(payload, source_url=url)
                listings.append(listing)
                fetched += 1
                lastmod = ensure_aware_utc(lastmod)
                published_at = ensure_aware_utc(listing.published_at)
                if lastmod is not None and (
                    newest_watermark is None or lastmod > newest_watermark
                ):
                    newest_watermark = lastmod
                elif published_at is not None and (
                    newest_watermark is None or published_at > newest_watermark
                ):
                    newest_watermark = published_at

        return CollectionFetchResult(
            listings=listings,
            newest_watermark=newest_watermark,
            pages_fetched=len(self.sitemap_urls),
            stopped_at_watermark=len(candidate_urls) > effective_max_urls,
        )

    def normalize(self, payload: dict, *, source_url: str) -> NormalizedListing:
        external_id = jobly_external_id(source_url)
        title = str(payload.get("title") or "").strip()
        employer = None
        hiring = payload.get("hiringOrganization")
        if isinstance(hiring, dict):
            employer = hiring.get("name")
        description = payload.get("description")
        if isinstance(description, str):
            description = re.sub(r"<[^>]+>", " ", description)
            description = re.sub(r"\s+", " ", description).strip() or None
        else:
            description = None

        published_raw = payload.get("datePosted")
        published_at = isoparse(published_raw) if published_raw else None
        stored_payload = {"source_url": source_url, **payload}

        return NormalizedListing(
            external_id=external_id,
            canonical_source_url=source_url,
            title=title,
            employer=employer,
            description=description,
            location=jobly_location(stored_payload),
            published_at=published_at,
            content_hash=payload_content_hash(stored_payload),
            payload=stored_payload,
            application_url=source_url,
        )
