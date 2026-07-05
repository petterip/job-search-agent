import { redirect } from "next/navigation";
import { NextRequest } from "next/server";

const VALID_LEGACY_ACTIONS = new Set(["good_match", "not_relevant", "applied"]);

function parseRating(value: FormDataEntryValue | null): number | null {
  if (value === null || value === "") {
    return null;
  }
  const rating = Number(value);
  if (!Number.isInteger(rating) || rating < 1 || rating > 5) {
    return null;
  }
  return rating;
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const formData = await request.formData();
  const jobId = String(formData.get("job_id") ?? "");
  const legacyAction = String(formData.get("action") ?? "");
  const rating = parseRating(formData.get("rating"));
  const applied = formData.get("applied") === "on" || formData.get("applied") === "true";

  const comment = String(formData.get("comment") ?? "").trim();

  const baseUrl = process.env.INTERNAL_API_BASE_URL ?? "http://localhost:8008";
  const url = new URL(`/recommendations/${id}/feedback`, baseUrl);

  let response: Response;
  if (VALID_LEGACY_ACTIONS.has(legacyAction)) {
    url.searchParams.set("action", legacyAction);
    response = await fetch(url, { method: "POST", cache: "no-store" });
  } else if (rating !== null) {
    response = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rating,
        applied,
        comment: comment || null,
      }),
    });
  } else {
    redirect(jobId ? `/tyopaikat/${jobId}` : "/");
    return;
  }

  if (!response.ok) {
    throw new Error("Palautteen tallennus epäonnistui");
  }

  redirect(jobId ? `/tyopaikat/${jobId}` : "/");
}
