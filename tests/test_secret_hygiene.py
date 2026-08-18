#!/usr/bin/env python3
"""Keep credentials and developer-local identifiers out of the Git index.

This guard exercises Git's ignore engine, checks tracked filenames, and scans
tracked content for high-confidence credential/PII signatures. It reports only
the category and repository-relative path, never a matched value.
"""

import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

IGNORED = (
    ".env",
    ".env.production",
    ".env.staging",
    ".env.development",
    ".env.development.local",
    ".envrc",
    ".envrc.local",
    "config/.env.production",
    "secrets/app.pem",
    "secrets/app.key",
    "secrets/signing.p8",
    "secrets/bundle.p12",
    "secrets/bundle.pfx",
    "secrets/bundle.pkcs12",
    "secrets/store.jks",
    "secrets/store.keystore",
    "secrets/putty.ppk",
    "secrets/passwords.kdbx",
    "secrets/id_rsa",
    "secrets/id_dsa",
    "secrets/id_ecdsa",
    "secrets/id_ed25519",
)

TRACKABLE_EXAMPLES = (
    ".env.example",
    ".env.production.example",
    ".envrc.example",
    ".envrc.local.example",
    "config/.env.staging.example",
    "secrets/app.pem.example",
    "secrets/app.key.example",
)

PRIVATE_SUFFIXES = {
    ".pem",
    ".key",
    ".p8",
    ".p12",
    ".pfx",
    ".pkcs12",
    ".jks",
    ".keystore",
    ".ppk",
    ".kdbx",
}
PRIVATE_BASENAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}


def content_patterns() -> tuple[tuple[str, re.Pattern[bytes]], ...]:
    """High-confidence signatures, assembled so this guard cannot match itself."""
    return (
        (
            "AWS access key id",
            re.compile(b"A" + rb"(?:K|S)IA" + rb"[0-9A-Z]{16}"),
        ),
        (
            "GitHub token",
            # GitHub's classic/runtime token body is base62. Underscore-heavy
            # sentinel strings used by log-redaction tests are not credentials.
            re.compile(b"gh" + rb"[pousr]_[A-Za-z0-9]{36,255}"),
        ),
        (
            "GitHub fine-grained token",
            re.compile(b"github" + b"_pat_" + rb"[A-Za-z0-9_]{20,}"),
        ),
        (
            "GitLab token",
            re.compile(b"gl" + b"pat-" + rb"[A-Za-z0-9_-]{20,}"),
        ),
        (
            "OpenAI API key",
            re.compile(
                rb"(?<![A-Za-z0-9])s" + b"k-" + rb"(?:proj-)?[A-Za-z0-9_-]{32,}"
            ),
        ),
        (
            "Stripe live key",
            re.compile(rb"(?<![A-Za-z0-9])(?:s" + b"k|r" + b"k)_live_[A-Za-z0-9]{20,}"),
        ),
        (
            "Slack token",
            re.compile(b"xo" + rb"x[baprs]-[A-Za-z0-9-]{20,}"),
        ),
        (
            "Google API key",
            re.compile(b"AI" + b"za" + rb"[A-Za-z0-9_-]{35}"),
        ),
        (
            "private key material",
            re.compile(
                b"-----BEGIN "
                + rb"(?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?"
                + b"PRIVATE KEY-----"
                + rb"\s+[A-Za-z0-9+/=\r\n]{64,}\s+-----END "
                + rb"(?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?"
                + b"PRIVATE KEY-----"
            ),
        ),
        (
            "personal Gmail address",
            re.compile(
                rb"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                + b"gmail"
                + rb"\.com\b",
                re.IGNORECASE,
            ),
        ),
        (
            "absolute macOS user path",
            re.compile(
                b"/" + b"Users/" + rb"[A-Za-z0-9._-]{1,64}/[^\s\"'<>]+"
            ),
        ),
        (
            "absolute Windows user path",
            re.compile(
                rb"[A-Za-z]:[\\/]" + b"Users" + rb"[\\/][^\\/\s]+[\\/][^\s\"'<>]+",
                re.IGNORECASE,
            ),
        ),
        (
            "absolute Linux user path",
            re.compile(
                b"/" + b"home/" + rb"(?!runner/|ubuntu/|vscode/)[A-Za-z0-9._-]+/"
            ),
        ),
    )


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", *args),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


NOREPLY_EMAIL = re.compile(
    rb"^(?:(?:[0-9]+\+)?(?P<handle>[A-Za-z0-9_.\-\[\]]+)@users\.noreply\.github\.com|noreply@github\.com)$",
    re.IGNORECASE,
)
IDENTITY_TRAILER = re.compile(
    rb"^(?:Signed-off-by|Co-authored-by):\s*(?P<name>[^<\r\n]+?)\s*<(?P<email>[^>\r\n]+)>\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def privacy_safe_identity(name: bytes, email: bytes) -> bool:
    """Accept only public-handle identities backed by GitHub noreply mail."""
    clean_name = name.strip()
    clean_email = email.strip()
    match = NOREPLY_EMAIL.fullmatch(clean_email)
    if match is None:
        return False
    handle = match.group("handle")
    if handle is None:
        return clean_name.lower() == b"github"
    return clean_name.lower() == handle.lower()


def commit_metadata_exposures() -> list[str]:
    """Return categories only; never expose a commit identity or message value."""
    result = git(
        "log",
        "--all",
        "--format=%an%x00%ae%x00%cn%x00%ce%x00%B%x00%x1e",
    )
    if result.returncode != 0:
        return ["unreadable commit metadata"]

    exposures: set[str] = set()
    for raw_record in result.stdout.split(b"\x1e"):
        record = raw_record.strip(b"\r\n")
        if not record:
            continue
        fields = record.split(b"\x00", 4)
        if len(fields) != 5:
            exposures.add("malformed commit metadata")
            continue
        author_name, author_email, committer_name, committer_email, message = fields
        if not privacy_safe_identity(author_name, author_email):
            exposures.add("non-noreply author identity")
        if not privacy_safe_identity(committer_name, committer_email):
            exposures.add("non-noreply committer identity")
        for match in IDENTITY_TRAILER.finditer(message):
            if not privacy_safe_identity(match.group("name"), match.group("email")):
                exposures.add("non-noreply message trailer identity")
        for category, pattern in content_patterns():
            if pattern.search(record):
                exposures.add(f"{category} in commit metadata")
    return sorted(exposures)


def is_ignored(path: str) -> bool:
    result = git("check-ignore", "--no-index", "--quiet", "--", path)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.returncode == 0


def is_secret_shaped(path: str) -> bool:
    name = Path(path).name
    if name.endswith(".example"):
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name == ".envrc" or name.startswith(".envrc."):
        return True
    return Path(name).suffix.lower() in PRIVATE_SUFFIXES or name in PRIVATE_BASENAMES


def repository_content_exposures(
    paths: list[str],
) -> list[tuple[str, str]]:
    """Return only (path, category); never retain or print the matched value."""
    exposures: list[tuple[str, str]] = []
    patterns = content_patterns()
    for relative in paths:
        candidate = ROOT / relative
        # Scan a tracked symlink's stored target text, never the target file.
        if candidate.is_symlink():
            try:
                content = os.readlink(candidate).encode("utf-8", errors="surrogateescape")
            except OSError:
                exposures.append((relative, "unreadable tracked symlink"))
                continue
        elif not candidate.is_file():
            continue
        else:
            try:
                content = candidate.read_bytes()
            except OSError:
                exposures.append((relative, "unreadable tracked content"))
                continue
        for category, pattern in patterns:
            if pattern.search(content):
                exposures.append((relative, category))
    return exposures


APPROVED_OFFICIAL_ACTIONS = {
    "actions/checkout": {"9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"},
    "actions/github-script": {"3a2844b7e9c422d3c10d287c895573f7108da1b3"},
    "actions/upload-artifact": {"043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"},
}


def unapproved_official_actions() -> list[tuple[str, str]]:
    """Return mutable or unknown official action pins; never include the ref."""
    unapproved: list[tuple[str, str]] = []
    action_line = re.compile(
        r"^\s*(?:-\s*)?uses:\s*(actions/[A-Za-z0-9_.-]+)@([^\s#]+)",
        re.MULTILINE,
    )
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        for match in action_line.finditer(text):
            action, ref = match.group(1), match.group(2)
            if (
                re.fullmatch(r"[0-9a-f]{40}", ref) is None
                or ref not in APPROVED_OFFICIAL_ACTIONS.get(action, set())
            ):
                unapproved.append((str(workflow.relative_to(ROOT)), action))
    return unapproved


def main() -> int:
    checks = []
    checks.extend((f"real secret path is ignored: {path}", is_ignored(path)) for path in IGNORED)
    checks.extend(
        (f"example remains trackable: {path}", not is_ignored(path))
        for path in TRACKABLE_EXAMPLES
    )

    listed_result = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if listed_result.returncode != 0:
        raise RuntimeError(listed_result.stderr.decode("utf-8", errors="replace"))
    repository_paths = [
        path.decode("utf-8", errors="surrogateescape")
        for path in listed_result.stdout.split(b"\0")
        if path
    ]
    exposed = sorted(path for path in repository_paths if is_secret_shaped(path))
    checks.append(("no real secret-shaped filename is tracked or pending", not exposed))

    content_exposed = repository_content_exposures(repository_paths)
    checks.append(
        (
            "tracked or non-ignored content has no high-confidence key, personal Gmail, or local user path",
            not content_exposed,
        )
    )
    metadata_exposed = commit_metadata_exposures()
    checks.append(
        (
            "all reachable commit metadata uses matching public handles and GitHub noreply email",
            not metadata_exposed,
        )
    )
    self_source = Path(__file__).read_bytes()
    checks.append(
        (
            "content signatures do not match their own guard source",
            not any(pattern.search(self_source) for _, pattern in content_patterns()),
        )
    )
    unapproved_actions = unapproved_official_actions()
    checks.append((
        "official GitHub Actions use approved immutable SHAs",
        not unapproved_actions,
    ))

    ok = all(passed for _, passed in checks)
    print("\n=== SECRET HYGIENE (offline) ===")
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    if exposed:
        print("[FAIL] tracked secret-shaped paths: " + ", ".join(exposed))
    for path, category in content_exposed:
        print(f"[FAIL] {category} found in tracked file: {path}")
    for category in metadata_exposed:
        print(f"[FAIL] commit metadata category: {category}")
    for path, action in unapproved_actions:
        print(f"[FAIL] unapproved official Action ref for {action} in: {path}")
    print("\nSECRET HYGIENE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
