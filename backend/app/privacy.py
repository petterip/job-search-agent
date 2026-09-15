"""Outbound privacy boundary for hosted LLM and embedding calls.

The profile's ``privacy`` block is an enforceable policy, not documentation.
Supported *allow* directives map to explicit structured projections; unsupported
directives fail closed with an actionable local error. Forbidden fields win over
allowed parents at every depth, so a field can never leave the host just because
a broad parent was allowed.

Everything that crosses a hosted boundary (profile summary, job summary, travel
assessment, feedback snapshot/context, learned hints and examples, embedding
input) is built from these helpers and passed through :func:`sanitize_outbound`
last.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


class PrivacyConfigurationError(ValueError):
    """Raised when the profile privacy contract is missing or unsupported."""


# Human-readable directive -> structured projection keys. The private profile's
# `privacy.llm_allowed_fields` uses phrases, so they are mapped explicitly here
# instead of being interpreted as arbitrary YAML key subtraction.
DIRECTIVE_PROJECTIONS: dict[str, tuple[str, ...]] = {
    "objective": ("objective",),
    "location policy summary": ("location",),
    "languages": ("languages",),
    "career_evidence": ("career_evidence",),
    "role_clusters": ("role_clusters",),
    "skills": ("skills",),
    "strength_signals": ("strength_signals",),
    "preferences": ("preferences",),
    "exclusions": ("exclusions",),
    "freshness": ("freshness",),
    "llm_guidance": ("llm_guidance",),
    "availability": ("availability",),
}

# Directives that must never leave the host, mapped to normalized field tokens
# removed recursively from every outbound structure.
FORBIDDEN_DIRECTIVES: dict[str, frozenset[str]] = {
    "raw source files": frozenset(
        {
            "raw",
            "raw_cv",
            "cv",
            "cv_raw",
            "cv_text",
            "source_files",
            "raw_source_files",
            "raw_listings",
            "raw_payload",
            "source_text",
        }
    ),
    "extracted application letters with contact details": frozenset(
        {
            "application_letters",
            "applications",
            "cover_letters",
            "extracted_applications",
            "extracted",
            "application_history",
        }
    ),
    "home_postal": frozenset({"home_postal", "postal_code", "postcode", "zip", "postinumero"}),
    "address": frozenset(
        {
            "address",
            "home_address",
            "street_address",
            "origin_address",
            "travel_origin_address",
            "travel_origin",
            "osoite",
            "katuosoite",
            "formatted_address",
        }
    ),
    "phone": frozenset({"phone", "phone_number", "mobile", "puhelin", "puhelinnumero"}),
    "email": frozenset({"email", "email_address", "sahkoposti", "sähköposti"}),
    "birth date": frozenset(
        {"birth_date", "birthdate", "date_of_birth", "syntymaaika", "syntymäaika"}
    ),
    "ssn": frozenset(
        {"ssn", "social_security_number", "henkilotunnus", "hetu", "personal_identity_code"}
    ),
    "photo": frozenset({"photo", "profile_photo", "image", "kuva"}),
}

ALLOW_DIRECTIVES: frozenset[str] = frozenset(DIRECTIVE_PROJECTIONS)
FORBIDDEN_KEY_TOKENS: frozenset[str] = frozenset(
    token for tokens in FORBIDDEN_DIRECTIVES.values() for token in tokens
)

# Location keys safe to send. `home_postal` and address-like keys are excluded
# even before the recursive deny pass.
SAFE_LOCATION_KEYS: tuple[str, ...] = (
    "confidence",
    "home_city",
    "region_towns",
    "proven_willing_locations",
    "commute_radius_km",
    "work_mode_policy",
    "location_cautions",
    "anchors",
)

# Travel assessment keys safe to send. `origin_address` is deliberately absent:
# the routing address stays in server-side routing/cache/audit only.
SAFE_TRAVEL_KEYS: tuple[str, ...] = (
    "commutable",
    "full_remote",
    "commutable_or_full_remote",
    "status",
    "duration_seconds",
    "distance_km",
    "score_adjustment",
    "evidence_text",
    "tone",
    "reason_code",
    "commute_limit_minutes",
    "routing_profile",
)

_EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE_PATTERN = re.compile(r"(?:\+?\d[\d\s().-]{6,}\d)")


def _normalized_key(value: object) -> str:
    return re.sub(r"[\s-]+", "_", str(value).strip().casefold())


def is_forbidden_key(key: object) -> bool:
    return _normalized_key(key) in FORBIDDEN_KEY_TOKENS


def supported_directives() -> frozenset[str]:
    return ALLOW_DIRECTIVES


@dataclass(frozen=True)
class PrivacyPolicy:
    llm_allowed: bool
    allowed_directives: frozenset[str]
    forbidden_directives: frozenset[str]

    def allows(self, directive: str) -> bool:
        return directive in self.allowed_directives


def load_privacy_policy(profile: dict[str, Any]) -> PrivacyPolicy:
    """Validate and return the profile privacy policy.

    Fails closed: a missing or malformed privacy block raises instead of
    silently sending the whole profile.
    """
    privacy = profile.get("privacy") if isinstance(profile, dict) else None
    if not isinstance(privacy, dict):
        raise PrivacyConfigurationError(
            "profile.privacy is missing or not a mapping; refusing hosted calls"
        )
    llm_allowed = privacy.get("llm_allowed")
    if not isinstance(llm_allowed, bool):
        raise PrivacyConfigurationError(
            "profile.privacy.llm_allowed must be a boolean; refusing hosted calls"
        )
    allowed = _validated_directive_list(privacy.get("llm_allowed_fields"), "llm_allowed_fields")
    forbidden = _validated_directive_list(
        privacy.get("llm_forbidden_fields"),
        "llm_forbidden_fields",
        supported=FORBIDDEN_DIRECTIVES,
    )
    return PrivacyPolicy(
        llm_allowed=llm_allowed,
        allowed_directives=allowed,
        forbidden_directives=forbidden,
    )


def _validated_directive_list(
    value: Any,
    field: str,
    *,
    supported: Iterable[str] | None = None,
) -> frozenset[str]:
    supported_set = frozenset(supported if supported is not None else ALLOW_DIRECTIVES)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PrivacyConfigurationError(
            f"profile.privacy.{field} must be a list of supported directives"
        )
    unknown = sorted({item for item in value if item not in supported_set})
    if unknown:
        raise PrivacyConfigurationError(
            f"profile.privacy.{field} contains unsupported directives: {', '.join(unknown)}"
        )
    return frozenset(value)


def llm_allowed(profile: dict[str, Any]) -> bool:
    return load_privacy_policy(profile).llm_allowed


def strip_forbidden(value: Any) -> Any:
    """Recursively remove forbidden fields from an arbitrary structure."""
    if isinstance(value, dict):
        return {
            key: strip_forbidden(item)
            for key, item in value.items()
            if not is_forbidden_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [strip_forbidden(item) for item in value]
    return value


def redact_sensitive_text(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    """Redact structured PII patterns and known server-side secrets from text."""
    redacted = _EMAIL_PATTERN.sub("[sähköposti poistettu]", text)
    redacted = _PHONE_PATTERN.sub("[puhelin poistettu]", redacted)
    for secret in extra_secrets:
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "[osoite poistettu]")
    return redacted


def sanitize_outbound(value: Any, *, extra_secrets: Iterable[str] = ()) -> Any:
    """Strip forbidden fields recursively and redact secrets from all text."""
    secrets = tuple(secret for secret in extra_secrets if secret)
    stripped = strip_forbidden(value)
    return _redact_structure(stripped, secrets)


def _redact_structure(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {key: _redact_structure(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_structure(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value, extra_secrets=secrets)
    return value


def collect_forbidden_values(profile: dict[str, Any]) -> tuple[str, ...]:
    """Literal values held under forbidden fields, for outbound redaction.

    Key-based stripping misses a forbidden value copied into an allowed prose
    field (for example an address quoted in the objective). Collecting the
    values lets the final sanitizer redact them anywhere they appear.
    """
    values: list[str] = []

    def walk(value: Any, forbidden_parent: bool) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, forbidden_parent or is_forbidden_key(key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, forbidden_parent)
        elif forbidden_parent and isinstance(value, bool):
            return
        elif forbidden_parent and isinstance(value, (str, int, float)):
            text = str(value).strip()
            if len(text) >= 4:
                values.append(text)

    walk(profile, False)
    return tuple(dict.fromkeys(values))


def _safe_location(location: Any) -> Any:
    if not isinstance(location, dict):
        return location
    return {key: location[key] for key in SAFE_LOCATION_KEYS if key in location}


def project_profile_for_llm(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the allowed, deny-filtered projection for a hosted profile call."""
    policy = load_privacy_policy(profile)
    projected: dict[str, Any] = {}
    for directive in DIRECTIVE_PROJECTIONS:
        if not policy.allows(directive):
            continue
        for key in DIRECTIVE_PROJECTIONS[directive]:
            if key not in profile:
                continue
            if key == "location":
                projected[key] = _safe_location(profile[key])
            else:
                projected[key] = profile[key]
    return sanitize_outbound(projected, extra_secrets=collect_forbidden_values(profile))


def outbound_llm_allowed(profile: dict[str, Any]) -> bool:
    return load_privacy_policy(profile).llm_allowed


def outbound_profile_summary(profile: dict[str, Any]) -> str:
    """JSON profile summary safe to send to a hosted provider."""
    return json.dumps(project_profile_for_llm(profile), ensure_ascii=False, sort_keys=True)


def sanitize_job_travel(travel_assessment: Any) -> dict[str, Any] | None:
    if not isinstance(travel_assessment, dict):
        return None
    return sanitize_outbound(
        {key: travel_assessment[key] for key in SAFE_TRAVEL_KEYS if key in travel_assessment}
    )


def sanitize_feedback_snapshot_fields(
    scoring_snapshot: Any, *, extra_secrets: Iterable[str] = ()
) -> dict[str, Any]:
    if not isinstance(scoring_snapshot, dict):
        return {}
    return sanitize_outbound(scoring_snapshot, extra_secrets=extra_secrets)


def sanitize_learned_payload(value: Any, *, extra_secrets: Iterable[str] = ()) -> Any:
    """Sanitize learned examples/hints before they are appended to a prompt."""
    return sanitize_outbound(value, extra_secrets=extra_secrets)
