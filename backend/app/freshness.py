"""Profile-specific recommendation freshness policy.

Freshness controls *recommendation eligibility*, never catalogue lifecycle: an
old but still-open job stays in the catalogue while being excluded from a
profile's recommendations. Source last-seen is never publication time.

One UTC ``as_of`` is captured per run and carried through scoring, stored
evidence and API reads so a job cannot appear fresh in one stage and stale in
another. Date-only deadlines are interpreted in Europe/Helsinki through the end
of the stated day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

HELSINKI = ZoneInfo("Europe/Helsinki")

# A publication timestamp slightly ahead of `as_of` (clock skew) is not a real
# future listing.
FUTURE_TOLERANCE = timedelta(hours=6)

REASON_FRESH = "fresh"
REASON_TOO_OLD = "too_old"
REASON_NO_PUBLICATION_DATE = "no_publication_date"
REASON_FUTURE_PUBLICATION_DATE = "future_publication_date"
REASON_MALFORMED_PUBLICATION_DATE = "malformed_publication_date"
REASON_PAST_DEADLINE = "past_deadline"
REASON_UNKNOWN_DEADLINE = "unknown_deadline"


@dataclass(frozen=True)
class FreshnessPolicy:
    max_age_days: int | None
    drop_past_deadline: bool


@dataclass(frozen=True)
class FreshnessEvaluation:
    eligible: bool
    reason_code: str
    publication_source: str
    as_of: datetime
    effective_date: datetime | None
    age_days: float | None
    deadline_passed: bool | None
    requires_review: bool
    policy_revision: str

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            "publication_source": self.publication_source,
            "as_of": self.as_of.isoformat(),
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "age_days": self.age_days,
            "deadline_passed": self.deadline_passed,
            "requires_review": self.requires_review,
            "policy_revision": self.policy_revision,
        }


def load_freshness_policy(profile: dict[str, Any]) -> FreshnessPolicy:
    freshness = profile.get("freshness") if isinstance(profile, dict) else None
    if freshness is None:
        return FreshnessPolicy(max_age_days=None, drop_past_deadline=False)
    if not isinstance(freshness, dict):
        raise ValueError("profile.freshness must be a mapping")
    raw_age = freshness.get("max_age_days")
    if raw_age is None:
        max_age_days: int | None = None
    elif isinstance(raw_age, bool) or not isinstance(raw_age, int):
        raise ValueError("profile.freshness.max_age_days must be an integer or null")
    elif raw_age < 0:
        raise ValueError("profile.freshness.max_age_days must not be negative")
    else:
        max_age_days = raw_age
    drop_raw = freshness.get("drop_past_deadline", False)
    if not isinstance(drop_raw, bool):
        raise ValueError("profile.freshness.drop_past_deadline must be a boolean")
    return FreshnessPolicy(max_age_days=max_age_days, drop_past_deadline=drop_raw)


def policy_revision(policy: FreshnessPolicy) -> str:
    return f"max_age_days={policy.max_age_days};drop_past_deadline={int(policy.drop_past_deadline)}"


def capture_as_of(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _coerce_datetime(
    value: Any, *, date_only_end_of_day: bool = False
) -> tuple[datetime | None, bool, bool]:
    """Return (aware UTC datetime, was_date_only, malformed).

    A date-only value means a Helsinki calendar day. For publication that is the
    start of the day (conservative age); for a deadline it is the end of the
    stated day (inclusive).
    """

    def from_date(day: date) -> datetime:
        base = datetime.combine(day, time.min, tzinfo=HELSINKI)
        if date_only_end_of_day:
            base += timedelta(days=1)
        return base.astimezone(timezone.utc)

    if value is None:
        return None, False, False
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=HELSINKI).astimezone(timezone.utc), False, False
        return value.astimezone(timezone.utc), False, False
    if isinstance(value, date):
        return from_date(value), True, False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, False, False
        # A bare calendar date has no time component; `datetime.fromisoformat`
        # would otherwise parse it as naive midnight and hide the date-only case.
        if len(text) == 10 and text[4] == "-" and text[7] == "-" and "T" not in text:
            try:
                return from_date(date.fromisoformat(text)), True, False
            except ValueError:
                return None, False, True
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None, False, True
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=HELSINKI).astimezone(timezone.utc), False, False
        return parsed.astimezone(timezone.utc), False, False
    return None, False, True


def _deadline_end_of_day(value: Any) -> tuple[datetime | None, bool, bool]:
    """Deadlines are inclusive through the end of a date-only day."""
    return _coerce_datetime(value, date_only_end_of_day=True)


def evaluate_freshness(
    *,
    published_at: Any,
    first_seen_at: Any,
    expires_at: Any,
    policy: FreshnessPolicy,
    as_of: datetime,
) -> FreshnessEvaluation:
    as_of = capture_as_of(as_of)
    revision = policy_revision(policy)
    published, _published_date_only, published_malformed = _coerce_datetime(published_at)
    first_seen, _first_seen_date_only, first_seen_malformed = _coerce_datetime(first_seen_at)
    deadline, _deadline_date_only, deadline_malformed = _deadline_end_of_day(expires_at)

    requires_review = False
    if published is not None:
        effective = published
        source = "published_at"
    elif first_seen is not None:
        effective = first_seen
        source = "first_seen"
    else:
        effective = None
        source = "unknown"

    if published_malformed and published_at is not None:
        requires_review = True
    if effective is None and (published_at is not None or first_seen_at is not None):
        requires_review = True
    if first_seen_malformed and first_seen_at is not None:
        requires_review = True

    age_days: float | None = None
    if effective is not None:
        age_days = (as_of - effective).total_seconds() / 86400.0

    reason = REASON_FRESH
    eligible = True
    if effective is None:
        reason = REASON_NO_PUBLICATION_DATE
        requires_review = True
    elif effective > as_of + FUTURE_TOLERANCE:
        reason = REASON_FUTURE_PUBLICATION_DATE
        requires_review = True
    elif policy.max_age_days is not None and age_days is not None:
        if age_days > policy.max_age_days:
            eligible = False
            reason = REASON_TOO_OLD

    deadline_passed: bool | None = None
    if policy.drop_past_deadline and expires_at is not None:
        if deadline is None:
            deadline_passed = None
            requires_review = True
            if eligible:
                reason = REASON_UNKNOWN_DEADLINE
        else:
            deadline_passed = deadline < as_of
            if deadline_passed:
                eligible = False
                reason = REASON_PAST_DEADLINE
    elif deadline_malformed and expires_at is not None:
        requires_review = True

    return FreshnessEvaluation(
        eligible=eligible,
        reason_code=reason,
        publication_source=source,
        as_of=as_of,
        effective_date=effective,
        age_days=age_days,
        deadline_passed=deadline_passed,
        requires_review=requires_review,
        policy_revision=revision,
    )


def freshness_sql_clause(
    policy: FreshnessPolicy,
    *,
    as_of: datetime,
    published_expression: str = "coalesce(j.published_at, j.created_at)",
) -> tuple[str, dict[str, Any]]:
    """SQL mirror of the age/deadline gate for API reads between nightly runs.

    Unknown or future publications are kept (the catalogue may still be valid)
    but never treated as newest; only a proven age breach or passed deadline
    filters a row out.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if policy.max_age_days is not None:
        cutoff = capture_as_of(as_of) - timedelta(days=policy.max_age_days)
        clauses.append(f"({published_expression} is null or {published_expression} >= :freshness_min_published)")
        params["freshness_min_published"] = cutoff
    if policy.drop_past_deadline:
        clauses.append("(j.expires_at is null or j.expires_at >= :freshness_as_of)")
        params["freshness_as_of"] = capture_as_of(as_of)
    return " and ".join(clauses), params
