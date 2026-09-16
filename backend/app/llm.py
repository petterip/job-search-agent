import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.privacy import outbound_profile_summary, sanitize_job_travel, sanitize_outbound

logger = logging.getLogger("matcher.llm")

FitTier = Literal[
    "strong_fit",
    "transferable_weaker",
    "generic_customer_service_only",
    "not_applicable",
]
SuggestedAction = Literal["apply", "consider", "skip"]
ConcernText = Annotated[str, Field(max_length=120)]


class JobFitEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    fit_tier: FitTier
    rationale: str = Field(max_length=360)
    concerns: list[ConcernText] = Field(max_length=3)
    suggested_action: SuggestedAction


class EvaluationProvider(Protocol):
    provider_name: str

    def evaluate_job_fit(
        self,
        *,
        profile_summary: str,
        job_summary: str,
        model: str,
        prompt_version: int,
    ) -> tuple[JobFitEvaluation, dict[str, Any]]: ...


class EvaluationProviderUnavailable(RuntimeError):
    pass


class EvaluationProviderRateLimited(RuntimeError):
    """The provider throttled this run; defer candidates instead of failing them.

    Distinct from ``EvaluationProviderUnavailable``: a throttle is transient and
    must stop dispatch for the current run without persisting a long cooldown
    that a quota/outage failure would justify.
    """


EVALUATION_INSTRUCTIONS = (
    "Olet suomenkielinen työnhakusuositusten arvioija. "
    "Arvioit yhtä työpaikkaa yksittäiselle hakijaprofiilille. "
    "Käytä vain annettuja faktoja. Älä keksi pätevyyksiä, koulutuksia, sertifikaatteja tai kokemusta. "
    "Jos työpaikka edellyttää olennaista osaamista, tutkintoa, sertifikaattia tai toimialakokemusta "
    "eikä sitä löydy hakijan tiedoista, käsittele sitä puuttuvana vaatimuksena. "
    "Jos hakijan tiedoissa on kirjastoalan kelpoisuus, älä merkitse tavallista kirjastoalan 60 op / 35 ov "
    "kelpoisuusvaatimusta puuttuvaksi. "
    "Jos hakijan tiedoissa on ruotsi B2/sujuva, käsittele se riittävänä tavalliseen tyydyttävään tai hyvään "
    "toisen kotimaisen kielen vaatimukseen, ellei ilmoitus vaadi natiivia tai erinomaista tasoa. "
    "Kirjoita tiivistä käyttöliittymätekstiä, ei raporttia. "
    "Rationale: enintään kaksi virkettä. Aloita tärkeimmästä sopivuudesta tai hylkäyssyystä. "
    "Concerns: enintään kolme lyhyttä kohtaa. Älä toista rationale-tekstin sanamuotoja. "
    "Älä aloita huolia toistuvasti samalla fraasilla; käytä suoria muotoja kuten 'SAP-osaaminen puuttuu' "
    "tai 'Sijainti on heikko'. "
    "Ei yleisluontoista täytetekstiä, yhteenvetomaista jaarittelua tai vaatimusten pitkää luettelointia. "
    "Älä käytä metapuhetta hakijan tietolähteistä; kirjoita sen sijaan suoraan 'hakijalta puuttuu ...'. "
    "Jos puuttuva vaatimus on työn keskeinen vaatimus, anna suggested_action='skip' ja pidä score enintään 35. "
    "Paikallinen sijainti ei saa korvata huonoa sisällöllistä sopivuutta: jos paikallinen työ on fyysistä hoiva-, "
    "arkiavun, myynnin buukkauksen tai erikoisalan asiantuntijatyötä ilman selkeää vastaavaa kokemusta, anna "
    "suggested_action='skip' tai score alle 40. "
    "Jos työ on vain heikko tai kaukainen siirrettävä osuma, anna suggested_action='skip' tai score alle 40. "
    "Käytä transferable_weaker + consider vain, kun työn ydintehtävissä on konkreettinen yhteys hakijan "
    "todennettuun kokemukseen eikä keskeinen osaamisvaatimus puutu. "
    "Käytä suggested_action='apply' vain, kun keskeiset vaatimukset, sijainti ja työn sisältö sopivat hyvin. "
    "Palauta vain skeeman mukainen arvio."
)


EVALUATION_TASK = (
    "Arvioi yksi työpaikka suhteessa hakijaprofiilin tietoihin. "
    "Kirjoita rationale ja concerns lukijalle suoraan hakijasta ja työpaikasta."
)


def normalize_provider_usage(provider_name: str, usage: Any) -> dict[str, Any]:
    """Normalize provider token accounting, flagging absent usage.

    Token counts are recorded exactly as the provider reported them. A missing
    usage block stays null with ``usage_present`` false rather than being
    estimated.
    """
    normalized: dict[str, Any] = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "usage_present": False,
    }
    if usage is None:
        return normalized
    if provider_name == "gemini":
        data = usage if isinstance(usage, dict) else {}
        normalized["input_tokens"] = data.get("promptTokenCount")
        normalized["output_tokens"] = data.get("candidatesTokenCount")
        normalized["cached_tokens"] = data.get("cachedContentTokenCount")
    else:
        if hasattr(usage, "model_dump"):
            data = usage.model_dump()
        elif isinstance(usage, dict):
            data = usage
        else:
            data = {}
        normalized["input_tokens"] = data.get("input_tokens")
        normalized["output_tokens"] = data.get("output_tokens")
        details = data.get("input_tokens_details")
        if isinstance(details, dict):
            normalized["cached_tokens"] = details.get("cached_tokens")
    normalized["usage_present"] = (
        normalized["input_tokens"] is not None
        or normalized["output_tokens"] is not None
    )
    return normalized


def provider_cooldown_path(settings: Settings) -> Path:
    return Path(settings.storage_dir) / "llm_provider_unavailable.json"


def provider_in_cooldown(settings: Settings) -> bool:
    path = provider_cooldown_path(settings)
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


def mark_provider_unavailable(settings: Settings, reason: str) -> None:
    path = provider_cooldown_path(settings)
    try:
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
    except OSError:
        logger.warning("event=provider_cooldown_write_failed", exc_info=True)


def minimized_profile_summary(profile: dict[str, Any]) -> str:
    """Privacy-projected profile summary. Fails closed without a valid policy."""
    return outbound_profile_summary(profile)


# Sentences that carry decisive requirements or deadlines are worth keeping
# even when they fall beyond a long description's excerpt window.
REQUIREMENT_KEYWORDS = (
    "edellyt",
    "vaadit",
    "vaatimus",
    "kelpoisuus",
    "tutkinto",
    "lisenssi",
    "sertifikaatti",
    "kielitaito",
    "kielen taito",
    "ajokortti",
    "hakuaika",
    "viimeinen hakupäivä",
    "haku päättyy",
    "deadline",
    "required",
    "requirement",
    "qualification",
    "licence",
    "license",
)


def requirement_aware_excerpt(description: str, *, limit: int = 4000) -> str:
    """Keep the head of a long description plus its decisive requirement lines.

    The returned text never exceeds ``limit``. Requirement/deadline sentences
    that would otherwise fall outside the window are appended at the end, where
    a long listing's decisive facts usually live.
    """
    if len(description) <= limit:
        return description
    head = description[: limit // 2]
    tail_budget = limit - len(head)
    sentences = re.split(r"(?<=[.!?])\s+", description)
    decisive = [
        sentence.strip()
        for sentence in sentences
        if any(keyword in sentence.casefold() for keyword in REQUIREMENT_KEYWORDS)
    ]
    selected: list[str] = []
    used = 0
    for sentence in reversed(decisive):
        if used + len(sentence) + 1 > tail_budget:
            continue
        selected.append(sentence)
        used += len(sentence) + 1
    selected.reverse()
    tail = " ".join(selected)
    if not tail:
        return (head + description[len(head) : len(head) + tail_budget])[:limit]
    return (head + tail)[:limit]


def job_summary(job: dict[str, Any]) -> str:
    description = str(job.get("description") or "")
    if get_settings().llm_requirement_aware_excerpt:
        excerpt = requirement_aware_excerpt(description, limit=4000)
    else:
        excerpt = description[:4000]
    deterministic_result = job.get("deterministic_result") or {}
    if isinstance(deterministic_result, str):
        deterministic_result = json.loads(deterministic_result)
    travel_assessment = sanitize_job_travel(
        deterministic_result.get("travel_assessment")
    )
    minimized = {
        "title": job.get("title"),
        "employer": job.get("employer"),
        "location": job.get("location"),
        "description_excerpt": excerpt,
        "travel_assessment": travel_assessment,
    }
    return json.dumps(sanitize_outbound(minimized), ensure_ascii=False, sort_keys=True)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evaluation_request_hash(
    *,
    profile_summary: str,
    job_summary_text: str,
    model: str,
    prompt_version: int,
    provider: str = "",
    schema_version: int = 1,
    schema: dict[str, Any] | None = None,
    instructions: str | None = None,
    task: str | None = None,
    job_id: int | None = None,
    profile_id: int | None = None,
    learned_input: Any = None,
) -> str:
    """Fingerprint the exact sanitized evaluation request.

    The hash covers the actual prompt/schema content sent, the configured
    provider and model, and the job/profile ownership. Readable version numbers
    stay in the payload as provenance, but they are never the only thing that
    makes a request distinct: changing instructions or schema content changes
    the identity even at a constant version. `returned_model` is deliberately
    absent because it is only known after the response.
    """
    payload = {
        "profile_summary": profile_summary,
        "job_summary": job_summary_text,
        "model": model,
        "provider": provider,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "schema": schema
        if schema is not None
        else JobFitEvaluation.model_json_schema(),
        "instructions": EVALUATION_INSTRUCTIONS
        if instructions is None
        else instructions,
        "task": EVALUATION_TASK if task is None else task,
        "job_id": job_id,
        "profile_id": profile_id,
        "learned_input": learned_input,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def gemini_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if isinstance(value, dict):
            cleaned[key] = gemini_response_schema(value)
        elif isinstance(value, list):
            cleaned[key] = [
                gemini_response_schema(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned


def _complete_sentence_prefix(value: str, *, max_sentences: int, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return ""
    sentence_matches = list(re.finditer(r"[^.!?]+[.!?]", text))
    sentences = [match.group(0).strip() for match in sentence_matches[:max_sentences]]
    if sentences:
        candidate = " ".join(sentences)
    else:
        boundary_parts = re.split(r"[,;:]", text, maxsplit=1)
        candidate = boundary_parts[0].strip() if boundary_parts[0].strip() else text
    if len(candidate) <= max_chars:
        if candidate.endswith((".", "!", "?")):
            return candidate
        if len(candidate) < max_chars:
            return f"{candidate}."
        return candidate
    truncated = candidate[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{truncated}." if truncated else candidate[:max_chars].rstrip()


def normalized_evaluation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["rationale"] = _complete_sentence_prefix(
        str(payload.get("rationale") or ""),
        max_sentences=2,
        max_chars=360,
    )
    concerns: list[str] = []
    for concern in list(payload.get("concerns") or [])[:3]:
        text = _complete_sentence_prefix(str(concern), max_sentences=1, max_chars=120)
        if text and text not in concerns:
            concerns.append(text)
    normalized["concerns"] = concerns
    return JobFitEvaluation.model_validate(normalized).model_dump()


def validated_evaluation_payload(response: Any) -> dict[str, Any] | None:
    """Return a normalized payload, or None for an unusable cached response.

    An invalid cache row is a recoverable miss: the caller may pay for one fresh
    call and replace the row, instead of repeatedly failing on it forever.
    """
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except (TypeError, ValueError):
            return None
    if not isinstance(response, dict):
        return None
    try:
        return normalized_evaluation_payload(response)
    except Exception:
        logger.warning("event=llm_evaluation_cache_unusable")
        return None


class OpenAIEvaluationProvider:
    provider_name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: int = 180,
        max_retries: int = 2,
    ) -> None:
        from openai import OpenAI

        self.timeout_seconds = timeout_seconds
        self.client = OpenAI(api_key=api_key, max_retries=max_retries)

    def evaluate_job_fit(
        self,
        *,
        profile_summary: str,
        job_summary: str,
        model: str,
        prompt_version: int,
    ) -> tuple[JobFitEvaluation, dict[str, Any]]:
        schema = JobFitEvaluation.model_json_schema()
        try:
            response = self.client.responses.create(
                model=model,
                instructions=EVALUATION_INSTRUCTIONS,
                input=(
                    f"Hakijan sallittu yhteenveto:\n{profile_summary}\n\n"
                    f"Työpaikan yhteenveto:\n{job_summary}\n\n" + EVALUATION_TASK
                ),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "job_fit_evaluation",
                        "strict": True,
                        "schema": schema,
                    }
                },
                store=False,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            body = getattr(exc, "body", None)
            code = body.get("code") if isinstance(body, dict) else None
            if code == "insufficient_quota":
                raise EvaluationProviderUnavailable(
                    "openai quota is unavailable"
                ) from exc
            status_code = getattr(exc, "status_code", None)
            if status_code == 429 or exc.__class__.__name__ == "RateLimitError":
                raise EvaluationProviderRateLimited("openai rate limit") from exc
            raise
        raw_text = response.output_text
        parsed = JobFitEvaluation.model_validate(
            normalized_evaluation_payload(json.loads(raw_text))
        )
        usage = getattr(response, "usage", None)
        metadata = {
            "returned_model": getattr(response, "model", None),
            "prompt_version": prompt_version,
            "usage": usage.model_dump()
            if usage is not None and hasattr(usage, "model_dump")
            else usage,
            "usage_normalized": normalize_provider_usage("openai", usage),
            "request_id": getattr(response, "id", None),
        }
        return parsed, metadata


class GeminiEvaluationProvider:
    provider_name = "gemini"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def _post_generate_content(
        self,
        *,
        url: str,
        body: dict[str, Any],
    ) -> httpx.Response:
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
                    return response
                if attempt == 2:
                    return response
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("unreachable Gemini retry state")

    def evaluate_job_fit(
        self,
        *,
        profile_summary: str,
        job_summary: str,
        model: str,
        prompt_version: int,
    ) -> tuple[JobFitEvaluation, dict[str, Any]]:
        schema = gemini_response_schema(JobFitEvaluation.model_json_schema())
        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                f"{EVALUATION_INSTRUCTIONS}\n\n"
                                f"Hakijan sallittu yhteenveto:\n{profile_summary}\n\n"
                                f"Työpaikan yhteenveto:\n{job_summary}\n\n"
                                + EVALUATION_TASK
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
            if status_code == 429 or error_status in {
                "RESOURCE_EXHAUSTED",
                "QUOTA_EXCEEDED",
            }:
                raise EvaluationProviderRateLimited("gemini rate limit") from exc
            if status_code == 402:
                raise EvaluationProviderUnavailable(
                    "gemini quota is unavailable"
                ) from exc
            raise

        payload = response.json()
        try:
            raw_text = str(payload["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(
                "Gemini response did not include structured output text"
            ) from exc
        parsed = JobFitEvaluation.model_validate(
            normalized_evaluation_payload(json.loads(raw_text))
        )
        usage = payload.get("usageMetadata")
        metadata = {
            "returned_model": payload.get("modelVersion") or model,
            "prompt_version": prompt_version,
            "usage": usage,
            "usage_normalized": normalize_provider_usage("gemini", usage),
            "request_id": payload.get("responseId"),
        }
        return parsed, metadata


def is_retryable_output_error(exc: BaseException) -> bool:
    """A malformed/refused structured response is worth one bounded retry.

    Transport retries already live in the provider clients; this covers a
    parsed-but-invalid or unparsable JSON body, not provider/quota failures.
    """
    if isinstance(exc, EvaluationProviderUnavailable):
        return False
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return True
    try:
        from pydantic import ValidationError

        return isinstance(exc, ValidationError)
    except Exception:  # pragma: no cover - pydantic is a hard dependency
        return False


class PaidBudgetExhausted(RuntimeError):
    """The declared aggregate paid-call budget is spent mid-retry."""


def evaluate_with_parse_retry(
    provider: EvaluationProvider,
    *,
    profile_summary: str,
    job_summary: str,
    model: str,
    prompt_version: int,
    max_retries: int = 1,
    claim_attempt: Callable[[], bool] | None = None,
) -> tuple[JobFitEvaluation, dict[str, Any]]:
    """Evaluate one job, retrying a malformed response within the paid budget.

    ``claim_attempt`` runs before **every** provider attempt, including a parse
    retry, so a retry cannot exceed the declared aggregate budget. It raises
    ``PaidBudgetExhausted`` when a further attempt is not funded.
    """
    attempts = 0
    while True:
        if claim_attempt is not None and not claim_attempt():
            raise PaidBudgetExhausted("paid call budget exhausted")
        attempts += 1
        try:
            evaluation, metadata = provider.evaluate_job_fit(
                profile_summary=profile_summary,
                job_summary=job_summary,
                model=model,
                prompt_version=prompt_version,
            )
        except Exception as exc:
            if attempts > max_retries or not is_retryable_output_error(exc):
                raise
            logger.warning(
                "event=llm_evaluation_parse_retry attempt=%s error=%s",
                attempts,
                exc.__class__.__name__,
            )
            continue
        return evaluation, {**metadata, "attempts": attempts}


def configured_eval_model(settings: Settings, provider_name: str | None = None) -> str:
    provider = provider_name or settings.llm_provider
    if provider == "gemini":
        return settings.gemini_eval_model
    return settings.openai_eval_model


def build_evaluation_provider(settings: Settings) -> EvaluationProvider | None:
    if provider_in_cooldown(settings):
        logger.warning(
            "event=llm_provider_skipped reason=provider_cooldown provider=%s",
            settings.llm_provider,
        )
        return None
    if settings.llm_provider == "openai" and settings.openai_api_key:
        return OpenAIEvaluationProvider(
            api_key=settings.openai_api_key,
            timeout_seconds=settings.openai_eval_timeout_seconds,
        )
    if settings.llm_provider == "gemini" and settings.gemini_api_key:
        return GeminiEvaluationProvider(api_key=settings.gemini_api_key)
    return None
