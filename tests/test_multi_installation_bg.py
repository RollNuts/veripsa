#!/usr/bin/env python3
"""MULTI-INSTALLATION BACKGROUND-LOOP gate — the App's BACKGROUND loops (graph-freshness self-heal +
boot_reconcile) must serve EVERY installation, not just the hardcoded primary.

THE BUG THIS LOCKS: the webhook worker is already multi-installation-correct — it calls
gh.for_installation(payload.installation.id) per event. But the BACKGROUND loops used the SINGLETON client
(pinned to the PRIMARY installation / GH_INSTALLATION_ID). So for a coordinate/repo in a NON-PRIMARY
installation (a 2nd synthetic org) every background GitHub call ran on the PRIMARY's token and 404/403'd — that
tenant's graph never self-healed
(went stale forever) and the loops spammed errors. True multi-tenancy was blocked: a 2nd customer's repos were
unserviced by every background path.

THE FIX (Fix A): every background loop that iterates repos/coordinates resolves the installation PER coordinate
and calls GitHub through gh.for_installation(<that installation id>) — exactly как the webhook worker does:
  • graph_freshness_all: a coordinate carries account_id → gh.for_account(account_id) (→ for_installation under
    the hood, account→installation resolved ONCE from GET /app/installations) → resolve HEAD through THAT client.
    An account with no installation is SKIPPED content-free (behind=None, no 404-spam).
  • boot_reconcile: ENUMERATE every installation (gh.list_app_installations) and reconcile each installation's
    repos through ITS OWN client (gh.for_installation(iid)).

WHAT THIS GATE PROVES (over the REAL GitHubREST — for_account / for_installation / list_app_installations /
the token-mint path are all exercised for real; only the socket is faked):
  (A1) graph_freshness_all: a NON-PRIMARY coordinate's HEAD resolve is authed by the NON-PRIMARY installation's
       token (minted at POST /app/installations/<secondary>/access_tokens), NOT the primary's — so it does NOT
       404; the primary coordinate still uses the primary token; an UNRESOLVABLE-account coordinate is skipped
       content-free (behind=None, no wrong-token call).
  (A2) boot_reconcile: it enumerates BOTH installations and reconciles the non-primary repo through the
       non-primary installation's client (the per-repo backfill ran under the secondary token).
  (A3) the singleton's primary behaviour is UNCHANGED (the primary coordinate/repo resolves exactly as before).

No DB, no network: a fake _urlopen scripts /app/installations + the per-installation token mints + the
repo-metadata reads, recording WHICH installation's token authorized each call. _sleep is a no-op.

Run:  python3 tests/test_multi_installation_bg.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
from github_rest import GitHubREST  # noqa: E402

# Two installations of THIS App: the PRIMARY (== the hardcoded GH_INSTALLATION_ID the singleton is pinned to)
# and a NON-PRIMARY synthetic organization. Each owns one account + one repo.
PRIMARY_IID, PRIMARY_OWNER = "111", "1001"      # installation id, owning-account (owner) id
SECOND_IID, SECOND_OWNER = "222", "2002"
PRIMARY_ACCT = GitHubREST.ACCOUNT_PREFIX + PRIMARY_OWNER     # the Veripsa tenant account_id a coordinate carries
SECOND_ACCT = GitHubREST.ACCOUNT_PREFIX + SECOND_OWNER
ORPHAN_ACCT = GitHubREST.ACCOUNT_PREFIX + "9999"            # an account with NO installation (uninstalled/unknown)
PRIMARY_REPO, SECOND_REPO = "primaryorg/app", "secondaryorg/example-app"
HEAD_PRIMARY, HEAD_SECOND = "a" * 40, "b" * 40


class _Resp:
    def __init__(self, body): self._body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, *a): return self._body


def _scripted_singleton():
    """The REAL GitHubREST pinned to the PRIMARY installation, with a faked socket. Records, per data call, WHICH
    installation's token authorized it (the mint encodes the iid into the token string → the data handler reads
    the Authorization header back to the iid). Also records each token mint's target installation id.

    The socket + JWT seams are stubbed at the CLASS level (restored by the returned teardown) so the SIBLING
    clients for_installation() creates for non-primary installations inherit the SAME fake socket — otherwise a
    sibling would try the real _jwt()/_urlopen and fail. _urlopen/_sleep are staticmethods; _jwt is an instance
    method, so each is replaced with a matching binding."""
    rec = {"mints": [], "head_calls": [], "list_calls": 0}   # mints: [iid]; head_calls: [(repo, iid)]; list_calls: int

    def _iid_from_auth(req):
        auth = req.get_header("Authorization") or ""
        tok = auth.split(" ", 1)[-1]
        return tok[len("itok-for-"):] if tok.startswith("itok-for-") else None

    def _urlopen(req):
        url = req.full_url
        # APP-level: list every installation of this App (account.id ↔ installation id).
        if url.endswith("/app/installations?per_page=100&page=1"):
            rec["list_calls"] += 1
            return _Resp(json.dumps([
                {"id": int(PRIMARY_IID), "account": {"id": int(PRIMARY_OWNER), "login": "primaryorg"}},
                {"id": int(SECOND_IID), "account": {"id": int(SECOND_OWNER), "login": "secondaryorg"}},
            ]).encode())
        if "/app/installations?" in url:                       # any later page → empty (one page only)
            return _Resp(b"[]")
        # mint an installation token — record WHICH installation it is for.
        if url.endswith("/access_tokens"):
            iid = url.split("/app/installations/", 1)[1].split("/access_tokens", 1)[0]
            rec["mints"].append(iid)
            return _Resp(json.dumps({"token": f"itok-for-{iid}",
                                     "expires_at": "2999-01-01T00:00:00Z"}).encode())
        # repo metadata (repo_default_branch_head = GET /repos/{repo} then /repos/{repo}/branches/{b}). Record the
        # repo + the installation whose token authed the call (read back from the Authorization header).
        iid = _iid_from_auth(req)
        if url.endswith(f"/repos/{PRIMARY_REPO}"):
            rec["head_calls"].append((PRIMARY_REPO, iid))
            return _Resp(json.dumps({"default_branch": "main"}).encode())
        if url.endswith(f"/repos/{SECOND_REPO}"):
            rec["head_calls"].append((SECOND_REPO, iid))
            return _Resp(json.dumps({"default_branch": "main"}).encode())
        if f"/repos/{PRIMARY_REPO}/branches/" in url:
            return _Resp(json.dumps({"commit": {"sha": HEAD_PRIMARY}}).encode())
        if f"/repos/{SECOND_REPO}/branches/" in url:
            return _Resp(json.dumps({"commit": {"sha": HEAD_SECOND}}).encode())
        raise AssertionError(f"unexpected urlopen: {url}")

    saved = (GitHubREST._urlopen, GitHubREST._sleep, GitHubREST._jwt)
    GitHubREST._urlopen = staticmethod(_urlopen)              # siblings inherit the fake socket (class-level)
    GitHubREST._sleep = staticmethod(lambda s: None)
    GitHubREST._jwt = lambda self: "APP-JWT"                  # stub: App-level JWT (no PyJWT / real key needed)

    def _teardown():
        GitHubREST._urlopen, GitHubREST._sleep, GitHubREST._jwt = saved

    gh = GitHubREST("app-id", "-----BEGIN KEY-----\nx\n-----END KEY-----", PRIMARY_IID)
    return gh, rec, _teardown


def _singleton_403_on_app_installations():
    """The REAL GitHubREST pinned to the PRIMARY installation, with a faked socket whose GET /app/installations
    RAISES HTTPError 403 — a defense-in-depth failure mode the success-path scripts never cover.

    GET /app/installations is an APP-LEVEL call (App JWT, NOT an installation token), so an unclassified app-level
    403 can make for_account resolve NOTHING: every coordinate's owning installation is unknown, so the freshness
    self-heal goes BLIND across ALL tenants at once ('freshness HEAD-resolve skipped ...: HTTP 403'). The
    contract under test is fail-soft: _rebuild_account_install_map swallows the 403 (returns False, caches NOTHING
    — never a partial/empty map) and for_account returns None, so the caller SKIPS the coordinate content-free —
    no crash, no wrong-token data call, and the NEXT pass retries the map once the failure clears.

    Records: list_attempts (how many times /app/installations was hit — proves no per-coordinate storm) and
    data_calls (any /repos/... call — MUST stay 0: a blind resolver must make NO GitHub data call on the wrong
    token). _urlopen/_sleep are staticmethods, _jwt an instance method — replaced with matching bindings, restored
    by the returned teardown (so sibling clients never reach the real socket)."""
    rec = {"list_attempts": 0, "data_calls": []}

    def _urlopen(req):
        url = req.full_url
        if "/app/installations?" in url:                       # the APP-level enumerate → defense-in-depth 403
            rec["list_attempts"] += 1
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"message":"Forbidden"}'))
        # ANY data call here means the resolver handed back a (wrong) client despite the blind map — it must not.
        rec["data_calls"].append(url)
        raise AssertionError(f"no data call must happen when /app/installations 403s, got: {url}")

    saved = (GitHubREST._urlopen, GitHubREST._sleep, GitHubREST._jwt)
    GitHubREST._urlopen = staticmethod(_urlopen)
    GitHubREST._sleep = staticmethod(lambda s: None)
    GitHubREST._jwt = lambda self: "APP-JWT"

    def _teardown():
        GitHubREST._urlopen, GitHubREST._sleep, GitHubREST._jwt = saved

    gh = GitHubREST("app-id", "-----BEGIN KEY-----\nx\n-----END KEY-----", PRIMARY_IID)
    return gh, rec, _teardown


def main() -> int:
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))

    import graph_freshness as F
    import ingest as I

    # ── (A1) graph_freshness_all resolves HEAD per coordinate through the OWNING installation's token ──────────
    # The cross-tenant freshness surface returns coordinates for BOTH installations + one orphan account. db is a
    # stub that returns that surface (the function's only DB call); the GitHub side is the real for_account path.
    gh, rec, _teardown = _scripted_singleton()

    surface = {"coordinates": [
        {"account_id": PRIMARY_ACCT, "repo": PRIMARY_REPO, "branch": "main", "commit_sha": HEAD_PRIMARY, "age_seconds": 5},
        {"account_id": SECOND_ACCT, "repo": SECOND_REPO, "branch": "main", "commit_sha": "c" * 40, "age_seconds": 9},
        {"account_id": ORPHAN_ACCT, "repo": "ghost/repo", "branch": "main", "commit_sha": "d" * 40, "age_seconds": 1},
    ]}

    def db_fresh(sql, args=()):
        if "owner_graph_freshness_surface" in sql:
            return surface
        return None

    try:
        rows = F.graph_freshness_all(db_fresh, gh)
    finally:
        _teardown()
    by_repo = {r["repo"]: r for r in rows}

    # the NON-PRIMARY repo's HEAD resolve was authed by the SECONDARY installation's token (NOT the primary's) →
    # it resolves HEAD instead of 404-ing. This is the crux: for_account → for_installation(SECOND_IID).
    second_head_authed_by = next((iid for (repo, iid) in rec["head_calls"] if repo == SECOND_REPO), None)
    chk(f"(A1) a NON-PRIMARY coordinate's HEAD resolve is authed by the NON-PRIMARY installation token "
        f"(authed_by={second_head_authed_by!r}, expected {SECOND_IID!r}) — not the primary's (no 404)",
        second_head_authed_by == SECOND_IID
        and by_repo.get(SECOND_REPO, {}).get("head_sha") == HEAD_SECOND
        and by_repo.get(SECOND_REPO, {}).get("behind") is True)     # stored c..c != HEAD b..b → genuine drift seen

    # the PRIMARY coordinate still uses the PRIMARY token (== self; the singleton's own installation).
    primary_head_authed_by = next((iid for (repo, iid) in rec["head_calls"] if repo == PRIMARY_REPO), None)
    chk(f"(A3) the PRIMARY coordinate still resolves on the PRIMARY token (authed_by={primary_head_authed_by!r}) "
        f"— behaviour-preserving for the primary install",
        primary_head_authed_by == PRIMARY_IID
        and by_repo.get(PRIMARY_REPO, {}).get("head_sha") == HEAD_PRIMARY
        and by_repo.get(PRIMARY_REPO, {}).get("behind") is False)   # stored a..a == HEAD a..a → fresh

    # the ORPHAN-account coordinate (no installation) is SKIPPED content-free: behind=None ('unknown'), and NO
    # GitHub HEAD call was made for it (no wrong-token 404-spam).
    orphan = by_repo.get("ghost/repo", {})
    orphan_called = any(repo == "ghost/repo" for (repo, _iid) in rec["head_calls"])
    chk(f"(A1) an UNRESOLVABLE-account coordinate is SKIPPED content-free (behind={orphan.get('behind')}, "
        f"github_called={orphan_called}) — no 404-spam, no crash",
        orphan.get("behind") is None and orphan_called is False)

    # (A4) REGRESSION LOCK (post-deploy fix): a NON-ACCT-GH tenant key (a dogfood/credential coordinate — e.g. the
    # self-hosted core repo, stored under ACCT-DEMO) must resolve to the PRIMARY client (self), NOT None. Returning
    # None made graph_freshness_all SKIP the dogfooded core coordinate as "unservable" (the live regression after the
    # multi-installation deploy); the singleton is the correct client for any non-ACCT-GH key per for_account's contract.
    chk("(A4) a non-ACCT-GH tenant key (ACCT-DEMO/dogfood) resolves to the PRIMARY client (self), not skipped — and "
        "an empty key is still None",
        gh.for_account("ACCT-DEMO") is gh and gh.for_account("") is None)

    # ── (A5) REGRESSION LOCK — defense-in-depth for GET /app/installations 403s ─────────────────────────────────
    # The success-path scripts (A1-A4) all model a HEALTHY /app/installations. After #537, the concrete
    # User-Agent coverage lives in test_github_http_keepalive.py; this gate keeps the generic
    # fail-soft contract for an unclassified app-level 403: for_account returns None for an ACCT-GH key (the caller
    # SKIPS the coordinate content-free) WITHOUT crashing and WITHOUT any wrong-token data call. The map caches
    # NOTHING (stays None), and #534 throttles failed rebuild attempts so the blind resolver does not storm GitHub.
    gh403, rec403, _teardown403 = _singleton_403_on_app_installations()
    crashed = None
    try:
        resolved = gh403.for_account(SECOND_ACCT)             # an ACCT-GH key needs the map → triggers the 403 list
    except Exception as e:                                    # MUST NOT happen: the resolver is never-crash fail-soft
        resolved, crashed = "CRASH", f"{type(e).__name__}: {e}"
    first_stamp = gh403._account_install_map_at
    first_list_attempts = rec403["list_attempts"]
    rapid_crashed = None
    try:
        rapid_primary = gh403.for_account(PRIMARY_ACCT)       # recent failed cold-build stamp → TTL-throttled miss
        rapid_second = gh403.for_account(SECOND_ACCT)         # another ACCT-GH key must NOT re-hit the list
    except Exception as e:
        rapid_primary, rapid_second, rapid_crashed = "CRASH", "CRASH", f"{type(e).__name__}: {e}"
    rapid_list_attempts = rec403["list_attempts"]

    forced_old_stamp = first_stamp - gh403.ACCOUNT_MAP_TTL_SECONDS - 1
    gh403._account_install_map_at = forced_old_stamp
    retry_crashed = None
    try:
        retry_resolved = gh403.for_account(PRIMARY_ACCT)      # TTL expired → exactly one new list attempt, still soft
    except Exception as e:
        retry_resolved, retry_crashed = "CRASH", f"{type(e).__name__}: {e}"
    retry_stamp = gh403._account_install_map_at
    retry_list_attempts = rec403["list_attempts"] - rapid_list_attempts
    # the map must stay UNSET (a transient list error caches NOTHING — never a partial/empty map that would wrongly
    # serve a later account as a permanent miss); a non-ACCT-GH key still resolves to self (the 403 didn't poison it).
    map_unpoisoned = gh403._account_install_map is None
    serves_self_still = gh403.for_account("ACCT-DEMO") is gh403
    _teardown403()
    chk(f"(A5) a 403 on GET /app/installations → for_account returns None fail-soft (resolved={resolved!r}, "
        f"crashed={crashed!r}) — the coordinate is skipped content-free, NO crash",
        resolved is None and crashed is None)
    chk(f"(A5) #534: the first failed install-map rebuild stamps the TTL throttle "
        f"(stamp={first_stamp:.6f}, list_attempts={first_list_attempts}) and rapid ACCT-GH lookups do NOT re-hit "
        f"(rapid_results={[rapid_primary, rapid_second]!r}, rapid_crashed={rapid_crashed!r}, "
        f"list_attempts={rapid_list_attempts})",
        first_stamp > 0 and first_list_attempts == 1 and rapid_primary is None and rapid_second is None
        and rapid_crashed is None and rapid_list_attempts == first_list_attempts)
    chk(f"(A5) #534: after forcing the stamp older than ACCOUNT_MAP_TTL_SECONDS, the next ACCT-GH lookup retries "
        f"once (retry_resolved={retry_resolved!r}, retry_crashed={retry_crashed!r}, retry_list_attempts={retry_list_attempts}, "
        f"retry_stamp={retry_stamp:.6f}, data_calls={rec403['data_calls']})",
        retry_resolved is None and retry_crashed is None and retry_list_attempts == 1 and retry_stamp > first_stamp
        and rec403["data_calls"] == [])
    chk(f"(A5) a 403 on GET /app/installations makes NO wrong-token data call (data_calls={rec403['data_calls']}) "
        f"and caches NOTHING (map still None={map_unpoisoned}); a non-ACCT-GH key still serves self "
        f"({serves_self_still}) — no blind-resolver poisoning",
        rec403["data_calls"] == [] and map_unpoisoned and serves_self_still)
    # NOTE (paired observability follow-up): once the app_jwt_reachable / freshness_blind watchdog alert lands
    # (the observability fix this regression pairs with), extend (A5) to assert that alert FIRES on this 403 path
    # (the operator must SEE a blind freshness loop, not just fail silently). The alert does not exist yet, so the
    # firing assertion is intentionally deferred — this gate locks the fail-soft behaviour that exists today.

    # BOUNDED: each REAL installation token is minted exactly ONCE (the account→installation map is built from ONE
    # GET /app/installations, then cached + reused per coordinate — not a mint-per-coordinate storm). The ORPHAN
    # account never resolves to an installation, so it triggers NO mint (no wasted token creation, no 404 call).
    chk(f"(A1) bounded: each installation token minted exactly ONCE, the orphan NONE (mints={sorted(rec['mints'])}) "
        f"— the account->installation map is built once + cached, never per-coordinate",
        sorted(rec["mints"]) == sorted([PRIMARY_IID, SECOND_IID]) and rec["mints"].count(SECOND_IID) == 1)

    # the account->installation map is built from a SINGLE GET /app/installations for the WHOLE pass (3 coordinates
    # incl. the orphan miss) — the cache + the TTL-throttle prevent a list-per-coordinate (or per-miss) storm.
    chk(f"(A1) bounded: ONE GET /app/installations served all 3 coordinates (list_calls={rec['list_calls']}) — "
        f"cached, never per-coordinate; the orphan miss is throttled (no per-tick storm)",
        rec["list_calls"] == 1)

    # ── (A2) boot_reconcile enumerates BOTH installations + reconciles each repo through its OWN client ────────
    gh2, _rec2, _teardown2 = _scripted_singleton()   # A2 asserts routing via `seen`, not _rec2's call log
    os.environ.pop("VERIPSA_BACKFILL_REPOS", None)   # exercise the per-installation enumerate path

    # Give each installation ITS OWN repo via the identity-preserving installation_repo_entries contract. The
    # stable id must survive the per-installation fan-out too; names alone are insufficient to distinguish a
    # deleted/recreated same-name repository during boot reconciliation.
    primary_repo_id, second_repo_id = 51001, 52002
    repos_by_iid = {
        PRIMARY_IID: [{"full_name": PRIMARY_REPO, "id": primary_repo_id}],
        SECOND_IID: [{"full_name": SECOND_REPO, "id": second_repo_id}],
    }
    _orig_inst_repo_entries = GitHubREST.installation_repo_entries
    GitHubREST.installation_repo_entries = (
        lambda self, cap=200: repos_by_iid.get(str(self.installation_id), [])
    )

    # stub the per-repo reconcile to RECORD which installation_id's client reconciled each repo (the real
    # backfill_open_prs would make live calls; here we capture the routing — the property under test). dsn=None →
    # _reconcile_one_repo calls backfill_open_prs directly with the per-installation client boot_reconcile passed.
    seen = []   # [(repo, installation_id_of_the_client_that_reconciled_it, stable_repository_id)]
    _orig_bf = I.backfill_open_prs
    I.backfill_open_prs = lambda db, ghx, repo, *a, **k: (
        seen.append((repo, str(ghx.installation_id), k.get("repository_id"))),
        {"backfilled": repo},
    )[1]
    try:
        res = I.boot_reconcile(db=None, gh=gh2, cap=50, dsn=None)
    finally:
        I.backfill_open_prs = _orig_bf
        GitHubREST.installation_repo_entries = _orig_inst_repo_entries
        _teardown2()

    seen_map = {repo: (iid, repo_id) for repo, iid, repo_id in seen}
    chk(f"(A2) boot_reconcile enumerated BOTH installations (installations={res.get('installations')}) and "
        f"reconciled {res.get('reconciled')} repos across them (seen={seen})",
        res.get("installations") == 2 and res.get("reconciled") == 2 and res.get("repos") == 2)
    chk(f"(A2) the NON-PRIMARY repo was reconciled through the NON-PRIMARY installation's client "
        f"(routed_to={seen_map.get(SECOND_REPO)!r}, expected {(SECOND_IID, second_repo_id)!r}) — not the "
        f"singleton/primary and not name-only",
        seen_map.get(SECOND_REPO) == (SECOND_IID, second_repo_id))
    chk(f"(A3) the PRIMARY repo was reconciled through the PRIMARY client "
        f"(routed_to={seen_map.get(PRIMARY_REPO)!r}) — behaviour-preserving for the primary install",
        seen_map.get(PRIMARY_REPO) == (PRIMARY_IID, primary_repo_id))

    okall = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        okall = okall and cond
    print("MULTI-INSTALLATION BACKGROUND GATE:", "PASS" if okall else "FAIL")
    return 0 if okall else 1


if __name__ == "__main__":
    sys.exit(main())
