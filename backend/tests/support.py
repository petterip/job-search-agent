"""Shared, sanitized test fixtures.

Only synthetic values belong here. Never copy private profile or provider
content into committed tests.
"""

from __future__ import annotations

from typing import Any

# Mirrors the supported directive vocabulary of the real profile contract
# without reproducing any private value.
DEFAULT_PRIVACY: dict[str, Any] = {
    "pii_level": "derived_minimized_profile",
    "llm_allowed": True,
    "llm_allowed_fields": [
        "objective",
        "location policy summary",
        "languages",
        "career_evidence",
        "role_clusters",
        "skills",
        "strength_signals",
        "preferences",
        "exclusions",
        "freshness",
        "llm_guidance",
        "availability",
    ],
    "llm_forbidden_fields": [
        "raw source files",
        "extracted application letters with contact details",
        "home_postal",
        "address",
        "phone",
        "email",
        "birth date",
        "ssn",
        "photo",
    ],
}


def with_privacy(profile: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    privacy = dict(DEFAULT_PRIVACY)
    privacy.update(overrides)
    return {**(profile or {}), "privacy": privacy}
