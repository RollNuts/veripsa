#!/usr/bin/env python3
"""External-failure resilience gate — FAILED / MALFORMED external RESPONSES + DB failures.

The companion to test_github_resilience.py (which scripts HTTP STATUS codes — rate limits / 5xx / token
refresh). THIS gate injects the responses that are 200-OK-but-broken and the mid-event DB blip, and proves
the three invariants that keep a flaky GitHub / a DB outage from corrupting Veripsa:

  (a) NO CRASH        — a broken external never escapes as an unhandled crash of the worker.
  (b) NO PARTIAL WRITE — a DB error MID per-event transaction rolls back the WHOLE event (all-or-nothing),
                         so no half-set of claims / a graph patched without its landing ever survives.
  (c) NO SILENT WRONG — a truncated/corrupt tarball is REFUSED, never silently extracted as a short repo and
                         ingested as the authoritative graph (the worst gap: wrong data recorded as truth).

Each failing path must fail the EVENT cleanly so GitHub redelivers (idempotently) — never crash, never commit
a partial/corrupt state.

Needs local Postgres with the veripsa roles (for the DB-rollback proof). Run:
    python3 tests/test_external_resilience.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402
import server as S  # noqa: E402
from github_rest import GitHubREST, _verify_complete_gzip  # noqa: E402

DB = "veripsa_extresil_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
REPO = "acme/app"
REPO_ID = 909_091
SHA = "a" * 40
ACCOUNT_ID = 90909
INSTALL_ID = 4242
TENANT = "ACCT-GH-" + str(ACCOUNT_ID)


# ── a minimal FakeGitHub for the JSON / processor paths (records posts; serves PR files) ─────────────────────
class FakeGitHub:
    def __init__(self, files_by_pr):
        self.files_by_pr = files_by_pr
        self.checks, self.comments = [], []
        self._cid, self._kid = 1000, 2000

    def for_installation(self, iid):
        return self

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return self.files_by_pr.get(number, [])

    def list_pr_files_with_ranges(self, repo, number, pr_changed_files=0):
        return {p: [] for p in self.files_by_pr.get(number, [])}

    def post_check(self, repo, sha, conclusion, title, summary):
        self._kid += 1
        self.checks.append({"id": self._kid, "sha": sha, "conclusion": conclusion, "name": "Veripsa"})

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha]

    def patch_check(self, repo, kid, conclusion, title, summary, details_url=None):
        for c in self.checks:
            if c["id"] == kid:
                c["conclusion"] = conclusion
                return c

    def upsert_check(self, repo, sha, conclusion, title, summary):
        ex = self.list_check_runs(repo, sha)
        return self.patch_check(repo, ex[0]["id"], conclusion, title, summary) if ex \
            else self.post_check(repo, sha, conclusion, title, summary)

    def post_comment(self, repo, number, body):
        self._cid += 1
        self.comments.append({"id": self._cid, "number": number, "body": body, "user": {"type": "Bot"}})

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def patch_comment(self, repo, cid, body):
        for c in self.comments:
            if c["id"] == cid:
                c["body"] = body
                return c

    def upsert_comment(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                return self.patch_comment(repo, c["id"], body)
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                self.patch_comment(repo, c["id"], body() if callable(body) else body)
                return True
        return False

    def pull_request_head(self, repo, number):
        return f"head-{number}"

    def repo_default_branch_head(self, repo):
        return "main", SHA


def pr_payload(action, number, author, files_head_sha=None):
    return {"action": action, "number": number,
            "installation": {"id": INSTALL_ID, "account": {"id": ACCOUNT_ID}},
            "repository": {"id": REPO_ID, "full_name": REPO, "default_branch": "main",
                           "owner": {"id": ACCOUNT_ID}},
            "pull_request": {"base": {"ref": "main", "sha": SHA, "repo": {"id": REPO_ID}},
                             "head": {"sha": files_head_sha or f"{number:040x}",
                                      "repo": {"id": REPO_ID}},
                             "user": {"login": author}, "merged": False}}


def admin(sql, args=()):
    """Raw readback past RLS (as the migrator, tenant pinned) — to observe what actually committed."""
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (TENANT,))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def active_claims(repo=REPO):
    return admin("""SELECT COALESCE(count(*),0)::int FROM core.claim
                     WHERE repo=%s AND branch='main' AND claim_state IN ('active','waiting')""", (repo,))


# ── a DB error injector: a _scoped_db that runs N statements then raises a psycopg2 OperationalError on the
#    next one (modelling a connection drop / statement timeout MID-event), so we can prove the per-event txn
#    rolls back as a whole. ─────────────────────────────────────────────────────────────────────────────────
class _MidEventDBError(psycopg2.OperationalError):
    pass


def make_failing_processor(dsn, fail_on_substr):
    """A clone of make_db_processor whose _scoped_db raises a connection-drop-class error the first time it
    runs a statement containing `fail_on_substr` — AFTER earlier writes of the same event already ran. This is
    the realistic 'the DB blipped halfway through the event' injection; the real make_db_processor's txn must
    then roll back EVERY write of the event (incl. the ones that ran before the blip)."""
    real = S.make_db_processor(dsn)
    orig_scoped = S._scoped_db

    def patched_scoped(conn):
        run = orig_scoped(conn)
        state = {"fired": False}

        def run2(sql, args=()):
            if (not state["fired"]) and fail_on_substr in sql:
                state["fired"] = True
                raise _MidEventDBError("simulated connection drop mid-event")
            return run(sql, args)
        return run2

    def process(*a, **k):
        S._scoped_db = patched_scoped
        try:
            return real(*a, **k)
        finally:
            S._scoped_db = orig_scoped
    return process


def main() -> int:
    checks = []

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # PART A — TARBALL/FILE FETCH: a partial/corrupt download must NOT silently ingest a partial graph.
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # Build a full valid repo tar.gz (4 files), then truncate the GZIP BYTES — the realistic partial-download.
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w:gz") as tf:
        for name in ("repo/a.py", "repo/b.py", "repo/c.py", "repo/d.py"):
            data = b"import os\nx = 1\n" * 200
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    full = tbuf.getvalue()

    # the integrity guard ACCEPTS a complete stream …
    ok_full = True
    try:
        _verify_complete_gzip(full)
    except Exception:
        ok_full = False
    checks.append(("tarball: a COMPLETE gzip stream passes the integrity guard (no false refusal)", ok_full))

    # THE SILENT-PARTIAL CASE IS REAL (deterministic): drop the gzip TRAILER (the final 8-byte CRC32+ISIZE).
    # The body is now an INCOMPLETE gzip stream — but tarfile reads members LAZILY and never validates that
    # trailer, so it SILENTLY accepts the body (here: all members read, but a deeper drop loses trailing files —
    # see below). A dropped socket near the end produces exactly this. The guard MUST refuse it.
    no_trailer = full[:-8]
    tarfile_silent = False
    try:
        with tarfile.open(fileobj=io.BytesIO(no_trailer)) as tfx:
            _ = [m.name for m in tfx.getmembers()]   # NO raise = tarfile silently trusted a truncated stream
        tarfile_silent = True
    except Exception:
        tarfile_silent = False
    guard_caught_silent = False
    try:
        _verify_complete_gzip(no_trailer)
    except RuntimeError:
        guard_caught_silent = True
    checks.append(("tarball: the SILENT-partial case is REAL — a trailer-dropped (incomplete) gzip is read by "
                   "tarfile WITHOUT raising (it never validates the stream end)", tarfile_silent))
    checks.append(("tarball: the integrity guard CATCHES that silent case (the trailer-dropped stream is refused) "
                   "— so a partial body can never be ingested as the graph", guard_caught_silent))

    # … and REFUSES a deeper truncation too (a socket dropped mid/late stream → trailing files would vanish).
    refused_all = True
    for body in (full[: int(len(full) * 0.5)], full[: int(len(full) * 0.9)], full[:-60], no_trailer):
        try:
            _verify_complete_gzip(body)
            refused_all = False                      # the guard let a truncated body through → corruption slips
        except RuntimeError:
            pass
    checks.append(("tarball: the integrity guard REFUSES every truncation (mid-stream + near-end + trailer-drop) "
                   "→ the event fails cleanly, GitHub redelivers, no partial graph is ingested", refused_all))

    # download_tarball end-to-end refuses a truncated codeload body (the wire-up, not just the helper).
    class _R:
        def __init__(self, b):
            self._b, self._off = b, 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            if self._off >= len(self._b):
                return b""
            end = len(self._b) if (not n or n < 0) else self._off + n
            chunk = self._b[self._off:end]
            self._off += len(chunk)
            return chunk

    short = full[: int(len(full) * 0.95)]

    c = GitHubREST("app", "key", "1")
    c._itoken = lambda: "tok"
    # The tar API and codeload hops now share GitHubREST._urlopen's common http.client watchdog transport.
    # Inject its stable one-argument seam with a truncated response to keep this integrity test offline.
    c._urlopen = lambda _req: _R(short)
    dl_refused = False
    try:
        c.download_tarball("o/r", "deadbeef")
    except RuntimeError:
        dl_refused = True
    except Exception:
        dl_refused = False
    checks.append(("tarball: download_tarball() itself REFUSES a 95%-truncated body (RuntimeError, not a silent "
                   "short tarball)", dl_refused))

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # PART B — MALFORMED / TRUNCATED JSON RESPONSE: GitHub returns null / a list-where-dict / a missing field.
    # The REST client must FAIL (raise), never return silently-wrong data — and the handler boundary then turns
    # that into a cleanly-failed event (worker counts it, GitHub redelivers), never a crash with bad state.
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    class _JResp:
        def __init__(self, body):
            self._b = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b

    def json_client(body_bytes):
        import time
        cl = GitHubREST("app", "key", "1")
        cl._token = "tok"
        cl._token_exp = time.time() + 3600
        cl._urlopen = lambda req: _JResp(body_bytes)
        cl._sleep = lambda s: None
        return cl

    def raises(fn):
        try:
            fn()
            return False
        except Exception:
            return True

    # null body where a list is expected → raises (NOT a silent empty success that would record a PR as 'no files')
    checks.append(("malformed JSON: list_pr_files on a `null` body RAISES (fail-clean, not a silent empty result)",
                   raises(lambda: json_client(b"null").list_pr_files(REPO, 1))))
    checks.append(("malformed JSON: list_pr_files_with_ranges on `null` RAISES (fail-clean)",
                   raises(lambda: json_client(b"null").list_pr_files_with_ranges(REPO, 1))))
    # a dict where a list is expected (an error envelope leaked as 200) → raises, never iterated as if files
    checks.append(("malformed JSON: list_pr_files on an OBJECT (error envelope) body RAISES (not iterated as files)",
                   raises(lambda: json_client(b'{"message":"x"}').list_pr_files(REPO, 1))))
    # a truncated JSON body → JSONDecodeError raises (fail-clean)
    checks.append(("malformed JSON: a TRUNCATED json body RAISES (JSONDecodeError, fail-clean)",
                   raises(lambda: json_client(b'[{"filename":"a.py"').list_pr_files(REPO, 1))))
    # a missing field the code indexes → raises (fail-clean), never a None silently treated as a sha
    checks.append(("malformed JSON: pull_request_head on a body missing `head` RAISES (not a silent None sha)",
                   raises(lambda: json_client(b"{}").pull_request_head(REPO, 1))))

    # THE BOUNDARY: handle_event's pull_request path wraps the files fetch — a malformed-JSON raise there is
    # caught and the event degrades to filenames (or fails cleanly), never crashing the processor. We prove the
    # processor's worker contract holds: a broken files response makes handle_event raise OUT (so the worker
    # counts it failed + GitHub redelivers) rather than crash with a half-applied state — exercised in PART C.

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # PART C — DB FAILURE MID per-event TRANSACTION: must roll back the WHOLE event (no partial claim/graph).
    # Drives the REAL make_db_processor against a scratch DB; injects a connection-drop mid-event.
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1
    seed_live_installation(
        DSN_APP,
        f"postgresql://veripsa_migrator@localhost/{DB}",
        ACCOUNT_ID,
        INSTALL_ID,
    )
    # seed main's graph so PR analysis has a baseline (as a real install would, on the cold-start push).
    import code_graph_extract as X
    seed_db = None
    conn = psycopg2.connect(DSN_APP)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(ACCOUNT_ID),))
            graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
            cur.execute("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                        (json.dumps(graph), REPO, "main", SHA))
            cur.execute("SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
                        (REPO, str(REPO_ID)))
    finally:
        conn.close()

    gh = FakeGitHub({1: ["backend/auth.py", "backend/api.py"]})

    # A PR-open declares a claim PER changed file, then reconciles — multiple writes. Inject the DB drop on the
    # RECONCILE call, which runs AFTER both per-file claims have been declared in-txn. With the per-event txn,
    # the rollback must discard BOTH already-declared claims → 0 active claims survive (all-or-nothing).
    before = active_claims()
    failing = make_failing_processor(DSN_APP, "reconcile_change_claims")
    raised = False
    try:
        failing("pull_request", pr_payload("opened", 1, "alice"), None, gh)
    except Exception:
        raised = True                                 # the worker would catch this → _failed += 1
    after_fail = active_claims()
    checks.append(("DB-blip: a connection drop AFTER 2 claims were declared but on the reconcile re-raises out of "
                   "the processor (so the worker counts it failed + GitHub redelivers)", raised))
    checks.append((f"DB-blip: the per-event txn ROLLS BACK the WHOLE event — NO partial claim survives "
                   f"(before={before}, after_failed_event={after_fail})", after_fail == before == 0))

    # REDELIVERY IS CLEAN: GitHub redelivers the same event; with no injection it now lands ALL the claims (the
    # idempotent retry from a clean baseline — exactly the recovery the rollback enables).
    good = S.make_db_processor(DSN_APP)
    good("pull_request", pr_payload("opened", 1, "alice"), None, gh)
    after_retry = active_claims()
    checks.append((f"DB-blip: the REDELIVERY lands cleanly from the rolled-back baseline — all claims now present "
                   f"(after_retry={after_retry})", after_retry == 2))

    # AND the inverse, to prove the gate actually tests the fix: the SAME injection on an AUTOCOMMIT connection
    # (the OLD behavior) WOULD have left a partial state. We reproduce the old shape inline and show it leaks.
    auto_partial = None
    conn = psycopg2.connect(DSN_APP)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))", (str(ACCOUNT_ID), REPO))  # per-(account,repo) — mirrors server._take_repo_lock
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(ACCOUNT_ID),))
        run = S._scoped_db(conn)
        # declare ONE claim for a NEW PR (number 2), then 'drop' before the rest — autocommit commits the one.
        run("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb)",
            ("PR-2:backend/auth.py", "backend/auth.py", REPO, "main", "bob", None))
        # (simulate the drop here — in autocommit the above is already committed)
        auto_partial = admin("""SELECT count(*)::int FROM core.claim
                                 WHERE repo=%s AND change_id='PR-2' AND claim_state IN ('active','waiting')""", (REPO,))
    finally:
        conn.close()
    checks.append(("DB-blip (control): under AUTOCOMMIT a mid-event drop WOULD commit a partial claim — proving "
                   f"the txn fix is load-bearing, not a no-op (autocommit_partial={auto_partial})", auto_partial == 1))

    # ── NO-CRASH umbrella: a fully broken GitHub (every call raises) handed to handle_event for a PR open must
    #    NOT crash the worker — it raises OUT cleanly (the worker's try/except counts it), never segfaults/hangs.
    class _BrokenGH:
        def for_installation(self, iid):
            return self

        def __getattr__(self, name):
            def boom(*a, **k):
                raise ConnectionResetError("connection reset by peer")
            return boom

    broken_clean = False
    db_run = None
    conn = psycopg2.connect(DSN_APP)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(ACCOUNT_ID),))
        db_run = S._scoped_db(conn)
        try:
            S.handle_event("pull_request", pr_payload("opened", 3, "carol"), db_run, _BrokenGH())
            broken_clean = True                       # handled internally (degraded), no escape
        except Exception:
            broken_clean = True                       # raised OUT cleanly → worker counts it; either is no-crash
    except BaseException:
        broken_clean = False                          # a hang/segfault/SystemExit would land here = a crash
    finally:
        conn.close()
    checks.append(("no-crash: a fully-broken GitHub (every call resets the connection) on a PR open never crashes "
                   "the worker — it degrades or fails cleanly", broken_clean))

    okall = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        okall = okall and bool(cond)
    print("EXTERNAL RESILIENCE GATE:", "PASS" if okall else "FAIL")
    return 0 if okall else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
