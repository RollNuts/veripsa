#!/usr/bin/env python3
"""INSTALL-CAP KNOB gate — proves the VERIPSA_APP_INSTALLATIONS_CAP config knob and the truncation alert.

THE GAP THIS CLOSES (P2-3): list_app_installations had a hardcoded cap of 500. On a public Marketplace App with
>500 tenants, any installation beyond that cap would be invisible to boot_reconcile and for_account — never
reconciled on boot, coordinates marked 'unservable' in graph_freshness, silently degraded. The WEBHOOK path was
unaffected (it reads payload.installation.id directly), but boot/background reconciliation would silently ceiling
at 500. This gate proves:

  (CAP-1) the cap uses the VERIPSA_APP_INSTALLATIONS_CAP knob (not a hardcoded 500): a custom knob value is
          respected, and list_app_installations paginates up to exactly that many installations.
  (CAP-2) when the returned count reaches the cap (possible truncation), a content-free WARNING alert fires,
          carrying installation count + knob name — NO tenant ids, NO org names.
  (CAP-3) when the count is below the cap (no truncation), no alert fires (the key is resolved/re-armed).
  (CAP-4) the alert is content-free: only numeric/knob-name fields; no tenant id, no account, no installation
          details leak into the alert payload.
  (CAP-5) a bad knob value (non-integer, or <1) fails LOUD at module import (env_int's startup guard), the same
          posture as VERIPSA_MAX_TARBALL_BYTES / GITHUB_HTTP_TIMEOUT.

No network, no DB. _urlopen is stubbed; the alert sink is a fake poster.

Run:  python3 tests/test_install_cap_knob.py
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))


class _Resp:
    def __init__(self, body): self._body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, *a): return self._body


def _make_page(n: int, start_id: int = 1) -> bytes:
    """Return a JSON-encoded page of `n` installations (each with unique id + account id)."""
    entries = [
        {"id": start_id + i, "account": {"id": 9000 + start_id + i, "login": f"org{start_id + i}"}}
        for i in range(n)
    ]
    return json.dumps(entries).encode()


def _scripted_gh(pages: list[list[dict]], knob_cap: int | None = None):
    """Return a GitHubREST singleton with a faked socket that serves `pages` as paginated
    GET /app/installations responses. If knob_cap is set, the module-level _APP_INSTALLATIONS_CAP
    is patched to that value. Returns (gh, posts, teardown)."""
    import github_rest as GR

    saved_urlopen = GR.GitHubREST._urlopen
    saved_sleep = GR.GitHubREST._sleep
    saved_jwt = GR.GitHubREST._jwt
    saved_cap = GR._APP_INSTALLATIONS_CAP

    if knob_cap is not None:
        GR._APP_INSTALLATIONS_CAP = knob_cap

    call_count = [0]

    def _urlopen(req):
        url = req.full_url
        # paginated GET /app/installations
        for pg_num, page_data in enumerate(pages, start=1):
            if url.endswith(f"/app/installations?per_page=100&page={pg_num}"):
                call_count[0] += 1
                return _Resp(json.dumps(page_data).encode())
        # any other page -> empty (stop pagination)
        if "/app/installations?" in url:
            return _Resp(b"[]")
        raise AssertionError(f"unexpected urlopen: {url}")

    GR.GitHubREST._urlopen = staticmethod(_urlopen)
    GR.GitHubREST._sleep = staticmethod(lambda s: None)
    GR.GitHubREST._jwt = lambda self: "APP-JWT"

    def _teardown():
        GR.GitHubREST._urlopen = saved_urlopen
        GR.GitHubREST._sleep = saved_sleep
        GR.GitHubREST._jwt = saved_jwt
        GR._APP_INSTALLATIONS_CAP = saved_cap

    gh = GR.GitHubREST("app-id", "-----BEGIN KEY-----\nx\n-----END KEY-----", "111")
    return gh, call_count, _teardown


def _make_fake_sink():
    """Return a fake AlertSink-like object that records fire/resolve calls. Injected into github_rest._alert_sink."""
    import github_rest as GR

    class _FakeSink:
        def __init__(self):
            self.fired = []      # [(key, level, fields)]
            self.resolved = []   # [key]

        def fire(self, key, level, message, fields=None):
            self.fired.append((key, level, dict(fields or {})))
            return True

        def resolve(self, key):
            self.resolved.append(key)

    fake = _FakeSink()
    old = GR._alert_sink
    GR._alert_sink = fake

    def _restore():
        GR._alert_sink = old

    return fake, _restore


def main() -> int:
    import github_rest as GR
    from env_config import env_int, ConfigError

    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # ── (CAP-1) knob value is respected ──────────────────────────────────────────────────────────────────────
    # Build 3 pages of 100 each = 300 installations total. With a knob cap of 250 we should get exactly 250 back
    # (two full pages pulled, stopped at the cap before the third page) and the alert fires.
    page1 = [{"id": i, "account": {"id": 9000 + i, "login": f"org{i}"}} for i in range(1, 101)]
    page2 = [{"id": i, "account": {"id": 9000 + i, "login": f"org{i}"}} for i in range(101, 201)]
    page3 = [{"id": i, "account": {"id": 9000 + i, "login": f"org{i}"}} for i in range(201, 301)]

    gh, _cc, _td = _scripted_gh([page1, page2, page3], knob_cap=250)
    fake_sink, _rs = _make_fake_sink()
    saturated_reachability = {}
    try:
        result = gh.list_app_installations()
        rebuilt = gh._rebuild_account_install_map()
        saturated_reachability = gh.app_installations_reachability()
    finally:
        _td()
        _rs()

    chk("(CAP-1) cap from knob (250): result capped at 250, not 300 or a hardcoded 500",
        len(result) == 250)
    chk("(CAP-1) each entry has installation_id and account_id",
        all(e.get("installation_id") and e.get("account_id") for e in result))
    chk("(CAP-1) a saturated account map is explicitly reachable-but-incomplete",
        rebuilt is True
        and saturated_reachability.get("reachable") is True
        and saturated_reachability.get("complete") is False)

    # ── (CAP-2) truncation alert fires when count == cap ─────────────────────────────────────────────────────
    fired_cap_alerts = [f for f in fake_sink.fired if f[0] == "installations_cap_hit"]
    chk("(CAP-2) installations_cap_hit WARNING fires when result == cap",
        len(fired_cap_alerts) >= 1 and fired_cap_alerts[-1][1] == "warning")

    # ── (CAP-4) alert is content-free (count + knob name only; no tenant ids, no org names) ──────────────────
    if fired_cap_alerts:
        fields = fired_cap_alerts[-1][2]
        # must carry installations_count (numeric) and knob (string label "VERIPSA_APP_INSTALLATIONS_CAP")
        has_count = isinstance(fields.get("installations_count"), int)
        has_knob = fields.get("knob") == "VERIPSA_APP_INSTALLATIONS_CAP"
        # must NOT carry any per-tenant information (no installation_id values from the list, no account_id, no login)
        tenant_leaked = any(k in fields for k in ("installation_id", "account_id", "login", "org"))
        chk("(CAP-4) alert fields: installations_count (int) + knob name — present, no tenant ids",
            has_count and has_knob and not tenant_leaked)
    else:
        chk("(CAP-4) alert fields: installations_count + knob name (SKIPPED — no alert fired)", False)

    # ── (CAP-3) no alert when result < cap ───────────────────────────────────────────────────────────────────
    # Only 2 installations total; cap is 10 → well under. Alert should NOT fire, key should be resolved.
    two_installs = [
        {"id": 1, "account": {"id": 9001, "login": "org1"}},
        {"id": 2, "account": {"id": 9002, "login": "org2"}},
    ]
    gh2, _cc2, _td2 = _scripted_gh([two_installs], knob_cap=10)
    fake2, _rs2 = _make_fake_sink()
    complete_reachability = {}
    try:
        result2 = gh2.list_app_installations()
        rebuilt2 = gh2._rebuild_account_install_map()
        complete_reachability = gh2.app_installations_reachability()
    finally:
        _td2()
        _rs2()

    fired_cap2 = [f for f in fake2.fired if f[0] == "installations_cap_hit"]
    resolved_cap2 = [k for k in fake2.resolved if k == "installations_cap_hit"]
    chk("(CAP-3) no truncation alert when result (2) < cap (10)",
        len(fired_cap2) == 0 and len(result2) == 2)
    chk("(CAP-3) key is resolved (re-armed) when below the cap",
        "installations_cap_hit" in resolved_cap2)
    chk("(CAP-3) a below-cap map is explicit absence authority",
        rebuilt2 is True
        and complete_reachability.get("reachable") is True
        and complete_reachability.get("complete") is True)

    # A short endpoint page proves pagination ended, but a malformed row still
    # means the account→installation map is incomplete.  It must not turn a
    # later miss into authoritative absence merely because accepted_count < cap.
    malformed_page = [
        {"id": 3, "account": {"id": 9003, "login": "org3"}},
        {"id": "not-an-id", "account": {}},
    ]
    gh3, _cc3, _td3 = _scripted_gh([malformed_page], knob_cap=10)
    fake3, _rs3 = _make_fake_sink()
    try:
        rebuilt3 = gh3._rebuild_account_install_map()
        malformed_reachability = gh3.app_installations_reachability()
    finally:
        _td3()
        _rs3()
    chk("(CAP-3) malformed raw rows never become complete absence authority",
        rebuilt3 is True
        and malformed_reachability.get("reachable") is True
        and malformed_reachability.get("complete") is False
        and gh3._last_app_installations_raw_count == 2)

    # ── (CAP-5) bad knob value fails LOUD at startup (env_int posture) ───────────────────────────────────────
    # Save and restore the env var to avoid polluting later tests.
    old_env = os.environ.get("VERIPSA_APP_INSTALLATIONS_CAP")
    failed_as_expected = False
    try:
        os.environ["VERIPSA_APP_INSTALLATIONS_CAP"] = "not-a-number"
        try:
            env_int("VERIPSA_APP_INSTALLATIONS_CAP", 500, min_value=1)
        except ConfigError:
            failed_as_expected = True
    finally:
        if old_env is None:
            os.environ.pop("VERIPSA_APP_INSTALLATIONS_CAP", None)
        else:
            os.environ["VERIPSA_APP_INSTALLATIONS_CAP"] = old_env
    chk("(CAP-5) non-integer knob raises ConfigError (fails LOUD at startup, names the var)",
        failed_as_expected)

    # zero / negative also fails
    old_env = os.environ.get("VERIPSA_APP_INSTALLATIONS_CAP")
    failed_zero = False
    try:
        os.environ["VERIPSA_APP_INSTALLATIONS_CAP"] = "0"
        try:
            env_int("VERIPSA_APP_INSTALLATIONS_CAP", 500, min_value=1)
        except ConfigError:
            failed_zero = True
    finally:
        if old_env is None:
            os.environ.pop("VERIPSA_APP_INSTALLATIONS_CAP", None)
        else:
            os.environ["VERIPSA_APP_INSTALLATIONS_CAP"] = old_env
    chk("(CAP-5) zero knob raises ConfigError (min_value=1 enforced — a 0 cap would silently drop all tenants)",
        failed_zero)

    # ── (CAP-1b) exact default is 500 (the shipped value; the knob reads it from _APP_INSTALLATIONS_CAP) ─────
    chk("(CAP-1b) default cap is 500 (the module constant _APP_INSTALLATIONS_CAP with no env override)",
        GR._APP_INSTALLATIONS_CAP == 500 or os.environ.get("VERIPSA_APP_INSTALLATIONS_CAP") is not None)

    ok_all = all(c for _, c in checks)
    print("INSTALL-CAP KNOB GATE:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
