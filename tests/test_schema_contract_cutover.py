#!/usr/bin/env python3
"""Unit gate for the production-only schema contract finalizer."""
from __future__ import annotations

import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import schema_contract_cutover as C  # noqa: E402


def main() -> int:
    checks: list[tuple[str, bool]] = []
    sha = "a" * 40
    token = "b" * 32

    checks.append((
        "artifact identity accepts only exact target=Render=baked SHA plus token",
        C._validate_artifact_identity(sha, token, sha, sha)
        and not C._validate_artifact_identity(sha, token, None, sha)
        and not C._validate_artifact_identity(sha, token, sha, None)
        and not C._validate_artifact_identity(sha, token, "c" * 40, sha)
        and not C._validate_artifact_identity(sha, token, sha, "c" * 40)
        and not C._validate_artifact_identity(sha.upper(), token, sha, sha)
        and not C._validate_artifact_identity(sha, "short", sha, sha),
    ))

    old_render = os.environ.get("RENDER_GIT_COMMIT")
    old_owner = os.environ.get("OWNER_DSN")
    old_baked = C._baked_build_sha
    old_manifest = C.build_manifest
    manifest_called = False

    def forbidden_manifest(_root):
        nonlocal manifest_called
        manifest_called = True
        raise AssertionError("manifest/DB path reached before artifact proof")

    try:
        os.environ["RENDER_GIT_COMMIT"] = "c" * 40
        os.environ["OWNER_DSN"] = "must-not-be-read-or-printed"
        C._baked_build_sha = lambda: sha
        C.build_manifest = forbidden_manifest
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            mismatch_rc = C.run(sha, token)
        checks.append((
            "mismatched artifact fails before manifest/DB work and never logs "
            "the token or owner credential",
            mismatch_rc == 2
            and not manifest_called
            and token not in output.getvalue()
            and "must-not-be-read-or-printed" not in output.getvalue(),
        ))

        os.environ.pop("RENDER_GIT_COMMIT", None)
        manifest_called = False
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            missing_rc = C.run(sha, token)
        checks.append((
            "missing Render artifact identity is a pre-DB hard failure",
            missing_rc == 2 and not manifest_called,
        ))
    finally:
        C._baked_build_sha = old_baked
        C.build_manifest = old_manifest
        if old_render is None:
            os.environ.pop("RENDER_GIT_COMMIT", None)
        else:
            os.environ["RENDER_GIT_COMMIT"] = old_render
        if old_owner is None:
            os.environ.pop("OWNER_DSN", None)
        else:
            os.environ["OWNER_DSN"] = old_owner

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("SCHEMA CONTRACT CUTOVER GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
