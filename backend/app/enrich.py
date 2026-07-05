import argparse
import json
import logging

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

    result = run_enrichment(
        dry_run=args.dry_run,
        enqueue_only=args.enqueue_only,
        enricher=args.enricher,
        source=args.source,
        max_jobs=args.max_jobs,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
