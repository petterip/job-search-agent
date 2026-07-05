import type { ReactNode } from "react";

type ActionTone = "apply" | "consider" | "skip" | "neutral";
type EvidenceTone = "good" | "warning" | "bad";

export type LocationEvidence = {
  text: string;
  tone: EvidenceTone;
};

const actionLabels: Record<string, { label: string; tone: ActionTone }> = {
  apply: { label: "Hae", tone: "apply" },
  consider: { label: "Harkitse", tone: "consider" },
  skip: { label: "Ohita", tone: "skip" },
};

function actionMeta(value: string | null | undefined): { label: string; tone: ActionTone } | null {
  if (!value) return null;
  return actionLabels[value] ?? { label: value, tone: "neutral" };
}

export function RecommendationActionBadge({ value }: { value: string | null | undefined }) {
  const meta = actionMeta(value);
  if (!meta) return null;

  return (
    <span className={`recommendation-action recommendation-action-${meta.tone}`} aria-label={`Suositus: ${meta.label}`}>
      <span aria-hidden="true">Suositus</span>
      <strong>{meta.label}</strong>
    </span>
  );
}

export function ScoreBadge({ rank, score }: { rank: number | null; score: number }) {
  const roundedScore = Math.round(score);
  return (
    <div className="score-badge" aria-label={`Sija ${rank ?? "-"}, pisteet ${roundedScore} sadasta`}>
      <span>#{rank ?? "-"}</span>
      <strong>{roundedScore}</strong>
      <span>/ 100</span>
    </div>
  );
}

function workMode(location: string | null): "remote" | "hybrid" | null {
  const normalized = (location ?? "").toLocaleLowerCase("fi-FI");
  if (normalized.includes("/ etä") || normalized.endsWith(" etä")) return "remote";
  if (normalized.includes("/ hybridi")) return "hybrid";
  return null;
}

function sentenceParts(value: string | null): string[] {
  return (value ?? "")
    .split(/(?<=[.!?])\s+/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function fallbackLocationEvidence(location: string | null): LocationEvidence | null {
  const mode = workMode(location);
  if (mode === "remote") {
    return { text: "Etätyö · sijainti joustava", tone: "good" };
  }
  if (!location) {
    return null;
  }
  return {
    text: `${location} · julkisen liikenteen matka Oulusta ei tiedossa`,
    tone: "warning",
  };
}

function resolveLocationEvidence(
  location: string | null,
  locationEvidence: LocationEvidence | null | undefined,
): LocationEvidence | null {
  return locationEvidence ?? fallbackLocationEvidence(location);
}

function concernTone(text: string, locationFit: LocationEvidence | null): EvidenceTone {
  if (locationFit && text === locationFit.text) {
    return locationFit.tone;
  }
  if (/kelpoisuus|puuttuu|edellyttää/i.test(text)) {
    return "bad";
  }
  if (/km ·|julkiset|matka oulusta ei tiedossa/i.test(text)) {
    return locationFit?.tone ?? "warning";
  }
  if (/sijainti/i.test(text)) {
    return "warning";
  }
  return "warning";
}

function locationAlreadyInConcerns(concerns: string[], locationFit: LocationEvidence): boolean {
  return concerns.some(
    (concern) =>
      concern === locationFit.text ||
      concern.includes(locationFit.text) ||
      locationFit.text.includes(concern),
  );
}

function concernText(concern: string, locationEvidence: LocationEvidence | null): string {
  if (/sijainti ei osu/i.test(concern)) {
    return locationEvidence?.text ?? concern;
  }
  return concern;
}

function evidenceItems({
  rationale,
  concerns,
  location,
  locationEvidence,
  score,
}: {
  rationale: string | null;
  concerns: string[];
  location: string | null;
  locationEvidence: LocationEvidence | null;
  score: number;
}): { pros: string[]; cons: { text: string; tone: EvidenceTone }[] } {
  const locationFit = locationEvidence;
  const rationalePros = sentenceParts(rationale)
    .filter((part) => !/^Sijainti\b/i.test(part))
    .slice(0, 2);
  const pros = [...rationalePros];

  if (score >= 80) pros.unshift("Vahva kokonaisosuma profiiliin.");
  else if (score >= 65) pros.unshift("Kohtalaisen vahva osuma profiiliin.");

  if (locationFit?.tone === "good") pros.push(locationFit.text);

  const cons = concerns.map((concern) => {
    const text = concernText(concern, locationFit);
    return { text, tone: concernTone(text, locationFit) };
  });

  if (
    locationFit &&
    locationFit.tone !== "good" &&
    !cons.some((item) => item.text === locationFit.text) &&
    !locationAlreadyInConcerns(concerns, locationFit)
  ) {
    cons.unshift(locationFit);
  }

  return {
    pros: Array.from(new Set(pros)).slice(0, 3),
    cons: cons.slice(0, 3),
  };
}

export function RecommendationEvidence({
  idPrefix,
  rationale,
  concerns,
  location,
  locationEvidence,
  score,
}: {
  idPrefix: string;
  rationale: string | null;
  concerns: string[];
  location: string | null;
  locationEvidence?: LocationEvidence | null;
  score: number;
}) {
  const resolvedLocationEvidence = resolveLocationEvidence(location, locationEvidence);
  const { pros, cons } = evidenceItems({
    rationale,
    concerns,
    location,
    locationEvidence: resolvedLocationEvidence,
    score,
  });

  return (
    <div className="fit-evidence">
      <section className="fit-evidence-panel fit-evidence-panel-good" aria-labelledby={`${idPrefix}-pros-title`}>
        <h3 id={`${idPrefix}-pros-title`}>Plussat</h3>
        {pros.length > 0 ? (
          <ul>
            {pros.map((item) => (
              <li key={item}>
                <span aria-hidden="true">+</span>
                <span>{item}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p>Ei vahvoja plussia näkyvissä.</p>
        )}
      </section>
      <section className="fit-evidence-panel fit-evidence-panel-risk" aria-labelledby={`${idPrefix}-cons-title`}>
        <h3 id={`${idPrefix}-cons-title`}>Huomiot</h3>
        {cons.length > 0 ? (
          <ul>
            {cons.map((item) => (
              <li className={`fit-evidence-${item.tone}`} key={item.text}>
                <span aria-hidden="true">{item.tone === "bad" ? "-" : "!"}</span>
                <span>{item.text}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p>Ei merkittäviä huomioita.</p>
        )}
      </section>
    </div>
  );
}

function isListItem(line: string): boolean {
  return /^([-*•]\s+|\d+[.)]\s+)/.test(line.trim());
}

function cleanListItem(line: string): string {
  return line.trim().replace(/^([-*•]\s+|\d+[.)]\s+)/, "").trim();
}

function isLikelyHeading(line: string): boolean {
  const value = line.trim();
  if (value.length < 3 || value.length > 72) return false;
  if (/^https?:\/\//i.test(value)) return false;
  if (/^[A-ZÅÄÖ0-9][^.!?]+:$/.test(value)) return true;
  return /^(tehtävän kuvaus|työtehtävät|kelpoisuus|kelpoisuusvaatimukset|edellytämme|odotamme|arvostamme|tarjoamme|palkkaus|työaika|lisätiedot|yhteystiedot|hae tehtävää|hakeminen|työnantajan kuvaus|meistä)$/i.test(
    value,
  );
}

function renderTextWithLinks(text: string): ReactNode {
  const parts = text.split(/(https?:\/\/[^\s]+)/g);
  return parts.map((part, index) => {
    if (/^https?:\/\//i.test(part)) {
      return (
        <a href={part} key={`${part}-${index}`} rel="noreferrer" target="_blank">
          {part.replace(/^https?:\/\//i, "")}
        </a>
      );
    }
    return part;
  });
}

export function JobDescription({ value }: { value: string | null }) {
  if (!value?.trim()) {
    return <p className="job-description-empty">Kuvausta ei ole saatavilla tästä lähteestä.</p>;
  }

  const lines = value
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  const blocks: ReactNode[] = [];
  let listItems: string[] = [];

  const flushList = () => {
    if (listItems.length === 0) return;
    const currentItems = listItems;
    blocks.push(
      <ul className="job-description-list" key={`list-${blocks.length}`}>
        {currentItems.map((item, index) => (
          <li key={`${item}-${index}`}>{renderTextWithLinks(item)}</li>
        ))}
      </ul>,
    );
    listItems = [];
  };

  lines.forEach((line) => {
    if (isListItem(line)) {
      listItems.push(cleanListItem(line));
      return;
    }

    flushList();
    if (isLikelyHeading(line)) {
      blocks.push(
        <h3 key={`heading-${blocks.length}`} className="job-description-heading">
          {line.replace(/:$/, "")}
        </h3>,
      );
      return;
    }

    blocks.push(
      <p className="job-description-paragraph" key={`paragraph-${blocks.length}`}>
        {renderTextWithLinks(line)}
      </p>,
    );
  });
  flushList();

  return <div className="job-description">{blocks}</div>;
}
