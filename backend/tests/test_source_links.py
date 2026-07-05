from app.source_links import (
    duunitori_url_needs_repair,
    source_external_apply_url,
    tmt_url_needs_repair,
)


def test_duunitori_url_needs_repair_detects_missing_tyo_segment() -> None:
    assert duunitori_url_needs_repair("https://duunitori.fi/tyopaikat/example-slug")
    assert not duunitori_url_needs_repair("https://duunitori.fi/tyopaikat/tyo/example-slug")
    assert not duunitori_url_needs_repair(None)


def test_tmt_url_needs_repair_detects_external_apply_links() -> None:
    assert tmt_url_needs_repair("https://www.kotiavut.fi/helppo-yrittajamalli/")
    assert not tmt_url_needs_repair(
        "https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat/details/?id=abc"
    )


def test_source_external_apply_url_returns_tmt_employer_link() -> None:
    payload = {"applicationUrl": {"values": {"fi": "https://www.kotiavut.fi/helppo-yrittajamalli/"}}}
    announcement = (
        "https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat/details/?id=abc"
    )

    assert (
        source_external_apply_url("tmt", payload, announcement)
        == "https://www.kotiavut.fi/helppo-yrittajamalli"
    )


def test_source_external_apply_url_omits_duplicate_links() -> None:
    url = "https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat/details/?id=abc"
    payload = {"applicationUrl": {"values": {"fi": url}}}

    assert source_external_apply_url("tmt", payload, url) is None
    assert source_external_apply_url("duunitori", payload, url) is None
