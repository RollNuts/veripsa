#!/usr/bin/env python3
"""Veripsa GitHub App — SHADOW compatibility analysis (compat lane PR-3; flag-gated DORMANT).

This is the candidate-pairing + transient-PR-side-shape + rules wiring of the compatibility program
(docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §3/§4/§7 item 3; docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md milestone 1).
It runs INSIDE the existing per-event webhook flow (webhook.handle_pull_request calls it after the impact
surface read and before render_pr_check, wrapped in the same `_optional(...)` savepoint isolation the
co-change read uses) and produces ZERO customer-visible output:

  * it feeds NOTHING into the check/comment render (no render change anywhere — that is a later, separately
    flagged PR: VERIPSA_COMPAT_RENDER);
  * its only persistence is the PR-2 shadow ledger — core.record_compat_finding_with_authority — for the
    S1 classification rows (`contract_delta:<rule>` / `rebase_needed` / `divergent_definitions`;
    compatible/unknown/unknown-baseline never writes a row) plus, since corrective lane S2, the ONE
    evidence-backed class `consumer_call_mismatch:<detail>` (correction §2.3 — recorded only when the
    counterpart PR's OWN changed files contain a call of the changed/removed contract that is provably
    incompatible with the NEW shape). Since corrective lane S3a every recorded row ALSO carries an EXPLICIT
    queryable classification in the recorder's typed fact_class column (contract_delta_observation /
    rebase_needed_observation / divergent_definition_observation for the telemetry classes;
    evidence_backed_incompatibility ONLY for consumer_call_mismatch rows) and the detector identity+version
    stamp ('python-call-compat/py-call-v1') in the typed detector column — taxonomy and detector vintage are
    column equalities downstream, never a reason-string parse;
  * its only log output is ONE content-free counts line per analyzed event (since compat lane PR-4 the
    line also carries the landing-order solver's aggregate counts — clusters / ordered_clusters /
    cycle_clusters / revision_required — from the pure `_compat_order` module; counts ONLY, never a PR
    number, an order, or a member list. The solver consumes ONLY evidence-backed compatibility findings —
    correction §2.3 — which since lane S2 means exactly the `consumer_call_mismatch` rows: its counts are
    meaningful again for PROVEN consumer breakage and stay zero for delta-only telemetry).

GATE (both read from os.environ AT CALL TIME — operable without deploy, the VERIPSA_CROSS_REPO_KEYS
precedent): `VERIPSA_COMPAT_ANALYSIS` truthy (conservative parse: 1/true/yes/on) AND the event's repo is in
the `VERIPSA_COMPAT_REPOS` CSV allowlist (the VERIPSA_BACKFILL_REPOS parse; EMPTY list = disabled
EVERYWHERE — global ON alone turns nothing on). Either off → `compat_shadow_enabled` returns False and
`run_compat_shadow` returns None BEFORE any work: zero DB statements, zero GitHub calls, zero log lines —
the OFF path stays byte-identical to the pre-PR webhook (proven by the shadow-OFF gate in
tests/test_compat_shadow_analysis.py).

WHAT IT ANALYZES (corrective lanes S1+S2 — docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §2/§3; Python only):
from the ALREADY-COMPUTED impact surface (`impact["changes"]` — no new DB queries; the surface rows are the
candidate substrate), take up to VERIPSA_COMPAT_PAIR_CAP candidate sibling open PRs — THREE deduped
candidacy sources, sibling-source first, all inside the ONE shared pair cap (lane S2, folding in the PR-3b
aim per correction §4):

  1. SHARED SURFACE (PR-3/S1): siblings sharing a changed `.py` path with the acting PR.
  2. MAIN-GRAPH 1-hop adjacency, BOTH directions, read from the surface's per-change `impact` field (the
     `_claim_adjacency` down+shared neighbor set main_impact_surface ALREADY computed — zero extra DB
     statements): a sibling's changed path inside the ACTING blast radius, or an acting changed path inside
     the SIBLING's, pairs them; the candidate contract paths are the side-being-consumed's changed `.py`
     files. Only files that EXIST ON MAIN have graph nodes — which is exactly why source 3 exists.
  3. TRANSIENT import scan for files NOT on main (a NEW consumer file has no graph node): the changed `.py`
     files, fetched within the SAME read budget, are ast-scanned IN MEMORY for their import statements;
     each imported module's dotted name maps to the repo paths it could live at (`pkg/mod.py`,
     `pkg/mod/__init__.py`), intersected with the OTHER side's changed-path set. Content-free (paths only;
     scanned bytes dropped), budget-honest (scan fetches count against VERIPSA_COMPAT_FILE_READS_CAP; a
     cap-refused scan leaves the pair un-considered), and bounded (the scan stops once the pair cap is
     reached). The log line keeps the legacy `pairs_via_graph=<n>` aggregate, but also separates
     `pairs_via_main_graph` from `pairs_via_import_scan`.  A refused/unreadable/unparseable scan is
     `candidacy_complete=false` (with a bounded unknown count), never silently treated as "no edge".

Per pair, resolve EACH PR's own BASELINE — its merge-base with the shared target branch,
via `gh.merge_base_sha` (compare API `merge_base_commit`; ≤ 1+pair_cap compare calls per event, cached per
head) — then fetch each shared path at up to FOUR shape points with the existing `gh.get_file_at`: both
verified heads AND both baselines (baseline reads count against the same TOTAL per-event cap,
VERIPSA_COMPAT_FILE_READS_CAP; a per-(sha,path) cache means a shared file version is fetched once). Parse in
memory with `ast`, extract each def's content-free signature shape with the landed
`_cg_python._py_signature_shape` (PR-1), and classify each contract key (path + qualified def name) over the
UNION of all four shape maps (so a removal is visible from EITHER PR's event) by the 4-WAY baseline model:

  * changed by ONE side only (vs its OWN baseline), baselines comparable → the CHANGER is the producer
    (role derived from the DELTA, never from event order); `_compat_rules.compare_shapes(baseline,
    changer_head)` classifies the delta; each firing rule records reason `contract_delta:<rule>` —
    NOT `breaking:*`: a def-shape delta alone is NOT proven consumer breakage (correction §2.4; the
    evidence-gated `consumer_call_mismatch` class is lane S2);
  * changed by BOTH sides, heads differ → `divergent_definitions` (cluster-coordination signal, no
    breaking claim, canonical sorted-head roles);
  * changed by NEITHER side but heads differ (a landed change + a stale sibling) → `rebase_needed`
    (advisory staleness, NEVER a breaking/contract_delta claim against the stale side);
  * the two baselines hold DIFFERENT shapes for the contract (non-comparable) → UNKNOWN for that
    contract — counted (`unknown_baseline`), never a finding.

CONSUMER EVIDENCE (lane S2 — correction §2.2/§2.3): for every ONE-SIDED contract delta the counterpart
(non-changer) PR's OWN changed `.py` files at ITS verified head — fetched through the same budget/cache;
in practice already fetched by candidacy or the pair analysis — are scanned for Consumer Expectations:
each `ast.Call` whose callee identity resolves through the file's own import map, recorded as NOTHING but
names + counts + flags (positional arg count, keyword names, star-args/star-kwargs presence). An
unresolvable or dynamic callee (`getattr(...)`, an attribute of a call result, `import *`) yields NO
expectation — never a guess, never an unknown explosion. The evidence-gated finding class
`consumer_call_mismatch:<detail>` is recorded ONLY when (a) one PR's baseline-anchored delta changed or
removed contract K AND (b) the OTHER PR's expectation set contains a call resolved to K that is PROVABLY
incompatible with the NEW shape while satisfied by the shared baseline (details: positional_shortfall /
missing_required_kwonly / callee_removed). Roles come from the deltas (changer = producer, caller =
consumer — identical from either PR's webhook event); this is the ONLY class the PR-4 solver consumes.

Fingerprints are CANONICAL + SYMMETRIC: sha1 over the UNORDERED head pair (two SHAs sorted) + contract key +
rule/classification code + the detector version tag (py-call-v1) — the same physical divergence analyzed
from either PR's webhook event produces the byte-identical ledger row, and the PR-2 recorder's deterministic
id (account|repo|producer|consumer|fingerprint) collapses both event orders onto ONE row.

BUDGET + HONESTY DISCIPLINE (the truncated_files precedent): cap exhaustion, a missing/unverified head, a
fetch failure, an over-size file, or a parser failure degrade that pair to UNKNOWN — counted, never recorded,
and NEVER claimed 'compatible'.  This applies to candidacy itself: graph/import provenance is counted
separately and a fallback scan that cannot prove presence OR absence reports `candidacy_complete=false`.
Beyond-cap pairs are not analyzed at all (counted in pairs_considered when already discovered; undiscovered
fallback siblings make `candidacy_capped=true`; either way `capped=true`, no claim either way).

CONTENT-FREE, non-negotiable (plan §2): fetched file bodies are TRANSIENT — parsed in memory and dropped;
never persisted, never logged, never placed in an exception message (a contained error logs only the
exception CLASS name, because a parser error's message can carry source text). What persists is: hex SHAs, a
sha1 fingerprint over structural identifiers, and a bounded safe-charset reason code — exactly what the PR-2
recorder's content-free wall admits. The telemetry line is counts only.

NEVER-CRASH: the module contains every non-DB failure itself, and each ledger write runs inside its OWN
best-effort savepoint (the webhook `_optional` idiom at statement granularity) so a refused/failed record can
never leave the shared per-event transaction aborted. The caller's `_optional(...)` wrap is the backstop.
"""
from __future__ import annotations

import hashlib
import os
import re

try:
    from _compat_rules import compare_shapes, BREAKING, RISKY, COMPATIBLE, UNKNOWN
    from _compat_order import solve_landing_order, CYCLE_CLUSTER
    from env_config import ConfigError, env_int
except ImportError:  # imported as a package
    from ._compat_rules import compare_shapes, BREAKING, RISKY, COMPATIBLE, UNKNOWN
    from ._compat_order import solve_landing_order, CYCLE_CLUSTER
    from .env_config import ConfigError, env_int

# ── Flags + caps (all read at CALL time; defaults are the shipped, documented values) ────────────────────────
_FLAG_ANALYSIS = "VERIPSA_COMPAT_ANALYSIS"      # global kill switch, default OFF (conservative truthy parse)
_FLAG_REPOS = "VERIPSA_COMPAT_REPOS"            # CSV allowlist; EMPTY ⇒ disabled everywhere (dogfood-only by list)
_PAIR_CAP_KEY = "VERIPSA_COMPAT_PAIR_CAP"       # sibling PRs analyzed per event (default 3)
_READS_CAP_KEY = "VERIPSA_COMPAT_FILE_READS_CAP"  # TOTAL GitHub file reads per event across all pairs (default 6)
_PAIR_CAP_DEFAULT = 3
_READS_CAP_DEFAULT = 6
# A file larger than this is not parsed (conservative UNKNOWN): the shadow lane must never hold the single
# serialized drain worker on a multi-MB ast.parse. Ordinary source files are far below this.
_MAX_FILE_BYTES = 2_000_000
# The PR-2 recorder's content-free wall for `detail` (db/schema/30_gate.sql): a bounded reason CODE in a safe
# charset — nothing a code body is made of. Checked here too so a buggy rule id degrades to a skipped record
# (counted unknown) instead of a raised refusal aborting the record savepoint.
_REASON_RE = re.compile(r"^[A-Za-z0-9_.:,+/-]{1,200}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
# Keep in lockstep with webhook._TRACE_TAG_WIDTH (the compact per-event trace prefix every hot-path line carries).
_TRACE_TAG_WIDTH = 12

# Detector identity + version (corrective lane S3a). The VERSION token is part of every fingerprint basis,
# so a semantics change NEVER collides with rows an older detector wrote — py-call-v1 replaced py-shape-v2
# when the S1/S2 corrected model (baseline-anchored classification + call-evidence gating) became the
# writer's semantics. Rows stamped py-shape-v2 remain in the ledger as APPEND-ONLY LEGACY EVIDENCE and MUST
# NOT be treated as current detector output (the current-surface exclusion itself is lane S3b; the queryable
# stamp that enables it lands here). The full '<name>/<version>' stamp is persisted on every row via the
# recorder's typed `detector` column — equality-queryable, never parsed out of a reason string.
_DETECTOR_NAME = "python-call-compat"
_DETECTOR_VERSION = "py-call-v1"
_DETECTOR_STAMP = _DETECTOR_NAME + "/" + _DETECTOR_VERSION

# Reason vocabulary v2 (docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §2.4 / lanes S1+S2). All fit the
# recorder's safe-charset detail wall. The old `breaking:*` / `risky:*` def-diff reasons are RETIRED from
# this writer: a def-shape delta alone is not proven consumer breakage — the S1 classes below are
# telemetry; the ONE evidence-gated finding class is `consumer_call_mismatch:<detail>` (lane S2).
_REASON_DELTA_PREFIX = "contract_delta:"   # one-sided shape change: `contract_delta:<rule_id>`
_REASON_REBASE = "rebase_needed"           # neither side changed vs its own baseline, heads differ (staleness)
_REASON_DIVERGENT = "divergent_definitions"  # both sides changed vs their baselines (coordination signal)
_MISMATCH_RULE = "consumer_call_mismatch"  # lane S2: the evidence-backed finding class (correction §2.3)
_REASON_MISMATCH_PREFIX = _MISMATCH_RULE + ":"  # `consumer_call_mismatch:<detail-code>` — bounded details:
# positional_shortfall / missing_required_kwonly / callee_removed (all inside the recorder's safe charset).

# EXPLICIT classification codes (corrective lane S3a — correction §2.4/§3 lane 3): every recorded row now
# carries exactly ONE of these in the recorder's typed `fact_class` column, passed EXPLICITLY by this writer
# (never derived downstream by parsing the reason string). The three *_observation classes are telemetry —
# a def-shape delta / staleness / divergence is NEVER a proven-breakage claim; the ONE class that claims
# proven consumer breakage is evidence_backed_incompatibility, stamped ONLY on consumer_call_mismatch rows.
_CLASS_CONTRACT_DELTA = "contract_delta_observation"
_CLASS_REBASE = "rebase_needed_observation"
_CLASS_DIVERGENT = "divergent_definition_observation"
_CLASS_INCOMPAT = "evidence_backed_incompatibility"

# The classification codes whose recorded findings may feed the PR-4 landing-order solver. Correction §2.3:
# solver edges consume ONLY evidence-backed compatibility findings. Since lane S2 exactly ONE class
# qualifies — `consumer_call_mismatch`, recorded only when the counterpart PR's own changed files contain a
# call of the changed/removed contract that is provably incompatible with the NEW shape. The S1
# delta/staleness/divergence classifications remain telemetry (a def delta alone is not consumer breakage)
# and never reach the solver.
_EVIDENCE_BACKED_CLASSES = frozenset({_MISMATCH_RULE})

# Sentinel for "no usable bytes at this (sha, path)": read-cap exhausted, fetch failed, or over the size
# bound. Distinct from b"" (an empty file IS parseable — zero defs).
_UNAVAILABLE = object()
# Sentinel for "the file does NOT exist at this sha" (the contents API's 404 → None). A REAL state, not a
# failure: it maps to an EMPTY shape map, which is what makes a whole-file add/delete classifiable as
# per-def deltas instead of degrading to unknown.
_ABSENT = object()
# Sentinel for "no def with this qualified name at this shape point" (absent from the map — never confused
# with None, which _def_shapes stores for a PRESENT but shape-unreadable def).
_NO_DEF = object()


def _truthy(v: "str | None") -> bool:
    """Conservative truthy parse for an env flag: 1/true/yes/on (case-insensitive). Anything else —
    including unset, '0', '', 'false' — is OFF. Never-crash (pure string compare; the _cg_xrepo parse)."""
    if not v:
        return False
    return v.strip().lower() in ("1", "true", "yes", "on")


def compat_shadow_enabled(repo) -> bool:
    """True iff shadow compat analysis is ON for `repo`: VERIPSA_COMPAT_ANALYSIS truthy AND repo in the
    VERIPSA_COMPAT_REPOS CSV allowlist. Read LIVE from the environment on every call (no import-time caching —
    a flag flip or a test child process takes effect immediately). An EMPTY allowlist disables everywhere:
    the global flag alone can never turn analysis on for a customer repo. Pure env read — zero DB, zero I/O,
    zero logs — so the OFF path costs one string compare and stays byte-identical to pre-PR behavior."""
    if not _truthy(os.environ.get(_FLAG_ANALYSIS)):
        return False
    allow = [r.strip() for r in (os.environ.get(_FLAG_REPOS) or "").split(",") if r.strip()]
    return bool(allow) and isinstance(repo, str) and repo in allow


def _cap(key: str, default: int, max_value: int) -> int:
    """env_int with a NEVER-CRASH wrapper: env_config.ConfigError is a SystemExit subclass (fail-loud at
    process start is right for the always-on knobs), but THIS knob gates an OPTIONAL shadow surface read at
    EVENT time — a misconfigured value must degrade to the shipped default, never kill the drain worker
    (SystemExit would sail through the caller's `except Exception` backstop)."""
    try:
        return env_int(key, default, min_value=1, max_value=max_value)
    except ConfigError:
        return default


# ── Savepoint-guarded ledger write (the _optional idiom at single-statement granularity) ─────────────────────
def _txn_ok(db, sql) -> bool:
    """Run a transaction-control statement through the SELECT-shaped `db` runner (webhook._txn_cmd copy —
    local so this module never imports webhook, which imports this module). The runner's fetchone() raises
    'no results to fetch' on a no-row statement even though it APPLIED — treat that as success. A genuine
    execute failure (autocommit per-call test runner, no open txn) → False (best-effort). NEVER raises."""
    try:
        db(sql)
        return True
    except Exception as e:
        if any(m in str(e).lower() for m in ("no results to fetch", "no result")):
            return True
        return False


def _guarded_record(db, args) -> bool:
    """ONE shadow-finding write inside its OWN savepoint. On the LIVE shared per-event transaction a refused
    record (e.g. the recorder's content-free wall, a grant gap) would otherwise leave the txn ABORTED —
    swallowing that exception here WITHOUT the rollback would silently kill every later statement of the
    customer event (the exact prod-bug class _optional exists for). Savepoint set/release are best-effort
    (a per-call autocommit test runner has no shared txn); the write's outcome is decided only by itself.
    Since lane S3a `args` is 9-wide: (..., reason, fact_class, detector) — the classification is passed
    EXPLICITLY to the recorder's typed columns, and the detector stamp rides on every row."""
    have = _txn_ok(db, "SAVEPOINT vp_compat_rec")
    try:
        db("SELECT core.record_compat_finding_with_authority(%s,%s,%s,%s,%s,%s,%s,%s,%s)", args)
    except Exception:
        if have:
            _txn_ok(db, "ROLLBACK TO SAVEPOINT vp_compat_rec")
        return False
    if have:
        _txn_ok(db, "RELEASE SAVEPOINT vp_compat_rec")
    return True


# ── Transient in-memory shape extraction (bodies are parsed and DROPPED — never persisted/logged) ────────────
def _sig_shape_fn():
    """The landed PR-1 extractor `_cg_python._py_signature_shape` (repo root — importable exactly like the
    `import code_graph_extract` calls webhook.py already makes). Resolved lazily + defensively: absent →
    None → every shape is unparseable → every pair degrades to UNKNOWN (never a guess, never a crash)."""
    try:
        import _cg_python as m
    except ImportError:
        return None
    fn = getattr(m, "_py_signature_shape", None)
    return fn if callable(fn) else None


def _def_shapes(source_bytes):
    """{qualified def name: shape dict | None} for ONE Python source body, or None when the file itself is
    unparseable (syntax error / decode error / pathological nesting). The qualified name is the ClassDef/
    FunctionDef nesting path joined with '.', so `Order.create` and a top-level `create` never collide.
    A duplicate qualified name (conditional re-def) keeps the LAST definition — Python runtime semantics.
    Per-def extraction failure stores None for that name (compared as UNKNOWN, never guessed). The source
    bytes are read ONLY by ast here and dropped by the caller — nothing content-bearing leaves this function
    (shapes are names + counts + flags + a hash, the PR-1 contract)."""
    import ast
    shape_of = _sig_shape_fn()
    if shape_of is None:
        return None
    if not isinstance(source_bytes, (bytes, bytearray)):
        return None
    try:
        tree = ast.parse(bytes(source_bytes))
    except Exception:
        return None                      # unparseable file → the pair degrades to unknown (never compatible)
    shapes: dict = {}

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = ".".join(stack + [child.name])
                try:
                    s = dict(shape_of(child) or {})
                except Exception:
                    s = {}
                if s and isinstance(s.get("param_names"), list):
                    s["symbol"] = child.name     # identifier only; _compat_rules._ident guards reason strings
                    shapes[qual] = s
                else:
                    shapes[qual] = None          # present but shape-unreadable → unknown at compare time
                walk(child, stack + [child.name])
            elif isinstance(child, ast.ClassDef):
                walk(child, stack + [child.name])
            else:
                walk(child, stack)               # descend into if/try/with bodies at the same qualification

    try:
        walk(tree, [])
    except Exception:                            # e.g. RecursionError on pathological nesting
        return None
    return shapes


def _module_paths(dotted):
    """The repo-relative FILE paths a dotted module name can live at: `pkg/mod.py` and
    `pkg/mod/__init__.py`. Pure; an empty/None name → empty set."""
    parts = [p for p in (dotted or "").split(".") if p]
    if not parts:
        return set()
    stem = "/".join(parts)
    return {stem + ".py", stem + "/__init__.py"}


def _py_import_paths(source_bytes, importer_path):
    """The repo-relative FILE paths one Python source could be importing — the candidacy-source-3 scan
    (harvested from compat lane PR-3b per correction §4). Parses IN MEMORY with `ast` (the bytes are the
    caller's transient fetch — dropped right after), collects every `import x.y` / `from x.y import z`
    module reference, resolves RELATIVE imports against the importer's own package directory (the
    _cg_python discipline: `.pricing` inside orders/receipt.py names orders/pricing — never every
    pricing.py in the repo), and maps each dotted name to the two places that module can live. For
    `from pkg import name`, `name` is often a submodule FILE, so both `pkg` and `pkg.name` are candidates
    (the extractor's own rule). Returns a SET of candidate paths — the caller intersects it with a KNOWN
    changed-path set, so an over-generated candidate can never invent a file, only miss one (recall-safe,
    precision-guarded). Content-free: paths out, nothing else retained. NEVER raises.  Returns None when
    bytes are unavailable/unparseable so candidacy can report UNKNOWN; an empty set means a successful parse
    proved that this file contributes no import candidate.  Failure and absence are deliberately distinct."""
    import ast
    import posixpath
    if not isinstance(source_bytes, (bytes, bytearray)):
        return None
    try:
        tree = ast.parse(bytes(source_bytes))
    except Exception:
        return None
    pkg_dir = posixpath.dirname(importer_path) if isinstance(importer_path, str) else ""

    def _absolute(level, dotted):
        # level 1 = the importer's own package dir; each extra level climbs one package (PEP 328).
        base = pkg_dir
        for _ in range(max(int(level) - 1, 0)):
            base = posixpath.dirname(base)
        parts = [p for p in base.split("/") if p] + [p for p in (dotted or "").split(".") if p]
        return ".".join(parts)

    mods = set()
    try:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if isinstance(a.name, str) and a.name:
                        mods.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                base, level = node.module, (node.level or 0)
                if level:
                    if base:
                        mods.add(_absolute(level, base))
                    for a in node.names:                     # `from . import pricing`: the name IS the module
                        if isinstance(a.name, str) and a.name:
                            mods.add(_absolute(level, ((base + ".") if base else "") + a.name))
                elif base:
                    mods.add(base)
                    for a in node.names:                     # `from pkg import name`: name may be a submodule
                        if isinstance(a.name, str) and a.name:
                            mods.add(base + "." + a.name)
    except Exception:
        return None
    paths = set()
    for m in mods:
        paths |= _module_paths(m)
    return paths


def _side_import_targets(gh, repo, paths, sha, cache, budget):
    """Budgeted import-candidacy scan for one PR side.

    Returns ``(targets, unknown, capped)``.  ``targets`` contains structural repo paths only.  ``unknown``
    means at least one requested file could not be fetched, bounded, or parsed; ``capped`` means the shared
    GitHub-file-read budget specifically prevented at least one read.  An authoritative absent file is a
    complete empty contribution (a removed changed file has no imports at its head).  Bodies remain transient.
    """
    targets = set()
    unknown = False
    capped = False
    for path in paths:
        data = _fetch(gh, repo, path, sha, cache, budget)
        if data is _ABSENT:
            continue
        if data is _UNAVAILABLE:
            if budget.exhausted:
                capped = True
            else:
                unknown = True
            continue
        parsed = _py_import_paths(data, path) if isinstance(data, (bytes, bytearray)) else None
        del data
        if parsed is None:
            unknown = True
        else:
            targets.update(parsed)
    return targets, unknown, capped


def _py_call_expectations(source_bytes, caller_path):
    """Consumer Expectation extraction (correction §2.2) over ONE Python source body — TRANSIENT and
    content-free. Returns a list of expectation dicts, one per `ast.Call` whose callee identity resolves
    through the file's OWN import map:
      {"targets": ((frozenset(candidate repo paths), qualified def name), ...),
       "pos": <positional arg count>, "kw": frozenset(<keyword arg names>),
       "star_args": bool, "star_kwargs": bool}
    NOTHING but names + counts + flags is recorded — no argument value, no expression, no source byte —
    and the entries never leave the event (what persists downstream is a bounded reason code).

    RESOLUTION (never a guess):
      * `from m import f [as g]` + `f(...)`/`g(...)`        → m :: f
      * `import a.b[.c] [as x]` + `a.b.f(...)`/`x.f(...)`   → longest bound module prefix; a remaining
        attribute chain becomes the qualified name (`import m` + `m.C.f(...)` → m :: C.f)
      * `from m import N` + `N.f(...)`                      → BOTH readings recorded — m.N :: f (N a
        submodule) and m :: N.f (N a class); the caller intersects targets with the producer's REAL delta
        key, so a wrong reading can only fail to match, never invent a contract
      * relative imports resolve against the importer's own package dir (the _py_import_paths discipline)
    Anything else — a local def, an attribute of a call result, a subscript, `getattr(...)(...)`,
    `import *` — is DYNAMIC/UNRESOLVABLE: no expectation, no unknown (§2.2 'never a guess').
    NEVER raises; unparseable source → empty list."""
    import ast
    import posixpath
    if not isinstance(source_bytes, (bytes, bytearray)):
        return []
    try:
        tree = ast.parse(bytes(source_bytes))
    except Exception:
        return []
    pkg_dir = posixpath.dirname(caller_path) if isinstance(caller_path, str) else ""

    def _absolute(level, dotted):
        base = pkg_dir
        for _ in range(max(int(level) - 1, 0)):
            base = posixpath.dirname(base)
        parts = [p for p in base.split("/") if p] + [p for p in (dotted or "").split(".") if p]
        return ".".join(parts)

    mod_bind: dict = {}    # local dotted name -> absolute module dotted name (`import m` / `import m.n as x`)
    from_bind: dict = {}   # local bare name  -> (absolute module, attribute name)  (`from m import f as g`)
    try:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if not (isinstance(a.name, str) and a.name):
                        continue
                    if a.asname:
                        mod_bind[a.asname] = a.name
                    else:
                        # `import a.b.c` binds `a`; attribute access reaches a, a.b and a.b.c — every
                        # prefix is a usable module base for a dotted call.
                        parts = a.name.split(".")
                        for i in range(1, len(parts) + 1):
                            mod_bind[".".join(parts[:i])] = ".".join(parts[:i])
            elif isinstance(node, ast.ImportFrom):
                base, level = node.module, (node.level or 0)
                mod = _absolute(level, base) if level else (base or "")
                if not mod:
                    continue
                for a in node.names:
                    nm = a.name
                    if not (isinstance(nm, str) and nm.isidentifier()):
                        continue                             # `import *` and non-identifier names: no binding
                    from_bind[a.asname or nm] = (mod, nm)
    except Exception:
        return []

    def _resolve(func):
        """The (module dotted name, qualified def name) readings for ONE callee node — [] = no expectation."""
        if isinstance(func, ast.Name):
            b = from_bind.get(func.id)
            return [(b[0], b[1])] if b else []
        if not isinstance(func, ast.Attribute):
            return []                                        # call of a call/subscript/lambda: dynamic
        chain = []
        cur = func
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            return []                                        # attribute of a non-name base: dynamic
        chain.append(cur.id)
        chain.reverse()                                      # [base name, ..., final attr]
        base_parts, fn = chain[:-1], chain[-1]
        for i in range(len(base_parts), 0, -1):              # longest bound module prefix wins
            dotted = ".".join(base_parts[:i])
            if dotted in mod_bind:
                return [(mod_bind[dotted], ".".join(base_parts[i:] + [fn]))]
        if len(base_parts) == 1 and base_parts[0] in from_bind:
            mod, attr = from_bind[base_parts[0]]
            return [(mod + "." + attr, fn), (mod, attr + "." + fn)]
        return []

    out = []
    try:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            readings = _resolve(node.func)
            if not readings:
                continue
            pos = 0
            star_args = False
            for a in node.args:
                if isinstance(a, ast.Starred):
                    star_args = True
                else:
                    pos += 1
            kw = set()
            star_kwargs = False
            for k in (node.keywords or []):
                if k.arg is None:
                    star_kwargs = True
                elif isinstance(k.arg, str):
                    kw.add(k.arg)
            out.append({"targets": tuple((frozenset(_module_paths(m)), q) for m, q in readings),
                        "pos": pos, "kw": frozenset(kw),
                        "star_args": star_args, "star_kwargs": star_kwargs})
    except Exception:
        return []                                            # conservative: partial evidence is dropped whole
    return out


def _shape_ok(shape) -> bool:
    """True iff `shape` carries the structural core the mismatch predicate needs (the _compat_rules
    validation, local so this module never reaches into the rule layer's privates)."""
    if not isinstance(shape, dict):
        return False
    names = shape.get("param_names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return False
    for k in ("required_arity", "optional_arity"):
        v = shape.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return False
    return True


def _expectation_mismatch(exp, baseline, new_shape):
    """The bounded detail code when ONE Consumer Expectation is PROVABLY incompatible with the producer's
    NEW shape while satisfied by the shared BASELINE shape — i.e. the producer's change is what breaks
    this call, never a pre-existing consumer bug (correction §2.3). None = no claim.
      callee_removed          — the contract was removed outright; a resolved call of it breaks in any form
      positional_shortfall    — the call covers fewer positional params (positionally or by keyword) than
                                the NEW shape provably requires (call has no *args/**kwargs)
      missing_required_kwonly — a NEWLY required keyword-only name the call does not pass (no **kwargs)
    Conservatism is structural: required-parameter sets are derived only where PROVABLE from the
    content-free shape (defaults fill the positional tail; ALL kwonly names are known-required only when
    optional_arity == 0; the required-positional count is bounded below by required_arity - len(kwonly)
    and above by min(required_arity, positional count)) — the FIRING check uses the lower bound, the
    baseline-satisfied check uses the upper bound, so uncertainty always means FEWER findings. A
    *args/**kwargs call suppresses both shape rules (the argument picture is open) and a malformed shape
    returns None (never a guess). Pure; never raises."""
    try:
        if new_shape is None:
            return "callee_removed"                          # removal breaks the resolved call regardless
        if exp.get("star_args") or exp.get("star_kwargs"):
            return None                                      # open argument picture: nothing provable
        if not (_shape_ok(baseline) and _shape_ok(new_shape)):
            return None
        pos = exp.get("pos")
        kw = exp.get("kw")
        if not isinstance(pos, int) or isinstance(pos, bool) or pos < 0 or not isinstance(kw, frozenset):
            return None

        def unfilled_required_pos(shape, lower_bound):
            names = [n for n in shape.get("param_names") if isinstance(n, str)]
            kwonly = [n for n in (shape.get("kwonly_names") or []) if isinstance(n, str)]
            pos_names = names[: max(len(names) - len(kwonly), 0)]
            ra = int(shape.get("required_arity") or 0)
            req = max(ra - len(kwonly), 0) if lower_bound else min(ra, len(pos_names))
            return [n for i, n in enumerate(pos_names[:req]) if i >= pos and n not in kw]

        # (1) positional shortfall: provably unfilled under the NEW shape (lower bound of its required
        #     positional set) while the call DEFINITELY satisfied the baseline (upper-bound check).
        if unfilled_required_pos(new_shape, True) and not unfilled_required_pos(baseline, False):
            return "positional_shortfall"
        # (2) newly required keyword-only name: provable only when the NEW shape has no optional param at
        #     all (then every kwonly name is required); a name already possibly-required at the baseline
        #     is a pre-existing consumer concern, not this producer's break.
        if int(new_shape.get("optional_arity") or 0) == 0:
            base_kwonly = {n for n in (baseline.get("kwonly_names") or []) if isinstance(n, str)}
            base_all_optional = int(baseline.get("required_arity") or 0) == 0
            missing = [n for n in (new_shape.get("kwonly_names") or [])
                       if isinstance(n, str) and n not in kw
                       and (n not in base_kwonly or base_all_optional)]
            if missing:
                return "missing_required_kwonly"
    except Exception:
        return None
    return None


def _side_expectations(gh, repo, py_paths, head_sha, cache, budget):
    """Consumer Expectations (§2.2) for ONE PR side: extraction over its changed `.py` files at its
    verified head. Fetches share the per-(sha,path) cache and the SAME per-event read budget — in practice
    the files were already fetched by candidacy or the pair analysis, so this is usually all cache hits. A
    cap-refused / failed / absent / oversized file contributes NO expectations (missing evidence ⇒ FEWER
    findings, the conservative direction — never a guess, never an unknown). Bodies stay transient:
    scanned and dropped; only names + counts + flags leave."""
    out: list = []
    for p in (py_paths or []):
        data = _fetch(gh, repo, p, head_sha, cache, budget)
        if isinstance(data, (bytes, bytearray)):
            out.extend(_py_call_expectations(data, p))
        del data
    return out


def _finding_fingerprint(head_sha_a, head_sha_b, path, symbol, rule_id) -> str:
    """The CANONICAL SYMMETRIC content-free finding id (detector py-call-v1): sha1 hex (40 chars, within the
    recorder's ≤128 reference-token wall) over the UNORDERED verified head pair (the two SHAs SORTED — the
    caller passes them in any order), the contract key (path + qualified symbol), the rule/classification
    code, and the detector version tag. The same physical divergence analyzed from EITHER PR's webhook event
    therefore reproduces the byte-identical fingerprint — and, with roles derived from the delta (not the
    event order), the PR-2 recorder's deterministic id (account|repo|producer|consumer|fingerprint)
    collapses both event orders onto ONE ledger row (idempotent dedupe)."""
    lo, hi = sorted((head_sha_a or "", head_sha_b or ""))
    basis = "|".join((lo, hi, path or "", symbol or "", rule_id or "", _DETECTOR_VERSION))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


# ── Budgeted, cached file fetch (TOTAL GitHub reads per event ≤ reads cap) ───────────────────────────────────
class _ReadBudget:
    def __init__(self, cap: int):
        self.cap = cap
        self.used = 0            # ACTUAL gh.get_file_at invocations (cache hits are free)
        self.exhausted = False


def _fetch(gh, repo, path, sha, cache, budget):
    """Bytes of `path` @ `sha` via the existing gh.get_file_at, or _ABSENT (the contents API's authoritative
    404 → None: the file does not exist at this pinned sha), or _UNAVAILABLE (cap exhausted / fetch error /
    over the size bound). Cached per (sha, path) so a shared file version — an acting head across every
    sibling pair, a common baseline across both sides — costs ONE read. A fetch that errors still consumed a
    GitHub call → counted. Never raises."""
    key = (sha, path)
    if key in cache:
        return cache[key]
    if gh is None or budget.used >= budget.cap:
        budget.exhausted = budget.exhausted or (gh is not None)
        return _UNAVAILABLE                      # NOT cached: the cap verdict is per-call, not per-file truth
    budget.used += 1
    try:
        data = gh.get_file_at(repo, path, sha)
        absent = data is None                    # 404 at a pinned sha = the file genuinely is not there
    except Exception:
        data, absent = None, False               # a read FAILURE is never evidence of absence
    if absent:
        data = _ABSENT
    elif not isinstance(data, (bytes, bytearray)) or len(data) > _MAX_FILE_BYTES:
        data = _UNAVAILABLE
    cache[key] = data
    return data


def _merge_base(gh, repo, branch, head_sha, mb_cache):
    """The merge-base SHA of (`branch`, `head_sha`) via gh.merge_base_sha — the S1 BASELINE POINT for one
    PR. Cached per head sha, so an event costs at most 1 + pairs_analyzed compare calls (bounded by the pair
    cap; these are commit-id lookups, not file reads, so they do not consume the FILE reads budget — that
    budget governs the content fetches the baselines then add). Defensive: a client without the method, any
    failure, or a non-hex result → None → the caller degrades the pair to UNKNOWN (never a guess)."""
    if head_sha in mb_cache:
        return mb_cache[head_sha]
    fn = getattr(gh, "merge_base_sha", None)
    sha = None
    if callable(fn):
        try:
            s = fn(repo, branch, head_sha)
            if isinstance(s, str) and _SHA_RE.match(s):
                sha = s
        except Exception:
            sha = None
    mb_cache[head_sha] = sha
    return sha


def _shapes_at(gh, repo, path, sha, cache, shape_cache, budget):
    """The {qualified def name: shape} map of `path` @ `sha`, or None when it CANNOT BE KNOWN (cap
    exhausted / fetch failure / oversized / unparseable — honest-unknown, never guessed). A file ABSENT at
    the sha is a REAL state → an EMPTY map. Parsed maps are cached per (sha, path) so a shared version is
    parsed once per event, not once per pair; the fetched bytes stay TRANSIENT (parsed and dropped here)."""
    key = (sha, path)
    if key in shape_cache:
        return shape_cache[key]
    data = _fetch(gh, repo, path, sha, cache, budget)
    if data is _UNAVAILABLE:
        return None                              # NOT cached: a cap-blocked read is per-call, not file truth
    if data is _ABSENT:
        shapes: "dict | None" = {}
    else:
        shapes = _def_shapes(data)               # None on an unparseable file (cacheable content truth)
        del data
    shape_cache[key] = shapes
    return shapes


def _shape_point_key(point):
    """The comparable identity of ONE shape point: ('absent',) when no such def exists there, ('shape', fp)
    for a readable shape, and None for a PRESENT-but-unreadable shape (the _def_shapes None marker or a
    fingerprint-less dict) — compared as unknown by the caller, never guessed."""
    if point is _NO_DEF:
        return ("absent",)
    if isinstance(point, dict):
        fp = point.get("shape_fingerprint")
        if isinstance(fp, str) and fp:
            return ("shape", fp)
    return None


# ── Entry point ──────────────────────────────────────────────────────────────────────────────────────────────
def run_compat_shadow(db, gh, *, repo, branch, change_id, head_sha,
                      changed_paths, impact, trace_id="", pr=""):
    """Shadow-analyze the acting PR against its candidate siblings (S1 baseline-anchored semantics + S2
    consumer evidence). Returns None when the flags say OFF for this repo (BEFORE any work — zero
    DB/GitHub/log activity), else a content-free counts dict:
      {pairs_considered, pairs_analyzed, pairs_via_graph, pairs_via_main_graph,
       pairs_via_import_scan, candidacy_complete, candidacy_unknown, candidacy_capped,
       reads_used, findings, unknowns, capped,
       contract_deltas, rebase_needed, divergent, unknown_baseline,
       expectations_extracted, consumer_mismatches,
       clusters, ordered_clusters, cycle_clusters, revision_required}
    which is also emitted as the ONE per-event telemetry line. `findings` counts ALL recorded rows
    (= contract_deltas + rebase_needed + divergent + consumer_mismatches); `unknown_baseline` counts
    contracts whose two baselines were non-comparable (correction §2.1 — counted, never recorded);
    `pairs_via_graph` is the backward-compatible graph/import aggregate; `pairs_via_main_graph` and
    `pairs_via_import_scan` are the exact provenance counters. `candidacy_complete=false` means a non-shared
    sibling could not be conclusively scanned because of a head/read/parser/cap boundary; an empty candidate
    set is therefore never misreported as complete. `expectations_extracted` / `consumer_mismatches` are the
    S2 evidence telemetry (counts only). The last four are the PR-4
    landing-order solver's shadow telemetry (`_compat_order.solve_landing_order`, COUNTS ONLY) — fed ONLY
    evidence-backed findings per correction §2.3, i.e. exactly the `consumer_call_mismatch` rows.
    NEVER raises (the caller's _optional wrap is the backstop; every failure class inside is contained and
    degrades to UNKNOWN, never 'compatible')."""
    if not compat_shadow_enabled(repo):
        return None
    try:
        return _run(db, gh, repo=repo, branch=branch, change_id=change_id, head_sha=head_sha,
                    changed_paths=changed_paths, impact=impact, trace_id=trace_id, pr=pr)
    except Exception as e:
        # Contained, content-free: the exception CLASS only — a parser/fetch error MESSAGE can carry source
        # text, so it must never reach a log line. All DB statements above run inside their own savepoints,
        # so an exception reaching here is pure-Python and swallowing it cannot poison the shared txn.
        tp = f"trace_id={str(trace_id)[:_TRACE_TAG_WIDTH]} " if trace_id else ""
        print(f"{tp}compat shadow contained repo={repo} pr={pr} error={type(e).__name__}", flush=True)
        return None


def _run(db, gh, *, repo, branch, change_id, head_sha, changed_paths, impact, trace_id="", pr=""):
    pair_cap = _cap(_PAIR_CAP_KEY, _PAIR_CAP_DEFAULT, 100)
    reads_cap = _cap(_READS_CAP_KEY, _READS_CAP_DEFAULT, 1000)

    # Milestone-1 scope: the acting PR's changed PYTHON files (the shapes PR-1 extracts). The candidate
    # substrate is the surface the webhook ALREADY computed — no new DB query, no all-pairs sweep.
    # A candidate is (sib_cid, sib_sha, contract_paths, sib_py): the contract paths get the 4-way
    # analysis; sib_py is the sibling's changed `.py` set, the S2 consumer-evidence source for that side.
    my_py = sorted({p for p in (changed_paths or []) if isinstance(p, str) and p.endswith(".py")})
    my_all = {p for p in (changed_paths or []) if isinstance(p, str)}
    candidates = []
    sib_rows: dict = {}                           # every OTHER open PR row: cid -> (head_sha, paths, impact)
    my_impact: set = set()                        # the acting change's blast radius (down+shared, MAIN graph)
    changes = impact.get("changes") if isinstance(impact, dict) else None
    for c in (changes if isinstance(changes, list) else []):
        if not isinstance(c, dict):
            continue
        sib = c.get("change_id")
        if not isinstance(sib, str):
            continue
        if sib == change_id:                      # the acting PR's own row: keep its graph neighbor set
            my_impact = {p for p in (c.get("impact") or []) if isinstance(p, str)}
            continue
        if not sib.startswith("PR-"):
            continue                              # PR↔PR pairs only; BR-* branch reservations have no PR head
        sib_paths = {p for p in (c.get("paths") or []) if isinstance(p, str)}
        sib_rows[sib] = (c.get("head_sha"), sib_paths,
                         {p for p in (c.get("impact") or []) if isinstance(p, str)})
        shared = sorted(set(my_py) & sib_paths)
        if shared:
            candidates.append((sib, c.get("head_sha"), shared,
                               sorted(p for p in sib_paths if p.endswith(".py"))))
    candidates.sort(key=lambda t: t[0])           # deterministic pair order (stable under re-delivery)

    acting_sha = head_sha if (isinstance(head_sha, str) and _SHA_RE.match(head_sha or "")) else None
    budget = _ReadBudget(reads_cap)
    cache: dict = {}
    shape_cache: dict = {}
    mb_cache: dict = {}

    # ── S2 candidacy sources 2+3 (correction §3 lane 2, folding in PR-3b per §4): cross-file pairs that
    # share NO file. Additive AFTER the sibling source (which keeps priority inside the one shared pair
    # cap) and DEDUPED against it (`known`). Deterministic: siblings visited in sorted order, paths sorted.
    cross_file_candidates = []
    known = {t[0] for t in candidates}
    acting_import_scan = None                     # (targets, unknown, capped), lazy and computed at most once
    pairs_via_main_graph = 0
    pairs_via_import_scan = 0
    candidacy_unknown = 0                         # sibling PAIRS whose fallback presence/absence is unprovable
    candidacy_capped = False
    for sib in sorted(sib_rows):
        if sib in known:
            continue                              # already paired by the shared-surface source
        sib_sha, sib_paths, sib_impact = sib_rows[sib]
        sib_py = sorted(p for p in sib_paths if p.endswith(".py"))
        items: set = set()
        # (2) MAIN-graph 1-hop import/call adjacency, BOTH directions, from the surface's per-change blast
        # radius (`impact` = the _claim_adjacency down+shared neighbor set — no new DB read). A change's
        # blast radius lists the files that depend on ITS files, so a hit names the OTHER side as the
        # consumer and THIS side's changed .py set as the candidate contract files. Files absent from main
        # have no node (a brand-new consumer file) — source 3 below covers exactly that hole.
        if sib_paths & my_impact:                 # sibling changed a file downstream of mine
            items.update(my_py)
        if my_all & sib_impact:                   # acting changed a file downstream of the sibling's
            items.update(sib_py)
        source = "main_graph" if items else None
        pair_scan_unknown = False
        pair_scan_capped = False
        # (3) transient import scan over files fetched within the SAME read budget. Runs only when (2)
        # found nothing, both heads are verified (the fetches need real SHAs), and the pair cap is not
        # already met (a beyond-cap pair is never analyzed, so probing GitHub for it would waste budget).
        # A scan fetch the cap refuses leaves the pair un-considered — exactly the pre-S2 behavior — and
        # every fetched body is dropped right after the ast scan (transient, content-free).
        if not items and len(candidates) + len(cross_file_candidates) >= pair_cap:
            # This sibling was never import-scanned.  It is not part of `pairs_considered` because its edge
            # was not discovered, but coverage is explicitly partial — never the old silent "no candidate".
            candidacy_capped = True
            continue
        if not items and (acting_sha is None
                          or not isinstance(sib_sha, str) or not _SHA_RE.match(sib_sha)):
            candidacy_unknown += 1               # exact heads are required before absence can be asserted
            continue
        if not items:
            if acting_import_scan is None:        # once per event: acting-side import candidates + honesty state
                acting_import_scan = _side_import_targets(
                    gh, repo, my_py, acting_sha, cache, budget)
            acting_import_targets, acting_unknown, acting_capped = acting_import_scan
            pair_scan_unknown = pair_scan_unknown or acting_unknown
            pair_scan_capped = pair_scan_capped or acting_capped
            # acting file imports a module whose defining file the SIBLING changed → sibling produces.
            items.update(acting_import_targets & set(sib_py))
            if not items:
                # sibling file imports a module whose defining file the ACTING PR changed → acting
                # produces. (Skipped when the acting-side scan already made the pair a candidate:
                # conserving reads beats a second-direction probe.)
                sib_targets, sib_unknown, sib_capped = _side_import_targets(
                    gh, repo, sib_py, sib_sha, cache, budget)
                items.update(sib_targets & set(my_py))
                pair_scan_unknown = pair_scan_unknown or sib_unknown
                pair_scan_capped = pair_scan_capped or sib_capped
            if items:
                source = "import_scan"
        if pair_scan_unknown:
            candidacy_unknown += 1
        if pair_scan_capped:
            candidacy_capped = True
        if items:
            cross_file_candidates.append((sib, sib_sha, sorted(items), sib_py))
            if source == "main_graph":
                pairs_via_main_graph += 1
            elif source == "import_scan":
                pairs_via_import_scan += 1
    # Backward-compatible aggregate: historically this was named `pairs_via_graph` even though it already
    # included transient import-scan candidates.  Preserve the field for dashboards, but never use it as proof
    # of persisted-graph use; the two provenance counters above are authoritative.
    pairs_via_graph = pairs_via_main_graph + pairs_via_import_scan
    candidacy_complete = candidacy_unknown == 0 and not candidacy_capped
    candidates = candidates + cross_file_candidates  # sibling-source pairs keep priority inside the shared cap

    considered = len(candidates)
    to_analyze = candidates[:pair_cap]
    capped = considered > pair_cap or candidacy_capped  # undiscovered cap-blocked siblings count as partial too

    findings_n = 0
    unknowns = 0
    contract_deltas = 0
    rebase_n = 0
    divergent_n = 0
    unknown_baseline = 0
    mismatch_n = 0
    expectations_n = 0
    acting_exps = None                            # acting-side Consumer Expectations — lazy, once per event
    # PR-4 input, restricted per correction §2.3 to EVIDENCE-BACKED findings only — since lane S2 that is
    # exactly the recorded `consumer_call_mismatch` rows; the S1 telemetry classes never feed the solver.
    order_findings: list = []

    for sib_cid, sib_sha, shared, sib_py in to_analyze:
        pair_deltas: list = []                    # S2 evidence gate input: this pair's one-sided deltas
        pair_unknown = False
        # An unverified head on EITHER side ⇒ the pair is unanalyzable ⇒ UNKNOWN (freshness discipline:
        # the surface's per-change head_sha is non-null only when every live claim stamp agrees).
        if acting_sha is None or not (isinstance(sib_sha, str) and _SHA_RE.match(sib_sha or "")):
            unknowns += 1
            continue
        # S1 BASELINE POINT: each PR's OWN merge-base with the shared target branch — the authoritative
        # "before" (correction §2.1). The sibling head is NEVER called "before". No baseline → no honest
        # classification → the whole pair degrades to UNKNOWN.
        a_base_sha = _merge_base(gh, repo, branch, acting_sha, mb_cache)
        b_base_sha = _merge_base(gh, repo, branch, sib_sha, mb_cache)
        if a_base_sha is None or b_base_sha is None:
            unknowns += 1
            continue
        for path in shared:
            # Four shape points per shared path: both BASELINES + both HEADS (a common baseline or a shared
            # version is cached — fetched and parsed once per event). Any unknowable point → pair unknown.
            a_base = _shapes_at(gh, repo, path, a_base_sha, cache, shape_cache, budget)
            b_base = _shapes_at(gh, repo, path, b_base_sha, cache, shape_cache, budget)
            a_head = _shapes_at(gh, repo, path, acting_sha, cache, shape_cache, budget)
            b_head = _shapes_at(gh, repo, path, sib_sha, cache, shape_cache, budget)
            if a_base is None or b_base is None or a_head is None or b_head is None:
                pair_unknown = True               # cap exhausted / fetch failed / oversized / unparseable
                if budget.used >= budget.cap:
                    break                         # nothing further can be read this event
                continue
            # 4-WAY per-contract classification over the UNION of all four maps — a def present at a
            # baseline and absent at the changer's head is a removal BY THAT SIDE, visible regardless of
            # which PR's event fired (the audit-A asymmetry killer).
            for qual in sorted(set(a_base) | set(b_base) | set(a_head) | set(b_head)):
                p_ab = a_base.get(qual, _NO_DEF)
                p_bb = b_base.get(qual, _NO_DEF)
                p_ah = a_head.get(qual, _NO_DEF)
                p_bh = b_head.get(qual, _NO_DEF)
                k_ab, k_bb = _shape_point_key(p_ab), _shape_point_key(p_bb)
                k_ah, k_bh = _shape_point_key(p_ah), _shape_point_key(p_bh)
                if k_ab is None or k_bb is None or k_ah is None or k_bh is None:
                    unknowns += 1                 # a present-but-unreadable shape → unknown, never guessed
                    continue
                if k_ah == k_bh:
                    continue                      # the heads agree on this contract — nothing to claim
                changed_a = k_ab != k_ah          # role from the DELTA vs its OWN baseline — never event order
                changed_b = k_bb != k_bh
                if changed_a == changed_b:
                    # Not one-sided: neither changed (a LANDED change left one side stale → advisory
                    # `rebase_needed`, NEVER a breaking/contract_delta claim against the stale side) or
                    # both changed (`divergent_definitions` — a coordination signal, no breaking claim).
                    # No producer exists → CANONICAL sorted-head roles keep the row byte-identical from
                    # either PR's event.
                    code = _REASON_DIVERGENT if changed_a else _REASON_REBASE
                    klass = _CLASS_DIVERGENT if changed_a else _CLASS_REBASE
                    lo, hi = sorted((acting_sha, sib_sha))
                    fp = _finding_fingerprint(acting_sha, sib_sha, path, qual, code)
                    if _guarded_record(db, (repo, branch, path, lo, hi, fp, code, klass, _DETECTOR_STAMP)):
                        findings_n += 1
                        if changed_a:
                            divergent_n += 1
                        else:
                            rebase_n += 1
                    else:
                        unknowns += 1             # a refused/failed record is honest-unknown, never silent
                    continue
                # ONE-SIDED change. Comparable baselines are the precondition for any delta claim: if the
                # two PRs' baselines hold DIFFERENT shapes for this contract, the pair has no shared
                # "before" → UNKNOWN for this contract (counted, never a finding) — correction §2.1.
                if k_ab != k_bb:
                    unknown_baseline += 1
                    continue
                baseline = p_ab if p_ab is not _NO_DEF else p_bb
                if baseline is _NO_DEF:
                    continue                      # pure addition by the changer: no prior contract → no claim
                changer_head = p_ah if changed_a else p_bh
                producer_sha = acting_sha if changed_a else sib_sha    # the CHANGER is the producer
                consumer_sha = sib_sha if changed_a else acting_sha
                new_shape = changer_head if changer_head is not _NO_DEF else None
                # S2 evidence gate input: EVERY one-sided changed/removed contract (comparable baselines) —
                # including a rule-neutral delta, which the mismatch predicate can then prove harmless.
                pair_deltas.append((path, qual, changed_a, baseline, new_shape))
                res = compare_shapes(baseline, new_shape)
                cr = res.get("compat_result")
                if cr == COMPATIBLE:
                    continue                      # rule-neutral delta (e.g. optional param added): no row
                if cr == UNKNOWN:
                    unknowns += 1
                    continue
                for f in (res.get("findings") or []):
                    if f.get("compat_result") not in (BREAKING, RISKY):
                        continue                  # defensive: only rule-firing findings become deltas
                    rule = f.get("rule_id") or ""
                    reason = f"{_REASON_DELTA_PREFIX}{rule}"
                    if not _REASON_RE.match(reason):
                        unknowns += 1             # defensive: never hand the recorder an unsafe token
                        continue
                    fp = _finding_fingerprint(acting_sha, sib_sha, path, qual, rule)
                    if _guarded_record(db, (repo, branch, path, producer_sha, consumer_sha, fp, reason,
                                            _CLASS_CONTRACT_DELTA, _DETECTOR_STAMP)):
                        findings_n += 1
                        contract_deltas += 1
                        if "contract_delta" in _EVIDENCE_BACKED_CLASSES:   # never: delta-only telemetry
                            order_findings.append({
                                "producer_pr": change_id if changed_a else sib_cid,
                                "consumer_pr": sib_cid if changed_a else change_id,
                                "producer_sha": producer_sha, "consumer_sha": consumer_sha,
                                "rule_id": rule, "compat_result": f.get("compat_result")})
                    else:
                        unknowns += 1             # a refused/failed record is honest-unknown, not silently done
        # ── S2 evidence gate (correction §2.2/§2.3): for this pair's one-sided deltas, scan the
        # counterpart (non-changer) side's OWN changed .py files for Consumer Expectations and record a
        # `consumer_call_mismatch:<detail>` finding ONLY where a resolved call of the changed/removed
        # contract is provably incompatible with the NEW shape. Extraction is lazy per side (the acting
        # side once per EVENT, the sibling side once per pair) and cache/budget-honest — in practice the
        # files were already fetched by candidacy or the pair analysis. Dedup per (contract, detail):
        # many call sites with the same incompatibility are ONE finding.
        if pair_deltas:
            sib_exps = None
            if any(not d[2] for d in pair_deltas) and acting_exps is None:   # sibling changed → acting consumes
                acting_exps = _side_expectations(gh, repo, my_py, acting_sha, cache, budget)
                expectations_n += len(acting_exps)
            if any(d[2] for d in pair_deltas):                               # acting changed → sibling consumes
                sib_exps = _side_expectations(gh, repo, sib_py, sib_sha, cache, budget)
                expectations_n += len(sib_exps)
            hits: dict = {}
            for path, qual, changed_a, base_shape, new_shape in pair_deltas:
                for e in ((sib_exps if changed_a else acting_exps) or []):
                    for cand_paths, cand_qual in e.get("targets") or ():
                        if qual != cand_qual or path not in cand_paths:
                            continue              # resolution intersects the REAL delta key — never invents
                        detail = _expectation_mismatch(e, base_shape, new_shape)
                        if detail:
                            hits[(path, qual, changed_a, detail)] = True
            for path, qual, changed_a, detail in sorted(hits):
                reason = _REASON_MISMATCH_PREFIX + detail
                if not _REASON_RE.match(reason):
                    unknowns += 1                 # defensive: never hand the recorder an unsafe token
                    continue
                producer_sha = acting_sha if changed_a else sib_sha    # the CHANGER is the producer
                consumer_sha = sib_sha if changed_a else acting_sha
                fp = _finding_fingerprint(acting_sha, sib_sha, path, qual, reason)
                if _guarded_record(db, (repo, branch, path, producer_sha, consumer_sha, fp, reason,
                                        _CLASS_INCOMPAT, _DETECTOR_STAMP)):
                    findings_n += 1
                    mismatch_n += 1
                    if _MISMATCH_RULE in _EVIDENCE_BACKED_CLASSES:     # the ONE evidence-backed class
                        order_findings.append({
                            "producer_pr": change_id if changed_a else sib_cid,
                            "consumer_pr": sib_cid if changed_a else change_id,
                            "producer_sha": producer_sha, "consumer_sha": consumer_sha,
                            "rule_id": _MISMATCH_RULE, "compat_result": BREAKING})
                else:
                    unknowns += 1                 # a refused/failed record is honest-unknown, not silently done
        if pair_unknown:
            unknowns += 1

    capped = capped or budget.exhausted

    # PR-4 landing-order solver — SHADOW TELEMETRY ONLY, fed ONLY evidence-backed findings (correction
    # §2.3): since lane S2 that is exactly the recorded `consumer_call_mismatch` rows, so the solver's
    # counts are meaningful again for PROVEN consumer breakage (and stay zero for delta-only telemetry).
    # As before, COUNTS ONLY ever reach the log line, and NOTHING here feeds render.
    plan = solve_landing_order(order_findings)
    plan_clusters = plan.get("clusters") if isinstance(plan, dict) else None
    plan_clusters = plan_clusters if isinstance(plan_clusters, list) else []
    clusters_n = len(plan_clusters)
    cycle_n = sum(1 for c in plan_clusters
                  if isinstance(c, dict) and c.get("classification") == CYCLE_CLUSTER)
    ordered_n = sum(1 for c in plan_clusters if isinstance(c, dict) and "order" in c)
    revision_n = len(plan.get("revision_required") or []) if isinstance(plan, dict) else 0

    out = {"pairs_considered": considered, "pairs_analyzed": len(to_analyze),
           "pairs_via_graph": pairs_via_graph,
           "pairs_via_main_graph": pairs_via_main_graph,
           "pairs_via_import_scan": pairs_via_import_scan,
           "candidacy_complete": candidacy_complete,
           "candidacy_unknown": candidacy_unknown,
           "candidacy_capped": candidacy_capped,
           "reads_used": budget.used, "findings": findings_n, "unknowns": unknowns, "capped": capped,
           "contract_deltas": contract_deltas, "rebase_needed": rebase_n,
           "divergent": divergent_n, "unknown_baseline": unknown_baseline,
           "expectations_extracted": expectations_n, "consumer_mismatches": mismatch_n,
           "clusters": clusters_n, "ordered_clusters": ordered_n,
           "cycle_clusters": cycle_n, "revision_required": revision_n}
    # ONE content-free telemetry line per analyzed event: counts only (no path, no symbol, no source byte).
    # pairs_via_graph = legacy aggregate; the main-graph/import split + candidacy completeness are the proof.
    # expectations_extracted /
    # consumer_mismatches = the S2 evidence telemetry — counts, never a member or a name.
    tp = f"trace_id={str(trace_id)[:_TRACE_TAG_WIDTH]} " if trace_id else ""
    print(f"{tp}compat shadow repo={repo} pr={pr} pairs_considered={considered} "
          f"pairs_analyzed={len(to_analyze)} pairs_via_graph={pairs_via_graph} "
          f"pairs_via_main_graph={pairs_via_main_graph} pairs_via_import_scan={pairs_via_import_scan} "
          f"candidacy_complete={str(candidacy_complete).lower()} "
          f"candidacy_unknown={candidacy_unknown} candidacy_capped={str(candidacy_capped).lower()} "
          f"reads_used={budget.used} findings={findings_n} "
          f"unknowns={unknowns} capped={str(capped).lower()} contract_deltas={contract_deltas} "
          f"rebase_needed={rebase_n} divergent={divergent_n} unknown_baseline={unknown_baseline} "
          f"expectations_extracted={expectations_n} consumer_mismatches={mismatch_n} "
          f"clusters={clusters_n} ordered_clusters={ordered_n} cycle_clusters={cycle_n} "
          f"revision_required={revision_n}", flush=True)
    return out


__all__ = ["compat_shadow_enabled", "run_compat_shadow"]
