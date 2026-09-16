"""Labelled evaluation tooling for the recommendation pipeline (P2-23).

The relevance labels themselves are private human input. This module consumes a
private labels file (never committed) and reports per-stage coverage by stratum
with explicit denominators and Wilson uncertainty, so quality claims are not
made from a biased feedback sample.

Labels file format (JSON):

```json
{
  "schema_version": 1,
  "sample_method": "how the sample was drawn",
  "labels": [
    {"job_id": 123, "stratum": "oulu_local", "relevant": true, "source": "user"}
  ]
}
```

Stages are observable pipeline states: a recommendation row exists, the job is
hard-eligible, it has been LLM-reviewed, and it is published.

`--build-sample` writes an unlabelled private template. Strata that need a
recommendation row reuse the same structural eligibility predicate as the
publication path. Jobs with no row are classified with the pipeline's own
deterministic and hard gates, so a deterministic reject is counted as a
rejection instead of inflating the not-retrieved (recall-miss) bucket. The
destination must stay outside the repository or inside the ignored `profile/`
tree, no path component is followed through a symlink, publishing is atomic
through a temporary file, and an existing file is never replaced without
`--force`.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import math
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.db import get_engine
from app.freshness import capture_as_of
from app.matching import (
    JobForScoring,
    apply_hard_eligibility,
    score_job,
    structural_eligibility_predicate,
)

STAGES = ("recommendation_row", "hard_eligible", "llm_reviewed", "published")
Z_95 = 1.959963984540054

logger = logging.getLogger(__name__)


class LabelledEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Label:
    job_id: int
    stratum: str
    relevant: bool


@dataclass(frozen=True)
class StageState:
    job_id: int
    recommendation_row: bool
    hard_eligible: bool
    llm_reviewed: bool
    published: bool
    accepted: bool = False


def wilson_interval(
    successes: int, total: int, *, z: float = Z_95
) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial proportion."""
    if total <= 0:
        return None
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = (
        z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total) / denominator
    )
    return (round(max(0.0, centre - margin), 3), round(min(1.0, centre + margin), 3))


def load_labels(path: Path) -> tuple[list[Label], str]:
    if not path.exists():
        raise LabelledEvaluationError(f"labelled evaluation file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LabelledEvaluationError("labelled evaluation file must be a JSON object")
    raw_labels = payload.get("labels")
    if not isinstance(raw_labels, list) or not raw_labels:
        raise LabelledEvaluationError("labelled evaluation file contains no labels")
    labels: list[Label] = []
    for index, item in enumerate(raw_labels):
        if not isinstance(item, dict):
            raise LabelledEvaluationError(f"labels[{index}] must be an object")
        job_id = item.get("job_id")
        relevant = item.get("relevant")
        stratum = item.get("stratum") or "unspecified"
        if not isinstance(job_id, int) or isinstance(job_id, bool):
            raise LabelledEvaluationError(f"labels[{index}].job_id must be an integer")
        if not isinstance(relevant, bool):
            raise LabelledEvaluationError(
                f"labels[{index}].relevant must be true or false; "
                "an unlabelled sample template must be filled in before evaluation"
            )
        labels.append(Label(job_id=job_id, stratum=str(stratum), relevant=relevant))
    sample_method = str(payload.get("sample_method") or "").strip()
    return labels, sample_method


def load_stage_states(
    connection: Connection,
    *,
    profile_id: int,
    job_ids: list[int],
) -> dict[int, StageState]:
    if not job_ids:
        return {}
    rows = connection.execute(
        sa.text(
            """
            select
                j.id as job_id,
                (r.id is not null) as recommendation_row,
                coalesce(r.deterministic_result->>'hard_eligible', 'true') <> 'false'
                    as hard_eligible,
                (r.llm_evaluation_id is not null) as llm_reviewed,
                coalesce(r.is_active, false) as published,
                coalesce(rf.rating >= 4 or rf.applied, false) as accepted
            from jobs j
            left join recommendations r
              on r.job_id = j.id and r.profile_id = :profile_id
            left join recommendation_feedback rf on rf.recommendation_id = r.id
            where j.id = any(:job_ids)
            """
        ),
        {"profile_id": profile_id, "job_ids": job_ids},
    ).mappings()
    return {
        int(row["job_id"]): StageState(
            job_id=int(row["job_id"]),
            recommendation_row=bool(row["recommendation_row"]),
            hard_eligible=bool(row["hard_eligible"]),
            llm_reviewed=bool(row["llm_reviewed"]),
            published=bool(row["published"]),
            accepted=bool(row["accepted"]),
        )
        for row in rows
    }


def _stage_summary(
    labels: list[Label], states: dict[int, StageState]
) -> dict[str, Any]:
    relevant = [label for label in labels if label.relevant]
    published = [
        label
        for label in labels
        if (state := states.get(label.job_id)) is not None and state.published
    ]
    relevant_published = sum(1 for label in published if label.relevant)
    stages: dict[str, Any] = {}
    for stage in STAGES:
        reached = sum(
            1
            for label in relevant
            if (state := states.get(label.job_id)) is not None and getattr(state, stage)
        )
        stages[stage] = {
            "numerator": reached,
            "denominator": len(relevant),
            "value": round(reached / len(relevant), 3) if relevant else None,
            "wilson_95": wilson_interval(reached, len(relevant)),
        }
    accepted = sum(
        1
        for label in relevant
        if (state := states.get(label.job_id)) is not None and state.accepted
    )
    stages["accepted"] = {
        "numerator": accepted,
        "denominator": len(relevant),
        "value": round(accepted / len(relevant), 3) if relevant else None,
        "wilson_95": wilson_interval(accepted, len(relevant)),
    }
    return {
        "labelled": len(labels),
        "relevant": len(relevant),
        "not_relevant": len(labels) - len(relevant),
        "published_total": len(published),
        "relevant_published": relevant_published,
        # Precision has a different denominator from recall; name it explicitly.
        "published_precision": {
            "numerator": relevant_published,
            "denominator": len(published),
            "value": round(relevant_published / len(published), 3)
            if published
            else None,
            "wilson_95": wilson_interval(relevant_published, len(published)),
        },
        "stages": stages,
    }


def deduplicate_labels(labels: list[Label]) -> list[Label]:
    """Collapse repeated job ids for overall metrics, rejecting contradictions.

    A job can legitimately belong to several strata (for example a published
    local job that is also accepted and unreviewed). Overall recall and
    precision must count that job once; per-stratum memberships stay intact.
    """
    deduplicated: dict[int, Label] = {}
    for label in labels:
        existing = deduplicated.get(label.job_id)
        if existing is None:
            deduplicated[label.job_id] = label
            continue
        if existing.relevant != label.relevant:
            raise LabelledEvaluationError(
                f"job {label.job_id} is labelled both relevant and not relevant "
                f"in strata {existing.stratum!r} and {label.stratum!r}"
            )
    return list(deduplicated.values())


def evaluate_labels(
    labels: list[Label],
    states: dict[int, StageState],
    *,
    sample_method: str = "",
) -> dict[str, Any]:
    strata: dict[str, list[Label]] = {}
    for label in labels:
        strata.setdefault(label.stratum, []).append(label)
    overall_labels = deduplicate_labels(labels)
    return {
        "sample_method": sample_method,
        "labels": len(labels),
        "unique_jobs": len(overall_labels),
        "overall": _stage_summary(overall_labels, states),
        "by_stratum": {
            stratum: _stage_summary(stratum_labels, states)
            for stratum, stratum_labels in sorted(strata.items())
        },
        "notes": [
            "Recall denominators are labelled positives only; precision denominators are published labelled rows.",
            "Overall metrics count each job once even when it belongs to several strata; per-stratum metrics keep every membership.",
            "Strata are reported separately because a single biased sample cannot estimate population recall.",
            "Wilson 95% intervals are reported for every proportion.",
        ],
    }


ROW_STRATA: dict[str, str] = {
    "oulu_local": """
        select j.id as job_id, j.title, j.employer, j.location
        from recommendations r
        join jobs j on j.id = r.job_id
        where r.profile_id = :profile_id
          and r.is_active = true
          and ({commutable_scope})
          and ({predicate})
    """,
    "nationwide": """
        select j.id as job_id, j.title, j.employer, j.location
        from recommendations r
        join jobs j on j.id = r.job_id
        where r.profile_id = :profile_id
          and r.is_active = true
          and r.commutable = false
          and r.commutable_or_full_remote = false
          and ({predicate})
    """,
    "remote": """
        select j.id as job_id, j.title, j.employer, j.location
        from recommendations r
        join jobs j on j.id = r.job_id
        where r.profile_id = :profile_id
          and r.is_active = true
          and r.commutable = false
          and ({remote_scope})
          and ({predicate})
    """,
    "hard_rejection": """
        select j.id as job_id, j.title, j.employer, j.location
        from recommendations r
        join jobs j on j.id = r.job_id
        where r.profile_id = :profile_id
          and coalesce(r.deterministic_result->>'hard_eligible', 'true') = 'false'
    """,
    "unreviewed": """
        select j.id as job_id, j.title, j.employer, j.location
        from recommendations r
        join jobs j on j.id = r.job_id
        left join llm_evaluations e on e.id = r.llm_evaluation_id
        where r.profile_id = :profile_id
          and (
              r.llm_evaluation_id is null
              or e.prompt_version is distinct from :prompt_version
          )
          and ({predicate})
    """,
    "accepted": """
        select j.id as job_id, j.title, j.employer, j.location
        from recommendations r
        join jobs j on j.id = r.job_id
        join recommendation_feedback rf on rf.recommendation_id = r.id
        where r.profile_id = :profile_id
          and (rf.rating >= 4 or rf.applied = true)
    """,
}

# Jobs that exist in an enabled source but have no recommendation row for the
# profile. They are split with the pipeline's own deterministic gates, because a
# missing row can mean either "never retrieved" or "retrieved and rejected".
UNRETRIEVED_JOBS_SQL = """
    select
        j.id as job_id,
        j.title,
        j.employer,
        j.description,
        j.location,
        j.published_at,
        j.created_at as first_seen_at,
        j.expires_at
    from jobs j
    where j.status = 'active'
      and exists (
          select 1 from job_sources js join sources s on s.id = js.source_id
          where js.job_id = j.id and s.enabled = true and js.closed_at is null
      )
      and not exists (
          select 1 from recommendations r
          where r.job_id = j.id and r.profile_id = :profile_id
      )
    order by md5(j.id::text || :seed)
    limit :scan_limit
"""

STRATUM_DEFINITIONS: dict[str, str] = {
    "oulu_local": "published commutable recommendation (structural eligibility applied)",
    "nationwide": "published non-commutable, non-remote recommendation (structural eligibility applied)",
    "remote": "published non-commutable, full-remote recommendation (structural eligibility applied)",
    "hard_rejection": "persisted or computed hard-gate rejection",
    "unreviewed": "structurally eligible row without a current prompt-version evaluation",
    "accepted": "recommendation the user rated 4-5 or applied to",
    "not_retrieved": "no row, but the job passes the current deterministic and hard gates",
    "deterministic_rejection": "no row, and the current deterministic gates reject it (diagnostic only)",
}


def _load_profile_document(
    connection: Connection, profile_id: int
) -> dict[str, Any] | None:
    row = (
        connection.execute(
            sa.text("select profile from job_seeker_profiles where id = :profile_id"),
            {"profile_id": profile_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return dict(row["profile"])


def _seeded_sample(
    connection: Connection,
    select_sql: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = connection.execute(
        sa.text(
            f"""
            with candidates as ({select_sql})
            select * from candidates
            order by md5(job_id::text || :seed)
            limit :sample_limit
            """
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]


def classify_unretrieved_jobs(
    connection: Connection,
    *,
    profile: dict[str, Any],
    profile_id: int,
    seed: str,
    scan_limit: int,
    as_of: Any,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Split source-backed jobs without a recommendation row using the real gates.

    Only missing rows that would pass today's deterministic and hard gates are
    retrieval misses; everything else is an explicit rejection, so a
    deterministic reject can never inflate measured recall.
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        "not_retrieved": [],
        "hard_rejection": [],
        "deterministic_rejection": [],
        "classification_error": [],
    }
    scanned = 0
    rows = connection.execute(
        sa.text(UNRETRIEVED_JOBS_SQL),
        {"profile_id": profile_id, "seed": seed, "scan_limit": scan_limit},
    ).mappings()
    for row in rows:
        scanned += 1
        job = JobForScoring(
            id=int(row["job_id"]),
            title=str(row["title"]),
            employer=row["employer"],
            description=row["description"],
            location=row["location"],
            published_at=row["published_at"],
            first_seen_at=row["first_seen_at"],
            expires_at=row["expires_at"],
        )
        try:
            result = score_job(profile, job)
            if result.hard_reasons:
                bucket = "hard_rejection"
            elif not result.passes:
                bucket = "deterministic_rejection"
            else:
                gated = apply_hard_eligibility(
                    [(job, result)], profile=profile, as_of=as_of
                )[0][1]
                bucket = (
                    "not_retrieved"
                    if gated.passes and gated.hard_eligible
                    else "hard_rejection"
                )
        except Exception:
            # One unscoreable job must not abort the whole sample; keep it out of
            # the recall-miss bucket and report the count instead.
            logger.exception(
                "event=label_sample_classification_failed job_id=%s", job.id
            )
            bucket = "classification_error"
        buckets[bucket].append(
            {
                "job_id": job.id,
                "title": job.title,
                "employer": job.employer,
                "location": job.location,
            }
        )
    return buckets, scanned


def build_label_sample(
    connection: Connection,
    *,
    profile_id: int,
    per_stratum: int = 50,
    seed: str = "job-search-agent",
    profile: dict[str, Any] | None = None,
    prompt_version: int | None = None,
    as_of: Any = None,
    scan_limit: int = 5000,
) -> dict[str, Any]:
    """Build a private, unlabelled sample template across the required strata."""
    if per_stratum <= 0:
        raise LabelledEvaluationError("per_stratum must be positive")
    if scan_limit <= 0:
        raise LabelledEvaluationError("scan_limit must be positive")
    if profile is None:
        profile = _load_profile_document(connection, profile_id)
        if profile is None:
            raise LabelledEvaluationError(f"profile {profile_id} not found")
    resolved_as_of = capture_as_of(as_of)
    if prompt_version is None:
        prompt_version = int(get_settings().llm_prompt_version)
    from app.main import current_travel_policy_params, recommendation_scope_sql

    commutable_scope, _ = recommendation_scope_sql("commutable")
    remote_scope, _ = recommendation_scope_sql("commutable_or_full_remote")
    predicate, predicate_params = structural_eligibility_predicate(
        profile=profile, as_of=resolved_as_of
    )
    labels: list[dict[str, Any]] = []
    per_stratum_counts: dict[str, int] = {}
    for stratum, sql in ROW_STRATA.items():
        params: dict[str, Any] = {
            "profile_id": profile_id,
            "seed": seed,
            "sample_limit": per_stratum,
            "prompt_version": prompt_version,
        }
        params.update(predicate_params)
        params.update(current_travel_policy_params(profile))
        rows = _seeded_sample(
            connection,
            sql.format(
                predicate=predicate,
                commutable_scope=commutable_scope,
                remote_scope=remote_scope,
            ),
            params,
        )
        per_stratum_counts[stratum] = len(rows)
        labels.extend(
            {
                "job_id": int(row["job_id"]),
                "stratum": stratum,
                "relevant": None,
                "title": row["title"],
                "employer": row["employer"],
                "location": row["location"],
            }
            for row in rows
        )

    buckets, scanned = classify_unretrieved_jobs(
        connection,
        profile=profile,
        profile_id=profile_id,
        seed=seed,
        scan_limit=scan_limit,
        as_of=resolved_as_of,
    )
    for stratum in ("hard_rejection", "not_retrieved"):
        remaining = max(per_stratum - per_stratum_counts.get(stratum, 0), 0)
        rows = buckets[stratum][:remaining]
        per_stratum_counts[stratum] = per_stratum_counts.get(stratum, 0) + len(rows)
        labels.extend(
            {
                "job_id": int(row["job_id"]),
                "stratum": stratum,
                "relevant": None,
                "title": row["title"],
                "employer": row["employer"],
                "location": row["location"],
            }
            for row in rows
        )
    return {
        "schema_version": 1,
        "sample_method": (
            f"stratified deterministic sample (seed={seed}, up to {per_stratum} per stratum); "
            "missing rows are classified with the current deterministic and hard gates "
            f"(scan_limit={scan_limit}); fill in relevant=true/false from the job "
            "description before evaluation"
        ),
        "stratum_definitions": STRATUM_DEFINITIONS,
        "unlabelled": True,
        "scan": {
            "scanned": scanned,
            "scan_limit": scan_limit,
            "scan_limit_reached": scanned >= scan_limit,
            "deterministic_rejection": len(buckets["deterministic_rejection"]),
            "classification_error": len(buckets["classification_error"]),
        },
        "per_stratum_counts": per_stratum_counts,
        "labels": labels,
    }


def _project_root() -> Path | None:
    """Locate the repository/project root used by the private-path policy.

    A source checkout is recognised by its version-control directory. The
    container image ships no ``.git`` and has a different directory depth
    (``/app/app/...``), so the project file marks the root there instead.
    Returns ``None`` when neither marker exists, and then only the ``profile/``
    rule applies rather than treating the filesystem root as the repository.
    """
    module_path = Path(__file__).resolve()
    for parent in module_path.parents:
        if (parent / ".git").exists():
            return parent
    for parent in module_path.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def resolve_private_destination(path: Path) -> Path:
    """Validate the private destination and keep it unresolved for the writer.

    The containment policy is checked against the fully resolved path, but the
    returned path keeps the caller's components so the writer can refuse a
    symlinked component instead of silently writing to its target.
    """
    if path.is_symlink():
        raise LabelledEvaluationError(
            f"{path} is a symbolic link; refusing to write private sample data through it"
        )
    absolute = path if path.is_absolute() else Path.cwd() / path
    resolved = absolute.resolve()
    repo_root = _project_root()
    if repo_root is not None and (
        resolved == repo_root or repo_root in resolved.parents
    ):
        private_root = repo_root / "profile"
        if resolved == private_root or private_root not in resolved.parents:
            raise LabelledEvaluationError(
                "refusing to write a private sample inside the repository outside "
                f"profile/: {resolved}"
            )
    return absolute


def _open_parent_directory(path: Path, *, create: bool) -> int:
    """Open the parent directory without following any symlinked component.

    Each component is opened relative to the previously held descriptor with
    ``O_DIRECTORY | O_NOFOLLOW``, so swapping an ancestor for a symlink after
    destination validation cannot redirect the write elsewhere.
    """
    if not path.is_absolute():
        raise LabelledEvaluationError(f"destination must be absolute: {path}")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise LabelledEvaluationError(
                        f"refusing to write through a symlink or non-directory component "
                        f"{part!r} of {path}"
                    ) from error
                raise
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def write_json_exclusive(
    path: Path, payload: dict[str, Any], *, overwrite: bool = False
) -> None:
    """Write private JSON with owner-only permissions, never silently replacing labels.

    The payload is written to a fresh owner-only temporary file in the held
    parent directory, then published with ``link`` (fail if the destination
    exists) or ``replace`` (``overwrite=True``). No path component is followed
    through a symlink, and neither a symlink nor a hard link at the destination
    can redirect the write or truncate another file's content.
    """
    directory_descriptor = _open_parent_directory(path, create=True)
    temporary_name = f".{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp"
    temporary_descriptor = -1
    temporary_created = False
    temporary_removed = False
    try:
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_created = True
        os.fchmod(temporary_descriptor, 0o600)
        with os.fdopen(temporary_descriptor, "w", encoding="utf-8") as handle:
            temporary_descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        try:
            existing = os.lstat(path.name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise LabelledEvaluationError(
                    f"{path} is a symbolic link; refusing to write private sample data"
                )
            if not overwrite:
                raise LabelledEvaluationError(
                    f"{path} already exists; refusing to overwrite a sample or its human "
                    "labels (pass --force to replace it)"
                )
        if overwrite:
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            temporary_removed = True
        else:
            try:
                os.link(
                    temporary_name,
                    path.name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
            except FileExistsError as error:
                raise LabelledEvaluationError(
                    f"{path} already exists; refusing to overwrite a sample or its human "
                    "labels (pass --force to replace it)"
                ) from error
            os.unlink(temporary_name, dir_fd=directory_descriptor)
            temporary_removed = True
    finally:
        try:
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
            # Only a file this call created may be removed; a random-name
            # collision must not delete someone else's file.
            if temporary_created and not temporary_removed:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory_descriptor)


def run_build_sample(
    path: Path,
    *,
    per_stratum: int,
    seed: str,
    overwrite: bool = False,
    scan_limit: int = 5000,
) -> dict[str, Any]:
    destination = resolve_private_destination(path)
    engine = get_engine()
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("select id from job_seeker_profiles order by id limit 1")
        ).scalar_one_or_none()
        if row is None:
            raise LabelledEvaluationError("no profile found")
        sample = build_label_sample(
            connection,
            profile_id=int(row),
            per_stratum=per_stratum,
            seed=seed,
            scan_limit=scan_limit,
        )
    write_json_exclusive(destination, sample, overwrite=overwrite)
    return {
        "path": str(destination),
        "per_stratum_counts": sample["per_stratum_counts"],
        "scan": sample["scan"],
        "total": len(sample["labels"]),
    }


def run_labelled_evaluation(path: Path) -> dict[str, Any]:
    labels, sample_method = load_labels(path)
    engine = get_engine()
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("select id from job_seeker_profiles order by id limit 1")
        ).scalar_one_or_none()
        if row is None:
            raise LabelledEvaluationError("no profile found")
        states = load_stage_states(
            connection,
            profile_id=int(row),
            job_ids=sorted({label.job_id for label in labels}),
        )
    return evaluate_labels(labels, states, sample_method=sample_method)


def resolve_labels_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    settings = get_settings()
    configured = getattr(settings, "labelled_evaluation_path", "")
    if configured:
        return Path(configured)
    raise LabelledEvaluationError(
        "no labelled evaluation file configured; pass --labels PATH or set LABELLED_EVALUATION_PATH"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report per-stage recall/precision from a private labelled sample."
    )
    parser.add_argument(
        "--labels", default=None, help="Path to the private labels JSON."
    )
    parser.add_argument(
        "--build-sample",
        default=None,
        help="Write an unlabelled private sample template across strata to this path.",
    )
    parser.add_argument("--per-stratum", type=int, default=50)
    parser.add_argument("--seed", default="job-search-agent")
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=5000,
        help="Maximum source-backed jobs without a row to classify by gate outcome.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing sample file instead of refusing to overwrite it.",
    )
    args = parser.parse_args()
    if args.build_sample:
        summary = run_build_sample(
            Path(args.build_sample),
            per_stratum=args.per_stratum,
            seed=args.seed,
            overwrite=args.force,
            scan_limit=args.scan_limit,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    report = run_labelled_evaluation(resolve_labels_path(args.labels))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
