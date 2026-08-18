#!/usr/bin/env python3
"""Large-.sql schema RECALL gate (regression lock for the schema-aware file-size cap).

A whole-DB DDL dump in ONE file (Rails/GitLab `db/structure.sql`) legitimately exceeds the general
1.5 MB per-file cap. Before the schema-aware cap, such a file was silently skipped → its tables never
entered the graph (a RECALL gap on exactly the large enterprise repos we want to cover; measured live:
GitLab's ~2.9 MB structure.sql minted 0 tables). This gate proves, content-free + hermetically (no
network, no committed fixture — synthesises its own files in a temp dir):

  1. a >1.5 MB `.sql` (above the GENERAL cap) IS parsed → its CREATE TABLEs are minted.
  2. the `.sql` exception is `.sql`-SPECIFIC — a >1.5 MB NON-`.sql` source file is still skipped
     (the general cap is unchanged; this isn't a blanket cap raise).
  3. the schema cap still BOUNDS — a `.sql` above `_SCHEMA_FILE_SIZE_CAP` is still skipped (no
     unbounded read of a pathological data/seed dump).

Run:  python3 tests/test_schema_large_file.py   (prints 'SCHEMA LARGE-FILE GATE: PASS' on success)
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import code_graph_extract as X  # noqa: E402


def _tables(root):
    g = X.build_graph(root)
    return [n for n in g["nodes"] if n.get("kind") == "table"]


def _file_nodes(root):
    g = X.build_graph(root)
    return [n for n in g["nodes"] if n.get("kind") == "file"]


def _write_sql(path, n_tables, pad_to_bytes=0):
    """A valid, parseable Postgres-style schema with n_tables CREATE TABLEs, padded (via a trailing
    comment) up to pad_to_bytes so we can dial the FILE SIZE independently of the table count."""
    parts = []
    for i in range(n_tables):
        parts.append(
            f"CREATE TABLE public.widget_{i} (\n"
            f"    id bigint NOT NULL,\n"
            f"    name text,\n"
            f"    owner_id bigint,\n"
            f"    created_at timestamp without time zone,\n"
            f"    payload jsonb\n"
            f");\n"
        )
    body = "".join(parts)
    if pad_to_bytes and len(body) < pad_to_bytes:
        body += "\n/* " + ("x" * (pad_to_bytes - len(body))) + " */\n"
    with open(path, "w") as f:
        f.write(body)
    return os.path.getsize(path)


def main() -> int:
    assert X._SCHEMA_FILE_SIZE_CAP > X._FILE_SIZE_CAP, "schema cap must exceed the general cap"

    # (1) a >1.5 MB .sql is PARSED — tables minted. ~4000 tables ≈ 0.9 MB of body, padded over 1.6 MB.
    with tempfile.TemporaryDirectory() as d:
        n = 4000
        size = _write_sql(os.path.join(d, "structure.sql"), n, pad_to_bytes=1_600_000)
        assert size > X._FILE_SIZE_CAP, f"fixture {size}B must exceed the general cap {X._FILE_SIZE_CAP}"
        tables = _tables(d)
        assert len(tables) == n, f"expected {n} tables from a {size}B .sql, got {len(tables)} (regression: large .sql skipped)"
        names = {t.get("name") for t in tables}
        assert "widget_0" in names and f"widget_{n-1}" in names, "first/last table missing — partial parse"
        print(f"  [1] {size}B .sql (> general cap) → {len(tables)} tables minted ✓")

    # (2) the exception is .sql-SPECIFIC: a >1.5 MB NON-.sql source file is still skipped (no file node).
    with tempfile.TemporaryDirectory() as d:
        big_py = os.path.join(d, "big_module.py")
        with open(big_py, "w") as f:
            f.write("# pad\n" + ("x = 1  # " + "y" * 80 + "\n") * 20000)  # ~1.8 MB of .py
        assert os.path.getsize(big_py) > X._FILE_SIZE_CAP
        paths = {n.get("path") for n in _file_nodes(d)}
        assert "big_module.py" not in paths, "general cap regressed: a >1.5MB .py was NOT skipped"
        print(f"  [2] {os.path.getsize(big_py)}B .py (> general cap) → still skipped (general cap intact) ✓")

    # (3) the schema cap still BOUNDS: a .sql above _SCHEMA_FILE_SIZE_CAP is skipped (no unbounded read).
    with tempfile.TemporaryDirectory() as d:
        huge = os.path.join(d, "seed_dump.sql")
        size = _write_sql(huge, 10, pad_to_bytes=X._SCHEMA_FILE_SIZE_CAP + 500_000)
        assert size > X._SCHEMA_FILE_SIZE_CAP
        tables = _tables(d)
        assert len(tables) == 0, f"schema cap not bounding: a {size}B .sql minted {len(tables)} tables"
        print(f"  [3] {size}B .sql (> schema cap) → skipped, 0 tables (pathological-dump bound intact) ✓")

    print("SCHEMA LARGE-FILE GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
