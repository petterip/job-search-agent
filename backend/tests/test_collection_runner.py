from datetime import datetime, timezone

from app.adapters.base import NormalizedListing, payload_content_hash
from app.collection.runner import (
    dedupe_key,
    is_generic_location,
    normalize_match_text,
    should_mark_missing_source_listings_removed,
    upsert_listing,
)


def listing(
    *,
    title: str,
    employer: str | None,
    location: str | None,
    published_at: datetime | None,
    canonical_source_url: str = "https://example.test/job/1",
) -> NormalizedListing:
    payload = {
        "title": title,
        "employer": employer,
        "location": location,
        "published_at": published_at.isoformat() if published_at else None,
    }
    return NormalizedListing(
        external_id=canonical_source_url.rsplit("/", 1)[-1],
        canonical_source_url=canonical_source_url,
        title=title,
        employer=employer,
        description="Test description",
        location=location,
        published_at=published_at,
        content_hash=payload_content_hash(payload),
        payload=payload,
    )


def test_normalize_match_text_removes_case_punctuation_and_extra_space() -> None:
    assert normalize_match_text("  Senior Data-Engineer, Oulu! ") == "senior data engineer oulu"


def test_dedupe_key_uses_title_employer_location_and_publication_day() -> None:
    published_at = datetime(2026, 6, 20, 8, 30, tzinfo=timezone.utc)
    first = listing(
        title="Senior Data-Engineer",
        employer="Example Oy",
        location="Oulu",
        published_at=published_at,
    )
    second = listing(
        title="senior data engineer",
        employer="Example Oy",
        location="OULU",
        published_at=published_at.replace(hour=15),
    )

    assert dedupe_key(first) == dedupe_key(second)


def test_dedupe_key_returns_none_without_employer_or_publication_time() -> None:
    assert (
        dedupe_key(
            listing(
                title="Developer",
                employer=None,
                location="Oulu",
                published_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
            )
        )
        is None
    )
    assert dedupe_key(listing(title="Developer", employer="Example Oy", location="Oulu", published_at=None)) is None


def test_generic_location_detection_marks_country_scope_as_weak() -> None:
    assert is_generic_location("Suomi")
    assert is_generic_location("Sijainti ei tiedossa")
    assert not is_generic_location("Rantasalmi")


def test_missing_source_listings_only_removed_after_complete_refresh() -> None:
    assert should_mark_missing_source_listings_removed(
        watermark=None,
        fetched_count=10,
        stopped_at_watermark=False,
        max_urls=None,
        max_pages=None,
    )
    assert not should_mark_missing_source_listings_removed(
        watermark=None,
        fetched_count=10,
        stopped_at_watermark=True,
        max_urls=None,
        max_pages=None,
    )
    assert not should_mark_missing_source_listings_removed(
        watermark=None,
        fetched_count=10,
        stopped_at_watermark=False,
        max_urls=10,
        max_pages=None,
    )
    assert not should_mark_missing_source_listings_removed(
        watermark=None,
        fetched_count=0,
        stopped_at_watermark=False,
        max_urls=None,
        max_pages=None,
    )
    assert not should_mark_missing_source_listings_removed(
        watermark=datetime(2026, 6, 20, tzinfo=timezone.utc),
        fetched_count=1,
        stopped_at_watermark=False,
        max_urls=None,
        max_pages=None,
    )


class MappingResult:
    def __init__(self, value: object = None) -> None:
        self.value = value

    def one_or_none(self) -> object:
        return self.value


class ScalarResult:
    def __init__(self, value: object = None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def scalar_one(self) -> object:
        return self.value

    def mappings(self) -> MappingResult:
        return MappingResult(self.value)


class UpsertUnchangedConnection:
    def __init__(self, *, existing_hash: str, existing_job_id: int) -> None:
        self.existing_hash = existing_hash
        self.existing_job_id = existing_job_id
        self.calls = 0
        self.statements: list[str] = []

    def execute(self, statement: object, params: dict[str, object] | None = None) -> ScalarResult:
        sql = str(statement)
        self.statements.append(sql.lower())
        self.calls += 1
        if "select rl.content_hash" in sql:
            return ScalarResult(self.existing_hash)
        if "returning id" in sql:
            return ScalarResult(123)
        if "select js.job_id" in sql:
            return ScalarResult(self.existing_job_id)
        if "from job_description_state" in sql:
            return ScalarResult()
        return ScalarResult()


class RelinkCrossSourceConnection(UpsertUnchangedConnection):
    def __init__(self, *, existing_hash: str, existing_job_id: int, cross_source_job_id: int) -> None:
        super().__init__(existing_hash=existing_hash, existing_job_id=existing_job_id)
        self.cross_source_job_id = cross_source_job_id

    def execute(self, statement: object, params: dict[str, object] | None = None) -> ScalarResult:
        sql = str(statement)
        self.statements.append(sql.lower())
        self.calls += 1
        if "select rl.content_hash" in sql:
            return ScalarResult(self.existing_hash)
        if "returning id" in sql:
            return ScalarResult(123)
        if "select js.job_id" in sql:
            return ScalarResult(self.existing_job_id)
        if "select j.id" in sql:
            return ScalarResult(self.cross_source_job_id)
        if "from job_description_state" in sql:
            return ScalarResult()
        return ScalarResult()


def test_upsert_unchanged_listing_reactivates_removed_job() -> None:
    existing = listing(
        title="Kirjastonhoitaja",
        employer="Rantasalmen kunta",
        location="Rantasalmi",
        published_at=datetime(2026, 6, 18, 10, 25, tzinfo=timezone.utc),
    )
    connection = UpsertUnchangedConnection(
        existing_hash=existing.content_hash,
        existing_job_id=391,
    )

    result = upsert_listing(connection, 1, existing)  # type: ignore[arg-type]

    executed_sql = "\n".join(connection.statements)
    assert result == "unchanged"
    assert "status = 'active'" in executed_sql
    assert "sijainti ei tiedossa" in executed_sql
    assert "application_url = :application_url" in executed_sql


def test_upsert_existing_source_listing_relinks_to_cross_source_duplicate() -> None:
    existing = listing(
        title="Kirjastonhoitaja",
        employer="Rantasalmen kunta",
        location="Rantasalmi",
        published_at=datetime(2026, 6, 18, 10, 25, tzinfo=timezone.utc),
    )
    connection = RelinkCrossSourceConnection(
        existing_hash=existing.content_hash,
        existing_job_id=391,
        cross_source_job_id=1411,
    )

    result = upsert_listing(connection, 1, existing)  # type: ignore[arg-type]

    executed_sql = "\n".join(connection.statements)
    assert result == "deduplicated"
    assert "set job_id = :job_id" in executed_sql
    assert "status = 'superseded'" in executed_sql
