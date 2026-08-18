#!/usr/bin/env python3
"""Veripsa Units — the billing METER, as a PURE, content-free computation over Veripsa's own code graph.

A customer's bill is anchored to the LANGUAGE-AGNOSTIC base (in-scope LOC) and lifted by a *premium* that
reflects how ENTANGLED the codebase is — because entanglement is exactly what Veripsa earns its keep on
(more cross-file / cross-substrate coupling = more pre-merge collisions to catch). The meter:

    Units = in_scope_LOC * premium
    premium = min(CAP_PREMIUM, 1
              + a_xs   * xs_intensity                       # cross-substrate (shared tables/config + migration<->query), per file
              + a_call * min(call_density,     CAP_CALL)    # resolved cross-file CALLS, per file (CAPPED)
              + a_xdir * min(xdir_density,     CAP_XDIR)    # resolved cross-directory imports, per file (CAPPED)
              + a_blast* min(blast_mean,       CAP_BLAST)   # mean coupling-graph breadth (CAPPED)
              + a_hub  * hub_bucket                         # bucketed hub/contention concentration {0,1,2,3}
              + a_cc   * min(cochange_blind,   CAP_CC))     # git CO-CHANGE coupling with NO code edge (graph-blind), per file (CAPPED)

WHY TWO STRUCTURAL TERMS (calls AND imports). Imports over-resolve for namespace languages (C#/Go up to 50×)
and under-resolve for others (Go/Rust/Kotlin/Swift); resolved CALLS are the FAIRER cross-language signal
(folded with inheritance + construction as of the symbol-use-recall work). Both are CAPPED so neither a
namespace import fan-out nor a hot method name can balloon the bill; for BILLING we resolve calls
PRECISION-biased (STOP-listed common names + ≤3-def fan-out) so an ambiguous call does not over-count —
the OPPOSITE bias from collision detection, where recall wins.

WHY A CO-CHANGE TERM. Veripsa is a TWO-detector product: the structural graph (lines that exist) PLUS git
co-change (files that change together with NO line between them — the coupling the graph is blind to). The
meter reflects both: `cochange_blind` counts file pairs that co-change in history yet have no import/call
edge, LIFT-corrected so a file touched every commit can't fabricate coupling, and CAPPED.

WHY A GLOBAL CAP. `xs_intensity` stays UNCAPPED on purpose — cross-substrate is the language-agnostic crown
jewel and should DRIVE the bill — but CAP_PREMIUM bounds the TOTAL so no pathological graph can run the bill
away. (Veripsa never blocks; the cap bounds price, it does not stop analysis.)

CONTENT-FREE. This consumes ONLY build_graph's output (paths, edge kinds, line counts), a sum of file line
counts, and — for the co-change term — git history FILENAMES ONLY (never a diff/body). It never reads,
returns, or prints a single line of code body. AGGREGATE SCALARS ONLY leave this module — never a per-edge /
per-pair list (the moat rule). The formula + coefficients stay INTERNAL/tunable; surfaces show the Units
NUMBER only.

PURE / OFFLINE. No Postgres, no network. `units_from_graph(graph, loc, cochange_blind_density=…)` is a
deterministic function of its inputs; `compute_units(repo_path)` is the convenience wrapper that runs the
engine, counts LOC, and derives the co-change scalar from local `git log` (fail-soft → 0.0 with no git).
"""
from __future__ import annotations

import collections
import itertools
import os
import subprocess
import sys

# Reuse Veripsa's own extraction engine (the SAME one shipped to customers) — see code_graph_extract.build_graph.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_graph_extract as X  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# PROVISIONAL, owner-tunable — calibrate from real usage; NOT final.
#   (PO: 「係数はまだ固定しない」.) These weights + caps are placeholders chosen to be SANE, not fitted. They get
#   centrally calibrated from the pooled component distribution across many real repos. Changing them re-prices
#   the meter — keep them in this ONE labeled dict so a tuner edits a single place, never the math below.
#   a_xs is heaviest ON PURPOSE: cross-substrate is language-agnostic (name-based) and is the crown-jewel signal.
#   Every NON-xs term is CAPPED, and CAP_PREMIUM bounds the whole premium so the bill can never run away.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
_UNIT_POLICY = {
    "a_xs":        2.0,    # weight on cross-substrate intensity (shared tables/config + migration<->query), per file — UNCAPPED (the crown jewel drives the bill)
    "a_call":      0.35,   # weight on resolved cross-file call density, per file (its input is CAPPED below) — the FAIRER cross-language structural signal
    "a_xdir":      0.3,    # weight on cross-directory import density, per file (its input is CAPPED below)
    "a_blast":     0.03,   # weight on mean coupling-graph breadth (its input is CAPPED below)
    "a_hub":       0.15,   # weight on the bucketed hub/contention concentration {0,1,2,3}
    "a_cc":        0.5,    # weight on graph-blind co-change density, per file (its input is CAPPED below) — the 2nd detector
    "CAP_CALL":    3.0,    # cap on call_density before weighting — bounds a hot method name / fan-out
    "CAP_XDIR":    3.0,    # cap on xdir_density before weighting — bounds a namespace-language import fan-out
    "CAP_BLAST":  10.0,    # cap on blast_mean before weighting — bounds a namespace-language import fan-out
    "CAP_CC":      2.0,    # cap on cochange_blind density before weighting — bounds a churny history
    "CAP_PREMIUM": 4.0,    # GLOBAL cap on the total premium — bounds the bill no matter how pathological the graph
    "HUB_DEGREE":  8,      # in-degree (or shared-resource toucher count) above which a node is a "hub" / "hot"
}

# Common method/verb names whose CALL edges are too ambiguous to bill on (would over-count). Mirrors the
# extractor's recall-vs-precision posture but biased to PRECISION here (a bill must not over-count). Same list
# used by the schema/SQL probe. Lower-cased; we also drop dunder names and any name defined in >3 files.
_STOP = {"main", "run", "setup", "teardown", "handle", "dispatch", "register", "wrap", "to_s", "to_str",
         "tostring", "str", "repr", "inspect", "format", "print", "println", "puts", "log", "warn", "debug",
         "trace", "equals", "eql?", "hash", "hashcode", "compareto", "compare", "cmp", "empty?", "blank?",
         "present?", "nil?", "valid?", "include?", "contains", "respond_to?", "key?", "has_key?", "to_a",
         "to_h", "to_sym", "to_i", "to_json", "map", "each", "collect", "select", "reject", "filter", "reduce",
         "merge", "flatten", "zip", "freeze", "dup", "clone", "tap", "then", "send", "call", "apply", "yield",
         "it", "its", "describe", "context", "before", "after", "around", "expect", "should", "assert",
         "refute", "let", "subject", "mock", "stub"}


def _hub_bucket(n: int) -> int:
    """Bucket a hub/contention count into {0,1,2,3} so a single mega-hub can't dominate the premium linearly."""
    return 0 if n == 0 else 1 if n <= 3 else 2 if n <= 10 else 3


def _resolved_file_pairs(graph: dict):
    """INTERNAL: resolved file<->file structural pairs — (import_pairs, call_pairs), each a set of
    frozenset({fileA, fileB}). NEVER surfaced: used only to derive aggregate densities and to mark a
    co-change pair "graph-blind". (Moat: aggregate scalars leave this module; these pair SETS do not.)

    Calls are resolved PRECISION-biased for billing: a called name maps to its def file(s) only when it is
    not a STOP-listed common name, not a dunder, and defined in ≤3 files — so an ambiguous call cannot
    over-count the bill (the opposite bias from collision detection, where recall wins)."""
    nodes, edges = graph.get("nodes", []), graph.get("edges", [])
    files = {n["path"] for n in nodes if n.get("kind") == "file"}

    import_pairs = set()
    for e in edges:
        if (e.get("kind") == "imports" and e.get("dst") in files and e.get("src") in files
                and e.get("src") != e.get("dst")):
            import_pairs.add(frozenset((e["src"], e["dst"])))

    defs = {}
    for n in nodes:
        if n.get("kind") in ("def", "class") and n.get("name"):
            nm = n["name"]
            if not nm.startswith("__") and nm.lower() not in _STOP:
                defs.setdefault(nm, set()).add(n["path"])
    defs_ok = {nm: fs for nm, fs in defs.items() if len(fs) <= 3}
    call_pairs = set()
    for e in edges:
        if e.get("kind") == "calls" and e.get("src") in files:
            for f in defs_ok.get(e.get("dst"), ()):
                if f in files and f != e["src"]:
                    call_pairs.add(frozenset((e["src"], f)))
    return import_pairs, call_pairs


def loc_of(repo: str, paths) -> int:
    """Sum the line counts of the given files (content-free: counts newlines, never inspects content)."""
    total = 0
    for p in paths:
        try:
            with open(os.path.join(repo, p), "rb") as fh:
                total += sum(1 for _ in fh)
        except Exception:
            # a path that vanished / is unreadable contributes 0 lines — never crash the meter
            pass
    return total


def graph_blind_cochange_density(repo: str, graph: dict, max_commits: int = 1500,
                                 max_files_per_commit: int = 60, min_co: int = 3,
                                 min_lift: float = 2.0) -> float:
    """OFFLINE 2nd-detector term: density of file pairs that CO-CHANGE in git history with NO structural edge
    between them — the "graph-blind" coupling co-change uniquely catches — normalized per in-scope file.

    Content-free: reads git FILENAMES ONLY (`git log --name-only`), never a diff or body. LIFT-corrected
    (lift = co·N / (n_a·n_b)) so a file touched almost every commit can't fabricate coupling. BOUNDED: a
    capped commit sample, per-commit file cap (skip bulk/refactor commits that fabricate cliques), and a
    pre-filter to files frequent enough to possibly qualify. FAIL-SOFT → 0.0 (no git / shallow / timeout /
    error). No Postgres."""
    nodes = graph.get("nodes", [])
    files = {n["path"] for n in nodes if n.get("kind") == "file"}
    nf = len(files)
    if nf == 0:
        return 0.0
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", "--no-merges", f"-n{max_commits}", "--name-only",
             "--pretty=format:%x00"],
            capture_output=True, text=True, timeout=30)
    except Exception:
        return 0.0
    if out.returncode != 0 or not out.stdout:
        return 0.0

    # parse: each commit chunk is its file list (filenames only); keep only in-scope files; drop bulk commits.
    commits = []
    for chunk in out.stdout.split("\x00"):
        fs = {ln.strip() for ln in chunk.splitlines() if ln.strip() in files}
        if 2 <= len(fs) <= max_files_per_commit:
            commits.append(fs)
    N = len(commits)
    if N < min_co:
        return 0.0

    # a pair's co-count can't exceed either endpoint's frequency, so only files seen >= min_co times can ever
    # qualify — pre-filtering to them is both a correctness-preserving filter AND a memory/CPU bound.
    freq = collections.Counter()
    for fs in commits:
        freq.update(fs)
    active = {f for f, c in freq.items() if c >= min_co}
    if len(active) < 2:
        return 0.0

    co = collections.Counter()
    for fs in commits:
        af = sorted(f for f in fs if f in active)
        for a, b in itertools.combinations(af, 2):
            co[(a, b)] += 1

    import_pairs, call_pairs = _resolved_file_pairs(graph)
    structural = import_pairs | call_pairs

    blind = 0
    for (a, b), c in co.items():
        if c < min_co:
            continue
        if c * N < min_lift * freq[a] * freq[b]:   # lift = c*N/(freq_a*freq_b) >= min_lift, integer-safe
            continue
        if frozenset((a, b)) in structural:
            continue
        blind += 1
    return blind / nf


def units_from_graph(graph: dict, loc: int, policy: dict = None,
                     cochange_blind_density: float = 0.0) -> dict:
    """PURE meter: compute Veripsa Units from an already-built code graph + an in-scope LOC total.

    Args:
        graph:  the dict returned by code_graph_extract.build_graph — {"nodes": [...], "edges": [...]}.
        loc:    in-scope LOC (sum of the file nodes' line counts); the language-agnostic billing base.
        policy: optional override of _UNIT_POLICY (owner-tunable coefficients/caps); defaults to _UNIT_POLICY.
        cochange_blind_density: the graph-blind co-change density (per file) — a precomputed SCALAR so this
            function stays pure (compute_units derives it from git history offline). Defaults to 0.0.

    Returns (content-free, aggregate scalars only):
        {"units": int, "loc": int, "premium": float, "premium_capped": bool,
         "components": {"xs_intensity","call_density","xdir_density","blast_mean","hub_bucket","cochange_blind_density"}}
    """
    P = dict(_UNIT_POLICY)
    if policy:
        P.update(policy)
    HUB = int(P["HUB_DEGREE"])

    nodes, edges = graph.get("nodes", []), graph.get("edges", [])
    files = {n["path"] for n in nodes if n.get("kind") == "file"}
    nf = len(files)
    d = os.path.dirname

    # ── resolved file<->file imports + calls (the entanglement core). Imports: cross-DIRECTORY share is what
    #    folders are blind to. Calls: the FAIRER cross-language structural signal (precision-biased for billing).
    import_pairs, call_pairs = _resolved_file_pairs(graph)
    n_xdir = sum(1 for p in import_pairs if len({d(x) for x in p}) == 2)
    xdir_density = (n_xdir / nf) if nf else 0.0
    call_density = (len(call_pairs) / nf) if nf else 0.0

    # ── cross-substrate: shared tables/config-keys (>=2 distinct file touchers) + migration<->query table pairs.
    #    NAME-BASED, hence LANGUAGE-AGNOSTIC → the crown-jewel term, weighted most (a_xs) and UNCAPPED.
    res_src, res_kind = {}, {}
    for e in edges:
        if e.get("kind") in ("queries", "alters", "reads_config") and e.get("src") in files:
            res_src.setdefault(e["dst"], set()).add(e["src"])
            res_kind.setdefault(e["dst"], set()).add(e["kind"])
    shared_res = sum(1 for s in res_src.values() if len(s) >= 2)
    mq_pairs = sum(1 for k in res_kind.values() if "alters" in k and "queries" in k)
    xs_intensity = ((shared_res + mq_pairs) / nf) if nf else 0.0   # per-file, so it does not double-count LOC scale

    # ── hub / contention concentration (bucketed): hub files (import in-degree>HUB) + hot resources (>HUB touchers).
    imp_indeg = {}
    for e in edges:
        if e.get("kind") == "imports" and e.get("dst") in files:
            imp_indeg.setdefault(e["dst"], set()).add(e["src"])
    hub_files = sum(1 for s in imp_indeg.values() if len(s) > HUB)
    hot_res = sum(1 for s in res_src.values() if len(s) > HUB)
    hub_bucket = _hub_bucket(hub_files + hot_res)

    # ── blast-radius mean breadth: undirected adjacency over resolved imports + non-hot shared resources.
    #    (Hot resources are excluded from adjacency so one mega-shared file can't fabricate a dense clique.)
    adj = {}

    def link(a, b):
        if a != b:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)

    for p in import_pairs:
        a, b = tuple(p)
        link(a, b)
    for s in res_src.values():
        if 2 <= len(s) <= HUB:
            members = list(s)
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    link(members[i], members[j])
    blast_mean = (sum(len(v) for v in adj.values()) / nf) if nf else 0.0

    # ── premium: UNCAPPED language-agnostic xs term (crown jewel) + every other term CAPPED + a GLOBAL cap.
    premium_raw = (1.0
                   + P["a_xs"]    * xs_intensity
                   + P["a_call"]  * min(call_density,           P["CAP_CALL"])
                   + P["a_xdir"]  * min(xdir_density,           P["CAP_XDIR"])
                   + P["a_blast"] * min(blast_mean,             P["CAP_BLAST"])
                   + P["a_hub"]   * hub_bucket
                   + P["a_cc"]    * min(cochange_blind_density, P["CAP_CC"]))
    premium = min(premium_raw, P["CAP_PREMIUM"])
    units = int(loc * premium)

    return {
        "units": units,
        "loc": int(loc),
        "premium": round(premium, 4),
        "premium_capped": premium_raw > P["CAP_PREMIUM"] + 1e-9,
        "components": {
            "xs_intensity": round(xs_intensity, 4),
            "call_density": round(call_density, 4),
            "xdir_density": round(xdir_density, 4),
            "blast_mean": round(blast_mean, 4),
            "hub_bucket": hub_bucket,
            "cochange_blind_density": round(cochange_blind_density, 4),
        },
    }


def compute_units(repo_path: str, policy: dict = None) -> dict:
    """Convenience wrapper: run Veripsa's engine on `repo_path`, count in-scope LOC, derive the graph-blind
    co-change scalar from local git history (filenames only, fail-soft), and return the meter result.

    Content-free: builds the graph (paths/edges only), sums file line counts, and reads git FILENAMES only;
    never reads code bodies into the result. Returns the same shape as units_from_graph plus "files"."""
    graph = X.build_graph(repo_path)
    files = sorted({n["path"] for n in graph.get("nodes", []) if n.get("kind") == "file"})
    loc = loc_of(repo_path, files)
    ccd = graph_blind_cochange_density(repo_path, graph)
    out = units_from_graph(graph, loc, policy=policy, cochange_blind_density=ccd)
    out["files"] = len(files)
    return out


def main(argv):
    """CLI sanity eyeball: print AGGREGATE Units for each repo path (no coefficients, no per-pair lists)."""
    paths = [a for a in argv[1:] if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    for p in paths:
        try:
            r = compute_units(p)
        except Exception as e:  # never crash the meter on one bad repo
            print(f"!! {p}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        c = r["components"]
        cap = " (CAPPED)" if r.get("premium_capped") else ""
        print(f"{os.path.basename(p.rstrip('/')):<24} files={r['files']:>5}  loc={r['loc']:>8}  "
              f"premium={r['premium']:>6}{cap}  Veripsa Units={r['units']:>10,}  "
              f"[xs/f={c['xs_intensity']} call={c['call_density']} xdir={c['xdir_density']} "
              f"blast={c['blast_mean']} hub={c['hub_bucket']} cc={c['cochange_blind_density']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
