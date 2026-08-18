#!/usr/bin/env python3
"""Fail closed when a public-source snapshot contains private operational material."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_BYTES = 5 * 1024 * 1024
SAFE_WORKFLOWS = {"public-ci.yml"}
REQUIRED_PUBLIC_FILES = {
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "SUPPORT.md",
    ".github/workflows/public-ci.yml",
    "github-app/app-manifest.json",
}
SENSITIVE_SUFFIXES = {
    ".pem", ".key", ".p8", ".p12", ".pfx", ".jks", ".keystore", ".kdbx"
}

RULES = {
    "credentialed-dsn": re.compile(
        r"(?i)\b(?:postgres(?:ql)?|redis|mysql)://[^\s/:@]+:[^\s@]+@"
        r"(?P<host>\[[^\]]+\]|[^/\s:?#]+)"
    ),
    "provider-resource-id": re.compile(
        r"\b(?:srv|dep|job|dpg|crn|evg|prj|tea|usr|reg|img)-[a-z0-9]{10,}\b"
    ),
    "private-key-pem": re.compile(r"-----BEGIN [A-Z ]*PRIVATE\s+KEY-----"),
    "github-token": re.compile(r"\b(?:gh[oprsu]_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    "absolute-mac-home": re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    "uuid": re.compile(
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
    ),
}
PUBLIC_PROVIDER_FIXTURE = re.compile(
    r"\b(?:srv|dep|job|dpg|crn|evg|prj|tea|usr|reg|img)-publicfix[0-9]{4}0{7}\b"
)
PUBLIC_UUID_FIXTURE = re.compile(
    r"\b(?:00000000-0000-4000-8000-[0-9]{12}|"
    r"11111111-1111-4111-8111-[0-9a-f]{12})\b"
)
SAFE_TEST_DSN_HOSTS = {
    "localhost", "127.0.0.1", "[::1]", "db", "host", "example"
}
SAFE_DSN_FIXTURE_FILES = {
    "tests/test_alert_dispatch_liveness.py",
    "tests/test_db_dns_deadline.py",
    "tests/test_ops_alerting.py",
    "tests/test_predeploy_schema_apply_e2e.py",
}
FORBIDDEN_LITERALS = (
    ("production-provider-url", ".onrender" + ".com"),
    ("internal-publication-marker", "never " + "republish"),
    ("production-provenance", "observed " + "live in prod"),
    ("production-provenance", "seen " + "live: pr #"),
    ("production-provenance", "live-" + "dogfood"),
    ("production-provenance", "prod-" + "observed"),
    ("production-provenance", "definitive " + "live evidence"),
    ("production-provenance", "caught " + "live in prod"),
    ("production-provenance", "silent-" + "prod-break"),
    ("production-provenance", "exactly as " + "prod"),
    ("production-provenance", "prod " + "bug"),
    ("production-provenance", "live " + "incident"),
    ("suspended-demo-link", "Get" + "Veripsa"),
    ("suspended-demo-link", "ai-pr-" + "collision-lab"),
)
STALE_INTERNAL_PATHS = (
    "docs/" + "MARKETPLACE_FREE_LAUNCH.md",
    "docs/" + "MARKETPLACE_READINESS.md",
    "docs/marketplace/" + "LISTING_PACK.md",
    "docs/" + "PUBLIC_COPY_MATRIX.md",
    "docs/" + "PUBLIC_PROOF_ASSET_LIBRARY.md",
    "docs/" + "INSTALL_ATTRIBUTION_TAXONOMY.md",
    "docs/" + "PASSIVE_LEARNING_SIGNALS.md",
    "docs/" + "ATTRIBUTION_PRIVACY_BOUNDARY.md",
    "scripts/" + "emergency_brake.py",
    "scripts/" + "render_deploy_inventory.py",
    "scripts/" + "render_release_preflight.py",
    "scripts/" + "render_incident_stage_readiness.py",
    "scripts/" + "render_strict_preflight.sh",
    "scripts/" + "render_worker_readiness.py",
    "github-app/scripts/" + "finalize_legacy_claim_cutover.sh",
    "github-app/scripts/" + "render_rollback.sh",
    "github-app/scripts/" + "render_verify_predecessor.sh",
    "github-app/scripts/" + "render_worker_quarantine.sh",
    "github-app/scripts/" + "render_worker_readiness.sh",
    "github-app/scripts/" + "render_worker_rollback.sh",
    "github-app/scripts/" + "cleanup_stale_branch_lanes.py",
)
GATE_TEST_PATH = re.compile(
    r'^\s*register_gate\s+"(?P<path>tests/[^"\n]+)"', re.MULTILINE
)
MARKDOWN_LINK = re.compile(
    r"\[[^\]\n]*\]\((?P<target><[^>\n]+>|[^)\s]+)(?:\s+[\"'][^\n]*[\"'])?\)"
)


def _files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts
    )


def main() -> int:
    failures: list[tuple[str, str]] = []
    for rel in sorted(REQUIRED_PUBLIC_FILES):
        if not (ROOT / rel).is_file():
            failures.append((rel, "missing-public-file"))

    for path in _files():
        rel = path.relative_to(ROOT)
        rel_text = rel.as_posix()
        lower_name = path.name.lower()

        if (
            lower_name == ".env"
            or lower_name.startswith(".env.")
            or path.suffix.lower() in SENSITIVE_SUFFIXES
            or lower_name.startswith("id_rsa")
            or lower_name.startswith("id_ed25519")
        ):
            failures.append((rel_text, "sensitive-filename"))

        if path.stat().st_size > MAX_TEXT_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        in_tests = rel.parts and rel.parts[0] == "tests"
        for rule, pattern in RULES.items():
            candidate = text
            if rel_text in SAFE_DSN_FIXTURE_FILES and rule == "credentialed-dsn":
                def redact_safe_test_dsn(match: re.Match[str]) -> str:
                    host = match.group("host").lower()
                    if (
                        host in SAFE_TEST_DSN_HOSTS
                        or host.endswith(".invalid")
                        or host.endswith(".test")
                    ):
                        return ""
                    return match.group(0)

                candidate = pattern.sub(redact_safe_test_dsn, candidate)
            if in_tests and rule == "provider-resource-id":
                candidate = PUBLIC_PROVIDER_FIXTURE.sub("", candidate)
            if in_tests and rule == "uuid":
                candidate = PUBLIC_UUID_FIXTURE.sub("", candidate)
            if pattern.search(candidate):
                failures.append((rel_text, rule))

        folded = text.casefold()
        for rule, literal in FORBIDDEN_LITERALS:
            if literal.casefold() in folded:
                failures.append((rel_text, rule))

        for stale_path in STALE_INTERNAL_PATHS:
            if stale_path.casefold() in folded:
                failures.append((rel_text, "stale-internal-path"))

        if path.suffix.lower() == ".md":
            for match in MARKDOWN_LINK.finditer(text):
                target = match.group("target").strip("<>")
                if (
                    not target
                    or target.startswith(("#", "/", "http://", "https://", "mailto:", "data:"))
                    or "://" in target
                ):
                    continue
                target = target.split("#", 1)[0].split("?", 1)[0]
                if not target:
                    continue
                resolved = (path.parent / target).resolve()
                try:
                    resolved.relative_to(ROOT.resolve())
                except ValueError:
                    failures.append((rel_text, "markdown-link-outside-root"))
                    continue
                if not resolved.exists():
                    failures.append((rel_text, "broken-markdown-link"))

        if rel.parts and rel.parts[0] == "gates.d":
            match = GATE_TEST_PATH.search(text)
            if match and not (ROOT / match.group("path")).is_file():
                failures.append((rel_text, "gate-target-missing"))

        if rel.parts[:2] == (".github", "workflows"):
            if path.name not in SAFE_WORKFLOWS:
                failures.append((rel_text, "non-public-workflow"))
            for token in (
                "self-hosted",
                "pull_request_target",
                "workflow_dispatch",
                "schedule:",
                "secrets.",
                "permissions: write-all",
            ):
                if token in text:
                    failures.append((rel_text, "unsafe-workflow-token"))

    for rel, rule in sorted(set(failures)):
        print(f"FAIL {rule}: {rel}")
    if failures:
        print("PUBLIC SNAPSHOT GATE: FAIL")
        return 1
    print("PUBLIC SNAPSHOT GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
