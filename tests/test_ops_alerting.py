#!/usr/bin/env python3
"""Ops-readiness gate — PROACTIVE ALERTING + DR EXPORT/RESTORE (the "will we KNOW it broke / can we RECOVER?").

Two halves, both content-free:

ALERTING (pure — no DB, no network, a fake poster):
  • a dead worker fires a CRITICAL `worker_dead`; recovery resolves it; a re-death pages again.
  • a backlog (queue_depth ≥ 50% of maxsize) fires `queue_backlog`; draining resolves it.
  • a failed-event SPIKE (delta ≥ threshold since last sample) fires `failed_spike`.
  • DB unreachable fires CRITICAL `db_unreachable` even while the worker is alive (/healthz stays 200 —
    the exact gap nothing else catches).
  • a systemic 403 on GET /app/installations fires CRITICAL `app_jwt_unreachable` (the App can't list its
    installs while ≥1 routed install exists → every background loop goes blind) and, when most of the fleet
    went behind=unknown at once, the DISTINCT `freshness_blind` — while `graph_stale` correctly stays quiet
    (the per-repo "don't page on what we can't see" rule is preserved). The watchdog stamps the reachability
    so /readyz can surface it without its own GitHub call.
  • edge-triggering + rate-limit: a SUSTAINED bad state pages ONCE within the window, not on every tick.
  • fail-open: a webhook that THROWS on POST never propagates out of fire().
  • content-free + secret-safe: a DSN accidentally passed in is redacted; no body/secret leaves.
  • ALWAYS logs an `ALERT[` line even with no webhook configured (log-based alerting still works).

DR (needs local Postgres with the veripsa roles): export the durable rows (core.event + core.statement) to
JSONL, restore into a FRESH bootstrapped DB, and assert the durable rows round-trip exactly. This is the
"a backup nobody restored is a hope" guard — the restore path is proven, not assumed.

Run:  python3 tests/test_ops_alerting.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import alerts  # noqa: E402
import backup_export  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_server.py.
DB = "veripsa_opsalerting_" + str(os.getpid())
SRC_ACCT = "ACCT-DEMO"


# ── a deterministic clock + a capturing fake webhook poster ──────────────────────────────────────────────
class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakePoster:
    def __init__(self, explode=False):
        self.posts = []
        self.explode = explode

    def __call__(self, url, body):
        if self.explode:
            raise OSError("webhook is down")
        self.posts.append((url, body))


def snap(worker_alive=True, depth=0, maxsize=1000, processed=0, failed=0):
    return {"worker_alive": worker_alive, "queue_depth": depth, "queue_maxsize": maxsize,
            "processed": processed, "failed": failed}


def alerting_checks():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # 1) worker death pages CRITICAL; recovery resolves; re-death pages again
    clk = FakeClock()
    poster = FakePoster()
    sink = alerts.AlertSink(webhook_url="https://hook.example/x", min_interval=900, clock=clk, poster=poster)
    fired = alerts.evaluate(sink, snap(worker_alive=False), db_reachable=True, prev_failed=0)
    check("worker_dead fires CRITICAL", any(p[1]["key"] == "worker_dead" and p[1]["level"] == "critical" for p in poster.posts))
    # recovery (worker alive again) → resolve → a recovery note, and the key re-arms
    poster.posts.clear()
    alerts.evaluate(sink, snap(worker_alive=True), db_reachable=True, prev_failed=fired)
    recovered = any(p[1]["key"] == "worker_dead" and "recover" in p[1]["text"].lower() for p in poster.posts)
    check("worker_dead resolves on recovery", recovered)
    poster.posts.clear()
    alerts.evaluate(sink, snap(worker_alive=False), db_reachable=True, prev_failed=fired)
    check("worker_dead re-pages after recovery (re-armed)", any(p[1]["key"] == "worker_dead" for p in poster.posts))

    # 2) edge-trigger + rate-limit: a SUSTAINED death within the window pages ONCE, not every tick
    clk2 = FakeClock()
    poster2 = FakePoster()
    sink2 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk2, poster=poster2)
    for _ in range(5):
        clk2.t += 30        # 5 ticks, 30s apart — all well within the 900s window
        alerts.evaluate(sink2, snap(worker_alive=False), db_reachable=True, prev_failed=0)
    dead_pages = [p for p in poster2.posts if p[1]["key"] == "worker_dead"]
    check("sustained outage pages ONCE within the window (not per-tick)", len(dead_pages) == 1)
    # after the window elapses it pages again (still down → still page)
    clk2.t += 1000
    alerts.evaluate(sink2, snap(worker_alive=False), db_reachable=True, prev_failed=0)
    check("re-pages after the rate-limit window elapses", len([p for p in poster2.posts if p[1]["key"] == "worker_dead"]) == 2)

    # 3) queue backlog fires at ≥50% and resolves on drain
    clk3 = FakeClock()
    poster3 = FakePoster()
    sink3 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk3, poster=poster3)
    alerts.evaluate(sink3, snap(depth=499), db_reachable=True, prev_failed=0)
    check("queue at 499/1000 does NOT alert (below 50%)", not any(p[1]["key"] == "queue_backlog" for p in poster3.posts))
    alerts.evaluate(sink3, snap(depth=500), db_reachable=True, prev_failed=0)
    check("queue at 500/1000 fires queue_backlog", any(p[1]["key"] == "queue_backlog" for p in poster3.posts))

    # Count-only alerting missed the real queue=11 / ~787s incident. One old
    # due row must warn independently of depth; scheduled future work is quiet.
    poster3age = FakePoster()
    sink3age = alerts.AlertSink(
        webhook_url="https://hook/x", min_interval=0, poster=poster3age)
    alerts.evaluate_delivery_depth(
        sink3age,
        {
            "queued": 11,
            "processing": 0,
            "failed": 0,
            "queued_due": 11,
            "queued_due_oldest_age_seconds": 130,
            "queued_max_attempts": 4,
            "fanout_active": 1,
            "fanout_remaining": 37,
            "fanout_oldest_progress_age_seconds": 125,
        },
        queued_age_seconds=120,
    )
    latency_pages = [
        post for post in poster3age.posts
        if post[1]["key"] == "delivery_latency_slo"
    ]
    check(
        "queue=11 warns once its oldest due row exceeds the response SLO",
        len(latency_pages) == 1
        and latency_pages[0][1]["level"] == "warning"
        and latency_pages[0][1]["fields"]["fanout_remaining"] == 37,
    )
    future_poster = FakePoster()
    future_sink = alerts.AlertSink(
        webhook_url="https://hook/x", min_interval=0, poster=future_poster)
    alerts.evaluate_delivery_depth(
        future_sink,
        {
            "queued": 11,
            "processing": 0,
            "failed": 0,
            "queued_due": 0,
            "queued_due_oldest_age_seconds": 0,
        },
        queued_age_seconds=120,
    )
    check(
        "future not_before rows do not fire the due-queue response SLO",
        not any(
            post[1]["key"] == "delivery_latency_slo"
            for post in future_poster.posts
        ),
    )
    alerts.evaluate_delivery_depth(
        sink3age,
        {
            "queued": 0,
            "processing": 0,
            "failed": 0,
            "queued_due": 0,
            "queued_due_oldest_age_seconds": 0,
        },
        queued_age_seconds=120,
    )
    check(
        "delivery latency warning resolves and re-arms when due work drains",
        any(
            post[1]["key"] == "delivery_latency_slo"
            and "recover" in post[1]["text"].lower()
            for post in poster3age.posts
        ),
    )

    # 3b) durable processing alert defaults to the same window as stale reclaim. A shorter alert window pages on
    # rows recovery is not yet willing to reclaim, which looks like a real incident during normal catch-up.
    old_stale = os.environ.get("VERIPSA_DELIVERY_STALE_SECONDS")
    old_stuck = os.environ.get("VERIPSA_DURABLE_PROCESSING_STUCK_SECONDS")
    try:
        os.environ["VERIPSA_DELIVERY_STALE_SECONDS"] = "1800"
        os.environ.pop("VERIPSA_DURABLE_PROCESSING_STUCK_SECONDS", None)
        poster3b = FakePoster()
        sink3b = alerts.AlertSink(webhook_url="https://hook/x", min_interval=0, poster=poster3b)
        for age in (600, 650):
            alerts.evaluate_delivery_depth(sink3b, {"processing": 3, "queued": 0, "failed": 0,
                                                    "processing_oldest_age_seconds": age},
                                           processing_stuck=1)
        check("delivery_processing_stuck default waits for stale reclaim, not the old 300s page",
              not any(p[1]["key"] == "delivery_processing_stuck" for p in poster3b.posts))
        for age in (1900, 1910):
            alerts.evaluate_delivery_depth(sink3b, {"processing": 3, "queued": 0, "failed": 0,
                                                    "processing_oldest_age_seconds": age},
                                           processing_stuck=1)
        check("delivery_processing_stuck still pages after the stale-reclaim window is sustained",
              any(p[1]["key"] == "delivery_processing_stuck" for p in poster3b.posts))

        os.environ["VERIPSA_DURABLE_PROCESSING_STUCK_SECONDS"] = "300"
        poster3c = FakePoster()
        sink3c = alerts.AlertSink(webhook_url="https://hook/x", min_interval=0, poster=poster3c)
        for age in (600, 650):
            alerts.evaluate_delivery_depth(sink3c, {"processing": 3, "queued": 0, "failed": 0,
                                                    "processing_oldest_age_seconds": age},
                                           processing_stuck=1)
        check("explicit VERIPSA_DURABLE_PROCESSING_STUCK_SECONDS keeps the operator override",
              any(p[1]["key"] == "delivery_processing_stuck" for p in poster3c.posts))
    finally:
        if old_stale is None:
            os.environ.pop("VERIPSA_DELIVERY_STALE_SECONDS", None)
        else:
            os.environ["VERIPSA_DELIVERY_STALE_SECONDS"] = old_stale
        if old_stuck is None:
            os.environ.pop("VERIPSA_DURABLE_PROCESSING_STUCK_SECONDS", None)
        else:
            os.environ["VERIPSA_DURABLE_PROCESSING_STUCK_SECONDS"] = old_stuck

    # 3d) LANE-FROZEN narration (2026-07-17 incident): one orphaned 'processing' row freezes its account/repo
    # causal lane for the whole stale window while delivery_backlog (needs 1000) and delivery_processing_stuck
    # (waits for the stale window, 3b above) both stay SILENT. The warning-grade narration fires well before
    # reclaim, names the reclaim ETA, and must not flap on a single long-but-live event (sustained >1 tick).
    poster3d = FakePoster()
    sink3d = alerts.AlertSink(webhook_url="https://hook/x", min_interval=0, poster=poster3d)
    alerts.evaluate_delivery_depth(sink3d, {"processing": 1, "queued": 41, "failed": 0,
                                            "processing_oldest_age_seconds": 400}, stale_seconds=1800)
    check("lane-frozen does NOT fire on the FIRST over-threshold tick (a single long event is not a freeze)",
          not any(p[1]["key"] == "delivery_lane_frozen" for p in poster3d.posts))
    alerts.evaluate_delivery_depth(sink3d, {"processing": 1, "queued": 41, "failed": 0,
                                            "processing_oldest_age_seconds": 430}, stale_seconds=1800)
    frozen = [p for p in poster3d.posts if p[1]["key"] == "delivery_lane_frozen"]
    check("lane-frozen fires WARNING on the sustained 2nd tick with the honest reclaim ETA (1800-430=1370s)",
          len(frozen) == 1 and frozen[0][1]["level"] == "warning"
          and frozen[0][1]["fields"].get("stale_reclaim_eta_seconds") == 1370
          and "Do NOT restart" in frozen[0][1]["text"])
    alerts.evaluate_delivery_depth(sink3d, {"processing": 0, "queued": 2, "failed": 0,
                                            "processing_oldest_age_seconds": 0}, stale_seconds=1800)
    check("lane-frozen RESOLVES once the lane drains (reclaim happened)",
          any(p[1]["key"] == "delivery_lane_frozen" and "recover" in p[1]["text"].lower()
              for p in poster3d.posts))
    # young rows never arm the edge: a normal in-flight event (age 45s) two ticks in a row stays quiet
    poster3e = FakePoster()
    sink3e = alerts.AlertSink(webhook_url="https://hook/x", min_interval=0, poster=poster3e)
    for age in (45, 50):
        alerts.evaluate_delivery_depth(sink3e, {"processing": 1, "queued": 5, "failed": 0,
                                                "processing_oldest_age_seconds": age}, stale_seconds=1800)
    check("a normal-age in-flight row (45-50s) never fires lane-frozen",
          not any(p[1]["key"] == "delivery_lane_frozen" for p in poster3e.posts))

    # 4) failed-event SPIKE fires on the delta since last sample
    clk4 = FakeClock()
    poster4 = FakePoster()
    sink4 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk4, poster=poster4)
    pf = alerts.evaluate(sink4, snap(failed=2), db_reachable=True, prev_failed=0)  # delta 2 < 5 → quiet
    check("small failed delta (2) does NOT spike", not any(p[1]["key"] == "failed_spike" for p in poster4.posts))
    alerts.evaluate(sink4, snap(failed=9), db_reachable=True, prev_failed=pf)      # delta 7 ≥ 5 → page
    check("failed delta of 7 fires failed_spike", any(p[1]["key"] == "failed_spike" for p in poster4.posts))

    # 5) DB unreachable while worker ALIVE → CRITICAL (the gap nothing else catches)
    clk5 = FakeClock()
    poster5 = FakePoster()
    sink5 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk5, poster=poster5)
    alerts.evaluate(sink5, snap(worker_alive=True), db_reachable=False, prev_failed=0)
    db_alert = [p for p in poster5.posts if p[1]["key"] == "db_unreachable"]
    check("db_unreachable fires CRITICAL even with worker alive", db_alert and db_alert[0][1]["level"] == "critical")

    # 6) FAIL-OPEN: a webhook that throws on POST never propagates out of fire()
    poster6 = FakePoster(explode=True)
    sink6 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=0, clock=FakeClock(), poster=poster6)
    threw = False
    try:
        alerts.evaluate(sink6, snap(worker_alive=False), db_reachable=False, prev_failed=0)
    except Exception:
        threw = True
    check("alerting is FAIL-OPEN (a throwing webhook never propagates)", not threw)

    # 7) content-free + secret-safe: a DSN passed as a 'message' is redacted before it leaves
    leaked = alerts.redact("connect postgresql://veripsa_app:supersecret@host/db failed")
    check("redact() strips a DSN (no secret in an alert body)", "supersecret" not in leaked and "<redacted>" in leaked)

    # 8) log-only mode: NO webhook configured → no POST, but an ALERT[ line is still emitted to stdout
    buf = io.StringIO()
    sink8 = alerts.AlertSink(webhook_url="", min_interval=0, clock=FakeClock(), poster=FakePoster())
    sink8._log_safe = lambda line: buf.write(line + "\n")  # capture the stdout line
    sink8.fire("worker_dead", "critical", "down", {"failed": 1})
    check("log-only mode still emits an ALERT[ line (works with no webhook)", "ALERT[critical] worker_dead" in buf.getvalue())
    check("alert body is content-free (only counts/condition; no code, no body)",
          all(k in ("service", "level", "key", "text", "fields", "ts") for p in poster.posts for k in p[1]))

    # 9) GRAPH-FRESHNESS alert: a coordinate BEHIND main HEAD pages; current/unknown stays quiet; recovery resolves.
    clk9 = FakeClock()
    poster9 = FakePoster()
    sink9 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk9, poster=poster9)
    # all current → no page
    alerts.evaluate_graph_freshness(sink9, [{"behind": False, "age_seconds": 10}])
    check("graph all-current does NOT page", not any(p[1]["key"] == "graph_stale" for p in poster9.posts))
    # HEAD unresolvable (behind=None) → never a false page (we don't page on what we can't see)
    alerts.evaluate_graph_freshness(sink9, [{"behind": None, "age_seconds": 99999}])
    check("graph behind=None (HEAD unknown) does NOT false-page", not any(p[1]["key"] == "graph_stale" for p in poster9.posts))
    # a coordinate confirmed BEHIND HEAD → page (a missed/lagged push left the graph stale)
    n_behind = alerts.evaluate_graph_freshness(sink9, [{"behind": True, "age_seconds": 120}], behind_count_threshold=1)
    stale_page = [p for p in poster9.posts if p[1]["key"] == "graph_stale"]
    check("graph BEHIND main HEAD fires graph_stale", n_behind == 1 and bool(stale_page))
    check("graph_stale alert is content-free (count + age, no repo/sha)",
          bool(stale_page) and set(stale_page[0][1]["fields"]).issubset(
              {"behind_count", "worst_age_seconds", "stale_threshold_seconds"}))

    # Missing evidence is Unknown, not recovery. A list-compatible sample can
    # carry explicit coverage metadata; legacy lists with behind=None are also
    # observationally incomplete for resolve purposes.
    class _IncompleteFreshness(list):
        coverage_complete = False
        cursor_healthy = True
        timed_out = False

    poster9.posts.clear()
    alerts.evaluate_graph_freshness(
        sink9, _IncompleteFreshness([{"behind": False, "age_seconds": 5}]))
    alerts.evaluate_graph_freshness(
        sink9, [{"behind": None, "age_seconds": 5}])
    check("partial/Unknown freshness evidence does NOT resolve an existing graph_stale alert",
          not poster9.posts)

    # recovery (all caught up again) → resolve + re-arm
    alerts.evaluate_graph_freshness(sink9, [{"behind": False, "age_seconds": 5}])
    check("graph_stale resolves once the graph catches up",
          any(p[1]["key"] == "graph_stale" and "recover" in p[1]["text"].lower() for p in poster9.posts))
    # PAST THE TIME BOUND: a behind coordinate older than stale_seconds escalates the message (drift persisted)
    clk10 = FakeClock()
    poster10 = FakePoster()
    sink10 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk10, poster=poster10)
    alerts.evaluate_graph_freshness(sink10, [{"behind": True, "age_seconds": 7200}],
                                    behind_count_threshold=1, stale_seconds=3600)
    esc = [p for p in poster10.posts if p[1]["key"] == "graph_stale"]
    check("graph stale PAST the time bound escalates the message (drift persisted, not self-healed)",
          bool(esc) and "NOT self-healed" in esc[0][1]["text"])
    # FAIL-OPEN: a non-list / missing freshness sample never raises
    threw_fresh = False
    try:
        alerts.evaluate_graph_freshness(sink10, None)
        alerts.evaluate_graph_freshness(sink10, "garbage")
    except Exception:
        threw_fresh = True
    check("graph-freshness eval is FAIL-OPEN on a missing/garbage sample", not threw_fresh)

    # 9b) ORPHAN/PHANTOM-COORDINATE FALSE-POSITIVE (audit): the past perpetual `graph_stale` came from an
    #     ABANDONED coordinate — a backfill/old-account graph_version for a (repo, branch) that is BEHIND main
    #     FOREVER (no PR will ever address that dead account, so the per-PR self-heal never fixes it), WHILE the
    #     LIVE coordinate for the SAME (repo, branch) is fresh/current and predictions actually run on it. The old
    #     eval paged on the orphan every interval → an un-actionable flood → the operator mutes graph_stale → a
    #     LATER REAL drift on another repo is then silently missed. Fix: a (repo, branch) that has ANY CURRENT
    #     (behind=False) record is being tracked fresh, so a behind=True record for that SAME (repo, branch) is a
    #     stale orphan/duplicate, NOT live drift — it must NOT page. A behind coordinate with NO fresh sibling is
    #     genuine drift and MUST still page.
    clk_orph = FakeClock(); poster_orph = FakePoster()
    sink_orph = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk_orph, poster=poster_orph)
    fresh_and_orphan = [
        {"repo": "acme/app", "branch": "main", "behind": False, "age_seconds": 30},      # LIVE coordinate, fresh
        {"repo": "acme/app", "branch": "main", "behind": True, "age_seconds": 999999},   # ORPHAN, behind forever
    ]
    n_orph = alerts.evaluate_graph_freshness(sink_orph, fresh_and_orphan, behind_count_threshold=1)
    check("ORPHAN behind-coordinate with a FRESH live sibling for the same (repo,branch) does NOT page graph_stale",
          n_orph == 0 and not any(p[1]["key"] == "graph_stale" for p in poster_orph.posts))

    # the GENUINE-drift coordinate (behind, NO fresh sibling) in the SAME sample MUST still page — the orphan
    # exclusion is surgical, not a blanket silence.
    clk_mix = FakeClock(); poster_mix = FakePoster()
    sink_mix = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk_mix, poster=poster_mix)
    mixed = [
        {"repo": "acme/app", "branch": "main", "behind": False, "age_seconds": 30},      # fresh sibling
        {"repo": "acme/app", "branch": "main", "behind": True, "age_seconds": 999999},   # orphan (excluded)
        {"repo": "acme/other", "branch": "main", "behind": True, "age_seconds": 7200},   # GENUINE drift (no sibling)
    ]
    n_mix = alerts.evaluate_graph_freshness(sink_mix, mixed, behind_count_threshold=1)
    mix_pages = [p for p in poster_mix.posts if p[1]["key"] == "graph_stale"]
    check("a GENUINE behind coordinate (no fresh sibling) STILL pages even when an orphan shares the sample",
          n_mix == 1 and bool(mix_pages) and mix_pages[0][1]["fields"]["behind_count"] == 1)

    # the orphan exclusion is keyed on (repo, branch): a behind coordinate on a DIFFERENT branch of a repo whose
    # OTHER branch is fresh is NOT excluded (each branch is its own coordinate — main being fresh says nothing
    # about a stale 'release' branch).
    clk_br = FakeClock(); poster_br = FakePoster()
    sink_br = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk_br, poster=poster_br)
    per_branch = [
        {"repo": "acme/app", "branch": "main", "behind": False, "age_seconds": 30},      # main fresh
        {"repo": "acme/app", "branch": "release", "behind": True, "age_seconds": 7200},  # release genuinely behind
    ]
    n_br = alerts.evaluate_graph_freshness(sink_br, per_branch, behind_count_threshold=1)
    check("orphan exclusion is per (repo,branch) — a stale OTHER branch still pages (main fresh != release fresh)",
          n_br == 1 and any(p[1]["key"] == "graph_stale" for p in poster_br.posts))

    # 10) OPERATOR DB-USAGE alert (the AWS-billing-alarm equivalent): the WHOLE DB filling toward its cap pages;
    #     under stays silent; misconfigured cap fails SAFE; unset webhook never crashes. Distinct from per-tenant.
    CAP = 100  # MiB — a tiny cap so the byte math is easy: 100 MiB = 104_857_600 bytes
    cap_bytes = CAP * 1024 * 1024

    def usage(total_bytes):
        return {"db_total_bytes": total_bytes, "cap_mb": CAP}

    # under the WARN line (50% < 70%) → silent
    clk11 = FakeClock(); poster11 = FakePoster()
    sink11 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk11, poster=poster11)
    alerts.evaluate_db_usage(sink11, usage(int(cap_bytes * 0.50)), cap_mb=CAP)
    check("db usage under the WARN line (50%) stays SILENT",
          not any(p[1]["key"] == "db_usage_high" for p in poster11.posts))

    # crossing the WARN line (75% ≥ 70%) → WARNING page
    alerts.evaluate_db_usage(sink11, usage(int(cap_bytes * 0.75)), cap_mb=CAP)
    warn_pages = [p for p in poster11.posts if p[1]["key"] == "db_usage_high"]
    check("db usage at 75% of cap fires db_usage_high WARNING",
          bool(warn_pages) and warn_pages[0][1]["level"] == "warning")
    check("db_usage_high alert is content-free (only percent + counts; no table rows, no path, no id)",
          bool(warn_pages) and set(warn_pages[0][1]["fields"]).issubset(
              {"pct_used", "warn_pct", "critical_pct", "db_total_bytes", "cap_mb"}))

    # EDGE-TRIGGERED + RATE-LIMITED: a sustained over-WARN state within the window pages ONCE, not per-tick
    clk12 = FakeClock(); poster12 = FakePoster()
    sink12 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk12, poster=poster12)
    for _ in range(5):
        clk12.t += 30
        alerts.evaluate_db_usage(sink12, usage(int(cap_bytes * 0.75)), cap_mb=CAP)
    check("sustained over-WARN pages ONCE within the window (edge-triggered, not spammy)",
          len([p for p in poster12.posts if p[1]["key"] == "db_usage_high"]) == 1)

    # escalation to CRITICAL once over the critical line (95% ≥ 90%)
    clk13 = FakeClock(); poster13 = FakePoster()
    sink13 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk13, poster=poster13)
    alerts.evaluate_db_usage(sink13, usage(int(cap_bytes * 0.95)), cap_mb=CAP)
    crit = [p for p in poster13.posts if p[1]["key"] == "db_usage_high"]
    check("db usage at 95% of cap fires db_usage_high CRITICAL",
          bool(crit) and crit[0][1]["level"] == "critical")

    # recovery: usage drops back below WARN → resolve + re-arm (the next crossing pages again)
    poster13.posts.clear()
    alerts.evaluate_db_usage(sink13, usage(int(cap_bytes * 0.30)), cap_mb=CAP)
    check("db_usage_high RESOLVES once usage drops back below the WARN line",
          any(p[1]["key"] == "db_usage_high" and "recover" in p[1]["text"].lower() for p in poster13.posts))

    # UNCAPPED (cap 0 / unset) → silent no-op even at a huge size (no cap → no percentage → no false page)
    clk14 = FakeClock(); poster14 = FakePoster()
    sink14 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk14, poster=poster14)
    alerts.evaluate_db_usage(sink14, usage(10 * 1024 ** 3), cap_mb=0)
    check("UNCAPPED instance (cap 0) never pages (no ceiling → no percentage to cross)",
          not any(p[1]["key"] == "db_usage_high" for p in poster14.posts))

    # MISCONFIGURED CAP via env (non-int / negative) FAILS SAFE: the knob reader returns the safe default (0 =
    # uncapped), so a typo SILENCES the alert rather than crashing the watchdog or inventing a breach.
    saved = os.environ.get("VERIPSA_DB_SIZE_CAP_MB")
    threw_cfg = False
    try:
        os.environ["VERIPSA_DB_SIZE_CAP_MB"] = "100MB-oops"     # non-int
        cap_a = alerts._db_size_cap_mb()
        os.environ["VERIPSA_DB_SIZE_CAP_MB"] = "-5"             # negative (out of range)
        cap_b = alerts._db_size_cap_mb()
    except Exception:
        threw_cfg = True
    finally:
        if saved is None:
            os.environ.pop("VERIPSA_DB_SIZE_CAP_MB", None)
        else:
            os.environ["VERIPSA_DB_SIZE_CAP_MB"] = saved
    check("misconfigured VERIPSA_DB_SIZE_CAP_MB FAILS SAFE (caught → uncapped 0, never crashes)",
          (not threw_cfg) and cap_a == 0 and cap_b == 0)

    # UNSET WEBHOOK → no-op, no crash: an over-threshold sample with NO webhook still emits the ALERT[ log line
    # (greppable) but never POSTs and never raises.
    buf2 = io.StringIO()
    sink15 = alerts.AlertSink(webhook_url="", min_interval=0, clock=FakeClock(), poster=FakePoster())
    sink15._log_safe = lambda line: buf2.write(line + "\n")
    threw_nh = False
    try:
        alerts.evaluate_db_usage(sink15, usage(int(cap_bytes * 0.92)), cap_mb=CAP)
    except Exception:
        threw_nh = True
    check("unset webhook is a no-op no-crash (still logs an ALERT[ line for log-based alerting)",
          (not threw_nh) and "ALERT[critical] db_usage_high" in buf2.getvalue())

    # FAIL-OPEN on a missing/garbage surface: never raises (absence is not a breach)
    threw_us = False
    try:
        alerts.evaluate_db_usage(sink15, None, cap_mb=CAP)
        alerts.evaluate_db_usage(sink15, "garbage", cap_mb=CAP)
        alerts.evaluate_db_usage(sink15, {"db_total_bytes": "not-a-number"}, cap_mb=CAP)
    except Exception:
        threw_us = True
    check("db-usage eval is FAIL-OPEN on a missing/garbage sample", not threw_us)

    # 11) WORKER STUCK (one live keyed-pool lane exceeded the delivery ceiling).
    # Thread liveness alone is green and a low-traffic lane never reaches the
    # backlog threshold, so worker_stuck is the signal that cancellation failed.
    clk16 = FakeClock(); poster16 = FakePoster()
    sink16 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk16, poster=poster16)
    # a healthy IDLE worker (no event in flight) → inflight None → never pages, even at the lowest threshold
    alerts.evaluate(sink16, snap(worker_alive=True), db_reachable=True, prev_failed=0, worker_stuck_seconds=1)
    check("idle worker (inflight None) never false-pages worker_stuck",
          not any(p[1]["key"] == "worker_stuck" for p in poster16.posts))
    # a worker wedged BELOW the bound is still healthy (a legitimately slow ingest in progress) → no page
    alerts.evaluate(sink16, dict(snap(worker_alive=True), inflight_age_seconds=300),
                    db_reachable=True, prev_failed=0, worker_stuck_seconds=600)
    check("worker in-flight BELOW the stuck bound (a slow-but-progressing ingest) does NOT page",
          not any(p[1]["key"] == "worker_stuck" for p in poster16.posts))
    # wedged AT/OVER the bound while ALIVE → CRITICAL worker_stuck, and worker_dead stays quiet (thread is alive)
    alerts.evaluate(sink16, dict(snap(worker_alive=True, processed=7), inflight_age_seconds=900),
                    db_reachable=True, prev_failed=0, worker_stuck_seconds=600)
    stuck_pages = [p for p in poster16.posts if p[1]["key"] == "worker_stuck"]
    check("worker ALIVE but wedged ≥ bound fires CRITICAL worker_stuck",
          bool(stuck_pages) and stuck_pages[0][1]["level"] == "critical")
    check("worker_stuck does NOT also fire worker_dead (thread is alive — no double-page)",
          not any(p[1]["key"] == "worker_dead" for p in poster16.posts))
    check("worker_stuck alert is content-free (only age + counts; no repo/path/body)",
          bool(stuck_pages) and set(stuck_pages[0][1]["fields"]).issubset(
              {"inflight_age_seconds", "stuck_threshold_seconds", "queue_depth", "processed",
               "alive_workers", "worker_count"}))
    # a DEAD worker is covered by worker_dead, not worker_stuck (even if a stale inflight age lingers) — no double-page
    clk17 = FakeClock(); poster17 = FakePoster()
    sink17 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk17, poster=poster17)
    alerts.evaluate(sink17, dict(snap(worker_alive=False), inflight_age_seconds=9999),
                    db_reachable=True, prev_failed=0, worker_stuck_seconds=600)
    check("a DEAD worker pages worker_dead (not worker_stuck — no double-page on the same outage)",
          any(p[1]["key"] == "worker_dead" for p in poster17.posts)
          and not any(p[1]["key"] == "worker_stuck" for p in poster17.posts))
    # recovery: the wedged event finished (inflight back to None) → worker_stuck RESOLVES + re-arms
    poster16.posts.clear()
    alerts.evaluate(sink16, snap(worker_alive=True, processed=8), db_reachable=True, prev_failed=0)
    check("worker_stuck RESOLVES once the wedged event clears (inflight None)",
          any(p[1]["key"] == "worker_stuck" and "recover" in p[1]["text"].lower() for p in poster16.posts))
    # FAIL-SAFE knob: malformed values use the hard derived envelope, and a
    # stale high override cannot delay the only signal for one isolated stuck
    # lane. Operators may still lower the page threshold.
    saved_stuck_env = {
        key: os.environ.get(key)
        for key in (
            "VERIPSA_WORKER_STUCK_SECONDS",
            "VERIPSA_EVENT_WALL_TIMEOUT_SECONDS",
            "VERIPSA_EVENT_TERMINAL_RESERVE_SECONDS",
            "VERIPSA_DELIVERY_RETRY_WINDOW_SECONDS",
            "VERIPSA_DURABLE_INBOX",
        )
    }
    threw_ws = False
    try:
        os.environ["VERIPSA_EVENT_WALL_TIMEOUT_SECONDS"] = "90"
        os.environ["VERIPSA_EVENT_TERMINAL_RESERVE_SECONDS"] = "5"
        os.environ["VERIPSA_DELIVERY_RETRY_WINDOW_SECONDS"] = "120"
        os.environ["VERIPSA_DURABLE_INBOX"] = "1"
        os.environ["VERIPSA_WORKER_STUCK_SECONDS"] = "ten-minutes-oops"   # non-int
        ws_default = alerts._default_worker_stuck_seconds()
        os.environ["VERIPSA_WORKER_STUCK_SECONDS"] = "86400"
        ws_high = alerts._default_worker_stuck_seconds()
        os.environ["VERIPSA_WORKER_STUCK_SECONDS"] = "60"
        ws_low = alerts._default_worker_stuck_seconds()
        os.environ["VERIPSA_EVENT_WALL_TIMEOUT_SECONDS"] = "900"
        os.environ["VERIPSA_DELIVERY_RETRY_WINDOW_SECONDS"] = "120"
        os.environ["VERIPSA_WORKER_STUCK_SECONDS"] = "86400"
        ws_retry_bounded = alerts._default_worker_stuck_seconds()
        ws_restart_bounded = alerts._derived_worker_restart_seconds()
        os.environ["VERIPSA_DURABLE_INBOX"] = "0"
        ws_memory_only = alerts._default_worker_stuck_seconds()
        ws_memory_restart = alerts._derived_worker_restart_seconds()
    except Exception:
        threw_ws = True
    finally:
        for key, value in saved_stuck_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check("worker-stuck threshold fails safe, clamps overrides, and follows the smaller executable window",
          (not threw_ws)
          and ws_default == 120
          and ws_high == 120
          and ws_low == 60
          and ws_retry_bounded == 150
          and ws_restart_bounded == 210
          and ws_memory_only == 930
          and ws_memory_restart == 990)

    # 12) NOTHING-MONITORS-THE-MONITOR: run_watchdog's loop must SURVIVE a tick that raises. The watchdog_tick is
    #     fail-open per sub-check, but ONE unhandled defect anywhere in the loop body would silently kill the
    #     watchdog THREAD → all proactive alerting stops while /healthz stays green. Monkeypatch watchdog_tick to
    #     ALWAYS raise; the loop must keep ticking and NEVER propagate, and a liveness heartbeat must surface so a
    #     stopped/raising watchdog is observable on /healthz. Pure (no DB, no real sleep — interval 0).
    import importlib
    hw = importlib.import_module("health_watchdog")
    saved_tick = hw.watchdog_tick
    saved_hb = hw._LAST_WATCHDOG_TICK_AT
    try:
        boom_calls = {"n": 0}
        def _boom_tick(sink, worker, db, prev_failed, gh=None, freshness_fn=None, run_cost=False):
            boom_calls["n"] += 1
            raise RuntimeError("synthetic watchdog tick defect")
        hw.watchdog_tick = _boom_tick
        hw._LAST_WATCHDOG_TICK_AT = None
        stops = {"n": 0}
        def _stop():
            stops["n"] += 1
            return stops["n"] > 4            # let ~4 iterations run, then stop the loop
        loop_threw = False
        try:
            hw.run_watchdog(sink=None, worker=None, db=None, interval=0, stop=_stop)
        except Exception:
            loop_threw = True
        check("run_watchdog NEVER raises out even when EVERY tick raises (the loop is crash-proof)", not loop_threw)
        check("the watchdog loop keeps ticking across a raising tick (the monitor survives its own defect)",
              boom_calls["n"] >= 3)
        check("a RAISING tick does not stamp the liveness heartbeat (a dead-tick watchdog is observable)",
              hw.watchdog_last_tick_seconds() is None)
        # a SUCCEEDING tick stamps the heartbeat → watchdog liveness is visible on /healthz
        hw.watchdog_tick = lambda sink, worker, db, prev_failed, gh=None, freshness_fn=None, run_cost=False: 0
        s2 = {"n": 0}
        hw.run_watchdog(sink=None, worker=None, db=None, interval=0,
                        stop=lambda: (s2.__setitem__("n", s2["n"] + 1) or s2["n"] > 1))
        hb_age = hw.watchdog_last_tick_seconds()
        check("a SUCCEEDING tick stamps the watchdog heartbeat (liveness surfaced for /healthz)",
              hb_age is not None and 0 <= hb_age < 5)
    finally:
        hw.watchdog_tick = saved_tick
        hw._LAST_WATCHDOG_TICK_AT = saved_hb

    # 13) APP-JWT REACHABILITY + FRESHNESS-BLIND (the LIVE 2026-06-26 gap): a systemic 403 on GET /app/installations
    #     makes the install map empty → for_account() returns None for EVERY tenant → freshness / boot-reconcile /
    #     cross-repo discovery all go blind (every coordinate degrades to behind=unknown, which graph_stale CORRECTLY
    #     never pages on). It was SILENT (/healthz green, /readyz only checked DB-side identity). Two new evaluators
    #     make it LOUD: app_jwt_unreachable (the App can't list installs while ≥1 routed install exists) and the
    #     DISTINCT freshness_blind (most of the fleet went unknown AT THE SAME TIME — systemic, not a missed push).
    clk_aj = FakeClock(); poster_aj = FakePoster()
    sink_aj = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk_aj, poster=poster_aj)
    # unreachable App-JWT WHILE ≥1 routed install exists → CRITICAL app_jwt_unreachable
    alerts.evaluate_app_reachability(sink_aj, reachable=False, installation_rows=2)
    aj = [p for p in poster_aj.posts if p[1]["key"] == "app_jwt_unreachable"]
    check("app_jwt_unreachable fires CRITICAL when the App can't list installs while installs exist",
          bool(aj) and aj[0][1]["level"] == "critical")
    check("app_jwt_unreachable alert is content-free (only a boolean + counts; no tenant id/org/token)",
          bool(aj) and set(aj[0][1]["fields"]).issubset({"app_jwt_reachable", "installation_rows", "min_installs"}))
    # ZERO routed installs → a brand-new / fully-uninstalled App has nothing to serve → never pages
    clk_aj0 = FakeClock(); poster_aj0 = FakePoster()
    sink_aj0 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk_aj0, poster=poster_aj0)
    alerts.evaluate_app_reachability(sink_aj0, reachable=False, installation_rows=0)
    check("app_jwt_unreachable does NOT page a zero-install App (nothing to serve → not a degraded service)",
          not any(p[1]["key"] == "app_jwt_unreachable" for p in poster_aj0.posts))
    # recovery: reachable again → resolve + re-arm
    poster_aj.posts.clear()
    alerts.evaluate_app_reachability(sink_aj, reachable=True, installation_rows=2)
    check("app_jwt_unreachable RESOLVES once the App can list installs again",
          any(p[1]["key"] == "app_jwt_unreachable" and "recover" in p[1]["text"].lower() for p in poster_aj.posts))
    # a probe that was NOT TAKEN (reachable=None — no gh client) is a NO-OP (we never page on a probe we didn't run)
    clk_ajn = FakeClock(); poster_ajn = FakePoster()
    sink_ajn = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk_ajn, poster=poster_ajn)
    alerts.evaluate_app_reachability(sink_ajn, reachable=None, installation_rows=9)
    check("app_jwt reachability with reachable=None (probe not run) is a no-op (no page, no resolve note)",
          len(poster_ajn.posts) == 0)

    # FRESHNESS-BLIND: a high fraction of behind=unknown WHILE the App-JWT is unreachable → systemic blindness page
    clk_fb = FakeClock(); poster_fb = FakePoster()
    sink_fb = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk_fb, poster=poster_fb)
    alerts.evaluate_freshness_blind(sink_fb, total=10, behind_none=8, app_reachable=False)
    fb = [p for p in poster_fb.posts if p[1]["key"] == "freshness_blind"]
    check("freshness_blind fires when most coordinates are behind=unknown AND the App-JWT is unreachable",
          bool(fb) and fb[0][1]["level"] == "warning")
    check("freshness_blind alert is content-free (only counts + fractions; no repo/sha/path)",
          bool(fb) and set(fb[0][1]["fields"]).issubset({"behind_none", "total", "blind_fraction", "blind_threshold"}))
    # the SAME high-unknown sample but the App-JWT is REACHABLE → behind=unknown is a normal per-repo unknown, NOT
    # systemic blindness → no page (this is the line that keeps freshness_blind from double-counting graph_stale's job)
    clk_fbr = FakeClock(); poster_fbr = FakePoster()
    sink_fbr = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk_fbr, poster=poster_fbr)
    alerts.evaluate_freshness_blind(sink_fbr, total=10, behind_none=8, app_reachable=True)
    check("freshness_blind stays QUIET when the App-JWT is reachable (per-repo unknown is normal, not blindness)",
          not any(p[1]["key"] == "freshness_blind" for p in poster_fbr.posts))
    # a FEW unknowns (below the blind fraction) while unreachable → not blindness → no page
    clk_fbl = FakeClock(); poster_fbl = FakePoster()
    sink_fbl = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk_fbl, poster=poster_fbl)
    alerts.evaluate_freshness_blind(sink_fbl, total=10, behind_none=1, app_reachable=False)
    check("freshness_blind stays QUIET below the blind fraction (a couple of unknowns is not systemic blindness)",
          not any(p[1]["key"] == "freshness_blind" for p in poster_fbl.posts))
    # FAIL-OPEN: garbage/None inputs never raise (a sample error must never take the watchdog down)
    threw_aj = False
    try:
        alerts.evaluate_app_reachability(sink_fbl, reachable=False, installation_rows="garbage")
        alerts.evaluate_freshness_blind(sink_fbl, total="x", behind_none=None, app_reachable=False)
        alerts.evaluate_freshness_blind(sink_fbl, total=0, behind_none=0, app_reachable=False)
    except Exception:
        threw_aj = True
    check("app-reachability / freshness-blind evals are FAIL-OPEN on garbage/empty input", not threw_aj)

    # the WATCHDOG PROBE (health_watchdog.app_jwt_reachability_probe): current clients use a cached,
    # constant-time GET /app identity point read; legacy fakes below preserve the old map-helper compatibility.
    # The result is stamped so /readyz can surface it without its own GitHub call.
    import importlib
    hw2 = importlib.import_module("health_watchdog")

    class _GH403:
        def app_installations_reachability(self):
            return {"reachable": False, "installations_count": None}

    class _GHok:
        def app_installations_reachability(self):
            return {"reachable": True, "installations_count": 1}

    class _GHempty:
        def app_installations_reachability(self):
            return {"reachable": True, "installations_count": 0}

    def _db_installs(n):
        def _db(sql, args=()):
            return n if "list_installation_ids" in sql else None
        return _db

    reach403, rows403 = hw2.app_jwt_reachability_probe(_GH403(), _db_installs(2))
    check("probe: a cached/current install-map failure → reachable False + the DB routed-install count",
          reach403 is False and rows403 == 2)
    reach_ok, rows_ok = hw2.app_jwt_reachability_probe(_GHok(), _db_installs(1))
    check("probe: a successful non-empty list → reachable True", reach_ok is True and rows_ok == 1)
    reach_empty, rows_empty = hw2.app_jwt_reachability_probe(_GHempty(), _db_installs(2))
    check("probe: an empty App-installations map while Core has routed installs is treated as unreachable",
          reach_empty is False and rows_empty == 2)
    reach_none, rows_none = hw2.app_jwt_reachability_probe(None, _db_installs(2))
    check("probe: no gh client → (None, None) (the probe was not taken)",
          reach_none is None and rows_none is None)
    # a DEFINITE probe stamps app_jwt_reachable() for /readyz; a None probe leaves the prior value untouched
    saved_ajr = hw2._APP_JWT_REACHABLE
    try:
        hw2._APP_JWT_REACHABLE = None
        # Run a full tick under an App-JWT failure against the REAL GitHubREST point-probe path. Production
        # freshness carries exact durable installation ids, so neither freshness nor watchdog may enumerate the
        # fleet just to determine App identity health.
        from github_rest import GitHubREST

        gh_live = GitHubREST("1", "unused-private-key", "123")
        rec = {"app_calls": 0, "list_calls": 0, "mode": "fail"}

        def _list_installs():
            rec["list_calls"] += 1
            raise AssertionError(
                "watchdog point-probe must not enumerate App installations")

        gh_live.list_app_installations = _list_installs

        def _app_registration():
            rec["app_calls"] += 1
            if rec["mode"] == "fail":
                raise RuntimeError("HTTP 403 on /app")
            return {"id": 1}

        gh_live.get_app_registration = _app_registration

        clk_t = FakeClock(); poster_t = FakePoster()
        sink_t = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk_t, poster=poster_t)

        class _W:
            def is_alive(self): return True
            def inflight_age(self): return None
            def retried(self): return 0
            def qsize(self): return 0
            def maxsize(self): return 1000
            def processed(self): return 5
            def failed(self): return 0
            def uptime(self): return 100.0

        def _db_tick(sql, args=()):
            if "list_installation_ids" in sql: return 2
            return None

        def _fresh_all_unknown(_db, _gh):
            return [{"repo": "a/b", "branch": "main", "behind": None, "age_seconds": 10},
                    {"repo": "a/c", "branch": "main", "behind": None, "age_seconds": 20},
                    {"repo": "a/d", "branch": "main", "behind": None, "age_seconds": 30}]

        hw2.watchdog_tick(sink_t, _W(), _db_tick, prev_failed=0, gh=gh_live,
                          freshness_fn=_fresh_all_unknown)
        tick_keys = set(p[1]["key"] for p in poster_t.posts)
        check("a full watchdog_tick under the 403 scenario fires app_jwt_unreachable + freshness_blind",
              "app_jwt_unreachable" in tick_keys and "freshness_blind" in tick_keys)
        check("the 403 tick does NOT fire graph_stale (behind=unknown never pages — the per-repo rule is preserved)",
              "graph_stale" not in tick_keys)
        check("the watchdog STAMPS app_jwt_reachable() so /readyz can surface it without a GitHub call",
              hw2.app_jwt_reachable() is False)
        check("the watchdog uses one constant-time App registration point read and zero fleet-list calls",
              rec["app_calls"] == 1 and rec["list_calls"] == 0)

        hw2.watchdog_tick(sink_t, _W(), _db_tick, prev_failed=0, gh=gh_live,
                          freshness_fn=_fresh_all_unknown)
        check("repeated watchdog ticks inside ACCOUNT_MAP_TTL_SECONDS reuse the App point-read result",
              rec["app_calls"] == 1 and rec["list_calls"] == 0)

        gh_live._app_registration_probe_at -= (
            gh_live.ACCOUNT_MAP_TTL_SECONDS + 1)
        hw2.watchdog_tick(sink_t, _W(), _db_tick, prev_failed=0, gh=gh_live,
                          freshness_fn=_fresh_all_unknown)
        check("after the shared TTL expires, the next tick retries exactly one App point read",
              rec["app_calls"] == 2 and rec["list_calls"] == 0)

        rec["mode"] = "ok"
        gh_live._app_registration_probe_at -= (
            gh_live.ACCOUNT_MAP_TTL_SECONDS + 1)
        hw2.watchdog_tick(sink_t, _W(), _db_tick, prev_failed=0, gh=gh_live,
                          freshness_fn=_fresh_all_unknown)
        check("a later successful App identity point read resolves the stamped App-JWT reachability",
              rec["app_calls"] == 3 and rec["list_calls"] == 0
              and hw2.app_jwt_reachable() is True)
    finally:
        hw2._APP_JWT_REACHABLE = saved_ajr

    # N) escalate_value: a STANDING gauge (e.g. N permanently-expired deliveries past GitHub's window) must
    # page ONCE, then stay quiet while stable/shrinking — but page again on a STRICT increase even inside the
    # re-arm window; resolve() re-arms. This kills the every-tick critical flap for an unactionable scar while
    # still surfacing a worsening loss immediately. Default (no escalate_value) time-re-arm is unchanged.
    clkE = FakeClock()
    posterE = FakePoster()
    sinkE = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clkE, poster=posterE)
    sinkE.fire("scar", "critical", "10 lost", {"expired_unrecovered": 10}, escalate_value=10)
    check("escalate: first appearance pages", any(p[1]["key"] == "scar" for p in posterE.posts))
    posterE.posts.clear()
    sinkE.fire("scar", "critical", "10 lost", {"expired_unrecovered": 10}, escalate_value=10)  # stable, same tick
    check("escalate: stable gauge is SUPPRESSED (no re-page every tick)", not posterE.posts)
    clkE.t += 100000  # far past the 900s re-arm window — time alone must NOT re-page a stable scar
    sinkE.fire("scar", "critical", "10 lost", {"expired_unrecovered": 10}, escalate_value=10)
    check("escalate: stable gauge stays quiet even past the time window", not posterE.posts)
    sinkE.fire("scar", "critical", "9 lost", {"expired_unrecovered": 9}, escalate_value=9)  # shrank
    check("escalate: a SHRINKING gauge does not page", not posterE.posts)
    sinkE.fire("scar", "critical", "12 lost", {"expired_unrecovered": 12}, escalate_value=12)  # worsened
    check("escalate: a STRICT increase pages again (worsening loss)", any(p[1]["key"] == "scar" for p in posterE.posts))
    posterE.posts.clear()
    sinkE.resolve("scar")
    sinkE.fire("scar", "critical", "3 lost", {"expired_unrecovered": 3}, escalate_value=3)
    check("escalate: resolve() re-arms the value gate (recurrence pages)",
          any(p[1]["key"] == "scar" for p in posterE.posts))
    # default path (no escalate_value) must keep the original TIME-based re-arm untouched
    clkD = FakeClock()
    posterD = FakePoster()
    sinkD = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clkD, poster=posterD)
    sinkD.fire("t", "warning", "x", {})
    posterD.posts.clear()
    sinkD.fire("t", "warning", "x", {})  # within window
    within = not posterD.posts
    clkD.t += 901  # past window
    sinkD.fire("t", "warning", "x", {})
    check("default (no escalate_value) keeps time-based re-arm", within and bool(posterD.posts))

    return results


# ── DR export / restore round-trip (needs Postgres) ──────────────────────────────────────────────────────
def dr_checks():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    owner = f"postgresql://veripsa_migrator@localhost/{DB}"

    # bootstrap a source DB and seed a couple of durable rows (a push event + a statement), as the owner,
    # with RLS pinned to the demo account (FORCE RLS walls the owner to exactly that account).
    import psycopg2
    conn = psycopg2.connect(owner)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, false)", (SRC_ACCT,))
            cur.execute("SELECT set_config('core.installation_account', %s, false)", (SRC_ACCT,))
            # register the demo account in the installation map so the DR gate (which enumerates tenants via
            # core.installation_account, the no-RLS install→account map) discovers it — a real GH tenant lands
            # here on its first event (enter_installation); the seat-provisioned demo account never does, so the
            # fixture wires it explicitly. Owner writes the no-RLS map directly.
            cur.execute("INSERT INTO core.installation_account (installation_id, account_id) "
                        "VALUES ('INST-DR-DEMO', %s) ON CONFLICT DO NOTHING", (SRC_ACCT,))
            # seed two durable rows as the owner. core.event / core.statement are forgery-blocked (only the
            # gate may write), so arm the governed-write token the gate arms — the legitimate owner-level way
            # to lay down a fixture row (mark_governed_write is REVOKEd from PUBLIC; only the owner can arm it).
            cur.execute("SELECT core.mark_governed_write('event')")
            cur.execute(
                "INSERT INTO core.event (event_id, account_id, kind, agent_id, repo, branch, commit_sha, detail) "
                "VALUES ('EV-DR-1', %s, 'push', 'AG-A', 'acme/dr', 'main', 'abc1234', 'dr-fixture') "
                "ON CONFLICT DO NOTHING", (SRC_ACCT,))
            cur.execute("SELECT core.mark_governed_write('statement')")
            cur.execute(
                "INSERT INTO core.statement (statement_id, account_id, agent_id, utterance, about_repo, about_path) "
                "VALUES ('ST-DR-1', %s, 'AG-A', 'a stated meaning', 'acme/dr', 'app/x.py') "
                "ON CONFLICT DO NOTHING", (SRC_ACCT,))
    finally:
        conn.close()

    before = backup_export.count(owner)
    check("source DB has the seeded durable rows", before["events"] >= 1 and before["statements"] >= 1)

    # export to JSONL
    buf = io.StringIO()
    exported = backup_export.export(owner, out=buf)
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    envelope = backup_export.verify(lines)
    parsed = backup_export.load_verified_records(lines)
    n_durable = sum(1 for p in parsed if p.get("_table") in backup_export.DURABLE_TABLES)
    check("versioned export footer exactly reconciles every durable row (+ tenant registry)",
          n_durable == sum(exported.values()) and exported["events"] >= 1
          and envelope["durable_counts"] == exported and envelope["records"] == len(parsed))
    check("every data record is valid JSON tagged with a known table",
          all(p.get("_table") in backup_export.TABLE_META for p in parsed))
    check("export is self-sufficient (carries identity registries plus the installation map)",
          all(any(p.get("_table") == table for p in parsed)
              for table in ("account", "agent", "credential", "installation_account")))
    # secret-safety: no DSN / password anywhere in the export
    blob = buf.getvalue()
    check("export is secret-free (no DSN / password in the backup)", "postgres://" not in blob and "password" not in blob.lower())

    # RESTORE into a FRESH bootstrapped DB (simulates 'the Postgres was lost')
    fresh = "veripsa_opsalerting_restore_" + str(os.getpid())  # PROCESS-UNIQUE — see DB note at top
    r = subprocess.run(["bash", "db/bootstrap_local.sh", fresh], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        check("fresh restore DB bootstrap", False)
        print(r.stderr[-600:])
        return results
    fresh_owner = f"postgresql://veripsa_migrator@localhost/{fresh}"
    try:
        # bootstrap_local provisions a demo fixture with fresh timestamps. A strict DR restore must not silently
        # swallow that same-PK/different-value collision, so make this the genuinely empty post-migration target
        # the runbook requires.
        cleanup = psycopg2.connect(fresh_owner)
        try:
            with cleanup, cleanup.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account',%s,true)", (SRC_ACCT,))
                cur.execute("DELETE FROM core.credential WHERE account_id=%s", (SRC_ACCT,))
                cur.execute("DELETE FROM core.installation_account WHERE account_id=%s", (SRC_ACCT,))
                cur.execute("DELETE FROM core.agent WHERE account_id=%s", (SRC_ACCT,))
                cur.execute("DELETE FROM core.account WHERE account_id=%s", (SRC_ACCT,))
        finally:
            cleanup.close()
        empty = backup_export.count(fresh_owner)
        check("fresh DB starts with no durable rows", all(value == 0 for value in empty.values()))
        restored = backup_export.import_jsonl(fresh_owner, lines)
        after = backup_export.count(fresh_owner)
        check("restore re-imported the durable rows into the fresh DB",
              all(after[key] == before[key] for key in before))
        check("restore counts match the export counts",
              all(restored[key] == exported[key] for key in exported))
        # idempotent only because every collision is value-identical.
        again = backup_export.import_jsonl(fresh_owner, lines)
        after2 = backup_export.count(fresh_owner)
        check("restore is idempotent (re-run adds no rows)",
              after2 == after)
        # the restored row is verifiably the SAME durable fact (content round-trips)
        c2 = psycopg2.connect(fresh_owner)
        try:
            with c2, c2.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account', %s, false)", (SRC_ACCT,))
                cur.execute("SELECT kind, repo, commit_sha, detail FROM core.event WHERE event_id='EV-DR-1'")
                row = cur.fetchone()
        finally:
            c2.close()
        check("the restored event round-trips byte-for-byte",
              row == ("push", "acme/dr", "abc1234", "dr-fixture"))
        # the append-only guard is INTACT on the restored DB (a plain DELETE is still refused)
        c3 = psycopg2.connect(fresh_owner)
        refused = False
        try:
            with c3.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account', %s, false)", (SRC_ACCT,))
                try:
                    cur.execute("DELETE FROM core.event WHERE event_id='EV-DR-1'")
                    c3.commit()
                except Exception:
                    refused = True
                    c3.rollback()
        finally:
            c3.close()
        check("append-only guard intact after restore (plain DELETE still refused)", refused)
    finally:
        subprocess.run(["dropdb", fresh], capture_output=True)

    # MOAT: only the dedicated backup principal may cross tenants. Neither a buyer nor the live App identity gets
    # this capability; restore separately remains owner-only.
    denied = False
    c4 = psycopg2.connect("postgresql://veripsa_demo_steward@localhost/" + DB)
    try:
        with c4.cursor() as cur:
            cur.execute("SET search_path=core")
            try:
                cur.execute("SELECT core.export_durable_rows_with_authority('')")
                cur.fetchone()
            except psycopg2.errors.InsufficientPrivilege:
                denied = True
                c4.rollback()
    finally:
        c4.close()
    check("MOAT: a buyer seat is DENIED the cross-tenant DR export (host-only)", denied)
    acl = psycopg2.connect(owner)
    try:
        with acl, acl.cursor() as cur:
            cur.execute(
                "SELECT has_schema_privilege('veripsa_backup','core','USAGE'), "
                "has_function_privilege('veripsa_backup','core.export_durable_rows_with_authority(text)','EXECUTE'), "
                "has_function_privilege('veripsa_app','core.export_durable_rows_with_authority(text)','EXECUTE'), "
                "has_table_privilege('veripsa_backup','core.event','SELECT'), "
                "has_table_privilege('veripsa_backup','core.credential','SELECT')"
            )
            backup_schema, backup_execute, app_execute, backup_event_select, backup_credential_select = cur.fetchone()
    finally:
        acl.close()
    check("least privilege: dedicated backup role has only the DR gate while live App has no export EXECUTE",
          backup_schema and backup_execute and not app_execute
          and not backup_event_select and not backup_credential_select)

    # OPERATOR DB-USAGE SURFACE (core.db_usage_surface): the real size read the watchdog samples. Run it as the
    # host (veripsa_app — the only role granted it) and assert it returns a sane, content-free shape, that the
    # cap → percent math is right, that an uncapped read yields pct_used null (never a /0), and that a buyer seat
    # is DENIED (operator-only). One bounded size query — cheap.
    capp = psycopg2.connect("postgresql://veripsa_app@localhost/" + DB)
    try:
        with capp, capp.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.db_usage_surface(0)")          # uncapped
            uncapped = cur.fetchone()[0]
            cur.execute("SELECT core.db_usage_surface(4096)")       # a 4 GiB cap
            capped = cur.fetchone()[0]
    finally:
        capp.close()
    check("db_usage_surface returns a positive whole-DB byte size",
          isinstance(uncapped, dict) and isinstance(uncapped.get("db_total_bytes"), int) and uncapped["db_total_bytes"] > 0)
    check("uncapped db_usage_surface yields pct_used NULL (no cap → no percentage, never a divide-by-zero)",
          uncapped.get("cap_mb") == 0 and uncapped.get("pct_used") is None)
    # capped: pct_used must equal total/cap, and top_tables must be a content-free list of {table,bytes,pct_of_db}
    cap_bytes_db = 4096 * 1024 * 1024
    expect_pct = round(capped["db_total_bytes"] * 100.0 / cap_bytes_db, 1)
    check("capped db_usage_surface computes pct_used = total / cap correctly",
          abs(float(capped.get("pct_used")) - expect_pct) < 0.2)
    tops = capped.get("top_tables")
    check("top_tables is a bounded list of content-free relation sizes (name + bytes + share — no rows)",
          isinstance(tops, list)
          and all(isinstance(t, dict) and set(t) == {"table", "bytes", "pct_of_db"}
                  and isinstance(t.get("bytes"), int) for t in tops))
    # content-free: the surface JSON must carry NO tenant id / repo / path / sha — only schema-qualified relation
    # names (the App's own DDL), byte sizes, and percentages. A tenant account id is 'ACCT-…' — assert it's absent.
    blob_usage = json.dumps(capped)
    check("db_usage_surface is content-free (no tenant id / account / repo / path leaks into the size view)",
          "ACCT-" not in blob_usage and SRC_ACCT not in blob_usage and "acme/" not in blob_usage)

    # OPERATOR-ONLY: a buyer seat (veripsa_demo_steward) is DENIED the whole-instance size view.
    usage_denied = False
    c5 = psycopg2.connect("postgresql://veripsa_demo_steward@localhost/" + DB)
    try:
        with c5.cursor() as cur:
            cur.execute("SET search_path=core")
            try:
                cur.execute("SELECT core.db_usage_surface(0)")
                cur.fetchone()
            except psycopg2.errors.InsufficientPrivilege:
                usage_denied = True
                c5.rollback()
    finally:
        c5.close()
    check("MOAT: a buyer seat is DENIED the operator DB-usage surface (host-only, like the cost lens)", usage_denied)

    # ── DURABLE ALERT BOARD — "an alert logged but ignored is meaningless" (the PO's principle). The AlertSink was
    #    in-memory + stdout + an OPTIONAL webhook: with no VERIPSA_ALERT_WEBHOOK_URL configured (the App default), an
    #    ACTIVE alert lived ONLY in the per-process Render log + the in-memory _firing set — invisible once nobody is
    #    tailing, GONE on the next deploy. The fix persists every fire/resolve to core.active_alert so the OWNER can
    #    SEE the live firing set via core.active_alerts_with_authority() (a notifier — email/dashboard — polls the
    #    same surface). Prove: (a) a fired alert lands on the owner board with its content-free fields; (b) resolve
    #    clears it; (c) the sink's persist SEAM drives those gated fns end-to-end; (d) a BUYER seat is DENIED the
    #    board (host-only). Run as veripsa_app (the watchdog identity, the only role granted the raise/clear fns).
    capp2 = psycopg2.connect("postgresql://veripsa_app@localhost/" + DB)
    try:
        with capp2, capp2.cursor() as cur:
            cur.execute("SET search_path=core")
            # FIRE two alerts the way the persist seam does (a counts-only, content-free fields object)
            cur.execute("SELECT core.raise_active_alert_with_authority('graph_stale','warning',%s,%s::jsonb)",
                        ("1 coordinate BEHIND main HEAD", '{"behind_count":1,"worst_age_seconds":380000}'))
            cur.execute("SELECT core.raise_active_alert_with_authority('db_usage_high','critical',%s,%s::jsonb)",
                        ("DB at 95% of cap", '{"pct_used":95}'))
            cur.execute("SELECT core.active_alerts_with_authority()")
            board = cur.fetchone()[0]
            # CLEAR one (a recovery) → it leaves the board, the other stays
            cur.execute("SELECT core.clear_active_alert_with_authority('db_usage_high')")
            cur.execute("SELECT core.active_alerts_with_authority()")
            board_after = cur.fetchone()[0]
    finally:
        capp2.close()
    keys = sorted(a["key"] for a in board.get("alerts", []))
    check("ALERT BOARD: a FIRED alert is durably persisted + readable by the owner (graph_stale + db_usage_high)",
          board.get("active_count") == 2 and keys == ["db_usage_high", "graph_stale"]
          and board.get("worst_level") == "critical")
    gs = next((a for a in board.get("alerts", []) if a["key"] == "graph_stale"), None)
    check("ALERT BOARD row is content-free (key + level + counts only; no repo/sha/path) and carries an age",
          gs is not None and set(gs.get("fields", {})) <= {"behind_count", "worst_age_seconds"}
          and isinstance(gs.get("age_seconds"), int) and "ACCT-" not in json.dumps(board))
    keys_after = sorted(a["key"] for a in board_after.get("alerts", []))
    check("ALERT BOARD: a RESOLVED alert is cleared from the board (only the still-firing one remains)",
          board_after.get("active_count") == 1 and keys_after == ["graph_stale"]
          and board_after.get("worst_level") == "warning")

    # the AlertSink PERSIST SEAM (the live wiring): a sink given a persist callable drives raise/clear on fire/
    # resolve, so the board reflects what the watchdog fired. Use a real veripsa_app connection as the seam's db.
    seam_calls = []
    cseam = psycopg2.connect("postgresql://veripsa_app@localhost/" + DB)
    try:
        cseam.autocommit = True
        def _persist(action, key, level, message, fields):
            seam_calls.append((action, key))
            with cseam.cursor() as cur:
                cur.execute("SET search_path=core")
                if action == "resolve":
                    cur.execute("SELECT core.clear_active_alert_with_authority(%s)", (key,))
                else:
                    cur.execute("SELECT core.raise_active_alert_with_authority(%s,%s,%s,%s::jsonb)",
                                (key, level, message, json.dumps(fields or {})))
        sink_p = alerts.AlertSink(webhook_url="", min_interval=0, clock=FakeClock(),
                                  poster=FakePoster(), persist=_persist)
        sink_p.fire("worker_dead", "critical", "the worker thread is not alive", {"failed": 3})
        sink_p.resolve("worker_dead")
        with cseam.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.active_alerts_with_authority()")
            board_seam = cur.fetchone()[0]
    finally:
        cseam.close()
    check("ALERT SINK persist SEAM drives the board end-to-end (fire→raise, resolve→clear)",
          ("fire", "worker_dead") in seam_calls and ("resolve", "worker_dead") in seam_calls
          and not any(a["key"] == "worker_dead" for a in board_seam.get("alerts", [])))

    persisted_payloads = []
    poster_persist = FakePoster()
    sink_san = alerts.AlertSink(webhook_url="https://hook/x", min_interval=0, clock=FakeClock(),
                                poster=poster_persist, persist=lambda *a: persisted_payloads.append(a))
    sink_san.fire("secret_shape", "warning",
                  "line1\npostgresql://user:pass@example/db",
                  {"dsn": "postgresql://user:pass@example/db",
                   "label": "first line\nsecond line",
                   "body": "x" * 500,
                   "nested": {"not": "scalar"}})
    _, _, _, persisted_message, persisted_fields = persisted_payloads[0]
    webhook_body = poster_persist.posts[0][1]
    check("ALERT SINK persist stores the SAME sanitized payload shape as the emitted alert",
          "pass@example" not in persisted_message and "\n" not in persisted_message
          and persisted_fields == webhook_body.get("fields")
          and persisted_fields.get("dsn") == "postgresql://<redacted>"
          and "\n" not in persisted_fields.get("label", "")
          and len(persisted_fields.get("body", "")) <= 200
          and "nested" not in persisted_fields)

    # FAIL-OPEN: a persist callable that THROWS must never propagate out of fire()/resolve() (a broken board can
    # never take alerting — or the server — down). The alert is still emitted to stdout/webhook.
    def _boom_persist(*a):
        raise RuntimeError("alert board is unreachable")
    sink_fo = alerts.AlertSink(webhook_url="", min_interval=0, clock=FakeClock(),
                               poster=FakePoster(), persist=_boom_persist)
    threw_persist = False
    try:
        sink_fo.fire("graph_stale", "warning", "x", {"behind_count": 1})
        sink_fo.resolve("graph_stale")
    except Exception:
        threw_persist = True
    check("ALERT SINK persist is FAIL-OPEN (a throwing board never propagates out of fire/resolve)", not threw_persist)

    # MOAT: a buyer seat is DENIED the owner alert board (host-only, like the cost/freshness lenses)
    board_denied = False
    cden = psycopg2.connect("postgresql://veripsa_demo_steward@localhost/" + DB)
    try:
        with cden.cursor() as cur:
            cur.execute("SET search_path=core")
            try:
                cur.execute("SELECT core.active_alerts_with_authority()")
                cur.fetchone()
            except psycopg2.errors.InsufficientPrivilege:
                board_denied = True
                cden.rollback()
    finally:
        cden.close()
    check("MOAT: a buyer seat is DENIED the owner alert board (host-only)", board_denied)

    return results


def main() -> int:
    print("== ops alerting (pure: watchdog/evaluate, no DB) ==")
    results = alerting_checks()

    # DR needs Postgres; bootstrap the source DB
    print("== DR export/restore round-trip (needs Postgres) ==")
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1
    try:
        results += dr_checks()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)

    ok = all(c for _, c in results)
    print("OPS-ALERTING GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
