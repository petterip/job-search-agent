from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
import json
import re
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlsplit

from dateutil.parser import isoparse

USER_AGENT = "job-search-agent-collector/0.1 (+local research project)"

MAX_URL_LENGTH = 2048
MAX_EXTERNAL_ID_LENGTH = 128

URL_REASON_MISSING = "missing"
URL_REASON_MALFORMED = "malformed"
URL_REASON_SCHEME = "non_http_scheme"
URL_REASON_CREDENTIALS = "embedded_credentials"
URL_REASON_PRIVATE_HOST = "private_host"
URL_REASON_UNEXPECTED_HOST = "unexpected_host"
URL_REASON_UNEXPECTED_PATH = "unexpected_path"
URL_REASON_TOO_LONG = "too_long"
URL_REASON_CONTROL_CHARACTERS = "control_characters"
URL_REASON_INVALID_EXTERNAL_ID = "invalid_external_id"
URL_REASON_UNKNOWN_SOURCE = "unknown_source"
URL_REASON_REJECTED = "rejected"

# Navigation allowlist per collector source, as (host suffixes, path prefixes).
# Hosts mirror docs/sources.yaml and app/collection/registry.py. An empty path
# tuple means the source has no reliably documented detail path, so only the host
# is constrained. These entries gate fetch/navigation, never display links.
SOURCE_URL_POLICY: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "duunitori": (("duunitori.fi",), ("/tyopaikat/tyo/", "/api/v1/jobentries")),
    "tmt": (
        ("tyomarkkinatori.fi",),
        (
            "/henkiloasiakkaat/avoimet-tyopaikat/details/",
            "/api/jobposting-new/v1/public/jobpostings/",
            "/api/jobpostingfulltext/search/v2/search",
        ),
    ),
    "tmt_oulu": (
        ("tyomarkkinatori.fi",),
        (
            "/henkiloasiakkaat/avoimet-tyopaikat/details/",
            "/api/jobposting-new/v1/public/jobpostings/",
            "/api/jobpostingfulltext/search/v2/search",
        ),
    ),
    "laura": (("laura.fi",), ()),
    "jobly": (("jobly.fi",), ("/tyopaikka/", "/sitemap.xml")),
    "eures_fi": (("europa.eu",), ("/eures/portal/jv-se/jv-details/", "/eures/api/")),
    "kuntarekry": (
        ("kuntarekry.fi",),
        ("/fi/tyopaikat/", "/fi/tyopaikka/", "/fi/api/filters-data/"),
    ),
    "kirkkorekry": (
        ("kirkkorekry.fi",),
        ("/fi/tyopaikat/", "/fi/tyopaikka/", "/fi/api/filters-data/"),
    ),
    "oulu_varbi": (("oulunyliopisto.varbi.com",), ("/fi/what:job/", "/fi/what:rssfeed/")),
    "careerjet": (("careerjet.net",), ()),
    "linkedin": (("linkedin.com",), ()),
    "valtiolle": (
        ("valtiolle.fi",),
        ("/fi/tyopaikat/", "/fi/tyopaikka/", "/fi/api/filters-data/"),
    ),
}

_FORBIDDEN_EXTERNAL_ID_CHARS = frozenset("/\\?#%&:")
_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.?$")


class UrlNavigationRejected(RuntimeError):
    """A fetch/navigation target was rejected.

    The exception carries only a safe reason code. The raw rejected value is
    never included so it cannot leak through logs or error surfaces.
    """

    def __init__(self, *, source_name: str, reason: str) -> None:
        super().__init__(f"navigation rejected source={source_name} reason={reason}")
        self.source_name = source_name
        self.reason = reason


def url_debug_reference(value: object) -> str:
    """Return a short, non-reversible reference for a rejected URL or ID."""
    digest = sha256(str(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def log_url_rejection(
    logger: Any,
    *,
    source_name: str,
    reason: str,
    value: object | None = None,
) -> None:
    """Log a rejection with only the reason code and a hashed reference."""
    if value is None:
        logger.info("event=url_rejected source=%s reason=%s", source_name, reason)
        return
    logger.info(
        "event=url_rejected source=%s reason=%s ref=%s",
        source_name,
        reason,
        url_debug_reference(value),
    )


def validated_external_id(
    value: object | None,
    *,
    max_length: int = MAX_EXTERNAL_ID_LENGTH,
) -> str | None:
    """Return a safe external ID for URL interpolation, or None.

    Rejects empty values, path traversal, slashes, control characters, URL
    metacharacters and overly long values.
    """
    text = str(value).strip() if value is not None else ""
    if not text or len(text) > max_length:
        return None
    if any(char in _FORBIDDEN_EXTERNAL_ID_CHARS for char in text):
        return None
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    if text in {".", ".."} or text.startswith("."):
        return None
    return text


def _host_is_private(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    if normalized.endswith((".local", ".internal", ".localdomain")):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return not address.is_global


def _host_allowed(host: str, allowed_suffixes: tuple[str, ...]) -> bool:
    for suffix in allowed_suffixes:
        normalized = suffix.strip().lower().lstrip(".")
        if normalized and (host == normalized or host.endswith(f".{normalized}")):
            return True
    return False


def _path_allowed(path: str, allowed_prefixes: tuple[str, ...]) -> bool:
    for prefix in allowed_prefixes:
        normalized = prefix if prefix.startswith("/") else f"/{prefix}"
        if path == normalized.rstrip("/") or path.startswith(normalized):
            return True
    return False


def url_rejection_reason(
    url: object | None,
    *,
    schemes: tuple[str, ...] = ("http", "https"),
    allowed_host_suffixes: tuple[str, ...] | None = None,
    allowed_path_prefixes: tuple[str, ...] | None = None,
    max_length: int = MAX_URL_LENGTH,
) -> str | None:
    """Return a safe reason code when a URL must not be fetched or rendered.

    ``allowed_host_suffixes=None`` allows any public host; an empty tuple allows
    none. Private, loopback, link-local and ``*.local`` hosts are always rejected.
    """
    if url is None:
        return URL_REASON_MISSING
    if not isinstance(url, str):
        return URL_REASON_MALFORMED
    text = url.strip()
    if not text:
        return URL_REASON_MISSING
    if len(text) > max_length:
        return URL_REASON_TOO_LONG
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return URL_REASON_CONTROL_CHARACTERS
    try:
        parts = urlsplit(text)
        _port = parts.port
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return URL_REASON_MALFORMED
    if parts.scheme.lower() not in schemes:
        return URL_REASON_SCHEME
    if parts.username or parts.password:
        return URL_REASON_CREDENTIALS
    if not host:
        return URL_REASON_MALFORMED
    if _host_is_private(host):
        return URL_REASON_PRIVATE_HOST
    if not _HOSTNAME_PATTERN.fullmatch(host):
        return URL_REASON_MALFORMED
    if allowed_host_suffixes is not None and not _host_allowed(host, allowed_host_suffixes):
        return URL_REASON_UNEXPECTED_HOST
    if allowed_path_prefixes and not _path_allowed(parts.path or "/", allowed_path_prefixes):
        return URL_REASON_UNEXPECTED_PATH
    return None


def source_url_rejection_reason(source_name: str, url: object | None) -> str | None:
    """Return a safe reason code when ``url`` is not a legal navigation target."""
    policy = SOURCE_URL_POLICY.get(source_name)
    if policy is None:
        return URL_REASON_UNKNOWN_SOURCE
    hosts, paths = policy
    return url_rejection_reason(
        url,
        allowed_host_suffixes=hosts,
        allowed_path_prefixes=paths or None,
    )


def is_allowed_source_url(source_name: str, url: object | None) -> bool:
    return source_url_rejection_reason(source_name, url) is None


def safe_source_url(source_name: str, url: object | None) -> str | None:
    """Return the stripped URL when it is a legal source navigation target."""
    if source_url_rejection_reason(source_name, url) is not None:
        return None
    return str(url).strip()


def safe_external_url(url: object | None) -> str | None:
    """Return a public HTTP(S) URL suitable for a rendered external action."""
    if url_rejection_reason(url) is not None:
        return None
    return str(url).strip()


def _redirect_target(url: object | None, location: object | None) -> str | None:
    if not url or not location:
        return None
    return urljoin(str(url), str(location))


def _reject_redirect_escape(source_name: str, response: object) -> None:
    headers = getattr(response, "headers", None)
    location = headers.get("location") if headers is not None else None
    if not location:
        return
    request = getattr(response, "request", None)
    base_url = getattr(request, "url", None)
    target = _redirect_target(base_url, location)
    if target is None:
        return
    reason = source_url_rejection_reason(source_name, target)
    if reason is not None:
        raise UrlNavigationRejected(source_name=source_name, reason=reason)


def source_redirect_guard(source_name: str) -> Callable[[object], None]:
    """Return a sync httpx response hook that blocks escaping redirects."""

    def hook(response: object) -> None:
        _reject_redirect_escape(source_name, response)

    return hook


def async_source_redirect_guard(
    source_name: str,
) -> Callable[[object], Awaitable[None]]:
    """Return an async httpx response hook that blocks escaping redirects."""

    async def hook(response: object) -> None:
        _reject_redirect_escape(source_name, response)

    return hook


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
    expires_at: datetime | None = None


@dataclass(frozen=True)
class CollectionFetchResult:
    """Result of one source fetch, with explicit completeness evidence.

    ``outcome`` distinguishes a legitimate empty delta from a partial fetch.
    Only a complete fetch may establish absence (remove listings) or advance the
    incremental cursor.
    """

    listings: list[NormalizedListing]
    newest_watermark: datetime | None = None
    pages_fetched: int = 0
    stopped_at_watermark: bool = False
    outcome: Literal["delta", "full_snapshot", "partial"] = "delta"
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Resumable-scan evidence: the full frozen sitemap frontier and the outcome
    # of each claimed member ("classified" | "missing" | "invalid").
    scan_entries: list[tuple[str, str, datetime | None]] = field(default_factory=list)
    scan_outcomes: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.outcome != "partial"


# Common source deadline field names across the adapters' payloads.
DEADLINE_KEYS = (
    "validThrough",
    "applicationEndDate",
    "application_end_date",
    "endDate",
    "end_date",
    "deadline",
    "expires",
    "expiresAt",
    "expires_at",
    "closingDate",
    "closing_date",
)


def extract_deadline(payload: object) -> datetime | None:
    """Best-effort source deadline from a listing payload, normalised to UTC.

    The value is evidence only: a stored deadline drives profile freshness and
    never sets the catalogue status by itself.
    """
    if not isinstance(payload, dict):
        return None
    containers: list[dict] = [payload]
    detail = payload.get("detail")
    if isinstance(detail, dict):
        containers.append(detail)
    for container in containers:
        for key in DEADLINE_KEYS:
            value = container.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                parsed = isoparse(value.strip())
            except (ValueError, OverflowError):
                continue
            return ensure_aware_utc(parsed)
    return None


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
        published_at = ensure_aware_utc(isoparse(published_raw)) if published_raw else None
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
