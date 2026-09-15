"""Configurable qualification checks driven by the profile contract.

`exclusions.qualification_checks` describes supported, listing-aware rules:
hard rejections that must not fire for adjacent roles, do-not-title-reject
entries, and required-phrase checks that stay a review caution. Rules are data,
so adding a qualification policy does not require new code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

TOKEN_PATTERN = re.compile(r"[0-9a-zåäö]+")


def _normalized(value: str | None) -> str:
    return " ".join(TOKEN_PATTERN.findall((value or "").casefold()))


# Suffixes stripped before comparison so Finnish inflection and consonant
# gradation do not hide a configured phrase (kelpoisuus ~ kelpoisuutta,
# äidinkielinen ~ äidinkielisen). Stripping only applies when a stem remains.
INFLECTION_SUFFIXES = (
    "uudesta",
    "uudelle",
    "uutta",
    "uuden",
    "uuteen",
    "yyttä",
    "yystä",
    "mystä",
    "myksestä",
    "mykseen",
    "miseen",
    "misesta",
    "nen",
    "sen",
    "seen",
    "uus",
    "mys",
    "us",
    "ssa",
    "ssä",
    "sta",
    "stä",
    "lla",
    "llä",
    "lle",
    "ksi",
    "tta",
    "ttä",
    "ta",
    "tä",
    "n",
    "a",
    "ä",
)


def _token_list(value: str | None) -> list[str]:
    return TOKEN_PATTERN.findall((value or "").casefold())


def _stem(token: str) -> str:
    for suffix in INFLECTION_SUFFIXES:
        if len(token) - len(suffix) >= 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def phrase_present(phrase: str, text: str | None) -> bool:
    """Token-sequence phrase match that tolerates Finnish inflected heads.

    Each configured token is compared with a bounded stem so
    `sosiaalityöntekijän kelpoisuus` matches `… kelpoisuutta` without matching
    an unrelated compound. Distinct words keep distinct stems.
    """
    phrase_tokens = [_stem(token) for token in _token_list(phrase)]
    if not phrase_tokens or not text:
        return False
    text_tokens = [_stem(token) for token in _token_list(text)]
    size = len(phrase_tokens)
    for start in range(len(text_tokens) - size + 1):
        if text_tokens[start : start + size] == phrase_tokens:
            return True
    return False


NEGATION_MARKERS = (
    "ei ole vaatimus",
    "ei vaadita",
    "ei edellytä",
    "ei edellytetä",
    "ei tarvitse",
    "ei ole pakollinen",
    "ei kuulu",
    "not required",
)
OPTIONAL_MARKERS = (
    "eduksi",
    "toivottava",
    "toivomme",
    "arvostamme",
    "optional",
    "nice to have",
    "etuna",
    "plussaa",
)


def _sentence_around(text: str, phrase: str) -> str:
    folded = (text or "").casefold()
    needle = phrase.casefold()
    index = folded.find(needle)
    if index < 0:
        return ""
    start = max(folded.rfind(mark, 0, index) for mark in (".", "!", "?", "\n")) + 1
    ends = [pos for pos in (folded.find(mark, index) for mark in (".", "!", "?", "\n")) if pos >= 0]
    end = min(ends) + 1 if ends else len(text)
    return text[start:end]


def _requirement_softened(text: str, phrase: str) -> bool:
    """True when the phrase appears in a negated or optional sentence."""
    sentence = _sentence_around(text, phrase).casefold()
    if not sentence:
        return False
    return any(marker in sentence for marker in NEGATION_MARKERS) or any(
        marker in sentence for marker in OPTIONAL_MARKERS
    )


@dataclass(frozen=True)
class QualificationGate:
    hard_reject: bool
    reasons: tuple[str, ...] = ()
    cautions: tuple[str, ...] = ()
    evidence: list[str] = field(default_factory=list)

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "hard_reject": self.hard_reject,
            "reasons": list(self.reasons),
            "cautions": list(self.cautions),
            "evidence": list(self.evidence),
        }


def _phrase_list(config: dict[str, Any], key: str) -> list[str]:
    value = config.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def evaluate_qualification_checks(
    profile: dict[str, Any],
    *,
    title: str | None,
    text: str | None,
) -> QualificationGate:
    exclusions = profile.get("exclusions")
    if not isinstance(exclusions, dict):
        return QualificationGate(hard_reject=False)
    checks = exclusions.get("qualification_checks")
    if not isinstance(checks, dict):
        return QualificationGate(hard_reject=False)

    haystack = f"{title or ''}. {text or ''}"
    reasons: list[str] = []
    cautions: list[str] = []
    evidence: list[str] = []
    for name, raw_config in sorted(checks.items()):
        if not isinstance(raw_config, dict):
            continue
        reason = str(raw_config.get("reason") or "").strip()

        title_or_phrases = _phrase_list(raw_config, "reject_when_title_or_phrases_present")
        if title_or_phrases:
            title_match = next(
                (phrase for phrase in title_or_phrases if phrase_present(phrase, title)), None
            )
            text_match = next(
                (phrase for phrase in title_or_phrases if phrase_present(phrase, haystack)), None
            )
            if title_match is not None:
                reasons.append(str(name))
                evidence.append(f"{name}: {title_match}"[:200])
                continue
            if text_match is not None:
                if _requirement_softened(haystack, text_match):
                    cautions.append(str(name))
                    evidence.append(f"{name}: negated/optional mention of {text_match}"[:200])
                else:
                    reasons.append(str(name))
                    evidence.append(f"{name}: {text_match}"[:200])
                continue

        reject_phrases = _phrase_list(raw_config, "reject_when_phrases_present")
        if reject_phrases:
            matched = [
                phrase
                for phrase in reject_phrases
                if phrase_present(phrase, haystack) and not _requirement_softened(haystack, phrase)
            ]
            softened = [
                phrase
                for phrase in reject_phrases
                if phrase_present(phrase, haystack) and _requirement_softened(haystack, phrase)
            ]
            if not matched and softened:
                cautions.append(str(name))
                evidence.append(f"{name}: negated/optional mention of {softened[0]}"[:200])
                continue
            if matched:
                adjacent = _phrase_list(raw_config, "adjacent_roles_that_can_pass")
                if adjacent and any(phrase_present(role, title) for role in adjacent):
                    cautions.append(str(name))
                    evidence.append(
                        f"{name}: adjacent role with a supported route ({matched[0]})"[:200]
                    )
                else:
                    reasons.append(str(name))
                    evidence.append(f"{name}: {matched[0]}"[:200])
                continue

        if raw_config.get("do_not_title_reject"):
            caution_phrases = _phrase_list(raw_config, "caution_when_phrases_present")
            if any(phrase_present(phrase, haystack) for phrase in caution_phrases):
                cautions.append(str(name))
                evidence.append(f"{name}: {reason or 'requires manual check'}"[:200])
            continue

        required_phrases = _phrase_list(raw_config, "check_required_phrases")
        if required_phrases and any(
            phrase_present(phrase, haystack) for phrase in required_phrases
        ):
            cautions.append(str(name))
            evidence.append(f"{name}: {reason or 'role-specific qualification varies'}"[:200])

    return QualificationGate(
        hard_reject=bool(reasons),
        reasons=tuple(sorted(set(reasons))),
        cautions=tuple(sorted(set(cautions))),
        evidence=evidence,
    )
