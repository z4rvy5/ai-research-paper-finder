"use strict";

// All server data is rendered with textContent / createElement, never innerHTML.

const DOI_PREFIX = "https://doi.org/";
const MAX_AUTHORS_SHOWN = 6;

// ---- anonymous browser id (NOT authentication) ---------------------------------------------
// A random id this browser generates and sends as X-Client-Id so the server can keep one
// browser's reading list apart from another's. Anyone who has the id can use that list, and
// clearing site data loses it. It is not a login and not private.

const CLIENT_ID_KEY = "paperFinderClientId";
let fallbackClientId = null; // used when localStorage is unavailable (private mode, blocked)

function newClientId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function getClientId() {
  try {
    let id = localStorage.getItem(CLIENT_ID_KEY);
    if (!id) {
      id = newClientId();
      localStorage.setItem(CLIENT_ID_KEY, id);
    }
    return id;
  } catch (err) {
    fallbackClientId = fallbackClientId ?? newClientId();
    return fallbackClientId;
  }
}

// fetch() that gives up after `ms` milliseconds, so a stalled server never leaves the page waiting.
async function fetchWithTimeout(url, options, ms) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

const ASK_TIMEOUT_MS = 150000; // the server itself can take up to about a minute (two model calls)
const LIST_TIMEOUT_MS = 30000;
const HEALTH_TIMEOUT_MS = 15000;

// Reading-list requests never throw: they resolve to { ok, status, body }.
async function listApi(path, options = {}) {
  try {
    const res = await fetchWithTimeout(
      `/api/reading-list${path}`,
      { ...options, headers: { "X-Client-Id": getClientId(), "Content-Type": "application/json" } },
      LIST_TIMEOUT_MS,
    );
    let body = null;
    if (res.status !== 204) {
      try {
        body = await res.json();
      } catch (err) {
        body = null;
      }
    }
    return { ok: res.ok, status: res.status, body };
  } catch (err) {
    return { ok: false, status: 0, body: null };
  }
}

function listErrorText(result, action) {
  const error = result.body?.error;
  if (!error) return `Could not ${action}: the server could not be reached. Please try again.`;
  const retry = error.retryable && !/try again/i.test(error.message) ? " Please try again shortly." : "";
  return `Could not ${action}: ${error.message}${retry}`;
}

const savedDois = new Set(); // DOIs in this browser's reading list, as last loaded from the server

function h(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function bulletList(items) {
  const list = h("ul");
  for (const item of items) list.appendChild(h("li", "", item));
  return list;
}

function showMessage(container, text, kind) {
  container.replaceChildren(h("p", `message ${kind}`, text));
}

async function checkHealth() {
  const el = document.getElementById("server-status");
  try {
    const res = await fetchWithTimeout("/api/health", {}, HEALTH_TIMEOUT_MS);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const health = await res.json();
    // The Crossref contact address is optional: public access is a normal, intended mode.
    // A missing model key is still a real limitation (answers use the fallbacks), so it is shown.
    const access = health.crossref_mailto_configured ? "polite" : "public";
    const modelNote = health.model_configured ? "" : " (not configured: model API key)";
    el.textContent = `Server is up \u00b7 Crossref ${access} access${modelNote}`;
    el.dataset.state = health.model_configured ? "ok" : "warn";
  } catch (err) {
    el.textContent = "Server unreachable. It may be waking up; try again in a minute.";
    el.dataset.state = "error";
  }
}

// ---- paper cards ---------------------------------------------------------------------------

function authorsLine(paper) {
  if (!paper.authors.length) {
    return h("span", "missing", "Authors not listed in Crossref record");
  }
  const shown = paper.authors.slice(0, MAX_AUTHORS_SHOWN).join(", ");
  const extra = paper.authors.length - MAX_AUTHORS_SHOWN;
  return h("span", "", extra > 0 ? `${shown} and ${extra} more` : shown);
}

function renderPaper(paper, index) {
  const card = h("li", "paper");

  const title = h("h3");
  const link = h("a", "", paper.title ?? "(No title in Crossref record)");
  // The server builds this link from the DOI; still refuse anything else.
  if (paper.url.startsWith(DOI_PREFIX)) {
    link.href = paper.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  title.append(`${index + 1}. `, link);
  card.appendChild(title);

  const meta = h("p", "meta");
  meta.append(authorsLine(paper), " · ");
  meta.append(
    paper.year ? h("span", "", String(paper.year)) : h("span", "missing", "Year unknown"),
  );
  if (paper.venue) meta.append(" · ", h("span", "", paper.venue));
  card.appendChild(meta);

  const doi = h("p", "doi");
  doi.append("DOI: ");
  const doiLink = h("a", "", paper.doi);
  if (paper.url.startsWith(DOI_PREFIX)) {
    doiLink.href = paper.url;
    doiLink.target = "_blank";
    doiLink.rel = "noopener noreferrer";
  }
  doi.appendChild(doiLink);
  card.appendChild(doi);

  // The evidence disclosure sits next to the explanation as one compact badge. Which text it
  // describes (written by the model, or built from metadata with no AI) is stated truthfully.
  const basis = paper.evidence_basis === "title_and_abstract" ? "title + abstract" : "title only";
  const source =
    paper.explanation_source === "model"
      ? "AI-generated \u00b7 Crossref metadata"
      : "Built from Crossref metadata (no AI text)";
  const badge = h("span", "badge", `${source} \u00b7 ${basis}`);
  badge.dataset.evidence = paper.evidence_basis;
  const label = h("p", "explanation-label", "Why it may be relevant");
  label.append(" ", badge);
  card.append(label, h("p", "explanation", paper.explanation));

  if (paper.abstract) {
    const details = h("details", "abstract");
    details.append(h("summary", "", "Abstract (from Crossref)"), h("p", "", paper.abstract));
    card.appendChild(details);
  } else {
    card.appendChild(h("p", "missing", "No abstract in Crossref record."));
  }
  if (paper.missing_fields.length) {
    card.appendChild(h("p", "missing", `Missing in Crossref: ${paper.missing_fields.join(", ")}`));
  }
  card.appendChild(saveControls(paper.doi));
  return card;
}

// ---- reading list ---------------------------------------------------------------------------

function setSaveButtonState(button) {
  const saved = savedDois.has(button.dataset.doi);
  button.textContent = saved ? "Saved: remove from reading list" : "Save to reading list";
  button.classList.toggle("saved", saved);
}

function refreshSaveButtons() {
  document.querySelectorAll("button.save-button").forEach(setSaveButtonState);
}

function saveControls(doi) {
  const row = h("p", "actions");
  const button = h("button", "save-button");
  button.type = "button";
  button.dataset.doi = doi;
  const note = h("span", "action-note");
  button.addEventListener("click", () => toggleSaved(doi, button, note));
  setSaveButtonState(button);
  row.append(button, note);
  return row;
}

// Saves the paper if it isn't saved, removes it if it is. The server is the source of truth:
// after every change the list is reloaded and every button is refreshed from it.
async function toggleSaved(doi, button, note) {
  const saving = !savedDois.has(doi);
  button.disabled = true;
  note.className = "action-note";
  note.textContent = saving ? "Saving…" : "Removing…";
  const result = saving
    ? await listApi("", { method: "POST", body: JSON.stringify({ doi }) })
    : await listApi(`/${encodeURIComponent(doi)}`, { method: "DELETE" });
  button.disabled = false;
  const alreadyGone = !saving && result.body?.error?.code === "not_saved";
  if (result.ok || alreadyGone) {
    note.textContent = "";
    await loadReadingList();
  } else {
    note.className = "action-note error";
    note.textContent = listErrorText(result, saving ? "save this paper" : "remove this paper");
  }
}

function renderReadingListItem(item) {
  const li = h("li", "saved-paper");
  const title = h("a", "", item.title ?? "(No title in Crossref record)");
  if (item.url.startsWith(DOI_PREFIX)) {
    title.href = item.url;
    title.target = "_blank";
    title.rel = "noopener noreferrer";
  }
  const meta = h("p", "meta");
  meta.append(authorsLine(item), " · ");
  meta.append(item.year ? h("span", "", String(item.year)) : h("span", "missing", "Year unknown"));
  li.append(title, meta, saveControls(item.doi));
  return li;
}

async function loadReadingList() {
  const status = document.getElementById("reading-list-status");
  const list = document.getElementById("reading-list-items");
  status.className = "status";
  status.textContent = "Loading your reading list…";
  const result = await listApi("");
  if (!result.ok) {
    status.className = "message error";
    const retry = h("button", "retry-button", "Try again");
    retry.type = "button";
    retry.addEventListener("click", loadReadingList);
    status.replaceChildren(listErrorText(result, "load your reading list"), " ", retry);
    return;
  }
  const items = result.body.items;
  savedDois.clear();
  items.forEach((item) => savedDois.add(item.doi));
  list.replaceChildren(...items.map(renderReadingListItem));
  status.textContent = items.length
    ? `${items.length} saved paper${items.length === 1 ? "" : "s"}.`
    : "No saved papers yet. Use \u201cSave to reading list\u201d on a recommendation.";
  refreshSaveButtons();
}

// ---- trace ---------------------------------------------------------------------------------

function traceSection(title, ...nodes) {
  const section = h("section", "trace-section");
  section.appendChild(h("h4", "", title));
  section.append(...nodes);
  return section;
}

function describePlan(plan) {
  const parts = [`search terms: "${plan.topic_query}"`];
  if (plan.from_year || plan.until_year) {
    parts.push(`years: ${plan.from_year ?? "any"} to ${plan.until_year ?? "any"}`);
  }
  if (plan.work_types.length) parts.push(`types: ${plan.work_types.join(", ")}`);
  if (plan.alt_queries.length) parts.push(`alternative searches: ${plan.alt_queries.join("; ")}`);
  return parts;
}

function renderTrace(trace, papers = []) {
  const details = h("details", "trace");
  details.appendChild(h("summary", "", "Agent trace: how this answer was produced"));

  const interp = trace.interpretation;
  const interpNodes = [
    h(
      "p",
      "",
      {
        model: "Your question was interpreted by the model.",
        fallback: "The model was unavailable, so keywords from your question were used instead.",
        precheck:
          "A deterministic rule recognised this as a request to invent papers, before any model or search call.",
      }[interp.source],
    ),
    bulletList([`intent: ${interp.plan.intent}`, ...describePlan(interp.plan)]),
  ];
  if (interp.assumptions.length) {
    interpNodes.push(h("p", "", "Assumptions made by the application:"), bulletList(interp.assumptions));
  }
  if (interp.adjustments.length) {
    interpNodes.push(h("p", "", "Corrections to the model's plan:"), bulletList(interp.adjustments));
  }
  details.appendChild(traceSection("Interpreted request", ...interpNodes));

  if (trace.failure) {
    const f = trace.failure;
    const filters = [
      f.filters.from_year || f.filters.until_year
        ? `years ${f.filters.from_year ?? "any"}-${f.filters.until_year ?? "any"}`
        : "",
      f.filters.types?.length ? `types ${f.filters.types.join(", ")}` : "",
    ].filter(Boolean);
    details.appendChild(
      traceSection(
        "Failure",
        bulletList([
          `${f.stage} failed: ${f.code}${f.http_status ? ` (HTTP ${f.http_status})` : " (no response)"}`,
          `${f.message}${f.retryable && !/try again|wait/i.test(f.message) ? " This may work if you try again shortly." : ""}`,
          `search terms: "${f.query}"${filters.length ? ` [${filters.join("; ")}]` : ""}`,
          ...(f.retries ? [`Crossref was retried ${f.retries} time${f.retries === 1 ? "" : "s"} before giving up.`] : []),
          ...(f.rate_limit?.pool
            ? [`Crossref reported pool ${f.rate_limit.pool}${f.rate_limit.limit ? `, limit ${f.rate_limit.limit} per ${f.rate_limit.interval ?? "interval"}` : ""}.`]
            : []),
        ]),
      ),
    );
    details.open = true;
  }

  details.appendChild(
    traceSection(
      "Steps",
      bulletList(trace.steps.map((s) => `${s.name} — ${s.status} (${s.duration_ms} ms): ${s.summary}`)),
    ),
  );

  const c = trace.counts;
  details.appendChild(
    traceSection(
      "Counts",
      bulletList([
        `Crossref returned: ${c.returned}`,
        `Remaining after filtering and de-duplication: ${c.surviving}`,
        `Presented: ${c.selected}`,
      ]),
    ),
  );

  if (trace.searches.length) {
    details.appendChild(
      traceSection(
        "Crossref requests",
        bulletList(
          trace.searches.map((s) => {
            const f = s.filters;
            const filters = [
              f.from_year || f.until_year ? `years ${f.from_year ?? "any"}-${f.until_year ?? "any"}` : "",
              f.types.length ? `types ${f.types.join(", ")}` : "",
            ].filter(Boolean);
            const pool = s.rate_limit.pool ? `, pool ${s.rate_limit.pool}` : "";
            const retried = s.retries ? `, after ${s.retries} retry` : "";
            return (
              `${s.purpose}: "${s.query}" ${filters.length ? `[${filters.join("; ")}] ` : ""}` +
              `rows=${s.rows} → HTTP ${s.http_status}, ${s.returned} of ${s.total_results.toLocaleString()} matches${pool}${retried}`
            );
          }),
        ),
      ),
    );
  }

  const fl = trace.filtering;
  const filteringNodes = [h("p", "", fl.ordering_rule || "No ordering applied.")];
  if (fl.removed.length) {
    filteringNodes.push(
      h("p", "", "Removed:"),
      bulletList(fl.removed.map((r) => `${r.doi ?? "(no DOI)"} — ${r.reason}${r.title ? ` — ${r.title}` : ""}`)),
    );
  }
  if (fl.duplicates_merged.length) {
    filteringNodes.push(
      h("p", "", "Duplicates merged:"),
      bulletList(fl.duplicates_merged.map((m) => `kept ${m.kept}, dropped ${m.dropped} (${m.reason})`)),
    );
  }
  details.appendChild(traceSection("Filtering and ordering", ...filteringNodes));

  if (fl.shortlisted.length) {
    const titles = new Map(papers.map((paper) => [paper.doi, paper.title]));
    details.appendChild(
      traceSection(
        "Selected papers",
        bulletList(
          fl.shortlisted.map(
            (entry) =>
              `${entry.ref}: ${titles.get(entry.doi) ?? "(no title in Crossref record)"} (${entry.doi}) — ` +
              (entry.term_match
                ? "matches a query term"
                : "no query-term match (kept: a relevant paper can use different wording)"),
          ),
        ),
      ),
    );
  }

  const g = trace.grounding;
  const groundingNodes = [];
  if (g.rejected_refs.length) {
    groundingNodes.push(h("p", "", "Model items ignored:"), bulletList(g.rejected_refs));
  }
  if (g.explanation_rewrites.length) {
    groundingNodes.push(
      h("p", "", "Explanations replaced by metadata-only text:"),
      bulletList(g.explanation_rewrites.map((r) => `${r.doi} — ${r.reason}`)),
    );
  }
  if (!groundingNodes.length) groundingNodes.push(h("p", "", "No model text needed to be changed."));
  details.appendChild(traceSection("Grounding checks", ...groundingNodes));

  details.appendChild(
    traceSection(
      "Model",
      h("p", "", `Model: ${trace.model ?? "none"}`),
      trace.fallbacks.length
        ? bulletList(trace.fallbacks.map((f) => `fallback used — ${f}`))
        : h("p", "", "No fallbacks were needed."),
    ),
  );

  const raw = h("details", "raw-trace");
  raw.append(h("summary", "", "Raw trace (JSON)"), h("pre", "", JSON.stringify(trace, null, 2)));
  details.appendChild(raw);
  return details;
}

// ---- results -------------------------------------------------------------------------------

function renderResults(container, body) {
  container.replaceChildren();

  container.appendChild(h("p", `summary status-${body.status}`, body.summary));
  if (body.status === "degraded") {
    container.appendChild(
      h("p", "message warn", "Some steps used deterministic fallbacks instead of the model. See the trace."),
    );
  }

  if (body.papers.length) {
    const list = h("ol", "papers");
    body.papers.forEach((paper, index) => list.appendChild(renderPaper(paper, index)));
    container.appendChild(list);
  }
  if (body.limitations.length) {
    container.appendChild(
      traceSection("Limitations and uncertainty", bulletList(body.limitations)),
    );
  }
  container.appendChild(renderTrace(body.trace, body.papers));
}

async function onSubmit(event) {
  event.preventDefault();
  const results = document.getElementById("results");
  const button = event.target.querySelector("button");
  const question = document.getElementById("question").value;

  button.disabled = true;
  showMessage(results, "Interpreting your question and searching Crossref… this can take a little while.", "info");
  try {
    const res = await fetchWithTimeout(
      "/api/ask",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) },
      ASK_TIMEOUT_MS,
    );
    const body = await res.json();
    if (!res.ok) {
      const error = body.error;
      const hint =
        error?.retryable && !/try again|wait/i.test(error.message) ? " You can try again shortly." : "";
      showMessage(results, `${error?.message ?? `Request failed (HTTP ${res.status}).`}${hint}`, "error");
      if (body.trace) {
        // An upstream failure after the request was understood: show what was attempted.
        if (body.trace.limitations.length) {
          results.appendChild(traceSection("Limitations and uncertainty", bulletList(body.trace.limitations)));
        }
        results.appendChild(renderTrace(body.trace));
      }
      return;
    }
    renderResults(results, body);
  } catch (err) {
    showMessage(
      results,
      err.name === "AbortError"
        ? "The request took too long, so the browser stopped waiting. The server may be waking up; please try again."
        : "Could not reach the server. Please try again.",
      "error",
    );
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("ask-form").addEventListener("submit", onSubmit);
  checkHealth();
  loadReadingList();
});
