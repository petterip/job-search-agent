import hashlib
import json
import logging
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.config import Settings
logger = logging.getLogger("matcher.embeddings")

EMBEDDING_BATCH_SIZE = 64


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
    guidance = profile.get("llm_guidance", {})
    positive_guidance = {}
    if isinstance(guidance, dict):
        positive_guidance = {
            key: guidance[key]
            for key in ("objective", "fit_tiers", "reward_signals")
            if key in guidance
        }
    payload = {
        "objective": profile.get("objective"),
        "location": profile.get("location"),
        "role_clusters": profile.get("role_clusters"),
        "languages": profile.get("languages"),
        "preferences": profile.get("preferences"),
        "positive_guidance": positive_guidance,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


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
