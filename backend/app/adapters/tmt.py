from dataclasses import dataclass
from datetime import datetime

from dateutil.parser import isoparse

from app.adapters.base import (
    extract_deadline,
    CollectionFetchResult,
    ensure_aware_utc,
    NormalizedListing,
    USER_AGENT,
    payload_content_hash,
    source_url_rejection_reason,
    validated_external_id,
)
from app.config import get_settings
from app.feedback_learning import get_discovery_search_queries


def tmt_title(title_obj: dict) -> str:
    if not title_obj:
        return ""
    return (
        title_obj.get("fi")
        or title_obj.get("en")
        or title_obj.get("sv")
        or next(iter(title_obj.values()), "")
    )


def tmt_detail_url(external_id: str) -> str:
    safe_id = validated_external_id(external_id)
    if safe_id is None:
        return ""
    return (
        "https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat/details/"
        f"?id={safe_id}"
    )


def tmt_localized(values_obj: dict | None) -> str | None:
    if not values_obj:
        return None
    values = values_obj.get("values") if isinstance(values_obj, dict) else None
    if not isinstance(values, dict):
        return None
    return values.get("fi") or values.get("en") or next(iter(values.values()), None)


def tmt_simple_localized(values_obj: dict | None) -> str | None:
    if not isinstance(values_obj, dict):
        return None
    value = values_obj.get("fi") or values_obj.get("en") or values_obj.get("sv")
    if value is None and values_obj:
        value = next(iter(values_obj.values()), None)
    return str(value) if value else None


def tmt_description(payload: dict) -> str | None:
    detail = payload.get("detail")
    if isinstance(detail, dict):
        position = detail.get("position")
        if isinstance(position, dict):
            description = tmt_simple_localized(position.get("jobDescription"))
            if description:
                return " ".join(description.split())
    return None


def tmt_employer_name(employer: dict | None) -> str | None:
    if not employer:
        return None
    owner_name = employer.get("ownerName")
    if isinstance(owner_name, dict):
        name = owner_name.get("fi") or owner_name.get("en") or next(iter(owner_name.values()), None)
        if name:
            return str(name)
    name = employer.get("name")
    return str(name) if name else None


def tmt_location(payload: dict) -> str | None:
    municipality = payload.get("officialMunicipality")
    if isinstance(municipality, dict):
        label = municipality.get("label")
        if isinstance(label, dict):
            return label.get("fi") or label.get("en") or next(iter(label.values()), None)
    location = payload.get("location")
    if isinstance(location, dict):
        municipalities = location.get("municipalities")
        if isinstance(municipalities, list) and municipalities:
            first = municipalities[0]
            if isinstance(first, dict):
                label = first.get("label")
                if isinstance(label, dict):
                    return label.get("fi") or label.get("en") or next(iter(label.values()), None)
    return None


class TmtAdapter:
    source_name = "tmt"
    source_method = "json_api"
    poll_interval_min = 5
    attribution = "Lähde: Työmarkkinatorin asiakastietojärjestelmä"

    def __init__(self) -> None:
        settings = get_settings()
        self.url = settings.tmt_url
        self.page_size = settings.tmt_page_size
        self.max_pages = settings.tmt_max_pages

    def extra_filters(self) -> dict:
        return {}

    def should_fetch_details(self, *, watermark: datetime | None, max_pages: int) -> bool:
        return watermark is not None or max_pages <= 10

    async def fetch_detail(self, client: "httpx.AsyncClient", external_id: str) -> dict | None:
        safe_id = validated_external_id(external_id)
        if safe_id is None:
            return None
        detail_url = (
            "https://tyomarkkinatori.fi/api/jobposting-new/v1/public/jobpostings/"
            f"{safe_id}"
        )
        if source_url_rejection_reason(self.source_name, detail_url) is not None:
            return None
        response = await client.get(detail_url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def collect(
        self,
        *,
        watermark: datetime | None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_urls: int | None = None,
    ) -> CollectionFetchResult:
        import httpx

        effective_page_size = page_size or self.page_size
        effective_max_pages = max_pages or self.max_pages
        listings: list[NormalizedListing] = []
        payloads_by_id: dict[str, dict] = {}
        newest_watermark: datetime | None = None
        pages_fetched = 0

        filters: dict = dict(self.extra_filters())
        if watermark is not None:
            filters["publishedAfter"] = watermark.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
            for page_number in range(effective_max_pages):
                body = {
                    "query": "",
                    "filters": filters,
                    "paging": {"pageNumber": page_number, "pageSize": effective_page_size},
                }
                if watermark is None:
                    body["sorting"] = "LATEST"

                response = await client.post(self.url, json=body)
                response.raise_for_status()
                data = response.json()
                content = list(data.get("content") or [])
                pages_fetched += 1

                if not content:
                    break

                for payload in content:
                    if self.should_fetch_details(
                        watermark=watermark,
                        max_pages=effective_max_pages,
                    ):
                        detail = await self.fetch_detail(client, str(payload["id"]))
                        if detail is not None:
                            payload = {**payload, "detail": detail}
                    listing = self.normalize(payload)
                    payloads_by_id[str(payload["id"])] = payload
                    if listing.published_at is not None and (
                        newest_watermark is None or listing.published_at > newest_watermark
                    ):
                        newest_watermark = listing.published_at

                if data.get("lastPage", False) or len(content) < effective_page_size:
                    break

            for query in get_discovery_search_queries():
                body = {
                    "query": query,
                    "filters": dict(self.extra_filters()),
                    "paging": {"pageNumber": 0, "pageSize": min(effective_page_size, 20)},
                    "sorting": "LATEST",
                }
                response = await client.post(self.url, json=body)
                response.raise_for_status()
                data = response.json()
                pages_fetched += 1
                for payload in list(data.get("content") or []):
                    detail = await self.fetch_detail(client, str(payload["id"]))
                    if detail is not None:
                        payload = {**payload, "detail": detail}
                    payloads_by_id[str(payload["id"])] = payload
                    listing = self.normalize(payload)
                    if listing.published_at is not None and (
                        newest_watermark is None or listing.published_at > newest_watermark
                    ):
                        newest_watermark = listing.published_at

        listings = [self.normalize(payload) for payload in payloads_by_id.values()]

        return CollectionFetchResult(
            listings=listings,
            newest_watermark=newest_watermark,
            pages_fetched=pages_fetched,
        )

    def normalize(self, payload: dict) -> NormalizedListing:
        external_id = str(payload["id"])
        published_raw = payload.get("publishDate") or payload.get("created")
        published_at = ensure_aware_utc(isoparse(published_raw)) if published_raw else None
        detail_url = tmt_detail_url(external_id)

        return NormalizedListing(
            external_id=external_id,
            canonical_source_url=detail_url,
            title=str(tmt_title(payload.get("title", {}))).strip(),
            employer=tmt_employer_name(payload.get("employer")),
            description=tmt_description(payload),
            location=tmt_location(payload),
            published_at=published_at,
            content_hash=payload_content_hash(payload),
            payload=payload,
            attribution=self.attribution,
            application_url=detail_url,
            expires_at=extract_deadline(payload),
        )
