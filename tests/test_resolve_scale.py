#!/usr/bin/env python3
"""Import-resolution SCALE gate (no DB): the extractor's #1 throughput cost.

WHY THIS GATE EXISTS (audit2/scale): every push that takes the FULL path (cold-start install, force-push,
rebase, or a >cap diff) re-extracts the WHOLE repo, and the single webhook worker drains events SERIALLY —
so build_graph's wall time IS the worker's per-event ceiling. _resolve_imports was the dominant cost: it
scanned the ENTIRE file universe with `endswith` for EVERY import edge → O(edges × files). MEASURED on a real
mid-size repo (Django: ~3.4k files, ~110k import edges): ~2.5e8 endswith calls, ~83% of build_graph's time,
~170s wall on a fast box (minutes on the 512MiB tier). A handful of rebases would then stall the worker and
spiral the bounded queue → 503 → GitHub redeliver → backlog.

The fix replaces the per-edge scan with a precomputed SUFFIX INDEX (by_suffix). This gate proves TWO things:
  1. EQUIVALENCE — the indexed resolver returns byte-identical edges to a brute-force reference scan that
     re-implements the original `ne == mod OR ne.endswith('/'+mod)` (+ /index, /__init__) semantics, over a
     deliberately adversarial fixture (deep nesting, ambiguous basenames, dir-imports, relative imports,
     cross-language collisions). A future refactor that breaks resolution fails here.
  2. SCALE — resolution over a LARGE synthetic universe (4000 files × 8000 import edges) completes well under
     a ceiling that the old O(edges × files) scan could never meet. A regression back to the quadratic scan
     blows the budget and fails the gate (so the throughput ceiling can't silently rot).
Content-free: paths / module names only; no repo content.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _cg_resolve as R  # noqa: E402


def _reference_resolve(nodes, edges):
    """A SLOW but OBVIOUSLY-CORRECT reference: re-implement the original module-suffix match as a literal
    full scan with endswith, so the fast suffix-index resolver can be proven equivalent to it. This mirrors
    _cg_resolve._resolve_imports EXCEPT the inner candidate search, which here is the un-optimized scan."""
    files = [n["path"] for n in nodes if n.get("kind") == "file"]
    go_dirs = R._go_pkg_dirs(files)
    by_noext = {}
    alias = {}
    for p in files:
        ne = R._noext(p)
        by_noext.setdefault(ne, p)
        for k in {ne, ne.replace("/", "."), os.path.basename(ne), os.path.basename(p)}:
            if k:
                alias.setdefault(k, set()).add(p)
    out = []
    for e in edges:
        if e.get("kind") != "imports":
            out.append(e)
            continue
        raw = (e.get("dst") or "").strip()
        if not raw:
            out.append(e)
            continue
        cands = set()
        if raw.startswith(("./", "../")):
            tgt = R._noext(os.path.normpath(os.path.join(os.path.dirname(e["src"]), raw)))
            if tgt in by_noext:
                cands = {by_noext[tgt]}
            else:
                cands = {by_noext[k] for k in (tgt + "/index", tgt + "/__init__") if k in by_noext}
        else:
            angle = raw.startswith("<")
            mod = R._mod_noext(raw.lstrip("@~/").strip("<>\"'")).replace(".", "/").strip("/")
            bare_single = "/" not in mod
            if bare_single and (R._no_bare_basename(e["src"]) or angle):
                mod = ""
            if mod:
                for ne, p in by_noext.items():                     # the ORIGINAL O(files) scan (the reference)
                    if (ne == mod or ne.endswith("/" + mod)
                            or ne == mod + "/index" or ne.endswith("/" + mod + "/index")
                            or ne == mod + "/__init__" or ne.endswith("/" + mod + "/__init__")):
                        cands.add(p)
            if not cands and bare_single and mod:
                cands = set(alias.get(mod, set()))
            if not cands and e["src"].endswith(".go"):
                cands = R._resolve_go_pkg(raw, go_dirs)
            cands = {c for c in cands if R._family(c) == R._family(e["src"])}
        cands = set(cands)
        cands.discard(e["src"])
        if cands:
            for tgt in sorted(cands):
                out.append({"src": e["src"], "dst": tgt, "kind": "imports"})
        else:
            out.append(e)
    return out


def _norm(out):
    return sorted((e["src"], e["dst"], e["kind"]) for e in out)


def _adversarial_fixture():
    """A small but tricky universe that exercises EVERY resolution branch (so equivalence is meaningful)."""
    nodes = [{"kind": "file", "path": p} for p in [
        "app/core/config.py", "app/core/__init__.py", "app/utils.py", "app/api/utils.py",
        "pkg/sub/index.ts", "pkg/sub/helper.ts", "web/src/main.tsx", "web/src/lib/config.ts",
        "deep/a/b/c/d/config.py", "myconfig.py",            # myconfig must NOT match a `/config` import
        "svc/internal/auth/auth.go", "svc/internal/auth/token.go",
    ]]
    edges = [
        {"src": "app/api/handler.py", "dst": "app.core.config", "kind": "imports"},   # dotted → app/core/config.py
        {"src": "app/api/handler.py", "dst": "config", "kind": "imports"},            # bare basename fan-out
        {"src": "app/api/handler.py", "dst": "app.core", "kind": "imports"},          # package → __init__
        {"src": "web/src/page.tsx", "dst": "@/lib/config", "kind": "imports"},        # scoped alias, web family
        {"src": "web/src/page.tsx", "dst": "./main", "kind": "imports"},              # relative → web/src/main.tsx
        {"src": "pkg/host.ts", "dst": "pkg/sub", "kind": "imports"},                  # dir import → index.ts
        {"src": "svc/cmd/main.go", "dst": "github.com/org/svc/internal/auth", "kind": "imports"},  # go pkg dir
        {"src": "svc/cmd/main.go", "dst": "fmt", "kind": "imports"},                  # go single-seg stdlib → inert
        {"src": "x/y.py", "dst": "config", "kind": "imports"},                        # cross-check basename
        {"src": "k.py", "dst": "os.path", "kind": "imports"},                         # external dotted → inert
        {"src": "app/api/handler.py", "dst": "utils", "kind": "imports"},             # ambiguous basename → fan-out
        {"src": "c/main.cpp", "dst": "<vector>", "kind": "imports"},                  # angle system include → inert
        {"src": "keep/me.py", "dst": "calls", "kind": "calls"},                       # a non-import edge: pass-through
    ]
    return nodes, edges


def equivalence_check():
    nodes, edges = _adversarial_fixture()
    ref = _norm(_reference_resolve([dict(n) for n in nodes], [dict(e) for e in edges]))
    fast = _norm(R._resolve_imports([dict(n) for n in nodes], [dict(e) for e in edges]))
    ok = ref == fast
    if not ok:
        rs, fs = set(ref), set(fast)
        print("  only in reference:", sorted(rs - fs)[:10])
        print("  only in fast:", sorted(fs - rs)[:10])
    return ("indexed resolver is byte-identical to the brute-force reference on the adversarial fixture", ok)


def scale_check():
    """A LARGE universe must resolve FAST. The old O(edges × files) scan could not meet this budget; the
    suffix index does. 4000 files × 8000 import edges: the quadratic scan would be ~3.2e7 endswith-heavy
    iterations (and on a real repo ~2.5e8) — the index makes it linear-ish in edges."""
    nodes = [{"kind": "file", "path": f"src/module{g % 40}/file{g}.py"} for g in range(4000)]
    edges = [{"src": f"src/caller{i}.py",
              "dst": f"module{(i * 7) % 40}.file{(i * 13) % 4000}", "kind": "imports"}
             for i in range(8000)]
    t0 = time.time()
    out = R._resolve_imports(nodes, edges)
    dt = time.time() - t0
    n_resolved = sum(1 for e in out if e["kind"] == "imports" and e["dst"].endswith(".py")
                     and "/" in e["dst"])
    # Generous ceiling (10x slack over the observed ~0.1s) so the gate is robust on a slow CI box but STILL
    # far below where the quadratic scan would land (the old code spent ~83% of a 170s build here).
    budget = 5.0
    ok = dt < budget
    print(f"  resolved {n_resolved} import edges over 4000 files × 8000 edges in {dt:.3f}s (budget {budget}s)")
    return (f"large-universe resolution stays well under the throughput budget ({dt:.3f}s < {budget}s)", ok)


def _resolve_one(file_paths, src, dst):
    """Resolve ONE import edge against a universe of `file_paths` and return the SET of `dst` values the
    resolver emits for it. A resolved edge's dst is a repo FILE PATH; an inert (stdlib/3rd-party/unresolved)
    edge keeps its raw MODULE NAME. So the returned set is either {repo files…} (resolved) or {module name}
    (inert) — exactly the targets that land on the graph. This lets a precision case assert the EXACT edge
    set (count + targets), which is what catches a phantom fan-out (an extra wrong file) or a regression to
    an inert false edge."""
    nodes = [{"kind": "file", "path": p} for p in file_paths]
    edges = [{"src": src, "dst": dst, "kind": "imports"}]
    out = R._resolve_imports([dict(n) for n in nodes], [dict(e) for e in edges])
    return frozenset(e["dst"] for e in out if e["kind"] == "imports" and e["src"] == src)


def precision_check():
    """FALSE-POSITIVE (over-resolution) audit: a wrong file→file `imports` edge = a coupling Veripsa warns
    about that ISN'T real = cry-wolf = the customer mutes the product. This pins the EXACT resolved edge set
    for every scenario that TEMPTS over-resolution, so a future refactor that loosens the match (re-introducing
    fan-out, a last-segment match, a cross-language edge, or an invented edge for stdlib) fails HERE.

    The resolver is RECALL-BIASED by design (docstring): a BARE single-segment basename (`import helper`) is
    genuinely ambiguous and intentionally fans out to every same-name file — that is correct, not a false
    positive, and is asserted as such below. The precision boundary is everything that is NOT a bare basename:
    a DOTTED/path import must hit its ONE path-suffix target (no last-segment fan-out), an EXTERNAL name must
    stay inert (no invented edge), and a match must never cross a language family.

    Each case: (why, file universe, src, raw import, EXACT expected dst set). Empty `{raw}` = stays inert."""
    cases = [
        # ── same basename in MULTIPLE dirs ──────────────────────────────────────────────────────────────
        # A DOTTED import disambiguates → the ONE path-suffix target, never a fan-out to all three.
        ("dotted `b.helper` → ONLY b/helper.py (not a/ or c/helper.py)",
         ["a/helper.py", "b/helper.py", "c/helper.py"], "consumer.py", "b.helper", {"b/helper.py"}),
        ("dotted `c.helper` → ONLY c/helper.py",
         ["a/helper.py", "b/helper.py", "c/helper.py"], "consumer.py", "c.helper", {"c/helper.py"}),
        # The BARE basename IS intentionally ambiguous → fans out to all three (recall-bias, documented).
        # Asserting the full set here is the guard that this stays EXACTLY the same-name files (no extras,
        # and never a different-basename file like myhelper.py).
        ("bare `helper` → recall fan-out to EXACTLY the three helper.py (intended; not myhelper.py)",
         ["a/helper.py", "b/helper.py", "c/helper.py", "z/myhelper.py"], "consumer.py", "helper",
         {"a/helper.py", "b/helper.py", "c/helper.py"}),
        # ── dotted / package paths resolve to the right path, NOT a last-segment match elsewhere ─────────
        ("`pkg.sub.helper` → pkg/sub/helper.py, NOT the decoy x/helper.py (no last-segment match)",
         ["pkg/sub/helper.py", "x/helper.py"], "src.py", "pkg.sub.helper", {"pkg/sub/helper.py"}),
        ("`app.core.config` (specific) → ONLY app/core/config.py, not other/core/config.py",
         ["app/core/config.py", "other/core/config.py"], "main.py", "app.core.config", {"app/core/config.py"}),
        ("package `app.core` → app/core/__init__.py ONLY (not the sibling app/core/config.py)",
         ["app/core/__init__.py", "app/core/config.py"], "main.py", "app.core", {"app/core/__init__.py"}),
        # ── segment-anchored: a trailing /config must NOT match a substring like myconfig / preconfig ─────
        ("`app.config` is segment-anchored → app/config.py, never myconfig.py / preconfig.py",
         ["app/config.py", "myconfig.py", "preconfig.py"], "main.py", "app.config", {"app/config.py"}),
        # ── a bare name that is a STDLIB / 3rd-party name (no repo file) → NO invented internal edge ──────
        ("`import os` (stdlib) → stays inert, no file→file edge invented",
         ["app.py", "helper.py"], "app.py", "os", {"os"}),
        ("`import json` (stdlib) → inert even though unrelated repo files exist",
         ["app.py", "models.py"], "app.py", "json", {"json"}),
        ("external scoped `@mui/material` → inert (tail must not basename-match a local file)",
         ["web/material/index.ts", "web/page.tsx"], "web/page.tsx", "@mui/material", {"@mui/material"}),
        ("EXTERNAL dotted `_typeshed.wsgi` → inert; NO fallback to the last-segment tests/wsgi.py",
         ["tests/wsgi.py"], "app.py", "_typeshed.wsgi", {"_typeshed.wsgi"}),
        ("2-segment external `lib.parse` (no lib/ dir) → inert; no basename fallback to parse.py",
         ["tests/parse.py"], "app.py", "lib.parse", {"lib.parse"}),
        # ── cross-language same-name files → NO cross-language edge (same family only) ───────────────────
        ("py importer `helper` → ONLY helper.py, never the same-name helper.go",
         ["x/helper.py", "y/helper.go"], "consumer.py", "helper", {"x/helper.py"}),
        ("ruby `require 'helper'` → ONLY helper.rb, never helper.py",
         ["lib/helper.rb", "app/helper.py"], "main.rb", "helper", {"lib/helper.rb"}),
        ("Java FQN `com.example.Helper` → the .java FILE, never the cross-language Helper.py",
         ["jsrc/com/example/Helper.java", "other/Helper.py"], "App.java", "com.example.Helper",
         {"jsrc/com/example/Helper.java"}),
        ("Kotlin `com.x.Service` → ONLY the .kt file, never the same-FQN .java (distinct families)",
         ["ksrc/com/x/Service.kt", "jsrc/com/x/Service.java"], "App.kt", "com.x.Service",
         {"ksrc/com/x/Service.kt"}),
        # ── deep / ambiguous package paths (Go pkg = DIR): most-specific dir, never fan-out to a shallow one
        ("Go `.../internal/auth` → the specific pkg DIR's files, NOT a shallow top-level auth/",
         ["auth/auth.go", "svc/internal/auth/auth.go", "svc/internal/auth/token.go"],
         "cmd/main.go", "github.com/org/svc/internal/auth",
         {"svc/internal/auth/auth.go", "svc/internal/auth/token.go"}),
        ("Go external pkg `go.uber.org/zap` (no matching dir) → inert",
         ["svc/internal/auth/auth.go"], "cmd/main.go", "go.uber.org/zap", {"go.uber.org/zap"}),
        ("Go single-segment `fmt` (stdlib) → inert, never a suffix-match to a local fmt.go",
         ["util/fmt/fmt.go", "cmd/main.go"], "cmd/main.go", "fmt", {"fmt"}),
        # ── aliased / re-exported & relative imports stay exact, no cross-language collision ─────────────
        ("aliased `@/utils` (web) → the web utils.ts, never the backend utils.py",
         ["web/src/utils.ts", "backend/utils.py", "web/src/page.tsx"], "web/src/page.tsx", "@/utils",
         {"web/src/utils.ts"}),
        ("relative `./main` → the importer's own dir main.tsx, never a backend main.py (cross-lang)",
         ["web/src/main.tsx", "backend/main.py"], "web/src/page.tsx", "./main", {"web/src/main.tsx"}),
        ("relative `./nope` (no such sibling) → inert, no basename fallback anywhere",
         ["web/src/main.tsx", "other/nope.tsx"], "web/src/page.tsx", "./nope", {"./nope"}),
        # ── C/C++ <system> include vs "local" quote, and the self-edge guard ─────────────────────────────
        ("C angle `<vector>` (single-seg system header) → inert, never a docs/vector.cpp match",
         ["docs/vector.cpp", "c/main.cpp"], "c/main.cpp", "<vector>", {"<vector>"}),
        ("C quoted `\"util.h\"` (local) → resolves to the sibling header",
         ["ccsrc/util.h", "ccsrc/widget.cpp"], "ccsrc/widget.cpp", '"util.h"', {"ccsrc/util.h"}),
        ("self-edge guard: helper.py `import helper` → the OTHER helper.py, never itself",
         ["app/helper.py", "other/helper.py"], "app/helper.py", "helper", {"other/helper.py"}),
    ]
    ok = True
    for why, files, src, dst, expected in cases:
        got = _resolve_one(files, src, dst)
        if got != frozenset(expected):
            ok = False
            print(f"  [precision MISMATCH] {why}")
            print(f"      import {dst!r} from {src!r}")
            print(f"      expected dst set: {sorted(expected)}")
            print(f"      got dst set:      {sorted(got)}")
            phantom = sorted(got - frozenset(expected))
            missing = sorted(frozenset(expected) - got)
            if phantom:
                print(f"      PHANTOM (false-positive) edges: {phantom}")
            if missing:
                print(f"      MISSING (recall lost) edges:    {missing}")
    return ("import resolution emits EXACTLY the correct edge set on every over-resolution-tempting "
            "scenario (no phantom fan-out, no last-segment / cross-language / stdlib false edge; "
            "recall-biased bare-basename fan-out is intact)", ok)


def main():
    checks = [equivalence_check(), scale_check(), precision_check()]
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("RESOLVE-SCALE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
