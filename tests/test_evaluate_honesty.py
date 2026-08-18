#!/usr/bin/env python3
"""evaluate.py HONESTY gate — the TRY-BEFORE-YOU-BUY artifact a prospect runs on THEIR repo. Honesty here is
existential (it is a SALES artifact), so this locks the three properties end-to-end ON REAL CRAFTED GIT REPOS
(the existing tests/test_evaluate*.py feed SYNTHETIC analyze() results and never exercise the real git path):

  (1) CONTENT-FREE (cardinal): run the WHOLE tool on a repo whose file BODIES and COMMIT MESSAGES each contain
      a secret; assert neither secret appears in stdout, stderr, OR the written --out report. evaluate.py reads
      ONLY paths + `git log --name-only` — nothing else may leak.
  (2) NEVER-CRASH + HONEST N/A: an empty (0-commit) repo, a non-git directory, a single-file repo, and a tiny
      flat repo must each report an HONEST N/A (not a fabricated number, not a Python stack trace) and exit 0
      (N/A is the wrong repo SHAPE for the differentiated proof, not a failure). [Regression-locks the real
      crash this gate's PR fixed: the engine's random-baseline sampling raised ValueError on <2 files-with-
      history; evaluate.py now pre-detects a degenerate repo and reports N/A instead.]
  (3) HONEST CLAIMS / no fragile-number / exit-code contract: the report ALWAYS prints the "does NOT prove
      rework-hours-saved" disclaimer and the content-free claim; it NEVER overclaims (no "guarantee"/"prevents
      bugs"/"correctness"); it NEVER prints a fragile "inf×" multiplier — even on the applicable branch where a
      zero control baseline would otherwise divide-by-zero. Exit 0 iff every applicable repo clears the bar.

Hermetic + OFFLINE: builds throwaway git repos in a tempdir with an isolated git config; no network, no
Postgres. Mirrors the heavy real-repo backtest proven separately in tests/backtest_cochange.py.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import evaluate as E  # noqa: E402

# Distinct secrets so a leak names its own channel. Bodies AND commit messages are content evaluate.py must
# never surface (it reads `git log --name-only` = filenames only).
SECRET_BODY = "VERIPSA_HONESTY_SECRET_BODY_3f9a2c_DO_NOT_LEAK"
SECRET_MSG = "VERIPSA_HONESTY_SECRET_COMMITMSG_7b1e8d_DO_NOT_LEAK"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,  # hermetic: ignore the host's git config
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=_GIT_ENV)


def _init(parent, name):
    repo = os.path.join(parent, name)
    os.makedirs(repo, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    return repo


def _write(repo, rel, body):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p) or repo, exist_ok=True)
    with open(p, "w") as fh:
        fh.write(body)


def _run_eval(args):
    """Invoke evaluate.main() in-process capturing stdout+stderr; return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = E.main(["evaluate.py", *args])
    return rc, out.getvalue(), err.getvalue()


def _make_secret_multidir_repo(parent):
    """A multi-directory repo with REAL co-changing history, with the secrets embedded in EVERY file body and
    EVERY commit message — so a content leak via either channel would be caught."""
    repo = _init(parent, "secret_repo")
    n = 22
    for i in range(n):
        _write(repo, f"svc/handler{i}.py",
               f"# {SECRET_BODY}\nfrom core.util{i} import helper{i}\ndef act{i}():\n    return helper{i}()\n")
        _write(repo, f"core/util{i}.py", f"# {SECRET_BODY}\ndef helper{i}():\n    return {i}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"{SECRET_MSG} initial")
    for rnd in range(3):                      # co-change the coupled pairs together a few times
        for i in range(n):
            _write(repo, f"svc/handler{i}.py",
                   f"# {SECRET_BODY} v{rnd}\nfrom core.util{i} import helper{i}\n"
                   f"def act{i}():\n    return helper{i}() + {rnd}\n")
            _write(repo, f"core/util{i}.py", f"# {SECRET_BODY} v{rnd}\ndef helper{i}():\n    return {i} + {rnd}\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", f"{SECRET_MSG} change {rnd}.{i}")
    return repo


def _result(c_rate, r_rate, cx_rate, rx_rate, sd_rate=0.8, pairs_x=30, files=40, repo="demo"):
    """A synthetic analyze()-shaped result (rate, median, n) — for deterministic report-branch assertions that
    must not depend on a statistical git baseline (the existing evaluate gates use the same helper)."""
    def stat(rate):
        return (rate, 0.0, 10)
    return {
        "repo": repo, "files": files, "commits": 60, "pairs": 50, "pairs_x": pairs_x,
        "coupled": stat(c_rate), "rnd": stat(r_rate), "samedir": stat(sd_rate),
        "coupled_x": stat(cx_rate), "rnd_x": stat(rx_rate),
        "by_type": {t: (stat(0.5), 5) for t in ("import", "call", "schema", "config")},
    }


# Overclaim denylist — words a TRUTHFUL advisory tool must never use about itself in a buyer-facing report.
_OVERCLAIM = ("guarantee", "guaranteed", "prevents bugs", "prevent bugs", "bug-free", "bugfree",
              "proves correctness", "prove correctness", "correctness guaranteed", "100% accurate",
              "never miss", "zero false positives", "eliminates rework", "guarantees correctness")


def main() -> int:
    checks = []
    work = tempfile.mkdtemp(prefix="veripsa_eval_honesty_")
    try:
        # ── (1) CONTENT-FREE — the cardinal property. Whole-tool run on a secret-bearing real repo. ──────────
        repo = _make_secret_multidir_repo(work)
        out_md = os.path.join(work, "report.md")
        rc, out, err = _run_eval([repo, "--out", out_md])
        report_file = open(out_md).read() if os.path.exists(out_md) else ""
        surfaced = out + "\n" + err + "\n" + report_file
        checks.append(("content-free: secret FILE BODY never appears in stdout/stderr/report",
                       SECRET_BODY not in surfaced))
        checks.append(("content-free: secret COMMIT MESSAGE never appears in stdout/stderr/report",
                       SECRET_MSG not in surfaced))
        checks.append(("content-free: a --out report file was produced", bool(report_file)))
        checks.append(("content-free: report still asserts content-freedom to the buyer",
                       "content-free" in report_file.lower()))
        checks.append(("content-free: whole-tool run did not crash (exit 0/1, not an exception)",
                       rc in (0, 1)))

        # ── (2) NEVER-CRASH + HONEST N/A on degenerate repos (regression-lock of the real crash this PR fixed).
        empty = _init(work, "empty_repo")                                  # git init, ZERO commits
        nongit = os.path.join(work, "nongit_dir"); os.makedirs(nongit); _write(nongit, "x.py", "def z():\n    return 0\n")
        single = _init(work, "single_repo"); _write(single, "only.py", "def a():\n    return 1\n")
        _git(single, "add", "-A"); _git(single, "commit", "-q", "-m", "x")
        tiny = _init(work, "tiny_repo")
        _write(tiny, "a.py", "def a():\n    return 1\n"); _write(tiny, "b.py", "def b():\n    return 2\n")
        _git(tiny, "add", "-A"); _git(tiny, "commit", "-q", "-m", "x")

        for label, path in [("empty (0 commits)", empty), ("non-git dir", nongit),
                            ("single-file", single), ("tiny flat", tiny)]:
            crashed = False
            try:
                rc, out, err = _run_eval([path])
            except BaseException as exc:  # noqa: BLE001 — a crash here is the exact defect we lock against
                crashed = True
                rc, out, err = None, "", f"{type(exc).__name__}: {exc}"
            checks.append((f"never-crash: {label} repo does NOT raise", not crashed))
            checks.append((f"honest N/A: {label} repo reports N/A", (not crashed) and "N/A" in out))
            checks.append((f"honest N/A: {label} repo exits 0 (N/A is not a failure)", rc == 0))
            checks.append((f"honest N/A: {label} repo prints NO fragile 'inf×'/'inf x'",
                           (not crashed) and "inf×" not in out and "inf x" not in out.lower()))

        # ── (3) HONEST CLAIMS, no fragile number, exit-code contract — deterministic via synthetic verdicts. ──
        strong = E._verdict(_result(c_rate=0.60, r_rate=0.15, cx_rate=0.45, rx_rate=0.10))
        md = E._report_md([strong])
        checks.append(("honest claims: disclaimer 'does NOT prove rework-hours-saved' ALWAYS present",
                       "does NOT prove rework-hours-saved" in md))
        checks.append(("honest claims: report states what it DOES prove (the SIGNAL)",
                       "SIGNAL IS REAL" in md))
        low = md.lower()
        checks.append(("honest claims: NO overclaim words (guarantee/prevents bugs/correctness/…)",
                       not any(w in low for w in _OVERCLAIM)))

        # fragile-number lock on the APPLICABLE branch: a zero loose-random baseline must NOT divide-to-'inf×'.
        inf_case = E._verdict(_result(c_rate=0.60, r_rate=0.0, cx_rate=0.45, rx_rate=0.10))
        md_inf = E._report_md([inf_case])
        checks.append(("fragile-number: applicable repo with 0 control baseline renders no 'inf×'",
                       "inf×" not in md_inf and "inf x" not in md_inf.lower()))
        checks.append(("fragile-number: it still emits a 'stronger than random' multiplier cell",
                       "stronger than random" in md_inf))
        # finite multipliers must be UNCHANGED (don't regress the moat report's exact 'N.N×' rendering).
        checks.append(("fragile-number: a finite lift still renders as 'N.N×'",
                       "4.5× stronger than random" in md))

        # exit-code contract: applicable-real → 0, applicable-weak → 1, N/A-only → 0, N/A+weak → 1.
        weak = E._verdict(_result(c_rate=0.16, r_rate=0.15, cx_rate=0.11, rx_rate=0.10))
        na = E._verdict(_result(c_rate=0.60, r_rate=0.0, cx_rate=0.50, rx_rate=0.0, pairs_x=4, files=4, repo="na"))
        checks.append(("exit-code: applicable-real → 0", E._exit_code([strong]) == 0))
        checks.append(("exit-code: applicable-weak → 1", E._exit_code([weak]) == 1))
        checks.append(("exit-code: N/A-only → 0 (a tiny lib is not a failure)", E._exit_code([na]) == 0))
        checks.append(("exit-code: N/A + weak-applicable → 1 (a real failure still fails)",
                       E._exit_code([na, weak]) == 1))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("EVALUATE HONESTY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
