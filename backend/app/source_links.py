from app.adapters.base import safe_external_url
from app.adapters.tmt import tmt_localized

TMT_SOURCE_NAMES = frozenset({"tmt", "tmt_oulu"})


def normalize_link(url: str | None) -> str | None:
    """Normalize a rendered external link, returning None for unsafe values.

    Only public HTTP(S) destinations survive. ``javascript:``, ``data:``,
    ``file:``, other schemes, malformed URLs and private/loopback hosts are
    rejected. Employer application links are intentionally not host-restricted:
    they stay displayable but are never used as collection/navigation targets.
    """
    safe_url = safe_external_url(url)
    if not safe_url:
        return None
    return safe_url.rstrip("/") or None


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
