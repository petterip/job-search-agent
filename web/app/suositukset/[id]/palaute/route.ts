import { redirect } from "next/navigation";
import { NextRequest } from "next/server";

const VALID_ACTIONS = new Set(["good_match", "not_relevant", "applied"]);

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const formData = await request.formData();
  const action = String(formData.get("action") ?? "");
  const jobId = String(formData.get("job_id") ?? "");

  if (VALID_ACTIONS.has(action)) {
    const baseUrl = process.env.INTERNAL_API_BASE_URL ?? "http://localhost:8008";
    const url = new URL(`/recommendations/${id}/feedback`, baseUrl);
    url.searchParams.set("action", action);
    await fetch(url, { method: "POST", cache: "no-store" });
  }

  redirect(jobId ? `/tyopaikat/${jobId}` : "/");
}
