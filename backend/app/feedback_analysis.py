import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

import httpx
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Connection

from app.config import Settings, get_settings
from app.db import get_engine
from app.llm import (
    EvaluationProviderUnavailable,
    configured_eval_model,
    gemini_response_schema,
    job_summary,
    minimized_profile_summary,
    normalize_provider_usage,
)
from app.matching import expanded_tokens, token_variants
from app.privacy import (
    PrivacyConfigurationError,
    collect_forbidden_values,
    outbound_llm_allowed,
    redact_sensitive_text,
    sanitize_feedback_snapshot_fields,
)

logger = logging.getLogger("matcher.feedback_analysis")

FEEDBACK_ANALYSIS_PROMPT_VERSION = 1

SystemAlignment = Literal["aligned", "over_ranked", "under_ranked", "mixed", "unclear"]
AnalysisConfidence = Literal["high", "medium", "low"]

EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?:\+?\d[\d\s().-]{6,}\d)")
PROPER_NAME_TOKEN = re.compile(r"^[A-ZÅÄÖ][a-zåäö'-]{2,}$")

FEEDBACK_ANALYSIS_INSTRUCTIONS = (
    "Olet järjestelmäanalyytikko. Arvioi, miksi käyttäjä on todennäköisesti antanut tämän arvion "
    "suhteessa siihen, miten järjestelmä suositteli työpaikkaa. "
    "Muodosta hypoteesi vain annettujen faktojen perusteella. Älä keksi pätevyyksiä, "
    "preferenssejä tai historiaa, joita syötteissä ei ole. "
    "Käyttäjän numeerinen arvio on aina auktoritatiivinen totuuslähde. "
    "Palauta vain skeeman mukainen JSON."
)

FEEDBACK_ANALYSIS_TASK = (
    "Vertaa käyttäjän arviota, mahdollista kommenttia ja haettu-merkintää järjestelmän "
    "pisteisiin, perusteluihin ja match-evidenssiin. Luokittele system_alignment, "
    "kirjoita lyhyt suomenkielinen hypothesis_fi, tunnista todennäköiset positiiviset ja "
    "negatiiviset signaalit, mismatch_drivers ja suggested_actions. "
    "Arvio 3 = kalibrointi, ei voimakkaita boost/exclude-ehdotuksia."
)


class MismatchDriver(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver: str = Field(max_length=64)
    detail: str = Field(max_length=240)


class SuggestedActions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boost_terms_fi: list[str] = Field(default_factory=list, max_length=20)
    exclude_terms_fi: list[str] = Field(default_factory=list, max_length=20)
    boost_sectors: list[str] = Field(default_factory=list, max_length=10)
    exclude_sectors: list[str] = Field(default_factory=list, max_length=10)
    discovery_queries_add: list[str] = Field(default_factory=list, max_length=10)
    discovery_queries_remove: list[str] = Field(default_factory=list, max_length=10)
    lane_notes: str = Field(default="", max_length=500)
    llm_eval_hint: str = Field(default="", max_length=500)


class FeedbackAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_rating: int = Field(ge=1, le=5)
    applied: bool
    system_alignment: SystemAlignment
    hypothesis_fi: str = Field(max_length=1000)
    likely_positive_signals: list[str] = Field(default_factory=list, max_length=20)
    likely_negative_signals: list[str] = Field(default_factory=list, max_length=20)
    mismatch_drivers: list[MismatchDriver] = Field(default_factory=list, max_length=5)
    suggested_actions: SuggestedActions
    confidence: AnalysisConfidence


class FeedbackAnalysisProvider(Protocol):
    provider_name: str

    def analyze_feedback(
        self,
        *,
        profile_summary: str,
        job_summary_text: str,
        feedback_context: str,
        model: str,
        prompt_version: int,
    ) -> tuple[FeedbackAnalysis, dict[str, Any]]:
        ...


def feedback_analysis_cooldown_path(settings: Settings) -> Path:
    return Path(settings.storage_dir) / "llm_feedback_analysis_unavailable.json"


def feedback_analysis_in_cooldown(settings: Settings) -> bool:
    path = feedback_analysis_cooldown_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        provider = str(payload["provider"])
        failed_at = datetime.fromisoformat(str(payload["failed_at"]))
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return False
    if provider != settings.llm_provider:
        return False
    if failed_at.tzinfo is None:
        failed_at = failed_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < failed_at + timedelta(
        minutes=settings.llm_provider_failure_cooldown_min,
    )


def mark_feedback_analysis_unavailable(settings: Settings, reason: str) -> None:
    path = feedback_analysis_cooldown_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "provider": settings.llm_provider,
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _redact_personal_names(text: str, allowed_name_tokens: set[str]) -> str:
    words = re.findall(r"[A-ZÅÄÖa-zåäö'-]+", text)
    redacted = text
    for word in words:
        if not PROPER_NAME_TOKEN.match(word):
            continue
        if word.casefold() in allowed_name_tokens:
            continue
        redacted = re.sub(rf"\b{re.escape(word)}\b", "[nimi poistettu]", redacted)
    return redacted


def sanitize_feedback_comment(
    comment: str | None,
    *,
    profile: dict[str, Any] | None = None,
) -> str | None:
    if comment is None:
        return None
    text = comment.strip()
    if not text:
        return None
    text = EMAIL_PATTERN.sub("[sähköposti poistettu]", text)
    text = PHONE_PATTERN.sub("[puhelin poistettu]", text)
    allowed_name_tokens: set[str] = set()
    if profile:
        for key in ("objective", "career_evidence", "llm_guidance"):
            allowed_name_tokens.update(expanded_tokens(str(profile.get(key) or "")))
        preferences = profile.get("preferences")
        if isinstance(preferences, dict):
            allowed_name_tokens.update(expanded_tokens(json.dumps(preferences, ensure_ascii=False)))
    text = _redact_personal_names(text, allowed_name_tokens)
    return text[:1000] if text else None


def snapshot_dict(scoring_snapshot: Any) -> dict[str, Any]:
    if isinstance(scoring_snapshot, dict):
        return scoring_snapshot
    if isinstance(scoring_snapshot, str):
        return json.loads(scoring_snapshot)
    return {}


def feedback_content_hash(
    *,
    rating: int,
    applied: bool,
    comment: str | None,
    scoring_snapshot: Any,
) -> str:
    payload = {
        "rating": rating,
        "applied": applied,
        "comment": comment,
        "scoring_snapshot": snapshot_dict(scoring_snapshot),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def analysis_request_hash(
    *,
    profile_summary: str,
    job_summary_text: str,
    scoring_snapshot: Any,
    rating: int,
    applied: bool,
    sanitized_comment: str | None,
    prompt_version: int,
    provider: str = "",
    model: str = "",
    feedback_id: int | None = None,
    job_id: int | None = None,
    schema: dict[str, Any] | None = None,
    instructions: str | None = None,
    task: str | None = None,
) -> str:
    payload = {
        "profile_summary": profile_summary,
        "job_summary": job_summary_text,
        "scoring_snapshot": snapshot_dict(scoring_snapshot),
        "rating": rating,
        "applied": applied,
        "sanitized_comment": sanitized_comment,
        "prompt_version": prompt_version,
        "provider": provider,
        "model": model,
        "feedback_id": feedback_id,
        "job_id": job_id,
        "schema": schema if schema is not None else FeedbackAnalysis.model_json_schema(),
        "instructions": (
            FEEDBACK_ANALYSIS_INSTRUCTIONS if instructions is None else instructions
        ),
        "task": FEEDBACK_ANALYSIS_TASK if task is None else task,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def grounded_text_fields(snapshot: dict[str, Any], profile_summary: str) -> set[str]:
    parts = [
        snapshot.get("title"),
        snapshot.get("employer"),
        snapshot.get("location"),
        snapshot.get("rationale"),
        " ".join(snapshot.get("concerns") or []),
        " ".join(snapshot.get("title_matches") or []),
        " ".join(snapshot.get("keyword_matches") or []),
        " ".join(snapshot.get("sector_matches") or []),
        " ".join(snapshot.get("negative_matches") or []),
        profile_summary,
    ]
    grounded: set[str] = set()
    for part in parts:
        grounded.update(expanded_tokens(str(part or "")))
    return grounded


def is_term_grounded(term: str, *, grounded_tokens: set[str]) -> bool:
    term_tokens = expanded_tokens(term)
    if not term_tokens:
        return False
    for token in term_tokens:
        variants = token_variants(token)
        if variants & grounded_tokens:
            return True
    return False


def apply_grounding_guard(
    analysis: FeedbackAnalysis,
    *,
    snapshot: dict[str, Any],
    profile_summary: str,
) -> FeedbackAnalysis:
    grounded_tokens = grounded_text_fields(snapshot, profile_summary)
    actions = analysis.suggested_actions.model_copy(deep=True)
    filtered_boosts: list[str] = []
    filtered_excludes: list[str] = []
    for term in actions.boost_terms_fi:
        if is_term_grounded(term, grounded_tokens=grounded_tokens):
            filtered_boosts.append(term)
        else:
            logger.info("event=analysis_term_ungrounded bucket=boost term=%s", term)
    for term in actions.exclude_terms_fi:
        if is_term_grounded(term, grounded_tokens=grounded_tokens):
            filtered_excludes.append(term)
        else:
            logger.info("event=analysis_term_ungrounded bucket=exclude term=%s", term)
    actions.boost_terms_fi = filtered_boosts
    actions.exclude_terms_fi = filtered_excludes
    filtered_boost_sectors: list[str] = []
    filtered_exclude_sectors: list[str] = []
    for sector in actions.boost_sectors:
        if is_term_grounded(sector, grounded_tokens=grounded_tokens):
            filtered_boost_sectors.append(sector)
        else:
            logger.info("event=analysis_term_ungrounded bucket=boost_sector term=%s", sector)
    for sector in actions.exclude_sectors:
        if is_term_grounded(sector, grounded_tokens=grounded_tokens):
            filtered_exclude_sectors.append(sector)
        else:
            logger.info("event=analysis_term_ungrounded bucket=exclude_sector term=%s", sector)
    actions.boost_sectors = filtered_boost_sectors
    actions.exclude_sectors = filtered_exclude_sectors
    return analysis.model_copy(update={"suggested_actions": actions})


def build_feedback_context(
    *,
    rating: int,
    applied: bool,
    sanitized_comment: str | None,
    scoring_snapshot: dict[str, Any],
) -> str:
    payload = {
        "user_rating": rating,
        "applied": applied,
        "sanitized_comment": sanitized_comment,
        "system_scores": {
            "machine_score": scoring_snapshot.get("machine_score"),
            "vector_score": scoring_snapshot.get("vector_score"),
            "llm_score": scoring_snapshot.get("llm_score"),
            "fit_tier": scoring_snapshot.get("fit_tier"),
            "suggested_action": scoring_snapshot.get("suggested_action"),
            "rank": scoring_snapshot.get("rank"),
            "is_active": scoring_snapshot.get("is_active"),
        },
        "match_evidence": {
            "candidate_lanes": scoring_snapshot.get("candidate_lanes"),
            "title_matches": scoring_snapshot.get("title_matches"),
            "keyword_matches": scoring_snapshot.get("keyword_matches"),
            "sector_matches": scoring_snapshot.get("sector_matches"),
            "negative_matches": scoring_snapshot.get("negative_matches"),
            "location_matches": scoring_snapshot.get("location_matches"),
        },
        "rationale": scoring_snapshot.get("rationale"),
        "concerns": scoring_snapshot.get("concerns"),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class OpenAIFeedbackAnalysisProvider:
    provider_name = "openai"

    def __init__(self, api_key: str) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, max_retries=0)

    def analyze_feedback(
        self,
        *,
        profile_summary: str,
        job_summary_text: str,
        feedback_context: str,
        model: str,
        prompt_version: int,
    ) -> tuple[FeedbackAnalysis, dict[str, Any]]:
        schema = FeedbackAnalysis.model_json_schema()
        try:
            response = self.client.responses.create(
                model=model,
                instructions=FEEDBACK_ANALYSIS_INSTRUCTIONS,
                input=(
                    f"Hakijan sallittu yhteenveto:\n{profile_summary}\n\n"
                    f"Työpaikan yhteenveto:\n{job_summary_text}\n\n"
                    f"Palaute ja järjestelmän konteksti:\n{feedback_context}\n\n"
                    + FEEDBACK_ANALYSIS_TASK
                ),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "feedback_analysis",
                        "strict": True,
                        "schema": schema,
                    }
                },
                store=False,
                timeout=60,
            )
        except Exception as exc:
            code = getattr(getattr(exc, "body", None), "get", lambda _key: None)("code")
            if code == "insufficient_quota":
                raise EvaluationProviderUnavailable("openai quota is unavailable") from exc
            raise
        parsed = FeedbackAnalysis.model_validate(json.loads(response.output_text))
        usage = getattr(response, "usage", None)
        metadata = {
            "returned_model": getattr(response, "model", None),
            "prompt_version": prompt_version,
            "usage": usage.model_dump() if usage is not None and hasattr(usage, "model_dump") else usage,
            "usage_normalized": normalize_provider_usage("openai", usage),
            "request_id": getattr(response, "id", None),
            "attempts": 1,
        }
        return parsed, metadata


class GeminiFeedbackAnalysisProvider:
    provider_name = "gemini"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def _post_generate_content(self, *, url: str, body: dict[str, Any]) -> httpx.Response:
        with httpx.Client(timeout=60) as client:
            for attempt in range(3):
                response = client.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": self.api_key,
                    },
                    json=body,
                )
                if response.status_code < 500:
                    self.last_attempts = attempt + 1
                    return response
                if attempt == 2:
                    self.last_attempts = 3
                    return response
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("unreachable Gemini retry state")

    def analyze_feedback(
        self,
        *,
        profile_summary: str,
        job_summary_text: str,
        feedback_context: str,
        model: str,
        prompt_version: int,
    ) -> tuple[FeedbackAnalysis, dict[str, Any]]:
        schema = gemini_response_schema(FeedbackAnalysis.model_json_schema())
        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                f"{FEEDBACK_ANALYSIS_INSTRUCTIONS}\n\n"
                                f"Hakijan sallittu yhteenveto:\n{profile_summary}\n\n"
                                f"Työpaikan yhteenveto:\n{job_summary_text}\n\n"
                                f"Palaute ja järjestelmän konteksti:\n{feedback_context}\n\n"
                                + FEEDBACK_ANALYSIS_TASK
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            response = self._post_generate_content(url=url, body=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            error_payload: dict[str, Any] = {}
            try:
                payload = exc.response.json()
                if isinstance(payload, dict):
                    error_payload = payload
            except ValueError:
                error_payload = {}
            error_status = str(error_payload.get("error", {}).get("status", ""))
            if status_code in {402, 429} or error_status in {"RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"}:
                raise EvaluationProviderUnavailable("gemini quota is unavailable") from exc
            raise
        payload = response.json()
        raw_text = str(payload["candidates"][0]["content"]["parts"][0]["text"])
        parsed = FeedbackAnalysis.model_validate(json.loads(raw_text))
        usage = payload.get("usageMetadata")
        metadata = {
            "returned_model": payload.get("modelVersion") or model,
            "prompt_version": prompt_version,
            "usage": usage,
            "usage_normalized": normalize_provider_usage("gemini", usage),
            "request_id": payload.get("responseId"),
            "attempts": int(getattr(self, "last_attempts", 1)),
        }
        return parsed, metadata


def build_feedback_analysis_provider(settings: Settings) -> FeedbackAnalysisProvider | None:
    if feedback_analysis_in_cooldown(settings):
        logger.warning(
            "event=feedback_analysis_skipped reason=provider_cooldown provider=%s",
            settings.llm_provider,
        )
        return None
    if settings.llm_provider == "openai" and settings.openai_api_key:
        return OpenAIFeedbackAnalysisProvider(api_key=settings.openai_api_key)
    if settings.llm_provider == "gemini" and settings.gemini_api_key:
        return GeminiFeedbackAnalysisProvider(api_key=settings.gemini_api_key)
    return None


def load_profile_for_analysis(connection: Connection) -> dict[str, Any] | None:
    row = connection.execute(
        sa.text(
            """
            select profile
            from job_seeker_profiles
            order by id
            limit 1
            """
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    profile = row["profile"]
    return dict(profile) if isinstance(profile, dict) else json.loads(profile)


def mark_feedback_analysis_status(
    connection: Connection,
    *,
    feedback_id: int,
    status: str,
) -> None:
    connection.execute(
        sa.text(
            """
            update recommendation_feedback
            set analysis_status = :status,
                updated_at = now()
            where id = :feedback_id
            """
        ),
        {"feedback_id": feedback_id, "status": status},
    )


def store_feedback_analysis(
    connection: Connection,
    *,
    feedback_id: int,
    provider_name: str,
    configured_model: str,
    returned_model: str | None,
    prompt_version: int,
    request_hash: str,
    analysis: dict[str, Any],
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
    latency_ms: int | None = None,
    attempts: int = 1,
    outcome: str = "succeeded",
    provider_request_id: str | None = None,
    usage_present: bool = False,
) -> None:
    connection.execute(
        sa.text(
            """
            insert into feedback_llm_analyses (
                feedback_id,
                provider,
                configured_model,
                returned_model,
                prompt_version,
                request_hash,
                analysis,
                input_tokens,
                output_tokens,
                cached_tokens,
                latency_ms,
                attempts,
                outcome,
                provider_request_id,
                usage_present
            )
            values (
                :feedback_id,
                :provider,
                :configured_model,
                :returned_model,
                :prompt_version,
                :request_hash,
                CAST(:analysis AS jsonb),
                :input_tokens,
                :output_tokens,
                :cached_tokens,
                :latency_ms,
                :attempts,
                :outcome,
                :provider_request_id,
                :usage_present
            )
            on conflict (feedback_id)
            do update set
                provider = excluded.provider,
                configured_model = excluded.configured_model,
                returned_model = excluded.returned_model,
                prompt_version = excluded.prompt_version,
                request_hash = excluded.request_hash,
                analysis = excluded.analysis,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                cached_tokens = excluded.cached_tokens,
                latency_ms = excluded.latency_ms,
                attempts = excluded.attempts,
                outcome = excluded.outcome,
                provider_request_id = excluded.provider_request_id,
                usage_present = excluded.usage_present,
                created_at = now()
            """
        ),
        {
            "feedback_id": feedback_id,
            "provider": provider_name,
            "configured_model": configured_model,
            "returned_model": returned_model,
            "prompt_version": prompt_version,
            "request_hash": request_hash,
            "analysis": json.dumps(analysis, ensure_ascii=False),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "latency_ms": latency_ms,
            "attempts": attempts,
            "outcome": outcome,
            "provider_request_id": provider_request_id,
            "usage_present": usage_present,
        },
    )


def reuse_cached_analysis(
    connection: Connection,
    *,
    feedback_id: int,
    request_hash: str,
) -> bool:
    row = connection.execute(
        sa.text(
            """
            select analysis
            from feedback_llm_analyses
            where feedback_id = :feedback_id
              and request_hash = :request_hash
            """
        ),
        {"feedback_id": feedback_id, "request_hash": request_hash},
    ).mappings().one_or_none()
    if row is None:
        return False
    analysis = row["analysis"]
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis)
        except ValueError:
            return False
    try:
        FeedbackAnalysis.model_validate(analysis)
    except Exception:
        logger.warning("event=feedback_analysis_cache_unusable feedback_id=%s", feedback_id)
        return False
    mark_feedback_analysis_status(connection, feedback_id=feedback_id, status="completed")
    return True


def _feedback_analysis_inputs(
    row: dict[str, Any],
    *,
    profile: dict[str, Any],
    provider_name: str,
    model: str,
) -> tuple[dict[str, Any], str | None, str, str, str]:
    snapshot = sanitize_feedback_snapshot_fields(
        snapshot_dict(row["scoring_snapshot"]),
        extra_secrets=collect_forbidden_values(profile),
    )
    sanitized_comment = sanitize_feedback_comment(row.get("comment"), profile=profile)
    if sanitized_comment:
        sanitized_comment = redact_sensitive_text(
            sanitized_comment, extra_secrets=collect_forbidden_values(profile)
        )
    profile_summary = minimized_profile_summary(profile)
    job_summary_text = job_summary(
        {
            "title": row.get("title") or snapshot.get("title"),
            "employer": row.get("employer") or snapshot.get("employer"),
            "location": row.get("location") or snapshot.get("location"),
            "description": row.get("description"),
        }
    )
    request_hash = analysis_request_hash(
        profile_summary=profile_summary,
        job_summary_text=job_summary_text,
        scoring_snapshot=snapshot,
        rating=int(row["rating"]),
        applied=bool(row["applied"]),
        sanitized_comment=sanitized_comment,
        prompt_version=FEEDBACK_ANALYSIS_PROMPT_VERSION,
        provider=provider_name,
        model=model,
        feedback_id=int(row["id"]),
        job_id=int(row["job_id"]) if row.get("job_id") is not None else None,
    )
    return snapshot, sanitized_comment, profile_summary, job_summary_text, request_hash


def load_feedback_row(connection: Connection, *, feedback_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        sa.text(
            """
            select
                rf.id,
                rf.recommendation_id,
                rf.job_id,
                rf.rating,
                rf.applied,
                rf.comment,
                rf.scoring_snapshot,
                rf.updated_at,
                j.title,
                j.employer,
                j.location,
                j.description
            from recommendation_feedback rf
            join jobs j on j.id = rf.job_id
            where rf.id = :feedback_id
            for update of rf
            """
        ),
        {"feedback_id": feedback_id},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _mark_status_if_current(
    connection: Connection,
    *,
    feedback_id: int,
    expected_updated_at: Any,
    status: str,
    reason: str | None = None,
    schedule_retry_at: Any = None,
    count_attempt: bool = False,
) -> bool:
    """Status update that refuses to overwrite a newer user edit.

    A retryable failure keeps the row `pending` with `next_attempt_at` and an
    attempt count; only an explicitly terminal status clears the retry schedule.
    """
    result = connection.execute(
        sa.text(
            """
            update recommendation_feedback
            set analysis_status = :status,
                updated_at = now(),
                analysis_reason = :reason,
                analysis_attempts = analysis_attempts + :attempt_increment,
                next_attempt_at = :next_attempt_at
            where id = :feedback_id
              and updated_at = :expected_updated_at
            """
        ),
        {
            "feedback_id": feedback_id,
            "status": status,
            "reason": reason,
            "attempt_increment": 1 if count_attempt else 0,
            "next_attempt_at": schedule_retry_at,
            "expected_updated_at": expected_updated_at,
        },
    )
    return bool(getattr(result, "rowcount", 1))


def analyze_feedback_row(
    row: dict[str, Any],
    *,
    provider: FeedbackAnalysisProvider,
    settings: Settings,
    profile: dict[str, Any],
    engine: Any,
) -> str:
    """Analyze one feedback row without holding a transaction over the call.

    Provider calls happen outside any write transaction; publication re-reads
    the feedback input and only writes when it is unchanged, so an edit during
    the call cannot be overwritten by the older analysis.
    """
    feedback_id = int(row["id"])
    model = configured_eval_model(settings, provider.provider_name)
    snapshot, sanitized_comment, profile_summary, job_summary_text, request_hash = (
        _feedback_analysis_inputs(
            row, profile=profile, provider_name=provider.provider_name, model=model
        )
    )
    with engine.begin() as connection:
        if reuse_cached_analysis(connection, feedback_id=feedback_id, request_hash=request_hash):
            logger.info("event=feedback_analysis_reused feedback_id=%s", feedback_id)
            return "reused"

    feedback_context = build_feedback_context(
        rating=int(row["rating"]),
        applied=bool(row["applied"]),
        sanitized_comment=sanitized_comment,
        scoring_snapshot=snapshot,
    )
    started = time.monotonic()
    try:
        analysis, metadata = provider.analyze_feedback(
            profile_summary=profile_summary,
            job_summary_text=job_summary_text,
            feedback_context=feedback_context,
            model=model,
            prompt_version=FEEDBACK_ANALYSIS_PROMPT_VERSION,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
    except EvaluationProviderUnavailable as exc:
        mark_feedback_analysis_unavailable(settings, str(exc))
        attempts = int(row.get("analysis_attempts") or 0) + 1
        retryable = attempts < settings.feedback_analysis_max_attempts
        with engine.begin() as connection:
            stored = _mark_status_if_current(
                connection,
                feedback_id=feedback_id,
                expected_updated_at=row.get("updated_at"),
                status="pending" if retryable else "skipped",
                reason="provider_unavailable" if retryable else "attempt_budget_exhausted",
                schedule_retry_at=(
                    datetime.now(timezone.utc)
                    + timedelta(minutes=settings.feedback_analysis_retry_minutes)
                    if retryable
                    else None
                ),
                count_attempt=True,
            )
        return "deferred" if retryable and stored else ("skipped" if stored else "stale")
    except Exception as exc:
        logger.exception("event=feedback_analysis_failed feedback_id=%s", feedback_id)
        attempts = int(row.get("analysis_attempts") or 0) + 1
        retryable = isinstance(exc, (ValueError, json.JSONDecodeError)) and (
            attempts < settings.feedback_analysis_max_attempts
        )
        with engine.begin() as connection:
            stored = _mark_status_if_current(
                connection,
                feedback_id=feedback_id,
                expected_updated_at=row.get("updated_at"),
                status="pending" if retryable else "failed",
                reason="retryable_response_error" if retryable else "permanent_failure",
                schedule_retry_at=(
                    datetime.now(timezone.utc)
                    + timedelta(minutes=settings.feedback_analysis_retry_minutes)
                    if retryable
                    else None
                ),
                count_attempt=True,
            )
        return "deferred" if retryable and stored else ("failed" if stored else "stale")

    grounded = apply_grounding_guard(
        analysis,
        snapshot=snapshot,
        profile_summary=profile_summary,
    )
    accounting = metadata.get("usage_normalized") or {}
    with engine.begin() as connection:
        current = load_feedback_row(connection, feedback_id=feedback_id)
        if current is None:
            return "stale"
        _snapshot, _comment, _profile, _job, current_hash = _feedback_analysis_inputs(
            current, profile=profile, provider_name=provider.provider_name, model=model
        )
        if current_hash != request_hash:
            logger.info("event=feedback_analysis_stale feedback_id=%s", feedback_id)
            return "stale"
        store_feedback_analysis(
            connection,
            feedback_id=feedback_id,
            provider_name=provider.provider_name,
            configured_model=model,
            returned_model=metadata.get("returned_model"),
            prompt_version=FEEDBACK_ANALYSIS_PROMPT_VERSION,
            request_hash=request_hash,
            analysis=grounded.model_dump(),
            input_tokens=accounting.get("input_tokens"),
            output_tokens=accounting.get("output_tokens"),
            cached_tokens=accounting.get("cached_tokens"),
            latency_ms=latency_ms,
            attempts=int(metadata.get("attempts") or 1),
            provider_request_id=metadata.get("request_id"),
            usage_present=bool(accounting.get("usage_present")),
        )
        if not _mark_status_if_current(
            connection,
            feedback_id=feedback_id,
            expected_updated_at=current.get("updated_at"),
            status="completed",
            reason=None,
            schedule_retry_at=None,
        ):
            logger.info("event=feedback_analysis_stale_on_publish feedback_id=%s", feedback_id)
            return "stale"
    logger.info(
        "event=feedback_analysis_completed feedback_id=%s alignment=%s confidence=%s",
        feedback_id,
        grounded.system_alignment,
        grounded.confidence,
    )
    return "completed"


def pending_feedback_rows(connection: Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        sa.text(
            """
            select
                rf.id,
                rf.recommendation_id,
                rf.job_id,
                rf.rating,
                rf.applied,
                rf.comment,
                rf.scoring_snapshot,
                rf.updated_at,
                rf.analysis_attempts,
                rf.analysis_reason,
                j.title,
                j.employer,
                j.location,
                j.description
            from recommendation_feedback rf
            join jobs j on j.id = rf.job_id
            where rf.analysis_status = 'pending'
              and (rf.next_attempt_at is null or rf.next_attempt_at <= now())
            order by rf.updated_at asc, rf.id asc
            limit :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


def run_analyze_feedback(*, limit: int = 20) -> dict[str, int]:
    settings = get_settings()
    counts = {
        "pending": 0,
        "completed": 0,
        "reused": 0,
        "failed": 0,
        "skipped": 0,
        "stale": 0,
        "deferred": 0,
    }
    engine = get_engine()
    with engine.connect() as connection:
        profile = load_profile_for_analysis(connection)
        if profile is None:
            return counts
        rows = pending_feedback_rows(connection, limit=limit)
    counts["pending"] = len(rows)
    if not rows:
        return counts
    try:
        allowed = outbound_llm_allowed(profile)
    except PrivacyConfigurationError:
        allowed = False
    provider = build_feedback_analysis_provider(settings) if allowed else None
    if provider is None:
        with engine.begin() as connection:
            for row in rows:
                mark_feedback_analysis_status(connection, feedback_id=int(row["id"]), status="skipped")
        counts["skipped"] = len(rows)
        return counts
    for row in rows:
        outcome = analyze_feedback_row(
            row,
            provider=provider,
            settings=settings,
            profile=profile,
            engine=engine,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def drain_pending_feedback_analyses(*, budget_minutes: int | None = None) -> dict[str, int]:
    settings = get_settings()
    budget = budget_minutes if budget_minutes is not None else settings.learner_analysis_drain_budget_minutes
    deadline = datetime.now(timezone.utc) + timedelta(minutes=budget)
    totals = {"completed": 0, "reused": 0, "failed": 0, "skipped": 0, "pending": 0}
    while datetime.now(timezone.utc) < deadline:
        batch = run_analyze_feedback(limit=10)
        totals["pending"] += batch.get("pending", 0)
        for key in ("completed", "reused", "failed", "skipped"):
            totals[key] += batch.get(key, 0)
        if batch.get("pending", 0) == 0:
            break
    return totals
