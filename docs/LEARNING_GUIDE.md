# Learning Guide

A guide to how this codebase actually works. It is updated at the end of each implementation
milestone, so it only describes code that exists. Planned-but-unbuilt parts are marked
**(planned)**.

**Current state: milestone 3 — the core research workflow.** A question goes through a model
call that produces a structured `SearchPlan`, a bounded Crossref search, deterministic
filtering / de-duplication / ordering, a model call that writes explanations, and deterministic
grounding checks. The response carries 3-5 papers and an inspectable trace, and the browser
renders both. Obvious requests to invent papers are refused by a deterministic pre-check
before any model or Crossref call, and a failed Crossref search still returns a safe trace.
**Milestone 4 added a persistent reading list** (save, list, remove) in Postgres (SQLite
locally), scoped by an anonymous per-browser id (§1b). **Not built yet:** rate limiting,
retries/caching, the final README/DESIGN/AI_USAGE/VERIFICATION documents, and deployment.

---

## 1. Request flow: `POST /api/ask`

```
Browser (app.js)          app/main.py            app/agent/orchestrator.py      boundaries
────────────────          ───────────            ─────────────────────────      ──────────
onSubmit()
 POST {question} ───────▶ AskRequest validates
                          (schemas.py; 422 on bad input, nothing else runs)
                          ask() ──────────────▶ Orchestrator.ask(question)
                                                 0 plan.fabrication_request_reason()   deterministic pre-check
                                                     match → "refused" (no model call, no Crossref call)
                                                 1 _interpret ─────────────────▶ ModelClient.interpret   (model call 1)
                                                     plan.effective_plan()  bounds the plan
                                                     on ModelError → plan.fallback_plan()
                                                 intent != find_papers → "refused", no search
                                                 2 _search (primary) ──────────▶ PaperSearch.search_works (Crossref)
                                                     CrossrefError → SearchFailed (error + trace)
                                                     normalize.normalize_works()
                                                 3 ranking.select_papers()  filter / dedupe / order
                                                 4 < 3 survivors and alt_queries? → _refine: ONE more search
                                                 5 no survivors → "no_results" (no model call 2)
                                                 6 _explain ───────────────────▶ ModelClient.explain     (model call 2)
                                                     on ModelError → no explanations (fallback text)
                                                 7 grounding.ground_explanations()  validate, replace
                                                 8 _response → AskResponse + Trace
renderResults() ◀──────── AskResponse
```

Step by step, with the function that does each thing:

1. **Validate.** `AskRequest.clean_question` (`app/schemas.py`) collapses whitespace, rejects
   control characters, and enforces 3-500 characters. Failure → `422 invalid_input`; neither
   the model nor Crossref is called.
2. **Pre-check for requests to invent papers.** `plan.fabrication_request_reason` is plain
   regexes, and it runs **before** the model or Crossref (§3). A match returns
   `status: "refused"` with `trace.interpretation.source == "precheck"`.
3. **Interpret (model call 1).** `AnthropicModelClient.interpret` sends the question, inside
   escaped `<user_question>` tags, and gets back a validated `SearchPlan`. Then
   `plan.effective_plan` bounds it: cleans the query, caps alternative queries at 2, drops
   out-of-range or contradictory years, and turns "recent" into a recorded 3-year window. If the
   model fails or returns a plan with no usable query, `plan.fallback_plan` builds a plan from
   the question's own keywords and the trace records `source: "fallback"`.
4. **Refuse without searching** (model-classified). If the plan's `intent` is `fabrication_request` or
   `out_of_scope`, the response is `status: "refused"` and Crossref is never called.
5. **Search.** `CrossrefClient.search_works` (M2, now with optional filters). The date range
   and work types from the plan become `filter=from-pub-date:…,until-pub-date:…,type:a,type:b`
   (different filters AND together; a repeated `type` ORs). Rows are the constant `SEARCH_ROWS = 25`.
6. **Normalize.** `normalize_works` turns raw records into `Paper` objects: authors,
   year, venue, type, plain-text abstract, and `missing_fields`. Records without a DOI are
   skipped and counted.
7. **Filter, de-duplicate, order.** `ranking.select_papers`, described in §3.
8. **One bounded refinement.** If fewer than 3 papers survive and the plan has `alt_queries`,
   `_refine` runs exactly one more search with the first alternative query (same filters). If
   that search fails, the primary results are kept and the answer is marked `degraded`.
9. **Explain (model call 2).** Only the selected papers' title, year, venue and abstract, labelled
   `P1…P5`, go to `AnthropicModelClient.explain`. The model never sees DOIs, URLs or authors.
10. **Ground.** `grounding.ground_explanations` maps explanations back to the papers *we*
   selected, checks each one, and replaces anything unsupported (§4).
11. **Respond.** `Orchestrator._response` builds `AskResponse` and `Trace`. `status` is `ok`,
    `degraded` (some stage fell back), `no_results`, or `refused`. A Crossref failure on the
    *primary* search is still an HTTP error (`503`/`502`, same `error` body as in M2), and it now
    also carries a `trace` (§6): the orchestrator raises `SearchFailed(error, trace)` and
    `on_search_failed` in `main.py` turns it into the response.
12. **Render.** `renderResults` in `app.js` draws paper cards and the trace with `textContent`.

## 1b. Request flow: the reading list (M4)

```
Browser (app.js)                    app/main.py                      boundaries
────────────────                    ───────────                      ──────────
getClientId()  random UUID in localStorage, sent as X-Client-Id on every call
loadReadingList()  GET /api/reading-list ─▶ client_id_header()  400 if missing / not a UUID
                                           repo.list_papers(client_id)  ─────▶ SQL (parameterized)
toggleSaved()  POST /api/reading-list {doi} ▶ SavePaperRequest  (DOI only; extra fields → 422)
                                           repo.get()  already saved? → 200, done
                                           recent_papers.get(doi)   a paper WE just recommended
                                             └ miss → crossref.get_work(doi) ───▶ GET /works/{doi}
                                                  404 → 404 doi_not_found; failure → 502/503
                                           repo.add()  → 201 (new)  ─────────────▶ INSERT
              DELETE /api/reading-list/{doi} ▶ normalize_doi → repo.remove() → 204 / 404 not_saved
```

1. **Identification.** On first load the browser makes a random UUID (`getClientId` in `app.js`),
   keeps it in `localStorage`, and sends it as `X-Client-Id`. `main.client_id_header` requires a
   valid UUID (`400 missing_client_id` / `invalid_client_id`) and canonicalizes it to lowercase.
   **This is browser-level separation, not authentication or authorization.** The id is a random
   value the browser chose; anyone who has it can read and change that list, a different
   browser can't see it, and clearing site data loses it. Saved lists are not private accounts.
   What is stored is public bibliographic metadata only.
2. **Save takes only a DOI** (`SavePaperRequest`, `extra="forbid"`). The server decides what to
   store, so a client can never save a title, author list or link of its own choosing. The DOI is
   normalized by `schemas.normalize_doi` (trim, drop a `https://doi.org/` or `doi:` prefix,
   lowercase, `10.NNNN/suffix`, at most 255 characters, no whitespace or control characters).
3. **Where the metadata comes from.** First `PaperCache` (`app/storage/paper_cache.py`): a small
   in-process cache of the papers `/api/ask` just recommended (256 entries, 1 hour), filled only
   from server-produced Crossref data. On a miss, `CrossrefClient.get_work` calls
   `GET /works/{doi}`. Either way the stored record is normalized Crossref data, never client
   or model text. A DOI Crossref doesn't know is `404 doi_not_found`.
4. **Persistence.** `ReadingListRepo` (`app/storage/reading_list.py`), SQLAlchemy Core, one table:
   `saved_papers(client_id, doi, title, authors, year, venue, url, abstract, work_type,
   missing_fields, saved_at)`.
   - Primary key `(client_id, doi)`: one row per paper per list, enforced by the database.
   - Index on `(client_id, saved_at)` for listing. `authors` and `missing_fields` are JSON text
     (portable across SQLite and Postgres); `title`, `year`, `venue` and `abstract` are nullable so
     missing data stays missing; `saved_at` is a timezone-aware UTC timestamp.
   - Every query is built with SQLAlchemy expressions (bound parameters). There is no SQL string
     concatenation anywhere.
   - `add` is idempotent: it checks, inserts, and treats a lost race (`IntegrityError`) as "already
     saved". Saving twice is `200` and returns the original row; the first save is `201`.
5. **Choosing the database.** `DATABASE_URL`: SQLite (`sqlite:///./data/app.db`, the default) or
   Postgres. `normalize_database_url` maps the `postgres://` / `postgresql://` URLs Neon and Render
   hand out to the psycopg 3 driver. Postgres engines use `pool_pre_ping` (Neon suspends idle
   databases and drops their connections), `pool_recycle=240` and a 10 s connect timeout. SQLite
   gets its directory created and `check_same_thread=False` (FastAPI runs the sync repository calls
   in worker threads via `run_in_threadpool`).
6. **Database failure.** Any `SQLAlchemyError` becomes `StorageError`: `503 storage_unavailable`,
   `retryable: true`, a fixed message. Driver text (which can contain hosts, user names and
   passwords) is never logged or returned; only the exception class is logged. The table is
   created on startup, and if the database is down then, the app still starts (`/api/ask` works)
   and creation is retried on the next use. `GET /api/health` reports `db: "ok"` or
   `"unavailable"` and stays `200`.
7. **UI.** Every card and every saved item has one button that saves or removes. After each
   change the list is reloaded from the server and all buttons are refreshed from it (the server
   is the source of truth). The list shows loading, empty and error states; save/remove errors
   appear next to the button. A removal that comes back `not_saved` is treated as already done.

## 2. File and function map

| File | Key contents |
|---|---|
| `app/main.py` | `create_app(settings, crossref, model, clock)`: the app factory. Builds the `Orchestrator`, the `ReadingListRepo` and the `PaperCache`; `ask()` is short. The reading-list endpoints and `client_id_header` live here too. `error_response(..., trace=None)` builds the error body and adds `trace` only when given. Error handlers: validation (422), `SearchFailed` (502/503, with the trace), `CrossrefError` (502/503), and a catch-all (500 `internal_error`) so a bug returns the error envelope instead of breaking the page. |
| `app/schemas.py` | Every contract in one place: `AskRequest`; `Paper` and `RecommendedPaper(Paper)`; the model-boundary types `SearchPlan`, `WorkType`, `Explanations`, `ExplanationItem`, `Candidate`; the trace types `Trace`, `TraceStep`, `Interpretation`, `Counts`, `FilteringInfo`, `GroundingInfo`, `SearchInfo`, `Failure`; `AskResponse`; `ErrorResponse` (whose optional `trace` is set only for upstream failures); and the reading-list types `SavePaperRequest`, `SavedPaper`, `ReadingListResponse`, plus `normalize_doi`. |
| `app/agent/orchestrator.py` | `Orchestrator.ask`: fixed control flow (no loop). `_precheck_refusal`, `_interpret`, `_search`, `_search_failed`, `_refine`, `_explain`, `_response`. `SearchFailed` is the exception that carries a failed primary search's error and trace. `_Run` accumulates the trace for one request. |
| `app/agent/llm.py` | The model boundary. `ModelClient` (Protocol: `interpret`, `explain`, `aclose`), `ModelError` (stable `code`), `AnthropicModelClient`. Timeout and retry constants live here. |
| `app/agent/prompts.py` | System prompts, `escape()` for untrusted text, and the two user-message builders. |
| `app/agent/plan.py` | `fabrication_request_reason` (the pre-check), `sanitize_query`, `topic_terms`, `fallback_plan`, `effective_plan`, `SEARCH_ROWS`, the stopword list. |
| `app/agent/ranking.py` | `select_papers` (pure), `term_match`, `stem`, `Selection`, `PRESENT_LIMIT = 5`, `WINDOW = 12`. |
| `app/agent/grounding.py` | `to_candidate`, `violations`, `fallback_explanation`, `ground_explanations`. |
| `app/crossref/client.py` | M2 client; `search_works`/`PaperSearch` gained optional `from_year`, `until_year`, `types`. `build_filter` builds the `filter=` value. `get_work(doi)` is the single-record lookup (`GET /works/{doi}`; a 404 is `None`). |
| `app/crossref/normalize.py` | `normalize_work(s)`, `abstract_text` (JATS → text), `strip_markup`. |
| `app/static/app.js` | `renderResults`, `renderPaper`, `renderTrace`. Everything is built with `textContent`. |
| `app/storage/reading_list.py` | `ReadingListRepo` (`get`, `add`, `list_papers`, `remove`, `ensure_schema`, `ping`, `dispose`), the `saved_papers` table, `make_engine`, `normalize_database_url`, `StorageError`. |
| `app/storage/paper_cache.py` | `PaperCache`: a bounded, expiring in-process cache of recently recommended papers. |
| `app/errors.py` | `ApiError(status, code, message)`: request errors that map onto the error envelope. |
| `tests/fakes.py` | `failing_repo()` (a repository whose database fails instantly with a secret-laden driver message), `FakeCrossrefClient` (now with `get_work`) (records calls, scripted responses) and `FakeModelClient` (scripted plan/explanations or errors). |
| `tests/builders.py` | `work()` builds Crossref-shaped records, plus `search_result`, `fixture_result`, `many_works`, `mock_crossref_client`. |

## 3. Model vs deterministic boundary

**The model controls exactly two things:**
- how to read the question (a `SearchPlan`: intent, search keywords, optional alternative
  phrasings, optional year bounds and work types, whether recency was asked for), and
- the wording of a one-or-two-sentence explanation for each paper *we already chose*.

**Code controls everything else:**

| Decision | Where | Rule |
|---|---|---|
| Whether to treat a request as an attempt to invent papers | `plan.fabrication_request_reason` | Three narrow regexes, run before any model or Crossref call (below). |
| What Crossref is asked | `plan.effective_plan`, `build_filter` | The plan has no URL, filter, or row-count fields (`extra="forbid"`), and a hostile query is reduced to letters, digits, spaces, hyphens, apostrophes. Rows are fixed at 25. |
| Which records count | `ranking.select_papers` | Hard constraints, de-duplication, soft term guard, ordering (below). |
| Which papers are shown | `select_papers` | The first 5 of the ordered window. The model does not pick. |
| Every bibliographic field | `ground_explanations` | Copied from the Crossref-derived `Paper`. The model's output type has only `ref` and `explanation`. |
| Evidence basis | `grounding.evidence_basis` | "title only" vs "title + abstract" from whether Crossref has an abstract. |
| Whether model prose is accepted | `grounding.violations` | Lexical checks; failures are replaced (below). |
| What happens when a model fails | `orchestrator` | Deterministic fallbacks; the request still succeeds. |

**Deterministic selection (`ranking.select_papers`), in order:**
1. *Hard constraints* — drop with a recorded reason: no title; front matter, index or an
   attached file (`..._supp1.pdf`); a work type that wasn't requested; a year outside the
   requested range, or no year when a range was requested.
2. *De-duplicate* — same DOI, or the same normalized title within one year. Keep the more
   complete record (has abstract, then has authors, then article over preprint, then earlier
   rank). Merges are recorded.
3. *Soft topic-term guard* — does any query term appear (lightly stemmed) in the title or
   abstract? **Not a filter:** papers with other wording are kept. The only drop is no match
   *and* no abstract *and* ranked beyond 12 (the noise case).
4. *Order* — Crossref's relevance order decides which 12 are considered; inside them, term
   matches first, then papers with an abstract, then newest first *only if recency was asked*,
   then Crossref rank. The top 5 are presented. There is **no weighted score**.

**The fabrication pre-check (`plan.fabrication_request_reason`).** It fires only on an
instruction *addressed to the assistant*, in one of three shapes:
1. an `invent` / `fabricate` / `forge` / `concoct` / `make up` verb at the start of a sentence (or
   after "please", "can you", "you must", ...) whose direct object is a source noun (`papers`,
   `citations`, `references`, `DOIs`, `studies`, `evidence`, ...), allowing only filler such as
   "five", "some", "supporting" in between ("Invent five papers that support my conclusion");
2. a request verb (`give`, `write`, `create`, `generate`, `produce`, ...) for *fake* sources
   ("Give me five fake citations", "Generate fake DOIs");
3. "do not search" / "without searching" at the start of a sentence, together with a request for
   papers or sources.

Topics about fabrication are not blocked, because the verb has to be an instruction rather than
part of the topic: "papers about LLMs that fabricate citations", "detecting fake references",
"who invented the transistor" and "studies on fake news detection" all go through normally. The
patterns are exercised by 11 refusal cases and 15 legitimate near-miss cases in `test_plan.py`.
The model's own `fabrication_request` classification remains a second layer for paraphrases the
regexes don't recognise.

The three counts are kept separate everywhere: **returned** (Crossref's records), **surviving**
(after filtering and de-duplication), **selected** (presented).

## 4. The grounding boundary (what it guarantees and what it doesn't)

**Guaranteed by construction:** model-generated bibliographic metadata never becomes
source-of-truth data. A presented paper's title, authors, year, venue, abstract, DOI and link
are copied from its Crossref record. The explanation call's output type
(`Explanations = list of {ref, explanation}`) has no place for them, and the refs are matched
only against the slots we assigned (`P1…Pn`), so the model cannot add, remove or change a paper.
The model also never receives DOIs, URLs or author names.

**Checked, not guaranteed:** the *prose* of an explanation. `grounding.violations` rejects text
that has any of: a length outside 15-600 characters; markup; a URL, DOI, `et al.` or
`(2021)`-style citation; certainty words (`proves`, `demonstrates that`, `establishes`,
`confirms`, `conclusively`, …); for title-only papers, claims about content (`the abstract`,
`results show`, `found that`, …); or a number that isn't in the title, abstract or year. A
rejected explanation is replaced by `fallback_explanation` (built only from the query terms
found in the paper's own metadata) and recorded in `trace.grounding.explanation_rewrites`.
These checks are lexical: they catch common overclaims but **can miss subtle ones**, so the UI
labels AI text as "AI-written, from Crossref metadata only" and fallback text as "no AI text
was used".

Untrusted input: the question and all metadata are escaped (`prompts.escape`) before going
inside tags, and the system prompts say to treat them as data. That *reduces* prompt-injection
risk; the output bounds above are what actually limit the damage.

## 5. Design decisions

- **Deterministic orchestration, two bounded model calls.** See §8.
- **The application selects; the model explains.** The earlier architecture had the model choose
  3-5 papers from 8 candidates. Milestone 3 follows the stated requirement instead: code selects
  at most 5, so a model error can't change *which* papers are shown.
- **Tolerant schemas, strict code.** Structured outputs don't support length, count or range
  constraints, so `SearchPlan` accepts what the model sends and `effective_plan` enforces limits
  (alternative queries capped at 2, years `1900..current+1`). Fewer structural failures, same
  guarantees.
- **Model choice is configuration.** `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`) and
  `ANTHROPIC_EFFORT` (default `low`, always sent explicitly because defaults differ between
  models). Sonnet 5.5 can't disable thinking and rejects forced tool use, so the calls use
  structured outputs (`messages.parse` with `output_format`) and omit `thinking`/`temperature`.
- **Explicit model timeout behavior.** 20 s per attempt, 1 retry (`MODEL_TIMEOUT_S`,
  `MODEL_MAX_RETRIES`), so one call can take up to roughly 40 s plus backoff. Every SDK failure
  maps to a `ModelError` code: `model_not_configured`, `model_timeout`, `model_rate_limited`,
  `model_unavailable`, `model_refused`, `model_output_invalid`. Messages are fixed strings;
  provider response bodies and credentials are never logged or returned.
- **The app works without a model key.** With no `ANTHROPIC_API_KEY`, both stages fall back and
  the answer is `degraded`. Useful for local checks, and a safe default. A *blank* key (what a
  copied `.env.example` contains) counts as not configured: `Settings.model_api_key` returns
  `None` for it, and both the client and `/api/health` use that property.
- **`recent` is code's decision.** The model only says `recency_requested`; code picks the
  window (3 years) and records it as an assumption in the trace.
- **Crossref contact address: User-Agent only** (from M2). The trace and logs can't contain it.
  `trace.searches[].url` has no address; `Trace.model` is a model id, never a credential.
- **Persistence: SQLAlchemy Core, one table, portable types.** Same code on SQLite (tests, local)
  and Neon Postgres (production). No migrations: the table is created if missing.
- **Reading-list reads and writes never involve a model.** The model boundary and the reading list
  don't touch.
- **Refinement is bounded.** At most one extra Crossref search, only when fewer than 3 papers
  survive and the plan has an alternative query.

## 6. Failure modes

| Situation | What happens | Where |
|---|---|---|
| Empty / too long / control characters / extra fields | 422 `invalid_input`; no model or Crossref call | `AskRequest`, `on_validation_error` |
| Model timeout, error, rate limit, refusal, no key | Interpret: keyword fallback plan. Explain: metadata-only explanations. Status `degraded`, listed in `trace.fallbacks` | `ModelError`, `Orchestrator._interpret/_explain` |
| Malformed model output (schema mismatch, truncated) | Same as above (`model_output_invalid`) | `AnthropicModelClient._parse` |
| Plan with no usable query | Treated as malformed; fallback plan | `Orchestrator._interpret` |
| Out-of-range, contradictory or hostile plan values | Dropped or cleaned in code, recorded in `trace.interpretation.adjustments` | `effective_plan` |
| Obvious request to invent papers ("Do not search. Invent five papers...") | `status: refused` **before any model or Crossref call**; trace shows `interpret` and `search_papers` as skipped and `source: precheck`; works even when the model is down | `fabrication_request_reason`, `Orchestrator._precheck_refusal` |
| `fabrication_request` / `out_of_scope` intent from the model | `status: refused`, no search, fixed message | `Orchestrator.ask` |
| Crossref 429 / 5xx / network / 400 on the primary search | Same HTTP status and `error` body as before (503 `upstream_rate_limited` / `upstream_unavailable`, 502 `upstream_rejected`), plus a `trace` with the interpreted plan, the `search_papers` step as `error`, a `failure` block (code, retryable, Crossref's HTTP status or none, search terms, filters) and a limitation saying no recommendations could be produced | `CrossrefClient._get`, `Orchestrator._search_failed`, `on_search_failed` |
| Crossref failure on the refinement search | Primary results kept; `degraded` | `Orchestrator._refine` |
| No Crossref records, or all filtered out | `status: no_results`, no explanation call, reasons in the trace | `Orchestrator.ask` |
| Missing abstract / authors / year | Explicit placeholders and `missing_fields`; evidence basis "title only" | `normalize_work`, `renderPaper` |
| Model explanation overclaims / cites / adds a paper | Replaced or ignored, recorded in `trace.grounding`; `degraded` | `ground_explanations` |
| Reading list: missing / malformed `X-Client-Id` | 400 `missing_client_id` / `invalid_client_id` | `client_id_header` |
| Reading list: invalid DOI or extra fields in the body | 422 `invalid_input`; nothing looked up or saved | `SavePaperRequest`, `normalize_doi` |
| Save: Crossref has no such DOI | 404 `doi_not_found`; nothing saved | `save_paper` |
| Save: Crossref failure during the lookup | 503 / 502 with the usual Crossref error body (no trace); nothing saved | `on_crossref_error` |
| Save: already saved | 200 with the original row; no lookup | `save_paper`, `repo.get` |
| Remove: not in this client's list | 404 `not_saved` | `remove_paper` |
| Database unavailable (list / save / remove) | 503 `storage_unavailable`, retryable, no connection details; `/api/ask` unaffected; `/api/health` says `db: unavailable` | `StorageError`, `on_storage_error` |
| Unexpected server bug | 500 `internal_error` envelope; details logged server-side only | `on_unexpected_error` |

## 7. Tests

```bash
uv run pytest                                   # all tests (349)
uv run pytest tests/test_workflow.py            # the end-to-end scenarios
uv run pytest "tests/test_grounding.py::test_overclaiming_explanation_is_replaced_by_a_deterministic_one_and_recorded"
uv run ruff check . && uv run ruff format --check .
```

Both boundaries are faked in every test: Crossref with `FakeCrossrefClient` or the real client
over `httpx2.MockTransport`; the model with `FakeModelClient`, or the real
`AnthropicModelClient` against a stub SDK object. **No test calls Crossref or Anthropic.**

| File (tests) | Proves |
|---|---|
| `test_workflow.py` (53) | The whole pipeline through `POST /api/ask`: question → plan → Crossref fixture → selection → explanations; date and work-type constraints (the exact call arguments, and the deterministic re-check); missing abstract and authors; no results; Crossref 429/5xx and a failed refinement; model timeout/failure/malformed output (interpret, explain, both); a plan with hostile values; the model adding a paper, citing a DOI, overclaiming, or obeying an injected abstract; the question trying to cancel the search; every bibliographic field equal to the Crossref record; the trace's stages, counts, filters and rules; no secrets or contact address anywhere in the response. **Pre-check:** invention requests never reach the model or Crossref (also with the model down), the refusal's trace, and legitimate fabrication-topic questions still run. **Failure trace:** 429, 5xx, network and 400 keep the error contract and add the trace; a model fallback before the failure is recorded; no key, contact address, request URL or raw upstream text appears |
| `test_grounding.py` (25) | Each lint rule, slot validation (unknown, duplicate, missing refs), fallback text, evidence basis, and that bibliographic fields come from the record |
| `test_llm.py` (20) | What is sent to the API (model, explicit effort, structured output, no forced tools, escaped input); every SDK failure → its `ModelError` code; refusal/truncation/wrong-type responses; no credentials in errors or logs; a blank key counts as not configured |
| `test_ranking.py` (18) | Each hard constraint with its reason; de-duplication (same DOI, preprint vs published, far-apart years); the soft guard; ordering; the 12-paper window and 5-paper limit; the real fixture's duplicate pair |
| `test_plan.py` (43) | Query sanitizing, fallback plan, date/type → exact `filter=` string, the recency assumption, year bounds, alternative-query cap, no way for a plan to set rows or URLs; the fabrication pre-check on 11 refusal cases and 15 legitimate near-misses |
| `test_normalize_metadata.py` (16) | Authors (placeholders, organizations), partial dates, JATS → text, entity/DOCTYPE safety, truncation, `missing_fields` |
| `test_prompts.py` (5) | Hostile text can't close or forge tags; the explanation call sees only title/year/venue/abstract |
| `test_api_ask.py` (22) | HTTP contract and compatibility, 422s (no trace), Crossref error envelopes (same `error`, plus a trace), the 500 catch-all, shutdown, and the contact-address / credential privacy tests |
| `test_reading_list_api.py` (60) | The reading-list HTTP API: save from a just-recommended paper (no second Crossref call) and by Crossref lookup; clients can't supply metadata; unknown DOI 404; duplicate save 200; DOI normalization and case; 10 invalid payloads; Crossref failures while saving; missing and injection-like metadata; newest-first list; remove 204/404, encoded and slash-containing DOIs; the `X-Client-Id` requirement on all three endpoints; separate lists per client; **persistence across an application restart** (file database, three app lifetimes); database failure → 503 with no connection details in the response or logs, while `/api/ask` keeps working; health `db` state; the exact response contract |
| `test_reading_list_repo.py` (24) | The repository on SQLite: round trip with unicode, missing data stays missing, idempotent add, ordering, UTC timestamps, client separation, restart, SQL-looking text stored as data, the composite primary key and index, database-enforced uniqueness, URL mapping for Neon, and failure handling with a driver error full of secrets (never logged or returned), plus schema-creation retry |
| `test_paper_cache.py` (6) | Only Crossref fields cached, independent copies, TTL expiry, eviction order |
| `test_crossref_client.py` (34), `test_capture_fixtures.py` (10), `test_normalize.py` (6), `test_app.py` (7) | M1/M2 behavior, plus `get_work` (record, 404 as not found, path encoding, error mapping, malformed responses). `test_app.py`'s health test now expects `db: "ok"` (it was a placeholder until M4) |

## 8. Why deterministic orchestration instead of an autonomous tool-using agent?

This is the central design choice, and it is now implemented in `Orchestrator.ask`.

**What the model controls:** reading the question into a `SearchPlan`, and wording the
explanations for papers the application selected.

**What deterministic code controls:** every Crossref call (what, how many, with which filters
and limits); filtering, de-duplication and ordering; which papers are shown; every bibliographic
field; the evidence basis; whether model prose is accepted; every fallback; and the
deterministic refusal of obvious requests to invent papers.

**Why this boundary suits this assignment:**
- **Grounding.** The model cannot produce the metadata users rely on, so model-written metadata
  cannot become source-of-truth data. Model *prose* can still be wrong or overstated; the checks
  in §4 reduce that and the UI labels it, but they cannot eliminate it.
- **Testability.** Each step is a pure function or a boundary behind an interface, so every
  failure path (timeouts, malformed output, overclaiming, injected text) is a plain unit test
  with no network.
- **Predictability.** Each request makes exactly two model calls and at most two Crossref
  calls, so cost, latency and rate-limit behavior are bounded.
- **What we give up:** open-ended exploration such as chasing citations or reformulating many
  times. For a 3-5 paper recommendation with an auditable trace, one bounded refinement is
  enough, and the assignment allows deterministic orchestration.

## 9. Known limitations (as of M4)

- **Relevance quality.** Crossref's search is lexical. A relevant paper that uses different words
  can rank low, and the 12-paper window means it may never be considered. The soft term guard and
  one refinement search mitigate this only partly.
- **Prose checks are lexical.** A subtly wrong or overstated explanation can pass.
- **The fabrication pre-check is deliberately narrow.** It recognises obvious instructions only.
  A paraphrase it doesn't match ("I'd like a few convenient sources for my claim, real or not")
  reaches the model, whose `fabrication_request` classification is the second layer. If the model
  is also unavailable, the fallback plan searches Crossref with the question's keywords. That can
  only return real papers (nothing is invented), but it won't say the request was declined. The
  opposite error is also possible: an unusual phrasing of a legitimate question could match.
- **Dates.** The year filter uses Crossref's `from-pub-date`/`until-pub-date`; the deterministic
  re-check uses `issued` (falling back to `published`). The two can disagree for papers published
  online before print.
- **Abstracts.** Many Crossref records have none (4 of the 5 in the captured fixture), and the
  ones that do are truncated to 1,500 characters and may be publisher-copyrighted.
- **No rate limiting or retries on our side yet** (Crossref's per-pool limits are recorded in the
  trace but not enforced), and no per-IP or daily cap on model calls, so a public deployment
  needs the resilience milestone first.
- **Failure traces are minimal.** A failed primary search reports the plan, the failing step, and
  fixed-text failure fields, but not Crossref's rate-limit headers (the client raises before
  reading them) and not any partial work, because there isn't any.
- **The reading list is not private.** `X-Client-Id` is browser-level separation only; anyone who
  has an id can read and change that list, and clearing site data loses the browser's access.
  There are no accounts, no export, and no way to recover a lost id.
- **Postgres is exercised only through SQLite in automated tests.** The SQL is portable and the
  failure path is tested, but the real Neon connection is verified only by the manual deployment
  smoke test. The tests deliberately never contact a database server.
- **The recent-recommendations cache is per process.** After a restart (or on a second instance) a
  save falls back to a Crossref `/works/{doi}` lookup, which adds one request.
- **Deployment and the remaining documents** (README, DESIGN, AI_USAGE, VERIFICATION) are not
  built yet.
