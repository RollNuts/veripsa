#!/usr/bin/env python3
"""Gate 103: blast_radius hub-dampening.

Builds a synthetic graph (no Postgres needed) that reproduces the ubiquitous-name
inflation bug and proves the fix:
  - File A defines both a UNIQUE symbol (only A defines it) and a UBIQUITOUS symbol
    (defined in >3 files).
  - Five files B1-B5 call ONLY the ubiquitous symbol.
  - One file C calls the unique symbol (a real structural neighbour of A).
  After the fix:
    blast_radius(A) must include C (real caller of the unique symbol)
    blast_radius(A) must NOT include B1-B5 (only callers of the ubiquitous symbol)
  Confirms a NORMAL file (no ubiquitous symbols) is unchanged: its callers still appear.
"""

import sys
import os

# Allow running from repo root or tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from code_graph_extract import blast_radius  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _edge(src, dst, kind):
    return {"src": src, "dst": dst, "kind": kind}


def _node(path, kind, name=None):
    n = {"id": path, "kind": kind, "path": path}
    if name:
        n["name"] = name
    return n


# ---------------------------------------------------------------------------
# Synthetic graph
# ---------------------------------------------------------------------------

# Files
FILE_A = "src/A.php"        # target file: defines ubiquitous + unique symbol
FILE_C = "src/C.php"        # real structural neighbour — calls unique symbol of A
FILES_B = [f"src/B{i}.php" for i in range(1, 6)]   # 5 files calling only ubiquitous symbol
# Extra files that also DEFINE the ubiquitous symbol so its def_count > 3
EXTRA_DEF = [f"src/Extra{i}.php" for i in range(1, 4)]  # 3 more definers -> total 4 (A + 3)

UBIQUITOUS = "__construct"   # will be defined in A + 3 extras = 4 files (> threshold 3)
UNIQUE = "handleMissingKey"  # defined ONLY in A

nodes = (
    [_node(FILE_A, "file")]
    + [_node(f, "file") for f in FILES_B]
    + [_node(FILE_C, "file")]
    + [_node(f, "file") for f in EXTRA_DEF]
    # def nodes for A
    + [_node(f"{FILE_A}::{UBIQUITOUS}", "def", UBIQUITOUS)]
    + [_node(f"{FILE_A}::{UNIQUE}",    "def", UNIQUE)]
)

edges = []

# A contains both symbols
edges.append(_edge(FILE_A, f"{FILE_A}::{UBIQUITOUS}", "contains"))
edges.append(_edge(FILE_A, f"{FILE_A}::{UNIQUE}",    "contains"))

# Extra files also define the ubiquitous symbol (makes def_file_count["__construct"] = 4 > 3)
for ef in EXTRA_DEF:
    edges.append(_edge(ef, f"{ef}::{UBIQUITOUS}", "contains"))

# B files call ONLY the ubiquitous symbol (should be excluded by hub-dampening)
for bf in FILES_B:
    edges.append(_edge(bf, UBIQUITOUS, "calls"))

# C calls the unique symbol (should remain in blast radius)
edges.append(_edge(FILE_C, UNIQUE, "calls"))

graph = {"nodes": nodes, "edges": edges}

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

result = blast_radius(graph, FILE_A)

fail = False

# C must be in blast radius (real unique-symbol caller)
if FILE_C not in result:
    print(f"FAIL: {FILE_C} should be in blast_radius({FILE_A}) — it calls the unique symbol {UNIQUE!r}")
    fail = True
else:
    print(f"OK: {FILE_C} present (calls unique symbol {UNIQUE!r})")

# B files must NOT be in blast radius (only call ubiquitous symbol)
for bf in FILES_B:
    if bf in result:
        print(f"FAIL: {bf} should NOT be in blast_radius({FILE_A}) — it only calls ubiquitous {UBIQUITOUS!r}")
        fail = True
    else:
        print(f"OK: {bf} absent (only calls ubiquitous symbol {UBIQUITOUS!r})")

# ---------------------------------------------------------------------------
# Normal-file unchanged: a file with only specific symbols still sees its callers
# ---------------------------------------------------------------------------
NORMAL_FILE = "src/Normal.php"
NORMAL_SYM = "processPayment"  # unique symbol, defined nowhere else
CALLER_OF_NORMAL = "src/Checkout.php"

normal_nodes = [
    _node(NORMAL_FILE, "file"),
    _node(CALLER_OF_NORMAL, "file"),
    _node(f"{NORMAL_FILE}::{NORMAL_SYM}", "def", NORMAL_SYM),
]
normal_edges = [
    _edge(NORMAL_FILE, f"{NORMAL_FILE}::{NORMAL_SYM}", "contains"),
    _edge(CALLER_OF_NORMAL, NORMAL_SYM, "calls"),
]
normal_graph = {"nodes": normal_nodes, "edges": normal_edges}
normal_result = blast_radius(normal_graph, NORMAL_FILE)

if CALLER_OF_NORMAL not in normal_result:
    print(f"FAIL: normal file blast radius broken — {CALLER_OF_NORMAL} missing for unique symbol")
    fail = True
else:
    print(f"OK: normal file blast radius intact — {CALLER_OF_NORMAL} present for {NORMAL_SYM!r}")

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
if fail:
    print("BLAST-DAMPEN GATE: FAIL")
    sys.exit(1)
else:
    print("BLAST-DAMPEN GATE: PASS")
    sys.exit(0)
