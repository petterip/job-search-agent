import { notFound } from "next/navigation";
import { JobDescription, RecommendationActionBadge, RecommendationEvidence, ScoreBadge } from "../../ui";

type JobSourceItem = {
  source_name: string;
  application_url: string | null;
  attribution: string | null;
  last_seen_at: string;
};

type RecommendationItem = {
  id: number;
  rank: number | null;
  machine_score: number;
  llm_score: number | null;
  fit_tier: string | null;
  suggested_action: string | null;
  rationale: string | null;
  concerns: string[];
  location_evidence: {
    text: string;
    tone: "good" | "warning" | "bad";
  } | null;
};

type JobDetailResponse = {
  id: number;
  title: string;
  employer: string | null;
  description: string | null;
  location: string | null;
  location_evidence: {
    text: string;
    tone: "good" | "warning" | "bad";
  } | null;
  published_at: string | null;
  status: string;
  sources: JobSourceItem[];
  recommendation: RecommendationItem | null;
};

type PageProps = {
  params: Promise<{ id: string }>;
};

async function getJob(id: string): Promise<JobDetailResponse | null> {
  const baseUrl = process.env.INTERNAL_API_BASE_URL ?? "http://localhost:8008";
  const response = await fetch(`${baseUrl}/jobs/${id}`, { cache: "no-store" });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error("Työpaikan lataus epäonnistui");
  }
  return (await response.json()) as JobDetailResponse;
}

function formatDate(value: string | null): string {
  if (!value) {
    return "Ei tiedossa";
  }
  return new Intl.DateTimeFormat("fi-FI", {
    day: "numeric",
    month: "numeric",
    year: "numeric",
  }).format(new Date(value));
}

export default async function JobPage({ params }: PageProps) {
  const { id } = await params;
  const job = await getJob(id);
  if (!job) {
    notFound();
  }
  const primaryApplicationUrl = job.sources.find((source) => source.application_url)?.application_url ?? null;

  return (
    <main className="app-shell">
      <article className="workspace detail-layout">
        <nav className="breadcrumb" aria-label="Murupolku">
          <a href="/">Takaisin hakuun</a>
        </nav>

        <header className="detail-heading">
          <p className="eyebrow">Työpaikka</p>
          <h1>{job.title}</h1>
          <dl className="detail-facts">
            <div>
              <dt>Työnantaja</dt>
              <dd>{job.employer ?? "Ei tiedossa"}</dd>
            </div>
            <div>
              <dt>Sijainti</dt>
              <dd>{job.location ?? "Ei tiedossa"}</dd>
            </div>
            {job.location_evidence ? (
              <div>
                <dt>Matka Oulusta</dt>
                <dd>{job.location_evidence.text}</dd>
              </div>
            ) : null}
            <div>
              <dt>Julkaistu</dt>
              <dd>{formatDate(job.published_at)}</dd>
            </div>
          </dl>
          {primaryApplicationUrl ? (
            <a className="apply-link apply-link-primary detail-apply-link" href={primaryApplicationUrl} rel="noreferrer" target="_blank">
              Katso hakuilmoitus
            </a>
          ) : null}
        </header>

        {job.recommendation ? (
          <section className="detail-panel" aria-labelledby="recommendation-heading">
            <div className="detail-panel-heading">
              <h2 id="recommendation-heading">Suosituksen perustelu</h2>
              <div className="recommendation-card-topline">
                <ScoreBadge
                  rank={job.recommendation.rank}
                  score={job.recommendation.llm_score ?? job.recommendation.machine_score}
                />
                <RecommendationActionBadge value={job.recommendation.suggested_action} />
              </div>
            </div>
            <RecommendationEvidence
              concerns={job.recommendation.concerns}
              idPrefix={`recommendation-${job.recommendation.id}`}
              location={job.location}
              locationEvidence={job.recommendation.location_evidence ?? job.location_evidence}
              rationale={job.recommendation.rationale}
              score={job.recommendation.llm_score ?? job.recommendation.machine_score}
            />
            <p className="job-meta">Oma merkintä</p>
            <form className="feedback-actions" action={`/suositukset/${job.recommendation.id}/palaute`} method="post">
              <input type="hidden" name="job_id" value={job.id} />
              <button name="action" value="good_match" type="submit">
                Merkitse sopivaksi
              </button>
              <button name="action" value="not_relevant" type="submit">
                Piilota suosituksista
              </button>
              <button name="action" value="applied" type="submit">
                Merkitse haetuksi
              </button>
            </form>
          </section>
        ) : null}

        <section className="detail-panel" aria-labelledby="description-heading">
          <h2 id="description-heading">Kuvaus</h2>
          <JobDescription value={job.description} />
        </section>

        <section className="detail-panel" aria-labelledby="sources-heading">
          <h2 id="sources-heading">Lähteet ja hakulinkit</h2>
          <div className="source-list">
            {job.sources.map((source) => (
              <div className="source-row" key={`${source.source_name}-${source.application_url ?? ""}`}>
                <div>
                  <p className="job-employer">{source.source_name}</p>
                  <p className="job-meta">Viimeksi nähty {formatDate(source.last_seen_at)}</p>
                  {source.attribution ? <p className="job-sources">{source.attribution}</p> : null}
                </div>
                {source.application_url ? (
                  <a className="apply-link" href={source.application_url} rel="noreferrer" target="_blank">
                    Katso hakuilmoitus
                  </a>
                ) : (
                  <span className="apply-link is-disabled">Ei linkkiä</span>
                )}
              </div>
            ))}
          </div>
        </section>
      </article>
    </main>
  );
}
