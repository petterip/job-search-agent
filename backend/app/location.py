import html
import re
from typing import Any

CITY_NAMES = (
    "Helsinki",
    "Espoo",
    "Vantaa",
    "Kauniainen",
    "Tampere",
    "Turku",
    "Oulu",
    "Rovaniemi",
    "Jyväskylä",
    "Kuopio",
    "Lahti",
    "Pori",
    "Vaasa",
    "Joensuu",
    "Lappeenranta",
    "Hämeenlinna",
    "Seinäjoki",
    "Kouvola",
    "Kotka",
    "Mikkeli",
    "Kokkola",
    "Kajaani",
    "Iisalmi",
    "Raahe",
    "Raasepori",
    "Porvoo",
    "Lohja",
    "Salo",
    "Kemi",
    "Tornio",
    "Ylivieska",
    "Kalajoki",
    "Kempele",
    "Liminka",
    "Ii",
    "Rantasalmi",
    "Utsjoki",
    "Ylöjärvi",
)

LAURA_REGION_NAMES = {
    301: "Varsinais-Suomi",
    571: "Uusimaa",
    572: "Keski-Suomi",
    573: "Pohjois-Savo",
    574: "Pirkanmaa",
    575: "Ahvenanmaa",
    576: "Etelä-Karjala",
    577: "Etelä-Pohjanmaa",
    578: "Etelä-Savo",
    579: "Kanta-Häme",
    581: "Kymenlaakso",
    582: "Pohjanmaa",
    583: "Pohjois-Karjala",
    584: "Pohjois-Pohjanmaa",
    585: "Päijät-Häme",
    586: "Satakunta",
    587: "Kainuu",
    588: "Lappi",
    836: "Pääkaupunkiseutu",
    838: "Ulkomaat",
    840: "Aasia",
    841: "Afrikka",
    842: "Etelä-Amerikka",
    844: "Eurooppa",
    845: "Pohjois-Amerikka",
    1482: "Keski-Pohjanmaa",
    1629: "Oseania",
    50721: "Pääkaupunkiseutu",
}

EURES_NUTS_NAMES = {
    "FI1B1": "Helsinki-Uusimaa",
    "FI1C1": "Varsinais-Suomi",
    "FI1C6": "Pirkanmaa",
    "FI1D9": "Pohjois-Pohjanmaa",
}

LOCATION_ALIASES = {
    "tuiran seurakunta": "Oulu",
    "oulun seurakuntayhtym": "Oulu",
    "oulun yliopisto": "Oulu",
    "rantasalmen kunta": "Rantasalmi",
    "rantasalmen-kunta": "Rantasalmi",
    "utsjoen kunta": "Utsjoki",
    "utsjoen-kunta": "Utsjoki",
    "ylöjärven kaupunki": "Ylöjärvi",
    "ylöjärven-kaupunki": "Ylöjärvi",
}


def strip_markup(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    return normalize_whitespace(html.unescape(text))


def normalize_whitespace(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def detect_work_mode(*values: object) -> str | None:
    text = strip_markup(" ".join(str(value) for value in values if value is not None)).casefold()
    if re.search(r"etätyö\s*:\s*(ei|no)\b", text) or "ei mahdollisuutta työskennellä etänä" in text:
        return None
    if any(token in text for token in ("hybridityö", "hybridityössä", "hybrid work", "hybridimalli", "hybrid model", "osittainen etätyö", "etä-/lähityö")):
        return "Hybridi"
    if any(
        token in text
        for token in (
            "kokonaan etätyönä",
            "etätyömahdollisuus",
            "mahdollisuus etätyöhön",
            "voidaan tehdä etänä",
            "remote work",
            "fully remote",
            "remotely",
        )
    ):
        return "Etä"
    return None


def append_work_mode(location: str | None, work_mode: str | None) -> str | None:
    location = normalize_whitespace(location)
    if not work_mode:
        return location or None
    if not location:
        return work_mode
    if work_mode.casefold() in location.casefold():
        return location
    return f"{location} / {work_mode}"


def infer_city_from_text(*values: object) -> str | None:
    text = strip_markup(" ".join(str(value) for value in values if value is not None))
    folded = text.casefold()
    for marker, location in LOCATION_ALIASES.items():
        if marker in folded:
            return location
    for city in CITY_NAMES:
        if re.search(rf"(?<![A-Za-zÅÄÖåäö]){re.escape(city)}(?:ssa|ssä|sta|stä|an|en|in)?(?![A-Za-zÅÄÖåäö])", text, re.I):
            return city
    return None


def laura_location(payload: dict[str, Any]) -> str | None:
    title = payload.get("title", {})
    if isinstance(title, dict):
        title_text = title.get("rendered")
    else:
        title_text = title
    content = payload.get("content", {})
    content_text = content.get("rendered") if isinstance(content, dict) else content
    link = payload.get("link")
    city = infer_city_from_text(title_text, content_text, link)
    region_ids = [int(value) for value in payload.get("job_listing_region") or [] if str(value).isdigit()]
    regions = [LAURA_REGION_NAMES[value] for value in region_ids if value in LAURA_REGION_NAMES]
    location = city
    if location is None and regions:
        location = "Suomi" if len(regions) >= 10 else ", ".join(dict.fromkeys(regions[:3]))
    if location is None:
        location = "Suomi"
    remote_flag = (payload.get("meta") or {}).get("_remote_position")
    work_mode = "Etä" if remote_flag in {1, "1", True} else detect_work_mode(title_text, content_text)
    return append_work_mode(location, work_mode)


def jobly_location(payload: dict[str, Any]) -> str | None:
    locations: list[str] = []
    job_location = payload.get("jobLocation")
    if isinstance(job_location, dict):
        job_location = [job_location]
    if isinstance(job_location, list):
        for item in job_location:
            if not isinstance(item, dict):
                continue
            address = item.get("address")
            if isinstance(address, dict):
                for key in ("addressLocality", "addressRegion"):
                    value = normalize_whitespace(str(address.get(key) or ""))
                    if value:
                        locations.append(value)
                        break
    if not locations:
        city = infer_city_from_text(payload.get("title"), payload.get("description"), payload.get("source_url"))
        if city:
            locations.append(city)
    location = ", ".join(dict.fromkeys(locations[:3])) or "Suomi"
    return append_work_mode(location, detect_work_mode(payload.get("title"), payload.get("description")))


def eures_location(payload: dict[str, Any]) -> str | None:
    location_map = payload.get("locationMap")
    names: list[str] = []
    if isinstance(location_map, dict):
        for country, codes in location_map.items():
            if isinstance(codes, list):
                for code in codes:
                    if isinstance(code, str) and code in EURES_NUTS_NAMES:
                        names.append(EURES_NUTS_NAMES[code])
            if not names and country == "FI":
                names.append("Suomi")
    location = ", ".join(dict.fromkeys(names[:3])) or "Suomi"
    return append_work_mode(location, detect_work_mode(payload.get("title"), payload.get("description")))


def talentech_location(payload: dict[str, Any], description: str | None = None) -> str | None:
    location = infer_city_from_text(
        payload.get("title"),
        payload.get("url"),
        payload.get("profit_center"),
        description or payload.get("description_text"),
    )
    if location is None:
        location = "Suomi"
    return append_work_mode(location, detect_work_mode(payload.get("title"), description or payload.get("description_text")))
