from support import with_privacy

from app.qualifications import evaluate_qualification_checks

PROFILE = with_privacy(
    {
        "exclusions": {
            "qualification_checks": {
                "sosiaalityontekija": {
                    "reject_when_phrases_present": [
                        "sosiaalityöntekijän kelpoisuus",
                        "laillistettu sosiaalityöntekijä",
                    ],
                    "adjacent_roles_that_can_pass": [
                        "sosiaalipalvelusihteeri",
                        "palvelusihteeri",
                    ],
                },
                "pappi_seurakuntapastori": {
                    "do_not_title_reject": True,
                    "caution_when_phrases_present": ["pappisvihkimys", "vihitty pappi"],
                },
                "nuorisotyonohjaaja": {
                    "check_required_phrases": ["kirkon nuorisotyönohjaajan tutkinto"],
                },
                "kirjastoautonkuljettaja": {
                    "reject_when_title_or_phrases_present": [
                        "kirjastoautonkuljettaja",
                        "CE-ajokortti",
                    ],
                },
            }
        }
    }
)


def test_required_missing_qualification_rejects() -> None:
    gate = evaluate_qualification_checks(
        PROFILE,
        title="Sosiaalityöntekijä",
        text="Kelpoisuusvaatimuksena sosiaalityöntekijän kelpoisuus.",
    )

    assert gate.hard_reject is True
    assert "sosiaalityontekija" in gate.reasons


def test_adjacent_role_with_supported_route_is_only_a_caution() -> None:
    gate = evaluate_qualification_checks(
        PROFILE,
        title="Sosiaalipalvelusihteeri",
        text="Arvostamme sosiaalityöntekijän kelpoisuutta.",
    )

    assert gate.hard_reject is False
    assert "sosiaalityontekija" in gate.cautions


def test_do_not_title_reject_stays_a_caution() -> None:
    gate = evaluate_qualification_checks(
        PROFILE,
        title="Seurakuntapastori",
        text="Tehtävä edellyttää pappisvihkimystä.",
    )

    assert gate.hard_reject is False
    assert "pappi_seurakuntapastori" in gate.cautions


def test_role_specific_required_phrase_is_a_caution() -> None:
    gate = evaluate_qualification_checks(
        PROFILE,
        title="Nuorisotyönohjaaja",
        text="Edellytyksenä kirkon nuorisotyönohjaajan tutkinto.",
    )

    assert gate.hard_reject is False
    assert "nuorisotyonohjaaja" in gate.cautions


def test_heavy_licence_exclusion_rejects_on_title_or_phrase() -> None:
    assert evaluate_qualification_checks(
        PROFILE, title="Kirjastoautonkuljettaja", text=""
    ).hard_reject is True
    assert evaluate_qualification_checks(
        PROFILE, title="Kuljettaja", text="Vaatimuksena CE-ajokortti."
    ).hard_reject is True


def test_unrelated_role_passes_all_checks() -> None:
    gate = evaluate_qualification_checks(
        PROFILE,
        title="Kirjastonhoitaja",
        text="Kirjastoalan tehtävät ja asiakaspalvelu.",
    )

    assert gate.hard_reject is False
    assert gate.cautions == ()
