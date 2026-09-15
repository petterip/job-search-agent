"""Unit tests for the P1-15 URL/ID trust boundary.

These tests cover the reusable helpers in ``app.adapters.base`` and the
navigation/display boundaries in the source adapters. No network access is
required: httpx is driven through ``MockTransport``.
"""

from __future__ import annotations

import functools
import types

import httpx
import pytest

from app.collection.registry import SOURCE_NAMES
from app.adapters.base import (
    SOURCE_URL_POLICY,
    UrlNavigationRejected,
    async_source_redirect_guard,
    safe_external_url,
    safe_source_url,
    source_redirect_guard,
    source_url_rejection_reason,
    url_rejection_reason,
    validated_external_id,
)
from app.adapters.careerjet import CareerjetAdapter
from app.adapters.duunitori import duunitori_job_url
from app.adapters.eures import EuresAdapter
from app.adapters.jobly import JoblyAdapter
from app.adapters.laura import LauraAdapter
from app.adapters.linkedin import parse_linkedin_guest_jobs
from app.adapters.talentech import talentech_canonical_url
from app.adapters.talentech_org_shard import TalentechOrgShardAdapter, TalentechOrgShardConfig
from app.adapters.tmt import TmtAdapter, tmt_detail_url

DANGEROUS_URLS = (
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "ftp://example.com/file",
    "vbscript:msgbox(1)",
    "//example.com/no-scheme",
    "http://",
    "https://exa mple.com/path",
    "https://user:secret@example.com/apply",
)

PRIVATE_HOST_URLS = (
    "http://127.0.0.1:8008/apply",
    "http://localhost/apply",
    "http://localhost.localdomain/apply",
    "http://10.0.0.5/apply",
    "http://192.168.1.10/apply",
    "http://172.16.4.4/apply",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/apply",
    "http://service.local/apply",
    "http://service.internal/apply",
)


def test_source_url_policy_covers_every_registry_source() -> None:
    assert set(SOURCE_NAMES) <= set(SOURCE_URL_POLICY)


@pytest.mark.parametrize("url", DANGEROUS_URLS)
def test_url_rejection_reason_rejects_dangerous_values(url: str) -> None:
    assert url_rejection_reason(url) is not None


@pytest.mark.parametrize("url", PRIVATE_HOST_URLS)
def test_url_rejection_reason_rejects_private_hosts(url: str) -> None:
    assert url_rejection_reason(url) == "private_host"


def test_url_rejection_reason_rejects_control_characters_and_long_values() -> None:
    assert url_rejection_reason("https://example.com/a\x00b") == "control_characters"
    assert url_rejection_reason("https://example.com/" + "a" * 3000) == "too_long"


def test_url_rejection_reason_allows_public_https() -> None:
    assert url_rejection_reason("https://example.com/apply") is None
    assert safe_external_url("https://example.com/apply") == "https://example.com/apply"
    assert safe_external_url("javascript:alert(1)") is None


def test_source_url_rejection_reason_rejects_wrong_host() -> None:
    assert source_url_rejection_reason("jobly", "https://evil.example/tyopaikka/x-1") == "unexpected_host"
    assert (
        source_url_rejection_reason("tmt", "https://tyomarkkinatori.fi.evil.example/api/x")
        == "unexpected_host"
    )
    assert source_url_rejection_reason("kuntarekry", "https://kuntarekry.fi.evil.example/fi/tyopaikat/oulu")
    assert source_url_rejection_reason("jobly", "https://www.jobly.fi/jobs/view/1") == "unexpected_path"


@pytest.mark.parametrize(
    ("source_name", "url"),
    (
        ("tmt", "https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat/details/?id=abc"),
        ("tmt", "https://tyomarkkinatori.fi/api/jobposting-new/v1/public/jobpostings/abc"),
        ("tmt_oulu", "https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search"),
        ("jobly", "https://www.jobly.fi/tyopaikka/myya-1234567"),
        ("kuntarekry", "https://kuntarekry.fi/fi/tyopaikat/oulu?format=json"),
        ("kuntarekry", "https://kuntarekry.fi/fi/tyopaikka/a-10/"),
        ("kirkkorekry", "https://kirkkorekry.fi/fi/api/filters-data/"),
        ("valtiolle", "https://valtiolle.fi/fi/tyopaikka/a-10/"),
        ("duunitori", "https://duunitori.fi/tyopaikat/tyo/test-slug"),
        ("eures_fi", "https://europa.eu/eures/portal/jv-se/jv-details/abc?lang=fi"),
        ("oulu_varbi", "https://oulunyliopisto.varbi.com/fi/what:job/jobID:12345/"),
        ("laura", "https://laura.fi/avoimet-tyopaikat/example-oy/kokki/1/"),
        ("linkedin", "https://www.linkedin.com/jobs/view/example-1234567890"),
    ),
)
def test_source_url_rejection_reason_allows_documented_source_pages(
    source_name: str,
    url: str,
) -> None:
    assert source_url_rejection_reason(source_name, url) is None
    assert safe_source_url(source_name, f"  {url}  ") == url


def test_source_url_rejection_reason_rejects_unknown_source() -> None:
    assert source_url_rejection_reason("not-a-source", "https://example.com/") == "unknown_source"


@pytest.mark.parametrize(
    "value",
    (
        None,
        "",
        "   ",
        "..",
        "../etc/passwd",
        "a/../b",
        "a/b",
        "a\\b",
        "a?b",
        "a#b",
        "a%b",
        "a&b",
        "a b",
        "a\nb",
        ".hidden",
        "x" * 200,
    ),
)
def test_validated_external_id_rejects_unsafe_values(value: object) -> None:
    assert validated_external_id(value) is None


@pytest.mark.parametrize("value", ("test-slug", "12345", "abc-123", "NGJiOWEzOGItMTkxMi00NTQ3"))
def test_validated_external_id_keeps_safe_values(value: str) -> None:
    assert validated_external_id(value) == value


def test_duunitori_job_url_rejects_traversal_slug() -> None:
    assert duunitori_job_url("test-slug") == "https://duunitori.fi/tyopaikat/tyo/test-slug"
    assert duunitori_job_url("../../admin") == ""
    assert duunitori_job_url("slug/extra") == ""
    assert duunitori_job_url("javascript:alert(1)") == ""


def test_tmt_detail_url_rejects_traversal_id() -> None:
    assert tmt_detail_url("abc-123").endswith("?id=abc-123")
    assert tmt_detail_url("../../etc/passwd") == ""
    assert tmt_detail_url("id&admin=1") == ""


@pytest.mark.asyncio
async def test_tmt_fetch_detail_skips_invalid_external_id() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get(self, url: str) -> object:  # pragma: no cover - must not run
            self.calls.append(url)
            raise AssertionError("invalid external ID must not be fetched")

    client = RecordingClient()
    assert await TmtAdapter().fetch_detail(client, "../../etc/passwd") is None
    assert await TmtAdapter().fetch_detail(client, "id&admin=1") is None
    assert client.calls == []


def test_talentech_canonical_url_rejects_cross_host_values() -> None:
    base = "https://kuntarekry.fi"
    assert talentech_canonical_url(base, "/fi/tyopaikat/example-1/") == (
        "https://kuntarekry.fi/fi/tyopaikat/example-1/"
    )
    assert talentech_canonical_url(base, "https://www.kuntarekry.fi/fi/tyopaikat/example-1/") == (
        "https://www.kuntarekry.fi/fi/tyopaikat/example-1/"
    )
    assert talentech_canonical_url(base, "https://evil.example/collect") is None
    assert talentech_canonical_url(base, "//evil.example/collect") is None
    assert talentech_canonical_url(base, "javascript:alert(1)") is None
    assert talentech_canonical_url(base, "") is None


@pytest.mark.asyncio
async def test_talentech_org_shard_skips_off_host_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    class FakeResponse:
        def __init__(self, payload: object, *, status_code: int = 200) -> None:
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self) -> object:
            return self._payload

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, headers: dict | None = None) -> FakeResponse:
            requested.append(url)
            if url.endswith("/fi/api/filters-data/"):
                return FakeResponse({"organisations": [{"id": "1"}]})
            if "organisation=1" in url:
                return FakeResponse(
                    [
                        {
                            "id": 10,
                            "url": "https://evil.example/fi/tyopaikka/a-10/",
                            "title": "A",
                            "publication_date": "21.6.2026",
                        }
                    ]
                )
            raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    adapter = TalentechOrgShardAdapter(
        TalentechOrgShardConfig(
            source_name="valtiolle",
            source_method="processwire_format_json_org_shard",
            site_root="https://valtiolle.fi",
            shard_fetch_delay_s=0,
        )
    )

    result = await adapter.collect(watermark=None, max_pages=1)

    assert result.listings == []
    assert all("evil.example" not in url for url in requested)


def test_jobly_normalize_rejects_off_host_source_url() -> None:
    listing = JoblyAdapter().normalize({"title": "Myyjä"}, source_url="https://evil.example/tyopaikka/x-1")

    assert listing.canonical_source_url == ""
    assert listing.application_url is None
    assert listing.external_id == ""


@pytest.mark.asyncio
async def test_jobly_collect_never_fetches_off_policy_sitemap_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []
    sitemap = """<?xml version="1.0"?>
    <urlset>
      <url><loc>http://127.0.0.1/tyopaikka/private-1</loc></url>
      <url><loc>https://evil.example/tyopaikka/wrong-2</loc></url>
      <url><loc>https://www.jobly.fi/tyopaikka/good-3</loc></url>
    </urlset>
    """
    good_html = (
        "<html><head><title>Myyjä</title>"
        '<meta name="description" content="A long enough Jobly description for the listing body.">'
        "</head></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url).endswith("/sitemap.xml"):
            return httpx.Response(200, text=sitemap)
        if str(request.url) == "https://www.jobly.fi/tyopaikka/good-3":
            return httpx.Response(200, text=good_html)
        raise AssertionError(f"off-policy fetch: {request.url}")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )

    adapter = JoblyAdapter()
    adapter.sitemap_urls = ["https://www.jobly.fi/sitemap.xml"]
    result = await adapter.collect(watermark=None)

    assert [listing.canonical_source_url for listing in result.listings] == [
        "https://www.jobly.fi/tyopaikka/good-3"
    ]
    assert set(requested) == {
        "https://www.jobly.fi/sitemap.xml",
        "https://www.jobly.fi/tyopaikka/good-3",
    }


def test_laura_normalize_falls_back_for_off_host_link() -> None:
    payload = {
        "id": 42,
        "link": "https://evil.example/job/test/",
        "title": {"rendered": "Ratsastustuntien pitäjä"},
    }

    listing = LauraAdapter().normalize(payload)

    assert listing.canonical_source_url == "https://laura.fi/?p=42"
    assert listing.application_url is None


def test_laura_normalize_falls_back_for_javascript_link() -> None:
    payload = {"id": 42, "link": "javascript:alert(1)", "title": {"rendered": "x"}}

    listing = LauraAdapter().normalize(payload)

    assert listing.canonical_source_url == "https://laura.fi/?p=42"


def test_eures_normalize_skips_traversal_id() -> None:
    listing = EuresAdapter().normalize({"id": "..", "title": "Chef"})

    assert listing.external_id == ".."
    assert listing.canonical_source_url == ""
    assert listing.application_url == ""


def test_linkedin_parser_skips_off_host_cards() -> None:
    markup = """
    <li>
      <a href="https://evil.example/jobs/view/information-specialist-1234567890"></a>
      <h3 class="base-search-card__title"> Information Specialist </h3>
    </li>
    """

    assert parse_linkedin_guest_jobs(markup) == []


def test_careerjet_normalize_rejects_unsafe_url() -> None:
    payload = {
        "title": "Information Specialist",
        "company": "Example Oy",
        "date": "Wed, 15 Nov 2025 19:13:43 GMT",
        "url": "javascript:alert(1)",
        "site": "example",
    }

    listing = CareerjetAdapter().normalize(payload)

    assert listing.canonical_source_url == ""
    assert listing.application_url is None


def _redirect_handler(target: str, calls: list[str]) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url).endswith("/tyopaikat/start-1"):
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, text="ok")

    return handler


def test_sync_redirect_guard_blocks_escaping_redirect() -> None:
    calls: list[str] = []
    transport = httpx.MockTransport(_redirect_handler("http://127.0.0.1/private", calls))

    with httpx.Client(
        transport=transport,
        follow_redirects=True,
        event_hooks={"response": [source_redirect_guard("kuntarekry")]},
    ) as client:
        with pytest.raises(UrlNavigationRejected) as excinfo:
            client.get("https://kuntarekry.fi/fi/tyopaikat/start-1")

    assert excinfo.value.reason == "private_host"
    assert "127.0.0.1" not in str(excinfo.value)
    assert calls == ["https://kuntarekry.fi/fi/tyopaikat/start-1"]


def test_sync_redirect_guard_blocks_wrong_host_redirect() -> None:
    calls: list[str] = []
    transport = httpx.MockTransport(_redirect_handler("https://evil.example/collect", calls))

    with httpx.Client(
        transport=transport,
        follow_redirects=True,
        event_hooks={"response": [source_redirect_guard("kuntarekry")]},
    ) as client:
        with pytest.raises(UrlNavigationRejected) as excinfo:
            client.get("https://kuntarekry.fi/fi/tyopaikat/start-1")

    assert excinfo.value.reason == "unexpected_host"
    assert calls == ["https://kuntarekry.fi/fi/tyopaikat/start-1"]


def test_sync_redirect_guard_allows_in_policy_redirect() -> None:
    calls: list[str] = []
    transport = httpx.MockTransport(_redirect_handler("https://kuntarekry.fi/fi/tyopaikat/final-2", calls))

    with httpx.Client(
        transport=transport,
        follow_redirects=True,
        event_hooks={"response": [source_redirect_guard("kuntarekry")]},
    ) as client:
        response = client.get("https://kuntarekry.fi/fi/tyopaikat/start-1")

    assert response.status_code == 200
    assert calls == [
        "https://kuntarekry.fi/fi/tyopaikat/start-1",
        "https://kuntarekry.fi/fi/tyopaikat/final-2",
    ]


@pytest.mark.asyncio
async def test_async_redirect_guard_blocks_escaping_redirect() -> None:
    calls: list[str] = []
    transport = httpx.MockTransport(_redirect_handler("http://169.254.169.254/latest/meta-data/", calls))

    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=True,
        event_hooks={"response": [async_source_redirect_guard("kuntarekry")]},
    ) as client:
        with pytest.raises(UrlNavigationRejected) as excinfo:
            await client.get("https://kuntarekry.fi/fi/tyopaikat/start-1")

    assert excinfo.value.reason == "private_host"
    assert "169.254.169.254" not in str(excinfo.value)
    assert calls == ["https://kuntarekry.fi/fi/tyopaikat/start-1"]


def test_browserbase_connect_url_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import browserbase_client

    def fake_client(connect_url: str) -> types.SimpleNamespace:
        class _Sessions:
            def create(self, region: str = "eu-central-1") -> types.SimpleNamespace:
                return types.SimpleNamespace(
                    id="sess_test",
                    connect_url=connect_url,
                    region=region,
                    status="RUNNING",
                )

        return types.SimpleNamespace(sessions=_Sessions())

    monkeypatch.setattr(browserbase_client, "get_browserbase_client", lambda: fake_client("wss://127.0.0.1:9222"))
    with pytest.raises(RuntimeError, match="URL policy"):
        browserbase_client.create_cloud_session()

    monkeypatch.setattr(
        browserbase_client,
        "get_browserbase_client",
        lambda: fake_client("ws://10.0.0.5:9222"),
    )
    with pytest.raises(RuntimeError, match="URL policy"):
        browserbase_client.create_cloud_session()

    monkeypatch.setattr(
        browserbase_client,
        "get_browserbase_client",
        lambda: fake_client("wss://connect.browserbase.com/session"),
    )
    session = browserbase_client.create_cloud_session()
    assert session.connect_url == "wss://connect.browserbase.com/session"
