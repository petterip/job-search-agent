from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EnrichmentMethod(str, Enum):
    HTTP_STATIC = "http_static"
    BROWSER_CDP = "browser_cdp"
    SOURCE_NORMALIZE = "source_normalize"


@dataclass(frozen=True)
class EnrichmentInput:
    job_id: int
    job_source_id: int
    raw_listing_id: int
    source_id: int
    source_name: str
    last_content_hash: str
    application_url: str | None
    canonical_source_url: str | None
    title: str
    employer: str | None
    published_at: datetime | None
    current_description: str | None
    enricher: str
    enricher_version: str


@dataclass(frozen=True)
class EnrichmentResult:
    job_id: int
    job_source_id: int
    raw_listing_id: int
    enricher: str
    input_hash: str
    description: str | None
    method: EnrichmentMethod
    confidence: float
    structured_fields: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None
    browser_session_id: str | None = None
    browser_dashboard_url: str | None = None

    @property
    def applied(self) -> bool:
        return bool(self.description and self.description.strip())
