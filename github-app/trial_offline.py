#!/usr/bin/env python3
"""Veripsa GitHub App — OFFLINE TRIAL RUN (does it actually work? — PO: 試走して実際に使えるか確認).

Drives the real gate (db/schema.sql) through a realistic pull-request lifecycle on a fresh DB, using the
ACTUAL App brain (webhook.handle_pull_request) and the ACTUAL renderer (render.render_pr_check). No deploy,
no GitHub — the only thing simulated is the webhook payload; every prediction / lock / landing is the real
function, and every comment is exactly what the App would post on the PR.

Run:  python3 github-app/trial_offline.py
(assumes local Postgres with the veripsa roles; it re-bootstraps a scratch DB each run.)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psycopg2  # noqa: E402
from webhook import handle_pull_request  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): this trial re-bootstraps + drops its own scratch DB, so a FIXED name lets
# concurrent runs (several agents each running run_gates) drop/recreate each other's DB mid-run →
# "createdb: duplicate key" / "does not exist". Per-PID, like db/smoke.sh (veripsa_smoke_$$), run_gates
# (veripsa_gates_$$), test_server.py.
DB = "veripsa_apptrial_" + str(os.getpid())
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dsn(role: str) -> str:
    return f"postgresql://{role}@localhost/{DB}"


def make_db(role: str):
    def run(sql, args=()):
        conn = psycopg2.connect(dsn(role))
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    return run


def banner(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def show(result):
    if result.get("noop"):
        print(f"(noop: {result['action']})"); return
    if result.get("action") == "merged":
        landed = result["landed"]
        print(f"🟢 MERGED PR#{result['pr']} → 着地 {landed.get('commit_sha')}")
        print(f"   released lanes : {landed.get('released')}")
        print(f"   promoted (queued PRs that advance): {landed.get('promoted')}")
        print(f"   → comments to refresh on the still-in-flight PRs:")
        for r in result["refreshed"]:
            print(f"       • {r['agent']}: [{r['conclusion']}] {r['summary']}")
        return
    chk = result["check"]
    print(f"PR#{result['pr']} ({result['action']}) → CHECK [{chk['conclusion']}] {chk['summary']}")
    print("-" * 78)
    # LESS NOISE: a 'clear' PR posts the check only, no comment — the App skips commenting on a clean PR.
    print(result["comment"] if result.get("comment") is not None else "(check only — clear, no comment posted)")


def main() -> int:
    banner("0. bootstrap a fresh DB (the real schema.sql gate) + a realistic main graph")
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    print(f"   bootstrapped {DB}: ACCT-DEMO + seats frontend(AG-A)/api(AG-B)")

    repo, branch = "acme/app", "main"
    frontend, api, app = make_db("veripsa_demo_agent"), make_db("veripsa_demo_agent2"), make_db("veripsa_app")

    # main's graph: api.handle & session.open_session CALL auth.login (auth is the hotspot everything needs)
    graph = {
        "nodes": [
            {"id": "src/auth.py", "kind": "file", "path": "src/auth.py", "language": "python"},
            {"id": "d_login", "kind": "def", "path": "src/auth.py", "name": "login"},
            {"id": "src/api.py", "kind": "file", "path": "src/api.py", "language": "python"},
            {"id": "d_handle", "kind": "def", "path": "src/api.py", "name": "handle"},
            {"id": "src/session.py", "kind": "file", "path": "src/session.py", "language": "python"},
            {"id": "d_open", "kind": "def", "path": "src/session.py", "name": "open_session"},
        ],
        "edges": [
            {"src": "src/api.py", "dst": "login", "kind": "calls"},
            {"src": "src/session.py", "dst": "login", "kind": "calls"},
        ],
    }
    frontend("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
             (json.dumps(graph), repo, branch, "abc123"))
    print("   ingested main's code graph (auth.login ← api.handle, session.open_session)")

    banner("1. PR#1 opens — frontend changes src/auth.py (nothing else in flight yet)")
    show(handle_pull_request(frontend, {
        "action": "opened", "repo": repo, "base_branch": branch, "pr_number": 1,
        "changed_files": ["src/auth.py"]}, "frontend"))

    banner("2. PR#2 opens — api changes src/api.py (api.handle CALLS auth.login → semantic A→B)")
    show(handle_pull_request(api, {
        "action": "opened", "repo": repo, "base_branch": branch, "pr_number": 2,
        "changed_files": ["src/api.py"]}, "api"))
    print("\n   ↳ PR#1's comment now refreshes too (its neighborhood changed):")
    show(handle_pull_request(frontend, {
        "action": "synchronize", "repo": repo, "base_branch": branch, "pr_number": 1,
        "changed_files": ["src/auth.py"]}, "frontend"))

    banner("3. PR#3 opens — api ALSO opens a PR on src/auth.py (DIRECT collision with PR#1 → wait in line)")
    show(handle_pull_request(api, {
        "action": "opened", "repo": repo, "base_branch": branch, "pr_number": 3,
        "changed_files": ["src/auth.py"]}, "api"))

    banner("4. PR#1 MERGES — frontend lands auth.py → its lanes free, the queued PR#3 is PROMOTED")
    show(handle_pull_request(app, {
        "action": "closed", "merged": True, "repo": repo, "base_branch": branch,
        "pr_number": 1, "head_sha": "deadbeef01", "model": "claude-opus-4-8"}, "frontend"))

    banner("DONE — every prediction/lock/landing above was the real gate; every comment is what the App posts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
