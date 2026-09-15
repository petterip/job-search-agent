export const HELSINKI_TIME_ZONE = "Europe/Helsinki";

const dateFormatter = new Intl.DateTimeFormat("fi-FI", {
  day: "numeric",
  month: "numeric",
  year: "numeric",
  timeZone: HELSINKI_TIME_ZONE,
});

const dateTimeFormatter = new Intl.DateTimeFormat("fi-FI", {
  day: "numeric",
  month: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: HELSINKI_TIME_ZONE,
});

function parseDate(value: string): Date | null {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatFinnishDate(value: string | null, fallback: string): string {
  if (!value) return fallback;
  const date = parseDate(value);
  if (!date) return fallback;
  return dateFormatter.format(date);
}

export function formatFinnishDateTime(value: string | null, fallback: string): string {
  if (!value) return fallback;
  const date = parseDate(value);
  if (!date) return fallback;
  return dateTimeFormatter.format(date);
}
