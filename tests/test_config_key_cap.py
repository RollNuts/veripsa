#!/usr/bin/env python3
"""CONFIG-KEY-CAP GATE (HIGH SILENT-DROP fix — 2026-06-20).

Before this fix, JSON Schema definition files (*.schema.json, *.tmLanguage.json, any JSON file
with a top-level $schema key) and pnpm-lock.yaml / other lock files were processed by the config
pass and flooded _MAX_CONFIG_KEYS with structural vocabulary keys (properties.X.description,
properties.X.type, package-name keys) that carry ZERO cross-file application coupling.

The silent-miss: once the 20,000-key pool was exhausted by these floods, every real config file
processed afterward had its keys silently dropped -> two source files both reading the SAME
env-var / app setting produced NO reads_config edge -> the coupling was reported as CLEAR when
it should be WARN/pause (the silent-miss class of bug).

Measured on nx (60,539 total keys attempted -> 40,539 dropped, 67%; the drops came from schema
floods exhausting the pool before the real app-config files were processed; .verdaccio/config.yml
keys were silently dropped).

THE FIX:
  (1) _is_json_schema_file: skip *.schema.json / *-schema.json / schema.json (exact basename) /
      *.tmLanguage.json / any JSON file whose top-level dict has a $schema key.
  (2) _MAX_CONFIG_KEYS_PER_FILE: cap keys minted from any single config file (raised 500 -> 2000 after
      a real-repo measurement showed discourse's genuine config/site_settings.yml has 1061 specific
      keys; 500 truncated it) so no individual file can exhaust the global pool.
  (3) Expanded _CONFIG_SKIP_FILES: pnpm-lock.yaml / yarn.lock / cargo.lock / go.sum / etc.

RECALL: the skip is positive for recall overall -- real coupling keys that were being crowded
out by schema floods are now minted. A legitimate real config file stays within the per-file cap;
a *.schema.json file is never a config file two source files read via os.environ / config.get.

This gate proves the fix on SYNTHETIC fixtures (offline, no Postgres):
  A) A large *.tmLanguage.json / *.schema.json flood file -> its keys are NOT minted.
  B) A JSON file with a top-level $schema key -> skipped by content detection.
  C) A lock-file-style yaml -> skipped by _CONFIG_SKIP_FILES.
  D) Two real config files sharing an app key -> both minted + coupled via reads_config.
  E) The cap is NOT exhausted even when a flood file with thousands of keys is present.
  F) Per-file cap: a single (non-fixture) config file with more than the per-file cap of distinct
     specific keys is capped -> the global pool is spared (a fixture-shaped file is skipped earlier).
  G) Precision: schema file keys do NOT appear in the minted keyset.
  H) Recall: keys that previously would have been crowded out now couple their readers.
"""
from __future__ import annotations
import os
import sys
import tempfile
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import _cg_config as C  # noqa: E402


def _w(d, rel, body):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(body)


def _graph(files):
    """Build (config_key_names, reads_config_edges, all_nodes) via _cg_config._config_graph."""
    with tempfile.TemporaryDirectory() as d:
        cfgs, srcs = [], []
        for rel, body in files.items():
            _w(d, rel, body)
            fn = os.path.basename(rel)
            ext = C._config_ext(fn)
            if fn in C._CONFIG_SKIP_FILES:
                continue  # skip files are excluded from config_files by _iter_config_files
            if ext in C._CONFIG_EXTS:
                cfgs.append((os.path.join(d, rel), ext))
            else:
                srcs.append((os.path.join(d, rel), os.path.splitext(rel)[1]))
        nodes, edges = C._config_graph(d, cfgs, srcs)
    keys = {n["name"] for n in nodes if n["kind"] == "config_key"}
    reads = [(e["src"], e["dst"]) for e in edges if e["kind"] == "reads_config"]
    return keys, reads, nodes


def _couple(reads, a, b, key=None):
    """True if files a and b are coupled via reads_config (optionally via a specific key)."""
    a_keys = {dst for (src, dst) in reads if src == a}
    b_keys = {dst for (src, dst) in reads if src == b}
    shared = a_keys & b_keys
    if key is not None:
        return key in shared
    return bool(shared)


def main() -> int:
    checks = []

    # -------------------------------------------------------------------------
    # A) *.tmLanguage.json flood -> keys NOT minted
    # -------------------------------------------------------------------------
    # Build a large fake .tmLanguage.json with thousands of dotted keys
    lang_keys = {f"properties.rule{i}.name": "string" for i in range(3000)}
    lang_keys["$schema"] = "http://example.com"
    lang_body = json.dumps(lang_keys)

    keys_a, reads_a, nodes_a = _graph({
        "grammars/python.tmLanguage.json": lang_body,
        ".env": "REAL_APP_KEY=x\n",
        "src/app.py": "import os\nv=os.environ['REAL_APP_KEY']\n",
        "src/worker.py": "import os\nv=os.environ['REAL_APP_KEY']\n",
    })
    tl_keys = [k for k in keys_a if k.startswith("properties.")]
    checks.append(("A: tmLanguage.json schema keys are NOT minted "
                   f"(properties.* keys found={len(tl_keys)}, expected=0)",
                   len(tl_keys) == 0))
    checks.append(("A: real app key (REAL_APP_KEY) IS minted despite schema flood "
                   f"(minted={sorted(keys_a)})",
                   "REAL_APP_KEY" in keys_a))
    checks.append(("A: two real source files ARE coupled via the real env key "
                   f"(coupled={_couple(reads_a, 'src/app.py', 'src/worker.py')})",
                   _couple(reads_a, "src/app.py", "src/worker.py", "REAL_APP_KEY")))

    # -------------------------------------------------------------------------
    # B) *.schema.json flood -> keys NOT minted (suffix detection)
    # -------------------------------------------------------------------------
    schema_keys_b = {f"properties.option{i}.type": "string" for i in range(2000)}
    schema_body_b = json.dumps(schema_keys_b)

    keys_b, reads_b, _ = _graph({
        "configs/executor.schema.json": schema_body_b,
        ".env": "DATABASE_URL=x\nDATABASE_SECRET=y\n",
        "src/db.py": "import os\nDB=os.environ['DATABASE_URL']\nS=os.environ['DATABASE_SECRET']\n",
        "src/auth.py": "import os\nDB=os.environ['DATABASE_URL']\nS=os.environ['DATABASE_SECRET']\n",
    })
    schema_props_b = [k for k in keys_b if k.startswith("properties.")]
    checks.append(("B: *.schema.json suffix -> properties.* keys NOT minted "
                   f"(found={len(schema_props_b)}, expected=0)",
                   len(schema_props_b) == 0))
    checks.append(("B: real env keys ARE minted after schema flood is skipped "
                   f"(minted={sorted(k for k in keys_b if k in {'DATABASE_URL','DATABASE_SECRET'})})",
                   {"DATABASE_URL", "DATABASE_SECRET"} <= keys_b))
    checks.append(("B: two files ARE coupled via the real env keys "
                   f"(coupled={_couple(reads_b, 'src/db.py', 'src/auth.py')})",
                   _couple(reads_b, "src/db.py", "src/auth.py")))

    # -------------------------------------------------------------------------
    # C) JSON file with $schema key -> skipped by content detection
    # -------------------------------------------------------------------------
    dollar_schema_body = json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "properties": {f"option_{i}": {"type": "string"} for i in range(500)},
        "type": "object",
    })
    keys_c, reads_c, _ = _graph({
        "config/nx-schema.json": dollar_schema_body,
        "config/app.yaml": "STRIPE_KEY: abc\nDB_POOL_SIZE: 10\n",
        "src/pay.py": "stripe=config['STRIPE_KEY']\npool=config['DB_POOL_SIZE']\n",
        "src/store.py": "stripe=config['STRIPE_KEY']\npool=config['DB_POOL_SIZE']\n",
    })
    schema_dollar_props = [k for k in keys_c if k.startswith("properties.")]
    checks.append(("C: JSON file with $schema key -> properties.* NOT minted "
                   f"(found={len(schema_dollar_props)}, expected=0)",
                   len(schema_dollar_props) == 0))
    checks.append(("C: real yaml config keys ARE minted after $schema file is skipped "
                   f"(minted={sorted(k for k in keys_c if k in {'STRIPE_KEY','DB_POOL_SIZE'})})",
                   {"STRIPE_KEY", "DB_POOL_SIZE"} <= keys_c))
    checks.append(("C: two files ARE coupled via the real yaml keys "
                   f"(coupled={_couple(reads_c, 'src/pay.py', 'src/store.py')})",
                   _couple(reads_c, "src/pay.py", "src/store.py")))

    # -------------------------------------------------------------------------
    # D) schema.json exact basename -> skipped
    # -------------------------------------------------------------------------
    schema_exact_body = json.dumps({
        "type": "object",
        "title": "Executor Schema",
        "properties": {f"arg{i}": {"type": "string", "default": ""} for i in range(300)},
    })
    keys_d, reads_d, _ = _graph({
        "packages/myexec/schema.json": schema_exact_body,
        ".env": "REDIS_URL=redis://localhost\nAPI_SECRET=abc\n",
        "src/cache.py": "import os\nr=os.environ['REDIS_URL']\n",
        "src/api.py": "import os\nr=os.environ['REDIS_URL']\ns=os.environ['API_SECRET']\n",
    })
    schema_exact_props = [k for k in keys_d if k.startswith("properties.") or k.startswith("arg")]
    checks.append(("D: schema.json exact basename -> executor schema keys NOT minted "
                   f"(found={len(schema_exact_props)}, expected=0)",
                   len(schema_exact_props) == 0))
    checks.append(("D: real env keys ARE minted "
                   f"(REDIS_URL={'present' if 'REDIS_URL' in keys_d else 'MISSING'})",
                   "REDIS_URL" in keys_d))
    checks.append(("D: two files ARE coupled via REDIS_URL "
                   f"(coupled={_couple(reads_d, 'src/cache.py', 'src/api.py', 'REDIS_URL')})",
                   _couple(reads_d, "src/cache.py", "src/api.py", "REDIS_URL")))

    # -------------------------------------------------------------------------
    # E) Cap NOT exhausted by flood -> real keys survive
    #    Build a scenario where the schema flood would have filled the pool (20K keys)
    #    but with the fix the pool is well under the cap.
    # -------------------------------------------------------------------------
    flood_keys = {f"properties.key{i}.{sub}": "x" for i in range(5000) for sub in ("type", "description")}
    flood_body = json.dumps(flood_keys)

    keys_e, reads_e, _ = _graph({
        "grammar/large.tmLanguage.json": flood_body,  # 10K keys, should be skipped
        "config/settings.yaml": "WEBHOOK_SECRET: z\nFEATURE_FLAG_ENABLED: true\n",
        "src/hooks.py": "wh = config['WEBHOOK_SECRET']\n",
        "src/features.py": "ff = config['WEBHOOK_SECRET']\n",
    })
    checks.append(("E: cap NOT exhausted (flood file skipped) "
                   f"(config_key_count={len(keys_e)}, cap={C._MAX_CONFIG_KEYS})",
                   len(keys_e) < C._MAX_CONFIG_KEYS))
    checks.append(("E: real key (WEBHOOK_SECRET) IS minted even though flood was present "
                   f"(present={'WEBHOOK_SECRET' in keys_e})",
                   "WEBHOOK_SECRET" in keys_e))
    checks.append(("E: two files ARE coupled via WEBHOOK_SECRET after flood skip "
                   f"(coupled={_couple(reads_e, 'src/hooks.py', 'src/features.py', 'WEBHOOK_SECRET')})",
                   _couple(reads_e, "src/hooks.py", "src/features.py", "WEBHOOK_SECRET")))

    # -------------------------------------------------------------------------
    # F) Per-file cap: a single config file with MORE than _MAX_CONFIG_KEYS_PER_FILE distinct specific
    #    keys is capped, so it cannot exhaust the global pool and starve SUBSEQUENT real config files.
    # -------------------------------------------------------------------------
    # NOTE: this must be a NON-fixture, non-dump-SHAPED file, otherwise the data-dump skip
    # (_is_data_dump_file) removes it BEFORE the per-file cap can apply and the cap is never exercised.
    # A flat key/value JSON object at a normal config path (NOT under fixtures/, NOT a JSON array of
    # rows) is treated as a genuine — if very large — config file, so the per-file cap is the guard.
    # Use cap+200 distinct specific compound keys (env-var style, so each is a real config_key) at a
    # plain `config/` path; assert the file alone mints AT MOST the per-file cap.
    n_over = C._MAX_CONFIG_KEYS_PER_FILE + 200
    big_keys = {f"SERVICE_{i}_API_TOKEN": "x" for i in range(n_over)}   # all compound ALL-CAPS → specific
    big_body = json.dumps(big_keys)

    keys_f, reads_f, _ = _graph({
        "config/huge_settings.json": big_body,   # cap+200 specific keys → must be capped at the per-file cap
        ".env": "SENTRY_DSN=https://sentry.io/abc\nNEWRELIC_KEY=abc123\n",
        "src/monitor.py": "import os\ns=os.environ['SENTRY_DSN']\n",
        "src/perf.py": "import os\ns=os.environ['SENTRY_DSN']\n",
    })
    # The big config had cap+200 specific keys; the per-file cap limits its contribution. The real .env
    # keys (SENTRY_DSN) come from a DIFFERENT file, so they still mint (the pool is not exhausted).
    keys_from_big = {k for k in keys_f if k.startswith("SERVICE_")}
    checks.append(("F: per-file cap limits a single huge (non-fixture) config file's minted keys "
                   f"(minted_from_big={len(keys_from_big)}, per_file_cap={C._MAX_CONFIG_KEYS_PER_FILE})",
                   len(keys_from_big) <= C._MAX_CONFIG_KEYS_PER_FILE
                   and len(keys_from_big) >= C._MAX_CONFIG_KEYS_PER_FILE - 5))   # cap actually BITES (not skipped to 0)
    checks.append(("F: real env key (SENTRY_DSN) IS still minted after the per-file cap trims the huge file "
                   f"(present={'SENTRY_DSN' in keys_f})",
                   "SENTRY_DSN" in keys_f))
    checks.append(("F: two files ARE coupled via SENTRY_DSN "
                   f"(coupled={_couple(reads_f, 'src/monitor.py', 'src/perf.py', 'SENTRY_DSN')})",
                   _couple(reads_f, "src/monitor.py", "src/perf.py", "SENTRY_DSN")))

    # -------------------------------------------------------------------------
    # G) Lock file skip: pnpm-lock.yaml is in _CONFIG_SKIP_FILES -> not processed
    # -------------------------------------------------------------------------
    lock_body = "lockfileVersion: 9.0\npackages:\n  express:\n    version: 4.18.0\n"
    checks.append(("G: pnpm-lock.yaml is in _CONFIG_SKIP_FILES "
                   f"(present={'pnpm-lock.yaml' in C._CONFIG_SKIP_FILES})",
                   "pnpm-lock.yaml" in C._CONFIG_SKIP_FILES))
    checks.append(("G: yarn.lock is in _CONFIG_SKIP_FILES "
                   f"(present={'yarn.lock' in C._CONFIG_SKIP_FILES})",
                   "yarn.lock" in C._CONFIG_SKIP_FILES))
    checks.append(("G: cargo.lock is in _CONFIG_SKIP_FILES "
                   f"(present={'cargo.lock' in C._CONFIG_SKIP_FILES})",
                   "cargo.lock" in C._CONFIG_SKIP_FILES))

    # -------------------------------------------------------------------------
    # H) Recall: compound env var with a generic-leaf word is KEPT (regression guard)
    # -------------------------------------------------------------------------
    keys_h, reads_h, _ = _graph({
        ".env": "OAUTH_CLIENT_SECRET=x\nREDIS_HOST_URL=y\n",
        "src/auth.py": "import os\ns=os.environ['OAUTH_CLIENT_SECRET']\n",
        "src/cache.py": "import os\ns=os.environ['OAUTH_CLIENT_SECRET']\n",
    })
    checks.append(("H: compound env var with generic leaf (OAUTH_CLIENT_SECRET) IS minted "
                   f"(present={'OAUTH_CLIENT_SECRET' in keys_h})",
                   "OAUTH_CLIENT_SECRET" in keys_h))
    checks.append(("H: two files reading compound env var ARE coupled (recall not harmed) "
                   f"(coupled={_couple(reads_h, 'src/auth.py', 'src/cache.py', 'OAUTH_CLIENT_SECRET')})",
                   _couple(reads_h, "src/auth.py", "src/cache.py", "OAUTH_CLIENT_SECRET")))

    # Print results
    ok = True
    for name, cond in checks:
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name}")
        ok = ok and bool(cond)

    print()
    print("CONFIG-KEY-CAP GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
