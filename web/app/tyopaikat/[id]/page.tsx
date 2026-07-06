import { notFound } from "next/navigation";
import { JobDescription, RecommendationActionBadge, RecommendationEvidence, ScoreBadge } from "../../ui";

type JobSourceItem = {
  source_name: string;
  application_url: string | null;
  external_apply_url: string | null;
  attribution: string | null;
  last_seen_at: string;
};

type RecommendationFeedback = {
  rating: number;
  applied: boolean;
  analysis_status: string;
  comment?: string | null;
  hypothesis_fi?: string | null;
};

function feedbackAnalysisNote(feedback: RecommendationFeedback): string | null {
  switch (feedback.analysis_status) {
    case "pending":
      return "Analyysi valmistuu taustalla.";
    case "completed":
      return null;
    case "failed":
      return "Analyysin tuottaminen epäonnistui; yritä tallentaa arvio uudelleen.";
    case "skipped":
      return "Analyysi ohitettiin (esim. palveluntarjoaja ei käytettävissä).";
    default:
      return null;
  }
}

type RecommendationItem = {
  id: number;
  rank: number | null;
  machine_score: number;
  llm_score: number | null;
  fit_tier: string | null;
  suggested_action: string | null;
  rationale: string | null;
  concerns: string[];
  is_active: boolean;
  feedback: RecommendationFeedback | null;
  location_evidence: {
    text: string;
    tone: "good" | "warning" | "bad";
  } | null;
};

const RATING_OPTIONS = [
  { value: 1, label: "Todella huono" },
  { value: 2, label: "Huono" },
  { value: 3, label: "Neutraali" },
  { value: 4, label: "Hyvä" },
  { value: 5, label: "Erittäin hyvä" },
] as const;

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
  const primaryExternalApplyUrl =
    job.sources.find((source) => source.external_apply_url)?.external_apply_url ?? null;
  const feedbackStatusNote = job.recommendation?.feedback
    ? feedbackAnalysisNote(job.recommendation.feedback)
    : null;

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
            <div className="job-row-actions detail-apply-link">
              <a className="apply-link apply-link-primary" href={primaryApplicationUrl} rel="noreferrer" target="_blank">
                Katso hakuilmoitus
              </a>
              {primaryExternalApplyUrl ? (
                <a className="apply-link" href={primaryExternalApplyUrl} rel="noreferrer" target="_blank">
                  Hae työnantajan sivulla
                </a>
              ) : null}
            </div>
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
            {!job.recommendation.is_active || (job.recommendation.feedback?.rating ?? 5) <= 2 ? (
              <p className="feedback-hidden-note" role="status">
                Piilotettu suosituksista
              </p>
            ) : null}
            <RecommendationEvidence
              concerns={job.recommendation.concerns}
              idPrefix={`recommendation-${job.recommendation.id}`}
              location={job.location}
              locationEvidence={job.recommendation.location_evidence ?? job.location_evidence}
              rationale={job.recommendation.rationale}
              score={job.recommendation.llm_score ?? job.recommendation.machine_score}
            />
            <p className="job-meta">Oma arvio</p>
            <form className="feedback-form" action={`/suositukset/${job.recommendation.id}/palaute`} method="post">
              <input type="hidden" name="job_id" value={job.id} />
              <fieldset className="feedback-rating-group">
                <legend className="sr-only">Arvioi suositus asteikolla 1–5</legend>
                {RATING_OPTIONS.map((option) => {
                  const selected = job.recommendation?.feedback?.rating === option.value;
                  return (
                    <label
                      className={`feedback-rating-option${selected ? " is-selected" : ""}`}
                      key={option.value}
                    >
                      <input
                        aria-label={option.label}
                        defaultChecked={selected}
                        name="rating"
                        required={job.recommendation?.feedback === null}
                        type="radio"
                        value={option.value}
                      />
                      <span>{option.label}</span>
                    </label>
                  );
                })}
              </fieldset>
              <label className="feedback-applied-toggle">
                <input
                  defaultChecked={job.recommendation.feedback?.applied ?? false}
                  name="applied"
                  type="checkbox"
                />
                <span>Merkitty haetuksi</span>
              </label>
              <label className="feedback-comment-field">
                <span className="feedback-comment-label">Vapaaehtoinen kommentti</span>
                <textarea
                  className="feedback-comment-input"
                  defaultValue={job.recommendation.feedback?.comment ?? ""}
                  maxLength={1000}
                  name="comment"
                  placeholder="Miksi tämä arvio? (ei pakollinen)"
                  rows={3}
                />
              </label>
              {feedbackStatusNote ? (
                <p className="job-meta" role="status">
                  {feedbackStatusNote}
                </p>
              ) : null}
              {job.recommendation.feedback?.hypothesis_fi ? (
                <div className="feedback-analysis-note">
                  <p className="feedback-analysis-label">Järjestelmän arvio palautteesta</p>
                  <p className="feedback-analysis-text">{job.recommendation.feedback.hypothesis_fi}</p>
                  <p className="feedback-analysis-disclaimer">
                    Tämä on järjestelmän hypoteesi, ei vahvistettu käyttäjän lausuma.
                  </p>
                </div>
              ) : null}
              <button className="feedback-submit" type="submit">
                Tallenna arvio
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
              <div className="source-row" key={`${source.source_name}-${source.last_seen_at}`}>
                <div>
                  <p className="job-employer">{source.source_name}</p>
                  <p className="job-meta">Viimeksi nähty {formatDate(source.last_seen_at)}</p>
                  {source.attribution ? <p className="job-sources">{source.attribution}</p> : null}
                </div>
                <div className="job-row-actions">
                  {source.application_url ? (
                    <a className="apply-link" href={source.application_url} rel="noreferrer" target="_blank">
                      Katso hakuilmoitus
                    </a>
                  ) : (
                    <span className="apply-link is-disabled">Ei linkkiä</span>
                  )}
                  {source.external_apply_url ? (
                    <a className="apply-link" href={source.external_apply_url} rel="noreferrer" target="_blank">
                      Hae työnantajan sivulla
                    </a>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        </section>
      </article>
    </main>
  );
}
