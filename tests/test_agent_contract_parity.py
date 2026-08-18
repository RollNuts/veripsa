#!/usr/bin/env python3
"""Drift gate: keep the agent-instruction contract aligned across surfaces.

`AGENTS.md`, `CLAUDE.md`, and `.github/copilot-instructions.md` are each read by
a different coding agent, often in isolation. They must therefore carry the same
core contract so an agent that reads only one of them still behaves the same.

This gate fails closed when any of them drifts from the canonical positioning
(`GitHub App for pre-merge PR traffic control`) back to retired phrasing
(`GitHub-native merge control`, `governs the merge lane`), or when a public
prose surface reintroduces a forbidden base-verdict name (`Clear to land`) or the
`control tower` safety-implying category.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Agent-instruction files that must each carry the same contract.
AGENT_INSTRUCTION_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    os.path.join(".github", "copilot-instructions.md"),
)

# Prose surfaces (agent instructions + docs) that must not reintroduce drift.
PROSE_SURFACES = AGENT_INSTRUCTION_FILES + (
    os.path.join("docs", "GITHUB_AI_AGENT_OPERATING_KIT.md"),
    os.path.join("docs", "marketplace", "LISTING_PACK.md"),
    os.path.join("docs", "PR_BRIEF_UX_GUIDE.md"),
    os.path.join("docs", "DEMO_TO_INSTALL.md"),
)

# Retired phrasing that must not reappear on any prose surface above.
FORBIDDEN = (
    "GitHub-native merge control",
    "governs the merge lane",
    "Clear to land",
    "Cleared to land",
    "control tower",
)

# Each agent-instruction file must still state the shared contract.
REQUIRED_IN_EACH_INSTRUCTION = (
    "pre-merge PR traffic control",  # canonical positioning
    "are not stored",                # content-free boundary
    "veripsa-ack",                   # acknowledgement discipline
)


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def test_no_retired_positioning_or_verdict_names_on_prose_surfaces() -> None:
    failures = []
    for rel in PROSE_SURFACES:
        text = _read(rel)
        for token in FORBIDDEN:
            if token in text:
                failures.append(f"{rel}: reintroduced forbidden phrase {token!r}")
    assert not failures, "Agent-contract drift:\n" + "\n".join(failures)


def test_each_agent_instruction_carries_the_shared_contract() -> None:
    failures = []
    for rel in AGENT_INSTRUCTION_FILES:
        text = _read(rel)
        for phrase in REQUIRED_IN_EACH_INSTRUCTION:
            if phrase not in text:
                failures.append(f"{rel}: missing required contract phrase {phrase!r}")
    assert not failures, "Agent-contract parity gap:\n" + "\n".join(failures)


if __name__ == "__main__":
    test_no_retired_positioning_or_verdict_names_on_prose_surfaces()
    test_each_agent_instruction_carries_the_shared_contract()
    print("AGENT CONTRACT PARITY: PASS")
