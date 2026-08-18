#!/usr/bin/env python3
"""OWNER ACTIVATION-FUNNEL SURFACE gate (Issue #648 / docs/PRODUCT_ACTIVATION_FUNNEL.md).

"A GitHub App install is not activation." core.owner_activation_funnel_surface() (db/schema/95_owner.sql) is
the owner-only, content-free lens the weekly GTM review reads to see how far live installations get down the
activation path — install -> selected a repo (A2) -> first eligible PR traffic (A3) -> first non-Clear signal
(A5) -> repeat use within 7 days (A9) — the SAME owner-only cross-tenant model + lock as owner_cost_surface /
owner_compat_shadow_surface (live tenants enumerated via the no-RLS installation_account routing map, each
pinned + read inside its own FORCE-RLS wall, REVOKE-PUBLIC / GRANT-veripsa_app), a pure READ over the existing
installation_account / claim / event / repository_lifecycle_activation tables (NO new table). It never changes
the write gate, webhook routing, or any runtime decision. github-app/activation_report.py renders it.

Proves (real scratch DB, three tenants — two external + one dev-exempt owner — seeded through the REAL
recorders declare_claim_with_authority / record_warn_with_authority / record_collision_with_authority plus a
direct governed activation insert, the repository_offboarding-test seam):
  (1) ZEROS WHEN EMPTY: before any install/traffic the surface returns all-zero counts + null timestamps — a
      dormant funnel reads as zeros, never an error;
  (2) AGGREGATES CORRECT + CROSS-TENANT: exact A1 install/live/owner-vs-external split + A2/A3/A5/A9 stage
      counts + the warn-vs-serialize signal split + the A1->A3 latency aggregate + the A6/A7/A8 GitHub-only
      NULL sentinels;
  (3) CONTENT-FREE (strictly aggregate): the serialized output carries NO repo name, NO path, NO branch, NO
      SHA, NO PR number, NO fingerprint — and exactly the expected key set;
  (7) A4 FIRST PR CHECK PUBLISHED: a 'check_published' event recorded via record_check_published_with_authority
      increments a4_first_check, computes the install->check and first-PR->check latency aggregates, buckets the
      first-check signal, DEDUPS a same-head redelivery to one count, and EXCLUDES a stale-generation publish;
  (4) OWNER-ONLY: a buyer/tenant role (veripsa_demo_agent — inherits veripsa_writer, NOT veripsa_app) cannot
      execute it (no cross-tenant leak path);
  (5) BOUNDED: p_cap=1 visits ONE account, flags capped=true with the exact account_count, and keeps the A1
      install/split totals EXACT (they are counts over the no-RLS routing map, not the per-account scan);
  (6) RENDERER: activation_report.render_report is pure + never raises — None degrades to an honest one-line
      message, a real surface renders the A1..A9 labels (incl. the A4 first-PR-check row + detail block) + the
      "not DB-derivable (GitHub-only)" A6/A7/A8 note + the authoritative-console caveat, and the poison tokens
      stay absent from the text.

Run:  python3 tests/test_owner_activation_funnel_surface.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import psycopg2  # noqa: E402

import activation_report  # noqa: E402  (github-app/activation_report.py — the operator surface that renders it)
from _installation_fixture import seed_live_installation  # noqa: E402

# PROCESS-UNIQUE (parallel-safe) scratch DB — the compat-shadow / smoke.sh pattern.
DB = "veripsa_actfunnel_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
DSN_MIG = f"postgresql://veripsa_migrator@localhost/{DB}"

# Distinctive poison tokens: if ANY appears in the surface output, content leaked.
REPO1 = "acme/leakfunnel-zz9"          # EXT1's repo
REPO2 = "acme/leakfunnel2-zz9"         # EXT2's repo
PATH_A = "src/leakpath_a.py"
PATH_B = "src/leakpath_b.py"
PATH_C = "src/leakpath_c.py"
PATH_D = "src/leakpath_d.py"
PATH_X = "src/leakpath_x.py"
BRANCH = "main"
# A4 first-check head SHAs (hex, content-free) — poison tokens too: none may appear in the aggregate output.
SHA_E1 = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"   # EXT1's first-check head
SHA_E2 = "b2c3d4e5f60718293a4b5c6d7e8f90123456789a"   # EXT2's first-check head
SHA_OWN = "c3d4e5f60718293a4b5c6d7e8f90123456789abc"  # OWNER's (stale-generation) head
REPO_OWN = "acme/leakfunnelown-zz9"                    # OWNER's repo for the stale-generation exclusion test

# Three installations: two EXTERNAL + one on the DEV-EXEMPT default trio (owner/dogfood).
EXT1_INST, EXT1_ACCT = "990001", "ACCT-GH-990001"
EXT2_INST, EXT2_ACCT = "990002", "ACCT-GH-990002"
OWN_INST, OWN_ACCT = "42424242", "ACCT-GH-42424242"    # in core._dev_exempt_account_ids() default trio

EXPECTED_KEYS = {
    "installs_total", "installs_live", "live_accounts", "external_accounts", "owner_accounts",
    "a2_selected_repo", "a3_first_pr", "a5_first_signal", "a9_repeat_7d",
    "a4_first_check", "a4_signal_distribution", "a3_to_a4_conversion",
    "install_to_check_latency", "pr_event_to_check_latency",
    "signals", "a1_to_a3_latency", "github_only_stages",
    "first_activity_at", "last_activity_at",
    "account_count", "capped", "cap", "accounts_scanned",
    # HUMAN JUDGMENT (added deliberately, not incidentally). Both are strictly AGGREGATE: counts keyed by
    # the two closed enums, with the external counters computed inside each tenant's RLS wall. They carry
    # no account id, repo, path, PR ref, or SHA -- the leakage checks below run over them unchanged.
    "human_judgment", "workflow_action",
}

checks = []


def chk(c, label):
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    checks.append(bool(c))


def app_for(inst_id):
    """A connection that has ENTERED `inst_id` as the App (veripsa_app) — the live per-event shape."""
    conn = psycopg2.connect(DSN_APP)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (inst_id,))
        account = cur.fetchone()[0]

    def run(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    return run, account


def seed_activation(account, repository_id, repo):
    """Seed a currently-selected repository (A2) — the repository_offboarding-test governed-insert seam: pin the
    account so the activation table's FORCE-RLS WITH CHECK admits the row (no simple public recorder exists)."""
    conn = psycopg2.connect(DSN_MIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT set_config('core.current_account', %s, false)", (account,))
        cur.execute("INSERT INTO core.repository_lifecycle_activation(account_id,repository_id,repo,activated_at) "
                    "VALUES (%s,%s,%s, now())", (account, repository_id, repo))
    conn.close()


def seed_superseding_tombstone(account, repository_id, repo):
    """Record a SUPERSEDED lifecycle tombstone (repo deleted then recreated) whose superseded_at is in the FUTURE
    — the offboarding governed-insert seam. This raises core._repository_generation_boundary(repo) ABOVE any
    already-recorded event, so a pre-generation-boundary 'check_published' reads as STALE and is excluded (the
    same fence the surface applies to every claim/event). A superseded tombstone does NOT exclude the repo itself
    (that needs superseded_at IS NULL) — it only moves the generation boundary forward."""
    conn = psycopg2.connect(DSN_MIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT set_config('core.current_account', %s, false)", (account,))
        cur.execute("INSERT INTO core.repository_lifecycle_tombstone"
                    "(account_id,repository_id,repo,reason,tombstoned_at,lifecycle_received_at,superseded_at) "
                    "VALUES (%s,%s,%s,'repository_deleted', now(), now(), now() + interval '1 day')",
                    (account, repository_id, repo))
    conn.close()


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", (r.stdout + r.stderr)[-1500:])
        return 1

    # ── (1) ZEROS WHEN EMPTY: no installs / no traffic → all-zero counts + null timestamps ──────────────
    seed_reader, _ = app_for(EXT1_INST)   # a live-app connection to READ the owner surface (granted veripsa_app)
    z = _j(seed_reader("SELECT core.owner_activation_funnel_surface()"))
    # note: entering EXT1 just now created ONE live install → installs reflect it, but NO stage/traffic exists.
    chk(z.get("a2_selected_repo") == 0 and z.get("a3_first_pr") == 0 and z.get("a5_first_signal") == 0
        and z.get("a9_repeat_7d") == 0 and z.get("a4_first_check") == 0
        and z.get("signals") == {"warn": 0, "serialize": 0}
        and z.get("a4_signal_distribution", {}).get("clear") == 0
        and z.get("a4_signal_distribution", {}).get("publication_failures") is None
        and z.get("install_to_check_latency", {}).get("count_with_latency") == 0
        and z.get("pr_event_to_check_latency", {}).get("count_with_latency") == 0
        and z.get("first_activity_at") is None and z.get("last_activity_at") is None
        and z.get("a1_to_a3_latency", {}).get("count_with_latency") == 0,
        f"dormant funnel → all-zero stage counts + null timestamps (a2={z.get('a2_selected_repo')} a4={z.get('a4_first_check')})")
    chk(set(z.keys()) == EXPECTED_KEYS, f"empty output carries exactly the expected key set ({sorted(z.keys())})")

    # ── seed a three-tenant funnel through the REAL recorders ──────────────────────────────────────────
    acct_ext1 = seed_live_installation(DSN_APP, DSN_MIG, int(EXT1_INST), int(EXT1_INST))
    acct_ext2 = seed_live_installation(DSN_APP, DSN_MIG, int(EXT2_INST), int(EXT2_INST))
    acct_own = seed_live_installation(DSN_APP, DSN_MIG, int(OWN_INST), int(OWN_INST))
    chk(acct_ext1 == EXT1_ACCT and acct_ext2 == EXT2_ACCT and acct_own == OWN_ACCT,
        f"three installations route to three distinct accounts (ext1={acct_ext1} ext2={acct_ext2} own={acct_own})")

    app1, _ = app_for(EXT1_INST)
    app2, _ = app_for(EXT2_INST)
    # EXT1 (external, full funnel): two PR-claims (A3 + A9 repeat) + one warn + one serialize (A5) on REPO1.
    c1 = app1("SELECT core.declare_claim_with_authority(%s,%s,%s,%s)", ("PR-1:" + PATH_A, PATH_A, REPO1, BRANCH))
    c2 = app1("SELECT core.declare_claim_with_authority(%s,%s,%s,%s)", ("PR-2:" + PATH_B, PATH_B, REPO1, BRANCH))
    w = app1("SELECT core.record_warn_with_authority(%s,%s,%s,%s)", (PATH_C, REPO1, BRANCH, "PR-3"))
    app1("SELECT core.declare_claim_with_authority(%s,%s,%s,%s)", ("PR-4:" + PATH_D, PATH_D, REPO1, BRANCH))
    coll = app1("SELECT core.record_collision_with_authority(%s,%s,%s)", (PATH_D, REPO1, BRANCH))
    # EXT2 (external, partial): ONE PR-claim on REPO2 (A3 only; no signal, no repeat).
    cx = app2("SELECT core.declare_claim_with_authority(%s,%s,%s,%s)", ("PR-1:" + PATH_X, PATH_X, REPO2, BRANCH))
    # A2: EXT1 + EXT2 have a selected repo; OWNER stays install-only (so A2 < A1 and the split is exercised).
    seed_activation(acct_ext1, "5001", REPO1)
    seed_activation(acct_ext2, "5002", REPO2)
    chk(_j(c1).get("change_id") == "PR-1" and _j(c2).get("change_id") == "PR-2" and _j(cx).get("change_id") == "PR-1"
        and isinstance(w, str) and w.startswith("EV-WARN-") and isinstance(coll, str) and coll.startswith("EV-"),
        "seeded PR-claims (A3/A9) + one warn + one serialize (A5) via the real recorders")

    # ── (2) AGGREGATES CORRECT + CROSS-TENANT ───────────────────────────────────────────────────────────
    s = _j(app1("SELECT core.owner_activation_funnel_surface()"))
    chk(s.get("installs_total") == 3 and s.get("installs_live") == 3 and s.get("live_accounts") == 3,
        f"A1: installs_total/live=3, live tenants=3 (got {s.get('installs_total')}/{s.get('installs_live')}/"
        f"{s.get('live_accounts')})")
    chk(s.get("external_accounts") == 2 and s.get("owner_accounts") == 1,
        f"A1 split: external=2, owner/dogfood=1 via the dev-exempt allowlist (got ext={s.get('external_accounts')} "
        f"own={s.get('owner_accounts')})")
    chk(s.get("a2_selected_repo") == 2,
        f"A2: 2 tenants selected >=1 repo (OWNER install-only excluded; got {s.get('a2_selected_repo')})")
    chk(s.get("a3_first_pr") == 2,
        f"A3: 2 tenants reached first eligible PR traffic (PR-claim proxy; got {s.get('a3_first_pr')})")
    chk(s.get("a5_first_signal") == 1,
        f"A5: 1 tenant saw a first non-Clear signal (only EXT1 has warn/serialize; got {s.get('a5_first_signal')})")
    chk(s.get("signals") == {"warn": 1, "serialize": 1},
        f"A5 split: warn=1, serialize=1 across the fleet (got {s.get('signals')})")
    chk(s.get("a9_repeat_7d") == 1,
        f"A9: 1 tenant showed repeat use within 7 days (EXT1's second PR-claim; got {s.get('a9_repeat_7d')})")
    lat = s.get("a1_to_a3_latency") or {}
    buckets = lat.get("buckets") or {}
    bsum = sum(int(buckets.get(k, 0)) for k in ("under_1h", "1h_to_1d", "1d_to_7d", "over_7d"))
    chk(lat.get("count_with_latency") == 2 and bsum == 2
        and isinstance(lat.get("median_seconds"), int) and lat.get("median_seconds") >= 0,
        f"A1->A3 latency: 2 installs with a first PR, buckets sum to the count, median a non-negative int "
        f"(got count={lat.get('count_with_latency')} bsum={bsum} median={lat.get('median_seconds')})")
    gh = s.get("github_only_stages") or {}
    chk(gh.get("note") == "not DB-derivable (GitHub-only)"
        and "a4_check_created" not in gh and gh.get("a6_pr_comment") is None
        and gh.get("a7_first_ack") is None and gh.get("a8_required_check") is None,
        "A6/A7/A8 are marked not-DB-derivable (GitHub-only) with NULL sentinels — never fabricated; A4 moved out")
    # A4 has no traffic yet (no check recorded) — DB-derivable stage present with zero count + null failures.
    chk(s.get("a4_first_check") == 0
        and s.get("a3_to_a4_conversion") == {"a3": 2, "a4": 0}
        and (s.get("a4_signal_distribution") or {}).get("publication_failures") is None,
        f"A4 present + zero before any check recorded (a4={s.get('a4_first_check')} conv={s.get('a3_to_a4_conversion')})")
    first, last = s.get("first_activity_at"), s.get("last_activity_at")
    chk(bool(first) and bool(last) and str(first) <= str(last),
        f"first/last activity timestamps present and ordered ({first} <= {last})")
    chk(s.get("account_count") == 3 and s.get("accounts_scanned") == 3 and s.get("capped") is False,
        f"bound honesty at default cap: 3 tenants, all scanned, not capped "
        f"(count={s.get('account_count')} scanned={s.get('accounts_scanned')} capped={s.get('capped')})")

    # ── (3) CONTENT-FREE — strictly aggregate: no repo/path/branch/SHA/PR-number leakage ────────────────
    blob = json.dumps(s)
    poisons = {
        "repo1": "leakfunnel-zz9", "repo2": "leakfunnel2-zz9", "org prefix": "acme/",
        "path prefix": "src/leakpath", "path a": PATH_A, "path x": PATH_X,
        "pr number": "PR-1", "pr number 2": "PR-4",
    }
    leaked = [name for name, tok in poisons.items() if tok in blob]
    chk(not leaked, f"NO leakage in the surface output — repos/paths/PR-numbers absent (leaked: {leaked or 'none'})")
    chk(set(s.keys()) == EXPECTED_KEYS,
        "output carries EXACTLY the aggregate key set (no per-tenant / per-repo / per-PR rows)")

    # ── (4) OWNER-ONLY: a buyer/tenant role cannot execute the lens ─────────────────────────────────────
    tconn = psycopg2.connect(f"postgresql://veripsa_demo_agent@localhost/{DB}")
    tconn.autocommit = True
    refused = False
    try:
        with tconn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute("SELECT core.owner_activation_funnel_surface()")
    except psycopg2.errors.InsufficientPrivilege:
        refused = True
    except Exception as e:
        refused = "permission denied" in str(e).lower()
    chk(refused, "OWNER-ONLY: a buyer/tenant role (veripsa_demo_agent) cannot execute the cross-tenant lens")

    # ── (5) BOUNDED: p_cap=1 visits one account; A1 totals stay EXACT (routing-map counts, not the scan) ─
    b = _j(app1("SELECT core.owner_activation_funnel_surface(1)"))
    # deterministic account_id order → the alphabetically-first account (OWNER 'ACCT-GH-2...') is the scanned one.
    chk(b.get("accounts_scanned") == 1 and b.get("capped") is True and b.get("account_count") == 3,
        f"p_cap=1 → one tenant scanned, capped=true, account_count=3 "
        f"(scanned={b.get('accounts_scanned')} capped={b.get('capped')} count={b.get('account_count')})")
    chk(b.get("installs_total") == 3 and b.get("installs_live") == 3
        and b.get("external_accounts") == 2 and b.get("owner_accounts") == 1,
        "p_cap=1 keeps the A1 install/owner-external totals EXACT (counted over the no-RLS routing map, "
        "not the bounded per-tenant scan)")
    chk(b.get("a2_selected_repo") == 0 and b.get("a3_first_pr") == 0,
        f"p_cap=1 A2..A9 cover the scanned set only (OWNER first, install-only → 0; "
        f"got a2={b.get('a2_selected_repo')} a3={b.get('a3_first_pr')})")

    # ── (6) RENDERER (pure, no DB beyond the dict already fetched): never raises, honest message on None ─
    txt_none = activation_report.render_report(None)
    chk(isinstance(txt_none, str) and "no activation surface available yet" in txt_none,
        "render_report(None) = an honest one-line message (no traceback)")
    txt = activation_report.render_report(s)
    chk("ACTIVATION FUNNEL" in txt and "A2  selected >=1 repository" in txt
        and "A4  first PR check published" in txt and "A3 -> A4 conversion" in txt
        and "not DB-derivable (GitHub-only)" in txt and "AUTHORITATIVE" in txt.upper()
        and "warn (Heads up)" in txt,
        "render_report(surface) renders the A1..A9 labels (incl. the A4 first-check row/detail) + the GitHub-only "
        "note + the authoritative-console caveat + the signal split")
    chk(not any(tok in txt for tok in poisons.values()),
        "rendered text carries NO poison token (repos/paths/PR-numbers absent)")
    try:
        junk = activation_report.render_report({"signals": "x", "a1_to_a3_latency": ["not", "a", "dict"],
                                                "installs_total": "?", "a2_selected_repo": None})
        ok_junk = isinstance(junk, str) and "ACTIVATION FUNNEL" in junk
    except Exception:
        ok_junk = False
    chk(ok_junk, "render_report never raises on a junk-shaped surface")

    # ── (7) A4 FIRST PR CHECK PUBLISHED — the 'check_published' recorder + surface wiring (Issue #648) ──────
    # A failed/skipped post NEVER calls record_check_published_with_authority (the webhook handler gates on
    # posted=True), so at the DB level A4 moves ONLY when a check was actually recorded — proven by a4 tracking
    # our explicit recorder calls exactly (0 above → 2 here → 3 → back to 2 after the stale-generation fence).
    r1 = app1("SELECT core.record_check_published_with_authority(%s,%s,%s,%s,%s)",
              ("PR-1", REPO1, BRANCH, SHA_E1, "clear"))
    # DUPLICATE (same account/repo/head_sha, different signal) — GitHub redelivers + neighbor/rerun re-post on the
    # same head: it must DEDUP to one row (the first 'clear' wins), never a second A4 count.
    r1b = app1("SELECT core.record_check_published_with_authority(%s,%s,%s,%s,%s)",
               ("PR-1", REPO1, BRANCH, SHA_E1, "heads_up"))
    r2 = app2("SELECT core.record_check_published_with_authority(%s,%s,%s,%s,%s)",
              ("PR-1", REPO2, BRANCH, SHA_E2, "wait_in_line"))
    chk(isinstance(r1, str) and r1.startswith("EV-CHKPUB-") and r1b == r1
        and isinstance(r2, str) and r2.startswith("EV-CHKPUB-") and r2 != r1,
        f"recorder returns a deterministic per-head id; the duplicate call returns the SAME id (dedup) "
        f"(r1={r1} r1b={r1b} r2={r2})")

    s4 = _j(app1("SELECT core.owner_activation_funnel_surface()"))
    chk(s4.get("a4_first_check") == 2,
        f"A4: 2 tenants got a first PR check published; the duplicate deduped (got {s4.get('a4_first_check')})")
    sig4 = s4.get("a4_signal_distribution") or {}
    chk(sig4.get("clear") == 1 and sig4.get("wait_in_line") == 1 and sig4.get("heads_up") == 0
        and sig4.get("unknown") == 0 and sig4.get("paused") == 0 and sig4.get("publication_failures") is None,
        f"A4 signal split: the FIRST-recorded signal wins per head (clear=1, wait_in_line=1); dedup did not "
        f"flip EXT1 to heads_up; publication_failures NULL (got {sig4})")
    chk(s4.get("a3_to_a4_conversion") == {"a3": 2, "a4": 2},
        f"A3 -> A4 conversion object carries both stage counts (got {s4.get('a3_to_a4_conversion')})")
    ic4 = s4.get("install_to_check_latency") or {}
    pc4 = s4.get("pr_event_to_check_latency") or {}
    chk(ic4.get("count_with_latency") == 2 and isinstance(ic4.get("median_seconds"), int)
        and ic4.get("median_seconds") >= 0,
        f"install -> first check latency computes for both installs (n={ic4.get('count_with_latency')} "
        f"median={ic4.get('median_seconds')})")
    chk(pc4.get("count_with_latency") == 2 and isinstance(pc4.get("median_seconds"), int)
        and pc4.get("median_seconds") >= 0,
        f"first-PR -> first check latency computes for both PR-active installs (n={pc4.get('count_with_latency')} "
        f"median={pc4.get('median_seconds')})")

    # CONTENT-FREE on the post-A4 surface: the head SHAs (and every earlier poison) stay absent from the output.
    blob4 = json.dumps(s4)
    leaked4 = [tok for tok in (SHA_E1, SHA_E2, "leakfunnel", "src/leakpath", "PR-1") if tok in blob4]
    chk(not leaked4 and set(s4.keys()) == EXPECTED_KEYS,
        f"A4 surface stays strictly aggregate — no head SHA / repo / path / PR-ref leakage (leaked: {leaked4 or 'none'})")

    # STALE-GENERATION EXCLUSION: a 'check_published' recorded BEFORE the repo's generation boundary is excluded,
    # exactly as A3/A5. Record OWNER's check (a4 -> 3), then supersede REPO_OWN's generation past it (a4 -> 2).
    seed_activation(acct_own, "5003", REPO_OWN)   # OWNER selects REPO_OWN so it is a real current repo
    ro = app_for(OWN_INST)[0]
    ro("SELECT core.record_check_published_with_authority(%s,%s,%s,%s,%s)",
       ("PR-9", REPO_OWN, BRANCH, SHA_OWN, "unknown"))
    s5 = _j(app1("SELECT core.owner_activation_funnel_surface()"))
    chk(s5.get("a4_first_check") == 3 and (s5.get("a4_signal_distribution") or {}).get("unknown") == 1,
        f"A4 counts OWNER's fresh check before any generation fence (a4={s5.get('a4_first_check')})")
    seed_superseding_tombstone(acct_own, "5003", REPO_OWN)   # repo deleted+recreated → boundary jumps past the event
    s6 = _j(app1("SELECT core.owner_activation_funnel_surface()"))
    chk(s6.get("a4_first_check") == 2 and (s6.get("a4_signal_distribution") or {}).get("unknown") == 0,
        f"A4 EXCLUDES the stale-generation check once the boundary moves past it (a4={s6.get('a4_first_check')})")

    # RENDERER on a NON-zero A4 surface: the funnel row + detail block render; no poison token leaks.
    txt4 = activation_report.render_report(s4)
    chk("A4  first PR check published" in txt4 and "First-check signal split" in txt4
        and "Install -> first check" in txt4
        and not any(tok in txt4 for tok in (SHA_E1, SHA_E2, "leakfunnel", "src/leakpath")),
        "render_report on a populated A4 surface renders the row + detail block, content-free")

    print("OWNER-ACTIVATION-FUNNEL GATE:", "PASS" if all(checks) else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
