import argparse
import logging

from app.logging import configure_logging
from app.matching import run_matching

logger = logging.getLogger("matcher")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Run deterministic recommendation matching.")
    parser.add_argument("--max-jobs", type=int, default=500)
    args = parser.parse_args()

    result = run_matching(max_jobs=args.max_jobs)
    logger.info("event=matching_completed result=%s", result)
    print(result)


if __name__ == "__main__":
    main()
