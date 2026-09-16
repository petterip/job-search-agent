"""Simulated relevance labelling for the private evaluation sample (P2-23).

The real P2-23 labels are the job seeker's own relevance judgements. When no
human labels exist yet, this tool fills an unlabelled sample with a **simulated
reviewer**: a hosted LLM that reads the seeker's own sanitized summary and one
job ad and answers whether the seeker would genuinely want that job. The output
records that provenance explicitly, so a report built from it can never be
mistaken for human-labelled evidence.

Design rules:

* the reviewer prompt is deliberately *not* the pipeline evaluation prompt, and
  the stratum is never disclosed, so the labels are not a copy of the decision
  being measured;
* only sanitized outbound text is sent (the same privacy projection as the
  evaluation path);
* every call is bounded by an explicit budget, and the tool refuses to start
  when the sample has more unique jobs than the budget allows;
* the destination follows the same private-path policy as the sample writer and
  an existing labels file is never replaced without ``--force``.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.db import get_engine
from app.llm import configured_eval_model, job_summary, normalize_provider_usage
from app.labelled_evaluation import (
    LabelledEvaluationError,
    resolve_private_destination,
    write_json_exclusive,
)
from app.matching import evaluation_request_context

LABELER_NAME = "simulated_llm_reviewer"
LABELER_CAVEAT = (
    "Relevance labels were assigned by a hosted LLM acting as a stand-in reviewer, "
    "not by the job seeker. They are provisional evidence only; real user labels "
    "supersede them and any enable/disable decision based on them must be revisited."
)

LABEL_INSTRUCTIONS = (
    "You are role-playing a specific job seeker to review job advertisements. "
    "You are given the seeker's own description of what they want and one job "
    "advertisement. Decide whether this is a job the seeker would genuinely want "
    "and be a plausible candidate for. Judge fit with the seeker's own goals, "
    "qualifications and constraints. Do not reward a job merely for being nearby, "
    "well known, or easy to apply to, and do not reject a job only because it is "
    "outside the seeker's current city if the seeker's summary allows remote or "
    "relocation. Answer with the required JSON only."
)

LABEL_TASK = (
    "Question: would this job seeker genuinely consider this job a relevant "
    "opportunity? Answer relevant=true only when the seeker would plausibly want "
    "to apply, given their stated goals and constraints."
)

LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["relevant", "confidence", "reason"],
    "additionalProperties": False,
}


def _job_row(connection: Connection, job_ids: list[int]) -> dict[int, dict[str, Any]]:
    rows = connection.execute(
        sa.text(
            """
            select id, title, employer, description, location, published_at, expires_at
            from jobs
            where id = any(:job_ids)
            """
        ),
        {"job_ids": job_ids},
    ).mappings()
    return {int(row["id"]): dict(row) for row in rows}


def _label_prompt(profile_summary: str, job_text: str) -> str:
    return (
        f"Hakijan kuvaus:\n{profile_summary}\n\n"
        f"Työpaikkailmoitus:\n{job_text}\n\n{LABEL_TASK}"
    )


def _parse_label(payload: Any) -> tuple[bool, int, str]:
    if not isinstance(payload, dict):
        raise ValueError("label response must be a JSON object")
    relevant = payload.get("relevant")
    if not isinstance(relevant, bool):
        raise ValueError("label response must contain a boolean 'relevant'")
    try:
        confidence = int(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    reason = str(payload.get("reason") or "").strip()
    return relevant, max(0, min(100, confidence)), reason[:240]


def label_with_openai(
    *,
    api_key: str,
    model: str,
    prompt: str,
    timeout_seconds: int = 180,
) -> tuple[bool, int, str, dict[str, Any]]:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, max_retries=2)
    response = client.responses.create(
        model=model,
        instructions=LABEL_INSTRUCTIONS,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "relevance_label",
                "strict": True,
                "schema": LABEL_SCHEMA,
            }
        },
        store=False,
        timeout=timeout_seconds,
    )
    relevant, confidence, reason = _parse_label(json.loads(response.output_text))
    usage = getattr(response, "usage", None)
    return (
        relevant,
        confidence,
        reason,
        {
            "returned_model": getattr(response, "model", None),
            "request_id": getattr(response, "id", None),
            "usage": normalize_provider_usage("openai", usage),
        },
    )


def label_with_gemini(
    *,
    api_key: str,
    model: str,
    prompt: str,
) -> tuple[bool, int, str, dict[str, Any]]:
    body = {
        "contents": [{"parts": [{"text": f"{LABEL_INSTRUCTIONS}\n\n{prompt}"}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": LABEL_SCHEMA,
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    with httpx.Client(timeout=60) as client:
        response = client.post(
            url,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json=body,
        )
        response.raise_for_status()
    payload = response.json()
    raw_text = str(payload["candidates"][0]["content"]["parts"][0]["text"])
    relevant, confidence, reason = _parse_label(json.loads(raw_text))
    return (
        relevant,
        confidence,
        reason,
        {
            "returned_model": payload.get("modelVersion") or model,
            "request_id": payload.get("responseId"),
            "usage": normalize_provider_usage("gemini", payload.get("usageMetadata")),
        },
    )


def label_job(
    *,
    provider_name: str,
    api_key: str,
    model: str,
    prompt: str,
) -> tuple[bool, int, str, dict[str, Any]]:
    if provider_name == "gemini":
        return label_with_gemini(api_key=api_key, model=model, prompt=prompt)
    return label_with_openai(api_key=api_key, model=model, prompt=prompt)


def run_simulated_review(
    labels_path: Path,
    *,
    output_path: Path,
    budget: int,
    provider_name: str | None = None,
    model: str | None = None,
    overwrite: bool = False,
    sleep_seconds: float = 0.0,
) -> dict[str, Any]:
    settings = get_settings()
    provider_name = (provider_name or settings.llm_provider or "openai").lower()
    model = model or configured_eval_model(settings, provider_name)

    destination = resolve_private_destination(output_path)
    if destination.exists() and not overwrite:
        # Fail before spending paid calls on a file that cannot be written.
        raise LabelledEvaluationError(
            f"{destination} already exists; refusing to overwrite a sample or its human labels "
            "(pass --force to replace it)"
        )
    payload = json.loads(labels_path.read_text(encoding="utf-8"))
    labels = payload.get("labels")
    if not isinstance(labels, list) or not labels:
        raise LabelledEvaluationError("labels template contains no labels")
    unique_job_ids = sorted(
        {
            int(entry["job_id"])
            for entry in labels
            if isinstance(entry, dict) and entry.get("job_id") is not None
        }
    )
    if not unique_job_ids:
        raise LabelledEvaluationError("labels template contains no job ids")
    # Budget is checked before the key so a mis-sized sample fails for the right
    # reason even when no provider is configured yet.
    if len(unique_job_ids) > budget:
        raise LabelledEvaluationError(
            f"sample has {len(unique_job_ids)} unique jobs but the budget allows "
            f"{budget} paid label calls; raise --budget or lower --per-stratum"
        )
    api_key = (
        settings.gemini_api_key
        if provider_name == "gemini"
        else settings.openai_api_key
    )
    if not api_key:
        raise LabelledEvaluationError(
            f"no API key configured for provider {provider_name!r}; cannot simulate review"
        )

    engine = get_engine()
    with engine.connect() as connection:
        profile_row = (
            connection.execute(
                sa.text(
                    "select id, profile from job_seeker_profiles order by id limit 1"
                )
            )
            .mappings()
            .one_or_none()
        )
        if profile_row is None:
            raise LabelledEvaluationError("no profile found")
        profile = dict(profile_row["profile"])
        profile_summary, _learned = evaluation_request_context(profile)
        jobs = _job_row(connection, unique_job_ids)

    results: dict[int, dict[str, Any]] = {}
    calls = 0
    started = time.monotonic()
    for job_id in unique_job_ids:
        row = jobs.get(job_id)
        if row is None:
            raise LabelledEvaluationError(
                f"job {job_id} from the sample no longer exists"
            )
        job_text = job_summary(row)
        prompt = _label_prompt(profile_summary, job_text)
        relevant, confidence, reason, metadata = label_job(
            provider_name=provider_name,
            api_key=api_key,
            model=model,
            prompt=prompt,
        )
        calls += 1
        results[job_id] = {
            "relevant": relevant,
            "confidence": confidence,
            "reason": reason,
            "model": metadata.get("returned_model") or model,
        }
        if sleep_seconds:
            time.sleep(sleep_seconds)

    for entry in labels:
        if not isinstance(entry, dict) or entry.get("job_id") is None:
            continue
        result = results[int(entry["job_id"])]
        entry["relevant"] = result["relevant"]
        entry["source"] = LABELER_NAME
        entry["label_confidence"] = result["confidence"]
        entry["label_reason"] = result["reason"]

    payload["labeler"] = LABELER_NAME
    payload["labeler_provider"] = provider_name
    payload["labeler_model"] = model
    payload["labeler_caveat"] = LABELER_CAVEAT
    payload["label_calls"] = calls
    payload["unlabelled"] = False
    write_json_exclusive(destination, payload, overwrite=overwrite)
    labelled = [entry for entry in labels if isinstance(entry, dict)]
    return {
        "path": str(destination),
        "unique_jobs": len(unique_job_ids),
        "labels": len(labelled),
        "relevant": sum(1 for entry in labelled if entry.get("relevant") is True),
        "calls": calls,
        "provider": provider_name,
        "model": model,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "caveat": LABELER_CAVEAT,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill an unlabelled private sample with a simulated LLM reviewer."
    )
    parser.add_argument("--labels", required=True, help="Unlabelled sample template.")
    parser.add_argument("--out", required=True, help="Destination labelled file.")
    parser.add_argument("--budget", type=int, default=120)
    parser.add_argument("--provider", default=None, choices=["openai", "gemini"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.0)
    args = parser.parse_args()
    summary = run_simulated_review(
        Path(args.labels),
        output_path=Path(args.out),
        budget=args.budget,
        provider_name=args.provider,
        model=args.model,
        overwrite=args.force,
        sleep_seconds=args.sleep,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
