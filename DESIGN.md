# Design

Labels: **[F]** confirmed fact from official documentation or observed live (dates in parentheses),
**[D]** design decision, **[A]** assumption, **[T]** trade-off. Sources are listed at the end.

## 1. Shape of the system

A FastAPI backend owns everything that matters; a vanilla-JS page renders it. The workflow is a
**fixed pipeline, not an autonomous tool loop** [D]: fabrication pre-check → interpret (model) →
Crossref search (+ at most one refinement) → filter / de-duplicate / order (code) → explain (model) →
grounding checks (code) → response + trace. The assignment permits this [F: assignment]. [T] We give up open-ended
exploration; we gain testability, bounded cost (two model calls, at most two Crossref calls) and an auditable trace.
The suggested tools map to code as `search_papers` → `CrossrefClient.search_works`, `get_paper` →
`CrossrefClient.get_work` (used when saving), `filter_papers` → `ranking.select_papers`.

## 2. The scholarly source: Crossref

Crossref is the default source [F: assignment]. What we rely on:

- **Search.** `GET /works` with `query.bibliographic`; `filter=` takes comma-separated filters, ANDed,
  and a repeated filter name ORs (e.g. several `type:` values); `from-pub-date` / `until-pub-date`
  (`YYYY-MM-DD`), `type`, `has-abstract` exist; `select=` limits fields; `sort=relevance`. [F: REST API docs]
- **Result limits.** `rows` defaults to 20 and is capped at 1000; `offset` caps at 10,000 and `cursor`
  is for deep paging. We fetch one page of 25 and never page [D]: we need five papers, not an index.
- **Single record.** `GET /works/{doi}`; an unknown DOI returns a *plain-text* 404, not JSON; an invalid
  filter returns a 400 JSON `validation-failure` (observed 2026-10-03). Both are handled explicitly.
- **Rate limits.** Public and polite pools, with **separate limits for list and single-record
  requests**, reported in `x-api-pool` / `x-rate-limit-*` headers (observed live: polite list 3/s,
  polite single 10/s, public list 1/s, public single 5/s; announced 2025-11-05). Identification with a
  `mailto` parameter *or* a `mailto:` in the `User-Agent` selects the polite pool [F: REST API docs;
  confirmed live by User-Agent alone]. [D] We send the address only in the User-Agent so it never
  appears in a URL (and so not in `httpx` request logs, exception text, or traces). [A] Crossref's
  current page "strongly recommend[s]" the parameter, so identification by header alone could change;
  `trace.searches[].rate_limit.pool` shows the pool actually used.
- **Behaviour on limits.** 429 → one retry after `Retry-After` (capped at 2 s), then a clear 503 with a
  trace [D]. Crossref's per-pool limits are recorded, not enforced client-side [A: demo-scale traffic].
- **Data quality.** Abstracts are JATS XML and often absent; some may be copyrighted; author lists can
  be empty or contain placeholder family names such as `-`; ranking is lexical [F: docs, observed 2026-10-03].
  Non-paper records (front matter, an attached `…_supp1.pdf`) and one paper under two DOIs occur in real
  results [F: captured fixture].

## 3. From intent to structured arguments

The first model call returns a Pydantic **`SearchPlan`** through structured outputs [F: Claude API
structured outputs]: `intent` (`find_papers` / `out_of_scope` / `fabrication_request`), `topic_query`,
up to 2 `alt_queries`, `from_year` / `until_year`, `work_types` (an enum of six Crossref types),
`recency_requested`, `ambiguities`. It has **no field for URLs, filters or row counts** (`extra="forbid"`).
[F] Structured outputs do not support length or range constraints, so the schema is tolerant and code
enforces the bounds [D]: queries are reduced to letters, digits, spaces, hyphens and apostrophes
(≤200 chars); years must be 1900…next year and consistent; "recent" becomes a recorded 3-year window
chosen by code, not the model; rows are fixed at 25. If the model fails or returns an unusable plan,
a deterministic plan is built from the question's keywords (`source: "fallback"` in the trace).

## 4. Filtering, de-duplication, ordering (no scoring formula) [D]

1. **Hard constraints**, each recorded with a reason: no title; front matter / attached files; a work
   type or year outside the plan (re-checked even though Crossref filtered).
2. **De-duplicate** by DOI, then by normalized title within one year, keeping the more complete record
   (abstract, then authors, then article over preprint).
3. **Soft topic-term guard:** a paper whose title/abstract lacks every query term is *kept*, because
   relevant papers use different words; only unmatched, abstract-less records ranked beyond 12 are dropped.
4. **Order:** Crossref's relevance order picks the first 12 survivors; within them term matches, then
   papers with abstracts, then (only if recency was asked) newest first, then Crossref rank. The top 5 are shown.

## 5. Prompts, grounding and the evidence boundary

Two prompts, both stating that the question and all metadata are **untrusted data**, escaping them
(`&`, `<`, `>`) inside delimiting tags, and requiring structured output. The explanation call sees only
the title, year, venue and abstract of the papers *code already selected*, labelled `P1…P5`, and never DOIs,
URLs or authors. Its output type is `{ref, explanation}` only.

**What is guaranteed:** model-generated bibliographic metadata never becomes source-of-truth data.
Every field of a shown paper is copied from its normalized Crossref record, and refs are matched only
to slots we assigned, so the model cannot add, remove or alter a paper. **What is checked, not
guaranteed:** the prose. Each explanation is rejected (and replaced by a deterministic sentence built from the
paper's own metadata, recorded in the trace) if it has a link, DOI or citation, a certainty word ("proves",
"demonstrates that", …), a content claim about a title-only paper, a number absent from the evidence, markup,
or the wrong length. These checks are lexical and can miss subtle overclaims [limitation]. "Evidence
basis" (title only vs title + abstract) is decided by code. An obvious request to invent papers is refused by a
deterministic pre-check *before* any model or Crossref call; the model's `fabrication_request`
classification is a second layer.

## 6. Data model and persistence

One table, `saved_papers(client_id, doi, title, authors, year, venue, url, abstract, work_type,
missing_fields, saved_at)`, primary key `(client_id, doi)`, index on `(client_id, saved_at)`; nullable
columns keep missing data missing; JSON-as-text for lists; UTC timestamps [D]. SQLAlchemy Core with bound
parameters only, on Neon Postgres in production and SQLite in tests/local [D]. `pool_pre_ping` and a
recycle time handle Neon dropping idle connections [F: Neon suspends idle databases after 5 minutes].
`X-Client-Id` (a browser-generated UUID) separates one browser's list from another's; **it is not
authentication** and lists are not private [D, stated plainly in the UI and README]. Saving takes only a DOI;
the server supplies the metadata (a just-recommended paper from an in-process cache, else
`GET /works/{doi}`), so clients cannot store data of their choosing.

## 7. API contracts, errors, security, privacy

Endpoints and shapes are in the README. Every error is `{"error": {code, message, retryable}}`: 422 invalid
input, 400 missing client id, 404 unknown DOI / not saved, 409 reading list full, 429 rate limit (`Retry-After`), 502/503 Crossref
(with a `trace` of the plan, failing step, retries and Crossref's rate-limit headers), 503 storage (no
connection details). Model failures never fail a request: both stages have deterministic fallbacks and the
answer is marked `degraded`.

Credentials come only from server environment variables; the browser never receives the model key, database
URL or Crossref contact address, and traces and errors contain fixed text, not provider responses. The page has
no inline script or style, so a strict CSP (`default-src 'self'`) applies; server data is rendered with
`textContent`, and links are built from DOIs, never from model or Crossref URL fields. Per-address rate
limits (asks and saves, separately), a per-client cap on saved papers and a daily model-call cap
protect a public demo (limits in memory, per process).

## 8. Limitations

Lexical relevance; metadata-only evidence; lexical prose checks; a deliberately narrow fabrication
pre-check; in-memory limits; no automated browser tests. **Production verification:** the live Anthropic and
Neon paths have been exercised in the deployed service, while the automated tests still mock the model and other
external services and use SQLite for storage tests. The model is configuration: `claude-sonnet-5-5` by
default for cost and latency [A: adequate quality at `low` effort]; `claude-opus-5-5` is a one-variable change.

## Sources

- Crossref REST API documentation: <https://github.com/CrossRef/rest-api-doc> (etiquette, filters, `select`, `rows`, `offset`, `cursor`);
  <https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/> (pools, `mailto`, last updated 2025-10-16);
  <https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/>;
  <https://www.crossref.org/blog/announcing-changes-to-rest-api-rate-limits/> (2025-11-05).
- Claude API: structured outputs <https://platform.claude.com/docs/en/build-with-claude/structured-outputs>;
  models and pricing <https://platform.claude.com/docs/en/about-claude/models/overview>; errors <https://platform.claude.com/docs/en/api/errors>.
- Render: <https://render.com/docs/free>, <https://render.com/docs/python-version>, <https://render.com/docs/blueprint-spec>.
- Neon pricing / free plan: <https://neon.com/pricing>.
