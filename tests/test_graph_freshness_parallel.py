#!/usr/bin/env python3
"""GRAPH-FRESHNESS PARALLEL gate — bounded concurrency without sharing a GitHub client across workers.

Pure/offline: fake clients pin the concurrency and fail-open contracts; no DB, GitHub, or timing-only overlap
assertion. Run: python3 tests/test_graph_freshness_parallel.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import graph_freshness as F  # noqa: E402
import github_rest as G  # noqa: E402


FAIL = 0


def check(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _db(coords):
    def run(sql, args=()):
        assert "owner_graph_freshness_surface" in sql
        return {"coordinates": coords}
    return run


def _cursor_db(entries, expected, nxt, *, coverage, advance=True):
    calls = []

    def run(sql, args=()):
        if "owner_graph_freshness_surface" in sql:
            return {
                "entries": entries,
                "expected_cursor": expected,
                "next_cursor": nxt,
                "coverage_complete": coverage,
                "accounts_scanned": len(entries),
            }
        if "advance_graph_freshness_cursor_with_authority" in sql:
            calls.append(tuple(args))
            return advance
        raise AssertionError(f"unexpected SQL: {sql}")

    run.advance_calls = calls
    return run


class Router:
    def __init__(self, clients, raises=(), installations=None):
        self.clients = dict(clients)
        self.installations = dict(installations or {})
        self.raises = set(raises)
        self.calls = []
        self.installation_calls = []
        self.main_ident = threading.get_ident()

    def for_account(self, account_id):
        self.calls.append((account_id, threading.get_ident()))
        if account_id in self.raises:
            raise RuntimeError(f"no mapping for {account_id}")
        return self.clients.get(account_id)

    def for_installation(self, installation_id):
        self.installation_calls.append(
            (str(installation_id), threading.get_ident()))
        return self.installations.get(str(installation_id))


class SerialClient:
    """Records repo order and catches accidental concurrent use of this SAME client instance."""

    def __init__(self, heads, delay=0.025):
        self.heads = dict(heads)
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def repo_default_branch_head_info(self, repo):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(repo)
        try:
            time.sleep(self.delay)
            value = self.heads[repo]
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            with self.lock:
                self.active -= 1


class OverlapTracker:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def enter(self):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def leave(self):
        with self.lock:
            self.active -= 1


class BarrierClient:
    def __init__(self, barrier, tracker, sha):
        self.barrier = barrier
        self.tracker = tracker
        self.sha = sha

    def repo_default_branch_head_info(self, repo):
        self.tracker.enter()
        try:
            # This can only pass when two DIFFERENT client groups run at the same time. A serial implementation
            # breaks the barrier and returns unknown, so this is stronger and less flaky than a wall-clock bound.
            self.barrier.wait(timeout=2)
            return "main", self.sha, repo
        finally:
            self.tracker.leave()


class CappedClient:
    def __init__(self, tracker, two_active, sha):
        self.tracker = tracker
        self.two_active = two_active
        self.sha = sha

    def repo_default_branch_head_info(self, repo):
        self.tracker.enter()
        try:
            if self.tracker.max_active >= 2:
                self.two_active.set()
            # The first worker waits until the second enters. With max_workers=2 this deterministically reaches
            # two active calls, while queued groups cannot enter and push max_active over the configured bound.
            self.two_active.wait(timeout=2)
            time.sleep(0.02)
            return "main", self.sha, repo
        finally:
            self.tracker.leave()


class RacingInstallationCache(dict):
    """Makes two lock-free cache readers deterministically observe the same pre-insert miss."""

    def __init__(self):
        super().__init__()
        self.read_barrier = threading.Barrier(2)

    def get(self, key, default=None):
        value = super().get(key, default)  # capture BEFORE either racing caller can insert
        try:
            self.read_barrier.wait(timeout=0.15)
        except threading.BrokenBarrierError:
            pass
        return value


def _coord(account, repo, sha, *, age=1, installation_id=None):
    row = {
        "account_id": account,
        "repo": repo,
        "branch": "main",
        "commit_sha": sha,
        "age_seconds": age,
    }
    if installation_id is not None:
        row["github_installation_id"] = installation_id
    return row


def main():
    old_workers = F._GRAPH_FRESHNESS_WORKERS
    try:
        # 0) Current DB surfaces carry the exact durable installation id. It
        # must bypass for_account and therefore never scan a capped fleet-wide
        # /app/installations map for a normal freshness sample.
        exact_sha = "e" * 40
        exact_client = SerialClient({
            "exact/repo": ("main", exact_sha, "exact/repo"),
        }, delay=0)
        exact_router = Router(
            {},
            installations={"9000001": exact_client},
        )
        exact_rows = F.graph_freshness_all(
            _db([
                _coord(
                    "ACCT-GH-1",
                    "exact/repo",
                    exact_sha,
                    installation_id="9000001",
                ),
            ]),
            exact_router,
        )
        check(
            exact_router.calls == []
            and [iid for iid, _tid in exact_router.installation_calls]
            == ["9000001"]
            and exact_rows[0]["behind"] is False,
            "durable installation id routes freshness directly; "
            "fleet-wide for_account inventory is never touched",
        )

        # 1) for_account stays on the calling thread/account-once; grouping uses returned object identity, not
        # account. Two account aliases share one client, so its repos are serial and duplicate (client,repo) reads
        # collapse to one. Output still follows the original coordinate order.
        F._GRAPH_FRESHNESS_WORKERS = 4
        z_sha, a_sha = "z" * 40, "a" * 40
        shared = SerialClient({
            "z/repo": ("main", z_sha, "z/repo"),
            "a/repo": ("main", a_sha, "a/repo"),
        })
        router = Router({"acct-a": shared, "acct-alias": shared})
        source = [
            _coord("acct-a", "z/repo", z_sha, age=11),
            _coord("acct-alias", "z/repo", z_sha, age=12),
            _coord("acct-a", "a/repo", a_sha, age=13),
            _coord("acct-alias", "z/repo", z_sha, age=14),
        ]
        rows = F.graph_freshness_all(_db(source), router)
        check([a for a, _tid in router.calls] == ["acct-a", "acct-alias"]
              and all(tid == router.main_ident for _a, tid in router.calls),
              "for_account runs on the main/calling thread exactly once per account")
        check(shared.calls == ["z/repo", "a/repo"],
              "actual client identity grouping: same (client,repo) resolves once, in first-seen repo order")
        check(shared.max_active == 1,
              "one actual GitHub client is never shared across workers (group repos strictly serial)")
        check([(r["repo"], r["age_seconds"]) for r in rows]
              == [("z/repo", 11), ("z/repo", 12), ("a/repo", 13), ("z/repo", 14)],
              "parallel results join back in original coordinate order (duplicates deterministic)")

        # 2) Two distinct clients must overlap. A two-party barrier cannot pass under serial execution.
        barrier = threading.Barrier(2)
        overlap = OverlapTracker()
        left_sha, right_sha = "l" * 40, "r" * 40
        left = BarrierClient(barrier, overlap, left_sha)
        right = BarrierClient(barrier, overlap, right_sha)
        router2 = Router({"right": right, "left": left})
        rows2 = F.graph_freshness_all(_db([
            _coord("right", "right/repo", right_sha),
            _coord("left", "left/repo", left_sha),
        ]), router2)
        check(overlap.max_active == 2 and [r["head_sha"] for r in rows2] == [right_sha, left_sha],
              "different client identities execute concurrently (barrier passed) while output order is stable")

        # 3) The validated worker count is a fleet-wide cap across distinct client groups.
        F._GRAPH_FRESHNESS_WORKERS = 2
        capped_tracker = OverlapTracker()
        two_active = threading.Event()
        clients = {}
        capped_coords = []
        for i in range(7):
            account, repo, sha = f"cap-{i}", f"org/repo-{i}", str(i) * 40
            clients[account] = CappedClient(capped_tracker, two_active, sha)
            capped_coords.append(_coord(account, repo, sha))
        capped_rows = F.graph_freshness_all(_db(capped_coords), Router(clients))
        check(capped_tracker.max_active == 2 and all(r["behind"] is False for r in capped_rows),
              "VERIPSA_GRAPH_FRESHNESS_WORKERS bounds active distinct-client groups (cap=2, observed=2)")

        # 3b) The live watchdog and /freshz sampler can overlap. Their separate graph_freshness_all calls must
        # still share BOTH invariants: one mutable GitHubREST client is never concurrent, and the worker knob is
        # a process-wide budget rather than a per-invocation multiplier.
        F._GRAPH_FRESHNESS_WORKERS = 2
        same_client = SerialClient({
            "same/one": ("main", "1" * 40, "same/one"),
            "same/two": ("main", "2" * 40, "same/two"),
        }, delay=0.06)
        outer_start = threading.Barrier(2)
        outer_errors = []

        def run_same(account, repo, sha):
            try:
                outer_start.wait(timeout=2)
                F.graph_freshness_all(_db([_coord(account, repo, sha)]),
                                      Router({account: same_client}))
            except Exception as exc:
                outer_errors.append(exc)

        same_threads = [
            threading.Thread(target=run_same, args=("same-a", "same/one", "1" * 40)),
            threading.Thread(target=run_same, args=("same-b", "same/two", "2" * 40)),
        ]
        for thread in same_threads:
            thread.start()
        for thread in same_threads:
            thread.join(timeout=3)
        check(not outer_errors and all(not thread.is_alive() for thread in same_threads)
              and same_client.max_active == 1,
              "overlapping samples share the per-client lock (same client max_active=1 process-wide)")

        # Real GitHubREST.for_account() lazily calls the lock-free for_installation() cache. Without serializing
        # the ROOT account-resolution phase, two samples can both observe a cache miss and receive distinct child
        # objects for the same installation; identity-keyed HEAD locks then cannot protect the shared token scope.
        # The custom dict captures the value before a two-reader barrier, making that historical race deterministic
        # without any network. With the root lock, the first read times out/inserts and the second reuses it.
        root = G.GitHubREST("app", "not-a-real-key", "111")
        root._account_install_map = {"1": "222"}
        root._installations = RacingInstallationCache()
        install_tracker = OverlapTracker()
        install_head_barrier = threading.Barrier(2)
        install_client_ids = set()
        original_head_info = G.GitHubREST.repo_default_branch_head_info

        def tracked_install_head(client, repo):
            install_client_ids.add(id(client))
            install_tracker.enter()
            try:
                try:
                    install_head_barrier.wait(timeout=0.15)
                except threading.BrokenBarrierError:
                    pass
                return "main", "i" * 40, repo
            finally:
                install_tracker.leave()

        G.GitHubREST.repo_default_branch_head_info = tracked_install_head
        install_start = threading.Barrier(2)
        install_errors = []

        def run_install_race():
            try:
                install_start.wait(timeout=2)
                F.graph_freshness_all(
                    _db([_coord("ACCT-GH-1", "install/repo", "i" * 40)]), root)
            except Exception as exc:
                install_errors.append(exc)

        install_threads = [threading.Thread(target=run_install_race) for _ in range(2)]
        try:
            for thread in install_threads:
                thread.start()
            for thread in install_threads:
                thread.join(timeout=3)
        finally:
            G.GitHubREST.repo_default_branch_head_info = original_head_info
        check(not install_errors and all(not thread.is_alive() for thread in install_threads)
              and len(install_client_ids) == 1 and install_tracker.max_active == 1,
              "overlapping samples serialize real for_account cache resolution and reuse one installation client")

        cross_tracker = OverlapTracker()
        cross_two_active = threading.Event()
        cross_start = threading.Barrier(2)
        cross_errors = []

        def run_distinct(prefix):
            clients_for_call = {}
            coords_for_call = []
            for i in range(4):
                account, repo, sha = f"{prefix}-{i}", f"{prefix}/repo-{i}", str(i + 3) * 40
                clients_for_call[account] = CappedClient(cross_tracker, cross_two_active, sha)
                coords_for_call.append(_coord(account, repo, sha))
            try:
                cross_start.wait(timeout=2)
                F.graph_freshness_all(_db(coords_for_call), Router(clients_for_call))
            except Exception as exc:
                cross_errors.append(exc)

        cross_threads = [threading.Thread(target=run_distinct, args=(prefix,))
                         for prefix in ("watchdog", "freshz")]
        for thread in cross_threads:
            thread.start()
        for thread in cross_threads:
            thread.join(timeout=4)
        check(not cross_errors and all(not thread.is_alive() for thread in cross_threads)
              and cross_tracker.max_active == 2 and F._HEAD_ACTIVE_GROUPS == 0,
              "overlapping samples share the process-wide worker budget (cap=2, observed=2)")

        # 4) A whole task/future failure leaves only that client group unknown. Successful groups still resolve;
        # source order is unchanged. Patch the narrow helper to exercise the future.result isolation path itself.
        F._GRAPH_FRESHNESS_WORKERS = 4
        good_sha = "g" * 40
        good = SerialClient({"good/repo": ("main", good_sha, "good/repo")}, delay=0)
        bad = SerialClient({}, delay=0)
        router4 = Router({"bad": bad, "good": good})
        source4 = [
            _coord("bad", "bad/one", "1" * 40),
            _coord("good", "good/repo", good_sha),
            _coord("bad", "bad/two", "2" * 40),
        ]
        original_resolver = F._resolve_head_group

        def explode_one_group(client, repos, *args):
            if client is bad:
                raise RuntimeError("group boom")
            return original_resolver(client, repos, *args)

        F._resolve_head_group = explode_one_group
        try:
            rows4 = F.graph_freshness_all(_db(source4), router4)
        finally:
            F._resolve_head_group = original_resolver
        check([r["repo"] for r in rows4] == ["bad/one", "good/repo", "bad/two"]
              and rows4[0]["behind"] is None and rows4[2]["behind"] is None
              and rows4[1]["head_sha"] == good_sha and rows4[1]["behind"] is False,
              "group future exception is isolated: only that client group becomes unknown")

        # A normal per-repo HEAD error retains the old finer-grained behavior: later repos on the same serial group
        # still resolve (the future itself did not fail).
        repo_good_sha = "q" * 40
        partial = SerialClient({
            "partial/bad": RuntimeError("repo boom"),
            "partial/good": ("main", repo_good_sha, "partial/good"),
        }, delay=0)
        rows_partial = F.graph_freshness_all(_db([
            _coord("partial", "partial/bad", "p" * 40),
            _coord("partial", "partial/good", repo_good_sha),
        ]), Router({"partial": partial}))
        check(rows_partial[0]["behind"] is None and rows_partial[1]["behind"] is False
              and partial.calls == ["partial/bad", "partial/good"],
              "per-repo HEAD failure remains fail-open and does not skip later repos in its serial group")

        # 5) cap applies to the raw source slice (as before), junk entries are skipped/normalized safely, and a
        # None client is never submitted to a worker/network call. The beyond-cap account is never even resolved.
        malformed_source = [
            None,
            _coord("none", "ghost/repo", "x" * 40, age=7),
            17,
            {"account_id": 99, "repo": 123, "branch": [], "commit_sha": 456, "age_seconds": "old"},
            _coord("beyond", "must/not-run", "y" * 40),
        ]
        router5 = Router({"none": None, "": None, "beyond": SerialClient({})})
        rows5 = F.graph_freshness_all(_db(malformed_source), router5, cap=4)
        check([a for a, _tid in router5.calls] == ["none", ""] and len(rows5) == 2,
              "cap is applied before malformed-entry filtering; beyond-cap account is untouched")
        check(rows5[0]["repo"] == "ghost/repo" and rows5[0]["head_sha"] is None
              and rows5[0]["behind"] is None,
              "None client makes zero HEAD/network calls and yields honestly unknown")
        check(rows5[1] == {"repo": "", "branch": "", "stored_sha": None, "head_sha": None,
                           "behind": None, "live_default": None, "age_seconds": None},
              "malformed coordinate fields normalize safely without perturbing the return schema")

        # A throwing account resolver also degrades only that account to None; a neighbor client remains visible.
        ok_sha = "o" * 40
        ok_client = SerialClient({"ok/repo": ("main", ok_sha, "ok/repo")}, delay=0)
        rows_resolver = F.graph_freshness_all(_db([
            _coord("mapping-boom", "unknown/repo", "u" * 40),
            _coord("ok", "ok/repo", ok_sha),
        ]), Router({"ok": ok_client}, raises={"mapping-boom"}))
        check(rows_resolver[0]["behind"] is None and rows_resolver[1]["behind"] is False,
              "for_account exception is isolated to that account; neighboring client group still resolves")

        # Executor construction itself is also observational/fail-open. No group can run, so all otherwise valid
        # coordinates remain unknown; importantly, the surface still returns in order instead of raising.
        original_pool = F.ThreadPoolExecutor

        class BrokenPool:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("pool boom")

        F.ThreadPoolExecutor = BrokenPool
        try:
            pool_rows = F.graph_freshness_all(_db([
                _coord("pool-a", "pool/a", "a" * 40),
                _coord("pool-b", "pool/b", "b" * 40),
            ]), Router({
                "pool-a": SerialClient({"pool/a": ("main", "a" * 40, "pool/a")}, delay=0),
                "pool-b": SerialClient({"pool/b": ("main", "b" * 40, "pool/b")}, delay=0),
            }))
        finally:
            F.ThreadPoolExecutor = original_pool
        check([r["repo"] for r in pool_rows] == ["pool/a", "pool/b"]
              and all(r["head_sha"] is None and r["behind"] is None for r in pool_rows),
              "pool-level exception never escapes; all unrun groups stay unknown in source order")

        # 6) The observer has an absolute wall. Once less than one transport
        # timeout remains it starts no new HEAD call and returns explicit
        # incomplete/Unknown instead of letting ThreadPoolExecutor.__exit__
        # wait indefinitely for queued work.
        old_sample_seconds = F._GRAPH_FRESHNESS_SAMPLE_SECONDS
        deadline_client = SerialClient({
            "deadline/repo": (
                "main", "d" * 40, "deadline/repo",
            ),
        }, delay=0)
        F._GRAPH_FRESHNESS_SAMPLE_SECONDS = 0.01
        started = time.monotonic()
        try:
            deadline_rows = F.graph_freshness_all(
                _db([
                    _coord(
                        "deadline",
                        "deadline/repo",
                        "d" * 40,
                    ),
                ]),
                Router({"deadline": deadline_client}),
            )
        finally:
            F._GRAPH_FRESHNESS_SAMPLE_SECONDS = old_sample_seconds
        elapsed = time.monotonic() - started
        check(
            elapsed < 0.5
            and deadline_client.calls == []
            and deadline_rows[0]["behind"] is None
            and deadline_rows.timed_out is True
            and deadline_rows.coverage_complete is False,
            "absolute sample budget starts no unsafe late call and "
            "returns incomplete/Unknown promptly",
        )

        # 7) /freshz and the watchdog never queue behind one another. The
        # losing sampler gets a list-compatible explicit incomplete result.
        F._FRESHNESS_SAMPLE_LOCK.acquire()
        try:
            overlapping = F.graph_freshness_all(
                _db([]), Router({}))
        finally:
            F._FRESHNESS_SAMPLE_LOCK.release()
        check(
            isinstance(overlapping, F.FreshnessSample)
            and overlapping == []
            and overlapping.coverage_complete is False
            and overlapping.timed_out is True,
            "overlapping freshness sample returns immediate Unknown "
            "instead of becoming another long-lived waiter",
        )

        # 8) New durable surfaces advance only the consumed page. A complete
        # page that started at the fleet origin can prove full coverage; a
        # tail page cannot greenwash the unseen prefix even when it wraps.
        cursor_sha = "c" * 40
        cursor_client = SerialClient({
            "cursor/repo": (
                "main", cursor_sha, "cursor/repo",
            ),
        }, delay=0)
        cursor_router = Router(
            {},
            installations={"808001": cursor_client},
        )
        cursor_entry = {
            "account_id": "ACCT-GH-000001",
            "coordinate": _coord(
                "ACCT-GH-000001",
                "cursor/repo",
                cursor_sha,
                installation_id="808001",
            ),
        }
        full_db = _cursor_db(
            [cursor_entry],
            {"after_account": None, "cycle": 4},
            {"after_account": None, "cycle": 5},
            coverage=True,
        )
        full_sample = F.graph_freshness_all(
            full_db, cursor_router)
        check(
            full_sample.coverage_complete is True
            and full_sample.cursor_healthy is True
            and full_db.advance_calls == [
                (None, 4, None, 5),
            ],
            "origin-to-tail consumed page advances exact CAS and alone "
            "may prove complete fleet coverage",
        )

        tail_db = _cursor_db(
            [cursor_entry],
            {
                "after_account": "ACCT-GH-000100",
                "cycle": 5,
            },
            {"after_account": None, "cycle": 6},
            coverage=True,
        )
        tail_sample = F.graph_freshness_all(
            tail_db, cursor_router)
        check(
            tail_sample.coverage_complete is False
            and tail_sample.cursor_healthy is True
            and tail_db.advance_calls == [
                ("ACCT-GH-000100", 5, None, 6),
            ],
            "tail wrap advances fairly but remains partial/Unknown because "
            "the current observation did not see the fleet prefix",
        )

        lost_cas_db = _cursor_db(
            [cursor_entry],
            {"after_account": None, "cycle": 6},
            {"after_account": None, "cycle": 7},
            coverage=True,
            advance=False,
        )
        lost_cas_sample = F.graph_freshness_all(
            lost_cas_db, cursor_router)
        check(
            lost_cas_sample.cursor_healthy is False
            and lost_cas_sample.coverage_complete is False,
            "concurrent/stale cursor CAS loss is explicit Unknown, never "
            "false complete",
        )

        # At a deadline, graph-empty accounts preceding unfinished network
        # work are safe to checkpoint, but the unfinished account is not
        # skipped. This is what keeps every page finite without re-reading a
        # permanently empty prefix forever.
        prefix_db = _cursor_db(
            [
                {
                    "account_id": "ACCT-GH-000001",
                    "coordinate": None,
                },
                {
                    "account_id": "ACCT-GH-000002",
                    "coordinate": _coord(
                        "ACCT-GH-000002",
                        "deadline/repo",
                        "d" * 40,
                        installation_id="808002",
                    ),
                },
            ],
            {"after_account": None, "cycle": 8},
            {"after_account": None, "cycle": 9},
            coverage=True,
        )
        prefix_router = Router(
            {},
            installations={
                "808002": SerialClient({
                    "deadline/repo": (
                        "main", "d" * 40, "deadline/repo",
                    ),
                }, delay=0),
            },
        )
        old_sample_seconds = F._GRAPH_FRESHNESS_SAMPLE_SECONDS
        F._GRAPH_FRESHNESS_SAMPLE_SECONDS = 0.01
        try:
            prefix_sample = F.graph_freshness_all(
                prefix_db, prefix_router)
        finally:
            F._GRAPH_FRESHNESS_SAMPLE_SECONDS = old_sample_seconds
        check(
            prefix_sample.timed_out is True
            and prefix_sample.coverage_complete is False
            and prefix_db.advance_calls == [
                (None, 8, "ACCT-GH-000001", 8),
            ],
            "deadline checkpoints only the contiguous consumed prefix and "
            "never skips an unfinished account",
        )
    finally:
        F._GRAPH_FRESHNESS_WORKERS = old_workers

    # 9) Pin the shipped default and both validation edges through a fresh module import (the production path).
    def import_with(value):
        env = dict(os.environ)
        if value is None:
            env.pop("VERIPSA_GRAPH_FRESHNESS_WORKERS", None)
        else:
            env["VERIPSA_GRAPH_FRESHNESS_WORKERS"] = value
        code = ("import sys; sys.path.insert(0, " + repr(os.path.join(ROOT, "github-app")) + "); "
                "import graph_freshness as f; print(f._GRAPH_FRESHNESS_WORKERS)")
        return subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
                              capture_output=True, text=True)

    default = import_with(None)
    max_ok = import_with("32")
    invalid = [(value, import_with(value)) for value in ("0", "33", "not-an-int")]
    check(default.returncode == 0 and default.stdout.strip() == "4"
          and max_ok.returncode == 0 and max_ok.stdout.strip() == "32",
          "worker knob defaults to 4 and accepts the validated maximum 32")
    check(all(p.returncode != 0 and "VERIPSA_GRAPH_FRESHNESS_WORKERS" in (p.stdout + p.stderr)
              for _value, p in invalid),
          "worker knob rejects 0, >32, and malformed values loudly with the variable named")

    print("GRAPH-FRESHNESS PARALLEL GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
