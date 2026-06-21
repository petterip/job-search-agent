import json

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.adapters.talentech import (
    TALENTECH_USER_AGENT,
    extract_talentech_description,
    talentech_canonical_url,
)
from app.adapters.laura import LauraAdapter
from app.adapters.eures import EuresAdapter
from app.adapters.tmt import TmtAdapter
from app.adapters.base import payload_content_hash
from app.db import get_engine

TALENTECH_BASE_URLS = {
    "kuntarekry": "https://www.kuntarekry.fi",
    "kirkkorekry": "https://kirkkorekry.fi",
}


def talentech_rows(connection: Connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            sa.text(
                """
                select
                    j.id as job_id,
                    rl.id as raw_listing_id,
                    s.name as source_name,
                    rl.payload
                from jobs j
                join job_sources js on js.job_id = j.id
                join sources s on s.id = js.source_id
                join raw_listings rl on rl.id = js.raw_listing_id
                where j.status = 'active'
                  and s.name in ('kuntarekry', 'kirkkorekry')
                """
            )
        ).mappings()
    ]


def source_rows(connection: Connection, source_name: str) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            sa.text(
                """
                select
                    j.id as job_id,
                    rl.id as raw_listing_id,
                    rl.payload
                from jobs j
                join job_sources js on js.job_id = j.id
                join sources s on s.id = js.source_id
                join raw_listings rl on rl.id = js.raw_listing_id
                where j.status = 'active'
                  and s.name = :source_name
                """
            ),
            {"source_name": source_name},
        ).mappings()
    ]


def update_description(
    connection: Connection,
    *,
    job_id: int,
    raw_listing_id: int,
    payload: dict,
    description: str,
) -> None:
    payload = dict(payload)
    payload["description_text"] = description
    connection.execute(
        sa.text(
            """
            update jobs
            set description = :description,
                updated_at = now()
            where id = :job_id
            """
        ),
        {"job_id": job_id, "description": description},
    )
    connection.execute(
        sa.text(
            """
            update raw_listings
            set payload = CAST(:payload AS jsonb),
                content_hash = :content_hash,
                last_seen_at = now()
            where id = :raw_listing_id
            """
        ),
        {
            "raw_listing_id": raw_listing_id,
            "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "content_hash": payload_content_hash(payload),
        },
    )


def enrich_talentech_descriptions(connection: Connection) -> dict[str, int]:
    rows = talentech_rows(connection)
    updated = 0
    with httpx.Client(timeout=60, headers={"User-Agent": TALENTECH_USER_AGENT}, follow_redirects=True) as client:
        for row in rows:
            payload = dict(row["payload"])
            base_url = TALENTECH_BASE_URLS[str(row["source_name"])]
            detail_url = talentech_canonical_url(base_url, str(payload["url"]))
            response = client.get(detail_url)
            if response.status_code == 429:
                continue
            response.raise_for_status()
            description = extract_talentech_description(response.text)
            if not description:
                continue
            update_description(
                connection,
                job_id=int(row["job_id"]),
                raw_listing_id=int(row["raw_listing_id"]),
                payload=payload,
                description=description,
            )
            updated += 1
    return {"examined": len(rows), "updated": updated}


def enrich_laura_descriptions(connection: Connection) -> dict[str, int]:
    rows = source_rows(connection, "laura")
    adapter = LauraAdapter()
    updated = 0
    for row in rows:
        payload = dict(row["payload"])
        listing = adapter.normalize(payload)
        if not listing.description:
            continue
        update_description(
            connection,
            job_id=int(row["job_id"]),
            raw_listing_id=int(row["raw_listing_id"]),
            payload=payload,
            description=listing.description,
        )
        updated += 1
    return {"examined": len(rows), "updated": updated}


def enrich_eures_descriptions(connection: Connection) -> dict[str, int]:
    rows = source_rows(connection, "eures_fi")
    adapter = EuresAdapter()
    updated = 0
    for row in rows:
        payload = dict(row["payload"])
        listing = adapter.normalize(payload)
        if not listing.description:
            continue
        update_description(
            connection,
            job_id=int(row["job_id"]),
            raw_listing_id=int(row["raw_listing_id"]),
            payload=payload,
            description=listing.description,
        )
        updated += 1
    return {"examined": len(rows), "updated": updated}


def enrich_tmt_descriptions(connection: Connection, source_name: str) -> dict[str, int]:
    rows = source_rows(connection, source_name)
    adapter = TmtAdapter()
    updated = 0
    with httpx.Client(timeout=30) as client:
        for row in rows:
            payload = dict(row["payload"])
            if payload.get("description_text") or payload.get("detail"):
                listing = adapter.normalize(payload)
            else:
                detail_response = client.get(
                    f"https://tyomarkkinatori.fi/api/jobposting-new/v1/public/jobpostings/{payload['id']}"
                )
                if detail_response.status_code == 404:
                    continue
                detail_response.raise_for_status()
                payload = {**payload, "detail": detail_response.json()}
                listing = adapter.normalize(payload)
            if not listing.description:
                continue
            update_description(
                connection,
                job_id=int(row["job_id"]),
                raw_listing_id=int(row["raw_listing_id"]),
                payload=payload,
                description=listing.description,
            )
            updated += 1
    return {"examined": len(rows), "updated": updated}


def main() -> None:
    engine = get_engine()
    with engine.begin() as connection:
        talentech_result = enrich_talentech_descriptions(connection)
        laura_result = enrich_laura_descriptions(connection)
        eures_result = enrich_eures_descriptions(connection)
        tmt_result = enrich_tmt_descriptions(connection, "tmt")
        tmt_oulu_result = enrich_tmt_descriptions(connection, "tmt_oulu")
        result = {
            "talentech": talentech_result,
            "laura": laura_result,
            "eures_fi": eures_result,
            "tmt": tmt_result,
            "tmt_oulu": tmt_oulu_result,
        }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
