#!/usr/bin/env python3
"""EFFECT backtest — thin CLI/runner. The reusable engine now lives in the top-level product module
`cochange_backtest.py` so PRODUCT/sales code (evaluate.py) never imports from tests/ (the auditor's src→test
smell). This module is the gated CLI a human runs against real history and re-exports the engine's public
surface (X / STOP / MIN_NAME_LEN / coupled_pairs / commit_touchsets / _pair_stats / analyze) so existing
imports `import backtest_cochange as B; B.STOP` keep working.

What the backtest proves (unchanged): if the file pairs Veripsa flags as coupled (its graph adjacency —
imports, cross-file calls, shared table/config) genuinely CO-CHANGE in the repo's commit history far more than
chance — and more than mere same-folder co-location — its warnings point at real entanglement, not noise.
Co-change is the standard empirical proxy for real coupling. The CROSS-DIRECTORY lift is the differentiated
proof (coupling a folder/text heuristic is blind to). This does NOT prove rework-hours-saved (needs a deployed
A/B over time) — it proves the SIGNAL IS REAL, the precondition for any effect. Honest boundary printed.

Run:  python3 tests/backtest_cochange.py /repo/with/history [/another/repo ...]
"""
from __future__ import annotations

import os
import sys

# Import the shared, non-test engine from the repo root (product module). Keeping the engine OUT of tests/ is
# the point of this split: evaluate.py imports cochange_backtest directly, so no product→tests edge exists.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cochange_backtest import (  # noqa: E402,F401  (re-exported for `import backtest_cochange as B`)
    ROOT,
    X,
    STOP,
    MIN_NAME_LEN,
    _MAX_COMMIT_FILES,
    coupled_pairs,
    commit_touchsets,
    _pair_stats,
    analyze,
    main,
)

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
