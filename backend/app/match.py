import argparse
import json
import logging

from app.collection.runner import acquire_publication_ownership
from app.logging import configure_logging
from app.matching import run_matching, run_review_backfill

logger = logging.getLogger("matcher")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Run deterministic recommendation matching.")
    parser.add_argument("--max-jobs", type=int, default=500)
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="Dry-run inventory of the eligible review backlog; makes no paid calls.",
    )
    parser.add_argument(
        "--allow-paid-backfill",
        action="store_true",
        help="Explicitly authorize a paid review backfill within --max-paid-calls.",
    )
    parser.add_argument(
        "--max-paid-calls",
        type=int,
        default=0,
        help="Aggregate paid-call budget for an authorized backfill.",
    )
    args = parser.parse_args()

    if args.inventory or args.allow_paid_backfill:
        # Inventory is read-only; a paid backfill still requires explicit
        # authorization and a positive budget.
        result = run_review_backfill(
            dry_run=not args.allow_paid_backfill,
            allow_paid=args.allow_paid_backfill,
            max_calls=args.max_paid_calls,
        )
        logger.info("event=review_backfill result=%s", result)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return

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
