import hashlib
import json
import logging
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import Settings
from app.privacy import project_profile_for_llm, sanitize_outbound

logger = logging.getLogger("matcher.embeddings")

EMBEDDING_BATCH_SIZE = 64


def _commit_if_supported(connection: Any) -> None:
    """Close any open write transaction before a remote call.

    Per-batch progress is committed as it is produced, so a later provider
    failure does not roll back embeddings that already succeeded and no HTTP
    request is issued while a transaction is open.
    """
    commit = getattr(connection, "commit", None)
    if callable(commit):
        commit()


def embedding_hash(*, model: str, text: str) -> str:
    payload = {"model": model, "text": text}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def job_embedding_text(job: dict[str, Any]) -> str:
    description = str(job.get("description") or "")
    payload = {
        "title": job.get("title"),
        "employer": job.get("employer"),
        "location": job.get("location"),
        "description_excerpt": description[:4000],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def profile_embedding_text(profile: dict[str, Any]) -> str:
    """Positive retrieval text only, drawn from the privacy-approved projection.

    Objective, location, role_clusters, languages, preferences and positive
    llm_guidance fields are included; learned exclusions and any forbidden
    nested field (for example a postal code) are excluded by the projection.
    """
    projected = project_profile_for_llm(profile)
    guidance = projected.get("llm_guidance", {})
    positive_guidance = {}
    if isinstance(guidance, dict):
        positive_guidance = {
            key: guidance[key]
            for key in ("objective", "fit_tiers", "reward_signals")
            if key in guidance
        }
    payload = {
        "objective": projected.get("objective"),
        "location": projected.get("location"),
        "role_clusters": projected.get("role_clusters"),
        "languages": projected.get("languages"),
        "preferences": projected.get("preferences"),
        "positive_guidance": positive_guidance,
        # Positive supported skills and strength signals are retrieval evidence;
        # learned exclusions stay out of the embedding by design.
        "skills": projected.get("skills"),
        "strength_signals": projected.get("strength_signals"),
    }
    return json.dumps(sanitize_outbound(payload), ensure_ascii=False, sort_keys=True)


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8g}" for value in values) + "]"


class OpenAIEmbeddingProvider:
    def __init__(self, *, api_key: str, model: str, dimension: int) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, max_retries=0)
        self.model = model
        self.dimension = dimension

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(
            model=self.model,
            input=texts,
            dimensions=self.dimension,
        )
        return [list(item.embedding) for item in response.data]


def build_embedding_provider(settings: Settings) -> OpenAIEmbeddingProvider | None:
    if not settings.openai_api_key:
        return None
    if settings.openai_embedding_dimension != 1536:
        logger.warning(
            "event=embedding_disabled reason=unsupported_dimension dimension=%s",
            settings.openai_embedding_dimension,
        )
        return None
    return OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.openai_embedding_model,
        dimension=settings.openai_embedding_dimension,
    )


def ensure_profile_embedding(
    connection: Connection,
    *,
    profile_id: int,
    profile: dict[str, Any],
    provider: OpenAIEmbeddingProvider,
) -> bool:
    text = profile_embedding_text(profile)
    content_hash = embedding_hash(model=provider.model, text=text)
    existing_hash = connection.execute(
        sa.text(
            """
            select content_hash
            from profile_embeddings
            where profile_id = :profile_id
              and model = :model
              and dimension = :dimension
            """
        ),
        {"profile_id": profile_id, "model": provider.model, "dimension": provider.dimension},
    ).scalar_one_or_none()
    if existing_hash == content_hash:
        return False
    _commit_if_supported(connection)
    embedding = provider.embed_texts([text])[0]
    connection.execute(
        sa.text(
            """
            insert into profile_embeddings (profile_id, model, dimension, content_hash, embedding)
            values (:profile_id, :model, :dimension, :content_hash, CAST(:embedding AS vector))
            on conflict (profile_id)
            do update set
                model = excluded.model,
                dimension = excluded.dimension,
                content_hash = excluded.content_hash,
                embedding = excluded.embedding,
                updated_at = now()
            """
        ),
        {
            "profile_id": profile_id,
            "model": provider.model,
            "dimension": provider.dimension,
            "content_hash": content_hash,
            "embedding": vector_literal(embedding),
        },
    )
    return True


def ensure_job_embeddings(
    connection: Connection,
    *,
    jobs: list[dict[str, Any]],
    provider: OpenAIEmbeddingProvider,
) -> int:
    if not jobs:
        return 0
    text_by_job_id = {int(job["id"]): job_embedding_text(job) for job in jobs}
    hash_by_job_id = {
        job_id: embedding_hash(model=provider.model, text=text)
        for job_id, text in text_by_job_id.items()
    }
    existing_rows = connection.execute(
        sa.text(
            """
            select job_id, content_hash
            from job_embeddings
            where job_id = any(:job_ids)
              and model = :model
              and dimension = :dimension
            """
        ),
        {
            "job_ids": list(text_by_job_id),
            "model": provider.model,
            "dimension": provider.dimension,
        },
    ).mappings()
    existing_hashes = {int(row["job_id"]): str(row["content_hash"]) for row in existing_rows}
    missing_or_stale = [
        job_id
        for job_id, content_hash in hash_by_job_id.items()
        if existing_hashes.get(job_id) != content_hash
    ]
    embedded = 0
    for start in range(0, len(missing_or_stale), EMBEDDING_BATCH_SIZE):
        batch_ids = missing_or_stale[start : start + EMBEDDING_BATCH_SIZE]
        texts = [text_by_job_id[job_id] for job_id in batch_ids]
        _commit_if_supported(connection)
        embeddings = provider.embed_texts(texts)
        for job_id, embedding in zip(batch_ids, embeddings, strict=True):
            connection.execute(
                sa.text(
                    """
                    insert into job_embeddings (job_id, model, dimension, content_hash, embedding)
                    values (:job_id, :model, :dimension, :content_hash, CAST(:embedding AS vector))
                    on conflict (job_id)
                    do update set
                        model = excluded.model,
                        dimension = excluded.dimension,
                        content_hash = excluded.content_hash,
                        embedding = excluded.embedding,
                        updated_at = now()
                    """
                ),
                {
                    "job_id": job_id,
                    "model": provider.model,
                    "dimension": provider.dimension,
                    "content_hash": hash_by_job_id[job_id],
                    "embedding": vector_literal(embedding),
                },
            )
            embedded += 1
    return embedded


def semantic_vector_scores(
    connection: Connection,
    *,
    profile_id: int,
    job_ids: list[int],
    model: str,
    dimension: int,
    limit: int,
) -> dict[int, float]:
    if not job_ids:
        return {}
    rows = connection.execute(
        sa.text(
            """
            with profile_vector as (
                select embedding
                from profile_embeddings
                where profile_id = :profile_id
                  and model = :model
                  and dimension = :dimension
            )
            select
                je.job_id,
                1 - (je.embedding <=> pv.embedding) as vector_score
            from job_embeddings je
            cross join profile_vector pv
            where je.job_id = any(:job_ids)
              and je.model = :model
              and je.dimension = :dimension
            order by je.embedding <=> pv.embedding
            limit :limit
            """
        ),
        {
            "profile_id": profile_id,
            "job_ids": job_ids,
            "model": model,
            "dimension": dimension,
            "limit": limit,
        },
    ).mappings()
    return {int(row["job_id"]): float(row["vector_score"]) for row in rows}


MIN_CENTROID_SUPPORT = 3


def mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    size = len(vectors[0])
    totals = [0.0] * size
    for vector in vectors:
        for index, value in enumerate(vector):
            totals[index] += value
    count = float(len(vectors))
    return [value / count for value in totals]


def _load_job_embedding_vectors(
    connection: Connection,
    *,
    job_ids: list[int],
    model: str,
    dimension: int,
) -> dict[int, list[float]]:
    if not job_ids:
        return {}
    rows = connection.execute(
        sa.text(
            """
            select job_id, embedding::text as embedding_text
            from job_embeddings
            where job_id = any(:job_ids)
              and model = :model
              and dimension = :dimension
            """
        ),
        {"job_ids": job_ids, "model": model, "dimension": dimension},
    ).mappings()
    vectors: dict[int, list[float]] = {}
    for row in rows:
        raw = str(row["embedding_text"]).strip("[]")
        vectors[int(row["job_id"])] = [float(part) for part in raw.split(",") if part]
    return vectors


def store_learned_preference_embedding(
    connection: Connection,
    *,
    profile_id: int,
    kind: str,
    model: str,
    dimension: int,
    embedding: list[float],
    job_count: int,
) -> None:
    connection.execute(
        sa.text(
            """
            insert into learned_preference_embeddings (
                profile_id, kind, model, dimension, job_count, embedding
            )
            values (
                :profile_id, :kind, :model, :dimension, :job_count, CAST(:embedding AS vector)
            )
            on conflict (profile_id, kind, model, dimension)
            do update set
                job_count = excluded.job_count,
                embedding = excluded.embedding,
                updated_at = now()
            """
        ),
        {
            "profile_id": profile_id,
            "kind": kind,
            "model": model,
            "dimension": dimension,
            "job_count": job_count,
            "embedding": vector_literal(embedding),
        },
    )


def delete_learned_preference_embedding(
    connection: Connection,
    *,
    profile_id: int,
    kind: str,
    model: str,
    dimension: int,
) -> None:
    connection.execute(
        sa.text(
            """
            delete from learned_preference_embeddings
            where profile_id = :profile_id
              and kind = :kind
              and model = :model
              and dimension = :dimension
            """
        ),
        {"profile_id": profile_id, "kind": kind, "model": model, "dimension": dimension},
    )


def recompute_learned_preference_embeddings(
    connection: Connection,
    *,
    profile_id: int,
    rows: list[dict[str, Any]],
    settings: Settings,
) -> dict[str, Any]:
    from app.feedback_analysis import snapshot_dict

    if not settings.openai_api_key or settings.openai_embedding_dimension != 1536:
        return {"status": "skipped", "reason": "embedding_provider_unavailable"}
    model = settings.openai_embedding_model
    dimension = settings.openai_embedding_dimension
    positive_job_ids: list[int] = []
    negative_job_ids: list[int] = []
    for row in rows:
        rating = int(row["rating"])
        applied = bool(row["applied"])
        snapshot = snapshot_dict(row.get("scoring_snapshot"))
        job_id = int(row.get("job_id") or snapshot.get("job_id") or 0)
        if not job_id:
            continue
        if rating >= 4 or applied:
            positive_job_ids.append(job_id)
        elif rating <= 2:
            negative_job_ids.append(job_id)
    result: dict[str, Any] = {
        "model": model,
        "dimension": dimension,
        "positive_job_ids": sorted(set(positive_job_ids)),
        "negative_job_ids": sorted(set(negative_job_ids)),
    }
    positive_vectors_by_job = _load_job_embedding_vectors(
        connection, job_ids=sorted(set(positive_job_ids)), model=model, dimension=dimension
    )
    positive_vectors = list(positive_vectors_by_job.values())
    result["positive_missing_embeddings"] = sorted(
        set(positive_job_ids) - set(positive_vectors_by_job)
    )
    if len(positive_vectors) >= MIN_CENTROID_SUPPORT:
        store_learned_preference_embedding(
            connection,
            profile_id=profile_id,
            kind="preference",
            model=model,
            dimension=dimension,
            embedding=mean_vector(positive_vectors),
            job_count=len(positive_vectors),
        )
        result["preference"] = len(positive_vectors)
    else:
        delete_learned_preference_embedding(
            connection,
            profile_id=profile_id,
            kind="preference",
            model=model,
            dimension=dimension,
        )
        result["preference"] = "skipped"
    negative_vectors_by_job = _load_job_embedding_vectors(
        connection, job_ids=sorted(set(negative_job_ids)), model=model, dimension=dimension
    )
    negative_vectors = list(negative_vectors_by_job.values())
    result["negative_missing_embeddings"] = sorted(
        set(negative_job_ids) - set(negative_vectors_by_job)
    )
    if len(negative_vectors) >= MIN_CENTROID_SUPPORT:
        store_learned_preference_embedding(
            connection,
            profile_id=profile_id,
            kind="anti_preference",
            model=model,
            dimension=dimension,
            embedding=mean_vector(negative_vectors),
            job_count=len(negative_vectors),
        )
        result["anti_preference"] = len(negative_vectors)
    else:
        delete_learned_preference_embedding(
            connection,
            profile_id=profile_id,
            kind="anti_preference",
            model=model,
            dimension=dimension,
        )
        result["anti_preference"] = "skipped"
    return result


def learned_centroid_similarity_scores(
    connection: Connection,
    *,
    profile_id: int,
    job_ids: list[int],
    model: str,
    dimension: int,
) -> tuple[dict[int, float], dict[int, float]]:
    if not job_ids:
        return {}, {}
    rows = connection.execute(
        sa.text(
            """
            select
                je.job_id,
                case
                    when pref.embedding is not null
                    then 1 - (je.embedding <=> pref.embedding)
                end as preference_score,
                case
                    when anti.embedding is not null
                    then 1 - (je.embedding <=> anti.embedding)
                end as anti_score
            from job_embeddings je
            left join learned_preference_embeddings pref
              on pref.profile_id = :profile_id
             and pref.kind = 'preference'
             and pref.model = :model
             and pref.dimension = :dimension
            left join learned_preference_embeddings anti
              on anti.profile_id = :profile_id
             and anti.kind = 'anti_preference'
             and anti.model = :model
             and anti.dimension = :dimension
            where je.job_id = any(:job_ids)
              and je.model = :model
              and je.dimension = :dimension
            """
        ),
        {
            "profile_id": profile_id,
            "job_ids": job_ids,
            "model": model,
            "dimension": dimension,
        },
    ).mappings()
    preference_scores: dict[int, float] = {}
    anti_scores: dict[int, float] = {}
    for row in rows:
        job_id = int(row["job_id"])
        if row["preference_score"] is not None:
            preference_scores[job_id] = float(row["preference_score"])
        if row["anti_score"] is not None:
            anti_scores[job_id] = float(row["anti_score"])
    return preference_scores, anti_scores
