#!/usr/bin/env python3
"""Installation lifecycle point reads must decide before tenant admission or any DB lock."""
from __future__ import annotations

import io
import os
import sys
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import event_processor as EP  # noqa: E402
import delivery_queue as DQ  # noqa: E402
import server_http as SH  # noqa: E402
import webhook_handlers as WH  # noqa: E402
import psycopg2  # noqa: E402
from github_rest_installs import _GitHubInstallationsMixin  # noqa: E402


class DBReached(RuntimeError):
    pass


class GH:
    def __init__(self, *, exact=None, current=None, error=None):
        self.exact = exact
        self.current = current
        self.error = error
        self.account_calls = []

    def app_installation_identity(self, installation_id):
        if self.error:
            raise self.error
        return self.exact

    def app_account_installation_identity(self, account_id):
        self.account_calls.append(account_id)
        if self.error:
            raise self.error
        return self.current


class RestProbe(_GitHubInstallationsMixin):
    def __init__(self, response=None, error=None, responses=None):
        self.response = response
        self.error = error
        self.responses = list(responses) if responses is not None else None
        self.calls = []

    def _jwt(self):
        return "app-jwt"

    def _req(self, method, path, token):
        self.calls.append((method, path, token))
        if self.error:
            raise self.error
        if self.responses is not None:
            if not self.responses:
                raise RuntimeError("unexpected extra installation scan page")
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return self.response


def payload(action):
    return {
        "action": action,
        "_veripsa_delivery_key": f"delivery-{action}",
        "installation": {
            "id": "A-42",
            "account": {"id": "42", "login": "acme", "type": "Organization"},
        },
    }


def identity(installation_id):
    return {
        "installation_id": installation_id,
        "account_id": "42",
        "created_at": "2026-01-01T00:00:00Z",
        "suspended": False,
    }


def raw_identity(installation_id, account_id="42"):
    return {
        "id": installation_id,
        "account": {"id": account_id},
        "created_at": "2026-01-01T00:00:00Z",
        "suspended_at": None,
    }


def main():
    checks = []
    connected = 0
    original_connect = psycopg2.connect

    def refuse_connect(*_args, **_kwargs):
        nonlocal connected
        connected += 1
        raise DBReached("processor reached tenant admission")

    psycopg2.connect = refuse_connect
    try:
        # Numeric test host bypasses the production DNS resolver while keeping
        # this injected psycopg2.connect seam observable.
        proc = EP.make_db_processor("postgresql://127.0.0.1/unused")

        # A missing/suspended activation is a completed stale no-op before activation admission can recreate an
        # erased account. An impossible exact-id/account mismatch is malformed authority and must retry, not go green.
        before = connected
        proc("installation", payload("created"), None, GH(exact=None))
        checks.append(("absent activation returns before DB", connected == before))
        before = connected
        mismatch_raised = False
        try:
            proc("installation", payload("created"), None, GH(exact=identity("other")))
        except RuntimeError as exc:
            mismatch_raised = "mismatched" in str(exc)
        checks.append(("mismatched exact activation authority retries before DB",
                       mismatch_raised and connected == before))
        suspended = identity("A-42")
        suspended["suspended"] = True
        before = connected
        proc("installation", payload("created"), None, GH(exact=suspended))
        checks.append(("suspended activation returns before DB", connected == before))
        invalid_time = identity("A-42")
        invalid_time["created_at"] = "not-a-time"
        before = connected
        invalid_time_raised = False
        try:
            proc("installation", payload("created"), None, GH(exact=invalid_time))
        except RuntimeError as exc:
            invalid_time_raised = "ISO timestamp" in str(exc)
        checks.append(("invalid activation created_at retries before DB",
                       invalid_time_raised and connected == before))

        # A delayed A delete carries replacement B into the DB lifecycle fence. The DB records B under its account
        # lock before returning a stale no-op, so a later real B delete cannot be mistaken for old work.
        replacement_payload = payload("deleted")
        replacement_payload["installation"]["account"]["login"] = "acme-before-rename"
        replacement_gh = GH(current=identity("B-42"))
        before = connected
        replacement_reached = False
        try:
            proc("installation", replacement_payload, None, replacement_gh)
        except DBReached:
            replacement_reached = True
        replacement_marker = replacement_payload.get(WH._DELETE_PROOF_MARKER, {})
        checks.append(("renamed account replacement resolves by stable id and reaches DB with bounded proof",
                       replacement_reached and connected == before + 1
                       and replacement_gh.account_calls == ["42"]
                       and replacement_marker.get("state") == "replacement"
                       and replacement_marker.get("current", {}).get("installation_id") == "B-42"))

        # GitHub may briefly return deleted A until its read model converges. This is a typed scheduled defer, not a
        # poison failure that consumes the durable attempt budget.
        before = connected
        same_deferred = None
        try:
            proc("installation", payload("deleted"), None, GH(current=identity("A-42")))
        except DQ.IntentionalDeliveryDeferral as exc:
            same_deferred = exc
        checks.append(("same-current delete schedules an intentional defer before DB",
                       same_deferred is not None and same_deferred.not_before > datetime.now(timezone.utc)
                       and connected == before))

        # An authoritative 404 is explicit (never inferred from malformed authority) and continues to the DB purge.
        absent_payload = payload("deleted")
        before = connected
        reached = False
        try:
            proc("installation", absent_payload, None, GH(current=None))
        except DBReached:
            reached = True
        checks.append(("absent current installation proceeds with an explicit absence proof",
                       reached and connected == before + 1
                       and absent_payload.get(WH._DELETE_PROOF_MARKER, {}).get("state") == "absent"))

        before = connected
        malformed_current_raised = False
        try:
            proc("installation", payload("deleted"), None,
                 GH(current={"account_id": "42", "created_at": "2026-01-01T00:00:00Z",
                             "suspended": False}))
        except RuntimeError as exc:
            malformed_current_raised = "installation_id" in str(exc)
        checks.append(("missing current installation_id never becomes a green replacement skip",
                       malformed_current_raised and connected == before))

        # API uncertainty is never converted to absence.
        before = connected
        transient_raised = False
        try:
            proc("installation", payload("deleted"), None, GH(error=RuntimeError("GitHub unavailable")))
        except RuntimeError as exc:
            transient_raised = "GitHub unavailable" in str(exc)
        checks.append(("point-read uncertainty retries before DB", transient_raised and connected == before))

        # Suspend uses the same App-JWT stable-account authority before opening a tenant connection. Its proof
        # carries either the exact current generation or absence from a complete scan; SQL performs the atomic
        # seed/compare.
        replacement_suspend = payload("suspend")
        before = connected
        suspend_reached = False
        try:
            proc("installation", replacement_suspend, None, GH(current=identity("B-42")))
        except DBReached:
            suspend_reached = True
        suspend_marker = replacement_suspend.get(WH._SUSPEND_PROOF_MARKER, {})
        checks.append(("suspend resolves current generation before tenant admission",
                       suspend_reached and connected == before + 1
                       and suspend_marker.get("state") == "current"
                       and suspend_marker.get("current", {}).get("installation_id") == "B-42"))

        absent_suspend = payload("suspend")
        before = connected
        absent_suspend_reached = False
        try:
            proc("installation", absent_suspend, None, GH(current=None))
        except DBReached:
            absent_suspend_reached = True
        checks.append(("suspend carries an explicit authoritative absence before tenant admission",
                       absent_suspend_reached and connected == before + 1
                       and absent_suspend.get(WH._SUSPEND_PROOF_MARKER, {}).get("state") == "absent"))

        before = connected
        suspend_uncertainty_raised = False
        try:
            proc("installation", payload("suspend"), None, GH(error=RuntimeError("suspend lookup unavailable")))
        except RuntimeError as exc:
            suspend_uncertainty_raised = "suspend lookup unavailable" in str(exc)
        checks.append(("suspend point-read uncertainty retries before DB",
                       suspend_uncertainty_raised and connected == before))
    finally:
        psycopg2.connect = original_connect

    raw = {
        "id": 123, "account": {"id": 42}, "created_at": "2026-01-01T00:00:00Z",
        "suspended_at": None,
    }
    rest = RestProbe(raw)
    rest_identity = rest.app_installation_identity("123")
    checks.append(("exact App installation point read is bounded and App-JWT authenticated",
                   rest_identity == identity("123")
                   and rest.calls == [("GET", "/app/installations/123", "app-jwt")]))
    account_rest = RestProbe([raw])
    account_identity = account_rest.app_account_installation_identity("42")
    checks.append(("account lifecycle proof scans App installations by stable account id",
                   account_identity == identity("123")
                   and account_rest.calls == [
                       ("GET", "/app/installations?per_page=100&page=1", "app-jwt")]))
    absent_account_rest = RestProbe([raw_identity("elsewhere", "99")])
    checks.append(("only a complete App-installations scan proves stable-account absence",
                   absent_account_rest.app_account_installation_identity("42") is None
                   and len(absent_account_rest.calls) == 1))

    full_page = [raw_identity(f"other-{n}", str(1000 + n)) for n in range(100)]
    full_then_empty = RestProbe(responses=[full_page, []])
    checks.append(("a full 100-entry page needs and accepts a following empty-page EOF proof",
                   full_then_empty.app_account_installation_identity("42", cap=100) is None
                   and [call[1] for call in full_then_empty.calls] == [
                       "/app/installations?per_page=100&page=1",
                       "/app/installations?per_page=100&page=2",
                   ]))

    second_page_replacement = RestProbe(responses=[
        full_page,
        [raw_identity("B-after-rename", "42")],
    ])
    checks.append(("a renamed replacement on page two is found by stable id after the complete scan",
                   second_page_replacement.app_account_installation_identity("42")
                   == identity("B-after-rename")
                   and len(second_page_replacement.calls) == 2))

    private_account = "private-account-proof-sentinel"
    private_installation = "private-installation-proof-sentinel"
    cap_page = [raw_identity(private_installation, private_account)] + full_page[1:]
    cap_scan = RestProbe(responses=[cap_page, [raw_identity("private-over-cap-installation", "99")]])
    cap_raised = False
    cap_message = ""
    cap_log = io.StringIO()
    with redirect_stdout(cap_log):
        try:
            cap_scan.app_account_installation_identity(private_account, cap=100)
        except RuntimeError as exc:
            cap_message = str(exc)
            cap_raised = "cap" in cap_message
    checks.append(("cap saturation retries without logging account or installation ids",
                   cap_raised
                   and private_account not in cap_message + cap_log.getvalue()
                   and private_installation not in cap_message + cap_log.getvalue()))

    transient_scan_raised = False
    try:
        RestProbe(error=RuntimeError("installation scan unavailable")).app_account_installation_identity("42")
    except RuntimeError as exc:
        transient_scan_raised = "unavailable" in str(exc)
    checks.append(("transient App-installations scan failure remains a durable retry", transient_scan_raised))

    malformed_scan_raised = False
    try:
        RestProbe({"not": "a page"}).app_account_installation_identity("42")
    except RuntimeError as exc:
        malformed_scan_raised = "malformed" in str(exc)
    checks.append(("malformed App-installations pagination never becomes absence", malformed_scan_raised))
    not_found = RestProbe(error=urllib.error.HTTPError("u", 404, "missing", {}, None))
    checks.append(("only a real GitHub 404 becomes authoritative absence",
                   not_found.app_installation_identity("gone") is None))
    malformed_raised = False
    try:
        RestProbe({"id": 1, "account": {"id": 42}}).app_installation_identity("1")
    except RuntimeError as exc:
        malformed_raised = "omitted" in str(exc)
    checks.append(("malformed point-read authority fails closed", malformed_raised))
    server_error_raised = False
    try:
        RestProbe(error=urllib.error.HTTPError("u", 500, "boom", {}, None)).app_installation_identity("1")
    except urllib.error.HTTPError as exc:
        server_error_raised = exc.code == 500
    checks.append(("non-404 GitHub errors remain durable retries", server_error_raised))

    invalid_iso_raised = False
    try:
        RestProbe({"id": 1, "account": {"id": 42}, "created_at": "2026-01-01",
                   "suspended_at": None}).app_installation_identity("1")
    except RuntimeError as exc:
        invalid_iso_raised = "timezone" in str(exc)
    checks.append(("REST point-read rejects created_at without an ISO timezone", invalid_iso_raised))

    missing_suspend_state_raised = False
    try:
        RestProbe({"id": 1, "account": {"id": 42},
                   "created_at": "2026-01-01T00:00:00Z"}).app_installation_identity("1")
    except RuntimeError as exc:
        missing_suspend_state_raised = "suspended_at" in str(exc)
    checks.append(("REST point-read requires an explicit suspension state", missing_suspend_state_raised))

    invalid_suspend_time_raised = False
    try:
        RestProbe({"id": 1, "account": {"id": 42}, "created_at": "2026-01-01T00:00:00Z",
                   "suspended_at": "not-a-time"}).app_installation_identity("1")
    except RuntimeError as exc:
        invalid_suspend_time_raised = "ISO timestamp" in str(exc)
    checks.append(("REST point-read validates a non-null suspension timestamp", invalid_suspend_time_raised))

    # Handler consumes the structured marker through SQL /1; replacement is not short-circuited in Python.
    handler_calls = []

    def proof_db(sql, args=()):
        handler_calls.append((sql, args))
        if "purge_account_working_set_with_authority" in sql:
            return {"ok": True, "stale_ignored": True, "observed_generation": "B-42"}
        return ""

    handler_result = WH._handle_installation_event("installation", replacement_payload, proof_db, GH())
    purge_calls = [(sql, args) for sql, args in handler_calls
                   if "purge_account_working_set_with_authority" in sql]
    checks.append(("delete handler passes replacement proof to purge SQL /1",
                   handler_result.get("purged", {}).get("stale_ignored") is True
                   and len(purge_calls) == 1 and "%s::jsonb" in purge_calls[0][0]
                   and '"state":"replacement"' in purge_calls[0][1][0]))

    # Suspend is also bound to its exact durable inbox row and App-JWT current-generation proof.  A delayed A
    # suspend after replacement B has activated is an observable stale no-op; it must not release/revoke B.
    suspend_calls = []

    def suspend_db(sql, args=()):
        suspend_calls.append((sql, args))
        return {"ok": True, "suspended": False, "stale_ignored": True, "released": 0}

    suspend_payload = payload("suspend")
    suspend_payload[WH._SUSPEND_PROOF_MARKER] = {
        "state": "current",
        "suspended_installation_id": "A-42",
        "account_id": "42",
        "current": identity("B-42"),
    }
    suspend_result = WH._handle_installation_event("installation", suspend_payload, suspend_db, GH())
    release_calls = [(sql, args) for sql, args in suspend_calls
                     if "release_account_claims_with_authority" in sql]
    checks.append(("suspend handler binds SQL /2 to delivery+proof and preserves a stale verdict",
                   suspend_result.get("released", {}).get("stale_ignored") is True
                   and suspend_result.get("released", {}).get("suspended") is False
                   and len(release_calls) == 1
                   and release_calls[0][0] ==
                       "SELECT core.release_account_claims_with_authority(%s,%s::jsonb)"
                   and release_calls[0][1][0] == "delivery-suspend"
                   and '"installation_id":"B-42"' in release_calls[0][1][1]))

    # DeliveryStore catches the typed wait, restores the lease attempt through SQL defer, and reports a worker
    # deferred outcome without finish/release.
    class DeferralStore(DQ.DeliveryStore):
        def __init__(self):
            super().__init__("postgresql://unused")
            self.calls = []

        def claim(self, key):
            self.calls.append(("claim", key))
            return {"claimed": True, "event_type": "installation",
                    "payload": {"action": "deleted"}, "lease_generation": 7}

        def defer(self, key, not_before, reason, lease_generation):
            self.calls.append(("defer", key, not_before, reason, lease_generation))
            return True

        def finish(self, key, lease_generation):
            self.calls.append(("finish", key, lease_generation))
            return True

        def release(self, key, error, lease_generation):
            self.calls.append(("release", key, lease_generation))
            return "queued"

    def convergence_wait(*_args, **_kwargs):
        raise DQ.IntentionalDeliveryDeferral(
            "wait for GitHub", datetime.now(timezone.utc) + timedelta(seconds=30))

    deferral_store = DeferralStore()
    deferred_result = deferral_store.wrap_processor(convergence_wait)(
        "installation", {"_veripsa_delivery_key": "defer-delete"}, None, None)
    checks.append(("typed convergence wait uses attempt-neutral durable defer and no finish/release",
                   deferred_result.get(DQ._WORKER_CLAIM_OUTCOME) == "deferred"
                   and [call[0] for call in deferral_store.calls] == ["claim", "defer"]
                   and deferral_store.calls[1][4] == 7))

    required_actions = {
        ("installation", action) for action in
        ("created", "unsuspend", "new_permissions_accepted", "suspend", "deleted")
    } | {("installation_repositories", "added")}
    checks.append(("installation generation lifecycle stays durable when the ordinary inbox kill switch is off",
                   all(SH._requires_durable_repository_offboard(event, {"action": action})
                       for event, action in required_actions)
                   and not SH._requires_durable_repository_offboard("pull_request", {"action": "opened"})))

    for label, ok in checks:
        print(("  [PASS] " if ok else "  [FAIL] ") + label)
    print("INSTALLATION GENERATION PREFLIGHT GATE:", "PASS" if all(ok for _, ok in checks) else "FAIL")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
