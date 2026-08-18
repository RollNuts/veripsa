"""Gate entry for the edge-precision-audit deterministic selftest.

`edge_precision_audit.py`'s selftest is guarded by a `--selftest` argv flag, but
the gate harness (`run_gates.sh`) invokes a gate's test as `python3 "<test>"`
with the test string QUOTED, so an arg embedded in the register_gate `<test>`
value (`"tests/edge_precision_audit.py --selftest"`) is passed as part of the
FILENAME and fails with "No such file or directory" — the gate never ran its
selftest and was silently red. Bare `python3 tests/edge_precision_audit.py`
does not help either: with no explicit targets and no panel clones (the CI
runner has none) it falls through to the honest-boundary no-op and never emits
the `EDGE PRECISION AUDIT GATE: PASS` marker the gate keys on.

This file is a no-arg entry point the harness can run directly: it invokes the
selftest and lets its marker drive the gate. Deterministic, no git/Postgres/
network, content-free — inherited from `_selftest()`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import edge_precision_audit  # noqa: E402

raise SystemExit(edge_precision_audit._selftest())
