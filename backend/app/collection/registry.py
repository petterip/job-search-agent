from collections.abc import Callable
from typing import Any

from app.adapters.base import SourceAdapter
from app.adapters.duunitori import DuunitoriAdapter
from app.adapters.eures import EuresAdapter
from app.adapters.jobly import JoblyAdapter
from app.adapters.kirkkorekry import KirkkorekryAdapter
from app.adapters.kuntarekry import KuntarekryAdapter
from app.adapters.laura import LauraAdapter
from app.adapters.tmt import TmtAdapter
from app.adapters.tmt_oulu import TmtOuluAdapter
from app.adapters.varbi import OuluVarbiAdapter
from app.collection.runner import run_source_collection

SOURCE_NAMES = (
    "duunitori",
    "tmt",
    "tmt_oulu",
    "laura",
    "jobly",
    "eures_fi",
    "kuntarekry",
    "kirkkorekry",
    "oulu_varbi",
)


def build_adapter(source_name: str) -> SourceAdapter:
    factories: dict[str, Callable[[], SourceAdapter]] = {
        "duunitori": DuunitoriAdapter,
        "tmt": TmtAdapter,
        "tmt_oulu": TmtOuluAdapter,
        "laura": LauraAdapter,
        "jobly": JoblyAdapter,
        "eures_fi": EuresAdapter,
        "kuntarekry": KuntarekryAdapter,
        "kirkkorekry": KirkkorekryAdapter,
        "oulu_varbi": OuluVarbiAdapter,
    }
    try:
        return factories[source_name]()
    except KeyError as exc:
        raise ValueError(f"Unknown source: {source_name}") from exc


async def collect_source(
    source_name: str,
    *,
    page_size: int | None = None,
    max_pages: int | None = None,
    max_urls: int | None = None,
    skip_if_running: bool = True,
) -> dict[str, Any]:
    adapter = build_adapter(source_name)
    return await run_source_collection(
        adapter,
        page_size=page_size,
        max_pages=max_pages,
        max_urls=max_urls,
        skip_if_running=skip_if_running,
    )


async def collect_all_sources(**kwargs: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for source_name in SOURCE_NAMES:
        results.append(await collect_source(source_name, **kwargs))
    return results
