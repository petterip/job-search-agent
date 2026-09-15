import argparse
import json
import logging

from app.collection.runner import PIPELINE_RUN_LOCK_CLASS, acquire_run_ownership
from app.enrichers.runner import run_enrichment
from app.logging import configure_logging

logger = logging.getLogger("enrichment")


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Run detail enrichment manually.")
    parser.add_argument("--dry-run", action="store_true", help="List candidates without side effects.")
    parser.add_argument("--enqueue-only", action="store_true", help="Enqueue candidates without enriching.")
    parser.add_argument("--source", default=None, help="Limit to one source name.")
    parser.add_argument("--enricher", default="detail_http", help="Enricher name.")
    parser.add_argument("--max-jobs", type=int, default=None, help="Cap candidates for this run.")
    args = parser.parse_args()

    if args.dry_run:
        # Read-only inventory needs no ownership.
        result = run_enrichment(
            dry_run=True,
            enqueue_only=args.enqueue_only,
            enricher=args.enricher,
            source=args.source,
            max_jobs=args.max_jobs,
        )
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0

    ownership = acquire_run_ownership(
        lock_class=PIPELINE_RUN_LOCK_CLASS,
        lock_object=0,
        lock_name="pipeline",
    )
    if ownership is None:
        print(
            json.dumps(
                {"status": "skipped", "reason": "pipeline_owned"},
                ensure_ascii=False,
            )
        )
        return 0
    try:
        result = run_enrichment(
            dry_run=False,
            enqueue_only=args.enqueue_only,
            enricher=args.enricher,
            source=args.source,
            max_jobs=args.max_jobs,
        )
    finally:
        ownership.release()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
