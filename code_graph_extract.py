#!/usr/bin/env python3
"""Static code-structure extractor (zero inference, cheap to run) — MULTI-LANGUAGE.

The proven direction (NOTE-PROOF-STATIC-MEANING-AWARE-STRUCTURE): Veripsa structures the
AI's *output* by recovering structure statically — no LLM in the hot path, so it is cheap to
run at any scale. Python is parsed with the stdlib `ast`; other languages are parsed with
tree-sitter (the multi-language path, so a buyer is NOT Python-only). Both feed ONE uniform
code graph —

  nodes:  file  | def (function / method) | class      (each carries its `language`)
  edges:  contains (file -> def)        a file defines a symbol
          calls    (file -> name)       a call site, by callee name
          imports  (file -> module)     an import / require

It does NOT resolve cross-file call targets semantically (that is roadmap); it records call
*names* and import *modules* so a cheap, deterministic join can answer "which files reference
a symbol / module" — enough to compute a structural blast radius for conflict scope. Python
needs no third-party deps; the other languages need the tree-sitter grammars (gracefully
skipped, with a count, if they are not installed). No network, no inference.

Usage:
  python3 code_graph_extract.py <root>            # print a JSON code graph for <root>
  python3 code_graph_extract.py <root> --summary  # print counts + language coverage + a blast-radius
  python3 code_graph_extract.py <root> --push      # record the graph through the gate (VERIPSA_DSN)
"""
import ast
import hashlib
import json
import os
import sys
import tokenize
import unicodedata

# ---------------------------------------------------------------------------
# Re-export the sibling modules' public surface so callers that do
#   import code_graph_extract as X
# can use X._ts_languages, X._git_repo, X.build_graph unchanged.
# ---------------------------------------------------------------------------
from _cg_languages import (  # noqa: F401  (re-exported)
    _GRAMMAR_BY_EXT, _LABEL_BY_EXT, _GENERIC_SPEC,
    _ts_languages,
    extract_file_ts, extract_file_generic, extract_file_html,
    extract_file_svelte, extract_file_vue, extract_file_astro, extract_file_css,
    extract_file_elixir,
)
from _cg_schema import _schema_graph
from _cg_config import _config_graph, _config_ext, _CONFIG_EXTS, _CONFIG_SKIP_FILES
from _cg_resolve import _resolve_imports
from _cg_routes import _routes_graph   # cross-tier route↔call contract coupling (PR #270 → integration)
from _cg_iac import _iac_graph         # IaC cross-substrate coupling: Terraform + Kubernetes (PR #xxx)
from _cg_api_contract import _api_contract_graph  # API-contract cross-substrate: GraphQL types + protobuf services/messages
from _cg_openapi import _openapi_graph  # OpenAPI/Swagger REST cross-substrate: spec operations + schemas referenced from code
from _cg_ci import _ci_script_graph  # CI script contracts: package.json scripts + GitHub Actions run commands
from _cg_tauri import _tauri_command_graph  # Tauri command contracts: Rust command definitions + JS invoke calls
from _cg_jobs import _job_queue_graph  # Async job/queue contracts: Celery send_task + BullMQ Queue/Worker
from cg_schema_contract import (
    EXTRACTOR_VERSION,
    RESOURCE_DEFINITION_EVIDENCE_KINDS,
    SCHEMA_CONTRACT_VERSION,
    RESOURCE_KIND_TO_SUBSTRATE,
    RESOURCE_NODE_KINDS,
    assert_valid_graph,
    classify_edge_substrate,
    collect_graph_metrics,
    enrich_resource_nodes,
)
# Generated/vendored PREDICATE group (GAP-15: anti-cry-wolf) lives in cg_generated; re-exported here so
# callers using `import code_graph_extract as X` see X._is_generated / X._GENERATED_FILE_SUFFIXES /
# X._GENERATED_DIR_NAMES / X._matches_gitattr / X._gitattr_pat_to_regex unchanged. The WALK that feeds
# them (_gitattributes_generated_matchers) stays in this module — it needs the extractor's _SKIP_DIRS.
from cg_generated import (  # noqa: F401  (re-exported)
    _GENERATED_DIR_NAMES, _GENERATED_FILE_SUFFIXES,
    _gitattr_pat_to_regex, _matches_gitattr, _is_generated,
)

# Directories skipped during walk — vendor/generated/build/cache trees that are not worth
# parsing (minified JS, compiled objects, package mirrors, IDE metadata).  Keep the set tight:
# only add dirs that are unambiguously NOT first-party source.
_SKIP_DIRS = {
    # version control / python / node — already present
    ".git", "__pycache__", ".venv", "node_modules", ".playwright-mcp", "dist", "build",
    # JavaScript / frontend tooling
    "vendor",           # Go dep mirror, Ruby bundler, PHP composer, generic
    "third_party",      # C/C++ / Bazel convention
    "third-party",      # hyphenated variant
    ".next",            # Next.js generated output
    ".nuxt",            # Nuxt.js generated output
    "bower_components", # legacy Bower
    "jspm_packages",    # JSPM
    "coverage",         # Jest / nyc / pytest-cov HTML reports
    # compiled / generated outputs
    "target",           # Rust cargo + Maven
    "bin",              # compiled binaries (Go / C / C++)
    "obj",              # C/C++ object files (MSBuild)
    # infrastructure / IaC generated state
    ".terraform",       # Terraform provider cache + state
    # mobile / native
    "Pods",             # CocoaPods (iOS/macOS)
    ".gradle",          # Gradle build cache (Android / JVM)
    # IDE / tool metadata
    ".idea",            # JetBrains IDE files
    ".mypy_cache",      # mypy type-check cache
    ".pytest_cache",    # pytest cache
    ".tox",             # tox virtualenvs
    # legacy VCS
    ".svn",             # Subversion
    ".hg",              # Mercurial
    # Unity build / cache trees — engine-regenerated, NEVER hand-edited, often huge.  A typical Unity
    # checkout dwarfs Assets/ with these; without skipping them the walk thrashes and emits noise file
    # nodes for engine-generated YAML / .meta entries.  `Assets/` (real source) and `Packages/` (manifest
    # + first-party package source) stay walked — only the engine's known cache/output dirs are pruned.
    # Capital-case is the Unity convention (the engine creates them that way and the standard Unity
    # `.gitignore` matches by exact capitalized name).  We deliberately omit ambiguous names (e.g.
    # `Build`/`Builds`/`Logs`) that a non-Unity repo might legitimately use as a source / app-output
    # folder — case-sensitivity on Linux gives some protection but not on macOS, and a recall-safe miss
    # (one extra walked dir) is far better than a wrongful prune of real source.
    "Library",          # Unity asset import cache + every engine-derived artifact
    "Temp",             # transient Unity build state
    "MemoryCaptures",   # Memory Profiler dumps
}

# SOURCE extensions we EMIT A NODE FOR even when there is no parser (GAP-14: honesty/completeness).
# The grammar-backed extensions (_GRAMMAR_BY_EXT) plus .py are PARSED; the rest in this set are
# hand-authored source in a language we have no grammar for (.scala/.dart/.ex/.lua/...). Dropping them
# entirely made an unsupported-language file MISSING from the directory/board surfaces — so a
# storey-1 (direct) collision on a file two agents both touch could not even fire. We instead emit a
# BARE `file` node (kind=file, language="unknown") with NO contains/calls/imports edges: direct
# collision + unknown-marking become complete, and we never invent false structural edges from a file
# we cannot parse. Scope = PROGRAMMING/source languages only (not arbitrary docs/data like .md/.txt/
# .po/.json): a node must mean "a file someone hand-edits as code", or direct-collision degrades to noise.
_SOURCE_EXT = frozenset(set(_GRAMMAR_BY_EXT) | {
    ".py", ".pyi", ".pyx", ".pxd",                       # python family (.py parsed; the rest bare)
    # JVM / .NET / mobile not (yet) grammar-backed
    ".scala", ".sc", ".groovy", ".gradle",               # Scala / Groovy / Gradle DSL
    ".clj", ".cljs", ".cljc", ".edn",                    # Clojure
    ".kt", ".kts",                                       # Kotlin (grammar may be absent → still a node)
    ".m", ".mm",                                         # Objective-C / Objective-C++
    ".fs", ".fsx", ".vb",                                # F# / Visual Basic
    # functional / scripting / scientific
    ".ex", ".exs", ".erl", ".hrl",                       # Elixir / Erlang
    ".hs", ".lhs", ".elm", ".purs",                      # Haskell / Elm / PureScript
    ".ml", ".mli", ".re", ".rei",                        # OCaml / ReasonML
    ".lua", ".tcl", ".pl", ".pm", ".raku",               # Lua / Tcl / Perl / Raku
    ".r", ".jl", ".nim", ".cr", ".d", ".zig", ".v",      # R / Julia / Nim / Crystal / D / Zig / V
    ".dart",                                             # Dart
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1",    # shells / PowerShell
    ".gate",                                             # Veripsa release-gate DSL: shell-sourced,
                                                         # hand-authored coordination surface; bare node only
    # web/source-adjacent that is authored (templates / styles / components)
    ".vue", ".svelte", ".astro",                         # single-file components
    ".scss", ".sass", ".less", ".styl", ".css",          # stylesheets
    ".sql",                                              # SQL is parsed by the schema graph, but a
                                                         # plain .sql also deserves its own file node
    ".prisma",                                           # Prisma ORM schema (model declarations →
                                                         # table nodes via _cg_schema._ORM_PATTERNS)
    ".tf",                                              # Terraform HCL (IaC: resource/data block
                                                         # cross-file coupling via _cg_iac._iac_graph)
    ".graphql", ".gql",                                 # GraphQL schema/operation (API-contract: type
                                                         # definitions → references via _cg_api_contract)
    ".proto",                                           # protobuf/gRPC (API-contract: message/service
                                                         # definitions → references via _cg_api_contract)
    # Unity (C#-driven game engine). The C# behind a Unity project parses through the regular .cs path
    # (.cs is already in _GRAMMAR_BY_EXT → tree-sitter-c-sharp). The HAND-AUTHORED non-.cs files in a
    # Unity repo are the YAML asset family below — they ARE diffed by humans (every scene rearrangement,
    # prefab edit, ScriptableObject value, import-setting tweak) and they ARE the typical surface of an
    # AI-agent draft PR on a Unity codebase. WITHOUT them in _SOURCE_EXT a Unity PR's paths are absent
    # from the graph → main_impact_surface flags the change "unknown" → the verdict on
    # example-org/game-app was "Veripsa — Unknown" (the dogfood gap that motivated this lane).
    # Emitting a BARE file node (no structural edges from us — we don't parse the YAML; conservative bias:
    # false couplings on prefab GUIDs would be worse than the missing edge) graduates the verdict from
    # 'unknown' → 'no-impact' / direct collision, exactly the GAP-14 pattern documented above for other
    # unsupported-language source.  Engine-generated copies of these files live under Library/Temp/ which
    # _SKIP_DIRS now prunes (see above), so what reaches this allowlist is the human-authored asset.
    ".meta",        # per-asset import settings + GUID (one alongside every Assets/ file; hand-tracked
                    #    in VCS — the GUID is the stable identity Unity uses to bind C# refs to assets)
    ".unity",       # Scene serialization (YAML)
    ".prefab",      # Prefab serialization (YAML)
    ".asset",       # ScriptableObject / generic engine asset (YAML, occasionally binary — the binary
                    #    case is caught by _is_binary upstream, recall-safe)
    ".asmdef",      # Assembly Definition (JSON: declares a C# assembly + its references)
    ".asmref",      # Assembly Definition Reference (JSON: per-folder override)
    ".shader",      # ShaderLab source (hand-written shader programs)
    ".cginc",       # CG include (hand-written shader includes)
    ".hlsl",        # HLSL shader source
    ".compute",     # compute shader source
})

# These formats intentionally have no main language-parser dispatch.  Either a
# dedicated bounded contract pass below owns their semantics, or their declared
# graph contract is deliberately file-only.  In both cases the bare file node
# from stage 1 is complete evidence for the supported contract, not a parser
# fallback that should poison effective adjacency.
_COMPLETE_BARE_EXTS = frozenset({
    ".sql", ".prisma",                     # schema/table/column pass
    ".tf",                                 # Terraform IaC pass
    ".graphql", ".gql", ".proto",          # API-contract pass
    ".gate",                               # release-gate DSL: file-level coordination only
})

# Unity-asset extensions — kept as their own set so the bare-node path can stamp the file node with a
# specific `language="unity"` (better than the catch-all `"unknown"`).  Pure label refinement: NO
# structural edges are emitted from these files (we don't parse the YAML; conservative bias keeps prefab
# GUID resolution out of the moat).  The set is the asset-side of the Unity additions to _SOURCE_EXT
# above (the .cs source files go through the regular tree-sitter-c-sharp path and are NOT in this set).
_UNITY_ASSET_EXTS = frozenset({
    ".meta", ".unity", ".prefab", ".asset",
    ".asmdef", ".asmref",
    ".shader", ".cginc", ".hlsl", ".compute",
})

# GENERATED / VENDORED exclusion (GAP-15: precision / anti-cry-wolf). Generated or vendored code that
# lives OUTSIDE _SKIP_DIRS (protobuf `*_pb2.py`, `*.pb.go`, gRPC stubs, `*.gen.*`, `__generated__/`,
# …) is still hand-edited by NOBODY — but parsing it produces synthetic symbols whose `calls`/`contains`
# edges create false adjacency, i.e. we cry wolf on files no one touches. We EXCLUDE such files from the
# graph entirely (no nodes, no edges) via two complementary signals:
#   (1) `.gitattributes` `linguist-generated` / `linguist-vendored` markers (the repo's OWN declaration);
#   (2) common generated filename SUFFIXES + directory names (when the repo did not declare them).
# The PREDICATE group that implements (1)+(2) — _GENERATED_DIR_NAMES, _GENERATED_FILE_SUFFIXES,
# _gitattr_pat_to_regex, _matches_gitattr, _is_generated — lives in cg_generated and is re-exported above
# (it is pure: a path in → yes/no out, no walk). The WALK that produces the .gitattributes matchers
# (_gitattributes_generated_matchers, below) stays here beside the extractor's other walks (it needs _SKIP_DIRS).

# Per-file size cap: files larger than this are skipped.  Minified JS, generated protobuf stubs,
# vendored bundles, and data files routinely exceed this; real source files almost never do.
# 1.5 MB is the threshold — above it the parse cost is disproportionate and the signal-to-noise
# ratio collapses (a minified bundle has thousands of synthetic symbol names).
_FILE_SIZE_CAP = 1_500_000  # bytes (1.5 MB)
# SCHEMA exception: a whole DB's DDL is routinely dumped into ONE file (Rails/GitLab `db/structure.sql`,
# Django `schema.sql`) that legitimately exceeds 1.5 MB — GitLab's is ~2.9 MB / ~1.5k tables. At the
# general cap such a file is silently skipped → its tables never enter the graph → a RECALL gap on exactly
# the large enterprise repos we most want to cover. A `.sql` schema parse is LINEAR and its output is
# already bounded by `_MAX_TABLES` (truncation, not blow-up), and a binary renamed `.sql` is still caught
# by `_is_binary` below — so a higher cap for `.sql` is recall-positive and bounded. Pathological huge
# `.sql` (data/seed dumps) above this are still skipped.
_SCHEMA_FILE_SIZE_CAP = 12_000_000  # bytes (12 MB) — .sql only (see _passes_file_guards)

# Size cap for individual .gitattributes files in _gitattributes_generated_matchers. A real
# .gitattributes is a few hundred lines; a crafted / degenerate one can be tens of MB, and the
# function reads the whole file + builds a matcher list that is O(lines) in memory. This cap
# bounds both the read and the resulting matcher list without any correctness loss for real repos.
_GITATTRIBUTES_SIZE_CAP = 1_000_000  # bytes (1 MB)

# Per-line byte cap. The file-size cap alone still lets a 1.4 MB SINGLE LINE through every parser and
# regex substrate. That shape is minified/data/ReDoS-bait, not hand-authored structure; it can spend
# tens of seconds in tree-sitter / schema regex passes while yielding almost no useful coupling signal.
# Skip such files before reading them in full. Large legitimate schemas remain covered when they are
# line-broken (the normal dump shape); one pathological line degrades to "not structurally analyzed".
_MAX_TEXT_LINE_BYTES = 250_000

# Per-file physical line cap for parser-backed source extraction. A sub-size-cap file with hundreds of
# thousands of tiny lines is a parser CPU attack (or generated output), even when no single line is long.
# Keep the file as a file-level node and let substrate passes that have their own bounded scanners still
# inspect it where appropriate (for example Rails migration table names).
_MAX_PARSE_LINES = 100_000
_PARSE_LINE_CHECK_MIN_BYTES = 1_000_000

# Per-file SYMBOL cap (audit:dos — algorithmic / pathological-input DoS). The size cap above bounds BYTES,
# but NOT symbols-per-byte: a 1.5 MB file of ~150 000 one-line `def f():0` slips UNDER the size cap yet
# explodes build_graph's node list (measured: ONE such file → ~150k nodes, ~400 MB peak RSS, a ~19 MB JSON
# ingest payload — and the engine's per-pair symbol joins scale with it). A real source file has at most a
# few thousand symbols; tens of thousands is pathological (machine-emitted / adversarial). When a single file
# yields MORE than this many nodes (or edges), we DROP its structural detail and keep ONLY a bare `file` node
# — the file still appears for direct (storey-1) collision + is honestly marked at FILE level, but it can no
# longer balloon memory/time/payload or seed the engine's O(symbols) joins. Degrade to honest file-level,
# never hang/OOM. Bounds ALL languages uniformly (the cap is applied in build_graph after each file's parse).
_PER_FILE_SYMBOL_CAP = 20_000   # nodes per single file (real files: a few thousand; >this = pathological)
_PER_FILE_EDGE_CAP   = 40_000   # edges per single file (calls/imports/contains; ~2× the symbol cap)

# Binary-detection chunk size: read this many bytes from the start of a file to test for NUL bytes.
# A NUL byte in the first chunk is a reliable indicator of a non-text (binary) file.
_BINARY_PROBE = 8_192  # bytes

# Paths that DEFINITIONALLY carry no code coupling — docs, prose, images, and generated lock files. A
# change to one of these cannot structurally affect (or be affected by) any code symbol, so it must NOT
# enter coupling coordination. Including such a path used to drag an otherwise-fully-analyzed PR to the
# scary "❓ Not analyzed" verdict just for touching a README (core.main_impact_surface flags a change
# 'unknown' when ANY of its paths is absent from main's graph — and a doc is never in the graph).
#
# ASYMMETRY WITH THE EXTRACTOR'S OWN ALLOWLIST (_iter_source_files: ext == ".py" or in _GRAMMAR_BY_EXT):
# that lists the languages we can PARSE. This denylist lists what is definitely-NOT-code. A source file in
# an UNSUPPORTED language (a .rs when the Rust grammar is absent, a .scala) is in NEITHER set — by design it
# STAYS in coordination and is honestly reported 'unknown' ("can't predict", never a false 'clear' = honest
# recall). So this list is deliberately CONSERVATIVE: when a type is ambiguous it is treated as code (kept),
# never silently dropped. Config (.json/.yaml/.toml/.ini) is NOT here — config can be coupling-relevant.
_NONCODE_EXTS = frozenset({
    # docs / prose
    ".md", ".markdown", ".mdx", ".rst", ".adoc", ".asciidoc", ".txt", ".text", ".rtf",
    # images / fonts / media (binary assets)
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".tiff", ".pdf",
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp4", ".mov", ".webm", ".mp3", ".wav",
    # tabular data dumps (not config — config stays coordinated)
    ".csv", ".tsv",
})
# Bare filenames (case-insensitive) that are non-code regardless of extension: license/notice family,
# repo-tooling dotfiles, and machine-generated lock files (huge, never hand-coupled). go.mod is NOT here
# (module deps ARE coupling-relevant); only go.sum, the checksum lock, is.
_NONCODE_NAMES = frozenset({
    "license", "license.txt", "license.md", "licence", "notice", "copying", "copyright",
    "authors", "changelog", "changelog.md", "contributing", "contributing.md", "code_of_conduct.md",
    ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore", ".npmignore", ".prettierignore",
    "codeowners", ".ds_store",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock",
    "gemfile.lock", "composer.lock", "go.sum",
})


def is_noncode_path(path: str) -> bool:
    """True when `path` definitionally has no code coupling (docs, images, lock files) and so must be
    excluded from coupling coordination (claims / blast-radius / the 'unknown' verdict). Conservative:
    ambiguous or unsupported-language source returns False (treated as code, kept in coordination and
    honestly reported 'unknown' if un-indexed). See _NONCODE_EXTS / _NONCODE_NAMES."""
    base = os.path.basename(path).lower()
    if base in _NONCODE_NAMES:
        return True
    return os.path.splitext(base)[1] in _NONCODE_EXTS


def _head_sha(root):
    """The HEAD commit sha of the repo at `root`, content-free (a hex sha), or None when `root` is
    not a git checkout. Used to pin the GRAPH FRESHNESS as-of: the commit the graph was extracted at."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        sha = (out.stdout or "").strip()
        # bound + hex-shape it so it can never carry anything but a sha (the gate also re-validates).
        if sha and len(sha) <= 64 and all(c in "0123456789abcdefABCDEF" for c in sha):
            return sha
    except Exception:
        pass
    return None


def _git_repo(root):
    """The content-free repo IDENTITY at `root` = 'owner/name' parsed from the origin remote URL, or
    None when there is no git remote. This is the COORDINATE's repo half — WHICH repository this
    directory is a checkout of. Never the file contents; just the repo name (already public on the
    remote). Both ssh (git@host:owner/name.git) and https (https://host/owner/name(.git)) forms reduce
    to 'owner/name'."""
    import re, subprocess
    try:
        out = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5)
        url = (out.stdout or "").strip()
    except Exception:
        url = ""
    if not url:
        return None
    # strip a trailing .git, then take the last two path segments (owner/name) from either URL form.
    u = url[:-4] if url.endswith(".git") else url
    u = u.replace(":", "/")               # git@host:owner/name -> git@host/owner/name
    segs = [s for s in u.split("/") if s]
    repo = "/".join(segs[-2:]) if len(segs) >= 2 else (segs[-1] if segs else "")
    repo = repo.strip()
    return repo[:512] if repo else None


def _git_branch(root):
    """The ref the working tree is ACTUALLY on at `root` — NEVER assumed to be 'main'. Returns the
    branch name, or 'detached@<short-sha>' when HEAD is detached, or None when not a git checkout.
    This is the COORDINATE's branch half: a path on a feature branch is a DIFFERENT logical file from
    the same path on main, so the graph must record which ref it was extracted at (no false collisions
    across branches)."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        ref = (out.stdout or "").strip()
    except Exception:
        return None
    if not ref:
        return None
    if ref == "HEAD":   # detached HEAD — label it honestly by the short sha (still not 'main')
        sha = _head_sha(root)
        return f"detached@{sha[:12]}" if sha else "detached"
    return ref[:512]


# UTF-16 byte-order marks. A UTF-16 source file encodes even plain ASCII as `\x00X` (LE) or `X\x00`
# (BE), so the NUL-byte binary probe would mis-classify a perfectly good UTF-16 source file as binary
# and DROP it entirely (no node — worse than the latin-1/sjis silent-symbol-drop). A leading UTF-16 BOM
# is an unambiguous "this is UTF-16 TEXT" signal, so we exempt it from the NUL probe and decode it as
# UTF-16 in the readers. (UTF-8 BOM `\xef\xbb\xbf` has no NUL, so it never tripped the probe.)
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _is_binary(path):
    """Return True if the file looks binary (contains a NUL byte in the first chunk).
    Cheap: reads at most _BINARY_PROBE bytes.  Catches compiled files given source-like
    extensions (e.g. a .py that is actually a compiled artefact or a test fixture).

    EXEMPTION (i18n): a file that opens with a UTF-16 BOM is UTF-16 *text*, not binary — its
    `\\x00` bytes are how UTF-16 encodes ASCII, not a binary marker. Treating it as binary would
    drop a real (e.g. Windows-authored) UTF-16 source file with no node at all. So a UTF-16-BOM
    prefix is NOT binary; the readers decode it as UTF-16."""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(_BINARY_PROBE)
        if chunk[:2] in _UTF16_BOMS:    # UTF-16 BOM → text, not binary (its NULs are ASCII encoding)
            return False
        return b"\x00" in chunk
    except OSError:
        return True   # unreadable → treat as binary (skip it)


def _has_overlong_line(path, limit=_MAX_TEXT_LINE_BYTES):
    """True when any physical line exceeds `limit` bytes.

    Streaming byte check, so it bounds the exact no-newline/minified/ReDoS shape without decoding the
    file or allocating the whole body. Newlines reset the counter; CRLF is naturally handled because
    the LF byte resets the count. A read error fails closed (skip the file)."""
    try:
        current = 0
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    return current > limit
                parts = chunk.split(b"\n")
                if len(parts) == 1:
                    current += len(chunk)
                    if current > limit:
                        return True
                    continue
                current += len(parts[0])
                if current > limit:
                    return True
                for part in parts[1:-1]:
                    if len(part) > limit:
                        return True
                current = len(parts[-1])
                if current > limit:
                    return True
    except OSError:
        return True


def _has_too_many_lines(path, limit=_MAX_PARSE_LINES):
    """True when a source file has more than `limit` physical lines. Streaming byte count; read errors fail
    closed to the safer file-level path."""
    try:
        if os.path.getsize(path) < _PARSE_LINE_CHECK_MIN_BYTES:
            return False
        lines = 0
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    return False
                lines += chunk.count(b"\n")
                if lines > limit:
                    return True
    except OSError:
        return True


def _git_blob_sha(path):
    """The file's content hash as a GIT BLOB SHA-1 — `sha1(b"blob <len>\\0" + bytes)` — over its RAW bytes.

    WHY this exact scheme (not a plain sha256): it is byte-identical to the `sha` GitHub reports for that
    file's blob in the tree/Files API, so the App can carry the file's content hash AT THE PR'S BASE for FREE
    (GitHub already hands it the blob sha per changed file — no extra fetch, no re-hashing). The graph side
    (this extractor) and the claim side (the App) then speak the SAME hash, so an equality test is meaningful.

    CONTENT-FREE: this returns a HASH, never the bytes. A hash is a fixed-width fingerprint — it cannot be
    inverted to the file's contents — so storing/comparing it keeps the content-free contract. Returns None
    on any read error (a vanished/unreadable file) → the node carries NO hash = unknown = the recall-safe
    fallback (the engine keeps the file-level collision rather than trusting an unverifiable freshness)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    h = hashlib.sha1()
    h.update(b"blob " + str(len(data)).encode("ascii") + b"\0")
    h.update(data)
    return h.hexdigest()


def _read_source_text(path):
    """Read a source file's TEXT robustly, never raising on encoding (i18n correctness, worldwide repos).
    Order: (1) a UTF-16 BOM → decode UTF-16 (handles Windows/legacy UTF-16 source, which the stdlib
    `tokenize.open` does NOT detect); (2) otherwise `tokenize.open`, which honors a PEP-263 coding cookie
    (`# -*- coding: shift_jis -*-`, latin-1, gb18030, …) AND strips a UTF-8 BOM — the canonical "read a
    Python source file in its declared encoding" path; (3) last-resort `utf-8` with errors='replace' so a
    file in an undeclared/odd encoding still yields a (lossy) text the parser can scan rather than dropping
    EVERY symbol. Content-free: only the file's own text is read here; it is parsed for symbol NAMES + line
    spans (metadata) and never stored/logged/returned as a body. Returns the decoded `str`."""
    with open(path, "rb") as fh:
        head = fh.read(2)
    if head in _UTF16_BOMS:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-16", "replace")
    try:
        with tokenize.open(path) as fh:        # PEP-263 coding cookie + UTF-8-BOM aware
            return fh.read()
    except (SyntaxError, UnicodeDecodeError, ValueError, LookupError, OSError):
        # bad/undeclared encoding (or an unknown codec name in the cookie) → lossy UTF-8, never a drop
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")


_GIT_C_QUOTE_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "t": b"\t",
    "n": b"\n",
    "v": b"\v",
    "f": b"\f",
    "r": b"\r",
    "\\": b"\\",
    '"': b'"',
}


def _gitattributes_fields(line):
    """Split one bounded attributes line using Git's pattern-field grammar.

    Git does not shell-split an attributes line.  An unquoted pattern ends at
    the first whitespace; a pattern whose first byte is ``"`` is C-style
    quoted and may therefore contain whitespace.  C quoting is decoded here,
    while wildmatch escapes (for example the backslash before a space in
    ``"space\\\\ file.py"``) intentionally survive for
    :func:`_gitattr_pat_to_regex`.

    Malformed quoting is ignored fail-closed.  The caller has already bounded
    the containing file, so this single forward scan is linear and dependency
    free.
    """
    if not line.startswith('"'):
        return line.split()

    decoded = bytearray()
    i = 1
    while i < len(line):
        char = line[i]
        if char == '"':
            rest = line[i + 1:]
            if rest and not rest[0].isspace():
                return []
            try:
                pattern = bytes(decoded).decode("utf-8")
            except UnicodeDecodeError:
                return []
            if "\x00" in pattern:
                return []
            return [pattern, *rest.split()]
        if char != "\\":
            decoded.extend(char.encode("utf-8"))
            i += 1
            continue

        i += 1
        if i >= len(line):
            return []
        escaped = line[i]
        replacement = _GIT_C_QUOTE_ESCAPES.get(escaped)
        if replacement is not None:
            decoded.extend(replacement)
            i += 1
            continue
        if escaped not in "01234567":
            # Git's C-style reader rejects unknown escapes such as ``\ ``.
            # To express a wildmatch-escaped space the file contains ``\\ ``,
            # which decodes above to a single backslash followed by the space.
            return []
        end = i
        while (
            end < len(line)
            and end < i + 3
            and line[end] in "01234567"
        ):
            end += 1
        value = int(line[i:end], 8)
        if value > 0xFF:
            return []
        decoded.append(value)
        i = end
    return []


def _gitattributes_generated_inventory(root):
    """Parse every bounded `.gitattributes` and return ``(matchers, files)``.

    ``matchers`` is the ordered list of
    ``(base_dir, pattern, attribute, state)`` rules for
    `linguist-generated` and `linguist-vendored` (GAP-15). ``state`` is
    ``True`` (set), ``False`` (unset), or ``None`` (unspecified via ``!``).
    ``files`` contains the corresponding on-disk `.gitattributes` paths which
    were actually accepted and parsed.  build_graph persists those control
    files as ``config_file`` nodes: incremental target-SHA reconstruction must
    fetch unchanged root/nested rules, including negative carve-outs which
    deliberately re-include a normally generated-looking path.

    The repo's OWN declaration that a path is generated/vendored is the
    highest-signal exclusion source.

    gitattributes semantics we honor (the subset that matters here):
      `path/to/*.pb.go linguist-generated`        → set
      `vendor/** linguist-vendored=true`          → set
      `*.pb.go -linguist-generated`               → unset (carve-out)
      `*.pb.go linguist-generated=false`          → unset
      `*.pb.go !linguist-generated`               → unspecified
    A pattern is anchored relative to the .gitattributes file's directory; later rules for the same
    attribute win (we keep order and fold each attribute independently left→right). Only the bounded
    `.gitattributes` rule text is read by this inventory; it never opens a target source body while
    deciding generated/vendored membership. Cheap + bounded (.gitattributes files are tiny and few —
    and we size-cap each one at _GITATTRIBUTES_SIZE_CAP to guard against a
    degenerate/adversarial .gitattributes that is tens of MB, which would read + parse into an
    unbounded matcher list)."""
    matchers = []   # (base_dir_rel, pattern, attribute_name, state)
    attribute_files = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
        if ".gitattributes" not in filenames:
            continue
        base_rel = os.path.relpath(dirpath, root)
        base_rel = "" if base_rel == "." else base_rel.replace(os.sep, "/")
        ga_path = os.path.join(dirpath, ".gitattributes")
        try:
            # Never follow a repository-controlled symlink for extraction
            # policy.  Apart from escaping the repo root, persisting the link as
            # target-SHA reconstruction context would be dishonest: GitHub's
            # file API returns the link entry, not an external workstation
            # file.  Match the source/config file guards and skip it entirely.
            if os.path.islink(ga_path):
                continue
            # SIZE CAP: skip degenerate/adversarial .gitattributes files that exceed the bound.
            # Real .gitattributes files are a few KB; a crafted one can be tens of MB.
            if os.path.getsize(ga_path) > _GITATTRIBUTES_SIZE_CAP:
                continue
            with open(ga_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        attribute_files.append(ga_path)
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = _gitattributes_fields(line)
            if len(parts) < 2:
                continue
            pattern, attrs = parts[0], parts[1:]
            # Unlike .gitignore, gitattributes forbids negative patterns.
            # Git warns and ignores a leading `!` after C-unquoting; treating
            # it as a literal would wrongly exclude a real `!name.py`. A
            # wildmatch-escaped `\!name.py` still begins with backslash and is
            # the supported way to address that literal filename.
            if not pattern or pattern.startswith("!"):
                continue
            for attr in attrs:
                if attr.startswith("!"):
                    state = None
                    a = attr[1:]
                elif attr.startswith("-"):
                    state = False
                    a = attr[1:]
                else:
                    state = True
                    a = attr
                name, _, val = a.partition("=")
                if name not in ("linguist-generated", "linguist-vendored"):
                    continue
                if val.lower() in ("false", "0", "no"):     # explicit OFF
                    state = False
                matchers.append((base_rel, pattern, name, state))
    return matchers, attribute_files


def _gitattributes_generated_matchers(root):
    """Backward-compatible matcher-only view of the gitattributes inventory."""
    matchers, _attribute_files = _gitattributes_generated_inventory(root)
    return matchers


# _gitattr_pat_to_regex / _matches_gitattr / _is_generated moved to cg_generated (re-exported at the top of
# this module). They are the pure generated/vendored PREDICATE (path → yes/no, no walk); the walk that feeds
# them (_gitattributes_generated_matchers, above) stays here because it needs the extractor's _SKIP_DIRS.


def _passes_file_guards(path, rel, attr_matchers):
    """The shared per-file SAFETY filter every walk in the extractor must apply (the ONE place the
    generated/symlink/size/binary checks live). Returns True iff `path` (repo-relative `rel`) is safe to
    OPEN-AND-READ for structure extraction:
    - NOT generated/vendored (GAP-15: `.gitattributes` linguist-generated/linguist-vendored + generated
      filename suffixes + generated dir names — nobody edits it → false adjacency)
    - NOT a symlink (avoids following cross-tree / cyclic links)
    - NOT larger than _FILE_SIZE_CAP (minified / generated / data — disproportionate parse cost)
    - NOT binary (a NUL byte in the first chunk — a renamed binary / compiled artefact)

    Extracted from _iter_source_files so the SCHEMA and CONFIG passes apply the IDENTICAL guards instead of
    re-walking + reading every .sql / .py / config file in FULL (the bypass that let a ~1.4 MB file, a binary
    renamed to .sql, or a generated *_pb2.py be fully read + regex-scanned outside these caps)."""
    # GAP-15: generated/vendored → EXCLUDE entirely. Checked BEFORE read so a parseable generated file
    # (e.g. *_pb2.py is valid Python / a generated .sql) still gets no synthetic symbols/tables/keys.
    if _is_generated(rel, attr_matchers):
        return False
    # Skip symlinks to files (avoids following cross-tree links).
    if os.path.islink(path):
        return False
    # Skip oversized files (minified/generated/data — see _FILE_SIZE_CAP). `.sql` schema dumps get a
    # larger cap (_SCHEMA_FILE_SIZE_CAP): a whole-DB DDL file is legitimately big, parses linearly, and is
    # output-bounded by _MAX_TABLES (a renamed binary is still rejected by _is_binary below).
    cap = _SCHEMA_FILE_SIZE_CAP if rel.lower().endswith(".sql") else _FILE_SIZE_CAP
    try:
        if os.path.getsize(path) > cap:
            return False
    except OSError:
        return False   # unreadable stat → skip
    # Skip pathological single-line/minified files before any parser or regex pass reads them in full. SQL is
    # handled one layer down by _cg_schema: large schema dumps are legitimate, so that pass drops only the
    # overlong physical line(s) and still parses the line-broken DDL around them.
    if not rel.lower().endswith(".sql") and _has_overlong_line(path):
        return False
    # Skip binary files (NUL byte in first chunk).
    if _is_binary(path):
        return False
    return True


def _iter_source_files(root, attr_matchers=None):
    """Walk `root` and yield (path, ext) for every SOURCE-ish file (GAP-14: a node for EVERY hand-edited
    source file, even one in a language we cannot parse). Skipping:
    - symlinks (avoids infinite loops on cyclic symlink trees)
    - directories in _SKIP_DIRS (vendor / generated / build / cache)
    - GENERATED / VENDORED files (GAP-15): `.gitattributes` linguist-generated/linguist-vendored markers
      + common generated filename suffixes + generated directory names (no node/edge — anti-cry-wolf)
    - files larger than _FILE_SIZE_CAP (minified / generated / data)
    - binary files (NUL byte in first chunk)
    - extensions not in _SOURCE_EXT (docs/data/images — never a code node)

    The yielded `ext` lets build_graph decide PARSE (.py + grammar-backed) vs bare NODE-ONLY (everything
    else in _SOURCE_EXT). We never skip NODING a source file just because we can't parse it; we only skip
    NODING generated/vendored files (which nobody edits → false adjacency).

    `attr_matchers`: pre-computed result of _gitattributes_generated_matchers(root). When None it is
    computed here (backward-compatible for direct callers). build_graph computes it once and passes it
    to avoid paying the tree-walk cost three times (source + config + this function)."""
    if attr_matchers is None:
        attr_matchers = _gitattributes_generated_matchers(root)   # GAP-15: repo's own generated/vendored decl
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune skipped dirs in-place so os.walk does not descend into them.
        # Also prune symlinked dirs to avoid cycles — os.walk with followlinks=False
        # does not follow them, but they still appear in dirnames on some platforms.
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))
        ]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _SOURCE_EXT:        # only source/programming files ever become nodes
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if _passes_file_guards(path, rel, attr_matchers):   # generated / symlink / size / binary
                yield path, ext


def _iter_config_files(root, attr_matchers=None):
    """Walk `root` and yield (path, cfg_ext) for every CONFIG file (.json/.yaml/.toml/.env/.ini/.cfg +
    the Dockerfile family), applying the IDENTICAL generated/symlink/size/binary guards as _iter_source_files.

    Config files are NOT in _SOURCE_EXT, so _iter_source_files never yields them — yet the config-graph pass
    must read them. Before this, _config_graph did its OWN os.walk + open().read() over every config file in
    FULL, bypassing the size/binary/generated/symlink caps: a ~1.4 MB config, a binary renamed to .json, or a
    generated config could be fully read + parsed. This shared, guarded walk closes that bypass. `cfg_ext` is
    resolved by NAME for the `.env` / Dockerfile families (via _cg_config._config_ext), so the caller gets the
    same synthetic extension its parser dispatches on.

    `attr_matchers`: pre-computed result of _gitattributes_generated_matchers(root). When None it is
    computed here (backward-compatible for direct callers). build_graph computes it once and passes it
    to avoid paying the tree-walk cost three times (source + config + this function)."""
    if attr_matchers is None:
        attr_matchers = _gitattributes_generated_matchers(root)   # GAP-15: repo's own generated/vendored decl
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))
        ]
        for fn in filenames:
            if fn in _CONFIG_SKIP_FILES:        # huge machine lock files (package-lock / poetry.lock / …)
                continue
            cfg_ext = _config_ext(fn)           # name-aware (.env / Dockerfile families) → synthetic ext
            if cfg_ext not in _CONFIG_EXTS:     # only real config files (code files go through source walk)
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if _passes_file_guards(path, rel, attr_matchers):   # generated / symlink / size / binary
                yield path, cfg_ext


# --------------------------------------------------------------------------------------------
# Python — stdlib ast (no third-party dep). Lives in _cg_python (its own leaf module — same
# discipline as the other per-language modules); re-exported here so `import code_graph_extract
# as X; X.extract_file_py(...)` and existing tests stay byte-identical.
# --------------------------------------------------------------------------------------------
from _cg_python import (  # noqa: E402, F401  (re-exported; deferred-import inside extract_file_py breaks the cycle)
    extract_file_py,
    _py_call_name,
    _py_base_names,
    _py_decorator_names,
    _py_signature_shape,
)


# --------------------------------------------------------------------------------------------
# UNICODE NORMALIZATION (NFC) of every coordinate string — the audit:unicode fix.
# --------------------------------------------------------------------------------------------
# A path/symbol can be the SAME logical file yet byte-DIFFERENT across the two sources that feed
# the graph: the EXTRACTOR (a filesystem walk / tarball — git preserves whatever bytes were
# committed, e.g. an NFD-decomposed `café/résumé.py` from a decomposing tool or platform) vs the
# WEBHOOK (GitHub's API reports paths in NFC). Postgres `text =` (the engine's `path = ANY(touched)`
# coupling/collision join, and `patch_graph`'s DELETE/re-INSERT by path) is CODEPOINT equality, not
# canonical equality — so an NFC vs NFD pair (`é` = U+00E9 vs `e`+U+0301) does NOT match. The cost:
# (1) a FALSE MISS — two PRs touching the "same" file under different normalizations are not seen to
# collide → the engine goes blind on that file; (2) DUPLICATE nodes — a patch (NFC touched-set)
# fails to DELETE the stored NFD node, then re-inserts an NFC twin. Fix: fold every coordinate string
# to NFC at the extractor boundary (the one funnel every ingest path flows through). NFC is the right
# target — it is what git/GitHub/the W3C use, so the graph lands in the SAME form the webhook reports.
# Content-free: a path/identifier is metadata (names, never code body); NFC re-encodes the same
# characters, never the file's contents. Idempotent (NFC of NFC = NFC), so re-normalizing is harmless.
def _nfc(s):
    """NFC-normalize a coordinate string; pass through non-str / empty unchanged (defensive)."""
    if not isinstance(s, str) or not s:
        return s
    return unicodedata.normalize("NFC", s)


def _normalize_node_coords(n):
    """Fold a node's coordinate strings (id / path / name) to NFC, in place. Bodies are never stored."""
    if not isinstance(n, dict):
        return n
    for k in ("id", "path", "name"):
        if k in n and isinstance(n[k], str):
            n[k] = _nfc(n[k])
    return n


def _normalize_edge_coords(e):
    """Fold an edge's coordinate strings (src / dst) to NFC, in place. `src`/`dst` are path or path::name."""
    if not isinstance(e, dict):
        return e
    for k in ("src", "dst"):
        if k in e and isinstance(e[k], str):
            e[k] = _nfc(e[k])
    return e


def _extract_source_files(root, source_files, parsers):
    """STAGE 1 — the per-file dispatch loop: walk the (already guard-filtered) `source_files`, route each
    to its per-language extractor, apply the per-file symbol/edge cap, stamp file nodes with their freshness
    hash, and accumulate. Returns (nodes, edges, parsed, failed, skipped) — the raw per-file contribution,
    BEFORE the schema/config/IaC/API passes and BEFORE NFC/dedup/resolution. `parsers` is the (possibly
    empty) {grammar-name -> tree_sitter.Parser} map computed once by the caller.

    Pure extraction of build_graph's main parse loop; behaviour byte-identical (same dispatch, same caps,
    same counters, same node/edge order)."""
    nodes, edges, parsed, failed, skipped = [], [], 0, 0, 0
    for path, ext in source_files:
        rel = os.path.relpath(path, root)
        # status: "parsed" (parser ran ok) | "failed" (parser ran, errored) | "noded" (no parse, bare node)
        status = "failed"
        try:
            if _has_too_many_lines(path):
                # Too many physical lines under the byte cap is the line-count analogue of the symbol explosion
                # attack. Preserve recall at file-level and let schema/config/IaC passes run their own bounded
                # substrate scanners later; do not spend parser CPU on a generated/adversarial source shape.
                _lang = "unity" if ext in _UNITY_ASSET_EXTS else _LABEL_BY_EXT.get(ext, "unknown")
                n, e = [{"id": rel, "kind": "file", "path": rel, "language": _lang}], []
                status = "noded"
            elif ext == ".py":
                n, e, ok = extract_file_py(path, rel)
                status = "parsed" if ok else "failed"
            elif ext in _GRAMMAR_BY_EXT:
                label = _LABEL_BY_EXT[ext]
                gname = _GRAMMAR_BY_EXT[ext]
                parser = parsers.get(gname)
                if parser is None and gname in ("c", "cpp"):    # c/c++ grammars are cross-compatible (cpp is a
                    parser = parsers.get("cpp" if gname == "c" else "c")  # C superset) — use whichever loaded
                if parser is None:
                    # GAP-14: grammar absent → still emit a BARE file node (NODE, not PARSE). Dropping it
                    # made the file MISSING from the board/direct-collision surface; we keep the node
                    # (language = its label, e.g. "go") with NO contains/calls/imports (no false edges).
                    n, e = [{"id": rel, "kind": "file", "path": rel, "language": label}], []
                    status = "noded"
                elif label == "html":                      # html/template → asset references (src/href)
                    n, e, ok = extract_file_html(path, rel, parser)
                    status = "parsed" if ok else "failed"
                elif label == "elixir":                    # Elixir: def/defmodule are call nodes, not distinct
                    # types — _GENERIC_SPEC cannot dispatch on node type here.  Use the bespoke extractor.
                    n, e, ok = extract_file_elixir(path, rel, parser)
                    status = "parsed" if ok else "failed"
                elif label in _GENERIC_SPEC:               # go / java / ruby / php / c# (spec-driven)
                    n, e, ok = extract_file_generic(path, rel, label, parser, _GENERIC_SPEC[label])
                    status = "parsed" if ok else "failed"
                else:                                      # js / ts (bespoke extractor)
                    n, e, ok = extract_file_ts(path, rel, label, parser)
                    status = "parsed" if ok else "failed"
            elif ext == ".astro":
                # Astro SFC: parse the leading frontmatter and client script as TypeScript,
                # then reuse the HTML local-asset pass over the markup.  Missing parsers
                # degrade to a correctly-labelled bare Astro file node.
                ts_parser = parsers.get("typescript") or parsers.get("javascript")
                html_parser = parsers.get("html")
                n, e, ok = extract_file_astro(path, rel, ts_parser, html_parser)
                status = "parsed" if ok else "noded"
            elif ext == ".svelte":
                # Svelte SFC: use tree-sitter-svelte to locate the <script> block, then
                # delegate JS/TS extraction to extract_file_svelte -> _walk_ts_tree.
                # Prefer the typescript parser for <script lang="ts"> parity; fall back to
                # javascript (still parses valid TS in most cases).  If neither is loaded
                # (grammar absent) the extractor returns a bare file node — recall-safe.
                svelte_parser = parsers.get("svelte")
                ts_parser = parsers.get("typescript") or parsers.get("javascript")
                n, e, ok = extract_file_svelte(path, rel, svelte_parser, ts_parser)
                status = "parsed" if ok else "noded"
            elif ext == ".vue":
                # Vue SFC: tree-sitter-vue has no pip wheel; use the regex-based extractor
                # to extract the <script>/<script setup> block and parse it with the TS/JS
                # parser.  Falls back to bare file node when the parser is absent.
                ts_parser = parsers.get("typescript") or parsers.get("javascript")
                n, e, ok = extract_file_vue(path, rel, ts_parser)
                status = "parsed" if ok else "noded"
            elif ext in (".css", ".scss", ".sass", ".less", ".styl"):
                # Styles stay precision-first: file nodes plus local dependency
                # directives only; selectors/declarations never become symbols.
                n, e, ok = extract_file_css(path, rel)
                status = "parsed" if ok else "noded"
            else:
                # GAP-14: a SOURCE file in a language we have NO grammar for (.scala/.dart/.ex/...). It is
                # hand-edited code, so it MUST appear as a node for direct (storey-1) collision + unknown-
                # marking — but we cannot parse it, so we emit ONE bare `file` node (language="unknown")
                # with NO structural edges (no false `calls`/`contains`). This is the honesty fix: skip
                # PARSING, never skip NODING. The schema/config graphs still see .sql/config separately.
                # Unity-asset refinement: a Unity scene/prefab/.meta/shader carries a specific language
                # label ("unity") instead of the generic "unknown" so the board/surface labels read
                # honestly (no structural edges either way — conservative bias keeps prefab GUIDs out
                # of the moat).
                _lang = "unity" if ext in _UNITY_ASSET_EXTS else "unknown"
                n, e = [{"id": rel, "kind": "file", "path": rel, "language": _lang}], []
                status = "noded"
            # A tree-sitter extractor can return useful evidence from an
            # error-tolerant partial tree.  The extractor stamps that document
            # ``incomplete`` before returning ``ok=False``; retain the partial
            # evidence but count it as a bounded/noded analysis rather than a
            # hard parser failure.
            if any(
                node.get("kind") == "file"
                and node.get("analysis_status") == "incomplete"
                for node in n
            ):
                status = "noded"
        except Exception:
            # Any unexpected exception on a single file is caught here so one pathological file
            # (e.g. a grammar bug, a memory edge case, a deeply nested AST, the C-stack RecursionError
            # ast.parse raises on a deeply-nested input on py3.12+) never aborts the whole build_graph
            # run. Preserve the canonical bare file node: direct-collision/file-level Unknown remains
            # available, and input_file_count continues to equal the persisted document-path set at
            # the DB observability wall. Only structural detail is lost; files_failed records why.
            lang = (
                "python" if ext == ".py"
                else "unity" if ext in _UNITY_ASSET_EXTS
                else _LABEL_BY_EXT.get(ext, "unknown")
            )
            n = [{"id": rel, "kind": "file", "path": rel, "language": lang}]
            e = []
            status = "failed"
        # PER-FILE SYMBOL/EDGE CAP (audit:dos): a single file that explodes into more than the cap of
        # nodes/edges is pathological (machine-emitted or adversarial — a 1.5 MB file of 150k one-line
        # defs slips under the SIZE cap). DROP its structural detail and keep ONLY a bare file node so
        # the file still shows for direct collision + is honestly file-level, but cannot balloon
        # memory/payload or seed the engine's O(symbols) joins. Degrade to honest 'unknown', never OOM.
        if len(n) > _PER_FILE_SYMBOL_CAP or len(e) > _PER_FILE_EDGE_CAP:
            lang = (n[0].get("language") if n and isinstance(n[0], dict) else None) or "unknown"
            n = [{"id": rel, "kind": "file", "path": rel, "language": lang}]
            e = []
            status = "noded"   # walked + got a (bare) node, but the structural detail was bounded away
        analysis_status = None
        if status == "failed":
            analysis_status = "failed"
        elif status == "noded" and ext not in _COMPLETE_BARE_EXTS:
            # Parser-unavailable, unsupported-language, unavailable SFC
            # parser, line-cap and per-file-cap fallbacks all retain a bare
            # document node. They are not a successful structural analysis:
            # treating them as normal would let a missing edge produce a false
            # Clear. Complete-bare formats above are different—their semantics
            # are intentionally extracted by a bounded pass or are explicitly
            # defined as file-only.
            analysis_status = "incomplete"
        if analysis_status is not None:
            # A partial/failed extractor still owns one canonical document
            # fact, but it must never be indistinguishable from a successfully
            # analyzed file at the effective-adjacency wall.
            file_nodes = [fn for fn in n if fn.get("kind") == "file"]
            if not file_nodes:
                lang = (
                    "python" if ext == ".py"
                    else "unity" if ext in _UNITY_ASSET_EXTS
                    else _LABEL_BY_EXT.get(ext, "unknown")
                )
                fallback = {
                    "id": rel,
                    "kind": "file",
                    "path": rel,
                    "language": lang,
                }
                n.insert(0, fallback)
                file_nodes = [fallback]
            for fn in file_nodes:
                fn["analysis_status"] = analysis_status
        # FRESHNESS KEY (content-free): stamp each walked FILE node with its git-blob-sha content hash, so the
        # gate can later PROVE a claim's diff line numbers were mapped against the SAME version of the file the
        # graph was ingested from (graph hash == base hash) before it dares demote a file-level collision to the
        # finer symbol verdict. A hash is a fingerprint, never the bytes (content-free). We hash here (once per
        # walked file) rather than re-reading; a file node already exists for every walked source file. Synthetic
        # universe nodes (incremental resolution only, never returned) are NOT hashed — they carry no hash = unknown.
        # (Runs AFTER the cap, so even a capped-to-bare file still carries its freshness hash.)
        for fn in n:
            if fn.get("kind") == "file":
                fn["content_hash"] = _git_blob_sha(path)
        nodes.extend(n)
        edges.extend(e)
        parsed += 1 if status == "parsed" else 0
        failed += 1 if status == "failed" else 0
        skipped += 1 if status == "noded" else 0   # noded = walked + got a node, but PARSING was skipped
    return nodes, edges, parsed, failed, skipped


def _assemble_nodes(nodes, edges):
    """STAGE 2 — node/edge assembly: fold every coordinate string to NFC (nodes AND edges, in place), then
    deduplicate repeated representations of the same (id, kind, path) Node (a .sql file gets two
    `kind="file"` nodes — one source-pass, one schema-pass).
    Returns the deduplicated node list; `edges` is normalized in place (the caller keeps its own reference).

    Runs AFTER all the substrate passes have contributed and BEFORE import resolution — exactly where the
    inline code did. Pure extraction; behaviour byte-identical (same NFC fold, same best-node tie-breaks,
    same content_hash merge, same resulting node set/order)."""
    # UNICODE NORMALIZATION (audit:unicode): fold every coordinate string to NFC BEFORE import resolution,
    # so (a) the graph lands in the SAME form the webhook reports → the engine's `path = ANY(touched)`
    # coupling/collision join and patch_graph's DELETE/re-INSERT match the same logical file (no NFC/NFD
    # false-miss, no duplicate node), and (b) resolution + the universe-path `have` join below compare
    # canonical paths (an unchanged NFC importer resolves to a just-extracted file even if the FS handed us
    # NFD). Content-free (re-encodes characters of a name/path, never the file body); idempotent.
    for n in nodes:
        _normalize_node_coords(n)
    for e in edges:
        _normalize_edge_coords(e)
    # SAME-NODE DEDUP (robustness): a .sql file receives TWO `kind="file"` nodes — one from
    # _iter_source_files (language="unknown", because .sql is in _SOURCE_EXT but has no grammar) and
    # one from _schema_graph (language="sql"). Two file nodes with the same id cause (a) duplicate rows
    # in the DB ingest, (b) double-counting on the board surface. Deduplicate here (after NFC fold,
    # before resolution) by (id,kind,path): for each exact logical Node, keep the representation with the most
    # information. IMPORTANT: a legal Git path can equal a generated resource/symbol id (for example file
    # `cfgkey::settings.json::veripsa.webhook.py`). Different kind/path Nodes with the same id are distinct
    # first-class facts and MUST both survive; id-only dedup silently erased the Resource Node.
    seen_nodes: dict = {}   # (id,kind,path) -> best representation
    seen_hash: dict = {}    # same key -> content_hash merged into that representation
    seen_analysis_status: dict = {}  # failed > incomplete > ambiguous > absent
    analysis_status_rank = {
        None: 0,
        "ambiguous": 1,
        "incomplete": 2,
        "failed": 3,
    }
    for n in nodes:
        nid = n.get("id")
        if nid is None:
            continue
        key = (nid, n.get("kind"), n.get("path"))
        # accumulate content_hash across all nodes for this id (source pass stamps it; schema pass does not)
        if "content_hash" in n and n["content_hash"] is not None:
            seen_hash[key] = n["content_hash"]
        status = n.get("analysis_status")
        if (
            analysis_status_rank.get(status, -1)
            > analysis_status_rank.get(seen_analysis_status.get(key), 0)
        ):
            seen_analysis_status[key] = status
        if key not in seen_nodes:
            seen_nodes[key] = n
        else:
            prev = seen_nodes[key]
            # prefer a specific language over "unknown" — schema pass knows language="sql";
            # source pass only knows language="unknown" for a .sql (no grammar-backed parser).
            if prev.get("language") in (None, "unknown") and n.get("language") not in (None, "unknown"):
                seen_nodes[key] = n
            # prefer whichever has a content_hash (the source-pass file node adds it; the schema
            # pass file node does not because _schema_graph never reads raw bytes).
            elif "content_hash" not in seen_nodes[key] and "content_hash" in n:
                seen_nodes[key] = n
    # MERGE content_hash: after picking the best node per id, stamp any merged hash back in.
    # This preserves the freshness fingerprint even when the richer-language node (e.g. schema-pass
    # language="sql") displaced the source-pass node that originally stamped the hash.
    for key, h in seen_hash.items():
        if key in seen_nodes and "content_hash" not in seen_nodes[key]:
            seen_nodes[key]["content_hash"] = h
    for key, status in seen_analysis_status.items():
        if key in seen_nodes:
            seen_nodes[key]["analysis_status"] = status
    return list(seen_nodes.values())


def _deduplicate_edges(edges):
    """Deduplicate by persisted identity without uncertainty-order drift.

    A resolved and a status-bearing producer can independently emit the same
    ``(src,dst,kind)`` edge. Effective adjacency must keep the stronger resolved
    fact regardless of substrate execution order; an uncertain first-wins row
    would otherwise downgrade real adjacency to Unknown.
    """
    by_identity = {}
    order = []
    for original_edge in edges:
        edge = original_edge
        if (
            edge.get("ambiguous_reference") is True
            and edge.get("reference_status") is None
        ):
            # Normalize the legacy producer annotation at the assembly boundary.
            # Otherwise persistence would store the row as resolved even though
            # observability counted it as ambiguous.
            edge = {**edge, "reference_status": "ambiguous"}
        key = (edge["src"], edge["dst"], edge["kind"])
        previous = by_identity.get(key)
        if previous is None:
            by_identity[key] = edge
            order.append(key)
            continue
        if (
            previous.get("reference_status") is not None
            and edge.get("reference_status") is None
        ):
            by_identity[key] = edge
    return [by_identity[key] for key in order]


def build_graph(root, universe_paths=None, repo=None):
    """Walk `root` and build the multi-language code graph. Python always works; other languages
    are extracted when the tree-sitter grammars are installed (else counted as skipped).

    INCREMENTAL extraction: when `root` holds only a SUBSET of a repo (the files a push changed),
    pass `universe_paths` = the FULL repo file-path set (content-free — every retained file path, e.g.
    from the DB). It supplies synthetic file nodes used ONLY to RESOLVE the changed files' imports to
    UNCHANGED targets — they are not returned. With `universe_paths=None` this is the unchanged whole-repo
    build (resolution sees exactly the walked files)."""
    langs = _ts_languages()
    parsers = {}
    if langs:
        from tree_sitter import Parser
        parsers = {name: Parser(lang) for name, lang in langs.items()}
    # Compute the GUARDED source-file list ONCE (generated/symlink/size/binary already filtered). The main
    # parse loop below uses it, AND it is handed to the schema/config passes so they share the SAME guards
    # instead of each re-walking + reading every .sql/.py/config file in FULL (the bypass that let an
    # oversized / binary-renamed / generated file slip past _iter_source_files' caps into those passes).
    #
    # SINGLE GITATTRIBUTES WALK (robustness): _gitattributes_generated_matchers does its own os.walk to
    # parse `.gitattributes` rules. Both _iter_source_files and _iter_config_files used to call it
    # independently, causing the tree to be walked (and every .gitattributes to be opened + parsed) up to
    # THREE times per build_graph call (source + config + schema internal). We compute it ONCE here and
    # pass the result to both iterators. This also means the _GITATTRIBUTES_SIZE_CAP guard fires only once
    # per .gitattributes file instead of per-iterator, which is the correct bound.
    attr_matchers, gitattributes_files = _gitattributes_generated_inventory(root)
    source_files = list(_iter_source_files(root, attr_matchers=attr_matchers))
    # Every dedicated substrate pass reports paths for which it could not prove
    # complete extraction (read cap, parser failure, candidate/catalog cap, or
    # bounded wrapper failure).  The shared set is promoted to persisted
    # ``analysis_status=incomplete`` below, so a missing edge can never be
    # interpreted as evidence of Clear.
    incomplete_paths = set()
    # STAGE 1 — per-file dispatch: walk source_files, route each to its per-language extractor, cap, and
    # stamp freshness hashes. Returns the raw per-file node/edge contribution + the parsed/failed/skipped tally.
    nodes, edges, parsed, failed, skipped = _extract_source_files(root, source_files, parsers)
    # schema + config passes read the SAME guarded `source_files` list (no independent re-walk that bypasses
    # the size/binary/generated/symlink caps). The config pass ALSO needs config files (.json/.yaml/.env/…),
    # which are NOT in _SOURCE_EXT, so it does a guarded config-file walk through _passes_file_guards.
    sch_nodes, sch_edges = _schema_graph(
        root,
        source_files,
        incomplete_paths_out=incomplete_paths,
    )   # schema graph: tables + alters + queries
    nodes.extend(sch_nodes)
    edges.extend(sch_edges)
    config_files = list(_iter_config_files(root, attr_matchers=attr_matchers))  # config files, SAME guards
    # A complete persisted file universe is part of the incremental correctness
    # contract.  Keep a config_file node even when a config has zero distinctive
    # keys (or is an OpenAPI/Kubernetes document handled by another substrate);
    # otherwise an unchanged contract-bearing file can disappear from the DB
    # catalog and a later changed file cannot reconstruct the full target tree.
    #
    # `.gitattributes` is also first-class reconstruction context even though it
    # is not parsed as application config: its linguist-generated/vendored rules
    # decide which OTHER paths exist in the full graph.  Persist every accepted
    # root/nested attributes file so an incremental target tree receives the
    # identical rule set, including negative carve-outs.  _assemble_nodes
    # de-duplicates any richer config node emitted below.
    for attributes_path in gitattributes_files:
        rel = os.path.relpath(attributes_path, root).replace(os.sep, "/")
        nodes.append(
            {
                "id": rel,
                "kind": "config_file",
                "path": rel,
                "language": "gitattributes",
            }
        )
    for config_path, _config_ext_name in config_files:
        rel = os.path.relpath(config_path, root).replace(os.sep, "/")
        nodes.append(
            {
                "id": rel,
                "kind": "config_file",
                "path": rel,
                "language": "config",
            }
        )
    cfg_nodes, cfg_edges = _config_graph(
        root,
        config_files,
        source_files,
        incomplete_paths_out=incomplete_paths,
    )   # config graph: config_file/key nodes + reads_config
    nodes.extend(cfg_nodes)
    edges.extend(cfg_edges)
    # IaC CROSS-SUBSTRATE GRAPH (Terraform + Kubernetes): a Terraform resource defined in one .tf file
    # and referenced in another, or a Kubernetes Service that selects a Deployment by label, are coupled
    # via a SHARED NON-CODE RESOURCE with NO code edge — the crown-jewel coupling structural tools miss.
    # .tf files are in _SOURCE_EXT → already in source_files. .yaml/.yml k8s manifests come from
    # config_files (they are in _CONFIG_EXTS). Both lists are already guard-filtered (size/binary/symlink/
    # generated). The IaC pass hands ALL_SOURCE + ALL_CONFIG to _iac_graph so it can route .tf to the
    # Terraform extractor and .yaml/.yml to the Kubernetes extractor (the latter only fires on documents
    # that carry both apiVersion and kind — generic YAML is untouched). Content-free (resource NAMES +
    # file paths only). Never-crash (all failures bounded within _iac_graph).
    iac_nodes, iac_edges = _iac_graph(
        root,
        source_files + config_files,
        incomplete_paths_out=incomplete_paths,
    )
    nodes.extend(iac_nodes)
    edges.extend(iac_edges)
    # API-CONTRACT CROSS-SUBSTRATE GRAPH (GraphQL + protobuf/gRPC): a GraphQL `type Order` defined in a
    # .graphql schema and referenced from a resolver / a gql`...` query, or a protobuf `service OrderService`
    # defined in a .proto and referenced by hand-written gRPC client code, are coupled via a SHARED CONTRACT
    # SYMBOL with NO code edge (the schema/proto is its own language and the generated bridge stubs are
    # excluded as vendored) — the crown-jewel coupling structural tools miss. .graphql/.gql/.proto are in
    # _SOURCE_EXT → already in source_files (bare file nodes). DEFINITIONS come from those files; REFERENCES
    # come from any source file (code or another schema file), all already in source_files. REUSES the
    # alters (definer) / queries (referencer) edge kinds so the existing shared-resource adjacency couples
    # them with NO SQL change (mirrors _cg_iac / _cg_schema). Content-free (contract symbol NAMES + paths
    # only). Never-crash (all failures bounded within _api_contract_graph).
    api_nodes, api_edges = _api_contract_graph(
        root,
        source_files,
        incomplete_paths_out=incomplete_paths,
    )
    nodes.extend(api_nodes)
    edges.extend(api_edges)
    # OPENAPI / SWAGGER REST CROSS-SUBSTRATE GRAPH: a REST operation (operationId `getUserById`, or
    # normalized `GET /users/{}`) or component schema DEFINED in an openapi.yaml/.json spec and
    # IMPLEMENTED by a backend handler (a function named after the operationId) and CALLED by a
    # frontend client (a `/users/:id` route literal) are coupled via a SHARED OPERATION/SCHEMA node
    # with NO code edge — the spec is its own document (different language/dir from server + client),
    # invisible to the call + import graphs = the crown-jewel coupling structural tools miss. A file
    # is a spec ONLY when its parsed top level carries openapi:/swagger: (mirrors _is_json_schema_file
    # discipline) — every ordinary yaml/json is left to _cg_config (ADDITIVE: zero api_operation nodes
    # for them). .yaml/.yml/.json are in _CONFIG_EXTS → already in config_files (guard-filtered);
    # DEFINITIONS come from spec-marked files there; REFERENCES come from any code source file. REUSES
    # the alters (definer) / queries (referencer) edge kinds so the shared-resource adjacency couples
    # them with NO SQL change (mirrors _cg_iac / _cg_api_contract). Content-free (operation/schema/path
    # NAMES + paths only — never descriptions/examples/defaults/bodies). Never-crash (all failures
    # bounded within _openapi_graph). PyYAML is tried first; absent → a bounded line-scanner fallback.
    oas_nodes, oas_edges = _openapi_graph(
        root,
        source_files + config_files,
        incomplete_paths_out=incomplete_paths,
    )
    nodes.extend(oas_nodes)
    edges.extend(oas_edges)
    # CI PACKAGE-SCRIPT CONTRACT GRAPH: a GitHub Actions workflow that runs `npm run typecheck`
    # depends on the `typecheck` script declared in package.json, but there is no source import edge
    # between `.github/workflows/ci.yml` and `package.json`. Reuse alters/queries on a shared script
    # key, same as API/IaC substrates. Content-free: script names + package dirs + file paths only.
    # Precision: references come only from workflow `run:` entries and ambiguous monorepo script names
    # are skipped unless a working-directory/cd target resolves the package uniquely.
    ci_nodes, ci_edges = _ci_script_graph(
        root,
        config_files,
        incomplete_paths_out=incomplete_paths,
    )
    nodes.extend(ci_nodes)
    edges.extend(ci_edges)
    # TAURI COMMAND CONTRACT GRAPH: a Rust `#[tauri::command] fn greet` and a frontend
    # `invoke("greet")` call are a cross-tier app contract with no import/call edge between
    # `src-tauri` and `src`. Reuse alters/queries on a shared command key, same as the API/IaC
    # substrates. Content-free: command names + file paths only. Precision: the frontend side
    # only fires when `invoke` comes from the official Tauri API module and the command is
    # locally defined once.
    tauri_nodes, tauri_edges = _tauri_command_graph(
        root,
        source_files,
        incomplete_paths_out=incomplete_paths,
    )
    nodes.extend(tauri_nodes)
    edges.extend(tauri_edges)
    # ASYNC JOB / QUEUE CONTRACT GRAPH: Celery `@shared_task(name="...")` and
    # `.send_task("...")`, plus BullMQ `new Worker("queue")` and `new Queue("queue")`,
    # are runtime contracts with no import/call edge between producer and consumer files.
    # Reuse alters/queries on shared contract keys, with literal names and official
    # framework anchors only. Content-free: task/queue names + file paths only.
    job_nodes, job_edges = _job_queue_graph(
        root,
        source_files,
        incomplete_paths_out=incomplete_paths,
    )
    nodes.extend(job_nodes)
    edges.extend(job_edges)
    # STAGE 2 — assembly: NFC-fold every node/edge coordinate (in place) and deduplicate file nodes by id,
    # AFTER all substrate passes have contributed and BEFORE import resolution (exactly where the inline code
    # ran). `edges` is normalized in place; the deduped node list is returned.
    nodes = _assemble_nodes(nodes, edges)
    # resolution universe: on an INCREMENTAL build, the changed files' imports must still resolve to
    # unchanged files. Add synthetic file nodes for every universe path we did NOT just extract — used
    # ONLY by _resolve_imports (it reads file PATHS, never bodies), and NOT returned in `nodes`.
    res_nodes = nodes
    if universe_paths:
        # NFC the universe paths too (they come from the webhook/DB) so the `have` set + resolution join
        # the just-extracted (now-NFC) file paths canonically — a stray NFD universe path can't shadow one.
        have = {n["path"] for n in nodes if n.get("kind") == "file"}
        res_nodes = nodes + [{"kind": "file", "path": _nfc(p)} for p in universe_paths if p and _nfc(p) not in have]
    ambiguous_import_paths = set()
    edges = _resolve_imports(
        res_nodes,
        edges,
        ambiguous_paths_out=ambiguous_import_paths,
    )  # imports: module name → repo FILE path (file→file deps)
    # CROSS-TIER ROUTE↔CALL coupling (PR #270 → integration): a backend route-DEFINITION file and a frontend
    # request-URL file that share a SPECIFIC route are a client/server CONTRACT — a real file→file dependency
    # with NO code edge / NO shared symbol between them (different language, different dir), invisible to the
    # call + import graphs. Emit it as a file→file `imports` edge (kind ∈ the CHECK set → no schema change) so
    # it rides the engine's EXISTING imp_out/imp_in adjacency + hub-dampening. Emitted HERE, AFTER
    # _resolve_imports, ON PURPOSE: resolution's SAME-LANGUAGE filter (_family(dst)==_family(src)) would DROP a
    # cross-tier .py↔.ts edge — these endpoints are already resolved FILE PATHS, so they must bypass it. The
    # specificity floors that give #270 its precision (concrete first segment + shared concrete anchor; drop
    # bare/ubiquitous routes) live in tests/cross_tier_route_probe.py and are reused VERBATIM by the producer
    # (no re-derivation → no drift). Content-free (route path strings + file paths only). The edges below ride
    # the same dedup the resolved edges do. (Build-subset/incremental: scans `root`, the changed-file subtree.)
    _xt_nodes, _xt_edges = _routes_graph(
        root,
        source_files,
        incomplete_paths_out=incomplete_paths,
    )
    for edge in _xt_edges:
        # Route coupling intentionally reuses the imports edge kind.  Stamp the
        # producer here so observability can distinguish it from language imports;
        # persistence ignores the annotation but retains the semantic edge.
        edge["substrate"] = "routes"
        edge["extractor"] = "_cg_routes"
    edges.extend(_xt_edges)
    # A call/import repeated in a file is one edge. When an exact and an
    # ambiguous producer share that identity, exact evidence wins independent
    # of producer order.
    deduped = _deduplicate_edges(edges)
    # UNCERTAINTY: each producer reports the document paths whose extraction it
    # could not prove complete.  Keep the persisted graph path-local here:
    # `main_impact_surface` performs the cross-change safety promotion only when
    # an unbounded failed/incomplete path is actually in flight.  Marking every
    # repository document here would let one unrelated unsupported source keep
    # every future review Unknown indefinitely.
    document_paths = {
        node.get("path")
        for node in nodes
        if (
            node.get("kind") in {"file", "config_file"}
            and node.get("path")
        )
    }
    incomplete_documents = {
        _nfc(path)
        for path in incomplete_paths
        if path
    }
    for node in nodes:
        if (
            node.get("kind") in {"file", "config_file"}
            and node.get("path") in incomplete_documents
            and node.get("analysis_status") != "failed"
        ):
            node["analysis_status"] = "incomplete"

    # Ambiguous resource/import evidence stays in the graph with an inert edge
    # marker. Promote both document endpoints when the destination is a real
    # document path (never a resource/reference string) so either side degrades
    # to Unknown. Failed/incomplete evidence remains stronger.
    ambiguous_documents = set(ambiguous_import_paths)
    ambiguous_documents.update({
        endpoint
        for edge in deduped
        if (
            edge.get("reference_status") == "ambiguous"
            or edge.get("ambiguous_reference") is True
        )
        for endpoint in (edge.get("src"), edge.get("dst"))
        if endpoint in document_paths
    })
    for node in nodes:
        if (
            node.get("kind") in {"file", "config_file"}
            and node.get("path") in ambiguous_documents
            and node.get("analysis_status") not in {"failed", "incomplete"}
        ):
            node["analysis_status"] = "ambiguous"
    # DETERMINISM (belt-and-suspenders, audit:iter-5): canonicalize the node/edge order BEFORE return so
    # the serialized graph is BYTE-IDENTICAL across runs regardless of any upstream INSERTION order. The
    # per-file/substrate passes accumulate nodes/edges from `set`s whose str-iteration order varies by
    # PYTHONHASHSEED (the ref-producing fns are now sorted at the source, but other substrate passes —
    # routes/config/iac/openapi/api-contract — still iterate sets/dicts into emission), and dict-based
    # node dedup (_assemble_nodes) preserves whatever insertion order it received. A final stable sort
    # makes the OUTPUT canonical no matter the upstream order, closing the regression class structurally.
    # This is a PURE REORDER — it does NOT change the node/edge SET. Distinct kinds/paths may deliberately share
    # an id (legal path versus generated resource id), so include all three identity fields in the total order.
    # Edges are already deduped to their full (src,dst,kind) identity. Content-free; reads no body.
    nodes.sort(key=lambda n: (
        n.get("id") or "", n.get("kind") or "", n.get("path") or ""
    ))
    deduped.sort(key=lambda e: (e.get("src") or "", e.get("dst") or "", e.get("kind") or ""))

    # FIRST-CLASS RESOURCE CONTRACT.  Extraction itself owns canonical key,
    # scope, producer and provenance; the coordinate writer supplies the real
    # repository name.  Offline/temp-tree callers get an explicit local sentinel
    # rather than missing metadata (the DB coordinate still remains authoritative).
    graph_repo = str(repo).strip() if repo is not None and str(repo).strip() else "<local>"
    graph = enrich_resource_nodes(
        {"root": root, "extractor_version": EXTRACTOR_VERSION,
         "nodes": nodes, "edges": deduped,
         "files_parsed": parsed, "files_failed": failed, "files_skipped": skipped},
        repo=graph_repo,
    )

    # CROSS-KIND RESOURCE-KEY COLLISION WALL. Canonical resource keys are the
    # join coordinate used by shared-resource adjacency, but the string itself
    # carries no kind tag. A table and a config_key may both legitimately be
    # named ``database_url``; treating their edges as resolved would therefore
    # false-couple unrelated SQL and configuration paths. Build the catalog
    # from the final enriched resource set and make every resource-bearing edge
    # to a key represented by more than one resource KIND explicit ambiguity.
    # Multiple nodes of the SAME kind remain governed by that substrate's
    # existing multi-definer contract and are intentionally unchanged.
    resource_catalog = {}
    resource_kinds_by_key = {}
    for node in graph["nodes"]:
        if node.get("kind") in RESOURCE_NODE_KINDS:
            key = node.get("canonical_key")
            resource_catalog.setdefault(key, set()).add(
                RESOURCE_KIND_TO_SUBSTRATE[node["kind"]])
            resource_kinds_by_key.setdefault(key, set()).add(node["kind"])
    cross_kind_resource_keys = {
        key for key, kinds in resource_kinds_by_key.items() if len(kinds) > 1
    }
    resource_edge_kinds = {
        "alters", "queries", "reads_config", "alters_col", "queries_col"
    }
    collision_source_paths = set()
    for edge in graph["edges"]:
        edge.setdefault("substrate", classify_edge_substrate(edge, resource_catalog))
        if (
            edge.get("kind") in resource_edge_kinds
            and edge.get("dst") in cross_kind_resource_keys
        ):
            edge["reference_status"] = "ambiguous"
            if edge.get("src"):
                collision_source_paths.add(edge["src"])

    # Keep the extractor's document status aligned with its retained ambiguous
    # edges. The durable edge status is sufficient for DB/query correctness;
    # the node marker additionally makes both source endpoints visibly
    # uncertain in the producer graph. Never weaken a failed/incomplete status.
    for node in graph["nodes"]:
        if (
            node.get("kind") in {"file", "config_file"}
            and node.get("path") in collision_source_paths
            and node.get("analysis_status") not in {"failed", "incomplete"}
        ):
            node["analysis_status"] = "ambiguous"

    assert_valid_graph(graph, require_resource_metadata=True)

    file_paths = {
        n.get("path") for n in graph["nodes"]
        if n.get("kind") in {"file", "config_file"} and n.get("path")
    }
    # Subset extraction intentionally keeps resolution-only universe nodes out of
    # the returned graph.  They are nevertheless known file targets: a resolved
    # import to one must not be reported as unresolved merely because its synthetic
    # node was removed before persistence.
    known_file_paths = set(file_paths)
    if universe_paths:
        known_file_paths.update(_nfc(path) for path in universe_paths if path)
    unresolved = sum(
        1 for edge in graph["edges"]
        if edge.get("kind") == "imports"
        and edge.get("dst") not in known_file_paths
    )
    resource_definition_keys = {
        edge.get("dst")
        for edge in graph["edges"]
        if edge.get("kind") in {"alters", "alters_col"}
    }
    definitionless_resource_keys = {
        node.get("canonical_key")
        for node in graph["nodes"]
        if node.get("kind") in RESOURCE_DEFINITION_EVIDENCE_KINDS
        and node.get("canonical_key") not in resource_definition_keys
    }
    # A reference to a retained first-class node with no definition evidence is
    # unresolved even though its canonical dst token exists.
    unresolved += sum(
        1
        for edge in graph["edges"]
        if edge.get("kind") in {"queries", "queries_col", "reads_config"}
        and edge.get("dst") in definitionless_resource_keys
    )
    ambiguity_tokens = {
        (
            "resource",
            RESOURCE_KIND_TO_SUBSTRATE[node["kind"]],
            node.get("canonical_key"),
        )
        for node in graph["nodes"]
        if node.get("kind") in RESOURCE_NODE_KINDS
        and isinstance(node.get("provenance"), dict)
        and node["provenance"].get("ambiguous") is True
    }
    # One cross-kind canonical coordinate is one logical ambiguity, regardless
    # of how many defining/reading source edges retain its evidence.
    ambiguity_tokens.update(
        ("canonical_collision", key)
        for key in cross_kind_resource_keys
    )
    for edge in graph["edges"]:
        if not (
            edge.get("reference_status") == "ambiguous"
            or edge.get("ambiguous_reference") is True
        ):
            continue
        key = edge.get("ambiguity_key") or edge.get("dst")
        if edge.get("kind") == "imports":
            # One import ambiguity may fan out to several retained candidate
            # edges. Count it once per source/raw reference.
            ambiguity_tokens.add(("import", edge.get("src"), key))
        elif key in cross_kind_resource_keys:
            # Already counted once above. All colliding edges intentionally
            # carry status, but observability reports the logical key
            # collision rather than double-counting each retained endpoint.
            continue
        else:
            # Resource multi-definer edges share the same substrate/key as the
            # retained ambiguous Resource node. Collapse all definers and
            # referencers to that one logical ambiguity.
            ambiguity_tokens.add(("resource", edge.get("substrate"), key))
    ambiguous = len(ambiguity_tokens)
    input_paths = {
        os.path.relpath(path, root).replace(os.sep, "/")
        for path in gitattributes_files
    }
    input_paths.update({
        os.path.relpath(path, root).replace(os.sep, "/")
        for path, _ext in source_files + config_files
    })
    metrics = collect_graph_metrics(
        graph,
        input_paths=input_paths,
        unresolved_references=unresolved,
        ambiguous_references=ambiguous,
    ).as_dict()
    metrics.update({
        "schema_contract_version": SCHEMA_CONTRACT_VERSION,
        "ambiguity_detection_scope": (
            "retained multi-definer resources, emitted canonical-key "
            "collisions, and local import candidate ambiguity"
        ),
    })
    graph["metrics"] = metrics
    return graph


def blast_radius(graph, target_file):
    """Which OTHER files call a symbol defined in target_file? (cheap, deterministic join)

    structure-only: a file F is in the blast radius of T if F has a `calls` edge whose name
    matches a symbol `contains`-ed by T. Returns {file: call_count}, T excluded. Language-agnostic
    (a name match is a name match), so a Python caller and a JS caller of the same name both count.

    Hub-dampening (matches the product pipeline's defs_ok threshold): symbol names defined in
    more than 3 distinct files (e.g. __construct, toString, new, each) are excluded from the
    match so ubiquitous names don't inflate the blast set with unrelated callers. One dict
    pre-pass counts def occurrences per name; the match loop skips names with count > 3.
    """
    _HUB_THRESHOLD = 3  # matches _claim_adjacency defs_ok

    # Pre-pass: count how many distinct files define each symbol name across the whole graph.
    def_file_count: dict = {}
    for e in graph["edges"]:
        if e["kind"] == "contains":
            name = e["dst"].split("::", 1)[1]
            if e["src"] not in def_file_count.get(name, set()):
                def_file_count[name] = def_file_count.get(name, set()) | {e["src"]}

    # Ubiquitous names: defined in more than _HUB_THRESHOLD distinct files — skip in match.
    hub_names = {name for name, files in def_file_count.items() if len(files) > _HUB_THRESHOLD}

    # Symbols defined by the target file, excluding ubiquitous names.
    defined = {e["dst"].split("::", 1)[1] for e in graph["edges"]
               if e["kind"] == "contains" and e["src"] == target_file
               and e["dst"].split("::", 1)[1] not in hub_names}

    hits = {}
    for e in graph["edges"]:
        if e["kind"] == "calls" and e["src"] != target_file and e["dst"] in defined:
            hits[e["src"]] = hits.get(e["src"], 0) + 1
    return dict(sorted(hits.items(), key=lambda kv: kv[1], reverse=True))


def language_coverage(graph):
    """{language: file_count} over the file nodes — the proof the graph is not Python-only."""
    cov = {}
    for n in graph["nodes"]:
        if n["kind"] == "file":
            lang = n.get("language") or "unknown"
            cov[lang] = cov.get(lang, 0) + 1
    return dict(sorted(cov.items(), key=lambda kv: kv[1], reverse=True))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    root = argv[1]
    graph = build_graph(root)
    if "--summary" in argv:
        kinds = {}
        for e in graph["edges"]:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        print(f"root={root}")
        print(f"files: {graph['files_parsed']} parsed, {graph['files_failed']} unparsed, "
              f"{graph.get('files_skipped', 0)} skipped (grammar absent)")
        print(f"languages: {language_coverage(graph)}")
        print(f"nodes: {len(graph['nodes'])}  edges: {len(graph['edges'])}  by kind: {kinds}")
        sample = next((n["path"] for n in graph["nodes"]
                       if n["kind"] == "file" and n["path"].endswith("mcp-server/server.py")), None)
        if sample:
            print(f"\nblast radius of {sample} (files that call its symbols):")
            for f, c in blast_radius(graph, sample).items():
                print(f"  {c:>3}  {f}")
    elif "--push" in argv:
        # Push the extracted graph to Veripsa THROUGH THE GATE (record == execution). The buyer
        # runs this; only the structure (paths/symbols/edges/language) crosses — never file bodies.
        # Identity and tenant are pinned by the connection role (VERIPSA_DSN), never an argument.
        import psycopg2  # local import: only --push needs the driver
        dsn = os.environ.get("VERIPSA_DSN")
        if not dsn:
            print("set VERIPSA_DSN to push (the agent's gate connection)")
            return 2
        # The graph object is already content-free.  Preserve every contracted
        # structural/resource field (hash/signature/provenance/metrics); a
        # handcrafted subset here previously made the CLI persistence path
        # semantically narrower than the service path.
        payload = {
            "extractor_version": graph["extractor_version"],
            "nodes": [dict(n) for n in graph["nodes"]],
            "edges": [dict(e) for e in graph["edges"]],
            "metrics": dict(graph.get("metrics") or {}),
        }
        # GRAPH FRESHNESS: pin the as-of — the HEAD sha the graph was extracted at + the capture time
        # (now). Both are content-free (a hex sha + a timestamp); None sha is fine (not a git checkout).
        import datetime
        head_sha = _head_sha(root)
        captured_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # COORDINATE: WHICH directory this graph is OF — the repo (origin remote, owner/name) and the
        # branch the working tree is ACTUALLY on (never assumed 'main'). Content-free; '' when not a git
        # checkout. Pinning this is what stops the graph from being a flat per-tenant path namespace
        # where a same-named file on another repo/branch was treated as the SAME file.
        repo = _git_repo(root) or ""
        branch = _git_branch(root) or ""
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path=core, pg_catalog")
        cur.execute("SELECT core.ingest_graph_with_authority(%s::jsonb, %s, %s, %s, %s::timestamptz)",
                    (json.dumps(payload), repo, branch, head_sha, captured_at))
        print(cur.fetchone()[0])
    else:
        print(json.dumps(graph, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
