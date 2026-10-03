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

function onSubmit(event) {
  event.preventDefault();
  const results = document.getElementById("results");
  showMessage(results, "Search is not available yet.", "info");
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("ask-form").addEventListener("submit", onSubmit);
  checkHealth();
});
