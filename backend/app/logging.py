import logging

from app.collection.events import SourceRunEventLogHandler


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s component=%(name)s %(message)s",
    )
    collector_logger = logging.getLogger("collector")
    if not any(isinstance(handler, SourceRunEventLogHandler) for handler in collector_logger.handlers):
        handler = SourceRunEventLogHandler(level=logging.WARNING)
        collector_logger.addHandler(handler)
