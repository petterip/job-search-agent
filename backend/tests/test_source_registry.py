from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.collection import registry as registry_module
from app.collection.registry import (
    SOURCE_NAMES,
    build_adapter,
    collect_all_sources,
    configured_collect_source_names,
)
from app.config import Settings, get_settings
from app.scheduler import AVAILABLE_SCHEDULED_SOURCES

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_YAML_PATH = REPO_ROOT / "docs" / "sources.yaml"


def _load_sources_yaml() -> dict:
    return yaml.safe_load(SOURCES_YAML_PATH.read_text(encoding="utf-8"))


def test_source_names_match_adapter_factory() -> None:
    for source_name in SOURCE_NAMES:
        adapter = build_adapter(source_name)
        assert adapter.source_name == source_name


def test_source_names_match_scheduled_registry() -> None:
    scheduled_names = [source_name for source_name, _ in AVAILABLE_SCHEDULED_SOURCES]
    assert scheduled_names == list(SOURCE_NAMES)


def test_default_enabled_sources_match_sources_yaml() -> None:
    sources = _load_sources_yaml()["sources"]
    scheduled_by_default = {
        name
        for name, entry in sources.items()
        if entry.get("scheduled_by_default") is True
    }
    assert set(Settings().collector_enabled_sources) == scheduled_by_default


def test_poll_intervals_match_sources_yaml_and_adapters() -> None:
    sources = _load_sources_yaml()["sources"]
    for source_name, interval_minutes in AVAILABLE_SCHEDULED_SOURCES:
        adapter = build_adapter(source_name)
        assert adapter.poll_interval_min == interval_minutes
        assert sources[source_name]["poll_interval_min"] == interval_minutes


def test_optional_registry_sources_have_adapters_but_are_disabled_by_default() -> None:
    sources = _load_sources_yaml()["sources"]
    optional_with_adapters = {
        name
        for name, entry in sources.items()
        if entry.get("scheduled_by_default") is False and name in SOURCE_NAMES
    }
    assert optional_with_adapters == {"careerjet", "linkedin", "valtiolle"}
    assert not (set(Settings().collector_enabled_sources) & optional_with_adapters)


def test_configured_collect_source_names_rejects_unknown_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLLECTOR_ENABLED_SOURCES", "duunitori,missing")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="missing"):
        configured_collect_source_names()


def test_configured_collect_source_names_matches_scheduler_enabled_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLLECTOR_ENABLED_SOURCES", "duunitori,tmt,laura,jobly,eures_fi")
    get_settings.cache_clear()

    assert configured_collect_source_names() == (
        "duunitori",
        "tmt",
        "laura",
        "jobly",
        "eures_fi",
    )


@pytest.mark.asyncio
async def test_collect_all_sources_honors_collector_enabled_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLLECTOR_ENABLED_SOURCES", "duunitori,jobly")
    get_settings.cache_clear()

    collected: list[str] = []

    async def fake_collect_source(source_name: str, **kwargs: object) -> dict[str, str]:
        collected.append(source_name)
        return {"source": source_name}

    monkeypatch.setattr(registry_module, "collect_source", fake_collect_source)

    results = await collect_all_sources()

    assert collected == ["duunitori", "jobly"]
    assert [result["source"] for result in results] == ["duunitori", "jobly"]
