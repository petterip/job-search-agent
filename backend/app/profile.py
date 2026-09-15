import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.freshness import load_freshness_policy
from app.privacy import load_privacy_policy


class ProfileValidationError(ValueError):
    """Raised when an imported profile would silently not be enforced."""


SUPPORTED_SCHEMA_VERSIONS = {1, 2}

# Top-level keys the pipeline actually consumes. Anything else is reported as a
# warning so an operator is never told a rule is enforced when it is ignored.
CONSUMED_TOP_LEVEL_KEYS = {
    "schema_version",
    "profile_id",
    "display_name",
    "locale",
    "updated",
    "privacy",
    "provenance",
    "location",
    "languages",
    "career_evidence",
    "role_clusters",
    "skills",
    "strength_signals",
    "preferences",
    "exclusions",
    "freshness",
    "llm_guidance",
    "learned",
    "base_revision",
}

DERIVED_LEARNED_KEY = "learned"
DERIVED_BOOSTS_KEY = "learned_boosts"


def profile_json(profile: dict[str, Any]) -> str:
    def default(value: Any) -> str:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")

    return json.dumps(profile, ensure_ascii=False, default=default)


def _require_mapping(profile: dict[str, Any], key: str) -> None:
    value = profile.get(key)
    if value is not None and not isinstance(value, dict):
        raise ProfileValidationError(f"profile.{key} must be a mapping")


def _require_list_of_mappings(profile: dict[str, Any], key: str) -> None:
    value = profile.get(key)
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ProfileValidationError(f"profile.{key} must be a list of mappings")


def _require_string_list(container: dict[str, Any], key: str, *, where: str) -> None:
    value = container.get(key)
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProfileValidationError(f"{where}.{key} must be a list of strings")


def validate_profile_document(profile: dict[str, Any]) -> list[str]:
    """Validate the consumed profile contract. Returns non-fatal warnings.

    Raises :class:`ProfileValidationError` before any database write when a
    supported rule cannot be interpreted, so an unsupported directive is never
    silently presented as enforced.
    """
    if not isinstance(profile, dict):
        raise ProfileValidationError("profile document must be a mapping")
    schema_version = profile.get("schema_version")
    if schema_version is not None:
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ProfileValidationError("profile.schema_version must be an integer")
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ProfileValidationError(
                f"unsupported profile.schema_version {schema_version}; "
                f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
    # Fail closed on unsupported privacy directives and malformed freshness.
    try:
        load_privacy_policy(profile)
        load_freshness_policy(profile)
    except ProfileValidationError:
        raise
    except ValueError as exc:
        raise ProfileValidationError(str(exc)) from exc

    for key in (
        "location",
        "career_evidence",
        "preferences",
        "exclusions",
        "llm_guidance",
        "provenance",
    ):
        _require_mapping(profile, key)
    for key in ("role_clusters", "skills", "strength_signals", "languages"):
        _require_list_of_mappings(profile, key)

    exclusions = profile.get("exclusions") or {}
    if isinstance(exclusions, dict):
        _require_string_list(exclusions, "hard_negative_titles_fi", where="profile.exclusions")
        _require_string_list(
            exclusions,
            "reject_if_required_qualification_missing",
            where="profile.exclusions",
        )
    preferences = profile.get("preferences") or {}
    if isinstance(preferences, dict):
        signals = preferences.get("application_history_signals")
        if signals is not None:
            if not isinstance(signals, dict):
                raise ProfileValidationError(
                    "profile.preferences.application_history_signals must be a mapping"
                )
            for signal_key in signals:
                _require_string_list(
                    signals,
                    signal_key,
                    where="profile.preferences.application_history_signals",
                )
        learned_boosts = preferences.get("learned_boosts")
        if learned_boosts is not None and not isinstance(learned_boosts, dict):
            raise ProfileValidationError("profile.preferences.learned_boosts must be a mapping")
    languages = profile.get("languages")
    if isinstance(languages, list):
        for index, language in enumerate(languages):
            if "code" in language and not isinstance(language["code"], str):
                raise ProfileValidationError(f"profile.languages[{index}].code must be a string")
    guidance = profile.get("llm_guidance") or {}
    if isinstance(guidance, dict):
        rules = guidance.get("profile_rules")
        if rules is not None:
            if not isinstance(rules, list) or not all(isinstance(item, str) for item in rules):
                raise ProfileValidationError(
                    "profile.llm_guidance.profile_rules must be a list of strings"
                )
            if any(not item.strip() or len(item) > 400 for item in rules):
                raise ProfileValidationError(
                    "profile.llm_guidance.profile_rules entries must be 1-400 characters"
                )

    warnings: list[str] = []
    unknown = sorted(set(profile) - CONSUMED_TOP_LEVEL_KEYS)
    if unknown:
        warnings.append(
            "profile contains keys the pipeline does not consume: " + ", ".join(unknown)
        )
    return warnings


def profile_base_revision(profile: dict[str, Any]) -> str:
    """Hash of the user-authored base profile, excluding learner-owned state."""
    base = {
        key: value
        for key, value in profile.items()
        if key not in {"learned", "base_revision"}
    }
    preferences = base.get("preferences")
    if isinstance(preferences, dict):
        base["preferences"] = {
            key: value for key, value in preferences.items() if key != DERIVED_BOOSTS_KEY
        }
    return hashlib.sha256(
        json.dumps(base, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def merge_derived_state(
    imported: dict[str, Any],
    existing: dict[str, Any] | None,
    *,
    reset_derived: bool = False,
) -> dict[str, Any]:
    """Preserve DB-owned learned state on an ordinary profile import."""
    merged = dict(imported)
    if reset_derived or not isinstance(existing, dict):
        return merged
    existing_learned = existing.get(DERIVED_LEARNED_KEY)
    if isinstance(existing_learned, dict) and DERIVED_LEARNED_KEY not in merged:
        merged[DERIVED_LEARNED_KEY] = existing_learned
    elif isinstance(existing_learned, dict) and isinstance(merged.get(DERIVED_LEARNED_KEY), dict):
        combined = dict(existing_learned)
        combined.update(merged[DERIVED_LEARNED_KEY])
        merged[DERIVED_LEARNED_KEY] = combined

    existing_preferences = existing.get("preferences")
    if isinstance(existing_preferences, dict):
        existing_boosts = existing_preferences.get(DERIVED_BOOSTS_KEY)
        if isinstance(existing_boosts, dict):
            merged_preferences = dict(merged.get("preferences") or {})
            if DERIVED_BOOSTS_KEY not in merged_preferences:
                merged_preferences[DERIVED_BOOSTS_KEY] = existing_boosts
            merged["preferences"] = merged_preferences
    return merged


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
    reset_derived: bool = False,
) -> int:
    existing_row = connection.execute(
        sa.text(
            """
            select id, profile
            from job_seeker_profiles
            order by id
            limit 1
            """
        )
    ).mappings().one_or_none()
    existing_profile = None
    existing_id = None
    if existing_row is not None:
        existing_id = int(existing_row["id"])
        raw = existing_row["profile"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        existing_profile = raw if isinstance(raw, dict) else None

    prepared = merge_derived_state(profile, existing_profile, reset_derived=reset_derived)
    prepared["base_revision"] = profile_base_revision(prepared)
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
                {"name": name, "profile": profile_json(prepared)},
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
            "profile_id": existing_id,
            "name": name,
            "profile": profile_json(prepared),
        },
    )
    return existing_id


def load_single_profile(
    path: Path,
    *,
    name: str,
    reset_derived: bool = False,
) -> dict[str, Any]:
    profile = load_profile_document(path)
    warnings = validate_profile_document(profile)
    # Profile import publishes into the same profile row the matcher and learner
    # write, so it must take the shared publication lock.
    from app.collection.runner import acquire_publication_ownership

    ownership = acquire_publication_ownership()
    if ownership is None:
        raise RuntimeError(
            "another publication run holds the active profile lock; retry later"
        )
    try:
        engine = get_engine()
        with engine.begin() as connection:
            profile_id = upsert_single_profile(
                connection,
                name=name,
                profile=profile,
                reset_derived=reset_derived,
            )
    finally:
        ownership.release()
    return {
        "profile_id": profile_id,
        "name": name,
        "top_level_keys": len(profile),
        "base_revision": profile_base_revision(profile),
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the single local job seeker profile into the database.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--name", default="default")
    parser.add_argument(
        "--reset-derived",
        action="store_true",
        help="Discard DB-owned learned state instead of preserving it.",
    )
    args = parser.parse_args()

    result = load_single_profile(args.path, name=args.name, reset_derived=args.reset_derived)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
