#!/usr/bin/env python3
"""Killable graph-extraction gate (offline; no GitHub or Postgres required).

This guards the worker-liveness boundary behind the 787-second head-of-line
incident.  Repository-controlled archive/filesystem work runs in a fresh exec
child; the parent enforces one wall-clock allowance, kills the whole child
process group on timeout, and performs the only authoritative DB write.
Timeout itself writes nothing. Inside an event it propagates through the typed
shared cancellation; background/boot/CLI work receives an ordinary
infrastructure failure. Only a deterministic configured repository-shape cap
may store Unknown.

Run:
  python3.12 tests/test_graph_extract_isolation.py
"""
from __future__ import annotations

import builtins
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import event_budget  # noqa: E402
import ingest  # noqa: E402


FAIL = 0


def check(condition: bool, label: str) -> None:
    global FAIL
    print(("PASS: " if condition else "FAIL: ") + label)
    if not condition:
        FAIL += 1


def _expect_resource_limit(fn, reason: str) -> None:
    try:
        fn()
    except ingest.GraphExtractionResourceLimit as exc:
        check(exc.reason == reason, f"resource refusal is classified as {reason}")
    else:
        check(False, f"resource refusal is classified as {reason}")


def test_normal_child() -> None:
    with tempfile.TemporaryDirectory(prefix="veripsa_iso_ok_") as workspace:
        source = os.path.join(workspace, "source")
        os.mkdir(source)
        with open(os.path.join(source, "app.py"), "w", encoding="utf-8") as fh:
            fh.write("def answer():\n    return 42\n")
        with open(os.path.join(source, "lib.ts"), "w", encoding="utf-8") as fh:
            fh.write("export const answer = 42;\n")
        with open(os.path.join(source, "app.ts"), "w", encoding="utf-8") as fh:
            fh.write(
                "import { answer } from './lib.ts';\n"
                "export const result = answer;\n"
            )

        result = ingest._run_isolated_extractor(
            workspace,
            mode="incremental",
            source_root=source,
            universe_paths=["app.py", "app.ts", "lib.ts"],
        )
        graph = json.loads(result.get("graph_json") or "{}")
        file_paths = {
            node.get("path")
            for node in graph.get("nodes", [])
            if isinstance(node, dict) and node.get("kind") == "file"
        }
        edge_identities = {
            (edge.get("src"), edge.get("dst"), edge.get("kind"))
            for edge in graph.get("edges", [])
            if isinstance(edge, dict)
        }
        check(result.get("status") == "ok", "fresh exec child returns an OK result")
        check(
            file_paths == {"app.py", "app.ts", "lib.ts"},
            "child returns the expected content-free file graph",
        )
        check(
            ("app.ts", "lib.ts", "imports") in edge_identities,
            "isolated child loads the installed TypeScript grammar and preserves its import edge",
        )
        check(
            all("ipc" not in str(path) for path in file_paths),
            "IPC JSON is outside the repository source root and never becomes a graph node",
        )


def test_ingest_import_is_cwd_independent() -> None:
    """Import ingest with ONLY github-app exposed, from an unrelated cwd."""
    with tempfile.TemporaryDirectory(prefix="veripsa_ingest_import_") as unrelated_cwd:
        probe = (
            "import os,sys\n"
            "app=sys.argv[1]\n"
            "root=sys.argv[2]\n"
            "sys.path.insert(0,app)\n"
            "import ingest\n"
            "loaded=os.path.realpath(ingest._GRAPH_EXTRACTOR.__file__)\n"
            "expected=os.path.realpath(os.path.join(root,'code_graph_extract.py'))\n"
            "assert loaded == expected, (loaded, expected)\n"
            "assert ingest._GRAPH_EXTRACTOR is sys.modules['code_graph_extract']\n"
            "print('cwd-independent-import-ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe, os.path.join(ROOT, "github-app"), ROOT],
            cwd=unrelated_cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    check(
        result.returncode == 0 and "cwd-independent-import-ok" in result.stdout,
        "ingest loads the owned root extractor with only github-app on sys.path from an unrelated cwd "
        f"(rc={result.returncode}, err={result.stderr[-200:]!r})",
    )


def test_timeout_kills_and_reaps_child() -> None:
    """A FIFO makes the real extractor block in its file guard without sleeps."""
    with tempfile.TemporaryDirectory(prefix="veripsa_iso_hang_") as workspace:
        source = os.path.join(workspace, "source")
        os.mkdir(source)
        fifo = os.path.join(source, "hang.py")
        os.mkfifo(fifo)

        spawned = []
        real_popen = ingest.subprocess.Popen
        old_timeout = ingest._GRAPH_EXTRACT_TIMEOUT_SECONDS

        def recording_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned.append(proc)
            return proc

        ingest.subprocess.Popen = recording_popen
        ingest._GRAPH_EXTRACT_TIMEOUT_SECONDS = 1
        started = time.monotonic()
        try:
            try:
                ingest._run_isolated_extractor(
                    workspace,
                    mode="incremental",
                    source_root=source,
                    universe_paths=["hang.py"],
                )
            except ingest.GraphExtractionInfrastructureError:
                timed_out = True
            else:
                timed_out = False
        finally:
            ingest.subprocess.Popen = real_popen
            ingest._GRAPH_EXTRACT_TIMEOUT_SECONDS = old_timeout

        elapsed = time.monotonic() - started
        check(
            timed_out,
            "background repository-controlled blocking raises an ordinary infrastructure timeout",
        )
        check(elapsed < 3.0, f"the timeout is a wall-clock bound (elapsed={elapsed:.2f}s)")
        check(len(spawned) == 1 and spawned[0].poll() is not None, "timed-out child leader exited")
        pid_gone = False
        reaped = False
        if spawned:
            try:
                os.kill(spawned[0].pid, 0)
            except ProcessLookupError:
                pid_gone = True
            try:
                os.waitpid(spawned[0].pid, os.WNOHANG)
            except ChildProcessError:
                reaped = True
        check(pid_gone, "no timed-out child PID remains")
        check(reaped, "the parent synchronously reaps the timed-out child (no zombie)")


def test_timeout_classification_tracks_event_context() -> None:
    outside = ingest._graph_timeout_error("background extractor timeout")
    token = event_budget.begin(2.0)
    try:
        inside = ingest._graph_timeout_error("event extractor timeout")
    finally:
        event_budget.end(token)
    check(
        isinstance(outside, ingest.GraphExtractionInfrastructureError)
        and isinstance(outside, Exception),
        "background/boot graph timeout remains an ordinary catchable infrastructure failure",
    )
    check(
        isinstance(inside, ingest.EventBudgetExceeded)
        and not isinstance(inside, Exception),
        "in-event graph timeout is BaseException delivery cancellation",
    )


def test_slow_kill_reap_is_handed_off() -> None:
    """Even an uninterruptible killed child cannot wedge cleanup indefinitely."""
    daemon_waiting = threading.Event()
    allow_reap = threading.Event()
    bounded_waits = []

    class _SlowKilledProc:
        pid = 987654321

        def kill(self):
            return None

        def wait(self, timeout=None):
            if timeout is not None:
                bounded_waits.append(timeout)
                raise RuntimeError("synthetic non-timeout wait failure")
            daemon_waiting.set()
            allow_reap.wait(2.0)
            return -9

    real_killpg = ingest.os.killpg
    old_kill_wait = ingest._GRAPH_CHILD_KILL_WAIT_SECONDS
    ingest.os.killpg = lambda *args, **kwargs: None
    ingest._GRAPH_CHILD_KILL_WAIT_SECONDS = 0.05
    acquired = ingest._GRAPH_EXTRACT_CHILD_LOCK.acquire(timeout=1.0)
    started = time.monotonic()
    try:
        caller_owns_slot = ingest._kill_graph_child(_SlowKilledProc()) if acquired else True
        elapsed = time.monotonic() - started
        waiting_seen = daemon_waiting.wait(0.5)
        slot_held = ingest._GRAPH_EXTRACT_CHILD_LOCK.locked()
        handed_off = ingest.graph_extraction_liveness(180.0)
    finally:
        ingest.os.killpg = real_killpg
        ingest._GRAPH_CHILD_KILL_WAIT_SECONDS = old_kill_wait

    check(acquired, "slow-reap test acquired the single extractor slot")
    check(
        not caller_owns_slot and elapsed < 0.5,
        f"any bounded-wait failure hands cleanup to a daemon (elapsed={elapsed:.2f}s)",
    )
    check(
        bounded_waits and bounded_waits[0] <= 0.051,
        "synchronous post-kill wait uses the short cleanup ceiling",
    )
    check(waiting_seen and slot_held, "daemon reaper retains the one-child slot until reap")
    check(
        handed_off.get("reaper_owned") is True
        and handed_off.get("healthy") is True,
        "reaper ownership remains visible after event cleanup without flapping before the hard bound",
    )

    allow_reap.set()
    deadline = time.monotonic() + 1.0
    while ingest._GRAPH_EXTRACT_CHILD_LOCK.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    check(
        not ingest._GRAPH_EXTRACT_CHILD_LOCK.locked(),
        "daemon releases the extractor slot only after wait completes",
    )


def test_graph_slot_liveness_has_an_absolute_bound() -> None:
    """A never-returning reaper cannot leave worker health green forever."""
    now = [0.0]
    slot = ingest._TenantFairGraphSlot(
        clock=lambda: now[0],
        turn_seconds=30.0,
        account_provider=lambda: ("event", "tenant-a"),
    )
    acquired = slot.acquire(timeout=0.1)
    now[0] = 60.0
    marked = slot.mark_reaper_owned()
    now[0] = 179.999
    before = slot.liveness_snapshot(180.0)
    now[0] = 180.0
    at_bound = slot.liveness_snapshot(180.0)
    slot.release()
    after = slot.liveness_snapshot(180.0)

    check(acquired and marked, "graph slot records the killed-child reaper handoff")
    check(
        before.get("healthy") is True
        and before.get("active_seconds") < 180.0,
        "normal extraction/reaping remains healthy before the absolute hold bound",
    )
    check(
        at_bound.get("healthy") is False
        and at_bound.get("stuck") is True
        and at_bound.get("reaper_owned") is True
        and at_bound.get("active_seconds") == 180.0,
        "never-returning reaper fails process liveness exactly at the hard bound",
    )
    check(
        after.get("healthy") is True
        and after.get("locked") is False
        and after.get("active_seconds") is None,
        "a proven reap clears the liveness failure and all hold timing",
    )

    long_now = [0.0]
    long_slot = ingest._TenantFairGraphSlot(
        clock=lambda: long_now[0],
        account_provider=lambda: ("background", ""),
    )
    long_slot.acquire(timeout=0.1)
    long_now[0] = 180.0
    legitimate = long_slot.liveness_snapshot(
        180.0, active_hard_seconds=926.0)
    long_now[0] = 926.0
    long_wedged = long_slot.liveness_snapshot(
        180.0, active_hard_seconds=926.0)
    long_slot.release()
    check(
        legitimate.get("healthy") is True
        and legitimate.get("hard_seconds") == 926.0
        and legitimate.get("background_owned") is True
        and long_wedged.get("healthy") is False,
        "a background extraction may use its explicitly long parser allowance",
    )

    event_now = [0.0]
    event_slot = ingest._TenantFairGraphSlot(
        clock=lambda: event_now[0],
        account_provider=lambda: ("event", "tenant-a"),
    )
    event_slot.acquire(timeout=0.1)
    event_now[0] = 180.0
    event_wedged = event_slot.liveness_snapshot(
        180.0, active_hard_seconds=926.0)
    event_slot.release()
    check(
        event_wedged.get("healthy") is False
        and event_wedged.get("hard_seconds") == 180.0
        and event_wedged.get("background_owned") is False,
        "a 900s background override cannot hide an event-owned graph slot past 180s",
    )

    old_timeout = ingest._GRAPH_EXTRACT_TIMEOUT_SECONDS
    ingest._GRAPH_EXTRACT_TIMEOUT_SECONDS = 900
    public_acquired = ingest._GRAPH_EXTRACT_CHILD_LOCK.acquire(timeout=0.1)
    try:
        configured = ingest.graph_extraction_liveness(180.0)
    finally:
        if public_acquired:
            ingest._GRAPH_EXTRACT_CHILD_LOCK.release()
        ingest._GRAPH_EXTRACT_TIMEOUT_SECONDS = old_timeout
    check(
        public_acquired
        and configured.get("hard_seconds") == 926.0
        and configured.get("background_owned") is True,
        "public graph liveness applies a configured 900s parser allowance to a background owner",
    )


def test_archive_resource_caps() -> None:
    def member_cap() -> None:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as tf:
            for name in ("repo/a.py", "repo/b.py"):
                member = tarfile.TarInfo(name)
                member.size = 0
                tf.addfile(member, io.BytesIO())
        payload.seek(0)
        with tempfile.TemporaryDirectory(prefix="veripsa_iso_members_") as target:
            with tarfile.open(fileobj=payload, mode="r|*") as tf:
                ingest._safe_extractall(
                    tf, target, max_members=1, max_expanded_bytes=1024)

    def expanded_cap() -> None:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as tf:
            member = tarfile.TarInfo("repo/a.py")
            member.size = 4
            tf.addfile(member, io.BytesIO(b"1234"))
        payload.seek(0)
        with tempfile.TemporaryDirectory(prefix="veripsa_iso_bytes_") as target:
            with tarfile.open(fileobj=payload, mode="r|*") as tf:
                ingest._safe_extractall(
                    tf, target, max_members=10, max_expanded_bytes=3)

    _expect_resource_limit(member_cap, "archive_member_count_cap")
    _expect_resource_limit(expanded_cap, "archive_expanded_bytes_cap")


def test_child_environment_is_secret_free() -> None:
    originals = {
        key: os.environ.get(key)
        for key in ("VERIPSA_DSN", "GH_PRIVATE_KEY", "GITHUB_TOKEN")
    }
    try:
        os.environ["VERIPSA_DSN"] = "postgresql://secret"
        os.environ["GH_PRIVATE_KEY"] = "private-secret"
        os.environ["GITHUB_TOKEN"] = "token-secret"
        child_env = ingest._graph_child_env()
    finally:
        for key, value in originals.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check("VERIPSA_DSN" not in child_env, "extractor child receives no database DSN")
    check("GH_PRIVATE_KEY" not in child_env, "extractor child receives no GitHub private key")
    check("GITHUB_TOKEN" not in child_env, "extractor child receives no GitHub token")


def test_event_reserve_prevents_spawn() -> None:
    spawned = []
    real_popen = ingest.subprocess.Popen
    token = event_budget.begin(0.02)

    def forbidden_popen(*args, **kwargs):
        spawned.append(True)
        raise AssertionError("child must not start without terminalization reserve")

    ingest.subprocess.Popen = forbidden_popen
    try:
        with tempfile.TemporaryDirectory(prefix="veripsa_iso_reserve_") as workspace:
            try:
                ingest._run_isolated_extractor(
                    workspace,
                    mode="incremental",
                    source_root=workspace,
                    universe_paths=[],
                )
            except ingest.EventBudgetExceeded:
                refused = True
            else:
                refused = False
    finally:
        ingest.subprocess.Popen = real_popen
        event_budget.end(token)
    check(refused, "event DB reserve refuses extraction before the shared deadline is consumed")
    check(not spawned, "no child is spawned when the event lacks its parent DB-write reserve")


def test_graph_slot_is_acquired_before_download() -> None:
    """Two keyed workers cannot each retain a capped tarball before parsing."""
    real_runner = ingest._run_isolated_extractor
    first_download_started = threading.Event()
    release_first_download = threading.Event()
    state_lock = threading.Lock()
    download_calls = []
    active_downloads = 0
    max_active_downloads = 0
    outcomes = {}

    class _BlockingTarGH:
        def download_tarball(self, repo, sha):
            nonlocal active_downloads, max_active_downloads
            with state_lock:
                download_calls.append(repo)
                active_downloads += 1
                max_active_downloads = max(max_active_downloads, active_downloads)
            try:
                if repo == "acme/one":
                    first_download_started.set()
                    release_first_download.wait(2.0)
                return b"bounded-tarball-fixture"
            finally:
                with state_lock:
                    active_downloads -= 1

    def completed_runner(*args, **kwargs):
        return {
            "status": "ok",
            "graph_json": '{"nodes":[],"edges":[]}',
            "files": 0,
            "edges": 0,
        }

    def db(_sql, _params=None):
        return None

    gh = _BlockingTarGH()
    ingest._run_isolated_extractor = completed_runner

    def run_one(name: str) -> None:
        token = event_budget.begin(10.0)
        started = time.monotonic()
        try:
            ingest._full_ingest(
                db, gh, f"acme/{name}", "main", name[0] * 40, captured_at=None)
            outcomes[name] = ("ok", time.monotonic() - started)
        except BaseException as exc:
            outcomes[name] = (exc, time.monotonic() - started)
        finally:
            event_budget.end(token)

    first = threading.Thread(target=run_one, args=("one",), daemon=True)
    second = threading.Thread(target=run_one, args=("two",), daemon=True)
    try:
        first.start()
        first_seen = first_download_started.wait(0.5)
        second.start()
        second.join(0.5)
        with state_lock:
            calls_while_first_held = list(download_calls)
            concurrent_peak = max_active_downloads
        release_first_download.set()
        first.join(1.0)
        second.join(1.0)

        second_outcome, second_elapsed = outcomes.get("two", (None, float("inf")))
        check(first_seen, "first worker begins its tarball download while owning the graph slot")
        check(
            isinstance(second_outcome, ingest.IntentionalDeliveryDeferral)
            and second_elapsed <= 0.3,
            f"contending event defers within 100ms+slack before fetch "
            f"(exc={type(second_outcome).__name__ if second_outcome else None}, "
            f"elapsed={second_elapsed:.3f}s)",
        )
        check(
            calls_while_first_held == ["acme/one"] and concurrent_peak == 1,
            f"two workers never start tarball downloads together "
            f"(calls={calls_while_first_held}, peak={concurrent_peak})",
        )
        check(outcomes.get("one", (None,))[0] == "ok", "slot owner completes after download is released")

        # Once the first complete lifetime exits, the deferred repository can
        # retry and fetch normally; the lease was not leaked by nested runner use.
        retry_result = ingest._full_ingest(
            db, gh, "acme/two", "main", "2" * 40, captured_at=None)
        check(
            retry_result.get("mode") == "full" and download_calls == ["acme/one", "acme/two"],
            "deferred repository can acquire the released slot and download on retry",
        )
    finally:
        release_first_download.set()
        first.join(1.0)
        second.join(1.0)
        ingest._run_isolated_extractor = real_runner


class _RecordingDB:
    def __init__(self):
        self.ingests = []
        self.patches = 0

    def __call__(self, sql, params=None):
        if "coordinate_file_paths" in sql:
            return ["app.py"]
        if "ingest_graph_with_authority" in sql:
            self.ingests.append(params)
        if "patch_graph_with_authority" in sql:
            self.patches += 1
        return None


class _BlobGH:
    def get_file_at(self, repo, path, sha):
        return b"def answer():\n    return 42\n"


def test_incremental_timeout_preserves_graph() -> None:
    db = _RecordingDB()
    real_runner = ingest._run_isolated_extractor
    real_full = ingest._full_ingest
    full_calls = []

    def timeout(*args, **kwargs):
        raise ingest.EventBudgetExceeded("isolated graph extraction timed out")

    ingest._run_isolated_extractor = timeout

    def forbidden_full(*args, **kwargs):
        full_calls.append(True)
        raise AssertionError("incremental extraction timeout must not multiply into a full clone")

    ingest._full_ingest = forbidden_full
    try:
        try:
            ingest._reingest_graph(
                db,
                _BlobGH(),
                "acme/repo",
                "main",
                "a" * 40,
                payload={},
                decision="normal",
                changed=["app.py"],
                removed=[],
                head_time=None,
            )
        except ingest.EventBudgetExceeded:
            propagated = True
        else:
            propagated = False
    finally:
        ingest._run_isolated_extractor = real_runner
        ingest._full_ingest = real_full

    check(propagated, "incremental timeout propagates as EventBudgetExceeded")
    check(not db.ingests, "incremental timeout does not replace the healthy graph")
    check(db.patches == 0, "timeout never writes a partial incremental patch")
    check(not full_calls, "incremental timeout never falls back to a second full extraction")


def test_full_timeout_preserves_graph() -> None:
    db = _RecordingDB()
    real_runner = ingest._run_isolated_extractor

    class _TarGH:
        def download_tarball(self, repo, sha):
            return b"staged but never opened by the injected timeout"

    def timeout(*args, **kwargs):
        raise ingest.EventBudgetExceeded("isolated graph extraction timed out")

    ingest._run_isolated_extractor = timeout
    try:
        try:
            ingest._full_ingest(
                db, _TarGH(), "acme/repo", "main", "d" * 40, captured_at=None)
        except ingest.EventBudgetExceeded:
            propagated = True
        else:
            propagated = False
    finally:
        ingest._run_isolated_extractor = real_runner

    check(propagated, "full extraction timeout propagates as EventBudgetExceeded")
    check(not db.ingests and db.patches == 0, "full timeout leaves the healthy graph untouched")


def test_only_deterministic_caps_can_store_unknown() -> None:
    db = _RecordingDB()
    try:
        ingest._store_unindexed_graph(
            db,
            "acme/repo",
            "main",
            "e" * 40,
            None,
            reason="extract_timeout",
        )
    except RuntimeError:
        refused = True
    else:
        refused = False
    check(refused, "non-deterministic timeout is refused by the Unknown writer")
    check(not db.ingests, "the Unknown writer cannot mutate DB for timeout")


def test_extractor_contention_preserves_healthy_graph() -> None:
    db = _RecordingDB()
    real_incremental = ingest._incremental_ingest
    real_full = ingest._full_ingest
    full_calls = []

    def busy(*args, **kwargs):
        raise ingest.EventBudgetExceeded("graph extractor remained busy")

    def forbidden_full(*args, **kwargs):
        full_calls.append(True)
        raise AssertionError("contention must not become a full clone")

    ingest._incremental_ingest = busy
    ingest._full_ingest = forbidden_full
    try:
        try:
            ingest._reingest_graph(
                db,
                _BlobGH(),
                "acme/repo",
                "main",
                "b" * 40,
                payload={},
                decision="normal",
                changed=["app.py"],
                removed=[],
                head_time=None,
            )
        except ingest.EventBudgetExceeded:
            propagated = True
        else:
            propagated = False
    finally:
        ingest._incremental_ingest = real_incremental
        ingest._full_ingest = real_full

    check(propagated, "extractor contention propagates as the shared event-budget failure")
    check(not full_calls, "extractor contention does not multiply into full extraction")
    check(not db.ingests and db.patches == 0, "extractor contention leaves the healthy graph untouched")


def test_ambiguous_child_failure_preserves_healthy_graph() -> None:
    db = _RecordingDB()
    real_incremental = ingest._incremental_ingest
    real_full = ingest._full_ingest
    full_calls = []

    def child_signal(*args, **kwargs):
        raise ingest.GraphExtractionInfrastructureError(
            "isolated graph extractor exited on signal 9")

    def forbidden_full(*args, **kwargs):
        full_calls.append(True)
        raise AssertionError("ambiguous child failure must not become a full clone")

    ingest._incremental_ingest = child_signal
    ingest._full_ingest = forbidden_full
    try:
        try:
            ingest._reingest_graph(
                db,
                _BlobGH(),
                "acme/repo",
                "main",
                "f" * 40,
                payload={},
                decision="normal",
                changed=["app.py"],
                removed=[],
                head_time=None,
            )
        except ingest.GraphExtractionInfrastructureError:
            propagated = True
        else:
            propagated = False
    finally:
        ingest._incremental_ingest = real_incremental
        ingest._full_ingest = real_full

    check(propagated, "ambiguous child signal propagates as infrastructure failure")
    check(not full_calls, "ambiguous child signal does not multiply into full extraction")
    check(not db.ingests and db.patches == 0, "ambiguous child signal leaves the healthy graph untouched")


def test_archive_staging_failure_preserves_healthy_graph() -> None:
    db = _RecordingDB()

    class _TarGH:
        def download_tarball(self, repo, sha):
            return b"archive bytes are never parsed because staging fails"

    def fail_archive(path, mode="r", *args, **kwargs):
        if os.path.basename(os.fspath(path)) == "archive.tar" and mode == "xb":
            raise OSError("simulated host disk failure")
        return builtins.open(path, mode, *args, **kwargs)

    had_open = "open" in ingest.__dict__
    prior_open = ingest.__dict__.get("open")
    ingest.open = fail_archive
    try:
        try:
            ingest._full_ingest(
                db, _TarGH(), "acme/repo", "main", "c" * 40, captured_at=None)
        except RuntimeError:
            propagated = True
        else:
            propagated = False
    finally:
        if had_open:
            ingest.open = prior_open
        else:
            del ingest.open

    check(propagated, "archive staging failure propagates as infrastructure failure")
    check(not db.ingests and db.patches == 0, "archive staging failure leaves the healthy graph untouched")


def main() -> int:
    print("=== GRAPH EXTRACTION ISOLATION GATE ===")
    test_normal_child()
    test_ingest_import_is_cwd_independent()
    test_timeout_kills_and_reaps_child()
    test_timeout_classification_tracks_event_context()
    test_slow_kill_reap_is_handed_off()
    test_graph_slot_liveness_has_an_absolute_bound()
    test_archive_resource_caps()
    test_child_environment_is_secret_free()
    test_event_reserve_prevents_spawn()
    test_graph_slot_is_acquired_before_download()
    test_incremental_timeout_preserves_graph()
    test_full_timeout_preserves_graph()
    test_only_deterministic_caps_can_store_unknown()
    test_extractor_contention_preserves_healthy_graph()
    test_ambiguous_child_failure_preserves_healthy_graph()
    test_archive_staging_failure_preserves_healthy_graph()
    if FAIL:
        print(f"GRAPH EXTRACTION ISOLATION GATE: FAIL ({FAIL} check(s))")
        return 1
    print("GRAPH EXTRACTION ISOLATION GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
