#!/usr/bin/env python3
"""EDGE PRECISION AUDIT — content-free precision harness for contract-graph detectors (shipped or candidate).

Issue #721's rule: a new coupling detector ships only with a MEASURED precision boundary, and shared-token
wallpaper is forbidden. This tool measures, per contract FAMILY, the pairs a detector's edges couple and how
many of those coincide with a real CORRECTION INCIDENT (a content-free precision PROXY) — so "does this
family create wallpaper or land on real coupling?" is a number, not a vibe. It works on the ALREADY-SHIPPED
substrates (route/openapi/api_*/tauri/ci/job/sibling_stem/role_feature) and on any future candidate the same
way, because they all emit the same content-free `queries`/`alters` edges to a `family::key` contract node.

PRECISION PROXY, stated honestly: incident-hit-rate is a LOWER BOUND on true precision — a coupled pair that
never happened to co-occur in a revert/fix commit is counted as a "miss" even if the coupling is real. So a
LOW number does not prove a family is wrong, but a family whose emitted-pair volume dwarfs its incident hits
is a wallpaper risk. Read it together with fan-out and emitted-pair volume, never alone.

CONTENT-FREE: reads only the extracted graph (contract KEY names + paths) and combined_recall's incident
pairs (paths + commit SUBJECT booleans). No file bodies.

Run:  python3 tests/edge_precision_audit.py [/repo ...]     # audit present panel repos (or given repos)
      python3 tests/edge_precision_audit.py --selftest       # deterministic logic proof (no git/network)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import combined_recall as CR  # noqa: E402

# res_adj edge kinds (a file that touches a shared contract `dst`). imports = file->file (routes cross-tier).
_RES_KINDS = ("queries", "alters", "reads_config")
_HUB = CR.HUB_DEGREE  # a contract key defined/used by > HUB distinct files is a hot resource -> dampened out.


def family_of(dst: str) -> str:
    """The contract FAMILY namespace of a contract key. `route::/api/x` -> 'route'; a bare file path or bare
    table name (no '::') is NOT a synthetic contract node -> 'file_or_table'. Pure/deterministic."""
    if not isinstance(dst, str):
        return "?"
    if "::" in dst:
        return dst.split("::", 1)[0]
    return "file_or_table"


def pairs_from_groups(groups):
    """Given {contract_key: set(files)}, emit undirected coupled pairs, applying the res-hub dampening (a key
    with > HUB distinct files is a hot resource, dropped — mirrors core._claim_adjacency res_hubs). Returns
    {family: set(frozenset(a,b))} plus a per-family fan-out histogram. Deterministic (sorted enumeration)."""
    by_family: dict = {}
    fanout: dict = {}
    for key, files in groups.items():
        fam = family_of(key)
        fl = sorted(f for f in files)
        if len(fl) < 2:
            continue
        fanout.setdefault(fam, {}).setdefault(len(fl), 0)
        fanout[fam][len(fl)] += 1
        if len(fl) > _HUB:
            continue  # hot-resource dampening (never couple all N users of a hub key)
        s = by_family.setdefault(fam, set())
        for i in range(len(fl)):
            for j in range(i + 1, len(fl)):
                s.add(frozenset((fl[i], fl[j])))
    return by_family, fanout


def contract_groups(g, files):
    """Build {contract_key: set(files)} from res_adj edges (queries/alters/reads_config) whose dst is a
    synthetic contract node (namespaced 'family::...'). Bare-table / file-path dsts are excluded (they are the
    within-language substrate, not a cross-dir contract). Content-free (keys + paths)."""
    groups: dict = {}
    for e in g["edges"]:
        if e.get("kind") in _RES_KINDS and e["src"] in files:
            dst = e.get("dst", "")
            if "::" in dst:  # synthetic contract node only
                groups.setdefault(dst, set()).add(e["src"])
    return groups


def audit(repo):
    g = CR.X.build_graph(repo)
    files = {n["path"] for n in g["nodes"] if n["kind"] == "file"}
    groups = contract_groups(g, files)
    by_family, fanout = pairs_from_groups(groups)
    incidents, _ = CR.incident_pairs(repo, files)
    out = {}
    for fam, pairs in by_family.items():
        hit = sum(1 for p in pairs if p in incidents)
        out[fam] = {
            "emitted_pairs": len(pairs),
            "incident_hits": hit,
            "incident_precision_proxy": (hit / len(pairs) * 100.0) if pairs else 0.0,
            "max_fanout": max(fanout.get(fam, {0: 0}).keys()) if fanout.get(fam) else 0,
        }
    return {"repo": os.path.basename(repo.rstrip("/")), "n_incidents": len(incidents), "families": out}


def _selftest():
    f = []
    # family parsing
    if family_of("route::/api/x") != "route" or family_of("sibling_stem::go::a::user") != "sibling_stem":
        f.append("family_of namespace parse wrong")
    if family_of("core.claim") != "file_or_table" or family_of("src/a.py") != "file_or_table":
        f.append("bare table/path must not be a contract family")
    # dampening + pair emission
    groups = {
        "route::/x": {"a.py", "b.ts"},                 # 2 files -> 1 pair, family route
        "job::big": {f"f{i}.py" for i in range(_HUB + 3)},  # > HUB files -> dampened out (0 pairs)
        "sibling_stem::s": {"m/u.go", "n/u.go", "o/u.go"},  # 3 files -> 3 pairs
    }
    bf, fo = pairs_from_groups(groups)
    if len(bf.get("route", set())) != 1:
        f.append(f"route emitted {len(bf.get('route', set()))} pairs, expected 1")
    if "job" in bf:  # the hot key must be dampened out of coupling entirely
        f.append("hub key was not dampened out")
    if len(bf.get("sibling_stem", set())) != 3:
        f.append(f"sibling_stem emitted {len(bf.get('sibling_stem', set()))} pairs, expected 3")
    # determinism / order independence
    g2 = {"route::/x": {"b.ts", "a.py"}}
    if pairs_from_groups(g2)[0]["route"] != pairs_from_groups(groups)[0]["route"]:
        f.append("pair emission order-dependent")
    # content-free source posture: the ANALYSIS path (everything BEFORE _selftest) must read no repo file
    # body. We excise the _selftest function itself so its own scan-literals (and its harmless read of this
    # source) do not self-trip — the real invariant is that audit()/contract_groups()/pairs_from_groups()
    # touch only the extracted graph + incident pairs, never a file body.
    full_src = open(os.path.join(ROOT, "tests", "edge_precision_audit.py"), "r", encoding="utf-8").read()
    analysis_src = full_src.split("def _selftest")[0]
    for forbidden in ("open(", ".read_text(", ".read_bytes(", "io.open(", "codecs.open("):
        if forbidden in analysis_src:
            f.append(f"analysis path reads file contents ({forbidden})")
    print("\n=== EDGE PRECISION AUDIT SELFTEST ===")
    if f:
        for x in f:
            print("  FAIL:", x)
    print("\nEDGE PRECISION AUDIT GATE:", "PASS" if not f else "FAIL")
    return 0 if not f else 1


def main():
    if "--selftest" in sys.argv:
        return _selftest()
    root = os.environ.get("AIERA_PANEL_ROOT", "/tmp")
    targets = [os.path.abspath(p) for p in sys.argv[1:] if not p.startswith("--")]
    if not targets:
        import recall_integrity as RI
        for full in RI.PANEL_MANIFEST:
            p = os.path.join(root, full.replace("/", "_"))
            if os.path.isdir(os.path.join(p, ".git")):
                targets.append(p)
    if not targets:
        print("NO repos present. Honest 'not measured'.")
        return 0
    pooled: dict = {}
    print("\n=== EDGE PRECISION AUDIT (shipped/candidate contract families; incident-precision PROXY) ===")
    for p in targets:
        r = audit(p)
        if r["n_incidents"] == 0:
            continue
        for fam, d in sorted(r["families"].items()):
            agg = pooled.setdefault(fam, {"emitted": 0, "hits": 0, "repos": 0})
            agg["emitted"] += d["emitted_pairs"]; agg["hits"] += d["incident_hits"]; agg["repos"] += 1
        top = sorted(r["families"].items(), key=lambda kv: -kv[1]["emitted_pairs"])[:6]
        print(f"\n{r['repo']} (incidents={r['n_incidents']}):")
        for fam, d in top:
            print(f"  {fam:<16} emitted={d['emitted_pairs']:<6} incident_hits={d['incident_hits']:<5} "
                  f"precision_proxy={d['incident_precision_proxy']:5.1f}%  max_fanout={d['max_fanout']}")
    print("\nPOOLED BY FAMILY (incident-precision PROXY = lower bound on true precision):")
    for fam, agg in sorted(pooled.items(), key=lambda kv: -kv[1]["emitted"]):
        e = agg["emitted"]; h = agg["hits"]
        print(f"  {fam:<16} emitted={e:<7} incident_hits={h:<6} proxy={h/max(e,1)*100:5.1f}%  repos={agg['repos']}")
    print("\nHONEST BOUNDARY: incident-precision is a LOWER BOUND (a real coupling that never hit a revert/fix")
    print("commit counts as a 'miss'). Read WITH emitted-pair volume + fan-out; a high emitted / low hit family")
    print("is a wallpaper risk, a low-volume family is precision-safe even at a modest proxy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
