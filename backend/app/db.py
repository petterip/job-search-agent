from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from app.config import get_settings


def get_engine():
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def db_connection() -> Iterator[Connection]:
    engine = get_engine()
    with engine.begin() as connection:
        yield connection
