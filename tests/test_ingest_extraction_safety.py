#!/usr/bin/env python3
"""INGEST EXTRACTION SAFETY gate — a malicious repo's tarball / push payload can never write OUTSIDE the
temp ingest directory (zip-slip / symlink-escape / absolute-path / traversal).

Premise (security audit 2026-06-18): on a `push` to main the App ingests the repo's code STRUCTURE by
downloading the repo tarball and extracting it (full path, server._safe_extractall via _full_ingest), or by
fetching the changed files and writing them to a temp dir (incremental path, server._incremental_ingest).
Both write files whose NAMES come from ATTACKER-CONTROLLED input — a tarball member name (the repo's own
content) or a push payload's commit.added/modified entry. Python's bare `tarfile.extractall` has NO
path-traversal guard before 3.14 (on the production 3.12 image the secure `data` filter only WARNS and is not
the default), so a member like `../../app/server.py`, an absolute `/etc/x`, or a symlink could write arbitrary
files on the host during ingest — RCE / host-compromise / cross-tenant contamination on a shared instance.

This gate is the permanent guard. It is PURE + OFFLINE (no DB, no network, no deploy): it builds malicious
in-memory tarballs and feeds attacker-controlled paths through the REAL server-side guards
(server._safe_extractall, server._safe_extract_member_name) and asserts nothing escapes the sandbox while a
legitimate file still extracts. Without the guard these assertions FAIL (a traversal member overwrites a file
outside the extract dir); with it they pass.

Run:  python3 tests/test_ingest_extraction_safety.py     (no DB needed)
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import server as S  # noqa: E402

FAIL = 0


def check(cond: bool, label: str):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _add_file(tf, name, payload: bytes):
    ti = tarfile.TarInfo(name)
    ti.size = len(payload)
    tf.addfile(ti, io.BytesIO(payload))


def _add_symlink(tf, name, target):
    lk = tarfile.TarInfo(name)
    lk.type = tarfile.SYMTYPE
    lk.linkname = target
    tf.addfile(lk)


def test_tarball_zip_slip():
    """A malicious repo tarball with traversal / absolute / symlink members must NOT escape the extract dir,
    while a legitimate file still extracts."""
    work = tempfile.mkdtemp(prefix="ingest_safety_tar_")
    try:
        extract = os.path.join(work, "extract")
        os.makedirs(extract)
        victim = os.path.join(work, "victim_outside.txt")   # OUTSIDE extract, inside the writable tree (= /app on prod)
        with open(victim, "w") as fh:
            fh.write("ORIGINAL")
        abs_target = os.path.join(work, "abs_pwn_target")    # an absolute member should not reach here either

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            _add_file(tf, "../victim_outside.txt", b"PWNED-by-traversal")  # zip-slip (one hop above extract dir)
            _add_file(tf, abs_target, b"PWNED-absolute")                   # absolute path member
            _add_symlink(tf, "escape", "../../../../etc/passwd")           # symlink escape
            _add_file(tf, "repo/app.py", b"print('ok')\n")                 # a LEGITIMATE file

        with tarfile.open(fileobj=io.BytesIO(buf.getvalue())) as tf:
            skipped = S._safe_extractall(tf, extract)

        check(skipped == 3, f"3 unsafe members skipped (got {skipped})")
        check(open(victim).read() == "ORIGINAL", "traversal member did NOT overwrite the file outside the extract dir")
        check(not os.path.exists(abs_target), "absolute-path member did NOT write outside the extract dir")
        check(not os.path.islink(os.path.join(extract, "escape")), "symlink member was NOT planted")
        check(os.path.isfile(os.path.join(extract, "repo", "app.py")), "the legitimate file WAS extracted")
        # nothing escaped the extract dir at all
        escaped = []
        for dp, _dn, fns in os.walk(work):
            for fn in fns:
                full = os.path.join(dp, fn)
                if not os.path.abspath(full).startswith(os.path.abspath(extract) + os.sep) and full != victim:
                    escaped.append(full)
        check(escaped == [], f"no file written outside the extract dir (escaped={escaped})")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_incremental_path_guard():
    """The incremental ingest writes attacker-controlled paths (push commit.added/modified). The containment
    guard must reject any path that escapes the temp dir and accept legitimate repo-relative paths."""
    work = tempfile.mkdtemp(prefix="ingest_safety_incr_")
    try:
        d = os.path.join(work, "extract")
        os.makedirs(d)
        for bad in ["../escape.txt", "/abs/escape", "a/../../escape", "../../etc/passwd", "/etc/passwd", "..", ""]:
            check(S._safe_extract_member_name(bad, d) is None, f"escaping/empty path rejected: {bad!r}")
        for good in ["src/app.py", "a/b/c.py", "x.py", "deep/nested/dir/file.go"]:
            dest = S._safe_extract_member_name(good, d)
            ok = dest is not None and os.path.abspath(dest).startswith(os.path.abspath(d) + os.sep)
            check(ok, f"legitimate path accepted + contained: {good!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    print("=== INGEST EXTRACTION SAFETY gate (zip-slip / traversal / symlink — offline) ===")
    print("-- malicious repo tarball (full ingest path) --")
    test_tarball_zip_slip()
    print("-- attacker-controlled per-file path (incremental ingest path) --")
    test_incremental_path_guard()
    print("------------------------------------------------------------")
    if FAIL == 0:
        print("INGEST EXTRACTION SAFETY GATE: PASS")
        return 0
    print("INGEST EXTRACTION SAFETY GATE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
