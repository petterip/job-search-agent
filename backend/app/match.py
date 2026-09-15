import argparse
import json
import logging

from app.collection.runner import acquire_publication_ownership
from app.logging import configure_logging
from app.matching import run_matching

logger = logging.getLogger("matcher")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Run deterministic recommendation matching.")
    parser.add_argument("--max-jobs", type=int, default=500)
    args = parser.parse_args()

    # Matching publishes into the active profile, so it must own the same
    # profile publication lock as the scheduled pipeline and feedback analysis.
    ownership = acquire_publication_ownership()
    if ownership is None:
        logger.warning("event=matching_skipped reason=run_ownership_not_acquired")
        print(json.dumps({"skipped": True, "reason": "run_ownership_not_acquired"}))
        return
    try:
        result = run_matching(max_jobs=args.max_jobs)
    finally:
        ownership.release()
    logger.info("event=matching_completed result=%s", result)
    print(result)


if __name__ == "__main__":
    main()
