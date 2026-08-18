#!/usr/bin/env python3
"""TIME-CORRECT column-impact precision measurement -- the HONEST one.

The naive measurement (scan HEAD) is WRONG for renames/drops: by HEAD the old column name
has already been removed from the code (the rename PR updated every call site), so the
referencing files no longer contain it.  Veripsa, by contrast, runs at PR time -- the
migration is PROPOSED and the OLD name is STILL in the code.  So we must evaluate the
analyzer at each migration's PARENT commit (old name still present) and check it against the
GROUND TRUTH = the set of non-migration files the migration's OWN commit edited (the files a
human had to touch to follow the rename = the true blast radius).

For a sample of real rename/drop commits we:
  1. Find the commit C that introduced the migration, and its parent P.
  2. At P (old name present), for the changed (table, col), predict referencing files under
     R0 (bare) / R1 (+ubiquitous guard) / R2 (+table-qualify).
  3. GROUND TRUTH = non-migration source files edited in C (the human's actual blast radius).
  4. A predicted file is TRUE if it is in the ground-truth set (the human DID edit it for this
     change), FALSE otherwise.  Report precision (TRUE / predicted) and recall (TRUE / truth)
     per rule.  This is the only honest precision number: it's measured against what real
     engineers actually had to change.

Content-free: paths + table/column NAMES + commit shas only.  No bodies stored.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from measure_column_impact import (  # noqa: E402
    column_changes, code_tables, column_refs, is_migration,
    UBIQUITOUS_COLUMNS,
)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True).stdout


def find_rename_commits(repo, limit=400):
    """Commits whose message/diff touched a RenameField/RemoveField -- the change events."""
    out = git(repo, "log", "--all", "-G", r"RenameField|RemoveField|rename_column|remove_column",
              "--format=%H", f"-n{limit}", "--", "*migrations*", "*migrate*")
    return [l for l in out.splitlines() if l.strip()]


def commit_migration_and_files(repo, sha):
    """For a commit: (list of migration files added/modified, set of non-migration source files
    edited in the same commit)."""
    out = git(repo, "show", "--name-only", "--format=", sha)
    files = [f.strip() for f in out.splitlines() if f.strip()]
    migs = [f for f in files if is_migration(f) and (f.endswith(".py") or f.endswith(".rb") or f.endswith(".sql"))]
    truth = set(f for f in files if not is_migration(f) and f.endswith((".py", ".rb")))
    return migs, truth


def file_at(repo, sha, path):
    out = subprocess.run(["git", "-C", repo, "show", f"{sha}:{path}"],
                         capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


def list_source_at(repo, sha):
    out = git(repo, "ls-tree", "-r", "--name-only", sha)
    return [f for f in out.splitlines() if f.endswith((".py", ".rb")) and not is_migration(f)]


def predict_at_parent(repo, parent, changed, src_files):
    """At commit `parent`, predict files that reference each changed (table,col).
    Returns {(t,c,k): {'r0':set,'r1':set,'r2':set}}.  Bounded: scans src_files (already the
    parent tree's non-migration sources)."""
    # build a lightweight index of the parent tree (only files we can read)
    idx = {}
    for rel in src_files:
        text = file_at(repo, parent, rel)
        if text is None:
            continue
        tables, classmap = code_tables(text)
        bare, qual = column_refs(text)
        idx[rel] = (tables, classmap, bare, qual)
    out = {}
    for (t, c, k) in changed:
        r0, r1, r2 = set(), set(), set()
        for rel, (tables, classmap, bare, qual) in idx.items():
            bare_hit = c in bare
            qual_hit = any(col == c and (cls == t or classmap.get(cls) == t)
                           for (cls, col) in qual)
            table_tie = t in tables
            if not (bare_hit or qual_hit):
                continue
            r0.add(rel)
            if qual_hit or (bare_hit and c not in UBIQUITOUS_COLUMNS):
                r1.add(rel)
            if qual_hit or (bare_hit and c not in UBIQUITOUS_COLUMNS and table_tie):
                r2.add(rel)
        out[(t, c, k)] = {"r0": r0, "r1": r1, "r2": r2}
    return out


def run(repo, max_events=40):
    repo = os.path.abspath(repo)
    name = os.path.basename(repo)
    print(f"\n########## {name}: TIME-CORRECT column-impact precision ##########")
    commits = find_rename_commits(repo)
    print(f"  candidate rename/drop commits: {len(commits)}")

    agg = {r: {"tp": 0, "pred": 0, "truth": 0} for r in ("r0", "r1", "r2")}
    events = 0
    examples = []
    for sha in commits:
        migs, truth = commit_migration_and_files(repo, sha)
        if not migs or not truth:
            continue   # need both a migration and same-commit code edits (the blast radius)
        parent = git(repo, "rev-parse", f"{sha}^").strip()
        if not parent:
            continue
        # column changes from the migration files (read at the CHILD commit -- the migration
        # exists there)
        changed = set()
        for mig in migs:
            mtext = file_at(repo, sha, mig)
            if mtext:
                for (t, c, k) in column_changes(mtext):
                    if k in ("rename", "drop") and c not in UBIQUITOUS_COLUMNS and len(c) > 3:
                        changed.add((t, c, k))
        if not changed:
            continue
        # restrict ground truth to files that EXIST at parent (so a NEWLY added file in the
        # same commit isn't counted as a missed prediction -- it didn't exist to reference the
        # old name).
        parent_src = set(list_source_at(repo, parent))
        truth_at_parent = truth & parent_src
        if not truth_at_parent:
            continue
        preds = predict_at_parent(repo, parent, changed, list(parent_src))
        # union predictions over all changed cols in this commit (the migration's blast radius)
        for r in ("r0", "r1", "r2"):
            pset = set()
            for ck in preds:
                pset |= preds[ck][r]
            tp = len(pset & truth_at_parent)
            agg[r]["tp"] += tp
            agg[r]["pred"] += len(pset)
            agg[r]["truth"] += len(truth_at_parent)
        if len(examples) < 8:
            r2set = set()
            for ck in preds:
                r2set |= preds[ck]["r2"]
            examples.append((sha[:8], sorted(changed)[:2], len(truth_at_parent),
                             len(r2set), len(r2set & truth_at_parent)))
        events += 1
        if events >= max_events:
            break

    print(f"  measured change events (migration + same-commit code edits): {events}")
    for sha, cols, nt, npred, ntp in examples:
        print(f"    {sha} cols={cols} truth={nt} R2pred={npred} R2hit={ntp}")
    print(f"\n  ---- {name}: precision / recall vs human blast radius ----")
    for r in ("r0", "r1", "r2"):
        tp, pred, truth = agg[r]["tp"], agg[r]["pred"], agg[r]["truth"]
        prec = tp / pred * 100 if pred else 0.0
        rec = tp / truth * 100 if truth else 0.0
        fp = 100 - prec
        print(f"   {r.upper()}: predicted {pred:5d}  TRUE {tp:4d}  -> precision {prec:5.1f}%  "
              f"(FALSE-POSITIVE {fp:5.1f}%)   recall {rec:5.1f}%")
    return agg


if __name__ == "__main__":
    for r in sys.argv[1:]:
        run(r)
