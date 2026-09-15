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
    async_source_redirect_guard,
    ensure_aware_utc,
    log_url_rejection,
    payload_content_hash,
    safe_source_url,
    source_url_rejection_reason,
    validated_external_id,
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
        lastmod = ensure_aware_utc(isoparse(mod_match.group(1))) if mod_match else None
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

    supports_resumable_scan = True

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
        pending_entries: list[tuple[str, str, datetime | None]] | None = None,
    ) -> CollectionFetchResult:
        import httpx

        effective_max_urls = max_urls or self.max_urls
        resumable = pending_entries is not None
        merged: dict[str, tuple[str, datetime | None]] = {}

        async with httpx.AsyncClient(
            timeout=60,
            headers={"User-Agent": USER_AGENT},
            event_hooks={"response": [async_source_redirect_guard(self.source_name)]},
        ) as client:
            for sitemap_url in self.sitemap_urls:
                sitemap_reason = source_url_rejection_reason(self.source_name, sitemap_url)
                if sitemap_reason is not None:
                    log_url_rejection(
                        logger,
                        source_name=self.source_name,
                        reason=sitemap_reason,
                        value=sitemap_url,
                    )
                    continue
                response = await client.get(sitemap_url)
                response.raise_for_status()
                for url, lastmod in parse_sitemap_urls(response.text, watermark=None):
                    external_id = validated_external_id(jobly_external_id(url))
                    if not external_id:
                        log_url_rejection(
                            logger,
                            source_name=self.source_name,
                            reason="invalid_external_id",
                            value=url,
                        )
                        continue
                    existing = merged.get(external_id)
                    # A corrected/newer lastmod and canonical URL are mutable
                    # evidence; the external id remains the occurrence identity.
                    if existing is None or (
                        lastmod is not None
                        and (existing[1] is None or lastmod > existing[1])
                    ):
                        merged[external_id] = (url, lastmod)

            scan_entries = [
                (external_id, url, lastmod)
                for external_id, (url, lastmod) in merged.items()
            ]

            if resumable:
                work = list(pending_entries or [])
            else:
                ordered = sorted(
                    scan_entries,
                    key=lambda item: (
                        item[2] or datetime.min.replace(tzinfo=timezone.utc),
                        item[0],
                    ),
                    reverse=True,
                )
                work = ordered[:effective_max_urls]

            listings: list[NormalizedListing] = []
            outcomes: dict[str, str] = {}
            newest_watermark = watermark

            for external_id, url, lastmod in work:
                url_reason = source_url_rejection_reason(self.source_name, url)
                if url_reason is not None:
                    log_url_rejection(
                        logger,
                        source_name=self.source_name,
                        reason=url_reason,
                        value=url,
                    )
                    outcomes[external_id] = "invalid"
                    continue
                page_response = await client.get(url)
                if page_response.status_code == 404:
                    logger.warning("event=jobly_listing_missing url=%s", url)
                    outcomes[external_id] = "missing"
                    continue
                page_response.raise_for_status()
                try:
                    payload = extract_jobly_detail_payload(page_response.text, source_url=url)
                    listing = self.normalize(payload, source_url=url)
                except Exception:
                    logger.warning(
                        "event=jobly_listing_parse_invalid url=%s", url, exc_info=True
                    )
                    outcomes[external_id] = "invalid"
                    continue
                listings.append(listing)
                outcomes[external_id] = "classified"
                normalized_lastmod = ensure_aware_utc(lastmod)
                published_at = ensure_aware_utc(listing.published_at)
                if normalized_lastmod is not None and (
                    newest_watermark is None or normalized_lastmod > newest_watermark
                ):
                    newest_watermark = normalized_lastmod
                elif published_at is not None and (
                    newest_watermark is None or published_at > newest_watermark
                ):
                    newest_watermark = published_at

        warnings: list[str] = []
        if not resumable and len(scan_entries) > effective_max_urls:
            # Legacy direct callers still truncate; the runner uses the
            # resumable path and drains the frontier across runs instead.
            warnings.append("legacy_truncated_scan")

        return CollectionFetchResult(
            listings=listings,
            newest_watermark=newest_watermark,
            pages_fetched=len(self.sitemap_urls),
            stopped_at_watermark=not resumable and len(scan_entries) > effective_max_urls,
            warnings=warnings,
            scan_entries=scan_entries,
            scan_outcomes=outcomes,
        )

    def normalize(self, payload: dict, *, source_url: str) -> NormalizedListing:
        safe_url = safe_source_url(self.source_name, source_url) or ""
        external_id = validated_external_id(jobly_external_id(safe_url)) if safe_url else ""
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
        published_at = ensure_aware_utc(isoparse(published_raw)) if published_raw else None
        stored_payload = {"source_url": safe_url, **payload}

        return NormalizedListing(
            external_id=external_id,
            canonical_source_url=safe_url,
            title=title,
            employer=employer,
            description=description,
            location=jobly_location(stored_payload),
            published_at=published_at,
            content_hash=payload_content_hash(stored_payload),
            payload=stored_payload,
            application_url=safe_url or None,
        )
