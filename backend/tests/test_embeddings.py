from support import with_privacy
from app.embeddings import embedding_hash, job_embedding_text, profile_embedding_text, vector_literal


def test_vector_literal_uses_pgvector_text_format() -> None:
    assert vector_literal([0.1, -0.25, 1.0]) == "[0.1,-0.25,1]"


def test_embedding_hash_changes_with_model_or_text() -> None:
    first = embedding_hash(model="text-embedding-3-small", text="kirjasto")

    assert first == embedding_hash(model="text-embedding-3-small", text="kirjasto")
    assert first != embedding_hash(model="text-embedding-3-large", text="kirjasto")
    assert first != embedding_hash(model="text-embedding-3-small", text="museo")


def test_job_embedding_text_is_minimized_and_stable() -> None:
    text = job_embedding_text(
        {
            "id": 1,
            "title": "Museo-opas",
            "employer": "Kaupunki",
            "location": "Oulu",
            "description": "Opastus " * 1000,
            "raw_payload": {"not": "included"},
        }
    )

    assert "Museo-opas" in text
    assert "raw_payload" not in text
    assert len(text) < 4300


def test_profile_embedding_text_uses_positive_matching_signals_not_exclusions() -> None:
    text = profile_embedding_text(
        with_privacy(
            {
                "role_clusters": [{"titles_fi": ["kirjastonhoitaja"]}],
                "exclusions": {"hard_negative_titles_fi": ["lähihoitaja"]},
                "llm_guidance": {
                    "objective": "Find good jobs.",
                    "reward_signals": ["library"],
                    "caution_signals": ["healthcare"],
                },
            }
        )
    )

    assert "kirjastonhoitaja" in text
    assert "library" in text
    assert "lähihoitaja" not in text
    assert "healthcare" not in text


def test_centroid_provenance_reports_missing_embeddings() -> None:
    from app.config import Settings
    from app.embeddings import recompute_learned_preference_embeddings

    class Rows:
        def __init__(self, rows: list[dict]) -> None:
            self.rows = rows

        def mappings(self) -> "Rows":
            return self

        def __iter__(self):
            return iter(self.rows)

    class Connection:
        def execute(self, statement, params=None):  # noqa: ANN001, ARG002
            sql = str(statement)
            if "from job_embeddings" in sql and "embedding::text" in sql:
                return Rows(
                    [
                        {"job_id": 10, "embedding_text": "[0.1,0.2]"},
                        {"job_id": 11, "embedding_text": "[0.3,0.4]"},
                        {"job_id": 12, "embedding_text": "[0.5,0.6]"},
                    ]
                )
            return Rows([])

    rows = [
        {"rating": 5, "applied": False, "scoring_snapshot": {"job_id": jid}}
        for jid in (10, 11, 12, 13, 14)
    ]
    result = recompute_learned_preference_embeddings(
        Connection(),  # type: ignore[arg-type]
        profile_id=1,
        rows=rows,
        settings=Settings(openai_api_key="k", openai_embedding_dimension=1536),
    )

    assert result["positive_job_ids"] == [10, 11, 12, 13, 14]
    assert result["positive_missing_embeddings"] == [13, 14]
    assert result["model"]
    assert result["dimension"] == 1536
