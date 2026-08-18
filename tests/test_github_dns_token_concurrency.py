#!/usr/bin/env python3
"""GitHub transport/auth hard-wall gate.

Pins the seams that can otherwise amplify one tenant's slow DNS or stale token
across keyed EventQueue workers:

* fixed resolver pool, bounded wait, in-flight de-duplication, and TTL cache;
* one absolute DNS+multi-address-connect deadline;
* real loopback HTTP reached through fake DNS while Host stays original;
* HTTPS connects to the resolved IP but keeps original SNI/cert hostname;
* event/logical-bounded installation-token lock waits;
* concurrent/delayed old-token 401s cause one mint and cannot erase new token;
* the historical two-argument tarball monkey-patch seam remains compatible.
"""
from __future__ import annotations

import io
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import bounded_resolver  # noqa: E402
import event_budget  # noqa: E402
import github_rest as gr  # noqa: E402


def _http_error(code=401):
    return urllib.error.HTTPError(
        "https://api.github.com/x",
        code,
        "auth failed",
        {},
        io.BytesIO(b""),
    )


def main() -> int:
    checks = []

    # A permanently stuck lookup consumes one of exactly two workers. Repeated
    # waiters share that in-flight call, while the second worker still resolves
    # a healthy name. TTL means that healthy result performs one native lookup.
    stuck = threading.Event()
    resolver_calls = {"stuck": 0, "healthy": 0}
    calls_lock = threading.Lock()

    def _resolver_fn(host, port, family, socktype, proto, flags):
        with calls_lock:
            resolver_calls["stuck" if host == "stuck.invalid" else "healthy"] += 1
        if host == "stuck.invalid":
            stuck.wait()  # deliberately never released: fixed daemon pool owns it
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", int(port)))]

    resolver = bounded_resolver.BoundedResolver(
        workers=2,
        max_pending=4,
        ttl_seconds=30,
        resolver_fn=_resolver_fn,
        thread_name_prefix="veripsa-test-bounded-dns",
    )
    started = time.monotonic()
    first_timeout = None
    try:
        resolver.resolve("stuck.invalid", 443, deadline=time.monotonic() + 0.05)
    except BaseException as exc:
        first_timeout = exc
    elapsed = time.monotonic() - started
    healthy1 = resolver.resolve("healthy.invalid", 443, deadline=time.monotonic() + 0.3)
    healthy2 = resolver.resolve("healthy.invalid", 443, deadline=time.monotonic() + 0.3)
    for _ in range(6):
        try:
            resolver.resolve("stuck.invalid", 443, deadline=time.monotonic() + 0.01)
        except bounded_resolver.ResolutionDeadlineExceeded:
            pass
    resolver_stats = resolver.stats()
    checks.append((
        "bounded resolver: permanent DNS stall times out, shares one inflight, "
        "keeps a healthy worker, caches success, and never grows threads "
        f"(elapsed={elapsed:.3f}s, stats={resolver_stats}, calls={resolver_calls})",
        isinstance(first_timeout, bounded_resolver.ResolutionDeadlineExceeded)
        and elapsed < 0.3
        and healthy1 == healthy2
        and resolver_calls == {"stuck": 1, "healthy": 1}
        and resolver_stats["threads_started"] == 2
        and resolver_stats["threads_alive"] == 2
        and resolver_stats["inflight"] == 1,
    ))

    # A collaborator/OS boundary raising outside Exception must complete its
    # inflight record without permanently shrinking the fixed daemon pool.
    fatal_calls = {"n": 0}

    class _ResolverFatal(BaseException):
        pass

    def _fatal_once(host, port, family, socktype, proto, flags):
        fatal_calls["n"] += 1
        if fatal_calls["n"] == 1:
            raise _ResolverFatal("injected resolver fatal")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", int(port)))]

    fatal_resolver = bounded_resolver.BoundedResolver(
        workers=1,
        max_pending=2,
        ttl_seconds=0,
        negative_ttl_seconds=0,
        resolver_fn=_fatal_once,
        thread_name_prefix="veripsa-test-fatal-dns",
    )
    fatal_error = None
    try:
        fatal_resolver.resolve(
            "fatal.invalid", 443, deadline=time.monotonic() + 0.3)
    except BaseException as exc:
        fatal_error = exc
    fatal_recovery = fatal_resolver.resolve(
        "healthy-after-fatal.invalid", 443, deadline=time.monotonic() + 0.3)
    fatal_stats = fatal_resolver.stats()
    checks.append((
        "bounded resolver: BaseException completes inflight and the fixed worker serves later lookups "
        f"(error={type(fatal_error).__name__ if fatal_error else None}, stats={fatal_stats})",
        isinstance(fatal_error, _ResolverFatal)
        and bool(fatal_recovery)
        and fatal_calls["n"] == 2
        and fatal_stats["threads_started"] == 1
        and fatal_stats["threads_alive"] == 1
        and fatal_stats["inflight"] == 0,
    ))

    # A libc getaddrinfo that outlives an event may remain on the shared fixed
    # resolver worker, but the EventQueue caller returns at ~0.1s.
    original_getaddrinfo = socket.getaddrinfo
    dns_release = threading.Event()
    unique_stuck_host = f"veripsa-dns-stuck-{os.getpid()}.invalid"

    def _stuck_getaddrinfo(host, *args, **kwargs):
        if host == unique_stuck_host:
            dns_release.wait(2.0)
        return original_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = _stuck_getaddrinfo
    dns_budget = event_budget.begin(0.14)  # 25% reserve -> ~0.105s work budget
    dns_started = time.monotonic()
    dns_error = None
    try:
        request = urllib.request.Request(f"http://{unique_stuck_host}/x")
        gr._urlopen_oneshot(request, deadline=event_budget.current_deadline())
    except BaseException as exc:
        dns_error = exc
    finally:
        dns_elapsed = time.monotonic() - dns_started
        event_budget.end(dns_budget)
        socket.getaddrinfo = original_getaddrinfo
        dns_release.set()
    checks.append((
        "HTTP DNS: fake stuck getaddrinfo cannot outlive the ~0.1s event work budget "
        f"(elapsed={dns_elapsed:.3f}s, exc={type(dns_error).__name__ if dns_error else None}, "
        f"resolver={bounded_resolver.stats()})",
        isinstance(dns_error, event_budget.EventBudgetExceeded)
        and dns_elapsed < 0.4
        and bounded_resolver.stats()["threads_started"]
        <= bounded_resolver.DEFAULT_WORKERS,
    ))

    # Real TCP loopback reached via a fake DNS answer. The socket address is
    # loopback, but the HTTP Host header must remain the original hostname.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    loop_port = server.getsockname()[1]
    server_seen = {}

    def _serve_once():
        conn, peer = server.accept()
        try:
            wire = b""
            while b"\r\n\r\n" not in wire:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                wire += chunk
            server_seen["peer"] = peer
            server_seen["wire"] = wire
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\nok"
            )
        finally:
            conn.close()
            server.close()

    server_thread = threading.Thread(target=_serve_once, daemon=True)
    server_thread.start()
    original_resolve = gr._bounded_resolver.resolve
    resolved_calls = []

    def _loopback_resolve(host, port, **kwargs):
        resolved_calls.append((host, port, kwargs["deadline"]))
        return ((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),)

    gr._bounded_resolver.resolve = _loopback_resolve
    loop_body = None
    loop_error = None
    try:
        request = urllib.request.Request(
            f"http://original-host.invalid:{loop_port}/probe?x=1")
        loop_deadline = time.monotonic() + 0.5
        with gr._urlopen_oneshot(request, deadline=loop_deadline) as response:
            loop_body = gr._read_through_deadline(response, loop_deadline)
    except BaseException as exc:
        loop_error = exc
    finally:
        gr._bounded_resolver.resolve = original_resolve
    server_thread.join(0.5)
    wire_lower = server_seen.get("wire", b"").lower()
    checks.append((
        "HTTP connect: real loopback uses fake resolved IP while request Host remains original "
        f"(resolved={[(h, p) for h, p, _ in resolved_calls]}, body={loop_body!r}, "
        f"exc={type(loop_error).__name__ if loop_error else None})",
        loop_error is None
        and loop_body == b"ok"
        and resolved_calls
        and resolved_calls[0][:2] == ("original-host.invalid", loop_port)
        and f"host: original-host.invalid:{loop_port}\r\n".encode() in wire_lower,
    ))

    # Fake sockets make the multi-address timing deterministic. The second
    # address receives only what remains after the first failure.
    original_socket_factory = socket.socket
    original_resolve = gr._bounded_resolver.resolve
    connect_attempts = []

    class _TimedCandidate:
        def __init__(self, *args):
            self.timeout = None
            self.closed = False

        def settimeout(self, value):
            self.timeout = value

        def bind(self, _address):
            pass

        def setsockopt(self, *_args):
            pass

        def connect(self, address):
            connect_attempts.append((address, self.timeout))
            if len(connect_attempts) == 1:
                time.sleep(0.06)
            else:
                time.sleep(max(0.0, self.timeout) + 0.005)
            raise ConnectionRefusedError("simulated address failure")

        def close(self):
            self.closed = True

    socket.socket = _TimedCandidate
    gr._bounded_resolver.resolve = lambda host, port, **kwargs: (
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", port)),
    )
    multi_conn = gr._DeadlineHTTPConnection("multi.invalid", 80, timeout=1.0)
    multi_conn._veripsa_connect_deadline = time.monotonic() + 0.1
    multi_started = time.monotonic()
    multi_error = None
    try:
        multi_conn.connect()
    except BaseException as exc:
        multi_error = exc
    finally:
        multi_elapsed = time.monotonic() - multi_started
        socket.socket = original_socket_factory
        gr._bounded_resolver.resolve = original_resolve
    checks.append((
        "HTTP connect: multiple addresses share one 0.1s absolute deadline "
        f"(elapsed={multi_elapsed:.3f}s, attempts={connect_attempts}, "
        f"exc={type(multi_error).__name__ if multi_error else None})",
        isinstance(multi_error, socket.timeout)
        and 1 <= len(connect_attempts) <= 2
        and multi_elapsed < 0.25
        and (
            len(connect_attempts) == 1
            or connect_attempts[1][1] < connect_attempts[0][1]
        ),
    ))

    # A blackholed first family must leave a deterministic share for the
    # healthy fallback instead of consuming the whole logical-call deadline.
    original_socket_factory = socket.socket
    original_resolve = gr._bounded_resolver.resolve
    fallback_attempts = []

    class _FallbackCandidate:
        def __init__(self, *args):
            self.timeout = None

        def settimeout(self, value):
            self.timeout = value

        def bind(self, _address):
            pass

        def setsockopt(self, *_args):
            pass

        def connect(self, address):
            fallback_attempts.append((address, self.timeout))
            if len(fallback_attempts) == 1:
                time.sleep(self.timeout + 0.003)
                raise socket.timeout("simulated IPv6 blackhole")

        def close(self):
            pass

    socket.socket = _FallbackCandidate
    gr._bounded_resolver.resolve = lambda host, port, **kwargs: (
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", port)),
    )
    fallback_conn = gr._DeadlineHTTPConnection("dualstack.invalid", 80, timeout=1.0)
    fallback_conn._veripsa_connect_deadline = time.monotonic() + 0.12
    fallback_started = time.monotonic()
    fallback_error = None
    try:
        fallback_conn.connect()
    except BaseException as exc:
        fallback_error = exc
    finally:
        fallback_elapsed = time.monotonic() - fallback_started
        socket.socket = original_socket_factory
        gr._bounded_resolver.resolve = original_resolve
    checks.append((
        "HTTP connect: blackholed first address leaves a fair share for healthy fallback "
        f"(elapsed={fallback_elapsed:.3f}s, attempts={fallback_attempts})",
        fallback_error is None
        and len(fallback_attempts) == 2
        and fallback_attempts[0][1] < 0.08
        and fallback_elapsed < 0.15,
    ))

    # HTTPS connects to an IP but SNI/certificate verification remains bound
    # to the original hostname. TLS handshake begins only after the wrapped
    # socket is assigned back to conn.sock.
    original_socket_factory = socket.socket
    original_resolve = gr._bounded_resolver.resolve
    tls_observed = {}

    class _RawSocket:
        def settimeout(self, value):
            tls_observed["raw_timeout"] = value

        def bind(self, _address):
            pass

        def connect(self, address):
            tls_observed["connect_address"] = address

        def setsockopt(self, *_args):
            pass

        def close(self):
            tls_observed["raw_closed"] = True

    class _TLSSocket:
        def settimeout(self, value):
            tls_observed["tls_timeout"] = value

        def do_handshake(self):
            tls_observed["handshake"] = True

        def close(self):
            tls_observed["tls_closed"] = True

    class _TLSContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        def wrap_socket(self, raw, *, server_hostname, do_handshake_on_connect):
            tls_observed["wrapped_raw"] = raw
            tls_observed["server_hostname"] = server_hostname
            tls_observed["deferred_handshake"] = not do_handshake_on_connect
            return _TLSSocket()

    socket.socket = lambda *args: _RawSocket()
    gr._bounded_resolver.resolve = lambda host, port, **kwargs: (
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port)),
    )
    tls_conn = gr._DeadlineHTTPSConnection(
        "api.original.invalid",
        443,
        timeout=1.0,
        context=_TLSContext(),
    )
    tls_conn._veripsa_connect_deadline = time.monotonic() + 0.2
    tls_error = None
    try:
        tls_conn.connect()
    except BaseException as exc:
        tls_error = exc
    finally:
        socket.socket = original_socket_factory
        gr._bounded_resolver.resolve = original_resolve
    checks.append((
        "HTTPS identity: resolved IP is used only for TCP; original hostname stays SNI/verify "
        f"(observed={tls_observed}, exc={type(tls_error).__name__ if tls_error else None})",
        tls_error is None
        and tls_observed.get("connect_address") == ("203.0.113.7", 443)
        and tls_observed.get("server_hostname") == "api.original.invalid"
        and tls_observed.get("deferred_handshake") is True
        and tls_observed.get("handshake") is True
        and tls_conn._context.check_hostname is True,
    ))

    # Same-installation token wait is no longer an unbounded RLock.acquire.
    client = gr.GitHubREST("app", "key", "installation")
    client._token = "fresh"
    client._token_exp = time.time() + 3600
    lock_entered = threading.Event()
    lock_release = threading.Event()

    def _hold_token_lock():
        with client._token_lock:
            lock_entered.set()
            lock_release.wait(1.0)

    holder = threading.Thread(target=_hold_token_lock, daemon=True)
    holder.start()
    lock_entered.wait(0.3)
    lock_budget = event_budget.begin(0.14)
    lock_started = time.monotonic()
    lock_error = None
    try:
        client._itoken()
    except BaseException as exc:
        lock_error = exc
    finally:
        lock_elapsed = time.monotonic() - lock_started
        event_budget.end(lock_budget)
        lock_release.set()
    holder.join(0.3)
    checks.append((
        "token lock: same-installation waiter stops at the ~0.1s event budget and cannot return late success "
        f"(elapsed={lock_elapsed:.3f}s, exc={type(lock_error).__name__ if lock_error else None})",
        isinstance(lock_error, event_budget.EventBudgetExceeded)
        and lock_elapsed < 0.4,
    ))

    # One worker's 401 is deliberately delayed until the other has invalidated
    # T0 and minted T1. Conditional invalidation must leave T1 intact, so both
    # retries use T1 and the installation mints exactly once.
    concurrent = gr.GitHubREST("app", "key", "same-install")
    concurrent._token = "T0"
    concurrent._token_exp = time.time() + 3600
    delayed_entered = threading.Event()
    release_delayed = threading.Event()
    fast_done = threading.Event()
    mint_count = {"n": 0}
    concurrent_results = {}
    concurrent_errors = []

    def _mint_once():
        mint_count["n"] += 1
        concurrent._token = f"T{mint_count['n']}"
        concurrent._token_exp = time.time() + 3600
        return concurrent._token

    def _concurrent_req(method, url, token, body=None, accept="application/vnd.github+json"):
        if token != "T0":
            return {"token": token}
        if threading.current_thread().name == "delayed-old-401":
            delayed_entered.set()
            release_delayed.wait(1.0)
        raise _http_error(401)

    concurrent._mint_installation_token = _mint_once
    concurrent._req = _concurrent_req

    def _call_api(name, done=None):
        try:
            concurrent_results[name] = concurrent._api("GET", "/x")
        except BaseException as exc:
            concurrent_errors.append(exc)
        finally:
            if done is not None:
                done.set()

    delayed_thread = threading.Thread(
        target=_call_api,
        args=("delayed",),
        name="delayed-old-401",
        daemon=True,
    )
    fast_thread = threading.Thread(
        target=_call_api,
        args=("fast", fast_done),
        name="fast-401",
        daemon=True,
    )
    delayed_thread.start()
    delayed_entered.wait(0.3)
    fast_thread.start()
    fast_done.wait(0.5)
    token_after_fast = concurrent._token
    release_delayed.set()
    delayed_thread.join(0.5)
    fast_thread.join(0.5)
    checks.append((
        "token 401 race: concurrent old-token failures mint once; delayed old 401 cannot clear the new token "
        f"(mints={mint_count['n']}, after_fast={token_after_fast}, final={concurrent._token}, "
        f"results={concurrent_results}, errors={[type(e).__name__ for e in concurrent_errors]})",
        not concurrent_errors
        and mint_count["n"] == 1
        and token_after_fast == "T1"
        and concurrent._token == "T1"
        and concurrent_results == {
            "fast": {"token": "T1"},
            "delayed": {"token": "T1"},
        },
    ))

    # The Link-header surface has its own request wrapper; pin the same
    # expected-token rule there rather than relying on similarity to `_api`.
    link_client = gr.GitHubREST("app", "key", "link-install")
    link_client._token = "L0"
    link_client._token_exp = time.time() + 3600
    link_calls = []
    link_mints = {"n": 0}

    def _link_mint():
        link_mints["n"] += 1
        link_client._token = "L2"
        link_client._token_exp = time.time() + 3600
        return link_client._token

    def _link_request(method, url, token, body=None, accept="application/vnd.github+json"):
        link_calls.append(token)
        if len(link_calls) == 1:
            # A concurrent worker has already replaced the failed attempt's
            # L0. The delayed L0 401 below must not clear L1.
            with link_client._token_lock:
                link_client._token = "L1"
                link_client._token_exp = time.time() + 3600
            raise _http_error(401)
        return {"token": token}, '<https://api.github.com/x?page=2>; rel="next"'

    link_client._mint_installation_token = _link_mint
    link_client._req_with_link = _link_request
    link_result = link_client._api_with_link("GET", "/x")
    checks.append((
        "_api_with_link token race: delayed old 401 preserves a concurrently published token "
        f"(calls={link_calls}, mints={link_mints['n']}, token={link_client._token})",
        link_calls == ["L0", "L1"]
        and link_mints["n"] == 0
        and link_client._token == "L1"
        and link_result[0] == {"token": "L1"},
    ))

    # Tarball keeps the two-positional-argument seam. Simulate another worker
    # publishing T1 before the old T0 response reaches us: retry reuses T1,
    # rather than clearing it and reminting T2.
    tar = gr.GitHubREST("app", "key", "tar-install")
    tar._token = "T0"
    tar._token_exp = time.time() + 3600
    tar_fetches = []
    tar_mints = {"n": 0}

    def _unexpected_tar_mint():
        tar_mints["n"] += 1
        tar._token = "T2"
        tar._token_exp = time.time() + 3600
        return tar._token

    def _two_arg_tarball_seam(repo, sha):
        tar_fetches.append((repo, sha, tar._token))
        if len(tar_fetches) == 1:
            with tar._token_lock:
                tar._token = "T1"
                tar._token_exp = time.time() + 3600
            raise _http_error(401)
        return b"tar"

    tar._mint_installation_token = _unexpected_tar_mint
    tar._tarball_fetch_once = _two_arg_tarball_seam
    original_verify = gr._verify_complete_gzip
    gr._verify_complete_gzip = lambda _data: None
    tar_error = None
    tar_data = None
    try:
        tar_data = tar.download_tarball("o/r", "sha")
    except BaseException as exc:
        tar_error = exc
    finally:
        gr._verify_complete_gzip = original_verify
    checks.append((
        "tarball auth race: legacy two-arg seam works and old 401 cannot erase another worker's new token "
        f"(fetches={tar_fetches}, mints={tar_mints['n']}, token={tar._token}, data={tar_data!r})",
        tar_error is None
        and tar_data == b"tar"
        and len(tar_fetches) == 2
        and all(len(item) == 3 for item in tar_fetches)
        and tar_fetches[0][2] == "T0"
        and tar_fetches[1][2] == "T1"
        and tar_mints["n"] == 0
        and tar._token == "T1",
    ))

    ok = True
    for name, condition in checks:
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
        ok = ok and bool(condition)
    print("GITHUB DNS TOKEN CONCURRENCY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
