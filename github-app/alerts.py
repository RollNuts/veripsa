#!/usr/bin/env python3
"""Veripsa — PROACTIVE ALERTING (the "does anyone KNOW it's broken?" primitive).

THE GAP THIS CLOSES: everything else in the App is PULL-based observability — /healthz exposes
`worker_alive / queue_depth / failed`, the worker prints `webhook worker FAILED: …`, the cron logs a
sweep line. ALL of it requires a human to ALREADY be looking (curl the probe, read the Render log). For a
PAID service that is silent failure: the worker can die, the queue can back up toward its 1000 cap, the
failed-event counter can spike, or the DB can go unreachable, and NOBODY is notified until a customer
complains. Render's own health check only restarts on a fully-dead worker — it does not tell us, and it
sees nothing of a backlog / failure spike / a DB outage where the worker is still alive.

WHAT THIS IS: the cheapest REAL alerting primitive — an outbound notification on a state change.
  • Sink: POST a tiny JSON to VERIPSA_ALERT_WEBHOOK_URL (a Slack / Discord / generic incoming-webhook URL —
    every team already has one; no new vendor, no API key beyond the URL itself which is the secret).
  • ALWAYS also emit a one-line `ALERT[level] key: message | <counts>` to stdout, so even with NO webhook
    configured the alert is greppable in the Render logs and a log-based alert (Render log alerts / an
    external log drain) can fire on the `ALERT[` prefix. So alerting degrades to "still in the logs",
    never to nothing.
  • FAIL-OPEN: a broken/slow/missing webhook NEVER throws into the caller. Alerting watching the server
    must not be able to take the server down. A POST is best-effort with a short timeout; any error is
    swallowed (and noted to stdout).

CONTENT-FREE (the moat, unchanged): an alert body carries ONLY the condition + the same counts already in
/healthz (queue_depth, failed, processed) and, for a worker-failure alert, the repo/account that the worker
log line already prints (public git metadata). NEVER code, file contents, request bodies, tokens, or the
DSN. `redact()` strips a DSN-shaped substring defensively before anything leaves the process.

EDGE-TRIGGERED + RATE-LIMITED: AlertSink.fire() de-dupes on (key) — it alerts on the TRANSITION into a bad
state and then stays quiet (a re-arm window, default 15 min) so a sustained outage is ONE page, not a
flood. A recovery (`resolve`) re-arms the key so the NEXT occurrence pages again.

ENV:
  VERIPSA_ALERT_WEBHOOK_URL   incoming-webhook URL to POST alerts to (Slack/Discord/generic). Unset = logs only.
  VERIPSA_ALERT_MIN_INTERVAL  seconds to suppress a repeat of the SAME alert key (default 900 = 15 min).
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request

# env_int is the ONE validated env reader (fail-safe-on-misconfig). It lives next to this module; import it the
# same dual way server.py imports alerts (flat dir on sys.path in tests/CLI; package-relative under the server).
try:
    from env_config import env_int, ConfigError  # type: ignore  # noqa: E402
except ImportError:  # pragma: no cover - exercised by the package layout, not the test layout
    from .env_config import env_int, ConfigError  # type: ignore  # noqa: E402

# A credential-bearing database URL must never leak into an alert body. Defensive only;
# the call sites already pass counts, not connection strings.
_DSN_RE = re.compile(r"postgres(?:ql)?://[^\s\"']+", re.IGNORECASE)


def redact(text: str) -> str:
    """Strip a DSN-shaped substring (belt-and-suspenders; alert bodies are built from counts, not secrets)."""
    return _DSN_RE.sub("postgresql://<redacted>", text or "")


# An alert field VALUE that is a string must stay a short, single-line LABEL — never a multi-line source-body
# run. Call sites only ever pass scalar counts, but the boundary defends against a future/hostile caller: a
# string field is collapsed to one line (whitespace runs → one space, control chars dropped) and capped, so a
# body fragment can never ride out in the JSON payload. CONTENT-FREE second layer (matches redact()'s spirit).
_FIELD_STR_CAP = 200
_ALERT_POST_QUEUE_CAP = 32


def _http_post_once(url: str, body: dict) -> None:
    """Best-effort blocking POST, called only by the isolated dispatcher."""
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read(1)
    except Exception as exc:
        # An incoming-webhook URL is itself a credential. urllib may include
        # the complete malformed/request URL in an exception message, so never
        # stringify transport errors. The exception class is enough to
        # distinguish configuration from network failures without leaking the
        # sink secret into Render logs.
        try:
            print(
                "ALERT-SINK POST failed "
                f"(alert still in logs above): {type(exc).__name__}",
                flush=True,
            )
        except Exception:
            pass


class _BoundedWebhookDispatcher:
    """One isolated outbound-alert worker with a bounded backlog.

    A socket timeout does not bound libc/NSS DNS. Alert emission can occur from
    a webhook path (for example an installation-cap signal), so doing the POST
    inline would let a stuck alert DNS lookup stop an event worker. One fixed
    daemon contains that failure; callers only perform ``put_nowait``. If the
    sink itself is stuck, later pages are still present in stdout/the durable
    alert board and this bounded queue drops instead of growing threads/memory.
    """

    def __init__(self, poster=_http_post_once, *, max_pending: int = _ALERT_POST_QUEUE_CAP):
        self._poster = poster
        self._queue = queue.Queue(maxsize=max(1, int(max_pending)))
        self._lock = threading.Lock()
        self._thread = None
        self._dropped = 0

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="veripsa-alert-webhook",
                daemon=True,
            )
            self._thread.start()

    def submit(self, url: str, body: dict) -> bool:
        self._ensure_started()
        try:
            self._queue.put_nowait((url, body))
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False

    def _run(self) -> None:
        while True:
            url, body = self._queue.get()
            try:
                self._poster(url, body)
            except Exception as exc:
                # Custom poster exceptions may also echo their URL argument.
                # Keep the dispatcher fail-open but log only a fixed type tag.
                try:
                    print(f"ALERT-SINK POST worker failed (alert remains on durable board): "
                          f"{type(exc).__name__}", flush=True)
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    def dropped(self) -> int:
        with self._lock:
            return int(self._dropped)


_DEFAULT_WEBHOOK_DISPATCHER = _BoundedWebhookDispatcher()


def dropped_alert_webhooks() -> int:
    """Outbound alert posts dropped because the isolated sink was saturated."""
    return _DEFAULT_WEBHOOK_DISPATCHER.dropped()


def _safe_field_str(v: str) -> str:
    """Collapse a string alert-field value to a single short line: newline/CR/tab/control → one space, capped."""
    out, prev = [], False
    for ch in v:
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F or ch.isspace():
            if not prev:
                out.append(" ")
            prev = True
        else:
            out.append(ch)
            prev = False
    return "".join(out).strip()[:_FIELD_STR_CAP]


class AlertSink:
    """A fail-open, edge-triggered, rate-limited alert emitter. One instance per process.

    fire(key, level, message, fields) — alert on the TRANSITION into a bad state for `key`; suppress a
    repeat of the same key for `min_interval` seconds. resolve(key) re-arms the key (so the next occurrence
    pages again) and, if it was firing, emits a content-free recovery note.
    """

    def __init__(self, webhook_url: str | None = None, min_interval: float | None = None,
                 service: str = "veripsa-webhook", clock=time.time, poster=None, persist=None):
        self._url = webhook_url if webhook_url is not None else os.environ.get("VERIPSA_ALERT_WEBHOOK_URL", "")
        if min_interval is None:
            try:
                min_interval = float(os.environ.get("VERIPSA_ALERT_MIN_INTERVAL", "900"))
            except ValueError:
                min_interval = 900.0
        self._min_interval = max(0.0, min_interval)
        self._service = service
        self._clock = clock
        # Custom/in-memory posters remain synchronous for deterministic tests.
        # The real network poster is isolated so DNS/log-drain failure can
        # never become an event-worker dependency.
        self._poster = poster if poster is not None else _DEFAULT_WEBHOOK_DISPATCHER.submit
        # DURABLE ALERT BOARD seam — "an alert logged but ignored is meaningless" (the PO's principle). The sink is
        # otherwise in-memory + stdout + an OPTIONAL webhook: with no VERIPSA_ALERT_WEBHOOK_URL configured (the App
        # default), an ACTIVE alert lives ONLY in the per-process Render log and the _firing set below — invisible
        # once nobody is tailing, GONE on the next deploy. `persist` is an injected callable
        # persist(action, key, level, message, fields) the LIVE path wires to write the firing/resolved alert to the
        # owner-readable core.active_alert board (server_boot), so the PO can SEE the live set via
        # core.active_alerts_with_authority() (a notifier polls the same surface). None = the pure/test path: no DB,
        # no persistence (every existing test constructs the sink without it → unchanged). FAIL-OPEN: a persist
        # error is swallowed (a broken board must never take the alerting — or the server — down).
        self._persist = persist
        self._last_fired: dict[str, float] = {}   # key -> ts of last alert sent (the re-arm gate)
        self._firing: set[str] = set()             # keys currently in a fired (un-resolved) state
        self._last_value: dict[str, float] = {}    # key -> gauge value last PAGED at (escalate-on-increase gate)

    def configured(self) -> bool:
        """True if an outbound webhook is set. Alerts ALWAYS log regardless; this only reports the POST path."""
        return bool(self._url)

    def _persist_safe(self, action: str, key: str, level: str, message: str, fields: dict) -> None:
        """Best-effort write to the durable alert board (the injected seam). FAIL-OPEN: any error is swallowed and
        noted — persistence is observability, it must never take alerting (or the server) down."""
        if self._persist is None:
            return
        try:
            self._persist(action, key, level, message, fields or {})
        except Exception as e:
            self._log_safe(
                f"ALERT-SINK persist {action} key={key} skipped "
                f"(alert still emitted): {type(e).__name__}"
            )

    def _safe_message(self, message: str) -> str:
        return redact(_safe_field_str(str(message or "")))

    def _safe_fields(self, fields: dict) -> dict:
        safe_fields = {}
        for k, v in (fields or {}).items():
            if isinstance(v, bool) or isinstance(v, (int, float)):
                safe_fields[k] = v
            elif isinstance(v, str):
                safe_fields[k] = redact(_safe_field_str(v))
        return safe_fields

    def fire(self, key: str, level: str, message: str, fields: dict | None = None,
             *, escalate_value: float | None = None) -> bool:
        """Edge-triggered alert. Returns True if an alert was actually emitted (not suppressed). Never raises.

        Two suppression modes:
          - default (escalate_value=None): TIME-based re-arm — re-page a sustained condition once per
            re-arm window. Correct for outages you want to keep being reminded of.
          - escalate_value given: VALUE-based edge — page only when the gauge first appears or STRICTLY
            increases (a new/worsening loss), and stay quiet while it is stable or shrinking. For a
            STANDING scar that no action can clear (e.g. N permanently-expired deliveries past GitHub's
            recovery window), this pages once per worsening instead of every tick for weeks; the durable
            alert board still shows the standing condition between pages, and resolve() re-arms it."""
        try:
            now = self._clock()
            last = self._last_fired.get(key)
            if escalate_value is not None:
                prev = self._last_value.get(key)
                escalated = prev is None or escalate_value > prev
                self._last_value[key] = escalate_value
                # value-edge mode: the time window is not a re-arm — only a strict increase re-pages.
                suppressed = not escalated
            else:
                # suppress if this key fired within the re-arm window.
                suppressed = last is not None and (now - last) < self._min_interval
            # mark it firing either way so a later resolve() knows the key was active and can emit recovery.
            self._firing.add(key)
            if suppressed:
                return False
            self._last_fired[key] = now
            safe_message = self._safe_message(message)
            safe_fields = self._safe_fields(fields or {})
            self._emit(level, key, safe_message, safe_fields, sanitized=True)
            # PERSIST the firing alert to the durable board AFTER emitting (the log/webhook is the primary signal;
            # the board is the owner-readable record). Only on a real edge-emit (not a suppressed re-fire), so the
            # board's last_fired_at advances at the SAME cadence as the page, not on every silent tick.
            self._persist_safe("fire", key, level, safe_message, safe_fields)
            return True
        except Exception as e:                       # alerting must NEVER take the server down
            self._log_safe(
                f"ALERT-SINK ERROR firing key={key}: {type(e).__name__}"
            )
            return False

    def resolve(self, key: str) -> None:
        """The condition for `key` cleared: re-arm it (next occurrence pages again) and note recovery if it
        had been firing. Never raises."""
        try:
            was_firing = key in self._firing
            self._firing.discard(key)
            self._last_fired.pop(key, None)
            self._last_value.pop(key, None)   # re-arm the value-edge gate: a recurrence pages again
            if was_firing:
                self._emit("info", key, "recovered", {})
            # CLEAR the durable board so it reflects only what is CURRENTLY firing. Done whenever resolve() is called
            # (idempotent on the board side: clearing an absent key is a no-op), so a board row left over from a prior
            # process's fire is reaped even if THIS process never saw the matching fire (e.g. after a restart).
            self._persist_safe("resolve", key, "info", "recovered", {})
        except Exception as e:
            self._log_safe(
                f"ALERT-SINK ERROR resolving key={key}: {type(e).__name__}"
            )

    # ── internals ──────────────────────────────────────────────────────────────────────────────────────
    def _emit(self, level: str, key: str, message: str, fields: dict, sanitized: bool = False) -> None:
        # keep only scalar fields; a string value is collapsed to one short line + redacted so a field can never
        # carry a multi-line source-body run or a DSN out in the payload (call sites pass counts; this is the
        # defensive boundary). bool is a subclass of int — test it first so True/False stay bool, not "True".
        if sanitized:
            safe_message = message
            safe_fields = fields or {}
        else:
            safe_message = self._safe_message(message)
            safe_fields = self._safe_fields(fields or {})
        # one greppable stdout line ALWAYS (log-based alerting can fire on the `ALERT[` prefix even w/o a webhook)
        flat = " ".join(f"{k}={v}" for k, v in safe_fields.items())
        self._log_safe(f"ALERT[{level}] {key}: {safe_message}" + (f" | {flat}" if flat else ""))
        if not self._url:
            return
        body = {
            "service": self._service,
            "level": level,
            "key": key,
            # Slack/Discord both render a top-level `text`; generic consumers read the structured fields too.
            "text": f"[{self._service}] {level.upper()} {key}: {safe_message}",
            "fields": safe_fields,
            "ts": round(self._clock(), 1),
        }
        self._poster(self._url, body)

    def _http_post(self, url: str, body: dict) -> None:
        # Backward-compatible direct seam for callers/tests that deliberately
        # want one synchronous POST. Normal AlertSink emission uses the bounded
        # module dispatcher installed in __init__.
        _http_post_once(url, body)

    def _log_safe(self, line: str) -> None:
        try:
            print(line, flush=True)
        except Exception:
            pass


# ── the WATCHDOG: sample health, decide which conditions are bad, fire/resolve. Pure given a snapshot +
#    db_reachable, so it is unit-testable with no server, no network, no DB. serve() runs evaluate() on a
#    timer thread; tests call it directly. ───────────────────────────────────────────────────────────────

# queue_depth at/over this FRACTION of maxsize is "backing up" (the fixed worker pool cannot keep up).
# 0.5 = 500/1000.
QUEUE_BACKLOG_FRACTION = 0.5
# an INCREASE in the failed counter of at least this many since the last sample is a "spike" worth paging.
FAILED_SPIKE_DELTA = 5
# DURABLE WEBHOOK INBOX depth thresholds (audit P1 — the P0 durability boundary had NO observability). These watch
# core.webhook_delivery (the DB-side persisted inbox), DISTINCT from the in-memory queue_backlog above: a growing
# DURABLE 'queued' backlog means the recovery loop / worker is not draining the persisted rows (events accepted but
# not processed), and ANY 'failed' row is a dead-lettered delivery GitHub will never redeliver (a real, invisible
# loss the DLQ re-arm is meant to rescue). Both were SILENT before — webhook_delivery_depth_with_authority existed
# but nothing called it. Default durable-queued threshold sized for the 5000 max_pending inbox; tunable upward.
DURABLE_QUEUED_BACKLOG = 1000
# DURABLE 'processing' STUCK thresholds (audit iter-4 P2): evaluate_delivery_depth alerted on failed/queued but only
# SAMPLED 'processing' -- never paged on it. A claim-then-CRASH loop (the worker claims a row -> status='processing'
# -> dies before marking it done/failed) accumulates 'processing' rows that are only re-picked after the 1800s
# stale-reclaim window. Recovery now claim-before-submits rows into the in-memory worker queue, so a nonzero
# processing count is normal during catch-up. Alert only when enough rows are processing AND the oldest processing
# row has aged past a real stuck window, sustained over more than one tick. The default age must track the actual
# durable stale-reclaim window; alerting at 300s while recovery waits 1800s pages on rows the system is not yet
# willing to reclaim.
DURABLE_PROCESSING_STUCK = 1
DURABLE_PROCESSING_STUCK_SECONDS = 1800
# LANE-FREEZE narration (the 2026-07-17 queued=41 incident): ONE orphaned 'processing' row (a deploy's SIGTERM
# landed mid-event) serializes its whole account/repo causal lane until stale-reclaim — while every headline
# signal stays green: /healthz healthy, delivery_backlog needs 1000 queued, and delivery_processing_stuck
# deliberately waits for the stale window (above). The entire 30-minute freeze was SILENT, which is what invited
# the harmful fix-attempt (restarting — which re-strands the then-in-flight event and resets the clock). This
# lower WARNING-grade threshold narrates the wait instead of paging: it names the reclaim ETA and says restarting
# makes it worse. Long legitimate events (the ~5-min post-deploy smoke) stay quiet via the sustained >1-tick edge
# unless they run well past this line — and then the message is still honest ("a live event resolves on its own").
DURABLE_LANE_FROZEN_SECONDS = 300
# End-to-end durable response SLO. Depth alone missed the reported queue=11
# incident because its warning starts at 1000 rows, and per-dequeue stuck age
# resets on every retry/slice. One old *due* row is enough to mean a customer
# has waited too long. Future not_before rows are excluded by the SQL surface.
DURABLE_QUEUED_AGE_SECONDS = 120
# Generation-21 ACCOUNT-CONVERGENCE latency bounds. The scheduler surface is
# global + aggregate-only, so one watchdog query can detect a stalled policy /
# graph lane without enumerating tenants. Keep warning and critical as distinct
# keys: a 300s incident must not leave an ambiguous warning firing beside the
# critical page.
ACCOUNT_CONVERGENCE_WARNING_SECONDS = 120
ACCOUNT_CONVERGENCE_CRITICAL_SECONDS = 300
_WORKER_STUCK_RESTART_GRACE_SECONDS = 60


# One pool lane WEDGED on one event longer than this (seconds) is "stuck" — its
# thread remains alive, so worker_dead does not fire. The shared event deadline
# should normally release the durable lease long before this threshold; crossing
# it therefore means an uninterruptible boundary or a broken cancellation path,
# not a legitimate rate-limit sleep. The other keyed lanes may still progress.
# An operator may lower the alert threshold with
# VERIPSA_WORKER_STUCK_SECONDS, but may not raise it beyond the derived hard
# envelope. With the durable inbox enabled, the default is the smaller of the
# event wall and durable cross-generation retry window, plus terminal reserve
# + 25s native/Render margin: 120s for the shipped min(90s,120s)+5s budgets.
# Under the supported VERIPSA_DURABLE_INBOX=0 kill switch, ordinary accepted
# work has no durable retry deadline and can legitimately use the full event
# wall, so the hard threshold follows that wall instead. A stale value in the
# old 600s range must not hide another unknown non-cooperative ~787s
# boundary—especially now that one isolated stuck lane deliberately leaves
# unrelated worker capacity healthy.
def _derived_worker_stuck_seconds() -> int:
    try:
        wall = env_int(
            "VERIPSA_EVENT_WALL_TIMEOUT_SECONDS", 90,
            min_value=1, max_value=900,
        )
    except ConfigError:
        wall = 90
    try:
        retry_window = env_int(
            "VERIPSA_DELIVERY_RETRY_WINDOW_SECONDS", 120,
            min_value=1, max_value=3600,
        )
    except ConfigError:
        retry_window = 120
    try:
        terminal = env_int(
            "VERIPSA_EVENT_TERMINAL_RESERVE_SECONDS", 5,
            min_value=1, max_value=60,
        )
    except ConfigError:
        terminal = 5
    durable_enabled = os.environ.get("VERIPSA_DURABLE_INBOX", "1") != "0"
    executable_wall = (
        min(int(wall), int(retry_window))
        if durable_enabled
        else int(wall)
    )
    return min(86400, max(30, executable_wall + int(terminal) + 25))


def _default_worker_stuck_seconds() -> int:
    default = _derived_worker_stuck_seconds()
    try:
        configured = env_int(
            "VERIPSA_WORKER_STUCK_SECONDS", default,
            min_value=1, max_value=86400,
        )
        return min(default, configured)
    except ConfigError:
        # A typo'd knob fails safe without killing the watchdog tick.
        return default


def _derived_worker_restart_seconds() -> int:
    """Absolute replacement bound after one isolated lane first pages.

    Python cannot kill a thread wedged below userspace. Keeping the process
    green forever would strand that repository's recovery reservation and
    permanently lose one worker slot. Other lanes get a short continuity grace
    after the first hard-envelope page; then any still-stuck lane requires
    whole-process replacement. This threshold is intentionally not
    environment-overridable.
    """
    return min(
        86400,
        _derived_worker_stuck_seconds()
        + _WORKER_STUCK_RESTART_GRACE_SECONDS,
    )


def evaluate(sink: AlertSink, snapshot: dict, db_reachable: bool, prev_failed: int,
             worker_stuck_seconds: int | None = None) -> int:
    """Inspect ONE health sample and fire/resolve the operational alerts. Returns the current failed count
    (the caller threads it back in as prev_failed next tick). Content-free: only counts cross the boundary.

    Conditions (each its own de-duped key):
      worker_dead   — the background worker thread is not alive (Render restarts, but we must also KNOW).
      worker_stuck  — at least one pool lane is ALIVE but has exceeded the
                      delivery ceiling (an uninterruptible call, deadlock, or
                      broken cancellation boundary). worker_dead stays quiet
                      because the thread never died.
      queue_backlog — queue_depth ≥ QUEUE_BACKLOG_FRACTION × maxsize (the worker is falling behind).
      failed_spike  — failed rose by ≥ FAILED_SPIKE_DELTA since the previous sample (a burst of bad events).
      db_unreachable— the data path is down while the worker is up (/healthz stays 200; nothing else pages).
    """
    maxsize = snapshot.get("queue_maxsize") or 1000
    depth = snapshot.get("queue_depth") or 0
    failed = snapshot.get("failed") or 0
    processed = snapshot.get("processed") or 0

    # worker liveness
    if not snapshot.get("worker_alive", True):
        sink.fire("worker_dead", "critical",
                  "background worker thread is NOT alive — events are not being processed",
                  {"queue_depth": depth, "processed": processed, "failed": failed})
    else:
        sink.resolve("worker_dead")

    # worker STUCK (alive but wedged on one event past the bound) — the lying-green: is_alive() is True so
    # worker_dead is quiet and /healthz stays 200, yet processed has frozen. Only paged while the worker is ALIVE
    # (a dead worker is already covered by worker_dead — don't double-page). inflight_age None = idle/healthy.
    if worker_stuck_seconds is None:
        worker_stuck_seconds = _default_worker_stuck_seconds()
    inflight = snapshot.get("inflight_age_seconds")
    if snapshot.get("worker_alive", True) and isinstance(inflight, (int, float)) and inflight >= worker_stuck_seconds:
        sink.fire("worker_stuck", "critical",
                  f"oldest worker lane has exceeded the delivery ceiling for {int(inflight)}s "
                  f"(≥ {int(worker_stuck_seconds)}s) — cancellation failed at an uninterruptible boundary; "
                  "inspect the exact build and durable lease before any restart",
                  {"inflight_age_seconds": int(inflight), "stuck_threshold_seconds": int(worker_stuck_seconds),
                   "queue_depth": depth, "processed": processed,
                   "alive_workers": snapshot.get("alive_workers", 1),
                   "worker_count": snapshot.get("worker_count", 1)})
    else:
        sink.resolve("worker_stuck")

    # queue backlog (toward the 1000 cap → do_POST starts 503ing → Core recovery redelivers; still a real signal)
    if depth >= QUEUE_BACKLOG_FRACTION * maxsize:
        sink.fire("queue_backlog", "warning",
                  f"queue backing up: {depth}/{maxsize} (worker can't keep up — slow ingest or rate-limit stall)",
                  {"queue_depth": depth, "queue_maxsize": maxsize})
    else:
        sink.resolve("queue_backlog")

    # failed-event spike (edge on the DELTA since last sample, so a single old failure doesn't page forever)
    delta = failed - prev_failed
    if delta >= FAILED_SPIKE_DELTA:
        sink.fire("failed_spike", "warning",
                  f"failed events jumped by {delta} (now {failed}) — check the worker FAILED lines for repo/account",
                  {"failed": failed, "delta": delta, "processed": processed})
    # note: failed_spike intentionally re-arms via the rate-limit window, not resolve() — failures are
    # cumulative, so there is no "recovered" edge; a fresh burst simply pages again after the interval.

    # DB reachability (the worker stays alive, /healthz stays 200 — this is the gap nothing else catches)
    if not db_reachable:
        sink.fire("db_unreachable", "critical",
                  "Postgres is unreachable (data path down; worker alive so /healthz stays 200)",
                  {"queue_depth": depth})
    else:
        sink.resolve("db_unreachable")

    return failed


def _default_durable_queued_backlog() -> int:
    try:
        return env_int("VERIPSA_DURABLE_QUEUED_BACKLOG", DURABLE_QUEUED_BACKLOG, min_value=1)
    except ConfigError:
        return DURABLE_QUEUED_BACKLOG  # a typo'd knob FAILS SAFE (no crash on the tick) → the shipped default


def _default_durable_processing_stuck() -> int:
    try:
        return env_int("VERIPSA_DURABLE_PROCESSING_STUCK", DURABLE_PROCESSING_STUCK, min_value=1)
    except ConfigError:
        return DURABLE_PROCESSING_STUCK  # a typo'd knob FAILS SAFE (no crash on the tick) → the shipped default


def _default_durable_processing_stuck_seconds() -> int:
    try:
        stale_seconds = env_int("VERIPSA_DELIVERY_STALE_SECONDS", DURABLE_PROCESSING_STUCK_SECONDS, min_value=1)
        return env_int("VERIPSA_DURABLE_PROCESSING_STUCK_SECONDS", stale_seconds, min_value=1)
    except ConfigError:
        return DURABLE_PROCESSING_STUCK_SECONDS


def _default_durable_lane_frozen_seconds() -> int:
    try:
        return env_int("VERIPSA_DURABLE_LANE_FROZEN_SECONDS", DURABLE_LANE_FROZEN_SECONDS, min_value=1)
    except ConfigError:
        return DURABLE_LANE_FROZEN_SECONDS  # a typo'd knob FAILS SAFE (no crash on the tick) → the shipped default


def _default_durable_queued_age_seconds() -> int:
    try:
        return env_int(
            "VERIPSA_DURABLE_QUEUED_AGE_SECONDS",
            DURABLE_QUEUED_AGE_SECONDS,
            min_value=1,
            max_value=86400,
        )
    except ConfigError:
        return DURABLE_QUEUED_AGE_SECONDS


def _default_delivery_stale_seconds() -> int:
    # The reclaim-ETA input for the lane-frozen narration. The live path passes the store's actual stale_seconds;
    # this fallback mirrors delivery_queue's env default so a direct caller still names an honest ETA.
    try:
        return env_int("VERIPSA_DELIVERY_STALE_SECONDS", 1800, min_value=1)
    except ConfigError:
        return 1800


def evaluate_delivery_depth(sink: AlertSink, depth: dict | None, queued_backlog: int | None = None,
                            processing_stuck: int | None = None,
                            processing_stuck_seconds: int | None = None,
                            lane_frozen_seconds: int | None = None,
                            stale_seconds: int | None = None,
                            queued_age_seconds: int | None = None) -> None:
    """Fire/resolve alerts on the DURABLE WEBHOOK INBOX depth — the P0 durability boundary that had NO
    observability (audit P1). `depth` is core.webhook_delivery_depth_with_authority()'s status→count map
    (e.g. {'queued': 12, 'processing': 1, 'done': 900, 'failed': 2}); the watchdog samples it each tick when a
    durable store is wired. Three conditions, each its own de-duped key (edge-triggered + rate-limited via the sink):

      delivery_dead_letter — depth['failed'] > 0. A 'failed' row is a delivery that exhausted even the (larger)
                             durable attempt budget: pending() no longer replays it and GitHub will NOT redeliver
                             a 202'd event, so it is a REAL, otherwise-INVISIBLE loss. The DLQ re-arm gives it more
                             tries, but a human must KNOW — this is the page. (Resolves once the count returns to 0,
                             i.e. every failed row was re-armed-and-finished or reaped.)
      delivery_backlog     — depth['queued'] ≥ queued_backlog. The DURABLE 'queued' pile is growing = the recovery
                             loop / worker is not draining persisted rows (events are accepted to the DB but not
                             processed). DISTINCT from the in-memory queue_backlog (that watches the live scheduler;
                             this watches the persisted inbox — a stuck recovery loop shows here, not there).
      delivery_latency_slo — at least one due queued row has exceeded the end-to-end age SLO. This catches a
                             queue of 11 and a repeatedly-yielding fanout parent even though neither reaches the
                             count threshold and each dequeue resets the in-memory stuck timer. Future scheduled
                             rows are excluded at the SQL boundary.
      delivery_processing_stuck — depth['processing'] ≥ processing_stuck AND the oldest processing row is at least
                             processing_stuck_seconds old, SUSTAINED over MORE THAN ONE tick (audit iter-4 P2).
                             A claim-then-CRASH loop leaves rows wedged in 'processing' until stale-reclaim, but
                             claim-before-submit recovery also uses 'processing' for rows leased into the memory
                             queue. Age gates the alert so normal recovery catch-up does not false-page.
      delivery_lane_frozen — the oldest 'processing' row is ≥ lane_frozen_seconds old, sustained >1 tick. The
                             WARNING-grade lane-freeze narration (2026-07-17 incident): one orphaned row freezes
                             its account/repo causal lane while everything above stays quiet. Names the stale-
                             reclaim ETA and warns that restarting re-strands the in-flight event.

    Fail-open + content-free (only counts cross the boundary). `depth` None/garbage (a sample error) is a no-op —
    a depth read must never take the watchdog down (the caller already swallows the read error)."""
    if not isinstance(depth, dict):
        return
    if queued_backlog is None:
        queued_backlog = _default_durable_queued_backlog()
    if processing_stuck is None:
        processing_stuck = _default_durable_processing_stuck()
    if processing_stuck_seconds is None:
        processing_stuck_seconds = _default_durable_processing_stuck_seconds()
    failed = depth.get("failed") or 0
    queued = depth.get("queued") or 0
    processing = depth.get("processing") or 0
    processing_age = depth.get("processing_oldest_age_seconds")
    queued_due = depth.get("queued_due")
    queued_due_age = depth.get("queued_due_oldest_age_seconds")
    if isinstance(failed, (int, float)) and failed > 0:
        sink.fire("delivery_dead_letter", "critical",
                  f"durable webhook inbox has {int(failed)} DEAD-LETTERED row(s) (status='failed') — GitHub will "
                  f"NOT redeliver a 202'd event, so these are real, invisible losses; the DLQ re-arm retries them "
                  f"but a poison/persistent failure needs a human",
                  {"failed": int(failed), "queued": int(queued), "processing": int(processing)})
    else:
        sink.resolve("delivery_dead_letter")
    if isinstance(queued, (int, float)) and queued >= queued_backlog:
        sink.fire("delivery_backlog", "warning",
                  f"durable webhook inbox 'queued' backlog: {int(queued)} (≥ {int(queued_backlog)}) — the recovery "
                  f"loop / worker is not draining the PERSISTED inbox (events accepted to the DB but not processed)",
                  {"queued": int(queued), "processing": int(processing), "backlog_threshold": int(queued_backlog)})
    else:
        sink.resolve("delivery_backlog")
    if queued_age_seconds is None:
        queued_age_seconds = _default_durable_queued_age_seconds()
    due_n = int(queued_due) if isinstance(queued_due, (int, float)) else 0
    due_age = int(queued_due_age) if isinstance(queued_due_age, (int, float)) else 0
    if due_n > 0 and due_age >= int(queued_age_seconds):
        sink.fire(
            "delivery_latency_slo",
            "warning",
            f"durable webhook response SLO exceeded: oldest due queued work is {due_age}s old "
            f"(threshold {int(queued_age_seconds)}s); {due_n} due row(s) await progress",
            {
                "queued": int(queued),
                "queued_due": due_n,
                "queued_due_oldest_age_seconds": due_age,
                "queued_age_threshold_seconds": int(queued_age_seconds),
                "queued_max_attempts": int(depth.get("queued_max_attempts"))
                if isinstance(depth.get("queued_max_attempts"), (int, float)) else 0,
                "fanout_active": int(depth.get("fanout_active"))
                if isinstance(depth.get("fanout_active"), (int, float)) else 0,
                "fanout_remaining": int(depth.get("fanout_remaining"))
                if isinstance(depth.get("fanout_remaining"), (int, float)) else 0,
                "fanout_oldest_progress_age_seconds": int(
                    depth.get("fanout_oldest_progress_age_seconds"))
                if isinstance(
                    depth.get("fanout_oldest_progress_age_seconds"),
                    (int, float),
                ) else 0,
            },
        )
    else:
        sink.resolve("delivery_latency_slo")
    # DURABLE 'processing' STUCK (audit iter-4 P2) — edge-triggered on the SECOND consecutive over-threshold tick so
    # a transient in-flight row never pages, only a SUSTAINED pile (a claim-then-crash wedge). The prior-tick
    # observation lives on the sink (reused across ticks, like _last_fired); read defensively so an older sink that
    # predates this attribute still works (a missing attr ⇒ this is the first observation ⇒ never fires this tick).
    proc_n = int(processing) if isinstance(processing, (int, float)) else 0
    proc_age = int(processing_age) if isinstance(processing_age, (int, float)) else None
    prev_over = bool(getattr(sink, "_delivery_processing_over", False))
    # Older schema revisions did not expose processing age; keep the old count-only behavior in that downgrade
    # shape, but production schema now supplies the age so fresh claim-before-submit recovery does not false-page.
    old_enough = True if proc_age is None else proc_age >= processing_stuck_seconds
    over = proc_n >= processing_stuck and old_enough
    if over and prev_over:
        sink.fire("delivery_processing_stuck", "critical",
                  f"durable webhook inbox has {proc_n} row(s) stuck in 'processing' (≥ {int(processing_stuck)}) for "
                  f">1 tick; oldest age is {proc_age if proc_age is not None else 'unknown'}s "
                  f"(threshold {int(processing_stuck_seconds)}s) — a claim-then-crash loop leaves rows wedged until "
                  f"stale-reclaim",
                  {"processing": proc_n, "queued": int(queued), "stuck_threshold": int(processing_stuck),
                   "processing_oldest_age_seconds": proc_age,
                   "stuck_age_threshold_seconds": int(processing_stuck_seconds)})
    elif not over:
        sink.resolve("delivery_processing_stuck")   # back below the line → re-arm (and note recovery if it was firing)
    # remember THIS tick's observation for the next call's "sustained over >1 tick" edge (set last so a fire/resolve
    # above always sees the PRIOR tick's value). Stored on the sink so it survives across the watchdog's ticks.
    try:
        sink._delivery_processing_over = over
    except Exception:
        pass   # a sink that forbids attribute set (exotic) simply never sustains — degrades to never-page, never crash

    # LANE-FROZEN narration (2026-07-17): the causal claim order serializes each account/repo lane behind its
    # oldest unfinished row, so ONE old 'processing' row — orphaned when a deploy killed its worker mid-event —
    # freezes the lane while every headline signal stays green (later same-lane deliveries defer as
    # blocked_by_earlier; other lanes flow). Same sustained >1-tick edge as above; proc_age None (older schema)
    # never fires. WARNING, not critical: the system recovers itself — the alert's job is to explain the wait and
    # name the one harmful move (restarting).
    if lane_frozen_seconds is None:
        lane_frozen_seconds = _default_durable_lane_frozen_seconds()
    if stale_seconds is None:
        stale_seconds = _default_delivery_stale_seconds()
    lane_over = proc_n >= 1 and proc_age is not None and proc_age >= int(lane_frozen_seconds)
    prev_lane_over = bool(getattr(sink, "_delivery_lane_frozen_over", False))
    if lane_over and prev_lane_over:
        reclaim_eta = max(0, int(stale_seconds) - proc_age)
        # The sink caps messages at _FIELD_STR_CAP (200) — keep the operator instruction UP FRONT so truncation
        # can never eat it; thresholds/counts ride in the structured fields.
        sink.fire("delivery_lane_frozen", "warning",
                  f"durable inbox oldest 'processing' row is {proc_age}s old: its lane is serialized behind it "
                  f"(same-lane deliveries defer). Do NOT restart — that re-strands it; auto-reclaim in "
                  f"≤{reclaim_eta}s if orphaned",
                  {"processing": proc_n, "queued": int(queued),
                   "processing_oldest_age_seconds": proc_age,
                   "lane_frozen_threshold_seconds": int(lane_frozen_seconds),
                   "stale_reclaim_eta_seconds": reclaim_eta})
    elif not lane_over:
        sink.resolve("delivery_lane_frozen")
    try:
        sink._delivery_lane_frozen_over = lane_over
    except Exception:
        pass   # same degrade-to-quiet contract as the processing-stuck edge above


# ── the ACCOUNT-CONVERGENCE WATCHDOG: generation 21 moves policy + main-graph
#    convergence onto one globally-fair DB scheduler. This evaluator watches
#    the scheduler's ONE content-free aggregate, not an O(N-account) scan. ────

def evaluate_account_convergence_depth(sink: AlertSink, depth: dict | None) -> None:
    """Fire/resolve alerts for ``account_convergence_depth_with_authority``.

    The surface contains only global counts and the oldest unfinished age. Any
    retry-exhausted row is a critical poison-isolation signal; the scheduler
    keeps it on its bounded slow automatic retry lane. Unfinished work warns at 120s
    and becomes critical at 300s; separate stable keys make the current
    severity unambiguous. ``quota_deferred`` by itself is expected flow
    control, not an error.

    A missing, partial, negative, internally inconsistent, or otherwise
    malformed sample is *Unknown*: this function deliberately makes no
    ``resolve`` calls in that case. A failed observation must never clear a
    standing incident.
    """
    if not isinstance(depth, dict):
        return

    names = (
        "pending",
        "claimed",
        "retry_exhausted",
        "quota_deferred",
        "due_accounts",
        "stalled_accounts",
        "oldest_age_seconds",
    )
    values: dict[str, int] = {}
    for name in names:
        value = depth.get(name)
        # bool is an int subclass but is not a valid count/age observation.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return
        values[name] = value

    pending = values["pending"]
    claimed = values["claimed"]
    exhausted = values["retry_exhausted"]
    quota_deferred = values["quota_deferred"]
    due_accounts = values["due_accounts"]
    stalled_accounts = values["stalled_accounts"]
    age = values["oldest_age_seconds"]

    # ``pending`` is only ordinary retryable work; exhausted and quota-paused
    # rows are intentionally separate buckets and may exceed it. Scheduler
    # due/reclaim time is independent of latency provenance. The structural
    # invariant is instead that no unfinished ordinary account means no
    # oldest unfinished age.
    if stalled_accounts == 0 and age != 0:
        return

    fields = {
        "pending": pending,
        "claimed": claimed,
        "retry_exhausted": exhausted,
        "quota_deferred": quota_deferred,
        "due_accounts": due_accounts,
        "stalled_accounts": stalled_accounts,
        "oldest_age_seconds": age,
    }

    if exhausted > 0:
        sink.fire(
            "account_convergence_retry_exhausted",
            "critical",
            f"account convergence has {exhausted} retry-exhausted row(s); "
            "rows remain isolated on the bounded 5m-to-1h automatic retry lane",
            fields,
        )
    else:
        sink.resolve("account_convergence_retry_exhausted")

    if stalled_accounts > 0 and age >= ACCOUNT_CONVERGENCE_CRITICAL_SECONDS:
        sink.resolve("account_convergence_latency_warning")
        sink.fire(
            "account_convergence_latency_critical",
            "critical",
            f"oldest unfinished account-convergence work is {age}s old "
            f"(critical at {ACCOUNT_CONVERGENCE_CRITICAL_SECONDS}s)",
            fields,
        )
    elif stalled_accounts > 0 and age >= ACCOUNT_CONVERGENCE_WARNING_SECONDS:
        sink.resolve("account_convergence_latency_critical")
        sink.fire(
            "account_convergence_latency_warning",
            "warning",
            f"oldest unfinished account-convergence work is {age}s old "
            f"(warning at {ACCOUNT_CONVERGENCE_WARNING_SECONDS}s)",
            fields,
        )
    else:
        sink.resolve("account_convergence_latency_warning")
        sink.resolve("account_convergence_latency_critical")


# ── the DB-COST WATCHDOG: page when the (small/cheap) Postgres is filling, or when a tenant crosses the free
#    line. The health watchdog above samples /healthz; this one samples core.owner_cost_surface() (the same
#    content-free aggregate cost_report.py prints). Separate from evaluate() because it watches a different
#    signal on a SLOWER cadence (cost grows slowly, and owner_cost_surface visits every tenant — so it runs on a
#    slow tick, not the worker's fast loop), but it reuses the SAME AlertSink so it is edge-triggered + rate-
#    limited + fail-open + content-free, unchanged. WIRED LIVE: health_watchdog.watchdog_tick calls this on a slow
#    cadence (run_watchdog's cost_every / VERIPSA_WATCHDOG_COST_INTERVAL) — so account_over_line PAGES at runtime,
#    not only when the founder runs cost_report.py by hand. (cost_report.py also calls it from its own cron path.)
#    ──────────────────────────────────────────────────────────────────────────────────────────────────────────

# default DB-size page threshold: 200 MiB — for the 256 MiB starter tier (page with headroom to act before the
# disk is full). Tune via VERIPSA_DB_SIZE_ALERT_BYTES for a bigger paid instance. Routed through env_int with
# min_value=1 like every other knob: a bare int() admitted 0/NEGATIVE, and a -1 makes total >= threshold ALWAYS
# true → db_size_high fires forever (a self-inflicted alert-flood from one typo). A non-int/out-of-range value
# now FAILS SAFE — caught here → the shipped 200 MiB default — exactly like _db_size_cap_mb / the db-usage knobs.
_DEFAULT_DB_SIZE_ALERT_BYTES = 200 * 1024 * 1024


def _default_db_size_alert_bytes() -> int:
    try:
        return env_int("VERIPSA_DB_SIZE_ALERT_BYTES", _DEFAULT_DB_SIZE_ALERT_BYTES, min_value=1)
    except ConfigError:
        return _DEFAULT_DB_SIZE_ALERT_BYTES


def evaluate_cost(sink: AlertSink, surface: dict | None, size_threshold_bytes: int | None = None) -> None:
    """Inspect ONE cost sample (core.owner_cost_surface() output) and fire/resolve the DB-cost alerts.
    Content-free: only the total size + the count of over-the-line accounts cross the boundary — no ids, no
    repo names, no paths. Never raises (a missing/empty surface simply does nothing — no false page).

    Conditions (each its own de-duped key, edge-triggered via the shared AlertSink):
      db_size_high     — db_total_bytes ≥ threshold (the cheap Postgres is filling; act before it's full).
      account_over_line— over_line_count > 0 (at least one account crossed the free tier — billing/limit cue).
    """
    if not isinstance(surface, dict) or not surface:
        return  # nothing observed → no page (absence is not a breach; we just stay quiet)
    if size_threshold_bytes is None:
        size_threshold_bytes = _default_db_size_alert_bytes()

    total = surface.get("db_total_bytes")
    if isinstance(total, (int, float)):
        if total >= size_threshold_bytes:
            sink.fire("db_size_high", "warning",
                      f"DB is filling: {int(total)} bytes ≥ threshold {int(size_threshold_bytes)} "
                      f"(prune harder or upsize before the disk is full)",
                      {"db_total_bytes": int(total), "threshold_bytes": int(size_threshold_bytes)})
        else:
            sink.resolve("db_size_high")
    # an unreadable/absent total is NOT a "recovered" edge — leave db_size_high as-is (no false resolve).

    over = surface.get("over_line_count")
    if over is None:  # tolerate a surface that omits the rollup: count the rows defensively (still content-free)
        accounts = surface.get("accounts")
        if isinstance(accounts, list):
            over = sum(1 for a in accounts if isinstance(a, dict) and a.get("over_free_line"))
    if isinstance(over, (int, float)):
        if over > 0:
            sink.fire("account_over_line", "warning",
                      f"{int(over)} account(s) crossed the free line (review footprint / billing)",
                      {"over_line_count": int(over)})
        else:
            sink.resolve("account_over_line")


# ── the GRAPH-FRESHNESS WATCHDOG: page when main's STORED code graph has drifted BEHIND main HEAD — i.e. a
#    push-to-main was MISSED/LAGGED and predictions are running against a stale baseline. The per-PR self-heal
#    fixes drift on the NEXT PR; this alert makes a SILENT drift LOUD in the gap before that PR arrives (a repo
#    that is behind HEAD but sees no new PR for hours would otherwise drift unnoticed). Same AlertSink → edge-
#    triggered + rate-limited + fail-open + content-free (a commit sha is public git metadata + a count/age). ──

# behind by AT LEAST this many distinct coordinates is "drifting" (a single just-pushed repo a heartbeat behind
# is normal; the self-heal catches it on its next PR — only page when drift is real/sustained). Default 1: ANY
# coordinate confirmed behind HEAD is worth one page. Tune via VERIPSA_GRAPH_BEHIND_ALERT_COUNT.
def _default_graph_behind_alert_count() -> int:
    # env_int = the ONE validated reader (clamps + FAILS LOUDLY on a typo'd value, naming the var) — vs the old
    # bare int()+try/except that SILENTLY fell back to the default on a misconfig, masking an operator typo.
    return env_int("VERIPSA_GRAPH_BEHIND_ALERT_COUNT", 1, min_value=1)


# a stored graph older than this (seconds) on a coordinate STILL CONFIRMED behind HEAD is "stale beyond the time
# bound" — a drift that has persisted (no self-healing PR arrived). Default 3600 (1h). Tune via VERIPSA_GRAPH_STALE_SECONDS.
def _default_graph_stale_seconds() -> int:
    # validated reader (see _default_graph_behind_alert_count): a typo'd VERIPSA_GRAPH_STALE_SECONDS now fails
    # loudly at startup instead of silently reverting to the default.
    return env_int("VERIPSA_GRAPH_STALE_SECONDS", 3600, min_value=0)


def evaluate_graph_freshness(sink: AlertSink, freshness: list | None,
                             behind_count_threshold: int | None = None,
                             stale_seconds: int | None = None) -> int:
    """Inspect the App's per-coordinate graph FRESHNESS and fire/resolve the stale-graph alert. `freshness` is
    a list of records {repo?, branch?, behind: bool|None, age_seconds: int} that the watchdog computed by
    joining the DB's stored sha with main's current HEAD. Content-free: only a COUNT of behind coordinates +
    the worst age cross the boundary — never a repo name, sha, path, or body. Never raises. Returns the count
    of coordinates confirmed behind HEAD (for logging/threading).

    Conditions (one de-duped key, edge-triggered via the shared AlertSink):
      graph_stale — ≥ behind_count_threshold coordinates are confirmed BEHIND main HEAD (a missed/lagged push →
                    predictions run against a stale graph). Escalates to a longer message when a behind
                    coordinate is ALSO older than the time bound (drift has persisted — no PR self-healed it).
    A record with behind=None (HEAD unresolvable) is NOT counted as behind (we never page on what we can't see).
    It also cannot RESOLVE an existing page: Unknown/incomplete evidence is a no-op, not proof of recovery.
    List-compatible samplers may attach ``coverage_complete``, ``cursor_healthy``, and ``timed_out`` attributes;
    a negative value on any of those likewise prevents recovery while preserving legacy plain-list compatibility.

    ORPHAN/PHANTOM-COORDINATE EXCLUSION (audit): a (repo, branch) can have MORE THAN ONE stored coordinate — the
    LIVE one that predictions run on, plus an ABANDONED duplicate (a backfill/old-account graph_version written
    into a dead account the webhook never re-addresses, or a renamed-away coordinate). The orphan is BEHIND main
    HEAD FOREVER: no PR will ever touch that dead account, so the per-PR self-heal can never fix it — it would
    page graph_stale every interval, an un-actionable flood. The operator mutes graph_stale → a LATER REAL drift
    is then silently missed (a false page that begets a missed page). Rule: a (repo, branch) that has ANY CURRENT
    (behind=False) sibling is being tracked FRESH, so a behind=True record for that SAME (repo, branch) is a stale
    orphan/duplicate, NOT live drift — it is excluded from the page. A behind coordinate whose (repo, branch) has
    NO fresh sibling is genuine drift and STILL pages (the exclusion is surgical, keyed on repo+branch, never a
    blanket silence). Content-free: only the repo/branch grouping (public git metadata, already in the records)
    and counts cross the boundary.

    DEFAULT-BRANCH-CHANGE EXCLUSION (audit, the SECOND orphan shape, mirrors graph_freshness_all): the coordinate
    IS the repo's DEFAULT branch, so a default-branch MOVE (main → master) freezes the old (…, main) coordinate
    (behind HEAD forever) while a fresh (…, master) one is ingested — and the (repo,branch)-sibling rule above
    can't catch it (the fresh sibling is at a DIFFERENT (repo, master) key). graph_freshness_all tags each record
    with `live_default` (branch == the repo's current default?); a behind record CONFIRMED on a non-current
    default branch (live_default is False) is a frozen non-live coordinate → it does NOT page. live_default None
    (default unresolvable) is never excluded; an absent field flows through unchanged. Auto-heals any future move.

    EXTRACTOR-VERSION STALENESS IS DELIBERATELY NOT PAGED HERE (G3, mirrors graph_freshness_all). graph_stale keys
    ONLY on SHA-vs-HEAD drift. Version-staleness (a graph built by an OLD extractor at the CURRENT HEAD) is a
    per-coordinate verdict-path concern — the singular graph_freshness marks it behind so the per-PR self-heal
    re-ingests it and the G1 withhold covers an un-healable case as `unknown`. It is kept OUT of this fleet page on
    purpose: an extractor bump (or the stamp's first deploy) transiently marks every coordinate version-behind
    until re-ingest re-stamps it, and paging that would be the same un-actionable flood the orphan exclusions above
    prevent.
    """
    if not isinstance(freshness, list):
        return 0  # nothing observed → no page (absence is not a breach; stay quiet)
    if behind_count_threshold is None:
        behind_count_threshold = _default_graph_behind_alert_count()
    if stale_seconds is None:
        stale_seconds = _default_graph_stale_seconds()

    coverage_complete = getattr(freshness, "coverage_complete", True) is True
    cursor_healthy = getattr(freshness, "cursor_healthy", True) is True
    timed_out = getattr(freshness, "timed_out", False) is True
    has_unknown = any(
        not isinstance(f, dict)
        or (
            f.get("behind") is not True
            and f.get("behind") is not False
        )
        for f in freshness
    )
    recovery_proven = (
        coverage_complete
        and cursor_healthy
        and not timed_out
        and not has_unknown
    )

    # a coordinate KEY = (repo, branch). Any (repo, branch) with a CURRENT (behind=False) record is being tracked
    # fresh → its predictions run on a live, up-to-date graph. Empty repo/branch is NOT a usable key (we cannot
    # prove a fresh sibling), so it never silences anything — those records flow through unchanged.
    def _coord_key(f):
        repo = f.get("repo") or ""
        branch = f.get("branch") or ""
        return (repo, branch) if repo and branch else None
    fresh_keys = {_coord_key(f) for f in freshness
                  if isinstance(f, dict) and f.get("behind") is False and _coord_key(f) is not None}

    # NON-CURRENT-DEFAULT-BRANCH EXCLUSION (default-branch-change orphan, audit) — MIRRORS the parallel exclusion
    # in graph_freshness.graph_freshness_all so the surface and this alert AGREE. The graph coordinate is the
    # repo's DEFAULT branch; when the default MOVES (main → master) the old (…, main) coordinate FREEZES (BEHIND
    # main HEAD forever) while a fresh (…, master) one is ingested — and the (repo,branch)-sibling rule above
    # can't catch it (the fresh sibling is at a DIFFERENT (repo, master) key). graph_freshness_all tags each
    # record with `live_default` (is this branch the repo's CURRENT default?); a record CONFIRMED on a non-current
    # default branch (live_default is False) is a frozen non-live coordinate predictions never run on → it does
    # NOT page. live_default None (HEAD/default unresolvable) is NEVER excluded here (never silence on what we
    # can't see); when the field is absent (a hand-built record) the test `is False` is simply False → unchanged.
    behind = [f for f in freshness if isinstance(f, dict) and f.get("behind") is True
              and _coord_key(f) not in fresh_keys and f.get("live_default") is not False]
    behind_count = len(behind)
    # the worst (oldest) age among coordinates CONFIRMED behind — 0 if none behind.
    def _age(f):
        a = f.get("age_seconds")
        return a if isinstance(a, (int, float)) else 0
    worst_age = max((_age(f) for f in behind), default=0)
    stale_persisted = any(_age(f) >= stale_seconds for f in behind) if stale_seconds is not None else False

    if behind_count >= behind_count_threshold:
        if stale_persisted:
            msg = (f"{behind_count} coordinate(s) BEHIND main HEAD and stale > {int(stale_seconds)}s "
                   f"(oldest {int(worst_age)}s) — a missed push has NOT self-healed; predictions run on a stale graph")
        else:
            msg = (f"{behind_count} coordinate(s) BEHIND main HEAD (oldest {int(worst_age)}s) — a push was "
                   f"missed/lagged; the next PR self-heals it, but the graph is stale until then")
        sink.fire("graph_stale", "warning", msg,
                  {"behind_count": behind_count, "worst_age_seconds": int(worst_age),
                   "stale_threshold_seconds": int(stale_seconds)})
    elif recovery_proven:
        sink.resolve("graph_stale")
    return behind_count


# ── the APP-JWT REACHABILITY WATCHDOG: page when the configured App identity cannot be verified by the cached
#    GET /app point probe (legacy clients use their bounded install-map probe) WHILE Core has ≥1 installation to
#    serve. A content-free reachability boolean is crossed with the DB's content-free routed-install count; no
#    tenant id, org name, or token leaves the process. Distinct key from graph_stale so muting one never mutes the
#    other, and so systemic observation blindness cannot masquerade as graph recovery. ─────────────────────────

# at least this many tenants must exist (the DB knows of ≥1 routed installation) before an UNREACHABLE App-JWT is
# a real outage worth paging: a zero-install App (a brand-new deploy, or a fully-uninstalled one) that cannot list
# installations is NOT a degraded service — there is nothing to serve — so it never pages. Default 1.
def _default_app_unreachable_min_installs() -> int:
    try:
        return env_int("VERIPSA_APP_UNREACHABLE_MIN_INSTALLS", 1, min_value=1)
    except ConfigError:
        return 1  # a typo'd knob FAILS SAFE (no crash on the tick) → the shipped default


def evaluate_app_reachability(sink: AlertSink, reachable: bool | None, installation_rows: int | None,
                              min_installs: int | None = None) -> None:
    """Fire/resolve the app_jwt_unreachable alert. `reachable` is whether the cached App identity point probe
    succeeded (or, for a rolling/legacy client, its bounded install-map probe); `installation_rows` is
    the content-free count of routed installations the DB knows of (core.list_installation_ids()). Content-free:
    only a boolean + a count cross the boundary — never a tenant id, an org name, a token, or the DSN. Never raises.

    Condition (one de-duped key, edge-triggered via the shared AlertSink):
      app_jwt_unreachable — the App identity could NOT be verified (a systemic 403/401/network fault or id
                            mismatch) WHILE the DB shows ≥ min_installs routed installation(s). This is the
                            ROOT of a silent fleet-wide blindness: with the install map empty, for_account() returns
                            None for every tenant, so freshness / boot-reconcile / cross-repo discovery all go blind
                            and every coordinate degrades to behind=None — which graph_stale CORRECTLY never pages
                            on. So nothing else catches this; THIS is the page. (The 403 root cause itself is
                            environmental — a revoked/clock-skewed App JWT, a suspended install — and is fixed by the
                            operator, not by Core; this alert just makes the systemic blindness LOUD.)

    A reachable=True probe RESOLVES (re-arms) the key. reachable=None (the probe was not run — no gh client wired)
    is a NO-OP: we never page on a probe we did not take. installation_rows None/unknown is treated as 0 (we cannot
    prove there is anything to serve → no false page) — absence is never a breach."""
    if reachable is None:
        return  # the probe was not run (no gh client) → nothing observed, stay quiet
    if min_installs is None:
        min_installs = _default_app_unreachable_min_installs()
    rows = installation_rows if isinstance(installation_rows, (int, float)) and installation_rows >= 0 else 0
    if not reachable and rows >= min_installs:
        sink.fire("app_jwt_unreachable", "critical",
                  f"the configured App identity point probe failed or mismatched while {int(rows)} routed "
                  f"installation(s) exist — fleet freshness observation may be BLIND (coordinates degrade to "
                  f"behind=unknown, which graph_stale correctly never pages on). Check the App JWT / configured "
                  f"App id / App-level permissions / clock skew",
                  {"app_jwt_reachable": False, "installation_rows": int(rows),
                   "min_installs": int(min_installs)})
    else:
        sink.resolve("app_jwt_unreachable")  # reachable again (or nothing to serve) → re-arm


# the FRACTION of coordinates whose freshness is UNKNOWN (behind=None) that, combined with an unreachable App-JWT,
# is "blind" rather than a couple of genuinely-unresolvable repos. 0.5 = half the fleet went dark at once. A high
# fraction of behind=None is the SYMPTOM the graph_stale rule deliberately stays quiet about (it never pages on
# what it can't see); freshness_blind is the COMPLEMENT — it pages on the systemic CAUSE (the App-JWT is down so
# we can't see ANY of them), never on a per-repo unknown. Tunable via VERIPSA_FRESHNESS_BLIND_FRACTION.
def _default_freshness_blind_fraction() -> float:
    try:
        v = float(os.environ.get("VERIPSA_FRESHNESS_BLIND_FRACTION", "0.5"))
    except (TypeError, ValueError):
        return 0.5
    if v != v or v <= 0.0 or v > 1.0:  # NaN / out of (0, 1] → safe default
        return 0.5
    return v


def evaluate_freshness_blind(sink: AlertSink, total: int | None, behind_none: int | None,
                             app_reachable: bool | None, blind_fraction: float | None = None) -> None:
    """Fire/resolve the freshness_blind alert — the DISTINCT signal for "we can't SEE freshness for most of the
    fleet" (vs graph_stale = "a coordinate we CAN see is behind"). Fires only when BOTH hold: (a) a high fraction
    of coordinates have behind=None (freshness unknown) AND (b) the App-JWT is unreachable (the systemic cause).
    This deliberately does NOT page on behind=None alone — a couple of genuinely-unresolvable repos (uninstalled /
    a dogfood non-GH coordinate) is normal and the per-repo graph_stale rule rightly stays quiet about them. It is
    the COMBINATION — most of the fleet went unknown AT THE SAME TIME the App can't list installations — that means
    "blind", not "a missed push". Content-free: only counts + a boolean cross the boundary. Never raises.

    `total` = number of coordinates sampled; `behind_none` = how many had behind=None; `app_reachable` = the
    list-installations probe result. app_reachable True (we CAN see) or None (probe not run) is a NO-OP resolve —
    if we can reach the App-JWT, behind=None is a real per-repo unknown, not systemic blindness. total 0 / unknown
    is a NO-OP (no coordinates → no fraction → nothing to be blind about)."""
    if blind_fraction is None:
        blind_fraction = _default_freshness_blind_fraction()
    t = int(total) if isinstance(total, (int, float)) and total > 0 else 0
    bn = int(behind_none) if isinstance(behind_none, (int, float)) and behind_none >= 0 else 0
    # only meaningful when the App-JWT is CONFIRMED unreachable (the systemic cause). Reachable / unknown → resolve.
    if app_reachable is not False or t <= 0:
        sink.resolve("freshness_blind")
        return
    frac = bn / t
    if frac >= blind_fraction:
        sink.fire("freshness_blind", "warning",
                  f"{bn} of {t} coordinate(s) have UNKNOWN freshness (behind=unknown) while the App-JWT is "
                  f"unreachable — this is systemic BLINDNESS (we can't resolve HEAD for most of the fleet), NOT a "
                  f"missed push; graph_stale stays quiet by design, so this is the signal. See app_jwt_unreachable",
                  {"behind_none": bn, "total": t, "blind_fraction": round(frac, 3),
                   "blind_threshold": round(float(blind_fraction), 3)})
    else:
        sink.resolve("freshness_blind")  # below the blind line → re-arm (a few unknowns is not blindness)


# ── the OPERATOR DB-USAGE WATCHDOG: page when the WHOLE Render Postgres is filling toward its storage cap — the
#    AWS-billing-alarm equivalent for our managed DB. This is a DIFFERENT LAYER from the per-tenant free-line
#    quota (#95 owner_cost_surface / the core quota gate, which blocks ONE over-using tenant at the gate): every
#    tenant can be UNDER its own line while the SUM silently fills the disk and surprises us with a bigger bill /
#    a full disk. Nothing watched the TOTAL until now. It samples core.db_usage_surface() (whole-DB size + the
#    configured cap + percent-used) and reuses the SAME AlertSink → edge-triggered + rate-limited + fail-open +
#    content-free, unchanged (only the percent + a byte count cross the boundary; never a row, path, or id). ─────

# WARN/CRITICAL fractions of the configured cap. 70% = "start watching / plan a prune-or-upsize"; 90% = "act now
# before the disk is full / the bill jumps". Tunable via the env knobs below (kept in [1,99], WARN < CRITICAL).
def _db_usage_warn_pct() -> int:
    try:
        return env_int("VERIPSA_DB_USAGE_WARN_PCT", 70, min_value=1, max_value=99)
    except ConfigError:
        return 70  # a typo'd knob FAILS SAFE here (no crash on the watchdog tick) → fall back to the shipped default


def _db_usage_critical_pct() -> int:
    try:
        return env_int("VERIPSA_DB_USAGE_CRITICAL_PCT", 90, min_value=1, max_value=99)
    except ConfigError:
        return 90


# the configured storage cap, in MiB — the operator's ceiling (the Render plan size, or a chosen budget) the
# percent-used is measured against. 0 / unset = UNCAPPED → the alert stays SILENT (no cap, no percentage, no
# false page). env_int validates it ([0, ~1 PiB] so a non-int/negative is refused); a misconfig FAILS SAFE here
# (caught → treated as uncapped → no-op) rather than crashing the watchdog. This is the ONE knob the task names.
_DB_SIZE_CAP_MAX_MB = 1024 * 1024 * 1024  # 1 PiB ceiling on the knob itself (a sane upper bound; far above any plan)


def _db_size_cap_mb() -> int:
    try:
        return env_int("VERIPSA_DB_SIZE_CAP_MB", 0, min_value=0, max_value=_DB_SIZE_CAP_MAX_MB)
    except ConfigError:
        # a misconfigured cap MUST NOT page falsely and MUST NOT crash the worker — fall back to UNCAPPED (0),
        # which makes evaluate_db_usage a no-op. A bad cap silences the alert; it never invents a breach.
        return 0


def evaluate_db_usage(sink: AlertSink, surface: dict | None,
                      cap_mb: int | None = None,
                      warn_pct: int | None = None,
                      critical_pct: int | None = None) -> int | None:
    """Inspect ONE whole-DB usage sample (core.db_usage_surface() output) and fire/resolve the operator-level
    db_usage_high alert. Content-free: only the PERCENT-used + a total byte count cross the boundary — never a
    table's rows, a path, an id, or anything tenant-specific. Never raises (a missing/garbage surface, or an
    UNCAPPED instance, simply does nothing — absence is not a breach, an uncapped DB cannot cross a percent).
    Returns the percent-used it acted on (None when it could not / did not evaluate — uncapped or unreadable).

    Condition (one de-duped key, edge-triggered via the shared AlertSink):
      db_usage_high — total DB size ≥ warn_pct% of the configured cap. WARNING at the warn line, escalates to
                      CRITICAL at the critical line (act now). Resolves (re-arms) once usage drops back below warn.

    Knobs (all fail SAFE — a misconfigured env knob is caught and the shipped default used, never a crash/false page):
      cap_mb       — the storage ceiling in MiB (env VERIPSA_DB_SIZE_CAP_MB; 0/unset = UNCAPPED → silent no-op).
      warn_pct     — WARN fraction of the cap (env VERIPSA_DB_USAGE_WARN_PCT, default 70).
      critical_pct — CRITICAL fraction of the cap (env VERIPSA_DB_USAGE_CRITICAL_PCT, default 90).
    """
    if not isinstance(surface, dict) or not surface:
        return None  # nothing observed → no page (absence is not a breach; stay quiet)

    if cap_mb is None:
        cap_mb = _db_size_cap_mb()
    # an UNCAPPED instance (no ceiling configured) cannot cross a percentage — stay silent (and re-arm any prior
    # firing so a later cap-config sees a clean edge). This is the fail-safe path for an unset/misconfigured cap.
    if not isinstance(cap_mb, (int, float)) or cap_mb <= 0:
        sink.resolve("db_usage_high")
        return None

    total = surface.get("db_total_bytes")
    if not isinstance(total, (int, float)) or total < 0:
        # an unreadable size is NOT a "recovered" edge — leave db_usage_high as-is (no false resolve, no page).
        return None

    if warn_pct is None:
        warn_pct = _db_usage_warn_pct()
    if critical_pct is None:
        critical_pct = _db_usage_critical_pct()
    # keep the thresholds sane even if a caller passed an inverted/odd pair (defensive — env_int already clamps the
    # env path): WARN ≤ CRITICAL, both within (0, 100]. A bad pair degrades to defaults rather than misbehaving.
    try:
        warn_pct = int(warn_pct); critical_pct = int(critical_pct)
    except (TypeError, ValueError):
        warn_pct, critical_pct = 70, 90
    if not (0 < warn_pct <= 100) or not (0 < critical_pct <= 100):
        warn_pct, critical_pct = 70, 90
    if warn_pct > critical_pct:
        warn_pct = critical_pct  # never WARN above CRITICAL (would skip the warning band)

    cap_bytes = int(cap_mb) * 1024 * 1024
    pct = round(total * 100.0 / cap_bytes, 1) if cap_bytes > 0 else 0.0
    pct_int = int(pct)

    if pct >= critical_pct:
        sink.fire("db_usage_high", "critical",
                  f"DB at {pct}% of the {int(cap_mb)} MiB cap (≥ {critical_pct}% CRITICAL) — "
                  f"act now: prune harder or upsize before the disk is full / the bill jumps",
                  {"pct_used": pct, "warn_pct": warn_pct, "critical_pct": critical_pct,
                   "db_total_bytes": int(total), "cap_mb": int(cap_mb)})
    elif pct >= warn_pct:
        sink.fire("db_usage_high", "warning",
                  f"DB at {pct}% of the {int(cap_mb)} MiB cap (≥ {warn_pct}% WARN) — "
                  f"plan a prune or an upsize before it fills",
                  {"pct_used": pct, "warn_pct": warn_pct, "critical_pct": critical_pct,
                   "db_total_bytes": int(total), "cap_mb": int(cap_mb)})
    else:
        sink.resolve("db_usage_high")  # back below the WARN line → re-arm so the next crossing pages again
    return pct_int
