#!/usr/bin/env python3
"""Graph workspace cleanup never owns a webhook worker or graph slot."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
sys.path.insert(0, APP)

# The production singleton reads this once at import.  Two reservations let
# the test prove one active blocked cleanup plus one bounded pending cleanup.
os.environ["VERIPSA_GRAPH_WORKSPACE_CLEANUP_CAP"] = "2"

import event_budget  # noqa: E402
import graph_workspace as GW  # noqa: E402
import ingest  # noqa: E402
from delivery_queue import DeliveryStore, with_delivery_key  # noqa: E402
from event_queue import EventQueue  # noqa: E402


FAIL = 0


def check(condition, label):
    global FAIL
    print(("  [PASS] " if condition else "  [FAIL] ") + label)
    if not condition:
        FAIL += 1


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class _GraphDB:
    def __init__(self):
        self.ingests = 0
        self.patches = 0

    def __call__(self, sql, _params=None):
        if "ingest_graph_with_authority" in sql:
            self.ingests += 1
        if "patch_graph_with_authority" in sql:
            self.patches += 1
            return {"edges_total": 0}
        if "coordinate_inert_imports" in sql:
            return []
        return None


class _GraphGH:
    def __init__(self):
        self.downloads = []
        self.blobs = []

    def download_tarball(self, repo, sha):
        self.downloads.append((repo, sha))
        return b"small-staged-archive"

    def get_file_at(self, repo, path, sha):
        self.blobs.append((repo, path, sha))
        return b"def answer():\n    return 42\n"


def _completed_runner(*_args, **_kwargs):
    return {
        "status": "ok",
        "graph_json": '{"nodes":[],"edges":[]}',
        "files": 0,
        "edges": 0,
    }


def test_workspace_janitor() -> None:
    janitor = GW._JANITOR
    real_rmtree = janitor._rmtree
    real_mkdtemp = GW.tempfile.mkdtemp
    real_runner = ingest._run_isolated_extractor
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_calls = []
    cleanup_lock = threading.Lock()
    mkdtemp_calls = []
    runner_calls = []
    first_thread = None

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        mkdtemp_calls.append(path)
        return path

    def controlled_rmtree(path):
        with cleanup_lock:
            cleanup_calls.append(path)
            call_number = len(cleanup_calls)
        if call_number == 1:
            cleanup_entered.set()
            # This deliberately has no timeout.  The test releases it only
            # after proving the graph ingest returned, so completion before
            # release is causal evidence that workspace exit does not own the
            # real filesystem deletion.
            allow_cleanup.wait()
        real_rmtree(path)

    def completed_runner(*args, **kwargs):
        runner_calls.append((args, kwargs))
        return _completed_runner(*args, **kwargs)

    janitor._rmtree = controlled_rmtree
    GW.tempfile.mkdtemp = recording_mkdtemp
    ingest._run_isolated_extractor = completed_runner
    db = _GraphDB()
    gh = _GraphGH()
    thread_ident = janitor.stats()["thread_ident"]
    thread_name = janitor.stats()["thread_name"]

    try:
        # Full ingest owns the real graph slot.  Its workspace cleanup enters a
        # deliberately stuck rmtree, but context exit and the whole ingest
        # return without waiting for that filesystem call.
        first_result = {}
        first_errors = []
        first_done = threading.Event()

        def run_first_ingest():
            try:
                first_result["value"] = ingest._full_ingest(
                    db,
                    gh,
                    "tenant-a/one",
                    "main",
                    "a" * 40,
                    captured_at=None,
                )
            except BaseException as exc:
                first_errors.append(exc)
            finally:
                first_done.set()

        started = time.monotonic()
        first_thread = threading.Thread(
            target=run_first_ingest,
            name="veripsa-graph-workspace-ingest-test",
        )
        first_thread.start()
        cleanup_started = cleanup_entered.wait(2.0)
        returned_before_release = (
            cleanup_started
            and first_done.wait(2.0)
            and not allow_cleanup.is_set()
        )
        first_elapsed = time.monotonic() - started
        first = first_result.get("value") or {}
        slot_reacquired = ingest._GRAPH_EXTRACT_CHILD_LOCK.acquire(timeout=0.05)
        if slot_reacquired:
            ingest._GRAPH_EXTRACT_CHILD_LOCK.release()
        check(
            first.get("mode") == "full"
            and cleanup_started
            and returned_before_release
            and not first_errors,
            f"full workspace exit returns before the blocked real rmtree is released "
            f"(elapsed={first_elapsed:.4f}s, done={first_done.is_set()}, "
            f"errors={[type(exc).__name__ for exc in first_errors]})",
        )
        check(
            returned_before_release and slot_reacquired,
            "global graph slot is released before blocked workspace cleanup is released",
        )
        if not returned_before_release:
            # Avoid cascading waits when this causal boundary regresses.  The
            # finally block releases the controlled rmtree and joins the
            # helper before restoring any injected production seams.
            return

        # Incremental graph staging uses the same janitor.  Its cleanup becomes
        # the one bounded pending ticket behind the stuck active deletion.
        second = ingest._incremental_ingest(
            db,
            gh,
            "tenant-a/two",
            "main",
            "b" * 40,
            ["app.py"],
            [],
            ["app.py"],
            captured_at=None,
        )
        saturated = janitor.stats()
        check(
            second.get("mode") == "patch"
            and saturated["reserved"] == 2,
            f"active plus pending workspaces consume the validated cap "
            f"(stats={saturated})",
        )
        check(
            janitor.stats()["thread_ident"] == thread_ident
            and sum(1 for thread in threading.enumerate()
                    if thread.name == thread_name) == 1,
            "multiple cleanups use one fixed daemon and never create a thread per workspace",
        )

        typed_downloads = len(gh.downloads)
        typed_extracts = len(runner_calls)
        typed_mkdtemps = len(mkdtemp_calls)
        token = event_budget.begin(10.0)
        typed_started = time.monotonic()
        typed_defer = None
        try:
            ingest._full_ingest(
                db, gh, "tenant-a/typed", "main", "0" * 40, captured_at=None)
        except ingest.IntentionalDeliveryDeferral as exc:
            typed_defer = exc
        finally:
            typed_elapsed = time.monotonic() - typed_started
            event_budget.end(token)
        check(
            isinstance(typed_defer, ingest.IntentionalDeliveryDeferral)
            and typed_elapsed < 0.2
            and typed_defer.not_before.tzinfo is not None
            and typed_defer.not_before.utcoffset() is not None
            and typed_defer.reason == "graph workspace cleanup busy; yielding keyed worker lane"
            and len(gh.downloads) == typed_downloads
            and len(runner_calls) == typed_extracts
            and len(mkdtemp_calls) == typed_mkdtemps,
            f"event saturation raises a typed short deferral before any graph work "
            f"(elapsed={typed_elapsed:.4f}s)",
        )

        # Production-path proof: the saturated graph job is a durable,
        # attempt-neutral defer and the same single worker immediately runs a
        # healthy event for another account.  Capacity is checked before
        # mkdtemp, tarball download, or extractor launch.
        payloads = {
            "limited-delivery": {
                "graph": True,
                "installation": {"account": {"id": "tenant-a"}},
                "repository": {"id": 101, "full_name": "tenant-a/limited"},
            },
            "healthy-delivery": {
                "installation": {"account": {"id": "tenant-b"}},
                "repository": {"id": 202, "full_name": "tenant-b/healthy"},
            },
        }
        state = {
            key: {"status": "queued", "attempts": 0, "not_before": None}
            for key in payloads
        }
        leases = {"value": 0}
        deferred = []
        finished = []
        released = []
        store = DeliveryStore("postgresql://unused")

        def claim(key):
            leases["value"] += 1
            state[key]["status"] = "processing"
            state[key]["attempts"] += 1
            return {
                "claimed": True,
                "lease_generation": leases["value"],
                "event_type": "push",
                "payload": payloads[key],
            }

        def defer(key, not_before, reason, lease_generation):
            deferred.append((key, not_before, reason, lease_generation))
            state[key]["status"] = "queued"
            state[key]["attempts"] -= 1
            state[key]["not_before"] = not_before
            return True

        def finish(key, lease_generation):
            finished.append((key, lease_generation))
            state[key]["status"] = "done"
            return True

        store.claim = claim
        store.defer = defer
        store.finish = finish
        store.release = lambda key, error, lease: released.append((key, error, lease))
        healthy_started = []
        saturation_downloads = len(gh.downloads)
        saturation_extracts = len(runner_calls)
        saturation_mkdtemps = len(mkdtemp_calls)

        def processor(_event_type, payload, _db, _gh, coalesce=None):
            if payload.get("graph"):
                return ingest._full_ingest(
                    db,
                    gh,
                    payload["repository"]["full_name"],
                    "main",
                    "c" * 40,
                    captured_at=None,
                )
            healthy_started.append(time.monotonic())
            return {"ok": True}

        worker = EventQueue(
            None,
            None,
            store.wrap_processor(processor),
            account_of=lambda payload: str(
                payload.get("installation", {}).get("account", {}).get("id", "")),
            repo_of=lambda payload: payload.get("repository", {}).get("full_name"),
            worker_count=1,
            retry_attempts=3,
            retry_base_seconds=1,
        )
        worker.submit(
            "push",
            with_delivery_key(payloads["limited-delivery"], "limited-delivery"),
            "limited-delivery",
        )
        worker.submit(
            "push",
            with_delivery_key(payloads["healthy-delivery"], "healthy-delivery"),
            "healthy-delivery",
        )
        worker_started = time.monotonic()
        worker.start()
        drained = worker.wait_idle(2.0)
        healthy_latency = (
            healthy_started[0] - worker_started
            if healthy_started else float("inf")
        )
        not_before = state["limited-delivery"]["not_before"]
        defer_delay = (
            (not_before - datetime.now(timezone.utc)).total_seconds()
            if not_before is not None else -1.0
        )
        check(
            drained
            and healthy_latency < 0.2
            and worker.failed() == 0
            and worker.retried() == 0
            and worker.processed() == 1
            and worker.claim_deferred() == 1,
            f"saturated janitor defers tenant A attempt-neutrally and tenant B runs immediately "
            f"(latency={healthy_latency:.4f}s, failed={worker.failed()}, "
            f"retried={worker.retried()}, deferred={worker.claim_deferred()}, "
            f"processed={worker.processed()}, drained={drained})",
        )
        check(
            state["limited-delivery"]["status"] == "queued"
            and state["limited-delivery"]["attempts"] == 0
            and not_before is not None
            and not_before.tzinfo is not None
            and not_before.utcoffset() is not None
            and 3.0 <= defer_delay <= 5.5
            and len(deferred) == 1
            and deferred[0][2] == "graph workspace cleanup busy; yielding keyed worker lane"
            and released == []
            and [key for key, _lease in finished] == ["healthy-delivery"],
            f"durable row keeps its attempt and receives a short timezone-aware schedule "
            f"(state={state['limited-delivery']}, delay={defer_delay:.2f}s)",
        )
        check(
            len(gh.downloads) == saturation_downloads
            and len(runner_calls) == saturation_extracts
            and len(mkdtemp_calls) == saturation_mkdtemps,
            "capacity is refused before mkdtemp, repository download, and graph extraction",
        )

        # Background/boot work has no durable delivery to schedule.  It gets a
        # catchable infrastructure error under the same no-download boundary.
        try:
            ingest._full_ingest(
                db, gh, "background/limited", "main", "d" * 40, captured_at=None)
        except ingest.GraphExtractionInfrastructureError:
            background_error = True
        else:
            background_error = False
        check(
            background_error
            and len(gh.downloads) == saturation_downloads
            and len(mkdtemp_calls) == saturation_mkdtemps,
            "background saturation is a graph infrastructure error before download",
        )

        # Once the controlled filesystem operation returns, the sole janitor
        # drains both reservations and new graph work recovers normally.
        allow_cleanup.set()
        recovered_capacity = wait_until(
            lambda: janitor.stats()["reserved"] == 0,
            timeout=1.0,
        )
        recovery = ingest._full_ingest(
            db, gh, "tenant-c/recovered", "main", "e" * 40, captured_at=None)
        recovery_drained = wait_until(
            lambda: janitor.stats()["reserved"] == 0,
            timeout=1.0,
        )
        check(
            recovered_capacity
            and recovery.get("mode") == "full"
            and recovery_drained
            and len(gh.downloads) == saturation_downloads + 1,
            f"janitor releases reservations after the stall and later graph work recovers "
            f"(stats={janitor.stats()})",
        )

        # A concrete rmtree failure retains its reservation until absence is
        # proven. Delete first, then raise: the bounded retry observes ENOENT,
        # treats the postcondition as satisfied, and only then releases.
        failures_before = janitor.stats()["failed"]
        fail_once = {"value": True}

        def one_failure(path):
            real_rmtree(path)
            if fail_once["value"]:
                fail_once["value"] = False
                raise OSError("controlled cleanup failure")

        janitor._rmtree = one_failure
        failed_cleanup_result = ingest._full_ingest(
            db, gh, "tenant-d/failure", "main", "f" * 40, captured_at=None)
        failure_retained = wait_until(
            lambda: (
                janitor.stats()["reserved"] == 1
                and janitor.stats()["quarantined"] == 1
                and janitor.stats()["failed"] >= failures_before + 1
            ),
            timeout=0.5,
        )
        failure_released = wait_until(
            lambda: (
                janitor.stats()["reserved"] == 0
                and janitor.stats()["quarantined"] == 0
            ),
            timeout=2.0,
        )
        check(
            failed_cleanup_result.get("mode") == "full"
            and failure_retained
            and failure_released
            and janitor.stats()["thread_alive"]
            and janitor.stats()["thread_ident"] == thread_ident,
            f"rmtree failure quarantines its reservation until retry proves deletion "
            f"(stats={janitor.stats()})",
        )
    finally:
        allow_cleanup.set()
        if first_thread is not None:
            first_thread.join()
        janitor._rmtree = real_rmtree
        GW.tempfile.mkdtemp = real_mkdtemp
        ingest._run_isolated_extractor = real_runner
        wait_until(lambda: janitor.stats()["reserved"] == 0, timeout=1.0)


def test_repeated_failures_remain_bounded() -> None:
    """A failing filesystem cannot turn released permits into unlimited trees."""
    real_mkdtemp = GW.tempfile.mkdtemp
    real_rmtree = GW.shutil.rmtree
    created = []
    calls = {}
    calls_lock = threading.Lock()
    failing = {"value": True}

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    def controlled_rmtree(path):
        with calls_lock:
            calls[path] = calls.get(path, 0) + 1
        if failing["value"]:
            raise OSError("controlled persistent cleanup failure")
        real_rmtree(path)

    janitor = GW.GraphWorkspaceJanitor(
        2,
        rmtree=controlled_rmtree,
        thread_name="veripsa-graph-workspace-retry-test",
        retry_base_seconds=0.01,
        retry_max_seconds=0.04,
    )
    GW.tempfile.mkdtemp = recording_mkdtemp
    paths = []
    try:
        leases = [janitor.reserve(prefix="veripsa_retry_") for _ in range(2)]
        paths = [lease.path for lease in leases]
        close_started = time.monotonic()
        for lease in leases:
            lease.close()
        close_elapsed = time.monotonic() - close_started
        repeatedly_failed = wait_until(
            lambda: (
                janitor.stats()["failed"] >= 4
                and janitor.stats()["reserved"] == 2
                and janitor.stats()["quarantined"] == 2
            ),
            timeout=1.0,
        )
        # The retry loop must keep making progress without spinning on a bad
        # filesystem. With 10ms→40ms capped backoff, two paths remain well
        # below this generous attempt ceiling over the observation window.
        time.sleep(0.12)

        refused = 0
        for _ in range(20):
            try:
                janitor.reserve(prefix="veripsa_retry_overflow_")
            except GW.GraphWorkspaceCapacityError:
                refused += 1
        saturated = janitor.stats()
        with calls_lock:
            attempts = sum(calls.values())
        check(
            close_elapsed < 0.05
            and repeatedly_failed
            and 4 <= attempts <= 16
            and refused == 20
            and len(created) == 2
            and len(paths) == janitor.capacity
            and all(os.path.isdir(path) for path in paths)
            and saturated["reserved"] == janitor.capacity
            and saturated["pending"] + saturated["active"] == janitor.capacity,
            f"repeated rmtree errors retain at most cap paths and admission fails before mkdtemp "
            f"(elapsed={close_elapsed:.4f}s, attempts={attempts}, stats={saturated})",
        )

        # Once the filesystem recovers (or an operator removes the trees), the
        # bounded retries prove absence/deletion, release permits, and normal
        # workspace admission resumes.
        failing["value"] = False
        recovered = wait_until(
            lambda: janitor.stats()["reserved"] == 0,
            timeout=1.0,
        )
        resumed = janitor.reserve(prefix="veripsa_retry_recovered_")
        resumed_path = resumed.path
        resumed.close()
        resumed_cleaned = wait_until(
            lambda: janitor.stats()["reserved"] == 0
            and not os.path.exists(resumed_path),
            timeout=1.0,
        )
        check(
            recovered
            and all(not os.path.exists(path) for path in paths)
            and resumed_cleaned
            and janitor.stats()["quarantined"] == 0
            and janitor.stats()["thread_alive"],
            f"successful retry releases quarantined permits and graph admission resumes "
            f"(stats={janitor.stats()})",
        )
    finally:
        failing["value"] = False
        GW.tempfile.mkdtemp = real_mkdtemp
        wait_until(lambda: janitor.stats()["reserved"] == 0, timeout=1.0)
        for path in paths:
            if os.path.exists(path):
                real_rmtree(path)


def test_saturated_janitor_has_a_hard_liveness_bound() -> None:
    """A live daemon stuck in rmtree cannot defer every tenant forever."""
    now = [0.0]
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    real_rmtree = GW.shutil.rmtree
    calls = {"value": 0}

    def blocked_first_cleanup(path):
        calls["value"] += 1
        if calls["value"] == 1:
            cleanup_entered.set()
            allow_cleanup.wait(3.0)
        real_rmtree(path)

    janitor = GW.GraphWorkspaceJanitor(
        2,
        rmtree=blocked_first_cleanup,
        thread_name="veripsa-graph-workspace-liveness-test",
        clock=lambda: now[0],
    )
    leases = []
    try:
        leases = [
            janitor.reserve(prefix="veripsa_liveness_")
            for _ in range(2)
        ]
        for lease in leases:
            lease.close()
        entered = cleanup_entered.wait(0.5)
        saturated = wait_until(
            lambda: (
                janitor.stats()["active"] == 1
                and janitor.stats()["pending"] == 1
                and janitor.stats()["saturated"]
            ),
            timeout=0.5,
        )
        now[0] = 179.999
        before = janitor.liveness_snapshot(180.0)
        now[0] = 180.0
        at_bound = janitor.liveness_snapshot(180.0)
        production_janitor = GW._JANITOR
        GW._JANITOR = janitor
        try:
            combined_at_bound = ingest.graph_extraction_liveness(180.0)
        finally:
            GW._JANITOR = production_janitor
        check(
            entered and saturated
            and before.get("healthy") is True
            and before.get("saturated_seconds") < 180.0,
            "short cleanup saturation remains bounded backpressure, not a false restart",
        )
        check(
            at_bound.get("healthy") is False
            and at_bound.get("stuck") is True
            and at_bound.get("thread_alive") is True
            and at_bound.get("saturated") is True
            and at_bound.get("no_progress_seconds") == 180.0,
            "live janitor with a full cap and no progress fails health at the absolute bound",
        )
        check(
            combined_at_bound.get("healthy") is False
            and combined_at_bound.get(
                "workspace_janitor", {}).get("stuck") is True,
            "graph extraction liveness composes janitor saturation into the Render restart signal",
        )

        allow_cleanup.set()
        recovered = wait_until(
            lambda: janitor.stats()["reserved"] == 0,
            timeout=1.0,
        )
        after = janitor.liveness_snapshot(180.0)
        check(
            recovered
            and after.get("healthy") is True
            and after.get("saturated") is False
            and after.get("reserved") == 0,
            "cleanup progress clears saturation and restores graph liveness",
        )

        live_thread = janitor._thread
        janitor._thread = threading.Thread(
            name="synthetic-dead-graph-janitor")
        dead = janitor.liveness_snapshot(180.0)
        janitor._thread = live_thread
        check(
            dead.get("healthy") is False
            and dead.get("thread_alive") is False,
            "a dead sole janitor fails health immediately even while capacity is empty",
        )
    finally:
        allow_cleanup.set()
        for lease in leases:
            lease.close()
        wait_until(lambda: janitor.stats()["reserved"] == 0, timeout=1.0)

    solo_now = [0.0]
    solo = GW.GraphWorkspaceJanitor(
        1,
        thread_name="veripsa-graph-workspace-cap-one-test",
        clock=lambda: solo_now[0],
    )
    solo_lease = solo.reserve(prefix="veripsa_cap_one_")
    try:
        solo_now[0] = 900.0
        legitimate_lease = solo.liveness_snapshot(180.0)
        check(
            legitimate_lease.get("healthy") is True
            and legitimate_lease.get("saturated") is True
            and legitimate_lease.get("cleanup_saturated") is False
            and legitimate_lease.get("leased") == 1,
            "capacity=1 does not misclassify one legitimate long active workspace as janitor failure",
        )
    finally:
        solo_lease.close()
        wait_until(lambda: solo.stats()["reserved"] == 0, timeout=1.0)


def test_capacity_env_is_validated() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = APP
    env["VERIPSA_GRAPH_WORKSPACE_CLEANUP_CAP"] = "0"
    result = subprocess.run(
        [sys.executable, "-c", "import graph_workspace"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3.0,
        check=False,
    )
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    check(
        result.returncode != 0
        and "VERIPSA_GRAPH_WORKSPACE_CLEANUP_CAP" in output,
        "workspace reservation cap is env_int-validated and fails loud",
    )


def main() -> int:
    print("=== GRAPH WORKSPACE JANITOR GATE ===")
    check(
        GW.GRAPH_WORKSPACE_CLEANUP_CAP == 2,
        "test process loaded the configured active+pending workspace cap",
    )
    test_workspace_janitor()
    test_repeated_failures_remain_bounded()
    test_saturated_janitor_has_a_hard_liveness_bound()
    test_capacity_env_is_validated()
    if FAIL:
        print(f"GRAPH WORKSPACE JANITOR GATE: FAIL ({FAIL} check(s))")
        return 1
    print("GRAPH WORKSPACE JANITOR GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
