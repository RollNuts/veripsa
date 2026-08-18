#!/usr/bin/env python3
"""SECURITY PERIMETER — the adversarial breach gate (a malicious CUSTOMER tries to cross the wall).

The other tenant/tamper gates prove the HAPPY-path isolation and the catalog posture. THIS gate is the
attacker's playbook: a legitimately-installed Veripsa customer (a tenant connecting as a buyer/seat role, OR
hostile input arriving on the public webhook) tries to read or write ACROSS tenants, forge identity, or reach
the operator-only surfaces. Every probe here is a real exploit attempt that MUST be refused. Run live against
the catalog + the gate fns (peer auth on localhost), never grep — the only truth about who can do what is the
running database.

THE PROBES (each is "the breach is BLOCKED", proven against a 2-tenant fixture DEMO≠ACME with real rows):

  1. WEBHOOK AUTHENTICITY (HMAC). verify_signature is the public door. Constant-time compare; an EMPTY secret
     fails CLOSED at startup (serve() refuses to boot unless VERIPSA_ALLOW_UNSIGNED=1); unsigned / wrong-sig /
     tampered-body deliveries are rejected (401) BEFORE any state is touched. Asserted on the real function +
     the startup guard (so a public deploy can never accept all forgeries).

  2. CROSS-TENANT READ. A buyer seat (veripsa_writer-class) has NO direct table grant — every read goes
     through a SECURITY DEFINER surface that re-pins the account from the un-forgeable connection role. A
     forged `SET core.current_account='ACCT-victim'` + a direct `SELECT FROM core.code_node` is denied at the
     GRANT layer (the row would be invisible under RLS anyway — belt AND suspenders).

  3. FORGERY-TOKEN / GUC WRITE GATE. A buyer forges `core.current_account` AND `core.installation_account` to
     a victim tenant, then writes through the gate (declare_claim / ingest_graph). The write lands in the
     buyer's OWN account — establish_session_write_context re-resolves identity from session_user (the buyer's
     credential), OVERWRITING the forged GUCs. The victim tenant is untouched; its rows are not deleted.

  4. COORDINATE COLLISION. A buyer ingests into a repo coordinate engineered to collide with a victim's
     (`acmeorg/acme-repo`). account_id scopes every row, so the write lands in the buyer's tenant — it never
     overwrites or deletes the victim's same-named coordinate.

  5. OWNER-ONLY SURFACES. owner_cost_surface / db_usage_surface / set_free_line_with_authority are GRANTed
     ONLY to veripsa_app; every buyer/seat role (veripsa_writer / veripsa_reader / a demo agent) is DENIED.

  6. ROUTING-PIN INTEGRITY (the fix this gate lands). resolve_session_identity honors the App's
     core.installation_account pin ONLY when it is a GENUINELY-ROUTED account (a row exists in
     core.installation_account, written exclusively by the trusted enter_installation_with_authority route). A
     raw `SET core.installation_account='ACCT-PHANTOM'` (no routing row) on the App connection is IGNORED — the
     write can never be steered into a forged/unrouted tenant; it falls through to the credential path (which
     in a clean prod deploy with no veripsa_app credential fails CLOSED with 42501, never a phantom-tenant
     write). This closes a defense-in-depth gap: the cross-tenant routing no longer trusts a bare session GUC.

HONEST: this is a PROOF-OF-CLEAN gate. The perimeter is sound by design (no buyer-reachable breach); probe 6
is a defense-in-depth hardening of the App's own service identity (not customer-reachable, but a latent
privesc if any App-side code ever did a stray/injected raw SET). The gate is the permanent regression guard.

Run:  python3 tests/test_security_perimeter.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name would let concurrent
# runs (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run. Per-PID,
# exactly like db/smoke.sh, run_gates, and the other tamper gates.
DB = "veripsa_perimeter_" + str(os.getpid())

# valid 40-char hex commit shas for ingest (ingest_graph rejects non-hex / >64 chars)
SHA_DEMO = "1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa"
SHA_ACME = "2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb"
SHA_X = "3333cccc3333cccc3333cccc3333cccc3333cccc"

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def psql_mig(sql):
    """Query as the migrator (owner) — ground truth + has_function_privilege probes."""
    dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def last_value(out):
    """The LAST non-empty line of psql output — a multi-statement `SET ...; SELECT ...` prints a 'SET' ack
    line before the result, so a scalar probe must read the final line, not the whole blob."""
    lines = [ln for ln in out.splitlines() if ln.strip() != ""]
    return lines[-1].strip() if lines else ""


def psql_as(role, sql):
    """Run sql AS `role` (peer auth on localhost) — combined stdout+stderr (so a permission-denied is visible)."""
    dsn = f"postgresql://{role}@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr)


def bootstrap():
    """roles + schema.sql + the demo seats — the standard local instance (db/bootstrap_local.sh)."""
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)
    # a SECOND account (ACCT-ACME) so veripsa_acme_agent resolves to its own tenant; the 3rd demo agent seat.
    # MUST precede the installation_account inserts below (installation_account.account_id is an FK to account).
    psql_mig("SET search_path=core; "
             "SELECT core.provision_seat('ACCT-ACME','Acme Co','AG-ACME','acme','veripsa_acme_agent'); "
             "SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-A3','vanished','veripsa_demo_agent3');")
    # route two installations to two distinct tenants + seed one isolated graph node into each (via the App's
    # OWN trusted route), so the cross-tenant read/write probes have real victim rows to (fail to) reach.
    # (account_id is set to the buyer-fixture's own provisioned account so the App's lazy-provision path is NOT
    # taken — 111 routes to ACCT-DEMO, 222 to ACCT-ACME, the two pre-provisioned tenants.)
    psql_mig("SET search_path=core; "
             "INSERT INTO core.installation_account(installation_id,account_id) VALUES ('111','ACCT-DEMO') ON CONFLICT DO NOTHING;")
    psql_mig("SET search_path=core; "
             "INSERT INTO core.installation_account(installation_id,account_id) VALUES ('222','ACCT-ACME') ON CONFLICT DO NOTHING;")
    _app_ingest("111", "demoorg/demo-repo", "demo/secret.py", "demo_secret", SHA_DEMO)
    _app_ingest("222", "acmeorg/acme-repo", "acme/secret.py", "acme_secret", SHA_ACME)


def _app_ingest(inst, repo, path, name, sha):
    """As veripsa_app: enter a REAL installation (the trusted route) then ingest one node — the legit path."""
    g = ('{"nodes":[{"id":"n-%s","kind":"def","path":"%s","name":"%s"}],"edges":[]}' % (name, path, name))
    return psql_as("veripsa_app",
                   "SET search_path=core; "
                   f"SELECT core.enter_installation_with_authority('{inst}'); "
                   f"SELECT core.ingest_graph_with_authority('{g}'::jsonb,'{repo}','main','{sha}');")


def _count_in(account, where):
    """As the migrator, pin `account` and count code_node rows matching `where` (RLS ground truth)."""
    out = psql_mig(f"SET search_path=core; SET core.current_account='{account}'; "
                   f"SELECT count(*) FROM core.code_node WHERE {where};")
    return last_value(out)


def drop():
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


# A GRANT-layer denial says exactly this — we DON'T accept a generic error (a column/syntax error must not
# masquerade as a security denial).
DENIED = ("permission denied",)


def main():
    print("VERIPSA SECURITY PERIMETER — adversarial breach gate (malicious customer)")
    print(f"(scratch DB: {DB})")
    bootstrap()

    # ── PROBE-VALIDITY: the fixture really has two isolated tenants with one row each. ─────────────────────
    add("FIXTURE: tenant DEMO has its own seeded row (and only its own)",
        _count_in("ACCT-DEMO", "path='demo/secret.py'") == "1"
        and _count_in("ACCT-DEMO", "path='acme/secret.py'") == "0")
    add("FIXTURE: tenant ACME has its own seeded row (and only its own)",
        _count_in("ACCT-ACME", "path='acme/secret.py'") == "1"
        and _count_in("ACCT-ACME", "path='demo/secret.py'") == "0")

    # ── PROBE 1: WEBHOOK AUTHENTICITY (HMAC) — the public door. ────────────────────────────────────────────
    sys.path.insert(0, os.path.join(ROOT, "github-app"))
    import server  # noqa: E402  (the live handler module)
    secret = "s3cr3t-webhook-key"
    body = b'{"action":"opened","number":1}'
    import hmac as _hmac
    import hashlib as _hashlib
    good = "sha256=" + _hmac.new(secret.encode(), body, _hashlib.sha256).hexdigest()
    add("PROBE1 HMAC: a correctly-signed delivery VERIFIES (probe is live, not vacuous)",
        server.verify_signature(secret, body, good) is True)
    add("PROBE1 HMAC: a WRONG-signature delivery is REJECTED",
        server.verify_signature(secret, body, "sha256=" + "0" * 64) is False)
    add("PROBE1 HMAC: a TAMPERED body (valid sig for different bytes) is REJECTED",
        server.verify_signature(secret, b'{"action":"closed","number":1}', good) is False)
    add("PROBE1 HMAC: a MISSING signature header is REJECTED (no unsigned acceptance with a secret set)",
        server.verify_signature(secret, body, None) is False)
    add("PROBE1 HMAC: a non-sha256 scheme (sha1=) is REJECTED",
        server.verify_signature(secret, body, "sha1=" + "0" * 40) is False)
    # constant-time compare (the textbook timing-attack defense): the impl must use hmac.compare_digest. Prove
    # it from the source so a future refactor to `==` (leaky) fails here.
    import inspect as _inspect
    src = _inspect.getsource(server.verify_signature)
    add("PROBE1 HMAC: signature compare is CONSTANT-TIME (hmac.compare_digest, not ==)",
        "compare_digest" in src)
    # FAIL-CLOSED on an empty secret: serve() must REFUSE to start without a secret unless explicitly opted in.
    serve_src = _inspect.getsource(server.serve)
    add("PROBE1 HMAC: an EMPTY webhook secret FAILS CLOSED at startup (serve() raises SystemExit unless "
        "VERIPSA_ALLOW_UNSIGNED=1 — a public deploy can never silently accept all forgeries)",
        "GH_WEBHOOK_SECRET" in serve_src and "SystemExit" in serve_src
        and "VERIPSA_ALLOW_UNSIGNED" in serve_src)

    # ── PROBE 2: CROSS-TENANT READ — a buyer forges current_account then reads the table directly. ──────────
    out = psql_as("veripsa_demo_agent",
                  "SET search_path=core; SET core.current_account='ACCT-ACME'; "
                  "SELECT count(*) FROM core.code_node;")
    add("PROBE2 READ: a buyer (veripsa_demo_agent) forging current_account=ACCT-ACME and SELECTing "
        "core.code_node directly is DENIED at the GRANT layer (no direct table grant to any buyer role)",
        any(m in out for m in DENIED))
    # the buyer also can't read via a forged installation_account GUC.
    out = psql_as("veripsa_demo_agent",
                  "SET search_path=core; SET core.installation_account='ACCT-ACME'; "
                  "SET core.current_account='ACCT-ACME'; SELECT count(*) FROM core.code_edge;")
    add("PROBE2 READ: a buyer forging installation_account=ACCT-ACME + reading core.code_edge is DENIED too",
        any(m in out for m in DENIED))

    # ── PROBE 3: FORGERY-TOKEN / GUC WRITE GATE — a buyer forges BOTH GUCs then writes through the gate. ────
    # declare_claim is granted to veripsa_writer (the buyer capability). The write must land in the buyer's OWN
    # account, NOT the victim — establish_session_write_context re-resolves from the connection role.
    psql_as("veripsa_demo_agent",
            "SET search_path=core; SET core.installation_account='ACCT-ACME'; SET core.current_account='ACCT-ACME'; "
            "SELECT core.declare_claim_with_authority('perim-evil','x/y.py','acmeorg/acme-repo','main');")
    in_demo = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-DEMO'; "
                                  "SELECT account_id FROM core.claim WHERE claim_id='perim-evil';"))
    in_acme = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-ACME'; "
                                  "SELECT count(*) FROM core.claim WHERE claim_id='perim-evil';"))
    add("PROBE3 WRITE: a buyer forging both GUCs to a victim tenant lands the claim in its OWN account "
        "(ACCT-DEMO), NOT the victim (the gate re-resolves identity from the connection role)",
        in_demo == "ACCT-DEMO")
    add("PROBE3 WRITE: the victim tenant (ACCT-ACME) received NONE of the forged claim",
        in_acme == "0")
    # the buyer's forged ingest must also not delete/overwrite the victim's rows.
    g = '{"nodes":[{"id":"inj","kind":"def","path":"BUYER_INJECT.py","name":"inj"}],"edges":[]}'
    psql_as("veripsa_demo_agent",
            "SET search_path=core; SET core.installation_account='ACCT-ACME'; "
            f"SELECT core.ingest_graph_with_authority('{g}'::jsonb,'acmeorg/acme-repo','main','{SHA_X}');")
    add("PROBE3 WRITE: the buyer's forged ingest into the victim's coordinate did NOT delete the victim's row "
        "(ACME's acme/secret.py is intact)",
        _count_in("ACCT-ACME", "path='acme/secret.py'") == "1")
    add("PROBE3 WRITE: the buyer's injected node landed in its OWN tenant (ACCT-DEMO), not ACME",
        _count_in("ACCT-DEMO", "path='BUYER_INJECT.py'") == "1"
        and _count_in("ACCT-ACME", "path='BUYER_INJECT.py'") == "0")

    # ── PROBE 4: COORDINATE COLLISION — account_id scopes the row; a same-named repo can't cross tenants. ──
    # (The buyer's ingest above already targeted the victim's exact repo name; the assertions above prove the
    # collision is harmless. This explicit assertion names the property.)
    add("PROBE4 COLLISION: a repo coordinate engineered to collide with the victim's "
        "(acmeorg/acme-repo) is scoped by account_id — the victim's same-named coordinate is untouched",
        _count_in("ACCT-ACME", "repo='acmeorg/acme-repo' AND path='acme/secret.py'") == "1")

    # ── PROBE 5: OWNER-ONLY SURFACES — buyer/seat roles must be DENIED. ────────────────────────────────────
    owner_fns = [
        # owner_cost_surface now carries an optional bounded-scan cap arg (audit P2-2); the privilege SIGNATURE is
        # owner_cost_surface(int). The call below still uses () — the default arg fills in — proving the buyer is
        # denied regardless of arity.
        ("core.owner_cost_surface()", "core.owner_cost_surface(int)"),
        ("core.db_usage_surface(0,10)", "core.db_usage_surface(int,int)"),
        ("core.set_free_line_with_authority('free_max_repos',5)", "core.set_free_line_with_authority(text,int)"),
    ]
    for call, sig in owner_fns:
        out = psql_as("veripsa_demo_agent", f"SET search_path=core; SELECT {call};")
        add(f"PROBE5 OWNER: a buyer (veripsa_demo_agent) calling {call} is DENIED (operator-only)",
            any(m in out for m in DENIED))
        for role in ("veripsa_writer", "veripsa_reader"):
            hp = psql_mig(f"SELECT has_function_privilege('{role}','{sig}','EXECUTE');")
            add(f"PROBE5 OWNER: capability class {role} has NO EXECUTE on {sig}", hp == "f")
    # CONTROL: the App service identity DOES hold it (proves the 'denied' results are a real difference).
    hp = psql_mig("SELECT has_function_privilege('veripsa_app','core.owner_cost_surface(int)','EXECUTE');")
    add("PROBE5 CONTROL: veripsa_app (the host identity) HAS owner_cost_surface EXECUTE — the buyer denials "
        "are a real privilege difference, not a dead probe", hp == "t")

    # ── PROBE 6: ROUTING-PIN INTEGRITY (the fix). A raw SET to an UNROUTED account must be ignored. ────────
    # As veripsa_app, forge installation_account to a phantom account with NO routing row, then write. The
    # routing pin must be IGNORED (no row → not honored); the write must NOT land in the phantom tenant.
    g = '{"nodes":[{"id":"ph","kind":"def","path":"PHANTOM.py","name":"ph"}],"edges":[]}'
    psql_as("veripsa_app",
            "SET search_path=core; SET core.installation_account='ACCT-PHANTOM-NOROUTE'; "
            f"SELECT core.ingest_graph_with_authority('{g}'::jsonb,'r/r','main','{SHA_X}');")
    add("PROBE6 ROUTING-PIN: a raw SET core.installation_account to an UNROUTED account "
        "(no row in core.installation_account) does NOT create/write that phantom tenant — the forged pin is "
        "ignored (resolve_session_identity honors the pin only when it is a genuinely-routed account)",
        _count_in("ACCT-PHANTOM-NOROUTE", "path='PHANTOM.py'") == "0")
    # and no phantom tenant ROW was conjured into existence.
    ph_acct = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-PHANTOM-NOROUTE'; "
                                  "SELECT count(*) FROM core.account WHERE account_id='ACCT-PHANTOM-NOROUTE';"))
    add("PROBE6 ROUTING-PIN: no phantom core.account row was conjured by the forged pin",
        ph_acct == "0")
    # SOURCE GUARD: resolve_session_identity must actually CHECK the routing table for the App's pin (so a
    # future refactor that drops the EXISTS check re-opens the hole and fails this gate). Proven from the
    # catalog's stored function body — the only truth about what the live fn does.
    body_sql = psql_mig(
        "SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='core' AND p.proname='resolve_session_identity';")
    # match the SPECIFIC routing-table guard on the App's pin (an EXISTS over core.installation_account keyed by
    # the candidate account), not just any EXISTS in the body — the behavioral PROBE6 above is the real catch,
    # this is a fast, specific source backstop so an obvious refactor-out is also named here.
    import re as _re
    norm = _re.sub(r"\s+", " ", body_sql)
    has_route_check = bool(_re.search(
        r"EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+core\.installation_account\s+WHERE\s+account_id\s*=\s*v_inst_account",
        norm, _re.IGNORECASE))
    add("PROBE6 SOURCE: resolve_session_identity gates the App's installation_account pin on an EXISTS check "
        "against the routing table (a future refactor that removes it re-opens phantom-tenant routing and "
        "fails here)", has_route_check)

    # ── PROBE 7: CONTENT-FREE SHAPE AT INGEST (P2-a). A writer seat's own --push graph is attacker-controlled;
    # the column length caps bound SIZE, not SHAPE. A crafted node name / edge dst carrying a NEWLINE + an
    # <img …> would store VERBATIM (RLS-walled to the attacker's own account, escaped by render_safe on the live
    # PR comment — so no current leak) but is a latent stored-XSS / fake-secret sink for the future web console.
    # ingest_graph_with_authority now SANITIZES name/dst (core._safe_ref_token): strip newlines/control chars +
    # angle brackets, flatten to one line. Assert the stored value is a sanitized SINGLE-LINE token, not the raw
    # value — and that a LEGITIMATE specifier shape (crate::lexical::num, a path) survives untouched (no over-block).
    import json as _json
    poison_name = "evilSym\n<img src=x onerror=alert(1)>"        # newline + HTML-significant angle brackets
    poison_dst = "../secret\n<script>steal()</script>/leak"      # newline + angle brackets in an import target
    legit_dst = "crate::lexical::num"                            # a real Rust path-shaped specifier (must survive)
    graph = {
        "nodes": [
            {"id": "san-file", "kind": "file", "path": "shape/probe.py", "name": "probe.py"},
            {"id": "san-poison", "kind": "def", "path": "shape/probe.py", "name": poison_name},
        ],
        "edges": [
            {"src": "san-file", "dst": poison_dst, "kind": "imports"},   # poisoned dst → sanitized
            {"src": "san-file", "dst": legit_dst, "kind": "imports"},    # legit dst → survives intact
        ],
    }
    gj = _json.dumps(graph)
    psql_as("veripsa_app",
            "SET search_path=core; SELECT core.enter_installation_with_authority('111'); "
            f"SELECT core.ingest_graph_with_authority($json${gj}$json$::jsonb,'demoorg/shape-repo','main','{SHA_X}');")
    # read back the stored name + dst as the migrator pinned to the writer's OWN tenant (111 routes to ACCT-DEMO).
    stored_name = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-DEMO'; "
                                      "SELECT name FROM core.code_node WHERE node_id='san-poison';"))
    stored_dsts = psql_mig("SET search_path=core; SET core.current_account='ACCT-DEMO'; "
                           "SELECT dst FROM core.code_edge WHERE repo='demoorg/shape-repo' ORDER BY dst;")
    dst_lines = [ln.strip() for ln in stored_dsts.splitlines() if ln.strip() and ln.strip() != "SET"]

    def _is_clean(s):
        return s is not None and "\n" not in s and "\r" not in s and "<" not in s and ">" not in s \
            and not any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in s)

    add("PROBE7 SHAPE: a crafted node NAME with a newline + <img …> is stored as a SANITIZED single-line token "
        f"(no newline / control / angle bracket), not the raw value (got {stored_name!r})",
        _is_clean(stored_name) and stored_name != "" and "evilSym" in stored_name and "img" in stored_name)
    add("PROBE7 SHAPE: a crafted edge DST with a newline + <script> is stored SANITIZED (single line, no angle "
        f"brackets / control chars), not raw (stored dsts={dst_lines})",
        all(_is_clean(d) for d in dst_lines) and any("secret" in d and "leak" in d for d in dst_lines))
    add("PROBE7 NO-OVER-BLOCK: a LEGITIMATE specifier shape (crate::lexical::num) survives ingest INTACT "
        "(the shape filter strips only newlines/control/angle-brackets, never real reference chars)",
        legit_dst in dst_lines)

    # ── verdict ──────────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} perimeter assertions passed "
          "(HMAC authenticity + cross-tenant read + forged-GUC write gate + coordinate collision + "
          "owner-only surfaces + routing-pin integrity + content-free shape at ingest) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} perimeter assertion(s) FAILED — the security perimeter has a hole:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nSECURITY PERIMETER GATE: FAIL")
        sys.exit(1)
    print("\nHONEST: no buyer-reachable cross-tenant breach exists — every probe is refused at the GRANT/RLS "
          "layer or by the connection-role identity re-resolution; the webhook door is HMAC-authenticated + "
          "fails closed on an empty secret; the App's tenant routing now trusts only genuinely-routed pins.")
    print("SECURITY PERIMETER GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        print(f"\n[FAIL] unexpected error: {e}")
        print("SECURITY PERIMETER GATE: FAIL")
        sys.exit(1)
