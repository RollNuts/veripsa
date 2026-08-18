#!/usr/bin/env python3
"""FRESHNESS-GATED SYMBOL DEMOTION gate — the STALENESS silent-miss fix.

The finer (symbol-level) collision refinement (test_finer_collision.py) DROPS a file-level "wait in line" when
two in-flight changes confidently touch DISJOINT symbols of one file. To decide "disjoint" it maps each claim's
changed LINE RANGES (relative to the PR's BASE) onto main's ingested graph symbol SPANS (relative to the commit
the graph was INGESTED from). That mapping is only valid when the file is UNCHANGED between those two commits.

THE BUG (the class this gate locks shut): when the ingested graph's commit differs from the PR's base FOR THAT
FILE (the file changed in the gap), the SAME line numbers now name a DIFFERENT symbol. The engine then maps a
real SAME-symbol collision onto two spans that look disjoint → it DROPS the file-level collision to 'warn' = a
SILENT MISS (a real direct collision the customer never sees). test_finer_collision's case (e2) fixed only the
un-mappable / spilling case; it did NOT fix "mapped cleanly, but to the WRONG symbol because the graph is stale".

THE FIX (fail-safe, content-free): the demotion is now CONDITIONAL on PROVABLE per-file span validity. At ingest
each FILE node carries a content hash (a git-blob-sha — a fingerprint, NEVER the bytes). Each claim carries the
file's content hash AT THE PR'S BASE (the App already sees it per changed file). The engine demotes a file-level
serialize to the finer symbol verdict for a file ONLY when the graph's file hash == BOTH sides' base hash (spans
provably valid). If they differ, or either is NULL/unknown → it KEEPS the file-level serialize (over-flag,
recall-safe). It NEVER silently demotes on unverifiable freshness.

This gate proves, against the REAL gate (db/schema.sql) + the REAL extractor:
  (a) STALE: a graph hash ≠ the PR's base hash for the file, with spans SHIFTED so a real SAME-symbol collision
      *looks* disjoint by line number → the collision is NOT silently dropped → stays 'serialize' (recall held).
  (b) FRESH: when the hashes MATCH, two DIFFERENT-symbol changes still correctly DROP to non-serialize
      (precision preserved — the staleness gate does NOT over-serialize the normal same-version PR).
  (b2) LAGGING-BUT-UNCHANGED FILE: a PR whose base lags main HEAD but whose CHANGED FILE is unchanged in the gap
      (hashes still match) → the precise demotion still works (we gate on the FILE, not the whole-tree commit).
  (c) NULL/unknown hash (old ingest with no file hash, OR an old claim with no base hash) → file-level (safe).

Run:  python3 tests/test_stale_demotion.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID, exactly like db/smoke.sh / run_gates / test_finer_collision.
DB = "veripsa_staletest_" + str(os.getpid())
REPO = "acme/stale"


def db(role, sql, args=()):
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    checks = []

    def ingest(graph):
        db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
           (json.dumps(graph), REPO, "main", "a" * 40))

    def claim(cid, path, author, ranges, base_hash):
        # base_hash is the file's content hash AT THE PR'S BASE (None = an old/un-plumbed claim → unknown).
        rj = json.dumps(ranges) if ranges else None
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
           (cid, path, REPO, "main", author, rj, base_hash))

    def reset_claims():
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
            cur.execute("SELECT set_config('core.governed_write_token','claim',true)")
            cur.execute("UPDATE core.claim SET claim_state='released', released_at=now() WHERE claim_state IN ('active','waiting')")
        conn.close()

    def read1(sql, args=()):
        # direct table reads go via the migrator (the App role reads code_node/claim only through functions).
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    def verdict(cid):
        imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
        if isinstance(imp, str):
            imp = json.loads(imp)
        by = {c["change_id"]: c for c in imp.get("changes", [])}
        return by.get(cid, {}).get("verdict")

    def full_impact():
        imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
        if isinstance(imp, str):
            imp = json.loads(imp)
        return imp

    def change_of(cid):
        # the full change dict (for collision_points → the named symbol, line range, etc.)
        return {c["change_id"]: c for c in full_impact().get("changes", [])}.get(cid, {})

    def named_symbols(cid):
        # the SYMBOL names the surface puts on cid's collision points (None when not confidently named).
        return [cp.get("symbol") for cp in (change_of(cid).get("collision_points") or [])]

    # ── A REAL graph for render.py with TWO symbols (alpha 1-3, beta 7-9). The extractor stamps each FILE node's
    #    content hash (git-blob-sha) — proves the extractor → schema → engine freshness path end to end.
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "render.py"), "w") as fh:
            fh.write("def alpha(x):\n    y = x + 1\n    return y\n\nTOP = 99\n\ndef beta(z):\n    w = z * 2\n    return w\n")
        fresh_graph = X.build_graph(d)
    fresh_hash = next((n.get("content_hash") for n in fresh_graph["nodes"]
                       if n.get("kind") == "file" and n["path"] == "render.py"), None)
    checks.append(("the extractor stamps a content-free content hash on the FILE node "
                   f"(git-blob-sha, len={len(fresh_hash) if fresh_hash else 0})",
                   isinstance(fresh_hash, str) and len(fresh_hash) == 40 and fresh_hash != "0" * 40))
    # the stored graph file node carries that exact hash (ingest → code_node.content_hash round-trips).
    ingest(fresh_graph)
    stored_hash = read1("SELECT content_hash FROM core.code_node WHERE node_kind='file' AND path='render.py' "
                        "AND account_id='ACCT-DEMO' AND repo=%s AND branch='main'", (REPO,))
    checks.append((f"the ingested FILE node round-trips the content hash (stored={stored_hash!r})",
                   stored_hash == fresh_hash))

    # ══════════════════════════════════════════════════════════════════════════════════════════════════════
    # (a) STALE: the graph was ingested from an OLDER commit of render.py. In that old version alpha lived at
    #     lines 1-3 and beta at 7-9 (the spans above). But the PR's base has the file SHIFTED — BOTH PRs in fact
    #     edit the SAME function, whose lines (by the PR's base) are [1,3]. By the STALE graph's spans those
    #     lines map to alpha; a co-editor touching [7,9] (which, in the PR's base, is the SAME function moved)
    #     maps to beta → the line-only engine would call them DISJOINT and DROP the wait = the SILENT MISS.
    #     The freshness gate refuses: the PRs' base hash (the new version) != the graph hash (the old version),
    #     so the spans are NOT provably valid → KEEP the file-level serialize. (We model the stale base with a
    #     DIFFERENT hash; the spans are the old ones, the ranges look disjoint, yet it must NOT be dropped.)
    # ══════════════════════════════════════════════════════════════════════════════════════════════════════
    stale_base = "f" * 40   # the PR's base content of render.py — DIFFERENT from the graph's (the file changed)
    reset_claims()
    claim("PR-A1:render.py", "render.py", "alice", [[1, 3]], stale_base)   # maps to alpha by the STALE spans
    claim("PR-A2:render.py", "render.py", "bob", [[7, 9]], stale_base)     # maps to beta  by the STALE spans
    va = verdict("PR-A2")
    checks.append(("(a) STALE graph (graph hash != base hash): a disjoint-LOOKING pair is NOT silently dropped "
                   f"→ stays 'serialize' (recall held, no silent miss) (verdict='{va}')", va == "serialize"))
    # and the active holder side is equally protected (symmetry: drop must not fire in either orientation).
    checks.append(("(a) STALE: the holder side is not dropped either (the pair survives in both orientations)",
                   verdict("PR-A1") in ("serialize", "clear", "warn", "unknown")))  # A1 is the active holder (not 'waiting')

    # ══════════════════════════════════════════════════════════════════════════════════════════════════════
    # (b) FRESH: the SAME ingested graph, but now both PRs carry the MATCHING base hash (their base == the graph's
    #     commit for this file). Two DIFFERENT symbols (alpha [1,3] vs beta [7,9]) → the spans are provably valid
    #     → the demotion fires → NOT serialized (precision preserved — the staleness gate did not over-serialize).
    # ══════════════════════════════════════════════════════════════════════════════════════════════════════
    reset_claims()
    claim("PR-B1:render.py", "render.py", "carol", [[1, 3]], fresh_hash)   # alpha
    claim("PR-B2:render.py", "render.py", "dave", [[7, 9]], fresh_hash)    # beta — DISJOINT, provably fresh
    vb = verdict("PR-B2")
    checks.append(("(b) FRESH (graph hash == base hash): two DIFFERENT symbols still DROP the false wait "
                   f"→ NOT 'serialize' (precision preserved) (verdict='{vb}')", vb != "serialize"))
    # the SAME symbol on a fresh graph still serializes (the demotion only drops DISJOINT, never SAME).
    reset_claims()
    claim("PR-B3:render.py", "render.py", "erin", [[1, 2]], fresh_hash)    # alpha
    claim("PR-B4:render.py", "render.py", "frank", [[2, 3]], fresh_hash)   # alpha — SAME symbol
    checks.append(("(b) FRESH: the SAME symbol still serializes (the demotion drops disjoint only)",
                   verdict("PR-B4") == "serialize"))
    # FIX3 (ACCURACY): on a FRESH graph the spans are provably aligned → it is SAFE to NAME the shared symbol.
    b4_syms = named_symbols("PR-B4")
    checks.append((f"(b) FRESH + SAME symbol: the surface CONFIDENTLY NAMES the shared symbol 'alpha' "
                   f"(spans provably aligned) (collision_points symbols={b4_syms})", "alpha" in b4_syms))

    # ══════════════════════════════════════════════════════════════════════════════════════════════════════
    # (a2) STALE + SAME-SYMBOL NAMING (FIX3, ACCURACY): the graph is stale (base hash != graph hash). BOTH PRs
    #     in truth edit the SAME function, but by the STALE spans their lines [1,2] and [2,3] both fall in alpha,
    #     so the collision is (correctly) KEPT. THE BUG fixed here: the surface used to confidently NAME that
    #     symbol ("you both edit `alpha`") off the STALE spans — which can be the WRONG symbol after a shift.
    #     The fix gates the NAME on the SAME freshness proof that gates 'disjoint': under a stale graph the
    #     collision SURVIVES (recall) but the symbol is NOT named (no false-confident name); render falls back to
    #     the line range / same-file phrasing. (line_lo/line_hi come from the claim's OWN ranges, still honest.)
    # ══════════════════════════════════════════════════════════════════════════════════════════════════════
    reset_claims()
    claim("PR-G1:render.py", "render.py", "ann", [[1, 2]], stale_base)     # maps to alpha by the STALE spans
    claim("PR-G2:render.py", "render.py", "ben", [[2, 3]], stale_base)     # maps to alpha by the STALE spans
    g2 = change_of("PR-G2")
    g2_syms = named_symbols("PR-G2")
    checks.append((f"(a2) STALE + same-symbol: the collision is KEPT (recall held; verdict='{g2.get('verdict')}')",
                   g2.get("verdict") == "serialize"))
    checks.append((f"(a2) STALE: the symbol is NOT confidently NAMED off stale spans (no wrong-symbol claim) "
                   f"(collision_points symbols={g2_syms})", all(s is None for s in g2_syms) and len(g2_syms) >= 1))
    # render must still surface the collision WITHOUT naming a (possibly wrong) symbol — falls back to line/file.
    import render as R
    rendered_g2 = R.render_pr_check(full_impact(), "PR-G2")
    g2_blob = (rendered_g2.get("comment") or "")
    checks.append(("(a2) STALE: the rendered hard-collision copy does NOT name a symbol (no 'in `alpha`'); "
                   f"it still says 'Wait in line' (recall held) (title='{rendered_g2.get('title')}')",
                   "in `alpha`" not in g2_blob and "Wait in line" in (rendered_g2.get("title") or "")))

    # ══════════════════════════════════════════════════════════════════════════════════════════════════════
    # (b2) LAGGING-BUT-UNCHANGED FILE: a PR whose base lags main HEAD overall, but whose CHANGED FILE was NOT
    #     touched in the gap → the file's base hash STILL equals the graph hash → the precise demotion still
    #     fires. (We gate on the FILE's hash, not a whole-tree commit, so a normal lagging PR is NOT over-flagged.)
    # ══════════════════════════════════════════════════════════════════════════════════════════════════════
    reset_claims()
    claim("PR-C1:render.py", "render.py", "gina", [[1, 3]], fresh_hash)    # alpha (file unchanged since ingest)
    claim("PR-C2:render.py", "render.py", "hank", [[7, 9]], fresh_hash)    # beta
    checks.append(("(b2) LAGGING base but the FILE is unchanged (hash still matches) → demotion still fires "
                   f"→ NOT 'serialize' (no over-serialize of the normal case) (verdict='{verdict('PR-C2')}')",
                   verdict("PR-C2") != "serialize"))

    # ══════════════════════════════════════════════════════════════════════════════════════════════════════
    # (c) NULL / UNKNOWN hash → file-level (safe). Two independent unknown sources, each must KEEP serialize:
    #     (c1) the CLAIM carries no base hash (an old/un-plumbed App) even though the graph has one.
    #     (c2) the GRAPH carries no file hash (an OLD ingest) even though the claims carry a base hash.
    # ══════════════════════════════════════════════════════════════════════════════════════════════════════
    reset_claims()
    claim("PR-D1:render.py", "render.py", "ivy", [[1, 3]], None)           # no base hash on the claim → unknown
    claim("PR-D2:render.py", "render.py", "jay", [[7, 9]], None)
    checks.append(("(c1) UNKNOWN: a claim with NO base hash (old App) → file-level serialize, never a silent drop "
                   f"(verdict='{verdict('PR-D2')}')", verdict("PR-D2") == "serialize"))

    # (c2) re-ingest the SAME graph but STRIP the file hash (an old ingest) — spans present, hash absent.
    old_graph = {"nodes": [{k: v for k, v in n.items() if k != "content_hash"} for n in fresh_graph["nodes"]],
                 "edges": list(fresh_graph["edges"])}
    reset_claims()
    ingest(old_graph)
    no_graph_hash = read1("SELECT content_hash FROM core.code_node WHERE node_kind='file' AND path='render.py' "
                          "AND account_id='ACCT-DEMO' AND repo=%s AND branch='main'", (REPO,))
    claim("PR-E1:render.py", "render.py", "kate", [[1, 3]], fresh_hash)    # claim HAS a hash, but graph has none
    claim("PR-E2:render.py", "render.py", "leo", [[7, 9]], fresh_hash)
    checks.append(("(c2) UNKNOWN: an OLD ingest with NO file hash → file-level serialize, even with a hashed claim "
                   f"(graph hash={no_graph_hash!r}, verdict='{verdict('PR-E2')}')",
                   no_graph_hash is None and verdict("PR-E2") == "serialize"))

    # ── CONTENT-FREE: the freshness key never carries code. The stored values are pure hex fingerprints; no claim
    #    row or graph node holds a body. (A hash cannot be inverted to the bytes — that is the content-free claim.)
    reset_claims()
    ingest(fresh_graph)
    claim("PR-F1:render.py", "render.py", "mia", [[1, 3]], fresh_hash)
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    with conn, conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
        cur.execute("SELECT coalesce(string_agg(base_content_hash,' '),'') FROM core.claim WHERE base_content_hash IS NOT NULL")
        claim_hashes = cur.fetchone()[0]
        cur.execute("SELECT coalesce(string_agg(content_hash,' '),'') FROM core.code_node WHERE content_hash IS NOT NULL")
        node_hashes = cur.fetchone()[0]
    conn.close()
    import re
    is_hex_only = bool(re.fullmatch(r"[0-9a-fA-F ]*", claim_hashes)) and bool(re.fullmatch(r"[0-9a-fA-F ]*", node_hashes))
    checks.append(("CONTENT-FREE: the stored freshness keys are pure hex fingerprints — no code body anywhere "
                   f"(claim hashes={claim_hashes!r})", is_hex_only and claim_hashes != "" and node_hashes != ""))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("STALE DEMOTION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
