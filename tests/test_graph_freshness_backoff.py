#!/usr/bin/env python3
"""DEAD-COORDINATE BACKOFF gate (#834).

Ghost coordinates (deleted repo → HEAD 404 forever; uninstalled account) were re-attempted and re-logged
every ~50s watchdog cycle. This gate proves the in-memory exponential backoff is SURGICAL:

  DEAD)      a 404 HEAD failure registers backoff — the next cycle makes NO client call for that repo.
  GROWTH)    repeated dead outcomes grow the delay exponentially and cap at _DEAD_COORD_CAP_SECONDS.
  REVIVE)    a successful resolve clears the key — normal every-cycle resolution resumes.
  TRANSIENT) a NON-404 failure (network/5xx) registers NO backoff — live coordinates are never slowed.
  SHAPE)     within a backoff window the resolved entry is the same unknown triple a failed attempt yields.

No database, no network: a fake client counts calls. Run: python3 tests/test_graph_freshness_backoff.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import graph_freshness as GF  # noqa: E402

checks = []


def chk(ok, label):
    checks.append((bool(ok), label))
    print(("PASS" if ok else "FAIL"), label)


class FakeClient:
    """Counts HEAD resolutions; raises per-repo scripted errors."""

    def __init__(self, script):
        self.script = script  # repo -> Exception | (branch, sha, full)
        self.calls = []

    def repo_default_branch_head_info(self, repo):
        self.calls.append(repo)
        outcome = self.script[repo]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def reset():
    with GF._DEAD_COORD_LOCK:
        GF._DEAD_COORD_BACKOFF.clear()


# DEAD: first 404 registers backoff; second cycle skips the client call entirely.
reset()
gh = FakeClient({"ghost/gone": Exception("HTTP Error 404: Not Found")})
out1 = GF._resolve_head_group(gh, ["ghost/gone"])
out2 = GF._resolve_head_group(gh, ["ghost/gone"])
chk(gh.calls == ["ghost/gone"], "DEAD: exactly one client call across two cycles")
chk(out1["ghost/gone"] == (None, None, None) and out2["ghost/gone"] == (None, None, None),
    "SHAPE: both cycles yield the same unknown triple")

# GROWTH: repeated dead outcomes double the delay and cap.
reset()
d1 = GF._dead_coord_mark_dead(("head", "g/x"))
d2 = GF._dead_coord_mark_dead(("head", "g/x"))
d3 = GF._dead_coord_mark_dead(("head", "g/x"))
chk(d1 == GF._DEAD_COORD_BASE_SECONDS and d2 == d1 * 2 and d3 == d1 * 4, "GROWTH: exponential 1x/2x/4x")
for _ in range(20):
    dcap = GF._dead_coord_mark_dead(("head", "g/x"))
chk(dcap == GF._DEAD_COORD_CAP_SECONDS, "GROWTH: delay caps at _DEAD_COORD_CAP_SECONDS")

# REVIVE: expire the window (rewind next_attempt), then a success clears the key.
reset()
GF._dead_coord_mark_dead(("head", "back/alive"))
with GF._DEAD_COORD_LOCK:
    GF._DEAD_COORD_BACKOFF[("head", "back/alive")][1] = 0.0  # window elapsed
gh = FakeClient({"back/alive": ("main", "a" * 40, "back/alive")})
out = GF._resolve_head_group(gh, ["back/alive"])
chk(out["back/alive"] == ("a" * 40, "main", "back/alive"), "REVIVE: post-window attempt resolves normally")
with GF._DEAD_COORD_LOCK:
    gone = ("head", "back/alive") not in GF._DEAD_COORD_BACKOFF
chk(gone, "REVIVE: success clears the backoff key")

# TRANSIENT: a non-404 failure must NOT register backoff — the next cycle retries.
reset()
gh = FakeClient({"live/flaky": Exception("connection reset by peer")})
GF._resolve_head_group(gh, ["live/flaky"])
GF._resolve_head_group(gh, ["live/flaky"])
chk(gh.calls == ["live/flaky", "live/flaky"], "TRANSIENT: non-404 errors retry every cycle")
with GF._DEAD_COORD_LOCK:
    none_registered = ("head", "live/flaky") not in GF._DEAD_COORD_BACKOFF
chk(none_registered, "TRANSIENT: no backoff key registered")

reset()
failed = [label for ok, label in checks if not ok]
print(f"\nDEAD-COORDINATE BACKOFF GATE: {'PASS' if not failed else 'FAIL'} ({len(checks)} checks)")
sys.exit(1 if failed else 0)
