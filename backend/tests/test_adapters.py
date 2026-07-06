from datetime import datetime, timezone

import pytest

from app.adapters.base import collect_page_results, ensure_aware_utc, is_newer_than_watermark
from app.adapters.careerjet import CareerjetAdapter, parse_careerjet_date
from app.adapters.duunitori import DuunitoriAdapter, DuunitoriFetchResult
from app.adapters.eures import EuresAdapter, epoch_ms_to_datetime, eures_description_text, eures_portal_url
from app.adapters.jobly import (
    JoblyAdapter,
    extract_jobly_static_description,
    jobly_external_id,
)
from app.adapters.kuntarekry import KuntarekryAdapter
from app.adapters.talentech_org_shard import (
    TalentechOrgShardAdapter,
    TalentechOrgShardConfig,
    parse_filters_organisation_ids,
)
from app.adapters.valtiolle import ValtiolleAdapter
from app.adapters.laura import LauraAdapter, laura_content_text, laura_employer_from_link, laura_rendered
from app.adapters.linkedin import LinkedinAdapter, parse_linkedin_guest_jobs
from app.adapters.talentech import (
    extract_talentech_description,
    normalize_talentech_summary,
    parse_talentech_publication,
)
from app.adapters.tmt import TmtAdapter, tmt_description, tmt_detail_url, tmt_employer_name, tmt_location, tmt_title
from app.adapters.tmt_oulu import TmtOuluAdapter
from app.config import get_settings
from app.adapters.varbi import (
    OuluVarbiAdapter,
    extract_varbi_description,
    parse_varbi_rss_items,
    varbi_job_id_from_link,
)
from app.location import detect_work_mode, eures_location, laura_location


def test_duunitori_normalize_maps_listing_fields() -> None:
    payload = {
        "slug": "test-slug",
        "heading": "Palveluneuvoja",
        "company_name": "Test Employer",
        "municipality_name": "Oulu",
        "date_posted": "2026-06-20T13:00:06.005942+03:00",
        "descr": "Asiakaspalvelun ja hallinnon tehtävä.",
    }

    listing = DuunitoriAdapter().normalize(payload)

    assert listing.external_id == "test-slug"
    assert listing.title == "Palveluneuvoja"
    assert listing.employer == "Test Employer"
    assert listing.location == "Oulu"
    assert listing.published_at is not None
    assert listing.content_hash
    assert listing.application_url == "https://duunitori.fi/tyopaikat/tyo/test-slug"
    assert listing.canonical_source_url == listing.application_url


def test_duunitori_normalize_falls_back_to_scope_and_remote_mode() -> None:
    payload = {
        "slug": "remote-slug",
        "heading": "Asiantuntija",
        "company_name": "Test Employer",
        "date_posted": "2026-06-20T13:00:06.005942+03:00",
        "descr": "Työ voidaan tehdä kokonaan etätyönä.",
    }

    listing = DuunitoriAdapter().normalize(payload)

    assert listing.location == "Suomi / Etä"


def test_laura_location_infers_smaller_municipality_from_employer_slug() -> None:
    payload = {
        "title": {"rendered": "Kirjastonhoitaja"},
        "content": {"rendered": "Haemme kirjastonhoitajaa."},
        "link": "https://laura.fi/avoimet-tyopaikat/rantasalmen-kunta/kirjastonhoitaja/3810265/",
        "job_listing_region": [571, 572, 573, 574, 575, 576, 577, 578, 581, 582],
    }

    assert laura_location(payload) == "Rantasalmi"


async def test_duunitori_collect_includes_discovery_search_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.adapters.duunitori.get_discovery_search_queries",
        lambda: ["kirjastonhoitaja"],
    )
    class FakeDuunitoriAdapter(DuunitoriAdapter):
        def __init__(self) -> None:
            self.url = "https://example.test"
            self.page_size = 10
            self.max_pages = 1

        async def fetch_since_watermark(self, **kwargs: object) -> DuunitoriFetchResult:
            return DuunitoriFetchResult(
                payloads=[],
                newest_published_at=None,
                pages_fetched=1,
                stopped_at_watermark=False,
            )

        async def fetch_search(self, *, query: str, page_size: int = 20) -> list[dict]:
            if query == "kirjastonhoitaja":
                return [
                    {
                        "slug": "kirjastonhoitaja-1",
                        "heading": "Kirjastonhoitaja",
                        "company_name": "Rantasalmen kunta",
                        "municipality_name": "Rantasalmi",
                        "date_posted": "2026-06-18T13:25:00+03:00",
                        "descr": "Kirjastotyötä.",
                    }
                ]
            return []

    result = await FakeDuunitoriAdapter().collect(watermark=datetime(2026, 6, 21, tzinfo=timezone.utc))

    assert [listing.title for listing in result.listings] == ["Kirjastonhoitaja"]
    assert result.pages_fetched == 2


def test_is_newer_than_watermark_without_watermark() -> None:
    published = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
    assert is_newer_than_watermark(published, None) is True


def test_is_newer_than_watermark_normalizes_naive_datetime() -> None:
    published = datetime(2026, 6, 20, 13, 0)
    watermark = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)

    assert ensure_aware_utc(published) == datetime(2026, 6, 20, 13, 0, tzinfo=timezone.utc)
    assert is_newer_than_watermark(published, watermark) is True


def test_collect_page_results_stops_at_watermark() -> None:
    watermark = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    results = [
        {"slug": "new-1", "date_posted": "2026-06-20T13:00:00+00:00"},
        {"slug": "new-2", "date_posted": "2026-06-20T12:30:00+00:00"},
        {"slug": "old-1", "date_posted": "2026-06-20T11:59:00+00:00"},
    ]

    collected, newest, stopped = collect_page_results(results, watermark=watermark)

    assert [row["slug"] for row in collected] == ["new-1", "new-2"]
    assert newest == datetime(2026, 6, 20, 13, 0, tzinfo=timezone.utc)
    assert stopped is True


def test_tmt_normalize_maps_listing_fields() -> None:
    payload = {
        "id": "abc-123",
        "title": {"fi": "Työnjohtaja"},
        "employer": {"ownerName": {"fi": "RM Two Oy"}},
        "publishDate": "2026-06-18T09:34:27.5Z",
        "applicationUrl": {"values": {"fi": "http://example.test/apply/1"}},
        "officialMunicipality": {"label": {"fi": "Tampere"}},
    }

    listing = TmtAdapter().normalize(payload)

    assert listing.external_id == "abc-123"
    assert listing.title == "Työnjohtaja"
    assert listing.employer == "RM Two Oy"
    assert listing.location == "Tampere"
    assert listing.attribution == "Lähde: Työmarkkinatorin asiakastietojärjestelmä"
    assert listing.application_url == tmt_detail_url("abc-123")
    assert listing.canonical_source_url == listing.application_url
    assert tmt_title({"en": "Developer"}) == "Developer"


def test_tmt_normalize_uses_detail_description() -> None:
    payload = {
        "id": "abc-123",
        "title": {"fi": "Suunnittelija"},
        "publishDate": "2026-06-18T09:34:27.5Z",
        "detail": {
            "position": {
                "jobDescription": {
                    "fi": "Ensimmäinen rivi.\r\n\r\nToinen rivi.",
                }
            }
        },
    }

    listing = TmtAdapter().normalize(payload)

    assert listing.description == "Ensimmäinen rivi. Toinen rivi."
    assert tmt_description(payload) == "Ensimmäinen rivi. Toinen rivi."


def test_tmt_detail_fetch_is_limited_for_large_backfills() -> None:
    adapter = TmtAdapter()

    assert adapter.should_fetch_details(watermark=None, max_pages=130) is False
    assert adapter.should_fetch_details(watermark=None, max_pages=10) is True


def test_laura_normalize_maps_listing_fields() -> None:
    payload = {
        "id": 42,
        "link": "https://laura.fi/job/test/",
        "title": {"rendered": "Ratsastustuntien pitäjä"},
        "content": {"rendered": "<p>Tehtävän kuvaus.</p>"},
        "date_gmt": "2026-06-20T10:00:00",
        "job_listing_region": [584],
        "meta": {"_remote_position": 1},
    }

    listing = LauraAdapter().normalize(payload)

    assert listing.external_id == "42"
    assert listing.title == "Ratsastustuntien pitäjä"
    assert "Tehtävän kuvaus" in (listing.description or "")
    assert listing.location == "Pohjois-Pohjanmaa / Etä"
    assert listing.application_url == "https://laura.fi/job/test/"
    assert laura_rendered({"rendered": "x"}) == "x"


def test_laura_content_text_strips_html_and_decodes_entities() -> None:
    assert (
        laura_content_text(
            {
                "rendered": "<p>We don&#x27;t chase quarterly results.</p><p>Toinen kappale.</p>",
            }
        )
        == "We don't chase quarterly results.\nToinen kappale."
    )


def test_laura_employer_from_link_uses_company_slug() -> None:
    assert (
        laura_employer_from_link(
            "https://laura.fi/avoimet-tyopaikat/maku-muna-oy/haemme-elintarviketyontekijaa/3810582/"
        )
        == "Maku Muna Oy"
    )


def test_eures_normalize_maps_listing_fields() -> None:
    payload = {
        "id": "abc",
        "title": "Chef",
        "description": "Kitchen work.",
        "creationDate": 1781864644342,
        "employer": {"name": "Restaurant Oy"},
        "locationMap": {"FI": ["FI1D9"]},
    }

    listing = EuresAdapter().normalize(payload)

    assert listing.external_id == "abc"
    assert listing.title == "Chef"
    assert listing.employer == "Restaurant Oy"
    assert listing.location == "Pohjois-Pohjanmaa"
    assert listing.published_at == epoch_ms_to_datetime(payload["creationDate"])
    assert listing.application_url == eures_portal_url("abc")


def test_eures_portal_url_uses_current_detail_route() -> None:
    external_id = "NGJiOWEzOGItMTkxMi00NTQ3LTkwNTgtOWE1NzBlMWVlMzQ4 IDgx"

    assert eures_portal_url(external_id) == (
        "https://europa.eu/eures/portal/jv-se/jv-details/"
        "NGJiOWEzOGItMTkxMi00NTQ3LTkwNTgtOWE1NzBlMWVlMzQ4%20IDgx"
        "?jvDisplayLanguage=fi&lang=fi"
    )


def test_eures_description_text_strips_html_blocks() -> None:
    assert (
        eures_description_text("<p>Tehtävä<br>Oulussa</p><a href=\"https://example.test\">Linkki</a>")
        == "Tehtävä\nOulussa\nLinkki"
    )


def test_jobly_normalize_maps_listing_fields() -> None:
    payload = {
        "title": "Myyjä",
        "datePosted": "2026-06-07",
        "description": "<p>Myyntiä hybridityössä.</p>",
        "hiringOrganization": {"name": "Kauppa Oy"},
        "jobLocation": [{"address": {"addressLocality": "Helsinki"}}],
    }
    url = "https://www.jobly.fi/tyopaikka/myya-1234567"

    listing = JoblyAdapter().normalize(payload, source_url=url)

    assert listing.external_id == "1234567"
    assert listing.title == "Myyjä"
    assert listing.employer == "Kauppa Oy"
    assert listing.location == "Helsinki / Hybridi"
    assert jobly_external_id(url) == "1234567"


def test_jobly_location_ignores_bare_country_codes() -> None:
    payload = {
        "title": "Head of Logistics",
        "description": "The position is based in Espoo, with a hybrid work model.",
        "jobLocation": [
            {"address": {"addressCountry": "FI", "addressLocality": "Espoo"}},
            {"address": {"addressCountry": "IN"}},
        ],
    }

    listing = JoblyAdapter().normalize(payload, source_url="https://www.jobly.fi/tyopaikka/head-123")

    assert listing.location == "Espoo / Hybridi"


def test_location_helpers_fallback_to_known_scope_without_blank_location() -> None:
    assert laura_location({"title": {"rendered": "Kokki Joensuu"}, "job_listing_region": []}) == "Joensuu"
    assert eures_location({"locationMap": {"FI": [None]}}) == "Suomi"
    assert detect_work_mode("mahdollisuus etätyöhön") == "Etä"
    assert detect_work_mode("Etätyö: Ei mahdollisuutta työskennellä etänä") is None


def test_tmt_oulu_adapter_adds_municipality_filter() -> None:
    adapter = TmtOuluAdapter()
    assert adapter.extra_filters() == {"municipalities": ["564"]}
    assert adapter.source_name == "tmt_oulu"


def test_talentech_normalize_maps_listing_fields() -> None:
    payload = {
        "id": 296375,
        "title": "Kappalainen Tuiran seurakuntaan",
        "url": "/fi/tyopaikat/kappalainen-tuiran-seurakuntaan-5061/",
        "profit_center": "Tuiran seurakunta",
        "publication_date": "18.6.2026",
        "publication_time": "13:18",
    }

    listing = normalize_talentech_summary(
        payload,
        source_name="kirkkorekry",
        base_url="https://kirkkorekry.fi",
        description="Tehtävän kuvaus.",
    )

    assert listing.external_id == "296375"
    assert listing.title == "Kappalainen Tuiran seurakuntaan"
    assert listing.employer == "Tuiran seurakunta"
    assert listing.description == "Tehtävän kuvaus."
    assert listing.location == "Oulu"
    assert parse_talentech_publication("20.6.2026") is not None


def test_talentech_description_strips_script_tags() -> None:
    html = (
        "<article><script>{\"@context\":\"http://schema.org\"}</script>"
        "<p>Lehtori Pöllönkankaan koulussa.</p></article>"
    )
    assert extract_talentech_description(html) == "Lehtori Pöllönkankaan koulussa."


def test_talentech_description_decodes_entities_and_keeps_blocks() -> None:
    html = (
        "<article><h1>Hallintosihteeri</h1>"
        "<p>Koulunk&#xE4;ynninohjaaja ty&#xF6;skentelee koulussa.</p>"
        "<p>Teht&#xE4;v&#xE4;&#xE4;n kuuluu yleishallintoa.</p></article>"
    )

    description = extract_talentech_description(html)

    assert description == (
        "Hallintosihteeri\n"
        "Koulunkäynninohjaaja työskentelee koulussa.\n"
        "Tehtävään kuuluu yleishallintoa."
    )


def test_kuntarekry_adapter_metadata() -> None:
    adapter = KuntarekryAdapter()
    assert adapter.source_name == "kuntarekry"
    assert "oulu" in adapter.url


def test_kuntarekry_adapter_org_shard_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUNTAREKRY_COLLECTION_MODE", "org_shard")
    get_settings.cache_clear()

    adapter = KuntarekryAdapter()
    assert adapter.source_method == "processwire_format_json_org_shard"
    assert adapter.url.endswith("/fi/api/filters-data/")


def test_valtiolle_adapter_metadata() -> None:
    adapter = ValtiolleAdapter()
    assert adapter.source_name == "valtiolle"
    assert adapter.poll_interval_min == 360
    assert adapter.url == "https://valtiolle.fi/fi/api/filters-data/"


def test_parse_filters_organisation_ids_dedupes_and_preserves_order() -> None:
    payload = {
        "organisations": [
            {"id": 12},
            {"id": "7"},
            {"id": 12},
            {"name": "skip"},
            {"id": "3"},
        ]
    }

    assert parse_filters_organisation_ids(payload) == ["12", "7", "3"]


@pytest.mark.asyncio
async def test_talentech_org_shard_collect_dedupes_and_fetches_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = TalentechOrgShardAdapter(
        TalentechOrgShardConfig(
            source_name="valtiolle",
            source_method="processwire_format_json_org_shard",
            site_root="https://valtiolle.test",
            shard_fetch_delay_s=0,
        )
    )

    class FakeResponse:
        def __init__(self, *, status_code: int, payload: object, text: str = "") -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = text

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self) -> object:
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, headers: dict | None = None):
            if url.endswith("/fi/api/filters-data/"):
                return FakeResponse(
                    status_code=200,
                    payload={"organisations": [{"id": "1"}, {"id": "2"}]},
                )
            if "organisation=1" in url:
                return FakeResponse(
                    status_code=200,
                    payload=[{"id": 10, "url": "/fi/tyopaikka/a-10/", "title": "A", "publication_date": "21.6.2026"}],
                )
            if "organisation=2" in url:
                return FakeResponse(
                    status_code=200,
                    payload=[{"id": 10, "url": "/fi/tyopaikka/a-10/", "title": "A dup", "publication_date": "21.6.2026"}],
                )
            if url.endswith("/fi/tyopaikka/a-10/"):
                return FakeResponse(
                    status_code=200,
                    payload={},
                    text="<article><p>State role body.</p></article>",
                )
            raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    result = await adapter.collect(watermark=None, max_pages=2)

    assert len(result.listings) == 1
    assert result.listings[0].external_id == "10"
    assert result.listings[0].description == "State role body."


def test_jobly_static_description_prefers_meta_and_opengraph() -> None:
    html = """
    <html><head>
      <meta name="description" content="Short" />
      <meta property="og:description" content="This is a long enough OpenGraph description for the Jobly listing body." />
    </head><body></body></html>
    """

    assert extract_jobly_static_description(html) == (
        "This is a long enough OpenGraph description for the Jobly listing body."
    )


def test_jobly_static_description_falls_back_to_visible_body() -> None:
    html = """
    <div class="job-description">
      <p>We are hiring a library professional for municipal services in northern Finland.</p>
    </div>
    """

    assert extract_jobly_static_description(html) == (
        "We are hiring a library professional for municipal services in northern Finland."
    )


def test_careerjet_normalize_maps_publisher_job_fields() -> None:
    payload = {
        "title": "Information Specialist",
        "company": "Example Oy",
        "date": "Wed, 15 Nov 2025 19:13:43 GMT",
        "description": "Library and information work.",
        "locations": "Oulu",
        "url": "https://jobviewtrack.com/v2/test",
    }

    listing = CareerjetAdapter().normalize(payload)

    assert listing.external_id == payload["url"]
    assert listing.title == "Information Specialist"
    assert listing.employer == "Example Oy"
    assert listing.location == "Oulu"
    assert listing.description == "Library and information work."
    assert listing.published_at == parse_careerjet_date(payload["date"])


def test_linkedin_guest_parser_extracts_search_cards() -> None:
    markup = """
    <li>
      <a href="/jobs/view/information-specialist-1234567890?trk=public_jobs"></a>
      <h3 class="base-search-card__title"> Information Specialist </h3>
      <h4 class="base-search-card__subtitle"> Example Org </h4>
      <span class="job-search-card__location"> Oulu, North Ostrobothnia </span>
      <time datetime="2026-07-01"></time>
    </li>
    """

    rows = parse_linkedin_guest_jobs(markup)
    listing = LinkedinAdapter().normalize(rows[0])

    assert rows[0]["id"] == "1234567890"
    assert rows[0]["url"] == "https://www.linkedin.com/jobs/view/information-specialist-1234567890"
    assert listing.title == "Information Specialist"
    assert listing.employer == "Example Org"
    assert listing.location == "Oulu, North Ostrobothnia"


@pytest.mark.asyncio
async def test_linkedin_collect_runs_configured_queries_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINKEDIN_ENABLED", "true")
    monkeypatch.setenv("LINKEDIN_REQUEST_DELAY_SECONDS", "0")
    monkeypatch.setenv("LINKEDIN_SEARCH_QUERIES", "kirjasto,informaatikko")
    get_settings.cache_clear()
    seen_keywords: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, job_id: str) -> None:
            self.text = f"""
            <li>
              <a href="/jobs/view/information-specialist-{job_id}?trk=public_jobs"></a>
              <h3 class="base-search-card__title"> Information Specialist </h3>
              <h4 class="base-search-card__subtitle"> Example Org </h4>
              <span class="job-search-card__location"> Oulu </span>
              <time datetime="2026-07-01"></time>
            </li>
            """

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, params: dict[str, object]):
            seen_keywords.append(str(params["keywords"]))
            return FakeResponse("1234567890")

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    adapter = LinkedinAdapter()
    assert adapter.search_queries == ["kirjasto", "informaatikko"]

    result = await adapter.collect(watermark=None, max_pages=2)

    assert seen_keywords == ["kirjasto", "informaatikko"]
    assert len(result.listings) == 1
    assert result.pages_fetched == 2


def test_varbi_helpers_parse_rss_and_description() -> None:
    rss = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>Test job</title>
        <link>https://oulunyliopisto.varbi.com/en/what:job/jobID:12345/</link>
        <pubDate>Sat, 20 Jun 2026 14:35:32 +0000</pubDate>
      </item>
    </channel></rss>"""

    items = parse_varbi_rss_items(rss)
    assert len(items) == 1
    assert varbi_job_id_from_link(items[0]["link"]) == "12345"

    html = '<div class="job-desc mb"><p>University role.</p></div>'
    assert extract_varbi_description(html) == "University role."

    listing = OuluVarbiAdapter().normalize(
        {
            "job_id": "12345",
            "title": "Test job",
            "link": items[0]["link"],
            "published_at": items[0]["published_at"],
            "description": "University role.",
            "detail_url": "https://oulunyliopisto.varbi.com/fi/what:job/jobID:12345/",
        }
    )
    assert listing.external_id == "12345"
    assert listing.employer == "Oulun yliopisto"
