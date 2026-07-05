import Link from "next/link";

import { RecommendationActionBadge, RecommendationEvidence, ScoreBadge } from "./ui";

type HealthResponse = {
  status: "ok";
  service: string;
  checked_at: string;
  llm_enabled: boolean;
  transit_distance_enabled: boolean;
  embedding_model: string;
  embedding_dimension: number;
  eval_model: string;
};

type LocationEvidence = {
  text: string;
  tone: "good" | "warning" | "bad";
};

type JobListItem = {
  id: number;
  title: string;
  employer: string | null;
  location: string | null;
  published_at: string | null;
  status: string;
  application_url: string | null;
  source_names: string[];
};

type JobListResponse = {
  items: JobListItem[];
  limit: number;
  offset: number;
  total: number;
};

type SourceListResponse = {
  sources: string[];
};

type SourceStatusItem = {
  name: string;
  enabled: boolean;
  poll_interval_min: number;
  active_jobs: number;
  stored_listings: number;
  last_run_status: string | null;
  last_run_started_at: string | null;
  last_run_finished_at: string | null;
  fetched_count: number;
  inserted_count: number;
  updated_count: number;
  unchanged_count: number;
  failed_count: number;
  error_summary: string | null;
  recent_error_events: number;
  recent_warning_events: number;
};

type SourceStatusResponse = {
  sources: SourceStatusItem[];
};

type RecommendationListItem = {
  id: number;
  rank: number | null;
  machine_score: number;
  llm_score: number | null;
  fit_tier: string | null;
  suggested_action: string | null;
  recommendation_category: string;
  hidden_opportunity: boolean;
  rationale: string | null;
  concerns: string[];
  job_id: number;
  title: string;
  employer: string | null;
  location: string | null;
  location_evidence: LocationEvidence | null;
  published_at: string | null;
  application_url: string | null;
  source_names: string[];
};

type RecommendationListResponse = {
  items: RecommendationListItem[];
  limit: number;
  offset: number;
  total: number;
};

type PageProps = {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
};

const PAGE_LIMIT = 30;
const RECOMMENDATION_LIMIT = 15;
const RECOMMENDATION_CATEGORY_ORDER = [
  "Kirjasto ja tietopalvelu",
  "Johtaminen ja hallinto",
  "Musiikki, kirjallisuus ja sisällöt",
  "Kulttuuri, tapahtumat ja viestintä",
  "Ohjaus ja asiakastyö",
  "Piilo-osumat",
  "Muut vahvuusosumat",
];

function firstParam(value: string | string[] | undefined): string {
  if (Array.isArray(value)) {
    return value[0] ?? "";
  }
  return value ?? "";
}

function buildJobsPath(filters: {
  q: string;
  source: string;
  employer: string;
  location: string;
  offset: number;
}): string {
  const params = new URLSearchParams();
  params.set("limit", String(PAGE_LIMIT));
  params.set("offset", String(filters.offset));
  if (filters.q) params.set("q", filters.q);
  if (filters.source) params.set("source", filters.source);
  if (filters.employer) params.set("employer", filters.employer);
  if (filters.location) params.set("location", filters.location);
  return `/jobs?${params.toString()}`;
}

async function getJson<T>(path: string): Promise<T | null> {
  const baseUrl = process.env.INTERNAL_API_BASE_URL ?? "http://localhost:8008";

  try {
    const response = await fetch(`${baseUrl}${path}`, { cache: "no-store" });
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

function formatDate(value: string | null): string {
  if (!value) {
    return "Julkaisuaika ei tiedossa";
  }
  return new Intl.DateTimeFormat("fi-FI", {
    day: "numeric",
    month: "numeric",
    year: "numeric",
  }).format(new Date(value));
}

function formatDateTime(value: string | null): string {
  if (!value) {
    return "Ei ajoa";
  }
  return new Intl.DateTimeFormat("fi-FI", {
    day: "numeric",
    month: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function pageHref(
  filters: { q: string; source: string; employer: string; location: string },
  offset: number,
): string {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.source) params.set("source", filters.source);
  if (filters.employer) params.set("employer", filters.employer);
  if (filters.location) params.set("location", filters.location);
  if (offset > 0) params.set("offset", String(offset));
  const query = params.toString();
  return query ? `/?${query}` : "/";
}

function groupRecommendations(items: RecommendationListItem[]): [string, RecommendationListItem[]][] {
  const groups = new Map<string, RecommendationListItem[]>();
  for (const item of items) {
    const existing = groups.get(item.recommendation_category) ?? [];
    existing.push(item);
    groups.set(item.recommendation_category, existing);
  }

  return Array.from(groups.entries()).sort(([first], [second]) => {
    const firstIndex = RECOMMENDATION_CATEGORY_ORDER.indexOf(first);
    const secondIndex = RECOMMENDATION_CATEGORY_ORDER.indexOf(second);
    return (firstIndex === -1 ? 999 : firstIndex) - (secondIndex === -1 ? 999 : secondIndex);
  });
}

function recommendationCategoryId(category: string): string {
  return `recommendation-group-${category
    .toLowerCase()
    .replaceAll("ä", "a")
    .replaceAll("ö", "o")
    .replaceAll("å", "a")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")}`;
}

export default async function Home({ searchParams }: PageProps) {
  const resolvedSearchParams = (await searchParams) ?? {};
  const filters = {
    q: firstParam(resolvedSearchParams.q).trim(),
    source: firstParam(resolvedSearchParams.source).trim(),
    employer: firstParam(resolvedSearchParams.employer).trim(),
    location: firstParam(resolvedSearchParams.location).trim(),
  };
  const offset = Number(firstParam(resolvedSearchParams.offset)) || 0;

  const [health, jobs, sourceList, sourceStatus, recommendations] = await Promise.all([
    getJson<HealthResponse>("/health"),
    getJson<JobListResponse>(buildJobsPath({ ...filters, offset })),
    getJson<SourceListResponse>("/sources"),
    getJson<SourceStatusResponse>("/sources/status"),
    getJson<RecommendationListResponse>(`/recommendations?limit=${RECOMMENDATION_LIMIT}`),
  ]);

  const items = jobs?.items ?? [];
  const total = jobs?.total ?? 0;
  const nextOffset = offset + PAGE_LIMIT;
  const previousOffset = Math.max(0, offset - PAGE_LIMIT);
  const activeSources = (sourceStatus?.sources ?? []).filter((source) => source.enabled);
  const recommendationGroups = recommendations ? groupRecommendations(recommendations.items) : [];

  return (
    <main className="app-shell">
      <section className="workspace">
        <header className="page-heading">
          <div>
            <p className="eyebrow">Paikallinen työnhakunäkymä</p>
            <h1>Työpaikkaportaali</h1>
          </div>
          <div className="system-status" aria-label="Järjestelmän tila">
            <span className={health ? "status-dot is-ok" : "status-dot is-down"} />
            <span>{health ? "API käytössä" : "API ei vastaa"}</span>
          </div>
        </header>

        <form className="filter-bar" action="/" aria-label="Työpaikkahaku">
          <label>
            <span>Hakusana</span>
            <input name="q" defaultValue={filters.q} placeholder="esim. data, opettaja" />
          </label>
          <label>
            <span>Työnantaja</span>
            <input name="employer" defaultValue={filters.employer} placeholder="esim. Oulun yliopisto" />
          </label>
          <label>
            <span>Sijainti</span>
            <input name="location" defaultValue={filters.location} placeholder="esim. Oulu" />
          </label>
          <label>
            <span>Lähde</span>
            <select name="source" defaultValue={filters.source}>
              <option value="">Kaikki lähteet</option>
              {(sourceList?.sources ?? []).map((source) => (
                <option key={source} value={source}>
                  {source}
                </option>
              ))}
            </select>
          </label>
          <button type="submit">Hae</button>
        </form>

        <section className="recommendations" aria-labelledby="recommendations-title">
          <div className="feed-heading">
            <div>
              <h2 id="recommendations-title">Suositukset</h2>
              <p>
                {recommendations
                  ? `${recommendations.total.toLocaleString("fi-FI")} tallennettua osumaa`
                  : "Suosituksia ei voitu ladata"}
              </p>
            </div>
          </div>
          {recommendationGroups.length > 0 ? (
            <div className="recommendation-groups">
              {recommendationGroups.map(([category, categoryItems]) => (
                <section className="recommendation-group" key={category} aria-labelledby={recommendationCategoryId(category)}>
                  <div className="recommendation-group-heading">
                    <h3 id={recommendationCategoryId(category)}>{category}</h3>
                    <span>{categoryItems.length.toLocaleString("fi-FI")}</span>
                  </div>
                  <div className="recommendation-grid">
                    {categoryItems.map((item) => (
                      <Link className="recommendation-card" href={`/tyopaikat/${item.job_id}`} key={item.id}>
                        <div className="recommendation-card-topline">
                          <ScoreBadge rank={item.rank} score={item.llm_score ?? item.machine_score} />
                          <RecommendationActionBadge value={item.suggested_action} />
                        </div>
                        {item.hidden_opportunity ? <p className="recommendation-category">Piilo-osuma</p> : null}
                        <h2>{item.title}</h2>
                        <p className="job-employer">{item.employer ?? "Työnantaja ei tiedossa"}</p>
                        <p className="job-location">
                          {item.location_evidence?.text ?? item.location ?? "Sijainti ei tiedossa"}
                        </p>
                        <RecommendationEvidence
                          concerns={item.concerns}
                          idPrefix={`recommendation-${item.id}`}
                          location={item.location}
                          locationEvidence={item.location_evidence}
                          rationale={item.rationale}
                          score={item.llm_score ?? item.machine_score}
                        />
                      </Link>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <h2>Ei suosituksia vielä</h2>
              <p>Suorita suosittelupipeline, kun profiili on tallennettu tietokantaan.</p>
            </div>
          )}
        </section>

        <section className="feed-heading" aria-live="polite">
          <div>
            <h2>Uusimmat työpaikat</h2>
            <p>
              {jobs
                ? `${total.toLocaleString("fi-FI")} aktiivista ilmoitusta`
                : "Työpaikkoja ei voitu ladata"}
            </p>
          </div>
          <div className="pager">
            <a aria-disabled={offset === 0} href={pageHref(filters, previousOffset)}>
              Edelliset
            </a>
            <a aria-disabled={nextOffset >= total} href={pageHref(filters, nextOffset)}>
              Seuraavat
            </a>
          </div>
        </section>

        <section className="job-list" aria-label="Työpaikat">
          {items.length === 0 ? (
            <div className="empty-state">
              <h2>Ei tuloksia</h2>
              <p>Kokeile väljempiä hakuehtoja tai tarkista keruun tila myöhemmin.</p>
            </div>
          ) : (
            items.map((job) => (
              <article className="job-row" key={job.id}>
                <Link className="job-row-link" href={`/tyopaikat/${job.id}`}>
                  <p className="job-meta">{formatDate(job.published_at)}</p>
                  <h2>{job.title}</h2>
                  <p className="job-employer">{job.employer ?? "Työnantaja ei tiedossa"}</p>
                  <p className="job-location">{job.location ?? "Sijainti ei tiedossa"}</p>
                  <p className="job-sources">{job.source_names.join(", ")}</p>
                </Link>
                {job.application_url ? (
                  <div className="job-row-actions">
                    <a
                      className="apply-link apply-link-primary"
                      href={job.application_url}
                      rel="noreferrer"
                      target="_blank"
                    >
                      Ilmoitus
                    </a>
                  </div>
                ) : null}
              </article>
            ))
          )}
        </section>

        <section className="source-health" aria-labelledby="source-health-title">
          <details className="source-health-accordion">
            <summary>
              <span>
                <strong id="source-health-title">Lähteiden tila</strong>
                <span>
                  {sourceStatus
                    ? `${activeSources.length.toLocaleString("fi-FI")} aktiivista lähdettä`
                    : "Lähteiden tilaa ei voitu ladata"}
                </span>
              </span>
            </summary>
            {sourceStatus ? (
              <div className="source-health-grid">
                {activeSources.map((source) => (
                  <article className="source-health-card" key={source.name}>
                    <div className="source-health-title">
                      <h3>{source.name}</h3>
                      <span className={`run-status run-status-${source.last_run_status ?? "missing"}`}>
                        {source.last_run_status ?? "ei ajoa"}
                      </span>
                    </div>
                    <p className="job-meta">Viimeksi {formatDateTime(source.last_run_finished_at ?? source.last_run_started_at)}</p>
                    <p className="job-meta">
                      {source.active_jobs.toLocaleString("fi-FI")} aktiivista ·{" "}
                      {source.stored_listings.toLocaleString("fi-FI")} tallennettua
                    </p>
                    <p className="job-meta">
                      Viime ajossa {source.fetched_count.toLocaleString("fi-FI")} haettu ·{" "}
                      {source.inserted_count.toLocaleString("fi-FI")} uutta ·{" "}
                      {source.updated_count.toLocaleString("fi-FI")} päivitetty ·{" "}
                      {source.unchanged_count.toLocaleString("fi-FI")} ennallaan
                    </p>
                    {(source.failed_count > 0 || source.recent_error_events > 0 || source.recent_warning_events > 0) ? (
                      <p className="recommendation-concern">
                        {source.failed_count.toLocaleString("fi-FI")} epäonnistunutta ·{" "}
                        {source.recent_error_events.toLocaleString("fi-FI")} virhettä ·{" "}
                        {source.recent_warning_events.toLocaleString("fi-FI")} varoitusta
                      </p>
                    ) : null}
                    {source.error_summary ? <p className="job-sources">{source.error_summary}</p> : null}
                  </article>
                ))}
              </div>
            ) : null}
          </details>
        </section>
      </section>
    </main>
  );
}
