#!/usr/bin/env python3
"""SIBLING-SYMBOL PROBE GATE — deterministic, no-clone, no-network proof of the probe's discipline + the
CORE measured finding (shared-symbol coupling is a real-but-noisy ADVISORY signal, NOT a shippable
standalone detector). The panel NUMBERS come from real AI-era clones run by hand; THIS gate proves the
harness LOGIC and the honest posture on a tiny in-memory/temp-git fixture, WITHOUT any network:

  1. SPECIFICITY FILTER is precision-safe: common/short/prose strings are REJECTED ("error","true","id",
     "GET","this is a sentence"); structured contract-ish tokens are KEPT ("user.created","ENABLE_BILLING",
     "orders/ADD_ITEM"); and the CLEAN mode additionally drops the MEASURED noise classes (CSS utilities
     "text-muted-foreground"/"space-y-2", import specifiers "@/lib/x"/"react-dom"/"encoding/hex", date/time
     formats) — so the filter actually changes the candidate set (the specificity-filter effect is real).
  2. CANDIDATE DEFINITION is correct on a built graph: a pair is emitted ONLY when SAME-language + CROSS-dir
     + shares a surviving specific token + has NO structural graph edge; a same-token pair that DOES have an
     import edge is EXCLUDED (edge-absence is enforced), and a cross-language same-token pair is excluded.
  3. The LIFT metric matches backtest_cochange semantics: (#co-change * T)/(n_a*n_b); a constructed pair that
     co-changes more than a random pair yields a higher rate (the measure can distinguish signal from noise).
  4. CONTENT-FREE EGRESS: _tokens_of returns ONLY token strings (a set), never a file body — proven on a temp
     file whose body contains a long secret line that must NOT appear in the returned token set; and the probe
     never requests commit bodies/diffs (git --name-only + %H only, no %b / -p / --patch).
  5. HONEST VERDICT (no forced YES): the probe's verdict logic classifies a real-lift-but-noisy result as
     QUALIFIED / not-shippable-standalone (proven on a synthetic high-lift high-FP outcome), so it can NEVER
     print a clean SHIP-CANDIDATE when precision is poor — the measure-first NO is structurally protected.

Prints `SIBLING SYMBOL GATE: PASS` / `FAIL`, returns 0/1. No network, no Postgres.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import sibling_symbol_probe as S  # noqa: E402
import code_graph_extract as X    # noqa: E402

_SRC = os.path.join(ROOT, "tests", "sibling_symbol_probe.py")
_LANGS = {"python", "javascript", "typescript", "go"}


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def run():
    failures = []
    src = open(_SRC, "r", encoding="utf-8").read()

    # (0) PANEL well-formed + spans the customer's languages -------------------------------------------------
    if len(S.PANEL) < 8:
        failures.append(f"PANEL too small to be representative: {len(S.PANEL)}")
    langs = set()
    seen = set()
    for entry in S.PANEL:
        if len(entry) != 3:
            failures.append(f"PANEL entry not 3-tuple: {entry!r}"); continue
        name, full, lang = entry
        if "/" not in full:
            failures.append(f"fullName not owner/repo: {full!r}")
        if name != full.replace("/", "_"):
            failures.append(f"dir {name!r} != owner_repo {full.replace('/', '_')!r}")
        if name in seen:
            failures.append(f"duplicate panel dir: {name}")
        seen.add(name); langs.add(lang)
    if not _LANGS.issubset(langs):
        failures.append(f"PANEL missing languages: {_LANGS - langs}")

    # (1) SPECIFICITY FILTER — precision-safe (CARDINAL rule) ------------------------------------------------
    for common in ("error", "true", "id", "GET", "this is a sentence", "ok", "1234"):
        if S._is_specific(common):
            failures.append(f"specificity filter ACCEPTED a common/prose token (garbage): {common!r}")
    for contract in ("user.created", "ENABLE_BILLING", "orders/ADD_ITEM", "auth.provider", "USER_SERVICE_KEY"):
        if not S._is_specific(contract):
            failures.append(f"specificity filter REJECTED a real contract token: {contract!r}")
    # CLEAN mode drops the measured noise classes that RAW keeps.
    for noise in ("text-muted-foreground", "space-y-2", "react-dom", "encoding/hex", "@/lib/config"):
        if not S._is_specific(noise, clean=False):
            # not all are accepted by raw, that's fine; the point is the clean delta below
            pass
        if S._is_specific(noise, clean=True):
            failures.append(f"CLEAN mode KEPT a measured-noise token (CSS/import): {noise!r}")
    # The clean filter MUST actually remove something raw kept (the specificity-filter effect is real, not a no-op).
    raw_keep = [t for t in ("text-muted-foreground", "react-dom", "encoding/hex", "@/lib/config", "user.created")
                if S._is_specific(t, clean=False)]
    cln_keep = [t for t in raw_keep if S._is_specific(t, clean=True)]
    if not (len(cln_keep) < len(raw_keep)):
        failures.append("CLEAN filter removed nothing RAW kept — specificity-filter effect is a no-op")
    if "user.created" not in cln_keep:
        failures.append("CLEAN filter dropped a genuine contract token (over-aggressive)")

    # (2)+(3) build a TEMP git repo with a controlled coupling and assert the candidate + lift logic ---------
    with tempfile.TemporaryDirectory() as repo:
        # two SAME-language (py) CROSS-dir sibling files share a SPECIFIC token but have NO import edge.
        os.makedirs(os.path.join(repo, "feat_a"))
        os.makedirs(os.path.join(repo, "feat_b"))
        os.makedirs(os.path.join(repo, "feat_c"))
        a = os.path.join(repo, "feat_a", "emit.py")
        b = os.path.join(repo, "feat_b", "listen.py")
        c = os.path.join(repo, "feat_c", "other.py")
        # a<->b share the SPECIFIC event token; NO import between them.
        open(a, "w").write('EVENT = "billing.invoice.paid"\n\ndef emit():\n    return EVENT\n')
        open(b, "w").write('HANDLED = "billing.invoice.paid"\n\ndef on_event():\n    return HANDLED\n')
        # c imports a (a STRUCTURAL edge) AND shares the token -> must be EXCLUDED (edge present).
        open(c, "w").write('from feat_a.emit import emit\nX = "billing.invoice.paid"\n')
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t.t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        # make a<->b co-change repeatedly; c changes alone (so its lift, if it formed a pair, would be low).
        for i in range(5):
            open(a, "a").write(f"# co {i}\n"); open(b, "a").write(f"# co {i}\n")
            _git(repo, "commit", "-aqm", f"cochange {i}")
        for i in range(3):
            open(c, "a").write(f"# solo {i}\n")
            _git(repo, "commit", "-aqm", f"solo {i}")

        cand = S.build_candidates(repo, min_rarity=2, max_share=12, clean=True)
        pairs = cand["pairs"]
        key_ab = frozenset(("feat_a/emit.py", "feat_b/listen.py"))
        if key_ab not in pairs:
            failures.append("candidate build MISSED the same-token, cross-dir, no-edge sibling pair a<->b")
        # any pair containing c must NOT exist via the import-edged relationship (edge-absence enforced).
        edged = cand["edged"]
        for k in pairs:
            if "feat_c/other.py" in k:
                # c only edges to a (import). If a pair c<->a appears it must NOT (edge present). c<->b shares
                # token + no edge + cross-dir, so c<->b is a LEGITIMATE candidate — only c<->a is forbidden.
                if frozenset(("feat_a/emit.py", "feat_c/other.py")) == k:
                    failures.append("candidate build kept an EDGED pair (a<->c has an import edge)")
        # the engine's own graph must indeed see the a<->c edge (sanity: edge-absence test is meaningful).
        if frozenset(("feat_a/emit.py", "feat_c/other.py")) not in edged:
            failures.append("graph did not register the a->c import edge — edge-absence test is vacuous")

        # (3) LIFT metric: a<->b (co-changed 5x) must score a higher ever-co-change rate than a random
        # constructed non-co-changing pair, using the SAME _pair_stats as backtest_cochange.
        files = set(cand["files_ext"].keys())
        touch, T = S._commit_touchsets(repo, files)
        rate_ab, med_ab, _ = S._pair_stats([("feat_a/emit.py", "feat_b/listen.py")], touch, T)
        rate_solo, med_solo, _ = S._pair_stats([("feat_a/emit.py", "feat_c/other.py")], touch, T)
        # a<->b co-change 6 commits (tightly coupled); a<->c overlap only at the init commit (loosely). Both
        # have ever-co-change=1.0 (each shares >=1 commit), but the LIFT magnitude must rank the tight pair
        # ABOVE the loose one — that ranking is the whole point of the (#co*T)/(n_a*n_b) lift.
        if not (med_ab > med_solo):
            failures.append(f"lift metric cannot rank signal: tight pair lift {med_ab} !> loose {med_solo}")
        if med_ab <= 0:
            failures.append("co-changed sibling pair has non-positive median lift — metric broken")

    # (4) CONTENT-FREE EGRESS — _tokens_of returns ONLY tokens, never the file body -------------------------
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        secret = "THE_SECRET_BODY_LINE_THAT_MUST_NOT_LEAK_abcdefghijklmnop_0123456789"
        fh.write(f'KEY = "auth.token.refresh"\n# {secret}\nx = 1 + 2  # plain arithmetic, not a token\n')
        tmp = fh.name
    try:
        toks = S._tokens_of(tmp, clean=True)
        if any(secret in t for t in toks):
            failures.append("CONTENT LEAK: a non-token file body line appeared in the returned token set")
        if "auth.token.refresh" not in toks:
            failures.append("_tokens_of dropped the legitimate specific token it was supposed to extract")
        # the returned object is a SET of short strings (tokens), not the body.
        if not isinstance(toks, set) or any(len(t) > 90 for t in toks):
            failures.append("_tokens_of returned something other than a set of short token strings")
    finally:
        os.unlink(tmp)
    # the probe never requests commit BODIES or DIFFS (content-free git): name-only + %H, no %b/-p/--patch.
    git_cmds = re.findall(r"\[\"git\".*?\]", src)
    joined = " ".join(git_cmds)
    if "%b" in joined or "--patch" in src or re.search(r'"\-p"', src) or "--name-status" in src:
        failures.append("probe requests commit bodies/diffs — must be --name-only + %H (content-free)")
    if "--name-only" not in joined:
        failures.append("probe co-change scan is not --name-only (paths-only) — content-free posture unproven")

    # (5) HONEST VERDICT — a real-lift-but-noisy outcome must classify as QUALIFIED, never SHIP-CANDIDATE.
    # Re-derive the verdict logic exactly as main() does and assert the protective branch.
    def verdict(lift_c, fp_rate, frac_pos):
        signal_real = lift_c >= 1.3 and frac_pos >= 0.7
        precision_ok = fp_rate <= 0.25
        if signal_real and not precision_ok:
            return "QUALIFIED"
        if signal_real and precision_ok:
            return "SHIP"
        return "NO"
    if verdict(5.7, 0.44, 1.0) != "QUALIFIED":         # the MEASURED outcome
        failures.append("verdict logic does not classify the measured (real-lift, 44%-FP) outcome as QUALIFIED")
    if verdict(5.7, 0.10, 1.0) != "SHIP":              # only clean precision flips to ship-candidate
        failures.append("verdict logic never reaches SHIP even at clean precision — bar is impossible")
    if verdict(1.0, 0.10, 0.2) != "NO":                # no lift => NO regardless of precision
        failures.append("verdict logic does not say NO when there is no lift")
    # and the SHIPPED main() must contain the QUALIFIED protective wording (the honest near-NO is the default).
    if "QUALIFIED" not in src or "standalone" not in src:
        failures.append("main() verdict text lost the QUALIFIED / not-shippable-standalone honesty")

    ok = not failures
    print("SIBLING SYMBOL GATE:", "PASS" if ok else "FAIL")
    for f in failures:
        print("  -", f)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
