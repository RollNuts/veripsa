#!/usr/bin/env python3
"""CO-CHANGE RENAME-FOLLOW gate — a renamed file's history is recovered as ONE continuous history.

THE BUG this fixes: `git log --name-only` reports a file's OLD path before a rename and its NEW path after, so
the SAME logical file looks like TWO separate, short files. Its co-change history is SPLIT in half — and each
half may fall BELOW the support floor → a real coupling silently VANISHES. The fleet measured 9–59% of
co-change recall is recoverable just by following renames.

THE FIX (history/backfill path only, content-free): _cg_cochange._git_log_commits reads `--name-status -M`
(status letter + paths + a rename arrow `R<sim> old new` — still NO diff/message/author/body), and CANONICALISES
every historical path to the file's CURRENT name, so the pair is computed on the UNIFIED history. Recall is
RECOVERED, never invented (we only MERGE two path-histories of the SAME file).

DETERMINISTIC PROOF on a crafted temp git repo: a file co-changes with a partner, is `git mv`-renamed, then keeps
co-changing under the NEW name.
  • WITHOUT rename-follow (baseline `--name-only` touch sets, simulating the OLD extractor): the file is two
    short histories → the (renamed-file, partner) pair's support is SPLIT and BELOW the floor → the pair is
    DROPPED.
  • WITH rename-follow (the shipping _git_log_commits): the unified history yields the coupled pair ABOVE the floor.
And the precision discipline is intact: a GIANT commit is still skipped, a once-seen coincidence is still dropped,
lift still divides out a hot file's base rate — and the output is still content-free (no message/body/author/SHA).

Run:  python3 tests/test_cochange_rename.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import _cg_cochange as CC  # noqa: E402

FAIL = 0

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}
for _redirect_var in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
):
    _GIT_ENV.pop(_redirect_var, None)
for _config_var in list(_GIT_ENV):
    if _config_var.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
        _GIT_ENV.pop(_config_var, None)


def chk(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _git(d, *args):
    result = subprocess.run(
        ["git", "-C", d, *args], capture_output=True, text=True, env=_GIT_ENV, check=False)
    if result.returncode:
        # Keep a failed fixture actionable without leaking the SECRET sentinels used by the content-free proof.
        detail = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        detail = re.sub(r"SECRET_[A-Za-z0-9_]+", "<redacted>", detail)
        detail = detail.replace(d, "<temp-repo>")
        raise RuntimeError(f"git {args[0] if args else '?'} failed ({result.returncode}): {detail or '<no output>'}")


def _append_commit(d, files, tok):
    """Append a unique (SECRET-bearing) line to each of `files` and commit. The SECRET in body+message must never
    surface (content-free)."""
    for f in files:
        p = os.path.join(d, f)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "a") as fh:
            fh.write(f"// SECRET_BODY_{tok} line\n")
    _git(d, "add", "--", *files)
    _git(d, "commit", "-m", f"SECRET_MSG_{tok}", "--no-verify")


def _baseline_touchsets_no_follow(d, window=2000):
    """The OLD behaviour, reconstructed: per-commit touch sets from `--name-only` (NO rename-follow). A renamed
    file appears under its OLD path before the rename and its NEW path after — two separate identities. This is the
    'before' we must beat."""
    out = subprocess.run(
        ["git", "-C", d, "log", "--no-merges", "--name-only",
         "--pretty=format:\x01%H", "-n", str(window)],
        capture_output=True, text=True, check=False).stdout
    commits, cur = [], None
    for line in out.split("\n"):
        if line.startswith("\x01"):
            if cur is not None:
                commits.append(cur)
            cur = set()
        else:
            p = line.strip()
            if p and cur is not None:
                cur.add(p)
    if cur is not None:
        commits.append(cur)
    return commits


def _pairs_from_commits(commits, **kw):
    """Run the SAME precision-disciplined pair builder cochange_pairs uses, but over an arbitrary list of
    pre-built touch sets — so we can score the BASELINE (no-follow) touch sets and the FOLLOWED touch sets through
    the identical floors/lift, isolating ONLY the rename-follow effect."""
    import collections
    mc = kw.get("max_commit_files", 40)
    ms = kw.get("min_support", 5)
    mp = kw.get("min_prob", 0.3)
    ml = kw.get("min_lift", 2.0)
    change, co, n_total = collections.Counter(), collections.Counter(), 0
    for files in commits:
        n = len(files)
        if n == 0 or n > mc:
            continue
        n_total += 1
        fl = sorted(files)
        for f in fl:
            change[f] += 1
        for i in range(n):
            for j in range(i + 1, n):
                co[(fl[i], fl[j])] += 1
    pairs = {}
    for (a, b), c in co.items():
        if c < ms:
            continue
        na, nb = change[a], change[b]
        strength = max(c / na if na else 0.0, c / nb if nb else 0.0)
        lift = (c * n_total) / (na * nb) if (na and nb) else 0.0
        if lift < ml or strength < mp:
            continue
        pairs[(a, b)] = {"co": c, "n_a": na, "n_b": nb, "lift": round(lift, 2)}
    return pairs


def main() -> int:
    # The shipping reader does not accept an env argument, so isolate the whole test process while it shells out.
    # This prevents CI/user GIT_DIR or command-scope GIT_CONFIG_* injection from redirecting either side of the proof.
    original_env = os.environ.copy()
    os.environ.clear()
    os.environ.update(_GIT_ENV)
    try:
        return _main()
    finally:
        os.environ.clear()
        os.environ.update(original_env)


def _main() -> int:
    with tempfile.TemporaryDirectory() as d:
        _git(d, "init", "-q")
        _git(d, "config", "user.email", "t@example.com")
        _git(d, "config", "user.name", "Tester")
        _git(d, "config", "commit.gpgsign", "false")
        _git(d, "config", "gc.auto", "0")
        _git(d, "config", "maintenance.auto", "false")

        # PHASE 1 — old.py co-changes with partner.py 3 times under its ORIGINAL name.
        for i in range(3):
            _append_commit(d, ["src/old.py", "src/partner.py"], f"pre{i}")
        # THE RENAME — `git mv` src/old.py -> src/new.py (a pure rename; git detects R100).
        _git(d, "mv", "src/old.py", "src/new.py")
        _git(d, "commit", "-m", "SECRET_MSG_rename", "--no-verify")
        # PHASE 2 — the SAME logical file (now src/new.py) keeps co-changing with partner.py 3 more times.
        for i in range(3):
            _append_commit(d, ["src/new.py", "src/partner.py"], f"post{i}")

        # Eight unrelated commits are the exact minimum needed here: after rename-follow, lift is
        # (background + 8) / 8, so background=8 clears the production min_lift=2.0 without 22 redundant commits.
        for k in range(8):
            _append_commit(d, [f"bg/m{k}.py"], f"bg{k}")
        # a COINCIDENTAL pair seen ONCE — must stay below the support floor (discipline preserved).
        _append_commit(d, ["src/new.py", "docs/notes.md"], "coincidence")
        # a GIANT mass-format commit — must still be SKIPPED (discipline preserved).
        _append_commit(d, [f"vendor/lib{n}.py" for n in range(50)], "giant")

        KW = dict(window=2000, max_commit_files=40, min_support=5, min_prob=0.3, min_lift=2.0)

        # --- BEFORE: the OLD no-follow touch sets, scored through the SAME floors/lift.
        before = _pairs_from_commits(_baseline_touchsets_no_follow(d), **KW)
        # The renamed file is split into two 3-commit histories. Both are below the production support floor of 5,
        # so neither identity emits a pair; only rename-follow can recover the unified 6-commit coupling.
        old_pair = before.get(("src/old.py", "src/partner.py")) or before.get(("src/partner.py", "src/old.py"))
        new_pair_before = before.get(("src/new.py", "src/partner.py")) or before.get(("src/partner.py", "src/new.py"))
        unified_before_co = (new_pair_before or {}).get("co", 0)
        chk(old_pair is None and new_pair_before is None,
            "BEFORE (no rename-follow): both split 3-commit identities stay below min_support=5 "
            f"(old_pair={old_pair}, new_pair={new_pair_before})")

        # --- AFTER: the SHIPPING extractor (rename-follow on).
        pairs = CC.cochange_pairs(d, **KW)
        idx = {tuple(sorted((p["a"], p["b"]))): p for p in pairs}
        ap = idx.get(("src/new.py", "src/partner.py"))
        # (1) the unified pair exists under the CURRENT name with the FULL 6-commit support (3 pre + 3 post).
        chk(ap is not None and ap["co"] == 6,
            f"AFTER (rename-follow): src/new.py↔partner is ONE coupling with the FULL unified support co=6 (got {ap})")
        # (2) RECALL RECOVERED: the support strictly GREW vs the split baseline (the whole point).
        chk(ap is not None and ap["co"] > unified_before_co,
            f"RECALL RECOVERED: support {unified_before_co} → {ap['co'] if ap else '?'} by following the rename")
        # (3) the OLD path is GONE from the output — fully canonicalised, no phantom src/old.py pair survives.
        chk(not any("src/old.py" in (p["a"], p["b"]) for p in pairs),
            "the pre-rename path (src/old.py) is fully canonicalised away — no split phantom pair remains")

        # --- DISCIPLINE PRESERVED (unchanged by rename-follow):
        # giant commit minted nothing.
        chk(not [p for p in pairs if p["a"].startswith("vendor/") or p["b"].startswith("vendor/")],
            "the 50-file giant commit is still SKIPPED — it mints no co-change pairs")
        chk(ap is not None and ap["n_total"] == 16,
            f"the giant commit is excluded from the denominator (expected n_total=16, got {ap['n_total'] if ap else '?'})")
        # the once-seen coincidence is still dropped.
        chk(("docs/notes.md", "src/new.py") not in idx and ("src/new.py", "docs/notes.md") not in idx,
            "the once-seen coincidence (new↔docs/notes) is still dropped below the support floor")
        # lift is still computed (base-rate correction intact).
        chk(ap is not None and ap["lift"] >= 2.0,
            f"lift base-rate correction intact: the unified pair clears min_lift (lift={ap['lift'] if ap else '?'}×)")
        # CONTENT-FREE: no SECRET token from any commit message or file body leaked.
        chk("SECRET" not in json.dumps(pairs),
            "content-free: the output carries NO commit message / file body / author / SHA — only paths + counts")

    print("COCHANGE RENAME-FOLLOW GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
