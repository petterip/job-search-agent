# Agent instructions

Docs in English; keep Finnish source data and filenames.

North star: `docs/goal.md`. Also see `docs/sources.yaml`, `docs/tyonhaku-rajapinnat.md`, `docs/tech-stack-plan.md`, `docs/llm-hosted.md`, and current build state in `docs/implementation-journal.md`. Follow stack plan; no SQLite/Redis/Celery/OpenSearch unless asked.

`make test-regression` before source changes. Sync `sources.yaml` + `tyonhaku-rajapinnat.md`. Respect `blocked` and poll intervals. `profile/` is private.

Local secrets live in `.env` at the repo root. OpenAI hosted LLM configuration uses `LLM_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_EMBEDDING_MODEL`, `OPENAI_EMBEDDING_DIMENSION`, `OPENAI_EVAL_MODEL`, and `OPENAI_EVAL_MODEL_ESCALATED` there. Gemini fallback configuration uses `GEMINI_API_KEY` and `GEMINI_EVAL_MODEL` there. Browser enrichment uses `BROWSERBASE_API_KEY`, `ENRICHMENT_ENABLED`, and `ENRICHMENT_PROVIDER` (`browserbase` when the cloud key is set). Google transit distance uses a server-side `GOOGLE_MAPS_API_KEY` (Routes API, no HTTP referrer restriction) and `TRANSIT_ORIGIN_ADDRESS` (default `Jalkatie 2, Oulu, Finland`). The `browserbase` SDK stays in the default backend dependencies so `make browserbase-check` works without extras; install Playwright with `pip install '.[enrichment]'` when browser extraction ships. Do not commit `.env`.

Docker Compose uses non-80 ports by default: web on `127.0.0.1:3080` and API on `127.0.0.1:8008`. Keep implementation-facing names job-related rather than using the profile/person name.

Use `make docker-config` for Compose validation because raw `docker compose config` expands `.env` and can print secrets.

Minimal diffs. No commits unless asked.
