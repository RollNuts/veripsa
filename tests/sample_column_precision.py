#!/usr/bin/env python3
"""HONEST hand-labelable precision sample for COLUMN-impact resolution.

The aggregate measure_column_impact.py shows the FAN-OUT collapse (R0 bare -> R2
table-qualified).  But fan-out is not precision.  This script produces a SAMPLE we can
classify TRUE/FALSE by hand, on the cases that MATTER: SPECIFIC (non-ubiquitous) renamed /
dropped columns -- exactly the AI-era silent-breakage case.

For each such changed column (table T, column C) it lists, per rule:
  - the files predicted to reference C
  - whether each predicted file actually references C OF TABLE T (a structural TRUE) vs a
    bare `.C` of a DIFFERENT table / a ubiquitous collision (a structural FALSE)
  - the git co-change of each predicted file with the migration's OWN COMMIT'S touched files
    (the strongest available corroboration: when a column is renamed, the SAME PR usually
    edits the model + the call sites; do the predicted files co-occur in that change?)

We then report the FALSE-POSITIVE RATE per rule on the labelled sample.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from measure_column_impact import (  # noqa: E402
    column_changes, code_tables, column_refs, is_migration, walk_py,
    UBIQUITOUS_COLUMNS, _norm,
)


def migration_commit_files(repo, mig_rel):
    """The set of files touched in the FIRST commit that introduced this migration file --
    the strongest ground truth: a rename PR usually edits model + call sites together."""
    # find the commit that ADDED this migration
    out = subprocess.run(
        ["git", "-C", repo, "log", "--diff-filter=A", "--format=%H", "-1", "--", mig_rel],
        capture_output=True, text=True).stdout.strip()
    if not out:
        return set(), None
    sha = out.splitlines()[0]
    files = subprocess.run(
        ["git", "-C", repo, "show", "--name-only", "--format=", sha],
        capture_output=True, text=True).stdout
    return set(f.strip() for f in files.splitlines() if f.strip()), sha


def build_index(repo):
    code = {}   # rel -> (tables, classmap, bare, qual)
    changes = {}  # (t,c,k) -> set(mig rels)
    for path in walk_py(repo):
        rel = os.path.relpath(path, repo)
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if is_migration(rel):
            for (t, c, k) in column_changes(text):
                changes.setdefault((t, c, k), set()).add(rel)
        else:
            tables, classmap = code_tables(text)
            bare, qual = column_refs(text)
            code[rel] = (tables, classmap, bare, qual)
    return code, changes


def predict(code, t, c):
    """Return {file: (rule_flags)} where rule_flags is (r0, r1, r2)."""
    res = {}
    for rel, (tables, classmap, bare, qual) in code.items():
        bare_hit = c in bare
        qual_hit = any(col == c and (cls == t or classmap.get(cls) == t)
                       for (cls, col) in qual)
        table_tie = t in tables
        if not (bare_hit or qual_hit):
            continue
        r0 = True
        r1 = qual_hit or (bare_hit and c not in UBIQUITOUS_COLUMNS)
        r2 = qual_hit or (bare_hit and c not in UBIQUITOUS_COLUMNS and table_tie)
        res[rel] = (r0, r1, r2, table_tie, qual_hit)
    return res


def run(repo, max_cols=25):
    repo = os.path.abspath(repo)
    name = os.path.basename(repo)
    code, changes = build_index(repo)

    # pick SPECIFIC (non-ubiquitous) renamed/dropped columns -- the high-value case.
    specific = [(t, c, k, migs) for (t, c, k), migs in changes.items()
                if k in ("rename", "drop") and c not in UBIQUITOUS_COLUMNS
                and len(c) > 3]
    # prefer columns that actually have code predictions and a resolvable table
    specific.sort(key=lambda x: x[0])
    print(f"\n########## {name}: SPECIFIC renamed/dropped column impact (hand-labelable) ##########")

    # accumulate confusion per rule on the structural label.
    tot = {"r0": [0, 0], "r1": [0, 0], "r2": [0, 0]}  # [true, false]
    cochange_kept = {"r0": [], "r1": [], "r2": []}
    cochange_dropped = {"r0": [], "r1": [], "r2": []}

    shown = 0
    for (t, c, k, migs) in specific:
        preds = predict(code, t, c)
        if not preds:
            continue
        mig = sorted(migs)[0]
        commit_files, sha = migration_commit_files(repo, mig)
        # the migration's own commit edited these NON-migration files -> the TRUE referencers
        # touched in the same change (strongest corroboration).
        co_true = set(f for f in commit_files if not is_migration(f) and f in code)

        if shown < 12:
            print(f"\n  [{k}] table={t!r} column={c!r}  (mig {os.path.basename(mig)}, commit {sha[:8] if sha else '?'})")
            print(f"       same-commit non-migration files (co-change TRUTH): {len(co_true)}")
        for rel, (r0, r1, r2, table_tie, qual_hit) in sorted(preds.items()):
            # STRUCTURAL label: TRUE if the file is tied to THIS table (qualified or touches t),
            # FALSE if it's a bare ubiquitous-ish collision with no table tie.
            structural_true = qual_hit or table_tie
            in_commit = rel in co_true
            for rk, flag in (("r0", r0), ("r1", r1), ("r2", r2)):
                if flag:
                    tot[rk][0 if structural_true else 1] += 1
                    (cochange_kept[rk]).append(1 if in_commit else 0)
                else:
                    (cochange_dropped[rk]).append(1 if in_commit else 0)
            if shown < 12 and (r0):
                tag = "TRUE " if structural_true else "FALSE"
                co = "co-changes" if in_commit else "no-cochange"
                kept = "".join(x for x, f in (("0", r0), ("1", r1), ("2", r2)) if f) or "-"
                print(f"         {tag} kept@R[{kept:3}] {co:11} {rel}")
        shown += 1
        if shown >= max_cols:
            break

    def fp(rk):
        tr, fa = tot[rk]
        n = tr + fa
        return (fa / n * 100 if n else 0.0), tr, fa

    def cc_precision(lst):
        return (sum(lst) / len(lst) * 100) if lst else 0.0

    print(f"\n  ---- {name}: precision over hand-labelable sample ----")
    for rk in ("r0", "r1", "r2"):
        rate, tr, fa = fp(rk)
        ck = cc_precision(cochange_kept[rk])
        print(f"   {rk.upper()}: structural FALSE-POSITIVE rate {rate:5.1f}%  "
              f"(TRUE {tr}, FALSE {fa})   |  kept preds that co-change with migration commit: {ck:4.0f}%")
    return tot, cochange_kept


if __name__ == "__main__":
    for r in sys.argv[1:]:
        run(r)
