#!/usr/bin/env python3
"""STOPLIST SYNC + DOMAIN-VERB RECALL gate (RECALL audit r4 2026-06-21).

CONTEXT. PR #365 added a BUILTIN/SHORT-NAME stoplist (+ a `char_length(ce.dst) > 2` min-name-length guard) to
the import-UNCONFIRMED single-definer branch of the coupling-adjacency logic in db/schema/70_social.sql
(core._claim_adjacency's out_adj/in_adj and core._dampened_adjacency's calls_h). The SAME name-set is MIRRORED
in the shared backtest engine cochange_backtest.py (STOP; re-exported by tests/backtest_cochange.py) — the two
MUST stay byte-identical, or the offline backtest stops modelling what the live engine actually drops.

THE FINDING (MED, measured r4): the #365 stoplist OVER-REACHED. Alongside genuine pure-language/stdlib method
tails (Array.pop, JSON.stringify, dict.keys, strings.HasPrefix, json.NewDecoder/Marshal, fmt.Sprintf, json.dumps)
it also listed DOMAIN-INTENT verbs — get/set/add/read/write/parse/decode/encode/load. A bare DOMAIN verb is NOT
language-intrinsic: it carries real cross-file intent (parse a config, load a model, decode a token, read/write a
record) and a single un-imported definer of such a name IS a legitimate coupling signal. Dropping them silently
removed real couplings on the import-unconfirmed branch (a recall regression). The fix RESTORES those 9 verbs
(removes them from BOTH stoplists), keeping ONLY pure language/stdlib tails.

WHAT THIS GATE PINS (lightweight — pure literal-stoplist invariants, no DB needed; the BEHAVIOURAL recall/precision
of the branch is already covered by the call_edge_precision / false_coupling_precision / symbol_use_recall /
builtin_call_stoplist / cochange_persist gates):
  (1) SYNC — every #365 BUILTIN array literal in 70_social.sql (out_adj, in_adj, calls_h: all 3) is the SAME set,
      and that set is byte-identical to the #365 sub-block of tests/backtest_cochange.py STOP. A drift in EITHER
      direction (a name added/removed on one side only) FAILS.
  (2) RECALL-RESTORE — the 9 DOMAIN-INTENT verbs (get/set/add/read/write/parse/decode/encode/load) are ABSENT
      from BOTH stoplists. A future regression that re-adds any of them (re-introducing the silent recall loss)
      FAILS here.
  (3) NON-EMPTY — the pure-builtin core (pop/stringify/keys/hasprefix/newdecoder/marshal/sprintf/dumps) is STILL
      present in both, so an over-correction that guts the stoplist (re-opening the original builtin FP class)
      also FAILS.

content-free (a name, not a body).
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The 9 DOMAIN-INTENT verbs restored by the r4 recall fix — these MUST NOT be in either stoplist.
DOMAIN_VERBS = {"get", "set", "add", "read", "write", "parse", "decode", "encode", "load"}
# A representative slice of the pure-language/stdlib tails that MUST remain (non-empty / not-gutted check).
PURE_BUILTINS = {"pop", "stringify", "keys", "hasprefix", "newdecoder", "marshal", "sprintf", "dumps"}


def _sql_365_blocks() -> list[set[str]]:
    """Every #365 BUILTIN array literal in 70_social.sql, as a set of lowercased names.

    The block is the 4-line ARRAY[…] literal that begins with the 'pop','push','shift' line and ends in
    'loads'])))) — anchored on that opening line so we never straddle the unrelated `defs` protocol stoplist
    (which also uses `<> ALL (ARRAY[…])` but a different name-set) or apostrophe-bearing comment prose.
    """
    lines = open(os.path.join(ROOT, "db", "schema", "70_social.sql"), encoding="utf-8").read().splitlines()
    blocks: list[set[str]] = []
    for i, line in enumerate(lines):
        if "'pop','push','shift'" in line:
            chunk = "\n".join(lines[i:i + 4])
            blocks.append(set(re.findall(r"'([^']+)'", chunk)))
    return blocks


def _backtest_365_set() -> set[str]:
    """The #365 sub-block of the backtest STOP (the pure-builtin tail, after its sync marker). The STOP literal
    now lives in the shared, non-test engine cochange_backtest.py (tests/backtest_cochange.py re-exports it),
    so PRODUCT code never imports from tests/ — read the canonical home."""
    bt = open(os.path.join(ROOT, "cochange_backtest.py"), encoding="utf-8").read()
    m = re.search(r"MUST stay byte-identical to the SQL #365 BUILTIN block.*?\n(.*?)\n\}", bt, re.S)
    if not m:
        return set()
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def main() -> int:
    checks = []

    sql_blocks = _sql_365_blocks()
    # the #365 set guard must appear in all THREE single-definer branches (out_adj, in_adj, calls_h).
    checks.append((f"the #365 BUILTIN array literal is present in all 3 single-definer branches (found {len(sql_blocks)})",
                   len(sql_blocks) == 3))

    sql_identical = bool(sql_blocks) and all(b == sql_blocks[0] for b in sql_blocks)
    checks.append(("the 3 SQL #365 blocks are byte-identical to each other (one canonical set)", sql_identical))

    sql_set = sql_blocks[0] if sql_blocks else set()
    bt_set = _backtest_365_set()
    checks.append((f"backtest_cochange.py STOP #365 sub-block parsed (found {len(bt_set)} names)", bool(bt_set)))

    # (1) SYNC — the SQL set and the backtest set are identical (the hard invariant).
    sync_ok = bool(sql_set) and sql_set == bt_set
    checks.append((f"SYNC: SQL #365 set == backtest #365 set (sql_only={sorted(sql_set - bt_set)}, bt_only={sorted(bt_set - sql_set)})",
                   sync_ok))

    # (2) RECALL-RESTORE — the 9 domain-intent verbs are absent from BOTH stoplists.
    sql_leak = sorted(DOMAIN_VERBS & sql_set)
    bt_leak = sorted(DOMAIN_VERBS & bt_set)
    checks.append((f"RECALL: domain-intent verbs ABSENT from the SQL stoplist (leaked={sql_leak})", not sql_leak))
    checks.append((f"RECALL: domain-intent verbs ABSENT from the backtest stoplist (leaked={bt_leak})", not bt_leak))

    # (3) NON-EMPTY — the pure-builtin core survives in both (an over-correction that guts the list also fails).
    checks.append((f"NON-EMPTY: pure builtins still present in SQL set (missing={sorted(PURE_BUILTINS - sql_set)})",
                   PURE_BUILTINS <= sql_set))
    checks.append((f"NON-EMPTY: pure builtins still present in backtest set (missing={sorted(PURE_BUILTINS - bt_set)})",
                   PURE_BUILTINS <= bt_set))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("STOPLIST SYNC GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
