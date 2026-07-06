import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings

logger = logging.getLogger("matcher.llm")

FitTier = Literal["strong_fit", "transferable_weaker", "generic_customer_service_only", "not_applicable"]
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
    ) -> tuple[JobFitEvaluation, dict[str, Any]]:
        ...


class EvaluationProviderUnavailable(RuntimeError):
    pass


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


def minimized_profile_summary(profile: dict[str, Any]) -> str:
    allowed_keys = [
        "objective",
        "location",
        "career_evidence",
        "role_clusters",
        "skills",
        "strength_signals",
        "languages",
        "availability",
        "preferences",
        "exclusions",
        "freshness",
        "llm_guidance",
    ]
    minimized = {key: profile[key] for key in allowed_keys if key in profile}
    return json.dumps(minimized, ensure_ascii=False, sort_keys=True)


def job_summary(job: dict[str, Any]) -> str:
    description = str(job.get("description") or "")
    excerpt = description[:4000]
    deterministic_result = job.get("deterministic_result") or {}
    if isinstance(deterministic_result, str):
        deterministic_result = json.loads(deterministic_result)
    travel_assessment = deterministic_result.get("travel_assessment")
    minimized = {
        "title": job.get("title"),
        "employer": job.get("employer"),
        "location": job.get("location"),
        "description_excerpt": excerpt,
        "travel_assessment": travel_assessment,
    }
    return json.dumps(minimized, ensure_ascii=False, sort_keys=True)


def evaluation_request_hash(
    *,
    profile_summary: str,
    job_summary_text: str,
    model: str,
    prompt_version: int,
    schema_version: int = 1,
    learned_version: int = 0,
) -> str:
    payload = {
        "profile_summary": profile_summary,
        "job_summary": job_summary_text,
        "model": model,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "learned_version": learned_version,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


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
                    f"Työpaikan yhteenveto:\n{job_summary}\n\n"
                    + EVALUATION_TASK
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
            code = getattr(getattr(exc, "body", None), "get", lambda _key: None)("code")
            if code == "insufficient_quota":
                raise EvaluationProviderUnavailable("openai quota is unavailable") from exc
            raise
        raw_text = response.output_text
        parsed = JobFitEvaluation.model_validate(
            normalized_evaluation_payload(json.loads(raw_text))
        )
        metadata = {
            "returned_model": getattr(response, "model", None),
            "prompt_version": prompt_version,
            "usage": getattr(response, "usage", None).model_dump() if getattr(response, "usage", None) else None,
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
            if status_code in {402, 429} or error_status in {"RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"}:
                raise EvaluationProviderUnavailable("gemini quota is unavailable") from exc
            raise

        payload = response.json()
        try:
            raw_text = str(payload["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Gemini response did not include structured output text") from exc
        parsed = JobFitEvaluation.model_validate(
            normalized_evaluation_payload(json.loads(raw_text))
        )
        metadata = {
            "returned_model": payload.get("modelVersion") or model,
            "prompt_version": prompt_version,
            "usage": payload.get("usageMetadata"),
        }
        return parsed, metadata


def configured_eval_model(settings: Settings, provider_name: str | None = None) -> str:
    provider = provider_name or settings.llm_provider
    if provider == "gemini":
        return settings.gemini_eval_model
    return settings.openai_eval_model


def build_evaluation_provider(settings: Settings) -> EvaluationProvider | None:
    if provider_in_cooldown(settings):
        logger.warning("event=llm_provider_skipped reason=provider_cooldown provider=%s", settings.llm_provider)
        return None
    if settings.llm_provider == "openai" and settings.openai_api_key:
        return OpenAIEvaluationProvider(
            api_key=settings.openai_api_key,
            timeout_seconds=settings.openai_eval_timeout_seconds,
        )
    if settings.llm_provider == "gemini" and settings.gemini_api_key:
        return GeminiEvaluationProvider(api_key=settings.gemini_api_key)
    return None
