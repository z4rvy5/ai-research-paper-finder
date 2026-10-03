# Learning Guide

A guide to how this codebase actually works. It is updated at the end of each implementation
milestone, so it only describes code that exists. Planned-but-unbuilt parts are marked
**(planned)**.

**Current state: milestone 2 — one Crossref search.** The question is sent to Crossref as a
single bibliographic query, and the results come back as a list of DOI/title links. There is no
model call, filtering, ranking or persistence yet.

---

## 1. Request flow

### `POST /api/ask` today

```
Browser (app.js)                FastAPI (app/main.py)             Crossref client (app/crossref/)
────────────────                ─────────────────────             ───────────────────────────────
onSubmit()
  POST /api/ask {question} ───▶ AskRequest.clean_question()
                                  (schemas.py: collapse whitespace,
                                   reject control chars, 3–500 chars)
                                  └─ invalid → on_validation_error → 422 {"error":{...}}
                                ask()
                                  crossref.search_works(question) ─▶ build_search_params()
                                                                     GET https://api.crossref.org/works
                                                                       ?query.bibliographic=<question>
                                                                       &rows=25&sort=relevance
                                                                       &select=DOI,title,author,…
                                                                     User-Agent: paper-finder/0.1 (mailto:<CROSSREF_MAILTO>)
                                                                       ↑ a header, never in the URL; "(mailto:…)" only if set
                                                                     _get(): HTTP status → CrossrefError
                                                                     _message(): validate envelope
                                                                     _rate_limit_info(): x-* headers
                                                ◀── SearchResult(request url, http_status,
                                                    total_results, raw items, rate_limit)
                                  normalize_works(result.items)    (normalize.py)
                                  └─ CrossrefError → on_crossref_error → 502/503 {"error":{...}}
  renderResults() ◀───────────  AskResponse {status, papers[], search{...}}
```

Step by step:
1. **Validation** (`AskRequest` in `app/schemas.py`): whitespace and newlines are collapsed to
   single spaces. Control characters are rejected. The cleaned question must be 3–500
   characters, and unknown fields are rejected. On failure, the `RequestValidationError`
   handler in `create_app` returns `422 {"error": {"code": "invalid_input", ...}}`, and Crossref
   is never called.
2. **Search** (`CrossrefClient.search_works` in `app/crossref/client.py`): a single
   `GET /works` with the cleaned question as `query.bibliographic`, Crossref's field for
   bibliographic search. It requests `rows=25` and explicit `sort=relevance`, and uses `select=`
   to fetch only the fields we use.
3. **Contact address (optional, User-Agent only):** `CROSSREF_MAILTO` is explicit project
   configuration, read only from that environment variable or `.env` via `Settings`. It is never
   inferred from anything else, and it is never put in the URL.
   - **Set:** it is sent in the `User-Agent` header as `paper-finder/0.1 (mailto:<address>)`.
     That is one of the two identification methods Crossref documents for its polite pool.
   - **Unset:** **no email address is sent at all**: the User-Agent is a plain
     `paper-finder/0.1`. Requests then use the public pool, which has lower limits.
   - See the "Contact address: User-Agent only" decision in section 4 for why it's a header and
     not a parameter.
4. **Response handling:**
   - `_get()` turns every non-200 status, timeout and network error into a `CrossrefError` with a
     stable `code` and a `retryable` flag.
   - `_message()` checks the envelope: valid JSON, `status == "ok"`, a dict `message`, a list
     `items` and an int `total-results`.
   - `_rate_limit_info()` records `x-api-pool`, `x-rate-limit-limit`, `x-rate-limit-interval` and
     `x-concurrency-limit` exactly as reported.
   - The recorded request URL never contains the contact address (it is only in the User-Agent
     header), so it can't reach the browser.
5. **Normalization** (`normalize_works` in `app/crossref/normalize.py`): each raw record
   becomes a `Paper(doi, title, url, crossref_rank)`. Records without a DOI are skipped, because
   there's nothing to cite. `crossref_rank` keeps the record's position in Crossref's order.
6. **Response:** `AskResponse` has `status` (`ok` or `no_results`), `papers`, and `search` (the
   query, request URL, HTTP status, total and returned counts, and rate-limit headers). `search`
   becomes a trace step in milestone 7.
7. **Rendering** (`renderResults` in `app.js`): a summary line plus an ordered list of title
   links. Everything is built with `textContent`, and a link is only set when the URL starts
   with `https://doi.org/`.

### Target (planned)
`POST /api/ask` → interpret (model) → `search_papers` with filters → filter / dedup / order
(code) → explain (model) → grounding checks (code) → response and trace.

## 2. File and function map

| File | Key contents |
|---|---|
| `app/main.py` | `create_app(settings=None, crossref=None)` is the app factory. If no Crossref client is passed it builds a real `CrossrefClient`; the `lifespan` handler closes it on shutdown. Routes: `health()`, `ask()`, `index()`. Error handlers: `on_validation_error` (422) and `on_crossref_error`, which uses `CROSSREF_ERROR_STATUS` to map codes to 502/503. `error_response()` builds the `{"error": {code, message, retryable}}` envelope. |
| `app/crossref/client.py` | `PaperSearch` (Protocol) is **the boundary**: `search_works(query, rows)` plus `aclose()`. `CrossrefClient` is the real implementation (async `httpx2`, 8s timeout). `build_search_params()` and `user_agent()` are pure helpers shared with the capture script. Also: `SearchResult`, `RateLimitInfo`, `CrossrefError`, `SELECT_FIELDS`. |
| `app/crossref/normalize.py` | `normalize_works(items)` normalizes in order; `normalize_work(item, rank)` handles one record; `doi_url(doi)` builds the link. |
| `app/schemas.py` | API contracts: `AskRequest` (with `clean_question`), `AskResponse`, `Paper`, `SearchInfo`, `ErrorResponse`/`ErrorBody`, `HealthResponse`. |
| `app/config.py` | `Settings`; `crossref_mailto` is used from this milestone on. |
| `app/static/app.js` | `onSubmit()` posts the question and shows a loading state and errors. `renderResults()` is the minimal list. `checkHealth()` is unchanged. |
| `scripts/capture_fixtures.py` | Makes **live** Crossref calls to record test fixtures. Run manually; never run by pytest. `resolve_mailto(environ)` reads only `CROSSREF_MAILTO`, and an unset or blank value means the public pool with no email sent. `capture(http, …)` writes the fixtures and never writes the address into them. The address is sent only in the client's User-Agent header. `main()` wires the two together. |
| `tests/fakes.py` | `FakeCrossrefClient` implements `PaperSearch`: it returns a canned `SearchResult` or raises a `CrossrefError`, and records the queries it received. |
| `tests/fixtures/crossref/works_llm_software_testing.json` | A real captured response (5 items) with its rate-limit headers. |

## 3. Model vs deterministic boundary
**Everything is deterministic so far.** The question goes to Crossref verbatim (after
whitespace cleanup), and results appear in Crossref's relevance order. Nothing is filtered,
reordered or explained.

Deterministic code currently controls:
- input validation
- the exact Crossref request: parameters, fields, and the User-Agent contact address
- mapping HTTP and network failures to error codes
- building each DOI link from the DOI
- result order (Crossref's)

The model is **(planned)** for milestones 5–6. Settings already default to Claude Sonnet 5.5
with explicit low effort.

## 4. Design decisions so far

- **Crossref sits behind a Protocol (`PaperSearch`).** The app depends only on
  `search_works()` and `aclose()`. Tests inject `FakeCrossrefClient` through
  `create_app(crossref=...)`, with no monkeypatching. The real client is tested separately
  against a mocked HTTP transport.
- **`httpx2` with its built-in `MockTransport`, not `httpx` + `respx`.** `httpx2` is the
  maintained successor to `httpx`, with the same API. Starlette's test client already prefers
  it, and `respx` only supports the old `httpx`. `MockTransport` lets client tests check the
  real request (URL, params, headers) without another dependency.
- **`query.bibliographic`, not `query`.** Crossref describes it as the field for bibliographic
  search (titles, authors, venues). [F] Sending the raw question has a visible cost. For the
  captured fixture question "Find recent papers about using LLMs for software testing",
  Crossref matched **8.1 million** records. The top hits matched incidental words ("Find",
  "Using"), for example "Editorial: Find the missing links?". This is real evidence for the
  planned model interpretation step (milestone 5).
- **Explicit `sort=relevance` and `select=`.** Crossref already sorts by relevance when a query
  is present; stating it makes the request self-documenting. `select` keeps responses small, as
  Crossref's guidance recommends.
- **Rate-limit headers are recorded, not yet enforced.** [F] Crossref uses separate pools for
  list queries (`*-array`) and single-record lookups (`*-single`). The fixture shows
  `polite-array`, 3 requests/1s, concurrency 3. The per-pool limiter that *uses* these values
  is milestone 10.
- **DOI links are built from the DOI, not taken from Crossref's `URL` field.** Every link has
  the form `https://doi.org/<doi>`. Characters that could change the URL's meaning (`#`, `?`,
  `%`, spaces) are percent-encoded. A test proves a hostile `URL` field is ignored.
- **Contact address: User-Agent only (a deliberate choice).** [F] Crossref documents two ways
  to identify yourself for its polite pool: a `mailto` query parameter, or a `mailto:` in the
  User-Agent header (CrossRef/rest-api-doc README, "Good manners = more reliable service"; the
  "Access and authentication" page, last updated 2025-10-16). We use only the header. It gives the
  documented contact mechanism and polite-pool behavior without putting the contact address into
  URLs, which get copied into places a header isn't:
  - **Logs.** [F] `httpx2` logs every request URL at INFO (`HTTP Request: GET <url> ...`). With the
    address in the URL (as `mailto=...%40...`) any INFO-level logging config would have written it
    to the logs. It doesn't log headers.
  - **Exception text.** `raise_for_status()` and some library exceptions embed the full URL.
  - **Echoed URLs.** The response's `search.url` (and later the trace) would have needed
    redaction code; now it's simply clean.

  What this means in practice:
  - **Unset:** nothing is sent. **Set:** the address goes to Crossref, in one header only.
  - It never reaches the browser: `/api/health` reports only a boolean and `search.url` has no
    address. It is never written into fixtures.
  - **Logs.** The URL has no address, so the library's request log has nothing to leak. The one
    remaining path is Crossref echoing the User-Agent back in a 400 error body, which is logged
    (first 500 characters). `CrossrefClient._redact` replaces the exact address in that text, and
    does it *before* truncating, so an address cut off at the boundary can't leave a partial copy.
  - **Exceptions.** `CrossrefError` messages are fixed strings, and `on_crossref_error` returns
    only `code`, `message` and `retryable`. The underlying library exception stays attached as
    `__cause__`; the app never logs or serializes it. Anything that adds exception logging later
    should keep that in mind.
  - **Tests** assert the address is in the User-Agent and nowhere else: not in the URL, the API
    response or the logs. `assert_address_absent` in `conftest.py` checks the raw and the
    URL-encoded form, because a raw-only check misses the `%40` form that URLs and logs use.
  - [F] **Observed live (2026-10-03):** a request identified by User-Agent only was served from
    the polite pools (`polite-array`, 3/s; `polite-single`, 10/s), while unidentified requests got
    the public pools. The response's pool is recorded in `search.rate_limit.pool`, so it's
    visible after deployment.
  - [A] **Uncertainty.** Crossref's current page *recommends* the parameter ("strongly recommend
    providing a `mailto` parameter in all requests", so they can contact you before a manual
    block) and its November 2025 rate-limit post mentions only the parameter. Nothing says the
    User-Agent stops working, but we are choosing the less-emphasized documented option. If
    Crossref stops honoring it, `search.rate_limit.pool` will say `public-*` and the fix is to add
    `mailto` to `build_search_params` (and bring URL redaction back).
- **Error envelope from day one.** Validation failures and Crossref failures both return
  `{"error": {code, message, retryable}}`, so the UI handles every error the same way.

## 5. Failure modes (so far)

| Situation | What happens | Where |
|---|---|---|
| Empty, too short or too long question, control characters, extra fields | 422 `invalid_input`; Crossref not called | `AskRequest.clean_question`, `on_validation_error` |
| Crossref 429 | 503 `upstream_rate_limited`, `retryable: true` | `CrossrefClient._get` |
| Crossref 5xx, timeout, connection error | 503 `upstream_unavailable`, `retryable: true` | `CrossrefClient._get` |
| Crossref 400 (our request was invalid) | 502 `upstream_rejected`; Crossref's detail is logged server-side, with any configured contact address redacted | `CrossrefClient._get`, `_redact` |
| 200 with non-JSON or an unexpected shape | 502 `upstream_invalid_response` | `_message`, `search_works` |
| Zero records | 200 `status: "no_results"`; UI says so | `ask()`, `renderResults()` |
| Record without DOI | Skipped (not citable) | `normalize_work` |
| Record without title | Kept with `title: null`; UI shows "(No title in Crossref record)" | `_first_text`, `renderResults()` |
| Server unreachable from the browser | "Could not reach the server" message | `onSubmit()` |

Not handled yet, by design: retries, rate limiting on our side, caching (milestone 10);
filtering out non-article records such as supplementary files, and deduplication
(milestone 8).

## 6. Tests

```bash
uv run pytest                                         # all tests (65)
uv run pytest tests/test_crossref_client.py           # one file
uv run pytest "tests/test_api_ask.py::test_ask_sends_cleaned_question_to_crossref"
uv run ruff check . && uv run ruff format --check .
uv run python scripts/capture_fixtures.py             # LIVE: re-record fixtures (CROSSREF_MAILTO optional)
```

No test calls Crossref. Client, capture-script and contact-email tests use
`httpx2.MockTransport`; the other API tests use `FakeCrossrefClient`. Async tests run with
AnyIO's pytest plugin (`pytest.mark.anyio`), which ships with FastAPI's dependencies.

| File | Proves |
|---|---|
| `test_crossref_client.py` | The exact request (host, path, all parameters, with no `mailto` parameter; the address only in the User-Agent, or no address in any header or URL when unset); fixture items parsed in Crossref order; rate-limit headers recorded and the URL free of the address; missing headers become `None`; 429, 5xx, 400 and other status codes map to the right codes and `retryable` flags; timeouts and connection errors become `upstream_unavailable`; malformed 200 responses become `upstream_invalid_response`. **Privacy:** with logging at DEBUG, the configured address (raw or URL-encoded) appears in no log record on success, on a 400 whose body echoes it, or when the address is cut off at the 500-character log boundary; error text stays clean even when the library exception embeds the URL and the User-Agent |
| `test_normalize.py` | Fixture records keep their Crossref DOI, title and rank; DOIs are lowercased; links are built from the DOI, ignoring a hostile `URL` field; records without a DOI are skipped while ranks keep Crossref positions; missing or empty titles become `None`; titles are whitespace-collapsed; DOI URL encoding |
| `test_api_ask.py` | Every returned paper comes from the fixture records; the cleaned question is what reaches Crossref; `no_results` status; 7 invalid payloads get a 422 envelope without calling Crossref; each Crossref failure maps to the right status and envelope; app shutdown closes the client. With the real `CrossrefClient` over a mock transport: unset `CROSSREF_MAILTO` sends no address in the URL or any header; a configured one is in the User-Agent only (not the URL), and never appears in the API response or the logs. A 400 or 500 whose body echoes the address, and a library exception whose text embeds the URL and User-Agent, expose it neither in the API response nor in the logs |
| `test_capture_fixtures.py` | `resolve_mailto` returns `None` for unset or blank `CROSSREF_MAILTO` and ignores other variables; capture without a mailto sends no address; capture with a configured one sends it in the User-Agent only (not the URL) and never writes it into the fixture; fixtures keep the trimmed envelope and rate-limit headers; a failed capture (400, 429, 500) raises `HTTPStatusError` without the address in its text and writes nothing |
| `test_app.py` | Health contract, secret values never exposed, static serving, settings defaults and env overrides |

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
  Every failure path can be reproduced in a unit test without network access. Milestone 2
  already works this way: `PaperSearch` + `FakeCrossrefClient`.
- **Predictability:** each question makes exactly two model calls and at most a few Crossref
  calls. Cost, latency and rate-limit behavior are bounded, which matters for a public demo on
  free hosting.
- **What we give up:** an autonomous agent could explore more open-endedly, for example chasing
  citations or reformulating queries many times. For a 3–5 paper recommendation with an
  auditable trace, one bounded refinement search is enough. The assignment explicitly allows
  deterministic orchestration.
