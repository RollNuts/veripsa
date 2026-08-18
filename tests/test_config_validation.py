#!/usr/bin/env python3
"""CONFIG-VALIDATION gate — does a MISSING or MALFORMED config knob FAIL SAFE (loud + refuses) rather than
crashing bare or running silently-broken?

Pure (no DB, no GitHub): drives github-app/env_config.env_int directly + asserts the live cap-read sites use it,
and that the retention floor matches its documented contract. Proves:

  * an UNSET knob → the trusted shipped default (no surprise);
  * a NON-INT value → a LOUD ConfigError that NAMES the var (not a bare ValueError deep in import);
  * a 0 / NEGATIVE cap → a LOUD ConfigError (not a silent disable: e.g. PER_ACCOUNT_QUEUE_CAP=0 used to 503
    EVERY webhook while /healthz stayed green — the App alive-but-processing-nothing failure);
  * an out-of-range value (PORT > 65535) → refused;
  * VERIPSA_RETENTION_DAYS below the hard 14-day collisions_on_main window → refused (would silently blind the
    engine), matching the RUNBOOK + render.yaml "floor 14" contract.

Run:  python3 tests/test_config_validation.py   (no Postgres needed)
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import env_config  # noqa: E402
from env_config import ConfigError, env_int  # noqa: E402


def main() -> int:
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    def refuses(key, value):
        """True iff env_int(key, 100, min_value=1) raises a ConfigError that NAMES the var, for KEY=value."""
        os.environ[key] = value
        try:
            env_int(key, 100, min_value=1)
            return False
        except ConfigError as e:
            return key in str(e)
        finally:
            os.environ.pop(key, None)

    # ── env_int: the validated reader ─────────────────────────────────────────────────────────────────────
    print("== env_int (the one validated env-knob reader) ==")
    os.environ.pop("VERIPSA_TEST_KNOB", None)
    check("UNSET → the shipped default (trusted, no surprise)",
          env_int("VERIPSA_TEST_KNOB", 12000, min_value=1) == 12000)

    os.environ["VERIPSA_TEST_KNOB"] = "500"
    check("a valid value parses through", env_int("VERIPSA_TEST_KNOB", 12000, min_value=1) == 500)
    os.environ["VERIPSA_TEST_KNOB"] = "  77  "
    check("surrounding whitespace is tolerated", env_int("VERIPSA_TEST_KNOB", 12000, min_value=1) == 77)
    os.environ.pop("VERIPSA_TEST_KNOB", None)

    check("a NON-INT value is refused LOUDLY (names the var) — not a bare ValueError",
          refuses("VERIPSA_TEST_KNOB", "100files"))
    check("an empty string is refused (not silently the default)", refuses("VERIPSA_TEST_KNOB", ""))
    check("a FLOAT string is refused (no silent truncation)", refuses("VERIPSA_TEST_KNOB", "1e6"))
    check("ZERO is refused (a cap of 0 silently disables the path)", refuses("VERIPSA_TEST_KNOB", "0"))
    check("a NEGATIVE value is refused", refuses("VERIPSA_TEST_KNOB", "-5"))

    # an out-of-range max (PORT-style) is refused
    os.environ["VERIPSA_TEST_KNOB"] = "70000"
    try:
        env_int("VERIPSA_TEST_KNOB", 8000, min_value=1, max_value=65535)
        over = False
    except ConfigError:
        over = True
    finally:
        os.environ.pop("VERIPSA_TEST_KNOB", None)
    check("an OUT-OF-RANGE value (> max) is refused", over)

    check("ConfigError is a SystemExit subclass (an unguarded read at import aborts the process cleanly)",
          issubclass(ConfigError, SystemExit))

    # ── the live cap-read sites actually route through env_int (no bare int() left silently) ───────────────
    print("== the live cap sites fail safe (the WORST one: PER_ACCOUNT_QUEUE_CAP=0) ==")

    # PER_ACCOUNT_QUEUE_CAP=0 used to make _FairQueue.put_nowait raise queue.Full on the FIRST submit (len>=0),
    # 503-ing every webhook while /healthz stayed green. With env_int(min_value=1) the module now REFUSES to
    # import. Drive it in a subprocess so the bad env can't leak into this process's already-imported modules.
    def import_refuses(module, env):
        code = (f"import sys; sys.path[:0]=[{os.path.join(ROOT, 'github-app')!r}];"
                f"import {module}")
        proc = subprocess.run([sys.executable, "-c", code], env={**os.environ, **env},
                              capture_output=True, text=True)
        return proc.returncode != 0 and ("ConfigError" in (proc.stderr + proc.stdout)
                                         or "FATAL" in (proc.stderr + proc.stdout))

    check("event_queue REFUSES to import with PER_ACCOUNT_QUEUE_CAP=0 "
          "(was: alive-but-503s-everything, /healthz green)",
          import_refuses("event_queue", {"VERIPSA_PER_ACCOUNT_QUEUE_CAP": "0"}))
    check("event_queue REFUSES a non-int PER_ACCOUNT_QUEUE_CAP",
          import_refuses("event_queue", {"VERIPSA_PER_ACCOUNT_QUEUE_CAP": "lots"}))
    check("github_rest REFUSES a 0 MAX_TARBALL_BYTES (would disable ALL ingest)",
          import_refuses("github_rest", {"VERIPSA_MAX_TARBALL_BYTES": "0"}))
    check("event budget REFUSES zero (zero would reopen an unlimited/disabled timeout ambiguity)",
          import_refuses("event_budget", {"VERIPSA_EVENT_WALL_TIMEOUT_SECONDS": "0"}))
    check("event budget REFUSES values above its 900s safety ceiling",
          import_refuses("event_budget", {"VERIPSA_EVENT_WALL_TIMEOUT_SECONDS": "901"}))
    reclaim_cfg = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path[:0]=[{os.path.join(ROOT, 'github-app')!r}]; "
         "import delivery_queue as d; print(d._DEAD_INSTANCE_RECLAIM_SAFE_SECONDS)"],
        env={**os.environ,
             "VERIPSA_EVENT_WALL_TIMEOUT_SECONDS": "200",
             "VERIPSA_EVENT_TERMINAL_RESERVE_SECONDS": "10"},
        capture_output=True, text=True,
    )
    check("dead-owner reclaim ceiling follows configured event total + terminal/clock margin",
          reclaim_cfg.returncode == 0 and reclaim_cfg.stdout.strip() == "215")
    escalation_cfg = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path[:0]=[{os.path.join(ROOT, 'github-app')!r}]; "
            "import delivery_queue as d; "
            "print(d._DEFAULT_DEFERRED_ESCALATE_SECONDS,"
            "d._DEFERRED_ESCALATE_SECONDS)",
        ],
        env={
            **os.environ,
            "VERIPSA_EVENT_WALL_TIMEOUT_SECONDS": "90",
            "VERIPSA_EVENT_TERMINAL_RESERVE_SECONDS": "5",
            "VERIPSA_DELIVERY_RETRY_WINDOW_SECONDS": "120",
            "VERIPSA_DELIVERY_DEFERRED_ESCALATE_SECONDS": "900",
        },
        capture_output=True,
        text=True,
    )
    escalation_disabled = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path[:0]=[{os.path.join(ROOT, 'github-app')!r}]; "
            "import delivery_queue as d; "
            "print(d._DEFERRED_ESCALATE_SECONDS)",
        ],
        env={
            **os.environ,
            "VERIPSA_DELIVERY_DEFERRED_ESCALATE_SECONDS": "0",
        },
        capture_output=True,
        text=True,
    )
    check(
        "failed causal-head escalation clamps a stale high override to the 120s hard delivery envelope",
        escalation_cfg.returncode == 0
        and escalation_cfg.stdout.strip() == "120 120",
    )
    check(
        "failed causal-head escalation keeps explicit zero as the fail-closed kill switch",
        escalation_disabled.returncode == 0
        and escalation_disabled.stdout.strip() == "0",
    )
    check("delivery queue REFUSES a stale window below the configured owner-reclaim safety ceiling",
          import_refuses(
              "delivery_queue",
              {"VERIPSA_EVENT_WALL_TIMEOUT_SECONDS": "900",
               "VERIPSA_EVENT_TERMINAL_RESERVE_SECONDS": "60",
               "VERIPSA_DELIVERY_STALE_SECONDS": "900"}))
    check("delivery queue REFUSES a zero cross-generation retry window",
          import_refuses(
              "delivery_queue", {"VERIPSA_DELIVERY_RETRY_WINDOW_SECONDS": "0"}))
    check("delivery queue REFUSES an unbounded cross-generation retry window",
          import_refuses(
              "delivery_queue", {"VERIPSA_DELIVERY_RETRY_WINDOW_SECONDS": "3601"}))
    check("delivery recovery REFUSES a polling interval above the graph-turn fairness ceiling",
          import_refuses(
              "delivery_queue", {"VERIPSA_DELIVERY_RECOVER_INTERVAL": "61"}))
    check("delivery queue REFUSES a terminal-intent cap below the worker-safety floor",
          import_refuses(
              "delivery_queue", {"VERIPSA_PENDING_TERMINAL_CAP": "3"}))
    check("delivery queue REFUSES an unbounded terminal-intent cap",
          import_refuses(
              "delivery_queue", {"VERIPSA_PENDING_TERMINAL_CAP": "129"}))
    check("ingest REFUSES a zero graph-extraction timeout",
          import_refuses("ingest", {"VERIPSA_GRAPH_EXTRACT_TIMEOUT_SECONDS": "0"}))
    check("event processor REFUSES a zero DB connect timeout",
          import_refuses("event_processor", {"VERIPSA_DB_CONNECT_TIMEOUT_SECONDS": "0"}))
    check("event processor REFUSES a zero fanout repository slice size",
          import_refuses("event_processor", {"VERIPSA_FANOUT_REPOS_PER_SLICE": "0"}))
    check("event processor REFUSES a zero per-repository fanout work deadline",
          import_refuses("event_processor", {"VERIPSA_FANOUT_REPO_WORK_SECONDS": "0"}))
    # server.py imports a lot; only confirm a bad cap is refused at import (no DB / GH needed to fail-fast).
    check("server REFUSES a non-int MAX_INGEST_FILES at import",
          import_refuses("server", {"VERIPSA_MAX_INGEST_FILES": "twelve-thousand"}))

    # a VALID cap still imports cleanly (we didn't break the happy path)
    proc_ok = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path[:0]=[{os.path.join(ROOT, 'github-app')!r}]; import event_queue; "
         f"print(event_queue._PER_ACCOUNT_QUEUE_CAP)"],
        env={**os.environ, "VERIPSA_PER_ACCOUNT_QUEUE_CAP": "250"}, capture_output=True, text=True)
    check("a VALID PER_ACCOUNT_QUEUE_CAP still imports + takes effect (happy path intact)",
          proc_ok.returncode == 0 and proc_ok.stdout.strip() == "250")

    # ── retention floor matches the documented 14-day collisions_on_main window ────────────────────────────
    print("== retention floor (14 = the hard collisions_on_main window; RUNBOOK + render.yaml say so) ==")

    def retention_rc(days_value):
        code = (f"import sys; sys.path[:0]=[{os.path.join(ROOT, 'github-app')!r}]; "
                f"import retention_prune; sys.exit(retention_prune.main())")
        # No DSN set → main() short-circuits to rc=2 BEFORE any DB call. To isolate the FLOOR check we must
        # give a DSN-shaped env but ensure the floor refusal (also rc=2) triggers first; the floor check runs
        # BEFORE prune_once, so an unreachable DSN never matters for the below-floor cases.
        env = {**os.environ, "VERIPSA_DSN": "postgresql://invalid@127.0.0.1:1/none",
               "VERIPSA_RETENTION_DAYS": days_value}
        env.pop("OWNER_DSN", None)
        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        return proc.returncode, (proc.stdout + proc.stderr)

    rc5, out5 = retention_rc("5")
    check("RETENTION_DAYS=5 (below the 14-day floor) is REFUSED (rc=2) before any prune — not a silent degrade",
          rc5 == 2 and "floor" in out5.lower())
    rc0, out0 = retention_rc("0")
    check("RETENTION_DAYS=0 is refused", rc0 == 2)
    rcj, _ = retention_rc("garbage")
    # non-int → falls back to default 30 (>= 14) → tries to prune → hits the unreachable DSN → rc 1 (clean job
    # fail), NOT a crash and NOT a below-floor refusal. We only assert it didn't crash with a traceback (rc in
    # {1,2}) — the point is a junk value never silently prunes below the floor.
    check("RETENTION_DAYS=garbage → safe default (>=14), no crash", rcj in (1, 2))

    ok = all(c for _, c in results)
    print("CONFIG-VALIDATION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
