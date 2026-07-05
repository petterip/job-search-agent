import json

import httpx

from app.config import Settings
from app.llm import (
    EVALUATION_INSTRUCTIONS,
    EVALUATION_TASK,
    GeminiEvaluationProvider,
    JobFitEvaluation,
    OpenAIEvaluationProvider,
    build_evaluation_provider,
    configured_eval_model,
    evaluation_request_hash,
    gemini_response_schema,
    job_summary,
    mark_provider_unavailable,
    minimized_profile_summary,
    normalized_evaluation_payload,
    provider_in_cooldown,
)


def test_minimized_profile_summary_omits_unapproved_fields() -> None:
    summary = minimized_profile_summary(
        {
            "objective": "fit",
            "location": {"home_city": "Oulu"},
            "raw_cv": "private",
            "email": "private@example.com",
        }
    )

    assert "Oulu" in summary
    assert "raw_cv" not in summary
    assert "private@example.com" not in summary


def test_minimized_profile_summary_includes_verified_fit_evidence() -> None:
    summary = minimized_profile_summary(
        {
            "career_evidence": {
                "qualifications": ["kirjastonhoitajan / kirjastoalan kelpoisuus"],
                "languages_verified": ["sv: sujuva B2"],
                "leadership": {"years": "noin 9 vuotta kirjastonjohtajatehtävissä"},
            },
            "skills": [{"name": "esimiestyö ja lähijohtaminen", "confidence": "high"}],
            "strength_signals": [
                {
                    "name": "laajemman kokonaisuuden hahmottaminen johtamiskokemuksen kautta",
                    "confidence": "high",
                }
            ],
            "raw_cv": "private",
            "email": "private@example.com",
        }
    )

    assert "sujuva B2" in summary
    assert "kirjastoalan kelpoisuus" in summary
    assert "kirjastonjohtajatehtävissä" in summary
    assert "esimiestyö" in summary
    assert "raw_cv" not in summary
    assert "private@example.com" not in summary


def test_job_summary_limits_description() -> None:
    summary = job_summary({"title": "Test", "description": "a" * 5000})
    data = json.loads(summary)

    assert data["title"] == "Test"
    assert len(data["description_excerpt"]) == 4000


def test_job_summary_includes_travel_assessment_and_changes_request_hash() -> None:
    without_travel = job_summary({"title": "Test", "location": "Oulu", "description": "d"})
    with_travel = job_summary(
        {
            "title": "Test",
            "location": "Oulu",
            "description": "d",
            "deterministic_result": {
                "travel_assessment": {"commutable": True, "status": "exact_home_city"},
            },
        }
    )
    without_hash = evaluation_request_hash(
        profile_summary="{}",
        job_summary_text=without_travel,
        model="model",
        prompt_version=8,
    )
    with_hash = evaluation_request_hash(
        profile_summary="{}",
        job_summary_text=with_travel,
        model="model",
        prompt_version=8,
    )

    assert json.loads(with_travel)["travel_assessment"]["commutable"] is True
    assert without_hash != with_hash


def test_evaluation_request_hash_is_stable() -> None:
    first = evaluation_request_hash(
        profile_summary='{"a": 1}',
        job_summary_text='{"b": 2}',
        model="model",
        prompt_version=3,
    )
    second = evaluation_request_hash(
        profile_summary='{"a": 1}',
        job_summary_text='{"b": 2}',
        model="model",
        prompt_version=3,
    )

    assert first == second
    assert len(first) == 64


def test_job_fit_evaluation_schema_forbids_additional_properties() -> None:
    schema = JobFitEvaluation.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "score",
        "fit_tier",
        "rationale",
        "concerns",
        "suggested_action",
    }


def test_job_fit_evaluation_schema_constrains_ui_copy_length() -> None:
    schema = JobFitEvaluation.model_json_schema()

    assert schema["properties"]["rationale"]["maxLength"] <= 360
    assert schema["properties"]["concerns"]["maxItems"] <= 3
    assert schema["properties"]["concerns"]["items"]["maxLength"] <= 120


def test_evaluation_instructions_use_reader_friendly_missing_requirement_language() -> None:
    instructions = f"{EVALUATION_INSTRUCTIONS} {EVALUATION_TASK}"

    assert "hakijalta puuttuu" in instructions
    assert "Älä aloita huolia toistuvasti" in instructions
    assert "hakijaprofiilin tiedoista puuttuu" not in instructions
    assert "suggested_action='skip'" in instructions
    assert "enintään kaksi virkettä" in instructions
    assert "Ei yleisluontoista täytetekstiä" in instructions
    assert "ruotsi B2/sujuva" in instructions
    assert "60 op / 35 ov" in instructions
    assert "profiilissa" not in instructions
    assert "lähdeaineistossa" not in instructions


def test_normalized_evaluation_payload_keeps_complete_short_copy() -> None:
    payload = {
        "score": 46,
        "fit_tier": "transferable_weaker",
        "suggested_action": "consider",
        "rationale": (
            "Tehtävä sopii viestinnän ja kansainvälisen asiakashankinnan kaltaisiin kokonaisuuksiin. "
            "Puuttuvaksi jää kuitenkin hakijan täsmällinen näyttö digimarkkinoinnista ja sosiaalisen median käytön"
        ),
        "concerns": [
            "Työ painottuu kansainväliseen opiskelijarekrytointimarkkinointiin ja digisisältöihin, mutta hakijalta puuttuu kuvauksena",
            "Kokonaisuus edellyttää tehtävään soveltuvaa korkeakoulututkintoa, mutta tutkintovaatimuksen täsmäosumaa ei voi varmistaa",
            "Virkasuhteen edellyttämä pitkä vaatimus voi päättyä täsmälleen pituusrajaan eikä piste saa ylittää kentän rajaa",
            "Kolmas huoli saa jäädä mukaan.",
            "Neljäs poistuu.",
        ],
    }

    normalized = normalized_evaluation_payload(payload)

    assert normalized["rationale"] == (
        "Tehtävä sopii viestinnän ja kansainvälisen asiakashankinnan kaltaisiin kokonaisuuksiin."
    )
    assert normalized["concerns"] == [
        "Työ painottuu kansainväliseen opiskelijarekrytointimarkkinointiin ja digisisältöihin.",
        "Kokonaisuus edellyttää tehtävään soveltuvaa korkeakoulututkintoa.",
        "Virkasuhteen edellyttämä pitkä vaatimus voi päättyä täsmälleen pituusrajaan eikä piste saa ylittää kentän rajaa.",
    ]
    assert all(len(concern) <= 120 for concern in normalized["concerns"])


def test_gemini_response_schema_omits_unsupported_additional_properties() -> None:
    schema = gemini_response_schema(JobFitEvaluation.model_json_schema())

    assert "additionalProperties" not in json.dumps(schema)
    assert schema["type"] == "object"
    assert schema["properties"]["fit_tier"]["enum"]


def test_provider_unavailable_marker_skips_provider_until_cooldown_expires(tmp_path) -> None:
    settings = Settings(
        llm_provider="openai",
        openai_api_key="test-key",
        storage_dir=str(tmp_path),
        llm_provider_failure_cooldown_min=60,
    )

    mark_provider_unavailable(settings, "quota")

    assert provider_in_cooldown(settings)
    assert build_evaluation_provider(settings) is None


def test_provider_cooldown_is_scoped_to_provider(tmp_path) -> None:
    openai_settings = Settings(
        llm_provider="openai",
        openai_api_key="test-key",
        storage_dir=str(tmp_path),
        llm_provider_failure_cooldown_min=60,
    )
    gemini_settings = Settings(
        llm_provider="gemini",
        gemini_api_key="test-key",
        storage_dir=str(tmp_path),
        llm_provider_failure_cooldown_min=60,
    )

    mark_provider_unavailable(openai_settings, "quota")

    assert provider_in_cooldown(openai_settings)
    assert not provider_in_cooldown(gemini_settings)
    assert build_evaluation_provider(gemini_settings) is not None


def test_configured_eval_model_follows_provider() -> None:
    settings = Settings(
        llm_provider="gemini",
        openai_eval_model="openai-model",
        gemini_eval_model="gemini-model",
    )

    assert configured_eval_model(settings) == "gemini-model"
    assert configured_eval_model(settings, "openai") == "openai-model"


def test_openai_provider_uses_request_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        output_text = json_dumps(
            {
                "score": 82,
                "fit_tier": "strong_fit",
                "rationale": "Hyvä osuma.",
                "concerns": [],
                "suggested_action": "apply",
            }
        )
        model = "openai-returned"
        usage = None

    class FakeResponses:
        def create(self, **kwargs):  # noqa: ANN003, ANN201
            captured.update(kwargs)
            return FakeResponse()

    class FakeOpenAI:
        def __init__(self, api_key, max_retries):  # noqa: ANN001
            self.responses = FakeResponses()

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    provider = OpenAIEvaluationProvider(api_key="test-key")

    evaluation, metadata = provider.evaluate_job_fit(
        profile_summary="{}",
        job_summary="{}",
        model="openai-test",
        prompt_version=5,
    )

    assert evaluation.score == 82
    assert metadata["returned_model"] == "openai-returned"
    assert captured["timeout"] == 60


def test_gemini_provider_parses_structured_response(monkeypatch) -> None:
    def fake_post(self, url, headers, json):  # noqa: ANN001, ARG001
        request = httpx.Request("POST", url)
        assert json["generationConfig"]["responseMimeType"] == "application/json"
        assert "responseSchema" in json["generationConfig"]
        return httpx.Response(
            200,
            request=request,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json_dumps(
                                        {
                                            "score": 82,
                                            "fit_tier": "strong_fit",
                                            "rationale": "Sopii hyvin profiiliin.",
                                            "concerns": [],
                                            "suggested_action": "apply",
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ],
                "modelVersion": "gemini-test-version",
                "usageMetadata": {"totalTokenCount": 123},
            },
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    provider = GeminiEvaluationProvider(api_key="test-key")

    evaluation, metadata = provider.evaluate_job_fit(
        profile_summary="{}",
        job_summary="{}",
        model="gemini-test",
        prompt_version=3,
    )

    assert evaluation.score == 82
    assert evaluation.fit_tier == "strong_fit"
    assert metadata["returned_model"] == "gemini-test-version"
    assert metadata["usage"] == {"totalTokenCount": 123}


def test_gemini_provider_retries_transient_server_errors(monkeypatch) -> None:
    calls = 0

    def fake_post(self, url, headers, json):  # noqa: ANN001, ARG001
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", url)
        if calls == 1:
            return httpx.Response(503, request=request, text="temporary unavailable")
        return httpx.Response(
            200,
            request=request,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json_dumps(
                                        {
                                            "score": 65,
                                            "fit_tier": "transferable_weaker",
                                            "rationale": "Osittainen osuma.",
                                            "concerns": ["Tarkista vaatimukset."],
                                            "suggested_action": "consider",
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ],
            },
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    monkeypatch.setattr("app.llm.time.sleep", lambda _seconds: None)
    provider = GeminiEvaluationProvider(api_key="test-key")

    evaluation, _metadata = provider.evaluate_job_fit(
        profile_summary="{}",
        job_summary="{}",
        model="gemini-test",
        prompt_version=3,
    )

    assert calls == 2
    assert evaluation.score == 65


def json_dumps(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False)
