#!/usr/bin/env python3
"""DRILL: categorize the incident pairs NEITHER detector covers, on REAL paths (content-free).

Reuses combined_recall's EXACT building blocks (same graph, same dampened adjacency, same incident
ground truth) so the numbers line up with the panel. For the missed pairs it prints a content-free
breakdown to root-cause WHAT couples them (path tokens / ext-tier / file-role only — never a body):

  - recoverable      : a RAW graph edge exists but dampening/fan-out dropped it (which dampener?).
  - xtier            : backend-ext <-> frontend-ext (the client/server contract vein).
  - shared_token     : different dirs, share a distinctive domain token in the basename (sibling
                       feature-module / value coupling, e.g. order_service.py <-> OrderTable.tsx).
  - role_file        : at least one endpoint is a types/models/schema/dto/const/config/index/route
                       file (a contract/registry hub that couples by shape, not by edge).
  - other            : none of the above.

Usage: python3 tests/drill_misses.py /path/to/repo [TOP]
"""
import os
import re
import sys

import combined_recall as CR

STOPTOK = {
    "src", "lib", "app", "index", "main", "test", "tests", "spec", "util", "utils", "common",
    "core", "api", "client", "server", "service", "services", "components", "component", "pages",
    "page", "types", "type", "model", "models", "handler", "handlers", "controller", "controllers",
    "ts", "js", "tsx", "jsx", "py", "go", "vue", "svelte", "new", "old", "the", "and", "for", "use",
    "get", "set", "data", "config", "const", "constants", "route", "routes", "view", "views", "store",
}
ROLE_RE = re.compile(r"(types?|models?|schema|dto|const|constants|config|index|route|routes|api|enums?|"
                     r"interface|interfaces|contract|registry|store|reducer|actions?)", re.I)
BACKEND = {".py", ".go", ".rb", ".java", ".kt", ".cs", ".php", ".rs", ".ex", ".exs"}
FRONTEND = {".ts", ".tsx", ".js", ".jsx", ".svelte", ".vue"}


def _ext(p):
    i = p.rfind(".")
    return p[i:].lower() if i >= 0 else ""


def _topdir(p):
    parts = p.split("/")
    return parts[0] if len(parts) > 1 else ""


def _tokens(p):
    base = p.rsplit("/", 1)[-1]
    base = base[: base.rfind(".")] if "." in base else base
    toks = {t.lower() for t in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", base) if t}
    return {t for t in toks if len(t) >= 4 and t not in STOPTOK}


def _which_damper(a, b, g, files, hubs, rhubs):
    """If a raw edge exists between a,b, name the dampener that dropped it (best-effort, content-free)."""
    for e in g["edges"]:
        s, d, k = e.get("src"), e.get("dst"), e.get("kind")
        if {s, d} != {a, b}:
            continue
        if k == "imports" and d in hubs:
            return f"hub-import(indeg>{CR.HUB_DEGREE})"
        if k in ("queries", "alters", "reads_config") and d in rhubs:
            return f"res-hub(indeg>{CR.HUB_DEGREE})"
        if k == "calls":
            return "calls(>3-fanout or hub)"
        return f"present({k})-but-paired-out"
    return "no-raw-edge"


def main():
    repo = sys.argv[1]
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    g = CR.X.build_graph(repo)
    files = {n["path"] for n in g["nodes"] if n["kind"] == "file"}
    graph_pairs = CR.graph_adjacency(g, files)
    cochange_pairs = CR.cochange_adjacency(repo, files)
    combined = graph_pairs | cochange_pairs
    incidents, _ = CR.incident_pairs(repo, files)
    raw_edge = set()
    for e in g["edges"]:
        if e["src"] in files and e["dst"] in files and e["src"] != e["dst"]:
            raw_edge.add(frozenset((e["src"], e["dst"])))
    hubs = CR.hub_files(g, files)
    rhubs = CR.res_hubs(g, files)
    missed = [tuple(sorted(k)) for k in incidents if k not in combined]

    cats = {"recoverable": [], "xtier": [], "shared_token": [], "role_file": [], "other": []}
    for a, b in missed:
        if frozenset((a, b)) in raw_edge:
            cats["recoverable"].append((a, b, _which_damper(a, b, g, files, hubs, rhubs)))
            continue
        ea, eb = _ext(a), _ext(b)
        if (ea in BACKEND and eb in FRONTEND) or (ea in FRONTEND and eb in BACKEND):
            cats["xtier"].append((a, b, f"{ea}<->{eb}"))
            continue
        shared = _tokens(a) & _tokens(b)
        if shared and _topdir(a) != _topdir(b):
            cats["shared_token"].append((a, b, ",".join(sorted(shared))))
            continue
        if ROLE_RE.search(a.rsplit("/", 1)[-1]) or ROLE_RE.search(b.rsplit("/", 1)[-1]):
            cats["role_file"].append((a, b, "role"))
            continue
        cats["other"].append((a, b, ""))

    n = len(missed) or 1
    print(f"\n{os.path.basename(repo.rstrip('/'))}: {len(missed)} missed pairs "
          f"(incidents {len(incidents)}, combined-covered {len(incidents) - len(missed)})")
    print("-" * 88)
    for c in ("recoverable", "xtier", "shared_token", "role_file", "other"):
        print(f"  {c:14s} {len(cats[c]):4d}  ({len(cats[c])*100//n:3d}%)")
    for c in ("recoverable", "xtier", "shared_token", "role_file"):
        if cats[c]:
            print(f"\n  --- {c} (top {min(top,len(cats[c]))}) ---")
            for a, b, tag in cats[c][:top]:
                print(f"    [{tag}]  {a}  <->  {b}")


if __name__ == "__main__":
    main()
