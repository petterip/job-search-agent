from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.browserbase_client import CloudBrowserSession
from app.config import get_settings
from app.enrichers import browser as browser_module


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
