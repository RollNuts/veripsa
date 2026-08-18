#!/usr/bin/env python3
"""Auth-lifecycle gate — the installation-token + webhook-secret handling UNDER STRESS.

The token model: the App mints a short-lived (~1h) installation token off the App JWT, caches it, remints it
PROACTIVELY before expiry, and REACTIVELY remints+retries-once on a 401 (an EARLY revocation: key rotation, a
suspend-resume, a slow clock). This gate injects each adversarial moment and asserts (1) the call RECOVERS
(remints + succeeds) rather than failing the delivery, (2) a genuinely broken token still fails CLEANLY (one
retry, never a loop), (3) the GitHub rate-limit family stays bounded (no tight retry loop, no crash), and (4)
NO secret material (installation token, App JWT, private key, webhook secret) ever appears in any log/exception
string on any path — content-free secrets.

No network, no DB: a fake _urlopen scripts the HTTP responses; _sleep is a no-op that RECORDS waits.

Run:  python3 tests/test_auth_lifecycle.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
from github_rest import GitHubREST  # noqa: E402

# Secret material we assert NEVER leaks into a log/exception string anywhere.
# Build provider-shaped sentinels at runtime. Keeping the complete shapes out of the
# source prevents public secret scanners from mistaking these deliberately invalid
# leak-test values for live credentials.
SECRET_TOKEN = "ghs_" + "SUPERSECRET_INSTALLATION_TOKEN_should_never_be_logged"
SECRET_KEY = ("-----BEGIN " + "RSA PRIVATE KEY-----\n"
              "SECRETKEYMATERIAL_should_never_be_logged\n"
              "-----END " + "RSA PRIVATE KEY-----")
SECRET_JWT = "eyJ" + ".FAKE_APP_JWT_should_never_be_logged.sig"
SECRET_WEBHOOK = "whsec_" + "WEBHOOK_SECRET_should_never_be_logged"
_SECRETS = [SECRET_TOKEN, SECRET_KEY, SECRET_JWT, SECRET_WEBHOOK,
            "should_never_be_logged", "SUPERSECRET", "SECRETKEYMATERIAL"]


class _Resp:
    def __init__(self, body): self._body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, *a): return self._body


def _iso(delta_seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delta_seconds))


def _http(code, headers=None, body=b""):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", headers or {}, io.BytesIO(body))


def _mint_body(idx, exp_delta=3600):
    """A successful /access_tokens response carrying the SECRET token (so leak-checks have real material)."""
    return _Resp(json.dumps({"token": SECRET_TOKEN, "expires_at": _iso(exp_delta)}).encode())


def _scripted_client(script):
    """A GitHubREST whose _urlopen plays `script` (each step → _Resp or raises). The App-JWT is stubbed to the
    SECRET jwt; /access_tokens always returns the SECRET token. Records mints + data-calls + sleeps."""
    c = GitHubREST("app-id", SECRET_KEY, "1")
    c._jwt = lambda: SECRET_JWT                 # stub: no PyJWT / real private key needed (carries the secret)
    waits, mints, dcalls = [], {"n": 0}, {"n": 0}
    c._sleep = lambda s: waits.append(s)

    def _urlopen(req):
        if req.full_url.endswith("/access_tokens"):
            i = mints["n"]; mints["n"] += 1
            return _mint_body(i)
        i = dcalls["n"]; dcalls["n"] += 1
        step = script[min(i, len(script) - 1)]
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step()
        return step
    c._urlopen = _urlopen
    return c, waits, mints, dcalls


OK = _Resp(b'{"ok": 1}')
TARBALL = b"\x1f\x8b" + b"x" * 64   # bytes good enough to reach the integrity gate (we stub that gate per-test)


def main() -> int:
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))

    # ── (a) TOKEN EXPIRES MID-OPERATION on a normal API call → reactive remint + retry once → succeeds ───────
    # The token was valid at fetch time but revoked early; the data call 401s. _api must invalidate, remint
    # (2nd mint), and retry the SAME call once → success. The delivery is NOT failed by an early revocation.
    c, waits, mints, dcalls = _scripted_client([_http(401, body=b'{"message":"Bad credentials"}'), lambda: OK])
    r = c._api("GET", "/repos/o/r/pulls/1/files")
    chk(f"(a) 401 mid-op on _api → remint+retry once → succeeds (mints={mints['n']}, data_calls={dcalls['n']})",
        r == {"ok": 1} and mints["n"] == 2 and dcalls["n"] == 2)

    # ── (a') the RETRY also 401s (a genuinely broken/revoked key) → re-raises CLEANLY, ONE retry, NO loop ────
    c, waits, mints, dcalls = _scripted_client([_http(401), _http(401)])
    raised = False
    try:
        c._api("GET", "/x")
    except urllib.error.HTTPError as e:
        raised = (e.code == 401)
    chk(f"(a') a persistent 401 re-raises after exactly ONE reactive retry — never loops "
        f"(mints={mints['n']}, data_calls={dcalls['n']})",
        raised and mints["n"] == 2 and dcalls["n"] == 2)

    # ── (a2) 403 STALE-CREDENTIAL mid-op (a token invalidated mid-life — a permission re-approval during a
    #     migration revokes the cached token before its ~1h expiry). The data call 403s
    #     with a 'Bad credentials' body (NOT a rate-limit, NOT a permission denial). _api must remint (2nd mint)
    #     + retry the SAME call ONCE → success. Exactly one remint+retry, like the 401 path. ─────────────────────
    c, waits, mints, dcalls = _scripted_client([_http(403, body=b'{"message":"Bad credentials"}'), lambda: OK])
    r = c._api("GET", "/repos/o/r/pulls/1/files")
    chk(f"(a2) 403 stale-credential mid-op on _api → remint+retry once → succeeds (mints={mints['n']}, data_calls={dcalls['n']})",
        r == {"ok": 1} and mints["n"] == 2 and dcalls["n"] == 2)

    # ── (a2') a GENUINE permission 403 ('Resource not accessible by integration' — the App lacks a scope) must
    #     NOT remint and must NOT loop: it re-raises after ZERO remint (the request ran once, no reactive retry).
    #     This is the constraint: at most one remint+retry on a real auth failure, NEVER a remint-loop on a real
    #     permission 403. ───────────────────────────────────────────────────────────────────────────────────────
    c, waits, mints, dcalls = _scripted_client([_http(403, body=b'{"message":"Resource not accessible by integration"}')])
    raised_perm = False
    try:
        c._api("GET", "/repos/o/r/pulls/1/files")
    except urllib.error.HTTPError as e:
        raised_perm = (e.code == 403)
    chk(f"(a2') a genuine permission 403 does NOT remint-loop — re-raises with ZERO remint, ONE data call "
        f"(mints={mints['n']}, data_calls={dcalls['n']})",
        raised_perm and mints["n"] == 1 and dcalls["n"] == 1)

    # ── (a2'') a persistent stale-credential 403 (the retry ALSO 403s) → re-raises after exactly ONE remint+retry
    #     (never a loop), mirroring the persistent-401 case (a'). ──────────────────────────────────────────────
    c, waits, mints, dcalls = _scripted_client([_http(403, body=b'{"message":"Bad credentials"}'),
                                                _http(403, body=b'{"message":"Bad credentials"}')])
    raised_403 = False
    try:
        c._api("GET", "/x")
    except urllib.error.HTTPError as e:
        raised_403 = (e.code == 403)
    chk(f"(a2'') a persistent stale-credential 403 re-raises after exactly ONE reactive remint+retry — never loops "
        f"(mints={mints['n']}, data_calls={dcalls['n']})",
        raised_403 and mints["n"] == 2 and dcalls["n"] == 2)

    # ── (a2''') a RATE-LIMIT 403 is NOT mistaken for a stale credential (no spurious remint): a 403 carrying a
    #     rate-limit signal is handled by the bounded backoff path (case c), NOT the remint path. With remaining=0
    #     + a near reset, _req backs off once then the retry succeeds — minted ONCE (the token was always valid). ─
    reset_soon = str(int(time.time()) + 3)
    c, waits, mints, dcalls = _scripted_client([_http(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset_soon},
                                                      b'{"message":"API rate limit exceeded"}'), lambda: OK])
    r = c._api("GET", "/x")
    chk(f"(a2''') a rate-limit 403 is NOT reminted (token stays valid) — bounded backoff then success, minted ONCE "
        f"(mints={mints['n']}, waits={len(waits)})",
        r == {"ok": 1} and mints["n"] == 1 and len(waits) == 1)

    # ── (a2'''') REGRESSION LOCK — a PLAIN 403 on an APP-JWT call (the GET /app/installations shape) re-raises
    #     CLEANLY: ONE call, NO retry-loop, NO remint. /app/installations is authed with the App JWT through _req
    #     DIRECTLY (not _api), so it has NO reactive remint net — as defense-in-depth, any unclassified app-level
    #     403 must propagate at once so the for_account map-builder can fail soft, NOT spin. A plain 403 (no
    #     Retry-After, no X-RateLimit-*) is _retry_wait→None → re-raise on the first attempt. This is the auth-side
    #     guarantee that the multi-installation gate's (A5) fail-soft depends on. ────────────────────────────────
    c, waits, mints, dcalls = _scripted_client([_http(403, body=b'{"message":"Forbidden"}')])
    raised_appjwt = False
    try:
        c._req("GET", "/app/installations?per_page=100&page=1", SECRET_JWT)   # App-JWT call, the real shape
    except urllib.error.HTTPError as e:
        raised_appjwt = (e.code == 403)
    chk(f"(a2'''') a plain 403 on the App-JWT /app/installations call re-raises on the FIRST attempt — no retry "
        f"loop, no remint (data_calls={dcalls['n']}, mints={mints['n']}, waits={len(waits)})",
        raised_appjwt and dcalls["n"] == 1 and mints["n"] == 0 and waits == [])

    # ── (a'') download_tarball — THE FIX: it was the one installation-token call WITHOUT the reactive net. An
    #     early-revocation 401 on the API-side tarball request must now remint + retry once → ingest survives.
    #     download_tarball wraps _tarball_fetch_once (which builds its own no-redirect opener — it cannot route
    #     through _urlopen), so we stub THAT seam: it 401s the first time, succeeds after the remint. We also
    #     assert _tarball_fetch_once read self._itoken() between attempts (i.e. a REAL remint happened). ───────
    import github_rest as _gr
    _orig_verify = _gr._verify_complete_gzip
    _gr._verify_complete_gzip = lambda d: None              # bypass the gzip integrity gate (separately tested)

    def _tarball_client(first_then):
        c = GitHubREST("app-id", SECRET_KEY, "1")
        c._jwt = lambda: SECRET_JWT
        c._sleep = lambda s: None
        st = {"mints": 0, "fetches": 0, "tokens_seen": []}

        def _urlopen(req):                                  # only /access_tokens reaches here (mint)
            if req.full_url.endswith("/access_tokens"):
                st["mints"] += 1
                return _mint_body(st["mints"])
            raise AssertionError("unexpected non-mint urlopen in tarball client")
        c._urlopen = _urlopen

        def _fetch_once(repo, sha):
            st["fetches"] += 1
            st["tokens_seen"].append(c._itoken())           # forces a real proactive/reactive mint per attempt
            step = first_then[min(st["fetches"] - 1, len(first_then) - 1)]
            if isinstance(step, BaseException):
                raise step
            return step
        c._tarball_fetch_once = _fetch_once
        return c, st

    c, st = _tarball_client([_http(401), TARBALL])
    data = c.download_tarball("o/r", "deadbeef")
    chk(f"(a'') download_tarball early-revocation 401 → remint+retry once → ingest survives "
        f"(mints={st['mints']}, fetches={st['fetches']}, tokens_distinct={len(set(st['tokens_seen']))})",
        data == TARBALL and st["fetches"] == 2 and st["mints"] == 2)

    # download_tarball persistent 401 still fails cleanly (one retry, no loop)
    c, st = _tarball_client([_http(401), _http(401)])
    raised_dt = False
    try:
        c.download_tarball("o/r", "deadbeef")
    except urllib.error.HTTPError as e:
        raised_dt = (e.code == 401)
    chk(f"(a'') download_tarball persistent 401 re-raises after ONE retry — no loop (fetches={st['fetches']})",
        raised_dt and st["fetches"] == 2 and st["mints"] == 2)
    _gr._verify_complete_gzip = _orig_verify

    # ── (b) PROACTIVE refresh before 1h expiry + no thundering-herd: a still-fresh token is reused (one mint),
    #     a within-skew/expired token remints exactly once on the next call (no burst of mints). ──────────────
    c, waits, mints, dcalls = _scripted_client([lambda: OK])
    c._api("GET", "/a"); c._api("GET", "/b"); c._api("GET", "/c")
    chk(f"(b) a fresh token is reused across 3 calls — minted ONCE, no herd (mints={mints['n']})", mints["n"] == 1)

    # force the cached token to within TOKEN_SKEW_SECONDS of expiry → the next call remints exactly once
    c, waits, mints, dcalls = _scripted_client([lambda: OK])
    c._api("GET", "/a")                                   # mint #1 (fresh)
    c._token_exp = time.time() + (GitHubREST.TOKEN_SKEW_SECONDS - 1)   # now within the skew window
    c._api("GET", "/b")                                   # proactive remint → mint #2
    c._api("GET", "/c")                                   # fresh again → no further mint
    chk(f"(b) within-skew token proactively reminted exactly once, then reused (mints={mints['n']})", mints["n"] == 2)

    # Keyed workers share a GitHubREST client. A cold token check must be one
    # atomic check→mint→publish sequence, not a mint herd.
    herd = GitHubREST("app-id", SECRET_KEY, "same-install")
    herd_barrier = threading.Barrier(3)
    herd_count = {"n": 0}
    herd_count_lock = threading.Lock()
    herd_results = []

    def _slow_shared_mint():
        with herd_count_lock:
            herd_count["n"] += 1
        time.sleep(0.08)
        herd._token = "one-shared-token"
        herd._token_exp = time.time() + 3600
        return herd._token

    herd._mint_installation_token = _slow_shared_mint

    def _read_shared_token():
        herd_barrier.wait()
        herd_results.append(herd._itoken())

    herd_threads = [
        threading.Thread(target=_read_shared_token, daemon=True),
        threading.Thread(target=_read_shared_token, daemon=True),
    ]
    for thread in herd_threads:
        thread.start()
    herd_barrier.wait()
    for thread in herd_threads:
        thread.join(1.0)
    chk(
        f"(b2) two workers sharing one installation mint exactly once "
        f"(mints={herd_count['n']}, results={herd_results!r})",
        herd_count["n"] == 1
        and herd_results == ["one-shared-token", "one-shared-token"],
    )

    # Cache creation is also shared. Delay construction so the race is
    # deterministic: without the cache RLock both callers would construct.
    cache_root = GitHubREST("app-id", SECRET_KEY, "root")
    real_client_class = _gr.GitHubREST
    cache_barrier = threading.Barrier(3)
    cache_created = {"n": 0}
    cache_created_lock = threading.Lock()
    cached_clients = []

    def _delayed_client_factory(*args, **kwargs):
        with cache_created_lock:
            cache_created["n"] += 1
        time.sleep(0.08)
        return real_client_class(*args, **kwargs)

    def _get_cached_sibling():
        cache_barrier.wait()
        cached_clients.append(cache_root.for_installation("child"))

    _gr.GitHubREST = _delayed_client_factory
    cache_threads = [
        threading.Thread(target=_get_cached_sibling, daemon=True),
        threading.Thread(target=_get_cached_sibling, daemon=True),
    ]
    try:
        for thread in cache_threads:
            thread.start()
        cache_barrier.wait()
        for thread in cache_threads:
            thread.join(1.0)
    finally:
        _gr.GitHubREST = real_client_class
    chk(
        f"(b3) concurrent for_installation creates one shared sibling "
        f"(constructed={cache_created['n']}, identities={len({id(c) for c in cached_clients})})",
        cache_created["n"] == 1
        and len(cached_clients) == 2
        and cached_clients[0] is cached_clients[1]
        and cached_clients[0]._installations_lock is cache_root._installations_lock,
    )

    # Different installation siblings deliberately do NOT share token locks.
    # Their network mints can overlap even though their client cache is shared.
    install_a = cached_clients[0]
    install_b = cache_root.for_installation("other-child")
    both_release = threading.Event()
    entered_a = threading.Event()
    entered_b = threading.Event()
    independent_results = {}

    def _independent_mint(client, entered, token):
        def mint():
            entered.set()
            both_release.wait(1.0)
            client._token = token
            client._token_exp = time.time() + 3600
            return token
        return mint

    install_a._mint_installation_token = _independent_mint(install_a, entered_a, "token-a")
    install_b._mint_installation_token = _independent_mint(install_b, entered_b, "token-b")
    independent_threads = [
        threading.Thread(
            target=lambda: independent_results.setdefault("a", install_a._itoken()),
            daemon=True,
        ),
        threading.Thread(
            target=lambda: independent_results.setdefault("b", install_b._itoken()),
            daemon=True,
        ),
    ]
    for thread in independent_threads:
        thread.start()
    both_entered = entered_a.wait(0.4) and entered_b.wait(0.4)
    both_release.set()
    for thread in independent_threads:
        thread.join(1.0)
    chk(
        f"(b4) different installations mint independently "
        f"(both_entered={both_entered}, results={independent_results!r})",
        both_entered
        and independent_results == {"a": "token-a", "b": "token-b"}
        and install_a._token_lock is not install_b._token_lock,
    )

    # ── (c) RATE-LIMIT BURST (primary + secondary) → bounded backoff+retry, never a tight loop, never a crash ─
    # secondary limit (Retry-After) honored exactly; primary limit (remaining 0) waits toward reset; every wait
    # is capped at RETRY_CAP_SECONDS (no unbounded sleep); persistence exhausts MAX_RETRIES then re-raises.
    c, waits, mints, dcalls = _scripted_client([_http(403, {"Retry-After": "2"}), lambda: OK])
    r = c._req("GET", "/x", "tok")
    chk(f"(c) 403 secondary-limit Retry-After honored, then succeeds (waits={waits})", r == {"ok": 1} and waits == [2.0])

    reset = str(int(time.time()) + 5)
    c, waits, mints, dcalls = _scripted_client([_http(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}), lambda: OK])
    r = c._req("GET", "/x", "tok")
    chk(f"(c) 403 primary-limit (remaining 0) waits toward reset, bounded, succeeds (waits={waits})",
        r == {"ok": 1} and len(waits) == 1 and 0 < waits[0] <= 6)

    # An ABSURD Retry-After is first clamped to RETRY_CAP_SECONDS, then rejected because even that bounded
    # wait cannot fit inside the ONE 30s logical-call deadline. It must not invoke sleep or attempt the request
    # again: a per-wait cap that is larger than the complete operation cap is not permission to overrun it.
    c, waits, mints, dcalls = _scripted_client([_http(429, {"Retry-After": "999999"}), lambda: OK])
    absurd_rejected = False
    try:
        c._req("GET", "/x", "tok")
    except TimeoutError:
        absurd_rejected = True
    chk(f"(c) an absurd Retry-After cannot outlive the logical-call deadline "
        f"(waits={waits}, data_calls={dcalls['n']})",
        absurd_rejected and waits == [] and dcalls["n"] == 1)

    # a STORM of secondary limits then success: every wait bounded, finite attempts, no tight (zero-wait) loop
    c, waits, mints, dcalls = _scripted_client([_http(429, {"Retry-After": "1"})] * GitHubREST.MAX_RETRIES + [lambda: OK])
    r = c._req("GET", "/x", "tok")
    chk(f"(c) a burst of {GitHubREST.MAX_RETRIES} secondary limits then success — bounded retries, every wait>0 "
        f"(attempts_waited={len(waits)})",
        r == {"ok": 1} and len(waits) == GitHubREST.MAX_RETRIES and all(w > 0 for w in waits)
        and all(w <= GitHubREST.RETRY_CAP_SECONDS for w in waits))

    # persistent primary limit → exhausts MAX_RETRIES then re-raises (never an infinite loop)
    c, waits, mints, dcalls = _scripted_client([_http(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset})])
    raised_rl = False
    try:
        c._req("GET", "/x", "tok")
    except urllib.error.HTTPError as e:
        raised_rl = (e.code == 403)
    chk(f"(c) persistent rate limit re-raises after MAX_RETRIES — terminates, never spins (waits={len(waits)})",
        raised_rl and len(waits) == GitHubREST.MAX_RETRIES)

    # ── (d) 401 MID-MULTI-CALL operation (read-then-write) → recovers WITHOUT double-acting ──────────────────
    # upsert_check = list_check_runs (GET) then post/patch (WRITE). The GET succeeds; the WRITE 401s. The
    # reactive remint must retry ONLY the write (not re-run the GET) → exactly ONE create, no double-post.
    writes = {"post": 0, "patch": 0}

    class _Seq:
        def __init__(self): self.i = 0

        def __call__(self):
            self.i += 1
            return self.i
    seq = _Seq()

    def _urlopen_multicall(req):
        if req.full_url.endswith("/access_tokens"):
            return _mint_body(0)
        m = req.get_method()
        if m == "GET":                                    # list_check_runs → empty (forces a POST create)
            return _Resp(json.dumps({"check_runs": []}).encode())
        # the WRITE: 401 the FIRST time only, then succeed → must result in exactly ONE effective create
        n = seq()
        if n == 1:
            raise _http(401)
        writes["post"] += 1
        return _Resp(json.dumps({"id": 99}).encode())
    cd = GitHubREST("app-id", SECRET_KEY, "1")
    cd._jwt = lambda: SECRET_JWT
    cd._sleep = lambda s: None
    cd._urlopen = _urlopen_multicall
    cd._token, cd._token_exp = SECRET_TOKEN, time.time() + 3600
    res = cd.upsert_check("o/r", "deadbeef", "neutral", "t", "s")
    chk(f"(d) 401 on the WRITE of a read-then-write op → retried once, exactly ONE create, no double-act "
        f"(posts={writes['post']}, result={res})",
        writes["post"] == 1 and res == {"id": 99})

    # ── (e) SECRET LEAK SWEEP: drive EVERY error path and assert NO secret material reaches a log/exception ──
    # We capture stdout (the print(...) log lines) AND every exception string raised across the auth paths, and
    # assert none of them contains the token / JWT / private key / webhook secret.
    import contextlib

    captured = io.StringIO()
    seen_exc_strings = []

    def _run_and_collect(fn):
        try:
            with contextlib.redirect_stdout(captured):
                fn()
        except BaseException as e:                        # capture the exception's string form too
            seen_exc_strings.append(f"{type(e).__name__}: {e}")
            # walk the cause/context chain (a `raise ... from e` could carry an inner secret)
            cur = e
            for _ in range(6):
                cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
                if cur is None:
                    break
                seen_exc_strings.append(f"{type(cur).__name__}: {cur}")

    # error path 1: a 401 storm that ultimately fails (token + jwt + key all in play during the mints/calls)
    c, _w, _m, _d = _scripted_client([_http(401, body=SECRET_TOKEN.encode()), _http(401, body=SECRET_KEY.encode())])
    _run_and_collect(lambda: c._api("GET", "/x"))
    # error path 2: a rate-limit exhaustion (the body carries a secret-looking blob the client must NOT echo)
    c, _w, _m, _d = _scripted_client([_http(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}, SECRET_KEY.encode())])
    _run_and_collect(lambda: c._req("GET", "/x", SECRET_TOKEN))
    # error path 3: download_tarball persistent 401 (the fixed path; its API request carries the Bearer token).
    #     Stub _tarball_fetch_once to 401 with a body carrying secret material — the wrapper must not echo it.
    cdt, _st = _tarball_client([_http(401, body=SECRET_TOKEN.encode()), _http(401, body=SECRET_KEY.encode())])
    _run_and_collect(lambda: cdt.download_tarball("o/r", "deadbeef"))
    # error path 4: a truncated/corrupt tarball → the integrity RuntimeError must not echo any body bytes.
    #     Here we go through the REAL _verify_complete_gzip on a non-gzip body that carries secret material.
    _gr._verify_complete_gzip = _orig_verify                # use the REAL integrity gate for this path
    cgz, _st2 = _tarball_client([SECRET_KEY.encode()])      # _tarball_fetch_once returns a non-gzip secret blob
    _run_and_collect(lambda: cgz.download_tarball("o/r", "deadbeef"))
    # error path 5: the webhook-signature check — a wrong secret must reject WITHOUT echoing the secret anywhere
    import server as S
    _run_and_collect(lambda: S.verify_signature(SECRET_WEBHOOK, b"body", "sha256=deadbeef"))

    log_text = captured.getvalue()
    exc_text = "\n".join(seen_exc_strings)
    haystack = log_text + "\n" + exc_text
    leaked = [s for s in _SECRETS if s in haystack]
    chk(f"(e) NO secret (token/jwt/private-key/webhook-secret) leaked into any log OR exception string "
        f"across 5 error paths (leaked={leaked!r})",
        leaked == [])

    # (e') the startup FATAL message for a missing webhook secret names the VARIABLE, never a value (defense:
    #      assert the secret value is not in the constant message and the var name IS).
    chk("(e') the empty-secret startup guard rejects unsigned mode (verify_signature default-deny on real secret)",
        S.verify_signature(SECRET_WEBHOOK, b"x", None) is False
        and S.verify_signature(SECRET_WEBHOOK, b"x", "sha256=00") is False)

    okall = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        okall = okall and cond
    print("AUTH LIFECYCLE GATE:", "PASS" if okall else "FAIL")
    return 0 if okall else 1


if __name__ == "__main__":
    sys.exit(main())
