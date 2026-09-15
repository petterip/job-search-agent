from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.browserbase_client import CloudBrowserSession
from app.config import get_settings
from app.enrichers import browser as browser_module
from app.enrichers import runner as runner_module
from app.enrichers.base import EnrichmentInput, EnrichmentMethod


@dataclass(frozen=True)
class _FakeCloudSession:
    session_id: str = "sess_test"
    connect_url: str = "wss://connect.browserbase.test"
    dashboard_url: str = "https://www.browserbase.com/sessions/sess_test"
    region: str = "eu-central-1"
    status: str = "RUNNING"


def _enable_browserbase_enrichment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENRICHMENT_ENABLED", "true")
    monkeypatch.setenv("ENRICHMENT_PROVIDER", "browserbase")
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb_live_test")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_browser_session_raises_when_enrichment_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENRICHMENT_ENABLED", "false")
    monkeypatch.setenv("ENRICHMENT_PROVIDER", "browserbase")
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb_live_test")
    get_settings.cache_clear()

    create_calls: list[str] = []
    monkeypatch.setattr(
        browser_module,
        "create_cloud_session",
        lambda **kwargs: create_calls.append("create") or _FakeCloudSession(),
    )

    with pytest.raises(RuntimeError, match="ENRICHMENT_ENABLED=false"):
        async with browser_module.browser_session():
            pass

    assert create_calls == []


@pytest.mark.asyncio
async def test_browser_session_awaits_to_thread_for_create_and_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_browserbase_enrichment(monkeypatch)
    to_thread_calls: list[str] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append(func.__name__)
        return func(*args, **kwargs)

    monkeypatch.setattr(browser_module.asyncio, "to_thread", fake_to_thread)

    def fake_create_cloud_session(**kwargs):
        return CloudBrowserSession(
            session_id="sess_1",
            connect_url="wss://connect.test",
            dashboard_url="https://www.browserbase.com/sessions/sess_1",
            region="eu-central-1",
            status="RUNNING",
        )

    monkeypatch.setattr(browser_module, "create_cloud_session", fake_create_cloud_session)
    release_calls: list[str] = []

    def fake_release_cloud_session(session_id: str) -> None:
        release_calls.append(session_id)

    monkeypatch.setattr(browser_module, "release_cloud_session", fake_release_cloud_session)

    async with browser_module.browser_session():
        pass

    assert len(to_thread_calls) == 2
    assert to_thread_calls[0] == "fake_create_cloud_session"
    assert to_thread_calls[1] == "fake_release_cloud_session"
    assert release_calls == ["sess_1"]


@pytest.mark.asyncio
async def test_browser_session_releases_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browserbase_enrichment(monkeypatch)

    async def fake_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(browser_module.asyncio, "to_thread", fake_to_thread)

    def fake_create_cloud_session(**kwargs):
        return CloudBrowserSession(
            session_id="sess_err",
            connect_url="wss://connect.test",
            dashboard_url="https://www.browserbase.com/sessions/sess_err",
            region="eu-central-1",
            status="RUNNING",
        )

    monkeypatch.setattr(browser_module, "create_cloud_session", fake_create_cloud_session)
    release_calls: list[str] = []

    def fake_release_cloud_session(session_id: str) -> None:
        release_calls.append(session_id)

    monkeypatch.setattr(browser_module, "release_cloud_session", fake_release_cloud_session)

    with pytest.raises(RuntimeError, match="boom"):
        async with browser_module.browser_session():
            raise RuntimeError("boom")

    assert release_calls == ["sess_err"]


@pytest.mark.asyncio
async def test_connect_playwright_over_cdp_closes_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    page = AsyncMock()
    context = MagicMock()
    context.pages = [page]
    browser = AsyncMock()
    browser.contexts = [context]
    browser.close = AsyncMock()

    playwright_instance = AsyncMock()
    chromium = AsyncMock()
    chromium.connect_over_cdp = AsyncMock(return_value=browser)
    playwright_instance.chromium = chromium

    @asynccontextmanager
    async def fake_playwright():
        yield playwright_instance

    fake_async_playwright = MagicMock(return_value=fake_playwright())
    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.async_api",
        MagicMock(async_playwright=fake_async_playwright),
    )

    async with browser_module.connect_playwright_over_cdp("wss://connect.test") as handle:
        assert handle.page is page
        assert handle.browser is browser

    chromium.connect_over_cdp.assert_awaited_once_with("wss://connect.test")
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_playwright_over_cdp_raises_when_playwright_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "playwright.async_api":
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="pip install '.\\[enrichment\\]'"):
        async with browser_module.connect_playwright_over_cdp("wss://connect.test"):
            pass


def _jobly_candidate(
    *,
    canonical_source_url: str,
    application_url: str | None = None,
    source_name: str = "jobly",
) -> EnrichmentInput:
    return EnrichmentInput(
        job_id=1,
        job_source_id=2,
        raw_listing_id=3,
        source_id=4,
        source_name=source_name,
        last_content_hash="hash",
        application_url=application_url if application_url is not None else canonical_source_url,
        canonical_source_url=canonical_source_url,
        title="Myyjä",
        employer="Kauppa Oy",
        published_at=None,
        current_description="short",
        enricher="jobly_browser",
        enricher_version="v1",
    )


class _FakeRoute:
    def __init__(self) -> None:
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakeRequest:
    def __init__(self, url: str, *, navigation: bool = True) -> None:
        self.url = url
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class _FakePage:
    def __init__(self, *, final_url: str, content: str = "") -> None:
        self._final_url = final_url
        self._content = content
        self.goto_calls: list[str] = []
        self.routes: list[tuple[str, Any]] = []

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append(url)

    @property
    def url(self) -> str:
        return self._final_url

    async def content(self) -> str:
        return self._content


def _install_fake_browser(monkeypatch: pytest.MonkeyPatch, page: _FakePage) -> list[str]:
    entered: list[str] = []

    @asynccontextmanager
    async def fake_session(*args: Any, **kwargs: Any):
        entered.append("session")
        yield browser_module.BrowserSessionHandle(
            provider="browserbase",
            session=CloudBrowserSession(
                session_id="sess_test",
                connect_url="wss://connect.browserbase.test",
                dashboard_url="https://www.browserbase.com/sessions/sess_test",
                region="eu-central-1",
                status="RUNNING",
            ),
        )

    @asynccontextmanager
    async def fake_connect(connect_url: str):
        yield types.SimpleNamespace(page=page)

    monkeypatch.setattr(runner_module, "browser_session", fake_session)
    monkeypatch.setattr(runner_module, "connect_playwright_over_cdp", fake_connect)
    return entered


@pytest.mark.asyncio
async def test_jobly_browser_rejects_non_http_navigation_before_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(final_url="https://www.jobly.fi/tyopaikka/x-1")
    entered = _install_fake_browser(monkeypatch, page)

    candidate = _jobly_candidate(canonical_source_url="javascript:alert(1)")

    assert await runner_module._jobly_browser_result_async(candidate) is None
    assert entered == []
    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_jobly_browser_rejects_wrong_host_navigation_before_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(final_url="https://www.jobly.fi/tyopaikka/x-1")
    entered = _install_fake_browser(monkeypatch, page)

    candidate = _jobly_candidate(canonical_source_url="https://evil.example/tyopaikka/x-1")

    assert await runner_module._jobly_browser_result_async(candidate) is None
    assert entered == []
    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_jobly_browser_rejects_redirect_escape_without_leaking_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(final_url="http://169.254.169.254/latest/meta-data/")
    _install_fake_browser(monkeypatch, page)
    candidate = _jobly_candidate(canonical_source_url="https://www.jobly.fi/tyopaikka/x-1")

    with pytest.raises(runner_module.BrowserEnrichmentError) as excinfo:
        await runner_module._jobly_browser_result_async(candidate)

    message = str(excinfo.value)
    assert "reason=private_host" in message
    assert "169.254.169.254" not in message
    assert page.goto_calls == ["https://www.jobly.fi/tyopaikka/x-1"]


@pytest.mark.asyncio
async def test_jobly_browser_extracts_allowed_page(monkeypatch: pytest.MonkeyPatch) -> None:
    long_description = "We are hiring a library professional for municipal services in northern Finland."
    page = _FakePage(
        final_url="https://www.jobly.fi/tyopaikka/x-1",
        content=(
            "<html><head><title>Myyjä</title>"
            f'<meta name="description" content="{long_description}">'
            "</head></html>"
        ),
    )
    _install_fake_browser(monkeypatch, page)
    candidate = _jobly_candidate(canonical_source_url="https://www.jobly.fi/tyopaikka/x-1")

    result = await runner_module._jobly_browser_result_async(candidate)

    assert result is not None
    assert result.method is EnrichmentMethod.BROWSER_CDP
    assert result.description == long_description
    assert page.goto_calls == ["https://www.jobly.fi/tyopaikka/x-1"]


@pytest.mark.asyncio
async def test_jobly_navigation_guard_aborts_off_policy_navigation() -> None:
    page = _FakePage(final_url="https://www.jobly.fi/tyopaikka/x-1")

    await runner_module._install_navigation_guard(page, "jobly")

    assert len(page.routes) == 1
    _, handler = page.routes[0]

    blocked = _FakeRoute()
    await handler(blocked, _FakeRequest("http://127.0.0.1/", navigation=True))
    assert blocked.aborted is True

    allowed = _FakeRoute()
    await handler(allowed, _FakeRequest("https://www.jobly.fi/tyopaikka/y-2", navigation=True))
    assert allowed.continued is True

    subresource = _FakeRoute()
    await handler(subresource, _FakeRequest("https://cdn.example/app.js", navigation=False))
    assert subresource.continued is True


def test_enrichment_tmt_rejects_traversal_external_id() -> None:
    candidate = _jobly_candidate(
        canonical_source_url="https://tyomarkkinatori.fi/x",
        source_name="tmt",
    )

    assert runner_module._http_detail_result(candidate, {"id": "../../etc/passwd"}) is None


def test_enrichment_talentech_rejects_cross_host_detail() -> None:
    candidate = _jobly_candidate(
        canonical_source_url="https://evil.example/collect",
        source_name="kuntarekry",
    )

    assert runner_module._http_detail_result(candidate, {"url": "https://evil.example/collect"}) is None
