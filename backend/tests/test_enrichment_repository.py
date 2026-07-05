from datetime import datetime, timezone

import pytest

from app.enrichers.base import EnrichmentMethod, EnrichmentResult
from app.enrichers.repository import (
    apply_enrichment_result,
    compute_enrichment_input_hash,
    description_is_short,
    preserve_enriched_description_on_upsert,
)
from app.enrichers.runner import run_enrichment


class ScalarResult:
    def __init__(self, value: object = None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def scalar_one(self) -> object:
        return self.value

    def mappings(self) -> "MappingResult":
        return MappingResult(self.value)


class MappingResult:
    def __init__(self, value: object = None) -> None:
        self.value = value

    def one_or_none(self) -> object:
        return self.value


class ProvenanceConnection:
    def __init__(
        self,
        *,
        description: str | None = None,
        provenance: str | None = None,
    ) -> None:
        self.description = description
        self.provenance = provenance
        self.statements: list[str] = []

    def execute(self, statement: object, params: dict[str, object] | None = None) -> ScalarResult:
        sql = str(statement).lower()
        self.statements.append(sql)
        if "from job_description_state" in sql:
            if self.provenance is None:
                return ScalarResult()
            return ScalarResult(
                {
                    "job_id": params["job_id"] if params else None,
                    "provenance": self.provenance,
                    "source_id": 1,
                    "enricher": None,
                    "input_hash": None,
                    "confidence": None,
                }
            )
        if "select description from jobs" in sql:
            return ScalarResult(self.description)
        return ScalarResult()


def test_compute_enrichment_input_hash_is_stable() -> None:
    published_at = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    first = compute_enrichment_input_hash(
        last_content_hash="abc123",
        application_url="https://example.test/apply",
        canonical_source_url="https://example.test/job/1",
        title="Kirjastonhoitaja",
        employer="Example Oy",
        published_at=published_at,
        enricher="detail_http",
    )
    second = compute_enrichment_input_hash(
        last_content_hash="abc123",
        application_url="https://example.test/apply",
        canonical_source_url="https://example.test/job/1",
        title="Kirjastonhoitaja",
        employer="Example Oy",
        published_at=published_at,
        enricher="detail_http",
    )

    assert first == second
    assert len(first) == 64


def test_description_is_short_threshold() -> None:
    assert description_is_short(None, threshold=200) is True
    assert description_is_short("short", threshold=200) is True
    assert description_is_short("x" * 200, threshold=200) is False


def test_preserve_enriched_keeps_existing_when_source_has_no_body() -> None:
    connection = ProvenanceConnection(
        description="Enriched body that must survive source re-collection.",
        provenance="enriched",
    )

    preserved = preserve_enriched_description_on_upsert(
        connection,  # type: ignore[arg-type]
        job_id=42,
        source_description=None,
        source_id=3,
    )

    assert preserved == "Enriched body that must survive source re-collection."
    assert not any("insert into job_description_state" in sql for sql in connection.statements)


def test_preserve_enriched_records_source_provenance_when_source_provides_body() -> None:
    connection = ProvenanceConnection(description="Old enriched body", provenance="enriched")

    preserved = preserve_enriched_description_on_upsert(
        connection,  # type: ignore[arg-type]
        job_id=42,
        source_description="New source-provided description.",
        source_id=3,
    )

    assert preserved == "New source-provided description."
    assert any("insert into job_description_state" in sql for sql in connection.statements)


def test_apply_enrichment_result_does_not_replace_adequate_source_description() -> None:
    connection = ProvenanceConnection(
        description="x" * 200,
        provenance="source",
    )
    result = EnrichmentResult(
        job_id=42,
        enricher="detail_http",
        input_hash="hash-1",
        description="Much longer enriched body that should not replace an adequate source description.",
        method=EnrichmentMethod.HTTP_STATIC,
        confidence=0.9,
    )

    applied = apply_enrichment_result(
        connection,  # type: ignore[arg-type]
        result,
        short_description_threshold=200,
    )

    assert applied is False
    assert not any("update jobs" in sql and "set description" in sql for sql in connection.statements)


def test_run_enrichment_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENRICHMENT_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()

    result = run_enrichment(dry_run=False)

    assert result["status"] == "skipped"


def test_run_enrichment_enqueue_only_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENRICHMENT_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()

    result = run_enrichment(enqueue_only=True)

    assert result["status"] == "skipped"
