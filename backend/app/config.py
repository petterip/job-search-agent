from functools import lru_cache
from os import getenv

from pydantic import BaseModel, Field


class Settings(BaseModel):
    app_name: str = "job-search-agent"
    database_url: str = (
        "postgresql+psycopg://jobsearchagent:change-me@db:5432/jobsearchagent"
    )
    llm_provider: str = ""
    operator_api_token: str = ""
    public_origin: str = ""
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimension: int = 1536
    openai_eval_model: str = "gpt-5.4-nano"
    openai_eval_model_escalated: str = "gpt-5.4-mini"
    openai_eval_timeout_seconds: int = 180
    gemini_api_key: str = ""
    gemini_eval_model: str = "gemini-3.5-flash"
    llm_eval_batch_size: int = 8
    llm_eval_parse_retries: int = 1
    llm_eval_concurrency: int = 1
    llm_requirement_aware_excerpt: bool = False
    llm_eval_max_jobs: int = 800
    llm_provider_failure_cooldown_min: int = 360
    storage_dir: str = "/storage"
    llm_prompt_version: int = 8
    feedback_analysis_poll_minutes: int = 5
    feedback_analysis_max_attempts: int = 5
    feedback_analysis_retry_minutes: int = 30
    learner_analysis_drain_budget_minutes: int = 5
    learner_daily_hour: int = 16
    learner_daily_minute: int = 45
    learned_discovery_query_cap: int = 12
    learned_exclusion_cap: int = 40
    feedback_decay_half_life_days: int = 60
    collector_daily_hour: int = 16
    collector_daily_minute: int = 0
    matcher_max_jobs: int = 1000
    matcher_local_max_jobs: int = 1000
    matcher_remote_max_jobs: int = 500
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
            "sisällöntuotanto",
            "viestintäasiantuntija",
            "tiedottaja",
            "koordinaattori",
            "projektikoordinaattori",
            "projektiassistentti",
            "hallintoassistentti",
            "toimistoassistentti",
            "johdon assistentti",
            "tapahtumakoordinaattori",
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
    jobly_scan_grace_hours: int = 72
    jobly_scan_retry_minutes: int = 60
    jobly_scan_closure_attempts: int = 3
    eures_url: str = (
        "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
    )
    eures_page_size: int = 50
    eures_max_pages: int = 5
    oulu_varbi_rss_url: str = "https://oulunyliopisto.varbi.com/fi/what:rssfeed/"
    oulu_varbi_base_url: str = "https://oulunyliopisto.varbi.com"
    collector_stale_run_minutes: int = 30
    pipeline_catchup_delay_minutes: int = 30
    source_stale_after_minutes: int = 1560
    retention_raw_listing_days: int = 365
    retention_activity_days: int = 180
    labelled_evaluation_path: str = ""
    kuntarekry_collection_mode: str = "regional"
    browserbase_api_key: str = ""
    google_maps_api_key: str = ""
    transit_origin_address: str = "Jalkatie 2, Oulu, Finland"
    recommendation_commute_limit_minutes: int = 120
    recommendation_transit_lookup_budget: int = 100
    transit_cache_ttl_days: int = 7
    transit_provider_backoff_min: int = 15
    enrichment_enabled: bool = False
    enrichment_provider: str = "browserbase"
    enrichment_max_jobs_per_run: int = 50
    enrichment_short_description_chars: int = 200
    jobly_browser_enrich_max_per_run: int = 20
    careerjet_api_key: str = ""
    careerjet_max_results_per_run: int = 50
    careerjet_user_ip: str = "127.0.0.1"
    linkedin_enabled: bool = False
    linkedin_max_results_per_run: int = 100
    linkedin_request_delay_seconds: float = 2.0
    linkedin_search_queries: list[str] = Field(
        default_factory=lambda: [
            "kirjastonhoitaja",
            "kirjastovirkailija",
            "informaatikko",
            "information specialist",
            "library",
            "content producer",
            "sisällöntuottaja",
            "kulttuurituottaja",
            "museo",
            "arkisto",
        ]
    )
    linkedin_browser_enrich_max_per_run: int = 10

    def browserbase_configured(self) -> bool:
        return bool(self.browserbase_api_key.strip())

    def google_maps_configured(self) -> bool:
        return bool(self.google_maps_api_key.strip())

    def llm_config_problems(self) -> list[str]:
        """Actionable configuration problems, reported without secret values.

        A blank provider is an intentional offline mode, not an error. Anything
        that would make every evaluation fail is reported once at startup
        instead of failing per candidate.
        """
        problems: list[str] = []
        provider = self.llm_provider.strip().lower()
        if not provider:
            return problems
        if provider not in {"openai", "gemini"}:
            problems.append(
                f"LLM_PROVIDER={provider!r} is unsupported; use 'openai', 'gemini' "
                "or leave it blank"
            )
        elif provider == "openai" and not self.openai_api_key.strip():
            problems.append("LLM_PROVIDER=openai but OPENAI_API_KEY is empty")
        elif provider == "gemini" and not self.gemini_api_key.strip():
            problems.append("LLM_PROVIDER=gemini but GEMINI_API_KEY is empty")
        model = (
            self.gemini_eval_model if provider == "gemini" else self.openai_eval_model
        ).strip()
        if not model:
            problems.append("the evaluation model for the selected provider is empty")
        if self.llm_eval_concurrency < 1:
            problems.append("LLM_EVAL_CONCURRENCY must be at least 1")
        if self.llm_eval_parse_retries < 0:
            problems.append("LLM_EVAL_PARSE_RETRIES cannot be negative")
        if self.llm_eval_max_jobs < 0:
            problems.append("LLM_EVAL_MAX_JOBS cannot be negative")
        if self.openai_eval_timeout_seconds <= 0:
            problems.append("OPENAI_EVAL_TIMEOUT_SECONDS must be positive")
        return problems


def _bool_env(name: str, default: bool) -> bool:
    raw = getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    return int(getenv(name, str(default)))


def _float_env(name: str, default: float) -> float:
    return float(getenv(name, str(default)))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    defaults = Settings()
    jobly_sitemap_raw = getenv("JOBLY_SITEMAP_URLS")
    if jobly_sitemap_raw:
        jobly_sitemap_urls = [
            part.strip() for part in jobly_sitemap_raw.split(",") if part.strip()
        ]
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
            part.strip() for part in discovery_search_raw.split(",") if part.strip()
        ]
    else:
        discovery_search_queries = defaults.discovery_search_queries
    linkedin_search_raw = getenv("LINKEDIN_SEARCH_QUERIES")
    if linkedin_search_raw:
        linkedin_search_queries = [
            part.strip() for part in linkedin_search_raw.split(",") if part.strip()
        ]
    else:
        linkedin_search_queries = defaults.linkedin_search_queries

    return Settings(
        database_url=getenv("DATABASE_URL", defaults.database_url),
        llm_provider=getenv("LLM_PROVIDER", ""),
        operator_api_token=getenv("OPERATOR_API_TOKEN", ""),
        public_origin=getenv("PUBLIC_ORIGIN", ""),
        openai_api_key=getenv("OPENAI_API_KEY", ""),
        openai_embedding_model=getenv(
            "OPENAI_EMBEDDING_MODEL", defaults.openai_embedding_model
        ),
        openai_embedding_dimension=_int_env(
            "OPENAI_EMBEDDING_DIMENSION",
            defaults.openai_embedding_dimension,
        ),
        openai_eval_model=getenv("OPENAI_EVAL_MODEL", defaults.openai_eval_model),
        openai_eval_model_escalated=getenv(
            "OPENAI_EVAL_MODEL_ESCALATED",
            defaults.openai_eval_model_escalated,
        ),
        openai_eval_timeout_seconds=_int_env(
            "OPENAI_EVAL_TIMEOUT_SECONDS",
            defaults.openai_eval_timeout_seconds,
        ),
        gemini_api_key=getenv("GEMINI_API_KEY", ""),
        gemini_eval_model=getenv("GEMINI_EVAL_MODEL", defaults.gemini_eval_model),
        llm_eval_batch_size=_int_env(
            "LLM_EVAL_BATCH_SIZE", defaults.llm_eval_batch_size
        ),
        llm_eval_parse_retries=_int_env(
            "LLM_EVAL_PARSE_RETRIES", defaults.llm_eval_parse_retries
        ),
        llm_eval_concurrency=_int_env(
            "LLM_EVAL_CONCURRENCY", defaults.llm_eval_concurrency
        ),
        llm_requirement_aware_excerpt=_bool_env(
            "LLM_REQUIREMENT_AWARE_EXCERPT", defaults.llm_requirement_aware_excerpt
        ),
        llm_eval_max_jobs=_int_env("LLM_EVAL_MAX_JOBS", defaults.llm_eval_max_jobs),
        llm_provider_failure_cooldown_min=_int_env(
            "LLM_PROVIDER_FAILURE_COOLDOWN_MIN",
            defaults.llm_provider_failure_cooldown_min,
        ),
        storage_dir=getenv("STORAGE_DIR", defaults.storage_dir),
        llm_prompt_version=_int_env("LLM_PROMPT_VERSION", defaults.llm_prompt_version),
        feedback_analysis_poll_minutes=_int_env(
            "FEEDBACK_ANALYSIS_POLL_MINUTES",
            defaults.feedback_analysis_poll_minutes,
        ),
        feedback_analysis_max_attempts=_int_env(
            "FEEDBACK_ANALYSIS_MAX_ATTEMPTS",
            defaults.feedback_analysis_max_attempts,
        ),
        feedback_analysis_retry_minutes=_int_env(
            "FEEDBACK_ANALYSIS_RETRY_MINUTES",
            defaults.feedback_analysis_retry_minutes,
        ),
        learner_analysis_drain_budget_minutes=_int_env(
            "LEARNER_ANALYSIS_DRAIN_BUDGET_MINUTES",
            defaults.learner_analysis_drain_budget_minutes,
        ),
        learner_daily_hour=_int_env("LEARNER_DAILY_HOUR", defaults.learner_daily_hour),
        learner_daily_minute=_int_env(
            "LEARNER_DAILY_MINUTE", defaults.learner_daily_minute
        ),
        learned_discovery_query_cap=_int_env(
            "LEARNED_DISCOVERY_QUERY_CAP",
            defaults.learned_discovery_query_cap,
        ),
        learned_exclusion_cap=_int_env(
            "LEARNED_EXCLUSION_CAP", defaults.learned_exclusion_cap
        ),
        feedback_decay_half_life_days=_int_env(
            "FEEDBACK_DECAY_HALF_LIFE_DAYS",
            defaults.feedback_decay_half_life_days,
        ),
        collector_daily_hour=_int_env(
            "COLLECTOR_DAILY_HOUR", defaults.collector_daily_hour
        ),
        collector_daily_minute=_int_env(
            "COLLECTOR_DAILY_MINUTE",
            defaults.collector_daily_minute,
        ),
        matcher_max_jobs=_int_env("MATCHER_MAX_JOBS", defaults.matcher_max_jobs),
        matcher_local_max_jobs=_int_env(
            "MATCHER_LOCAL_MAX_JOBS",
            defaults.matcher_local_max_jobs,
        ),
        matcher_remote_max_jobs=_int_env(
            "MATCHER_REMOTE_MAX_JOBS",
            defaults.matcher_remote_max_jobs,
        ),
        discovery_search_queries=discovery_search_queries,
        collector_enabled_sources=collector_enabled_sources,
        duunitori_page_size=_int_env(
            "DUUNITORI_PAGE_SIZE", defaults.duunitori_page_size
        ),
        duunitori_max_pages=_int_env(
            "DUUNITORI_MAX_PAGES", defaults.duunitori_max_pages
        ),
        tmt_page_size=_int_env("TMT_PAGE_SIZE", defaults.tmt_page_size),
        tmt_max_pages=_int_env("TMT_MAX_PAGES", defaults.tmt_max_pages),
        tmt_oulu_max_pages=_int_env("TMT_OULU_MAX_PAGES", defaults.tmt_oulu_max_pages),
        laura_page_size=_int_env("LAURA_PAGE_SIZE", defaults.laura_page_size),
        laura_max_pages=_int_env("LAURA_MAX_PAGES", defaults.laura_max_pages),
        jobly_sitemap_urls=jobly_sitemap_urls,
        jobly_max_urls_per_run=_int_env(
            "JOBLY_MAX_URLS_PER_RUN", defaults.jobly_max_urls_per_run
        ),
        jobly_scan_grace_hours=_int_env(
            "JOBLY_SCAN_GRACE_HOURS", defaults.jobly_scan_grace_hours
        ),
        jobly_scan_retry_minutes=_int_env(
            "JOBLY_SCAN_RETRY_MINUTES", defaults.jobly_scan_retry_minutes
        ),
        jobly_scan_closure_attempts=_int_env(
            "JOBLY_SCAN_CLOSURE_ATTEMPTS", defaults.jobly_scan_closure_attempts
        ),
        eures_page_size=_int_env("EURES_PAGE_SIZE", defaults.eures_page_size),
        eures_max_pages=_int_env("EURES_MAX_PAGES", defaults.eures_max_pages),
        collector_stale_run_minutes=_int_env(
            "COLLECTOR_STALE_RUN_MINUTES",
            defaults.collector_stale_run_minutes,
        ),
        pipeline_catchup_delay_minutes=_int_env(
            "PIPELINE_CATCHUP_DELAY_MINUTES",
            defaults.pipeline_catchup_delay_minutes,
        ),
        source_stale_after_minutes=_int_env(
            "SOURCE_STALE_AFTER_MINUTES",
            defaults.source_stale_after_minutes,
        ),
        retention_raw_listing_days=_int_env(
            "RETENTION_RAW_LISTING_DAYS", defaults.retention_raw_listing_days
        ),
        retention_activity_days=_int_env(
            "RETENTION_ACTIVITY_DAYS", defaults.retention_activity_days
        ),
        labelled_evaluation_path=getenv(
            "LABELLED_EVALUATION_PATH", defaults.labelled_evaluation_path
        ),
        kuntarekry_collection_mode=getenv(
            "KUNTAREKRY_COLLECTION_MODE",
            defaults.kuntarekry_collection_mode,
        ),
        browserbase_api_key=getenv("BROWSERBASE_API_KEY", ""),
        google_maps_api_key=getenv("GOOGLE_MAPS_API_KEY", ""),
        transit_origin_address=getenv(
            "TRANSIT_ORIGIN_ADDRESS", defaults.transit_origin_address
        ),
        recommendation_commute_limit_minutes=_int_env(
            "RECOMMENDATION_COMMUTE_LIMIT_MINUTES",
            defaults.recommendation_commute_limit_minutes,
        ),
        recommendation_transit_lookup_budget=_int_env(
            "RECOMMENDATION_TRANSIT_LOOKUP_BUDGET",
            defaults.recommendation_transit_lookup_budget,
        ),
        transit_cache_ttl_days=_int_env(
            "TRANSIT_CACHE_TTL_DAYS", defaults.transit_cache_ttl_days
        ),
        transit_provider_backoff_min=_int_env(
            "TRANSIT_PROVIDER_BACKOFF_MIN", defaults.transit_provider_backoff_min
        ),
        enrichment_enabled=_bool_env("ENRICHMENT_ENABLED", defaults.enrichment_enabled),
        enrichment_provider=getenv("ENRICHMENT_PROVIDER", defaults.enrichment_provider),
        enrichment_max_jobs_per_run=_int_env(
            "ENRICHMENT_MAX_JOBS_PER_RUN",
            defaults.enrichment_max_jobs_per_run,
        ),
        enrichment_short_description_chars=_int_env(
            "ENRICHMENT_SHORT_DESCRIPTION_CHARS",
            defaults.enrichment_short_description_chars,
        ),
        jobly_browser_enrich_max_per_run=_int_env(
            "JOBLY_BROWSER_ENRICH_MAX_PER_RUN",
            defaults.jobly_browser_enrich_max_per_run,
        ),
        careerjet_api_key=getenv("CAREERJET_API_KEY", defaults.careerjet_api_key),
        careerjet_max_results_per_run=_int_env(
            "CAREERJET_MAX_RESULTS_PER_RUN",
            defaults.careerjet_max_results_per_run,
        ),
        careerjet_user_ip=getenv("CAREERJET_USER_IP", defaults.careerjet_user_ip),
        linkedin_enabled=_bool_env("LINKEDIN_ENABLED", defaults.linkedin_enabled),
        linkedin_max_results_per_run=_int_env(
            "LINKEDIN_MAX_RESULTS_PER_RUN",
            defaults.linkedin_max_results_per_run,
        ),
        linkedin_request_delay_seconds=_float_env(
            "LINKEDIN_REQUEST_DELAY_SECONDS",
            defaults.linkedin_request_delay_seconds,
        ),
        linkedin_search_queries=linkedin_search_queries,
        linkedin_browser_enrich_max_per_run=_int_env(
            "LINKEDIN_BROWSER_ENRICH_MAX_PER_RUN",
            defaults.linkedin_browser_enrich_max_per_run,
        ),
    )
