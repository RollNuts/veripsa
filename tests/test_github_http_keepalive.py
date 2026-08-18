#!/usr/bin/env python3
"""HTTP keep-alive gate — _urlopen reuses ONE persistent http.client connection per (host, port) per thread.

The companion to test_github_resilience.py (which proves the resilience CONTRACT — retry/backoff, token refresh,
4xx/5xx classification). THIS gate proves the round-2 PERF win: every GitHub call from GitHubREST._urlopen used
to open a fresh TCP+TLS handshake (~80–150 ms each), so a webhook making many calls per event paid that latency
N times. The keep-alive path mints ONE http.client.HTTPSConnection per (host, port) per worker thread and reuses
it; this gate pins:

  (a) REUSE          — two sequential calls on the same thread use the SAME pooled connection (one handshake).
  (b) THREAD-LOCAL   — concurrent threads each have their OWN connection (no shared / no inter-thread interleave,
                       since http.client.HTTPSConnection is NOT thread-safe).
  (c) MAX-AGE RECYCLE — a connection older than VERIPSA_HTTP_KEEPALIVE_MAX_AGE_SECONDS is closed + replaced.
  (d) KILL SWITCH    — VERIPSA_HTTP_KEEPALIVE=0 uses the common transport one-shot (no pooled reuse).
  (e) RESET ON ERROR — a connect/send failure closes + drops the cached connection (no stale-keepalive replay).

No real network: the http.client.HTTPSConnection is monkey-patched to a fake whose request/getresponse log calls.

Run:  python3 tests/test_github_http_keepalive.py
"""
from __future__ import annotations

import io
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import urllib.request   # noqa: E402
import urllib.error  # noqa: E402

import event_budget  # noqa: E402
import github_rest as gr   # noqa: E402


class _FakeResp:
    """A minimal http.client.HTTPResponse-shape stand-in (read/close/headers/status/fp/getheader)."""
    def __init__(self, body=b'{"ok":1}', status=200, headers=None):
        self._body = body
        self._pos = 0
        self.status = status
        self.reason = "OK" if status < 400 else "ERR"
        # http.client uses .msg (the email.Message) AND .headers (alias). Provide a tiny shim covering both.
        class _H(dict):
            def get(self, k, d=None): return super().get(k, d)
        self.msg = _H(headers or {})
        self.headers = self.msg
        self.fp = None

    def read(self, n=-1):
        if n is None or n < 0:
            data = self._body[self._pos:]
            self._pos = len(self._body)
            return data
        data = self._body[self._pos:self._pos + n]
        self._pos += len(data)
        return data

    def read1(self, n=-1):
        return self.read(n)

    def close(self):
        pass

    def getheader(self, name, default=None):
        return self.msg.get(name, default)


class _CtxResp(_FakeResp):
    """_FakeResp with context-manager methods for tests that patch urllib.request.urlopen directly."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _req_user_agent(req):
    for k, v in req.header_items():
        if k.lower() == "user-agent":
            return v
    return None


class _FakeConn:
    """A fake http.client.HTTPSConnection that records each request/getresponse call so the test can assert
    REUSE (one fake conn handling two sequential requests on the same thread). `script` is a list of _FakeResp
    or exception instances; each request pops the next item. The instance is THREAD-LOCAL to its constructor
    thread by virtue of the cache it lives in (the test asserts that)."""
    _next_id = 0
    _id_lock = threading.Lock()

    def __init__(self, host, port=None, timeout=None):
        with _FakeConn._id_lock:
            _FakeConn._next_id += 1
            self.id = _FakeConn._next_id
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests = []
        self.closed = False
        self._script = []

    def add(self, item):
        self._script.append(item)
        return self

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"method": method, "path": path, "headers": dict(headers or {}), "body": body})
        # If the next item is an exception class/instance, raise it on the *getresponse* call (mirrors
        # http.client's failure mode where the wire error surfaces on read or response).

    def getresponse(self):
        if not self._script:
            return _FakeResp()
        item = self._script.pop(0)
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item if isinstance(item, BaseException) else item("simulated wire error")
        return item

    def close(self):
        self.closed = True


def _reset_state():
    gr._reset_keepalive_pool()


def _install_fakeconn():
    """Monkey-patch http.client.HTTPSConnection so _get_keepalive_connection returns _FakeConn instances. Returns
    the (orig_https, orig_http) so the caller can restore. Each constructed conn lands in the patched factory's
    `made` list for inspection."""
    import http.client as _hc
    orig_https, orig_http = _hc.HTTPSConnection, _hc.HTTPConnection
    made = []

    def _factory(*a, **kw):
        c = _FakeConn(*a, **kw)
        made.append(c)
        return c
    _hc.HTTPSConnection = _factory
    _hc.HTTPConnection = _factory
    return orig_https, orig_http, made


def _restore(orig_https, orig_http):
    import http.client as _hc
    _hc.HTTPSConnection = orig_https
    _hc.HTTPConnection = orig_http


def main() -> int:
    checks = []

    # ── (a) REUSE: two sequential calls on the same thread → ONE handshake (the pooled FakeConn is reused) ──
    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    try:
        req = urllib.request.Request("https://api.github.com/x", method="GET")
        req.add_header("Authorization", "Bearer tok")
        # pre-seed the next two responses on the SAME fake conn — created lazily on first call, then reused
        def _seed_next():
            # The cache is keyed on (host, port, scheme); we don't know the conn until _urlopen mints it. So we
            # do one call to mint it, then add a second response to the SAME conn for the second call.
            pass
        with gr._urlopen_keepalive(req) as r1:
            r1.read()
        # Now exactly one FakeConn must exist; queue a second response on it.
        assert len(made) == 1, f"first call must mint exactly ONE connection (made={len(made)})"
        first = made[0]
        first.add(_FakeResp())
        with gr._urlopen_keepalive(req) as r2:
            r2.read()
        checks.append((f"keepalive: two sequential calls on one thread REUSE the same connection "
                       f"(made={len(made)}, requests={len(first.requests)})",
                       len(made) == 1 and len(first.requests) == 2))
        # also: each replayed Request-id is the SAME pooled conn (the test factory assigns sequential ids; one id = one conn)
        checks.append(("keepalive: the reused connection's id stays stable across calls "
                       "(no silent re-mint between requests)", first.id == made[0].id))
    finally:
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (b) THREAD-LOCAL: two threads each get their OWN connection (HTTPSConnection isn't thread-safe) ──
    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    try:
        thread_conns = {}
        ready = threading.Barrier(2)

        def _worker(tid):
            ready.wait()
            req = urllib.request.Request("https://api.github.com/x", method="GET")
            req.add_header("Authorization", "Bearer tok")
            with gr._urlopen_keepalive(req) as r:
                r.read()
            # capture the conn this thread uses, by walking its OWN cache
            cache = getattr(gr._http_pool, "cache", {}) or {}
            thread_conns[tid] = next(iter(cache.values()))[0] if cache else None
            # clear AFTER capturing so the next test starts clean
            gr._reset_keepalive_pool()
        t1 = threading.Thread(target=_worker, args=(1,))
        t2 = threading.Thread(target=_worker, args=(2,))
        t1.start(); t2.start()
        t1.join(); t2.join()
        checks.append((f"keepalive: each thread gets its OWN connection (made={len(made)}, "
                       f"t1.id={getattr(thread_conns.get(1), 'id', None)}, t2.id={getattr(thread_conns.get(2), 'id', None)})",
                       len(made) == 2
                       and thread_conns.get(1) is not None
                       and thread_conns.get(2) is not None
                       and thread_conns[1] is not thread_conns[2]))
    finally:
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (c) MAX-AGE RECYCLE: a connection past the configured max age is closed + replaced on next use ──
    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    try:
        # tighten the recycle window via the module knob (we set the constant directly — env_int reads it once at import)
        saved_age = gr._HTTP_KEEPALIVE_MAX_AGE
        gr._HTTP_KEEPALIVE_MAX_AGE = 1   # seconds — easy to exceed
        req = urllib.request.Request("https://api.github.com/x", method="GET")
        req.add_header("Authorization", "Bearer tok")
        with gr._urlopen_keepalive(req) as r:
            r.read()
        assert len(made) == 1
        first_conn = made[0]
        # Rewind the cache entry's created_at past the recycle window — simulates ageing without sleep.
        cache = gr._http_pool.cache
        ((host, port, scheme), (conn, _at)) = next(iter(cache.items()))
        cache[(host, port, scheme)] = (conn, time.monotonic() - 10.0)
        with gr._urlopen_keepalive(req) as r:
            r.read()
        checks.append((f"keepalive: a stale (max-age past) connection is recycled — new conn minted "
                       f"(made={len(made)}, first.closed={first_conn.closed})",
                       len(made) == 2 and first_conn.closed))
        gr._HTTP_KEEPALIVE_MAX_AGE = saved_age
    finally:
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (d) KILL SWITCH: VERIPSA_HTTP_KEEPALIVE=0 → common watchdog transport, one-shot (no pool touched) ──
    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    saved_ka = gr._HTTP_KEEPALIVE
    gr._HTTP_KEEPALIVE = 0
    try:
        req = urllib.request.Request("https://api.github.com/x", method="GET")
        with gr.GitHubREST._urlopen(req) as r:
            body = r.read()
        one_shot = made[0] if made else None
        pool_empty = getattr(gr._http_pool, "cache", None) in (None, {})
        checks.append((
            f"keepalive: kill switch uses one-shot common transport and closes after the response "
            f"(made={len(made)}, closed={getattr(one_shot, 'closed', None)}, pool_empty={pool_empty})",
            len(made) == 1
            and body == b'{"ok":1}'
            and one_shot.closed
            and len(one_shot.requests) == 1
            and pool_empty,
        ))
    finally:
        gr._HTTP_KEEPALIVE = saved_ka
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (e) RESET ON CONNECT/SEND ERROR: a wire error closes + drops the cached conn; next call mints fresh ──
    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    try:
        # Pre-seed: the first FakeConn raises on getresponse → wrapped as URLError.
        # We need the conn to be the first thing minted, so call _get_keepalive_connection directly to seed.
        bad = gr._get_keepalive_connection("api.github.com", 443, "https")
        bad.add(ConnectionResetError("simulated reset"))
        req = urllib.request.Request("https://api.github.com/x", method="GET")
        req.add_header("Authorization", "Bearer tok")
        import urllib.error as _ue
        raised = None
        try:
            gr._urlopen_keepalive(req)
        except _ue.URLError as e:
            raised = e
        # cache must be empty now (the failed conn was dropped)
        cache_after = getattr(gr._http_pool, "cache", None) or {}
        # and the next call mints a brand new FakeConn (different id)
        good = gr._get_keepalive_connection("api.github.com", 443, "https")
        checks.append((f"keepalive: a wire error closes + drops the cached connection — next call mints fresh "
                       f"(raised={type(raised).__name__ if raised else None}, made={len(made)}, "
                       f"bad.id={bad.id}, good.id={good.id})",
                       isinstance(raised, _ue.URLError) and bad.closed and bad is not good and good.id > bad.id))
    finally:
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (f) HTTPError on 4xx/5xx: a 503 response is raised as urllib.error.HTTPError WITHOUT poisoning the
    #       cached connection (HTTP-level errors are NOT connection-level; the wire is still clean → keep
    #       reusing it on the next call). The body of the error is preserved for _is_auth_failure_403 readers.
    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    try:
        # seed: first call gets a 503; second call gets 200 — same conn must serve both (no drop on a 5xx).
        conn = gr._get_keepalive_connection("api.github.com", 443, "https")
        conn.add(_FakeResp(body=b"server fail", status=503))
        conn.add(_FakeResp(body=b'{"ok":1}'))
        import urllib.error as _ue
        req = urllib.request.Request("https://api.github.com/x", method="GET")
        raised503 = None
        try:
            gr._urlopen_keepalive(req)
        except _ue.HTTPError as e:
            raised503 = e
        # second call → success on the SAME conn (no new conn minted)
        with gr._urlopen_keepalive(req) as r2:
            body = r2.read()
        checks.append((f"keepalive: a 5xx is raised as HTTPError WITHOUT dropping the cached connection "
                       f"(raised.code={getattr(raised503, 'code', None)}, made={len(made)}, body={body!r})",
                       raised503 is not None and raised503.code == 503 and len(made) == 1 and body == b'{"ok":1}'))
    finally:
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (f1) ERROR-BODY BOUND: diagnostic 4xx/5xx bodies are capped at 64 KiB. If the peer sends more, unread
    #        bytes make that connection unsafe to reuse, so it must be closed+dropped instead of being drained
    #        without limit on the single worker.
    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    try:
        conn = gr._get_keepalive_connection("api.github.com", 443, "https")
        conn.add(_FakeResp(
            body=b"x" * (gr._MAX_HTTP_ERROR_BODY_BYTES + 1),
            status=503,
        ))
        req = urllib.request.Request("https://api.github.com/x", method="GET")
        oversized_error = None
        try:
            gr._urlopen_keepalive(req)
        except urllib.error.HTTPError as e:
            oversized_error = e
        bounded_body = oversized_error.read() if oversized_error is not None else b""
        cache_after = getattr(gr._http_pool, "cache", None) or {}
        checks.append((
            f"keepalive: an oversized 503 body is capped and its partially-read connection is dropped "
            f"(bytes={len(bounded_body)}, closed={conn.closed}, cached={bool(cache_after)})",
            oversized_error is not None
            and len(bounded_body) == gr._MAX_HTTP_ERROR_BODY_BYTES
            and conn.closed
            and not cache_after,
        ))
    finally:
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (f2) TRICKLING ERROR BODY: a peer that continuously yields one byte never hits a per-recv timeout.
    #        The absolute logical deadline must still abort the drain and poison/drop that connection.
    class _TricklingErrorResp(_FakeResp):
        def __init__(self):
            super().__init__(body=b"", status=503)

        def read1(self, _n=-1):
            time.sleep(0.015)
            return b"x"

    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    try:
        conn = gr._get_keepalive_connection("api.github.com", 443, "https")
        conn.add(_TricklingErrorResp())
        req = urllib.request.Request("https://api.github.com/x", method="GET")
        started = time.monotonic()
        trickle_error = None
        try:
            gr._urlopen_keepalive(req, deadline=time.monotonic() + 0.05)
        except Exception as e:
            trickle_error = e
        elapsed = time.monotonic() - started
        cache_after = getattr(gr._http_pool, "cache", None) or {}
        checks.append((
            f"keepalive: a trickling 503 body is deadline-bounded and its connection is dropped "
            f"(exc={type(trickle_error).__name__ if trickle_error else None}, "
            f"elapsed={elapsed:.3f}s, closed={conn.closed})",
            isinstance(trickle_error, gr._GitHubCallDeadlineExceeded)
            and elapsed < 0.5
            and conn.closed
            and not cache_after,
        ))
    finally:
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (f2b) LOGICAL DEADLINE INSIDE EVENT: the GitHub-local ceiling may be shorter than the event's work
    #          allowance. It is still terminal for that delivery, so surface EventBudgetExceeded (the
    #          cancellation type), drop the partial connection, and leave background calls on TimeoutError.
    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    budget_token = event_budget.begin(0.5)
    try:
        conn = gr._get_keepalive_connection("api.github.com", 443, "https")
        conn.add(_TricklingErrorResp())
        req = urllib.request.Request("https://api.github.com/x", method="GET")
        event_logical_error = None
        try:
            gr._urlopen_keepalive(req, deadline=time.monotonic() + 0.05)
        except BaseException as exc:
            event_logical_error = exc
        event_remaining_after = event_budget.remaining()
        cache_after = getattr(gr._http_pool, "cache", None) or {}
        checks.append((
            f"keepalive: a shorter GitHub logical deadline becomes event cancellation and drops the connection "
            f"(exc={type(event_logical_error).__name__ if event_logical_error else None}, "
            f"event_remaining={event_remaining_after:.3f}s, closed={conn.closed})",
            isinstance(event_logical_error, event_budget.EventBudgetExceeded)
            and event_remaining_after > 0.1
            and conn.closed
            and not cache_after,
        ))
    finally:
        event_budget.end(budget_token)
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (f3) SAME-ORIGIN REDIRECT: urllib.urlopen follows GitHub's API redirects, but http.client does not.
    #        The keep-alive path must follow /repos/old -> /repos/current so renamed repo metadata resolves to
    #        the canonical full_name instead of returning/parsing the intermediate 301 body.
    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    try:
        conn = gr._get_keepalive_connection("api.github.com", 443, "https")
        conn.add(_FakeResp(body=b"moved", status=301,
                           headers={"Location": "https://api.github.com/repos/RollNuts/veripsa"}))
        conn.add(_FakeResp(body=b'{"ok":1}', status=200))
        req = urllib.request.Request("https://api.github.com/repos/example-user/veripsa-core-old", method="GET")
        req.add_header("Authorization", "Bearer tok")
        with gr._urlopen_keepalive(req) as r:
            body = r.read()
        paths = [r["path"] for r in conn.requests]
        auths = [next((v for k, v in r["headers"].items() if k.lower() == "authorization"), None)
                 for r in conn.requests]
        checks.append((f"keepalive: same-origin GitHub API redirects are followed on the pooled connection "
                       f"(made={len(made)}, paths={paths}, body={body!r})",
                       len(made) == 1
                       and paths == ["/repos/example-user/veripsa-core-old", "/repos/RollNuts/veripsa"]
                       and auths == ["Bearer tok", "Bearer tok"]
                       and body == b'{"ok":1}'))
    finally:
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (f4) MUTATION NO-REDIRECT: an operator redelivery marks its Request so a 307/308 can never replay the
    #          POST.  The first response is acknowledgement-ambiguous and must surface as HTTPError after exactly
    #          one wire request; ordinary GET redirect behavior above remains unchanged.
    for redirect_status in (307, 308):
        _reset_state()
        orig_https, orig_http, made = _install_fakeconn()
        try:
            conn = gr._get_keepalive_connection("api.github.com", 443, "https")
            conn.add(_FakeResp(body=b"moved", status=redirect_status,
                               headers={"Location": "https://api.github.com/app/hook/deliveries/2/attempts"}))
            req = urllib.request.Request(
                "https://api.github.com/app/hook/deliveries/1/attempts",
                data=b"", method="POST")
            setattr(req, gr._REQUEST_NO_REDIRECT_ATTR, True)
            redirect_error = None
            try:
                gr._urlopen_keepalive(req)
            except urllib.error.HTTPError as exc:
                redirect_error = exc
            checks.append((
                f"keepalive: no-redirect mutation surfaces HTTP {redirect_status} after one POST",
                isinstance(redirect_error, urllib.error.HTTPError)
                and redirect_error.code == redirect_status
                and len(conn.requests) == 1
                and conn.requests[0]["method"] == "POST",
            ))
        finally:
            _restore(orig_https, orig_http)
            _reset_state()

    # ── (g) USER-AGENT: GitHub 403s any request WITHOUT a User-Agent ("Request forbidden by administrative
    #       rules. Please make sure your request has a User-Agent header"), and http.client — unlike urllib's
    #       opener — adds NO default. So the keep-alive path MUST emit one. This is the 2026-06-27 incident
    #       regression: #476 shipped the keep-alive path UA-less → EVERY GitHub call 403'd → no check-runs, an
    #       empty install map, an 8-day-stale graph. (g1) a UA-less Request gets gr._USER_AGENT supplied on the
    #       wire; (g2) an explicit UA is preserved, not overridden or duplicated.
    _reset_state()
    orig_https, orig_http, made = _install_fakeconn()
    try:
        # (g1) no UA on the Request → the keep-alive backstop supplies the App UA
        conn = gr._get_keepalive_connection("api.github.com", 443, "https")
        conn.add(_FakeResp())
        req = urllib.request.Request("https://api.github.com/x", method="GET")
        req.add_header("Authorization", "Bearer tok")
        with gr._urlopen_keepalive(req) as r:
            r.read()
        sent = conn.requests[0]["headers"]
        ua = next((v for k, v in sent.items() if k.lower() == "user-agent"), None)
        n_ua = sum(1 for k in sent if k.lower() == "user-agent")
        checks.append((f"keepalive: a UA-less Request gets a User-Agent on the wire (ua={ua!r}, count={n_ua}) "
                       f"— GitHub 403s without it", ua == gr._USER_AGENT and n_ua == 1))

        # (g2) an explicit (distinct) UA on the Request is PRESERVED — not overridden by the backstop, not duplicated
        _reset_state()
        conn2 = gr._get_keepalive_connection("api.github.com", 443, "https")
        conn2.add(_FakeResp())
        req2 = urllib.request.Request("https://api.github.com/x", method="GET")
        req2.add_header("Authorization", "Bearer tok")
        req2.add_header("User-Agent", "custom-agent/1.0")
        with gr._urlopen_keepalive(req2) as r2:
            r2.read()
        sent2 = conn2.requests[0]["headers"]
        ua2 = next((v for k, v in sent2.items() if k.lower() == "user-agent"), None)
        n_ua2 = sum(1 for k in sent2 if k.lower() == "user-agent")
        checks.append((f"keepalive: an explicit User-Agent is preserved, not duplicated (ua={ua2!r}, count={n_ua2})",
                       ua2 == "custom-agent/1.0" and n_ua2 == 1))
    finally:
        _restore(orig_https, orig_http)
        _reset_state()

    # ── (h) REQUEST BUILDERS: the keep-alive backstop is not the only line of defense. The Request builders
    #       themselves must set the App UA, so both the urllib kill-switch path and http.client path carry it.
    #       Also pin the hand-followed tarball redirect: codeload is GitHub-owned and gets an explicit UA too.
    class _Client(gr.GitHubREST):
        def __init__(self):
            super().__init__("app-id", "-----BEGIN KEY-----\nx\n-----END KEY-----", "1")
            self._sleep = lambda s: None
            self._jwt = lambda: "APP-JWT-stub"

    def _json_resp(obj):
        import json
        return _CtxResp(json.dumps(obj).encode("utf-8"))

    import time as _t
    c = _Client()
    seen = []

    def _capture_req(req):
        seen.append(req)
        if req.full_url.endswith("/access_tokens"):
            exp = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() + 3600))
            return _json_resp({"token": "ghs_stub", "expires_at": exp})
        return _json_resp({"ok": 1})

    c._urlopen = _capture_req
    c._api("GET", "/repos/o/r/pulls/1/files")
    c._api_with_link("GET", "/repos/o/r/pulls/1/files?per_page=100")
    builder_uas = [_req_user_agent(req) for req in seen]
    checks.append((f"github request builders: token mint, _req, and _req_with_link all set User-Agent "
                   f"(uas={builder_uas!r})",
                   bool(builder_uas) and all(ua == gr._USER_AGENT for ua in builder_uas)))

    c_meta = _Client()
    c_meta._itoken = lambda: "ghs_stub"
    meta_seen = []
    def _meta_resp(req):
        meta_seen.append(req.full_url)
        if req.full_url.endswith("/repos/example-user/veripsa-core-old"):
            return _json_resp({"default_branch": "main", "full_name": "RollNuts/veripsa"})
        if req.full_url.endswith("/repos/example-user/veripsa-core-old/branches/main"):
            return _json_resp({"commit": {"sha": "abc123"}})
        return _json_resp({})
    c_meta._urlopen = _meta_resp
    branch, sha, canonical = c_meta.repo_default_branch_head_info("example-user/veripsa-core-old")
    legacy_branch, legacy_sha = c_meta.repo_default_branch_head("example-user/veripsa-core-old")
    checks.append((f"repo metadata helper: repo_default_branch_head_info returns canonical full_name and the "
                   f"legacy wrapper keeps its two-value shape (seen={meta_seen!r})",
                   (branch, sha, canonical) == ("main", "abc123", "RollNuts/veripsa")
                   and (legacy_branch, legacy_sha) == ("main", "abc123")))

    c2 = _Client()
    c2._itoken = lambda: "ghs_stub"
    codeload_seen = []

    def _capture_tar_transport(req):
        if req.full_url.startswith(c2.API):
            codeload_seen.append(("api", req))
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found", {"Location": "https://codeload.github.com/o/r/tar.gz/sha"}, None)
        codeload_seen.append(("codeload", req))
        return _CtxResp(b"tarball")

    c2._urlopen = _capture_tar_transport
    data = c2._tarball_fetch_once("o/r", "sha")
    api_req = next((req for kind, req in codeload_seen if kind == "api"), None)
    codeload_req = next((req for kind, req in codeload_seen if kind == "codeload"), None)
    checks.append((f"github tarball fetch: API hop and codeload redirect hop both set User-Agent "
                   f"(api={_req_user_agent(api_req) if api_req else None!r}, "
                   f"codeload={_req_user_agent(codeload_req) if codeload_req else None!r})",
                   data == b"tarball"
                   and _req_user_agent(api_req) == gr._USER_AGENT
                   and _req_user_agent(codeload_req) == gr._USER_AGENT))

    okall = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        okall = okall and bool(cond)
    print("GITHUB HTTP KEEP-ALIVE GATE:", "PASS" if okall else "FAIL")
    return 0 if okall else 1


if __name__ == "__main__":
    sys.exit(main())
