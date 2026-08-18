"""Import-resolution pass for the code-graph extractor.

Owns: _SRC_EXT, _WEB, _CFAM, _noext, _family, _resolve_imports.

QUALITY (#1, recall-biased): an `imports` edge today carries dst = a MODULE NAME,
which the adjacency engine cannot turn into a file (so ~all import edges are inert —
measured: 161/161 dead on the real graph). Here we resolve each module to the repo
FILE(S) it names and rewrite the edge to dst = that file PATH, so imports become
first-class exact file dependencies or explicitly inert ambiguity evidence.
Local imports resolve; stdlib/3rd-party match nothing and are kept as-is (harmless).
A bounded ambiguous basename retains every candidate as explicitly inert ambiguity
evidence; Ruby and over-cap cases retain one raw ambiguity edge so they remain
bounded. Content-free (paths/module names only).
"""
import glob
import os

_EMPTY = frozenset()                                 # shared empty default for by_suffix.get (no per-probe alloc)

_SRC_EXT = (
    ".tsx", ".jsx", ".mjs", ".cjs", ".ts", ".js",
    ".astro", ".svelte", ".vue",
    ".css", ".scss", ".sass", ".less", ".styl",
    ".py", ".go", ".rb", ".php", ".cs", ".rs",
)
_WEB = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".html", ".htm", ".astro", ".svelte", ".vue",
    ".css", ".scss", ".sass", ".less", ".styl",
)
_STYLE_EXT = (".css", ".scss", ".sass", ".less", ".styl")
_SASS_EXT = (".scss", ".sass")
_CFAM = (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")   # C/C++ share headers

# Languages whose LOCAL imports are ALWAYS multi-segment paths, so a bare SINGLE-segment import is
# necessarily external (stdlib) and must NOT fall back to a local basename match. Go is the clear
# case: a local import is the full module path (github.com/org/repo/pkg); `time`/`fmt`/`strings` are
# always the standard library.
_NO_BARE_BASENAME_EXT = (".go",)

# Languages where a bare SINGLE-segment import is ALMOST ALWAYS an external gem/stdlib and should NOT
# fan out to ALL local same-named files. Ruby's canonical load path is directory-relative; a bare
# `require 'test_helper'` in a real project is typically a bundler-resolved gem name, NOT a local file
# (measured: 18 local `test_helper.rb` files in Rails → 3,474 spurious edges from 193 bare requires).
# FIX: for Ruby, a bare single-segment require is resolved ONLY when there is a UNIQUE local .rb match.
# If multiple local files share the same basename the import is ambiguous — retain one inert raw edge with
# an explicit status (same precision guard applied to Rust workspace cross-crate resolution and the C# type fallback).
# RECALL COST: near-zero for real projects (same-dir `require 'helper'` with ONE local helper.rb still
# resolves; only the genuinely ambiguous multi-target fan-outs are suppressed). Multi-segment requires
# (`require 'active_support/test_helper'`) are UNAFFECTED — they resolve via the suffix index as before.
_RUBY_BARE_UNIQUE_ONLY_EXT = (".rb",)
# Fan-out DEGREE ceiling for an AMBIGUOUS BARE single-segment import (`import util`, `import config`).
# The resolver is recall-biased: a bare basename fans out to EVERY same-basename file (a real single
# coupling we don't want to miss). But an adversarial / pathological repo with N files all named `util.py`
# in distinct dirs + N importers each doing `import util` makes EACH bare import resolve to all N files
# → N² `imports` edges (MEASURED: 50→2,500, 200→40,000, 500→250,000 = 17.9 MB ingest payload). The
# per-FILE _PER_FILE_EDGE_CAP runs BEFORE resolution, so it never bounds this CROSS-file fan-out.
# RECALL-SAFE cap: a bare import resolving to MORE than this many distinct files is LOW-PRECISION
# wallpaper — `import util` that could mean any of 9+ `util.py` is not a confident single coupling, so
# we retain one inert raw ambiguity edge rather than fan out. 1-8 candidates still retain every candidate.
# Aligned with the codebase's existing hub-dampening discipline (code_graph_extract _HUB_THRESHOLD=3 for
# symbol-name hubs; tests/audit_realrepos _FANOUT_ABSURD=50 for absurd basename blow-ups): 8 sits well
# above the 1-3 confident-coupling band (so legitimate moderate duplication like KMP expect/actual is
# untouched) and well below the absurd-blow-up line, capping only the genuinely-ambiguous tail.
_MAX_BARE_FANOUT = 8

# Manifest basenames that declare a PUBLISHABLE PACKAGE rooted at their directory. Their PRESENCE (a
# path, content-free) plus the source layout under them is what tells us a repo is itself a published
# package — so a same-named SELF-import (`zustand/shallow`, bare `flask`) is the repo importing its OWN
# code by its published name, not an external dependency. We read ONLY the manifest's PATH from the
# graph (never its body): the package NAME is recovered from the directory layout the manifest roots
# (a `src/<name>/__init__.py` Python package, an npm `src/` entry), not from parsing the file.
_NPM_MANIFEST = "package.json"
_PY_MANIFEST = ("pyproject.toml", "setup.py", "setup.cfg")
# Source-root directory names a repo writes root-relative imports against (`src/foo`, `lib/foo`). These are
# NEVER the PUBLISHED package name, so they must not be mistaken for an own-name self-import scope.
_SRCROOT_DIRS = frozenset({"src", "lib", "source", "sources", "app"})


def _no_bare_basename(src_path):
    """True when `src_path`'s language forbids resolving a bare single-segment import to a local
    basename (its local imports are always multi-segment — see _NO_BARE_BASENAME_EXT)."""
    return src_path.endswith(_NO_BARE_BASENAME_EXT)


def _own_packages(files, manifest_paths, by_noext_all, imports):
    """Recover the repo's OWN published package name(s) → (source-root dir, entry file) — CONTENT-FREE,
    from the path LAYOUT a manifest roots (never the manifest's body).

    WHY this exists (two measured defects, both content-free path/name resolution):
      • RECALL: inside a published package, a self-import by the package's OWN name (`zustand/shallow`,
        `zustand/middleware`) names NO file at `…/zustand/shallow` — so the suffix probe misses and a real
        intra-package dependency edge is LOST. Stripping the leading own-name and resolving the REMAINDER
        (`shallow`) against the package source tree recovers it (zustand RAW recall ~52→61%).
      • PRECISION: a bare single-segment import that equals the OWN name (`import flask` inside Flask)
        basename-fans-out to a DECOY file literally named `flask.py` (a test fixture) — 43 false edges,
        17% of flask's resolved internal edges. The own name must resolve to the package ENTRY, never a
        same-basename decoy.

    Derivation (paths only; the manifest VALUE is not in the graph and the file is not openable from a
    repo-relative path, so we infer the name from the layout the manifest roots — exactly as content-free):
      • PYTHON: a `pyproject.toml`/`setup.py`/`setup.cfg` at dir D makes D a package root. Its importable
        package is the directory P with `P/__init__.py` sitting directly in D, in D/`src`, or at the repo
        root — name = basename(P), entry = `P/__init__.py`. (Flask: `src/flask/__init__.py` → name `flask`.)
      • NPM/TS: a `package.json` at dir D roots a package whose source lives in D/`src` (else D). The literal
        name is NOT in the layout, so we recover it IMPORT-CORROBORATED: a non-relative, non-scoped import
        first-segment S that recurs (≥2) AND whose stripped remainder resolves under the source root is the
        own name (its bare-entry = D/src|/index). Corroboration requires a
        same-language-family local subpath, so an unrelated `src/foo.py`
        cannot turn a TypeScript `react/foo` external import into an own-package
        claim. (Zustand: `zustand/middleware` → `src/middleware.ts`, bare entry
        `src/index.ts`.)

    Returns {own_name_lower: {"roots": [source-root dirs…],
    "entry": deterministic_legacy_entry_or_None, "entries": [all same-coordinate
    entry candidates]}}. Empty when the repo is not a published package (no
    manifest) — so a plain app with no `package.json`/`pyproject` is untouched
    (the existing precision tests have no manifest → this is inert there)."""
    fset = set(files)
    npm_dirs, py_dirs = set(), set()
    for p in manifest_paths:                            # manifests are config_file nodes, NOT in `files`
        b = p.rsplit("/", 1)[-1]
        d = p.rsplit("/", 1)[0] if "/" in p else ""
        if b == _NPM_MANIFEST:
            npm_dirs.add(d)
        elif b in _PY_MANIFEST:
            py_dirs.add(d)
    if not npm_dirs and not py_dirs:
        return {}                                       # not a published package → inert (no own-name logic)
    own = {}

    def _src_roots(d):
        """The plausible SOURCE roots a manifest at dir `d` roots: `d/src` (if it holds source) then `d`."""
        roots = []
        sr = (d + "/src") if d else "src"
        if any(p == sr or p.startswith(sr + "/") for p in fset):
            roots.append(sr)
        roots.append(d)
        return roots

    # PYTHON: an __init__.py-bearing dir directly under a manifest dir / its src/ / the repo root.
    for p in sorted(files):
        if not p.endswith("/__init__.py"):
            continue
        pkgdir = p[: -len("/__init__.py")]
        nm = pkgdir.rsplit("/", 1)[-1]
        parent = pkgdir.rsplit("/", 1)[0] if "/" in pkgdir else ""
        grand = parent.rsplit("/", 1)[0] if "/" in parent else ""
        rooted = (parent in py_dirs
                  or (parent.rsplit("/", 1)[-1] == "src" and grand in py_dirs)
                  or (parent == "" and "" in py_dirs))
        if rooted and nm:
            rec = own.setdefault(
                nm.lower(),
                {
                    "roots": [],
                    "entry": None,
                    "entries": [],
                    "origin": "python",
                },
            )
            if pkgdir not in rec["roots"]:
                rec["roots"].append(pkgdir)             # the REMAINDER of `name/sub` resolves under the package dir
            entries = set(rec["entries"])
            entries.add(p)
            rec["entries"] = sorted(entries)
            rec["entry"] = rec["entries"][0]            # deterministic legacy representative

    # NPM/TS: own name = an import first-segment whose stripped remainder resolves under a package source root.
    if npm_dirs:
        firsts, subs = {}, {}
        for e in imports:
            raw = (e.get("dst") or "").strip()
            if not raw or raw[0] in "./<@":             # relative / system / scoped → not a bare own-name self-import
                continue
            seg = raw.strip("\"'").replace("\\", "/").split("/")
            s0 = seg[0]
            if not s0 or s0 != s0.strip():
                continue
            firsts[s0] = firsts.get(s0, 0) + 1
            if len(seg) > 1:
                subs.setdefault(s0, []).append(
                    ("/".join(seg[1:]), e.get("src") or "")
                )
        for D in sorted(npm_dirs):
            roots = _src_roots(D)
            entry = None
            entries = set()
            for r in roots:                             # bare-entry for an `import <name>` (folder/index entry)
                for cand in (r + "/index", r):
                    candidates = set(by_noext_all.get(cand, ()))
                    if candidates:
                        entries = candidates
                        entry = min(candidates)
                        break
                if entry:
                    break
            for s0 in firsts:
                existing = own.get(s0.lower())
                if s0.lower() in _SRCROOT_DIRS:
                    continue                            # a source-root dir name (src/lib/...) is never the published name
                # CORROBORATION (precision — the discriminator that keeps EXTERNAL deps out): a first-segment
                # is the OWN name ONLY when a `S/<sub>` self-import has its REMAINDER resolving under THIS
                # package's source root. That is genuinely discriminating: an external `react`/`vitest`/`immer`
                # is imported many times BY NAME but its subpaths (`react/jsx-runtime`, `react-dom/client`)
                # do NOT exist in the repo's `src/`, so they never corroborate. (A mere recurring BARE import
                # is NOT enough — every external dep is imported repeatedly by name; relying on a count alone
                # falsely claimed react/vitest/immer as 'own' on the real zustand repo.) The rare case where an
                # external `foo/bar` DOES coincide with a local `src/bar` is harmless: the strip resolves to
                # that same local file the suffix probe would not have found, never an unrelated decoy.
                subpath_hit = False
                for sub, importer in subs.get(s0, ()):
                    subne = sub.rsplit(".", 1)[0] if "." in sub.rsplit("/", 1)[-1] else sub
                    for r in roots:
                        key = (r + "/" + subne) if r else subne
                        candidates = set(by_noext_all.get(key, ()))
                        candidates.update(
                            by_noext_all.get(key + "/index", ())
                        )
                        if any(
                            _family(candidate) == _family(importer)
                            for candidate in candidates
                        ):
                            subpath_hit = True
                            break
                    if subpath_hit:
                        break
                if subpath_hit:
                    rec = own.setdefault(
                        s0.lower(),
                        {
                            "roots": [],
                            "entry": entry,
                            "entries": sorted(entries),
                            "origin": "npm",
                        },
                    )
                    if (
                        existing
                        and rec.get("origin") == "python"
                    ):
                        rec["origin"] = "polyglot"
                    for r in roots:
                        if r and r not in rec["roots"]:
                            rec["roots"].append(r)
                    if entries:
                        merged_entries = set(rec["entries"])
                        merged_entries.update(entries)
                        rec["entries"] = sorted(merged_entries)
                        rec["entry"] = rec["entries"][0]
    for rec in own.values():
        rec["roots"] = sorted(set(rec["roots"]))
    return own


# Every real source/header file extension the extractor handles. `_mod_noext` strips ONLY these from
# an import string — so a C/C++ include `<a/b.hpp>` loses its `.hpp` (it names a FILE), while a dotted
# module's last component (`com.google.gson.Gson` → `.Gson`, `app.core.config` → `.config`) is NEVER
# stripped (it is a package/class segment, not a file extension).
_ALL_EXT = _SRC_EXT + _CFAM + (".go", ".java", ".kt", ".kts", ".swift", ".html", ".htm")


def _noext(p):
    for x in _SRC_EXT:
        if p.endswith(x):
            return p[:-len(x)]
    e = os.path.splitext(p)[1]
    return p[: -len(e)] if e else p


def _mod_noext(raw):
    """Strip a trailing FILE extension from an import string, but only a real source/header extension
    (`_ALL_EXT`). A C include `<a/b.hpp>` → `<a/b>`; a dotted module `a.b.Class` is left intact (its
    last segment is not a file extension). This is what lets Java FQNs and C includes BOTH resolve."""
    for x in _ALL_EXT:
        if raw.endswith(x):
            return raw[: -len(x)]
    return raw


def _family(p):
    """Language family (web bundles ts/js/html; c/c++ share .h)."""
    x = os.path.splitext(p)[1]
    return "web" if x in _WEB else "cfam" if x in _CFAM else x


def _go_pkg_dirs(files):
    """Map each repo directory that holds .go files → the list of those package's IMPORTABLE .go files.

    A Go PACKAGE is a DIRECTORY of .go files, not a single file, and a local Go import is the
    FULL module path (`github.com/org/repo/internal/auth`). To resolve such an import to repo
    files we match its tail against an actual package DIRECTORY (`internal/auth`) and link every
    .go file in it — see `_resolve_go_pkg`.

    PRECISION (real-repo audit on gin): a `*_test.go` file is NOT part of the importable package
    surface — Go compiles tests into a SEPARATE binary, so production code can never import a
    `_test.go` file (and a `_test.go` is itself unimportable as a target). Linking a package import
    to the directory's `_test.go` files produced 55/286 (19%) FALSE production→test couplings on gin
    (`context.go` → `binding/json_test.go`, `deprecated.go` → `binding/binding_test.go`, …) — exactly
    the cry-wolf that gets the product muted. So a package directory maps to its NON-test .go files
    only. No recall lost: a `_test.go`'s real coupling to production is recorded from the test file's
    OWN imports (test is the `src`, production is the target), which this never touches."""
    dirs = {}
    for p in files:
        if p.endswith(".go") and not p.endswith("_test.go"):
            d = p.rsplit("/", 1)[0] if "/" in p else ""
            if d:
                dirs.setdefault(d, []).append(p)
    return dirs


def _go_pkg_by_suffix(go_dirs):
    """Build an O(1) lookup structure for Go package directory resolution — inverts the scan.

    `go_dirs` maps each .go-bearing directory path → its list of non-test .go files (from
    `_go_pkg_dirs`). A Go import like `github.com/org/repo/internal/auth` is resolved by
    matching its PATH-SUFFIX against a real directory (`internal/auth`): a directory `d` matches
    an import `imp` when `imp == d OR imp.endswith('/' + d)`.

    The linear scan in `_resolve_go_pkg` iterates ALL go_dirs for EVERY import — O(go_dirs ×
    imports). kubernetes = ~3,879 pkg dirs × ~63,447 imports ≈ 246M checks = ~19.6s.

    INVERSION: instead of iterating dirs and testing whether the dir is a suffix of the import,
    we iterate SUFFIXES OF THE IMPORT and test whether each suffix is a known dir. Since
    `go_dirs` IS a dict keyed by dir paths, each "is this suffix a known dir?" is an O(1) dict
    lookup. The loop runs at most O(import_depth) iterations per edge (import depth ≪ dir count).

    Index structure: `go_dirs` itself is reused directly by `_resolve_go_pkg_indexed` — no
    new dict is needed. This function returns `go_dirs` unchanged; its purpose is to make the
    design explicit (the index IS the go_dirs dict) and to serve as the integration point that
    `_resolve_imports` calls.

    IDENTICAL SEMANTICS to `_resolve_go_pkg` (proved by test_resolve_go_ruby.py gate 123):
    the set of matching dirs found by iterating import suffixes and probing go_dirs is exactly
    the set the old code found by iterating go_dirs and testing endswith — just traversed in the
    opposite direction."""
    return go_dirs   # the dict itself is the index; _resolve_go_pkg_indexed iterates import suffixes


def _resolve_go_pkg(raw, go_dirs):
    """Resolve a repo-local Go package import to the .go files of the package DIRECTORY it names.

    `raw` is the full import path (`github.com/org/repo/internal/auth`); we don't know the module
    prefix, so we match the import's PATH-SUFFIX against a real .go-bearing directory in the repo
    (`internal/auth`) and return that directory's .go files. To keep the precision the prior Go fix
    won, we (a) require a MULTI-SEGMENT import (single-segment = stdlib, resolved to nothing
    upstream) and (b) keep only the MOST-SPECIFIC (most-path-segments) matching directory, so
    `.../internal/auth` resolves to `internal/auth/` and never fans out to an unrelated top-level
    `auth/`. An external import (`go.uber.org/zap`) whose tail is no repo package matches nothing.

    NOTE: `_resolve_imports` uses the faster `_go_pkg_by_suffix` index instead of calling this
    function directly — `_resolve_go_pkg` is kept for the reference resolver in test_resolve_scale.py
    and for any external callers that pass a plain `go_dirs` dict."""
    imp = raw.strip().strip("\"'")
    if "/" not in imp:                               # single-segment → stdlib (handled upstream)
        return set()
    matches = [d for d in go_dirs if imp == d or imp.endswith("/" + d)]
    if not matches:
        return set()
    best = max(len(d.split("/")) for d in matches)   # most-specific suffix only (precision)
    out = set()
    for d in matches:
        if len(d.split("/")) == best:
            out.update(go_dirs[d])
    return out


def _resolve_go_pkg_indexed(imp_raw, go_suffix, go_dirs):
    """O(import_depth) alternative to `_resolve_go_pkg` by iterating the IMPORT's suffixes.

    WHERE THE SPEEDUP COMES FROM (the critical insight):
      The linear scan iterates ALL go_dirs and tests `imp.endswith('/' + d)` for each —
      O(go_dirs) per edge. The inversion: iterate the SUFFIXES OF THE IMPORT and test whether
      each suffix is in go_dirs (an O(1) dict lookup). Since import depth ≪ dir count, the
      total work per edge falls from O(go_dirs) to O(import_depth) — typically 3–8 iterations.

    MATCHING SEMANTICS (identical to `_resolve_go_pkg`):
      A dir `d` matches import `imp` when `imp == d OR imp.endswith('/' + d)` — i.e. when `d`
      is a PATH-SEGMENT SUFFIX of `imp`. We check this by stripping leading segments of `imp`
      one at a time and probing go_dirs for the truncated form:
        imp = "github.com/kubernetes/kubernetes/internal/auth"
        segs[0:]  "github.com/kubernetes/kubernetes/internal/auth"  → not in go_dirs
        segs[1:]  "kubernetes/kubernetes/internal/auth"              → not in go_dirs
        segs[2:]  "kubernetes/internal/auth"                         → not in go_dirs
        segs[3:]  "internal/auth"                                    → IN go_dirs → match!
      The FIRST match is the LONGEST matching suffix = the MOST-SPECIFIC dir (same as the
      max-depth precision guard in `_resolve_go_pkg`).

    PRECISION: when a short suffix like `auth` would match TWO dirs (deep `internal/auth`
      and shallow `auth`), the loop finds the LONGER suffix first (`internal/auth` at segs[3:]
      before `auth` at segs[4:]) and returns immediately — the shallow dir is never reached.
      Byte-identical to the max-depth selection in the linear scan (proved by gate 123).

    SINGLE-SEGMENT guard: `_resolve_imports` already blanks single-segment Go imports
      (stdlib/external), so we only ever see multi-segment imports here. The `'/' not in imp`
      guard is kept for safety.

    `go_suffix` is the result of `_go_pkg_by_suffix(go_dirs)` — which returns `go_dirs`
    directly. We accept it as a parameter to keep the call-site signature consistent."""
    imp = imp_raw.strip().strip("\"'")
    if "/" not in imp:                               # single-segment → stdlib (handled upstream)
        return set()
    # Walk import suffixes longest → shortest; first hit = most-specific dir (precision).
    # We probe ALL suffixes down to a single segment: a 1-segment DIR name (e.g. `build`) IS a
    # valid Go package directory, even though a 1-segment IMPORT (`fmt`) is stdlib. The upstream
    # guard already blanked single-segment imports, so we only reach here with multi-segment `imp`.
    # A match on the bare last segment (`build`) is legitimate: it means a deep import like
    # `github.com/org/repo/build` maps to the repo-root-level `build/` package directory.
    segs = imp.split("/")
    for i in range(len(segs)):
        suffix = "/".join(segs[i:])
        files = go_suffix.get(suffix)               # go_suffix IS go_dirs: O(1) dict lookup
        if files is not None:
            return set(files)                        # most-specific match: return immediately
    return set()


# A C# project's test directory segments — a `using` of a production namespace must NOT couple to a
# test/example file (the Go `_test.go` lesson, applied to C# directory layout). Matched case-insensitively
# against each path segment, like the audit's _is_test_path.
_CS_TEST_SEG = ("test", "tests", "spec", "specs", "__tests__", "examples", "example")

# A Rust crate's SEPARATE-COMPILE-TARGET directories (Cargo convention): integration `tests/`, `examples/`,
# `benches/`. A file under one of these is its OWN compile target — it can NEVER be referenced as an
# IN-CRATE module path (`crate::…` / a bare module name). So the trailing-type-name fallback (which guesses
# the CONTAINING MODULE file of `use crate::mod::Type`) must treat such a file as INERT, exactly as the C#
# namespace fallback excludes `_CS_TEST_SEG` and the Go resolver excludes `_test.go`. RECALL-SAFE: a real
# in-crate module is never here, so excluding these can only DROP a decoy — and when a decoy is dropped a
# genuine in-crate module that shares the basename then resolves UNIQUELY (a recall GAIN). The test file's
# OWN couplings are still recorded from the test file as the `src` (test imports its target, not vice-versa).
_RS_TARGET_SEG = ("tests", "test", "examples", "example", "benches", "bench")

# JS/TS TEST-DOUBLE directories (Jest / Vitest convention). A `__mocks__/<name>/` (or `__mock__`,
# `__fixtures__`) directory holds a MANUAL MOCK that SHADOWS a node-resolved package or module — the
# test runner swaps it in at test time; PRODUCTION code NEVER imports it by path. So when a BARE
# SINGLE-segment import (`import Vue from 'vue'`, `import $ from 'jquery'` — always an EXTERNAL package,
# since a local module is reached via a relative `./`/`../` or an aliased `~/`/`@/` path, never a bare
# name) resolves SOLELY to a file inside one of these dirs, it is a FALSE production→test coupling: the
# bare name matched the mock's `<pkg>/index.js` decoy. Measured on gitlabhq: 1,388 such src→test edges
# (`vue`/`lodash-es`/`jquery` → `spec/frontend/__mocks__/<pkg>/index.js`), 37 surviving hub-dampening as
# real false couplings. Exclude these targets for a bare web import — the SAME separate-test-target rule
# the Rust (`_RS_TARGET_SEG`) / C# (`_CS_TEST_SEG`) / Go (`_test.go`) resolvers already apply. Matched
# case-sensitively (these are literal fixed dir names) against each path segment. `__helpers__` is the
# sibling Jest convention for test helpers / DOM-shims / polyfills (`__helpers__/dom_shims/clipboard.js`,
# `__helpers__/crypto.js`) — a bare external name (`clipboard`, the `crypto` builtin) matching one is the
# SAME decoy; a genuine RELATIVE import of a helper (`require('./spec/frontend/__helpers__/test_constants')`)
# is NOT bare_single, so it is untouched (recall-safe).
_JS_MOCK_SEG = ("__mocks__", "__mock__", "__fixtures__", "__helpers__")


def _js_mock_target(path):
    """True if `path` lives inside a Jest/Vitest test-double directory (a manual mock that shadows an
    external package; never imported by production code via a path). Mirrors _rs_inert_module_target."""
    return any(seg in _JS_MOCK_SEG for seg in path.split("/"))


def _rs_inert_module_target(path):
    """True if a .rs `path` is a SEPARATE Cargo compile target (tests/examples/benches) and therefore
    can never be the resolution of an IN-CRATE module path. Case-insensitive, per path segment."""
    return any(seg.lower() in _RS_TARGET_SEG for seg in path.split("/"))


# A Python TEST/FIXTURE directory segment set (mirrors `_CS_TEST_SEG` and tests/audit_repo.py's
# `_is_test_path`). A test tree (`tests/units/…`) frequently MIRRORS the package directory layout
# (real-repo audit on reflex-dev/reflex: `tests/units/reflex_base/utils/__init__.py`,
# `tests/units/reflex_base/event/__init__.py` shadow the real `reflex_base/utils`, `reflex_base/event`
# packages). A dotted import `reflex_base.utils` then suffix-matches BOTH the real package file AND the
# test-fixture `__init__.py`, so the recall-biased suffix probe fans out to the test mirror — a FALSE
# production→test coupling (measured: 203 such edges on reflex). Matched case-insensitively per segment.
_PY_TEST_SEG = ("test", "tests", "spec", "specs", "__tests__", "examples", "example")


def _py_test_path(path):
    """True if a .py `path` lives in a TEST/FIXTURE directory. Case-insensitive, per path segment."""
    return any(seg.lower() in _PY_TEST_SEG for seg in path.split("/"))


def _cs_ns_dirs(files):
    """Map each repo directory that holds .cs files → (dotted-canonical directory path, the .cs files).

    A C# `using App.Services;` names a NAMESPACE = a DIRECTORY of .cs files (like a Go package), NOT a
    single file — the bare last segment (`Services`/`AuthService`) basename-fans-out to every same-named
    .cs file in the repo (real-repo audit on jellyfin: `using MediaBrowser.Controller.Entities.TV` falsely
    coupled to BOTH `MediaBrowser.Controller/Entities/TV/Series.cs` AND the unrelated
    `src/Jellyfin.Database/.../Entities/Libraries/Series.cs`). The namespace and its directory differ in
    SEPARATOR (a project root is often a single dotted dir name `MediaBrowser.Controller`, while nested
    folders use `/`), so we CANONICALIZE the directory to a dotted path (`/`→`.`) and suffix-match the
    using-namespace against THAT (see `_resolve_csharp_ns`).

    PRECISION: a test/example directory is NOT part of the production namespace surface a `using`
    targets (the Go `_test.go` lesson) — production code does not depend on a test file, so a dir whose
    path contains a test segment is excluded as a target. No recall lost: a test file's real coupling is
    recorded from the TEST file's own `using` (test is the `src`)."""
    dirs = {}
    for p in files:
        if not p.endswith(".cs"):
            continue
        d = p.rsplit("/", 1)[0] if "/" in p else ""
        if not d:
            continue
        if any(seg.lower() in _CS_TEST_SEG for seg in d.split("/")):
            continue
        dirs.setdefault(d, []).append(p)
    # canonical dotted form of each dir path, for suffix-matching a dotted namespace against it
    return {d: (d.replace("/", "."), fs) for d, fs in dirs.items()}


def _resolve_csharp_ns_detail(raw, cs_dirs):
    """Resolve a C# `using A.B.C;` namespace to the .cs files of the DIRECTORY it names.

    `raw` is the dotted namespace (`MediaBrowser.Controller.Entities.TV`). We don't know the project
    root, so we match the namespace as a path-SUFFIX of a real .cs-bearing directory's dotted-canonical
    form (`MediaBrowser.Controller/Entities/TV` → `MediaBrowser.Controller.Entities.TV`) and return that
    directory's .cs files. A single-segment `using` like `System` is the BCL and
    names no local directory. When one directory's canonical path exactly equals
    the namespace, that exact declaration wins. Otherwise every suffix-matching
    directory is a competing candidate: repository path depth is not evidence of
    a C# project boundary, because this resolver does not parse project references.
    An external namespace (`Newtonsoft.Json`) whose dotted tail is no repo dir
    matches nothing.

    Returns ``(files, matching_directory_count)``. Multiple files in ONE
    matched directory are exact namespace contents; files unioned from two
    suffix-matching directories are competing candidates and must be marked
    ambiguous by the orchestrator.
    """
    ns = raw.strip().strip("\"'")
    if "." not in ns:                                # single-segment → BCL/external (no local dir)
        return set(), 0

    def _matching_dirs(name):
        """Exact canonical match, otherwise every unproven suffix candidate."""
        exact = [
            directory
            for directory, (canonical, _) in cs_dirs.items()
            if canonical == name
        ]
        if exact:
            return exact
        return [
            directory
            for directory, (canonical, _) in cs_dirs.items()
            if canonical.endswith("." + name)
        ]

    def _files_in(directories):
        out = set()
        for directory in directories:
            out.update(cs_dirs[directory][1])
        return out

    matched_dirs = _matching_dirs(ns)
    files = _files_in(matched_dirs)
    if files:
        return files, len(matched_dirs)
    # TYPE-qualified `using` (an ALIAS `using X = A.B.C.Type;` or `using static A.B.C.Type;`): the FULL
    # path names a TYPE, not a directory, so the dir match above misses. Drop the LAST segment (the type),
    # match the namespace `A.B.C` to its directory, and select the ONE `Type.cs` file in it — a PRECISE
    # single-file resolution (the real win over the old bare-alias basename fan-out to every same-named
    # file). Recall-safe: if no `Type.cs` exists in the dir, return nothing (inert, never a false edge).
    head, _, last = ns.rpartition(".")
    if "." in head:                                  # need a multi-segment namespace head to bind a dir
        typed_dirs = []
        typed = set()
        for directory in _matching_dirs(head):
            matches = {
                f for f in cs_dirs[directory][1]
                if f.rsplit("/", 1)[-1] == last + ".cs"
            }
            if matches:
                typed_dirs.append(directory)
                typed.update(matches)
        if typed:
            return typed, len(typed_dirs)
    return set(), 0


def _resolve_csharp_ns(raw, cs_dirs):
    """Backward-compatible set-only wrapper for direct resolver callers."""
    files, _matching_directory_count = _resolve_csharp_ns_detail(
        raw,
        cs_dirs,
    )
    return files


def _find_abs_root_from_nodes(nodes):
    """Infer the absolute repo root from the call stack.

    `build_graph(root)` passes `root` as its first argument, then calls `_resolve_imports(nodes, edges)`.
    Since `_resolve_imports` cannot receive `root` directly (callers outside the file lane cannot be
    modified), we recover it from the call stack: walk up the Python frame stack looking for a local
    variable named `root` whose value is a directory that contains one of the Cargo.toml paths present
    in `nodes`. This is bounded (frame count is small) and validated (checked on disk).

    Returns the abs_root string, or None if not found. Never raises."""
    import sys
    # Gather candidate Cargo.toml paths (repo-relative) from config_file nodes.
    cargo_rels = [n["path"] for n in nodes
                  if n.get("kind") in ("config_file", "file")
                  and (n.get("path", "").endswith("/Cargo.toml") or n.get("path") == "Cargo.toml")]
    if not cargo_rels:
        return None
    # Shallowest (root) Cargo.toml is the most likely to exist at abs_root/path.
    root_cargo_rel = min(cargo_rels, key=lambda p: p.count("/"))
    try:
        frame = sys._getframe(2)       # _find_abs_root_from_nodes ← _rust_workspace_crate_map ← _resolve_imports ← build_graph
        depth = 0
        while frame is not None and depth < 20:
            cand = frame.f_locals.get("root")
            if isinstance(cand, str) and os.path.isdir(cand):
                if os.path.isfile(os.path.join(cand, root_cargo_rel)):
                    return cand
            frame = frame.f_back
            depth += 1
    except Exception:
        pass
    return None


def _find_abs_root_npm(nodes):
    """Infer the absolute repo root for an npm/JS/TS repo from the call stack.

    Analogous to `_find_abs_root_from_nodes` but uses `package.json` paths (present in
    config_file nodes for npm repos) as the anchor instead of Cargo.toml. Walks up the
    Python frame stack looking for a local variable `root` that is a directory containing
    one of the `package.json` paths present in the graph nodes.

    Returns the abs_root string, or None if not found. Never raises."""
    import sys
    pkg_rels = [n["path"] for n in nodes
                if n.get("kind") in ("config_file", "file")
                and (n.get("path", "").endswith("/package.json") or n.get("path") == "package.json")]
    if not pkg_rels:
        return None
    # Use the shallowest (root) package.json as the anchor for validation.
    root_pkg_rel = min(pkg_rels, key=lambda p: p.count("/"))
    try:
        frame = sys._getframe(2)   # _find_abs_root_npm ← _scoped_workspace_pkg_map ← _resolve_imports ← build_graph
        depth = 0
        while frame is not None and depth < 20:
            cand = frame.f_locals.get("root")
            if isinstance(cand, str) and os.path.isdir(cand):
                if os.path.isfile(os.path.join(cand, root_pkg_rel)):
                    return cand
            frame = frame.f_back
            depth += 1
    except Exception:
        pass
    return None


def _rust_workspace_crate_map(files, nodes):
    """Build a crate-name → source-directory map for a Rust Cargo workspace.

    WHY THIS EXISTS (measured MED recall miss, ~66-93 unresolved cross-crate imports on ripgrep):
      A Cargo workspace has member crates (e.g. `crates/matcher` = package `grep-matcher`). A file
      in a sibling crate doing `use grep_matcher::Matcher;` is importing a LOCAL workspace member,
      but the resolver has no knowledge of this — it can't match `grep_matcher` to
      `crates/matcher/src/lib.rs`. Those imports stay unresolved (inert). This function builds the
      crate-name → src-dir map so the resolver can route cross-crate imports to the member's source.

    CONTENT-FREE: only repo file PATHS are used (to locate `Cargo.toml` files). The package name
    is extracted from the manifest body (a `name = "..."` line) — this is unavoidable, but is
    still content-free in the Veripsa sense: it's a CRATE NAME (like a module name), not user code.
    Cargo.toml reading is bounded (tiny files, stops after the relevant section).

    PRECISION: only accept a crate name that maps to a UNIQUE member source dir. A crate name that
    is NOT in the workspace stays unresolved (it's a real external dep — correct non-resolve).
    NEVER-CRASH: a missing/malformed Cargo.toml → that member is skipped; no workspace map if the
    root Cargo.toml is absent or has no [workspace] section → behaves as before (empty map).

    CARGO NAME vs RUST NAME: Cargo package names use hyphens (`grep-matcher`) but Rust import
    names use underscores (`grep_matcher`). Both forms are added to the map as aliases.

    Returns {crate_name_lower: member_src_dir} where member_src_dir is a REPO-RELATIVE path like
    `crates/matcher/src`. Only the default src layout (member_dir/src/) is checked; a `[lib] path`
    override would require reading more of the manifest — out of scope (extremely rare in practice).
    Empty dict when the repo is not a Cargo workspace (no root Cargo.toml or no [workspace])."""
    try:
        abs_root = _find_abs_root_from_nodes(nodes)
        if not abs_root:
            return {}
        return _rust_ws_map_impl(files, nodes, abs_root)
    except Exception:
        return {}


def _rust_ws_map_impl(files, nodes, abs_root):
    """Implementation: never-crash caller wraps this."""
    # Locate root Cargo.toml (repo-relative path with fewest segments). Look in both source file
    # paths and config_file nodes (Cargo.toml is a config_file in the graph, not a source file).
    all_paths = list(files) + [n["path"] for n in nodes
                                if n.get("kind") == "config_file" and n.get("path")]
    cargo_paths = [p for p in all_paths if p == "Cargo.toml" or p.endswith("/Cargo.toml")]
    if not cargo_paths:
        return {}
    root_cargo_rel = min(cargo_paths, key=lambda p: p.count("/"))
    root_dir_rel = root_cargo_rel.rsplit("/", 1)[0] if "/" in root_cargo_rel else ""

    root_cargo_abs = os.path.join(abs_root, root_cargo_rel)
    if not os.path.isfile(root_cargo_abs):
        return {}

    # Parse [workspace] members from root Cargo.toml (minimal line-by-line parse).
    members = _parse_workspace_members(root_cargo_abs)
    if not members:
        return {}

    # Build crate-name → repo-relative src-dir.
    crate_map = {}
    files_set = set(files)
    for member_rel in members:
        # member_rel is relative to root_dir_rel (where root Cargo.toml lives).
        if root_dir_rel:
            member_from_root = root_dir_rel + "/" + member_rel
        else:
            member_from_root = member_rel
        member_cargo_abs = os.path.join(abs_root, member_from_root, "Cargo.toml")
        if not os.path.isfile(member_cargo_abs):
            continue
        pkg_name, lib_name = _parse_cargo_package_name(member_cargo_abs)
        if not pkg_name:
            continue

        # The Rust crate name: [lib] name overrides [package] name; hyphens → underscores.
        rust_name = (lib_name or pkg_name).replace("-", "_")
        # Also add the hyphenated form as a key (Rust imports always use underscores, but
        # keeping both ensures robustness if someone uses hyphens in imports — rare but safe).
        cargo_name = pkg_name.replace("_", "-")

        # Source directory: conventionally `<member>/src/`. Fall back to `<member>/` if no src/.
        src_rel = member_from_root + "/src"
        if not any(p == src_rel or p.startswith(src_rel + "/") for p in files_set):
            src_rel = member_from_root
        if not any(p == src_rel or p.startswith(src_rel + "/") for p in files_set):
            continue  # member has no source files in the graph — skip

        crate_map[rust_name] = src_rel
        if cargo_name != rust_name:
            crate_map[cargo_name] = src_rel

    return crate_map


def _parse_workspace_members(cargo_toml_path):
    """Parse [workspace].members from a Cargo.toml file. Returns list of member dir paths
    (relative to the directory containing the Cargo.toml). Expands simple `crates/*` globs.
    Returns [] when no [workspace] section or on any error. Never raises.

    Minimal hand-parser: reads only until it sees another top-level [section] after [workspace],
    or EOF. Does not handle multi-line TOML arrays split across many lines beyond the simple
    `members = [ ... ]` inline or bracket-per-line forms common in Cargo workspaces."""
    try:
        cargo_dir = os.path.dirname(cargo_toml_path)
        with open(cargo_toml_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    in_workspace = False
    in_members = False
    members_raw = []
    bracket_depth = 0

    for line in lines:
        stripped = line.strip()
        # Detect section headers (top-level only: `[foo]` not `[foo.bar]` unless it IS `[workspace]`).
        if stripped.startswith("[") and not stripped.startswith("[["):
            section = stripped.lstrip("[").rstrip("]").strip()
            if section == "workspace":
                in_workspace = True
                in_members = False
                bracket_depth = 0
                continue
            elif in_workspace:
                # Another top-level section → end of [workspace].
                break
            continue

        if not in_workspace:
            continue

        # Look for `members = [...]` line(s).
        if not in_members:
            if stripped.startswith("members"):
                # Could be `members = ["a", "b"]` (inline) or `members = [` (multi-line).
                rest = stripped.split("=", 1)[1].strip() if "=" in stripped else ""
                bracket_depth = rest.count("[") - rest.count("]")
                # Extract quoted strings from what we have so far.
                members_raw.extend(_extract_quoted(rest))
                if bracket_depth <= 0:
                    break  # complete on this line
                in_members = True
        else:
            bracket_depth += stripped.count("[") - stripped.count("]")
            members_raw.extend(_extract_quoted(stripped))
            if bracket_depth <= 0:
                break

    # Expand globs relative to the cargo_dir; keep only directories that exist.
    result = []
    for raw in members_raw:
        raw = raw.strip().strip("\"'")
        if not raw:
            continue
        if "*" in raw or "?" in raw:
            pattern = os.path.join(cargo_dir, raw)
            for expanded in sorted(glob.glob(pattern)):
                if os.path.isdir(expanded):
                    result.append(os.path.relpath(expanded, cargo_dir).replace(os.sep, "/"))
        else:
            result.append(raw)
    return result


def _extract_quoted(s):
    """Extract all double- or single-quoted strings from `s`. Simple scan; handles common TOML."""
    out = []
    i = 0
    while i < len(s):
        if s[i] in ('"', "'"):
            q = s[i]
            j = s.find(q, i + 1)
            if j > i:
                out.append(s[i + 1:j])
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    return out


def _parse_cargo_package_name(cargo_toml_path):
    """Parse [package].name and optionally [lib].name from a member Cargo.toml.

    Returns (pkg_name, lib_name) where lib_name may be None. Both are strings.
    Returns ("", None) on error or if [package] not found. Never raises."""
    try:
        with open(cargo_toml_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ("", None)

    pkg_name = ""
    lib_name = None
    in_package = False
    in_lib = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            section = stripped.lstrip("[").rstrip("]").strip()
            in_package = (section == "package")
            in_lib = (section == "lib")
            continue
        if in_package and stripped.startswith("name"):
            val = _toml_string_value(stripped)
            if val:
                pkg_name = val
                in_package = False  # got what we need from [package]
        if in_lib and stripped.startswith("name"):
            val = _toml_string_value(stripped)
            if val:
                lib_name = val
                in_lib = False

    return (pkg_name, lib_name)


def _toml_string_value(line):
    """Extract the string value from a TOML `key = "value"` line. Returns "" if not found."""
    if "=" not in line:
        return ""
    val = line.split("=", 1)[1].strip().strip("#").strip()
    # Handle inline comment: `name = "foo" # comment`
    for q in ('"', "'"):
        if val.startswith(q):
            end = val.find(q, 1)
            if end > 0:
                return val[1:end]
    return ""


def _scoped_workspace_pkg_map(files, nodes):
    """Build a scoped-package-name → source-directory map for JS/TS monorepos.

    WHY THIS EXISTS (measured MED recall miss, huge JS/TS ecosystem):
      NestJS / Angular nx / Turborepo / Lerna monorepos host many workspace packages
      under `packages/*/`, `libs/*/`, `apps/*/` etc., each with a `package.json`
      declaring `"name": "@scope/pkg"`. Files within the SAME repo import these packages
      by their scoped name (`@nestjs/common`, `@myorg/utils`), but the resolver skipped
      ALL `@`-prefixed imports outright (the `@` guard in `_own_packages`). On a real
      NestJS monorepo this left 1,763 cross-package imports from 1,030 files at 0%
      resolution.

    CONTENT-FREE: only package NAMES and directory PATHS are used. The package name IS
    read from the `package.json` body (unavoidable — the scoped name `@org/pkg` cannot be
    inferred from directory layout alone), but it is a MANIFEST IDENTIFIER (like a Cargo
    crate name), not user code. Identical precedent to `_rust_workspace_crate_map`.

    PRECISION / corroboration guard: a scoped import `@org/pkg/sub` is treated as local
    ONLY when:
      1. a local `package.json` declares `"name": "@org/pkg"` (the scope matches), AND
      2. the target subpath actually resolves to a real local file under that package's
         source tree (see the call site in `_resolve_imports`).
    An external dep (`@nestjs/common` when NOT locally declared, `@babel/core`, `@types/node`)
    that is never hosted in the repo's own `package.json` files simply never enters the map
    and stays unresolved (correct non-resolve).

    NEVER-CRASH: a missing / malformed / non-JSON `package.json` → that entry is skipped;
    no entry means the import is not resolved (safe).

    Returns {scoped_name: {"roots": [src_dirs...], "entry": entry_file_or_None}} where
    scoped_name is the full `@scope/pkg` string as declared. Empty when no scoped local
    packages are found."""
    abs_root = _find_abs_root_npm(nodes)
    if not abs_root:
        return {}
    try:
        return _scoped_ws_map_impl(files, abs_root)
    except Exception:
        return {}


def _scoped_ws_map_impl(files, abs_root):
    """Implementation — never-crash wrapper above calls this."""
    import json

    fset = set(files)
    # Locate all package.json paths among the graph's config_file nodes.
    # They are already enumerated as repo-relative paths in `files` (actually passed
    # from manifest_paths in the caller); but here we operate on ALL source files to
    # also find the package dirs. We discover package.json files by inspecting the file
    # tree under the abs_root, bounded to one-level-past the conventional monorepo dirs.
    # We ONLY look in canonical monorepo member directories: packages/*, libs/*, apps/*,
    # modules/*, projects/* (the five most common roots across NestJS/nx/Lerna/Turborepo).
    scoped_map = {}
    _MONO_ROOTS = ("packages", "libs", "apps", "modules", "projects")
    # dirs that are never a workspace member at the repo root (skip them in the top-level scan)
    _SKIP_TOPLEVEL = {"node_modules", ".git", "dist", "build", "out", "vendor",
                      ".venv", "venv", "__pycache__", ".next", ".turbo", "coverage"}

    def _register_pkg(pkg_dir_abs):
        """Read ONE candidate package dir's package.json; if it declares a SCOPED name (@scope/pkg)
        register its source roots + entry into scoped_map. SAME logic + SAME precision guards for both
        the conventional-monorepo-root scan and the top-level-workspace-member scan, so neither can
        introduce an edge an external/undeclared package would (the name must be scoped and the roots
        must be real local dirs/files)."""
        if not os.path.isdir(pkg_dir_abs):
            return
        pkg_json_abs = os.path.join(pkg_dir_abs, "package.json")
        if not os.path.isfile(pkg_json_abs):
            return
        try:
            with open(pkg_json_abs, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except Exception:
            return                                # malformed JSON → skip, never crash
        pkg_name = (data.get("name") or "").strip()
        if not pkg_name.startswith("@") or "/" not in pkg_name:
            return                                # not a scoped name → skip
        pkg_rel = os.path.relpath(pkg_dir_abs, abs_root).replace(os.sep, "/")
        if pkg_rel == "." or pkg_rel.startswith(".."):
            return                                # the repo root itself / outside the tree is never a member
        # Source roots: prefer `src/` under the package dir if it holds source files, else the dir itself.
        src_rel = pkg_rel + "/src"
        roots = []
        if any(p == src_rel or p.startswith(src_rel + "/") for p in fset):
            roots.append(src_rel)
        roots.append(pkg_rel)
        # Entry: `src/index.ts` (or .js) if it exists, else the package dir.
        entry = None
        for r in roots:
            for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
                cand = r + "/index" + ext
                if cand in fset:
                    entry = cand
                    break
            if entry:
                break
        rec = scoped_map.setdefault(pkg_name, {"roots": [], "entry": None})
        for r in roots:
            if r not in rec["roots"]:
                rec["roots"].append(r)
        if entry and not rec["entry"]:
            rec["entry"] = entry

    # (1) conventional monorepo member dirs: packages/*, libs/*, apps/*, modules/*, projects/*
    for mono_root in _MONO_ROOTS:
        mono_abs = os.path.join(abs_root, mono_root)
        if not os.path.isdir(mono_abs):
            continue
        try:
            entries = os.listdir(mono_abs)
        except OSError:
            continue
        for entry in entries:
            _register_pkg(os.path.join(mono_abs, entry))

    # (2) TOP-LEVEL workspace members: repos like Directus declare named packages at the repo ROOT
    # itself (e.g. sdk/, api/, app/ — pnpm-workspace.yaml / package.json "workspaces" members NOT nested
    # under packages/*), which the _MONO_ROOTS scan never sees (it only lists the five conventional roots,
    # never abs_root's own children). Scan abs_root's DIRECT children (depth 1) through the SAME
    # _register_pkg — the scoped-name + local-resolution guards keep it recall-safe (an external package
    # declared nowhere locally registers nothing). The repo root + obvious non-member dirs are excluded.
    try:
        root_entries = os.listdir(abs_root)
    except OSError:
        root_entries = []
    for entry in root_entries:
        if entry in _SKIP_TOPLEVEL or entry in _MONO_ROOTS:
            continue                             # _MONO_ROOTS handled in (1); skip vendor/build dirs
        _register_pkg(os.path.join(abs_root, entry))

    return scoped_map


def _suffix_keys(ne):
    """Every PATH-SEGMENT SUFFIX of a no-ext file path `ne` — the set of `mod` strings that the
    original scan's `ne == mod OR ne.endswith('/'+mod)` test would accept for this file. For
    `a/b/c` that is {`a/b/c`, `b/c`, `c`}. This is the key insight that turns the O(edges × files)
    endswith scan into O(1) dict lookups: a file is reachable by exactly its segment-suffixes, so we
    index each file under all of them ONCE, then resolve each import by a single dict probe.
    Segment-anchored by construction (we only cut at '/'), so a trailing `/config` never matches
    `myconfig` — identical semantics to the endswith('/'+mod) guard it replaces."""
    parts = ne.split("/")
    return ["/".join(parts[i:]) for i in range(len(parts)) if parts[i:]]


class _ResolveCtx:
    """Immutable per-build resolution index, computed ONCE by `_build_resolution_index` and
    threaded read-only through the candidate-generation helpers. Holds exactly the lookup
    structures the old single-function body kept as locals — extracting them into one object
    keeps each phase helper's signature small while the heavy indexes are still built one time.

    Fields (all content-free — paths / module names only):
      by_path     : exact repo-relative path set (preserves an explicit import extension)
      by_noext    : exact no-ext path → deterministic legacy representative
      by_noext_all: exact no-ext path → {files}; preserves cross-language stems
      alias       : basename / dotted alias → {files}  (bare-import fallback)
      by_suffix   : path-segment-suffix → {files} (the O(1) endswith-scan replacement)
      go_suffix   : Go package-dir index (IS go_dirs; see _go_pkg_by_suffix)
      go_dirs     : .go-bearing dir → its non-test .go files
      cs_dirs     : .cs-bearing dir → (dotted-canon, its .cs files)
      rust_ws_map : Rust workspace crate-name → repo-rel src-dir
      own_pkgs    : own published package name → {roots, entry, entries}
      scoped_pkgs : `@scope/pkg` → {roots, entry}  (npm monorepo members)"""
    __slots__ = ("by_path", "by_noext", "by_noext_all", "alias", "by_suffix",
                 "go_suffix", "go_dirs", "cs_dirs", "rust_ws_map", "own_pkgs",
                 "scoped_pkgs")

    def __init__(self, by_path, by_noext, by_noext_all, alias, by_suffix,
                 go_suffix, go_dirs, cs_dirs, rust_ws_map, own_pkgs,
                 scoped_pkgs):
        self.by_path = by_path
        self.by_noext = by_noext
        self.by_noext_all = by_noext_all
        self.alias = alias
        self.by_suffix = by_suffix
        self.go_suffix = go_suffix
        self.go_dirs = go_dirs
        self.cs_dirs = cs_dirs
        self.rust_ws_map = rust_ws_map
        self.own_pkgs = own_pkgs
        self.scoped_pkgs = scoped_pkgs


def _build_resolution_index(nodes, edges):
    """PHASE 1 (build-once): precompute every lookup structure import resolution needs, returned
    as a `_ResolveCtx`. This is the body that used to open `_resolve_imports`; it is pure setup
    with no per-edge work.

    PERF (the extractor's #1 scale cost): the prior version scanned the ENTIRE file universe with
    `endswith` for EVERY import edge → O(edges × files). On a real mid-size repo (Django: ~110k
    import edges × ~3.4k files) that was ~2.5e8 endswith calls and ~83% of build_graph's wall time
    (measured). Here we precompute a SUFFIX INDEX (`by_suffix`: every path-segment-suffix of every
    file → the files reachable by it, incl. the dir-import `/index` + `/__init__` forms) ONCE, so
    each edge resolves by a handful of O(1) dict probes instead of a full scan. Output is identical
    (the suffix keys are exactly the strings the endswith test accepted — see _suffix_keys)."""
    files = [n["path"] for n in nodes if n.get("kind") == "file"]
    go_dirs = _go_pkg_dirs(files)                    # .go-bearing dir → its .go files (Go packages)
    go_suffix = _go_pkg_by_suffix(go_dirs)           # O(1) suffix index for Go pkg dir resolution (PERF)
    cs_dirs = _cs_ns_dirs(files)                      # .cs-bearing dir → (dotted-canon, its .cs files)
    rust_ws_map = _rust_workspace_crate_map(files, nodes)  # Rust workspace: crate-name → repo-rel src-dir
    # Git permits same-stem files with different extensions (foo.py + foo.ts).
    # Keep every candidate for exact extensionless resolution. The companion
    # scalar map remains for older specialized probes, but chooses a stable
    # representative so their behavior can never depend on Node input order.
    by_noext = {}
    by_noext_all = {}
    alias = {}                                       # basename / dotted → files (for bare imports)
    # by_suffix[s] = the files whose no-ext path is `s` or ends in `/s` — the O(1) replacement for the
    # per-edge `for ne, p in by_noext.items(): if ne == mod or ne.endswith('/'+mod) ...` scan. We also
    # fold in the dir-import forms: a file `…/mod/index` (or `…/mod/__init__`) is reachable by `mod`,
    # so we index it under `mod` too (its suffix with the final `/index`|`/__init__` segment dropped).
    by_suffix = {}
    for p in files:
        ne = _noext(p)
        by_noext_all.setdefault(ne, set()).add(p)
        previous = by_noext.get(ne)
        if previous is None or p < previous:
            by_noext[ne] = p
        for k in {ne, ne.replace("/", "."), os.path.basename(ne), os.path.basename(p)}:
            if k:
                alias.setdefault(k, set()).add(p)
        for s in _suffix_keys(ne):
            by_suffix.setdefault(s, set()).add(p)
        # a `…/mod/index` / `…/mod/__init__` file ALSO resolves the bare module `mod` (folder import):
        # index it under each suffix with the trailing index/__init__ segment removed.
        for tail in ("/index", "/__init__"):
            if ne.endswith(tail):
                base = ne[: -len(tail)]
                if base:
                    for s in _suffix_keys(base):
                        by_suffix.setdefault(s, set()).add(p)
                else:                                # a top-level index/__init__ → reachable by '' is meaningless; skip
                    pass
    # OWN-PACKAGE NAMES (content-free, from the layout a manifest roots): a self-import by the repo's OWN
    # published name (`zustand/shallow`, bare `flask`) is the package importing its own code, NOT an external
    # dep. Recovered here ONCE; used below to (a) STRIP the leading own-name and resolve the remainder for
    # recall, and (b) resolve a bare own-name to the package ENTRY (never a same-basename decoy). Empty for a
    # repo with no package.json/pyproject → all the own-name logic below is then inert (a plain app is untouched).
    import_edges = [e for e in edges if e.get("kind") == "imports"]
    # Manifests (`package.json`/`pyproject.toml`/`setup.*`) are CONFIG_FILE nodes, not `kind=='file'`, so we
    # collect their PATHS from every node (content-free — a path, never the body). Their presence + directory
    # are all _own_packages needs (it recovers the name from the layout, never by parsing the manifest).
    _MAN = (_NPM_MANIFEST,) + _PY_MANIFEST
    manifest_paths = [n["path"] for n in nodes
                      if n.get("path") and n["path"].rsplit("/", 1)[-1] in _MAN]
    own_pkgs = _own_packages(
        files,
        manifest_paths,
        by_noext_all,
        import_edges,
    )
    # SCOPED WORKSPACE PACKAGES (`@scope/pkg`): npm monorepos (NestJS / nx / Lerna / Turborepo)
    # host member packages under `packages/*/`, `libs/*/`, etc., each declaring `"name": "@scope/pkg"`
    # in their own `package.json`. An import of `@scope/pkg/subpath` inside the same repo is a LOCAL
    # cross-package dependency that the resolver previously skipped (the `@` guard in `_own_packages`
    # blocked ALL scoped imports). We build the map here, once (abs_root is inferred from the call
    # stack via _find_abs_root_npm — its frame walk is depth-tolerant, so the extra helper frame
    # introduced by this extraction does not affect it). PRECISION guard at the call site: only
    # resolve when the target subpath maps to a real local file (external `@nestjs/common` when not
    # locally hosted simply doesn't appear in the map and stays unresolved — correct).
    scoped_pkgs = _scoped_workspace_pkg_map(files, nodes)
    return _ResolveCtx(
        frozenset(files), by_noext, by_noext_all, alias, by_suffix,
        go_suffix, go_dirs, cs_dirs, rust_ws_map, own_pkgs, scoped_pkgs,
    )


def _candidates_relative(e, raw, ctx):
    """PHASE 2a (candidate generation — RELATIVE imports `./x`, `../x`).

    RELATIVE import → resolve EXACTLY against the importer's directory. No basename
    fallback, so a frontend `./src/main.tsx` never collides with a backend `main.py`
    (real cross-language bug found by dogfood). Also try dir/index for folder imports."""
    by_noext_all = ctx.by_noext_all
    target_path = os.path.normpath(os.path.join(os.path.dirname(e["src"]), raw))
    # An author-supplied extension is an exact coordinate.  Preserve it before the
    # extensionless module probe so ``./theme.css`` can never bind to a same-stem
    # ``theme.ts`` (and vice versa) merely because directory enumeration saw it first.
    if target_path in ctx.by_path:
        return {target_path}
    src_lower = e["src"].lower()
    if src_lower.endswith(_STYLE_EXT):
        # Stylesheet resolution is dialect-specific. Never fall through to the generic
        # web-family same-stem index: `@use './tokens'` must not bind to `tokens.ts`.
        target_lower = target_path.lower()
        explicit_style_ext = next((ext for ext in _STYLE_EXT if target_lower.endswith(ext)), None)

        if src_lower.endswith(_SASS_EXT):
            parent, base = os.path.split(target_path)
            if explicit_style_ext:
                # Sass accepts an explicit .scss/.sass URL for a partial whose physical
                # basename starts with `_`. An existing direct file already won above.
                if explicit_style_ext in _SASS_EXT:
                    stem = target_path[: -len(explicit_style_ext)]
                    stem_parent, stem_base = os.path.split(stem)
                    partial = os.path.join(stem_parent, "_" + stem_base) + explicit_style_ext
                    return {partial} if partial in ctx.by_path else set()
                return set()

            # A dot in a Sass module basename is not necessarily an extension
            # (`theme.dark` -> `_theme.dark.scss`). Probe direct/partial modules first.
            direct_stems = [target_path]
            if base:
                direct_stems.append(os.path.join(parent, "_" + base))
            direct_hits = {
                stem + ext
                for stem in direct_stems
                for ext in (".scss", ".sass", ".css")
                if stem + ext in ctx.by_path
            }
            if direct_hits:
                return direct_hits

            # Sass consults a directory index only when no direct/partial module exists.
            return {
                stem + ext
                for stem in (os.path.join(target_path, "index"), os.path.join(target_path, "_index"))
                for ext in (".scss", ".sass", ".css")
                if stem + ext in ctx.by_path
            }

        # CSS/Less/Stylus treat an author-supplied suffix as an exact coordinate.
        # The exact file already won above; if it is absent, never invent `foo.php.less`
        # or substitute a different stylesheet dialect.
        if os.path.splitext(os.path.basename(target_path))[1]:
            return set()
        if src_lower.endswith(".css"):
            candidate = target_path + ".css"
            return {candidate} if candidate in ctx.by_path else set()
        if src_lower.endswith(".less"):
            candidate = target_path + ".less"
            return {candidate} if candidate in ctx.by_path else set()

        # Stylus resolves an extensionless import to `name.styl` first, then the
        # directory entry `name/index.styl`. A sibling `.css` is not a fallback.
        for candidate in (target_path + ".styl", os.path.join(target_path, "index.styl")):
            if candidate in ctx.by_path:
                return {candidate}
        return set()
    tgt = _noext(target_path)
    candidates = set(by_noext_all.get(tgt, ()))
    if not candidates:
        for key in (tgt + "/index", tgt + "/__init__"):
            candidates.update(by_noext_all.get(key, ()))
    # An extensionless relative import is local but not cross-language. Keep
    # every same-family stem candidate so two web files (foo.ts + foo.js)
    # become explicit ambiguity rather than first-wins; a Python decoy beside
    # foo.ts is excluded for a TypeScript importer. An author-supplied exact
    # extension already returned through by_path above and remains untouched.
    source_family = _family(e["src"])
    return {
        candidate
        for candidate in candidates
        if _family(candidate) == source_family
    }


def _candidates_scoped_workspace(e, raw, ctx):
    """Generate candidates for a declared scoped workspace coordinate.

    Returns ``(recognized_local_name, candidates)``. A declared package name is
    recognized even when its entry/subpath is absent; that distinction prevents
    a known-local miss from falling through to an unrelated repository-wide
    suffix match.

    PRECISION guard: only resolve when `@scope/pkg` is declared by a local package.json AND the
    resolved subpath maps to a real local file (external scoped deps like `@nestjs/common` when
    not locally hosted stay unresolved — correct non-resolve)."""
    scoped_pkgs = ctx.scoped_pkgs
    by_suffix = ctx.by_suffix
    by_noext_all = ctx.by_noext_all
    cands = set()
    if raw.startswith("@") and scoped_pkgs:
        # Parse the scoped name: `@scope/pkg` is the first two slash-delimited segments;
        # the rest (if any) is the subpath inside the package.
        norm_raw = raw.strip("\"'")
        parts_raw = norm_raw.split("/")
        # parts_raw[0] = "@scope", parts_raw[1] = "pkg", parts_raw[2:] = subpath segments
        if len(parts_raw) >= 2:
            scoped_name = parts_raw[0] + "/" + parts_raw[1]   # e.g. "@nestjs/common"
            scoped_rec = scoped_pkgs.get(scoped_name)
            if scoped_rec:
                sub_parts = parts_raw[2:]                      # may be empty
                if sub_parts:
                    sub = _mod_noext("/".join(sub_parts))      # strip trailing .ts/.js etc.
                    for root in scoped_rec["roots"]:
                        key = (root + "/" + sub) if root else sub
                        # Precision: only if the subpath resolves to a real local file.
                        hits = by_suffix.get(key, _EMPTY)
                        if hits:
                            cands |= hits
                        # Also try the exact no-ext path.
                        cands.update(by_noext_all.get(key, ()))
                        # And the folder-index form (`@scope/pkg/utils` → `.../utils/index.ts`).
                        for idx_tail in ("/index", "/__init__"):
                            idx_key = key + idx_tail
                            cands.update(by_noext_all.get(idx_key, ()))
                else:
                    # Bare `@scope/pkg` with no subpath → resolve to the package entry.
                    if scoped_rec["entry"]:
                        cands.add(scoped_rec["entry"])
                if cands:
                    # Applied family filter (same as the general path below —
                    # web only for .ts/.js). Keep a possible self candidate;
                    # _emit_resolution removes it after recording that a real
                    # repository target existed.
                    cands = {
                        c
                        for c in cands
                        if _family(c) == _family(e["src"])
                    }
                return True, cands
    return False, set()


def _resolve_scoped_workspace_edge(e, raw, ctx):
    """Resolve a declared ``@scope/pkg[/sub]`` or return ``None`` if external."""
    recognized, candidates = _candidates_scoped_workspace(e, raw, ctx)
    if not recognized:
        return None
    return _emit_resolution(
        e,
        candidates,
        unresolved_if_empty=True,
    )


def _candidates_bare(e, raw, ctx):
    """PHASE 2c (candidate generation — the GENERAL bare / dotted / qualified path).

    Handles everything that is neither a `./`-relative import nor a short-circuited scoped
    import: namespace normalization, Rust self/super/crate + workspace, own-package self-import,
    the O(1) suffix probe, and the per-language recall fallbacks (Rust/PHP qualified-path drop,
    Rust crate type-name, Go package-dir, C# namespace, bare basename). Returns the candidate
    file set, `bare_single` (whether `mod` ended up single-segment), whether
    multiple targets are the exact contents of one Go/C# package directory
    rather than competing candidates, and whether an inferred own-package
    coordinate proves the raw reference is repository-local even when no target
    exists. The tie-break phase needs `bare_single` for the Ruby-unique and
    fan-out degree guards."""
    by_noext_all = ctx.by_noext_all
    alias = ctx.alias
    by_suffix = ctx.by_suffix
    go_suffix = ctx.go_suffix
    go_dirs = ctx.go_dirs
    cs_dirs = ctx.cs_dirs
    rust_ws_map = ctx.rust_ws_map
    own_pkgs = ctx.own_pkgs
    cands = set()
    # Go imports and C# using directives name a package/namespace DIRECTORY.
    # Returning every importable file in that one resolved directory is the
    # exact language semantics, not a set of competing candidates.  Keep this
    # bit separate from ``len(cands)`` so the generic ambiguity contract does
    # not demote legitimate package-directory fan-out.
    exact_directory_fanout = False
    known_local_reference = False
    # bare / scoped (npm @alias, python pkg path) — resolve by PATH-SEGMENT SUFFIX so a
    # dotted/aliased module names the file it points at: `app.core.config` → …/app/core/
    # config.py (python package root, repo-rooted elsewhere), `@/client` → …/client/
    # index.ts (dir import). Segment-anchored: a trailing /config never matches myconfig.py.
    # External pkgs (react, sqlmodel) name no local file → stay inert. Falls back to bare
    # basename for single-segment names.
    # Normalize the import to a path: strip a real FILE extension if present (a C include
    # `<a/b.hpp>` names a file), then dots→slashes. _mod_noext strips ONLY real source/header
    # extensions, so a dotted module's last segment (`com.google.gson.Gson` → keep `Gson`,
    # `app.core.config` → keep `config`) is preserved — the earlier blanket _noext wrongly
    # treated `.Gson`/`.config` as extensions and dropped the file the import named.
    angle = raw.startswith("<")          # C/C++ <header> = SYSTEM/library header (vs "local")
    # (SCOPED WORKSPACE `@scope/pkg` resolution ran first, in _resolve_scoped_workspace_edge —
    #  it either short-circuited this edge or left no scoped candidates, so we restart clean here.)
    # Normalize EVERY namespace separator to '/': `.` (Java/C#/python), `::` (Rust), `\` (PHP).
    # A Rust/PHP qualified import (`crate::auth::login`, `App\Models\User`) is a fully-qualified
    # path that names ONE file — without splitting `::`/`\` only the last segment survived and
    # basename-fanned-out to every same-named file (the Java-FQN bug, now also fixed for Rust/PHP).
    qualified_path = ("::" in raw) or ("\\" in raw)   # Rust/PHP qualified path → enable suffix-drop below
    mod = (_mod_noext(raw.lstrip("@~/").strip("<>\"'"))
           .replace("::", "/").replace("\\", "/").replace(".", "/").strip("/"))
    # Rust path roots: `crate`/`self`/`super` are not directory segments. `self`/`super` are
    # RELATIVE to the importer's module — resolve them against the importer's DIRECTORY (exactly
    # like a JS `./`/`../` import) so `use super::num::*` from `src/lexical/float.rs` resolves to
    # `src/lexical/num.rs` (its sibling module) and NEVER basename-fans-out to `tests/lexical/num.rs`
    # (real-repo audit on serde_json: the `super::`/`self::` wildcards were the dominant src→test
    # false couplings). A `mod.rs`/`lib.rs`/`main.rs` IS its module dir, so `super` climbs from the
    # file's directory; a `name.rs` is a module INSIDE its directory, so `super` (parent module) is
    # that same directory. `crate` is the crate root — strip it and resolve the tail by global
    # suffix below (we don't know the crate-root dir content-free; the suffix probe handles it).
    if e["src"].endswith(".rs"):
        segs = mod.split("/")
        if segs and segs[0] in ("self", "super"):
            known_local_reference = True
            base = os.path.basename(e["src"])
            src_dir = os.path.dirname(e["src"])
            # MODULE DIRECTORY of the importer. A `name.rs` is a leaf module INSIDE its directory,
            # so its OWN module items (`self`) live in `src_dir` and its PARENT module (`super`) is
            # also `src_dir` (the dir holds the sibling modules). A `mod.rs`/`lib.rs`/`main.rs` IS
            # its directory's module, so `self` = `src_dir` but `super` (its PARENT module) is the
            # PARENT directory. So: name.rs → first super anchors at src_dir; mod.rs → first super
            # climbs to the parent dir. Each EXTRA `super` climbs one more dir.
            is_mod_file = base in ("mod.rs", "lib.rs", "main.rs")
            climb = src_dir
            i = 0
            while i < len(segs) and segs[i] in ("self", "super"):
                if segs[i] == "super" and (i > 0 or is_mod_file):
                    climb = os.path.dirname(climb)
                i += 1
            tail = "/".join(segs[i:])
            rel_target = _noext(os.path.normpath(os.path.join(climb, tail))) if tail else None
            if rel_target:
                cands = {
                    candidate
                    for candidate in by_noext_all.get(rel_target, ())
                    if _family(candidate) == _family(e["src"])
                }
            if rel_target and not cands:     # folder module: …/tail/mod
                modfile = _noext(os.path.normpath(os.path.join(climb, tail, "mod.rs")))
                cands = {
                    candidate
                    for candidate in by_noext_all.get(modfile, ())
                    if _family(candidate) == _family(e["src"])
                }
                if not cands and "/" not in tail:
                    # SUPER/SELF TRAILING ITEM-NAME fallback (recall miss measured on tokio: 36 dropped
                    # `super::ReadBuf`/`super::TcpListener`/`super::Inject` edges). When the tail is a SINGLE
                    # identifier that is NOT a submodule (no `…/tail.rs`, no `…/tail/mod.rs`), it is an ITEM
                    # (a type/fn/const re-exported or defined in the PARENT module itself) — Rust's name
                    # resolution falls through `super::X` to "an item X in the parent module". The parent
                    # module is the FILE for `climb`: either `climb/mod.rs` (folder-module convention) or
                    # `climb.rs` (a flat module whose submodules live in `climb/`). Probe both and accept a
                    # UNIQUE match, discarding a self-reference. This MIRRORS the `crate::` trailing
                    # type-name fallback (below) for the relative root, and runs ONLY after submodule
                    # resolution missed, so a real `super::submod` still wins its sibling FILE first.
                    # PRECISION/RECALL-SAFE: single-identifier tail only (a multi-seg `super::a::B` resolves
                    # its `a` submodule above and never reaches here); unique-match only — never fans out.
                    parent_mod = _noext(os.path.normpath(os.path.join(climb, "mod.rs")))
                    parent_flat = _noext(os.path.normpath(climb)) if climb else None
                    pcands = set()
                    pcands.update(by_noext_all.get(parent_mod, ()))
                    if parent_flat:
                        pcands.update(by_noext_all.get(parent_flat, ()))
                    pcands = {
                        candidate
                        for candidate in pcands
                        if _family(candidate) == _family(e["src"])
                    }
                    pcands.discard(e["src"])    # `super::X` from the parent file itself is not a cross-file edge
                    if len(pcands) == 1:        # unique parent module file → trust it (precision guard)
                        cands = pcands
            mod = ""                        # relative root consumed → skip global suffix/basename (no src→test fan-out)
        else:
            saw_crate_root = False
            while segs and segs[0] == "crate":
                segs = segs[1:]
                saw_crate_root = True
            if saw_crate_root:
                known_local_reference = True
            mod = "/".join(segs)
    # A bare SINGLE-segment import in a language whose LOCAL imports are ALWAYS multi-segment
    # (Go: a local import is the full module path `github.com/org/repo/pkg`) is necessarily the
    # stdlib (`time`, `fmt`, `strings`) and names NO local file. Resolving it — by suffix match
    # to `…/pkg/time.go` OR by bare basename — is a pure false coupling. Real-repo audit on hugo:
    # 682 false `strings.go` edges, 339 `fmt.go`, 316 `context.go`, … (thousands). No recall lost:
    # a real local Go import is multi-segment and resolves through the suffix loop below.
    # Likewise a SINGLE-segment ANGLE-bracket C/C++ include (`<array>`, `<vector>`, `<string>`)
    # is the standard library — never a local file. (A LOCAL include is quoted: `"auth.h"`.)
    # Real-repo audit on nlohmann/json: `#include <array>` falsely matched docs/examples/array.cpp.
    bare_single = "/" not in mod
    if bare_single and (_no_bare_basename(e["src"]) or angle):
        mod = ""
    # OWN-PACKAGE SELF-IMPORT (content-free). `mod` is the dot/slash-normalized import path with the
    # own scope/separators already folded. Its FIRST segment is the candidate package name.
    own_seg0 = mod.split("/", 1)[0] if mod else ""
    own_rec = own_pkgs.get(own_seg0.lower()) if own_seg0 else None
    if own_rec and "/" in mod:
        known_local_reference = True
        # SELF-IMPORT BY OWN NAME `<own>/<sub>` (`zustand/shallow`, `zustand/middleware`): the FULL
        # path names no file (there is no `…/zustand/shallow`), so the suffix probe below misses and a
        # real intra-package edge is LOST. STRIP the leading own-name and resolve the REMAINDER against
        # the package SOURCE ROOTS (`src/`, the package dir) — exactly like a local subpath import.
        # Recall-biased (fan out to all matches), consistent with the resolver's existing bias. This is
        # the ONLY new edge an own-package introduces; an external `lodash/merge` is unaffected (its
        # first segment is not the own name).
        sub = mod.split("/", 1)[1]
        for root in own_rec["roots"]:
            key = (root + "/" + sub) if root else sub
            cands |= by_suffix.get(key, _EMPTY)        # resolves the remainder under the package source root
            cands.update(by_noext_all.get(key, ()))
        # The inferred own-name coordinate has been consumed. Never fall
        # through to a repository-wide suffix/basename probe: a missing own
        # subpath is unresolved local evidence, not permission to bind an
        # unrelated vendor/acme/subpath.ts.
        mod = ""
    elif own_rec and bare_single:
        known_local_reference = True
        # BARE OWN NAME `<own>` (`import flask` inside Flask): the basename fallback below would couple
        # it to a same-basename DECOY (a test-fixture `flask.py`) — measured 43 false edges on flask.
        # Resolve it to the package ENTRY (`src/flask/__init__.py`), the file the published name names,
        # NEVER a decoy. A known package with no entry remains raw unresolved
        # evidence. mod="" suppresses the generic basename fallback in both
        # cases.
        cands.update(
            own_rec.get("entries")
            or ((own_rec["entry"],) if own_rec["entry"] else ())
        )
        mod = ""
    if mod:
        # O(1) suffix-index probe (replaces the O(files) endswith scan). `by_suffix[mod]`
        # holds exactly the files whose no-ext path is `mod` or ends in `/mod`, PLUS the
        # `…/mod/index` and `…/mod/__init__` folder-import files (folded in at index time) —
        # i.e. precisely the set the old six-way endswith disjunction matched.
        cands |= by_suffix.get(mod, _EMPTY)
    if not cands and qualified_path and "/" in mod:
        # RUST/PHP qualified-path recall: the FULL path can miss because its LEADING segment is
        # a namespace root, not a directory — Rust's already-stripped `crate`, or a PHP PSR-4
        # namespace whose top segment differs in case from the on-disk dir (`App\Models\User`
        # under `app/Models/User.php`). Drop leading segments and probe each MULTI-segment tail,
        # taking the FIRST (most-specific) match: `App/Models/User` → miss → `Models/User` →
        # `app/Models/User.php`. We STOP before the bare last segment (the loop keeps ≥2 segments),
        # so this never re-introduces basename fan-out — `App\Models\User` resolves to
        # `app/Models/User.php` and NOT to an unrelated `lib/User.php`. Only fires for `::`/`\`
        # imports that the full-path probe left unresolved, so no other language changes.
        #
        # UNIQUENESS GUARD (mirrors the Rust crate:: / C# namespace fallbacks below/above): accept a
        # tail match ONLY when it resolves to exactly ONE file. A 2-segment tail like `Mapping/
        # ClassMetadata` is too generic in a large PSR-4 namespace — it suffix-matches MULTIPLE
        # unrelated files (`Serializer/Mapping/ClassMetadata.php` AND `Validator/Mapping/
        # ClassMetadata.php`). When the import that triggered the leading-drop is an EXTERNAL class
        # (`use Doctrine\…\ClassMetadata`, `use Twig\…\AbstractExtension`, Laravel's `use Symfony\…\
        # Application`), it names NO internal file at all, so EVERY such fan-out target is a FALSE
        # edge — the comment above already promised "NOT to an unrelated file", but the loop fanned
        # out to all matches. Because the suffix index is monotone (a shorter tail's match set is a
        # SUPERSET of any longer tail's — verified 0 violations on symfony+laravel), an ambiguous
        # most-specific tail can never disambiguate by shortening, so we suppress rather than continue
        # (the existing unresolved baseline is safer). Measured on symfony: 119 false fan-out matches
        # dropped, 81 unique recall kept; laravel: 11 dropped, 799 kept. RECALL-SAFE: a real qualified
        # import that names one internal file (Laravel `types/` stubs, a PSR interface re-implemented
        # at one path) still resolves uniquely; only the genuinely-ambiguous external/decoy fan-out is cut.
        segs = mod.split("/")
        for i in range(1, len(segs) - 1):          # drop 1..n-1 leading segs; keep ≥2-segment tail
            sub = by_suffix.get("/".join(segs[i:]), _EMPTY)
            if sub:
                if len(sub) == 1:                  # unique most-specific tail → trust it (precision guard)
                    cands |= sub
                break                              # ambiguous tail → suppress (shorter tails are only more generic)
    if not cands and e["src"].endswith(".rs") and "/" in mod:
        # RUST crate:: TRAILING TYPE-NAME fallback: `use crate::searcher::Searcher` strips
        # `crate` → mod=`searcher/Searcher`. No file is named `Searcher.rs`; the real module
        # is `searcher/mod.rs` (or `searcher.rs`). The leading-drop loop above does not help
        # here (it only drops LEADING segments; `searcher/Searcher` has no extra leading
        # segments to drop). We also try dropping the TRAILING segment (the type name) to
        # reach the CONTAINING MODULE file.
        # Two probes per shortened prefix (most-specific wins):
        #   1. `module_prefix/mod`  → `…/searcher/mod.rs` (the by_suffix key is `searcher/mod`,
        #      which is a direct suffix_key of the file; `searcher` alone is NOT indexed as a
        #      key because the folder-import folding at index time covers only `/index`/`/__init__`
        #      tails, not `/mod`. Rust uses `mod.rs` as its folder-module convention.)
        #   2. `module_prefix`      → `…/searcher.rs` (a flat module file alongside siblings)
        # We try them in that order and take the first non-empty, unique hit.
        # Measured on ripgrep: 85/87 crate:: imports resolve (the remaining 2 are
        # single-segment after crate:: strip, e.g. `crate::SearcherBuilder`, which are
        # correct non-resolves at this stage — the type lives in lib.rs/main.rs itself).
        # PRECISION: only accept a UNIQUE match (one file). If the prefix is ambiguous we do
        # NOT fan out; the existing baseline edge (unresolved) is safer.
        # EXTERNAL-CRATE / INLINE-MODULE GUARD (measured FALSE src->test on clap-rs/clap): the SAME
        # 2-segment shape arises for an EXTERNAL crate (`use roff::Roff` → `roff/Roff`) or an INLINE
        # module (`use markdown::f;` with a same-file `mod markdown {}` → `markdown/f`). There the
        # dropped-tail prefix (`roff`/`markdown`) is NOT a local directory — it basename-matches an
        # unrelated same-named file, and when the ONLY match is a TEST/EXAMPLE/BENCH file (a separate
        # Cargo compile target, never an in-crate module) the uniqueness guard was satisfied by that
        # lone decoy → a false production->test edge. Exclude separate-compile-target files from the
        # candidate set BEFORE the uniqueness check (the C# `_CS_TEST_SEG` / Go `_test.go` precedent).
        # RECALL-SAFE & a recall GAIN: a real in-crate module is never a test target, so removing the
        # decoy lets a genuine same-basename in-crate module resolve UNIQUELY (e.g. src/walk.rs winning
        # over an examples/walk.rs decoy that previously made the pair ambiguous → unresolved).
        # Scope: .rs only. Does NOT touch C#/Go/PHP branches.
        # NOTE: the cross-workspace-crate case (`grep_matcher::` from a sibling crate) is
        # handled by the RUST-WORKSPACE step immediately below.
        segs = mod.split("/")
        if len(segs) >= 2:
            module_prefix = "/".join(segs[:-1])    # drop the trailing type-name segment
            for probe in (module_prefix + "/mod", module_prefix):
                candidates = {c for c in by_suffix.get(probe, _EMPTY)
                              if not _rs_inert_module_target(c)}
                if len(candidates) == 1:           # unique non-target match only — precision guard
                    cands |= candidates
                    break
    if not cands and e["src"].endswith(".rs") and rust_ws_map and mod and "/" in mod:
        # RUST WORKSPACE-CRATE resolution: `use grep_matcher::Matcher;` from a sibling crate.
        # After `crate::` stripping, `mod` = `grep_matcher/Matcher`. The first segment
        # (`grep_matcher`) is a WORKSPACE-MEMBER crate name (from `rust_ws_map`), not a local
        # module directory — so the suffix probe above found nothing. Route the remainder
        # into the member's src-dir (`crates/matcher/src/`) and resolve using the SAME
        # suffix-index + mod.rs / lib fallback the intra-crate logic already handles.
        # PRECISION: only accept a UNIQUE match. A crate name not in the workspace map is a
        # real external dep (std, serde, …) — NOT probed, stays unresolved (correct).
        # SAME-LANGUAGE: all targets are .rs → family matches the .rs source (no filter needed).
        ws_segs = mod.split("/")
        ws_crate_name = ws_segs[0]
        ws_src_dir = rust_ws_map.get(ws_crate_name)
        if ws_src_dir:
            ws_tail = "/".join(ws_segs[1:]) if len(ws_segs) > 1 else ""
            ws_hits = set()
            if ws_tail:
                # Probe 1: full suffix in the member src-dir (e.g. `crates/matcher/src/lib`)
                ws_hits |= by_suffix.get(ws_src_dir + "/" + ws_tail, _EMPTY)
                # Probe 2: folder-module convention (e.g. `crates/matcher/src/sink/mod`)
                if not ws_hits:
                    ws_hits |= by_suffix.get(ws_src_dir + "/" + ws_tail + "/mod", _EMPTY)
                # Probe 3: drop trailing type-name (e.g. `grep_matcher/Matcher` → tail `Matcher`,
                # strip → try module `grep_matcher/src/lib.rs` via the no-tail path below)
                if not ws_hits and "/" not in ws_tail:
                    # single-segment tail → the type lives in lib.rs / mod.rs of this crate
                    ws_hits |= by_suffix.get(ws_src_dir + "/lib", _EMPTY)
                    if not ws_hits:
                        ws_hits |= by_suffix.get(ws_src_dir + "/mod", _EMPTY)
                if not ws_hits and "/" in ws_tail:
                    # multi-segment tail: also try dropping the final type-name segment
                    ws_mod_prefix = ws_tail.rsplit("/", 1)[0]
                    ws_hits |= by_suffix.get(ws_src_dir + "/" + ws_mod_prefix + "/mod", _EMPTY)
                    if not ws_hits:
                        ws_hits |= by_suffix.get(ws_src_dir + "/" + ws_mod_prefix, _EMPTY)
            else:
                # bare crate name with no sub-path: resolve to lib.rs (crate root entry)
                ws_hits |= by_suffix.get(ws_src_dir + "/lib", _EMPTY)
                if not ws_hits:
                    ws_hits |= by_suffix.get(ws_src_dir + "/mod", _EMPTY)
            # PRECISION: only accept a non-empty hit; never fan out to all .rs in the crate.
            cands |= ws_hits
    if not cands and bare_single and mod:
        # bare SINGLE-segment name (`utils`, `config`) → resolve by basename (recall). A DOTTED /
        # multi-segment module (`_typeshed.wsgi`, `os.path`) that did NOT path-suffix-match above is
        # external — falling back to its last segment matched unrelated local files (real-repo audit:
        # `from _typeshed.wsgi` → tests/.../wsgi.py). Multi-segment local imports already suffix-match,
        # so this only drops false external→local edges (precision; no real-coupling recall lost).
        cands = set(alias.get(mod, set()))
    if not cands and e["src"].endswith(".go"):
        # GO PACKAGE recall: a Go import names a PACKAGE = a DIRECTORY of .go files (the suffix
        # loop above matches FILES by no-ext path, so it never finds `internal/auth/auth.go` from
        # `…/internal/auth`). Resolve the import's path-suffix to a real .go-bearing directory and
        # link its files. Multi-segment-only + most-specific dir keeps the prior fix's precision:
        # a single-segment stdlib import (`fmt`) was already blanked above and matches no directory.
        # PERF: uses the O(1) go_suffix index built once in _go_pkg_by_suffix; byte-identical to
        # the linear _resolve_go_pkg scan (proven in tests/test_resolve_go_ruby.py gate 123).
        cands = _resolve_go_pkg_indexed(raw, go_suffix, go_dirs)
        exact_directory_fanout = bool(cands)
    if not cands and e["src"].endswith(".cs"):
        # C# NAMESPACE recall: a `using A.B.C;` names a NAMESPACE = a DIRECTORY of .cs files (like a
        # Go package), NOT a file — so the FILE-suffix probe above misses (a namespace path with a
        # dotted project-root dir like `MediaBrowser.Controller` never equals a `/`-joined file
        # suffix). Resolve the dotted namespace against the dotted-canonical form of a real
        # .cs-bearing directory (canonical exact match first; otherwise every
        # suffix candidate, with test dirs excluded) and link its .cs files. A
        # single-segment `using System;` is the BCL (blanked / matches no dir). This REPLACES the
        # basename fan-out (`using …Entities.TV` → BOTH `…/TV/Series.cs` and an unrelated
        # `…/Libraries/Series.cs`) with directory-scoped evidence. Multiple
        # matching project roots remain explicit ambiguity because project
        # references are not parsed here.
        cands, matching_directory_count = _resolve_csharp_ns_detail(
            raw,
            cs_dirs,
        )
        exact_directory_fanout = (
            bool(cands) and matching_directory_count == 1
        )
    if bare_single and e["src"].endswith(".rs") and cands:
        # RUST bare SINGLE-segment import (`use roff;` / `pub use roff;`) names an EXTERNAL crate — a
        # LOCAL module is ALWAYS reached via `crate::`/`self::`/`super::` or a `mod` declaration, NEVER a
        # bare `use barename;`. So such a bare name must NOT resolve (via the suffix probe OR the basename
        # fallback) to a SEPARATE Cargo COMPILE TARGET (tests/examples/benches). Measured on clap-rs/clap:
        # `pub use roff;` matched the ONLY repo `roff.rs` — `clap_mangen/tests/testsuite/roff.rs` — minting
        # a false production->test edge that survived dampening (sole, non-hub). Drop test/example/bench
        # candidates for a bare .rs import (the same separate-compile-target rule the trailing-type / C# /
        # Go fallbacks apply). Applied at the END of candidate generation so it covers BOTH the suffix
        # probe and the basename fallback. RECALL-SAFE: a real in-crate module is never a test target, and
        # a genuine same-basename SOURCE module (`use foo;` → src/foo.rs) is UNTOUCHED — only the decoy is
        # removed. Multi-segment Rust paths are unaffected (this is the bare-single arm only).
        cands = {c for c in cands if not _rs_inert_module_target(c)}
    if bare_single and e["src"].endswith(_WEB) and cands:
        # JS/TS bare SINGLE-segment import (`import Vue from 'vue'`) names an EXTERNAL package — a LOCAL
        # module is reached via a relative `./`/`../` or an aliased `~/`/`@/` path (those carry a `/`, so
        # they are NOT bare_single), NEVER a bare name. So such a bare name must NOT resolve to a Jest/
        # Vitest TEST-DOUBLE file (`__mocks__/<pkg>/index.js`) — that mock SHADOWS the external package at
        # test time and is never a production import target. Measured on gitlabhq: `vue`/`lodash-es`/
        # `jquery` matched `spec/frontend/__mocks__/<pkg>/index.js` decoys (1,388 false src→test edges; 37
        # survived hub-dampening). Drop test-double candidates for a bare web import — the same separate-
        # test-target rule the Rust (`_RS_TARGET_SEG`) / C# (`_CS_TEST_SEG`) / Go (`_test.go`) resolvers
        # apply. Applied at the END so it covers BOTH the suffix probe and the basename fallback. RECALL-
        # SAFE: a real local module under `__mocks__/` is never imported by a BARE name (it would be a
        # relative/aliased path = not bare_single), so this can only drop the external-package decoy; a
        # genuine bare same-basename SOURCE module is untouched. Multi-segment/relative imports unaffected.
        cands = {c for c in cands if not _js_mock_target(c)}
    return (
        cands,
        bare_single,
        exact_directory_fanout,
        known_local_reference,
    )


def _filter_bare_candidates(cands, e, bare_single):
    """PHASE 3 (scoring / tie-break — bare-path candidates only). Apply, in order, the
    same-language family filter and the two ambiguity guards (Ruby bare-require uniqueness,
    cross-language fan-out degree cap). Relative imports skip this phase entirely, exactly as
    in the original (these guards lived inside the bare branch). Returns retained candidates,
    whether the result collapsed to raw ambiguity evidence, and the candidate paths hidden by
    that collapse."""
    # SAME-LANGUAGE only: a bare basename (`utils`, `config`) collides across languages —
    # a frontend `@/utils` must NOT resolve to a backend `utils.py`. Cross-language coupling
    # was the #1 false-edge source on the real polyglot dogfood (161 bad edges). web
    # (ts/js/html) is one family.
    cands = {c for c in cands if _family(c) == _family(e["src"])}
    # PYTHON TEST-FIXTURE SHADOW guard (the Go `_test.go` / C# `_CS_TEST_SEG` / Rust separate-target
    # precedent, applied to Python's dotted-suffix probe). A test tree (`tests/units/…`) commonly MIRRORS
    # the package directory layout, so a dotted import like `reflex_base.utils` suffix-matches BOTH the real
    # package file AND a test-fixture `tests/units/reflex_base/utils/__init__.py` — the recall-biased probe
    # then emits a FALSE production→test edge (real-repo audit on reflex-dev/reflex: 203 such edges, 6
    # surviving hub-dampening to a customer warn). When a Python import co-resolves to BOTH a test-path and a
    # NON-test file, the non-test file IS the module the import names; the test-path candidate is a
    # fixture-mirror false edge. Drop the test-path candidate(s) ONLY IN THAT CASE. RECALL-SAFE — this is
    # exactly tests/audit_repo.py's `src_to_test_real` rule (a sole-resolution test target is KEPT): a real
    # `from tests.foo import …` (the import literally names a test path → no non-test sibling co-resolves) and
    # a genuine module under an `examples/` package (a unique suffix → no non-test sibling) both survive
    # untouched. Only fires when a non-test sibling proves the test candidate is the shadow, never alone.
    if e["src"].endswith(".py") and len(cands) > 1:
        nontest = {c for c in cands if not _py_test_path(c)}
        if nontest and len(nontest) < len(cands):
            cands = nontest                          # drop test-fixture shadow(s); real module sibling co-resolved
    # RUBY PRECISION (bare single-segment only): a bare `require 'name'` resolves via the
    # suffix index to EVERY local `name.rb` — measured 5,188 spurious edges on Rails
    # (28 bare-require names × multi-target fan-out). Multi-segment requires
    # (`require 'active_support/log_subscriber'`) resolve via the suffix index by FULL path
    # suffix and are UNAFFECTED (they have a '/' in `mod` so bare_single is False).
    # For bare single-segment Ruby requires, only accept a UNIQUE local match (exactly one
    # .rb file). If multiple `.rb` files share the same basename the import is ambiguous —
    # Ruby's actual load order is determined by the runtime $LOAD_PATH, not the static
    # directory layout — retain one inert raw ambiguity edge (zero spurious fan-out).
    # RECALL COST: near-zero. A repo with exactly one `helper.rb` still resolves to it.
    # A bare name with TWO or more same-basename `.rb` files is genuinely ambiguous and
    # marked ambiguous. This guard fires AFTER the family filter so it sees only same-family cands.
    ambiguous = False
    collapsed_candidate_paths = set()
    if bare_single and e["src"].endswith(_RUBY_BARE_UNIQUE_ONLY_EXT) and len(cands) > 1:
        collapsed_candidate_paths = set(cands)
        cands = set()   # ambiguous bare require → retain inert raw evidence
        ambiguous = True
    # FAN-OUT DEGREE CAP (cross-file DoS bound). A bare single-segment import that resolved to
    # MORE than _MAX_BARE_FANOUT distinct same-basename files is LOW-PRECISION wallpaper, AND the
    # source of an UNCAPPED N² edge blow-up (N same-named files × N importers — the per-file edge
    # cap runs before resolution so it can't bound this). Collapse the over-cap ambiguous fan-out:
    # recall-safe (an import resolving to 9+ candidates carries ~no precision — noise, not signal),
    # content-free, and the bound is now N×cap instead of N². Multi-segment / dotted / relative
    # imports are NOT bare_single, so they keep their exact (already-precise) resolution. The
    # normal case (1-8 candidates) retains all candidates with an ambiguity marker. Mirrors the resolver's existing per-language
    # ambiguity guards (Ruby above), generalized to bound the degree across ALL languages.
    if bare_single and len(cands) > _MAX_BARE_FANOUT:
        collapsed_candidate_paths = set(cands)
        cands = set()   # over-cap ambiguity → retain one inert raw edge, never N-way fan-out
        ambiguous = True
    return cands, ambiguous, collapsed_candidate_paths


def _emit_resolution(
    e,
    cands,
    *,
    forced_ambiguous=False,
    exact_multi_target=False,
    unresolved_if_empty=False,
):
    """PHASE 4 (resolution-write). Drop a self-edge, then either rewrite the import edge to one
    edge per resolved FILE (sorted, so output is deterministic) or — when nothing resolved — keep
    the original edge (stdlib / 3rd-party / unresolved: it matches no file, harmless). Known local
    ambiguity carries a closed status on either representation. A caller may
    also mark a reference whose syntax proves it is repository-local
    (``./``, ``../``, ``@/`` or ``~/``) as explicitly unresolved when candidate
    generation found no repository target. Returns the list of edges to append."""
    cands = set(cands)
    had_repository_candidate = bool(cands)
    cands.discard(e["src"])
    # Extraction may attach a resolver-only probe marker to synthesized
    # named-import coordinates. It is control evidence for this phase only and
    # must never enter graph assembly, persistence, observability payloads, or
    # canonical hashes.
    emitted_edge = {
        key: value
        for key, value in e.items()
        if key != "resolution_probe"
    }
    ambiguous = forced_ambiguous or (
        len(cands) > 1 and not exact_multi_target
    )
    reference_evidence = (
        {
            "reference_status": "ambiguous",
            # Keep the pre-contract annotations for backwards-compatible
            # producer observability while reference_status is the durable
            # persistence/query contract.
            "ambiguous_reference": True,
            "ambiguity_key": e.get("dst"),
        }
        if ambiguous
        else (
            {"reference_status": "unresolved"}
            if unresolved_if_empty and not had_repository_candidate
            else {}
        )
    )
    if cands:
        # Preserve extractor/substrate provenance attached by the assembly pipeline.
        # Resolution changes only the destination; rebuilding a minimal dict here used
        # to erase observability metadata specifically for successfully-resolved imports.
        return [{**emitted_edge, **reference_evidence, "dst": tgt, "kind": "imports"}  # dst is now a FILE PATH
                for tgt in sorted(cands)]
    # Stdlib/3rd-party/unresolved references stay raw and inert. A known local
    # ambiguity (Ruby load-path collision or over-cap basename fan-out) is
    # distinguishable from an ordinary external reference and survives as one
    # bounded evidence edge.
    return [{**emitted_edge, **reference_evidence}]


def _resolve_imports(nodes, edges, ambiguous_paths_out=None):
    """Rewrite `imports` edges from module-name → repo FILE path where possible (thin orchestrator).

    Local imports resolve; stdlib/3rd-party match nothing and are kept as-is. The work is split
    into four cohesive phases, each a private helper so this driver reads as the pipeline it is:
      1. _build_resolution_index  — build every lookup structure ONCE (the O(1) suffix index etc.)
      2. _candidates_*            — generate candidate files for one edge (relative / scoped / bare)
      3. _filter_bare_candidates  — same-language + ambiguity guards (bare path only)
      4. _emit_resolution         — write the resolved file→file edge(s), or keep the edge inert
    Exact resolutions retain the prior structural behavior. Known local
    ambiguity is now retained explicitly: bounded fan-out candidates carry
    ``reference_status="ambiguous"`` and Ruby/over-cap collisions keep one
    inert raw ambiguity edge instead of disappearing. A syntactically local
    relative/alias reference with zero repository candidates carries
    ``reference_status="unresolved"``. Ordinary bare, dotted and scoped
    external packages remain statusless.

    ``ambiguous_paths_out`` is an optional mutable set used only as a transient
    extraction side channel.  For a collapsed Ruby/over-cap ambiguity it
    receives the importer plus every real local candidate path, because the
    persisted raw edge intentionally does not enumerate those destinations.
    Enumerated ambiguity needs no side channel: its surviving deduplicated
    edges provide the endpoints, preserving exact-over-ambiguous precedence.
    The side channel is never attached to an edge or persisted as resolver
    metadata.
    """
    ctx = _build_resolution_index(nodes, edges)
    out = []

    # Resolve syntax-proven and catalog-proven local candidates once so a
    # named-import extractor's multi-edge representation can be judged as one
    # reference trie. Python
    # ``from .module import member`` and web
    # ``import { member } from "./module"`` both emit ``./module`` (the
    # importable module) plus ``./module/member`` (which may be either a
    # submodule or merely a symbol). Aliased local forms (``@/`` and ``~/``),
    # inferred own-package names, and declared scoped workspaces have the same
    # contract.
    #
    # A zero-candidate synthesized member probe may be satisfied by a resolved
    # ancestor at a path-segment boundary. A normal module edge never is:
    # separate imports ``acme`` and ``acme/missing`` remain separate facts.
    # Python alone also allows a resolved descendant to satisfy an emitted
    # namespace prefix. A sibling that merely shares a prefix is always
    # independent: resolving ``./foo/bar`` must not hide ``./foo/missing``.
    # Remaining zero member-probe chains collapse under their shortest normal
    # module root. The transient probe marker is stripped by _emit_resolution.
    local_resolution = {}
    local_by_source = {}
    unresolved_local_indices = set()
    suppressed_member_probe_indices = set()
    for index, edge in enumerate(edges):
        raw = (edge.get("dst") or "").strip()
        if edge.get("kind") != "imports" or not raw:
            continue
        if raw.startswith(("./", "../")):
            candidates = _candidates_relative(edge, raw, ctx)
            forced_ambiguous = False
            collapsed_candidate_paths = set()
            exact_multi_target = False
            # Python relative imports may traverse an implicit namespace
            # package: a concrete ``./foo/bar.py`` can satisfy the module
            # prefix ``./foo`` emitted by ImportFrom. Web/CSS/etc. do not have
            # that guarantee; a separate ``./foo`` import still needs its own
            # file or index even when ``./foo/bar`` exists.
            bidirectional_family_evidence = edge["src"].endswith(".py")
        else:
            scoped_local, candidates = _candidates_scoped_workspace(
                edge,
                raw,
                ctx,
            )
            if scoped_local:
                forced_ambiguous = False
                collapsed_candidate_paths = set()
                exact_multi_target = False
                bidirectional_family_evidence = False
            else:
                syntax_local_alias = raw.startswith(("@/", "~/"))
                normalized = (
                    _mod_noext(raw.lstrip("@~/").strip("<>\"'"))
                    .replace("::", "/")
                    .replace("\\", "/")
                    .replace(".", "/")
                    .strip("/")
                )
                own_name = normalized.split("/", 1)[0].lower()
                if (
                    not syntax_local_alias
                    and own_name not in ctx.own_pkgs
                ):
                    continue
                (
                    candidates,
                    bare_single,
                    exact_multi_target,
                    known_local_reference,
                ) = _candidates_bare(
                    edge, raw, ctx
                )
                if (
                    not syntax_local_alias
                    and not known_local_reference
                ):
                    continue
                (
                    candidates,
                    forced_ambiguous,
                    collapsed_candidate_paths,
                ) = _filter_bare_candidates(
                    candidates,
                    edge,
                    bare_single,
                )
                bidirectional_family_evidence = False
        local_resolution[index] = (
            candidates,
            forced_ambiguous,
            collapsed_candidate_paths,
            exact_multi_target,
        )
        comparison_raw = raw
        if (
            edge["src"].endswith(".py")
            and not raw.startswith(("./", "../"))
        ):
            # Python absolute modules use dotted coordinates
            # (``acme.member``), while every other local family uses `/`.
            # Normalize only the transient trie key; never rewrite the emitted
            # raw evidence, and never reinterpret dots inside relative
            # filenames.
            comparison_raw = raw.replace(".", "/")
        local_by_source.setdefault(
            (
                edge["src"],
                bidirectional_family_evidence,
            ),
            [],
        ).append(
            (
                index,
                raw,
                comparison_raw,
                bool(candidates) or forced_ambiguous,
                edge.get("resolution_probe") == "member",
            )
        )

    for (
        _source,
        bidirectional_family_evidence,
    ), records in local_by_source.items():
        candidate_raws = {
            comparison_raw
            for (
                _index,
                _raw,
                comparison_raw,
                has_candidate,
                _member_probe,
            ) in records
            if has_candidate
        }
        remaining_zero = []
        for (
                index,
                raw,
                comparison_raw,
                has_candidate,
                member_probe,
        ) in records:
            if has_candidate:
                continue
            resolved_ancestor = (
                member_probe
                and any(
                    comparison_raw.startswith(
                        candidate_raw.rstrip("/") + "/"
                    )
                    for candidate_raw in candidate_raws
                )
            )
            if resolved_ancestor:
                # The synthesized probe has completed its only job: it found no
                # submodule, while the real base module exists. Dropping it is
                # essential when a separate exact import has the same raw
                # coordinate; a statusless probe must never deduplicate away
                # that exact edge's unresolved evidence.
                suppressed_member_probe_indices.add(index)
                continue
            if (
                comparison_raw in candidate_raws
                or (
                    bidirectional_family_evidence
                    and any(
                        candidate_raw.startswith(
                            comparison_raw.rstrip("/") + "/"
                        )
                        for candidate_raw in candidate_raws
                    )
                )
            ):
                continue
            remaining_zero.append(
                (index, raw, comparison_raw, member_probe)
            )
        zero_raws = {
            comparison_raw
            for _index, _raw, comparison_raw, _probe in remaining_zero
        }
        for (
                index,
                _raw,
                comparison_raw,
                member_probe,
        ) in remaining_zero:
            collapsed_under_zero_root = (
                member_probe
                and any(
                    comparison_raw != possible_parent
                    and comparison_raw.startswith(
                        possible_parent.rstrip("/") + "/"
                    )
                    for possible_parent in zero_raws
                )
            )
            if collapsed_under_zero_root:
                suppressed_member_probe_indices.add(index)
            else:
                unresolved_local_indices.add(index)

    def record_ambiguous_paths(src, candidates):
        if ambiguous_paths_out is None:
            return
        ambiguous_paths_out.add(src)
        ambiguous_paths_out.update(
            candidate
            for candidate in candidates
            if candidate and candidate != src
        )

    for index, e in enumerate(edges):
        if e.get("kind") != "imports":
            out.append(e)
            continue
        raw = (e.get("dst") or "").strip()
        if not raw:
            out.append(e)
            continue
        if index in suppressed_member_probe_indices:
            continue
        if raw.startswith(("./", "../")):
            # RELATIVE import: resolve exactly against the importer's dir (no basename fallout, no
            # bare-path ambiguity guards — same as before, the filters lived in the bare branch).
            cands, _forced, _collapsed, _exact_multi = local_resolution[index]
            emitted = _emit_resolution(
                e,
                cands,
                unresolved_if_empty=index in unresolved_local_indices,
            )
            out.extend(emitted)
            continue
        else:
            if index in local_resolution:
                (
                    cands,
                    forced_ambiguous,
                    collapsed_candidate_paths,
                    exact_directory_fanout,
                ) = local_resolution[index]
            else:
                # A declared scoped workspace coordinate would have been
                # recorded in local_resolution above. This call therefore
                # short-circuits only if a future caller bypasses that
                # preprocessing; ordinary external @scope/pkg returns None.
                scoped_edges = _resolve_scoped_workspace_edge(e, raw, ctx)
                if scoped_edges is not None:
                    out.extend(scoped_edges)
                    continue
                (
                    cands,
                    bare_single,
                    exact_directory_fanout,
                    known_local_reference,
                ) = _candidates_bare(e, raw, ctx)
                (
                    cands,
                    forced_ambiguous,
                    collapsed_candidate_paths,
                ) = _filter_bare_candidates(
                    cands,
                    e,
                    bare_single,
                )
                if (
                    cands
                    and raw.startswith("@")
                    and not raw.startswith("@/")
                ):
                    # An undeclared scoped name may be an external package or
                    # an unparsed repository alias. A coincident local suffix
                    # is not enough to choose between them, so retain the
                    # candidate only as inert ambiguity evidence. With zero
                    # candidates it remains an ordinary statusless external
                    # reference.
                    forced_ambiguous = True
            emitted = _emit_resolution(
                e,
                cands,
                forced_ambiguous=forced_ambiguous,
                exact_multi_target=exact_directory_fanout,
                unresolved_if_empty=(
                    index in unresolved_local_indices
                    or (
                        index not in local_resolution
                        and known_local_reference
                    )
                ),
            )
            if forced_ambiguous:
                record_ambiguous_paths(
                    e["src"], collapsed_candidate_paths
                )
            out.extend(emitted)
            continue
    return out
