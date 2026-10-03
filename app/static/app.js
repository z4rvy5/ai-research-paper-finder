"use strict";

// All server data is rendered with textContent / createElement, never innerHTML.

function showMessage(container, text, kind) {
  container.replaceChildren();
  const p = document.createElement("p");
  p.className = `message ${kind}`;
  p.textContent = text;
  container.appendChild(p);
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

const DOI_PREFIX = "https://doi.org/";

// Minimal result list (title link + DOI). Milestone 4 replaces this with full result cards.
function renderResults(container, body) {
  container.replaceChildren();
  const summary = document.createElement("p");
  summary.className = "summary";
  const { total_results: total, returned } = body.search;
  summary.textContent =
    body.status === "no_results"
      ? "Crossref returned no records for this question."
      : `Crossref matched ${total.toLocaleString()} records; showing its top ${returned} by relevance.`;
  container.appendChild(summary);

  const list = document.createElement("ol");
  for (const paper of body.papers) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.textContent = paper.title ?? "(No title in Crossref record)";
    // Links are built server-side from the DOI; still refuse anything else.
    if (paper.url.startsWith(DOI_PREFIX)) {
      link.href = paper.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    const doi = document.createElement("span");
    doi.className = "doi";
    doi.textContent = ` doi:${paper.doi}`;
    item.append(link, doi);
    list.appendChild(item);
  }
  container.appendChild(list);
}

async function onSubmit(event) {
  event.preventDefault();
  const results = document.getElementById("results");
  const button = event.target.querySelector("button");
  const question = document.getElementById("question").value;

  button.disabled = true;
  showMessage(results, "Searching Crossref…", "info");
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const body = await res.json();
    if (!res.ok) {
      showMessage(results, body.error?.message ?? `Request failed (HTTP ${res.status}).`, "error");
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
