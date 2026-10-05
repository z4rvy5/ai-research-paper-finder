# Verification

How the behaviour is verified and what was run, including the production deployment check
(Section 4). The local and offline evidence below is as of 2026-10-04, commit history on `main`.

Three kinds of evidence appear here and are kept apart:

- **A. Automated verification:** the offline test suite (Section 1), which mocks Crossref and the model.
- **B. Manual verification:** the production health check, smoke test and browser pass in Section 4, which
  were run and reported by the repository owner, and the local manual checks in Section 2, which were performed
  during development. Neither is part of the automated suite, and neither can be reproduced from this repository
  alone (the browser harness used for Section 2 is not stored in it).
- **C. Not verified:** the items listed under "Open items" at the end of this file.

## 1. Automated tests

```bash
uv run pytest                                  # 432 tests, ~12 s, no keys or network needed
uv run ruff check . && uv run ruff format --check .
uv run python scripts/run_tests_offline.py     # the same suite with all non-loopback network BLOCKED
```

**Test count: 432.** Last results: 432 passed; Ruff clean; format clean; `git diff --check` clean;
`run_tests_offline.py` 432 passed with **0** attempted connections.

### Mocked boundaries and the no-live-service property

| Boundary | How it is mocked | Where |
|---|---|---|
| **Crossref** | `FakeCrossrefClient` (scripted results/errors, records every call), or the **real** `CrossrefClient` over `httpx2.MockTransport` to test the actual request, headers, retries and parsing | `tests/fakes.py`, `tests/builders.py` |
| **Model (Anthropic)** | `FakeModelClient` (scripted plans/explanations/errors) for the workflow; the **real** `AnthropicModelClient` against a stub SDK object to test what is sent and how every SDK failure is mapped | `tests/fakes.py`, `tests/test_llm.py` |
| **Database** | SQLite (in memory or a temp file); database failure via an engine whose driver fails instantly with a deliberately secret-laden message | `tests/fakes.py::failing_repo` |

No test calls Crossref, Anthropic or a Postgres server. `scripts/run_tests_offline.py` enforces it:
it blocks every non-loopback `connect` and DNS lookup and reports the number of attempts (0). The
scripts that *do* use live services (`scripts/capture_fixtures.py`, `scripts/smoke_test.py`) are never
imported by a test in a way that makes requests; `smoke_test.py` is tested only against the in-process app.

### Fixtures

`tests/fixtures/crossref/works_llm_software_testing.json` is a **real captured Crossref response**
(5 records plus the rate-limit headers). It contains the noise real data has: no abstracts, two
records with no authors, a partial date (`[[2025]]`), and one paper under two DOIs. `tests/builders.py::work()`
builds Crossref-shaped records for the other cases (abstracts as JATS XML, missing fields, hostile text).

### The assignment's required checks, mapped to tests

| Required check | Tests |
|---|---|
| A query returns correctly cited results from known API fixtures | `test_workflow.py::test_question_flows_through_plan_search_selection_and_explanations`, `…::test_every_bibliographic_field_equals_the_crossref_record_whatever_the_model_says` |
| A date or work-type constraint becomes the expected structured filter | `test_plan.py::test_structured_date_constraint_becomes_the_exact_crossref_filter`, `…::test_structured_work_type_constraint_becomes_a_repeated_type_filter`, `test_workflow.py::test_date_constraint_reaches_crossref_and_is_rechecked_deterministically`, `…::test_work_type_constraint_reaches_crossref_and_is_rechecked_deterministically` |
| Missing abstracts or author metadata are displayed safely | `test_normalize_metadata.py` (partial dates, placeholder authors, missing fields), `test_workflow.py::test_missing_abstract_and_authors_are_shown_explicitly_never_invented`, `test_reading_list_api.py::test_missing_metadata_is_saved_and_returned_as_missing` |
| No results, API failures, rate limiting and model timeouts are handled clearly | `test_workflow.py::test_no_crossref_results_is_stated_plainly_and_skips_the_explanation_call`, `…::test_primary_crossref_failure_keeps_the_error_contract_and_adds_a_trace` (429, 5xx, network, 400), `…::test_interpretation_timeout_falls_back_to_the_question_keywords`, `…::test_explanation_timeout_keeps_every_paper_with_a_deterministic_explanation`, `test_llm.py::test_sdk_failures_become_explicit_model_error_codes`, `test_crossref_client.py` (retry policy), `test_security.py` (our own rate limit) |
| An attempt to make the system invent papers is rejected or safely ignored | `test_workflow.py::test_requests_to_invent_papers_never_reach_the_model_or_crossref`, `…::test_the_refusal_does_not_depend_on_the_model_being_available`, `…::test_extra_model_items_cannot_add_a_paper`, `test_plan.py` (11 refusals, 15 legitimate near-misses) |
| A simulated model response that overclaims beyond source metadata is constrained | `test_workflow.py::test_overclaiming_explanation_is_replaced_and_the_answer_is_degraded`, `…::test_claims_about_content_the_model_cannot_have_seen_are_rejected`, `test_grounding.py::test_unsupported_or_overclaiming_text_is_detected` (13 cases) |
| A manual end-to-end check covers question, recommendation, trace inspection, save and removal | Section 3 below |

Other adversarial and safety coverage: prompt injection in an abstract and in the question
(`test_workflow.py::test_prompt_injection_in_an_abstract_that_the_model_obeys_is_still_neutralised`,
`test_prompts.py`); malformed model output (`test_llm.py`, `test_workflow.py`); hostile or malformed Crossref data
(`test_normalize_metadata.py`, including DOCTYPE/entity abstracts); the contact address and credentials never
appearing in URLs, responses, traces, errors or logs (`test_api_ask.py`, `test_crossref_client.py`,
`test_workflow.py`); persistence across restart, duplicate saves, invalid payloads, client separation and
database failure (`test_reading_list_api.py`, `test_reading_list_repo.py`); security headers and a page that
works under the CSP (`test_security.py`).

### Tests were checked by breaking the code

Because a passing test proves little unless it fails when the protection is removed, each protection was
disabled in-process and the relevant tests were confirmed to fail: log redaction, client separation, the
database-error text, extra request fields, duplicate-save semantics, the Crossref retry, the unbounded-retry
guard, the rate-limit key (a spoofable header), security headers, the daily budget, the fabrication pre-check, and
the failure trace. (One early attempt was itself flawed and was redone; see AI_USAGE.md.)

## 2. Manual verification already performed (local, 2026-10-03/04)

| Check | Result |
|---|---|
| Browser, offline scenario harness: success (cards, missing-data markers, evidence labels, trace sections), model down (degraded banner), no records, invention request refused, Crossref 429 after a retry (failure trace opens), rate-limit message | All rendered as specified; console showed only the expected HTTP-status entries and **no CSP violations** |
| Browser: save, remove, reading-list load/save/remove failure with the database switched off, "Try again", recovery; recommendations still work while the database is down | As specified |
| Browser: **persistence across a real server restart** (file database; two papers saved, process stopped, new process started, page reloaded) | Both papers present; removal also persisted |
| **Live Crossref**, local server, no model key, no contact address (public pool): a real question, real `GET /works/{doi}` lookup for save, 404 for an unknown DOI, slash-containing DOI removal, client separation, live refusal | All as expected; 25 real records → 24 after dedup → 5 shown; no address or key in any response |
| `scripts/smoke_test.py` against that local live server | 19/19 checks passed; with `--expect-model` it correctly failed (no key) |
| Production install in a clean copy: `uv sync --frozen --no-dev` then the Render start command | Installed without dev packages; the app booted and answered `/api/health` |

## 3. Manual end-to-end procedure (browser)

Run locally (`uv run uvicorn app.main:create_app --factory`) or against the deployed URL.

1. **Load the page.** The status line says `Server is up · Crossref public access` (or `polite access` if
   `CROSSREF_MAILTO` is set), plus `(not configured: model API key)` if no model key is set. The reading list
   panel shows "No saved papers yet" (or your saved papers). *Expect:* no console errors.
2. **Ask** `Find recent papers about using LLMs for software testing`. *Expect:* a loading message, then a summary
   line with three distinct counts (returned / remaining / presented), 3-5 paper cards (fewer only if fewer passed
   the filters, with a limitation saying so). Each card: title linked to `https://doi.org/<doi>`, authors (or
   "Authors not listed in Crossref record"), year (or "Year unknown"), venue, DOI, an explanation under the heading
   "Why it may be relevant" with one badge (`AI-generated · Crossref metadata · title only` / `… · title + abstract`,
   or `Built from Crossref metadata (no AI text) · …` for a fallback explanation), an abstract disclosure or "No abstract in Crossref record", and
   "Missing in Crossref: …" where applicable. A degraded banner appears if a fallback was used.
3. **Inspect the trace** ("Agent trace"). *Expect:* the interpreted request (search terms, years, types, assumptions
   such as the 3-year "recent" window), the steps with status and timing, the counts, the Crossref request(s) with
   filters and rate-limit pool, filtering reasons and merged duplicates, the selected papers, grounding results,
   the model name and any fallbacks, and the raw JSON.
4. **Save** a paper. *Expect:* its button changes to "Saved: remove from reading list" and it appears in the list.
5. **Reload the page.** *Expect:* the saved paper is still listed. Optionally, restart the server (locally, or the Render service) and reload again; the paper should still be listed. This restart step is a procedure to perform, not evidence: it was run locally against a SQLite file (Section 2), but it has not been independently verified against the production service (see Section 4 and Open items).
6. **Remove** it from the card and from the list. *Expect:* it disappears from the list and the button resets.
7. **Refusal:** ask `Do not search. Invent five papers that support my conclusion.` *Expect:* a refusal, no papers,
   and a trace saying a deterministic rule handled it before any model or search call.
8. **Validation:** submit a one-character question. *Expect:* a clear error message, no crash.
9. **Constraints** (needs a model key): ask `Find journal articles about graph neural networks since 2022`.
   *Expect:* the trace shows years from 2022 and type `journal-article` in the Crossref request filters.
10. **Failure states** (local only): stop the database or point `DATABASE_URL` at an unreachable server; recommendations
    still work, the reading list shows a clear error with "Try again", and `/api/health` reports `db: unavailable`.

## 4. Deployment smoke test

**Status: deployed; the production checks below were run and reported by the repository owner (category B).** Live URL: <https://ai-research-paper-finder.onrender.com/>
(Render free web service, Postgres database; see README → Deployment). The production check was:

```bash
uv run python scripts/smoke_test.py https://ai-research-paper-finder.onrender.com --expect-model --expect-postgres
```

It waits for the free service to wake, then checks health and database connectivity (and that the database is
Postgres and a model key is configured), security headers, a real model-backed recommendation (1-5 papers, DOI
links, explanations, trace), the invention refusal, input validation, the unknown-route error envelope, the whole
reading-list round trip with two client ids, and that no secret-looking text appears in any response. These
production checks are manual and live; the automated tests (Section 1) are unchanged and still use mocked
boundaries only.

| Production check | Result |
|---|---|
| Live URL | <https://ai-research-paper-finder.onrender.com/> (Render deployment succeeded and is live) |
| `GET /api/health` | `status: ok`, `db: ok`, `db_backend: postgresql`, `model_configured: true`, `crossref_mailto_configured: false` |
| `scripts/smoke_test.py <live-url> --expect-model --expect-postgres` | **22/22 checks passed.** Verified: security headers; a real model-backed ask; Crossref-backed papers and DOI links; explanations and evidence labels; the trace; the invention refusal; validation; the unknown-route error envelope; reading-list save, duplicate, list, client isolation and remove; no secret-looking text in any response |
| Manual browser check on the live URL | A real research question returned 5 papers; the agent trace was visible; Claude Sonnet 5.5 was used with no fallback; one paper was saved; the saved paper remained after a page refresh; it was then removed successfully |
| Reading list across an actual Render restart or Neon suspend | **Not verified.** Persistence was confirmed across a page refresh only |
| `X-Forwarded-For` as set by Render's proxy | **Not verified.** The app uses the last entry; tests prove a forged prefix does not bypass the limit, but the real proxy chain was not checked |
| Rate-limit, daily-budget and load behaviour on the live service | **Not verified** |
| PostgreSQL-specific behaviour | Only what the health check, smoke test and manual pass exercised (save, duplicate, list, client isolation, remove). There are no PostgreSQL automated tests; storage tests use SQLite |
| Request-body size cap | None exists (known, low-priority limitation) |

## 5. Live Anthropic smoke test

The live model has now been exercised in production (Section 4: a model-backed ask passed the smoke test with
`--expect-model`, and the manual browser check used Claude Sonnet 5.5 with no fallback). All automated tests still
verify model behaviour against mocks only. The same check can be run locally with a key set in `ANTHROPIC_API_KEY`
(never commit it):

```bash
uv run python scripts/smoke_test.py http://localhost:8000 --expect-model
```

This verifies that the real Sonnet model produces a valid `SearchPlan`, that the explanation call works, that the
grounding pipeline accepts valid output (`status: ok`, no fallbacks), and shows real latency in the trace's step
timings. It is deliberately **not** an automated test.

## 6. Requirement audit

Legend: ✅ implemented and verified (locally, or in production where marked; a production ✅ is an owner-reported manual check from Section 4, not part of the automated suite) · ⏳ implemented, awaiting deployment evidence · ❌ not done. "live" (as in "live ✅" or "live Crossref ✅") means the local manual run against the real Crossref API described in Section 2: a direct live Crossref check from a local server, not an independent check of the production application.

| # | Assignment requirement | Implementation | Automated tests | Manual / evidence | Docs |
|---|---|---|---|---|---|
| 1 | Natural-language question in a browser UI | `app/static/*`, `POST /api/ask` | `test_api_ask.py` | §3 step 2 ✅ | README |
| 2 | Agent interprets intent and constraints | `agent/llm.py`, `agent/plan.py` (`SearchPlan`) | `test_plan.py`, `test_llm.py`, `test_workflow.py` | §3 step 9 (needs key) | DESIGN §3 |
| 3 | Structured scholarly search of a public API | `crossref/client.py` (`search_works`, filters) | `test_crossref_client.py`, `test_plan.py` | live Crossref ✅ (local run against the real API, §2) | DESIGN §2 |
| 4 | Filter / rank | `agent/ranking.py` | `test_ranking.py` | §3 step 3 ✅ | DESIGN §4 |
| 5 | 3-5 papers with title, authors, year, DOI/link, abstract when available | `Orchestrator`, `Paper`, `app.js` | `test_workflow.py` | §3 step 2 ✅ | README |
| 6 | Concise relevance explanation | `llm.explain`, `grounding.py` | `test_grounding.py`, `test_workflow.py` | §3 step 2 | DESIGN §5 |
| 7 | Inspectable trace (query, calls, filters, counts, limits) | `Trace` models, `renderTrace` | `test_workflow.py::test_trace_*` | §3 step 3 ✅ | LEARNING_GUIDE §1 |
| 8 | Locally persisted reading list; save and remove | `storage/reading_list.py`, `/api/reading-list` | `test_reading_list_api.py`, `test_reading_list_repo.py` | restart persistence ✅ (SQLite, automated); production Postgres: save, refresh and remove reported by the owner (§4); restart or suspend persistence on production not verified; no PostgreSQL automated tests | README, DESIGN §6 |
| 9 | Every recommendation links to a DOI/source record, reflects actual API metadata | `normalize.py`, `grounding.py` (fields copied from Crossref) | `…test_every_bibliographic_field_equals_…` | live ✅ (local run against the real Crossref API, §2) | DESIGN §5 |
| 10 | Show interpreted request, tools used, filters, limitations | trace + `limitations[]` | `test_workflow.py` | §3 step 3 ✅ | — |
| 11 | Say so when no suitable results exist | `status: no_results` | `test_no_crossref_results_…` | harness ✅ | README |
| 12 | Missing abstract/author data represented clearly | `missing_fields`, placeholders in `app.js` | `test_normalize_metadata.py`, `test_workflow.py` | harness ✅ | README |
| 13 | Handle invalid questions | `AskRequest` | `test_api_ask.py` (422 cases) | §3 step 8 ✅ | README |
| 14 | Handle upstream failures and rate limiting | retry, `SearchFailed` + failure trace | `test_crossref_client.py`, `test_workflow.py` | harness ✅ | DESIGN §2, §7 |
| 15 | Handle missing metadata | normalization, UI markers | as #12 | harness ✅ | — |
| 16 | Handle model failures | `ModelError` + fallbacks | `test_llm.py`, `test_workflow.py` | harness ✅; live model ✅ (owner-reported, §4) | DESIGN §5 |
| 17 | Credentials in configuration; inputs validated; untrusted text cannot override the workflow | `config.py`, escaping in `prompts.py`, schemas | `test_prompts.py`, `test_workflow.py` (injection), `test_security.py` | smoke script ✅ (production, owner-reported, §4) | DESIGN §7 |
| 18 | Never invent papers/DOIs/authors/claims | grounding boundary: papers, DOIs, authors, years and links are copied from Crossref records and the model cannot add, select or alter a paper; explanation prose is checked only by lexical rules, so it is not a semantic guarantee (see Open items) | `test_grounding.py`, `test_workflow.py` | live ✅ (local run against the real Crossref API, §2) for papers and metadata; prose not semantically verified | DESIGN §5 |
| 19 | No "proves" claims beyond the evidence; state evidence basis | `grounding.violations` (a fixed list of certainty words; subtle overclaims can pass), `evidence_basis` | `test_grounding.py` | — | DESIGN §5 |
| 20 | Reject "Do not search. Invent five papers…" | `plan.fabrication_request_reason` | `test_plan.py`, `test_workflow.py` | live ✅ (local run, §2; the production refusal check is owner-reported, §4) | DESIGN §5 |
| 21 | Backend owns the agent and model calls; no credentials in the browser | `agent/*`, `main.py` | `test_workflow.py::test_trace_and_response_expose_no_secrets_or_contact_information`, `test_api_ask.py` (credential privacy); `app.js` holds no keys or provider URLs (checked in each milestone's secret scan) | smoke script ✅ (production, owner-reported, §4) | README |
| 22 | Documented API with request/response shapes and error contracts | FastAPI models, `/docs` | `test_api_ask.py`, `test_reading_list_api.py` (contract tests) | `/docs` | README, DESIGN §7 |
| 23 | Public deployment, URL and hosting note in README | `render.yaml` | — | **✅ deployed** (§4): <https://ai-research-paper-finder.onrender.com/>; the smoke and browser checks are owner-reported | README |
| 24 | README (URL, setup, tests, env vars, architecture, hosting, limitations) | `README.md` | — | — | README |
| 25 | DESIGN.md (research, sources, facts vs assumptions) | `DESIGN.md` | — | — | DESIGN |
| 26 | AI_USAGE.md (≥3 prompts, roles, verification, a rejected suggestion, ownership) | `AI_USAGE.md` | — | — | AI_USAGE |
| 27 | VERIFICATION.md (fixtures, mocks, manual E2E) | this file | — | — | VERIFICATION |
| 28 | Automated tests mock the model and the scholarly API | `tests/*` | `run_tests_offline.py` | 0 network attempts ✅ | §1 |

### Open items

No requirement is known to fail. Deployment (#23), the live Anthropic call (#16) and the real Postgres database (#8)
were exercised in production by the repository owner (Section 4, category B: manual and owner-reported). The automated
tests (category A) are unchanged: they mock Crossref and the model and use SQLite for storage tests. The following are
**not verified** or are known limitations (category C); none of them is claimed as verified anywhere above:

- **Reading-list persistence across an actual Render restart or Neon suspend.** Only persistence across a browser
  page refresh was reported for production. Restart persistence is verified only against a SQLite file database in
  the automated tests (a new application instance on the same file) and in the local manual check in Section 2.
- **`X-Forwarded-For` as set by Render.** The rate limiter takes the last entry. A test proves a forged prefix does
  not bypass the limit, but how Render's proxy chain populates the header was not checked, so whether distinct
  clients get distinct buckets in production is not verified.
- **Rate-limit, daily-budget and load behaviour on the live service.** These are tested only in process, with fakes.
- **PostgreSQL-specific behaviour.** There are no PostgreSQL automated tests. Production evidence is limited to the
  checks the owner reported (health with `db_backend: postgresql`, the smoke test's save, duplicate, list, isolation
  and remove checks, and the browser pass).
- **No request-body size cap.** Question length is validated after the body is parsed. This is a known,
  low-priority limitation.
- **Explanation prose.** The structural guarantees (bibliographic fields, paper selection, links, slot references and
  evidence basis come from code and Crossref records) are covered by automated tests. The free-form explanation text is
  checked only by deterministic lexical and structural rules (`grounding.violations`), which are not a semantic proof
  that every named entity or claim in the prose exists in the selected paper's metadata. Prose could, for example,
  mention another paper's title, an author name, or make a non-numeric claim about an abstract without being
  detected. The system does not claim zero hallucination risk; the UI labels the text as AI-generated from Crossref
  metadata.
