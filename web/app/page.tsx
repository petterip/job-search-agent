import Link from "next/link";

import {
  formatCommuteLimitMinutes,
  RecommendationActionBadge,
  RecommendationEvidence,
  ScoreBadge,
} from "./ui";
import { formatFinnishDate, formatFinnishDateTime } from "./datetime";

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

type RecommendationScope = "commutable" | "commutable_or_full_remote" | "nationwide";

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
  travel_commute_limit_minutes: number | null;
  published_at: string | null;
  application_url: string | null;
  source_names: string[];
};

type RecommendationListResponse = {
  items: RecommendationListItem[];
  limit: number;
  offset: number;
  total: number;
  scope: RecommendationScope;
  scope_counts: {
    commutable: number;
    commutable_or_full_remote: number;
    nationwide: number;
    remote_only: number;
    nationwide_extra: number;
  };
};

type PageProps = {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
};

const PAGE_LIMIT = 30;
const RECOMMENDATION_LIMIT = 30;
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

const RECOMMENDATION_SCOPES: RecommendationScope[] = [
  "commutable",
  "commutable_or_full_remote",
  "nationwide",
];

type RecommendationScopeLabels = Record<RecommendationScope, string>;

function recommendationScopeLabels(
  recommendations: RecommendationListItem[],
): RecommendationScopeLabels {
  const configuredLimit =
    recommendations.find((item) => item.travel_commute_limit_minutes != null)
      ?.travel_commute_limit_minutes ?? null;
  const limit = formatCommuteLimitMinutes(configuredLimit);
  return {
    commutable: limit ? `Enintään ${limit}` : "Työmatka",
    commutable_or_full_remote: limit ? `${limit} + etä` : "Työmatka + etä",
    nationwide: "Koko maa",
  };
}

function recommendationScopeCount(
  counts: RecommendationListResponse["scope_counts"] | undefined,
  scope: RecommendationScope,
): number | null {
  if (!counts) return null;
  return counts[scope];
}

function recommendationScopeDelta(
  counts: RecommendationListResponse["scope_counts"] | undefined,
  scope: RecommendationScope,
): number | null {
  if (!counts) return null;
  if (scope === "commutable_or_full_remote") return counts.remote_only;
  if (scope === "nationwide") return counts.nationwide_extra;
  return null;
}

function recommendationRangeText(start: number, end: number, total: number): string {
  const formattedTotal = total.toLocaleString("fi-FI");
  if (end === 0) {
    return `Näytetään 0 / ${formattedTotal} osumaa`;
  }
  return `Näytetään ${start.toLocaleString("fi-FI")}–${end.toLocaleString("fi-FI")} / ${formattedTotal} osumaa`;
}

function recommendationScopeSummary(
  counts: RecommendationListResponse["scope_counts"] | undefined,
  scope: RecommendationScope,
  labels: RecommendationScopeLabels,
): string {
  if (!counts) return labels[scope];
  if (scope === "commutable_or_full_remote" && counts.remote_only === 0) {
    return "Etälaajennus ei lisää hyväksyttyjä etä-only suosituksia tällä hetkellä.";
  }
  if (scope === "commutable_or_full_remote") {
    return `${counts.remote_only.toLocaleString("fi-FI")} hyväksyttyä etä-only lisäystä.`;
  }
  if (scope === "nationwide") {
    return `${counts.nationwide_extra.toLocaleString("fi-FI")} lisäystä ${labels.commutable_or_full_remote} -näkymään verrattuna.`;
  }
  return labels[scope];
}

function buildHomePath(filters: {
  q: string;
  source: string;
  employer: string;
  location: string;
  offset: number;
  recommendationOffset: number;
  scope: RecommendationScope;
}): string {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.source) params.set("source", filters.source);
  if (filters.employer) params.set("employer", filters.employer);
  if (filters.location) params.set("location", filters.location);
  if (filters.offset > 0) params.set("offset", String(filters.offset));
  if (filters.recommendationOffset > 0) {
    params.set("recommendation_offset", String(filters.recommendationOffset));
  }
  if (filters.scope !== "commutable") params.set("scope", filters.scope);
  const query = params.toString();
  return query ? `/?${query}` : "/";
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
  // Private reads require the operator token when the API enforces it. This
  // runs server-side only, so the token is never sent to the browser.
  const token = process.env.OPERATOR_API_TOKEN?.trim();
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};

  try {
    const response = await fetch(`${baseUrl}${path}`, { cache: "no-store", headers });
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as T;
  } catch {
    return null;
  }
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
  const offset = Math.max(0, Number(firstParam(resolvedSearchParams.offset)) || 0);
  const recommendationOffset = Math.max(
    0,
    Number(firstParam(resolvedSearchParams.recommendation_offset)) || 0,
  );
  const scopeParam = firstParam(resolvedSearchParams.scope).trim();
  const scope: RecommendationScope =
    scopeParam === "commutable_or_full_remote" || scopeParam === "nationwide"
      ? scopeParam
      : "commutable";

  const [jobs, sourceList, sourceStatus, recommendations] = await Promise.all([
    getJson<JobListResponse>(buildJobsPath({ ...filters, offset })),
    getJson<SourceListResponse>("/sources"),
    getJson<SourceStatusResponse>("/sources/status"),
    getJson<RecommendationListResponse>(
      `/recommendations?scope=${scope}&limit=${RECOMMENDATION_LIMIT}&offset=${recommendationOffset}`,
    ),
  ]);

  const items = jobs?.items ?? [];
  const total = jobs?.total ?? 0;
  const nextOffset = offset + PAGE_LIMIT;
  const previousOffset = Math.max(0, offset - PAGE_LIMIT);
  const activeSources = (sourceStatus?.sources ?? []).filter((source) => source.enabled);
  const recommendationItems = recommendations?.items ?? [];
  const recommendationTotal = recommendations?.total ?? 0;
  const recommendationGroups = groupRecommendations(recommendationItems);
  const scopeLabels = recommendationScopeLabels(recommendationItems);
  const recommendationNextOffset = recommendationOffset + RECOMMENDATION_LIMIT;
  const recommendationPreviousOffset = Math.max(0, recommendationOffset - RECOMMENDATION_LIMIT);
  const recommendationRangeEnd = recommendationOffset + recommendationItems.length;

  return (
    <main className="app-shell">
      <section className="workspace">
        <header className="page-heading">
          <div>
            <p className="eyebrow">Paikallinen työnhakunäkymä</p>
            <h1>Työpaikkaportaali</h1>
          </div>
        </header>

        <form className="filter-bar" action="/" aria-label="Työpaikkahaku">
          <input type="hidden" name="scope" value={scope} />
          <label>
            <span>Hakusana</span>
            <input name="q" defaultValue={filters.q} placeholder="esim. data, opettaja" />
          </label>
          <label>
            <span>Työnantaja</span>
            <input name="employer" defaultValue={filters.employer} placeholder="esim. Yliopisto" />
          </label>
          <label>
            <span>Sijainti</span>
            <input name="location" defaultValue={filters.location} placeholder="esim. Helsinki" />
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
          <div className="recommendations-header">
            <div className="feed-heading">
              <div>
                <h2 id="recommendations-title">Suositukset</h2>
                <p>
                  {recommendations
                    ? `${recommendationRangeText(
                        recommendationOffset + 1,
                        recommendationRangeEnd,
                        recommendationTotal,
                      )} · ${recommendationScopeSummary(
                        recommendations.scope_counts,
                        scope,
                        scopeLabels,
                      )}`
                    : "Suosituksia ei voitu ladata"}
                </p>
              </div>
            </div>
            <div className="scope-toggle scope-toggle-bar" role="group" aria-label="Suositusten laajuus">
              {RECOMMENDATION_SCOPES.map((item) => {
                const count = recommendationScopeCount(recommendations?.scope_counts, item);
                const delta = recommendationScopeDelta(recommendations?.scope_counts, item);
                return (
                  <Link
                    key={item}
                    className={scope === item ? "scope-toggle-option is-active" : "scope-toggle-option"}
                    href={buildHomePath({
                      ...filters,
                      offset,
                      recommendationOffset: 0,
                      scope: item,
                    })}
                    aria-current={scope === item ? "page" : undefined}
                  >
                    <span>{scopeLabels[item]}</span>
                    {count !== null ? (
                      <small>
                        {count.toLocaleString("fi-FI")}
                        {delta !== null ? ` (+${delta.toLocaleString("fi-FI")})` : ""}
                      </small>
                    ) : null}
                  </Link>
                );
              })}
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
                          travelCommuteLimitMinutes={item.travel_commute_limit_minutes}
                        />
                      </Link>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <h2>Ei suosituksia tällä rajauksella</h2>
              <p>
                {scope === "commutable"
                  ? "Kokeile laajempaa näkymää, esimerkiksi kokopäiväisiä etätyöpaikkoja tai koko maan listaa."
                  : scope === "commutable_or_full_remote"
                    ? "Kokeile koko maan näkymää, jos haluat nähdä myös kaukaiset työpaikat."
                    : "Suorita suosittelupipeline, kun profiili on tallennettu tietokantaan."}
              </p>
              {scope === "commutable" ? (
                <p>
                  <Link
                    href={buildHomePath({
                      ...filters,
                      offset,
                      recommendationOffset: 0,
                      scope: "commutable_or_full_remote",
                    })}
                  >
                    Näytä {scopeLabels.commutable_or_full_remote}
                  </Link>
                  {" · "}
                  <Link
                    href={buildHomePath({
                      ...filters,
                      offset,
                      recommendationOffset: 0,
                      scope: "nationwide",
                    })}
                  >
                    Näytä koko maa
                  </Link>
                </p>
              ) : scope !== "nationwide" ? (
                <p>
                  <Link
                    href={buildHomePath({
                      ...filters,
                      offset,
                      recommendationOffset: 0,
                      scope: "nationwide",
                    })}
                  >
                    Näytä koko maa
                  </Link>
                </p>
              ) : null}
            </div>
          )}
          {recommendations && recommendationTotal > RECOMMENDATION_LIMIT ? (
            <nav className="pager" aria-label="Suositusten sivutus">
              <a
                aria-disabled={recommendationOffset === 0}
                href={buildHomePath({
                  ...filters,
                  offset,
                  recommendationOffset: recommendationPreviousOffset,
                  scope,
                })}
              >
                Edelliset suositukset
              </a>
              <a
                aria-disabled={recommendationNextOffset >= recommendationTotal}
                href={buildHomePath({
                  ...filters,
                  offset,
                  recommendationOffset: recommendationNextOffset,
                  scope,
                })}
              >
                Seuraavat suositukset
              </a>
            </nav>
          ) : null}
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
            <a
              aria-disabled={offset === 0}
              href={buildHomePath({ ...filters, offset: previousOffset, recommendationOffset, scope })}
            >
              Edelliset
            </a>
            <a
              aria-disabled={nextOffset >= total}
              href={buildHomePath({ ...filters, offset: nextOffset, recommendationOffset, scope })}
            >
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
                  <p className="job-meta">
                    {formatFinnishDate(job.published_at, "Julkaisuaika ei tiedossa")}
                  </p>
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
                    <p className="job-meta">
                      Viimeksi{" "}
                      {formatFinnishDateTime(
                        source.last_run_finished_at ?? source.last_run_started_at,
                        "Ei ajoa",
                      )}
                    </p>
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
