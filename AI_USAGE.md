# AI usage

This project was built with an AI coding agent, **Claude Code**, working in the developer's
repository and terminal. This document describes how, from the actual working sessions. Prompts are
**faithful summaries**, not verbatim transcripts. Where something is the developer's decision rather
than the agent's, it says so. It does not describe any work that was not done.

## Who did what

| | Developer (repository owner) | AI agent (Claude Code) |
|---|---|---|
| **Direction** | Set the goal and milestones; approved or redirected every architecture proposal; chose the stack and hosting; defined the grounding and privacy rules; set an explicit stop-and-ask gate for design decisions | Proposed designs and trade-offs; asked for decisions rather than guessing at them when the rules required it |
| **Research** | Asked for official-documentation research and required confirmed facts to be separated from assumptions | Read the Crossref, Anthropic, Render and Neon documentation; probed the real Crossref API read-only to confirm response shapes, error bodies and rate-limit headers |
| **Implementation** | Reviewed milestone reports and diffs; accepted or rejected each milestone | Wrote the code, tests, fixtures and documentation, one milestone at a time |
| **Review** | Audited the agent's behaviour (see the privacy correction below); handled git history, commit metadata and GitHub privacy settings personally | Self-review passes: diffs scanned for secrets, personal data, unsafe rendering and live calls in tests |
| **Test design** | Required the adversarial cases (fabrication request, overclaiming, injection-like metadata, model timeout, malformed output) | Designed the fixtures and tests, then **broke each protection on purpose to confirm the tests fail** (mutation checks) |

There were no parallel sub-agents: one agent performed every role in sequence. Roughly, the
architecture research, the first two milestones and the first privacy audit ran on Claude Opus 5.5,
and the later work ran on Claude Sonnet 5.5 (the developer switched models between sessions).

## Prompts that shaped the project

**1. Architecture research, before any code.** *Summary:* "Don't implement yet. Research the
architecture for the smallest reliable, explainable system: read the current official docs for the
Crossref REST API (search, filters, pagination, abstracts, rate limits), model APIs with structured
output, and deployment options; propose a concrete 2-3 day design covering twelve listed areas
(frontend, backend, model boundary, Crossref boundary, deterministic filtering/ranking/dedup, the
evidence boundary, trace model, persistence, validation, test strategy, deployment, stack). Prefer
deterministic code for correctness-critical behaviour. For every significant choice, say whether it
is a confirmed fact, a design decision, an assumption or a trade-off. List the highest-risk failure
modes and how to test them." *Why:* to force research and falsifiable labelling before
implementation. *Result:* the plan that became [DESIGN.md](DESIGN.md), including the fact /
decision / assumption / trade-off labels used throughout it.

**2. Revising the architecture.** *Summary:* "Direction approved, but change: Sonnet 5.5 as the
default model behind the existing interface; drop the hand-tuned scoring formula in favour of
Crossref's order plus explicit rules; replace 'fabrication is structurally impossible' with accurate
wording (model prose can still be wrong); document the client id as *not* authentication; treat
Crossref's list and single-record rate limits separately with the headers as authoritative; reorder
the work so a browser flow exists early; add a learning guide." *Why:* the developer's review of the
first draft. This is where the first draft's weaknesses were corrected (below).

**3. The privacy audit.** *Summary:* "I am concerned about privacy. You said you used my account
email as the Crossref contact address. Change nothing. Audit the repository and explain exactly where
it is read, whether any email is hard-coded, whether it appears in files, fixtures, logs or git
history, what request was sent, and why you chose it instead of requiring explicit configuration."
*Why:* the agent had mentioned the lapse (below) in a milestone summary, and the developer
required a full, non-defensive accounting before any fix. *Result:* a complete disclosure (nine live requests had
carried the address), then the corrections.

**4. The core workflow specification.** *Summary:* "Implement the research workflow end to end with
a model-produced `SearchPlan`, a bounded Crossref search, deterministic filtering/dedup/ordering, a
selection of at most five papers from actual results, model-written explanations of only those
papers, grounding validation, deterministic fallbacks, an inspectable trace and a safe UI. The model
must never produce a paper, DOI, author or date; untrusted text must not steer the workflow. Mock
both boundaries; cover these 14 scenarios (…); then review the diff for secrets, personal data,
live calls in tests, model-generated metadata and unsafe rendering." *Why:* to specify behaviour
and the evidence for it together.

**5. Hardening before commit.** *Summary:* "Two targeted fixes only: a deterministic pre-check that
refuses 'invent papers' requests before any model or Crossref call (without blocking legitimate
questions about fabricated citations), and a safe trace on Crossref failures. Then re-verify,
including with network access blocked." *Why:* the developer spotted that the fabrication
requirement depended on the model.

**6. Unattended milestones with a human-approval gate.** *Summary:* "Implement the reading list,
then the complete UX and resilience, then final documentation and deployment. Stop and wait for me
on any architectural, security, scope or scoring decision; ordinary implementation choices are
yours. Never rewrite git history; never add co-author trailers; no live services in automated
tests." *Why:* to let routine work proceed while keeping design decisions with the developer.

## Verification practices

- **Documentation first.** Facts about Crossref, Claude structured outputs, Render and Neon were
  read from official pages and are cited in DESIGN.md. Crossref behaviour (headers, the plain-text
  404, validation errors) was also observed against the live API.
- **Tests that must fail.** After writing protective tests, the agent disabled each protection
  in-process (the log redaction, client separation, the retry, the rate-limit key, the security
  headers, the budget, the pre-check) and confirmed the relevant tests failed. One of these
  attempts initially proved nothing (a malformed stand-in made 52 unrelated tests fail) and was
  redone correctly.
- **Offline by construction.** The whole suite is also run with every non-loopback network
  connection blocked and must record zero attempts.
- **Real data.** A captured real Crossref response is a fixture; it exposed behaviour the agent
  had not anticipated (see below).
- **Manual checks of the UI** in a browser pane with an offline scenario harness (success, no
  results, refusal, upstream failure, degraded, database failure and recovery, rate limiting, and
  persistence across a real server restart), plus a local run against live Crossref.
- **Every milestone** ended with the full tests, lint, format check, whitespace check, and scans of
  the staged diff for secrets, personal data and stray files before a commit.

## Agent suggestions that were rejected, corrected or improved

1. **A weighted ranking formula (rejected by the developer).** The first architecture draft scored
   papers as `0.6·relevance + 0.3·term-coverage + 0.1·has-abstract`. The developer asked for
   something simple and easy to explain instead. The result is Crossref's relevance order plus
   explicit, individually recorded rules ([DESIGN.md §4](DESIGN.md), `app/agent/ranking.py`).
2. **A hard topic-term filter (softened at the developer's request).** The draft dropped any paper
   lacking a literal query term. That would discard relevant papers that use different words, so it
   became a soft guard: unmatched papers are kept and sorted lower, and only no-match,
   no-abstract records ranked far down are removed (`test_topic_term_guard_is_soft_papers_with_other_wording_are_kept`).
3. **Using the developer's account email as the Crossref contact address (an agent error).** During
   research and fixture capture the agent put the developer's account email into Crossref requests.
   Nothing required that: the public pool needs no address, and the account email was not to be sent
   to unrelated services. The agent mentioned it in a milestone summary; the developer's audit request
   followed, the agent disclosed all nine requests, and the corrections followed: the address is now optional, read only from
   `CROSSREF_MAILTO`, and tests assert the configured address appears in the `User-Agent` and
   nowhere else (URL, response, trace, logs). **Evidence:** `tests/test_crossref_client.py`,
   `tests/test_api_ask.py`, `tests/test_workflow.py`.
4. **A log filter, then a simpler design.** The agent's first fix redacted the address from logs
   after finding that `httpx` logs full request URLs. Reading Crossref's documentation showed the
   header alone identifies a client; the agent recommended a User-Agent-only design and the developer
   approved it, which removes the leak class instead of filtering it. A related self-correction: a raw-address check missed the
   URL-encoded form (`%40`) that the logs actually contained; the tests now check both forms.
5. **Model-selected papers (changed to code-selected).** The plan had the model choose 3-5 papers from
   eight candidates. The developer's workflow specification said the application selects and the model
   only explains, so the model now sees only already-selected papers and cannot influence which are
   shown.
6. **Test expectations corrected against real data.** The agent expected five results from the real
   captured fixture; the code returned four because two records were one paper under two DOIs. The
   behaviour was right and the expectation wrong, so the *test* was corrected to assert the merge (and
   the merge is now documented as an example of real-data noise). Tests were not otherwise weakened.

## Decisions the developer made

The stack (Python, FastAPI, vanilla JS, SQLAlchemy Core) and hosting (Render + Neon, chosen over
Fly.io with a paid volume and over Railway); Claude Sonnet 5.5 as the default model; Crossref as the
only scholarly source; approval of deterministic orchestration instead of an autonomous agent; approval
of the User-Agent-only contact address; the scope of the fabrication pre-check; a human-approval gate for design decisions; deferring the live
Anthropic smoke test until access is available; that no co-author trailers are added to commits; and
all git history, commit-metadata and GitHub privacy handling, which the agent was told not to touch.

## Limits of this record

It is reconstructed from the agent's working sessions. Anything the developer did outside those
sessions (for example reading the code, or testing by hand) is not claimed here.
