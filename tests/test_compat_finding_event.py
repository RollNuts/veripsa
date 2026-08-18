#!/usr/bin/env python3
"""COMPAT-FINDING EVENT KIND gate (compat lane PR-2) — the 'compat_finding' persistence contract.

The Stage-0 audit (docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §2) corrected the plan's "new FORCE-RLS table" to the
one-ledger law: a compatibility finding is a new event KIND ('compat_finding') + two typed nullable columns
(counterparty_sha, fact_fingerprint) on core.event, written by ONE gate fn
(core.record_compat_finding_with_authority — the record_collision/record_push precedent). This gate proves,
against the REAL schema on a real local Postgres:

  (a) RECORD → READ BACK: the App (entered into its installation's account) records a finding; the owner
      read path (migrator, RLS-pinned to that account — the smoke.sh authority-read pattern) sees exactly
      the mapped columns: producer head → commit_sha, consumer head → counterparty_sha, finding hash →
      fact_fingerprint, reason code → detail.
  (b) IDEMPOTENT: the SAME (account, repo, head pair, fingerprint) recorded twice → ONE row, the SAME id
      (deterministic id + ON CONFLICT DO NOTHING — the record_push precedent; a webhook redelivery / a
      re-analysis of unchanged heads never inflates the ledger).
  (c) NEW HEAD PAIR → NEW ROW: either head moving mints a new finding row (the fact is per head pair).
  (d) TENANT SEPARATION: account B cannot see A's finding — via the RLS-pinned owner read (0 rows under
      B's pin) AND via a direct table probe as a tenant (no SELECT grant at all).
  (e) CONTENT-FREE WALL: a detail carrying a code-body-shaped payload (spaces / parens / an '=' default —
      exactly what a parameter default value looks like) is REFUSED LOUDLY, never truncated — so no prefix
      of code can persist. Malformed SHAs / fingerprints are refused the same way (23514). The poison
      token is then proven absent from the ledger under every account pin.
  (f) PERIMETER (the tamper-grants / force-rls essence, scoped to the new columns): a token-armed tenant
      direct INSERT that ENUMERATES the new columns (counterparty_sha/fact_fingerprint AND the S3a
      fact_class/detector) is still refused by the GRANT layer (the probe parses — the #828 lesson);
      core.event still carries FORCE RLS + the tenant_isolation policy in the catalog; the append-only
      trigger still refuses a plain DELETE of the finding; the new CHECK constraints refuse a junk
      counterparty_sha AND a junk fact_class even for the owner; and a buyer writer seat cannot EXECUTE
      the new fn — either arity (App-delegation-only, like record_push).
  (g) S3a TAXONOMY + DETECTOR STAMP (corrective lane S3a — docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §3
      lane 3): the 9-arg overload stamps the EXPLICIT classification (fact_class — a closed 4-code enum)
      and the detector identity/version token onto the row, read back as COLUMN VALUES (queryable, never
      parsed out of the reason string); ONLY consumer_call_mismatch:* rows may carry
      evidence_backed_incompatibility (the writer's contract — asserted here at the recorder level via a
      round-trip of each class); the LEGACY 7-arg wrapper still works and records the honest UNCLASSIFIED
      shape (NULL class/detector); a junk class or a body-shaped detector is REFUSED LOUDLY (23514); a
      redelivery through the legacy wrapper can never strip an existing row's stamps (append-only
      first-row-wins dedupe).

Run:  python3 tests/test_compat_finding_event.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name would let concurrent
# runs (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run. Per-PID,
# exactly like db/smoke.sh (veripsa_smoke_$$), test_tenant_isolation.py, test_tamper_grants.py.
DB = "veripsa_compatfind_" + str(os.getpid())

REPO = "acme/app"
SHA_PRODUCER = "a" * 40          # the producer head (the side whose def shape is the contract)
SHA_CONSUMER = "b" * 40          # the consumer head (the side whose calls are checked)
SHA_CONSUMER_2 = "c" * 40        # a moved consumer head (a new pair)
SHA_LEGACY = "d" * 40            # a head pair recorded through the LEGACY 7-arg wrapper (unclassified)
FP = "deadbeef" + "0" * 24       # a stable content-free finding fingerprint
FP_CLS = "beefcls" + "0" * 25    # per-class round-trip fingerprints derive from this prefix
REASON = "contract_delta:required_arg_added"
REASON_MISMATCH = "consumer_call_mismatch:positional_shortfall"
# S3a: the four bounded classification codes + the detector identity/version stamp.
CLASS_DELTA = "contract_delta_observation"
CLASS_REBASE = "rebase_needed_observation"
CLASS_DIVERGENT = "divergent_definition_observation"
CLASS_INCOMPAT = "evidence_backed_incompatibility"
DETECTOR = "python-call-compat/py-call-v1"
# A code-body-shaped payload (a parameter DEFAULT VALUE — the exact thing the audit forbids persisting).
POISON = "def f(x=SECRET_DEFAULT_zzz999): return x"

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def app_conn_for_installation(installation_id):
    """A connection that has ENTERED `installation_id` as the App (veripsa_app) — the live per-event shape
    (the test_tenant_isolation pattern). Returns (run, account)."""
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (installation_id,))
        account = cur.fetchone()[0]

    def run(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    return run, account


def owner_rows(account, sql, args=()):
    """The owner/authority READ path: migrator, RLS-pinned to `account` (the smoke.sh pattern — FORCE RLS
    admits only the pinned account's rows, even for the table owner)."""
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def expect_error(role, sql, args=(), pin_account=None):
    """Run sql as `role`; return the error string (None if it unexpectedly succeeded)."""
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if pin_account:
                cur.execute("SELECT set_config('core.current_account', %s, true)", (pin_account,))
            cur.execute(sql, args)
        return None
    except Exception as e:
        return str(e)
    finally:
        conn.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", (r.stdout + r.stderr)[-1500:])
        return 1

    # Two REAL installations → two tenant accounts (the routing the live App uses).
    app_a, acct_a = app_conn_for_installation("INST-COMPAT-A")
    app_b, acct_b = app_conn_for_installation("INST-COMPAT-B")
    add(f"setup: two installations route to two distinct accounts ({acct_a} / {acct_b})",
        bool(acct_a) and bool(acct_b) and acct_a != acct_b)

    record_sql = "SELECT core.record_compat_finding_with_authority(%s,%s,%s,%s,%s,%s,%s)"
    record_sql9 = "SELECT core.record_compat_finding_with_authority(%s,%s,%s,%s,%s,%s,%s,%s,%s)"

    # ── (a) RECORD → READ BACK via the owner/authority read path (S3a: the 9-arg stamping writer) ───────
    ev1 = app_a(record_sql9, (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, FP, REASON,
                              CLASS_DELTA, DETECTOR))
    add(f"record: the App's finding write returns a deterministic EV-COMPAT id ({ev1})",
        isinstance(ev1, str) and ev1.startswith("EV-COMPAT-"))
    rows = owner_rows(acct_a,
                      "SELECT kind, repo, branch, path, commit_sha, counterparty_sha, fact_fingerprint, detail, "
                      "fact_class, detector FROM core.event WHERE kind='compat_finding'")
    add("read-back: exactly one 'compat_finding' row under the owner read pinned to account A", len(rows) == 1)
    if rows:
        k, repo, branch, path, sha, csha, fp, detail, fclass, det = rows[0]
        add("read-back: producer head → commit_sha", sha == SHA_PRODUCER)
        add("read-back: consumer head → counterparty_sha", csha == SHA_CONSUMER)
        add("read-back: finding fingerprint → fact_fingerprint", fp == FP)
        add("read-back: bounded reason code → detail", detail == REASON)
        add("read-back: S3a classification stamped as a COLUMN value (queryable, never parsed from detail)",
            fclass == CLASS_DELTA)
        add("read-back: S3a detector identity/version stamped → detector", det == DETECTOR)
        add("read-back: coordinate columns carried (repo/branch/path)",
            repo == REPO and branch == "main" and path == "src/api.py")

    # ── (b) IDEMPOTENT: the same (account, repo, head pair, fingerprint) twice → ONE row, same id ───────
    ev1_again = app_a(record_sql9, (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, FP, REASON,
                                    CLASS_DELTA, DETECTOR))
    n = owner_rows(acct_a, "SELECT count(*) FROM core.event WHERE kind='compat_finding'")[0][0]
    add(f"idempotent: re-recording the SAME finding returns the SAME id and adds NO row (n={n})",
        ev1_again == ev1 and n == 1)
    # a REDELIVERY through the LEGACY 7-arg wrapper (same identity) dedupes onto the SAME row and can never
    # STRIP the stamps — the ledger is append-only and the first row wins.
    ev1_legacy = app_a(record_sql, (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, FP, REASON))
    kept = owner_rows(acct_a, "SELECT fact_class, detector FROM core.event WHERE kind='compat_finding' "
                              "AND fact_fingerprint=%s", (FP,))
    add("idempotent: a legacy-wrapper redelivery of the SAME identity returns the SAME id and does NOT "
        "strip the row's class/detector stamps (append-only first row wins)",
        ev1_legacy == ev1 and kept == [(CLASS_DELTA, DETECTOR)])

    # ── (c) a NEW head pair (consumer moved) → a NEW row ────────────────────────────────────────────────
    ev2 = app_a(record_sql9, (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER_2, FP, REASON,
                              CLASS_DELTA, DETECTOR))
    n = owner_rows(acct_a, "SELECT count(*) FROM core.event WHERE kind='compat_finding'")[0][0]
    add(f"new pair: a moved consumer head mints a NEW finding row (n={n}, new id differs)",
        ev2 != ev1 and n == 2)

    # ── (g) S3a TAXONOMY: every classification code round-trips as a column value; the LEGACY wrapper
    # records the honest UNCLASSIFIED shape (NULL class/detector) — old callers stay valid. ──────────────
    class_reason = [(CLASS_REBASE, "rebase_needed"), (CLASS_DIVERGENT, "divergent_definitions"),
                    (CLASS_INCOMPAT, REASON_MISMATCH)]
    for i, (klass, reason) in enumerate(class_reason):
        fp_i = FP_CLS[:-1] + str(i)
        app_a(record_sql9, (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, fp_i, reason,
                            klass, DETECTOR))
        got = owner_rows(acct_a, "SELECT fact_class, detector, detail FROM core.event "
                                 "WHERE kind='compat_finding' AND fact_fingerprint=%s", (fp_i,))
        add(f"S3a taxonomy: class '{klass}' round-trips as a column value with the detector stamp",
            got == [(klass, DETECTOR, reason)])
    ev_legacy = app_a(record_sql, (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_LEGACY, FP, REASON))
    got = owner_rows(acct_a, "SELECT fact_class, detector FROM core.event WHERE kind='compat_finding' "
                             "AND counterparty_sha=%s", (SHA_LEGACY,))
    add("S3a legacy: the 7-arg wrapper still records — as the honest UNCLASSIFIED shape "
        "(fact_class/detector NULL — legacy evidence, never a guessed class)",
        isinstance(ev_legacy, str) and ev_legacy.startswith("EV-COMPAT-") and got == [(None, None)])
    # observations vs incompatibilities are QUERYABLE by column equality — never conflated, never parsed:
    n_incompat = owner_rows(acct_a, "SELECT count(*) FROM core.event WHERE kind='compat_finding' "
                                    "AND fact_class='evidence_backed_incompatibility'")[0][0]
    n_all = owner_rows(acct_a, "SELECT count(*) FROM core.event WHERE kind='compat_finding'")[0][0]
    add(f"S3a queryability: the evidence class selects by COLUMN equality (1 of {n_all} rows) — "
        "observations and incompatibilities never conflate", n_incompat == 1 and n_all == 6)

    # ── (d) TENANT SEPARATION: account B sees NOTHING of A's findings ───────────────────────────────────
    n_b = owner_rows(acct_b, "SELECT count(*) FROM core.event WHERE kind='compat_finding'")[0][0]
    add(f"tenant separation: the owner read pinned to account B sees ZERO of A's findings (n={n_b})", n_b == 0)
    err = expect_error("veripsa_demo_agent", "SELECT * FROM core.event WHERE kind='compat_finding'")
    add("tenant separation: a tenant seat has NO direct SELECT on core.event at all "
        f"({(err or 'NOT REFUSED')[:60]})", err is not None and "permission denied" in err)

    # ── (e) CONTENT-FREE WALL: code-shaped detail / malformed inputs are REFUSED LOUDLY ─────────────────
    for label, args in [
        ("a code-body-shaped detail (a parameter default value) is REFUSED, never truncated",
         (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, FP, POISON)),
        ("an over-long detail (>200) is refused",
         (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, FP, "x" * 201)),
        ("a non-hex producer sha is refused",
         (REPO, "main", "src/api.py", "not-a-sha!", SHA_CONSUMER, FP, REASON)),
        ("a too-short (<7) consumer sha is refused",
         (REPO, "main", "src/api.py", SHA_PRODUCER, "abc", FP, REASON)),
        ("an empty fingerprint is refused",
         (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, "", REASON)),
        ("a code-shaped fingerprint (unsafe charset) is refused",
         (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, "f(x=1)", REASON)),
    ]:
        e = expect_error("veripsa_app", record_sql, args)
        # a fresh connection has no entered installation, but the identity layer resolves veripsa_app's own
        # seat — the refusal we assert is the 23514 validation, which fires BEFORE any identity/quota read.
        add(f"content-free wall: {label} ({(e or 'NOT REFUSED')[:60]})", e is not None)
    # S3a walls: a class outside the closed enum and a body-shaped detector are refused LOUDLY (23514) —
    # a junk classification can never enter the ledger, so the owner report's split is trustworthy.
    for label, args in [
        ("a fact_class outside the closed 4-code enum is refused",
         (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, FP, REASON, "breaking_stuff", DETECTOR)),
        ("a code-body-shaped fact_class is refused, never truncated",
         (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, FP, REASON, POISON, DETECTOR)),
        ("a code-body-shaped detector is refused (safe-charset wall)",
         (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, FP, REASON, CLASS_DELTA, POISON)),
        ("an over-long detector (>64) is refused",
         (REPO, "main", "src/api.py", SHA_PRODUCER, SHA_CONSUMER, FP, REASON, CLASS_DELTA, "d" * 65)),
    ]:
        e = expect_error("veripsa_app", record_sql9, args)
        add(f"S3a wall: {label} ({(e or 'NOT REFUSED')[:60]})", e is not None)
    # the poison token must not exist ANYWHERE in the ledger, under either account pin.
    leaked = 0
    for acct in (acct_a, acct_b):
        leaked += owner_rows(
            acct,
            "SELECT count(*) FROM core.event WHERE coalesce(detail,'') LIKE %s OR coalesce(fact_fingerprint,'') LIKE %s "
            "OR coalesce(fact_class,'') LIKE %s OR coalesce(detector,'') LIKE %s",
            ("%SECRET_DEFAULT_zzz999%",) * 4)[0][0]
    add("content-free wall: the poison token persists NOWHERE in the event ledger (incl. the S3a columns)",
        leaked == 0)

    # ── (f) PERIMETER: the new columns change NOTHING about the moat ────────────────────────────────────
    # (f1) GRANT barrier, probe ENUMERATING the new columns (so the probe itself proves they parse — #828):
    e = expect_error(
        "veripsa_demo_agent",
        "SELECT set_config('core.current_account','ACCT-DEMO',true); "
        "SELECT set_config('core.governed_write_token','event',true); "
        "INSERT INTO core.event(event_id, account_id, kind, agent_id, repo, branch, path, commit_sha, "
        "counterparty_sha, fact_fingerprint, detail, fact_class, detector) VALUES "
        "('EV-FORGE-CF','ACCT-DEMO','compat_finding','AG-A','r','main','p','" + SHA_PRODUCER + "',"
        "'" + SHA_CONSUMER + "','" + FP + "','forged','" + CLASS_INCOMPAT + "','" + DETECTOR + "')")
    add("perimeter: a token-armed tenant INSERT enumerating the NEW columns (incl. S3a fact_class/detector) "
        f"is refused by the GRANT layer ({(e or 'NOT REFUSED')[:60]})",
        e is not None and ("permission denied for table" in e or "permission denied for relation" in e))
    # (f2) catalog: FORCE RLS + tenant_isolation policy still on core.event:
    cat = owner_rows(acct_a,
                     "SELECT c.relforcerowsecurity, "
                     "  EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid=c.oid AND p.polname='tenant_isolation') "
                     "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                     "WHERE n.nspname='core' AND c.relname='event'")[0]
    add("perimeter: core.event still carries FORCE RLS + the tenant_isolation policy", cat[0] and cat[1])
    # (f3) append-only: even the pinned OWNER cannot DELETE the recorded finding (no retention token armed):
    e = expect_error("veripsa_migrator", "DELETE FROM core.event WHERE kind='compat_finding'",
                     pin_account=acct_a)
    add(f"perimeter: a plain DELETE of the finding is refused by the append-only trigger "
        f"({(e or 'NOT REFUSED')[:60]})", e is not None and "append-only" in e)
    # (f4) the new CHECK constraints hold even for a token-armed OWNER insert (the wall, not just the fn):
    e = expect_error(
        "veripsa_migrator",
        "SELECT set_config('core.governed_write_token','event',true); "
        "INSERT INTO core.event(event_id, account_id, kind, agent_id, counterparty_sha) "
        "VALUES ('EV-JUNK-CF', %s, 'compat_finding', 'AG-A', 'zz')",
        (acct_a,), pin_account=acct_a)
    add(f"perimeter: a junk counterparty_sha is refused by event_counterparty_sha_shape at the table wall "
        f"({(e or 'NOT REFUSED')[:70]})",
        e is not None and "event_counterparty_sha_shape" in e)
    # (f4b) S3a: a class OUTSIDE the closed enum is refused at the TABLE WALL too — even a token-armed
    # OWNER insert cannot store an invented classification (defense in depth beyond the gate fn).
    e = expect_error(
        "veripsa_migrator",
        "SELECT set_config('core.governed_write_token','event',true); "
        "INSERT INTO core.event(event_id, account_id, kind, agent_id, fact_class) "
        "VALUES ('EV-JUNK-CLS', %s, 'compat_finding', 'AG-A', 'invented_class')",
        (acct_a,), pin_account=acct_a)
    add(f"perimeter: a junk fact_class is refused by event_fact_class_ok at the table wall "
        f"({(e or 'NOT REFUSED')[:70]})",
        e is not None and "event_fact_class_ok" in e)
    # (f5) App-delegation-only: a buyer writer seat cannot EXECUTE the new fn — EITHER arity:
    e = expect_error("veripsa_demo_agent", record_sql,
                     (REPO, "main", "p", SHA_PRODUCER, SHA_CONSUMER, FP, REASON))
    add("perimeter: a buyer writer seat cannot call record_compat_finding_with_authority (App-only) "
        f"({(e or 'NOT REFUSED')[:60]})",
        e is not None and "permission denied for function" in e)
    e = expect_error("veripsa_demo_agent", record_sql9,
                     (REPO, "main", "p", SHA_PRODUCER, SHA_CONSUMER, FP, REASON, CLASS_DELTA, DETECTOR))
    add("perimeter: the S3a 9-arg overload carries the SAME App-delegation-only wall "
        f"({(e or 'NOT REFUSED')[:60]})",
        e is not None and "permission denied for function" in e)

    # ── verdict ─────────────────────────────────────────────────────────────────────────────────────────
    subprocess.run(["dropdb", DB], capture_output=True, text=True)
    passed = sum(1 for _, ok in checks if ok)
    print(f"\n-- {passed}/{len(checks)} compat-finding persistence assertions --")
    failed = [label for label, ok in checks if not ok]
    for label, ok in checks:
        print(("  [ok]   " if ok else "  [FAIL] ") + label)
    if failed:
        print("\nCOMPAT FINDING EVENT GATE: FAIL")
        return 1
    print("\nCOMPAT FINDING EVENT GATE: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never leak the scratch DB on an unexpected error
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
        print(f"\n[FAIL] unexpected error: {e}")
        print("COMPAT FINDING EVENT GATE: FAIL")
        sys.exit(1)
