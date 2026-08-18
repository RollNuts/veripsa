#!/usr/bin/env python3
"""Offline gate for the App-JWT failed-webhook recovery protocol."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
import inspect
import io
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "github-app"))

import failed_delivery_recovery as R  # noqa: E402
from github_rest_installs import _GitHubInstallationsMixin, InvalidDeliveryCursor  # noqa: E402


NOW = datetime.now(timezone.utc).replace(microsecond=0)
TIMED_OUT_FIXTURE = {
    "id": 9101,
    "guid": "11111111-2222-3333-4444-555555555555",
    "delivered_at": NOW.isoformat().replace("+00:00", "Z"),
    "status_code": 0,
    "status": "Timed Out",
    "redelivery": False,
}


class FakeStore:
    def __init__(self, *, acquired=True, cursor=None, high_water=None, page_tail=None,
                 candidates=None, depth=None):
        self.acquired = acquired
        self.epoch = 7
        self.cursor = cursor
        self.hwm = high_water
        self.cutoff = NOW - timedelta(days=3)
        self.page_tail = page_tail
        self.candidates = list(candidates or [])
        self.events = []
        self.observed = []
        self.reset_count = 0
        self.records = []
        self.depth_value = depth or {"eligible": 0, "exhausted": 0, "expiring": 0,
                                     "terminal_unrecovered": 0, "cooldown_active": False,
                                     "expired_unrecovered": 0, "scan_lag_seconds": 0}

    @contextmanager
    def singleflight(self):
        self.events.append("lock")
        yield self.acquired

    def begin_scan(self):
        self.events.append("begin")
        return {"epoch": self.epoch, "cursor": self.cursor,
                "high_water_delivery_id": self.hwm,
                "page_tail_delivery_id": self.page_tail[1] if self.page_tail else None,
                "page_tail_delivered_at": self.page_tail[0].isoformat() if self.page_tail else None,
                "cutoff_at": self.cutoff.isoformat()}

    def observe(self, epoch, item):
        assert epoch == self.epoch
        self.events.append("observe")
        self.observed.append(item)
        return True

    def advance_scan(self, epoch, expected_cursor, cursor, complete, page_head, page_tail):
        assert epoch == self.epoch
        if self.cursor != expected_cursor:
            return False
        if (self.page_tail and page_head
                and (page_head["delivered_at"], page_head["id"]) > self.page_tail):
            return False
        self.events.append("complete" if complete else "advance")
        self.cursor = cursor
        if page_tail:
            self.page_tail = (page_tail["delivered_at"], page_tail["id"])
        if complete:
            self.hwm = max((x["id"] for x in self.observed), default=self.hwm)
        return True

    def reset_scan(self, epoch, expected_cursor):
        assert epoch == self.epoch
        if self.cursor != expected_cursor:
            return False
        self.reset_count += 1
        self.cursor = None
        self.page_tail = None
        return True

    def claim_candidate(self):
        self.events.append("claim")
        return self.candidates.pop(0) if self.candidates else {}

    def record_attempt(self, guid, generation, outcome, not_before=None):
        self.events.append("record")
        self.records.append((guid, generation, outcome, not_before))
        return True

    def prune(self):
        self.events.append("prune")
        return 0

    def depth(self):
        self.events.append("depth")
        return dict(self.depth_value, eligible=len(self.candidates))


class StickyGuidStore(FakeStore):
    """Small protocol model; SQL-source assertions below pin the same upsert shape."""
    def __init__(self, *, rows=None, **kwargs):
        super().__init__(**kwargs)
        self.rows = rows if rows is not None else {}

    def observe(self, epoch, item):
        super().observe(epoch, item)
        key = item["guid"]
        incoming = dict(item, resolved=item["status"] == "OK" and 200 <= item["status_code"] <= 399,
                        attempts=0)
        current = self.rows.get(key)
        if current is None:
            self.rows[key] = incoming
        else:
            if (item["delivered_at"], item["id"]) >= (current["delivered_at"], current["id"]):
                current.update(item)
            current["resolved"] = current["resolved"] or incoming["resolved"]
        return True

    def claim_candidate(self):
        self.events.append("claim")
        for guid, row in self.rows.items():
            if (not row["resolved"] and row["status"] in ("Other", "Timed Out")
                    and row["attempts"] < 3):
                row["attempts"] += 1
                return {"delivery_guid": guid, "delivery_id": row["id"],
                        "generation": row["attempts"], "attempt": row["attempts"]}
        return {}


class FakeGH:
    def __init__(self, pages, post_error=None, post_status=202, events=None):
        self.pages = pages
        self.post_error = post_error
        self.post_status = post_status
        self.list_calls = []
        self.posts = []
        self.events = events

    def list_app_hook_deliveries(self, cursor=None, per_page=100):
        self.list_calls.append((cursor, per_page))
        value = self.pages[cursor]
        if isinstance(value, Exception):
            raise value
        return value

    def redeliver_app_hook_delivery_once(self, delivery_id):
        if self.events is not None:
            self.events.append("post")
        self.posts.append(delivery_id)
        if self.post_error:
            raise self.post_error
        return self.post_status


class FakeSink:
    def __init__(self):
        self.fired = []
        self.resolved = []

    def fire(self, key, level, message, fields, *, escalate_value=None):
        self.fired.append((key, level, fields))

    def resolve(self, key):
        self.resolved.append(key)


class HTTPish(Exception):
    def __init__(self, code, headers=None):
        super().__init__("SECRET-MUST-NOT-LOG")
        self.code = code
        self.headers = headers or {}


def candidate(n=1):
    return {"delivery_guid": f"aaaaaaaa-bbbb-cccc-dddd-{n:012d}",
            "delivery_id": 8000 + n, "generation": n, "attempt": n}


def page(items, cursor=None):
    return {"deliveries": items, "next_cursor": cursor}


def test_pagination_resume_and_precommit_order():
    first = dict(TIMED_OUT_FIXTURE)
    second = dict(TIMED_OUT_FIXTURE, id=9100, guid="aaaaaaaa-2222-3333-4444-555555555555")
    store = FakeStore(candidates=[candidate()])
    gh = FakeGH({None: page([first], "opaque-1"), "opaque-1": page([second])}, events=store.events)
    one = R.recover_failed_deliveries_tick(store, gh, max_pages=1)
    assert not one["scan_complete"] and not gh.posts and store.cursor == "opaque-1"
    two = R.recover_failed_deliveries_tick(store, gh, max_pages=1)
    assert two["scan_complete"] and gh.list_calls[-1][0] == "opaque-1"
    assert store.events.index("claim") < store.events.index("post") < store.events.index("record")
    assert gh.posts == [8001] and store.records[0][2] == "accepted_ambiguous"


def test_singleflight_skips_second_instance():
    store = FakeStore(acquired=False)
    gh = FakeGH({})
    result = R.recover_failed_deliveries_tick(store, gh)
    assert result["singleflight"] is False and gh.list_calls == [] and gh.posts == []


def test_invalid_cursor_resets_without_post():
    store = FakeStore(cursor="expired", candidates=[candidate()])
    gh = FakeGH({"expired": InvalidDeliveryCursor("cursor rejected")})
    try:
        R.recover_failed_deliveries_tick(store, gh)
        raise AssertionError("invalid cursor must fail")
    except InvalidDeliveryCursor:
        pass
    assert store.reset_count == 1 and gh.posts == []

    empty_store = FakeStore(candidates=[candidate()])
    try:
        R.recover_failed_deliveries_tick(
            empty_store, FakeGH({None: page([], "impossible-next")}))
        raise AssertionError("empty non-terminal page must invalidate the cursor chain")
    except InvalidDeliveryCursor:
        pass
    assert empty_store.reset_count == 1 and "claim" not in empty_store.events


def test_high_water_item_is_refreshed_before_scan_stops():
    refreshed = dict(TIMED_OUT_FIXTURE, status="OK", status_code=204)
    older = dict(TIMED_OUT_FIXTURE, id=9100, guid="bbbbbbbb-2222-3333-4444-555555555555",
                 delivered_at=(NOW - timedelta(seconds=1)).isoformat())
    store = FakeStore(high_water=TIMED_OUT_FIXTURE["id"])
    result = R.recover_failed_deliveries_tick(store, FakeGH({None: page([refreshed, older], "unused")}))
    assert result["scan_complete"] and result["observed"] == 1
    assert store.observed == [R._delivery_metadata(refreshed)]


def test_any_success_for_guid_stays_resolved_across_order_page_and_restart():
    guid = TIMED_OUT_FIXTURE["guid"]
    newer_failure = dict(TIMED_OUT_FIXTURE, status="Bad Gateway", status_code=502)
    older_ok = dict(TIMED_OUT_FIXTURE, id=9100, status="OK", status_code=204,
                    delivered_at=(NOW - timedelta(minutes=1)).isoformat())

    same_page = StickyGuidStore()
    result = R.recover_failed_deliveries_tick(
        same_page, FakeGH({None: page([newer_failure, older_ok])}))
    assert result["scan_complete"] and same_page.rows[guid]["resolved"] and result["attempted"] == 0

    # The opposite status ordering is sticky too: a newer success cannot be reopened
    # by an older failure later in the same page.
    newer_ok = dict(newer_failure, status="OK", status_code=204)
    older_failure = dict(older_ok, status="Bad Gateway", status_code=502)
    inverse = StickyGuidStore()
    result = R.recover_failed_deliveries_tick(inverse, FakeGH({None: page([newer_ok, older_failure])}))
    assert inverse.rows[guid]["resolved"] and result["attempted"] == 0

    # Crash/restart between pages: the partial scan never POSTs, and the durable
    # older success still resolves the GUID when the cursor is resumed.
    before_restart = StickyGuidStore()
    first_gh = FakeGH({None: page([newer_failure], "opaque-resume")})
    first = R.recover_failed_deliveries_tick(before_restart, first_gh, max_pages=1)
    assert not first["scan_complete"] and first["attempted"] == 0
    after_restart = StickyGuidStore(rows=before_restart.rows, cursor=before_restart.cursor)
    after_restart.page_tail = before_restart.page_tail
    second = R.recover_failed_deliveries_tick(
        after_restart, FakeGH({"opaque-resume": page([older_ok])}), max_pages=1)
    assert second["scan_complete"] and after_restart.rows[guid]["resolved"] and second["attempted"] == 0


def test_ack_and_transport_are_both_ambiguous():
    for error, expected in ((None, "accepted_ambiguous"), (OSError("secret network"), "transport_ambiguous")):
        store = FakeStore(candidates=[candidate()])
        gh = FakeGH({None: page([])}, post_error=error)
        result = R.recover_failed_deliveries_tick(store, gh)
        assert store.records[0][2] == expected and result["attempted"] == 1
    unknown_store = FakeStore(candidates=[candidate()])
    R.recover_failed_deliveries_tick(
        unknown_store, FakeGH({None: page([])}, post_status=204))
    assert unknown_store.records[0][2] == "transport_ambiguous"


def test_4xx_terminal_and_rate_limit_global_cooldown():
    terminal_store = FakeStore(candidates=[candidate()])
    terminal_gh = FakeGH({None: page([])}, post_error=HTTPish(422))
    R.recover_failed_deliveries_tick(terminal_store, terminal_gh)
    assert terminal_store.records[0][2] == "terminal_rejected"

    limited_store = FakeStore(candidates=[candidate(1), candidate(2)])
    limited_gh = FakeGH({None: page([])}, post_error=HTTPish(429, {"Retry-After": "120"}))
    result = R.recover_failed_deliveries_tick(limited_store, limited_gh)
    assert result["rate_limited"] == 1 and len(limited_gh.posts) == 1
    assert limited_store.records[0][2] == "rate_limited"
    delay = (limited_store.records[0][3] - datetime.now(timezone.utc)).total_seconds()
    assert 110 <= delay <= 130
    nan_delay = (R._http_cooldown(HTTPish(429, {"Retry-After": "nan"}), 900)
                 - datetime.now(timezone.utc)).total_seconds()
    assert 890 <= nan_delay <= 910


def test_three_day_boundary_and_exact_timeout_fixture():
    old = dict(TIMED_OUT_FIXTURE, delivered_at=(NOW - timedelta(days=3, seconds=1)).isoformat())
    store = FakeStore()
    result = R.recover_failed_deliveries_tick(store, FakeGH({None: page([old], "unused")}))
    assert result["scan_complete"] and store.observed == []
    normalized = R._delivery_metadata(TIMED_OUT_FIXTURE)
    assert normalized["status"] == "Timed Out" and normalized["status_code"] == 0
    ok = R._delivery_metadata(dict(TIMED_OUT_FIXTURE, status="OK", status_code=204))
    redirected = R._delivery_metadata(dict(TIMED_OUT_FIXTURE, status="OK", status_code=302))
    assert ok["status"] == "OK" and ok["status_code"] == 204
    assert redirected["status"] == "OK" and redirected["status_code"] == 302

    # Delivery-list failures are broader than 5xx/timeouts.  Preserve the bounded
    # code while discarding the raw status label so 3xx/4xx can be redelivered
    # without persisting arbitrary GitHub metadata.
    for code, label in ((301, "Moved Permanently"), (400, "Bad Request"),
                        (422, "Unprocessable Entity")):
        explicit_failure = R._delivery_metadata(
            dict(TIMED_OUT_FIXTURE, status=label, status_code=code))
        assert explicit_failure["status"] == "Other"
        assert explicit_failure["status_code"] == code


def test_unknown_outcome_is_terminal_sentinel_not_page_poison():
    unknown = dict(TIMED_OUT_FIXTURE, id=9200, guid="99999999-2222-3333-4444-555555555555",
                   status_code=None, status={"future": "enum"})
    exact_5xx = dict(TIMED_OUT_FIXTURE, id=9199, guid="88888888-2222-3333-4444-555555555555",
                     status_code=503, status="Internal Server Error")
    store = FakeStore()
    result = R.recover_failed_deliveries_tick(store, FakeGH({None: page([unknown, exact_5xx])}))
    assert result["scan_complete"] and len(store.observed) == 2
    assert store.observed[0]["status_code"] == -1 and store.observed[0]["status"] == "Unknown"
    assert store.observed[1]["status_code"] == 503 and store.observed[1]["status"] == "Other"
    missing_code_failure = R._delivery_metadata(
        dict(TIMED_OUT_FIXTURE, status_code=None, status="Service Unavailable"))
    assert missing_code_failure == dict(
        R._delivery_metadata(TIMED_OUT_FIXTURE), status_code=-1, status="Other")
    for malformed in ("", " OK", "OK\n", "失敗"):
        assert R._delivery_metadata(dict(TIMED_OUT_FIXTURE, status=malformed))["status"] == "Unknown"

    redirect_store = StickyGuidStore()
    redirect_gh = FakeGH({None: page([
        dict(TIMED_OUT_FIXTURE, status="OK", status_code=302)])})
    redirect_result = R.recover_failed_deliveries_tick(redirect_store, redirect_gh)
    assert redirect_result["attempted"] == 0 and redirect_gh.posts == []

    no_code_store = StickyGuidStore()
    no_code_gh = FakeGH({None: page([
        dict(TIMED_OUT_FIXTURE, status="Service Unavailable", status_code=None)])})
    no_code_result = R.recover_failed_deliveries_tick(no_code_store, no_code_gh)
    assert no_code_result["attempted"] == 3
    assert no_code_gh.posts == [TIMED_OUT_FIXTURE["id"]] * 3
    try:
        R._delivery_metadata(dict(TIMED_OUT_FIXTURE, guid="éééééééé-2222-3333-4444-555555555555"))
        raise AssertionError("non-ASCII GUID must fail the same boundary as SQL")
    except RuntimeError:
        pass


def test_page_order_violation_never_advances_cursor():
    older = dict(TIMED_OUT_FIXTURE, id=9300, delivered_at=(NOW - timedelta(minutes=2)).isoformat())
    newer = dict(TIMED_OUT_FIXTURE, id=9301, guid="77777777-2222-3333-4444-555555555555",
                 delivered_at=(NOW - timedelta(minutes=1)).isoformat())
    store = FakeStore()
    try:
        R.recover_failed_deliveries_tick(store, FakeGH({None: page([older, newer], "must-not-advance")}))
        raise AssertionError("out-of-order page must fail")
    except RuntimeError as exc:
        assert "order" in str(exc)
    # Fail closed (no observe/advance on a violated page) AND re-baseline so a persistently
    # out-of-order page cannot wedge an in-progress scan into raising on every tick forever.
    assert store.cursor is None and store.observed == [] and "advance" not in store.events
    assert store.reset_count == 1


def test_cross_page_order_and_cursor_cas_fail_before_claim():
    first = dict(TIMED_OUT_FIXTURE, id=9400)
    partial = FakeStore(candidates=[candidate()])
    one = R.recover_failed_deliveries_tick(
        partial, FakeGH({None: page([first], "opaque-order")}), max_pages=1)
    assert not one["scan_complete"] and not one["attempted"]

    # Simulate a process restart. A next page whose head is newer than the durable previous
    # tail must neither advance nor claim a queued candidate, AND must re-baseline: an
    # interrupted in-progress scan would otherwise raise on this same stale tail every tick.
    newer = dict(TIMED_OUT_FIXTURE, id=9401,
                 delivered_at=(NOW + timedelta(seconds=1)).isoformat())
    resumed = FakeStore(cursor=partial.cursor, page_tail=partial.page_tail, candidates=[candidate()])
    resumed_gh = FakeGH({"opaque-order": page([newer])})
    try:
        R.recover_failed_deliveries_tick(resumed, resumed_gh)
        raise AssertionError("cross-page order violation must fail closed")
    except RuntimeError as exc:
        assert "cross-page order" in str(exc)
    assert resumed.observed == [] and resumed_gh.posts == []
    assert resumed.cursor is None and resumed.reset_count == 1

    # A different caller advancing the cursor between observe and advance is
    # rejected by expected-cursor CAS, even within the same scan epoch.
    class CursorRaceStore(FakeStore):
        def observe(self, epoch, item):
            result = super().observe(epoch, item)
            self.cursor = "concurrent-cursor"
            return result

    raced = CursorRaceStore(candidates=[candidate()])
    raced_gh = FakeGH({None: page([TIMED_OUT_FIXTURE])})
    try:
        R.recover_failed_deliveries_tick(raced, raced_gh)
        raise AssertionError("cursor CAS loss must fail closed")
    except RuntimeError as exc:
        assert "cursor/epoch/order authority" in str(exc)
    assert raced_gh.posts == [] and "claim" not in raced.events


def test_scan_invariant_self_heals_and_carries_content_free_reason():
    # A malformed high-water at the top of the scan re-baselines the scan (CAS-safe
    # reset_scan) BEFORE failing, and the raised error carries a compile-time reason
    # SLUG so the alert can name the invariant without leaking any delivery content.
    store = FakeStore(high_water=0, candidates=[candidate()])
    gh = FakeGH({None: page([TIMED_OUT_FIXTURE])})
    try:
        R.recover_failed_deliveries_tick(store, gh)
        raise AssertionError("malformed high-water must fail closed")
    except R.RecoveryScanInvariant as exc:
        assert isinstance(exc, RuntimeError)          # IS-A RuntimeError: legacy catches still hold
        assert exc.reason == "high-water-malformed"
        assert exc.reason and " " not in exc.reason   # a slug, never prose/cursor/guid/body
    assert store.reset_count == 1                      # self-healed (re-baselined) before raising
    assert store.observed == [] and gh.posts == []     # failed closed, no redelivery

    # advance-lost-authority (a concurrent writer moved the cursor between observe and
    # advance) also carries its slug. Re-baseline is CAS-guarded: it must NOT stomp the
    # concurrent writer, so reset_count stays 0 here — the slug still names the invariant.
    class CursorRaceStore(FakeStore):
        def observe(self, epoch, item):
            result = super().observe(epoch, item)
            self.cursor = "concurrent-cursor"
            return result

    raced = CursorRaceStore(candidates=[candidate()])
    try:
        R.recover_failed_deliveries_tick(raced, FakeGH({None: page([TIMED_OUT_FIXTURE])}))
        raise AssertionError("advance CAS loss must fail closed")
    except R.RecoveryScanInvariant as exc:
        assert exc.reason == "advance-lost-authority"
    assert raced.reset_count == 0 and "claim" not in raced.events


def test_hard_work_caps_and_zero_budget():
    pages = {
        None: page([dict(TIMED_OUT_FIXTURE, id=9104)], "c1"),
        "c1": page([dict(TIMED_OUT_FIXTURE, id=9103)], "c2"),
        "c2": page([dict(TIMED_OUT_FIXTURE, id=9102)], "c3"),
        "c3": page([TIMED_OUT_FIXTURE]),
    }
    bounded_gh = FakeGH(pages)
    result = R.recover_failed_deliveries_tick(FakeStore(), bounded_gh, max_pages=999)
    assert result["pages"] == R._SCAN_PAGES_PER_TICK and len(bounded_gh.list_calls) == 3

    candidates = [candidate(i) for i in range(1, 11)]
    candidate_gh = FakeGH({None: page([])})
    result = R.recover_failed_deliveries_tick(
        FakeStore(candidates=candidates), candidate_gh, max_candidates=999)
    assert result["attempted"] == R._CANDIDATES_PER_TICK and len(candidate_gh.posts) == 5

    zero_gh = FakeGH({})
    result = R.recover_failed_deliveries_tick(FakeStore(), zero_gh, max_pages=0)
    assert result["pages"] == 0 and zero_gh.list_calls == []


def test_durable_alert_state_survives_next_tick_and_restart():
    sink = FakeSink()
    R._evaluate_alerts(sink, {
        "transport_ambiguous": 0, "rate_limited": 0, "auth_deferred": 0,
        "depth": {"cooldown_active": True, "terminal_unrecovered": 2, "exhausted": 1,
                  "expiring": 1, "expired_unrecovered": 7, "archived_unresolved": 4,
                  "archived_terminal": 1, "archived_exhausted": 2,
                  "scan_lag_seconds": 49 * 3600},
    })
    levels = {key: level for key, level, _ in sink.fired}
    assert levels["github_delivery_redelivery_global_cooldown"] == "warning"
    assert levels["github_delivery_redelivery_terminal"] == "warning"
    assert levels["github_delivery_redelivery_expired"] == "critical"
    assert levels["github_delivery_recovery_scan_lag"] == "critical"
    assert "github_delivery_redelivery_archived" not in levels

    recovered = FakeSink()
    R._evaluate_alerts(recovered, {
        "transport_ambiguous": 0, "rate_limited": 0, "auth_deferred": 0,
        "depth": {"cooldown_active": False, "terminal_unrecovered": 0, "exhausted": 0,
                  "expiring": 0, "expired_unrecovered": 0, "archived_unresolved": 0,
                  "scan_lag_seconds": 0},
    })
    assert "github_delivery_redelivery_global_cooldown" in recovered.resolved
    assert "github_delivery_redelivery_terminal" in recovered.resolved
    assert "github_delivery_redelivery_expired" in recovered.resolved
    assert "github_delivery_recovery_scan_lag" in recovered.resolved


def test_app_client_metadata_only_and_single_post():
    import urllib.error
    calls = []

    class Response:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size): return b""

    class Client(_GitHubInstallationsMixin):
        API = "https://api.github.test"
        def _jwt(self): return "TOP-SECRET-JWT"
        def _req_with_link(self, method, url, token):
            calls.append((method, url, token))
            return ([dict(TIMED_OUT_FIXTURE, event="push", repository_id=999, body="forbidden")],
                    '<https://api.github.test/app/hook/deliveries?cursor=opaque%2Bnext>; rel="next"')
        def _urlopen(self, request):
            calls.append((request.method, request.full_url, request.get_header("Authorization")))
            return Response()

    client = Client()
    result = client.list_app_hook_deliveries()
    assert result["next_cursor"] == "opaque+next"
    assert set(result["deliveries"][0]) == {
        "id", "guid", "delivered_at", "status_code", "status"}
    operator_result = client.list_app_hook_deliveries(include_redelivery=True)
    assert set(operator_result["deliveries"][0]) == {
        "id", "guid", "delivered_at", "status_code", "status", "redelivery"}
    client.redeliver_app_hook_delivery_once(9101)
    assert calls[0][0:2] == ("GET", "/app/hook/deliveries?per_page=100")
    assert calls[1][0:2] == ("GET", "/app/hook/deliveries?per_page=100")
    assert calls[2][0] == "POST" and calls[2][1].endswith("/app/hook/deliveries/9101/attempts")
    assert calls[2][2] == "Bearer TOP-SECRET-JWT"
    list_source = inspect.getsource(_GitHubInstallationsMixin.list_app_hook_deliveries)
    assert "/attempts" not in list_source and "detail" in list_source

    class CursorClient(Client):
        def _req_with_link(self, method, url, token):
            raise urllib.error.HTTPError(url, self.error_code, "rejected", {}, io.BytesIO())

    for code in (400, 422):
        rejected = CursorClient()
        rejected.error_code = code
        try:
            rejected.list_app_hook_deliveries(cursor="expired")
            raise AssertionError(f"stored cursor HTTP {code} must reset the scan")
        except InvalidDeliveryCursor:
            pass


def test_sql_protocol_privacy_acl_and_sticky_success():
    sql = (ROOT / "db/schema/25_webhook_queue.sql").read_text()
    assert "core.github_delivery_recovery" in sql and "scan_epoch" in sql and "high_water_delivery_id" in sql
    assert "pg_try_advisory_lock" not in sql  # held as a session lock by the Python store, not transaction-local
    assert "core.github_delivery_recovery.resolved" in sql and "OR EXCLUDED.resolved" in sql
    observe_slice = sql[sql.index("CREATE OR REPLACE FUNCTION core.observe_github_delivery_with_authority"):
                        sql.index("ALTER FUNCTION core.observe_github_delivery_with_authority")]
    timeout_arm = "WHEN p_status_code=0 AND p_status='Timed Out' THEN 'timeout'"
    retry_arm = "WHEN p_status IN ('Other','Timed Out') AND p_status_code BETWEEN -1 AND 599 THEN 'redeliverable'"
    assert timeout_arm in observe_slice and retry_arm in observe_slice and "ELSE 'terminal'" in observe_slice
    assert observe_slice.index(timeout_arm) < observe_slice.index(retry_arm)
    assert "latest_status_code BETWEEN -1 AND 599" in sql
    assert "latest_status IN ('OK','Timed Out','Other','Unknown')" in sql
    assert "v_resolved := p_status='OK' AND p_status_code BETWEEN 200 AND 399" in observe_slice
    assert "latest_recovery_class IN ('redeliverable','timeout')" in sql
    assert "attempt_count<3" in sql and "window_expires_at<=now()" in sql
    record_slice = sql[sql.index("CREATE OR REPLACE FUNCTION core.record_github_delivery_redelivery_attempt"):
                       sql.index("ALTER FUNCTION core.record_github_delivery_redelivery_attempt")]
    assert "attempt_count=" not in record_slice  # every claimed/API POST spends one of the hard three attempts
    assert "'expired_unrecovered'" in sql and "'terminal_unrecovered'" in sql
    prune_slice = sql[sql.index("CREATE OR REPLACE FUNCTION core.prune_github_delivery_recovery"):
                      sql.index("ALTER FUNCTION core.prune_github_delivery_recovery")]
    assert "WHERE singleton AND in_progress" in prune_slice
    assert "WHERE resolved AND window_expires_at<=now()" in prune_slice
    assert "WHERE NOT resolved AND window_expires_at<=now()-interval '30 days'" in prune_slice
    assert prune_slice.count("LIMIT 1000 FOR UPDATE SKIP LOCKED") == 2
    assert "archived_unresolved_count" in prune_slice and "archived_terminal_count" in prune_slice
    depth_slice = sql[sql.index("CREATE OR REPLACE FUNCTION core.github_delivery_recovery_depth"):
                      sql.index("ALTER FUNCTION core.github_delivery_recovery_depth")]
    assert "+ COALESCE((SELECT archived_" not in depth_slice
    assert "'archived_unresolved'" in depth_slice  # telemetry only; never active paging totals
    # A successful GUID survives ordinary prune ticks for the whole API window;
    # if the same GUID is observed failed meanwhile, sticky resolution wins. In
    # particular, newest-first scanning may see a failure before an older success:
    # no ON-CONFLICT WHERE is allowed to discard that older proof.
    assert "resolved=core.github_delivery_recovery.resolved" in observe_slice
    assert "OR core.github_delivery_recovery.locally_received OR EXCLUDED.resolved" in observe_slice
    conflict_slice = observe_slice[observe_slice.index("ON CONFLICT (delivery_guid) DO UPDATE SET"):]
    assert "WHERE (EXCLUDED.latest_delivered_at" not in conflict_slice
    assert "THEN EXCLUDED.latest_delivery_id ELSE core.github_delivery_recovery.latest_delivery_id END" in conflict_slice
    advance_slice = sql[sql.index("CREATE OR REPLACE FUNCTION core.advance_github_delivery_recovery_scan"):
                        sql.index("ALTER FUNCTION core.advance_github_delivery_recovery_scan")]
    assert "cursor IS NOT DISTINCT FROM p_expected_cursor" in advance_slice
    assert "(page_tail_delivered_at,page_tail_delivery_id) >=" in advance_slice
    assert "DROP FUNCTION IF EXISTS core.advance_github_delivery_recovery_scan_with_authority(bigint,text,boolean)" in sql
    assert "redelivery_not_before" in sql and "rate_limited" in sql
    assert "'cooldown_active'" in sql and "'terminal_unrecovered'" in sql
    assert "'scan_lag_seconds'" in sql
    lag_slice = sql[sql.index("'scan_lag_seconds'"):sql.index("'scan_in_progress'")]
    assert "COALESCE(scan_completed_at,scan_started_at,updated_at)" in lag_slice
    assert "CASE WHEN in_progress" not in lag_slice
    for forbidden in ("repository_name", "repository_id", "account_id", "installation_id",
                      "event_type", "action", "payload jsonb", "webhook_body"):
        # The first existing inbox legitimately has payload; constrain the new table slice only.
        scan_start = sql.index("CREATE TABLE IF NOT EXISTS core.github_delivery_recovery_scan (")
        scan_slice = sql[scan_start:
                         sql.index("CREATE TABLE IF NOT EXISTS core.github_delivery_recovery (", scan_start)]
        recovery_start = sql.index("CREATE TABLE IF NOT EXISTS core.github_delivery_recovery (")
        recovery_slice = sql[recovery_start:
                             sql.index("ALTER TABLE core.github_delivery_recovery_scan", recovery_start)]
        assert forbidden not in scan_slice and forbidden not in recovery_slice
    assert "redelivery boolean" not in scan_slice and "redelivery boolean" not in recovery_slice
    alert_source = inspect.getsource(R._evaluate_alerts)
    assert "github_delivery_redelivery_archived" not in alert_source
    assert "REVOKE ALL ON TABLE core.github_delivery_recovery FROM PUBLIC" in sql
    assert "GRANT EXECUTE ON FUNCTION core.claim_github_delivery_redelivery_with_authority() TO veripsa_app" in sql


def test_contract_wiring_fixed_defaults_and_no_secret_logging():
    contract = (ROOT / "github-app/schema_contract.py").read_text()
    boot = (ROOT / "github-app/server_boot.py").read_text()
    source = inspect.getsource(R)
    assert '("record_github_delivery_redelivery_attempt_with_authority", 4)' in contract
    assert '("advance_github_delivery_recovery_scan_with_authority", 8)' in contract
    assert '("reset_github_delivery_recovery_scan_with_authority", 2)' in contract
    assert "start_failed_delivery_recovery(failed_delivery_store, gh, alert_sink)" in boot
    assert "VERIPSA_" not in inspect.getsource(R.start_failed_delivery_recovery)
    assert "Marketplace and Sponsors" in source and "details" in source

    secret = "PRIVATE-KEY-DO-NOT-PRINT"
    store = FakeStore(candidates=[candidate()])
    gh = FakeGH({None: page([])}, post_error=RuntimeError(secret))
    output = io.StringIO()
    with redirect_stdout(output):
        R.recover_failed_deliveries_tick(store, gh)
    assert secret not in output.getvalue() and output.getvalue() == ""


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"FAILED WEBHOOK RECOVERY GATE: PASS ({len(tests)} checks)")


if __name__ == "__main__":
    main()
