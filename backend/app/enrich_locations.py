import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.adapters.duunitori import DuunitoriAdapter
from app.adapters.eures import EuresAdapter
from app.adapters.jobly import JoblyAdapter
from app.adapters.laura import LauraAdapter
from app.adapters.talentech import normalize_talentech_summary
from app.adapters.tmt import TmtAdapter
from app.adapters.tmt_oulu import TmtOuluAdapter
from app.adapters.varbi import OuluVarbiAdapter
from app.db import get_engine

TALENTECH_BASE_URLS = {
    "kuntarekry": "https://www.kuntarekry.fi",
    "kirkkorekry": "https://kirkkorekry.fi",
}


def location_precision(location: str | None) -> int:
    if not location:
        return 0
    if location in {"Suomi", "Etä"}:
        return 1
    if any(part in location for part in ("maakunta", "Pohjois-", "Etelä-", "Keski-", "Varsinais-", "Uusimaa")):
        return 2
    return 3


def choose_location(current: str | None, candidate: str | None) -> str | None:
    if location_precision(candidate) >= location_precision(current):
        return candidate
    return current


def source_location(source_name: str, payload: dict[str, Any]) -> str | None:
    if source_name == "duunitori":
        return DuunitoriAdapter().normalize(payload).location
    if source_name == "tmt":
        return TmtAdapter().normalize(payload).location
    if source_name == "tmt_oulu":
        return TmtOuluAdapter().normalize(payload).location
    if source_name == "laura":
        return LauraAdapter().normalize(payload).location
    if source_name == "jobly":
        source_url = str(payload.get("source_url") or payload.get("url") or "")
        return JoblyAdapter().normalize(payload, source_url=source_url).location
    if source_name == "eures_fi":
        return EuresAdapter().normalize(payload).location
    if source_name in TALENTECH_BASE_URLS:
        return normalize_talentech_summary(
            payload,
            source_name=source_name,
            base_url=TALENTECH_BASE_URLS[source_name],
            description=payload.get("description_text"),
        ).location
    if source_name == "oulu_varbi":
        payload = dict(payload)
        payload.setdefault("detail_url", payload.get("detail_url") or payload.get("link") or "")
        return OuluVarbiAdapter().normalize(payload).location
    return None


def enrich_job_locations(connection: Connection) -> dict[str, int]:
    rows = connection.execute(
        sa.text(
            """
            select
                j.id as job_id,
                j.location as current_location,
                s.name as source_name,
                rl.payload
            from jobs j
            join job_sources js on js.job_id = j.id
            join sources s on s.id = js.source_id
            join raw_listings rl on rl.id = js.raw_listing_id
            where j.status = 'active'
              and s.enabled = true
            order by j.id, js.last_seen_at desc
            """
        )
    ).mappings()

    best_by_job: dict[int, str | None] = {}
    current_by_job: dict[int, str | None] = {}
    for row in rows:
        job_id = int(row["job_id"])
        current = row["current_location"]
        current_by_job.setdefault(job_id, current)
        payload = dict(row["payload"])
        try:
            candidate = source_location(str(row["source_name"]), payload)
        except Exception:
            candidate = None
        best_by_job[job_id] = choose_location(best_by_job.get(job_id) or current, candidate)

    updated = 0
    for job_id, location in best_by_job.items():
        if location and location != current_by_job.get(job_id):
            connection.execute(
                sa.text(
                    """
                    update jobs
                    set location = :location,
                        updated_at = now()
                    where id = :job_id
                    """
                ),
                {"job_id": job_id, "location": location},
            )
            updated += 1
    return {"examined": len(best_by_job), "updated": updated}


def main() -> None:
    engine = get_engine()
    with engine.begin() as connection:
        result = enrich_job_locations(connection)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
