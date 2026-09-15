from collections.abc import Iterator
from atexit import register
from threading import Lock

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

from app.config import get_settings

# One engine per database URL per process. Keying by URL means a test or config
# override never reuses an engine pointed at the wrong database.
_ENGINES: dict[str, Engine] = {}
_LOCK = Lock()


def get_engine() -> Engine:
    url = get_settings().database_url
    engine = _ENGINES.get(url)
    if engine is not None:
        return engine
    with _LOCK:
        engine = _ENGINES.get(url)
        if engine is None:
            engine = create_engine(url, pool_pre_ping=True)
            _ENGINES[url] = engine
    return engine


def dispose_engines() -> None:
    """Dispose cached engines; intended for shutdown and tests."""
    with _LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
    for engine in engines:
        engine.dispose()


register(dispose_engines)


def db_connection() -> Iterator[Connection]:
    engine = get_engine()
    with engine.begin() as connection:
        yield connection
