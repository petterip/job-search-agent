"""Explicit provider smoke check for rollout (P2-4).

Configuration validation happens at startup; this module makes **one** real
provider call so a rollout can prove credentials, model access and structured
output before the pipeline spends a full run. It is deliberately not part of
``/health``: a liveness probe must never make a paid call.

Usage::

    make llm-check              # or: python -m app.llm_check
    python -m app.llm_check --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from app.config import get_settings
from app.llm import (
    PaidBudgetExhausted,
    build_evaluation_provider,
    configured_eval_model,
    evaluate_with_parse_retry,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Make one real evaluation call to verify provider credentials, model "
            "access and structured output. This call is billable."
        )
    )
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON"
    )
    args = parser.parse_args()

    settings = get_settings()
    problems = settings.llm_config_problems()
    report: dict[str, object] = {
        "provider": settings.llm_provider or None,
        "configured_model": configured_eval_model(settings)
        if settings.llm_provider
        else None,
        "config_problems": problems,
        "concurrency": settings.llm_eval_concurrency,
        "requirement_aware_excerpt": settings.llm_requirement_aware_excerpt,
    }
    if problems:
        report["ok"] = False
        report["reason"] = "invalid_configuration"
        print(
            json.dumps(report, ensure_ascii=False, indent=2)
            if args.json
            else "invalid LLM configuration: " + "; ".join(problems),
            file=sys.stderr,
        )
        return 1
    if not settings.llm_provider:
        report["ok"] = False
        report["reason"] = "provider_not_configured"
        print(
            json.dumps(report, ensure_ascii=False, indent=2)
            if args.json
            else "LLM_PROVIDER is blank; nothing to check",
            file=sys.stderr,
        )
        return 1

    provider = build_evaluation_provider(settings)
    if provider is None:
        report["ok"] = False
        report["reason"] = "provider_unavailable"
        print(
            json.dumps(report, ensure_ascii=False, indent=2)
            if args.json
            else "provider is unavailable (missing key or active cooldown)",
            file=sys.stderr,
        )
        return 1

    model = configured_eval_model(settings, provider.provider_name)
    started = time.monotonic()
    try:
        evaluation, metadata = evaluate_with_parse_retry(
            provider,
            profile_summary=json.dumps(
                {
                    "objective": "smoke check",
                    "role_clusters": [{"titles_fi": ["testi"]}],
                },
                ensure_ascii=False,
            ),
            job_summary=json.dumps(
                {
                    "title": "Testitehtävä",
                    "employer": "Smoke Check Oy",
                    "location": "Oulu",
                    "description_excerpt": "Lyhyt kuvaus.",
                },
                ensure_ascii=False,
            ),
            model=model,
            prompt_version=settings.llm_prompt_version,
            max_retries=settings.llm_eval_parse_retries,
            claim_attempt=lambda: True,
        )
    except PaidBudgetExhausted:  # pragma: no cover - the lambda always funds a call
        report["ok"] = False
        report["reason"] = "budget_exhausted"
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - the message is the point of the check
        report["ok"] = False
        report["reason"] = "provider_call_failed"
        report["error"] = f"{type(error).__name__}: {error}"[:400]
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"provider call failed: {report['error']}", file=sys.stderr)
        return 1

    report.update(
        {
            "ok": True,
            "returned_model": metadata.get("returned_model"),
            "attempts": metadata.get("attempts"),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "score": evaluation.score,
            "suggested_action": evaluation.suggested_action,
            "usage": metadata.get("usage_normalized"),
        }
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            "provider ok: "
            f"{report['provider']} {report['configured_model']} -> "
            f"{report['returned_model']} in {report['latency_ms']} ms "
            f"(attempts={report['attempts']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
