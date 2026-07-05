from app.adapters.tmt import tmt_localized

TMT_SOURCE_NAMES = frozenset({"tmt", "tmt_oulu"})


def normalize_link(url: str | None) -> str | None:
    if not url:
        return None
    normalized = url.strip()
    return normalized.rstrip("/") if normalized else None


def tmt_external_application_url(payload: dict | None) -> str | None:
    if not payload:
        return None
    return normalize_link(tmt_localized(payload.get("applicationUrl")))


def source_external_apply_url(
    source_name: str,
    payload: dict | None,
    announcement_url: str | None,
) -> str | None:
    if source_name not in TMT_SOURCE_NAMES:
        return None
    external = tmt_external_application_url(payload)
    announcement = normalize_link(announcement_url)
    if not external or external == announcement:
        return None
    return external


def duunitori_url_needs_repair(url: str | None) -> bool:
    return bool(
        url
        and url.startswith("https://duunitori.fi/tyopaikat/")
        and "/tyopaikat/tyo/" not in url
    )


def tmt_url_needs_repair(url: str | None) -> bool:
    return bool(url and not url.startswith("https://tyomarkkinatori.fi/"))
