#!/usr/bin/env python3
"""Veripsa code-graph extractors — shared bounded file reader (content-free leaf).

Several substrate extractors (_cg_api_contract, _cg_openapi, …) read a source file's TEXT capped at a byte budget
and must never crash on a bad/binary file. That reader was copy-pasted IDENTICALLY into each; centralised here so
a change to HOW a file is read/capped happens in ONE place (the hotspot lesson — finer files = finer collision
point, same leaf-split discipline as render_safe / render_bound). CONTENT-FREE in the same sense as the callers:
the bytes are read only to extract structure, never stored. Leaf: depends on the stdlib only.
"""
from __future__ import annotations

_MAX_SCAN_BYTES = 1_000_000  # 1 MB cap on the bytes a structured / line-scanner extractor reads from one file


def _mark_incomplete(incomplete_paths_out, relative_path: "str | None") -> None:
    """Record one repo-relative document path without imposing a diagnostics API.

    ``incomplete_paths_out`` is intentionally duck-typed as a mutable ``set`` so
    every extractor keeps its historical two-tuple return value.  Existing
    callers can omit it; graph assembly can opt in and conservatively mark the
    reported document Unknown.
    """
    if incomplete_paths_out is None or not relative_path:
        return
    incomplete_paths_out.add(str(relative_path).replace("\\", "/"))


def _read_capped(
    abs_path: str,
    incomplete_paths_out=None,
    relative_path: "str | None" = None,
) -> "str | None":
    """Read text up to ``_MAX_SCAN_BYTES`` and report lossy reads.

    Returns ``None`` on read error, as before.  When a diagnostics set and
    repo-relative path are supplied, both read failures and actual truncation
    are recorded.  Reading one extra character distinguishes an exactly-at-cap
    file from a truncated file without loading the remainder.
    """
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(_MAX_SCAN_BYTES + 1)
        if len(text) > _MAX_SCAN_BYTES:
            _mark_incomplete(incomplete_paths_out, relative_path)
            return text[:_MAX_SCAN_BYTES]
        return text
    except OSError:
        _mark_incomplete(incomplete_paths_out, relative_path)
        return None
