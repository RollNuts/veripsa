#!/usr/bin/env python3
"""WORKER EVENT-TIMEOUT gate — one poison event must not consume a multiplied retry budget and wedge every tenant.

ROOT CAUSE this guards (a confirmed, recurring PROD incident): the App drains its webhook queue on ONE worker
thread. A trickling response could defeat the per-socket-operation timeout. Even after each GitHub call became
bounded, GitHubREST's five network attempts and EventQueue's three processor attempts could multiply independent
timeouts/Retry-After waits. A persistent ``Retry-After: 60`` therefore allowed about 60s × 4 waits × 3 runs =
720s of single-worker head-of-line blocking, matching the observed ~787s incident once surrounding work is added.

THE FIX (proven here): EventQueue opens ONE monotonic VERIPSA_EVENT_WALL_TIMEOUT_SECONDS budget at dequeue. The
same ContextVar deadline is consumed by every GitHub attempt and EventQueue attempt. A positive GitHub
Retry-After/reset wait is never slept inside that budget: GitHubREST raises a typed scheduling signal and the
DeliveryStore wrapper returns the row to ``queued`` with ``not_before`` without consuming an attempt. An
EventBudgetExceeded remains terminal for blocking I/O and other expired work, so nested retries cannot multiply
the deadline; either way the worker proceeds to the next tenant.

Proves, with a REAL loopback socket:
  (1) THE BUG IS REAL: with NO total deadline, a chunked read off a trickling socket is UNBOUNDED (each recv
      stays under the per-op timeout, so the loop runs past many multiples of it — the production hang).
  (2) THE FIX BOUNDS IT: github_rest._read_through_deadline caps that same read to ~the total deadline (it RAISES
      socket.timeout well before an unbounded time), so ONE call's wall-clock is bounded by the knob.
  (3) END-TO-END through the REAL DeliveryStore wrapper + EventQueue worker: a persistent Retry-After:60 row is
      scheduled once, attempt-neutral, without an in-worker sleep, failure, or retry.
  (4) a healthy event for tenant B begins immediately and receives its own fresh budget.
  (5) a non-cooperative processor that returns after its deadline is failed, never recorded as processed/retried.
  (6) REAL status/header trickles are bounded before getresponse() returns: pooled, one-shot kill-switch, and
      tarball codeload all use the same absolute-deadline watchdog; the poisoned connection is dropped and the
      next request/event succeeds.

No network beyond loopback, no DB. Run:  python3 tests/test_worker_event_timeout.py
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

FAIL = 0


def chk(c, label):
    global FAIL
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    if not c:
        FAIL = 1


class _TricklingServer:
    """Loopback HTTP server with a trickling route and a persistent Retry-After:60 route."""

    def __init__(self, interval: float = 0.2):
        self.interval = interval
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self.rate_limit_requests = 0
        self.header_trickle_requests = 0
        self.healthy_requests = 0
        self.tar_api_requests = 0
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                self._srv.settimeout(0.3)
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._drip, args=(conn,), daemon=True).start()

    def _drip(self, conn):
        try:
            request = conn.recv(65536)  # consume the request line/headers
            if b" /rate-limit " in request:
                self.rate_limit_requests += 1
                conn.sendall(
                    b"HTTP/1.1 429 Too Many Requests\r\n"
                    b"Content-Length: 0\r\n"
                    b"Retry-After: 60\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return
            if b" /header-trickle " in request:
                self.header_trickle_requests += 1
                # Status arrives, but ONE header line never terminates. Every recv gets a byte well inside the
                # 60s inactivity timeout, so only an independent ABSOLUTE watchdog can wake getresponse().
                conn.sendall(b"HTTP/1.1 200 OK\r\nX-Never-Ends: ")
                while not self._stop.is_set():
                    conn.sendall(b"x")
                    time.sleep(self.interval)
                return
            if b" /ok " in request:
                self.healthy_requests += 1
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Length: 8\r\n"
                    b"Connection: close\r\n\r\n"
                    b'{"ok":1}'
                )
                return
            if b" /repos/o/r/tarball/sha " in request:
                self.tar_api_requests += 1
                location = f"http://127.0.0.1:{self.port}/header-trickle".encode("ascii")
                conn.sendall(
                    b"HTTP/1.1 302 Found\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n"
                    b"Location: " + location + b"\r\n\r\n"
                )
                return
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000000\r\n\r\n")
            while not self._stop.is_set():
                conn.sendall(b"x")
                time.sleep(self.interval)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/x"

    @property
    def rate_limit_url(self):
        return f"http://127.0.0.1:{self.port}/rate-limit"

    @property
    def header_trickle_url(self):
        return f"http://127.0.0.1:{self.port}/header-trickle"

    @property
    def healthy_url(self):
        return f"http://127.0.0.1:{self.port}/ok"

    def close(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


def _read_in_thread(fn, timeout):
    """Run fn() on a daemon thread; return (returned_within_timeout?, elapsed_or_None, exc_or_None).
    returned=False means it HUNG past `timeout` (the unbounded case)."""
    box = {}

    def run():
        t0 = time.monotonic()
        try:
            box["r"] = fn()
        except BaseException as e:  # noqa: BLE001 — we want to SEE a timeout raise, not let it escape the thread
            box["e"] = e
        box["dt"] = time.monotonic() - t0

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    return (not th.is_alive()), box.get("dt"), box.get("e")


def main() -> int:
    # A tiny total deadline so the test is fast; well above the 0.2s drip so a healthy read would never trip it.
    os.environ["VERIPSA_GITHUB_HTTP_TIMEOUT"] = "1"
    # ONE budget for the whole dequeued delivery. It is deliberately much smaller than Retry-After:60.
    os.environ["VERIPSA_EVENT_WALL_TIMEOUT_SECONDS"] = "2"
    # Make the per-op timeout LARGE (so it can't be what bounds the trickle — only the total deadline can).
    os.environ["GITHUB_HTTP_TIMEOUT"] = "60"

    import urllib.request
    import event_budget
    import github_rest as gr  # imported AFTER the env is set so the module knobs read these values

    chk(gr._HTTP_TOTAL_TIMEOUT == 1 and gr._HTTP_TIMEOUT == 60,
        f"VERIPSA_GITHUB_HTTP_TIMEOUT knob is read (total={gr._HTTP_TOTAL_TIMEOUT}s, per-op={gr._HTTP_TIMEOUT}s)")
    chk(event_budget._EVENT_WALL_TIMEOUT_SECONDS == 2,
        f"VERIPSA_EVENT_WALL_TIMEOUT_SECONDS knob is read "
        f"(event={event_budget._EVENT_WALL_TIMEOUT_SECONDS}s)")

    srv = _TricklingServer(interval=0.2)
    try:
        UNBOUNDED_PROBE = gr._HTTP_TOTAL_TIMEOUT * 3 + 1   # if a read is still going this long, it is unbounded

        # (1) THE BUG IS REAL: a chunked read with NO total deadline off the trickling socket is UNBOUNDED. We
        #     give it a generous watch window; an unbounded loop is still going (thread alive) when it expires.
        def _read_no_deadline():
            r = urllib.request.urlopen(srv.url, timeout=gr._HTTP_TIMEOUT)
            return gr._read_capped(r, deadline=None)       # deadline=None = the OLD unbounded-time behaviour
        returned_nd, dt_nd, _ = _read_in_thread(_read_no_deadline, timeout=UNBOUNDED_PROBE)
        chk(not returned_nd,
            f"BUG IS REAL: WITHOUT a total deadline, a read off a trickling socket is UNBOUNDED "
            f"(still running after {UNBOUNDED_PROBE:.0f}s — would wedge the worker forever)")

        # (2) THE FIX BOUNDS IT: the SAME read WITH a total deadline raises socket.timeout in ~the deadline,
        #     never unbounded. Assert it both RETURNED (didn't hang) and the wall-clock is near the deadline.
        def _read_with_deadline():
            r = urllib.request.urlopen(srv.url, timeout=gr._HTTP_TIMEOUT)
            return gr._read_capped(r, deadline=time.monotonic() + gr._HTTP_TOTAL_TIMEOUT)
        returned_d, dt_d, exc_d = _read_in_thread(_read_with_deadline, timeout=UNBOUNDED_PROBE)
        _dt_d_s = f"{dt_d:.1f}s" if dt_d is not None else "HUNG"
        chk(returned_d and isinstance(exc_d, (socket.timeout, TimeoutError)),
            f"FIX BOUNDS IT: WITH a total deadline the trickling read RAISES (a timeout), does NOT hang "
            f"(returned={returned_d}, exc={type(exc_d).__name__ if exc_d else None}, dt={_dt_d_s})")
        chk(returned_d and dt_d is not None and dt_d <= gr._HTTP_TOTAL_TIMEOUT + 1.5,
            f"FIX: the per-call wall-clock is bounded by ~the deadline "
            f"(dt={_dt_d_s} ≤ {gr._HTTP_TOTAL_TIMEOUT}s + slack)")

        # (2b) HEADER TRICKLE: getresponse() has not returned yet, so the body reader above cannot help. The
        #      common http.client transport arms an independent watchdog that shutdowns the socket at the same
        #      logical deadline, drops the poisoned pooled connection, and lets the next request start fresh.
        from github_rest import GitHubREST

        header_client = GitHubREST("app", "key", "1")
        header_started = time.monotonic()
        header_exc = None
        try:
            header_client._req("GET", srv.header_trickle_url, "tok")
        except BaseException as e:
            header_exc = e
        header_elapsed = time.monotonic() - header_started
        header_pool_empty = not (getattr(gr._http_pool, "cache", None) or {})
        healthy_after_header = header_client._req("GET", srv.healthy_url, "tok")
        chk(isinstance(header_exc, TimeoutError)
            and header_elapsed <= gr._HTTP_TOTAL_TIMEOUT + 0.75,
            f"HEADER WATCHDOG: pooled getresponse header trickle is logically bounded "
            f"(exc={type(header_exc).__name__ if header_exc else None}, elapsed={header_elapsed:.2f}s)")
        chk(header_pool_empty and healthy_after_header == {"ok": 1},
            f"HEADER WATCHDOG: poisoned pooled connection is dropped and next request succeeds "
            f"(pool_empty={header_pool_empty}, next={healthy_after_header})")
        gr._reset_keepalive_pool()

        # A GitHub-local deadline may be shorter than the containing event budget. It must still cancel the
        # delivery (not remain a catchable background TimeoutError), while leaving the event's unused tail for
        # durable terminalization. The interrupted connection is poisoned and must be gone before recovery.
        event_token = event_budget.begin()
        logical_in_event_exc = None
        logical_in_event_started = time.monotonic()
        try:
            try:
                header_client._req("GET", srv.header_trickle_url, "tok")
            except BaseException as e:
                logical_in_event_exc = e
            logical_in_event_remaining = event_budget.remaining()
        finally:
            event_budget.end(event_token)
        logical_in_event_elapsed = time.monotonic() - logical_in_event_started
        logical_in_event_pool_empty = not (getattr(gr._http_pool, "cache", None) or {})
        healthy_after_event_logical = header_client._req("GET", srv.healthy_url, "tok")
        chk(isinstance(logical_in_event_exc, event_budget.EventBudgetExceeded)
            and logical_in_event_remaining > 0
            and logical_in_event_elapsed <= gr._HTTP_TOTAL_TIMEOUT + 0.75,
            f"HEADER WATCHDOG: shorter logical deadline is promoted to event cancellation "
            f"(exc={type(logical_in_event_exc).__name__ if logical_in_event_exc else None}, "
            f"event_remaining={logical_in_event_remaining:.2f}s, elapsed={logical_in_event_elapsed:.2f}s)")
        chk(logical_in_event_pool_empty and healthy_after_event_logical == {"ok": 1},
            f"HEADER WATCHDOG: event cancellation drops the poisoned connection and recovery is healthy "
            f"(pool_empty={logical_in_event_pool_empty}, next={healthy_after_event_logical})")
        gr._reset_keepalive_pool()

        # The operational kill switch must remove REUSE only. It must not fall back to urllib's vulnerable
        # readline parser; one-shot mode uses the same watchdog and closes every connection after its response.
        saved_keepalive = gr._HTTP_KEEPALIVE
        gr._HTTP_KEEPALIVE = 0
        one_shot_started = time.monotonic()
        one_shot_exc = None
        try:
            header_client._req("GET", srv.header_trickle_url, "tok")
        except BaseException as e:
            one_shot_exc = e
        one_shot_elapsed = time.monotonic() - one_shot_started
        healthy_one_shot = header_client._req("GET", srv.healthy_url, "tok")
        one_shot_pool_empty = not (getattr(gr._http_pool, "cache", None) or {})
        gr._HTTP_KEEPALIVE = saved_keepalive
        chk(isinstance(one_shot_exc, TimeoutError)
            and one_shot_elapsed <= gr._HTTP_TOTAL_TIMEOUT + 0.75
            and healthy_one_shot == {"ok": 1}
            and one_shot_pool_empty,
            f"HEADER WATCHDOG: keepalive-off one-shot is bounded and remains healthy "
            f"(exc={type(one_shot_exc).__name__ if one_shot_exc else None}, "
            f"elapsed={one_shot_elapsed:.2f}s, next={healthy_one_shot}, pool_empty={one_shot_pool_empty})")

        # Tar API redirect + codeload are now on that same transport. The API host uses ``localhost`` while its
        # signed Location uses ``127.0.0.1`` so the auth-bearing cross-origin hop is surfaced and followed cleanly
        # without replaying Authorization; codeload then trickles headers and is deadline-killed.
        tar_client = GitHubREST("app", "key", "1")
        tar_client.API = f"http://localhost:{srv.port}"
        tar_client._itoken = lambda: "tok"
        tar_started = time.monotonic()
        tar_exc = None
        try:
            tar_client._tarball_fetch_once("o/r", "sha")
        except BaseException as e:
            tar_exc = e
        tar_elapsed = time.monotonic() - tar_started
        tar_cache = getattr(gr._http_pool, "cache", None) or {}
        codeload_key_absent = ("127.0.0.1", srv.port, "http") not in tar_cache
        healthy_after_tar = tar_client._req("GET", f"http://localhost:{srv.port}/ok", "tok")
        chk(isinstance(tar_exc, TimeoutError)
            and tar_elapsed <= gr._HTTP_TOTAL_TIMEOUT + 0.75
            and srv.tar_api_requests >= 1
            and codeload_key_absent
            and healthy_after_tar == {"ok": 1},
            f"HEADER WATCHDOG: tar API/codeload share the bound; codeload drops and next API request succeeds "
            f"(exc={type(tar_exc).__name__ if tar_exc else None}, elapsed={tar_elapsed:.2f}s, "
            f"codeload_cached={not codeload_key_absent}, next={healthy_after_tar})")
        gr._reset_keepalive_pool()

        # (3) END-TO-END through the REAL DeliveryStore wrapper + EventQueue worker. The rate-limited row receives
        #     a persistent HTTP 429 carrying Retry-After:60. Production defaults permit 5 GitHub network attempts
        #     and 3 EventQueue processor attempts: without the typed durable scheduling boundary those waits
        #     compose to about 720 seconds. Do not stub sleep and do not disable either retry layer. The first
        #     positive wait must schedule the durable row and let the next tenant run immediately.
        import server as S  # the EventQueue lives here (re-exported)
        from delivery_queue import DeliveryStore, with_delivery_key

        gh = GitHubREST("app", "key", "1")
        gh._token = "tok"
        gh._token_exp = time.time() + 3600

        # (2c) EVENT DEADLINE + NEXT EVENT: make the per-call logical ceiling deliberately LARGE so the EventQueue
        #      budget is the first deadline. A header-trickle poison must fail once, then tenant B must run with a
        #      fresh budget. This is the exact single-worker/noisy-neighbour production boundary.
        saved_total_timeout = gr._HTTP_TOTAL_TIMEOUT
        gr._HTTP_TOTAL_TIMEOUT = 60
        header_event_runs = []
        header_event_healthy = []
        header_event_healthy_budget = []
        header_event_started = time.monotonic()
        header_requests_before = srv.header_trickle_requests

        def header_event_proc(event_type, payload, db, gh_, **kw):
            if payload.get("header_poison"):
                header_event_runs.append(time.monotonic())
                gh_._req("GET", srv.header_trickle_url, "tok")
            else:
                header_event_healthy.append(payload["id"])
                header_event_healthy_budget.append(event_budget.remaining())

        header_eq = S.EventQueue(
            db=None, gh=gh, process=header_event_proc,
            account_of=S._event_account_key,
            maxsize=10, per_account_cap=10,
            retry_attempts=3, retry_base_seconds=0,
        ).start()
        header_eq.submit(
            "push",
            {"header_poison": True, "repository": {"owner": {"id": 10}, "full_name": "header/a"}},
        )
        header_eq.submit(
            "push",
            {"id": 101, "repository": {"owner": {"id": 11}, "full_name": "header/b"}},
        )
        header_event_drained = header_eq.wait_idle(
            timeout=event_budget._EVENT_WALL_TIMEOUT_SECONDS + 1.5)
        header_event_elapsed = time.monotonic() - header_event_started
        gr._HTTP_TOTAL_TIMEOUT = saved_total_timeout
        chk(header_event_drained
            and header_eq.failed() == 1
            and header_eq.processed() == 1
            and header_eq.retried() == 0
            and len(header_event_runs) == 1
            and header_event_healthy == [101]
            and srv.header_trickle_requests == header_requests_before + 1,
            f"EVENT HEADER DEADLINE: poison runs once, then next tenant succeeds "
            f"(drained={header_event_drained}, failed={header_eq.failed()}, "
            f"processed={header_eq.processed()}, poison_runs={len(header_event_runs)}, "
            f"next={header_event_healthy}, elapsed={header_event_elapsed:.2f}s)")
        header_fresh = header_event_healthy_budget[0] if header_event_healthy_budget else None
        chk(header_fresh is not None
            and header_fresh > event_budget._EVENT_WALL_TIMEOUT_SECONDS / 2,
            f"EVENT HEADER DEADLINE: next event receives a fresh budget "
            f"(remaining={header_fresh:.2f}s)" if header_fresh is not None else
            "EVENT HEADER DEADLINE: next event did not run")

        processed_healthy = []
        poison_inflight = {"max": 0.0}
        poison_processor_attempts = []
        healthy_started_after = []
        healthy_budget = []
        submitted_at = time.monotonic()
        durable_payloads = {
            "rate-limited-delivery": {
                "poison": True,
                "repository": {"owner": {"id": 1}, "full_name": "a/r"},
            },
            "healthy-delivery": {
                "id": 99,
                "repository": {"owner": {"id": 2}, "full_name": "b/r"},
            },
        }
        durable_state = {
            key: {"status": "queued", "attempts": 0, "not_before": None}
            for key in durable_payloads
        }
        durable_defers = []
        durable_finishes = []
        durable_releases = []
        lease_counter = {"value": 0}
        durable = DeliveryStore("postgresql://unused")

        def durable_claim(key):
            lease_counter["value"] += 1
            durable_state[key]["status"] = "processing"
            durable_state[key]["attempts"] += 1
            return {
                "claimed": True,
                "lease_generation": lease_counter["value"],
                "event_type": "push",
                "payload": durable_payloads[key],
            }

        def durable_defer(key, not_before, reason, lease_generation):
            durable_defers.append((key, not_before, reason, lease_generation))
            durable_state[key]["status"] = "queued"
            durable_state[key]["attempts"] -= 1
            durable_state[key]["not_before"] = not_before
            return True

        def durable_finish(key, lease_generation):
            durable_finishes.append((key, lease_generation))
            durable_state[key]["status"] = "done"
            return True

        durable.claim = durable_claim
        durable.defer = durable_defer
        durable.finish = durable_finish
        durable.release = lambda key, error, lease: durable_releases.append((key, error, lease))

        def proc(event_type, payload, db, gh_, **kw):
            if payload.get("poison"):
                poison_processor_attempts.append(time.monotonic())
                gh_._req("GET", srv.rate_limit_url, "tok")
            else:
                healthy_started_after.append(time.monotonic() - submitted_at)
                healthy_budget.append(event_budget.remaining())
                processed_healthy.append(payload.get("id"))

        eq = S.EventQueue(db=None, gh=gh, process=durable.wrap_processor(proc),
                          account_of=S._event_account_key,
                          maxsize=100, per_account_cap=100,
                          retry_attempts=3, retry_base_seconds=1)
        eq.start()

        # observe inflight_age while the poison is in flight (the lying-green signal): it must stay BOUNDED.
        watch_stop = threading.Event()

        def watch():
            while not watch_stop.is_set():
                a = eq.inflight_age()
                if a is not None:
                    poison_inflight["max"] = max(poison_inflight["max"], a)
                time.sleep(0.05)
        wt = threading.Thread(target=watch, daemon=True)
        wt.start()

        submitted_at = time.monotonic()
        eq.submit(
            "push",
            with_delivery_key(durable_payloads["rate-limited-delivery"], "rate-limited-delivery"),
            "rate-limited-delivery",
        )
        # A DIFFERENT account is the noisy-neighbour regression: tenant B must not wait ~787 seconds for A.
        eq.submit(
            "push",
            with_delivery_key(durable_payloads["healthy-delivery"], "healthy-delivery"),
            "healthy-delivery",
        )

        EVENT_BUDGET = event_budget._EVENT_WALL_TIMEOUT_SECONDS
        drained = eq.wait_idle(timeout=1.0)
        watch_stop.set()

        chk(drained,
            "END-TO-END: the worker drained both in-memory generations without waiting for Retry-After:60")
        scheduled = durable_state["rate-limited-delivery"]
        scheduled_delay = (
            (scheduled["not_before"] - datetime.now(timezone.utc)).total_seconds()
            if scheduled["not_before"] is not None else -1.0
        )
        chk(eq.failed() == 0 and eq.retried() == 0 and eq.claim_deferred() == 1,
            f"END-TO-END: rate-limit scheduling is neither failure nor retry "
            f"(failed={eq.failed()}, retried={eq.retried()}, deferred={eq.claim_deferred()})")
        chk(scheduled["status"] == "queued"
            and scheduled["attempts"] == 0
            and 57.0 <= scheduled_delay <= 60.5
            and len(durable_defers) == 1
            and durable_defers[0][0] == "rate-limited-delivery"
            and durable_releases == [],
            f"DURABLE DEFERRAL: row is queued attempt-neutral for Retry-After:60 "
            f"(state={scheduled}, defers={len(durable_defers)}, releases={len(durable_releases)})")
        chk(processed_healthy == [99],
            f"END-TO-END: tenant B was processed after tenant A was scheduled (seen={processed_healthy})")
        chk(len(poison_processor_attempts) == 1 and eq.retried() == 0,
            f"ROOT REGRESSION: typed rate-limit deferral was NOT re-run by EventQueue "
            f"(processor_runs={len(poison_processor_attempts)}, event_queue_retries={eq.retried()}, configured=3)")
        chk(srv.rate_limit_requests == 1,
            f"ROOT REGRESSION: Retry-After:60 did not start GitHub's remaining four attempts "
            f"(HTTP attempts={srv.rate_limit_requests}, configured={GitHubREST.MAX_RETRIES + 1})")
        healthy_latency = healthy_started_after[0] if healthy_started_after else float("inf")
        chk(healthy_latency <= 0.5,
            f"FAIRNESS: tenant B began immediately after tenant A was scheduled "
            f"(latency={healthy_latency:.3f}s; old multiplier≈720s)")
        fresh_remaining = healthy_budget[0] if healthy_budget else None
        chk(fresh_remaining is not None and fresh_remaining > EVENT_BUDGET / 2,
            f"FRESH BUDGET: tenant B received a new per-dequeue budget "
            f"(remaining_at_start={fresh_remaining:.2f}s)" if fresh_remaining is not None else
            "FRESH BUDGET: tenant B did not start")
        chk(durable_state["healthy-delivery"]["status"] == "done"
            and [key for key, _lease in durable_finishes] == ["healthy-delivery"],
            f"END-TO-END: only tenant B is durably finished "
            f"(state={durable_state['healthy-delivery']}, finishes={durable_finishes})")
        chk(poison_inflight["max"] <= 0.5,
            f"END-TO-END: sampled tenant A inflight age remained near zero "
            f"(max_inflight={poison_inflight['max']:.3f}s)")

        # (5) ACCOUNTING SAFETY: a non-cooperative stage may return just after the deadline instead of raising.
        # EventQueue must check again before recording processed/receipt state and must not inline-retry it.
        original_begin = event_budget.begin
        late_runs = []
        late_healthy = []

        def late_proc(event_type, payload, db, gh_, **kw):
            if payload.get("late"):
                late_runs.append(payload["late"])
                time.sleep(0.12)
            else:
                late_healthy.append(payload["id"])

        try:
            event_budget.begin = lambda: original_begin(0.08)
            lateq = S.EventQueue(
                db=None, gh=None, process=late_proc, account_of=S._event_account_key,
                maxsize=10, per_account_cap=10, retry_attempts=3, retry_base_seconds=0,
            ).start()
            lateq.submit("push", {"late": 1, "repository": {"owner": {"id": 3}, "full_name": "c/r"}})
            lateq.submit("push", {"id": 100, "repository": {"owner": {"id": 4}, "full_name": "d/r"}})
            late_drained = lateq.wait_idle(timeout=1.0)
        finally:
            event_budget.begin = original_begin

        chk(late_drained and lateq.failed() == 1 and lateq.processed() == 1,
            f"POST-RETURN DEADLINE: an overdue return is failed, while the next tenant is processed "
            f"(drained={late_drained}, failed={lateq.failed()}, processed={lateq.processed()})")
        chk(late_runs == [1] and lateq.retried() == 0 and late_healthy == [100],
            f"POST-RETURN DEADLINE: overdue work is not reported successful or re-run "
            f"(runs={late_runs}, retries={lateq.retried()}, next={late_healthy})")
    finally:
        srv.close()

    print("WORKER EVENT-TIMEOUT GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
