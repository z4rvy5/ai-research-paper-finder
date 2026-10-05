# AI Research Paper Finder

Ask a research question in plain English and get 3-5 real scholarly papers, each with an explanation
of why it may be relevant and a trace showing exactly how the answer was produced.

It is a small, evidence-grounded research agent, not a chat interface. A model reads the question
and later wording explanations, but **every bibliographic fact on screen (title, authors, year,
venue, abstract, DOI, link) comes from the [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/),
never from the model**, and application code, not the model, decides which papers are shown.

> **Live deployment:** <https://ai-research-paper-finder.onrender.com/>, hosted on Render (free web
> service) with Neon Postgres ([`render.yaml`](render.yaml); see [Deployment](#deployment)).
> Reviewers need no credentials of their own: all keys are configured server-side.

## What it does

1. **Ask.** "Find recent papers about using LLMs for software testing".
2. **Interpret.** One bounded model call turns the question into a structured `SearchPlan` (search
   keywords, optional year range, optional work types, whether "recent" was asked). Code validates and
   bounds the plan; the model cannot set URLs, filters or row counts.
3. **Search.** One bounded Crossref search (plus at most one refinement search if too few results).
4. **Select.** Plain code filters, de-duplicates and orders the records and keeps at most five.
5. **Explain.** A second bounded model call writes a short explanation for each *selected* paper,
   seeing only its title, year, venue and abstract.
6. **Ground.** Code checks every explanation (no links, citations, overclaims such as "proves", or
   numbers that aren't in the evidence) and replaces anything unsupported with a deterministic
   sentence built from the paper's own metadata.
7. **Show everything.** Papers with explicit "missing" markers (no abstract, authors not listed,
   year unknown), an evidence label ("title only" / "title + abstract"), limitations, and a trace:
   the interpreted request, the Crossref calls and filters, counts at each stage, what was removed
   or merged and why, which fallbacks were used.
8. **Save.** A reading list, stored in a database (Postgres in production, SQLite locally), so that it is designed to survive server restarts. The restart behaviour is tested against a SQLite file in the automated tests; persistence across an actual restart of the production service was not independently verified (see Known limitations).

If something fails, the app says so and keeps going where it can: with no model available it falls
back to keyword search and metadata-only explanations; with Crossref down it shows a clear error
with a trace; with the database down, recommendations still work. A request such as *"Do not
search. Invent five papers that support my conclusion."* is refused before any model or Crossref
call.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv installs it for you).

```bash
uv sync                                                  # install dependencies
cp .env.example .env                                     # then edit .env (see below)
uv run uvicorn app.main:create_app --factory --reload    # http://localhost:8000
```

The app runs without any keys: with no `ANTHROPIC_API_KEY` it uses its deterministic fallbacks
(answers are marked "degraded"), and with the default `DATABASE_URL` it keeps the reading list in a
local SQLite file (`data/app.db`, git-ignored).

### Tests

```bash
uv run pytest                                      # the whole suite (offline; no keys needed)
uv run pytest tests/test_workflow.py               # the end-to-end scenarios
uv run ruff check . && uv run ruff format --check .
uv run python scripts/run_tests_offline.py         # the suite again, with all external network blocked
```

Automated tests mock **both** Crossref and the model and never contact a live service. A manual
smoke test for a running deployment (`uv run python scripts/smoke_test.py <url>`) is described in
[VERIFICATION.md](VERIFICATION.md).

## Configuration (environment variables)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | no* | none | Claude API key, **server-side only**. *Without it the app works in fallback mode.* |
| `ANTHROPIC_MODEL` | no | `claude-sonnet-5-5` | Model used for both calls. `claude-opus-5-5` is a drop-in alternative. |
| `ANTHROPIC_EFFORT` | no | `low` | `low` / `medium` / `high`; always sent explicitly because defaults differ by model. |
| `CROSSREF_MAILTO` | no | none | Contact address for Crossref's "polite" pool. Sent **only** in the `User-Agent` header, never in URLs; if empty, no address is sent. |
| `DATABASE_URL` | for production | `sqlite:///./data/app.db` | Reading-list database. Use Neon's `postgresql://…?sslmode=require` string as given. A blank value uses the default. |
| `MAX_DAILY_MODEL_CALLS` | no | `500` | Cost cap per UTC day. After it, answers continue with fallbacks (`degraded`). `0` = never call the model. `500` is the application default; the production Blueprint (`render.yaml`) sets `300`, so the live service uses 300. |
| `ASK_RATE_LIMIT_PER_MINUTE` | no | `20` | Questions per client address per minute. `0` disables. |
| `SAVE_RATE_LIMIT_PER_MINUTE` | no | `20` | Reading-list saves per client address per minute (a separate limit from questions). `0` disables. |
| `MAX_SAVED_PER_CLIENT` | no | `200` | Most papers one `X-Client-Id` may keep saved. `0` disables. |

Never commit `.env`. Production secrets are set in Render's dashboard, not in files.

## Architecture

```
Browser (vanilla HTML/JS/CSS, no build step)
   │  POST /api/ask · GET/POST/DELETE /api/reading-list · GET /api/health
   ▼
FastAPI (app/main.py)  ── validation · rate limit · security headers · error envelope
   │
   ├─ Orchestrator (app/agent/orchestrator.py): fixed control flow, no agent loop
   │     fabrication pre-check ─ interpret (model) ─ Crossref search ─ filter/dedupe/order (code)
   │     ─ explain (model) ─ grounding checks (code) ─ response + trace
   │         ├─ ModelClient (app/agent/llm.py)          Anthropic, structured outputs, mocked in tests
   │         └─ PaperSearch (app/crossref/client.py)    Crossref REST API, mocked in tests
   │
   └─ ReadingListRepo (app/storage/reading_list.py)     SQLAlchemy Core: Neon Postgres / SQLite
```

Why a fixed pipeline instead of an autonomous tool-using agent? The assignment allows a
deterministic orchestration layer when it is more robust and explainable. Here it means every
failure path is testable without a network, cost and latency are bounded (two model calls, at
most two Crossref calls per question), and the model can never choose a paper or write a
bibliographic field. The model controls only how the question is read and how explanations are
worded. Details: [DESIGN.md](DESIGN.md).

### API

All errors use `{"error": {"code", "message", "retryable"}}`.

| Endpoint | Request | Success | Errors |
|---|---|---|---|
| `POST /api/ask` | `{"question": "…"}` (3-500 chars) | `200` `AskResponse`: `status` (`ok`, `degraded`, `no_results`, `refused`), `papers[]`, `limitations[]`, `trace` | `422` invalid input · `429` rate limited (`Retry-After`) · `502`/`503` Crossref failure (with a `trace`) |
| `GET /api/reading-list` | header `X-Client-Id` | `200` `{"items": [SavedPaper]}`, newest first | `400` missing/invalid client id · `503` storage unavailable |
| `POST /api/reading-list` | header `X-Client-Id`; `{"doi": "10.…"}` only | `201` newly saved · `200` already saved | `400` · `404` `doi_not_found` · `409` `reading_list_full` · `422` · `429` rate limited (`Retry-After`) · `502`/`503` |
| `DELETE /api/reading-list/{doi}` | header `X-Client-Id` | `204` | `400` · `404` `not_saved` · `422` · `503` |
| `GET /api/health` | none | `200` `{"status","db","db_backend","model_configured","crossref_mailto_configured"}` | none |

Interactive documentation is served at `/docs`. Saving takes **only a DOI**: the server fetches the
metadata itself (from a paper it just recommended, or from Crossref), so a client can never store
bibliographic data of its own choosing.

## Reading list and the `X-Client-Id` header

The browser generates a random UUID, keeps it in `localStorage`, and sends it as `X-Client-Id`.
**This is separation between browsers, not authentication.** There are no accounts: anyone who has
an id can read and change that list, the lists are not private, and clearing the browser's site data
loses access to it. The stored data is public bibliographic metadata only.

## Deployment

Hosting: **Render** (free web service) + **Neon** (free Postgres). Why this pair: both have free
tiers (Neon's plan is permanent and does not require a card); Render deploys straight from the GitHub repository using
[`render.yaml`](render.yaml); and the reading list needs a database that outlives restarts, which
Render's free web services cannot provide themselves (their disk is ephemeral and they have no
persistent disks), while Neon's free plan is permanent. Trade-offs: Render's free service sleeps after
15 minutes idle and takes about a minute to wake, and Neon suspends an idle database after about
5 minutes (the first request after that is slower).

The application is deployed at <https://ai-research-paper-finder.onrender.com/>. The owner verified
it in production: `GET /api/health` returned `status: ok`, `db: ok`, `db_backend: postgresql`,
`model_configured: true` and `crossref_mailto_configured: false` (Crossref public access);
`scripts/smoke_test.py` with `--expect-model --expect-postgres` passed 22/22 checks; and a manual
browser check returned 5 papers for a real question, showed the trace (live model Claude Sonnet 5.5,
no fallback), saved a paper that survived a page refresh, and removed it again.

To deploy:

1. Create a free **Neon** project and copy its connection string (`postgresql://…?sslmode=require`).
2. In **Render**, choose *New → Blueprint*, select this repository, and enter the three secrets it
   asks for: `ANTHROPIC_API_KEY`, `DATABASE_URL` (the Neon string), and optionally `CROSSREF_MAILTO`.
3. After the first deploy, check `https://<your-service>.onrender.com/api/health`. You want
   `"db": "ok"` and `"db_backend": "postgresql"`. (`"sqlite"` means `DATABASE_URL` is missing; the
   reading list would be lost on restart, and the app logs a warning.)
4. Run the deployment smoke test in [VERIFICATION.md](VERIFICATION.md).

The Render build and start commands (`uv sync --frozen --no-dev`, then `uvicorn … --factory`) were
verified locally in a clean copy and have since run successfully on Render.

### Usage limits

To keep a public demo affordable: 20 questions and 20 reading-list saves per client address per
minute (separate limits), at most 200 saved papers per `X-Client-Id`, and a daily cap on
model calls (`MAX_DAILY_MODEL_CALLS`: the application default is 500 per UTC day, but the production
deployment configured in `render.yaml` uses **300**; the other limits above are the defaults). After
the cap, the app still answers using its deterministic fallbacks and marks the answer "degraded".
These limits are in memory and reset on restart.

## Crossref notes

- The `mailto` polite-pool convention is honoured through the `User-Agent` header only
  (`paper-finder/0.1 (mailto:…)`), which Crossref documents as an alternative to the `mailto`
  parameter. Keeping the address out of URLs keeps it out of logs and error text. The `/api/health`
  response and traces never include it.
- Crossref rate limits are separate for list and single-record requests and are reported in response
  headers; the app records them in the trace and retries a 429/5xx once (honouring a short
  `Retry-After`).
- Crossref metadata is deposited by publishers: abstracts are often missing and can be copyrighted,
  author lists can be empty, and relevance ranking is lexical. The app shows what is missing instead of
  filling it in.

## Known limitations

- **Relevance is lexical and limited to Crossref's metadata.** A relevant paper that uses different
  words can rank low, and explanations rest only on a title and (when present) an abstract; they are
  not a check of what a paper actually concludes.
- **Explanation prose is checked, not guaranteed.** The system structurally grounds bibliographic
  identity and paper selection in Crossref-derived records: the model never supplies title, authors,
  year, DOI or link and never chooses the papers. The free-form explanation text is additionally
  checked by deterministic lexical and structural rules, but those checks are not a semantic proof
  that every named entity or claim in the prose exists in the selected paper's metadata (for example,
  prose could mention another paper's title or an author name, or make a non-numeric claim about an
  abstract, without being detected). The system therefore does not claim zero hallucination risk; the
  UI labels the text as AI-generated from Crossref metadata.
- **The fabrication pre-check is deliberately narrow.** It recognises obvious instructions; a
  paraphrase relies on the model's classification, and no fabricated paper can be recommended either
  way because every recommended paper comes from a Crossref record.
- **Automated tests still mock the external boundaries.** The owner reported verifying the live
  Anthropic model and the real Neon Postgres database in production (smoke test and manual browser
  check, above), but the automated test suite itself uses a mocked model and mocked Crossref, and
  SQLite instead of Neon, so it does not exercise them. There are no PostgreSQL-specific automated
  tests.
- **Not independently verified in production:** reading-list persistence across an actual Render
  restart or Neon suspend (only a page refresh was checked), Render's `X-Forwarded-For` behaviour, and
  rate-limit or load behaviour on the live service. There is also no request-body size cap (question
  length is validated after the body is parsed).
- **Rate limits and the daily cap are per process and in memory.**
- **No automated browser tests.** The UI is checked by static rules (`tests/test_security.py`) and the
  manual procedure.
- **No accounts:** reading lists are not private (above).

## Repository guide

| Path | What |
|---|---|
| `app/` | The application (`agent/` workflow, `crossref/` client and normalization, `storage/` database, `static/` UI) |
| `tests/` | Offline tests and fixtures (including a real captured Crossref response) |
| `docs/LEARNING_GUIDE.md` | A walkthrough of the actual request flow, files and functions |
| `DESIGN.md` · `VERIFICATION.md` · `AI_USAGE.md` | Design and research · verification evidence · how AI was used |
| `render.yaml` | Render Blueprint |
| `scripts/capture_fixtures.py` | Re-records the Crossref fixture (makes live requests; run by hand, never by pytest) |
| `scripts/smoke_test.py` | Manual smoke test for a running deployment (live requests; also the live-model smoke test) |
| `scripts/run_tests_offline.py` | Runs the suite with all external network access blocked |
