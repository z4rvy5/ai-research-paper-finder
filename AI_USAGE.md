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

## Later phases: audit, hardening and documentation review

After the milestones, deployment configuration and submission documents were in place, the same agent was used for
review passes that were deliberately kept separate from implementation. Each pass was read-only until the developer
decided what to change.

1. **Final technical audit (read-only).** The agent was asked to audit, not fix: re-read the code, run the suite,
   and report findings with severities and evidence. It found no critical issue. Findings included: the reading-list
   save endpoint was neither rate limited nor capped (medium); a blank `DATABASE_URL` crashed startup; 500 responses
   lacked the security headers; no request-body size cap; and a mutation check showed that removing the DOI-mismatch
   check in the save path was not caught by any test.
2. **The developer decided what to fix.** The developer approved specific findings and explicitly left others
   alone: the save-limit and cap, blank-`DATABASE_URL`, 500-header and DOI-mismatch findings were approved; the
   missing body-size cap was not approved and stayed a documented limitation; deployment and the real
   `X-Forwarded-For` behaviour were assigned to the owner as post-deployment actions. The agent then made only the
   approved changes: a separate rate-limit bucket for saves, a per-client cap of 200 saved papers, a blank
   `DATABASE_URL` falling back to the default, the security headers on 500 responses, and a regression test for a
   Crossref record whose DOI differs from the one requested. Each new protection was mutation-checked (disabled
   in-process and confirmed to make a test fail) before the change was committed by the developer.
3. **Production verification.** The owner deployed the service and ran the health check, the smoke test with
   `--expect-model --expect-postgres`, and a manual browser pass. The agent only recorded those results in the
   documents, labelled as owner-reported manual verification rather than automated tests. At the earlier stage the
   live Anthropic smoke test was deferred; it was subsequently completed during this production verification.
4. **Independent compliance audit against `assignment.pdf` (read-only).** The agent was told not to trust earlier
   reports or the documentation, and to treat `assignment.pdf` as the source of truth. It re-ran the suite and the
   network-blocked run, scanned the git history for secrets, applied in-process mutations, probed the grounding
   checks with hand-written explanations, and compared the documents with the code. It found no failed requirement.
   It did find: explanation prose could pass the lexical checks while naming another paper by title, an invented
   author name, or an unsupported non-numeric claim about an abstract; stale statements in `CLAUDE.md`,
   `VERIFICATION.md` ("Open items: None"), `docs/LEARNING_GUIDE.md` (an abstract count that did not match the
   fixture) and the README (the daily model-call limit given as 500 although production uses 300); and that the
   repository's visibility to reviewers could not be confirmed.
5. **Targeted documentation hardening, then a documentation consistency review.** For the prose finding the
   developer chose to document the limitation honestly (structural grounding of bibliographic fields and paper
   selection, lexical and structural checks on prose, no claim of zero hallucination risk) instead of changing the
   grounding behaviour or leaving a stronger claim in place. The stale statements were corrected, and unverified
   production behaviour (restart persistence, `X-Forwarded-For`, rate-limit and load behaviour) was recorded as not
   independently verified. A further read-only consistency review of all documents then found remaining stale
   items, which were fixed in a last documentation-only pass.

**How AI results were checked rather than accepted.** Audit findings were evidence-backed and classified (requirement
gap, defect, documentation mismatch, limitation) and were not applied automatically: the developer reviewed them and
approved or declined each. Fixes were limited to what was approved, required tests that fail when the protection is
removed, and were re-verified with the full suite, lint, format check and the network-blocked run. Where an audit
showed a claim was stronger than the implementation (the "Open items: None" line, the explanation-prose guarantee),
the documentation was corrected instead of the claim being defended.

## Decisions the developer made

The stack (Python, FastAPI, vanilla JS, SQLAlchemy Core) and hosting (Render + Neon, chosen over
Fly.io with a paid volume and over Railway); Claude Sonnet 5.5 as the default model; Crossref as the
only scholarly source; approval of deterministic orchestration instead of an autonomous agent; approval
of the User-Agent-only contact address; the scope of the fabrication pre-check; a human-approval gate for design decisions; deferring the live
Anthropic smoke test until access was available (at that stage it was deferred; it was subsequently completed
during the owner's production verification, see `VERIFICATION.md` section 4); that no co-author trailers are added
to commits; which audit findings to fix, which to leave as documented limitations (such as the request-body size
cap) and which to hand to the owner after deployment; the choice to document the explanation-prose limitation
instead of claiming complete hallucination prevention; and all git history, commit-metadata and GitHub privacy
handling, which the agent was told not to touch.

## Limits of this record

It is reconstructed from the agent's working sessions. Anything the developer did outside those
sessions (for example reading the code, or testing by hand) is not claimed here.
