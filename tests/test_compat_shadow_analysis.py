#!/usr/bin/env python3
"""COMPAT SHADOW ANALYSIS gate (compat lanes PR-3/PR-4 + corrective lanes S1/S2) — the flag-gated shadow
wiring's full contract under the CORRECTED semantics (docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §2/§3).

S1+S2 semantics proven here, against the REAL schema on a real local Postgres, with a fake GitHub client
serving fixture file bodies + per-head merge-bases:

  (1)  OFF (flags unset / allowlist empty / repo not listed) → the module returns BEFORE any work: zero
       'compat_finding' rows, ZERO calls on the fake GitHub client (file reads AND merge-base lookups),
       zero log output.
  (2)  BASELINE ANCHORING: each PR's own merge-base with the target branch is the "before"; baseline file
       reads go through the SAME VERIPSA_COMPAT_FILE_READS_CAP budget and the per-(sha,path) cache (a
       shared baseline is fetched once).
  (3)  SYMMETRY (audit A killed): the same head pair analyzed acting=P then acting=Q produces ONE
       byte-identical ledger row — same canonical fingerprint (unordered head pair + contract key + rule +
       detector tag py-call-v1, the S3a bump replacing py-shape-v2), same delta-derived producer/consumer —
       including the REMOVAL case, which the union iteration makes visible from BOTH event orders.
  (4)  4-WAY classification (audit B killed): acting-only change → `contract_delta:<rule>` with the acting
       head as producer; sibling-only change → `contract_delta:<rule>` with the SIBLING head as producer;
       both-changed → `divergent_definitions` (canonical roles, no breaking claim); neither-changed but
       heads differ (landed change + stale sibling) → `rebase_needed` and NEVER a breaking/contract_delta
       row against the stale side.
  (5)  BASELINE MISMATCH: the two PRs' baselines hold different shapes for a contract → counted
       `unknown_baseline`, NO row (never a finding).
  (6)  REASON VOCABULARY v2: no `breaking:*` / `risky:*` def-diff reason is ever written by this lane.
  (7)  SOLVER ZEROS (S1 wiring of PR-4): the solver consumes only evidence-backed findings — an empty set
       in S1 — so clusters/ordered/cycle/revision read ZERO even when classification rows were recorded.
  (8)  CAPS + HONESTY: pair cap and read cap (INCLUDING baseline reads) degrade to UNKNOWN, never
       'compatible'; parser errors contained; a client without merge_base_sha degrades to unknown.
  (9)  WEBHOOK WIRING (shadow-OFF byte identity): with flags unset, handle_pull_request's result is
       byte-identical (json-serialized) to a run with the compat path severed; with flags ON the wiring
       fires (rows + gh reads + one counts line) while the check/comment output stays byte-identical.
  (10) CONTENT-FREE: sentinel default values / annotations / body strings / junk bytes in the fixtures
       never appear in any DB row or in any captured log output.
  (11) S2 CANDIDACY (audit D killed): persisted graph provenance is proven end-to-end through the REAL
       `ingest_graph_with_authority` + `main_impact_surface`. The §25 mandatory fixture — a producer PR
       reshaping src/api.py and a consumer PR ADDING src/service.py that imports and calls the OLD shape,
       sharing NO file — IS paired by import scan from EITHER PR's event. Graph and transient-import
       provenance are separate, and a head/read/parser/cap failure makes candidacy explicitly incomplete
       rather than a silent empty set.
  (12) S2 EVIDENCE GATE (audit C killed): exactly ONE `consumer_call_mismatch:positional_shortfall` row for
       the §25 fixture — producer = the changer's head, consumer = the caller's head, byte-identical
       canonical fingerprint from both event orders (recorder dedupe holds). Negative controls: the call
       passing the new arg (positionally or by keyword), **kwargs, an unrelated callee, an optional-only
       producer change, a *args call, and a dynamic getattr callee each yield NO mismatch row (and the
       dynamic case no unknown explosion). Positives: a newly required keyword-only name →
       missing_required_kwonly; a removed callee → callee_removed.
  (13) S2 SOLVER WIRING: `consumer_call_mismatch` is the ONE evidence-backed class — the recorded mismatch
       feeds the PR-4 solver (1 ordered cluster on the fixture) while delta-only telemetry still leaves
       every solver count at zero.

Run:  python3 tests/test_compat_shadow_analysis.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                          # _cg_python (the PR-1 shape extractor) lives at repo root
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import _compat_analysis as CA  # noqa: E402
import webhook as WH  # noqa: E402

# PROCESS-UNIQUE (parallel-safe), exactly like test_compat_finding_event.py / db/smoke.sh.
DB = "veripsa_compatshadow_" + str(os.getpid())
INSTALL = "INST-COMPAT-SHADOW"
REPO = "acme/app"          # module-level scenarios (synthetic impact surface)
WREPO = "acme/webapp"      # webhook-wiring scenarios (real claims through handle_pull_request)
BRANCH = "main"
CID = "PR-11"

# Head + baseline SHAs (hex — the recorder refuses anything else).
BASE = "e0" * 20      # common merge-base serving the OLD shape (SIB_SRC)
BASE_NEW = "e9" * 20  # merge-base AFTER a landed signature change (serves ACT_REQ_ADDED)
A_REQ = "a1" * 20     # acting head: required arg added (one-sided acting change)
A_OPT = "a2" * 20     # acting head: optional→required (one-sided acting change)
A_REM = "a3" * 20     # acting head: symbol removed (removal symmetry)
A_DEF = "a4" * 20     # acting head: default value changed only (shape-neutral)
A_JUNK = "a6" * 20    # acting head paired with the junk sibling
A_CAP = "a8" * 20     # acting head for the read-cap scenario
A_PCAP = "a9" * 20    # acting head for the pair-cap scenario
S_BASE = "b1" * 20    # sibling head serving the UNCHANGED old source
S_JUNK = "b5" * 20    # sibling head serving unparseable junk
SIBS = ["c1" * 20, "c2" * 20, "c3" * 20, "c4" * 20, "c5" * 20]   # pair-cap siblings
AH2 = "d3" * 20       # 4-way config 2: acting head, UNCHANGED
SH2 = "d4" * 20       # 4-way config 2: sibling head, changed (required arg added) → sibling is producer
BH1 = "d5" * 20       # 4-way config 3: acting head, changed one way
BH2 = "d6" * 20       # 4-way config 3: sibling head, changed the other way
RH1 = "d7" * 20       # 4-way config 4: acting head on the NEW baseline (landed change), def untouched
RH2 = "d8" * 20       # 4-way config 4: STALE sibling head on the OLD baseline, def untouched
MH1 = "f1" * 20       # baseline-mismatch: acting head, changed vs its OLD baseline
MH2 = "f2" * 20       # baseline-mismatch: sibling head, unchanged vs its NEW baseline
WA = "d1" * 20        # webhook scenario: acting PR-11 head
WS = "d2" * 20        # webhook scenario: sibling PR-22 head
WB = "d9" * 20        # webhook scenario: shared merge-base
MB2 = "e5" * 20       # S2 fixture shared merge-base (api.py holds the OLD create_order shape)
PA2 = "f3" * 20       # S2 producer head: create_order gains required `currency` (§25 fixture)
PB2 = "f4" * 20       # S2 consumer head: ADDS src/service.py calling the OLD shape (§25 fixture)
PB2_CUR = "f5" * 20   # S2 negative: the consumer call passes `currency`
PB2_KWA = "f6" * 20   # S2 negative: the consumer call forwards **kwargs
PB2_UNR = "f7" * 20   # S2 negative: the consumer calls an UNRELATED function
PB2_STAR = "f8" * 20  # S2 negative: *args call (shape expectations suppressed)
PB2_DYN = "f9" * 20   # S2 negative: dynamic getattr callee (no expectation, no unknown explosion)
PA2_OPT = "ea" * 20   # S2 negative: producer adds an OPTIONAL param only
PB2_OPT = "eb" * 20   # S2 negative: consumer head for the optional-add control
PA2_KWO = "ec" * 20   # S2 positive: producer adds a required KEYWORD-ONLY param
PB2_KWO = "ed" * 20   # S2 positive: consumer head for the kwonly case
PA2_REM = "ee" * 20   # S2 positive: producer REMOVES create_order outright
PB2_REM = "ef" * 20   # S2 positive: consumer head for the removal case
CG_BASE = "ca" * 20   # real-main-graph fixture merge base
CG_PROD = "cb" * 20   # real-main-graph fixture producer head
CG_CONS = "cc" * 20   # real-main-graph fixture consumer head
PATH = "src/api.py"
SVC = "src/service.py"

# ── Fixture sources. SENTINELS prove content-freedom: a default-value expression, an annotation name, a body
# string, and a junk-file token — none may ever reach a DB row or a log line. ─────────────────────────────────
SENTINELS = ("DEFAULT_SENTINEL_zz1", "SECRET_BODY_TOKEN_zz2", "AnnotSentinelZz3",
             "SECRET_JUNK_zz4", "OTHER_DEFAULT_SENTINEL_zz9",  # gitleaks:allow -- invalid sentinels
             "S2CallBodySentinel_zz7", "S2DefaultSentinel_zz8")
SIB_SRC = (
    b"def create_order(user, amount=DEFAULT_SENTINEL_zz1):\n"
    b"    return \"SECRET_BODY_TOKEN_zz2\"\n"
    b"\n"
    b"def legacy_hook(x: AnnotSentinelZz3):\n"
    b"    return x\n"
    b"\n"
    b"def helper(a, b=2):\n"
    b"    return a + b\n")
ACT_REQ_ADDED = (          # + required `currency` (only rule_required_arg_added fires → exactly one finding)
    b"def create_order(user, currency, amount=DEFAULT_SENTINEL_zz1):\n"
    b"    return \"SECRET_BODY_TOKEN_zz2\"\n"
    b"\n"
    b"def legacy_hook(x: AnnotSentinelZz3):\n"
    b"    return x\n"
    b"\n"
    b"def helper(a, b=2):\n"
    b"    return a + b\n")
ACT_OPT_REQ = (            # `amount` loses its default → optional_to_required (+ the arity-count rule)
    b"def create_order(user, amount):\n"
    b"    return \"SECRET_BODY_TOKEN_zz2\"\n"
    b"\n"
    b"def legacy_hook(x: AnnotSentinelZz3):\n"
    b"    return x\n"
    b"\n"
    b"def helper(a, b=2):\n"
    b"    return a + b\n")
ACT_REMOVED = (            # legacy_hook gone → exported_symbol_removed (as a contract delta)
    b"def create_order(user, amount=DEFAULT_SENTINEL_zz1):\n"
    b"    return \"SECRET_BODY_TOKEN_zz2\"\n"
    b"\n"
    b"def helper(a, b=2):\n"
    b"    return a + b\n")
ACT_DEF_ONLY = (           # ONLY the default value expression differs → identical shape → no head divergence
    b"def create_order(user, amount=OTHER_DEFAULT_SENTINEL_zz9):\n"
    b"    return \"SECRET_BODY_TOKEN_zz2\"\n"
    b"\n"
    b"def legacy_hook(x: AnnotSentinelZz3):\n"
    b"    return x\n"
    b"\n"
    b"def helper(a, b=2):\n"
    b"    return a + b\n")
JUNK_SRC = b"def broken((((:\n    SECRET_JUNK_zz4\n"

# ── S2 fixture sources (§25 mandatory fixture + controls). The consumer sources carry their own body
# sentinel; the optional-add producer carries a default-expression sentinel — call extraction records
# names+counts+flags ONLY, so none of these may ever surface. ────────────────────────────────────────────────
S2_API_OLD = (b"def create_order(user, amount):\n"
              b"    return \"S2CallBodySentinel_zz7\"\n")
S2_API_NEW = (          # + required `currency` (the producer PR updated its own callers elsewhere)
    b"def create_order(user, amount, currency):\n"
    b"    return \"S2CallBodySentinel_zz7\"\n")
S2_API_OPT = (          # + OPTIONAL `currency` only — compatible delta, must never fire a mismatch
    b"def create_order(user, amount, currency=S2DefaultSentinel_zz8):\n"
    b"    return None\n")
S2_API_KWONLY = (       # + required KEYWORD-ONLY `currency`
    b"def create_order(user, amount, *, currency):\n"
    b"    return None\n")
S2_API_REMOVED = (      # create_order gone entirely
    b"def other_helper(x):\n"
    b"    return x\n")
S2_SVC_CALL = (         # the §25 consumer: NEW file importing and calling the OLD shape
    b"from src.api import create_order\n\n"
    b"def place(user, amount):\n"
    b"    return create_order(user, amount)\n")
S2_SVC_CURRENCY = (     # negative: the call already passes the new arg
    b"from src.api import create_order\n\n"
    b"def place(user, amount, currency):\n"
    b"    return create_order(user, amount, currency)\n")
S2_SVC_KWARGS = (       # negative: **kwargs forwarding — the argument picture is open
    b"from src.api import create_order\n\n"
    b"def place(user, amount, **kw):\n"
    b"    return create_order(user, amount, **kw)\n")
S2_SVC_UNRELATED = (    # negative: resolves fine but to a DIFFERENT symbol than the delta
    b"from src.api import other_helper\n\n"
    b"def place(x):\n"
    b"    return other_helper(x)\n")
S2_SVC_STAR = (         # negative: *args call — shape expectations suppressed
    b"from src.api import create_order\n\n"
    b"def place(*args):\n"
    b"    return create_order(*args)\n")
S2_SVC_DYNAMIC = (      # negative: dynamic callee — NO expectation, never a guess, no unknown explosion
    b"import src.api\n\n"
    b"def place(name, user, amount):\n"
    b"    return getattr(src.api, name)(user, amount)\n")

checks = []
ALL_LOGS = []              # every captured stdout chunk — scanned for sentinels at the end


def add(label, passed):
    checks.append((label, passed))


def counts(**over):
    """The full counts dict with every field zero except the given overrides — one source of truth for the
    S1+S2 field set (incl. the four solver fields, zero unless an evidence-backed mismatch was recorded)."""
    base = {"pairs_considered": 0, "pairs_analyzed": 0, "pairs_via_graph": 0,
            "pairs_via_main_graph": 0, "pairs_via_import_scan": 0,
            "candidacy_complete": True, "candidacy_unknown": 0, "candidacy_capped": False,
            "reads_used": 0,
            "findings": 0, "unknowns": 0, "capped": False, "contract_deltas": 0, "rebase_needed": 0,
            "divergent": 0, "unknown_baseline": 0, "expectations_extracted": 0, "consumer_mismatches": 0,
            "clusters": 0, "ordered_clusters": 0, "cycle_clusters": 0, "revision_required": 0}
    base.update(over)
    return base


class FakeGH:
    """A fake GitHub client serving fixture file bodies by (ref, path) + merge-bases by head sha —
    records every call (file reads and merge-base lookups separately)."""
    def __init__(self, files=None, merge_bases=None):
        self.files = dict(files or {})
        self.merge_bases = dict(merge_bases or {})
        self.calls = []       # get_file_at invocations
        self.mb_calls = []    # merge_base_sha invocations

    def get_file_at(self, repo, path, ref):
        self.calls.append((repo, path, ref))
        return self.files.get((ref, path))

    def merge_base_sha(self, repo, base_ref, head_sha):
        self.mb_calls.append((repo, base_ref, head_sha))
        return self.merge_bases.get(head_sha)


class LegacyGH(FakeGH):
    """An OLD-STYLE client exposing only get_file_at — no merge_base_sha. The module must degrade the
    pair to unknown (no baseline → no claim), never crash, never fall back to head-vs-head."""
    merge_base_sha = None


class FailingFileGH(FakeGH):
    """A transport/read failure, distinct from the contents API's authoritative 404/None."""
    def get_file_at(self, repo, path, ref):
        self.calls.append((repo, path, ref))
        raise RuntimeError("fixture read failure")


@contextlib.contextmanager
def flags(analysis=None, repos=None, pair_cap=None, reads_cap=None):
    """Set/clear the four compat env knobs for one scenario, restoring the previous state after."""
    keys = {"VERIPSA_COMPAT_ANALYSIS": analysis, "VERIPSA_COMPAT_REPOS": repos,
            "VERIPSA_COMPAT_PAIR_CAP": pair_cap, "VERIPSA_COMPAT_FILE_READS_CAP": reads_cap}
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k, v in keys.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def make_run():
    """A db(sql,args) runner authed as the App, entering the installation per call — the live per-event model
    (the prove_outcome_loop pattern)."""
    def run(sql, args=()):
        conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", (INSTALL,))
                cur.fetchone()
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    return run


def owner_rows(account, sql, args=()):
    """The owner/authority READ path: migrator, RLS-pinned (the test_compat_finding_event pattern)."""
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def impact_for(*siblings, acting=CID):
    """A synthetic impact surface: the acting change + the given (change_id, head_sha, paths) siblings —
    exactly the fields run_compat_shadow reads from the real core.main_impact_surface output."""
    changes = [{"change_id": acting, "head_sha": None, "paths": [PATH]}]
    for sib_cid, sha, paths in siblings:
        changes.append({"change_id": sib_cid, "head_sha": sha, "paths": list(paths)})
    return {"repo": REPO, "branch": BRANCH, "changes": changes}


def run_shadow(db, gh, head, impact, changed=(PATH,), acting=CID, pr="11"):
    """One run_compat_shadow call with stdout captured (returned + accumulated for the sentinel scan)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = CA.run_compat_shadow(db, gh, repo=REPO, branch=BRANCH, change_id=acting,
                                   head_sha=head, changed_paths=list(changed), impact=impact,
                                   trace_id="0123456789abcdef", pr=pr)
    log = buf.getvalue()
    ALL_LOGS.append(log)
    return out, log


def compat_rows(account, commit_sha, repo=REPO):
    """Columns 0-6 keep the pre-S3a order; 7-8 append the S3a fact_class + detector stamps."""
    return owner_rows(account,
                      "SELECT commit_sha, counterparty_sha, fact_fingerprint, detail, path, repo, branch, "
                      "fact_class, detector "
                      "FROM core.event WHERE kind='compat_finding' AND repo=%s AND commit_sha=%s "
                      "ORDER BY detail", (repo, commit_sha))


def rows_touching(account, sha):
    """Every compat row referencing `sha` on EITHER side — the 'no claim against these heads' probe.
    Columns 0-3 keep the pre-S3a order; 4-5 append the S3a fact_class + detector stamps."""
    return owner_rows(account,
                      "SELECT commit_sha, counterparty_sha, fact_fingerprint, detail, fact_class, detector "
                      "FROM core.event "
                      "WHERE kind='compat_finding' AND (commit_sha=%s OR counterparty_sha=%s) "
                      "ORDER BY detail", (sha, sha))


def mism_rows(account, sha):
    """Every S2 consumer_call_mismatch row referencing `sha` on either side (the evidence-gated class).
    Columns 0-4 keep the pre-S3a order; 5-6 append the S3a fact_class + detector stamps."""
    return owner_rows(account,
                      "SELECT commit_sha, counterparty_sha, fact_fingerprint, detail, path, "
                      "fact_class, detector "
                      "FROM core.event WHERE kind='compat_finding' "
                      "AND detail LIKE 'consumer_call_mismatch%%' "
                      "AND (commit_sha=%s OR counterparty_sha=%s) ORDER BY detail", (sha, sha))


def impact2(acting_cid, acting_paths, *siblings):
    """A synthetic S2 impact surface with PER-CHANGE paths: the acting change plus (cid, sha, paths[,
    impact]) siblings — the fields run_compat_shadow reads from core.main_impact_surface output."""
    changes = [{"change_id": acting_cid, "head_sha": None, "paths": list(acting_paths)}]
    for s in siblings:
        row = {"change_id": s[0], "head_sha": s[1], "paths": list(s[2])}
        if len(s) > 3:
            row["impact"] = list(s[3])
        changes.append(row)
    return {"repo": REPO, "branch": BRANCH, "changes": changes}


def wev(action, prn, head, files=(PATH,), verified=True):
    """A content-free pull_request event dict (the shape server.py builds + handle_pull_request consumes)."""
    return {"action": action, "repo": WREPO, "base_branch": BRANCH, "pr_number": prn,
            "changed_files": list(files), "changed_ranges": {}, "author": "alice",
            "head_sha": head, "head_snapshot_verified": verified, "truncated_files": False}


def brain(db, event, gh):
    """One handle_pull_request call (hosted path) with stdout captured for the sentinel scan."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = WH.handle_pull_request(db, event, "alice", act_for=True, gh=gh)
    ALL_LOGS.append(buf.getvalue())
    return result, buf.getvalue()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", (r.stdout + r.stderr)[-1500:])
        return 1

    db = make_run()
    account = db("SELECT core.enter_installation_with_authority(%s)", (INSTALL,))
    add(f"setup: the installation routes to a tenant account ({account})", bool(account))

    def total_rows():
        return owner_rows(account, "SELECT count(*) FROM core.event WHERE kind='compat_finding'")[0][0]

    # ── (0) shape extraction sanity: qualified names nest through classes ───────────────────────────────
    q = CA._def_shapes(b"class A:\n    def m(self, x):\n        pass\n\ndef top(y):\n    pass\n")
    add("shapes: defs are keyed by class-qualified name (A.m + top)",
        isinstance(q, dict) and set(q) == {"A.m", "top"} and q["A.m"]["param_names"] == ["self", "x"])
    add("fingerprint: canonical + symmetric — both head orders produce the SAME fingerprint",
        CA._finding_fingerprint(A_REQ, S_BASE, PATH, "create_order", "required_arg_added")
        == CA._finding_fingerprint(S_BASE, A_REQ, PATH, "create_order", "required_arg_added"))
    # S3a: the fingerprint BASIS carries the bumped version token py-call-v1 (py-shape-v2 replaced) — pinned
    # by recomputing the documented basis by hand. Old py-shape-v2 rows can therefore never collide with (or
    # be re-recorded as) current rows: they remain append-only legacy evidence, excluded by the detector
    # stamp once the S3b current-surface read lands.
    import hashlib
    _lo, _hi = sorted((A_REQ, S_BASE))
    manual_fp = hashlib.sha1("|".join((_lo, _hi, PATH, "create_order", "required_arg_added",
                                       "py-call-v1")).encode("utf-8")).hexdigest()
    add("S3a version bump: detector py-call-v1 stamped in the module constants AND in the fingerprint basis",
        CA._DETECTOR_VERSION == "py-call-v1"
        and CA._DETECTOR_STAMP == "python-call-compat/py-call-v1"
        and CA._finding_fingerprint(A_REQ, S_BASE, PATH, "create_order", "required_arg_added") == manual_fp)

    # ── (1) OFF ⇒ zero work: no rows, no gh calls (files OR merge-bases), no logs ───────────────────────
    for label, kw in [
        ("flags unset entirely", dict()),
        ("global ON but allowlist EMPTY (empty ⇒ disabled everywhere)", dict(analysis="1")),
        ("global ON but repo NOT in the allowlist", dict(analysis="1", repos="someone/else")),
        ("allowlisted but global flag OFF", dict(repos=REPO)),
    ]:
        gh = FakeGH({(A_REQ, PATH): ACT_REQ_ADDED, (S_BASE, PATH): SIB_SRC, (BASE, PATH): SIB_SRC},
                    {A_REQ: BASE, S_BASE: BASE})
        with flags(**kw):
            out, log = run_shadow(db, gh, A_REQ, impact_for(("PR-22", S_BASE, [PATH])))
        add(f"OFF ({label}): returns None before any work, ZERO gh calls, ZERO log output",
            out is None and gh.calls == [] and gh.mb_calls == [] and log == "")
    add("OFF: zero compat_finding rows exist after every OFF run", total_rows() == 0)

    # ── (2)+(3) SYMMETRY on a one-sided ACTING change: baseline-anchored delta, both event orders ───────
    files = {(A_REQ, PATH): ACT_REQ_ADDED, (S_BASE, PATH): SIB_SRC, (BASE, PATH): SIB_SRC}
    mbs = {A_REQ: BASE, S_BASE: BASE}
    gh = FakeGH(files, mbs)
    with flags(analysis="1", repos=f"other/x,{REPO}"):
        out, log = run_shadow(db, gh, A_REQ, impact_for(("PR-22", S_BASE, [PATH])))
    add("ON acting=P: counts = 1 pair, 3 reads (shared baseline fetched ONCE), 1 finding = 1 contract "
        "delta, no unknowns, solver counts ZERO (S1: no evidence-backed class feeds the solver)",
        out == counts(pairs_considered=1, pairs_analyzed=1, reads_used=3, findings=1, contract_deltas=1))
    add("ON: two merge-base lookups (one per PR head), none charged to the file-reads budget",
        len(gh.mb_calls) == 2 and {c[2] for c in gh.mb_calls} == {A_REQ, S_BASE})
    add("ON: exactly ONE content-free counts log line with the S1+S2 fields, no path/symbol tokens",
        log.count("compat shadow") == 1 and "pairs_considered=1" in log
        and "contract_deltas=1" in log and "rebase_needed=0" in log and "divergent=0" in log
        and "unknown_baseline=0" in log and "clusters=0" in log and "pairs_via_graph=0" in log
        and "expectations_extracted=0" in log and "consumer_mismatches=0" in log
        and PATH not in log and "create_order" not in log and "PR-22" not in log and CID not in log)
    expected_fp = CA._finding_fingerprint(A_REQ, S_BASE, PATH, "create_order", "required_arg_added")
    rows = compat_rows(account, A_REQ)
    add("ON: exactly ONE compat_finding row for the pair", len(rows) == 1)
    if rows:
        sha, csha, fp, detail, path, repo_col, branch_col, fclass, det = rows[0]
        add("row: PRODUCER = the CHANGER's head (delta-derived role) → commit_sha", sha == A_REQ)
        add("row: consumer = the unchanged side's head → counterparty_sha", csha == S_BASE)
        add("row: reason is vocabulary v2 — contract_delta:<rule>, NOT breaking:*",
            detail == "contract_delta:required_arg_added")
        add("row: fingerprint is the canonical symmetric sha1 (40 hex, py-call-v1 basis)",
            fp == expected_fp and len(fp) == 40)
        add("row: S3a class stamped EXPLICITLY by the writer — contract_delta_observation (telemetry, "
            "never a breakage claim), queryable as a column",
            fclass == "contract_delta_observation")
        add("row: S3a detector identity/version stamped — python-call-compat/py-call-v1",
            det == "python-call-compat/py-call-v1")
        add("row: coordinates carried (repo/branch/path)",
            repo_col == REPO and branch_col == BRANCH and path == PATH)
    # The SAME physical pair, analyzed from the OTHER PR's event (acting=Q, the UNCHANGED side).
    gh2 = FakeGH(files, mbs)
    with flags(analysis="1", repos=REPO):
        out2, _ = run_shadow(db, gh2, S_BASE, impact_for((CID, A_REQ, [PATH]), acting="PR-22"),
                             acting="PR-22", pr="22")
    rows2 = compat_rows(account, A_REQ)
    add("SYMMETRY: acting=Q re-analysis records the SAME single row — same fp, PRODUCER STILL the "
        "changer's head (role from the delta, not the event order); dedupe held across event orders",
        out2 == counts(pairs_considered=1, pairs_analyzed=1, reads_used=3, findings=1, contract_deltas=1)
        and len(rows2) == 1 and rows2[0][0] == A_REQ and rows2[0][1] == S_BASE
        and rows2[0][2] == expected_fp and rows2[0][3] == "contract_delta:required_arg_added"
        and total_rows() == 1)

    # ── (3b) REMOVAL SYMMETRY: the union iteration makes a removal visible from BOTH event orders ───────
    files = {(A_REM, PATH): ACT_REMOVED, (S_BASE, PATH): SIB_SRC, (BASE, PATH): SIB_SRC}
    mbs = {A_REM: BASE, S_BASE: BASE}
    rem_fp = CA._finding_fingerprint(A_REM, S_BASE, PATH, "legacy_hook", "exported_symbol_removed")
    with flags(analysis="1", repos=REPO):
        out_r1, _ = run_shadow(db, FakeGH(files, mbs), A_REM, impact_for(("PR-22", S_BASE, [PATH])))
        out_r2, _ = run_shadow(db, FakeGH(files, mbs), S_BASE,
                               impact_for((CID, A_REM, [PATH]), acting="PR-22"), acting="PR-22", pr="22")
    rrows = compat_rows(account, A_REM)
    add("REMOVAL from the remover's event: one contract_delta:exported_symbol_removed row, producer = "
        "the remover's head",
        out_r1 == counts(pairs_considered=1, pairs_analyzed=1, reads_used=3, findings=1, contract_deltas=1)
        and len(rrows) == 1 and rrows[0][1] == S_BASE
        and rrows[0][3] == "contract_delta:exported_symbol_removed" and rrows[0][2] == rem_fp)
    add("REMOVAL from the OTHER PR's event (old code was blind here): SAME single row, byte-identical — "
        "producer still the remover, fingerprint unchanged, no second row",
        out_r2 == counts(pairs_considered=1, pairs_analyzed=1, reads_used=3, findings=1, contract_deltas=1)
        and compat_rows(account, A_REM) == rrows)

    # ── (4) 4-WAY config 2: SIBLING-only change ⇒ the SIBLING head is the producer ──────────────────────
    gh = FakeGH({(AH2, PATH): SIB_SRC, (SH2, PATH): ACT_REQ_ADDED, (BASE, PATH): SIB_SRC},
                {AH2: BASE, SH2: BASE})
    with flags(analysis="1", repos=REPO):
        out, _ = run_shadow(db, gh, AH2, impact_for(("PR-22", SH2, [PATH])))
    srows = compat_rows(account, SH2)
    add("4-way sibling-only: contract_delta:required_arg_added recorded with the SIBLING head as producer "
        "(the audit-A 'roles invert by event order' case now lands on the true changer)",
        out == counts(pairs_considered=1, pairs_analyzed=1, reads_used=3, findings=1, contract_deltas=1)
        and len(srows) == 1 and srows[0][0] == SH2 and srows[0][1] == AH2
        and srows[0][3] == "contract_delta:required_arg_added"
        and srows[0][2] == CA._finding_fingerprint(AH2, SH2, PATH, "create_order", "required_arg_added"))

    # ── (4) 4-WAY config 3: BOTH changed ⇒ divergent_definitions (no breaking claim, canonical roles) ───
    gh = FakeGH({(BH1, PATH): ACT_REQ_ADDED, (BH2, PATH): ACT_OPT_REQ, (BASE, PATH): SIB_SRC},
                {BH1: BASE, BH2: BASE})
    with flags(analysis="1", repos=REPO):
        out, _ = run_shadow(db, gh, BH1, impact_for(("PR-22", BH2, [PATH])))
    lo, hi = sorted((BH1, BH2))
    drows = rows_touching(account, BH1)
    add("4-way both-changed: ONE divergent_definitions row, canonical sorted-head roles, and NO "
        "breaking/contract_delta claim anywhere on the pair",
        out == counts(pairs_considered=1, pairs_analyzed=1, reads_used=3, findings=1, divergent=1)
        and len(drows) == 1 and drows[0][0] == lo and drows[0][1] == hi
        and drows[0][3] == "divergent_definitions"
        and drows[0][2] == CA._finding_fingerprint(BH1, BH2, PATH, "create_order", "divergent_definitions"))
    add("4-way both-changed: S3a class stamped divergent_definition_observation (coordination telemetry)",
        len(drows) == 1 and drows[0][4] == "divergent_definition_observation")

    # ── (4) 4-WAY config 4: LANDED change + STALE sibling ⇒ rebase_needed, NEVER breaking (audit B) ─────
    files = {(RH1, PATH): ACT_REQ_ADDED, (RH2, PATH): SIB_SRC,
             (BASE_NEW, PATH): ACT_REQ_ADDED, (BASE, PATH): SIB_SRC}
    mbs = {RH1: BASE_NEW, RH2: BASE}
    with flags(analysis="1", repos=REPO):
        out_s1, _ = run_shadow(db, FakeGH(files, mbs), RH1, impact_for(("PR-22", RH2, [PATH])))
        out_s2, _ = run_shadow(db, FakeGH(files, mbs), RH2,
                               impact_for((CID, RH1, [PATH]), acting="PR-22"), acting="PR-22", pr="22")
    lo, hi = sorted((RH1, RH2))
    strows = rows_touching(account, RH1)
    add("4-way stale-sibling: neither side changed vs its OWN baseline, heads differ → ONE advisory "
        "rebase_needed row (canonical roles), 4 reads (two baselines + two heads)",
        out_s1 == counts(pairs_considered=1, pairs_analyzed=1, reads_used=4, findings=1, rebase_needed=1)
        and len(strows) == 1 and strows[0][0] == lo and strows[0][1] == hi
        and strows[0][3] == "rebase_needed"
        and strows[0][2] == CA._finding_fingerprint(RH1, RH2, PATH, "create_order", "rebase_needed"))
    add("4-way stale-sibling: S3a class stamped rebase_needed_observation (advisory staleness telemetry)",
        len(strows) == 1 and strows[0][4] == "rebase_needed_observation")
    add("4-way stale-sibling: NEVER a breaking/contract_delta row against the stale side (the audit-B "
        "systematic false positive is dead), and the reversed event order dedupes onto the same row",
        all(not r[3].startswith(("breaking", "risky", "contract_delta")) for r in strows)
        and out_s2 == counts(pairs_considered=1, pairs_analyzed=1, reads_used=4, findings=1,
                             rebase_needed=1)
        and rows_touching(account, RH1) == strows)

    # ── (5) BASELINE MISMATCH on a one-sided change ⇒ unknown_baseline, NO row ──────────────────────────
    gh = FakeGH({(MH1, PATH): ACT_OPT_REQ, (MH2, PATH): ACT_REQ_ADDED,
                 (BASE, PATH): SIB_SRC, (BASE_NEW, PATH): ACT_REQ_ADDED},
                {MH1: BASE, MH2: BASE_NEW})
    with flags(analysis="1", repos=REPO):
        out, _ = run_shadow(db, gh, MH1, impact_for(("PR-22", MH2, [PATH])))
    add("baseline mismatch: the two PRs' baselines hold DIFFERENT shapes for the contract → counted "
        "unknown_baseline, NO row, no unknown-degradation",
        out == counts(pairs_considered=1, pairs_analyzed=1, reads_used=4, unknown_baseline=1)
        and rows_touching(account, MH1) == [] and rows_touching(account, MH2) == [])

    # ── one-sided optional→required: the v2 reason keeps the rule ids ───────────────────────────────────
    gh = FakeGH({(A_OPT, PATH): ACT_OPT_REQ, (S_BASE, PATH): SIB_SRC, (BASE, PATH): SIB_SRC},
                {A_OPT: BASE, S_BASE: BASE})
    with flags(analysis="1", repos=REPO):
        out, _ = run_shadow(db, gh, A_OPT, impact_for(("PR-22", S_BASE, [PATH])))
    details = [r[3] for r in compat_rows(account, A_OPT)]
    add("optional→required: both rule rows carry the contract_delta: prefix with the rule id kept",
        out == counts(pairs_considered=1, pairs_analyzed=1, reads_used=3, findings=2, contract_deltas=2)
        and details == ["contract_delta:optional_to_required", "contract_delta:required_arg_added"])

    # ── default value changed ONLY: heads agree on shape ⇒ nothing to claim ─────────────────────────────
    gh = FakeGH({(A_DEF, PATH): ACT_DEF_ONLY, (S_BASE, PATH): SIB_SRC, (BASE, PATH): SIB_SRC},
                {A_DEF: BASE, S_BASE: BASE})
    with flags(analysis="1", repos=REPO):
        out, _ = run_shadow(db, gh, A_DEF, impact_for(("PR-22", S_BASE, [PATH])))
    add("default value changed ONLY: shape-neutral ⇒ NO finding, NO row, no unknown",
        out == counts(pairs_considered=1, pairs_analyzed=1, reads_used=3)
        and len(compat_rows(account, A_DEF)) == 0)

    # ══ LANE S2 (11)+(12)+(13): the §25 MANDATORY FIXTURE — cross-file candidacy + evidence gate ════════
    # main: src/api.py create_order(user, amount). PR-30 (producer) → create_order(user, amount, currency).
    # PR-31 (consumer) ADDS src/service.py importing and calling the OLD shape. NO shared file: the pair
    # must be found by import-scan candidacy (service.py is new — the main graph has no node), from EITHER
    # PR's webhook event, and yield exactly ONE consumer_call_mismatch:positional_shortfall row.
    s2_files = {(PA2, PATH): S2_API_NEW, (PB2, PATH): S2_API_OLD, (MB2, PATH): S2_API_OLD,
                (PB2, SVC): S2_SVC_CALL}
    s2_mbs = {PA2: MB2, PB2: MB2}
    s2_fp = CA._finding_fingerprint(PA2, PB2, PATH, "create_order",
                                    "consumer_call_mismatch:positional_shortfall")

    # REAL persisted-main-graph route (not a synthetic `impact` dict). Both files exist on main and the
    # service file imports the API file. Ingest that graph, create exact live claims, stamp verified heads,
    # and feed the REAL main_impact_surface into Compatibility from BOTH event orders.
    graph = {
        "nodes": [
            {"id": PATH, "kind": "file", "path": PATH, "language": "python"},
            {"id": SVC, "kind": "file", "path": SVC, "language": "python"},
        ],
        "edges": [{"src": SVC, "dst": PATH, "kind": "imports"}],
    }
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps(graph), REPO, BRANCH, CG_BASE))
    db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
       (f"PR-50:{PATH}", PATH, REPO, BRANCH, "graph-producer"))
    db("SELECT core.set_change_head_sha_with_authority(%s,%s,%s,%s)",
       ("PR-50", REPO, BRANCH, CG_PROD))
    db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
       (f"PR-51:{SVC}", SVC, REPO, BRANCH, "graph-consumer"))
    db("SELECT core.set_change_head_sha_with_authority(%s,%s,%s,%s)",
       ("PR-51", REPO, BRANCH, CG_CONS))
    real_impact = db("SELECT core.main_impact_surface(%s,%s)", (REPO, BRANCH))
    real_impact = real_impact if isinstance(real_impact, dict) else json.loads(real_impact)
    real_changes = {c.get("change_id"): c for c in (real_impact.get("changes") or [])
                    if isinstance(c, dict)}
    graph_files = {(CG_PROD, PATH): S2_API_NEW, (CG_CONS, PATH): S2_API_OLD,
                   (CG_BASE, PATH): S2_API_OLD, (CG_CONS, SVC): S2_SVC_CALL}
    graph_mbs = {CG_PROD: CG_BASE, CG_CONS: CG_BASE}
    with flags(analysis="1", repos=REPO):
        out_graph_p, log_graph_p = run_shadow(
            db, FakeGH(graph_files, graph_mbs), CG_PROD, real_impact,
            changed=(PATH,), acting="PR-50", pr="50")
        out_graph_c, log_graph_c = run_shadow(
            db, FakeGH(graph_files, graph_mbs), CG_CONS, real_impact,
            changed=(SVC,), acting="PR-51", pr="51")
    graph_expected = counts(
        pairs_considered=1, pairs_analyzed=1, pairs_via_graph=1, pairs_via_main_graph=1,
        reads_used=4, findings=2, contract_deltas=1, expectations_extracted=1,
        consumer_mismatches=1, clusters=1, ordered_clusters=1)
    add("S2 real graph E2E: ingest_graph → main_impact_surface carries service.py in the producer's "
        "one-hop impact", SVC in (real_changes.get("PR-50", {}).get("impact") or []))
    add("S2 real graph E2E: BOTH event orders use persisted main graph only, produce the same evidence/plan "
        "counts, and candidacy is complete", out_graph_p == graph_expected and out_graph_c == graph_expected
        and "pairs_via_main_graph=1" in log_graph_p and "pairs_via_import_scan=0" in log_graph_p
        and "candidacy_complete=true" in log_graph_p
        and "pairs_via_main_graph=1" in log_graph_c and "pairs_via_import_scan=0" in log_graph_c)
    db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)", ("PR-50", REPO, BRANCH))
    db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)", ("PR-51", REPO, BRANCH))

    with flags(analysis="1", repos=REPO):
        # consumer's event first (acting = PR-31): candidacy via the ACTING side's import scan
        out_b, log_b = run_shadow(db, FakeGH(s2_files, s2_mbs), PB2,
                                  impact2("PR-31", [SVC], ("PR-30", PA2, [PATH])),
                                  changed=(SVC,), acting="PR-31", pr="31")
        # producer's event (acting = PR-30): candidacy via the SIBLING side's import scan
        out_a, _ = run_shadow(db, FakeGH(s2_files, s2_mbs), PA2,
                              impact2("PR-30", [PATH], ("PR-31", PB2, [SVC])),
                              changed=(PATH,), acting="PR-30", pr="30")
    s2_expected = counts(pairs_considered=1, pairs_analyzed=1, pairs_via_graph=1,
                         pairs_via_import_scan=1, reads_used=4,
                         findings=2, contract_deltas=1, expectations_extracted=1, consumer_mismatches=1,
                         clusters=1, ordered_clusters=1)
    add("S2 §25 fixture, consumer's event: import-scan candidacy pairs the no-shared-file PRs "
        "(pairs_via_import_scan=1), 4 reads, delta telemetry + exactly ONE evidence-backed mismatch, and the "
        "solver consumes it (1 ordered cluster — counts meaningful again)", out_b == s2_expected)
    add("S2 §25 fixture, producer's event: byte-identical counts from the OTHER order", out_a == s2_expected)
    s2_rows = mism_rows(account, PA2)
    add("S2 §25 fixture: ONE consumer_call_mismatch row across BOTH event orders — producer = the "
        "changer's head, consumer = the caller's head, detail positional_shortfall, canonical symmetric "
        "fingerprint (recorder dedupe held)",
        len(s2_rows) == 1 and s2_rows[0][0] == PA2 and s2_rows[0][1] == PB2
        and s2_rows[0][3] == "consumer_call_mismatch:positional_shortfall"
        and s2_rows[0][2] == s2_fp and s2_rows[0][4] == PATH)
    add("S2 §25 fixture: S3a class stamped evidence_backed_incompatibility — the ONE class that claims "
        "proven consumer breakage — with the py-call-v1 detector stamp",
        len(s2_rows) == 1 and s2_rows[0][5] == "evidence_backed_incompatibility"
        and s2_rows[0][6] == "python-call-compat/py-call-v1")
    add("S2 telemetry: the legacy aggregate is split into import-scan provenance, with candidacy complete "
        "and counts only — still no path/symbol token",
        "pairs_via_graph=1" in log_b and "pairs_via_main_graph=0" in log_b
        and "pairs_via_import_scan=1" in log_b and "candidacy_complete=true" in log_b
        and "expectations_extracted=1" in log_b
        and "consumer_mismatches=1" in log_b and SVC not in log_b and "create_order" not in log_b
        and "place" not in log_b)

    # ── S2 negative controls + positives, each from the producer's event (one order suffices; the fixture
    # above proved order symmetry). Fresh consumer/producer heads per control keep the ledger separable. ──
    def s2_run(pa_sha, api_src, pb_sha, svc_src, cid_a, cid_b, prn):
        files = {(pa_sha, PATH): api_src, (pb_sha, PATH): S2_API_OLD, (MB2, PATH): S2_API_OLD,
                 (pb_sha, SVC): svc_src}
        mbs = {pa_sha: MB2, pb_sha: MB2}
        with flags(analysis="1", repos=REPO):
            out, _ = run_shadow(db, FakeGH(files, mbs), pa_sha,
                                impact2(cid_a, [PATH], (cid_b, pb_sha, [SVC])),
                                changed=(PATH,), acting=cid_a, pr=prn)
        return out

    neg = counts(pairs_considered=1, pairs_analyzed=1, pairs_via_graph=1,
                 pairs_via_import_scan=1, reads_used=4,
                 findings=1, contract_deltas=1, expectations_extracted=1)
    out_cur = s2_run(PA2, S2_API_NEW, PB2_CUR, S2_SVC_CURRENCY, "PR-32", "PR-33", "32")
    add("S2 negative — call passes the new arg: delta telemetry only, NO mismatch row",
        out_cur == neg and mism_rows(account, PB2_CUR) == [])
    out_kwa = s2_run(PA2, S2_API_NEW, PB2_KWA, S2_SVC_KWARGS, "PR-34", "PR-35", "34")
    add("S2 negative — **kwargs forwarding: expectation recorded but suppressed, NO mismatch row",
        out_kwa == neg and mism_rows(account, PB2_KWA) == [])
    out_unr = s2_run(PA2, S2_API_NEW, PB2_UNR, S2_SVC_UNRELATED, "PR-36", "PR-37", "36")
    add("S2 negative — unrelated callee: resolves to a DIFFERENT symbol than the delta, NO mismatch row",
        out_unr == neg and mism_rows(account, PB2_UNR) == [])
    out_star = s2_run(PA2, S2_API_NEW, PB2_STAR, S2_SVC_STAR, "PR-38", "PR-39", "38")
    add("S2 negative — *args call: shape expectation suppressed, NO mismatch row",
        out_star == neg and mism_rows(account, PB2_STAR) == [])
    out_dyn = s2_run(PA2, S2_API_NEW, PB2_DYN, S2_SVC_DYNAMIC, "PR-40", "PR-41", "40")
    add("S2 negative — dynamic getattr callee: NO expectation, NO mismatch row, and NO unknown explosion",
        out_dyn == counts(pairs_considered=1, pairs_analyzed=1, pairs_via_graph=1,
                          pairs_via_import_scan=1, reads_used=4,
                          findings=1, contract_deltas=1)
        and mism_rows(account, PB2_DYN) == [])
    out_opt = s2_run(PA2_OPT, S2_API_OPT, PB2_OPT, S2_SVC_CALL, "PR-42", "PR-43", "42")
    add("S2 negative — producer added an OPTIONAL arg only: compatible delta, expectation extracted, NO "
        "row of ANY kind against either head",
        out_opt == counts(pairs_considered=1, pairs_analyzed=1, pairs_via_graph=1,
                          pairs_via_import_scan=1, reads_used=4,
                          expectations_extracted=1)
        and rows_touching(account, PA2_OPT) == [] and rows_touching(account, PB2_OPT) == [])
    out_kwo = s2_run(PA2_KWO, S2_API_KWONLY, PB2_KWO, S2_SVC_CALL, "PR-44", "PR-45", "44")
    kwo_rows = mism_rows(account, PA2_KWO)
    add("S2 positive — newly required KEYWORD-ONLY name: ONE consumer_call_mismatch:missing_required_"
        "kwonly row (producer = the changer's head)",
        out_kwo == counts(pairs_considered=1, pairs_analyzed=1, pairs_via_graph=1,
                          pairs_via_import_scan=1, reads_used=4,
                          findings=2, contract_deltas=1, expectations_extracted=1, consumer_mismatches=1,
                          clusters=1, ordered_clusters=1)
        and len(kwo_rows) == 1 and kwo_rows[0][0] == PA2_KWO and kwo_rows[0][1] == PB2_KWO
        and kwo_rows[0][3] == "consumer_call_mismatch:missing_required_kwonly"
        and kwo_rows[0][2] == CA._finding_fingerprint(PA2_KWO, PB2_KWO, PATH, "create_order",
                                                      "consumer_call_mismatch:missing_required_kwonly"))
    out_rem = s2_run(PA2_REM, S2_API_REMOVED, PB2_REM, S2_SVC_CALL, "PR-46", "PR-47", "46")
    rem_rows = mism_rows(account, PA2_REM)
    add("S2 positive — REMOVED callee: ONE consumer_call_mismatch:callee_removed row alongside the "
        "removal delta telemetry",
        out_rem == counts(pairs_considered=1, pairs_analyzed=1, pairs_via_graph=1,
                          pairs_via_import_scan=1, reads_used=4,
                          findings=2, contract_deltas=1, expectations_extracted=1, consumer_mismatches=1,
                          clusters=1, ordered_clusters=1)
        and len(rem_rows) == 1 and rem_rows[0][0] == PA2_REM and rem_rows[0][1] == PB2_REM
        and rem_rows[0][3] == "consumer_call_mismatch:callee_removed")

    # ── S2 read cap binds the IMPORT SCAN itself: a cap-refused scan leaves the pair un-considered ──────
    gh_cap = FakeGH(s2_files, s2_mbs)
    with flags(analysis="1", repos=REPO, reads_cap="1"):
        out_scap, _ = run_shadow(db, gh_cap, PA2, impact2("PR-30", [PATH], ("PR-31", PB2, [SVC])),
                                 changed=(PATH,), acting="PR-30", pr="30")
    add("S2 read cap: the acting-side scan consumes the budget of 1, the sibling-side scan is refused → "
        "the pair stays UN-CONSIDERED (no claim either way), candidacy is explicitly capped/incomplete, "
        "zero merge-base lookups",
        out_scap == counts(reads_used=1, capped=True, candidacy_complete=False,
                           candidacy_capped=True) and len(gh_cap.calls) == 1
        and gh_cap.mb_calls == [])

    # A transport failure, parser failure, oversized body, or unverified sibling head during FALLBACK
    # candidacy is not an empty import set. Each leaves the pair undiscovered but explicitly incomplete.
    with flags(analysis="1", repos=REPO):
        out_fetch, _ = run_shadow(
            db, FailingFileGH({}, s2_mbs), PA2,
            impact2("PR-60", [PATH], ("PR-61", PB2, [SVC])),
            changed=(PATH,), acting="PR-60", pr="60")
        out_parse, _ = run_shadow(
            db, FakeGH({(PA2, PATH): S2_API_NEW, (PB2, SVC): JUNK_SRC}, s2_mbs), PA2,
            impact2("PR-62", [PATH], ("PR-63", PB2, [SVC])),
            changed=(PATH,), acting="PR-62", pr="62")
        out_large, _ = run_shadow(
            db, FakeGH({(PA2, PATH): b"x" * (CA._MAX_FILE_BYTES + 1),
                        (PB2, SVC): b"def local_only():\n    return 1\n"}, s2_mbs), PA2,
            impact2("PR-64", [PATH], ("PR-65", PB2, [SVC])),
            changed=(PATH,), acting="PR-64", pr="64")
        out_unverified, _ = run_shadow(
            db, FakeGH({}, s2_mbs), PA2,
            impact2("PR-66", [PATH], ("PR-67", None, [SVC])),
            changed=(PATH,), acting="PR-66", pr="66")
    incomplete = counts(reads_used=2, candidacy_complete=False, candidacy_unknown=1)
    add("S2 candidacy read failure: two failed reads are UNKNOWN coverage, never clean zero",
        out_fetch == incomplete)
    add("S2 candidacy parser failure: unparseable import source is UNKNOWN coverage, never an empty import set",
        out_parse == incomplete)
    add("S2 candidacy size bound: oversized import source is UNKNOWN coverage, never clean zero",
        out_large == incomplete)
    add("S2 candidacy head freshness: missing sibling head is UNKNOWN before any file read",
        out_unverified == counts(candidacy_complete=False, candidacy_unknown=1))

    # Pair-cap honesty for an UNDISCOVERED fallback sibling: after the first import candidate fills the cap,
    # the second sibling is not scanned and therefore MUST make candidacy_capped=true even though the legacy
    # `pairs_considered` count can only include the one edge actually discovered.
    gh_ccap = FakeGH(s2_files, s2_mbs)
    with flags(analysis="1", repos=REPO, pair_cap="1"):
        out_ccap, _ = run_shadow(
            db, gh_ccap, PA2,
            impact2("PR-68", [PATH], ("PR-69", PB2, [SVC]), ("PR-70", PB2_CUR, [SVC])),
            changed=(PATH,), acting="PR-68", pr="68")
    add("S2 candidacy pair cap: unscanned non-shared sibling makes coverage capped/incomplete instead of "
        "silently absent",
        out_ccap == counts(pairs_considered=1, pairs_analyzed=1, pairs_via_graph=1,
                           pairs_via_import_scan=1, reads_used=4, findings=2, contract_deltas=1,
                           expectations_extracted=1, consumer_mismatches=1, clusters=1,
                           ordered_clusters=1, capped=True, candidacy_complete=False,
                           candidacy_capped=True))

    # ── PAIR CAP: 5 sharing siblings, cap 3 ⇒ 3 analyzed, capped, extras get NO claim; solver ZERO ──────
    files = {(A_PCAP, PATH): ACT_REQ_ADDED, (BASE, PATH): SIB_SRC}
    files.update({(s, PATH): SIB_SRC for s in SIBS})
    mbs = {A_PCAP: BASE}
    mbs.update({s: BASE for s in SIBS})
    gh = FakeGH(files, mbs)
    sib_rows = [(f"PR-2{i+1}", SIBS[i], [PATH]) for i in range(5)]
    with flags(analysis="1", repos=REPO):     # default pair cap 3 (unset ⇒ the shipped default)
        out, _ = run_shadow(db, gh, A_PCAP, impact_for(*sib_rows))
    counterparties = {r[1] for r in compat_rows(account, A_PCAP)}
    add("pair cap: 5 considered, 3 analyzed (default cap), capped=true, shared acting file + baseline "
        "each read ONCE (5 reads); SOLVER COUNTS STAY ZERO despite 3 recorded deltas (S1: nothing "
        "recorded is evidence-backed)",
        out == counts(pairs_considered=5, pairs_analyzed=3, reads_used=5, findings=3, contract_deltas=3,
                      capped=True))
    add("pair cap: rows reference ONLY the 3 analyzed sibling heads — the 2 extra pairs got no row and "
        "no 'compatible' claim; merge-bases resolved once per head (4 lookups)",
        counterparties == set(SIBS[:3]) and len(gh.mb_calls) == 4)

    # ── READ CAP binds INCLUDING baseline reads ⇒ unknown, no finding, no crash ─────────────────────────
    gh = FakeGH({(A_CAP, PATH): ACT_REQ_ADDED, (S_BASE, PATH): SIB_SRC, (BASE, PATH): SIB_SRC},
                {A_CAP: BASE, S_BASE: BASE})
    with flags(analysis="1", repos=REPO, reads_cap="2"):
        out, _ = run_shadow(db, gh, A_CAP, impact_for(("PR-22", S_BASE, [PATH])))
    add("read cap: baseline + one head consumed the budget of 2 → the pair degrades to UNKNOWN (never "
        "'compatible'), capped=true, no row, no crash",
        out == counts(pairs_considered=1, pairs_analyzed=1, reads_used=2, unknowns=1, capped=True)
        and len(compat_rows(account, A_CAP)) == 0)

    # ── PARSER ERROR at one head ⇒ unknown, no crash, no source text in logs ────────────────────────────
    gh = FakeGH({(A_JUNK, PATH): SIB_SRC, (S_JUNK, PATH): JUNK_SRC, (BASE, PATH): SIB_SRC},
                {A_JUNK: BASE, S_JUNK: BASE})
    with flags(analysis="1", repos=REPO):
        out, log = run_shadow(db, gh, A_JUNK, impact_for(("PR-22", S_JUNK, [PATH])))
    add("parser error: junk at one head ⇒ pair UNKNOWN, no finding, no crash",
        out == counts(pairs_considered=1, pairs_analyzed=1, reads_used=3, unknowns=1)
        and len(compat_rows(account, A_JUNK)) == 0)
    add("parser error: no junk-source byte in the captured log", "SECRET_JUNK_zz4" not in log)

    # ── robustness: malformed inputs + baseline-less clients never crash, never guess ───────────────────
    with flags(analysis="1", repos=REPO):
        out_none, _ = run_shadow(db, FakeGH(), A_REQ, None)                      # no surface at all
        out_nogh, _ = run_shadow(db, None, A_REQ, impact_for(("PR-22", S_BASE, [PATH])))   # no gh client
        out_nohead, _ = run_shadow(db, FakeGH(), None, impact_for(("PR-22", S_BASE, [PATH])))  # unverified
        legacy = LegacyGH({(A_REQ, PATH): ACT_REQ_ADDED, (S_BASE, PATH): SIB_SRC})
        out_nomb, _ = run_shadow(db, legacy, A_REQ, impact_for(("PR-22", S_BASE, [PATH])))
        gh_mbfail = FakeGH({(A_REQ, PATH): ACT_REQ_ADDED, (S_BASE, PATH): SIB_SRC}, {})  # mb → None
        out_mbnone, _ = run_shadow(db, gh_mbfail, A_REQ, impact_for(("PR-22", S_BASE, [PATH])))
    add("robustness: absent surface / gh client / verified head each degrade cleanly (no crash, no row)",
        out_none == counts()
        and out_nogh == counts(pairs_considered=1, pairs_analyzed=1, unknowns=1)
        and out_nohead == counts(pairs_considered=1, pairs_analyzed=1, unknowns=1))
    add("robustness: NO BASELINE ⇒ NO CLAIM — a client without merge_base_sha, or a failed merge-base "
        "lookup, degrades the pair to unknown with ZERO file reads (never falls back to head-vs-head)",
        out_nomb == counts(pairs_considered=1, pairs_analyzed=1, unknowns=1) and legacy.calls == []
        and out_mbnone == counts(pairs_considered=1, pairs_analyzed=1, unknowns=1)
        and gh_mbfail.calls == [])

    # ── (9) WEBHOOK WIRING: shadow-OFF byte identity + shadow-ON zero output change ─────────────────────
    wgh = FakeGH({(WA, PATH): ACT_REQ_ADDED, (WS, PATH): SIB_SRC, (WB, PATH): SIB_SRC},
                 {WA: WB, WS: WB})
    spy_calls = []
    real_run = WH.run_compat_shadow

    def spy(*a, **kw):
        spy_calls.append(1)
        return real_run(*a, **kw)

    WH.run_compat_shadow = spy
    try:
        with flags():                                             # flags unset = shipped default
            brain(db, wev("opened", 22, WS), wgh)                 # sibling enters first
            r11_off_open, _ = brain(db, wev("opened", 11, WA), wgh)
        add("wiring OFF: two overlapping PRs analyzed; run_compat_shadow NEVER invoked; gh untouched",
            isinstance(r11_off_open.get("check"), dict) and spy_calls == []
            and wgh.calls == [] and wgh.mb_calls == [])
        # SEVERED run (= pre-PR main: the compat path unreachable) vs the real flags-OFF run, on the SAME
        # idempotent re-render event (synchronize with unchanged heads) — byte-identical results required.
        def _off(_repo):
            return False
        WH.compat_shadow_enabled, real_enabled = _off, WH.compat_shadow_enabled
        try:
            with flags(analysis="1", repos=WREPO):                # flags ON but the path is severed
                r_sev, _ = brain(db, wev("synchronize", 11, WA), wgh)
        finally:
            WH.compat_shadow_enabled = real_enabled
        with flags():                                             # real code, flags OFF
            r_off, _ = brain(db, wev("synchronize", 11, WA), wgh)
        add("SHADOW-OFF BYTE IDENTITY: flags-OFF result == severed-path (pre-PR) result, byte for byte",
            json.dumps(r_off, sort_keys=True, default=str) == json.dumps(r_sev, sort_keys=True, default=str))
        add("wiring OFF: still zero gh calls + zero compat rows after the full OFF sequence",
            wgh.calls == [] and wgh.mb_calls == [] and spy_calls == [] and
            owner_rows(account, "SELECT count(*) FROM core.event WHERE kind='compat_finding' AND repo=%s",
                       (WREPO,))[0][0] == 0)
        # ON: same event, flags ON — the wiring fires (rows + reads + one counts line) and the output stays
        # byte-identical: shadow means zero customer-visible change even when ON.
        with flags(analysis="1", repos=WREPO):
            r_on, log_on = brain(db, wev("synchronize", 11, WA), wgh)
        wrows = owner_rows(account,
                           "SELECT commit_sha, counterparty_sha, detail, fact_class, detector FROM core.event "
                           "WHERE kind='compat_finding' AND repo=%s", (WREPO,))
        add("wiring ON: analysis fired through the real webhook flow (spy called; 3 file reads = shared "
            "baseline once + both heads; 2 merge-base lookups)",
            spy_calls == [1] and len(wgh.calls) == 3 and len(wgh.mb_calls) == 2
            and "compat shadow" in log_on)
        add("wiring ON: ONE compat_finding row — delta-derived producer, v2 reason vocabulary, S3a "
            "class + detector stamped through the REAL webhook flow",
            len(wrows) == 1 and wrows[0][0] == WA and wrows[0][1] == WS
            and wrows[0][2] == "contract_delta:required_arg_added"
            and wrows[0][3] == "contract_delta_observation"
            and wrows[0][4] == "python-call-compat/py-call-v1")
        add("wiring ON: check/comment output BYTE-IDENTICAL to the OFF run (shadow = zero render change)",
            json.dumps(r_on, sort_keys=True, default=str) == json.dumps(r_off, sort_keys=True, default=str))
    finally:
        WH.run_compat_shadow = real_run

    # ── (6) REASON VOCABULARY v2 is exhaustive: no breaking:/risky: row anywhere after every scenario ───
    legacy_reasons = owner_rows(account,
                                "SELECT count(*) FROM core.event WHERE kind='compat_finding' "
                                "AND (detail LIKE 'breaking:%%' OR detail LIKE 'risky:%%')")[0][0]
    add("vocabulary v2: ZERO breaking:*/risky:* def-diff rows across every scenario (the retired v1 "
        "reasons are never written by this lane)", legacy_reasons == 0)

    # ── S3a TAXONOMY SWEEP over the WHOLE ledger after every scenario: the class stamped on EVERY row is
    # exactly the one its reason family dictates (writer passes it explicitly — this proves no call site
    # missed the mapping), the detector stamp rides on every row, and the evidence class is stamped on
    # consumer_call_mismatch rows ONLY (observations and incompatibilities never conflate at the writer). ──
    mis_stamped = owner_rows(account, """
        SELECT count(*) FROM core.event WHERE kind='compat_finding' AND (
             (detail LIKE 'contract_delta:%%'          AND fact_class IS DISTINCT FROM 'contract_delta_observation')
          OR (detail = 'rebase_needed'                 AND fact_class IS DISTINCT FROM 'rebase_needed_observation')
          OR (detail = 'divergent_definitions'         AND fact_class IS DISTINCT FROM 'divergent_definition_observation')
          OR (detail LIKE 'consumer_call_mismatch:%%'  AND fact_class IS DISTINCT FROM 'evidence_backed_incompatibility')
          OR (fact_class = 'evidence_backed_incompatibility' AND detail NOT LIKE 'consumer_call_mismatch:%%')
          OR fact_class IS NULL
          OR detector IS DISTINCT FROM 'python-call-compat/py-call-v1')""")[0][0]
    n_ledger = owner_rows(account, "SELECT count(*) FROM core.event WHERE kind='compat_finding'")[0][0]
    add(f"S3a sweep: across ALL {n_ledger} recorded rows the explicit class matches its reason family, "
        "evidence_backed_incompatibility marks consumer_call_mismatch rows ONLY, and every row carries "
        "the python-call-compat/py-call-v1 detector stamp (zero mis-stamped rows)",
        n_ledger > 0 and mis_stamped == 0)

    # ── (10) CONTENT-FREE: sentinels appear NOWHERE — not in the ledger, not in any captured log ────────
    leaked_db = 0
    for tok in SENTINELS:
        pat = f"%{tok}%"
        leaked_db += owner_rows(
            account,
            "SELECT count(*) FROM core.event WHERE coalesce(detail,'') LIKE %s "
            "OR coalesce(fact_fingerprint,'') LIKE %s OR coalesce(path,'') LIKE %s "
            "OR coalesce(repo,'') LIKE %s OR coalesce(branch,'') LIKE %s",
            (pat, pat, pat, pat, pat))[0][0]
    add("content-free: no sentinel default/annotation/body/junk token in ANY event ledger column",
        leaked_db == 0)
    all_logs = "".join(ALL_LOGS)
    add("content-free: no sentinel token in ANY captured log output across every scenario",
        all(tok not in all_logs for tok in SENTINELS))

    # ── verdict ─────────────────────────────────────────────────────────────────────────────────────────
    subprocess.run(["dropdb", DB], capture_output=True, text=True)
    passed = sum(1 for _, ok in checks if ok)
    print(f"\n-- {passed}/{len(checks)} compat shadow-analysis assertions --")
    failed = [label for label, ok in checks if not ok]
    for label, ok in checks:
        print(("  [ok]   " if ok else "  [FAIL] ") + label)
    if failed:
        print("\nCOMPAT SHADOW ANALYSIS GATE: FAIL")
        return 1
    print("\nCOMPAT SHADOW ANALYSIS GATE: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never leak the scratch DB on an unexpected error
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
        print(f"\n[FAIL] unexpected error: {e}")
        print("COMPAT SHADOW ANALYSIS GATE: FAIL")
        sys.exit(1)
