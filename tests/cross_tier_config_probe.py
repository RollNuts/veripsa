#!/usr/bin/env python3
"""CROSS-TIER shared-config coupling — a standalone, content-free MEASUREMENT probe.

THE GAP (PR #268). AI-era full-stack monorepos couple backend↔frontend not only via API routes but
via SHARED config/secrets exposed to BOTH tiers under a build-time PUBLIC-exposure prefix. The SAME
underlying secret is referenced two ways:

    backend:   process.env.STRIPE_SECRET_KEY        os.environ['SUPABASE_URL']
    frontend:  import.meta.env.VITE_STRIPE_KEY      process.env.NEXT_PUBLIC_SUPABASE_URL

These are the SAME config split by a framework prefix (`VITE_`, `NEXT_PUBLIC_`, `REACT_APP_`,
`PUBLIC_`, `EXPO_PUBLIC_`, `GATSBY_`, `VUE_APP_`). Rotate/rename the secret and you must change BOTH
files — a real coupling. Veripsa's config graph matches the SAME key within a tier (a `reads_config`
edge to the same dst) but is BLIND to this cross-tier STEM match (`VITE_STRIPE_KEY` ≠ `STRIPE_KEY`
as strings, so no shared dst, no pair). This probe MEASURES whether bridging that prefix divide is a
real coupling signal — and at what precision.

WHAT IT DOES (content-free — KEY NAMES + file PATHS only, never a value/body; a `.env` VALUE right of
`=` is never read):
  1. Extract env/config key NAMES referenced per file from BOTH tiers:
     process.env.X / import.meta.env.X (JS/TS), os.environ['X'] / os.getenv("X") (py),
     os.Getenv("X") (go), .env* declared NAMEs, ALL-CAPS env-style identifiers.
  2. Normalize the cross-tier STEM: strip a leading public-exposure PREFIX → the stem
     (VITE_STRIPE_KEY → STRIPE_KEY). A file that references a PREFIXED key is "exposed-tier"
     (frontend-ish); one that references the bare stem is "private-tier" (backend-ish).
  3. CANDIDATE cross-tier coupling: an exposed-tier file referencing prefix+STEM and a private-tier
     file referencing the bare STEM ⇒ those two files are candidate-coupled by that stem.
  4. PRECISION GUARD (reuse + mirror the PROVEN ubiquitous-config-key discipline): a UBIQUITOUS stem
     (every word-token generic — PORT/DEBUG/URL/KEY, or an infra word like API_URL/APP_URL/BASE_URL)
     must NOT couple everything. We reuse `_cg_config._is_ubiquitous_config_key` (READ-ONLY) as the
     base layer and supplement it with the cross-tier infra words it does not yet carry
     (api/app/base/web/client/server/public/host/site/url-stems) — same spirit, applied HERE so the
     extractor/_cg_config are untouched (measure-first). When in doubt → KEEP.
  5. GROUND TRUTH = co-change lift (the proxy recall_measure.py / measure_config_precision.py use):
     do files sharing a cross-tier stem co-change ABOVE random (lift > 1)? Split by SPECIFIC vs
     UBIQUITOUS stem and compare. SPECIFIC stems should co-change far above random; ubiquitous stems
     should not — that is the false-positive the guard removes.

This is MEASURE-FIRST: a probe + a deterministic gate, NOT an extractor change. Numbers are the
deliverable. Run:  python3 tests/cross_tier_config_probe.py /repo/with/history [/repo2 ...]
"""
from __future__ import annotations
import os
import re
import sys
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _cg_config import _is_ubiquitous_config_key, _key_word_tokens  # READ-ONLY reuse of the proven guard

MAX_COMMIT_FILES = int(os.environ.get("BACKTEST_MAX_COMMIT_FILES", "50"))
HUB_DEGREE = int(os.environ.get("XTIER_HUB_DEGREE", "12"))

# --- public-exposure PREFIXES that split one secret into a frontend-visible twin (build-time inlined) ---
# Ordered longest-first so NEXT_PUBLIC_ strips before a bare PUBLIC_ would (it never reaches the short one,
# but explicit is safer). EXPO_PUBLIC_ before EXPO_ for the same reason.
_PUBLIC_PREFIXES = (
    "NEXT_PUBLIC_", "EXPO_PUBLIC_", "REACT_APP_", "GATSBY_", "VUE_APP_",
    "VITE_", "PUBLIC_",
)

# Cross-tier INFRA words: generic across a full-stack app's two tiers (every app has an api/app/base
# url, a host, a site). They are NOT secrets whose rotation couples a specific backend↔frontend pair;
# matching on them would couple nearly every file (API_URL appears everywhere). The base guard
# (_is_ubiquitous_config_key) does NOT yet carry these (api/app/base aren't generic config WORDS), so
# we add them HERE — same discipline, applied in the probe so _cg_config stays untouched. A stem is
# ubiquitous when EVERY one of its word-tokens is generic (base-guard words ∪ these). One distinguishing
# token (`stripe` in STRIPE_API_URL, `supabase` in SUPABASE_URL) → KEPT. When in doubt → KEEP.
_XTIER_INFRA_WORDS = frozenset({
    "api", "app", "application", "base", "web", "website", "site", "client", "server",
    "public", "private", "frontend", "backend", "front", "back", "service", "gateway",
    "main", "root", "global", "common", "shared", "core",
})

# env-access patterns across the tiers — capture the KEY NAME only (group 1). Content-free.
_ENV_PATTERNS = [
    re.compile(r'process\.env\.([A-Za-z_][A-Za-z0-9_]*)'),                 # JS/TS  process.env.X
    re.compile(r'process\.env\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*\]'),  # process.env['X']
    re.compile(r'import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)'),            # Vite   import.meta.env.X
    re.compile(r'import\.meta\.env\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*\]'),
    re.compile(r'os\.environ\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*\]'),    # py  os.environ['X']
    re.compile(r'os\.environ\.get\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),    # py  os.environ.get("X")
    re.compile(r'os\.getenv\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),          # py  os.getenv("X")
    re.compile(r'getenv\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),              # C/py getenv("X")
    re.compile(r'os\.Getenv\(\s*["`]([A-Za-z_][A-Za-z0-9_]*)["`]\)'),          # go  os.Getenv("X")
    re.compile(r'Deno\.env\.get\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),      # deno Deno.env.get("X")
    re.compile(r'env\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),                 # laravel/django env("X")
    re.compile(r'ENV\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*\]'),            # ruby ENV['X']
]
# a declared NAME in a .env file (left of `=`) — the VALUE (right of `=`) is NEVER read.
_DOTENV_DECL = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', re.M)

_SRC_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
             ".py", ".go", ".rb", ".php", ".java", ".kt"}
_SKIP_DIR = re.compile(r'(^|/)(node_modules|\.git|dist|build|out|\.next|vendor|__pycache__|coverage|\.turbo|target)(/|$)')
_MAX_FILE_BYTES = 1_000_000


def _strip_prefix(key):
    """(stem, prefix) — strip the FIRST matching public-exposure prefix; (key, None) if none."""
    for p in _PUBLIC_PREFIXES:
        if key.startswith(p) and len(key) > len(p):
            return key[len(p):], p
    return key, None


def _stem_is_ubiquitous(stem):
    """Final ubiquitous-stem predicate: every word-token is generic — generic = the base guard calls
    the single-token key ubiquitous, OR it is a cross-tier infra word. One specific token → KEEP."""
    if _is_ubiquitous_config_key(stem):
        return True
    toks = _key_word_tokens(stem)
    if not toks:
        return False
    for t in toks:
        if t in _XTIER_INFRA_WORDS:
            continue
        if _is_ubiquitous_config_key(t):   # the bare word is itself a generic config word
            continue
        return False                        # a distinguishing token → KEEP the stem
    return True


def file_keys(repo, files):
    """{path: set(raw_key_names)} for code/env files under `files`. Content-free: names only."""
    out = {}
    for rel in files:
        ap = os.path.join(repo, rel)
        base = os.path.basename(rel)
        is_env = base == ".env" or base.startswith(".env.")
        ext = os.path.splitext(rel)[1]
        if not is_env and ext not in _SRC_EXTS:
            continue
        try:
            if os.path.getsize(ap) > _MAX_FILE_BYTES:
                continue
            with open(ap, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        keys = set()
        if is_env:
            keys.update(_DOTENV_DECL.findall(text))
        else:
            for pat in _ENV_PATTERNS:
                keys.update(pat.findall(text))
        if keys:
            out[rel] = keys
    return out


def cross_tier_pairs(keys_by_file):
    """{frozenset(exposed_file, private_file): set(stems)} — an exposed-tier file referencing a
    PREFIXED key and a private-tier file referencing the bare STEM, joined on the stem.

    Tier is per (file, stem): a file is the EXPOSED side of a stem if it references prefix+stem; the
    PRIVATE side if it references the bare stem. A file can be exposed for one stem and private for
    another (e.g. a shared config module). We pair exposed↔private across the SAME stem only."""
    exposed = {}   # stem -> set(files referencing it WITH a prefix)
    private = {}   # stem -> set(files referencing the BARE stem)
    for f, keys in keys_by_file.items():
        for k in keys:
            stem, prefix = _strip_prefix(k)
            if prefix is not None:
                exposed.setdefault(stem, set()).add(f)
            else:
                private.setdefault(stem, set()).add(f)
    pairs = {}
    stem_meta = {}   # stem -> (n_exposed, n_private)
    for stem, efs in exposed.items():
        pfs = private.get(stem)
        if not pfs:
            continue   # exposed-only stem: no backend twin in THIS repo (honest — no candidate)
        stem_meta[stem] = (len(efs), len(pfs))
        for ef in efs:
            for pf in pfs:
                if ef == pf:
                    continue   # same file references both forms — not a cross-FILE coupling
                pairs.setdefault(frozenset((ef, pf)), set()).add(stem)
    return pairs, stem_meta


def commit_touchsets(repo, files):
    out = subprocess.run(
        ["git", "-C", repo, "log", "--no-merges", "--name-only", "--pretty=format:@@%H"],
        capture_output=True, text=True).stdout
    touch, idx, cur, T = {}, -1, [], set()

    def flush():
        if cur and len(cur) <= MAX_COMMIT_FILES:
            for f in cur:
                touch.setdefault(f, set()).add(idx)
            T.add(idx)
    fset = set(files)
    for line in out.splitlines():
        if line.startswith("@@"):
            flush(); cur = []; idx += 1
        elif idx >= 0 and line in fset:
            cur.append(line)
    flush()
    return touch, len(T)


def lift_of(pair, touch, T):
    a, b = tuple(pair)
    sa, sb = touch.get(a), touch.get(b)
    if not sa or not sb:
        return None, 0      # a file with no history — UNKNOWN, excluded honestly
    nco = len(sa & sb)
    if nco == 0:
        return 0.0, 0
    return (nco * T) / (len(sa) * len(sb)), nco


def _tracked_files(repo):
    out = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if not _SKIP_DIR.search(f)]


def analyze(repo):
    files = _tracked_files(repo)
    keys_by_file = file_keys(repo, files)
    pairs, stem_meta = cross_tier_pairs(keys_by_file)
    touch, T = commit_touchsets(repo, list(keys_by_file.keys()))

    specific, ubiquitous = [], []      # each entry: (lift, nco)
    spec_stems, ubiq_stems = {}, {}
    for pair, stems in pairs.items():
        spec = {s for s in stems if not _stem_is_ubiquitous(s)}
        lift, nco = lift_of(pair, touch, T)
        if lift is None:
            continue
        if spec:
            specific.append((lift, nco))
            for s in spec:
                spec_stems[s] = spec_stems.get(s, 0) + 1
        else:
            ubiquitous.append((lift, nco))
            for s in stems:
                ubiq_stems[s] = ubiq_stems.get(s, 0) + 1

    def stats(rows):
        n = len(rows)
        if n == 0:
            return dict(n=0, mean_lift=0.0, median_lift=0.0, frac_cochange=0.0, frac_strong=0.0)
        lifts = sorted(r[0] for r in rows)
        cochange = sum(1 for l, c in rows if c > 0)
        strong = sum(1 for l, c in rows if c >= 3 and l >= 2.0)   # recall_measure "real coupling" bar
        return dict(n=n, mean_lift=sum(lifts) / n, median_lift=lifts[n // 2],
                    frac_cochange=cochange / n, frac_strong=strong / n)

    return {
        "repo": os.path.basename(repo.rstrip("/")),
        "files_with_env": len(keys_by_file), "commits": T,
        "candidate_stems": len(stem_meta),
        "total_pairs": len(pairs),
        "specific": stats(specific), "ubiquitous": stats(ubiquitous),
        "top_spec_stems": sorted(spec_stems.items(), key=lambda kv: -kv[1])[:14],
        "top_ubiq_stems": sorted(ubiq_stems.items(), key=lambda kv: -kv[1])[:14],
    }


def main():
    repos = [os.path.abspath(p) for p in sys.argv[1:]] or [ROOT]
    rows = []
    for r in repos:
        if not os.path.isdir(os.path.join(r, ".git")):
            print(f"  (skip {r}: not a git checkout)")
            continue
        rows.append(analyze(r))
    if not rows:
        print("No repos to measure (pass full-history full-stack checkouts).")
        return 0

    print("\n=== CROSS-TIER CONFIG COUPLING — MEASUREMENT (ground truth = co-change lift) ===")
    agg = {"sn": 0, "sl": 0.0, "sco": 0.0, "sst": 0.0,
           "un": 0, "ul": 0.0, "uco": 0.0, "ust": 0.0}
    for r in rows:
        s, u = r["specific"], r["ubiquitous"]
        print("\n" + "=" * 94)
        print(f"{r['repo']}  —  {r['files_with_env']} env-referencing files, {r['commits']} commits, "
              f"{r['candidate_stems']} cross-tier stems, {r['total_pairs']} candidate pairs")
        print("=" * 94)
        print(f"  SPECIFIC-stem pairs:   {s['n']:>5}   mean lift {s['mean_lift']:6.2f}   median {s['median_lift']:5.2f}   "
              f"co-change>0 {s['frac_cochange']*100:4.0f}%   STRONG(co>=3,lift>=2) {s['frac_strong']*100:4.1f}%")
        print(f"  UBIQUITOUS-stem pairs: {u['n']:>5}   mean lift {u['mean_lift']:6.2f}   median {u['median_lift']:5.2f}   "
              f"co-change>0 {u['frac_cochange']*100:4.0f}%   STRONG(co>=3,lift>=2) {u['frac_strong']*100:4.1f}%")
        print(f"  specific stems kept:    {[k for k,_ in r['top_spec_stems']]}")
        print(f"  ubiquitous stems dropped: {[k for k,_ in r['top_ubiq_stems']]}")
        agg["sn"] += s["n"]; agg["sl"] += s["mean_lift"] * s["n"]
        agg["sco"] += s["frac_cochange"] * s["n"]; agg["sst"] += s["frac_strong"] * s["n"]
        agg["un"] += u["n"]; agg["ul"] += u["mean_lift"] * u["n"]
        agg["uco"] += u["frac_cochange"] * u["n"]; agg["ust"] += u["frac_strong"] * u["n"]

    print("\n" + "#" * 94)
    print("AGGREGATE across repos  (lift 1.0 == random co-change; > 1 == above random)")
    print("#" * 94)
    if agg["sn"]:
        print(f"  SPECIFIC-stem (KEPT):   {agg['sn']:>5} pairs   mean lift {agg['sl']/agg['sn']:6.2f}   "
              f"co-change {agg['sco']/agg['sn']*100:4.0f}%   STRONG {agg['sst']/agg['sn']*100:4.1f}%")
    if agg["un"]:
        print(f"  UBIQUITOUS-stem (DROP): {agg['un']:>5} pairs   mean lift {agg['ul']/agg['un']:6.2f}   "
              f"co-change {agg['uco']/agg['un']*100:4.0f}%   STRONG {agg['ust']/agg['un']*100:4.1f}%")
    if agg["sn"] and agg["un"]:
        sl, ul = agg["sl"] / agg["sn"], agg["ul"] / max(agg["un"], 1)
        print(f"\n  specific-vs-ubiquitous mean-lift ratio: {sl/ul:.2f}x" if ul else "  (ubiq mean lift 0)")
    print("\nINTERPRETATION: if SPECIFIC cross-tier stems co-change ABOVE random (lift > 1) and well")
    print("above the UBIQUITOUS-stem pairs, bridging the public-exposure prefix is a real coupling")
    print("signal and the ubiquitous-stem guard is the precision floor. If specific ~= random or the")
    print("guard cannot separate them, the honest verdict is NO (too noisy), like #252 / #261.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
