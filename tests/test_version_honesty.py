#!/usr/bin/env python3
"""VERSION HONESTY gate — /healthz reports the build it is ACTUALLY running, derived (never a hand-bumped lie).

The bug this locks out: /healthz used to report a MANUAL env string (VERIPSA_VERSION="render-12") that nobody
bumped, so right after deploying new code /healthz still claimed the OLD build — a health endpoint that
misreports which build is live (it confused even the operator). The fix derives the label from the GIT COMMIT
baked at build time, so it is automatic + always honest.

Proves on the REAL health_watchdog.build_version() + health_snapshot() (pure, offline, no DB, no network):
  (1) the BUILD SHA wins — when the build-time sha (VERIPSA_BUILD_SHA) is present, /healthz reports IT;
  (2) the file fallback — with no env sha but a baked /app/BUILD_SHA file, /healthz reports the file's sha;
  (3) RENDER_GIT_COMMIT — Render injects the RUNNING commit into the runtime env automatically; with no build-sha
      source, /healthz reports it (shortened). This is the rebuild-free safety net that needs NO wiring, and the
      assertion that LOCKS OUT the regression: RENDER_GIT_COMMIT set + VERIPSA_VERSION unset => the version is the
      real (short) commit, never a hardcoded label. It beats the legacy env; the explicit build sha beats it;
  (4) the legacy env is a LAST resort — only used when no derived source (build-sha / file / RENDER_GIT_COMMIT) resolves;
  (5) DEGRADES, never crashes — with NOTHING set (no sha env, no file, no render commit, no legacy), it returns
      "dev" and the whole health_snapshot still builds (a missing build label is honest "dev", never an exception);
  (6) precedence is strict — the derived sources BEAT the legacy env (a deploy can't be masked by a stale manual label);
  (7) content-free — the label is a short commit sha (no secrets, no customer data).

Run:  python3 tests/test_version_honesty.py
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import health_watchdog as H  # noqa: E402

FAIL = 0


def chk(c, label):
    global FAIL
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    if not c:
        FAIL = 1


class _FakeWorker:
    """Minimal stand-in so health_snapshot() builds without a real worker/DB — version is the only concern."""
    def is_alive(self): return True
    def inflight_age(self): return None
    def qsize(self): return 0
    def maxsize(self): return 1000
    def processed(self): return 0
    def failed(self): return 0
    def uptime(self): return 1.0


def _clear_version_env():
    for k in ("VERIPSA_BUILD_SHA", "RENDER_GIT_COMMIT", "VERIPSA_VERSION"):
        os.environ.pop(k, None)


def _with_build_sha_file(sha: str):
    """Write the baked BUILD_SHA file at the exact path build_version() reads, returning a cleanup callable."""
    path = os.path.abspath(H._BUILD_SHA_FILE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(sha + "\n")

    def _cleanup():
        try:
            os.remove(path)
        except OSError:
            pass
    return _cleanup


def main() -> int:
    dockerfile = (
        Path(ROOT) / "github-app" / "Dockerfile"
    ).read_text(encoding="utf-8")
    chk(
        'ARG RENDER_GIT_COMMIT=""' in dockerfile
        and 'BUILD_SHA_CANDIDATE="$RENDER_GIT_COMMIT"' in dockerfile
        and '${#BUILD_SHA_CANDIDATE}" -ne 40' in dockerfile
        and "*[!0-9a-f]*" in dockerfile
        and "> /app/BUILD_SHA" in dockerfile,
        "Docker artifact bakes Render's exact 40-hex commit for independent cutover proof",
    )

    # Make the test hermetic: a BUILD_SHA file already on disk (e.g. a baked image) would shadow the env cases.
    preexisting = os.path.exists(os.path.abspath(H._BUILD_SHA_FILE))
    chk(not preexisting, "no stray BUILD_SHA file shadows the test (hermetic env)")

    # (1) BUILD SHA env wins — the derived, automatic, honest source.
    _clear_version_env()
    os.environ["VERIPSA_BUILD_SHA"] = "a1b2c3d"
    chk(H.build_version() == "a1b2c3d", "build sha env (VERIPSA_BUILD_SHA) is reported")
    chk(_FakeWorker and H.health_snapshot(_FakeWorker())["version"] == "a1b2c3d",
        "health_snapshot()['version'] surfaces the build sha (the /healthz body)")
    chk(H.health_snapshot(_FakeWorker())["repository_offboarding_protocol"] == 2,
        "/healthz advertises the rollback-compatible repository offboarding protocol")
    chk(H.health_snapshot(_FakeWorker())["durable_retry_protocol"] == 3,
        "/healthz advertises the absolute durable retry-window protocol")

    # (5) precedence — the build sha BEATS a stale legacy label (a deploy cannot be masked by an old manual env).
    os.environ["VERIPSA_VERSION"] = "render-12"
    chk(H.build_version() == "a1b2c3d", "build sha OVERRIDES a stale legacy VERIPSA_VERSION (deploy is never masked)")

    # (2) file fallback — no env sha, but a baked /app/BUILD_SHA file (the Dockerfile-baked artifact path).
    _clear_version_env()
    cleanup = _with_build_sha_file("f00dface")
    try:
        chk(H.build_version() == "f00dface", "baked BUILD_SHA file is read when no build-sha env is set")
        # legacy env present but still loses to the baked file (file is a derived source, env is the last resort).
        os.environ["VERIPSA_VERSION"] = "render-12"
        chk(H.build_version() == "f00dface", "baked BUILD_SHA file OVERRIDES the legacy VERIPSA_VERSION too")
    finally:
        cleanup()
        _clear_version_env()

    # (3) RENDER_GIT_COMMIT — Render injects the RUNNING commit into the runtime env automatically. With no
    #     build-sha env and no baked file, /healthz reports IT (the rebuild-free safety net), SHORTENED to a
    #     content-free prefix. This is the assertion that locks out the regression the task names: when
    #     RENDER_GIT_COMMIT is set and VERIPSA_VERSION is unset, the reported version is the (short) commit,
    #     NEVER a hardcoded label.
    _clear_version_env()
    full_sha = "0123456789abcdef0123456789abcdef01234567"  # a full 40-char sha, as Render sets it
    os.environ["RENDER_GIT_COMMIT"] = full_sha
    short = full_sha[:H._RENDER_SHA_LEN]
    chk(H.build_version() == short, "RENDER_GIT_COMMIT is read directly from the runtime env (shortened) when no build-sha source is set")
    chk(H.health_snapshot(_FakeWorker())["version"] == short,
        "health_snapshot()['version'] surfaces the deployed commit from RENDER_GIT_COMMIT (NOT a hardcoded label)")
    chk(short != "render-12" and short != "dev", "the reported version is the real commit, not a stale manual label")
    # precedence — RENDER_GIT_COMMIT (a DERIVED source) BEATS a stale legacy VERIPSA_VERSION.
    os.environ["VERIPSA_VERSION"] = "render-12"
    chk(H.build_version() == short, "RENDER_GIT_COMMIT OVERRIDES a stale legacy VERIPSA_VERSION (deploy is never masked)")
    # precedence — but the explicit build-sha env still wins over RENDER_GIT_COMMIT (the build's own baked commit).
    os.environ["VERIPSA_BUILD_SHA"] = "a1b2c3d"
    chk(H.build_version() == "a1b2c3d", "VERIPSA_BUILD_SHA still wins over RENDER_GIT_COMMIT (explicit build sha is primary)")
    _clear_version_env()

    # (4) legacy env is the LAST resort — used only when no derived source resolves.
    os.environ["VERIPSA_VERSION"] = "render-12"
    chk(H.build_version() == "render-12", "legacy VERIPSA_VERSION is honored ONLY as the last-resort fallback")

    # (4) DEGRADES, never crashes — nothing set at all → honest "dev", and the snapshot still builds.
    _clear_version_env()
    try:
        v = H.build_version()
        crashed = False
    except Exception as e:  # pragma: no cover — the whole point is this never raises
        v, crashed = None, True
        print("  [FAIL] build_version() raised: " + str(e))
    chk(not crashed and v == "dev", "with NOTHING set it returns 'dev' (degrades, never crashes)")
    try:
        snap = H.health_snapshot(_FakeWorker())
        snap_ok = snap.get("version") == "dev"
    except Exception as e:
        snap_ok = False
        print("  [FAIL] health_snapshot() raised with no version source: " + str(e))
    chk(snap_ok, "health_snapshot() still builds with no version source ('dev', no crash)")

    # whitespace tolerance — a baked file/env with a trailing newline must not leak whitespace into the label.
    os.environ["VERIPSA_BUILD_SHA"] = "  deadbee \n"
    chk(H.build_version() == "deadbee", "the label is trimmed (no stray whitespace from a baked sha)")
    _clear_version_env()

    # (6) content-free — a short commit sha is the contract: no '/', no '@', no secret-shaped material.
    os.environ["VERIPSA_BUILD_SHA"] = "a1b2c3d"
    v = H.build_version()
    chk("/" not in v and "@" not in v and len(v) <= 64, "the label is a short, content-free token (no path/secret shape)")
    _clear_version_env()

    print("VERSION HONESTY GATE: " + ("PASS" if FAIL == 0 else "FAIL"))
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
