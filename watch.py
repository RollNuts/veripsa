#!/usr/bin/env python3
"""Veripsa watcher — LOCAL auto-capture (no deploy, no cost; runs on your machine).

Reads `git status` to see what THIS agent is editing RIGHT NOW, registers it as claims against the
protected branch, and prints what's heading to main — the live contention. No manual input: just run it
(or loop it / call it from a git hook). Each AI agent runs its own (ideally in its own git worktree, so
its `git status` is its own changes); they all feed the same Veripsa instance and see each other.

  VERIPSA_DSN=postgresql://<agent-writer>@localhost/<db>  python3 watch.py [repo_path]
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "github-app"))
import psycopg2  # noqa: E402
import code_graph_extract as X  # noqa: E402


def git_modified(repo_path):
    """The files THIS working tree is changing now (added/modified/renamed; deletions skipped)."""
    out = subprocess.run(["git", "-C", repo_path, "status", "--porcelain"],
                         capture_output=True, text=True).stdout
    files = []
    for line in out.splitlines():
        st, path = line[:2], line[3:].strip()
        if not st.strip() or st.strip() == "D":
            continue
        if "->" in path:           # rename: old -> new
            path = path.split("->")[-1].strip()
        if path and not path.endswith("/"):
            files.append(path)
    return files


def main(argv):
    repo_path = argv[1] if len(argv) > 1 else "."
    dsn = os.environ.get("VERIPSA_DSN")
    if not dsn:
        print("set VERIPSA_DSN to this agent's writer connection"); return 2
    repo = X._git_repo(repo_path) or "local"
    branch = "main"                # Veripsa protects main
    mod = git_modified(repo_path)

    conn = psycopg2.connect(dsn); conn.autocommit = True
    cur = conn.cursor(); cur.execute("SET search_path=core, pg_catalog")
    cur.execute("SELECT agent FROM core.resolve_session_identity() AS r(agent, account)")
    me_agent = cur.fetchone()[0]
    for p in mod:                  # declare-before-edit; claim_id agent-scoped (no cross-agent clash); idempotent
        cur.execute("SELECT core.declare_claim_with_authority(%s,%s,%s,%s)", (f"{me_agent}:{p}", p, repo, branch))
    cur.execute("SELECT core.main_impact_surface(%s,%s)", (repo, branch))
    imp = cur.fetchone()[0] or {}
    conn.close()

    print(f"● watching {repo}@{branch} — this agent has {len(mod)} file(s) in flight")
    print(f"  heading to main: inflight={imp.get('inflight_count',0)} "
          f"⚠warn={imp.get('warn_count',0)} ⏸serialize={imp.get('serialize_count',0)} "
          f"❓unknown={imp.get('unknown_count',0)} ✓clear={imp.get('clear_count',0)}")
    for c in imp.get("changes", []):
        flag = {"serialize": "⏸", "warn": "⚠", "unknown": "❓", "clear": "✓"}.get(c["verdict"], "•")
        note = ""
        if c.get("serialize_behind"):
            note = " behind " + ", ".join(c["serialize_behind"])
        elif c.get("contested_with"):
            note = " with " + ", ".join(c["contested_with"])
        elif c.get("unknown_paths"):
            note = " (unanalyzed: " + ", ".join(c["unknown_paths"][:3]) + ")"
        print(f"    {flag} {c.get('label', c['agent'])}: {c['verdict']}{note}  [{', '.join(c.get('paths', [])[:4])}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
