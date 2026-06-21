import type { ReactNode } from "react";

type ActionTone = "apply" | "consider" | "skip" | "neutral";
type EvidenceTone = "good" | "warning" | "bad";

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

const cityCoordinates: Record<string, { lat: number; lon: number; label: string }> = {
  espoo: { lat: 60.2055, lon: 24.6559, label: "Espoo" },
  helsinki: { lat: 60.1699, lon: 24.9384, label: "Helsinki" },
  jyväskylä: { lat: 62.2426, lon: 25.7473, label: "Jyväskylä" },
  kauniainen: { lat: 60.2124, lon: 24.7272, label: "Kauniainen" },
  kokkola: { lat: 63.8385, lon: 23.1307, label: "Kokkola" },
  oulu: { lat: 65.0121, lon: 25.4651, label: "Oulu" },
  pelkosenniemi: { lat: 67.1108, lon: 27.5106, label: "Pelkosenniemi" },
  ranua: { lat: 65.9167, lon: 26.5333, label: "Ranua" },
  rantasalmi: { lat: 62.0667, lon: 28.3, label: "Rantasalmi" },
  rovaniemi: { lat: 66.5039, lon: 25.7294, label: "Rovaniemi" },
  utsjoki: { lat: 69.9086, lon: 27.0284, label: "Utsjoki" },
  ylöjärvi: { lat: 61.5563, lon: 23.5961, label: "Ylöjärvi" },
  ylivieska: { lat: 64.0833, lon: 24.55, label: "Ylivieska" },
};

function normalizeLocation(value: string): string {
  return value
    .toLocaleLowerCase("fi-FI")
    .normalize("NFKC")
    .replace(/[^\p{L}\p{N}\s-]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function locationParts(location: string | null): string[] {
  return (location ?? "")
    .split(/[,/]/)
    .map((part) => normalizeLocation(part))
    .filter(Boolean);
}

function firstKnownLocation(location: string | null): { key: string; label: string; lat: number; lon: number } | null {
  for (const part of locationParts(location)) {
    const direct = cityCoordinates[part];
    if (direct) return { key: part, ...direct };
    const embeddedKey = Object.keys(cityCoordinates).find((key) => part.includes(key));
    if (embeddedKey) return { key: embeddedKey, ...cityCoordinates[embeddedKey] };
  }
  return null;
}

function distanceKm(from: { lat: number; lon: number }, to: { lat: number; lon: number }): number {
  const earthRadiusKm = 6371;
  const toRad = (value: number) => (value * Math.PI) / 180;
  const dLat = toRad(to.lat - from.lat);
  const dLon = toRad(to.lon - from.lon);
  const lat1 = toRad(from.lat);
  const lat2 = toRad(to.lat);
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
  return Math.round((earthRadiusKm * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a))) / 10) * 10;
}

function workMode(location: string | null): "remote" | "hybrid" | null {
  const normalized = normalizeLocation(location ?? "");
  if (normalized.includes("etä")) return "remote";
  if (normalized.includes("hybridi")) return "hybrid";
  return null;
}

function sentenceParts(value: string | null): string[] {
  return (value ?? "")
    .split(/(?<=[.!?])\s+/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function locationEvidence(location: string | null): { text: string; tone: EvidenceTone } | null {
  const mode = workMode(location);
  const known = firstKnownLocation(location);
  const home = cityCoordinates.oulu;

  if (mode === "remote") {
    return { text: known ? `${known.label} / etä · sijainti joustava` : "Etätyö · sijainti joustava", tone: "good" };
  }
  if (!known) {
    return location ? { text: `${location} · etäisyys Oulusta ei tiedossa`, tone: "warning" } : null;
  }

  const km = distanceKm(home, known);
  if (km <= 25) return { text: `${known.label} · Oulun seutu`, tone: "good" };
  if (mode === "hybrid") return { text: `${known.label} · noin ${km} km Oulusta, hybridityö`, tone: km <= 250 ? "warning" : "bad" };
  return { text: `${known.label} · noin ${km} km Oulusta`, tone: km <= 180 ? "warning" : "bad" };
}

function concernText(concern: string, location: string | null): string {
  if (/sijainti ei osu/i.test(concern)) {
    return locationEvidence(location)?.text ?? concern;
  }
  return concern;
}

function evidenceItems({
  rationale,
  concerns,
  location,
  score,
}: {
  rationale: string | null;
  concerns: string[];
  location: string | null;
  score: number;
}): { pros: string[]; cons: { text: string; tone: EvidenceTone }[] } {
  const locationFit = locationEvidence(location);
  const rationalePros = sentenceParts(rationale)
    .filter((part) => !/^Sijainti\b/i.test(part))
    .slice(0, 2);
  const pros = [...rationalePros];

  if (score >= 80) pros.unshift("Vahva kokonaisosuma profiiliin.");
  else if (score >= 65) pros.unshift("Kohtalaisen vahva osuma profiiliin.");

  if (locationFit?.tone === "good") pros.push(locationFit.text);

  const cons = concerns.map((concern) => {
    const text = concernText(concern, location);
    const tone = /km Oulusta|sijainti|kelpoisuus|puuttuu|edellyttää/i.test(text) ? "bad" : "warning";
    return { text, tone: locationFit?.text === text ? locationFit.tone : tone };
  });

  if (locationFit && locationFit.tone !== "good" && !cons.some((item) => item.text === locationFit.text)) {
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
  score,
}: {
  idPrefix: string;
  rationale: string | null;
  concerns: string[];
  location: string | null;
  score: number;
}) {
  const { pros, cons } = evidenceItems({ rationale, concerns, location, score });

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
