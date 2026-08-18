#!/usr/bin/env python3
"""CROSS-TENANT CONSENT RED-TEAM GATE — the moat bar for the cross-repo CONSENT layer (db/schema/75_workspace.sql).

The route-tier cross-repo substrate (core._cross_repo_adjacency) is SAME-ACCOUNT-only. This layer lets the SAME
contract-key signal cross between repos owned by DIFFERENT tenants — but ONLY behind BILATERAL workspace consent,
content-free, RLS-safe. THIS is the highest-stakes surface in the schema: a sloppy cross-tenant read leaks repo
B's graph to repo A and kills the content-free / FORCE-RLS moat. So this gate is a 2-TENANT FORCE-RLS PROOF — not
"looks isolated", but every concrete leak attempt RUN against a live 2-tenant fixture and proven to FAIL.

THE FIXTURE: two REAL tenants, DEMO (account ACCT-DEMO) and ACME (account ACCT-ACME), each with its OWN graph in
its OWN repo coordinate. DEMO's repo DEFINES a contract route (`alters route::workspaces/{}/projects`); ACME's
repo CONSUMES it (`queries route::workspaces/{}/projects`). So a real cross-repo contract EXISTS between them —
the signal the layer would surface IF (and only if) both consent. There is NO shared account, NO shared repo.

THE PROBES (each is "the breach is BLOCKED" / "consent gates exactly", proven live):

  R0  FIXTURE VALIDITY — DEMO sees only DEMO's contract edge, ACME only ACME's (the 2-tenant base isolation), and
      the cross-repo contract really does exist (so the WITH-consent probe is not vacuous).

  R1  FORCE-RLS on the consent tables — a migrator (table OWNER) with NO pin sees ZERO workspace_member rows; a
      migrator PINNED to A sees ONLY A's membership, NEVER B's. (The moat baseline the whole layer rests on: an
      owner can't read across; a tenant certainly can't.)

  R2  WITHOUT consent → the cross-tenant read returns NOTHING. Even though the contract genuinely couples the two
      repos, with no accepted workspace_member pair core.cross_tenant_contract_surface(A..,B..) is EMPTY — the
      relaxed adjacency is never reached, so nothing about B crosses.

  R3  FORGED GUC does NOT leak — a session forges `SET veripsa.cross_repo=1` (the SAME-ACCOUNT kill switch) AND a
      forged `SET core.current_account=<other tenant>`, then calls the cross read. Still EMPTY: this read is gated
      on a CONSENT FACT, never a GUC, so a forged GUC buys nothing.

  R3b FORGED workspace_member ROW does NOT leak — an attacker tenant tries to INSERT a workspace_member row for
      the OTHER account (to fabricate the other side's consent). RLS WITH CHECK (account_id = the pinned account)
      REFUSES it (and the governed-write forgery gate refuses a direct insert outright). It can never write Y's
      consent, so it can never manufacture a bilateral pair.

  R4  ONE-SIDED consent → ZERO cross-read. Only A accepts (B never does). The bilateral check fails → EMPTY. (The
      core distinction from a unilateral "I want to see your repo" — both owners must independently opt in.)

  R5  WITH BILATERAL consent → ONLY the contract-key "consumed-by" FACT crosses, NEVER a path/node/edge/graph of
      B. Both accept; the read returns the shared KEY + DIRECTION + the CONSUMER repo NAME + COUNTS — and the
      gate asserts B's actual file PATH (`acme/...`) is NOWHERE in the output, and the row shape carries no path
      column at all. Content-free by construction.

  R6  REVOCATION is immediate — after B revokes, the cross read is EMPTY again (consent is a live, revocable
      fact, not a one-way door).

  R7  A buyer SEAT cannot reach the cross-tenant read AT ALL — veripsa_writer / a demo agent (a real connecting
      seat role) calling core.cross_tenant_contract_surface gets `permission denied` (REVOKE-from-PUBLIC + GRANT
      only to veripsa_app). Same lock as owner_cost_surface. The internal mutual-consent fn is likewise denied to
      a seat. (Belt: even the host App, which CAN call it, gets EMPTY without a real consent fact — R2/R4 cover that.)

A GRANT-layer denial must say exactly "permission denied" — a generic/syntax error must NOT masquerade as a
security denial. Run live against the catalog + the gate fns (peer auth on localhost); the only truth about who
can read what is the running database. Prints `CROSS-TENANT CONSENT RED-TEAM: PASS` / `... FAIL`.

Run:  python3 tests/test_cross_tenant_consent.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# PROCESS-UNIQUE (parallel-safe) scratch DB — the gate bootstraps + drops it, so a fixed name would let
# concurrent runs drop each other's DB mid-run. Per-PID, like db/smoke.sh / test_security_perimeter.py.
DB = "veripsa_xtenant_" + str(os.getpid())

A_ACCT, B_ACCT = "ACCT-DEMO", "ACCT-ACME"
A_REPO, B_REPO = "demoorg/backend", "acmeorg/frontend"
# the cross-repo-stable contract key DEMO defines + ACME consumes (the mount-prefix-bridged suffix key shape).
KEY = "route::workspaces/{}/projects"
COLLISION_DISPLAY = "route::contract x"
COLLISION_RAW_A = "route::contract<x>"
COLLISION_RAW_B = "route::contract x"
# B's REAL file path — the thing that must NEVER appear in any cross-tenant output (the leak canary).
B_SECRET_PATH = "acme/web/services/project.service.ts"
A_DEF_PATH = "demo/app/urls/workspace.py"

DENIED = "permission denied"  # the exact GRANT-layer denial string (a generic error must not pass for this)

checks = []  # (label, passed, detail)


def add(label, passed, detail=""):
    checks.append((label, bool(passed), detail))


def psql_mig(sql):
    dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def psql_as(role, sql):
    """Run sql AS `role` (peer auth on localhost), stdout+stderr combined (so a permission-denied is visible)."""
    dsn = f"postgresql://{role}@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def last_value(out):
    lines = [ln for ln in out.splitlines() if ln.strip() != ""]
    return lines[-1].strip() if lines else ""


def bootstrap():
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)
    # a SECOND tenant ACCT-ACME so veripsa_acme_agent resolves to its own account (cross-tenant isolation).
    psql_mig("SET search_path=core; "
             "SELECT core.provision_seat('ACCT-ACME','Acme Co','AG-ACME','acme','veripsa_acme_agent');")
    # seed each tenant's contract edge DIRECTLY (governed triggers disabled in a txn), each under its OWN pin —
    # the same seeding shape as tests/test_cross_repo_shadow.py / test_dampened_res_scale.py. DEMO defines the
    # route; ACME consumes it. The two land in two DIFFERENT accounts (no shared account, no shared repo).
    _seed(A_ACCT, A_REPO, A_DEF_PATH, KEY, "alters")
    _seed(B_ACCT, B_REPO, B_SECRET_PATH, KEY, "queries")


def _seed(acct, repo, src, dst, kind, semantic_raw=None):
    semantic = (
        "NULL"
        if semantic_raw is None
        else f"core._semantic_ref_key('{semantic_raw}')"
    )
    psql_mig(
        "SET search_path=core; "
        f"SET core.current_account='{acct}'; "
        "ALTER TABLE core.code_edge DISABLE TRIGGER trg_governed_code_edge; "
        "INSERT INTO core.code_edge("
        "account_id,repo,branch,src,dst,edge_kind,semantic_dst_key"
        ") "
        f"VALUES ('{acct}','{repo}','main','{src}','{dst}','{kind}',"
        f"{semantic}); "
        "ALTER TABLE core.code_edge ENABLE TRIGGER trg_governed_code_edge;")


def _xread_as(role, a_acct, a_repo, b_acct, b_repo, extra_set=""):
    """Call the cross-tenant read AS `role`. extra_set lets a probe forge GUCs first. Returns raw output."""
    return psql_as(role,
        "SET search_path=core,pg_catalog; " + extra_set +
        f"SELECT shared_key||'|'||dir||'|'||consumer_repo||'|'||producer_files||'|'||consumer_files "
        f"FROM core.cross_tenant_contract_surface('{a_acct}','{a_repo}','main','{b_acct}','{b_repo}','main');")


def _accept(acct, ws, repo):
    """Record an ACCEPTED workspace_member row for `acct`/`repo` in `ws` (governed write under acct's pin)."""
    psql_mig(
        "SET search_path=core; "
        f"SET core.current_account='{acct}'; "
        "SELECT core.mark_governed_write('workspace_member'); "
        "INSERT INTO core.workspace_member(workspace_id,account_id,repo,branch,consent_state,consented_at) "
        f"VALUES ('{ws}','{acct}','{repo}','main','accepted',now()) "
        "ON CONFLICT (workspace_id,account_id,repo) DO UPDATE SET consent_state='accepted',consented_at=now();")


def _revoke(acct, ws, repo):
    psql_mig(
        "SET search_path=core; "
        f"SET core.current_account='{acct}'; "
        "SELECT core.mark_governed_write('workspace_member'); "
        f"UPDATE core.workspace_member SET consent_state='revoked' "
        f"WHERE workspace_id='{ws}' AND account_id='{acct}' AND repo='{repo}';")


def _make_workspace(creator, ws):
    psql_mig(
        "SET search_path=core; "
        f"SET core.current_account='{creator}'; "
        "SELECT core.mark_governed_write('workspace'); "
        f"INSERT INTO core.workspace(workspace_id,created_by_account,state) VALUES ('{ws}','{creator}','active') "
        "ON CONFLICT DO NOTHING;")


def _rows(out):
    """Parse the piped 'k|dir|repo|p|c' result lines (skip the SET/INSERT ack lines)."""
    return [ln.strip() for ln in out.splitlines() if "|" in ln and "route::" in ln]


def drop():
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


def main():
    print("VERIPSA CROSS-TENANT CONSENT RED-TEAM — 2-tenant FORCE-RLS proof (the cross-repo moat bar)")
    print(f"(scratch DB: {DB})")
    bootstrap()

    # ── R0: FIXTURE VALIDITY — two isolated tenants, each seeing only its own contract edge; the contract exists.
    a_sees_a = last_value(psql_mig(
        f"SET search_path=core; SET core.current_account='{A_ACCT}'; "
        f"SELECT count(*) FROM core.code_edge WHERE dst='{KEY}' AND repo='{A_REPO}';"))
    a_sees_b = last_value(psql_mig(
        f"SET search_path=core; SET core.current_account='{A_ACCT}'; "
        f"SELECT count(*) FROM core.code_edge WHERE repo='{B_REPO}';"))
    b_sees_b = last_value(psql_mig(
        f"SET search_path=core; SET core.current_account='{B_ACCT}'; "
        f"SELECT count(*) FROM core.code_edge WHERE dst='{KEY}' AND repo='{B_REPO}';"))
    add("R0 FIXTURE: DEMO sees its own contract edge, NONE of ACME's repo (2-tenant base isolation)",
        a_sees_a == "1" and a_sees_b == "0", f"a_sees_a={a_sees_a} a_sees_b={a_sees_b}")
    add("R0 FIXTURE: ACME sees its own consuming edge (the cross-repo contract genuinely exists)",
        b_sees_b == "1", f"b_sees_b={b_sees_b}")

    # ── R1: FORCE ROW LEVEL SECURITY on the consent tables — the owner can't read across without a pin.
    _make_workspace(A_ACCT, "WS-R1")
    _accept(A_ACCT, "WS-R1", A_REPO)
    _accept(B_ACCT, "WS-R1", B_REPO)
    nopin = last_value(psql_mig("SET search_path=core; SELECT count(*) FROM core.workspace_member;"))
    pin_a_total = last_value(psql_mig(
        f"SET search_path=core; SET core.current_account='{A_ACCT}'; SELECT count(*) FROM core.workspace_member;"))
    pin_a_sees_b = last_value(psql_mig(
        f"SET search_path=core; SET core.current_account='{A_ACCT}'; "
        f"SELECT count(*) FROM core.workspace_member WHERE account_id='{B_ACCT}';"))
    add("R1 FORCE-RLS: a migrator (table OWNER) with NO pin sees ZERO workspace_member rows",
        nopin == "0", f"nopin={nopin}")
    add("R1 FORCE-RLS: a migrator PINNED to A sees ONLY A's membership, NEVER B's",
        pin_a_total == "1" and pin_a_sees_b == "0", f"pin_a_total={pin_a_total} pin_a_sees_b={pin_a_sees_b}")

    # ── R2: WITHOUT consent → cross read EMPTY. (Tear the R1 consent down first so this is a true no-consent state.)
    _revoke(A_ACCT, "WS-R1", A_REPO)
    _revoke(B_ACCT, "WS-R1", B_REPO)
    out = _xread_as("veripsa_app", A_ACCT, A_REPO, B_ACCT, B_REPO)
    add("R2 NO consent: core.cross_tenant_contract_surface returns NOTHING (the contract exists but no consent)",
        _rows(out) == [], f"rows={_rows(out)}")

    # ── R3: FORGED GUC does not leak — forge the same-account kill switch + a victim current_account, still EMPTY.
    out = _xread_as("veripsa_app", A_ACCT, A_REPO, B_ACCT, B_REPO,
                    extra_set=f"SET veripsa.cross_repo='1'; SET core.current_account='{B_ACCT}'; ")
    add("R3 FORGED GUC: SET veripsa.cross_repo=1 + forged current_account leaks NOTHING (consent-gated, not GUC)",
        _rows(out) == [], f"rows={_rows(out)}")

    # ── R3b: FORGED workspace_member row for the OTHER account is REFUSED (RLS WITH CHECK + governed-write gate).
    # As the App pinned to A, try to write B's consent row directly (both: without the token, and with a forged token).
    forge_direct = psql_as("veripsa_app",
        "SET search_path=core; "
        f"SET core.current_account='{A_ACCT}'; "
        "INSERT INTO core.workspace_member(workspace_id,account_id,repo,consent_state) "
        f"VALUES ('WS-FORGE','{B_ACCT}','{B_REPO}','accepted');")
    forge_token = psql_as("veripsa_app",
        "SET search_path=core; "
        f"SET core.current_account='{A_ACCT}'; "
        "SELECT core.mark_governed_write('workspace_member'); "
        "INSERT INTO core.workspace_member(workspace_id,account_id,repo,consent_state) "
        f"VALUES ('WS-FORGE','{B_ACCT}','{B_REPO}','accepted');")
    # the row must NOT exist for B under any pin (RLS WITH CHECK rejects account_id<>pinned; forgery gate rejects
    # the un-tokened insert). Count under B's own pin.
    forged_present = last_value(psql_mig(
        f"SET search_path=core; SET core.current_account='{B_ACCT}'; "
        f"SELECT count(*) FROM core.workspace_member WHERE workspace_id='WS-FORGE';"))
    add("R3b FORGED ROW: a tenant CANNOT INSERT a workspace_member row for the OTHER account (RLS WITH CHECK)",
        forged_present == "0"
        and ("denied" in forge_direct.lower() or "forgery" in forge_direct.lower()
             or "row-level security" in forge_direct.lower() or "violates" in forge_direct.lower()),
        f"present={forged_present} direct_err={last_value(forge_direct)!r} token_err={last_value(forge_token)!r}")

    # ── R3c: FORGE-VIA-SETTER — the DEMO seat calls the REAL consent setter, naming the OTHER tenant's repo, to
    # try to fabricate that side's consent. The setter takes identity from the CONNECTION (never an arg), so the
    # row binds to the CALLER's account (ACCT-DEMO) — ACCT-ACME gets NOTHING. So a tenant can never manufacture
    # the other side of a bilateral pair even through the sanctioned write path.
    ws_demo = last_value(psql_as("veripsa_demo_agent",
        "SET search_path=core,pg_catalog; "
        f"SELECT core.create_workspace_with_authority('{A_REPO}','main');"))
    psql_as("veripsa_demo_agent",
        "SET search_path=core,pg_catalog; "
        f"SELECT core.open_repo_for_consent_with_authority('{ws_demo}','{B_REPO}','main');")
    demo_owns = last_value(psql_mig(
        f"SET search_path=core; SET core.current_account='{A_ACCT}'; "
        f"SELECT count(*) FROM core.workspace_member WHERE workspace_id='{ws_demo}' AND repo='{B_REPO}';"))
    acme_owns = last_value(psql_mig(
        f"SET search_path=core; SET core.current_account='{B_ACCT}'; "
        f"SELECT count(*) FROM core.workspace_member WHERE workspace_id='{ws_demo}' AND repo='{B_REPO}';"))
    add("R3c FORGE-VIA-SETTER: a tenant consenting the OTHER tenant's repo binds the row to ITSELF, NEVER to the "
        "other account (identity from the connection — the bilateral pair cannot be one-sidedly manufactured)",
        demo_owns == "1" and acme_owns == "0", f"demo_owns={demo_owns} acme_owns={acme_owns}")

    # ── R4: ONE-SIDED consent → ZERO cross-read. Only A accepts (B does not).
    _make_workspace(A_ACCT, "WS-R4")
    _accept(A_ACCT, "WS-R4", A_REPO)   # ONLY A
    out = _xread_as("veripsa_app", A_ACCT, A_REPO, B_ACCT, B_REPO)
    add("R4 ONE-SIDED: only A accepted → cross read EMPTY (bilateral consent required, not unilateral)",
        _rows(out) == [], f"rows={_rows(out)}")

    # ── R5: WITH BILATERAL consent → ONLY the contract-key fact + counts cross, NEVER B's path/graph.
    _accept(B_ACCT, "WS-R4", B_REPO)   # now BOTH accepted
    out = _xread_as("veripsa_app", A_ACCT, A_REPO, B_ACCT, B_REPO)
    rows = _rows(out)
    got_key = rows and rows[0].split("|")[0] == KEY
    consumer_repo_ok = rows and rows[0].split("|")[2] == B_REPO  # the CONSUMER repo NAME (allowed: a name, not a path)
    add("R5 BILATERAL: the shared contract-key 'consumed-by' FACT crosses (key + dir + consumer repo + counts)",
        bool(got_key) and bool(consumer_repo_ok), f"rows={rows}")
    # THE LEAK CANARY: B's actual file PATH must appear NOWHERE in the cross-tenant output.
    leaked_path = (B_SECRET_PATH in out) or ("acme/web" in out) or (".service.ts" in out)
    add("R5 CONTENT-FREE: B's file PATH (acme/...service.ts) appears NOWHERE in the cross-tenant output",
        not leaked_path, f"output_tail={out[-300:]!r}")
    # the row shape itself carries NO path column — 5 fields: key|dir|consumer_repo|producer_files|consumer_files.
    shape_ok = rows and len(rows[0].split("|")) == 5 and rows[0].split("|")[3].isdigit()
    add("R5 SHAPE: the returned row is COUNTS-only (key|dir|consumer_repo|producer_files|consumer_files), no path",
        bool(shape_ok), f"row={rows[0] if rows else None!r}")

    # R5b: two exact contract keys can sanitize to the same display token.
    # They must remain separate count facts, with a deterministic safe digest
    # suffix, rather than being merged by GROUP BY on the display value.
    _seed(
        A_ACCT, A_REPO, "demo/contract-angle.py", COLLISION_DISPLAY,
        "alters", semantic_raw=COLLISION_RAW_A,
    )
    _seed(
        B_ACCT, B_REPO, "acme/contract-angle.ts", COLLISION_DISPLAY,
        "queries", semantic_raw=COLLISION_RAW_A,
    )
    _seed(
        A_ACCT, A_REPO, "demo/contract-space.py", COLLISION_DISPLAY,
        "alters", semantic_raw=COLLISION_RAW_B,
    )
    _seed(
        B_ACCT, B_REPO, "acme/contract-space-a.ts", COLLISION_DISPLAY,
        "queries", semantic_raw=COLLISION_RAW_B,
    )
    _seed(
        B_ACCT, B_REPO, "acme/contract-space-b.ts", COLLISION_DISPLAY,
        "queries", semantic_raw=COLLISION_RAW_B,
    )
    collision_out = _xread_as(
        "veripsa_app", A_ACCT, A_REPO, B_ACCT, B_REPO
    )
    collision_rows = [
        row for row in _rows(collision_out)
        if row.split("|")[0].startswith(COLLISION_DISPLAY + "#ref-")
    ]
    collision_counts = {
        (row.split("|")[3], row.split("|")[4])
        for row in collision_rows
    }
    collision_keys = {row.split("|")[0] for row in collision_rows}
    add(
        "R5b DISPLAY COLLISION: distinct semantic contracts remain two "
        "safe-suffixed count facts",
        len(collision_rows) == 2
        and len(collision_keys) == 2
        and collision_counts == {("1", "1"), ("1", "2")}
        and COLLISION_RAW_A not in collision_out,
        f"rows={collision_rows}",
    )

    # ── R6: REVOCATION is immediate — B revokes → cross read EMPTY again.
    _revoke(B_ACCT, "WS-R4", B_REPO)
    out = _xread_as("veripsa_app", A_ACCT, A_REPO, B_ACCT, B_REPO)
    add("R6 REVOKE: after B revokes, the cross read is EMPTY again (consent is a live, revocable fact)",
        _rows(out) == [], f"rows={_rows(out)}")

    # ── R7: a buyer SEAT cannot reach the cross-tenant read AT ALL (permission denied). Re-accept both so the
    # ONLY thing standing between the seat and B's data is the GRANT — proving it's the grant, not absent consent.
    _accept(A_ACCT, "WS-R4", A_REPO)
    _accept(B_ACCT, "WS-R4", B_REPO)
    # NOTE: the privilege CLASS veripsa_writer is NOLOGIN (reached by inheritance, never connected-as — db/roles.sql),
    # so a real BUYER SEAT is a LOGIN role that INHERITS veripsa_writer. We connect as veripsa_demo_agent (DEMO's seat,
    # inherits veripsa_writer) and veripsa_acme_agent (ACME's seat) — the actual connecting identities a buyer uses.
    seat_demo = _xread_as("veripsa_demo_agent", A_ACCT, A_REPO, B_ACCT, B_REPO)
    seat_acme = _xread_as("veripsa_acme_agent", A_ACCT, A_REPO, B_ACCT, B_REPO)
    add("R7 SEAT-DENIED: a DEMO seat (veripsa_demo_agent, inherits veripsa_writer) calling the cross read gets "
        "'permission denied' (REVOKE PUBLIC; GRANT only veripsa_app)",
        DENIED in seat_demo.lower(), f"err={last_value(seat_demo)!r}")
    add("R7 SEAT-DENIED: the OTHER tenant's seat (veripsa_acme_agent) ALSO cannot reach the cross read",
        DENIED in seat_acme.lower(), f"err={last_value(seat_acme)!r}")
    # the internal mutual-consent fn is likewise locked to a seat.
    seat_consent_fn = psql_as("veripsa_demo_agent",
        "SET search_path=core,pg_catalog; "
        f"SELECT count(*) FROM core._mutual_consent_active('{A_ACCT}','{A_REPO}','{B_ACCT}','{B_REPO}');")
    add("R7 SEAT-DENIED: the internal _mutual_consent_active fn is also denied to a seat role",
        DENIED in seat_consent_fn.lower(), f"err={last_value(seat_consent_fn)!r}")
    # POSITIVE control: the host App (veripsa_app) CAN reach it (so R7 denials are about the GRANT, not a broken fn).
    app_ok = _xread_as("veripsa_app", A_ACCT, A_REPO, B_ACCT, B_REPO)
    add("R7 CONTROL: the host App (veripsa_app) CAN call the cross read (proves the denials are GRANT-specific)",
        _rows(app_ok) != [] and DENIED not in app_ok.lower(), f"rows={_rows(app_ok)}")

    drop()

    # ── verdict ────────────────────────────────────────────────────────────────────────────────────────────
    failed = [(lbl, det) for (lbl, ok, det) in checks if not ok]
    for lbl, ok, det in checks:
        print(("  [PASS] " if ok else "  [FAIL] ") + lbl + (f"   :: {det}" if (det and not ok) else ""))
    if failed:
        print(f"\nCROSS-TENANT CONSENT RED-TEAM: FAIL ({len(failed)} probe(s) breached/regressed)")
        return 1
    print("\ncross-tenant consent: 2-tenant FORCE-RLS proof holds — without bilateral consent the cross read "
          "returns NOTHING; a forged GUC / forged workspace_member row does NOT leak B to A (RLS WITH CHECK + the "
          "consent FACT both hold); one-sided consent yields ZERO cross-read; WITH bilateral consent ONLY the "
          "contract-key 'consumed-by' fact + counts cross, NEVER a path/node/edge/graph of B; revocation is "
          "immediate; and a buyer SEAT cannot reach the cross-tenant read at all (GRANT only to veripsa_app).")
    print("CROSS-TENANT CONSENT RED-TEAM: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
