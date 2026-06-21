import argparse
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from sqlalchemy.engine import Connection

from app.db import get_engine


def profile_json(profile: dict[str, Any]) -> str:
    def default(value: Any) -> str:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")

    return json.dumps(profile, ensure_ascii=False, default=default)


def load_profile_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("profile document must be a mapping")
    return data


def upsert_single_profile(
    connection: Connection,
    *,
    name: str,
    profile: dict[str, Any],
) -> int:
    existing_id = connection.execute(
        sa.text(
            """
            select id
            from job_seeker_profiles
            order by id
            limit 1
            """
        )
    ).scalar_one_or_none()
    if existing_id is None:
        return int(
            connection.execute(
                sa.text(
                    """
                    insert into job_seeker_profiles (name, profile)
                    values (:name, CAST(:profile AS jsonb))
                    returning id
                    """
                ),
                {"name": name, "profile": profile_json(profile)},
            ).scalar_one()
        )
    connection.execute(
        sa.text(
            """
            update job_seeker_profiles
            set name = :name,
                profile = CAST(:profile AS jsonb),
                updated_at = now()
            where id = :profile_id
            """
        ),
        {
            "profile_id": int(existing_id),
            "name": name,
            "profile": profile_json(profile),
        },
    )
    return int(existing_id)


def load_single_profile(path: Path, *, name: str) -> dict[str, Any]:
    profile = load_profile_document(path)
    engine = get_engine()
    with engine.begin() as connection:
        profile_id = upsert_single_profile(connection, name=name, profile=profile)
    return {"profile_id": profile_id, "name": name, "top_level_keys": len(profile)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the single local job seeker profile into the database.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--name", default="default")
    args = parser.parse_args()

    result = load_single_profile(args.path, name=args.name)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
