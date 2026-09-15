"""Required-language evidence for the deterministic hard gate.

Language requirements are interpreted from bounded phrase/level rules rather
than from merely mentioning a language. Mandatory and preferred requirements,
negation and alternative supported languages are distinguished; unknown levels
become a review caution, not invented certainty. A language failure is a hard
eligibility result and cannot be recovered by a semantic score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

LANGUAGE_NAMES: dict[str, tuple[str, ...]] = {
    "fi": ("suom", "finnish"),
    "sv": ("ruots", "swedish"),
    "en": ("englann", "english"),
    "de": ("saks", "german", "deutsch"),
    "uk": ("ukrain", "ukrainian"),
}

# Strong markers make a requirement mandatory even when optional wording is
# present; weak markers only count when the sentence is not optional.
STRONG_REQUIREMENT_MARKERS = (
    "vaaditaan",
    "edellytetään",
    "edellyttää",
    "edellytämme",
    "edellytyksenä",
    "edellytys",
    "vaatimus",
    "on osattava",
    "täytyy osata",
    "hallittava",
    "required",
    "proficiency",
    "requirement",
)
WEAK_REQUIREMENT_MARKERS = ("taito", "sujuva", "sujuvasti", "fluent", "osaaminen")
OPTIONAL_MARKERS = (
    "eduksi",
    "katsotaan eduksi",
    "toivottava",
    "toivomme",
    "arvostamme",
    "optional",
    "nice to have",
    "plussaa",
    "hyödyksi",
    "etuna",
)
NEGATION_MARKERS = (
    "ei vaadita",
    "ei edellytetä",
    "ei tarvitse",
    "ei ole vaatimus",
    "ei edellytyksenä",
    "not required",
)
NATIVE_MARKERS = ("äidinkiel", "natiivi", "native", "mother tongue")
EXCELLENT_MARKERS = ("erinoma", "excellent", "täydellis")
# Levels at or below the supported B2 that satisfy an "above B2" gate.
AT_OR_BELOW_B2_MARKERS = ("sujuv", "fluent", "hyvä", "hyvin", "tyydyttävä", "perus", "b1", "b2")

SENTENCE_SPLIT = re.compile(r"[.!?\n;]+")


@dataclass(frozen=True)
class LanguageGate:
    failed: bool
    requires_review: bool
    failed_languages: tuple[str, ...] = ()
    cautions: tuple[str, ...] = ()
    evidence: list[str] = field(default_factory=list)

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "failed": self.failed,
            "requires_review": self.requires_review,
            "failed_languages": list(self.failed_languages),
            "cautions": list(self.cautions),
            "evidence": list(self.evidence),
        }


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in SENTENCE_SPLIT.split(text or "") if part.strip()]


def _mentions(sentence: str, language: str) -> bool:
    folded = sentence.casefold()
    return any(name in folded for name in LANGUAGE_NAMES.get(language, ()))


def _is_negated(sentence: str) -> bool:
    folded = sentence.casefold()
    return any(marker in folded for marker in NEGATION_MARKERS)


def _is_optional(sentence: str) -> bool:
    folded = sentence.casefold()
    return any(marker in folded for marker in OPTIONAL_MARKERS)


def _is_mandatory(sentence: str) -> bool:
    folded = sentence.casefold()
    return any(marker in folded for marker in STRONG_REQUIREMENT_MARKERS) or any(
        marker in folded for marker in WEAK_REQUIREMENT_MARKERS
    )


def _is_strongly_mandatory(sentence: str) -> bool:
    folded = sentence.casefold()
    return any(marker in folded for marker in STRONG_REQUIREMENT_MARKERS)


def _alternative_supported(sentence: str, language: str, profile_languages: dict[str, dict[str, Any]]) -> bool:
    """True when `suomi tai ruotsi`-style wording offers a supported option."""
    lowered = sentence.casefold()
    if " tai " not in lowered and " or " not in lowered:
        return False
    for other, names in LANGUAGE_NAMES.items():
        if other == language:
            continue
        if not any(name in lowered for name in names):
            continue
        capability = profile_languages.get(other)
        if capability is None:
            continue
        hard_filter = str(capability.get("hard_filter") or "")
        if hard_filter == "pass" or hard_filter.startswith("reject_if_required_level_above_B2"):
            return True
    return False


def evaluate_language_requirements(
    profile: dict[str, Any],
    *,
    title: str | None,
    text: str | None,
) -> LanguageGate:
    languages = profile.get("languages")
    if not isinstance(languages, list):
        return LanguageGate(failed=False, requires_review=False)
    profile_languages: dict[str, dict[str, Any]] = {
        str(item.get("code")): item
        for item in languages
        if isinstance(item, dict) and item.get("code")
    }
    haystack = f"{title or ''}. {text or ''}"
    failed: list[str] = []
    cautions: list[str] = []
    evidence: list[str] = []
    for code, capability in profile_languages.items():
        hard_filter = str(capability.get("hard_filter") or "")
        if not hard_filter.startswith("reject_if_required"):
            continue
        above_b2_only = hard_filter.startswith("reject_if_required_level_above_B2")
        for sentence in _sentences(haystack):
            if not _mentions(sentence, code):
                continue
            if _is_negated(sentence):
                continue
            optional = _is_optional(sentence)
            native_or_excellent = any(
                marker in sentence.casefold() for marker in NATIVE_MARKERS
            ) or any(marker in sentence.casefold() for marker in EXCELLENT_MARKERS)
            mandatory = _is_strongly_mandatory(sentence) or native_or_excellent
            if not mandatory or optional:
                continue
            if _alternative_supported(sentence, code, profile_languages):
                continue
            snippet = sentence.strip()[:120]
            folded = sentence.casefold()
            evidence.append(f"{code}: {snippet}")
            if above_b2_only:
                if any(marker in folded for marker in NATIVE_MARKERS) or any(
                    marker in folded for marker in EXCELLENT_MARKERS
                ):
                    failed.append(code)
                elif any(marker in folded for marker in AT_OR_BELOW_B2_MARKERS):
                    # Explicitly at/below B2 satisfies the requirement.
                    break
                else:
                    cautions.append(code)
            else:
                failed.append(code)
            break
    return LanguageGate(
        failed=bool(failed),
        requires_review=bool(cautions),
        failed_languages=tuple(sorted(set(failed))),
        cautions=tuple(sorted(set(cautions))),
        evidence=evidence,
    )
