from datetime import date, datetime, timedelta, timezone

import pytest

from app.freshness import (
    REASON_FUTURE_PUBLICATION_DATE,
    REASON_NO_PUBLICATION_DATE,
    REASON_PAST_DEADLINE,
    REASON_TOO_OLD,
    FreshnessPolicy,
    capture_as_of,
    evaluate_freshness,
    freshness_sql_clause,
    load_freshness_policy,
    policy_revision,
)

AS_OF = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _evaluate(policy, **kwargs):
    return evaluate_freshness(policy=policy, as_of=AS_OF, **kwargs)


def test_exactly_max_age_is_still_fresh() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=False)
    result = _evaluate(
        policy,
        published_at=AS_OF - timedelta(days=30),
        first_seen_at=None,
        expires_at=None,
    )

    assert result.eligible is True
    assert result.reason_code == "fresh"
    assert result.age_days == pytest.approx(30.0)


def test_just_older_than_max_age_is_rejected() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=False)
    result = _evaluate(
        policy,
        published_at=AS_OF - timedelta(days=30, seconds=1),
        first_seen_at=None,
        expires_at=None,
    )

    assert result.eligible is False
    assert result.reason_code == REASON_TOO_OLD


def test_no_age_policy_keeps_old_jobs_eligible() -> None:
    policy = FreshnessPolicy(max_age_days=None, drop_past_deadline=False)
    result = _evaluate(
        policy,
        published_at=AS_OF - timedelta(days=5000),
        first_seen_at=None,
        expires_at=None,
    )

    assert result.eligible is True


def test_first_seen_is_tagged_fallback_when_publication_missing() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=False)
    result = _evaluate(
        policy,
        published_at=None,
        first_seen_at=AS_OF - timedelta(days=2),
        expires_at=None,
    )

    assert result.eligible is True
    assert result.publication_source == "first_seen"


def test_first_seen_fallback_still_ages_out() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=False)
    result = _evaluate(
        policy,
        published_at=None,
        first_seen_at=AS_OF - timedelta(days=100),
        expires_at=None,
    )

    assert result.eligible is False
    assert result.reason_code == REASON_TOO_OLD


def test_missing_publication_and_first_seen_is_flagged_for_review() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=False)
    result = _evaluate(policy, published_at=None, first_seen_at=None, expires_at=None)

    assert result.reason_code == REASON_NO_PUBLICATION_DATE
    assert result.requires_review is True


def test_future_publication_is_flagged_not_treated_as_newest() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=False)
    result = _evaluate(
        policy,
        published_at=AS_OF + timedelta(days=3),
        first_seen_at=None,
        expires_at=None,
    )

    assert result.reason_code == REASON_FUTURE_PUBLICATION_DATE
    assert result.requires_review is True


def test_malformed_publication_date_is_flagged_for_review() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=False)
    result = _evaluate(
        policy,
        published_at="not-a-date",
        first_seen_at=None,
        expires_at=None,
    )

    assert result.requires_review is True


def test_past_deadline_is_rejected_when_enforced() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=True)
    result = _evaluate(
        policy,
        published_at=AS_OF - timedelta(days=1),
        first_seen_at=None,
        expires_at=AS_OF - timedelta(hours=1),
    )

    assert result.eligible is False
    assert result.reason_code == REASON_PAST_DEADLINE
    assert result.deadline_passed is True


def test_future_deadline_keeps_job_eligible() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=True)
    result = _evaluate(
        policy,
        published_at=AS_OF - timedelta(days=1),
        first_seen_at=None,
        expires_at=AS_OF + timedelta(days=5),
    )

    assert result.eligible is True
    assert result.deadline_passed is False


def test_past_deadline_is_ignored_when_policy_disables_it() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=False)
    result = _evaluate(
        policy,
        published_at=AS_OF - timedelta(days=1),
        first_seen_at=None,
        expires_at=AS_OF - timedelta(days=10),
    )

    assert result.eligible is True
    assert result.deadline_passed is None


def test_date_only_deadline_is_inclusive_through_helsinki_end_of_day() -> None:
    # 2026-09-16 is the AS_OF calendar day in Helsinki (UTC+3); end of day is
    # 2026-09-16T21:00Z, after the 12:00Z as_of.
    policy = FreshnessPolicy(max_age_days=None, drop_past_deadline=True)
    result = _evaluate(
        policy,
        published_at=None,
        first_seen_at=AS_OF - timedelta(days=1),
        expires_at="2026-09-16",
    )

    assert result.eligible is True
    assert result.deadline_passed is False


def test_date_only_deadline_yesterday_is_past() -> None:
    policy = FreshnessPolicy(max_age_days=None, drop_past_deadline=True)
    result = _evaluate(
        policy,
        published_at=None,
        first_seen_at=AS_OF - timedelta(days=1),
        expires_at="2026-09-15",
    )

    assert result.eligible is False
    assert result.reason_code == REASON_PAST_DEADLINE


def test_naive_datetime_is_interpreted_as_helsinki_local() -> None:
    policy = FreshnessPolicy(max_age_days=30, drop_past_deadline=False)
    result = _evaluate(
        policy,
        published_at=datetime(2026, 9, 15, 12, 0),
        first_seen_at=None,
        expires_at=None,
    )

    # 2026-09-15T12:00 Helsinki == 09:00Z, i.e. 27 hours before AS_OF.
    assert result.age_days == pytest.approx(27 / 24, abs=1e-6)


def test_load_freshness_policy_defaults_and_validation() -> None:
    assert load_freshness_policy({}) == FreshnessPolicy(None, False)
    assert load_freshness_policy({"freshness": {"max_age_days": 30, "drop_past_deadline": True}}) == (
        FreshnessPolicy(30, True)
    )
    with pytest.raises(ValueError):
        load_freshness_policy({"freshness": {"max_age_days": "30"}})
    with pytest.raises(ValueError):
        load_freshness_policy({"freshness": {"max_age_days": -1}})
    with pytest.raises(ValueError):
        load_freshness_policy({"freshness": {"drop_past_deadline": "yes"}})


def test_policy_revision_is_readable_and_stable() -> None:
    policy = FreshnessPolicy(30, True)

    assert policy_revision(policy) == "max_age_days=30;drop_past_deadline=1"
    assert policy_revision(policy) == policy_revision(FreshnessPolicy(30, True))


def test_freshness_sql_clause_matches_policy_shape() -> None:
    clause, params = freshness_sql_clause(FreshnessPolicy(30, True), as_of=AS_OF)

    assert "freshness_min_published" in clause
    assert "expires_at >= :freshness_as_of" in clause
    assert params["freshness_min_published"] == AS_OF - timedelta(days=30)
    assert params["freshness_as_of"] == AS_OF

    empty_clause, empty_params = freshness_sql_clause(FreshnessPolicy(None, False), as_of=AS_OF)
    assert empty_clause == ""
    assert empty_params == {}


def test_capture_as_of_normalizes_naive_values_to_utc() -> None:
    naive = datetime(2026, 9, 16, 12, 0)

    assert capture_as_of(naive).tzinfo is timezone.utc
    assert capture_as_of(naive).hour == 12
