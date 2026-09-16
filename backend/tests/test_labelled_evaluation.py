import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import create_engine, text as sa_text

from app.labelled_evaluation import (
    Label,
    build_label_sample,
    LabelledEvaluationError,
    StageState,
    evaluate_labels,
    load_labels,
    wilson_interval,
)


def _state(
    job_id, *, row=True, eligible=True, reviewed=True, published=True, accepted=False
):
    return StageState(
        job_id=job_id,
        recommendation_row=row,
        hard_eligible=eligible,
        llm_reviewed=reviewed,
        published=published,
        accepted=accepted,
    )


def test_wilson_interval_bounds() -> None:
    assert wilson_interval(0, 0) is None
    low, high = wilson_interval(5, 10)  # type: ignore[misc]
    assert 0.0 <= low < 0.5 < high <= 1.0
    assert wilson_interval(0, 10) == (0.0, 0.278)  # type: ignore[comparison-overlap]


def test_evaluate_labels_counts_overlapping_strata_once() -> None:
    # A published, accepted job that also appears in another stratum must not be
    # counted twice in overall recall or its Wilson denominators.
    labels = [
        Label(1, "oulu_local", True),
        Label(1, "accepted", True),
        Label(2, "nationwide", True),
    ]
    states = {
        1: _state(1, reviewed=True, published=True, accepted=True),
        2: _state(2, reviewed=True, published=False),
    }

    report = evaluate_labels(labels, states)

    assert report["labels"] == 3
    assert report["unique_jobs"] == 2
    published = report["overall"]["stages"]["published"]
    assert published["numerator"] == 1
    assert published["denominator"] == 2
    assert report["overall"]["stages"]["accepted"]["denominator"] == 2
    # Per-stratum membership is retained for both strata.
    assert set(report["by_stratum"]) == {"oulu_local", "accepted", "nationwide"}
    assert report["by_stratum"]["accepted"]["stages"]["accepted"]["numerator"] == 1


def test_evaluate_labels_rejects_conflicting_relevance_labels() -> None:
    labels = [Label(1, "oulu_local", True), Label(1, "unreviewed", False)]
    with pytest.raises(LabelledEvaluationError, match="both relevant and not relevant"):
        evaluate_labels(labels, {1: _state(1)})


def test_evaluate_labels_reports_stage_denominators_and_precision() -> None:
    labels = [
        Label(1, "oulu_local", True),
        Label(2, "oulu_local", True),
        Label(3, "nationwide", True),
        Label(4, "nationwide", False),
    ]
    states = {
        1: _state(1, reviewed=True, published=True, accepted=True),
        2: _state(2, reviewed=False, published=False),
        3: _state(3, reviewed=True, published=True),
        4: _state(4, reviewed=True, published=True),
    }

    report = evaluate_labels(labels, states, sample_method="synthetic")

    assert report["overall"]["relevant"] == 3
    published = report["overall"]["stages"]["published"]
    assert published["numerator"] == 2
    assert published["denominator"] == 3
    assert published["value"] == 0.667
    assert report["overall"]["published_precision"] == {
        "numerator": 2,
        "denominator": 3,
        "value": 0.667,
        "wilson_95": report["overall"]["published_precision"]["wilson_95"],
    }
    assert set(report["by_stratum"]) == {"nationwide", "oulu_local"}
    assert report["sample_method"] == "synthetic"
    json.dumps(report)


def test_load_labels_rejects_missing_or_malformed(tmp_path) -> None:
    missing = tmp_path / "labels.json"
    with pytest.raises(LabelledEvaluationError):
        load_labels(missing)

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"labels": [{"job_id": "x", "relevant": True}]}))
    with pytest.raises(LabelledEvaluationError):
        load_labels(bad)

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"labels": []}))
    with pytest.raises(LabelledEvaluationError):
        load_labels(empty)


def test_load_labels_reads_sample_method(tmp_path) -> None:
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            {
                "sample_method": "stratified random",
                "labels": [{"job_id": 1, "stratum": "local", "relevant": True}],
            }
        )
    )

    labels, method = load_labels(path)

    assert labels == [Label(1, "local", True)]
    assert method == "stratified random"


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pg_only = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set; PostgreSQL integration tests skipped",
)


SAMPLE_PROFILE = {
    "role_clusters": [{"titles_fi": ["kirjastonhoitaja"], "keywords_fi": ["kirjasto"]}],
    "location": {"home_city": "Oulu"},
    "freshness": {"max_age_days": 30, "drop_past_deadline": True},
    "exclusions": {
        "qualification_checks": {
            "kirjastonhoitaja": {
                "reject_when_phrases_present": ["vaaditaan kelpoisuus"]
            }
        }
    },
}


@pytest.fixture()
def pg_engine() -> Iterator[Any]:
    engine = create_engine(str(TEST_DATABASE_URL))
    with engine.begin() as connection:
        connection.execute(
            sa_text(
                "truncate table recommendation_feedback, llm_evaluations, recommendations, "
                "job_sources, raw_listings, jobs, sources, job_seeker_profiles "
                "restart identity cascade"
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_profile(connection: Any) -> int:
    from support import with_privacy

    return int(
        connection.execute(
            sa_text(
                "insert into job_seeker_profiles (name, profile) "
                "values ('sample', CAST(:profile AS jsonb)) returning id"
            ),
            {"profile": json.dumps(with_privacy(dict(SAMPLE_PROFILE)))},
        ).scalar_one()
    )


def _sample_job(
    connection: Any, *, index: int, title: str, description: str, location: str = "Oulu"
) -> int:
    source_id = int(
        connection.execute(
            sa_text(
                "insert into sources (name, method, poll_interval_min, enabled) "
                "values (:name, 'api', 60, true) returning id"
            ),
            {"name": f"sample-{index}"},
        ).scalar_one()
    )
    job_id = int(
        connection.execute(
            sa_text(
                "insert into jobs (title, employer, description, status, location, published_at) "
                "values (:title, 'Testityönantaja', :description, 'active', :location, now()) "
                "returning id"
            ),
            {"title": title, "description": description, "location": location},
        ).scalar_one()
    )
    listing_id = int(
        connection.execute(
            sa_text(
                "insert into raw_listings (source_id, external_id, canonical_source_url, "
                "content_hash, payload) values (:source_id, :external_id, :url, 'hash', '{}'::jsonb) "
                "returning id"
            ),
            {
                "source_id": source_id,
                "external_id": f"ext-{index}",
                "url": f"https://example.invalid/job/{index}",
            },
        ).scalar_one()
    )
    connection.execute(
        sa_text(
            "insert into job_sources (job_id, source_id, raw_listing_id, external_id, "
            "last_content_hash) values (:job_id, :source_id, :listing_id, :external_id, 'hash')"
        ),
        {
            "job_id": job_id,
            "source_id": source_id,
            "listing_id": listing_id,
            "external_id": f"ext-{index}",
        },
    )
    return job_id


def _travel_params() -> dict[str, Any]:
    from app.main import current_travel_policy_params

    return current_travel_policy_params(dict(SAMPLE_PROFILE))


def _sample_recommendation(
    connection: Any,
    *,
    job_id: int,
    profile_id: int,
    is_active: bool = True,
    commutable: bool = True,
    full_remote: bool = False,
    commutable_or_full_remote: bool | None = None,
    hard_eligible: bool | None = None,
    evaluation_id: int | None = None,
    commute_limit_minutes: int | None = None,
) -> int:
    deterministic = {"passes": True}
    if hard_eligible is not None:
        deterministic["hard_eligible"] = hard_eligible
    travel = _travel_params()
    return int(
        connection.execute(
            sa_text(
                "insert into recommendations (job_id, profile_id, is_active, commutable, "
                "commutable_or_full_remote, full_remote, machine_score, llm_evaluation_id, "
                "travel_reason_code, travel_origin_address, travel_commute_limit_minutes, "
                "travel_routing_profile, deterministic_result) values (:job_id, :profile_id, "
                ":is_active, :commutable, :commutable_or_full_remote, :full_remote, 55.0, "
                ":evaluation_id, 'transit_within_limit', :travel_origin_address, "
                ":travel_commute_limit_minutes, :travel_routing_profile, "
                "CAST(:deterministic AS jsonb)) returning id"
            ),
            {
                "job_id": job_id,
                "profile_id": profile_id,
                "is_active": is_active,
                "commutable": commutable,
                "commutable_or_full_remote": (
                    commutable or full_remote
                    if commutable_or_full_remote is None
                    else commutable_or_full_remote
                ),
                "full_remote": full_remote,
                "evaluation_id": evaluation_id,
                "travel_origin_address": travel["travel_origin_address"],
                "travel_commute_limit_minutes": (
                    travel["travel_commute_limit_minutes"]
                    if commute_limit_minutes is None
                    else commute_limit_minutes
                ),
                "travel_routing_profile": travel["travel_routing_profile"],
                "deterministic": json.dumps(deterministic),
            },
        ).scalar_one()
    )


def _stratum_ids(sample: dict[str, Any], stratum: str) -> list[int]:
    return sorted(
        entry["job_id"] for entry in sample["labels"] if entry["stratum"] == stratum
    )


@pg_only
def test_build_label_sample_classifies_strata_exactly(pg_engine: Any) -> None:
    """Missing rows are split by the real gates so rejects cannot inflate recall."""
    with pg_engine.begin() as connection:
        profile_id = _seed_profile(connection)
        local_job = _sample_job(
            connection,
            index=1,
            title="Kirjastonhoitaja",
            description="Kirjasto ja asiakaspalvelu.",
        )
        remote_job = _sample_job(
            connection,
            index=2,
            title="Kirjastonhoitaja",
            description="Kirjasto etätyönä.",
        )
        distant_job = _sample_job(
            connection,
            index=3,
            title="Kirjastonhoitaja",
            description="Kirjasto kaukana.",
        )
        hard_row_job = _sample_job(
            connection, index=4, title="Kirjastonhoitaja", description="Kirjasto."
        )
        unreviewed_job = _sample_job(
            connection, index=5, title="Kirjastonhoitaja", description="Kirjasto."
        )
        missing_pass_job = _sample_job(
            connection,
            index=6,
            title="Kirjastonhoitaja",
            description="Kirjasto ja asiakaspalvelu.",
        )
        missing_hard_job = _sample_job(
            connection,
            index=7,
            title="Kirjastonhoitaja",
            description="Kirjasto. Vaaditaan kelpoisuus tehtävään.",
        )
        missing_reject_job = _sample_job(
            connection,
            index=8,
            title="Myyntipäällikkö",
            description="Myyntiä ja ravintolaa.",
            location="Rovaniemi",
        )
        stale_policy_job = _sample_job(
            connection, index=9, title="Kirjastonhoitaja", description="Kirjasto."
        )
        review_ids = {
            job_id: int(
                connection.execute(
                    sa_text(
                        "insert into llm_evaluations (job_id, profile_id, provider, "
                        "configured_model, prompt_version, schema_version, request_hash, response) "
                        "values (:job_id, :profile_id, 'test', 'test-model', 1, 1, :hash, "
                        "'{}'::jsonb) returning id"
                    ),
                    {
                        "job_id": job_id,
                        "profile_id": profile_id,
                        "hash": f"hash-{job_id}",
                    },
                ).scalar_one()
            )
            for job_id in (local_job, remote_job, distant_job)
        }
        _sample_recommendation(
            connection,
            job_id=local_job,
            profile_id=profile_id,
            commutable=True,
            evaluation_id=review_ids[local_job],
        )
        _sample_recommendation(
            connection,
            job_id=remote_job,
            profile_id=profile_id,
            commutable=False,
            full_remote=True,
            evaluation_id=review_ids[remote_job],
        )
        _sample_recommendation(
            connection,
            job_id=distant_job,
            profile_id=profile_id,
            commutable=False,
            evaluation_id=review_ids[distant_job],
        )
        _sample_recommendation(
            connection,
            job_id=hard_row_job,
            profile_id=profile_id,
            hard_eligible=False,
        )
        _sample_recommendation(
            connection, job_id=unreviewed_job, profile_id=profile_id, is_active=False
        )
        _sample_recommendation(
            connection,
            job_id=stale_policy_job,
            profile_id=profile_id,
            commutable=True,
            commute_limit_minutes=int(_travel_params()["travel_commute_limit_minutes"])
            + 30,
        )
        sample = build_label_sample(
            connection, profile_id=profile_id, per_stratum=10, prompt_version=1
        )

    assert _stratum_ids(sample, "oulu_local") == [local_job]
    assert _stratum_ids(sample, "remote") == [remote_job]
    assert _stratum_ids(sample, "nationwide") == [distant_job]
    assert _stratum_ids(sample, "hard_rejection") == sorted(
        [hard_row_job, missing_hard_job]
    )
    assert _stratum_ids(sample, "unreviewed") == sorted(
        [unreviewed_job, stale_policy_job]
    )
    assert _stratum_ids(sample, "not_retrieved") == [missing_pass_job]
    labelled_ids = {entry["job_id"] for entry in sample["labels"]}
    assert missing_reject_job not in labelled_ids
    # A stale travel-policy assessment must not enter the scope the user sees.
    assert stale_policy_job not in _stratum_ids(sample, "oulu_local")
    assert sample["scan"]["deterministic_rejection"] >= 1
    assert sample["scan"]["scanned"] == 3
    assert sample["stratum_definitions"]["not_retrieved"]
    assert all(entry["relevant"] is None for entry in sample["labels"])
    assert len(
        {(entry["job_id"], entry["stratum"]) for entry in sample["labels"]}
    ) == len(sample["labels"])
    json.dumps(sample)


@pg_only
def test_hard_rejection_budget_is_shared_between_row_and_computed_rejects(
    pg_engine: Any,
) -> None:
    with pg_engine.begin() as connection:
        profile_id = _seed_profile(connection)
        persisted_job = _sample_job(
            connection, index=1, title="Kirjastonhoitaja", description="Kirjasto."
        )
        computed_job = _sample_job(
            connection,
            index=2,
            title="Kirjastonhoitaja",
            description="Kirjasto. Vaaditaan kelpoisuus tehtävään.",
        )
        _sample_recommendation(
            connection, job_id=persisted_job, profile_id=profile_id, hard_eligible=False
        )
        sample = build_label_sample(connection, profile_id=profile_id, per_stratum=1)
    assert _stratum_ids(sample, "hard_rejection") == [persisted_job]
    assert computed_job not in _stratum_ids(sample, "hard_rejection")
    assert sample["per_stratum_counts"]["hard_rejection"] == 1


@pg_only
def test_scan_stops_once_requested_buckets_are_full(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = _seed_profile(connection)
        persisted_job = _sample_job(
            connection, index=1, title="Kirjastonhoitaja", description="Kirjasto."
        )
        _sample_recommendation(
            connection, job_id=persisted_job, profile_id=profile_id, hard_eligible=False
        )
        for index in range(2, 10):
            _sample_job(
                connection,
                index=index,
                title="Kirjastonhoitaja",
                description="Kirjasto ja asiakaspalvelu.",
            )
        sample = build_label_sample(connection, profile_id=profile_id, per_stratum=1)
    assert sample["per_stratum_counts"]["hard_rejection"] == 1
    assert sample["per_stratum_counts"]["not_retrieved"] == 1
    # The persisted rejection fills its budget, so one scorable row is enough.
    assert sample["scan"]["scanned"] == 1
    assert sample["scan"]["scan_limit_reached"] is False


@pg_only
def test_accepted_stratum_requires_positive_feedback(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = _seed_profile(connection)
        accepted_job = _sample_job(
            connection, index=1, title="Kirjastonhoitaja", description="Kirjasto."
        )
        ignored_job = _sample_job(
            connection, index=2, title="Kirjastonhoitaja", description="Kirjasto."
        )
        accepted_id = _sample_recommendation(
            connection, job_id=accepted_job, profile_id=profile_id
        )
        ignored_id = _sample_recommendation(
            connection, job_id=ignored_job, profile_id=profile_id
        )
        connection.execute(
            sa_text(
                "insert into recommendation_feedback (recommendation_id, job_id, action, rating) "
                "values (:recommendation_id, :job_id, 'good_match', 5)"
            ),
            {"recommendation_id": accepted_id, "job_id": accepted_job},
        )
        connection.execute(
            sa_text(
                "insert into recommendation_feedback (recommendation_id, job_id, action, rating) "
                "values (:recommendation_id, :job_id, 'not_relevant', 1)"
            ),
            {"recommendation_id": ignored_id, "job_id": ignored_job},
        )
        sample = build_label_sample(connection, profile_id=profile_id, per_stratum=5)
    assert _stratum_ids(sample, "accepted") == [accepted_job]


@pg_only
def test_unreviewed_stratum_includes_stale_prompt_version(pg_engine: Any) -> None:
    with pg_engine.begin() as connection:
        profile_id = _seed_profile(connection)
        stale_job = _sample_job(
            connection, index=1, title="Kirjastonhoitaja", description="Kirjasto."
        )
        current_job = _sample_job(
            connection, index=2, title="Kirjastonhoitaja", description="Kirjasto."
        )
        evaluation_id = int(
            connection.execute(
                sa_text(
                    "insert into llm_evaluations (job_id, profile_id, provider, configured_model, "
                    "prompt_version, schema_version, request_hash, response) values (:job_id, "
                    ":profile_id, 'test', 'test-model', 1, 1, 'hash', '{}'::jsonb) returning id"
                ),
                {"job_id": current_job, "profile_id": profile_id},
            ).scalar_one()
        )
        stale_evaluation_id = int(
            connection.execute(
                sa_text(
                    "insert into llm_evaluations (job_id, profile_id, provider, configured_model, "
                    "prompt_version, schema_version, request_hash, response) values (:job_id, "
                    ":profile_id, 'test', 'test-model', 0, 1, 'hash-stale', '{}'::jsonb) "
                    "returning id"
                ),
                {"job_id": stale_job, "profile_id": profile_id},
            ).scalar_one()
        )
        _sample_recommendation(
            connection,
            job_id=stale_job,
            profile_id=profile_id,
            is_active=False,
            evaluation_id=stale_evaluation_id,
        )
        _sample_recommendation(
            connection,
            job_id=current_job,
            profile_id=profile_id,
            is_active=False,
            evaluation_id=evaluation_id,
        )
        sample = build_label_sample(
            connection, profile_id=profile_id, per_stratum=5, prompt_version=1
        )
    assert _stratum_ids(sample, "unreviewed") == [stale_job]


@pg_only
def test_build_label_sample_seeded_selection_is_order_independent(
    pg_engine: Any,
) -> None:
    with pg_engine.begin() as connection:
        profile_id = _seed_profile(connection)
        for index in range(1, 9):
            _sample_job(
                connection,
                index=index,
                title="Kirjastonhoitaja",
                description="Kirjasto ja asiakaspalvelu.",
            )
        first = build_label_sample(
            connection, profile_id=profile_id, per_stratum=3, seed="alpha"
        )
        second = build_label_sample(
            connection, profile_id=profile_id, per_stratum=3, seed="alpha"
        )
        other = build_label_sample(
            connection, profile_id=profile_id, per_stratum=3, seed="beta"
        )
    assert first == second
    assert _stratum_ids(first, "not_retrieved") != _stratum_ids(other, "not_retrieved")
    assert first["scan"]["scanned"] == 8
    assert first["scan"]["scan_limit_reached"] is False


@pg_only
def test_unlabelled_sample_template_is_rejected_by_evaluation(
    pg_engine: Any, tmp_path
) -> None:
    with pg_engine.begin() as connection:
        profile_id = _seed_profile(connection)
        _sample_job(
            connection, index=1, title="Kirjastonhoitaja", description="Kirjasto."
        )
        sample = build_label_sample(connection, profile_id=profile_id, per_stratum=1)
    path = tmp_path / "template.json"
    path.write_text(json.dumps(sample), encoding="utf-8")
    with pytest.raises(LabelledEvaluationError, match="unlabelled"):
        load_labels(path)


def test_resolve_private_destination_uses_detected_project_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.labelled_evaluation as labelled

    # The source checkout resolves to the repository root, not the app package.
    assert labelled._project_root() == Path(__file__).resolve().parents[2]

    # The container layout has no .git: the project file marks /app instead.
    fake_root = tmp_path / "app"
    (fake_root / "profile").mkdir(parents=True)
    monkeypatch.setattr(labelled, "_project_root", lambda: fake_root)
    allowed = labelled.resolve_private_destination(
        tmp_path / "elsewhere" / "labels.json"
    )
    assert allowed.name == "labels.json"
    assert (
        labelled.resolve_private_destination(fake_root / "profile" / "labels.json").name
        == "labels.json"
    )
    with pytest.raises(LabelledEvaluationError, match="outside"):
        labelled.resolve_private_destination(fake_root / "labels.json")

    # Without any marker only the profile/ rule applies, never the filesystem root.
    monkeypatch.setattr(labelled, "_project_root", lambda: None)
    assert (
        labelled.resolve_private_destination(tmp_path / "anywhere.json").name
        == "anywhere.json"
    )


def test_sample_writer_refuses_repository_and_overwrite(tmp_path) -> None:
    from app.labelled_evaluation import (
        resolve_private_destination,
        run_build_sample,
        write_json_exclusive,
    )

    repo_file = Path(__file__).resolve().parents[2] / "backend" / "sample.json"
    with pytest.raises(LabelledEvaluationError, match="outside"):
        resolve_private_destination(repo_file)

    target = tmp_path / "labels.json"
    write_json_exclusive(target, {"labels": []})
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(LabelledEvaluationError, match="already exists"):
        write_json_exclusive(target, {"labels": []})

    # --force must also tighten permissions on a pre-existing permissive file.
    target.chmod(0o644)
    write_json_exclusive(target, {"labels": ["replaced"]}, overwrite=True)
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text()) == {"labels": ["replaced"]}

    # A symlink planted at the destination is never followed, with or without --force.
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("keep me")
    link = tmp_path / "link.json"
    link.symlink_to(elsewhere)
    for overwrite in (False, True):
        with pytest.raises(LabelledEvaluationError, match="symbolic link"):
            write_json_exclusive(link, {"labels": []}, overwrite=overwrite)
    assert elsewhere.read_text() == "keep me"
    assert link.is_symlink()

    # An overwritten hard link must not truncate the other name for the same inode.
    shared = tmp_path / "shared.json"
    shared.write_text("shared content")
    hard = tmp_path / "hard.json"
    os.link(shared, hard)
    hard.chmod(0o644)
    write_json_exclusive(hard, {"labels": ["replaced"]}, overwrite=True)
    assert shared.read_text() == "shared content"
    assert json.loads(hard.read_text()) == {"labels": ["replaced"]}
    assert hard.stat().st_ino != shared.stat().st_ino
    assert hard.stat().st_mode & 0o777 == 0o600

    # A symlinked ancestor at any depth cannot redirect the write.
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    ancestor = tmp_path / "private"
    ancestor.symlink_to(outside, target_is_directory=True)
    unresolved = resolve_private_destination(ancestor / "nested" / "labels.json")
    assert unresolved.parent.parent.name == "private"
    with pytest.raises(
        LabelledEvaluationError, match="symlink or non-directory component"
    ):
        write_json_exclusive(unresolved, {"labels": []})
    assert list((outside / "nested").iterdir()) == []

    # `..` must not be collapsed away before the walk; crossing a symlink is refused.
    dotted = resolve_private_destination(ancestor / ".." / "labels.json")
    assert ".." in dotted.parts
    with pytest.raises(
        LabelledEvaluationError, match="symlink or non-directory component"
    ):
        write_json_exclusive(dotted, {"labels": []})

    # A regular file used as a directory component is reported, not followed.
    regular = tmp_path / "regular"
    regular.write_text("x")
    with pytest.raises(LabelledEvaluationError, match="non-directory component"):
        write_json_exclusive(regular / "labels.json", {"labels": []})

    # Missing parent directories are created safely.
    nested = tmp_path / "one" / "two" / "labels.json"
    write_json_exclusive(nested, {"labels": ["nested"]})
    assert json.loads(nested.read_text()) == {"labels": ["nested"]}

    assert callable(run_build_sample)


@pg_only
def test_run_build_sample_rejects_symlinked_ancestor(
    pg_engine: Any, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.labelled_evaluation as labelled

    with pg_engine.begin() as connection:
        _seed_profile(connection)
    monkeypatch.setattr(labelled, "get_engine", lambda: pg_engine)
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    ancestor = tmp_path / "profile"
    ancestor.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        LabelledEvaluationError, match="symlink or non-directory component"
    ):
        labelled.run_build_sample(
            ancestor / "nested" / "labelled-evaluation.json", per_stratum=1, seed="s"
        )
    assert list((outside / "nested").iterdir()) == []
