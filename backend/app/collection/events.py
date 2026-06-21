import contextvars
import json
import logging
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.db import get_engine

current_source_run_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_source_run_id",
    default=None,
)
current_source_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_source_id",
    default=None,
)
current_source_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_source_name",
    default=None,
)


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _event_type_from_message(message: str) -> str:
    for part in message.split():
        if part.startswith("event="):
            return part.split("=", 1)[1]
    return "log_record"


def insert_source_run_event(
    connection: Connection,
    *,
    source_run_id: int | None,
    source_id: int | None,
    source_name: str,
    level: str,
    event_type: str,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        sa.text(
            """
            insert into source_run_events (
                source_run_id,
                source_id,
                source_name,
                level,
                event_type,
                message,
                details
            )
            values (
                :source_run_id,
                :source_id,
                :source_name,
                :level,
                :event_type,
                :message,
                CAST(:details AS jsonb)
            )
            """
        ),
        {
            "source_run_id": source_run_id,
            "source_id": source_id,
            "source_name": source_name,
            "level": level.lower(),
            "event_type": event_type,
            "message": message,
            "details": json.dumps(details or {}, ensure_ascii=False),
        },
    )


def record_source_run_event(
    *,
    source_run_id: int | None,
    source_id: int | None,
    source_name: str,
    level: str,
    event_type: str,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    engine = get_engine()
    with engine.begin() as connection:
        insert_source_run_event(
            connection,
            source_run_id=source_run_id,
            source_id=source_id,
            source_name=source_name,
            level=level,
            event_type=event_type,
            message=message,
            details=details,
        )


class SourceRunEventLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        source_name = current_source_name.get()
        if not source_name:
            return
        if not record.name.startswith("collector"):
            return

        message = record.getMessage()
        details = {
            "logger": record.name,
            "pathname": record.pathname,
            "lineno": record.lineno,
        }
        if record.exc_info:
            details["exception"] = self.formatException(record.exc_info)
        if record.args:
            details["args"] = [_json_safe(value) for value in record.args]

        try:
            record_source_run_event(
                source_run_id=current_source_run_id.get(),
                source_id=current_source_id.get(),
                source_name=source_name,
                level=record.levelname,
                event_type=_event_type_from_message(message),
                message=message,
                details=details,
            )
        except Exception:
            return
