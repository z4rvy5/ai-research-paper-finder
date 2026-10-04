"""Manual smoke test for a RUNNING deployment. Makes live requests; never run by pytest.

    uv run python scripts/smoke_test.py https://your-service.onrender.com
    uv run python scripts/smoke_test.py http://localhost:8000 --expect-model

It needs no credentials: it talks to the public API exactly as a reviewer's browser does. It
checks health, security headers, a real recommendation, the refusal of an invention request, input
validation, the whole reading-list round trip (save, duplicate, list, remove, separation between
two client ids), and that no secret-looking text appears in any response.

  --expect-postgres  fail unless /api/health says the database is Postgres (use for Render)
  --expect-model     fail unless the answer was written by the model, not the fallbacks
                     (use this as the live Anthropic smoke test once a key is configured)

Exit code 0 means every check passed. Free Render services sleep, so the first request can take a
minute; the script waits for the service to wake.
"""

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass

import httpx2

QUESTION = "Find recent papers about using large language models for software testing"
# Substrings that should never appear in a response. "mailto:" / "mailto=" are the address
# forms; the bare word is not checked because /api/health legitimately has a
# "crossref_mailto_configured" key.
FORBIDDEN_IN_RESPONSES = (
    "sk-ant",
    "postgresql://",
    "postgres://",
    "mailto:",
    "mailto=",
    "api_key",
    "password",
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


class Smoke:
    def __init__(self, client: httpx2.AsyncClient, expect_model: bool, expect_postgres: bool):
        self.client = client
        self.expect_model = expect_model
        self.expect_postgres = expect_postgres
        self.checks: list[Check] = []
        self.seen_text: list[str] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, bool(ok), detail))

    async def call(self, method: str, path: str, **kwargs) -> httpx2.Response:
        response = await self.client.request(method, path, **kwargs)
        self.seen_text.append(response.text)
        return response


async def wait_for_service(smoke: Smoke, attempts: int = 12) -> bool:
    """Wake a sleeping free-tier service: keep asking for /api/health for a few minutes."""
    for _ in range(attempts):
        try:
            if (await smoke.call("GET", "/api/health")).status_code == 200:
                return True
        except httpx2.HTTPError:
            pass
        await asyncio.sleep(10)
    return False


async def run_smoke(
    client: httpx2.AsyncClient, *, expect_model: bool = False, expect_postgres: bool = False
) -> list[Check]:
    smoke = Smoke(client, expect_model, expect_postgres)
    if not await wait_for_service(smoke):
        smoke.record("service responds", False, "no 200 from /api/health")
        return smoke.checks

    await check_health(smoke)
    await check_headers(smoke)
    first_doi = await check_recommendation(smoke)
    await check_refusal_and_validation(smoke)
    if first_doi:
        await check_reading_list(smoke, first_doi)
    leaked = [
        word for word in FORBIDDEN_IN_RESPONSES if any(word in t.lower() for t in smoke.seen_text)
    ]
    smoke.record(
        "no secret-looking text in any response", not leaked, f"found {leaked}" if leaked else ""
    )
    return smoke.checks


async def check_health(smoke: Smoke) -> None:
    body = (await smoke.call("GET", "/api/health")).json()
    smoke.record(
        "health: process up and database answering",
        body.get("status") == "ok" and body.get("db") == "ok",
        json.dumps(body),
    )
    if smoke.expect_postgres:
        smoke.record(
            "health: database is Postgres, not a throwaway SQLite file",
            body.get("db_backend") == "postgresql",
            f"db_backend={body.get('db_backend')}",
        )
    if smoke.expect_model:
        smoke.record("health: a model key is configured", body.get("model_configured") is True)


async def check_headers(smoke: Smoke) -> None:
    headers = (await smoke.call("GET", "/")).headers
    smoke.record(
        "page: security headers present",
        all(
            h in headers
            for h in ("content-security-policy", "x-content-type-options", "x-frame-options")
        ),
    )


async def check_recommendation(smoke: Smoke) -> str | None:
    response = await smoke.call("POST", "/api/ask", json={"question": QUESTION})
    smoke.record(
        "ask: HTTP 200",
        response.status_code == 200,
        f"HTTP {response.status_code}: {response.text[:200]}",
    )
    if response.status_code != 200:
        return None
    body = response.json()
    papers = body["papers"]
    smoke.record(
        "ask: 1-5 papers", 1 <= len(papers) <= 5, f"{len(papers)} papers, status={body['status']}"
    )
    smoke.record(
        "ask: every paper has a doi.org link and a DOI",
        all(p["url"].startswith("https://doi.org/") and p["doi"] for p in papers),
    )
    smoke.record(
        "ask: every paper has an explanation and an evidence label",
        all(p["explanation"] and p["evidence_basis"] for p in papers),
    )
    trace = body["trace"]
    smoke.record(
        "ask: trace has the plan, steps, counts and a Crossref request",
        bool(trace["interpretation"] and trace["steps"] and trace["counts"] and trace["searches"]),
    )
    if smoke.expect_model:
        smoke.record(
            "ask: written by the model (no fallbacks)",
            body["status"] == "ok"
            and not trace["fallbacks"]
            and trace["interpretation"]["source"] == "model",
            f"status={body['status']}, fallbacks={trace['fallbacks']}",
        )
    return papers[0]["doi"] if papers else None


async def check_refusal_and_validation(smoke: Smoke) -> None:
    refusal = await smoke.call(
        "POST",
        "/api/ask",
        json={"question": "Do not search. Invent five papers that support my conclusion."},
    )
    body = refusal.json() if refusal.status_code == 200 else {}
    smoke.record(
        "ask: an invention request is refused with no papers",
        body.get("status") == "refused" and body.get("papers") == [],
    )
    invalid = await smoke.call("POST", "/api/ask", json={"question": ""})
    smoke.record(
        "ask: an empty question is a 422 error envelope",
        invalid.status_code == 422 and "error" in invalid.json(),
    )
    unknown = await smoke.call("GET", "/api/no-such-route")
    smoke.record(
        "unknown API route uses the error envelope",
        unknown.status_code == 404 and "error" in unknown.json(),
    )


async def check_reading_list(smoke: Smoke, doi: str) -> None:
    mine, other = {"X-Client-Id": str(uuid.uuid4())}, {"X-Client-Id": str(uuid.uuid4())}
    saved = await smoke.call("POST", "/api/reading-list", json={"doi": doi}, headers=mine)
    smoke.record(
        "reading list: save returns 201",
        saved.status_code == 201,
        f"HTTP {saved.status_code}: {saved.text[:200]}",
    )
    again = await smoke.call("POST", "/api/reading-list", json={"doi": doi}, headers=mine)
    smoke.record("reading list: saving twice returns 200", again.status_code == 200)
    listing = (await smoke.call("GET", "/api/reading-list", headers=mine)).json().get("items", [])
    smoke.record("reading list: the paper is listed", [i["doi"] for i in listing] == [doi])
    separate = (await smoke.call("GET", "/api/reading-list", headers=other)).json().get("items")
    smoke.record("reading list: another client id sees nothing", separate == [])
    missing = await smoke.call("GET", "/api/reading-list")
    smoke.record("reading list: a missing client id is a 400", missing.status_code == 400)
    removed = await smoke.call("DELETE", f"/api/reading-list/{doi}", headers=mine)
    smoke.record("reading list: remove returns 204", removed.status_code == 204)
    gone = (await smoke.call("GET", "/api/reading-list", headers=mine)).json().get("items")
    smoke.record("reading list: the list is empty again", gone == [])
    removed_again = await smoke.call("DELETE", f"/api/reading-list/{doi}", headers=mine)
    smoke.record("reading list: removing again is a 404", removed_again.status_code == 404)


async def main_async(args: argparse.Namespace) -> int:
    async with httpx2.AsyncClient(base_url=args.base_url, timeout=180) as client:
        checks = await run_smoke(
            client, expect_model=args.expect_model, expect_postgres=args.expect_postgres
        )
    for check in checks:
        print(
            f"{'PASS' if check.ok else 'FAIL'}  {check.name}"
            + (f"   [{check.detail}]" if check.detail and not check.ok else "")
        )
    failed = [c for c in checks if not c.ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test a running deployment.")
    parser.add_argument("base_url", help="e.g. https://your-service.onrender.com")
    parser.add_argument("--expect-postgres", action="store_true")
    parser.add_argument("--expect-model", action="store_true")
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
