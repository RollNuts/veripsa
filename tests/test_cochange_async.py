#!/usr/bin/env python3
"""CO-CHANGE OFF-WORKER gate — co-change population must NOT sit on the event worker, must land in the RIGHT
tenant, and must FAIL CLOSED (never a cross-tenant write).

co-change is an ADVISORY second signal whose feeder is a git clone (I/O-bound, up to history_clone's timeout).
Running it inline on the single webhook worker would stall every queued PR/push behind a slow clone AND hold the
per-(account,repo) advisory lock for the clone's whole duration. So backfill_repo hands it to a dedicated pool
(populate_cochange_async) and returns immediately; the pool clones/extracts holding NO lock, then BRIEFLY
tenant-pins + repo-locks to store — copying the proven self_heal_main_graph off-path pattern.

Proves against a real scratch-tenant DB:
  (1) NON-BLOCKING: populate_cochange_async RETURNS IMMEDIATELY even when the clone is slow (a 2s clone must not
      block the caller — the event worker is freed);
  (2) it still POPULATES, in the RIGHT tenant (the pool task derives the owning account from
      gh.installation_account_id() and pins it — editing auth then surfaces api for that tenant);
  (3) CROSS-TENANT ISOLATION holds through the async pin: a 2nd tenant sees NOTHING;
  (4) FAIL CLOSED: if the owning account can't be resolved, NOTHING is written (never an unrouted/cross-tenant
      write);
  (5) CONTENT-FREE: no body / message / SHA in the stored values.

Run:  python3 tests/test_cochange_async.py   (needs local Postgres with the veripsa roles + git)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import ingest  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402

DB = "veripsa_ccasync_" + str(os.getpid())
REPO = "acme/cc"
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def tenant(install_id):
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("SET search_path=core")
        c.execute("SELECT core.enter_installation_with_authority(%s)", (install_id,))

    def run(sql, args=()):
        with conn.cursor() as c:
            c.execute(sql, args)
            row = c.fetchone()
            return row[0] if row else None
    return run


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def _git(d, *a):
    subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, check=True)


def _commit(d, files, tok):
    for f in files:
        p = os.path.join(d, f)
        os.makedirs(os.path.dirname(p) or d, exist_ok=True)
        open(p, "a").write(f"// SECRET_BODY_{tok}\n")
        _git(d, "add", f)
    _git(d, "commit", "-m", f"SECRET_MSG_{tok}", "--no-verify")


def _craft(d):
    _git(d, "init", "-q"); _git(d, "config", "user.email", "t@e.com"); _git(d, "config", "user.name", "T")
    _git(d, "config", "commit.gpgsign", "false")
    for i in range(5):
        _commit(d, ["backend/auth.py", "backend/api.py"], f"p{i}")
    _commit(d, ["backend/auth.py"], "as1"); _commit(d, ["backend/auth.py"], "as2")
    _commit(d, ["backend/api.py"], "is1"); _commit(d, ["backend/api.py"], "is2")
    for k in range(40):
        _commit(d, [f"misc/m{k}.py"], f"bg{k}")


class FakeGh:
    """No network. installation_account_id() returns the owning-account id (the tenant key the pool task pins);
    history_clone sleeps `clone_sleep`s (to prove the dispatch did NOT block on it) then does a local NO-CHECKOUT
    clone of the crafted repo."""

    def __init__(self, src, account="111", clone_sleep=0.0):
        self.src, self.account, self.clone_sleep = src, account, clone_sleep

    def installation_account_id(self):
        return self.account

    def history_clone(self, repo, branch, dest, timeout=120):
        if self.clone_sleep:
            time.sleep(self.clone_sleep)
        subprocess.run(["git", "clone", "--no-checkout", "--quiet", self.src, dest],
                       capture_output=True, text=True, check=True)
        return dest


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    app_dsn = f"postgresql://veripsa_app@localhost/{DB}"
    owner_dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    seed_live_installation(app_dsn, owner_dsn, "111", "111")
    seed_live_installation(app_dsn, owner_dsn, "222", "222")
    os.environ["VERIPSA_DSN"] = app_dsn  # the pool task opens its OWN conn from this

    with tempfile.TemporaryDirectory() as src:
        _craft(src)

        # (1) NON-BLOCKING: a 2s clone must not block the caller. Dispatch returns a Future immediately.
        gh = FakeGh(src, account="111", clone_sleep=2.0)
        t0 = time.time()
        fut = ingest.populate_cochange_async(gh, REPO, "main")
        dt = time.time() - t0
        chk(fut is not None and dt < 0.5,
            f"populate_cochange_async RETURNS IMMEDIATELY ({dt:.2f}s) despite a 2s clone — the worker is freed")

        # (2) it still populates, in the RIGHT tenant (await the pool task, then read as ACCT-GH-111).
        res = fut.result(timeout=30)
        chk(isinstance(res, dict) and res.get("ok") and res.get("pairs_found", 0) >= 1,
            f"the pool task populated co-change (got {res})")
        A = tenant("111")
        parts = _j(A("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)", (REPO, ["backend/auth.py"], 3, 0.4)))
        api = next((p for p in parts if p.get("partner") == "backend/api.py"), None)
        chk(api is not None and float(api.get("lift", 0)) >= 2.0,
            f"...in the RIGHT tenant (ACCT-GH-111): editing auth surfaces api (got {api})")

        # (3) CROSS-TENANT: tenant 222 reading the SAME repo sees NOTHING (the async pin didn't leak the tenant).
        b_parts = _j(tenant("222")("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)", (REPO, ["backend/auth.py"], 3, 0.4)))
        chk(b_parts == [], f"cross-tenant isolation holds through the async pin (2nd tenant sees nothing; got {b_parts})")

        # (4) FAIL CLOSED: an unresolved owning account writes NOTHING (never an unrouted/cross-tenant write).
        gh_noacct = FakeGh(src, account=None)
        res2 = ingest.populate_cochange_async(gh_noacct, "acme/never", "main").result(timeout=30)
        chk(isinstance(res2, dict) and res2.get("ok") is False,
            f"fail-closed: an unresolved owning account reports ok:False (got {res2})")
        empty = _j(A("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)", ("acme/never", ["x.py"], 3, 0.4)))
        chk(empty == [], f"...and wrote NOTHING for the unresolved-account repo (got {empty})")

        # (5) CONTENT-FREE.
        chk("SECRET" not in json.dumps(parts), "content-free: stored/returned values carry no body / message / SHA")

    ok = all(checks)
    print("CO-CHANGE OFF-WORKER GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
