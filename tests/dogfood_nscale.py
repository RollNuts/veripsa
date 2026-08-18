#!/usr/bin/env python3
"""N-AGENT contention dogfood — the real N-scale test (PO: 2体だと意味ない, そういう試験もいる).

Two agents only ever prove ONE pair. This drives the App brain at N: it auto-discovers every local
`inflight/*` branch (each = one AI agent's open PR against main), maps each to its REAL changed-file set
(git diff against main), ingests THIS repo's main code graph, opens all N as in-flight changes through the
ACTUAL App brain (webhook.handle_pull_request) + the REAL gate (db/schema.sql), and prints what the App
would report: per-change verdict (serialize/warn/clear/unknown), each change's blast radius, and — the
N-scale unit — the contention CLUSTERS (connected components) with suggested land order + hotspots. Then it
LANDS one change and shows the lane free + waiters promoted (着地).

Nothing is simulated except the webhook payload; every prediction/lock/cluster/landing is the real function.

Run:  python3 tests/dogfood_nscale.py            (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
from webhook import handle_pull_request  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): run_gates invokes this and it bootstraps + drops its own DB, so a FIXED name
# lets concurrent runs drop each other's DB mid-run. Per-PID, like db/smoke.sh + run_gates (veripsa_gates_$$).
DB = "veripsa_nscale_" + str(os.getpid())
REPO, BRANCH = "example-org/example-repo", "main"


def git(*args) -> str:
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True).stdout.strip()


def make_db(role: str):
    def run(sql, args=()):
        conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    return run


def discover_inflight():
    """Every local inflight/* branch → (label, [changed paths vs main]). One per real agent's PR."""
    branches = [b.strip() for b in git("for-each-ref", "--format=%(refname:short)", "refs/heads/inflight").splitlines() if b.strip()]
    out = []
    for b in sorted(branches):
        files = [f for f in git("diff", "--name-only", f"main..{b}").splitlines() if f.strip()]
        if files:
            out.append((b.split("/", 1)[1], files))
    return out


def main() -> int:
    # MODES: (default) auto-discover this repo's inflight/* agent branches; or --repo PATH --spec FILE to
    # run against ANY codebase with an explicit set of in-flight PRs ([{"label":..,"files":[..]}, ...]).
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=ROOT, help="codebase to ingest as 'main' (default: this repo)")
    ap.add_argument("--spec", default=None, help="JSON file of in-flight PRs: [{label, files}, ...]")
    ap.add_argument("--assert-whole-graph", action="store_true",
                    help="gate mode: fail unless ALL graph dimensions fired (calls·imports·schema·config·direct·unknown)")
    ap.add_argument("--show-comments", action="store_true",
                    help="print the ACTUAL GitHub PR check + comment each flagged PR's author would see (render.py)")
    args = ap.parse_args()
    ingest_root = os.path.abspath(args.repo)

    if args.spec:
        with open(args.spec) as fh:
            spec = json.load(fh)
        inflight = [(p["label"], p["files"]) for p in spec]
    else:
        inflight = discover_inflight()
    if not inflight:
        print("no in-flight PRs (pass --spec, or create inflight/* branches)."); return 1
    print(f"in-flight PRs ({len(inflight)}), each = one agent's open PR against main:")
    for i, (name, files) in enumerate(inflight, 1):
        print(f"   PR-{i}  {name:14s}  touches: {', '.join(files)}")

    print(f"\n— bootstrap fresh DB + ingest main code graph of: {ingest_root} —")
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    sys.path.insert(0, ROOT)
    import code_graph_extract as X
    graph = X.build_graph(ingest_root)
    files_n = sum(1 for n in graph["nodes"] if n.get("kind") == "file")
    import collections as _c
    ek = _c.Counter(e["kind"] for e in graph["edges"])
    db = make_db("veripsa_app")        # the hosted-App identity → claims are reserved per PR AUTHOR (delegation)
    head_sha = (git("rev-parse", "HEAD") or "0" * 40) if ingest_root == ROOT else "0" * 40
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, BRANCH, head_sha))
    print(f"   ingested main graph: {files_n} files, {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")
    print(f"   edge kinds exercised: {dict(ek)}")

    print("\n— open all N PRs against main (real brain + real gate) —")
    for i, (name, files) in enumerate(inflight, 1):
        handle_pull_request(db, {"action": "opened", "repo": REPO, "base_branch": BRANCH,
                                 "pr_number": i, "changed_files": files}, name, act_for=True)
        print(f"   opened PR-{i} ({name})")

    impact = json.loads(json.dumps(db("SELECT core.main_impact_surface(%s,%s)", (REPO, BRANCH)))) \
        if isinstance(db("SELECT core.main_impact_surface(%s,%s)", (REPO, BRANCH)), (dict, list)) \
        else db("SELECT core.main_impact_surface(%s,%s)", (REPO, BRANCH))
    if isinstance(impact, str):
        impact = json.loads(impact)

    print("\n" + "=" * 80)
    print(f"MAIN-IMPACT SURFACE  ({impact['inflight_count']} in-flight · "
          f"{impact['serialize_count']} serialize · {impact['warn_count']} warn · "
          f"{impact['clear_count']} clear · {impact['unknown_count']} unknown · "
          f"{impact['cluster_count']} clusters)")
    print("=" * 80)
    icon = {"serialize": "⏸ WAIT", "warn": "⚠ HEADS-UP", "clear": "✓ CLEAR", "unknown": "❓ UNKNOWN"}
    for c in impact["changes"]:
        line = f"  [{icon.get(c['verdict'], c['verdict']):11s}] {c['label']:24s} touches {', '.join(c['paths'])}"
        print(line)
        if c["impact_count"]:
            print(f"               ↳ blast radius {c['impact_count']}: {', '.join(c['impact'])}")
        if c.get("contested_with") and c["contested_with"] != []:
            print(f"               ↳ structurally collides with: {', '.join(c['contested_with'])}")
        if c.get("serialize_behind") and c["serialize_behind"] != []:
            print(f"               ↳ queued behind: {', '.join(c['serialize_behind'])}")
        if c.get("unknown_paths") and c["unknown_paths"] != []:
            print(f"               ↳ not in graph (can't predict): {', '.join(c['unknown_paths'])}")

    print("\n  CONTENTION CLUSTERS (the N-scale unit — coordinate each group together):")
    if not impact["clusters"]:
        print("     (none — no entangled neighborhoods)")
    for cl in impact["clusters"]:
        print(f"     • cluster {cl['cluster_id']} [{cl['verdict']}] — {cl['size']} changes: {', '.join(cl['changes'])}")
        print(f"         suggested land order (biggest blast first): {' → '.join(cl['suggested_order'])}")
        if cl["hotspots"]:
            print(f"         hotspots (files ≥2 members touch): {', '.join(cl['hotspots'])}")

    if args.show_comments:
        from render import render_pr_check
        print("\n  ── what each flagged PR's author actually SEES on their PR (the real GitHub check + comment) ──")
        for c in impact["changes"]:
            if c["verdict"] in ("warn", "serialize"):
                o = render_pr_check(impact, c["change_id"])
                print(f"\n  ┌── PR «{c['label']}»   →   CHECK: [{o['conclusion']}] {o['title']}")
                for ln in o["comment"].splitlines():
                    print("  │ " + ln)
                print("  └──")

    gate_ok = True
    if args.assert_whole_graph:
        # PROVE every graph dimension actually fired — not just that the engine ran. The differentiated depth
        # (Code + Schema + Config, multi-language) is the moat; a green here means the whole graph is exercised.
        need_edges = {"calls", "imports", "queries", "alters", "reads_config"}
        have_edges = need_edges & set(ek)
        schema_fired = any(p.endswith(".sql") and c["contested_with"] for c in impact["changes"] for p in c["paths"])
        # billing.py couples to worker.py ONLY through the shared config key (no call/import between them),
        # so a non-empty contested_with on the billing.py change is proof the config-graph coupling fired.
        config_fired = any(p.endswith("billing.py") and c["contested_with"] for c in impact["changes"] for p in c["paths"])
        cross_lang = {n.get("language") for n in graph["nodes"] if n.get("kind") == "file"}
        checks = [
            ("every edge kind produced (calls·imports·queries·alters·reads_config)", have_edges == need_edges),
            ("schema coupling fired in the surface (a .sql migration collides with code via a table)", schema_fired),
            ("config coupling fired (two files contend on the same config key)", config_fired),
            ("multi-language graph (≥3 languages)", len([l for l in cross_lang if l]) >= 3),
            ("direct same-path serialize present", impact["serialize_count"] >= 1),
            ("unknown-first honesty present (a path not in graph)", impact["unknown_count"] >= 1),
            ("N-scale clustering (≥4 contention neighborhoods)", impact["cluster_count"] >= 4),
        ]
        print("\n  WHOLE-GRAPH COVERAGE:")
        for name, cond in checks:
            print(f"     [{'PASS' if cond else 'FAIL'}] {name}")
            gate_ok = gate_ok and bool(cond)

    # land an active lane-HOLDER that someone is queued behind → free the lane + visibly promote the waiter.
    holders_with_waiters = {sb for c in impact["changes"] for sb in c.get("serialize_behind", [])}
    holder = next((c for c in impact["changes"] if c["label"] in holders_with_waiters), None)
    holder = holder or next((c for c in impact["changes"] if c["verdict"] in ("clear", "warn")), None)
    if holder:
        print(f"\n— LAND {holder['label']} (着地) → free its lanes + promote whoever was queued behind it —")
        res = handle_pull_request(db, {"action": "closed", "merged": True, "repo": REPO, "base_branch": BRANCH,
                                       "pr_number": int(holder["change_id"].split("-")[1]),
                                       "head_sha": "abcdef1234567890", "model": "claude-opus-4-8"}, holder["label"])
        landed = res["landed"]
        print(f"   🟢 landed {holder['change_id']}: released {landed.get('released')} lane(s), "
              f"promoted {landed.get('promoted')} waiter(s)")
        after = db("SELECT core.main_impact_surface(%s,%s)", (REPO, BRANCH))
        after = json.loads(after) if isinstance(after, str) else after
        print(f"   board now: {after['serialize_count']} serialize · {after['warn_count']} warn · "
              f"{after['clear_count']} clear · {after['cluster_count']} clusters")
    if args.assert_whole_graph:
        print("\nWHOLE-GRAPH TEST:", "PASS" if gate_ok else "FAIL")
        return 0 if gate_ok else 1
    print("\nDONE — every verdict/cluster/landing above is the real gate over this repo's real main graph.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
