#!/usr/bin/env bash
# _ephemeral_pg.sh — stand up a PRIVATE, throwaway Postgres cluster for ONE gate run, then tear it down.
#
# WHY: the gate suite (run_gates.sh / db/bootstrap_local.sh / db/smoke.sh / every tests/*.py) creates
# per-PID SCRATCH databases. Historically those landed on the developer's SHARED local postmaster, so when
# several agents ran gates in parallel (the project's multi-agent workflow) they all pounded ONE postmaster:
#   (a) CPU/IO contention made every gate crawl,
#   (b) scratch DBs LEAKED by the hundreds when a run was killed (catalog bloat → everything slower),
#   (c) flaky/transient errors under load looked like real gate FAILs (this corrupted a main-red diagnosis).
# Local Postgres is ONLY the test substrate (production runs managed Postgres), so each run should get its OWN
# fully-isolated, disposable cluster — a private universe with ZERO shared state.
#
# HOW: initdb a unique temp datadir, start a postmaster on a UNIQUE TCP port (loopback only) + a private unix
# socket dir, then export the libpq env so EVERY gate (shell + python) lands on THIS cluster without touching
# any source DSN:
#   - PGHOST  → our private socket dir   ⇒ bare clients (createdb/dropdb/psql with no -h, no URI) use it.
#   - PGPORT  → our unique port          ⇒ the gates' `postgresql://role@localhost/db` URIs keep host=localhost
#                                          (libpq IGNORES PGHOST when the URI names a host) but inherit the port,
#                                          so localhost (both 127.0.0.1 AND ::1) reaches THIS postmaster.
# A trap on EXIT/INT/TERM `pg_ctl stop -m immediate` + `rm -rf` the datadir, so even a `kill` tears it down.
#
# USAGE (source it, don't exec it — it must export into the caller's shell + own the trap):
#   source db/_ephemeral_pg.sh
#   ephemeral_pg_start            # blocks a few seconds until ready; exports PGHOST/PGPORT/PGDATABASE
#   ... run gates ...             # teardown happens automatically on exit (trap)
#
# Opt-out (use the caller's already-running shared postmaster instead, e.g. a CI service container):
#   VERIPSA_EPHEMERAL_PG=0 bash run_gates.sh

# --- guard: only meaningful when sourced (we export + trap into the caller) -------------------------------
if [ "${VERIPSA_EPHEMERAL_PG:-1}" = "0" ]; then
  # explicit opt-out: leave the environment as-is (caller points us at a shared/managed postmaster).
  ephemeral_pg_start() { echo "  [ephemeral-pg] disabled (VERIPSA_EPHEMERAL_PG=0) — using the ambient postmaster (PGHOST=${PGHOST:-default} PGPORT=${PGPORT:-default})"; }
  ephemeral_pg_teardown() { :; }
  return 0 2>/dev/null || true
fi

# State for the trap (set by ephemeral_pg_start). Kept at file scope so the trap can see them.
_EPH_DATADIR=""
_EPH_STARTED=0

ephemeral_pg_teardown() {
  # idempotent: safe to call from the trap AND explicitly. Never errors out the caller.
  if [ "${_EPH_STARTED}" = "1" ] && [ -n "${_EPH_DATADIR}" ] && [ -d "${_EPH_DATADIR}/data" ]; then
    pg_ctl -D "${_EPH_DATADIR}/data" -m immediate stop >/dev/null 2>&1 || true
  fi
  if [ -n "${_EPH_DATADIR}" ] && [ -d "${_EPH_DATADIR}" ]; then
    # the whole private universe (datadir + private socket dir + logs) lives under ONE temp dir → one rm.
    rm -rf "${_EPH_DATADIR}" 2>/dev/null || true
  fi
  _EPH_STARTED=0
}

# Pick a free loopback TCP port (bind-test so concurrent runs never collide on the same port).
_eph_free_port() {
  # try a handful of random high ports; succeed on the first that nothing is bound to.
  local p
  for _ in $(seq 1 50); do
    p=$(( 49152 + RANDOM % 16000 ))   # IANA dynamic/ephemeral range 49152..65535
    # a TCP bind-test on BOTH loopbacks via python (portable on macOS; no `ss`/`lsof` dependency).
    if python3 - "$p" <<'PY' 2>/dev/null
import socket, sys
p = int(sys.argv[1])
for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
    try:
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind((addr, p))
        s.close()
    except OSError:
        sys.exit(1)
sys.exit(0)
PY
    then
      echo "$p"; return 0
    fi
  done
  echo "  [ephemeral-pg] could not find a free loopback port after 50 tries" >&2
  return 1
}

ephemeral_pg_start() {
  command -v initdb >/dev/null 2>&1 || { echo "  [ephemeral-pg] FATAL: initdb not on PATH (install Postgres)"; return 1; }
  command -v pg_ctl >/dev/null 2>&1 || { echo "  [ephemeral-pg] FATAL: pg_ctl not on PATH"; return 1; }

  _EPH_DATADIR="$(mktemp -d "${TMPDIR:-/tmp}/veripsa-eph-pg.XXXXXX")" || { echo "  [ephemeral-pg] FATAL: mktemp failed"; return 1; }
  local sock="${_EPH_DATADIR}/sock"
  mkdir -p "${sock}"

  # initdb: trust auth (local-only loopback cluster, never reachable off-box), current OS user as superuser.
  # --no-sync trades crash-durability for speed — correct for a throwaway test cluster we rm at the end.
  if ! initdb -D "${_EPH_DATADIR}/data" -U "$(whoami)" -A trust --no-sync --no-instructions \
        >"${_EPH_DATADIR}/initdb.log" 2>&1; then
    echo "  [ephemeral-pg] FATAL: initdb failed:"; tail -8 "${_EPH_DATADIR}/initdb.log"
    ephemeral_pg_teardown; return 1
  fi

  # Arm the trap BEFORE start, so a kill between start and ready still tears the cluster down.
  # EXIT ONLY: bash runs the EXIT trap on normal exit AND when the shell dies from an UNCAUGHT fatal signal
  # (SIGINT/SIGTERM/SIGHUP) — so a `kill` still tears the cluster down — WITHOUT us catching those signals
  # explicitly. Catching them explicitly is actively wrong here: in this runner harness a stray, harmless
  # SIGHUP/SIGINT arrives mid-startup and an explicit signal trap would fire teardown prematurely (ripping the
  # cluster out from under the gates that are about to run). EXIT-only is both correct AND robust.
  trap 'ephemeral_pg_teardown' EXIT

  # Start the postmaster, RETRYING on a fresh port if the chosen one was lost to a concurrent run. The free-port
  # bind-test is inherently TOCTOU: two sibling runs can both see the same high port free, then both try to bind
  # it — the loser's `pg_ctl start` fails to bind. So pick→start in a loop; a lost race just re-rolls a new port
  # (never collides with the winner, never leaks — each attempt's failed start left nothing bound).
  #  - Both loopbacks (127.0.0.1 + ::1) so a `localhost` URI resolving to either family reaches us.
  #  - lc_messages=en_US.UTF-8: the gates assert on ENGLISH Postgres error TEXT (e.g. "permission denied for
  #    function ..."). A fresh initdb inherits the developer's OS locale (here ja_JP) → LOCALIZED errors → every
  #    English-text assertion silently FAILS. Pin the message locale to the managed/CI Postgres the regexes target.
  #  - fsync/full_page_writes/synchronous_commit off: a fast, disposable test cluster (we rm it at the end).
  local port started=0 attempt
  for attempt in 1 2 3 4 5; do
    port="$(_eph_free_port)" || { ephemeral_pg_teardown; return 1; }
    if pg_ctl -D "${_EPH_DATADIR}/data" -w -t 30 \
          -o "-p ${port} -k ${sock} -c listen_addresses=127.0.0.1,::1 -c lc_messages=en_US.UTF-8 -c fsync=off -c full_page_writes=off -c synchronous_commit=off" \
          -l "${_EPH_DATADIR}/postmaster.log" start >/dev/null 2>&1; then
      started=1; break
    fi
    # start failed (likely a raced port) — make sure nothing half-started, then re-roll a fresh port.
    pg_ctl -D "${_EPH_DATADIR}/data" -m immediate stop >/dev/null 2>&1 || true
  done
  if [ "${started}" != "1" ]; then
    echo "  [ephemeral-pg] FATAL: postmaster failed to start after 5 port attempts:"; tail -12 "${_EPH_DATADIR}/postmaster.log"
    ephemeral_pg_teardown; return 1
  fi
  _EPH_STARTED=1

  # Export the libpq env so EVERY downstream gate (shell + python, bare-client + @localhost URI) lands here.
  export PGHOST="${sock}"   # bare clients use the private socket
  export PGPORT="${port}"   # @localhost URIs inherit the port → loopback TCP to THIS cluster
  unset PGDATABASE 2>/dev/null || true   # never pin a default db; each gate names its own scratch DB

  # The bootstrap runs db/roles.sql against ADMIN_DSN; default it at THIS cluster's maintenance db.
  # (As the OS-superuser over loopback, with trust auth — exactly what roles.sql needs.)
  export ADMIN_DSN="postgresql://$(whoami)@localhost/postgres"

  # An EXPLICIT-port admin DSN at OUR cluster's maintenance db. We use this (not `@localhost` via PGPORT) for the
  # setup statements below so we are UNAMBIGUOUSLY talking to THIS cluster even while a sibling concurrent run is
  # standing up its own — no reliance on the just-exported PGPORT, no chance of crossing wires during startup.
  local admin="postgresql://$(whoami)@127.0.0.1:${port}/postgres"

  # Sanity + settle: confirm we can actually reach it. A just-started postmaster under heavy parallel load may
  # need a few hundred ms before it accepts the FIRST client cleanly, so RETRY briefly (fail LOUD here, not as a
  # confusing gate error later). pg_ctl -w already waited for "ready", but the first real connection can still
  # race the postmaster's final warm-up when the box is saturated by sibling runs.
  local i ready=0
  for i in $(seq 1 30); do
    if psql "${admin}" -tAc "SELECT 1" >/dev/null 2>&1; then ready=1; break; fi
    sleep 0.2
  done
  if [ "${ready}" != "1" ]; then
    echo "  [ephemeral-pg] FATAL: cluster started but is unreachable on 127.0.0.1:${port}:"; tail -12 "${_EPH_DATADIR}/postmaster.log"
    ephemeral_pg_teardown; return 1
  fi

  # PRE-CREATE the two roles the gates CONNECT AS but db/roles.sql leaves NOLOGIN by design.
  # roles.sql intentionally creates veripsa_migrator + veripsa_app NOLOGIN — a real deploy grants them LOGIN +
  # a password by hand (never bakes a credential into the repo). The gate runner, though, connects AS
  # veripsa_migrator (apply schema) and veripsa_app (the App writer) over loopback trust auth. On a SHARED
  # postmaster that LOGIN was granted once, long ago, by hand — which is exactly why the leak/contention was
  # invisible: the cluster carried hidden hand-set state. A fresh ephemeral cluster has none, so we grant LOGIN
  # HERE, scoped to this throwaway loopback-only trust cluster (no password, never reachable off-box). roles.sql
  # runs next: its `CREATE ROLE ... IF NOT EXISTS` SKIPS these (so LOGIN is preserved) and its GRANTs still apply.
  # Retry under load (the same warm-up race), and on final failure SHOW the real error (don't swallow it).
  local granted=0 grant_err=""
  for i in 1 2 3 4 5; do
    if grant_err="$(psql "${admin}" -v ON_ERROR_STOP=1 -q -c "
      DO \$\$
      BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='veripsa_migrator') THEN CREATE ROLE veripsa_migrator LOGIN; ELSE ALTER ROLE veripsa_migrator LOGIN; END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='veripsa_app')      THEN CREATE ROLE veripsa_app LOGIN;      ELSE ALTER ROLE veripsa_app LOGIN;      END IF;
      END \$\$;" 2>&1)"; then granted=1; break; fi
    sleep 0.3
  done
  if [ "${granted}" != "1" ]; then
    echo "  [ephemeral-pg] FATAL: could not grant LOGIN to the gate roles on the fresh cluster: ${grant_err}"
    ephemeral_pg_teardown; return 1
  fi

  echo "  [ephemeral-pg] private cluster up: port=${port} datadir=${_EPH_DATADIR} (auto-torn-down on exit)"
}
