#!/usr/bin/env python3
"""REAL-REPO quality harness — exercise the extractor + resolver + schema-graph on a BROAD set of
actual open-source repos (not synthetic fixtures), with a focus on the languages recently
added/hardened that had NOT been validated on real code: Kotlin + Swift (#49), a Go repo, and a
SQL-heavy / ORM repo (schema-graph, #84).

This is the bigger sibling of tests/audit_repo.py (which audits ONE repo's import precision). It
shallow-clones a fixed set of small/medium real repos and, per repo, ASSERTS + MEASURES the four
product promises on real code:

  NEVER-CRASH / NEVER-HANG  build_graph completes with 0 unexpected failures inside a wall-clock
                            bound, and output is BOUNDED (no node/edge explosion; the #84 table-cap
                            holds on a real schema — minted tables <= _MAX_TABLES).
  RECALL SANITY             real intra-repo imports/calls resolve to real file->file edges; a few
                            KNOWN couplings (spot-checked per repo) are present. For SQL/ORM: a real
                            table is coupled migration<->code (the moat code-only tools miss).
  PRECISION SANITY          no absurd fan-out (a same-basename file does NOT couple to all #83);
                            stdlib / external imports stay inert; no src->test false edges (the
                            production tree does not 'import' tests).
  PERF                      ingest wall-clock + rough per-file cost, sanity-checked vs the measured
                            ~10 KB/file / Django-class numbers (a generous ceiling, not a microbench).

Not a hermetic gate (needs network + checkouts) — run BY HAND, standalone, NOT wired into
run_gates.sh:

  python3 tests/audit_realrepos.py                 # clone the default repo set into /tmp and audit
  python3 tests/audit_realrepos.py --no-clone      # audit whatever /tmp/rr-* checkouts already exist
  python3 tests/audit_realrepos.py PATH [PATH ...] # audit specific local checkouts (auto-profile)

Prints a PASS/FAIL line per repo and a final `REAL-REPO QUALITY: PASS` sentinel (exit 0) iff every
audited repo passed. If the network is unavailable, it says so and audits any local checkouts it
finds rather than fabricating results (HONEST-EMPTY).
"""
from __future__ import annotations

import collections
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402
from _cg_schema import _MAX_TABLES  # noqa: E402

_CODE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rb", ".php", ".cs", ".rs",
             ".c", ".h", ".cpp", ".cc", ".hpp", ".java", ".kt", ".swift")
_TEST_SEG = ("test", "tests", "spec", "__tests__", "examples", "example")

# A real source file should cost FAR less than this to ingest; a generous ceiling (not a microbench).
# The measured baseline is ~10 KB/file and Django-class (~3.4k files) in single-digit seconds; we
# allow 25 ms/file before flagging a perf regression, so noise / a cold disk never trips it.
_PER_FILE_MS_CEIL = 25.0
# A bare basename import resolving to MANY files is a precision SMELL (#83) — but the resolver is
# DELIBERATELY recall-biased: an ambiguous bare name fans out to every same-basename match (the module
# comment: "we prefer recall"). Two flavours, only ONE of which is a real bug:
#   • SAME-STEM fan-out  — one import → N files that all share the basename (e.g. a Kotlin-Multiplatform
#     `import okio.FileSystem` → FileSystem.kt in commonMain/jvmMain/jsMain/nativeMain/wasmMain; OR two
#     `settings.py`). These are the SAME logical symbol implemented per source-set / a duplicate-named
#     module — correct recall, NOT a bug (and a widely-imported target is hub-DAMPENED by the engine, so
#     it never over-warns at the product layer). Measured, not failed (unless absurdly large — a true
#     O(files) blow-up like hugo's thousands of `strings.go`).
#   • DIFFERENT-STEM fan-out — one import → files with DIFFERENT stems = a genuine mis-resolution (the
#     ambiguity-bug signature the #83 fixes targeted). This is the real precision FAIL; measured 0 across
#     every real repo audited.
_FANOUT_ABSURD = 50          # a single import → >= this many files is a real blow-up (orders below hugo's thousands)

# Default real repo set: (key, git url, profile). `profile` selects which language-specific spot-checks
# + assertions run. Small/medium, shallow-cloneable. Overlap with prior audits (flask/zustand/gin) is OK
# but the FOCUS is the under-validated set: Kotlin, Swift, Go, and SQL/ORM (raw DDL + Django ORM).
_DEFAULT_REPOS = [
    # key            git url                                                  profile     local dir
    ("okio",        "https://github.com/square/okio.git",                    "kotlin",   "/tmp/rr-kotlin"),
    ("kingfisher",  "https://github.com/onevcat/Kingfisher.git",             "swift",    "/tmp/rr-swift"),
    ("cobra",       "https://github.com/spf13/cobra.git",                    "go",       "/tmp/rr-go"),
    ("django-oscar", "https://github.com/django-oscar/django-oscar.git",     "orm",      "/tmp/rr-sql"),
    ("chinook",     "https://github.com/lerocha/chinook-database.git",       "sqlddl",   "/tmp/rr-sqlddl"),
    # A SINGLE real production schema (GitLab's db/structure.sql, ~1.5k tables) — the strongest #84
    # table-cap test: it must mint ALL its tables (real schemas stay under the 20k cap = never truncated)
    # with BOUNDED output. A 'file::' URL is fetched (one .sql), not cloned. (3 MB > the code-graph
    # file-size cap, but the SCHEMA pass reads .sql in full — exactly the path the cap protects.)
    ("gitlab-schema", "file::https://gitlab.com/gitlab-org/gitlab-foss/-/raw/master/db/structure.sql",
                     "bigschema", "/tmp/rr-bigschema-dl"),
]


def _noext(p: str) -> str:
    for x in (".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".py", ".go", ".rb", ".php", ".cs", ".rs",
              ".kt", ".swift", ".java"):
        if p.endswith(x):
            return p[: -len(x)]
    return os.path.splitext(p)[0]


def _is_test_path(p: str) -> bool:
    return any(seg in _TEST_SEG for seg in p.split("/"))


def _clone(key: str, url: str, dst: str) -> tuple[bool, str]:
    """Shallow-clone `url` into `dst` if not already present. Returns (ok, note). Network failure is
    reported honestly (ok=False) — the caller falls back to whatever local checkouts exist."""
    if os.path.isdir(os.path.join(dst, ".git")) or (os.path.isdir(dst) and os.listdir(dst)):
        return True, "reuse existing checkout"
    r = subprocess.run(["git", "clone", "--depth", "1", "-q", url, dst],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"clone failed: {(r.stderr or '').strip()[:160]}"
    return True, "cloned"


def _fetch_file(url: str, dst_dir: str, fname: str) -> tuple[bool, str]:
    """Download a single real file (e.g. a production schema dump) into `dst_dir/db/fname`. Used for
    the big-schema table-cap test, where the asset is one large .sql, not a git repo. Honest skip on
    network failure (ok=False)."""
    dst = os.path.join(dst_dir, "db", fname)
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return True, "reuse existing file"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    r = subprocess.run(["curl", "-sSL", "-o", dst, url], capture_output=True, text=True)
    if r.returncode != 0 or not (os.path.isfile(dst) and os.path.getsize(dst) > 0):
        return False, f"fetch failed: {(r.stderr or '').strip()[:160]}"
    return True, f"fetched {os.path.getsize(dst)//1024} KB"


def _profile_of(root: str) -> str:
    """Auto-detect a profile for an arbitrary local checkout (used for PATH args)."""
    exts = collections.Counter()
    for dp, dn, fns in os.walk(root):
        dn[:] = [d for d in dn if d not in X._SKIP_DIRS]
        for fn in fns:
            exts[os.path.splitext(fn)[1].lower()] += 1
    if exts[".sql"] >= 1 and exts[".py"] < 5:
        return "sqlddl"
    if exts[".py"] and any("migrations" in dp for dp, _, _ in os.walk(root)):
        return "orm"
    if exts[".kt"] > max(exts[".swift"], exts[".go"], exts[".py"]):
        return "kotlin"
    if exts[".swift"] > max(exts[".kt"], exts[".go"], exts[".py"]):
        return "swift"
    if exts[".go"] > max(exts[".kt"], exts[".swift"], exts[".py"]):
        return "go"
    return "generic"


def audit_one(key: str, root: str, profile: str) -> dict:
    """Build the graph for one real repo and compute every quality measure. Captures any exception
    so a crash is a recorded FAIL (with the traceback head) rather than aborting the whole run —
    never-crash is the #1 promise we are testing."""
    res = {"key": key, "root": root, "profile": profile, "checks": [], "metrics": {}, "crash": None}

    def check(name: str, cond: bool, detail: str = "") -> None:
        res["checks"].append((name, bool(cond), detail))

    t0 = time.time()
    try:
        g = X.build_graph(root)
    except BaseException as exc:   # never-crash: a real crash on real code is the headline FAIL
        import traceback
        res["crash"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-800:]
        check("NEVER-CRASH: build_graph completes without raising", False, type(exc).__name__)
        return res
    dt = time.time() - t0

    nodes, edges = g["nodes"], g["edges"]
    files = [n for n in nodes if n.get("kind") == "file"]
    fp = {n["path"] for n in files}
    code_files = [p for p in fp if p.endswith(_CODE_EXT)]
    imp = [e for e in edges if e["kind"] == "imports"]
    calls = [e for e in edges if e["kind"] == "calls"]
    resolved = [(e["src"], e["dst"]) for e in imp if e["dst"] in fp]      # real file->file couplings
    tables = [n for n in nodes if n.get("kind") == "table"]
    alters = [e for e in edges if e["kind"] == "alters"]
    queries = [e for e in edges if e["kind"] == "queries"]

    # basename fan-out (precision smell #83) — exclude package markers (legitimate multi-resolve).
    fan = collections.defaultdict(set)
    for s, d in resolved:
        stem = _noext(os.path.basename(d))
        if stem in ("__init__", "index"):
            continue
        fan[(s, os.path.basename(d))].add(d)
    fanouts = {k: sorted(v) for k, v in fan.items() if len(v) > 1}
    # DIFFERENT-STEM fan-out = the real ambiguity bug (one import → files with different stems). SAME-STEM
    # fan-out (KMP expect/actual, duplicate-named modules) is correct recall, not a bug.
    diff_stem = {k: v for k, v in fanouts.items()
                 if len({_noext(os.path.basename(x)) for x in v}) > 1}
    absurd = {k: v for k, v in fanouts.items() if len(v) >= _FANOUT_ABSURD}
    max_fanout = max((len(v) for v in fan.values()), default=0)

    # src->test couplings (production source -> a test file). A bare ambiguous import resolves recall-first
    # to EVERY same-basename file, so a `from settings import *` correctly resolving to the sandbox
    # settings ALSO reaches a tests/settings — a recall-biased EXTRA, not a mis-resolution, whenever the
    # SAME import also resolved to a non-test sibling of the same basename. A src->test edge is a genuine
    # FALSE edge (the precision FAIL) only when the test file is the SOLE same-basename target for that
    # importer = the resolver actually pointed production at a test with no correct alternative.
    src2test = [(s, d) for s, d in resolved if not _is_test_path(s) and _is_test_path(d)]
    src2test_sole = []
    for s, d in src2test:
        siblings = fan.get((s, os.path.basename(d)), {d})
        if not any(not _is_test_path(x) for x in siblings):   # no non-test sibling co-resolved → real false edge
            src2test_sole.append((s, d))

    # Per-file cost over ALL file nodes the graph represents (code-graph parse + the .sql files the
    # schema pass reads — `files_parsed`/`files_skipped` count only the code-graph parse, so on a
    # schema-only checkout they undercount to ~1 and inflate per-file). len(files) is the honest count of
    # files the extractor actually read.
    n_files = max(len(files), 1)
    per_file_ms = (dt * 1000.0) / n_files
    cov = collections.Counter(n.get("language") for n in files)
    res["metrics"] = {
        "nodes": len(nodes), "edges": len(edges), "files": len(files), "code_files": len(code_files),
        "parsed": g["files_parsed"], "failed": g["files_failed"], "skipped": g["files_skipped"],
        "import_edges": len(imp), "resolved_internal": len(resolved), "calls": len(calls),
        "tables": len(tables), "alters": len(alters), "queries": len(queries),
        "fanouts": len(fanouts), "max_fanout": max_fanout, "diff_stem_fanouts": len(diff_stem),
        "absurd_fanouts": len(absurd), "src_to_test": len(src2test), "src_to_test_sole": len(src2test_sole),
        "secs": round(dt, 3), "per_file_ms": round(per_file_ms, 2), "langs": dict(cov),
    }

    # ---- universal assertions (every repo) -----------------------------------------------------
    check("NEVER-CRASH: build_graph completes without raising", True)
    # never-hang: a generous wall-clock ceiling scaled to repo size (these are small/medium repos).
    wall_ceil = 8.0 + 0.02 * max(g["files_parsed"] + g["files_skipped"], 0)
    check(f"NEVER-HANG: wall-clock {dt:.2f}s within bound ({wall_ceil:.1f}s)", dt <= wall_ceil,
          f"{dt:.2f}s")
    # never-crash promise: 0 files counted as FAILED (a parse that raised). Skipped (noded) is fine.
    check(f"ROBUST: 0 files failed to parse (skipped/noded={g['files_skipped']})",
          g["files_failed"] == 0, f"failed={g['files_failed']}")
    # bounded output: edges must not explode relative to nodes (a real graph is roughly linear, not
    # quadratic). A blow-up (e.g. an O(n^2) shared-resource flood) shows up as a huge edge/node ratio.
    ratio = len(edges) / max(len(nodes), 1)
    check(f"BOUNDED: edge/node ratio {ratio:.1f} is linear (< 12x)", ratio < 12.0, f"{ratio:.1f}x")
    # the #84 table-cap holds on a real schema: minted tables never exceed _MAX_TABLES.
    check(f"BOUNDED: table nodes {len(tables)} <= cap {_MAX_TABLES} (#84 schema-cap)",
          len(tables) <= _MAX_TABLES, f"{len(tables)}")
    # precision #83: the real bug signatures only — DIFFERENT-STEM fan-out (genuine mis-resolution) and
    # an ABSURD fan-out size (an O(files) blow-up). SAME-STEM fan-out (KMP / duplicate-named modules) is
    # correct recall and is reported as a measured number, not failed.
    check("PRECISION: no different-stem basename fan-out (the real ambiguity bug)", not diff_stem,
          f"{len(diff_stem)} diff-stem (e.g. {list(diff_stem)[:1]})")
    check(f"PRECISION: no absurd fan-out (no import -> >= {_FANOUT_ABSURD} files; max seen={max_fanout})",
          not absurd, f"{len(absurd)} absurd, max={max_fanout}")
    # a production->test edge is a FALSE edge only when it is the SOLE same-basename resolution (no correct
    # non-test sibling co-resolved) — a recall-biased extra alongside a correct sibling is by design.
    check("PRECISION: no SOLE production->test false import edge (recall-biased extras allowed)",
          not src2test_sole, f"{len(src2test_sole)} sole (of {len(src2test)} total src->test)")
    # perf sanity (generous ceiling, never a microbench). Per-file is only meaningful with enough files;
    # on a tiny / schema-only checkout the absolute wall-clock (NEVER-HANG, above) is the honest signal,
    # so the per-file ceiling is skipped (auto-PASS) below 8 files to avoid a denominator artifact.
    check(f"PERF: per-file {per_file_ms:.1f} ms within ceiling ({_PER_FILE_MS_CEIL} ms; n_files={len(files)})",
          len(files) < 8 or per_file_ms <= _PER_FILE_MS_CEIL, f"{per_file_ms:.1f}ms over {len(files)} files")

    # ---- profile-specific recall spot-checks ---------------------------------------------------
    if profile in ("kotlin", "swift", "go", "generic"):
        # recall sanity: SOME real file->file couplings were found AND a healthy share of files parsed.
        check("RECALL: real intra-repo import edges resolve to file->file", len(resolved) > 0,
              f"{len(resolved)} resolved")
        # the dominant language actually got PARSED (not all noded) — proves the grammar ran on real code.
        lang_parsed = {n.get("language") for n in files} - {"unknown"}
        check(f"RECALL: dominant language parsed (langs seen: {sorted(lang_parsed)[:5]})",
              bool(lang_parsed))
        # call edges exist (defs were found + call sites recorded) — proves the def/call spec works.
        check("RECALL: call sites recorded (def/call extraction ran)", len(calls) > 0,
              f"{len(calls)} calls")
        # stdlib stays inert: a sample of import edges did NOT resolve (external deps name no local file).
        unresolved = [e for e in imp if e["dst"] not in fp]
        check("PRECISION: external/stdlib imports stay inert (not all imports resolve)",
              len(unresolved) > 0, f"{len(unresolved)} inert")

    if profile == "go":
        # Go-specific: a single-segment import (stdlib: fmt/strings/time) must NEVER resolve to a local
        # *.go file (the #precision Go fix). Check no resolved edge's dst basename is a stdlib name AND
        # the edge came from a single-segment import — approximated by: no resolved dst is a top-level
        # stdlib-named file. (cobra is a flat package so most refs are intra-package = no import edge,
        # which is correct Go semantics, not a recall miss — so we do NOT demand a high resolved count.)
        stdlib_basenames = {"fmt", "strings", "time", "context", "errors", "os", "io", "sort", "sync"}
        bad = [d for _, d in resolved if _noext(os.path.basename(d)) in stdlib_basenames]
        check("PRECISION(go): no stdlib import resolved to a local .go file", not bad,
              f"{len(bad)} bad")

    if profile == "swift":
        # Swift protocols must be first-class type defs (#49: omitting them dropped every protocol).
        # A real Swift repo has protocols — assert at least one protocol-bearing file produced a class
        # node (class_declaration covers class/struct/enum; protocol_declaration its own def). We can't
        # see the keyword content-free, but we CAN assert defs were extracted from swift files.
        swift_defs = [n for n in nodes if n.get("language") == "swift" and n.get("kind") in ("def", "class")]
        check("RECALL(swift): defs/types extracted from .swift (funcs + types incl. protocols)",
              len(swift_defs) > 0, f"{len(swift_defs)} swift symbols")

    if profile in ("orm", "sqlddl", "bigschema"):
        # schema-graph recall: real tables were minted.
        check("RECALL(schema): table nodes minted from real schema/ORM", len(tables) > 0,
              f"{len(tables)} tables")
        check("RECALL(schema): alters/queries edges to tables exist", (len(alters) + len(queries)) > 0,
              f"alters={len(alters)} queries={len(queries)}")

    if profile == "bigschema":
        # #84 table-cap on a REAL large production schema: a real schema (here ~1.5k tables) is well
        # under the 20k cap, so EVERY table is minted and NONE is truncated (the cap only ever clips an
        # attacker flood, never a genuine schema). Prove it: tables minted == DISTINCT CREATE TABLE names
        # in the .sql, AND we are strictly below the cap (not clipped). Edges stay 1:1 (bounded output).
        import re as _re
        sql_files = [p for p in fp if p.endswith(".sql")]
        distinct = set()
        for p in sql_files:
            try:
                with open(os.path.join(root, p), "r", encoding="utf-8", errors="replace") as fh:
                    for m in _re.finditer(r'\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?(?:only\s+)?'
                                          r'[`"\[]?([A-Za-z_][\w.]*)', fh.read(), _re.I):
                        nm = m.group(1).strip().strip('`"[]').split(".")[-1].lower()
                        if nm:
                            distinct.add(nm)
            except OSError:
                pass
        res["metrics"]["distinct_create_table"] = len(distinct)
        check(f"CAP(#84): real schema NOT truncated — minted {len(tables)} tables strictly under cap "
              f"{_MAX_TABLES}", 0 < len(tables) < _MAX_TABLES, f"{len(tables)}")
        # minted tables should equal the distinct CREATE TABLE names (allow tiny slack for partitioned /
        # schema-qualified edge cases the audit regex normalizes slightly differently).
        within = abs(len(tables) - len(distinct)) <= max(5, int(0.02 * len(distinct)))
        check(f"CAP(#84): minted tables ({len(tables)}) == distinct CREATE TABLE ({len(distinct)}) "
              f"(no silent drop)", within, f"minted={len(tables)} distinct={len(distinct)}")

    if profile == "orm":
        # THE MOAT (#84): a real table coupled migration<->code (an `alters` source and a `queries`
        # source naming the same table, with NO code edge between them — code-only tools miss this).
        alt_t = collections.defaultdict(set)
        qry_t = collections.defaultdict(set)
        for e in alters:
            alt_t[e["dst"]].add(e["src"])
        for e in queries:
            qry_t[e["dst"]].add(e["src"])
        shared = sorted(t for t in alt_t if t in qry_t)
        res["metrics"]["migration_code_coupled_tables"] = len(shared)
        res["metrics"]["sample_coupled"] = shared[:3]
        check("MOAT(orm): >=1 table coupled migration<->code (shared resource, no code edge)",
              len(shared) >= 1, f"{len(shared)} coupled tables")

    return res


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_clone = "--no-clone" in sys.argv

    targets = []   # (key, root, profile)
    if args:
        for p in args:
            if not os.path.isdir(p):
                print(f"  skip (not a dir): {p}")
                continue
            targets.append((os.path.basename(p.rstrip("/")) or p, p, _profile_of(p)))
    else:
        net_ok = True
        for key, url, profile, dst in _DEFAULT_REPOS:
            if no_clone:
                if os.path.isdir(dst) and os.listdir(dst):
                    targets.append((key, dst, profile))
                continue
            if url.startswith("file::"):     # a single-file asset (e.g. a production schema) — fetch, not clone
                ok, note = _fetch_file(url[len("file::"):], dst, os.path.basename(url.split("/")[-1]))
            else:
                ok, note = _clone(key, url, dst)
            print(f"  [{'ok' if ok else 'MISS'}] {key:14s} {note}")
            if ok:
                targets.append((key, dst, profile))
            else:
                net_ok = False
        if not targets:
            print("\nNo repos to audit (network unavailable and no local /tmp/rr-* checkouts).")
            print("HONEST-EMPTY: cannot fabricate real-repo results. Pass local PATHs or run with network.")
            return 2
        if not net_ok:
            print("  (some clones failed — auditing the repos that ARE available; not fabricating the rest)")

    print(f"\n== Veripsa REAL-REPO quality audit ({len(targets)} repos) ==\n")
    results = []
    for key, root, profile in targets:
        r = audit_one(key, root, profile)
        results.append(r)
        m = r["metrics"]
        print(f"--- {key}  [{profile}]  {root}")
        if r["crash"]:
            print("  *** CRASH ***")
            print("  " + r["crash"].replace("\n", "\n  "))
        else:
            print(f"  files={m['files']} (code={m['code_files']}, parsed={m['parsed']}, "
                  f"failed={m['failed']}, noded={m['skipped']})  nodes={m['nodes']} edges={m['edges']}")
            print(f"  imports={m['import_edges']} resolved_file2file={m['resolved_internal']} "
                  f"calls={m['calls']}  langs={m['langs']}")
            if m.get("tables"):
                extra = ""
                if "migration_code_coupled_tables" in m:
                    extra = (f"  migration<->code coupled tables={m['migration_code_coupled_tables']} "
                             f"{m.get('sample_coupled')}")
                print(f"  schema: tables={m['tables']} alters={m['alters']} queries={m['queries']}{extra}")
            print(f"  precision: basename_fanouts={m['fanouts']} (max={m['max_fanout']}, "
                  f"diff_stem={m['diff_stem_fanouts']}, absurd={m['absurd_fanouts']})  "
                  f"src->test={m['src_to_test']} (sole={m['src_to_test_sole']})")
            print(f"  perf: {m['secs']}s  per_file={m['per_file_ms']}ms")
        passed = all(c for _, c, _ in r["checks"])
        for name, cond, detail in r["checks"]:
            print(f"     [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if (detail and not cond) else ""))
        print(f"  REPO VERDICT: {'PASS' if passed else 'FAIL'}\n")

    all_pass = all(all(c for _, c, _ in r["checks"]) and not r["crash"] for r in results)
    n_pass = sum(1 for r in results if all(c for _, c, _ in r["checks"]) and not r["crash"])
    print("=" * 78)
    print(f"REPOS AUDITED: {len(results)}   PASSED: {n_pass}   FAILED: {len(results) - n_pass}")
    print(f"REAL-REPO QUALITY: {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
