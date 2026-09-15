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
from app.enrichers import runner as runner_module


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
                    "job_source_id": 10,
                    "job_enrichment_id": None,
                    "enricher": None,
                    "input_hash": None,
                    "confidence": None,
                }
            )
        if "from job_sources" in sql and "join jobs" in sql:
            return ScalarResult(
                {
                    "job_id": 42,
                    "raw_listing_id": 99,
                    "status": "active",
                    "last_content_hash": "abc123",
                    "application_url": "https://example.test/apply",
                    "canonical_source_url": "https://example.test/job/1",
                    "title": "Kirjastonhoitaja",
                    "employer": "Example Oy",
                    "published_at": datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
                    "enabled": True,
                }
            )
        if "select description from jobs" in sql:
            return ScalarResult(self.description)
        if "insert into job_enrichments" in sql:
            return ScalarResult(777)
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
        job_source_id=10,
    )

    # None means "keep the current canonical description".
    assert preserved is None
    assert not any("insert into job_description_state" in sql for sql in connection.statements)


def test_preserve_enriched_records_source_provenance_when_source_provides_body() -> None:
    connection = ProvenanceConnection(description="Old enriched body", provenance="enriched")

    preserved = preserve_enriched_description_on_upsert(
        connection,  # type: ignore[arg-type]
        job_id=42,
        source_description="New source-provided description.",
        job_source_id=10,
    )

    assert preserved == "New source-provided description."
    assert any("insert into job_description_state" in sql for sql in connection.statements)


def _occurrence_input_hash(enricher: str) -> str:
    return compute_enrichment_input_hash(
        last_content_hash="abc123",
        application_url="https://example.test/apply",
        canonical_source_url="https://example.test/job/1",
        title="Kirjastonhoitaja",
        employer="Example Oy",
        published_at=datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
        enricher=enricher,
    )

def test_apply_enrichment_result_does_not_replace_adequate_source_description() -> None:
    connection = ProvenanceConnection(
        description="x" * 200,
        provenance="source",
    )
    result = EnrichmentResult(
        job_id=42,
        job_source_id=10,
        raw_listing_id=99,
        enricher="detail_http",
        input_hash=_occurrence_input_hash("detail_http"),
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


def test_jobly_browser_dry_run_uses_browser_cap_and_jobly_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENRICHMENT_ENABLED", "false")
    monkeypatch.setenv("JOBLY_BROWSER_ENRICH_MAX_PER_RUN", "7")
    from app.config import get_settings

    get_settings.cache_clear()
    calls: list[dict[str, object]] = []

    def fake_plan(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"candidate_count": 0, "actions": []}

    monkeypatch.setattr(runner_module, "plan_enrichment_actions", fake_plan)

    result = run_enrichment(dry_run=True, enricher="jobly_browser", max_jobs=50)

    assert result["status"] == "dry_run"
    assert calls == [{"enricher": "jobly_browser", "source": "jobly", "max_jobs": 7}]


def test_apply_enrichment_result_ignores_a_stale_response() -> None:
    connection = ProvenanceConnection(description="short", provenance="source")
    result = EnrichmentResult(
        job_id=42,
        job_source_id=10,
        raw_listing_id=99,
        enricher="detail_http",
        input_hash="stale-input-hash",
        description="Enriched body from an older input.",
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


def test_short_source_summary_does_not_replace_richer_enriched_body() -> None:
    connection = ProvenanceConnection(
        description="Enriched body that is much longer than the incoming summary.",
        provenance="enriched",
    )

    preserved = preserve_enriched_description_on_upsert(
        connection,  # type: ignore[arg-type]
        job_id=42,
        source_description="Short summary.",
        job_source_id=10,
    )

    assert preserved is None


def test_unchanged_source_keeps_canonical_description() -> None:
    connection = ProvenanceConnection(description="Canonical body.", provenance="source")

    preserved = preserve_enriched_description_on_upsert(
        connection,  # type: ignore[arg-type]
        job_id=42,
        source_description="A different but equal-length summary.",
        job_source_id=10,
        content_changed=False,
    )

    assert preserved is None


def test_richer_source_edit_replaces_weaker_body() -> None:
    connection = ProvenanceConnection(description="Short.", provenance="source")

    preserved = preserve_enriched_description_on_upsert(
        connection,  # type: ignore[arg-type]
        job_id=42,
        source_description="A clearly longer and corrected source description.",
        job_source_id=10,
        content_changed=True,
    )

    assert preserved == "A clearly longer and corrected source description."


def test_canonical_field_is_order_independent() -> None:
    from app.enrichers.repository import canonical_field

    assert canonical_field("Suomi", "Kokkola") == "Kokkola"
    assert canonical_field("Kokkola", "Suomi") == "Kokkola"
    assert canonical_field(None, "Oulu") == "Oulu"
    assert canonical_field("Oulu", None) == "Oulu"
    # Equal length resolves to the lexicographically smaller value both ways.
    assert canonical_field("abcd", "abce") == "abcd"
    assert canonical_field("abce", "abcd") == "abcd"
