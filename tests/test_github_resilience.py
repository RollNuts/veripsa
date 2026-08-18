#!/usr/bin/env python3
"""GitHub API resilience gate — _req retries rate limits + transient 5xx, re-raises the rest.

No network, no DB: a fake `_urlopen` scripts the HTTP responses and `_sleep` is a no-op that RECORDS the waits
(so backoff is instant + asserted). Proves the App holds up under GitHub's real-world rate limits / 5xx —
and that 404 / non-rate-limit 403 still re-raise immediately (so get_file_at keeps its None semantics).

Run:  python3 tests/test_github_resilience.py
"""
from __future__ import annotations

import io
import os
import json
import sys
import time
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
from github_rest import GitHubREST  # noqa: E402


class _Resp:
    def __init__(self, body): self._body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._body


def _raise(exc):
    raise exc


def _http(code, headers=None):
    return lambda: _raise(urllib.error.HTTPError("https://api.github.com/x", code, "err", headers or {}, io.BytesIO(b"")))


def _urlerr():
    return _raise(urllib.error.URLError("connection reset"))


OK = _Resp(b'{"ok": 1}')


def client_with(script):
    """A GitHubREST whose _urlopen plays `script` (each step a 0-arg callable → _Resp or raises); _sleep records."""
    c = GitHubREST("app", "key", "1")
    c._token = "tok"
    c._token_exp = time.time() + 3600     # a valid cached token so _api/_itoken reuse it (no remint in these tests)
    waits, calls = [], {"n": 0}

    def _urlopen(req):
        i = calls["n"]; calls["n"] += 1
        return script[min(i, len(script) - 1)]()
    c._urlopen = _urlopen
    c._sleep = lambda s: waits.append(s)
    return c, waits, calls


def main() -> int:
    checks = []

    # 1) transient 503 then 200 → retried once, succeeds
    c, waits, calls = client_with([_http(503), lambda: OK])
    r = c._req("GET", "/x", "tok")
    checks.append((f"503 then 200 → retried and succeeded (calls={calls['n']}, waits={len(waits)})",
                   r == {"ok": 1} and calls["n"] == 2 and len(waits) == 1))

    # 2) 403 secondary limit with Retry-After:2 then 200 → waits 2s (mocked), succeeds
    c, waits, calls = client_with([_http(403, {"Retry-After": "2"}), lambda: OK])
    r = c._req("GET", "/x", "tok")
    checks.append((f"403 Retry-After:2 then 200 → waited exactly the Retry-After (waits={waits})",
                   r == {"ok": 1} and waits == [2.0]))

    # 3) 403 primary limit (remaining 0 + reset ~5s out) then 200 → waits ~to reset, succeeds
    reset = str(int(time.time()) + 5)
    c, waits, calls = client_with([_http(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}), lambda: OK])
    r = c._req("GET", "/x", "tok")
    checks.append((f"403 primary limit (remaining 0) then 200 → waited toward X-RateLimit-Reset (waits={waits})",
                   r == {"ok": 1} and len(waits) == 1 and 0 < waits[0] <= 6))

    # 4) 429 with Retry-After then 200 → retried
    c, waits, calls = client_with([_http(429, {"Retry-After": "1"}), lambda: OK])
    r = c._req("GET", "/x", "tok")
    checks.append((f"429 Retry-After then 200 → retried (waits={waits})", r == {"ok": 1} and waits == [1.0]))

    # 5) persistent 503 → exhausts retries, re-raises (webhook fails cleanly → GitHub redelivers, idempotently)
    c, waits, calls = client_with([_http(503)])
    raised = False
    try:
        c._req("GET", "/x", "tok")
    except urllib.error.HTTPError as e:
        raised = (e.code == 503)
    checks.append((f"persistent 503 → re-raises after MAX_RETRIES (attempts={calls['n']}, waits={len(waits)})",
                   raised and calls["n"] == GitHubREST.MAX_RETRIES + 1 and len(waits) == GitHubREST.MAX_RETRIES))

    # 6) 404 → immediate re-raise, NO retry (so get_file_at can map it to None)
    c, waits, calls = client_with([_http(404)])
    raised404 = False
    try:
        c._req("GET", "/x", "tok")
    except urllib.error.HTTPError as e:
        raised404 = (e.code == 404)
    checks.append((f"404 → immediate re-raise, no retry (attempts={calls['n']}, waits={len(waits)})",
                   raised404 and calls["n"] == 1 and len(waits) == 0))

    # 7) plain 403 (no rate-limit headers — e.g. a permissions denial) → no retry, re-raise
    c, waits, calls = client_with([_http(403, {})])
    raised403 = False
    try:
        c._req("GET", "/x", "tok")
    except urllib.error.HTTPError as e:
        raised403 = (e.code == 403)
    checks.append((f"plain 403 (no rate-limit headers) → immediate re-raise (attempts={calls['n']})",
                   raised403 and calls["n"] == 1 and len(waits) == 0))

    # 8) get_file_at on 404 → returns None (the resilience wrapper preserves the existing semantics)
    c, waits, calls = client_with([_http(404)])
    val = c.get_file_at("o/r", "a/x.py", "deadbeef")
    checks.append((f"get_file_at on 404 returns None, no retry (got {val!r}, attempts={calls['n']})",
                   val is None and calls["n"] == 1))

    # 9) transient network URLError then 200 → retried
    c, waits, calls = client_with([lambda: _urlerr(), lambda: OK])
    r = c._req("GET", "/x", "tok")
    checks.append((f"URLError then 200 → retried (calls={calls['n']}, waits={len(waits)})",
                   r == {"ok": 1} and calls["n"] == 2 and len(waits) == 1))

    # ── APP-JWT WINDOW (the most common real-world App-JWT failure): GitHub rejects an App JWT whose iat→exp
    #    span exceeds 600s (10 min). If we mint it right up to the 600s ceiling, ANY positive clock skew on our
    #    host pushes exp past the limit → EVERY installation-token mint 401s → the App silently posts no
    #    checks/comments (the 401-retry just re-mints the same too-far JWT). So the span MUST stay under 600s
    #    with skew headroom (mirrors TOKEN_SKEW_SECONDS on the installation token). Mint a REAL JWT and read back
    #    its claims. (If PyJWT/cryptography aren't installed — offline-only env — this self-checks as a no-op.) ──
    try:
        import jwt as _pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
        from cryptography.hazmat.primitives import serialization as _ser
        _key_pem = _rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            _ser.Encoding.PEM, _ser.PrivateFormat.PKCS8, _ser.NoEncryption()).decode()
        _jc = GitHubREST("app-id", _key_pem, "1")
        _claims = _pyjwt.decode(_jc._jwt(), options={"verify_signature": False})
        _span = _claims["exp"] - _claims["iat"]
        _headroom = 600 - _span                       # how far the iat→exp span sits UNDER GitHub's 600s ceiling
        checks.append((f"app-jwt: iat→exp span ≤ 540s with skew headroom under GitHub's 600s ceiling "
                       f"(span={_span}s, headroom={_headroom}s, iss={_claims.get('iss')})",
                       _span <= 540 and _headroom >= 60 and _claims.get("iss") == "app-id"
                       and _claims["iat"] < _claims["exp"]))
    except ImportError:
        checks.append(("app-jwt: window check skipped (PyJWT/cryptography not installed — live-only deps)", True))

    # ── INSTALLATION TOKEN REFRESH (a 1h-expiry token cached forever would 401 on every call after an hour) ──
    def _iso(delta_seconds):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delta_seconds))

    def token_client(expires_at_seq, data=lambda dc: OK):
        """A client whose App-JWT is stubbed and whose /access_tokens endpoint returns successive tokens with
        the given expires_at deltas; data calls go through `data(call_index)`. Counts mints + data calls."""
        c = GitHubREST("app", "key", "1")
        c._jwt = lambda: "fake-jwt"          # stub: no PyJWT / private key needed
        c._sleep = lambda s: None
        mints, dcalls = {"n": 0}, {"n": 0}

        def _urlopen(req):
            if req.full_url.endswith("/access_tokens"):
                i = mints["n"]; mints["n"] += 1
                exp = expires_at_seq[min(i, len(expires_at_seq) - 1)]
                return _Resp(json.dumps({"token": f"tok{i}", "expires_at": _iso(exp)}).encode())
            dcalls["n"] += 1
            return data(dcalls["n"])
        c._urlopen = _urlopen
        return c, mints, dcalls

    # 10) a FRESH token (expiry in the future) is reused across calls — minted once
    c, mints, dcalls = token_client([3600])
    c._api("GET", "/x"); c._api("GET", "/y")
    checks.append((f"token: a fresh token is reused across calls — minted once (mints={mints['n']})", mints["n"] == 1))

    # 11) an EXPIRED token is reminted before the next call — two mints (the 1h-death fix)
    c, mints, dcalls = token_client([-60, 3600])   # first mint already expired, second fresh
    c._api("GET", "/x"); c._api("GET", "/y")
    checks.append((f"token: an expired token is reminted before the next call (mints={mints['n']})", mints["n"] == 2))

    # 12) a 401 on a data call → invalidate + remint + retry ONCE → succeeds (early-revocation safety net)
    def _data_401_once(dc):
        if dc == 1:
            raise urllib.error.HTTPError("https://api.github.com/x", 401, "unauth", {}, io.BytesIO(b""))
        return OK
    c, mints, dcalls = token_client([3600, 3600], data=_data_401_once)
    r = c._api("GET", "/x")
    checks.append((f"token: a 401 on a data call → invalidate+remint+retry once → succeeds "
                   f"(result={r}, mints={mints['n']}, data_calls={dcalls['n']})",
                   r == {"ok": 1} and mints["n"] == 2 and dcalls["n"] == 2))

    # ── PER-INSTALLATION client caching (multi-tenant: don't re-mint a token every event per installation) ──
    base = GitHubREST("app", "key", "100")            # configured installation id = 100
    c1, c1b, c2 = base.for_installation("200"), base.for_installation("200"), base.for_installation("300")
    checks.append(("for_installation: same id returns the SAME cached instance (not re-created per event)",
                   c1 is c1b))
    checks.append(("for_installation: the configured id returns self", base.for_installation("100") is base))
    checks.append(("for_installation: different installations get different cached instances",
                   c1 is not c2 and c1.installation_id == "200" and c2.installation_id == "300"))

    # a cached per-installation client RETAINS its token across events → mints ONCE (the multi-tenant win)
    imints = {"n": 0}
    c1._jwt = lambda: "fake-jwt"
    c1._sleep = lambda s: None

    def _c1_urlopen(req):
        if req.full_url.endswith("/access_tokens"):
            imints["n"] += 1
            return _Resp(json.dumps({"token": f"t{imints['n']}", "expires_at": _iso(3600)}).encode())
        return OK
    c1._urlopen = _c1_urlopen
    c1._api("GET", "/x"); c1._api("GET", "/y")
    checks.append((f"for_installation: a cached client reuses its token across events — minted once (mints={imints['n']})",
                   imints["n"] == 1))

    # ── installation_repos (the boot self-heal's repo source): paginates + honors the cap ────────────────
    c, _w, _ca = client_with([lambda: _Resp(json.dumps(
        {"repositories": [{"id": 101, "full_name": "o/a"},
                          {"id": 102, "full_name": "o/b"}]}).encode())])
    checks.append(("installation_repo_entries: preserves stable ids for lifecycle authority",
                   c.installation_repo_entries(cap=200) == [
                       {"id": 101, "full_name": "o/a"}, {"id": 102, "full_name": "o/b"}]))
    c, _w, _ca = client_with([lambda: _Resp(json.dumps(
        {"repositories": [{"id": 101, "full_name": "o/a"},
                          {"id": 102, "full_name": "o/b"}]}).encode())])
    checks.append(("installation_repos: returns repo full_names", c.installation_repos(cap=200) == ["o/a", "o/b"]))
    c, _w, _ca = client_with([lambda: _Resp(json.dumps(
        {"repositories": [{"id": 101, "full_name": "o/a"},
                          {"id": 102, "full_name": "o/b"}]}).encode())])
    checks.append(("installation_repos: honors the cap (a huge install can't make boot hammer the API)",
                   c.installation_repos(cap=1) == ["o/a"]))

    # A legacy repository-deletion payload omitted the stable id. Resolve it with one bounded installation-token
    # metadata read: 200 identifies a current visible object, a real 404 means absence, while permission failures
    # remain errors so lifecycle processing retries instead of misclassifying them as deletion.
    c, waits, calls = client_with([
        lambda: _Resp(json.dumps({"id": 303, "full_name": "o/current", "owner": {"id": 404}}).encode()),
    ])
    identity = c.repo_current_identity("o/current")
    checks.append(("repo_current_identity: 200 returns bounded full_name/id/owner metadata",
                   identity == {"id": 303, "full_name": "o/current", "owner_id": 404}
                   and calls["n"] == 1 and waits == []))

    c, waits, calls = client_with([_http(404)])
    identity = c.repo_current_identity("o/gone")
    checks.append(("repo_current_identity: 404 is authoritative absence without retry",
                   identity is None and calls["n"] == 1 and waits == []))

    c, waits, calls = client_with([_http(403, {})])
    identity_403 = False
    try:
        c.repo_current_identity("o/forbidden")
    except urllib.error.HTTPError as exc:
        identity_403 = exc.code == 403
    checks.append(("repo_current_identity: permission failure re-raises instead of becoming absence",
                   identity_403 and calls["n"] == 1 and waits == []))

    # Private rapid A→B→C: B's token 404s, but App JWT can resolve C's installation without reading the repo
    # body; C's scoped token then supplies the bounded identity. App-level 404 remains honest unknown.
    app_client = GitHubREST("app", "key", "200")
    app_calls = []
    scoped_calls = []
    app_client._jwt = lambda: "app-jwt"
    app_client._req = lambda method, path, token: (
        app_calls.append((method, path, token)) or {"id": 300})

    class _ScopedIdentity:
        def repo_current_identity(self, repo):
            scoped_calls.append(repo)
            return {"id": 303, "full_name": "c/private", "owner_id": 505}

    app_client.for_installation = lambda installation_id: (
        _ScopedIdentity() if installation_id == "300" else None)
    app_identity = app_client.repo_current_identity_via_app_installation("b/private")
    checks.append(("private onward identity: App installation lookup switches to C's scoped token",
                   app_identity == {"id": 303, "full_name": "c/private", "owner_id": 505}
                   and app_calls == [("GET", "/repos/b/private/installation", "app-jwt")]
                   and scoped_calls == ["b/private"]))

    app_absent = GitHubREST("app", "key", "200")
    app_absent._jwt = lambda: "app-jwt"
    app_absent._req = lambda *_args: _raise(urllib.error.HTTPError(
        "https://api.github.com/x", 404, "missing", {}, io.BytesIO(b"")))
    checks.append(("private onward identity: App-level 404 preserves non-destructive unknown",
                   app_absent.repo_current_identity_via_app_installation("b/gone") is None))

    # ── boot_reconcile (the restart self-heal): bounded by cap, and NEVER fatal (defers to live webhooks) ──
    sys.path.insert(0, ROOT)
    import server as S
    # The graph-ingestion + reconciliation cluster (_full_ingest / ingest_push / backfill_open_prs /
    # boot_reconcile / self_heal_main_graph + their private caps) lives in ingest.py; server re-exports the
    # names. The intra-cluster calls (boot_reconcile→backfill_open_prs, ingest_push→_full_ingest) dispatch
    # through ingest.py's GLOBALS, so a monkeypatch must rebind the symbol WHERE IT LIVES — on `ingest`, not on
    # the server re-export (rebinding S.<name> would leave ingest's own global, and thus the intra-cluster call,
    # pointing at the original). These patch the SAME behavior the test always asserted, just at its new home.
    import ingest

    class _FakeGH:
        def __init__(self, repos): self._repos = repos
        def installation_repos(self, cap=200): return self._repos[:cap]

    def _boom(*a, **k): raise RuntimeError("boom")

    os.environ.pop("VERIPSA_BACKFILL_REPOS", None)   # exercise the auto-discover (installation_repos) path
    # PATCH ON ingest (where boot_reconcile resolves backfill_open_prs) — NOT S — so the intra-cluster call sees it.
    _orig_bf, seen = ingest.backfill_open_prs, []
    ingest.backfill_open_prs = lambda db, gh, repo: (seen.append(repo), {"backfilled": repo})[1]
    try:
        res = S.boot_reconcile(db=None, gh=_FakeGH(["o/a", "o/b", "o/c"]), cap=2)
        checks.append((f"boot_reconcile: bounded by cap — reconciles 2 of 3 (seen={seen})",
                       seen == ["o/a", "o/b"] and res["reconciled"] == 2))
        ingest.backfill_open_prs = _boom
        res2 = S.boot_reconcile(db=None, gh=_FakeGH(["o/a"]), cap=5)
        checks.append(("boot_reconcile: a repo's failure is non-fatal (sweep still completes)",
                       res2["reconciled"] == 0 and res2["repos"] == 1))

        class _BadGH:
            def installation_repos(self, cap=200): raise RuntimeError("api down")
        res3 = S.boot_reconcile(db=None, gh=_BadGH(), cap=5)
        checks.append(("boot_reconcile: a listing failure is non-fatal (defers to live webhooks)",
                       res3["reconciled"] == 0 and "error" in res3))

        # ── PER-REPO ISOLATION (the real bug): ONE repo that raises MID-SWEEP (since-deleted/renamed,
        #    a GitHub 404/410) must be caught, COUNTED, and SKIPPED — the repos AFTER it still reconcile.
        #    Before the fix, a mid-loop raise would strand every later repo (no check until its next push).
        seen2 = []
        def _one_bad(db, gh, repo):
            seen2.append(repo)
            if repo == "o/gone":        # the since-deleted repo in the MIDDLE of the sweep
                raise RuntimeError("404 Not Found")   # content-free: never carries a secret/file body
            return {"backfilled": repo}
        # PATCH ON ingest: boot_reconcile→_reconcile_one_repo resolves backfill_open_prs through ingest's global
        # (dsn=None here → _reconcile_one_repo calls backfill_open_prs directly), so the stub must live on ingest.
        ingest.backfill_open_prs = _one_bad
        res4 = S.boot_reconcile(db=None, gh=_FakeGH(["o/a", "o/gone", "o/b"]), cap=5)
        checks.append((f"boot_reconcile: one failing repo in the middle does NOT strand the rest (seen={seen2})",
                       seen2 == ["o/a", "o/gone", "o/b"]            # the sweep reached EVERY repo, incl. after the bad one
                       and res4["reconciled"] == 2                  # o/a + o/b reconciled
                       and res4["failed"] == 1                      # o/gone caught + counted (not silently dropped)
                       and res4["repos"] == 3))

        # ── PER-WAKE WALL-CLOCK BUDGET (defense-in-depth on top of cap + the skip-if-recent throttle):
        #    `deadline_seconds=0` (the default) → no deadline check, every repo is touched (behaviour-preserving).
        #    `deadline_seconds>0` → break BETWEEN repos once elapsed >= budget; remaining repos are reported
        #    `deferred` (counted, content-free, same safety-net contract as cap-truncation — they pick up live
        #    webhooks). A repo already mid-reconcile is NEVER preempted (the loop only checks BEFORE each repo).
        seen3 = []
        def _slow(db, gh, repo):
            seen3.append(repo)
            time.sleep(0.06)                                  # each repo "takes" 60ms to reconcile
            return {"backfilled": repo}
        ingest.backfill_open_prs = _slow
        # Budget = 100ms, 5 repos × 60ms each = ~300ms total → the sweep must stop EARLY with deferred>0.
        # The first repo is processed (the deadline is checked at the START of each iteration, so iter-0 always
        # runs; behaviour-preserving for tiny work). At iter-1 we've burned ~60ms; iter-2 we've burned ~120ms >
        # 100ms → break; remaining=3 reported deferred.
        res5 = S.boot_reconcile(db=None, gh=_FakeGH(["o/1", "o/2", "o/3", "o/4", "o/5"]), cap=10,
                                deadline_seconds=1)            # use a generous budget so timing is non-flaky
        checks.append((f"boot_reconcile: deadline_seconds default-0/large is behaviour-preserving (reconciled={res5.get('reconciled')}, deferred={res5.get('deferred')})",
                       res5["reconciled"] == 5 and res5.get("deferred", 0) == 0))
        # Now a TIGHT budget — fewer than the count of repos × per-repo-cost must be deferred.
        seen3.clear()
        res6 = S.boot_reconcile(db=None, gh=_FakeGH(["o/1", "o/2", "o/3", "o/4", "o/5"]), cap=10,
                                deadline_seconds=0)            # 0 still means unlimited (a knob OFF)
        checks.append((f"boot_reconcile: deadline_seconds=0 → unlimited (every repo touched, reconciled={res6.get('reconciled')})",
                       res6["reconciled"] == 5 and res6.get("deferred", 0) == 0))
        # The TIGHT-budget assertion: with a 0.05s budget + 0.06s per repo, the sweep MUST stop before all 5 are done.
        seen3.clear()
        ingest.backfill_open_prs = _slow
        res7 = S.boot_reconcile(db=None, gh=_FakeGH(["o/1", "o/2", "o/3", "o/4", "o/5"]), cap=10,
                                deadline_seconds=1)            # this is the upper bound; will not actually deadline-cut
        # Tight-budget invariant test: monkeypatch time.monotonic to drive the deadline deterministically (no sleep
        # flake). A monotonic clock that jumps 1000s on the SECOND read (i.e. AFTER iter-0 has been kicked off, BEFORE
        # iter-1's deadline check) makes the loop stop with reconciled=1, deferred=4 — content-free, repeatable.
        seen3.clear()
        ingest.backfill_open_prs = lambda db, gh, repo: (seen3.append(repo), {"backfilled": repo})[1]
        ticks = [0.0, 0.0, 9999.0, 9999.0, 9999.0, 9999.0]    # iter-0 sees t=0, iter-1+ sees t>budget → break
        _orig_mono = time.monotonic
        ingest.time.monotonic = lambda: ticks.pop(0) if ticks else 9999.0
        try:
            res8 = S.boot_reconcile(db=None, gh=_FakeGH(["o/1", "o/2", "o/3", "o/4", "o/5"]), cap=10,
                                    deadline_seconds=10)
        finally:
            ingest.time.monotonic = _orig_mono
        checks.append((f"boot_reconcile: deadline_seconds>0 stops BETWEEN repos (reconciled={res8.get('reconciled')}, deferred={res8.get('deferred')}, seen={seen3})",
                       res8["reconciled"] == 1                  # only iter-0 ran before the deadline tripped
                       and res8.get("deferred") == 4            # the other 4 are reported deferred (content-free)
                       and seen3 == ["o/1"]                     # no repo was reconciled past the deadline
                       and res8["repos"] == 5))                 # repos enumerated (the cap honored, work was built)
    finally:
        ingest.backfill_open_prs = _orig_bf

    # ── FORCE-PUSH COALESCING: a storm of pushes to the SAME (repo,branch) collapses to ONE re-ingest (the
    #    latest, FULL), while every push still records its facts. Decision logic + ingest_push integration. ──
    def _push(sha):
        return {"repository": {"full_name": "o/r"}, "ref": "refs/heads/main", "after": sha}
    eqc = S.EventQueue(db=None, gh=None, process=lambda *a, **k: None, branch_from_ref=S._branch_from_ref, maxsize=8)
    for s in ("a" * 40, "b" * 40, "c" * 40):
        eqc._register_push(_push(s))                       # registry's latest for (o/r,main) is now c..c
    d1 = eqc._push_coalesce("o/r", "main", "a" * 40)       # older → superseded
    d2 = eqc._push_coalesce("o/r", "main", "b" * 40)       # older → superseded
    d3 = eqc._push_coalesce("o/r", "main", "c" * 40)       # the latest → must FULL-reingest (stood in for skipped)
    checks.append(("coalesce: older pushes in a same-branch storm are SKIPPED", d1 == "skip" and d2 == "skip"))
    checks.append(("coalesce: the latest push of the storm does a FULL re-ingest", d3 == "full"))
    eqc2 = S.EventQueue(db=None, gh=None, process=lambda *a, **k: None, branch_from_ref=S._branch_from_ref, maxsize=8)
    eqc2._register_push(_push("d" * 40))
    checks.append(("coalesce: a lone push is 'normal' (the incremental fast path is preserved)",
                   eqc2._push_coalesce("o/r", "main", "d" * 40) == "normal"))
    checks.append(("coalesce: a bare 4-arg process does NOT receive the coalesce kwarg (backward compatible)",
                   S.EventQueue(db=None, gh=None, process=lambda et, pl, d, g: None)._coalesce_supported is False))
    checks.append(("coalesce: handle_event (accepts coalesce) is detected as supported",
                   S.EventQueue(db=None, gh=None, process=S.handle_event)._coalesce_supported is True))
    # If the latest push of a coalesced storm decides "full" and then the processor fails, retrying must reuse
    # that same destructive decision. Recomputing would consume/discard "full" on attempt 1 and downgrade attempt
    # 2 to "normal", leaving the graph stale while the delivery looks processed.
    retry_decisions = []
    retry_attempts = {"b": 0}
    def _coalesce_retry_proc(et, pl, db, gh, coalesce=None):
        sha = pl["after"]
        decision = coalesce("o/r", "main", sha)
        retry_decisions.append((sha[0], decision))
        if sha.startswith("b") and retry_attempts["b"] == 0:
            retry_attempts["b"] += 1
            raise RuntimeError("transient full failure")
    eqr = S.EventQueue(db=None, gh=None, process=_coalesce_retry_proc,
                       branch_from_ref=S._branch_from_ref, maxsize=8,
                       retry_attempts=2, retry_base_seconds=0)
    eqr.submit("push", _push("a" * 40))
    eqr.submit("push", _push("b" * 40))
    eqr.start()
    eqr.wait_idle(2.0)
    checks.append((f"coalesce retry: the latest push keeps its FULL decision across a transient failure "
                   f"(decisions={retry_decisions}, retried={eqr.retried()}, failed={eqr.failed()})",
                   retry_decisions == [("a", "skip"), ("b", "full"), ("b", "full")]
                   and eqr.retried() == 1 and eqr.failed() == 0))
    # ingest_push honors the decision: 'skip' records the push fact but does NOT re-ingest; 'full' re-ingests.
    _calls = {"record": 0, "full": 0}
    def _fakedb(sql, args=()):
        if "record_push" in sql:
            _calls["record"] += 1
        return None
    # PATCH ON ingest (where ingest_push resolves _full_ingest) — NOT S — so the intra-cluster call sees the stub.
    _orig_full = ingest._full_ingest
    ingest._full_ingest = lambda *a, **k: (_calls.__setitem__("full", _calls["full"] + 1), {"mode": "full", "files": 0, "edges": 0})[1]
    try:
        out = S.ingest_push(_fakedb, object(), "o/r", "main", "e" * 40, {"after": "e" * 40, "commits": []},
                            coalesce=lambda r, b, s: "skip")
        checks.append(("coalesce: a SKIP records the push fact but skips the expensive re-ingest",
                       out.get("coalesced") is True and _calls["record"] == 1 and _calls["full"] == 0))
        S.ingest_push(_fakedb, object(), "o/r", "main", "f" * 40, {"after": "f" * 40, "commits": []},
                      coalesce=lambda r, b, s: "full")
        checks.append(("coalesce: a FULL re-ingests AND records facts (never a stale graph)",
                       _calls["full"] == 1 and _calls["record"] == 2))
    finally:
        ingest._full_ingest = _orig_full

    # ── FORCE-PUSH REPLICATION-LAG RACE: the head sha isn't fetchable YET (a 404/410 on the tarball, the
    #    classic moment-after-a-force-push state). The full re-ingest must NOT crash the delivery — it defers
    #    as a content-free no-op (facts already recorded above; the prior graph is left untouched, never a
    #    silent stale graph from a crashed half-build). ANY non-404 error must still re-raise (we never swallow
    #    a real ingest bug behind 'deferred'). This exercises the REAL _full_ingest → gh.download_tarball path
    #    (not the monkeypatched stand-in), so the 404 propagates exactly as the live client would raise it. ──
    class _LagGH:                                          # download_tarball 404s like GitHub before the sha replicates
        def __init__(self, code): self._code = code
        def download_tarball(self, repo, sha):
            raise urllib.error.HTTPError(f"https://api.github.com/repos/{repo}/tarball/{sha}",
                                         self._code, "Not Found", {}, io.BytesIO(b""))
    _facts = {"record": 0}
    def _factdb(sql, args=()):
        if "record_push" in sql:
            _facts["record"] += 1
        if "coordinate_file_paths" in sql:                 # no baseline graph → forces the full-ingest path
            return []
        return None
    # forced=True (a real force-push) → straight to the full re-ingest, which hits the not-yet-fetchable 404.
    fp = {"after": "a" * 40, "forced": True, "commits": []}
    out404 = S.ingest_push(_factdb, _LagGH(404), "o/r", "main", "a" * 40, fp, coalesce=None)
    checks.append(("force-push 404 (sha not fetchable yet) → ingest DEFERRED, does NOT raise",
                   out404.get("deferred") == "sha_not_yet_fetchable" and out404.get("reingested") is False))
    checks.append(("force-push 404 → push facts STILL recorded (audit/landing intact, never lost)",
                   _facts["record"] == 1))
    out410 = S.ingest_push(_factdb, _LagGH(410), "o/r", "main", "b" * 40,
                           {"after": "b" * 40, "forced": True, "commits": []}, coalesce=None)
    checks.append(("force-push 410 (gone) also defers cleanly (transient not-found family)",
                   out410.get("deferred") == "sha_not_yet_fetchable"))
    # a NON-404 download error (e.g. 500) is NOT the deferrable race → it must re-raise (delivery retries/fails).
    raised500 = False
    try:
        S.ingest_push(_factdb, _LagGH(500), "o/r", "main", "c" * 40,
                      {"after": "c" * 40, "forced": True, "commits": []}, coalesce=None)
    except urllib.error.HTTPError as e:
        raised500 = (e.code == 500)
    checks.append(("non-404 ingest error (500) is NOT swallowed — it re-raises honestly", raised500))
    checks.append(("_is_sha_not_yet_fetchable: 404/410 defer, 500/missing-code re-raise (fail closed)",
                   S._is_sha_not_yet_fetchable(urllib.error.HTTPError("u", 404, "x", {}, io.BytesIO(b"")))
                   and S._is_sha_not_yet_fetchable(urllib.error.HTTPError("u", 410, "x", {}, io.BytesIO(b"")))
                   and not S._is_sha_not_yet_fetchable(urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b"")))
                   and not S._is_sha_not_yet_fetchable(RuntimeError("no code attr"))))

    # ── CONCURRENCY: the live server runs the HTTP submit thread, ONE daemon worker, the watchdog, and the
    #    boot-reconcile concurrently over shared mutable state. These checks pin the two contracts that a race
    #    would break: (a) the coalesce VISIBILITY invariant — a push's own sha is in _latest_push BEFORE its
    #    queue item is dequeuable, so the worker can never decide 'skip' against a latest missing that push (the
    #    newest-of-a-burst stale-graph race); (b) under an N-thread submit+drain hammer the queue loses/doubles
    #    NO task and the processed counter is exactly consistent. ──────────────────────────────────────────────
    import threading as _th

    def _pp(sha):
        return {"repository": {"full_name": "o/r"}, "ref": "refs/heads/main", "after": sha}

    # (a) REGRESSION for the register-after-enqueue race: register MUST land before the item is visible. We
    #     model the worker by reading _latest_push the instant submit() returns — it must already reflect THIS
    #     push (so a same-instant _push_coalesce of the newest push returns 'normal'/'full', never a stale 'skip').
    eqv = S.EventQueue(db=None, gh=None, process=lambda *a, **k: None,
                       branch_from_ref=S._branch_from_ref, maxsize=100)
    eqv._register_push(_pp("a" * 40))                     # an earlier push A was the prior latest
    eqv.submit("push", _pp("b" * 40))                     # newest push B; nothing queued behind it
    checks.append(("concurrency: a push's sha is registered BEFORE its item is dequeuable (no stale-latest skip)",
                   eqv._latest_push.get(("o/r", "main")) == "b" * 40
                   and eqv._push_coalesce("o/r", "main", "b" * 40) == "normal"))

    # (b) a rejected (queue.Full → 503) push must NOT leave a phantom 'latest' — it never entered the queue, so
    #     GitHub will redeliver it; a phantom latest would make an EARLIER queued push wrongly coalesce against it.
    eqf = S.EventQueue(db=None, gh=None, process=lambda *a, **k: None,
                       branch_from_ref=S._branch_from_ref, maxsize=1)
    eqf.submit("push", _pp("a" * 40))                      # fills the single global slot (latest = A)
    rejected = eqf.submit("push", _pp("b" * 40))           # queue full → 503; B must roll back
    checks.append(("concurrency: a 503'd push rolls back its registry entry (no phantom latest)",
                   rejected is False and eqf._latest_push.get(("o/r", "main")) == "a" * 40))

    # (c) N-THREAD HAMMER: many HTTP threads submit while the single worker drains. Assert the worker processed
    #     EXACTLY the accepted count (no lost task, no double-process), unfinished drains to 0, and the
    #     processed counter == len(unique items seen) (no torn counter / lost task_done).
    seen, seen_lock = [], _th.Lock()
    def _proc(et, pl, db, gh, **k):
        with seen_lock:
            seen.append(pl["i"])
    hammer = S.EventQueue(db=None, gh=None, process=_proc, account_of=S._event_account_key,
                          maxsize=100000, per_account_cap=100000).start()
    N_THREADS, PER = 12, 400
    accepted = [0] * N_THREADS
    start = _th.Event()
    def _flood(tid):
        start.wait()
        acc = 0
        for j in range(PER):
            # spread across a few accounts so the round-robin drain + per-account buckets are exercised
            pl = {"i": tid * PER + j,
                  "repository": {"owner": {"id": tid % 3}, "full_name": f"o{tid%3}/r"}}
            if hammer.submit("pull_request", pl):
                acc += 1
        accepted[tid] = acc
    threads = [_th.Thread(target=_flood, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()
    drained = hammer.wait_idle(timeout=10.0)
    total_accepted = sum(accepted)
    checks.append((f"concurrency: N-thread hammer drains fully — no lost/double task "
                   f"(accepted={total_accepted}, processed={hammer.processed()}, seen={len(seen)}, "
                   f"unique={len(set(seen))})",
                   drained and total_accepted == N_THREADS * PER
                   and hammer.processed() == total_accepted
                   and len(seen) == total_accepted and len(set(seen)) == total_accepted
                   and hammer.failed() == 0 and hammer.qsize() == 0))

    okall = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        okall = okall and bool(cond)
    print("GITHUB RESILIENCE GATE:", "PASS" if okall else "FAIL")
    return 0 if okall else 1


if __name__ == "__main__":
    sys.exit(main())
