from functools import lru_cache
from os import getenv

from pydantic import BaseModel, Field


class Settings(BaseModel):
    app_name: str = "job-search-agent"
    database_url: str = "postgresql+psycopg://jobsearchagent:change-me@db:5432/jobsearchagent"
    llm_provider: str = ""
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimension: int = 1536
    openai_eval_model: str = "gpt-5.4-nano"
    openai_eval_model_escalated: str = "gpt-5.4-mini"
    gemini_api_key: str = ""
    gemini_eval_model: str = "gemini-3.5-flash"
    llm_eval_batch_size: int = 8
    llm_eval_max_jobs: int = 50
    llm_provider_failure_cooldown_min: int = 360
    storage_dir: str = "/storage"
    llm_prompt_version: int = 7
    matcher_interval_min: int = 15
    matcher_max_jobs: int = 1000
    discovery_search_queries: list[str] = Field(
        default_factory=lambda: [
            "kirjastonhoitaja",
            "kirjastovirkailija",
            "kirjasto",
            "informaatikko",
            "kirjastopedagogi",
            "musiikkikirjasto",
            "musiikkitapahtumat",
            "sisällöntuottaja",
            "kulttuurituottaja",
        ]
    )
    collector_enabled_sources: list[str] = Field(
        default_factory=lambda: [
            "duunitori",
            "tmt",
            "tmt_oulu",
            "laura",
            "jobly",
            "eures_fi",
            "kuntarekry",
            "kirkkorekry",
            "oulu_varbi",
        ]
    )
    duunitori_url: str = "https://duunitori.fi/api/v1/jobentries"
    duunitori_page_size: int = 100
    duunitori_max_pages: int = 50
    tmt_url: str = "https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search"
    tmt_page_size: int = 90
    tmt_max_pages: int = 130
    tmt_oulu_municipality_codes: list[str] = Field(default_factory=lambda: ["564"])
    tmt_oulu_max_pages: int = 10
    laura_url: str = "https://laura.fi/wp-json/wp/v2/job-listings"
    laura_page_size: int = 100
    laura_max_pages: int = 5
    jobly_sitemap_urls: list[str] = Field(
        default_factory=lambda: [
            "https://www.jobly.fi/sitemap.xml?page=1",
            "https://www.jobly.fi/sitemap.xml?page=2",
        ]
    )
    jobly_max_urls_per_run: int = 100
    eures_url: str = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
    eures_page_size: int = 50
    eures_max_pages: int = 5
    oulu_varbi_rss_url: str = "https://oulunyliopisto.varbi.com/fi/what:rssfeed/"
    oulu_varbi_base_url: str = "https://oulunyliopisto.varbi.com"
    collector_stale_run_minutes: int = 30


def _int_env(name: str, default: int) -> int:
    return int(getenv(name, str(default)))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    defaults = Settings()
    jobly_sitemap_raw = getenv("JOBLY_SITEMAP_URLS")
    if jobly_sitemap_raw:
        jobly_sitemap_urls = [part.strip() for part in jobly_sitemap_raw.split(",") if part.strip()]
    else:
        jobly_sitemap_urls = defaults.jobly_sitemap_urls
    enabled_sources_raw = getenv("COLLECTOR_ENABLED_SOURCES")
    if enabled_sources_raw:
        collector_enabled_sources = [
            part.strip() for part in enabled_sources_raw.split(",") if part.strip()
        ]
    else:
        collector_enabled_sources = defaults.collector_enabled_sources
    discovery_search_raw = getenv("DISCOVERY_SEARCH_QUERIES")
    if discovery_search_raw:
        discovery_search_queries = [
            part.strip()
            for part in discovery_search_raw.split(",")
            if part.strip()
        ]
    else:
        discovery_search_queries = defaults.discovery_search_queries

    return Settings(
        database_url=getenv("DATABASE_URL", defaults.database_url),
        llm_provider=getenv("LLM_PROVIDER", ""),
        openai_api_key=getenv("OPENAI_API_KEY", ""),
        openai_embedding_model=getenv("OPENAI_EMBEDDING_MODEL", defaults.openai_embedding_model),
        openai_embedding_dimension=_int_env(
            "OPENAI_EMBEDDING_DIMENSION",
            defaults.openai_embedding_dimension,
        ),
        openai_eval_model=getenv("OPENAI_EVAL_MODEL", defaults.openai_eval_model),
        openai_eval_model_escalated=getenv(
            "OPENAI_EVAL_MODEL_ESCALATED",
            defaults.openai_eval_model_escalated,
        ),
        gemini_api_key=getenv("GEMINI_API_KEY", ""),
        gemini_eval_model=getenv("GEMINI_EVAL_MODEL", defaults.gemini_eval_model),
        llm_eval_batch_size=_int_env("LLM_EVAL_BATCH_SIZE", defaults.llm_eval_batch_size),
        llm_eval_max_jobs=_int_env("LLM_EVAL_MAX_JOBS", defaults.llm_eval_max_jobs),
        llm_provider_failure_cooldown_min=_int_env(
            "LLM_PROVIDER_FAILURE_COOLDOWN_MIN",
            defaults.llm_provider_failure_cooldown_min,
        ),
        storage_dir=getenv("STORAGE_DIR", defaults.storage_dir),
        llm_prompt_version=_int_env("LLM_PROMPT_VERSION", defaults.llm_prompt_version),
        matcher_interval_min=_int_env("MATCHER_INTERVAL_MIN", defaults.matcher_interval_min),
        matcher_max_jobs=_int_env("MATCHER_MAX_JOBS", defaults.matcher_max_jobs),
        discovery_search_queries=discovery_search_queries,
        collector_enabled_sources=collector_enabled_sources,
        duunitori_page_size=_int_env("DUUNITORI_PAGE_SIZE", defaults.duunitori_page_size),
        duunitori_max_pages=_int_env("DUUNITORI_MAX_PAGES", defaults.duunitori_max_pages),
        tmt_page_size=_int_env("TMT_PAGE_SIZE", defaults.tmt_page_size),
        tmt_max_pages=_int_env("TMT_MAX_PAGES", defaults.tmt_max_pages),
        tmt_oulu_max_pages=_int_env("TMT_OULU_MAX_PAGES", defaults.tmt_oulu_max_pages),
        laura_page_size=_int_env("LAURA_PAGE_SIZE", defaults.laura_page_size),
        laura_max_pages=_int_env("LAURA_MAX_PAGES", defaults.laura_max_pages),
        jobly_sitemap_urls=jobly_sitemap_urls,
        jobly_max_urls_per_run=_int_env("JOBLY_MAX_URLS_PER_RUN", defaults.jobly_max_urls_per_run),
        eures_page_size=_int_env("EURES_PAGE_SIZE", defaults.eures_page_size),
        eures_max_pages=_int_env("EURES_MAX_PAGES", defaults.eures_max_pages),
        collector_stale_run_minutes=_int_env(
            "COLLECTOR_STALE_RUN_MINUTES",
            defaults.collector_stale_run_minutes,
        ),
    )
