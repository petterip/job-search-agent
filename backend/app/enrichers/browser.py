from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator

from app.browserbase_client import (
    CloudBrowserSession,
    create_cloud_session,
    release_cloud_session,
)
from app.config import get_settings

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page


@dataclass(frozen=True)
class BrowserSessionHandle:
    provider: str
    session: CloudBrowserSession | None = None


@dataclass(frozen=True)
class PlaywrightCdpHandle:
    browser: Browser
    page: Page


@asynccontextmanager
async def browser_session(*, region: str = "eu-central-1") -> AsyncIterator[BrowserSessionHandle]:
    settings = get_settings()
    if not settings.enrichment_enabled:
        raise RuntimeError("browser enrichment is disabled (ENRICHMENT_ENABLED=false)")
    provider = settings.enrichment_provider.strip().lower()
    if provider in {"", "off", "disabled"}:
        raise RuntimeError("browser enrichment is disabled (ENRICHMENT_PROVIDER=off)")
    if provider == "browserbase":
        if not settings.browserbase_configured():
            raise RuntimeError("ENRICHMENT_PROVIDER=browserbase requires BROWSERBASE_API_KEY")
        cloud = await asyncio.to_thread(create_cloud_session, region=region)
        try:
            yield BrowserSessionHandle(provider="browserbase", session=cloud)
        finally:
            await asyncio.to_thread(release_cloud_session, cloud.session_id)
        return
    if provider == "local":
        raise RuntimeError(
            "local Playwright enrichment is not implemented yet; set ENRICHMENT_PROVIDER=browserbase"
        )
    raise RuntimeError(f"unsupported ENRICHMENT_PROVIDER: {settings.enrichment_provider}")


@asynccontextmanager
async def connect_playwright_over_cdp(connect_url: str) -> AsyncIterator[PlaywrightCdpHandle]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed; pip install '.[enrichment]' to use browser extraction"
        ) from exc

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(connect_url)
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            yield PlaywrightCdpHandle(browser=browser, page=page)
        finally:
            await browser.close()
