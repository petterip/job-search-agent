import asyncio
import logging
import signal

from app.config import get_settings
from app.llm import configured_eval_model
from app.logging import configure_logging
from app.scheduler import build_scheduler

logger = logging.getLogger("worker")


async def run_worker() -> None:
    configure_logging()
    settings = get_settings()
    stop_event = asyncio.Event()
    scheduler = build_scheduler()

    def request_stop() -> None:
        logger.info("event=shutdown_requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)

    scheduler.start()
    logger.info(
        "event=started llm_enabled=%s embedding_model=%s eval_model=%s scheduled_jobs=%s",
        bool(settings.llm_provider),
        settings.openai_embedding_model,
        configured_eval_model(settings),
        [job.id for job in scheduler.get_jobs()],
    )

    await stop_event.wait()
    scheduler.shutdown(wait=False)
    logger.info("event=stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
