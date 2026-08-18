#!/usr/bin/env python3
"""CROSS-TIER shared-config coupling — MEASURE-FIRST gate (PR #268 finding).

WHAT WAS MEASURED (tests/cross_tier_config_probe.py, 2026-06-19, on full-history full-stack
monorepos cal.com / dub / twenty / formbricks / documenso): the hypothesis was that a frontend
public-prefixed key (`NEXT_PUBLIC_STRIPE_KEY`, `VITE_SUPABASE_URL`) and a backend bare key
(`STRIPE_KEY`, `SUPABASE_URL`) sharing a STEM are the SAME config and therefore couple those two
files. The measurement says NO:

  * cross-tier-stem-sharing pairs co-change BELOW a random baseline of env-referencing files:
        cal.com  candidate mean lift 0.48  vs  random 3.72
        dub      candidate mean lift 1.55  vs  random 5.52
        twenty   candidate mean lift 0.00  vs  random 6.03
  * STRONG co-change (co>=3, lift>=2) among candidate pairs: 0.0% across all repos.
  * STRUCTURAL reason: in real monorepos the frontend public key and the backend key with the same
    stem are USUALLY DELIBERATELY DIFFERENT secrets (a `NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY` is NOT
    the server `STRIPE_SECRET_KEY` — different values, changed independently), OR they are shared
    platform infra (`SENTRY_DSN`, `VERCEL_URL`) wired identically into many UNRELATED files (high
    fan-out, low coupling). Only 4 of cal.com's 72 public-prefixed keys even HAD a bare-stem backend
    twin, and those twins co-changed near random.

VERDICT: HONEST NO — too noisy to build as a coupling edge, like #252 / #261. The literal
strip-prefix→same-stem join is WORSE than random at predicting co-change. So this lands as a
MEASUREMENT + a recall-safe guard, NOT an extractor change (we do NOT touch `_cg_config` / the
extractor / render / cochange).

THIS GATE proves, deterministically and offline (no DB, no network), that the probe's MECHANICS are
correct — so the NO is a real measurement, not a parser bug — and that the ubiquitous-stem precision
guard (reusing `_cg_config._is_ubiquitous_config_key`, supplemented with cross-tier infra words)
behaves exactly as claimed:

  A) PREFIX NORMALIZATION: every public-exposure prefix (VITE_, NEXT_PUBLIC_, REACT_APP_, PUBLIC_,
     EXPO_PUBLIC_, GATSBY_, VUE_APP_) strips to the right stem; a bare key is unchanged.
  B) CROSS-TIER JOIN: an exposed-tier file (prefix+STEM) and a private-tier file (bare STEM) are
     candidate-coupled by that stem; an exposed-ONLY stem (no backend twin) yields NO pair (honest);
     a file referencing BOTH forms of the same stem does not self-couple.
  C) UBIQUITOUS-STEM GUARD (precision floor, recall-safe): a ubiquitous stem (URL / API_URL /
     APP_URL / BASE_URL / SERVER_BASE_URL / PORT / DEBUG) is dropped — it must NOT couple everything;
     a SPECIFIC stem (STRIPE_SECRET / SUPABASE_URL / SENTRY_DSN / GOOGLE_CLIENT_ID) is KEPT. The base
     guard is reused verbatim, so the discipline is the proven one.
  D) ENV-REF EXTRACTION is content-free and multi-tier: it pulls KEY NAMES (never values/bodies) from
     process.env.X / import.meta.env.X / os.environ[...] / os.getenv / os.Getenv / .env declarations,
     and reads a `.env` NAME left of `=` WITHOUT ever reading the value to the right.
"""
from __future__ import annotations
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tests.cross_tier_config_probe as P  # noqa: E402


def main() -> int:
    checks = []

    # ---- A) PREFIX NORMALIZATION -------------------------------------------------------------
    strip_cases = {
        "VITE_STRIPE_KEY":            ("STRIPE_KEY",  "VITE_"),
        "NEXT_PUBLIC_SUPABASE_URL":   ("SUPABASE_URL", "NEXT_PUBLIC_"),
        "REACT_APP_API_TOKEN":        ("API_TOKEN",   "REACT_APP_"),
        "PUBLIC_SENTRY_DSN":          ("SENTRY_DSN",  "PUBLIC_"),
        "EXPO_PUBLIC_AMPLITUDE_KEY":  ("AMPLITUDE_KEY", "EXPO_PUBLIC_"),
        "GATSBY_SEGMENT_KEY":         ("SEGMENT_KEY", "GATSBY_"),
        "VUE_APP_GA_ID":              ("GA_ID",       "VUE_APP_"),
        "STRIPE_SECRET_KEY":          ("STRIPE_SECRET_KEY", None),   # bare backend key — unchanged
    }
    for raw, (stem, pre) in strip_cases.items():
        got = P._strip_prefix(raw)
        checks.append((f"A: _strip_prefix({raw}) -> {got} (want ({stem!r},{pre!r}))",
                       got == (stem, pre)))
    # NEXT_PUBLIC_ must strip BEFORE a bare PUBLIC_ (longest-prefix-first), else stem would be wrong
    checks.append(("A: NEXT_PUBLIC_ strips fully (not left as PUBLIC_-stripped 'NEXT_...')",
                   P._strip_prefix("NEXT_PUBLIC_FOO_BAR")[0] == "FOO_BAR"))

    # ---- B) CROSS-TIER JOIN ------------------------------------------------------------------
    keys_by_file = {
        "web/checkout.tsx":  {"VITE_STRIPE_SECRET", "VITE_FEATURE_FLAG_X"},   # exposed tier
        "api/billing.py":    {"STRIPE_SECRET"},                              # private tier (twin)
        "api/other.py":      {"UNRELATED_TOKEN"},                            # no shared stem
        "web/only.tsx":      {"NEXT_PUBLIC_ANALYTICS_WRITE_KEY"},            # exposed-ONLY (no twin)
        "shared/env.ts":     {"VITE_STRIPE_SECRET", "STRIPE_SECRET"},        # references BOTH forms
    }
    pairs, meta = P.cross_tier_pairs(keys_by_file)
    coupled = {frozenset(p) for p in pairs}

    checks.append(("B: exposed web file and backend twin ARE candidate-coupled on the shared stem",
                   frozenset(("web/checkout.tsx", "api/billing.py")) in coupled))
    checks.append(("B: an exposed-ONLY public key (no bare-stem backend twin) yields NO pair (honest)",
                   not any("web/only.tsx" in p for p in coupled)))
    checks.append(("B: an unrelated backend key is not coupled to the exposed file",
                   frozenset(("web/checkout.tsx", "api/other.py")) not in coupled))
    # a file that references BOTH forms of the SAME stem must not self-pair (no cross-FILE coupling)
    checks.append(("B: a file referencing both prefixed+bare of one stem does not self-couple",
                   not any(len(p) == 1 for p in pairs)))
    # FEATURE_FLAG_X has no backend twin → exposed-only → contributes no pair via that stem
    flag_pairs = [p for p, stems in pairs.items() if "FEATURE_FLAG_X" in stems]
    checks.append(("B: exposed-only FEATURE_FLAG_X stem contributes no candidate pair",
                   flag_pairs == []))

    # ---- C) UBIQUITOUS-STEM GUARD (precision floor, recall-safe) ------------------------------
    must_drop = ["URL", "API_URL", "APP_URL", "BASE_URL", "SERVER_BASE_URL", "WEBSITE_URL",
                 "PORT", "DEBUG", "APP_NAME", "CLIENT_HOST", "WEB_BASE_URL"]
    must_keep = ["STRIPE_SECRET", "STRIPE_PUBLISHABLE_KEY", "SUPABASE_URL", "SUPABASE_ANON_KEY",
                 "SENTRY_DSN", "GOOGLE_CLIENT_ID", "POSTHOG_KEY", "FIREBASE_API_KEY",
                 "AUTH0_DOMAIN", "STRIPE_TEAM_MONTHLY_PRICE_ID"]
    for s in must_drop:
        checks.append((f"C: ubiquitous stem {s!r} is DROPPED (must not couple everything)",
                       P._stem_is_ubiquitous(s)))
    for s in must_keep:
        checks.append((f"C: specific stem {s!r} is KEPT (real config, recall preserved)",
                       not P._stem_is_ubiquitous(s)))
    # the guard reuses the PROVEN base predicate verbatim — the discipline is not reinvented
    from _cg_config import _is_ubiquitous_config_key
    checks.append(("C: a fully-generic stem the base guard already drops is dropped here too",
                   _is_ubiquitous_config_key("LOG_LEVEL") and P._stem_is_ubiquitous("LOG_LEVEL")))
    checks.append(("C: a stem with ONE distinguishing token (stripe) is KEPT even with infra words",
                   not P._stem_is_ubiquitous("STRIPE_API_URL")))

    # ---- D) CONTENT-FREE ENV-REF EXTRACTION (names only; value right of `=` never read) -------
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        def w(rel, body):
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        w("web/a.tsx",  "const k = import.meta.env.VITE_STRIPE_KEY;\nconst n = process.env.NEXT_PUBLIC_SUPABASE_URL;\n")
        w("api/b.py",   "import os\nx = os.environ['STRIPE_KEY']\ny = os.getenv('SUPABASE_URL')\n")
        w("svc/c.go",   'v := os.Getenv("STRIPE_KEY")\n')
        w(".env",       "STRIPE_KEY=sk_live_THIS_VALUE_MUST_NEVER_BE_READ\nSUPABASE_URL=https://secret.supabase.co\n")
        files = ["web/a.tsx", "api/b.py", "svc/c.go", ".env"]   # file_keys takes (root, rel-paths); no git needed
        kbf = P.file_keys(d, files)

    checks.append(("D: import.meta.env.X and process.env.X key NAMES extracted from frontend",
                   {"VITE_STRIPE_KEY", "NEXT_PUBLIC_SUPABASE_URL"} <= kbf.get("web/a.tsx", set())))
    checks.append(("D: os.environ[...] and os.getenv(...) key NAMES extracted from python backend",
                   {"STRIPE_KEY", "SUPABASE_URL"} <= kbf.get("api/b.py", set())))
    checks.append(("D: os.Getenv(...) key NAME extracted from go backend",
                   "STRIPE_KEY" in kbf.get("svc/c.go", set())))
    checks.append(("D: .env declared NAMES (left of `=`) extracted",
                   {"STRIPE_KEY", "SUPABASE_URL"} <= kbf.get(".env", set())))
    # CONTENT-FREE proof: NO extracted token contains any part of a VALUE (right of `=`)
    all_tokens = set()
    for ks in kbf.values():
        all_tokens |= ks
    leaked = {t for t in all_tokens if "sk_live" in t or "secret.supabase" in t or "VALUE_MUST" in t}
    checks.append((f"D: content-free — no `.env` VALUE leaked into extracted key names (leaked={leaked})",
                   not leaked))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("CROSS-TIER CONFIG PROBE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
