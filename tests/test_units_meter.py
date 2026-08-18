#!/usr/bin/env python3
"""Units meter gate (NO DB, fully offline): build tiny synthetic repos in a tempdir, run the PURE meter
(units.compute_units / units.units_from_graph over build_graph output), and assert the meter's invariants:

  (a) a normal repo yields units > 0 and premium >= 1.0, with the v2 component shape;
  (b) SPLIT-INVARIANCE — splitting one file's functions across two files (SAME code) does NOT materially
      inflate units (the bill is LOC-anchored, not file-count-anchored) — within ~10%;
  (c) a repo with shared-table/config COUPLING gets a HIGHER premium than a flat repo of similar LOC;
  (d) the import CAPS bound the premium — a pathologically high xdir_density (a namespace-style fan-out)
      cannot 50× the premium;
  (e) the CALL term lifts the premium — a repo whose only coupling is cross-file CALLS (no imports, no
      substrate) still earns a premium > 1.0 (the fairer cross-language structural signal, #258);
  (f) the CO-CHANGE term lifts the premium and is BOUNDED by its cap (the 2nd detector enters the meter as a
      pure scalar; the raw density is stored, the cap is applied only in the premium math);
  (g) the GLOBAL CAP_PREMIUM bounds the whole premium — an UNCAPPED xs term that would run the bill away is
      clamped to CAP_PREMIUM and flagged premium_capped;
  (h) the offline git-history co-change path runs end-to-end on a REAL repo (this worktree) without Postgres.

Pure: no Postgres, no network — just build_graph output + a LOC sum + local `git log` filenames. Mirrors
tests/test_extractor.py's import idiom (sys.path.insert repo root) and its tiny-fixture-in-tempdir style.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_graph_extract as X  # noqa: E402,F401
import units as U  # noqa: E402


def _w(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(body)


def _flat_repo(root):
    """A flat repo: several files in one directory, NO cross-directory imports, NO shared substrate."""
    for i in range(6):
        _w(os.path.join(root, f"m{i}.py"),
           f"def f{i}_a():\n    return {i}\n\n\ndef f{i}_b():\n    return {i} + 1\n\n\ndef f{i}_c():\n    return {i} + 2\n")


def _coupled_repo(root):
    """Similar LOC to the flat repo, but with shared-substrate coupling: TWO files query the same table and a
    THIRD migrates it (a migration<->query pair on a shared resource — the language-agnostic xs term)."""
    _w(os.path.join(root, "svc", "orders.py"),
       "def list_orders(db):\n    return db.execute('SELECT id FROM orders')\n\n\n"
       "def count_orders(db):\n    return db.execute('SELECT count(*) FROM orders')\n")
    _w(os.path.join(root, "svc", "reports.py"),
       "def revenue(db):\n    return db.execute('SELECT sum(total) FROM orders')\n\n\n"
       "def daily(db):\n    return db.execute('SELECT day FROM orders')\n")
    _w(os.path.join(root, "db", "0001_orders.sql"),
       "CREATE TABLE orders (id int, total int, day date);\nALTER TABLE orders ADD COLUMN note text;\n")
    # pad to a similar LOC scale as the flat repo, no coupling in these
    for i in range(2):
        _w(os.path.join(root, f"pad{i}.py"),
           f"def p{i}_a():\n    return {i}\n\n\ndef p{i}_b():\n    return {i} + 1\n")


# The split-invariance fixture: big.py is two halves (HALF_A + HALF_B) concatenated. The UNSPLIT file is the
# exact concatenation; the SPLIT version writes the two halves to two files. Total LOC is BYTE-IDENTICAL across
# the two layouts (we split at a clean function boundary, no separators added or removed), so any units delta
# can ONLY come from the meter reacting to file COUNT — which is exactly what split-invariance forbids. We use a
# non-trivial number of functions so the LOC base isn't so tiny that 1 line reads as a large percentage.
_HALF_A = "".join(f"def a{i}():\n    return {i}\n\n\n" for i in range(8))
_HALF_B = "".join(f"def b{i}():\n    return {i}\n\n\n" for i in range(8))


def _one_big_file(root):
    """One file holding ALL the functions (the 'before' of the split-invariance check)."""
    _w(os.path.join(root, "lib", "big.py"), _HALF_A + _HALF_B)
    # a couple of neutral neighbors so the graph is non-trivial but identical across before/after
    _w(os.path.join(root, "lib", "util.py"), "def helper():\n    return 0\n")
    _w(os.path.join(root, "app.py"), "def run():\n    return 42\n")


def _split_file(root):
    """The SAME code as _one_big_file, byte-for-byte, but big.py's functions split across two files at a clean
    function boundary (HALF_A | HALF_B). Total LOC is identical — so units must not jump (LOC-anchored)."""
    _w(os.path.join(root, "lib", "big_a.py"), _HALF_A)
    _w(os.path.join(root, "lib", "big_b.py"), _HALF_B)
    _w(os.path.join(root, "lib", "util.py"), "def helper():\n    return 0\n")
    _w(os.path.join(root, "app.py"), "def run():\n    return 42\n")


def _meter(root):
    return U.compute_units(root)


def main():
    checks = []
    P = U._UNIT_POLICY

    # (a) a normal (coupled) repo yields units > 0 and premium >= 1.0, with the v2 component shape.
    with tempfile.TemporaryDirectory() as d:
        _coupled_repo(d)
        r = _meter(d)
    checks.append(("(a) units > 0 on a normal repo", r["units"] > 0))
    checks.append(("(a) premium >= 1.0 (floor)", r["premium"] >= 1.0))
    checks.append(("(a) result is content-free shape (units/loc/premium/premium_capped/components only)",
                   set(r) >= {"units", "loc", "premium", "premium_capped", "components"}
                   and set(r["components"]) == {"xs_intensity", "call_density", "xdir_density",
                                                "blast_mean", "hub_bucket", "cochange_blind_density"}))

    # (b) SPLIT-INVARIANCE: splitting one file's functions across two files (same code) must NOT materially
    #     inflate units — the meter is LOC-anchored, not file-count-anchored. Within ~10%.
    with tempfile.TemporaryDirectory() as d1:
        _one_big_file(d1)
        before = _meter(d1)
    with tempfile.TemporaryDirectory() as d2:
        _split_file(d2)
        after = _meter(d2)
    rel = abs(after["units"] - before["units"]) / max(before["units"], 1)
    checks.append((f"(b) split-invariance: units stable when one file is split in two "
                   f"(before={before['units']} after={after['units']}, Δ={rel*100:.1f}% ≤ 10%)", rel <= 0.10))

    # (c) a repo with shared-substrate COUPLING gets a HIGHER premium than a flat repo of similar LOC.
    with tempfile.TemporaryDirectory() as df:
        _flat_repo(df)
        flat = _meter(df)
    with tempfile.TemporaryDirectory() as dc:
        _coupled_repo(dc)
        coupled = _meter(dc)
    checks.append((f"(c) coupled premium > flat premium "
                   f"(flat={flat['premium']:g} on {flat['loc']} LOC, coupled={coupled['premium']:g} on "
                   f"{coupled['loc']} LOC)", coupled["premium"] > flat["premium"]))
    checks.append(("(c) flat repo (no cross-dir imports, no shared substrate) has ~no premium lift "
                   f"(flat premium={flat['premium']:g} ≈ 1.0)", abs(flat["premium"] - 1.0) < 0.05))

    # (d) the import CAPS bound the premium: a PATHOLOGICALLY high xdir_density (namespace-style fan-out) cannot
    #     50× the premium. Feed the PURE function a synthetic graph with an absurd cross-dir import count.
    nf = 4
    nodes = [{"id": f"d{i}/f{i}.py", "kind": "file", "path": f"d{i}/f{i}.py"} for i in range(nf)]
    edges = []
    paths = [n["path"] for n in nodes]
    for a in paths:
        for b in paths:
            if a != b:
                edges.append({"src": a, "dst": b, "kind": "imports"})
    for i in range(50):  # pile on a fake mega-hub in-degree to try to spike the hub/blast terms too
        nodes.append({"id": f"x{i}/leaf{i}.py", "kind": "file", "path": f"x{i}/leaf{i}.py"})
        edges.append({"src": f"x{i}/leaf{i}.py", "dst": "d0/f0.py", "kind": "imports"})
    pathological = U.units_from_graph({"nodes": nodes, "edges": edges}, loc=1000)
    # max premium the import/hub caps allow (xs/call/cc are 0 here):
    cap_max = 1.0 + P["a_xdir"] * P["CAP_XDIR"] + P["a_blast"] * P["CAP_BLAST"] + P["a_hub"] * 3
    checks.append((f"(d) caps bound the premium under a pathological fan-out "
                   f"(premium={pathological['premium']:g} ≤ cap_max={cap_max:g})",
                   pathological["premium"] <= cap_max + 1e-9))
    checks.append((f"(d) capped premium stays far below a 50× bill blow-up "
                   f"(premium={pathological['premium']:g} < 5×)", pathological["premium"] < 5.0))

    # (e) the CALL term lifts the premium: a graph whose ONLY coupling is a cross-file CALL (no imports, no
    #     substrate) still earns premium > 1.0. file a/x.py calls a uniquely-named fn defined in b/y.py.
    call_nodes = [{"id": "a/x.py", "kind": "file", "path": "a/x.py"},
                  {"id": "b/y.py", "kind": "file", "path": "b/y.py"},
                  {"id": "b/y.py::unique_widget_fn", "kind": "def", "path": "b/y.py", "name": "unique_widget_fn"}]
    call_edges = [{"src": "a/x.py", "dst": "unique_widget_fn", "kind": "calls"}]
    call_only = U.units_from_graph({"nodes": call_nodes, "edges": call_edges}, loc=200)
    checks.append((f"(e) cross-file call resolved into a call pair (call_density="
                   f"{call_only['components']['call_density']:g} > 0)",
                   call_only["components"]["call_density"] > 0))
    checks.append((f"(e) the call term lifts the premium above the 1.0 floor "
                   f"(premium={call_only['premium']:g} > 1.0)", call_only["premium"] > 1.0))
    # a STOP-listed common call name must NOT resolve into a billable pair (precision bias for billing):
    stop_nodes = [{"id": "a/x.py", "kind": "file", "path": "a/x.py"},
                  {"id": "b/y.py", "kind": "file", "path": "b/y.py"},
                  {"id": "b/y.py::run", "kind": "def", "path": "b/y.py", "name": "run"}]
    stop_only = U.units_from_graph({"nodes": stop_nodes, "edges": [{"src": "a/x.py", "dst": "run", "kind": "calls"}]},
                                   loc=200)
    checks.append((f"(e) a STOP-listed call name ('run') does NOT bill as a call pair "
                   f"(call_density={stop_only['components']['call_density']:g} == 0)",
                   stop_only["components"]["call_density"] == 0))

    # (f) the CO-CHANGE term lifts the premium and is BOUNDED by its cap. Pure-scalar path (no git needed).
    g2 = {"nodes": [{"id": "a.py", "kind": "file", "path": "a.py"},
                    {"id": "b.py", "kind": "file", "path": "b.py"}], "edges": []}
    cc0 = U.units_from_graph(g2, loc=200, cochange_blind_density=0.0)
    cc1 = U.units_from_graph(g2, loc=200, cochange_blind_density=1.0)
    cc_big = U.units_from_graph(g2, loc=200, cochange_blind_density=999.0)
    checks.append((f"(f) co-change density lifts the premium (cc0={cc0['premium']:g} < cc1={cc1['premium']:g})",
                   cc1["premium"] > cc0["premium"]))
    checks.append((f"(f) the co-change term is bounded by its cap "
                   f"(Δ={cc_big['premium'] - cc0['premium']:g} ≤ a_cc·CAP_CC={P['a_cc'] * P['CAP_CC']:g})",
                   (cc_big["premium"] - cc0["premium"]) <= P["a_cc"] * P["CAP_CC"] + 1e-9))
    checks.append((f"(f) the RAW co-change density is stored (cap applied only in premium math) "
                   f"(stored={cc_big['components']['cochange_blind_density']:g} == 999.0)",
                   cc_big["components"]["cochange_blind_density"] == 999.0))

    # (g) the GLOBAL CAP_PREMIUM bounds the whole premium: an UNCAPPED xs term that would run the bill away
    #     (100 shared tables across 4 files → xs_intensity=25 → premium_raw ≈ 51) is clamped to CAP_PREMIUM.
    gnodes = [{"id": f"f{i}.py", "kind": "file", "path": f"f{i}.py"} for i in range(4)]
    gedges = []
    for t in range(100):
        gedges.append({"src": "f0.py", "dst": f"tbl{t}", "kind": "queries"})
        gedges.append({"src": "f1.py", "dst": f"tbl{t}", "kind": "queries"})
    huge = U.units_from_graph({"nodes": gnodes, "edges": gedges}, loc=1000)
    checks.append((f"(g) CAP_PREMIUM clamps a runaway xs premium "
                   f"(premium={huge['premium']:g} == CAP_PREMIUM={P['CAP_PREMIUM']:g})",
                   abs(huge["premium"] - P["CAP_PREMIUM"]) < 1e-6))
    checks.append((f"(g) the clamp is flagged (premium_capped={huge['premium_capped']})",
                   huge["premium_capped"] is True))
    checks.append((f"(g) units honor the cap (units={huge['units']} == int(1000·CAP_PREMIUM))",
                   huge["units"] == int(1000 * P["CAP_PREMIUM"])))

    # (h) the offline git-history co-change path runs END-TO-END on a REAL repo (this worktree) — no Postgres.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    real = U.compute_units(repo_root)
    ccd = real["components"]["cochange_blind_density"]
    checks.append((f"(h) real-repo meter runs (units={real['units']:,} > 0, premium={real['premium']:g} ≥ 1.0)",
                   real["units"] > 0 and real["premium"] >= 1.0))
    checks.append((f"(h) the offline git co-change term computed without crashing (cochange_blind_density="
                   f"{ccd:g} ≥ 0)", isinstance(ccd, float) and ccd >= 0.0))

    ok = all(c[1] for c in checks)
    print("\n=== UNITS METER (pure, offline) ===")
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    if not ok:
        print("\nUNITS METER GATE: FAIL")
        return 1
    print("\nUNITS METER GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
