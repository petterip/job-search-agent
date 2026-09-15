from app.source_links import (
    duunitori_url_needs_repair,
    normalize_link,
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


def test_normalize_link_rejects_non_http_and_malformed_values() -> None:
    assert normalize_link("javascript:alert(1)") is None
    assert normalize_link("data:text/html,<script>alert(1)</script>") is None
    assert normalize_link("file:///etc/passwd") is None
    assert normalize_link("ftp://example.com/cv") is None
    assert normalize_link("//example.com/no-scheme") is None
    assert normalize_link("http://") is None
    assert normalize_link("") is None
    assert normalize_link(None) is None


def test_normalize_link_rejects_private_and_local_hosts() -> None:
    assert normalize_link("http://127.0.0.1:8008/apply") is None
    assert normalize_link("http://localhost/apply") is None
    assert normalize_link("http://10.0.0.5/apply") is None
    assert normalize_link("http://169.254.169.254/latest/meta-data/") is None
    assert normalize_link("http://intranet.local/apply") is None
    assert normalize_link("https://user:secret@example.com/apply") is None


def test_normalize_link_keeps_employer_application_links() -> None:
    assert (
        normalize_link("https://www.kotiavut.fi/helppo-yrittajamalli/")
        == "https://www.kotiavut.fi/helppo-yrittajamalli"
    )
    assert normalize_link("http://example.com/apply") == "http://example.com/apply"


def test_source_external_apply_url_rejects_unsafe_application_urls() -> None:
    announcement = "https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat/details/?id=abc"
    for unsafe in (
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "http://127.0.0.1/apply",
        "http://localhost/apply",
        "not a url",
    ):
        payload = {"applicationUrl": {"values": {"fi": unsafe}}}
        assert source_external_apply_url("tmt", payload, announcement) is None


def test_source_external_apply_url_keeps_legitimate_employer_link() -> None:
    payload = {"applicationUrl": {"values": {"fi": "https://www.kotiavut.fi/helppo-yrittajamalli/"}}}
    announcement = "https://tyomarkkinatori.fi/henkiloasiakkaat/avoimet-tyopaikat/details/?id=abc"

    assert (
        source_external_apply_url("tmt", payload, announcement)
        == "https://www.kotiavut.fi/helppo-yrittajamalli"
    )
