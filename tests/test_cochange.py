#!/usr/bin/env python3
"""CO-CHANGE (logical coupling) extractor gate — the EMPIRICAL coupling signal, computed CONTENT-FREE from git
history, with the precision discipline that keeps it from becoming wallpaper.

Proves on a CRAFTED git repo (deterministic) that core._cg_cochange:
  (1) finds a real co-change pair (auth↔api change together) with a sane CONDITIONAL PROBABILITY (co/n_a), not
      just a raw count;
  (2) DROPS a coincidental pair below the support floor (auth↔unrelated, seen once);
  (3) is NOT fooled by a GIANT refactor/mass-format commit (50 files touched once) — that commit must NOT mint
      4·9·... co-change pairs (the dominant noise source);
  (4) cochange_partners surfaces "you touched auth; api historically comes with it" for the completeness hint;
  (5) is CONTENT-FREE: every value emitted is a file path or a number — never a commit message, author, SHA, or
      a line of code.

Run:  python3 tests/test_cochange.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import _cg_cochange as CC  # noqa: E402

FAIL = 0


def chk(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _git(d, *args):
    subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, check=True)


def _commit(d, files, secret_msg):
    """Touch `files` (write a unique line) and commit them. The commit MESSAGE carries a SECRET token — the
    extractor must never surface it (content-free)."""
    for f in files:
        p = os.path.join(d, f)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as fh:
            fh.write(f"// SECRET_BODY_{secret_msg} line\n")   # the FILE CONTENT also carries a secret
        _git(d, "add", f)
    _git(d, "commit", "-m", f"SECRET_MSG_{secret_msg}", "--no-verify")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        _git(d, "init", "-q")
        _git(d, "config", "user.email", "t@example.com")
        _git(d, "config", "user.name", "Tester")
        _git(d, "config", "commit.gpgsign", "false")

        # version.txt changes in EVERY commit (a HOT file: changelog/version/lockfile). It will co-change with
        # auth at 100% CONFIDENCE — but only because it ALWAYS changes, not because it is coupled. LIFT must
        # divide that base rate out and DROP it (the PO's exact concern).
        def commit(files, tok):
            _commit(d, files + ["version.txt"], tok)
        # auth.py + api.py change together 5 times (a real coupling, no code edge needed).
        for i in range(5):
            commit(["backend/auth.py", "backend/api.py"], f"pair{i}")
        commit(["backend/auth.py"], "as1"); commit(["backend/auth.py"], "as2")
        commit(["backend/api.py"], "is1"); commit(["backend/api.py"], "is2")
        # ~40 BACKGROUND commits on unrelated files — so auth/api are RARE relative to N (the base rate), which
        # is what makes their lift HIGH (a real coupling stands out) while the hot file's lift stays ≈ 1.
        for k in range(40):
            commit([f"misc/m{k}.py"], f"bg{k}")
        # a COINCIDENTAL pair seen ONCE — below the support floor, must be dropped.
        commit(["backend/auth.py", "docs/notes.md"], "coincidence")
        # a GIANT mass-format commit: 50 files at once. SKIPPED (else ~1225 false pairs — the dominant noise).
        _commit(d, [f"vendor/lib{n}.py" for n in range(50)], "giant_refactor")

        pairs = CC.cochange_pairs(d, window=2000, max_commit_files=40, min_support=3, min_prob=0.3, min_lift=2.0)
        idx = {tuple(sorted((p["a"], p["b"]))): p for p in pairs}

        ap = idx.get(("backend/api.py", "backend/auth.py"))
        # (1) the real coupling is found with a confidence AND a lift WELL above chance (lift >> 1).
        chk(ap is not None and ap["co"] == 5 and ap["lift"] >= 2.0 and abs(ap["strength"] - 5 / 7) < 0.02,
            f"real coupling auth↔api: confidence ~{5/7:.2f} AND lift {ap['lift'] if ap else '?'}× (>> chance) (got {ap})")
        # (★) LIFT: the HOT file (version.txt, every commit) co-changes with auth at ~100% CONFIDENCE but lift≈1
        # → it is DROPPED. This is the base-rate correction — a file that changes constantly is not "coupled".
        hot = idx.get(("backend/auth.py", "version.txt")) or idx.get(("version.txt", "backend/auth.py"))
        chk(hot is None, f"the HOT file (version.txt, changes every commit, 100% confidence) is DROPPED by lift (got {hot})")
        # (2) the coincidental once-seen pair is dropped (support floor).
        chk(("backend/auth.py", "docs/notes.md") not in idx and ("docs/notes.md", "backend/auth.py") not in idx,
            "coincidental once-seen pair (auth↔docs/notes) is dropped below the support floor")
        # (3) the GIANT commit minted NO co-change pairs.
        chk(not [p for p in pairs if p["a"].startswith("vendor/") or p["b"].startswith("vendor/")],
            "the 50-file giant commit was SKIPPED — it minted no co-change pairs")
        # (4) the completeness hint: editing auth surfaces api (NOT the hot version.txt) as the partner.
        partners = CC.cochange_partners(d, ["backend/auth.py"], min_support=3, min_prob=0.3, min_lift=2.0)
        au = partners.get("backend/auth.py", [])
        chk(any(x["partner"] == "backend/api.py" for x in au) and not any(x["partner"] == "version.txt" for x in au),
            f"cochange_partners(['auth.py']) surfaces api.py (a real coupling) and NOT version.txt (a hot file) (got {au})")
        # (5) CONTENT-FREE: the whole output carries no SECRET token (no commit message, no file body, no author).
        chk("SECRET" not in (json.dumps(pairs) + json.dumps(partners)),
            "content-free: the output carries NO commit message / file body / author / SHA — only paths + counts")

    print("CO-CHANGE GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
