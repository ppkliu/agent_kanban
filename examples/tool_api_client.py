"""End-to-end Tool API client — fire-and-check round trip.

This script is the canonical "hello world" for using Symphony as a Coding
Service from external code. It demonstrates every endpoint of the Tool API
(``/api/v1/tools/*``) using only the Python standard library; no third-party
HTTP client, no LLM credentials, no Docker required.

Usage
-----
1. Start Symphony in another terminal (either path works):

   * Docker:  ``docker compose up -d`` (binds to host :17957)
   * Native:  ``python -m symphony_mvp.dashboard examples/WORKFLOW.docker.md \
              --port 7957``

2. Run this client::

       python examples/tool_api_client.py

It will:

   * Discover the available repo via ``list_repos``
   * Inspect it via ``inspect_repo`` (shows AGENTS.md, languages, tree)
   * Submit a coding task via ``submit_coding_task`` (returns a task_id < 1s)
   * Poll ``check_task_status`` every ~1s, printing the live ``stage``
   * Fetch the structured ``TaskResult`` via ``get_task_result`` once done
   * Demonstrate ``cancel_task`` against an already-completed task
     (idempotent — returns ``was_running: false``)

The script targets the EchoRunner by default (no real LLM needed) so the
round-trip completes in under a second on any machine. Switch to a real
runner by editing ``WORKFLOW.md`` — the client code does not change.

Environment variables
---------------------
``SYMPHONY_URL``       Base URL (default ``http://localhost:17957``).
                       Set this to ``http://localhost:7957`` for native runs.
``DASHBOARD_API_KEY``  Bearer token if the server is started with auth.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


SYMPHONY_URL = os.environ.get("SYMPHONY_URL", "http://localhost:17957").rstrip("/")
API_KEY = os.environ.get("DASHBOARD_API_KEY", "")


# --------------------------------------------------------------------------- #
# Tiny tool-API wrapper                                                       #
# --------------------------------------------------------------------------- #


def _post(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST a JSON body and return the parsed JSON response."""
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        f"{SYMPHONY_URL}{path}",
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    if API_KEY:
        req.add_header("Authorization", f"Bearer {API_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Non-2xx responses still carry a JSON body with a `detail` field.
        body_bytes = e.read()
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {"detail": body_bytes.decode("utf-8", errors="replace")}
        raise RuntimeError(
            f"HTTP {e.code} on {path}: {payload.get('detail', payload)}"
        ) from e


def list_repos() -> list[dict[str, Any]]:
    return _post("/api/v1/tools/list_repos")["repos"]


def inspect_repo(repo_id: str) -> dict[str, Any]:
    return _post("/api/v1/tools/inspect_repo", {"repo": repo_id})


def submit_coding_task(
    task: str, repo_id: str, files_hint: list[str] | None = None
) -> str:
    body: dict[str, Any] = {"task": task, "repo": repo_id}
    if files_hint:
        body["files_hint"] = files_hint
    return _post("/api/v1/tools/submit_coding_task", body)["task_id"]


def check_task_status(task_id: str) -> dict[str, Any]:
    return _post("/api/v1/tools/check_task_status", {"task_id": task_id})


def get_task_result(task_id: str) -> dict[str, Any]:
    return _post("/api/v1/tools/get_task_result", {"task_id": task_id})


def cancel_task(task_id: str, reason: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"task_id": task_id}
    if reason:
        body["reason"] = reason
    return _post("/api/v1/tools/cancel_task", body)


# --------------------------------------------------------------------------- #
# Demo flow                                                                   #
# --------------------------------------------------------------------------- #


def _print_section(title: str) -> None:
    bar = "=" * len(title)
    print(f"\n{bar}\n{title}\n{bar}")


def _wait_for_terminal(
    task_id: str, *, timeout_s: float = 60.0, poll_s: float = 1.0
) -> dict[str, Any]:
    """Poll ``check_task_status`` until status is one of {done, failed,
    cancelled}, printing each observed stage."""
    deadline = time.time() + timeout_s
    last_stage = None
    while time.time() < deadline:
        st = check_task_status(task_id)
        stage = st["stage"]
        if stage != last_stage:
            print(
                f"  status={st['status']!r:11} stage={stage!r:22} "
                f"turn={st['current_turn']}/{st['max_turns']}  "
                f"progress={st['progress']:.0%}"
            )
            last_stage = stage
        if st["status"] in ("done", "failed", "cancelled"):
            return st
        # Server-recommended back-off (Symphony returns this hint per status)
        time.sleep(min(poll_s, st.get("next_poll_after_ms", 2_000) / 1_000))
    raise TimeoutError(f"task {task_id} did not finish within {timeout_s}s")


def main() -> int:
    # 0. Quick health check so we fail loudly on a bad URL / wrong port.
    _print_section(f"Health check ({SYMPHONY_URL})")
    try:
        with urllib.request.urlopen(
            f"{SYMPHONY_URL}/healthz", timeout=5
        ) as r:
            print(f"  GET /healthz → {r.read().decode()}")
    except urllib.error.URLError as e:
        print(
            f"  ERROR: could not reach {SYMPHONY_URL} — is Symphony running?\n"
            f"  Try one of:\n"
            f"    docker compose up -d\n"
            f"    python -m symphony_mvp.dashboard examples/WORKFLOW.docker.md "
            f"--port 7957\n"
            f"  ({e})"
        )
        return 2

    # 1. Discover repos.
    _print_section("1. list_repos")
    repos = list_repos()
    for r in repos:
        print(
            f"  • id={r['id']!r:18} name={r['name']!r:30} "
            f"default_mode={r['default_mode']}"
        )
    if not repos:
        print("  (no repos available — check the server's WORKFLOW.md)")
        return 1
    repo_id = repos[0]["id"]

    # 2. Inspect the repo.
    _print_section(f"2. inspect_repo (repo={repo_id!r})")
    info = inspect_repo(repo_id)
    if info.get("agents_md"):
        ad = info["agents_md"]
        snippet = ad["content"].splitlines()[:3]
        print(f"  AGENTS doc: {ad['filename']!r}")
        for line in snippet:
            print(f"    | {line}")
    else:
        print("  (no AGENTS.md / CONTRIBUTING.md / README.md found)")
    print(f"  languages: {info['languages']}")
    print(f"  file_count: {info['file_count']}")

    # 3. Submit a coding task.
    _print_section("3. submit_coding_task")
    task_text = (
        "Demo: write a one-line note in scratch.txt confirming the Tool API "
        "round-trip works end to end. Keep it under 100 characters."
    )
    task_id = submit_coding_task(task_text, repo_id, files_hint=["scratch.txt"])
    print(f"  task_id = {task_id}")

    # 4. Poll status until terminal.
    _print_section("4. check_task_status (polling)")
    final = _wait_for_terminal(task_id, timeout_s=30.0, poll_s=0.5)

    # 5. Fetch the structured result.
    _print_section("5. get_task_result")
    result = get_task_result(task_id)
    print(f"  status:         {result['status']}")
    print(f"  summary:        {result['summary']}")
    print(f"  files_changed:  {result['files_changed']}")
    print(f"  tests_run:      {result.get('tests_run')}")
    print(f"  cost_usd:       {result['cost_usd']}")
    print(f"  detailed_url:   {result['detailed_url']}")

    # 6. Demonstrate cancel idempotency on the now-completed task.
    _print_section("6. cancel_task (idempotency demo)")
    cancel = cancel_task(task_id, reason="demo idempotency")
    print(
        f"  ok={cancel['ok']}  was_running={cancel['was_running']}  "
        f"(false because the task already completed)"
    )

    # 7. Recap.
    _print_section("Done")
    print(
        "  All 6 endpoints exercised. Open the dashboard at "
        f"{SYMPHONY_URL} to see the kanban."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
