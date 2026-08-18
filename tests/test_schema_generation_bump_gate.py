#!/usr/bin/env python3
"""SCHEMA-GENERATION BUMP GATE — fail a PR that changes the schema manifest digest without
bumping db/schema_generation (the drift that made #880's deploy fail closed at deploy time).

The pre-deploy manifest guard (schema_manifest.decide) already fails a deploy where the image's
generation matches the live marker's generation but the digest differs (fail_same_generation_digest).
That is the LAST line of defense — it only fires at deploy. This gate moves the SAME contract to PR
time: if the checked-in schema manifest digest changed versus the PR base but db/schema_generation did
not increase, fail LOUD on the PR, naming the drift — so a schema change and its generation bump can
never land as separate PRs (exactly what happened with #880/#881/#888).

It computes the digest with the SAME schema_manifest.build_manifest the deploy guard uses, so the gate
rejects EXACTLY what the deploy would reject (byte-identical canonical manifest; a comment/whitespace
change to a hashed schema file changes the digest and therefore needs a bump, matching the deploy).

Static (no database). Runs in the fast PR gate. Needs git (to read the PR base).

Run:  python3 tests/test_schema_generation_bump_gate.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "github-app"))
import schema_manifest as sm  # noqa: E402


# ── PURE DECISION (the contract; unit-tested below without git) ────────────────────────────────────
def decide(base_digest: str, base_gen: int, head_digest: str, head_gen) -> tuple[bool, str]:
    """Return (ok, reason). The gate fails closed for every reason != 'ok'."""
    if not isinstance(head_gen, int) or isinstance(head_gen, bool) or head_gen < 1:
        return False, "malformed_generation"           # generation must be a positive integer
    if head_gen < base_gen:
        return False, "generation_decreased"           # monotonic: a rollback image never lowers it
    if head_digest != base_digest and head_gen <= base_gen:
        return False, "schema_changed_without_bump"    # THE drift: schema changed, generation not bumped
    return True, "ok"


# ── GIT INTEGRATION (compare HEAD working tree vs the PR base) ──────────────────────────────────────
def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def _base_ref() -> str | None:
    """The ref to diff against. In CI use the PR base; locally, the merge-base with origin/main.
    Returns None when there is nothing to compare (push to main / detached with no base)."""
    override = os.environ.get("VERIPSA_SCHEMA_GATE_BASE")
    if override:
        return override
    base_branch = os.environ.get("GITHUB_BASE_REF")
    if base_branch:
        _git("fetch", "--quiet", "--depth=50", "origin", base_branch)
        return f"origin/{base_branch}"
    _git("fetch", "--quiet", "--depth=50", "origin", "main")
    mb = _git("merge-base", "HEAD", "origin/main").stdout.strip()
    head = _git("rev-parse", "HEAD").stdout.strip()
    if not mb or mb == head:
        return None
    return mb


def _read_gen(root: Path) -> int:
    return int((root / "db" / "schema_generation").read_text(encoding="ascii").strip())


def _manifest_at_base(ref: str) -> tuple[str, int]:
    """Extract the ref's db/ into a temp dir and build its manifest + read its generation."""
    with tempfile.TemporaryDirectory(prefix="veripsa-schema-base-") as tmp:
        archive = _git("archive", ref, "db")
        if archive.returncode != 0:
            raise RuntimeError(f"git archive {ref} db failed: {archive.stderr[:200]}")
        # re-run capturing bytes (text=True above mangles the tar); use a pipe to tar
        proc = subprocess.run(["git", "-C", str(ROOT), "archive", ref, "db"], capture_output=True)
        with tarfile.open(fileobj=__import__("io").BytesIO(proc.stdout)) as tf:
            tf.extractall(tmp)
        base_root = Path(tmp)
        return sm.build_manifest(base_root).digest, _read_gen(base_root)


def gate() -> int:
    base = _base_ref()
    if base is None:
        print("SCHEMA-GENERATION BUMP GATE: PASS (no base to compare — push/main).")
        return 0
    base_digest, base_gen = _manifest_at_base(base)
    head_digest = sm.build_manifest(ROOT).digest
    head_gen = _read_gen(ROOT)
    ok, reason = decide(base_digest, base_gen, head_digest, head_gen)
    if not ok:
        detail = {
            "schema_changed_without_bump":
                f"db/schema/* manifest digest changed vs base ({base[:12]}) but db/schema_generation "
                f"did not increase (base={base_gen}, head={head_gen}). Bump db/schema_generation "
                f"(usually +1) in this PR — a schema change and its generation bump must land together, "
                f"or the deploy fails closed (fail_same_generation_digest).",
            "generation_decreased":
                f"db/schema_generation decreased (base={base_gen}, head={head_gen}); it is monotonic.",
            "malformed_generation":
                f"db/schema_generation must be a positive integer (got {head_gen!r}).",
        }[reason]
        print(f"SCHEMA-GENERATION BUMP GATE: FAIL — {detail}")
        return 1
    print(f"SCHEMA-GENERATION BUMP GATE: PASS "
          f"(base={base[:12]} gen={base_gen} -> head gen={head_gen}; "
          f"{'digest changed + bumped' if head_digest != base_digest else 'digest unchanged'}).")
    return 0


def _unit_tests() -> None:
    """The pure decision, exercised with the closeout's negative fixtures (no git needed)."""
    checks = [
        ("schema changed, generation held -> FAIL",
         decide("aaa", 1, "bbb", 1) == (False, "schema_changed_without_bump")),
        ("schema changed, generation +1 -> PASS",
         decide("aaa", 1, "bbb", 2) == (True, "ok")),
        ("no schema change, generation held -> PASS",
         decide("aaa", 1, "aaa", 1) == (True, "ok")),
        ("no schema change, generation +1 (rollout-only) -> PASS (allowed; PR body should say why)",
         decide("aaa", 1, "aaa", 2) == (True, "ok")),
        ("generation decreased -> FAIL",
         decide("aaa", 2, "aaa", 1) == (False, "generation_decreased")),
        ("schema changed, generation decreased -> FAIL (decrease caught first)",
         decide("aaa", 2, "bbb", 1) == (False, "generation_decreased")),
        ("malformed generation (zero) -> FAIL",
         decide("aaa", 1, "aaa", 0) == (False, "malformed_generation")),
        ("malformed generation (bool) -> FAIL",
         decide("aaa", 1, "aaa", True) == (False, "malformed_generation")),
        ("schema changed, big bump -> PASS",
         decide("aaa", 2, "bbb", 5) == (True, "ok")),
    ]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    assert all(ok for _, ok in checks), "decision unit checks failed"


def main() -> int:
    _unit_tests()
    rc = gate()
    if rc == 0:
        print("SCHEMA-GENERATION BUMP GATE: PASS")
    return rc


if __name__ == "__main__":
    sys.exit(main())
