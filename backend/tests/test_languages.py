from support import with_privacy

from app.languages import evaluate_language_requirements

PROFILE = with_privacy(
    {
        "languages": [
            {"code": "fi", "level": "C2", "hard_filter": "pass"},
            {
                "code": "en",
                "level": "B2",
                "hard_filter": "reject_if_required_level_above_B2_or_native",
            },
            {
                "code": "sv",
                "level": "B2",
                "hard_filter": "reject_if_required_level_above_B2_or_native",
            },
            {"code": "de", "level": "A2", "hard_filter": "reject_if_required"},
            {"code": "uk", "level": "A1", "hard_filter": "reject_if_required"},
        ]
    }
)


def _gate(text: str):
    return evaluate_language_requirements(PROFILE, title="Asiantuntija", text=text)


def test_mandatory_beginner_language_is_rejected() -> None:
    assert _gate("Työ edellyttää saksan kielen taitoa.").failed is True


def test_mandatory_ukrainian_is_rejected() -> None:
    assert _gate("Tehtävässä vaaditaan ukrainan kielen osaamista.").failed is True


def test_optional_language_passes() -> None:
    gate = _gate("Saksan kielen taito katsotaan eduksi.")

    assert gate.failed is False
    assert gate.requires_review is False


def test_not_required_negation_passes() -> None:
    assert _gate("Saksan kieltä ei vaadita.").failed is False


def test_alternative_supported_language_passes() -> None:
    # Finnish is the native language, so "suomi tai ruotsi" is satisfied.
    assert _gate("Edellytämme suomen tai ruotsin kielen taitoa.").failed is False


def test_supported_language_at_b2_passes() -> None:
    assert _gate("Edellytämme sujuvaa englannin kielen taitoa.").failed is False


def test_native_requirement_above_b2_is_rejected() -> None:
    gate = _gate("Haemme äidinkielisen englannin puhujaa.")

    assert gate.failed is True


def test_unknown_level_for_above_b2_language_is_a_caution() -> None:
    gate = _gate("Tehtävä edellyttää englannin kielen taitoa.")

    assert gate.failed is False
    assert gate.requires_review is True


def test_language_mere_mention_is_not_a_requirement() -> None:
    gate = _gate("Työssä käytetään englannin kieltä asiakaspalvelussa.")

    assert gate.failed is False
    assert gate.requires_review is False


def test_country_mention_is_not_a_language_requirement() -> None:
    gate = _gate("Edellytämme Saksan markkinan tuntemusta ja myyntikokemusta.")

    assert gate.failed is False
    assert gate.requires_review is False
