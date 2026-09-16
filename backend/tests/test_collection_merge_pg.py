"""Both-order cross-source collection merge (P1-13).

Two sources publishing the same normalized job must produce one canonical job
row whose title/employer/location/description do not depend on collection order.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from app.adapters.base import NormalizedListing, payload_content_hash
from app.collection.runner import upsert_listing

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set; PostgreSQL integration tests skipped",
)

TABLES = (
    "enrichment_attempts",
    "enrichment_queue",
    "job_description_state",
    "job_sources",
    "raw_listings",
    "jobs",
    "sources",
)


@pytest.fixture()
def pg_engine() -> Iterator[Any]:
    engine = create_engine(str(TEST_DATABASE_URL))
    with engine.begin() as connection:
        connection.execute(
            sa.text(f"truncate table {', '.join(TABLES)} restart identity cascade")
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _listing(
    *,
    external_id: str,
    title: str,
    employer: str,
    location: str,
    description: str,
) -> NormalizedListing:
    published_at = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    payload = {
        "title": title,
        "employer": employer,
        "location": location,
        "published_at": published_at.isoformat(),
    }
    return NormalizedListing(
        external_id=external_id,
        canonical_source_url=f"https://example.invalid/{external_id}",
        title=title,
        employer=employer,
        description=description,
        location=location,
        published_at=published_at,
        content_hash=payload_content_hash(payload),
        payload=payload,
    )


SPARSE = _listing(
    external_id="ext-a",
    title="Kirjastonhoitaja",
    employer="Rantasalmen kunta",
    location="Oulu",
    description="Kirjasto.",
)
RICH = _listing(
    external_id="ext-b",
    title="Kirjastonhoitaja",
    employer="Rantasalmen kunta",
    location="OULU",
    description=(
        "Kirjastonhoitaja Rantasalmen kunnalle. Tehtävään kuuluu kokoelmatyö, "
        "asiakaspalvelu ja verkkokirjaston kehittäminen. "
        + "Lisätietoja tehtävästä saa kirjastotoimenjohtajalta. "
        * 5
    ),
)


def _collect(
    connection: Any, first: NormalizedListing, second: NormalizedListing
) -> dict:
    source_a = int(
        connection.execute(
            sa.text(
                "insert into sources (name, method, poll_interval_min, enabled) "
                "values ('source-a', 'api', 60, true) returning id"
            )
        ).scalar_one()
    )
    source_b = int(
        connection.execute(
            sa.text(
                "insert into sources (name, method, poll_interval_min, enabled) "
                "values ('source-b', 'api', 60, true) returning id"
            )
        ).scalar_one()
    )
    first_source = source_a if first.external_id == SPARSE.external_id else source_b
    second_source = source_b if first_source == source_a else source_a
    results = [
        upsert_listing(connection, first_source, first),
        upsert_listing(connection, second_source, second),
    ]
    row = (
        connection.execute(
            sa.text(
                "select id, title, employer, location, description from jobs order by id"
            )
        )
        .mappings()
        .one()
    )
    occurrences = connection.execute(
        sa.text("select count(*) from job_sources where job_id = :job_id"),
        {"job_id": int(row["id"])},
    ).scalar_one()
    return {
        "canonical": dict(row),
        "occurrences": int(occurrences),
        "results": results,
        "jobs": connection.execute(sa.text("select count(*) from jobs")).scalar_one(),
    }


def test_cross_source_merge_is_order_independent(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        sparse_first = _collect(connection, SPARSE, RICH)
    with pg_engine.begin() as connection:
        connection.execute(
            sa.text(f"truncate table {', '.join(TABLES)} restart identity cascade")
        )
        rich_first = _collect(connection, RICH, SPARSE)

    sparse_canonical = sparse_first.pop("canonical")
    rich_canonical = rich_first.pop("canonical")
    sparse_canonical.pop("id")
    rich_canonical.pop("id")

    assert sparse_first == rich_first
    assert sparse_canonical == rich_canonical
    # Either raw spelling may win, but the same one must win in both orders.
    assert sparse_canonical["location"] in {"Oulu", "OULU"}
    assert sparse_canonical["title"] == "Kirjastonhoitaja"
    assert sparse_canonical["employer"] == "Rantasalmen kunta"
    # The richer body wins in both collection orders, normalized on insert.
    assert sparse_canonical["description"] == RICH.description.strip()
    assert sparse_first["jobs"] == 1
    assert sparse_first["occurrences"] == 2
    assert sorted(sparse_first["results"]) == ["deduplicated", "inserted"]
    json.dumps(sparse_first)
