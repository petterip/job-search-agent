import argparse
import asyncio
import logging

from app.collection.registry import SOURCE_NAMES, collect_all_sources, collect_source
from app.logging import configure_logging

logger = logging.getLogger("collector")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Run source collection manually.")
    parser.add_argument(
        "source",
        choices=[*SOURCE_NAMES, "all"],
        help="Source to collect, or 'all' for every enabled source.",
    )
    parser.add_argument("--page-size", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--max-urls", type=int, default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Deprecated: database ownership is always required, so this cannot bypass a live run.",
    )
    args = parser.parse_args()

    kwargs = {
        "page_size": args.page_size,
        "max_pages": args.max_pages,
        "max_urls": args.max_urls,
        "skip_if_running": not args.force,
    }

    if args.source == "all":
        results = asyncio.run(collect_all_sources(**kwargs))
        for counts in results:
            logger.info("event=source_collected counts=%s", counts)
        return

    counts = asyncio.run(collect_source(args.source, **kwargs))
    logger.info("event=source_collected counts=%s", counts)


if __name__ == "__main__":
    main()
