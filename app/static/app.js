"use strict";

// All server data is rendered with textContent / createElement, never innerHTML.

const DOI_PREFIX = "https://doi.org/";
const MAX_AUTHORS_SHOWN = 6;

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
    const res = await fetch("/api/health");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const health = await res.json();
    const missing = [];
    if (!health.model_configured) missing.push("model API key");
    if (!health.crossref_mailto_configured) missing.push("Crossref contact email");
    el.textContent = missing.length
      ? `Server is up (not configured: ${missing.join(", ")}).`
      : "Server is up.";
    el.dataset.state = missing.length ? "warn" : "ok";
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

  const basis =
    paper.evidence_basis === "title_and_abstract" ? "Based on title + abstract" : "Based on title only";
  card.appendChild(h("p", "badge", basis));

  const generated = paper.explanation_source === "model";
  card.appendChild(
    h(
      "p",
      "explanation-label",
      generated
        ? "Why it may be relevant (AI-written, from Crossref metadata only):"
        : "Why it may be relevant (generated from metadata; no AI text was used):",
    ),
  );
  card.appendChild(h("p", "explanation", paper.explanation));

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
  return card;
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

function renderTrace(trace) {
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
          `${f.message}${f.retryable ? " This may work if you try again shortly." : ""}`,
          `search terms: "${f.query}"${filters.length ? ` [${filters.join("; ")}]` : ""}`,
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
            return (
              `${s.purpose}: "${s.query}" ${filters.length ? `[${filters.join("; ")}] ` : ""}` +
              `rows=${s.rows} → HTTP ${s.http_status}, ${s.returned} of ${s.total_results.toLocaleString()} matches${pool}`
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
  container.appendChild(renderTrace(body.trace));
}

async function onSubmit(event) {
  event.preventDefault();
  const results = document.getElementById("results");
  const button = event.target.querySelector("button");
  const question = document.getElementById("question").value;

  button.disabled = true;
  showMessage(results, "Interpreting your question and searching Crossref… this can take a little while.", "info");
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const body = await res.json();
    if (!res.ok) {
      const error = body.error;
      const hint =
        error?.retryable && !/try again/i.test(error.message) ? " You can try again shortly." : "";
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
    showMessage(results, "Could not reach the server. Please try again.", "error");
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("ask-form").addEventListener("submit", onSubmit);
  checkHealth();
});
