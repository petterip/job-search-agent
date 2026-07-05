import json

import sqlalchemy as sa

from app.adapters.duunitori import duunitori_job_url
from app.adapters.tmt import tmt_detail_url
from app.db import get_engine
from app.source_links import TMT_SOURCE_NAMES


def repair_duunitori_job_urls(connection: sa.Connection) -> dict[str, int]:
    rows = connection.execute(
        sa.text(
            """
            select js.id, js.external_id, js.raw_listing_id
            from job_sources js
            join sources s on s.id = js.source_id
            where s.name = 'duunitori'
              and js.external_id is not null
              and js.application_url like 'https://duunitori.fi/tyopaikat/%'
              and js.application_url not like 'https://duunitori.fi/tyopaikat/tyo/%'
            """
        )
    ).mappings()
    job_sources = 0
    raw_listings = 0
    for row in rows:
        url = duunitori_job_url(str(row["external_id"]))
        connection.execute(
            sa.text("update job_sources set application_url = :url where id = :id"),
            {"url": url, "id": row["id"]},
        )
        job_sources += 1
        if row["raw_listing_id"] is not None:
            connection.execute(
                sa.text("update raw_listings set canonical_source_url = :url where id = :id"),
                {"url": url, "id": row["raw_listing_id"]},
            )
            raw_listings += 1
    return {"job_sources": job_sources, "raw_listings": raw_listings}


def repair_tmt_job_urls(connection: sa.Connection) -> dict[str, int]:
    rows = connection.execute(
        sa.text(
            """
            select js.id, js.external_id, js.raw_listing_id
            from job_sources js
            join sources s on s.id = js.source_id
            where s.name = any(:source_names)
              and js.external_id is not null
              and (
                js.application_url is null
                or js.application_url not like 'https://tyomarkkinatori.fi/%'
              )
            """
        ),
        {"source_names": list(TMT_SOURCE_NAMES)},
    ).mappings()
    job_sources = 0
    raw_listings = 0
    for row in rows:
        url = tmt_detail_url(str(row["external_id"]))
        connection.execute(
            sa.text("update job_sources set application_url = :url where id = :id"),
            {"url": url, "id": row["id"]},
        )
        job_sources += 1
        if row["raw_listing_id"] is not None:
            connection.execute(
                sa.text("update raw_listings set canonical_source_url = :url where id = :id"),
                {"url": url, "id": row["raw_listing_id"]},
            )
            raw_listings += 1
    return {"job_sources": job_sources, "raw_listings": raw_listings}


def repair_source_job_urls(connection: sa.Connection) -> dict[str, dict[str, int]]:
    return {
        "duunitori": repair_duunitori_job_urls(connection),
        "tmt": repair_tmt_job_urls(connection),
    }


def main() -> None:
    engine = get_engine()
    with engine.begin() as connection:
        counts = repair_source_job_urls(connection)
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
