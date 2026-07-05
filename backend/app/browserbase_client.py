from __future__ import annotations

from dataclasses import dataclass

from browserbase import Browserbase

from app.config import get_settings

SESSION_DASHBOARD_URL = "https://www.browserbase.com/sessions/{session_id}"


@dataclass(frozen=True)
class CloudBrowserSession:
    session_id: str
    connect_url: str
    dashboard_url: str
    region: str
    status: str


def get_browserbase_client() -> Browserbase:
    settings = get_settings()
    api_key = settings.browserbase_api_key.strip()
    if not api_key:
        raise RuntimeError("BROWSERBASE_API_KEY is not configured")
    return Browserbase(api_key=api_key)


def create_cloud_session(*, region: str = "eu-central-1") -> CloudBrowserSession:
    client = get_browserbase_client()
    session = client.sessions.create(region=region)
    return CloudBrowserSession(
        session_id=session.id,
        connect_url=session.connect_url,
        dashboard_url=SESSION_DASHBOARD_URL.format(session_id=session.id),
        region=session.region,
        status=session.status,
    )


def release_cloud_session(session_id: str) -> None:
    client = get_browserbase_client()
    client.sessions.update(session_id, status="REQUEST_RELEASE")


def verify_browserbase_access(*, region: str = "eu-central-1") -> CloudBrowserSession:
    session = create_cloud_session(region=region)
    release_cloud_session(session.session_id)
    return session
