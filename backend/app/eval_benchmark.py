"""Bounded paid benchmarks for the recommendation pipeline (P2-1, P2-6, P2-7).

Two modes, both of which refuse to run without an explicit paid-call flag and a
declared budget:

``throughput``
    Runs the real sequential and concurrent evaluation paths over the same
    candidate cohort on a **disposable** database and reports wall-clock,
    provider-call counts and the measured speed-up. It rewrites only
    ``llm_evaluations`` rows for that cohort.

``prompt-variant``
    Paired direct provider calls on fixed job inputs, comparing the baseline
    prompt with the requirement-aware excerpt (P2-7) or with profile-calibration
    rules (P2-6). It reports decision parity and measured token usage. It never
    writes to the database.

Both modes print a JSON report; nothing here is on a schedule.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.db import get_engine
from app.llm import (
    REQUIREMENT_KEYWORDS,
    build_evaluation_provider,
    configured_eval_model,
    evaluation_request_hash,
    job_summary,
    normalize_provider_usage,
    requirement_aware_excerpt,
)
from app.labelled_evaluation import LabelledEvaluationError
from app.matching import (
    evaluation_request_context,
    run_llm_evaluations,
    run_llm_evaluations_concurrent,
    select_llm_review_candidates,
)


def provider_for(provider_name: str, settings: Any) -> Any:
    """Build the provider client for an explicit provider name."""
    if provider_name == "gemini":
        from app.llm import GeminiEvaluationProvider

        return GeminiEvaluationProvider(api_key=settings.gemini_api_key)
    from app.llm import OpenAIEvaluationProvider

    return OpenAIEvaluationProvider(
        api_key=settings.openai_api_key,
        timeout_seconds=settings.openai_eval_timeout_seconds,
    )


def _database_label() -> str:
    """Report the database host/database without credentials."""
    from sqlalchemy.engine import make_url

    url = make_url(get_settings().database_url)
    return f"{url.host or 'local'}:{url.port or ''}/{url.database or ''}"


def _require_paid_authorization(
    allow_paid: bool, budget: int, expected_calls: int
) -> None:
    if not allow_paid:
        raise LabelledEvaluationError(
            "refusing to make paid provider calls without --allow-paid-calls"
        )
    if budget <= 0:
        raise LabelledEvaluationError("--budget must be positive")
    if expected_calls > budget:
        raise LabelledEvaluationError(
            f"this run would make up to {expected_calls} paid calls but the budget is {budget}"
        )


def _profile_document(connection: Connection) -> tuple[int, dict[str, Any]]:
    row = (
        connection.execute(
            sa.text("select id, profile from job_seeker_profiles order by id limit 1")
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise LabelledEvaluationError("no profile found in this database")
    return int(row["id"]), dict(row["profile"])


def _cohort(connection: Connection, *, profile_id: int, limit: int) -> list[int]:
    settings = get_settings()
    profile = _profile_document(connection)[1]
    rows = select_llm_review_candidates(
        connection,
        profile_id=profile_id,
        profile=profile,
        settings=settings,
        max_jobs=limit,
    )
    return [int(row["job_id"]) for row in rows][:limit]


def _reset_cohort(
    connection: Connection, *, profile_id: int, job_ids: list[int]
) -> None:
    connection.execute(
        sa.text(
            "update recommendations set llm_evaluation_id = null "
            "where profile_id = :profile_id and job_id = any(:job_ids)"
        ),
        {"profile_id": profile_id, "job_ids": job_ids},
    )
    connection.execute(
        sa.text(
            "delete from llm_evaluations "
            "where profile_id = :profile_id and job_id = any(:job_ids)"
        ),
        {"profile_id": profile_id, "job_ids": job_ids},
    )
    connection.commit()


def _evaluation_count(
    connection: Connection, *, profile_id: int, job_ids: list[int]
) -> int:
    return int(
        connection.execute(
            sa.text(
                "select count(*) from llm_evaluations "
                "where profile_id = :profile_id and job_id = any(:job_ids)"
            ),
            {"profile_id": profile_id, "job_ids": job_ids},
        ).scalar_one()
    )


def _usage_totals(
    connection: Connection, *, profile_id: int, job_ids: list[int]
) -> dict[str, Any]:
    row = (
        connection.execute(
            sa.text(
                """
                select
                    count(*) as calls,
                    coalesce(sum(input_tokens), 0) as input_tokens,
                    coalesce(sum(output_tokens), 0) as output_tokens,
                    count(*) filter (where usage_present) as calls_with_usage
                from llm_evaluations
                where profile_id = :profile_id and job_id = any(:job_ids)
                """
            ),
            {"profile_id": profile_id, "job_ids": job_ids},
        )
        .mappings()
        .one()
    )
    return {
        "calls": int(row["calls"]),
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "calls_with_usage": int(row["calls_with_usage"]),
    }


def run_throughput_benchmark(
    *,
    limit: int,
    concurrency: int,
    budget: int,
    allow_paid_calls: bool,
    database_is_disposable: bool,
) -> dict[str, Any]:
    if not database_is_disposable:
        raise LabelledEvaluationError(
            "throughput mode rewrites llm_evaluations rows; pass "
            "--database-is-disposable to confirm the target database is disposable"
        )
    _require_paid_authorization(allow_paid_calls, budget, expected_calls=2 * limit)
    settings = get_settings()
    provider = build_evaluation_provider(settings)
    if provider is None:
        raise LabelledEvaluationError(
            "no evaluation provider is configured (or it is in cooldown)"
        )
    engine = get_engine()
    model = configured_eval_model(settings, provider.provider_name)

    with engine.connect() as connection:
        profile_id, _profile = _profile_document(connection)
        cohort = _cohort(connection, profile_id=profile_id, limit=limit)
        if not cohort:
            raise LabelledEvaluationError("no review candidates found in this database")
        _reset_cohort(connection, profile_id=profile_id, job_ids=cohort)

    sequential_started = time.monotonic()
    with engine.connect() as connection:
        sequential_counts = run_llm_evaluations(
            connection, provider=provider, max_jobs=limit
        )
    sequential_elapsed = time.monotonic() - sequential_started
    with engine.connect() as connection:
        sequential_usage = _usage_totals(
            connection, profile_id=profile_id, job_ids=cohort
        )
        _reset_cohort(connection, profile_id=profile_id, job_ids=cohort)

    concurrent_started = time.monotonic()
    concurrent_counts = run_llm_evaluations_concurrent(
        engine,
        provider=provider,
        max_jobs=limit,
        concurrency=concurrency,
    )
    concurrent_elapsed = time.monotonic() - concurrent_started
    with engine.connect() as connection:
        concurrent_usage = _usage_totals(
            connection, profile_id=profile_id, job_ids=cohort
        )
        remaining = _evaluation_count(connection, profile_id=profile_id, job_ids=cohort)

    total_calls = sequential_usage["calls"] + concurrent_usage["calls"]
    if total_calls > budget:
        raise LabelledEvaluationError(
            f"benchmark made {total_calls} paid calls, above the declared budget {budget}"
        )
    speedup = (
        round(sequential_elapsed / concurrent_elapsed, 2)
        if concurrent_elapsed > 0
        else None
    )
    return {
        "mode": "throughput",
        "database": _database_label(),
        "provider": provider.provider_name,
        "model": model,
        "cohort_jobs": len(cohort),
        "concurrency": max(1, min(concurrency, 16)),
        "budget": budget,
        "sequential": {
            "elapsed_seconds": round(sequential_elapsed, 2),
            "counts": sequential_counts,
            **sequential_usage,
        },
        "concurrent": {
            "elapsed_seconds": round(concurrent_elapsed, 2),
            "counts": concurrent_counts,
            **concurrent_usage,
        },
        "speedup": speedup,
        "paid_calls": total_calls,
        "evaluations_left_in_cohort": remaining,
    }


def _with_excerpt_setting(aware: bool, action: Any) -> Any:
    previous = os.environ.get("LLM_REQUIREMENT_AWARE_EXCERPT")
    os.environ["LLM_REQUIREMENT_AWARE_EXCERPT"] = "true" if aware else "false"
    get_settings.cache_clear()
    try:
        return action()
    finally:
        if previous is None:
            os.environ.pop("LLM_REQUIREMENT_AWARE_EXCERPT", None)
        else:
            os.environ["LLM_REQUIREMENT_AWARE_EXCERPT"] = previous
        get_settings.cache_clear()


def _variant_jobs(
    connection: Connection, *, limit: int, long_only: bool
) -> list[dict[str, Any]]:
    clause = "and length(coalesce(description, '')) > 4000" if long_only else ""
    rows = connection.execute(
        sa.text(
            f"""
            select id, title, employer, description, location, published_at, expires_at
            from jobs
            where status = 'active'
              and coalesce(description, '') <> ''
              {clause}
            order by length(coalesce(description, '')) desc, id
            limit :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


def decisive_sentence_coverage(description: str) -> dict[str, int]:
    """Count decisive requirement sentences kept by each excerpt policy.

    The baseline truncates the description; the requirement-aware variant keeps
    decisive sentences that fall beyond the window. Comparing the two counts
    shows what the variant preserves without any provider call.
    """
    import re

    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", description)
        if sentence.strip()
    ]
    decisive = [
        sentence
        for sentence in sentences
        if any(keyword in sentence.casefold() for keyword in REQUIREMENT_KEYWORDS)
    ]
    baseline = description[:4000]
    variant = requirement_aware_excerpt(description, limit=4000)
    return {
        "decisive_sentences": len(decisive),
        "kept_by_baseline": sum(1 for sentence in decisive if sentence in baseline),
        "kept_by_variant": sum(1 for sentence in decisive if sentence in variant),
    }


def _rules_summary(profile_summary: str, rules: list[str]) -> str:
    if not rules:
        raise LabelledEvaluationError(
            "--rule is required for the profile-rules variant"
        )
    return f"{profile_summary}\n\nProfiilikohtaiset arviointisäännöt:\n" + "\n".join(
        f"- {rule}" for rule in rules
    )


def run_prompt_variant_benchmark(
    *,
    variant: str,
    limit: int,
    budget: int,
    allow_paid_calls: bool,
    rules: list[str],
) -> dict[str, Any]:
    _require_paid_authorization(allow_paid_calls, budget, expected_calls=2 * limit)
    settings = get_settings()
    provider = build_evaluation_provider(settings)
    if provider is None:
        raise LabelledEvaluationError(
            "no evaluation provider is configured (or it is in cooldown)"
        )
    model = configured_eval_model(settings, provider.provider_name)
    engine = get_engine()
    with engine.connect() as connection:
        profile_id, profile = _profile_document(connection)
        profile_summary, learned_input = evaluation_request_context(profile)
        jobs = _variant_jobs(connection, limit=limit, long_only=variant == "excerpt")
    if not jobs:
        raise LabelledEvaluationError("no jobs with a usable description found")

    def _excerpt_summary(job: dict[str, Any], *, aware: bool) -> str:
        return _with_excerpt_setting(aware, lambda: job_summary(job))

    if variant == "excerpt":
        variant_summary = profile_summary

        def build_variant(job: dict[str, Any]) -> str:
            return _excerpt_summary(job, aware=True)

        def build_baseline(job: dict[str, Any]) -> str:
            return _excerpt_summary(job, aware=False)

    elif variant == "profile-rules":
        variant_summary = _rules_summary(profile_summary, rules)

        def build_variant(job: dict[str, Any]) -> str:
            return job_summary(job)

        def build_baseline(job: dict[str, Any]) -> str:
            return job_summary(job)

    else:
        raise LabelledEvaluationError(f"unknown variant {variant!r}")

    calls = 0
    results: list[dict[str, Any]] = []
    for job in jobs:
        baseline_job_text = build_baseline(job)
        variant_job_text = build_variant(job)
        baseline_summary = profile_summary
        baseline_started = time.monotonic()
        baseline_evaluation, baseline_meta = provider.evaluate_job_fit(
            profile_summary=baseline_summary,
            job_summary=baseline_job_text,
            model=model,
            prompt_version=settings.llm_prompt_version,
        )
        baseline_elapsed = time.monotonic() - baseline_started
        variant_started = time.monotonic()
        variant_evaluation, variant_meta = provider.evaluate_job_fit(
            profile_summary=variant_summary,
            job_summary=variant_job_text,
            model=model,
            prompt_version=settings.llm_prompt_version,
        )
        variant_elapsed = time.monotonic() - variant_started
        calls += 2
        if calls > budget:
            raise LabelledEvaluationError(
                f"benchmark exceeded the declared budget of {budget} paid calls"
            )
        baseline_hash = evaluation_request_hash(
            profile_summary=baseline_summary,
            job_summary_text=baseline_job_text,
            model=model,
            provider=provider.provider_name,
            prompt_version=settings.llm_prompt_version,
            job_id=int(job["id"]),
            profile_id=profile_id,
            learned_input=learned_input or None,
        )
        variant_hash = evaluation_request_hash(
            profile_summary=variant_summary,
            job_summary_text=variant_job_text,
            model=model,
            provider=provider.provider_name,
            prompt_version=settings.llm_prompt_version,
            job_id=int(job["id"]),
            profile_id=profile_id,
            learned_input=learned_input or None,
        )
        results.append(
            {
                "job_id": int(job["id"]),
                "description_chars": len(str(job.get("description") or "")),
                "decisive_sentences": decisive_sentence_coverage(
                    str(job.get("description") or "")
                )
                if variant == "excerpt"
                else None,
                "baseline": {
                    "score": baseline_evaluation.score,
                    "action": baseline_evaluation.suggested_action,
                    "tier": baseline_evaluation.fit_tier,
                    "elapsed_seconds": round(baseline_elapsed, 2),
                    "input_tokens": normalize_provider_usage(
                        provider.provider_name, baseline_meta.get("usage")
                    )["input_tokens"],
                    "output_tokens": normalize_provider_usage(
                        provider.provider_name, baseline_meta.get("usage")
                    )["output_tokens"],
                    "request_hash": baseline_hash,
                },
                "variant": {
                    "score": variant_evaluation.score,
                    "action": variant_evaluation.suggested_action,
                    "tier": variant_evaluation.fit_tier,
                    "elapsed_seconds": round(variant_elapsed, 2),
                    "input_tokens": normalize_provider_usage(
                        provider.provider_name, variant_meta.get("usage")
                    )["input_tokens"],
                    "output_tokens": normalize_provider_usage(
                        provider.provider_name, variant_meta.get("usage")
                    )["output_tokens"],
                    "request_hash": variant_hash,
                },
                "same_action": baseline_evaluation.suggested_action
                == variant_evaluation.suggested_action,
                "same_tier": baseline_evaluation.fit_tier
                == variant_evaluation.fit_tier,
                "identity_changed": baseline_hash != variant_hash,
            }
        )

    def _sum(field: str, arm: str) -> int | None:
        values = [entry[arm][field] for entry in results]
        if any(value is None for value in values):
            return None
        return int(sum(values))

    baseline_input = _sum("input_tokens", "baseline")
    variant_input = _sum("input_tokens", "variant")
    return {
        "mode": "prompt-variant",
        "variant": variant,
        "database": _database_label(),
        "provider": provider.provider_name,
        "model": model,
        "jobs": len(results),
        "budget": budget,
        "paid_calls": calls,
        "baseline_totals": {
            "elapsed_seconds": round(
                sum(entry["baseline"]["elapsed_seconds"] for entry in results), 2
            ),
            "input_tokens": baseline_input,
            "output_tokens": _sum("output_tokens", "baseline"),
        },
        "variant_totals": {
            "elapsed_seconds": round(
                sum(entry["variant"]["elapsed_seconds"] for entry in results), 2
            ),
            "input_tokens": variant_input,
            "output_tokens": _sum("output_tokens", "variant"),
        },
        "input_token_change": (
            None
            if baseline_input is None or variant_input is None
            else variant_input - baseline_input
        ),
        "decision_parity": {
            "same_action": sum(1 for entry in results if entry["same_action"]),
            "same_tier": sum(1 for entry in results if entry["same_tier"]),
            "jobs": len(results),
        },
        "identity_changed": all(entry["identity_changed"] for entry in results),
        "per_job": results,
    }


def run_model_compare_benchmark(
    *,
    limit: int,
    budget: int,
    allow_paid_calls: bool,
    candidate_provider: str,
    candidate_model: str,
    baseline_provider: str | None = None,
    baseline_model: str | None = None,
    prices: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Compare the configured evaluator with a candidate model on fixed jobs.

    Both models receive the identical sanitized prompts, so the report shows
    decision agreement, measured token use, latency and per-call cost. Prices
    are passed in explicitly (USD per million tokens) instead of being guessed.
    """
    _require_paid_authorization(allow_paid_calls, budget, expected_calls=2 * limit)
    settings = get_settings()
    baseline_provider = (baseline_provider or settings.llm_provider).lower()
    baseline_model = baseline_model or configured_eval_model(
        settings, baseline_provider
    )
    prices = prices or {}
    engine = get_engine()
    with engine.connect() as connection:
        profile_id, profile = _profile_document(connection)
        profile_summary, learned_input = evaluation_request_context(profile)
        jobs = _variant_jobs(connection, limit=limit, long_only=True)
    if not jobs:
        raise LabelledEvaluationError("no jobs with a usable description found")

    baseline_client = provider_for(baseline_provider, settings)
    candidate_client = provider_for(candidate_provider, settings)

    calls = 0
    results: list[dict[str, Any]] = []
    for job in jobs:
        job_text = job_summary(job)
        baseline_started = time.monotonic()
        baseline_evaluation, baseline_meta = baseline_client.evaluate_job_fit(
            profile_summary=profile_summary,
            job_summary=job_text,
            model=baseline_model,
            prompt_version=settings.llm_prompt_version,
        )
        baseline_elapsed = time.monotonic() - baseline_started
        candidate_started = time.monotonic()
        candidate_evaluation, candidate_meta = candidate_client.evaluate_job_fit(
            profile_summary=profile_summary,
            job_summary=job_text,
            model=candidate_model,
            prompt_version=settings.llm_prompt_version,
        )
        candidate_elapsed = time.monotonic() - candidate_started
        calls += 2
        if calls > budget:
            raise LabelledEvaluationError(
                f"benchmark exceeded the declared budget of {budget} paid calls"
            )
        baseline_usage = normalize_provider_usage(
            baseline_provider, baseline_meta.get("usage")
        )
        candidate_usage = normalize_provider_usage(
            candidate_provider, candidate_meta.get("usage")
        )
        results.append(
            {
                "job_id": int(job["id"]),
                "description_chars": len(str(job.get("description") or "")),
                "baseline": {
                    "score": baseline_evaluation.score,
                    "action": baseline_evaluation.suggested_action,
                    "tier": baseline_evaluation.fit_tier,
                    "elapsed_seconds": round(baseline_elapsed, 2),
                    "input_tokens": baseline_usage["input_tokens"],
                    "output_tokens": baseline_usage["output_tokens"],
                    "returned_model": baseline_meta.get("returned_model"),
                },
                "candidate": {
                    "score": candidate_evaluation.score,
                    "action": candidate_evaluation.suggested_action,
                    "tier": candidate_evaluation.fit_tier,
                    "elapsed_seconds": round(candidate_elapsed, 2),
                    "input_tokens": candidate_usage["input_tokens"],
                    "output_tokens": candidate_usage["output_tokens"],
                    "returned_model": candidate_meta.get("returned_model"),
                },
                "score_gap": abs(
                    baseline_evaluation.score - candidate_evaluation.score
                ),
                "same_action": baseline_evaluation.suggested_action
                == candidate_evaluation.suggested_action,
                "same_tier": baseline_evaluation.fit_tier
                == candidate_evaluation.fit_tier,
            }
        )

    def _totals(arm: str) -> dict[str, Any]:
        def _sum(field: str) -> int | None:
            values = [entry[arm][field] for entry in results]
            if any(value is None for value in values):
                return None
            return int(sum(values))

        input_tokens = _sum("input_tokens")
        output_tokens = _sum("output_tokens")
        price = prices.get(arm) or {}
        cost = None
        if input_tokens is not None and output_tokens is not None and price:
            cost = round(
                input_tokens / 1_000_000 * float(price.get("input", 0))
                + output_tokens / 1_000_000 * float(price.get("output", 0)),
                6,
            )
        return {
            "elapsed_seconds": round(
                sum(entry[arm]["elapsed_seconds"] for entry in results), 2
            ),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
            "cost_per_call_usd": (
                round(cost / len(results), 6) if cost is not None and results else None
            ),
            "projected_run_cost_usd": (
                round(cost / len(results) * settings.llm_eval_max_jobs, 4)
                if cost is not None and results
                else None
            ),
        }

    return {
        "mode": "model-compare",
        "database": _database_label(),
        "baseline": {
            "provider": baseline_provider,
            "model": baseline_model,
            **(_totals("baseline")),
        },
        "candidate": {
            "provider": candidate_provider,
            "model": candidate_model,
            **(_totals("candidate")),
        },
        "prices_usd_per_million": prices,
        "jobs": len(results),
        "budget": budget,
        "paid_calls": calls,
        "decision_parity": {
            "same_action": sum(1 for entry in results if entry["same_action"]),
            "same_tier": sum(1 for entry in results if entry["same_tier"]),
            "jobs": len(results),
            "mean_abs_score_gap": round(
                sum(entry["score_gap"] for entry in results) / len(results), 1
            ),
        },
        "run_cap_jobs": settings.llm_eval_max_jobs,
        "per_job": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded paid pipeline benchmarks.")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["throughput", "prompt-variant", "model-compare"],
    )
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--budget", type=int, default=24)
    parser.add_argument(
        "--variant", default="excerpt", choices=["excerpt", "profile-rules"]
    )
    parser.add_argument("--rule", action="append", default=[])
    parser.add_argument("--candidate-provider", default="gemini")
    parser.add_argument("--candidate-model", default="gemini-3.5-flash-lite")
    parser.add_argument("--baseline-provider", default=None)
    parser.add_argument("--baseline-model", default=None)
    parser.add_argument(
        "--baseline-price",
        default=None,
        help="Baseline price as INPUT/OUTPUT USD per million tokens, e.g. 0.20/1.25.",
    )
    parser.add_argument(
        "--candidate-price",
        default=None,
        help="Candidate price as INPUT/OUTPUT USD per million tokens, e.g. 0.30/2.50.",
    )
    parser.add_argument("--allow-paid-calls", action="store_true")
    parser.add_argument("--database-is-disposable", action="store_true")
    parser.add_argument("--out", default=None, help="Optional JSON report destination.")
    args = parser.parse_args()

    def _price(value: str | None) -> dict[str, float] | None:
        if not value:
            return None
        input_price, _, output_price = value.partition("/")
        return {"input": float(input_price), "output": float(output_price)}

    if args.mode == "model-compare":
        report = run_model_compare_benchmark(
            limit=args.limit,
            budget=args.budget,
            allow_paid_calls=args.allow_paid_calls,
            candidate_provider=args.candidate_provider,
            candidate_model=args.candidate_model,
            baseline_provider=args.baseline_provider,
            baseline_model=args.baseline_model,
            prices={
                "baseline": _price(args.baseline_price) or {},
                "candidate": _price(args.candidate_price) or {},
            },
        )
    elif args.mode == "throughput":
        report = run_throughput_benchmark(
            limit=args.limit,
            concurrency=args.concurrency,
            budget=args.budget,
            allow_paid_calls=args.allow_paid_calls,
            database_is_disposable=args.database_is_disposable,
        )
    else:
        report = run_prompt_variant_benchmark(
            variant=args.variant,
            limit=args.limit,
            budget=args.budget,
            allow_paid_calls=args.allow_paid_calls,
            rules=list(args.rule),
        )
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
