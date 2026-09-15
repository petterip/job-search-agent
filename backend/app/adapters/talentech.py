import html as html_lib
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from dateutil.parser import parse as parse_date

from app.adapters.base import (
    USER_AGENT,
    NormalizedListing,
    payload_content_hash,
    url_rejection_reason,
)
from app.location import talentech_location


def parse_talentech_publication(
    publication_date: str | None,
    publication_time: str | None = None,
) -> datetime | None:
    if not publication_date:
        return None
    raw = publication_date.strip()
    if publication_time:
        raw = f"{raw} {publication_time.strip()}"
    try:
        return parse_date(raw, dayfirst=True)
    except (ValueError, TypeError, OverflowError):
        return None


def extract_talentech_description(html: str) -> str | None:
    article = re.search(r"<article[^>]*>(.*?)</article>", html, re.S | re.I)
    if article:
        body = re.sub(r"<script[^>]*>.*?</script>", " ", article.group(1), flags=re.S | re.I)
        body = re.sub(r"</?(p|div|section|article|h[1-6]|ul|ol|li|br)\b[^>]*>", "\n", body, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", body)
        text = html_lib.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        return text or None
    return None


def talentech_canonical_url(base_url: str, url_path: str) -> str | None:
    """Resolve a Talentech detail path against the source's documented host.

    Absolute and scheme-relative values are accepted only while they stay on the
    configured source host; everything else is rejected.
    """
    text = str(url_path or "").strip()
    if not text:
        return None
    base_host = (urlsplit(base_url).hostname or "").lower()
    if not base_host:
        return None
    candidate = urljoin(f"{base_url.rstrip('/')}/", text)
    if url_rejection_reason(candidate, allowed_host_suffixes=(base_host,)) is not None:
        return None
    return candidate


def normalize_talentech_summary(
    payload: dict,
    *,
    source_name: str,
    base_url: str,
    description: str | None = None,
) -> NormalizedListing:
    external_id = str(payload["id"])
    canonical_source_url = talentech_canonical_url(base_url, str(payload["url"])) or ""
    published_at = parse_talentech_publication(
        payload.get("publication_date"),
        payload.get("publication_time"),
    )
    stored_payload = {"source": source_name, **payload}
    if description is not None:
        stored_payload["description_text"] = description

    return NormalizedListing(
        external_id=external_id,
        canonical_source_url=canonical_source_url,
        title=str(payload.get("title") or "").strip(),
        employer=payload.get("profit_center"),
        description=description,
        location=talentech_location(payload, description),
        published_at=published_at,
        content_hash=payload_content_hash(stored_payload),
        payload=stored_payload,
        application_url=canonical_source_url,
    )


TALENTECH_USER_AGENT = USER_AGENT
