#!/usr/bin/env python3
"""CO-CHANGE-CORROBORATED HUB COUPLING GATE — the precision-SAFE recall relaxation of AUDIT3.

THE FEATURE: AUDIT3 (the unknown-first invariant, gate 55) says a REAL import/calls/shared-resource coupling
that hub-dampening suppressed renders 'unknown' (honest, never a silent 'clear', never a noise-exploding
'warn'). That is the floor. THIS gate proves the ONE sanctioned relaxation: when git CO-CHANGE history
INDEPENDENTLY confirms the two dampened files really move together — lift >= 2 (≥2× chance, base-rate
corrected) AND co >= 3 (repeated, not a one-off) — then TWO agreeing signals (the structural import edge we hid
for hub-noise + a corroborated co-change) make it a CONFIDENT real coupling, so it EARNS a 'warn'. Everything
else is UNCHANGED: an uncorroborated dampened coupling (no co_change row, or one below the lift/co floor) stays
'unknown', and an empty co_change table degrades gracefully to today's 'unknown' behavior.

This must FAIL on origin/main (pre-feature, where the corroborated coupling is still 'unknown') and PASS on the
branch — i.e. it is a REAL gate, not a tautology. It builds the EXACT minimal hub shape against the REAL gate
(db functions) and asserts:
  (a) hub + ONE importer both in-flight, coupled ONLY through the hub (in-degree > 8 ⇒ dampened), WITH a
      co_change row for the canonical (path_a<path_b) pair at lift>=2 AND co>=3
        ⇒ main_impact_surface verdict = 'warn' AND the dampened_with entry has corroborated=true.
  (b) IDENTICAL shape, NO co_change row ⇒ 'unknown' (unchanged AUDIT3), corroborated=false; AND a separate pair
      with a co_change row BELOW the floor (lift<2 OR co<3) ⇒ also 'unknown', corroborated=false.
  (c) co_change EMPTY (the whole table) ⇒ 'unknown' (graceful — cc.* NULL → corroborated=false).
  (d) CONTENT-FREE: the surface JSON carries no file BODIES — only paths / labels / the hub file name / counts.
  (e) RENDER: the renderer on a 'warn' change whose dampened_with=[{by, via_hub, corroborated:true}] emits the
      corroborated "two independent signals … agree they are coupled" copy; an 'unknown' change whose
      dampened_with=[{…corroborated:false}] emits the "suppressed to avoid noise" copy (and NOT the other).

Run:  python3 tests/test_cochange_corroborated_warn.py   (needs local Postgres with the veripsa roles)
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
import render as R  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run. Per-PID, exactly
# like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_unknown_first_dampening.py.
DB = "veripsa_cc_corrob_" + str(os.getpid())
REPO = "acme/ccwarn"
N = 12   # importers of each hub (> default cutoff 8 → the import target is a dampened hub)

# the canonical pairs the LEFT JOIN keys on (path_a < path_b). leaf<hub for 'leaf_0.py' & 'hub.py'? no — sort.
HUB = "hub.py"
LEAF = "leaf_0.py"            # the in-flight importer that WILL get a corroborating co_change row
UHUB = "uhub.py"             # a second hub whose importer gets NO / sub-floor co_change (control)
ULEAF = "uleaf_0.py"


def db(role, sql, args=(), hub_degree=None):
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if hub_degree is not None:
                cur.execute("SET veripsa.hub_degree = %s", (str(hub_degree),))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def build_graph():
    """A content-free graph: two HUBS each imported by N>8 leaves (so both are dampened), plus a NORMAL non-hub
    pair as the precision control. NO file bodies cross the boundary — the extractor reads paths + import edges
    only. We embed a SECRET token in the file bodies so (d) can prove the surface never echoes it."""
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        # HUB #1 — will be CORROBORATED by a co_change row in case (a).
        with open(os.path.join(d, HUB), "w") as fh:
            fh.write("# SECRET_BODY_hub\ndef helper():\n    return 1\n")
        for i in range(N):
            with open(os.path.join(d, f"leaf_{i}.py"), "w") as fh:
                fh.write(f"# SECRET_BODY_leaf{i}\nfrom hub import helper\n\ndef use_{i}():\n    return helper()\n")
        # HUB #2 — its importer gets NO co_change row (case b) / a SUB-FLOOR row (case b'). Must stay 'unknown'.
        with open(os.path.join(d, UHUB), "w") as fh:
            fh.write("# SECRET_BODY_uhub\ndef uhelper():\n    return 2\n")
        for i in range(N):
            with open(os.path.join(d, f"uleaf_{i}.py"), "w") as fh:
                fh.write(f"# SECRET_BODY_uleaf{i}\nfrom uhub import uhelper\n\ndef uuse_{i}():\n    return uhelper()\n")
        return X.build_graph(d)


def seed_cochange(pairs):
    """Seed core.co_change for REPO via the gated write (content-free: paths + counts only). `pairs` is a list of
    {a,b,co,n_a,n_b,strength,lift,n_total}. ingest_cochange_with_authority canonicalizes a/b internally."""
    db("veripsa_app", "SELECT core.ingest_cochange_with_authority(%s,%s)", (json.dumps(pairs), REPO))


def surface(hub_degree=8):
    imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"), hub_degree=hub_degree)
    if isinstance(imp, str):
        imp = json.loads(imp)
    return imp, {c["change_id"]: c for c in imp.get("changes", [])}


def corrob_of(change):
    """The corroborated flag the dampened_with entry for this change carries (any True ⇒ True)."""
    dw = change.get("dampened_with", []) or []
    return any(bool(e.get("corroborated")) for e in dw), dw


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    graph = build_graph()
    db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", "a" * 40))

    def claim(cid, path, author):
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, REPO, "main", author))

    # IN-FLIGHT, two distinct changes per hub (different authors) — the dampened-coupling shape. PR-HUB+PR-LEAF
    # (corroborated in case a); PR-UHUB+PR-ULEAF (uncorroborated control, cases b/b').
    claim("PR-HUB:hub.py", HUB, "hubdev")
    claim("PR-LEAF:leaf_0.py", LEAF, "leafdev")
    claim("PR-UHUB:uhub.py", UHUB, "uhubdev")
    claim("PR-ULEAF:uleaf_0.py", ULEAF, "uleafdev")

    checks = []

    def chk(cond, label):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        checks.append(bool(cond))

    # ── (c) co_change EMPTY (table untouched) ⇒ graceful 'unknown', corroborated=false. (Run FIRST, before any
    #    co_change is seeded — proves the LEFT JOIN of an empty table degrades to today's AUDIT3 behavior.) ──────
    _, chC = surface()
    leafC = chC.get("PR-LEAF", {})
    cC, dwC = corrob_of(leafC)
    chk(leafC.get("verdict") == "unknown" and not cC,
        f"(c) co_change EMPTY ⇒ the dampened importer is graceful 'unknown' + corroborated=false "
        f"(got verdict={leafC.get('verdict')!r}, corroborated={cC}, dw={dwC})")

    # ── (b) a SUB-FLOOR co_change row ⇒ NOT corroborated. Seed BOTH controls below the floor and one above-co but
    #    below-lift, to prove BOTH thresholds gate (lift>=2 AND co>=3). leaf<->hub here gets a row with co=2
    #    (below the co>=3 floor) AND uleaf<->uhub a row with lift=1.0 (below the lift>=2 floor). Both stay
    #    'unknown'. (Seed now; case (a) RE-seeds leaf above the floor afterwards.) ───────────────────────────────
    seed_cochange([
        {"a": HUB,  "b": LEAF,  "co": 2, "n_a": 10, "n_b": 4, "strength": 0.5, "lift": 5.0, "n_total": 100},   # lift OK, co<3
        {"a": UHUB, "b": ULEAF, "co": 9, "n_a": 10, "n_b": 9, "strength": 0.9, "lift": 1.0, "n_total": 100},   # co OK, lift<2
    ])
    _, chB = surface()
    leafB = chB.get("PR-LEAF", {})
    uleafB = chB.get("PR-ULEAF", {})
    cB, dwB = corrob_of(leafB)
    cUB, dwUB = corrob_of(uleafB)
    chk(leafB.get("verdict") == "unknown" and not cB,
        f"(b) a co_change row with co<3 does NOT corroborate ⇒ still 'unknown', corroborated=false "
        f"(got verdict={leafB.get('verdict')!r}, corroborated={cB}, dw={dwB})")
    chk(uleafB.get("verdict") == "unknown" and not cUB,
        f"(b') a co_change row with lift<2 does NOT corroborate ⇒ still 'unknown', corroborated=false "
        f"(got verdict={uleafB.get('verdict')!r}, corroborated={cUB}, dw={dwUB})")

    # ── (a) a co_change row AT/ABOVE the floor (lift>=2 AND co>=3) ⇒ the dampened coupling EARNS a 'warn' and the
    #    dampened_with entry has corroborated=true. Re-seed leaf<->hub above the floor (uhub stays below). ────────
    seed_cochange([
        {"a": HUB,  "b": LEAF,  "co": 6, "n_a": 8, "n_b": 7, "strength": 0.75, "lift": 4.5, "n_total": 100},   # corroborated
        {"a": UHUB, "b": ULEAF, "co": 9, "n_a": 10, "n_b": 9, "strength": 0.9, "lift": 1.0, "n_total": 100},   # still sub-floor control
    ])
    surfA, chA = surface()
    leafA = chA.get("PR-LEAF", {})
    hubA = chA.get("PR-HUB", {})
    uleafA = chA.get("PR-ULEAF", {})
    cA, dwA = corrob_of(leafA)
    chk(leafA.get("verdict") == "warn" and cA,
        f"(a) a corroborated dampened coupling (lift>=2 & co>=3) EARNS 'warn' + corroborated=true "
        f"(got verdict={leafA.get('verdict')!r}, corroborated={cA}, dw={dwA})")
    # the corroborated dampened_with names the OTHER in-flight change + the hub it runs through (silence→signal).
    chk(any(("hubdev" in str(e.get("by", "")) and e.get("via_hub") == HUB and e.get("corroborated"))
            for e in dwA),
        f"(a) the corroborated dampened_with names the hub-editing PR + via_hub={HUB} (got {dwA})")
    # SYMMETRIC: the hub-editing PR is corroborated too (same canonical pair) ⇒ it also EARNS 'warn'.
    cHubA, dwHubA = corrob_of(hubA)
    chk(hubA.get("verdict") == "warn" and cHubA and any("leafdev" in str(e.get("by", "")) for e in dwHubA),
        f"(a) SYMMETRIC: the hub-editing PR is ALSO 'warn'+corroborated, surfacing the importer "
        f"(got verdict={hubA.get('verdict')!r}, corroborated={cHubA}, dw={dwHubA})")
    # PRECISION: the SUB-FLOOR control PR is UNAFFECTED by the seeding above — still 'unknown', corroborated=false
    # (proves the promotion is gated on the corroboration, never a blanket relaxation of the never-warn guard).
    cUA, dwUA = corrob_of(uleafA)
    chk(uleafA.get("verdict") == "unknown" and not cUA,
        f"(a) PRECISION: the sub-floor control stays 'unknown'+corroborated=false beside a corroborated pair "
        f"(got verdict={uleafA.get('verdict')!r}, corroborated={cUA}, dw={dwUA})")

    # ── (d) CONTENT-FREE: the whole surface JSON carries NO file body (only paths / labels / the hub name / counts).
    surf_json = json.dumps(surfA)
    chk("SECRET" not in surf_json,
        "(d) content-free: the surface JSON echoes NO file body (no 'SECRET_BODY_*' token) — paths/labels/counts only")
    # belt: the hub path + the corroborated flag ARE present (the signal is real, just body-free).
    chk(HUB in surf_json and '"corroborated": true' in surf_json,
        "(d) the surface still carries the hub path + corroborated flag (the body-free signal survives)")

    # ── (e) RENDER: drive the REAL renderer's detail builder on hand-built dampened_with rows (content-free) ─────
    #    'warn' + corroborated:true → the "two independent signals … agree they are coupled" copy.
    Lw: list = []
    R._render_warn_unknown_detail(
        Lw, "warn", contested_with=[],
        unknown_paths=[],
        dampened_with=[{"by": "GH-leafdev PR-LEAF:leaf_0.py", "via_hub": HUB, "corroborated": True}])
    warn_body = "\n".join(Lw)
    chk("two independent signals" in warn_body and "repeatedly change together" in warn_body,
        f"(e) 'warn'+corroborated renders the 'two independent signals … repeatedly change together' copy "
        f"(got: {warn_body!r})")
    chk("suppressed to avoid noise" not in warn_body,
        "(e) the corroborated 'warn' copy does NOT use the 'suppressed to avoid noise' (unknown) wording")
    #    'unknown' + corroborated:false → the "suppressed to avoid noise" copy (and NOT the corroborated one).
    Lu: list = []
    R._render_warn_unknown_detail(
        Lu, "unknown", contested_with=[],
        unknown_paths=[],
        dampened_with=[{"by": "GH-leafdev PR-LEAF:leaf_0.py", "via_hub": HUB, "corroborated": False}])
    unk_body = "\n".join(Lu)
    chk("suppressed to avoid noise" in unk_body and "treat as unknown, not clear" in unk_body.lower(),
        f"(e) 'unknown'+uncorroborated renders the 'suppressed to avoid noise — treat as unknown, not clear' copy "
        f"(got: {unk_body!r})")
    chk("two independent signals" not in unk_body,
        "(e) the uncorroborated 'unknown' copy does NOT use the corroborated 'two independent signals' wording")

    ok = all(checks)
    print("CO-CHANGE-CORROBORATED WARN GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], cwd=ROOT, capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
