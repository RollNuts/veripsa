#!/usr/bin/env python3
"""UNINSTALL/ERASE RESURRECTION + PHANTOM-TENANT + PROCESSING-PILE gate (audit iteration-4 findings).

THE BUGS (RED on origin/main):

  P1 — a BACKGROUND per-repo writer can RESURRECT a purged/erased tenant across installation.deleted.
       installation.deleted runs the ACCOUNT-WIDE purge holding NO advisory lock, while a co-change populate /
       self_heal / boot-reconcile task holds a PER-REPO lock (the lock-key asymmetry → they do NOT serialize). An
       in-flight task then calls enter_installation_with_authority (which LAZILY RE-CREATES the deleted account) and
       writes co_change rows (private file paths) / re-ingests the graph back into the resurrected tenant — a
       GDPR/uninstall purge silently undone.
       THE FIX (gated here):
         (a) uninstall purge records a TOMBSTONE (core.account_lifecycle_tombstone) while its retained account can
             still be reached by in-flight work. Right-to-erasure deletes that marker too: no raw account/install id
             survives the hard delete.
         (b) ordinary webhooks and background tasks route only through
             enter_existing_installation_with_authority, which returns NULL instead of provisioning an absent or
             revoked route. A delayed event therefore cannot recreate an erased account.
         (c) the per-repo background writers, AFTER taking their lock + pinning the tenant and BEFORE writing, call
             core.assert_account_live_with_authority() which RAISES on a tombstone → the write is skipped (this is
             load-bearing for the PURGE case: the account row still EXISTS, so RLS alone would NOT block the write).
         (d) only an authenticated activation (installation.created / repos-added) that first obtained an App-JWT
             installation-generation proof may use the provisioning route and call
             core.reactivate_account_with_authority(). A real reinstall works without weakening the ordinary route.

  P2 — installation_account.installation_id holds the OWNER id; the installation.id fallback was a phantom-tenant
       trigger. _event_account_key fell back to installation.id (a DIFFERENT id space) for a payload lacking
       owner/org, minting a phantom ACCT-GH-<install_id> distinct from the repo's real ACCT-GH-<owner_id>.
       THE FIX: the installation.id fallback is DROPPED (honest-unknown None instead of a phantom tenant).

  P2 — evaluate_delivery_depth was blind to a stuck-'processing' pile. It alerted on failed/queued but only SAMPLED
       'processing'. A claim-then-crash loop accumulates 'processing' rows re-picked only after the 1800s stale
       reclaim — nothing paged meanwhile.
       THE FIX: a delivery_processing_stuck condition (processing>=N SUSTAINED over >1 tick), edge-triggered.

CONTENT-FREE + FORCE-RLS + least-privilege invariants hold throughout (only counts / the account routing key cross
any boundary; the uninstall tombstone is cross-tenant, written ONLY via SECURITY DEFINER authority fns,
REVOKE-from-PUBLIC, and is itself deleted by the explicit erasure path).

PROCESS-UNIQUE scratch DB (parallel-safe). Run:  python3 tests/test_uninstall_resurrection.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402

DB = "veripsa_resurrect_" + str(os.getpid())
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def conn_for(role):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def run_as(role, sql, args=()):
    """One short-lived statement as `role`; tolerant of void-returning calls (provision_seat etc.)."""
    conn = conn_for(role)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                return None
            return row[0] if row else None
    finally:
        conn.close()


def owner_count(account, sql_tail, args=()):
    """Owner read with RLS pinned to `account` (FORCE RLS still walls the owner to exactly that account)."""
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute(sql_tail, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def tomb(account):
    # The row persists as a lifecycle high-water mark after reinstall; only active=true is a live tombstone.
    return run_as("veripsa_migrator",
                  "SELECT count(*) FROM core.account_lifecycle_tombstone WHERE account_id=%s AND active", (account,))


def tomb_rows(account):
    """All raw lifecycle-marker rows, including an inactive uninstall high-water."""
    return run_as("veripsa_migrator",
                  "SELECT count(*) FROM core.account_lifecycle_tombstone WHERE account_id=%s", (account,))


def provisioning_app_conn(install_id):
    """Activation-only route: caller has already authenticated the installation generation with App JWT."""
    c = conn_for("veripsa_app")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (install_id,))
    return c


def existing_app_conn(install_id):
    """Ordinary/background route: resolve a live route and never provision one."""
    c = conn_for("veripsa_app")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (install_id,))
    return c


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    import code_graph_extract as X
    import tempfile

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # P1 (a) UNINSTALL-PURGE TOMBSTONE + (c) background-writer REFUSAL + load-bearing proof (RLS alone won't block)
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    INSTALL, ACCT = "555", "ACCT-GH-555"
    conn = provisioning_app_conn(INSTALL)

    def run(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None

    # seed a working set + a co_change pair (the private-repo file paths) for the tenant.
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "a.py"), "w").write("def fa():\n    return 1\n")
        g = X.build_graph(d)
    run("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(g), "acme/web", "main", "a" * 40))
    run("SELECT core.ingest_cochange_with_authority(%s,%s)",
        (json.dumps([{"a": "a.py", "b": "b.py", "co": 7, "n_a": 7, "n_b": 7,
                      "n_total": 7, "strength": 1.0, "lift": 2.0}]), "acme/web"))
    seeded_nodes = owner_count(ACCT, "SELECT count(*) FROM core.code_node WHERE account_id=%s", (ACCT,))
    seeded_cc = owner_count(ACCT, "SELECT count(*) FROM core.co_change WHERE account_id=%s", (ACCT,))
    chk(seeded_nodes > 0 and seeded_cc > 0, f"seeded a working set + co_change for the tenant (nodes={seeded_nodes}, cc={seeded_cc})")
    chk(tomb(ACCT) == 0, "no tombstone before the uninstall")
    # Hold one already-routed background session across the purge to model the real in-flight race. A newly-started
    # background task after the purge is rejected earlier by enter_existing because the route is revoked.
    bg = existing_app_conn(INSTALL)

    # UNINSTALL: destructive admission is bound to this exact processing delivery plus the App-JWT account point
    # read that proves generation A is absent. The legacy proof-less overload must never be used by new tests/code.
    uninstall_key = "delete-generation-a-555"
    deleted_generation = "A-555"
    run_as(
        "veripsa_migrator",
        "INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,payload,status,received_at) "
        "VALUES (%s,'installation',%s,%s::jsonb,'processing',clock_timestamp())",
        (uninstall_key, INSTALL, json.dumps({
            "action": "deleted",
            "installation": {"id": deleted_generation, "account": {"id": INSTALL}},
        })),
    )
    run("SELECT set_config('core.current_delivery_key',%s,false)", (uninstall_key,))
    delete_proof = json.dumps({
        "state": "absent",
        "deleted_installation_id": deleted_generation,
        "account_id": INSTALL,
    })
    res = run("SELECT core.purge_account_working_set_with_authority(%s::jsonb)", (delete_proof,))
    res = res if isinstance(res, dict) else json.loads(res)
    chk(res.get("ok") and res.get("account_wide"), f"account-wide purge ran (keys={sorted(res.get('purged', {}).keys())})")
    chk(owner_count(ACCT, "SELECT count(*) FROM core.code_node WHERE account_id=%s", (ACCT,)) == 0
        and owner_count(ACCT, "SELECT count(*) FROM core.co_change WHERE account_id=%s", (ACCT,)) == 0,
        "the working set + co_change are purged account-wide")
    chk(tomb(ACCT) == 1, "the uninstall purge SET the resurrection tombstone")
    # the purge KEEPS the account row (this is the content-free working-set purge, not erase) — so RLS alone can NOT
    # be what blocks a resurrection write; the tombstone is the load-bearing guard. Prove the account row survives.
    chk(owner_count(ACCT, "SELECT count(*) FROM core.account WHERE account_id=%s", (ACCT,)) == 1,
        "the purge RETAINS the account row (so a re-write is NOT blocked by RLS — the tombstone must be)")

    late_route = run_as(
        "veripsa_app", "SELECT core.enter_existing_installation_with_authority(%s)", (INSTALL,))
    chk(late_route is None,
        "new ordinary/background work cannot enter the revoked uninstall route")
    # A BACKGROUND WRITER already routed before the uninstall still holds its pin; its in-transaction liveness guard
    # must refuse after the purge commits.
    refused_code = None
    try:
        with bg.cursor() as cur:
            cur.execute("SELECT core.assert_account_live_with_authority()")
    except psycopg2.Error as e:
        refused_code = e.pgcode
        try:
            bg.rollback()
        except Exception:
            pass
    chk(refused_code == "42501",
        f"assert_account_live_with_authority REFUSES a background write on a purged (tombstoned) tenant [42501] (got {refused_code})")
    # CONTROL (load-bearing proof): a write that BYPASSES every liveness guard DOES land — confirming RLS alone would
    # NOT have blocked it on the RETAINED account, so the tombstone guard is what closes the vector. NOTE: the gated
    # writer core.ingest_cochange_with_authority now carries its OWN in-txn tombstone refusal (the small-findings
    # co-change/uninstall LIVE-race fix), so it can no longer serve as the "bypass" — a RAW direct INSERT does. We
    # pin the account + arm the governed-write token (the forgery trigger gates INSERT) and write the row straight to
    # the table as the migrator: this skips BOTH the pre-flight assert AND the writer's in-txn guard, yet RLS admits
    # it on the still-present account row — proving RLS alone is not the wall. Then clean the control row back out.
    bg2 = conn_for("veripsa_migrator")
    try:
        # ONE transaction: the governed-write token is txn-local, so the INSERT must run in the SAME txn that armed it
        # (the forgery trigger refuses an un-armed direct write). `with conn` commits on success.
        with bg2, bg2.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (ACCT,))
            cur.execute("SELECT core.mark_governed_write('co_change')")
            cur.execute("INSERT INTO core.co_change(account_id, repo, path_a, path_b, co, n_a, n_b, strength, lift, n_total) "
                        "VALUES (%s,'acme/web','x.py','y.py',9,9,9,1.0,2.0,9) "
                        "ON CONFLICT (account_id, repo, path_a, path_b) DO NOTHING", (ACCT,))
    finally:
        bg2.close()
    leaked = owner_count(ACCT, "SELECT count(*) FROM core.co_change WHERE account_id=%s", (ACCT,))
    chk(leaked == 1, f"control: a RAW write bypassing every liveness guard resurrects data (RLS alone does NOT block it on the retained account) → the guard is load-bearing (rows={leaked})")
    run_as("veripsa_migrator",
           "SELECT set_config('core.current_account',%s,true); DELETE FROM core.co_change WHERE account_id=%s", (ACCT, ACCT))

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # P1 (d) GENUINE REINSTALL reactivates: the tombstone is cleared and writers proceed again.
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    conn2 = provisioning_app_conn(INSTALL)

    def run2(sql, args=()):
        with conn2.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None

    activation_key = "reinstall-after-purge"
    run_as("veripsa_migrator",
           "INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,payload,status,received_at) "
           "VALUES (%s,'installation',%s,%s::jsonb,'processing',clock_timestamp())",
           (activation_key, INSTALL, json.dumps({
               "action": "created",
               "installation": {"id": "B-4242", "account": {"id": INSTALL}},
           })))
    proof = json.dumps({"installation_id": "B-4242", "account_id": INSTALL,
                        "created_at": "2099-01-01T00:00:00Z", "suspended": False})
    rr = run2("SELECT core.reactivate_account_with_authority(%s,%s::jsonb)", (activation_key, proof))
    rr = rr if isinstance(rr, dict) else json.loads(rr)
    chk(rr.get("ok") and rr.get("tombstone_cleared") == 1, f"reactivate clears the tombstone (cleared={rr.get('tombstone_cleared')})")
    chk(tomb(ACCT) == 0 and tomb_rows(ACCT) == 1,
        "genuine reinstall deactivates the uninstall fence while retaining its ordering high-water")
    chk(run2("SELECT core.assert_account_live_with_authority()") == ACCT,
        "assert_account_live now PASSES (the tenant is live again post-reinstall)")
    run2("SELECT core.ingest_cochange_with_authority(%s,%s)",
         (json.dumps([{"a": "p.py", "b": "q.py", "co": 5, "n_a": 5, "n_b": 5,
                       "n_total": 5, "strength": 1.0, "lift": 2.0}]), "acme/web"))
    chk(owner_count(ACCT, "SELECT count(*) FROM core.co_change WHERE account_id=%s", (ACCT,)) == 1,
        "a genuine post-reinstall write LANDS (the reinstall worked — history preserved, not blocked)")

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # P1 (b) ERASE: hard-deletes every raw account/install id. The non-provisioning ordinary route cannot resurrect
    # it; a later App-JWT-proven activation can still establish a genuinely new generation.
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    EID, EACCT = "9999", "ACCT-GH-9999"
    run_as("veripsa_migrator", "SELECT core.provision_seat(%s,'o','AG-9999','w','veripsa_gh9999_agent')", (EACCT,))
    run_as("veripsa_migrator",
           "INSERT INTO core.installation_account(installation_id,account_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (EID, EACCT))
    run_as("veripsa_migrator",
           "INSERT INTO core.credential(role_name,agent_id,account_id) VALUES ('veripsa_app','AG-9999',%s) "
           "ON CONFLICT (role_name) DO UPDATE SET account_id=EXCLUDED.account_id, agent_id=EXCLUDED.agent_id", (EACCT,))
    run_as("veripsa_app", "SELECT core.tombstone_account_with_authority('uninstall_purge')")
    chk(tomb_rows(EACCT) == 1, "erase setup includes an uninstall lifecycle marker carrying the account id")
    er = run_as("veripsa_app", "SELECT core.erase_account_with_authority()")
    er = er if isinstance(er, dict) else json.loads(er)
    chk(er.get("ok") and run_as("veripsa_migrator", "SELECT count(*) FROM core.account WHERE account_id=%s", (EACCT,)) == 0,
        "erase HARD-DELETES the account row")
    chk(tomb_rows(EACCT) == 0 and int(er.get("erased", {}).get("account_tombstones", -1)) == 1,
        "erase deletes the account lifecycle marker too and reports it in the deletion manifest")
    # A delayed ordinary event uses the non-provisioning route. It returns NULL and creates neither account nor
    # installation mapping; prevention no longer depends on retaining the erased identifier in another table.
    ordinary_route = run_as(
        "veripsa_app", "SELECT core.enter_existing_installation_with_authority(%s)", (EID,))
    acct_after = run_as("veripsa_migrator", "SELECT count(*) FROM core.account WHERE account_id=%s", (EACCT,))
    inst_after = run_as("veripsa_migrator", "SELECT count(*) FROM core.installation_account WHERE account_id=%s", (EACCT,))
    raw_identifier_refs = run_as(
        "veripsa_migrator",
        "SELECT "
        "(SELECT count(*) FROM core.credential WHERE account_id=%s) + "
        "(SELECT count(*) FROM core.account_lifecycle_tombstone WHERE account_id=%s) + "
        "(SELECT count(*) FROM core.webhook_delivery WHERE account_key IN (%s,%s) "
        " OR payload::text LIKE %s OR payload::text LIKE %s)",
        (EACCT, EACCT, EACCT, EID, f"%{EACCT}%", f"%{EID}%"),
    )
    chk(ordinary_route is None and acct_after == 0 and inst_after == 0 and raw_identifier_refs == 0,
        f"enter_existing returns NULL and does NOT resurrect an ERASED account "
        f"(route={ordinary_route!r}, account={acct_after}, inst={inst_after}, raw_refs={raw_identifier_refs})")

    # A genuine replacement activation is different: the application obtains this exact App-JWT point-read proof
    # before selecting the activation-only provisioning route. The durable delivery binds the proof to this event.
    activation_key_2 = "reinstall-after-hard-erase"
    replacement_id = "B-9999"
    run_as(
        "veripsa_migrator",
        "INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,payload,status,received_at) "
        "VALUES (%s,'installation',%s,%s::jsonb,'processing',clock_timestamp())",
        (activation_key_2, EID, json.dumps({
            "action": "created",
            "installation": {"id": replacement_id, "account": {"id": EID}},
        })),
    )
    activation = provisioning_app_conn(EID)
    try:
        with activation.cursor() as cur:
            cur.execute(
                "SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
                (activation_key_2, json.dumps({
                    "installation_id": replacement_id,
                    "account_id": EID,
                    "created_at": "2099-02-01T00:00:00Z",
                    "suspended": False,
                })),
            )
            activated = cur.fetchone()[0]
    finally:
        activation.close()
    activated = activated if isinstance(activated, dict) else json.loads(activated)
    current_generation = run_as(
        "veripsa_migrator",
        "SELECT github_installation_id FROM core.installation_account WHERE account_id=%s",
        (EACCT,),
    )
    chk(activated.get("reactivated") and current_generation == replacement_id,
        f"an App-JWT-proven activation provisions a genuine replacement generation (current={current_generation})")
    chk(tomb_rows(EACCT) == 0,
        "successful reinstall does not recreate a raw GDPR-erasure tombstone")

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # P2 — PHANTOM-TENANT GUARD: _event_account_key drops the installation.id fallback (no phantom ACCT-GH-<install>).
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    import event_processor as EP
    # an owner/org-bearing payload still resolves the OWNER id (the real, stable tenant key) — unchanged.
    k_owner = EP._event_account_key({"repository": {"owner": {"id": 4242, "login": "acme"}}})
    k_instacct = EP._event_account_key({"installation": {"id": 777, "account": {"id": 4242}}})
    k_org = EP._event_account_key({"organization": {"id": 4242}})
    chk(k_owner == "4242" and k_instacct == "4242" and k_org == "4242",
        f"_event_account_key still keys by the OWNER/account id from every honest source (owner={k_owner}, inst.account={k_instacct}, org={k_org})")
    # a payload carrying ONLY installation.id (no owner/org/installation.account) now resolves None — NOT a phantom
    # ACCT-GH-<install_id> tenant. (Before the fix this returned the install id, minting a split-brain tenant.)
    k_phantom = EP._event_account_key({"installation": {"id": 777}})
    chk(k_phantom is None,
        f"_event_account_key returns None (honest-unknown) for an install-id-ONLY payload — no phantom tenant (got {k_phantom!r})")
    # the source order is unchanged for the consistent common case: owner.id wins even when an install.id is present.
    k_both = EP._event_account_key({"installation": {"id": 777}, "repository": {"owner": {"id": 4242}}})
    chk(k_both == "4242", f"owner.id remains the first-choice key even alongside an install id (got {k_both})")

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # P2 — DELIVERY 'processing' STUCK ALERT: edge-triggered, SUSTAINED over >1 tick (a single tick never pages).
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    import alerts as A
    posts = []
    sink = A.AlertSink(webhook_url="https://hook.example/x", min_interval=0,
                       poster=lambda url, body: posts.append(body))

    def stuck_fired():
        return any(p.get("key") == "delivery_processing_stuck" for p in posts)

    def stuck_resolved():
        return any(p.get("key") == "delivery_processing_stuck" and "recover" in p.get("text", "").lower() for p in posts)

    # fresh claim-before-submit recovery rows can sit in 'processing' while queued in memory; age must prevent pages.
    posts.clear()
    A.evaluate_delivery_depth(sink, {"processing": 3, "queued": 0, "failed": 0,
                                     "processing_oldest_age_seconds": 10},
                              processing_stuck=1, processing_stuck_seconds=300)
    A.evaluate_delivery_depth(sink, {"processing": 3, "queued": 0, "failed": 0,
                                     "processing_oldest_age_seconds": 20},
                              processing_stuck=1, processing_stuck_seconds=300)
    chk(not stuck_fired(), "delivery_processing_stuck does NOT page on fresh processing rows below the age threshold")
    # tick 1: aged processing over the line for the FIRST time → must NOT page yet (>1 tick gate still applies).
    posts.clear()
    A.evaluate_delivery_depth(sink, {"processing": 3, "queued": 0, "failed": 0,
                                     "processing_oldest_age_seconds": 600},
                              processing_stuck=1, processing_stuck_seconds=300)
    chk(not stuck_fired(), "delivery_processing_stuck does NOT page on the FIRST aged over-threshold tick")
    # tick 2: STILL over the line → the sustained wedge pages (edge on the 2nd consecutive over-threshold tick).
    posts.clear()
    A.evaluate_delivery_depth(sink, {"processing": 3, "queued": 0, "failed": 0,
                                     "processing_oldest_age_seconds": 650},
                              processing_stuck=1, processing_stuck_seconds=300)
    fired_lvl = next((p.get("level") for p in posts if p.get("key") == "delivery_processing_stuck"), None)
    chk(stuck_fired() and fired_lvl == "critical",
        f"delivery_processing_stuck PAGES (critical) on a SUSTAINED 'processing' pile (>1 tick) (level={fired_lvl})")
    # the pile drains → it resolves (re-arms) so the next wedge pages again.
    posts.clear()
    A.evaluate_delivery_depth(sink, {"processing": 0, "queued": 0, "failed": 0,
                                     "processing_oldest_age_seconds": 0},
                              processing_stuck=1, processing_stuck_seconds=300)
    chk(stuck_resolved(), "delivery_processing_stuck RESOLVES when the 'processing' pile drains (re-arms for the next wedge)")
    # a single over-threshold tick AFTER a resolve does not immediately re-page (sustained-over-1-tick is preserved).
    posts.clear()
    A.evaluate_delivery_depth(sink, {"processing": 2, "queued": 0, "failed": 0,
                                     "processing_oldest_age_seconds": 700},
                              processing_stuck=1, processing_stuck_seconds=300)
    chk(not stuck_fired(), "after a drain, a single fresh over-threshold tick again does NOT page (the >1-tick gate re-arms)")
    # the existing conditions still work (no regression): dead-letter on failed>0, backlog on queued>=threshold.
    posts.clear()
    A.evaluate_delivery_depth(sink, {"failed": 2, "queued": 0, "processing": 0}, queued_backlog=1000)
    chk(any(p.get("key") == "delivery_dead_letter" for p in posts),
        "regression check: delivery_dead_letter still fires on a 'failed' row (the existing alerts are intact)")

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # STRUCTURAL WIRING (recall-safety): every per-repo BACKGROUND writer named in the audit must CALL the live-
    # revalidation guard AFTER its lock/pin and BEFORE its write — so a future refactor that drops a guard (re-
    # opening the resurrection vector) FAILS this gate, not silently ships. Inspect each writer's own source.
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    import inspect
    import cochange as CC
    import ingest as ING
    import webhook_handlers as WH
    GUARD = "core.assert_account_live_with_authority"
    # NOTE: self_heal_main_graph is NOT in this list — it runs on BOTH the live event txn AND boot; a RAISE there
    # would poison the live event's transaction, so the guard sits at its BACKGROUND caller (_reconcile_one_repo),
    # which runs it on its own autocommit connection BEFORE the backfill+heal. That caller IS asserted below.
    for mod, fn, name in ((CC, "_cochange_populate_task", "co-change populate"),
                          (CC, "_cochange_increment_task", "co-change per-push increment"),
                          (ING, "_reconcile_one_repo", "boot-reconcile per-repo")):
        try:
            src = inspect.getsource(getattr(mod, fn))
        except Exception:
            src = ""
        chk("core.enter_existing_installation_with_authority" in src and GUARD in src,
            f"structural: the background writer {name} resolves only an existing route, then calls {GUARD}()")
    try:
        processor_src = inspect.getsource(EP.make_db_processor)
    except Exception:
        processor_src = ""
    chk("enter_existing_installation_with_authority" in processor_src
        and "enter_installation_with_authority" in processor_src
        and "activation_route" in processor_src,
        "structural: webhook routing separates ordinary existing-only admission from activation-only provisioning")
    # the GENUINE-reinstall path must clear the tombstone via reactivate (so a real reinstall is never blocked).
    try:
        wh_src = inspect.getsource(WH._handle_installation_event) + inspect.getsource(WH._reactivate_account)
    except Exception:
        wh_src = ""
    chk("core.reactivate_account_with_authority" in wh_src,
        "structural: the onboarding handler reactivates (clears the tombstone) on a genuine reinstall")
    try:
        WH._reactivate_account(lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db blip")), "delivery")
        reactivate_error_raised = False
    except RuntimeError:
        reactivate_error_raised = True
    chk(reactivate_error_raised,
        "runtime: a reactivation DB error propagates so the durable delivery retries instead of finishing done")

    ok = all(checks)
    print("UNINSTALL/ERASE RESURRECTION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
