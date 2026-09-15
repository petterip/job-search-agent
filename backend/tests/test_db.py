from app import db as db_module


def test_get_engine_caches_per_url(monkeypatch) -> None:
    db_module.dispose_engines()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/one")
    db_module.get_settings.cache_clear()
    first = db_module.get_engine()
    second = db_module.get_engine()
    assert first is second

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/two")
    db_module.get_settings.cache_clear()
    other = db_module.get_engine()
    assert other is not first
    assert db_module.get_engine() is other

    db_module.dispose_engines()
    db_module.get_settings.cache_clear()
