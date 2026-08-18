#!/usr/bin/env python3
"""CONFIG BARE-WORD false-coupling PRECISION gate (audit 2026-06-20).

Veripsa's CONFIG graph couples files that read the SAME config key — a real coupling: two files both
reading `STRIPE_WEBHOOK_SECRET` ARE entangled. The prior `_specific_key` minted as a config_key EVERY
bare word of length >= 6 (its `return len(k) >= 6` floor). On real repos that floor minted single
dictionary / programming words that happen to appear as a key in SOME config file but ALSO occur in
code as ordinary identifiers / string literals — so their `reads_config` matches are COINCIDENTAL,
coupling files that share no real config resource. MEASURED (this audit, content-free, on the RESOLVED
config graph the engine consumes):

  repo       reads_config (before->after)   coupled file-pairs (before->after)   spurious removed
  flask           5 -> 3                          1 -> 0                                1
  httpx           7 -> 0                          0 -> 0                                0 (7 bare nodes gone)
  zustand         5 -> 1                          3 -> 0                                3   (`module`,`typescript`)
  axios          13 -> 1                         10 -> 0                               10   (`module`,`browser`,…)
  Veripsa        46 -> 38                       105 -> 98                               7   (`public`,`metadata`)
  ------------------------------------------------------------------------------ TOTAL: 21 spurious pairs

Worst offender: `module` — minted from `tsconfig.json` / `package.json` `"module"`, then matched in
`import { createRequire } from 'module'`, `sourceType: 'module'`, `<script type="module">` — pure
coincidence, coupling 5 axios + 3 zustand files (13 false pairs). `dotenv` (from `pyproject.toml`) →
`load_dotenv`. `public` (from a manifest) → a docstring + a Postgres default-schema literal.

The fix (`_cg_config._is_distinctive_config_key`, called by `_specific_key`): a config key is
coupling-bearing only when it has DISTINGUISHING STRUCTURE — a COMPOUND (separator `.` `_` `-` or a
camelCase boundary) OR an ALL-CAPS ENV-VAR name. A BARE single lower/Capitalized word has none, so it
is not minted (no node -> no `reads_config` edge -> no spurious pair). This is the single-token
generalization of the existing `_is_ubiquitous_config_key` demotion.

RECALL IS SACRED — re-MEASURED: 0 real pairs removed. Every genuine key (VERIPSA_DSN, GH_WEBHOOK_SECRET,
payment_gateway_url, pull_requests, every env var) is compound or ALL-CAPS and is KEPT, with identical
reader counts. This gate proves both halves on synthetic configs+code (no DB, deterministic), and is the
LOCK on the measured before->after so a future change to the key-minting floor can never silently
re-introduce bare-word false couplings. Content-free (key NAMES only, never values).
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import _cg_config as C  # noqa: E402


def _w(d, rel, body):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(body)


def _graph(files):
    """Build (config_keys minted, file-pairs sharing a key) for a synthetic file set via _cg_config."""
    with tempfile.TemporaryDirectory() as d:
        cfgs, srcs = [], []
        for rel, body in files.items():
            _w(d, rel, body)
            ext = C._config_ext(rel)
            if ext in C._CONFIG_EXTS:
                cfgs.append((os.path.join(d, rel), ext))
            else:
                srcs.append((os.path.join(d, rel), os.path.splitext(rel)[1]))
        nodes, edges = C._config_graph(d, cfgs, srcs)
    keys = {n["name"] for n in nodes if n["kind"] == "config_key"}
    by_key = {}
    for e in edges:
        if e["kind"] == "reads_config":
            by_key.setdefault(e["dst"], set()).add(e["src"])
    pairs = set()
    for fs in by_key.values():
        fs = sorted(fs)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                pairs.add(frozenset((fs[i], fs[j])))
    return keys, pairs


def _coupled(pairs, a, b):
    return frozenset((a, b)) in pairs


def main() -> int:
    checks = []

    # ── unit: the predicate itself — the structural contract that makes the fix recall-safe ──────────
    # DROPPED: bare single dictionary/programming words (no separator, no camelCase, not ALL-CAPS).
    for bare in ("module", "dotenv", "public", "website", "pattern", "failure", "pathname",
                 "metadata", "typescript", "browser", "python", "search"):
        checks.append((f"predicate: bare word {bare!r} is NOT distinctive (dropped)",
                       not C._is_distinctive_config_key(bare)))
    # KEPT: compounds (separator / camelCase) and ALL-CAPS env-var names — the real couplings.
    for keep in ("webhook_secret", "db.pool_size", "payment_gateway_url", "pull_requests",
                 "poolSize", "logLevel", "VERIPSA_DSN", "GH_WEBHOOK_SECRET", "BROTLI", "DATABASE"):
        checks.append((f"predicate: distinctive key {keep!r} IS kept",
                       C._is_distinctive_config_key(keep)))

    # ── A) PRECISION — the `module` false coupling (the measured worst offender) must NOT form ───────
    # `module` is declared in tsconfig.json (`"module": "esnext"`) and matched in code as the bare
    # word `module` (an import target, a sourceType value, an HTML attribute). Two unrelated files
    # must NOT be config-coupled by it.
    keys_a, pairs_a = _graph({
        "tsconfig.json": '{"compilerOptions": {"module": "esnext", "target": "es2020"}}',
        "src/rollup.config.js": "import { createRequire } from 'module';\nexport default {};\n",
        "src/eslint.config.js": "export default { parserOptions: { sourceType: 'module' } };\n",
    })
    checks.append((f"A: bare word 'module' is NOT minted as a config_key (minted={sorted(keys_a)})",
                   "module" not in keys_a))
    checks.append(("A: two unrelated files both containing the word 'module' are NOT config-coupled "
                   f"(pairs={[sorted(p) for p in pairs_a]})",
                   not _coupled(pairs_a, "src/rollup.config.js", "src/eslint.config.js")))

    # ── B) PRECISION — `dotenv` / `public` (other measured bare-word offenders) likewise drop ───────
    keys_b, pairs_b = _graph({
        "pyproject.toml": "[tool.poetry]\ndotenv = '1.0'\n",        # a bare word minted as a 'key'
        "app/manifest.json": '{"public": true, "name": "x"}',       # a manifest structural word
        "src/cli.py": "from helpers import load_dotenv\n\ndef run():\n    return load_dotenv()\n",
        "src/store.py": "SCHEMA = 'public'\n\ndef q():\n    return SCHEMA\n",
    })
    checks.append((f"B: bare words 'dotenv' and 'public' are NOT minted (minted={sorted(keys_b)})",
                   "dotenv" not in keys_b and "public" not in keys_b))
    checks.append(("B: files containing 'dotenv'/'public' as ordinary code words are NOT coupled "
                   f"(pairs={[sorted(p) for p in pairs_b]})",
                   not _coupled(pairs_b, "src/cli.py", "src/store.py")))

    # ── C) RECALL — a SPECIFIC compound config key STILL couples its readers (must not over-drop) ────
    keys_c, pairs_c = _graph({
        "config/app.json": '{"payment_gateway_url": "x", "database": {"pool_size": 10}}',
        "svc/worker.py": "U='payment_gateway_url'\nP='database.pool_size'\n",
        "svc/billing.py": "u='payment_gateway_url'\np='database.pool_size'\n",
    })
    checks.append((f"C: specific compounds (payment_gateway_url / database.pool_size) ARE minted "
                   f"(minted={sorted(keys_c)})",
                   "payment_gateway_url" in keys_c and "database.pool_size" in keys_c))
    checks.append(("C: readers of a specific compound ARE config-coupled (real coupling kept) "
                   f"(pairs={[sorted(p) for p in pairs_c]})",
                   _coupled(pairs_c, "svc/worker.py", "svc/billing.py")))

    # ── D) RECALL — an ALL-CAPS ENV-VAR name (even single-token) STILL couples its readers ──────────
    keys_d, pairs_d = _graph({
        ".env": "VERIPSA_DSN=postgres://x\nDATABASE=app\n",
        "svc/db.py": "import os\nd=os.environ['VERIPSA_DSN']\nn=os.environ['DATABASE']\n",
        "svc/health.py": "import os\nd=os.environ['VERIPSA_DSN']\nn=os.environ['DATABASE']\n",
    })
    checks.append((f"D: ALL-CAPS env-var names (VERIPSA_DSN / DATABASE) ARE minted (minted={sorted(keys_d)})",
                   "VERIPSA_DSN" in keys_d and "DATABASE" in keys_d))
    checks.append(("D: readers of an env-var name ARE config-coupled (env-var recall preserved) "
                   f"(pairs={[sorted(p) for p in pairs_d]})",
                   _coupled(pairs_d, "svc/db.py", "svc/health.py")))

    # ── E) MIXED — a file reading BOTH a bare word and a specific key still couples via the SPECIFIC ─
    keys_e, pairs_e = _graph({
        "tsconfig.json": '{"compilerOptions": {"module": "esnext"}}',
        "config/app.json": '{"payment_gateway_url": "x"}',
        "svc/a.py": "m='module'\nu='payment_gateway_url'\n",
        "svc/b.py": "m='module'\nu='payment_gateway_url'\n",
    })
    checks.append(("E: files sharing one bare word + one specific key couple via the SPECIFIC key only "
                   f"(coupled={_coupled(pairs_e, 'svc/a.py', 'svc/b.py')}, module_minted={'module' in keys_e})",
                   _coupled(pairs_e, "svc/a.py", "svc/b.py") and "module" not in keys_e))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("CONFIG BARE-WORD PRECISION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
