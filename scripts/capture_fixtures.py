"""Capture real Crossref responses as test fixtures. Makes LIVE requests; never run by pytest.

Usage:
    uv run python scripts/capture_fixtures.py

CROSSREF_MAILTO is optional. If it is set in the environment, that address is sent to Crossref
in the User-Agent header only (polite pool); it is never put in the URL. If it is unset, requests
go to the public pool and no email address is sent. The address is never written into a fixture.

Each fixture keeps Crossref's response envelope with `items` trimmed to a few records, plus
the rate-limit headers that came back, so tests run on real data shapes.
"""

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crossref.client import BASE_URL, build_search_params, user_agent  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "crossref"
RATE_LIMIT_HEADERS = (
    "x-api-pool",
    "x-rate-limit-limit",
    "x-rate-limit-interval",
    "x-concurrency-limit",
)

# name -> (query, items to keep)
SEARCHES = {
    "works_llm_software_testing": ("Find recent papers about using LLMs for software testing", 5),
}


def resolve_mailto(environ: Mapping[str, str]) -> str | None:
    """The contact address comes only from an explicitly set CROSSREF_MAILTO; never inferred."""
    value = environ.get("CROSSREF_MAILTO", "").strip()
    return value or None


def capture(
    http: httpx2.Client,
    searches: Mapping[str, tuple[str, int]] = SEARCHES,
    fixture_dir: Path = FIXTURE_DIR,
) -> list[tuple[Path, int]]:
    """Run each search and write its fixture; returns (path, item count) per fixture."""
    fixture_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, (query, keep) in searches.items():
        response = http.get("/works", params=build_search_params(query, keep))
        response.raise_for_status()
        body = response.json()
        body["message"]["items"] = body["message"]["items"][:keep]
        fixture = {
            "query": query,
            "headers": {
                h: response.headers[h] for h in RATE_LIMIT_HEADERS if h in response.headers
            },
            "body": body,
        }
        path = fixture_dir / f"{name}.json"
        path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append((path, len(body["message"]["items"])))
    return written


def main() -> None:
    mailto = resolve_mailto(os.environ)
    if mailto:
        print("CROSSREF_MAILTO is set: using the Crossref polite pool.")
    else:
        print("CROSSREF_MAILTO is not set: using the Crossref public pool (no email sent).")
    with httpx2.Client(
        base_url=BASE_URL, headers={"User-Agent": user_agent(mailto)}, timeout=15
    ) as http:
        for path, count in capture(http):
            print(f"wrote {path.name} ({count} items)")


if __name__ == "__main__":
    main()
