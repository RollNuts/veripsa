#!/usr/bin/env python3
"""INGRESS NO-OP FAST-ACK gate — the ingress filter must drop EXACTLY the check_run events that
`_handle_check_event` unconditionally no-ops, and NEVER a load-bearing event.

Because the App holds checks:write, GitHub sends a check_run for every check by every CI provider on every
install — the single largest webhook volume source. `created` and `completed`-with-a-non-failing-conclusion
are skipped by the handler (webhook_handlers.py:3354/3356), so fast-acking them 2xx before enqueue yields the
identical outcome at none of the persist->claim->tenant-admit->lock + per-account-fairness-cap cost that
starves the org's own PR/push work. This gate pins that the filter drops those two and ONLY those two."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "github-app"))
import server_http as H  # noqa: E402

# The authoritative failing set (server.py:268). Kept in sync by the ingress call passing S._FAILING_CONCLUSIONS.
FAILING = frozenset({"failure", "timed_out", "cancelled", "action_required"})

checks = []


def check(cond, label):
    checks.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + label)


def drop(event_type, action, conclusion=None, enabled=True):
    return H._ingress_noop_fastack(event_type, action, conclusion, FAILING, enabled=enabled)


print("(a) check_run no-ops are DROPPED:")
check(drop("check_run", "created") is True, "check_run.created → dropped (always a no-op)")
for c in ("success", "neutral", "skipped", None):
    check(drop("check_run", "completed", c) is True,
          f"check_run.completed conclusion={c!r} (non-failing) → dropped")

print("(b) LOAD-BEARING check_run is KEPT:")
check(drop("check_run", "rerequested") is False, "check_run.rerequested → KEPT (Re-run button)")
check(drop("check_run", "requested_action") is False, "check_run.requested_action → KEPT (Re-run button)")
for c in sorted(FAILING):
    check(drop("check_run", "completed", c) is False,
          f"check_run.completed conclusion={c!r} (FAILING) → KEPT (stuck-PR fact)")

print("(c) ALL check_suite is KEPT (its 'requested' is the deploy canary + synchronize backstop):")
for a in ("requested", "rerequested", "completed"):
    check(drop("check_suite", a, "success") is False, f"check_suite.{a} → KEPT")
check(drop("check_suite", "completed", "failure") is False, "check_suite.completed(failure) → KEPT")

print("(d) other load-bearing events are KEPT:")
for et, a in (("pull_request", "opened"), ("pull_request", "synchronize"), ("pull_request", "labeled"),
              ("push", None), ("repository", "deleted"), ("installation", "created"),
              ("merge_group", "checks_requested")):
    check(drop(et, a, None) is False, f"{et}.{a} → KEPT")

print("(e) kill switch: enabled=False drops NOTHING:")
check(drop("check_run", "created", None, enabled=False) is False, "created + disabled → KEPT")
check(drop("check_run", "completed", "success", enabled=False) is False, "completed(success) + disabled → KEPT")

print("(f) fail-safe totality: junk action/type never raises, defaults to KEEP:")
check(drop("check_run", None) is False, "check_run action=None → KEPT (fail-safe)")
check(drop("check_run", "some_future_action") is False, "unknown check_run action → KEPT (fail-safe)")
check(drop(None, None, None) is False, "None/None → KEPT (never raises)")

print()
if all(checks):
    print("INGRESS NO-OP FAST-ACK GATE: PASS")
    raise SystemExit(0)
print(f"INGRESS NO-OP FAST-ACK GATE: FAIL ({sum(not c for c in checks)} of {len(checks)} failed)")
raise SystemExit(1)
