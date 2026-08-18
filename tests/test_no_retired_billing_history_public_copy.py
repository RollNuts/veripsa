#!/usr/bin/env python3
"""Fail if current public copy narrates a retired billing model.

This is stricter than the general drift scanner: historical/negated wording is
also forbidden because public surfaces should describe only the product that
exists now.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETIRED_VENDOR = "Pad" + "dle"
RETIRED_MOR = "Merchant" + " of Record"
FORBIDDEN = (
    RETIRED_VENDOR,
    RETIRED_MOR,
    "codebase-size billing",
    "billing by codebase size",
    "コードベース規模による課金",
)
ROOT_PUBLIC_FILES = {
    "README.md",
    "ROADMAP.md",
    "SECURITY.md",
    "SUPPORT.md",
}
SKIP_DIRS = {"internal", "notes"}


def is_current_public_copy(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in rel.parts):
        return False
    if rel.parts[0] == "articles":
        text = path.read_text(encoding="utf-8")
        return bool(re.search(r"^published:\s*true\s*$", text, re.MULTILINE))
    if rel.parts[0] in {"docs", ".github"}:
        return path.suffix.lower() in {".md", ".mdx", ".txt", ".yml", ".yaml"}
    return str(rel) in ROOT_PUBLIC_FILES


def main() -> None:
    findings: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or not is_current_public_copy(path):
            continue
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for forbidden in FORBIDDEN:
            if forbidden.lower() in lowered:
                findings.append(f"{path.relative_to(ROOT)}: contains retired billing history")

    assert not findings, "\n".join(findings)
    print("NO-RETIRED-BILLING-HISTORY PUBLIC COPY: PASS")


if __name__ == "__main__":
    main()
