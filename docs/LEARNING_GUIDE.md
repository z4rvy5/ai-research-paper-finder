# Learning Guide

A guide to how this codebase actually works. It is updated at the end of each implementation
milestone, so it only describes code that exists. Planned-but-unbuilt parts are marked
**(planned)**.

**Current state: milestone 1 — FastAPI skeleton.** The app starts, serves the page and
reports health. There is no search, model call or persistence yet.

---

## 1. Request flow

### Today

```
Browser                               FastAPI (app/main.py)
───────                               ─────────────────────
GET /                ───────────────▶ index()        → FileResponse(app/static/index.html)
GET /static/app.js   ───────────────▶ StaticFiles mount at /static
GET /api/health      ───────────────▶ health()       → HealthResponse (app/schemas.py)
submit question form ─╳ (handled in app.js only: shows "Search is not available yet.")
```

1. `uvicorn app.main:create_app --factory` calls `create_app()`. This builds `Settings()` from
   environment variables (and `.env`) and registers the routes.
2. The browser loads `/`, which returns `index.html`. That page loads `styles.css` and `app.js`
   from `/static`.
3. On `DOMContentLoaded`, `app.js` calls `checkHealth()`, which fetches `/api/health` and writes
   a status line. The status says which credentials are missing, without revealing their values.
4. Submitting the form calls `onSubmit()`, which currently only shows a message. Wiring it to
   `POST /api/ask` is milestone 2.

### Target (planned)
`POST /api/ask` → orchestrator → interpret (model) → `search_papers` (Crossref) →
filter / dedup / order (code) → explain (model) → grounding checks (code) → response and trace.

## 2. File and function map

| File | What it contains |
|---|---|
| `app/main.py` | `create_app(settings=None)` is the **app factory**. It registers `health()` (`GET /api/health`) and `index()` (`GET /`) and mounts `app/static` at `/static`. Later milestones will add `model_client`, `crossref_client` and `repo` parameters so tests can inject fakes. |
| `app/config.py` | `Settings` (pydantic-settings). It reads `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`), `ANTHROPIC_EFFORT` (default `low`), `CROSSREF_MAILTO`, `DATABASE_URL` and `MAX_DAILY_MODEL_CALLS`. The API key is a `SecretStr`, so it isn't printed in logs or reprs. |
| `app/schemas.py` | `HealthResponse`, the documented contract for `/api/health`. |
| `app/static/index.html` | The single page. It has no inline scripts or styles, so a strict `Content-Security-Policy: default-src 'self'` can be added later without changes. |
| `app/static/app.js` | `checkHealth()`, `onSubmit()` and `showMessage()`. Rule: server data is rendered only with `textContent` / `createElement`, never `innerHTML`. |
| `app/static/styles.css` | Minimal styling, with light and dark themes through CSS variables. |
| `tests/conftest.py` | `make_settings(**overrides)` builds `Settings` with `_env_file=None`, so tests never read a developer's `.env`. The `client` fixture is a `TestClient` over `create_app(make_settings())`. |
| `tests/test_app.py` | Milestone 1 tests (see §6). |
| `.env.example` | Every configuration variable, with comments. |

## 3. Model vs deterministic boundary
Nothing calls the model yet. The settings already default to **Claude Sonnet 5.5** with
**explicit low effort**. Opus 5.5 can be selected with `ANTHROPIC_MODEL=claude-opus-5-5` without
code changes, once the `ModelClient` interface exists (milestone 5).

## 4. Design decisions so far

- **App factory with `--factory`.** uvicorn calls `create_app()` itself, so importing `app.main`
  has no side effects. Tests build isolated apps with their own settings. (The plan said
  `uvicorn app.main:app`; this is the same idea without a module-level global.)
- **Credentials are optional at startup.** The server boots and `/api/health` works before keys
  are set. Health reports `model_configured` and `crossref_mailto_configured` as booleans and
  never the values. That helps diagnose a deployment without leaking secrets. Features that need
  a missing credential will fail with a clear error once they exist.
- **`db: "not_checked"`.** Health doesn't touch a database yet. Milestone 9 replaces this with a
  real check returning `ok` or `unavailable`.
- **No build step.** The frontend is plain HTML/JS/CSS served by FastAPI. There is one process
  to run and deploy.

## 5. Failure modes (so far)

| Situation | What happens | Where |
|---|---|---|
| Server not reachable (for example, Render cold start) | The status line says the server is unreachable and may be waking up | `checkHealth()` in `app.js` |
| Credentials not configured | The status line lists what's missing; health still returns 200 | `health()` in `main.py` |

## 6. Tests

```bash
uv run pytest                                   # all tests
uv run pytest tests/test_app.py::test_index_page_is_served   # a single test
uv run ruff check . && uv run ruff format --check .
```

| Test | Proves |
|---|---|
| `test_health_reports_ok_and_unconfigured_credentials` | The exact health contract when nothing is configured |
| `test_health_reports_configured_credentials_without_exposing_them` | Configured credentials show up as booleans only; the secret values never appear in the response |
| `test_index_page_is_served` / `test_static_assets_are_served` | The page and its assets are served |
| `test_default_model_is_sonnet_with_explicit_low_effort` | Model defaults match the architecture decision |
| `test_settings_read_from_environment` | Env vars override the defaults, so Opus can be switched in by configuration alone |

## 7. Why deterministic orchestration instead of an autonomous tool-using agent?

This is the central design choice. It's implemented from milestone 5 onward; the reasoning is
recorded here now.

**What the model controls**
- Interpreting the question into a structured `SearchPlan`: topic query, up to 2 alternative
  queries, year range, work types, whether recency was requested, and whether the request is
  out of scope or asks to fabricate papers.
- Choosing which of the shortlisted candidates (labelled `P1`–`P8`) are relevant, and writing a
  short explanation for each.

**What deterministic code controls**
- Every Crossref call: what is called, with which filters, how many times, the rate limits and
  the retries.
- Filtering, deduplication and ordering of results.
- All bibliographic data shown to users: title, authors, year, venue, DOI and link always come
  from the Crossref record, joined by code onto the model's `P#` choice.
- Whether an explanation is "based on title only" or "title + abstract". Code decides this from
  whether the record has an abstract.
- Checking the model's output: unknown or duplicate slots are dropped, and overclaiming wording,
  stray URLs or DOIs, and numbers not in the abstract are rewritten.
- Fallbacks when the model or Crossref fails.

**Why this boundary suits this assignment**
- **Grounding:** the model never produces the metadata that users rely on. Its output schema has
  no title, author or DOI fields, so model-written metadata cannot become source-of-truth data.
  Model *prose* can still be wrong or overstated. The deterministic checks reduce that risk but
  can't remove it, and the UI labels the prose as an AI explanation.
- **Testability:** each step is either a pure function (mapping, filtering, checking) or a
  boundary behind an interface (Crossref through mocked HTTP, the model through a fake client).
  Every failure path can be reproduced in a unit test without network access.
- **Predictability:** each question makes exactly two model calls and at most a few Crossref
  calls. Cost, latency and rate-limit behavior are bounded, which matters for a public demo on
  free hosting.
- **What we give up:** an autonomous agent could explore more open-endedly, for example chasing
  citations or reformulating queries many times. For a 3–5 paper recommendation with an
  auditable trace, one bounded refinement search is enough. The assignment explicitly allows
  deterministic orchestration.
