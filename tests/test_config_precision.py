#!/usr/bin/env python3
"""CONFIG-COUPLING PRECISION gate (audit 2026-06-19).

Veripsa's CONFIG graph couples files that read the SAME config key — a real coupling: two files
both reading `STRIPE_WEBHOOK_SECRET` ARE entangled. But UBIQUITOUS keys (`timeout`, `log.level`,
`max_size`, `config.default`, `status`) appear in nearly every config and file, so two UNRELATED
files both reading `timeout` are NOT really coupled. This mirrors the proven ubiquitous-CALL-name
false-coupling fix (test_false_coupling_precision.py).

MEASURED (tests/measure_config_precision.py + measure_config_recall_cost.py on real repos
airflow / netbox / redash): ubiquitous-key-ONLY config couplings co-change ~3x LESS than
specific-key couplings (mean lift 2.36 vs 7.79 on airflow; co-change 1% vs 5%), and demoting all
~99k such pairs across the three repos cost 0 real recall (the only strong-co-change ubiq-only
pairs were themselves spurious). So `_cg_config._specific_key` no longer mints a config_key node
for a FULLY-GENERIC key (every token ubiquitous) → no `reads_config` edge → no spurious pair.

This gate proves the extraction is RECALL-SAFE and PRECISE on constructed synthetic configs+code,
deterministically and with no DB (it asserts directly on the config graph `_cg_config` emits):

  A) PRECISION: two unrelated files both reading ONLY ubiquitous keys (`timeout`, `log.level`,
     `max_size`) are NOT config-coupled (no shared config_key → no pair).
  B) RECALL — specific key kept: two files both reading `STRIPE_WEBHOOK_SECRET` (and `db.pool_size`)
     ARE config-coupled (the specific config_key is minted; both files get a reads_config edge).
  C) RECALL — specific compound whose LEAF word is generic: `webhook_secret` / `redis_url` /
     `oauth_client_id` (generic leaf `secret`/`url`/`id`, distinguishing prefix) MUST be KEPT.
  D) MIXED file: a file reading BOTH a ubiquitous key and a specific key still couples via the
     SPECIFIC key (the demotion drops only the ubiquitous edge, never the file's real coupling).
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
    """Build (config_keys minted, reads_config pairs) for a synthetic file set via _cg_config."""
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
    # invert reads_config edges to file pairs sharing a key (the coupling the engine forms)
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
    return keys, pairs, by_key


def _coupled(pairs, a, b):
    return frozenset((a, b)) in pairs


def main() -> int:
    checks = []

    # A) PRECISION — ubiquitous-key-ONLY readers must NOT couple.
    keys_a, pairs_a, _ = _graph({
        "config/app.yaml": "timeout: 30\nmax_size: 100\nlog:\n  level: info\n",
        "svc/alpha.py":  "TIMEOUT='timeout'\nMAX='max_size'\nLVL='log.level'\n",
        "svc/beta.py":   "t='timeout'\nm='max_size'\nl='log.level'\n",
    })
    checks.append(("A: ubiquitous-only keys (timeout/max_size/log.level) are NOT minted as config_key "
                   f"(minted={sorted(keys_a)})",
                   not ({"timeout", "max_size", "log.level", "level"} & keys_a)))
    checks.append(("A: two files reading ONLY ubiquitous keys are NOT config-coupled (no spurious pair) "
                   f"(pairs={[sorted(p) for p in pairs_a]})",
                   not _coupled(pairs_a, "svc/alpha.py", "svc/beta.py")))

    # B) RECALL — a SPECIFIC key couples its readers (must be kept).
    # (JSON config so the dotted `database.pool_size` form is minted too — the yaml line-regex would
    # only mint the bare leaf `pool_size`; JSON exercises both the dotted AND leaf specific keys.)
    keys_b, pairs_b, _ = _graph({
        "config/app.json": '{"stripe_webhook_secret": "x", "database": {"pool_size": 10}}',
        "svc/pay.py":   "S='stripe_webhook_secret'\nP='database.pool_size'\n",
        "svc/store.py": "s='stripe_webhook_secret'\np='database.pool_size'\n",
    })
    checks.append(("B: specific keys (stripe_webhook_secret / database.pool_size) ARE minted "
                   f"(minted={sorted(keys_b)})",
                   "stripe_webhook_secret" in keys_b and "database.pool_size" in keys_b))
    checks.append(("B: two files reading a SPECIFIC key ARE config-coupled (real coupling kept) "
                   f"(pairs={[sorted(p) for p in pairs_b]})",
                   _coupled(pairs_b, "svc/pay.py", "svc/store.py")))

    # C) RECALL — specific COMPOUND whose leaf word is generic must be KEPT (recall not over-dropped).
    keys_c, pairs_c, _ = _graph({
        ".env": "WEBHOOK_SECRET=a\nREDIS_URL=b\nOAUTH_CLIENT_ID=c\n",
        "svc/hook.py": "import os\nw=os.environ['WEBHOOK_SECRET']\nr=os.environ['REDIS_URL']\no=os.environ['OAUTH_CLIENT_ID']\n",
        "svc/sess.py": "import os\nw=os.environ['WEBHOOK_SECRET']\nr=os.environ['REDIS_URL']\no=os.environ['OAUTH_CLIENT_ID']\n",
    })
    checks.append(("C: specific compounds with a generic LEAF (WEBHOOK_SECRET/REDIS_URL/OAUTH_CLIENT_ID) are KEPT "
                   f"(minted={sorted(keys_c)})",
                   {"WEBHOOK_SECRET", "REDIS_URL", "OAUTH_CLIENT_ID"} <= keys_c))
    checks.append(("C: readers of a generic-leaf compound ARE coupled (recall preserved) "
                   f"(pairs={[sorted(p) for p in pairs_c]})",
                   _coupled(pairs_c, "svc/hook.py", "svc/sess.py")))

    # D) MIXED — a file reading BOTH a ubiquitous and a specific key still couples via the SPECIFIC one.
    keys_d, pairs_d, by_key_d = _graph({
        "config/app.yaml": "timeout: 5\nstripe_webhook_secret: z\n",
        "svc/a.py": "t='timeout'\ns='stripe_webhook_secret'\n",
        "svc/b.py": "t='timeout'\ns='stripe_webhook_secret'\n",
    })
    # coupled (via the specific key), and NOT via the ubiquitous one (which was never minted)
    checks.append(("D: files sharing one ubiquitous + one specific key ARE coupled via the SPECIFIC key only "
                   f"(coupled={_coupled(pairs_d, 'svc/a.py', 'svc/b.py')}, "
                   f"timeout_minted={'timeout' in keys_d})",
                   _coupled(pairs_d, "svc/a.py", "svc/b.py") and "timeout" not in keys_d))
    checks.append(("D: the coupling is carried ONLY by the specific config_key (no ubiquitous via_hub) "
                   f"(keys_with_2plus_readers={[k for k,fs in by_key_d.items() if len(fs)>=2]})",
                   [k for k, fs in by_key_d.items() if len(fs) >= 2] == ["stripe_webhook_secret"]))

    # E) DATA-DUMP / FIXTURE / SEED / LOCALE SKIP (audit 2026-06-20: 65-91% of config couples on real
    #    Django/Rails repos were fixture FIELD-NAME false couples). A serialized-row dump's "keys" are MODEL
    #    FIELD NAMES (`shipping_address`, `content_object`, `price_excl_tax`) — compound, so they slip the
    #    distinctive/ubiquitous guards, then false-couple every unrelated file that mentions that field name.
    #    config shares the SAME contested->warn->material-pause path as schema, so a false config couple causes
    #    a false PAUSE. `_cg_config._is_data_dump_file` skips minting from such files (mirrors the JSON-schema
    #    skip). This must lose 0 REAL config coupling (recall) while emitting 0 fixture-field couples (precision).

    # E1) Django fixture (in fixtures/ AND the {"model","fields"} shape) + a saleor-style populatedb_data.json
    #     seed dump: NO config_key minted from either, so two unrelated files mentioning `shipping_address` /
    #     `description_plaintext` (model field names) are NOT config-coupled — no false pause.
    keys_e, pairs_e, by_key_e = _graph({
        "apps/orders/fixtures/orders.json":
            '[{"model": "orders.order", "fields": {"shipping_address": "x", "price_excl_tax": 1}}]',
        "static/populatedb_data.json":
            '[{"model": "product.product", "fields": {"description_plaintext": "d", "seo_description": "s"}}]',
        # two UNRELATED source files that merely mention the fixture-minted field names as strings
        "svc/shipping.py": "addr='shipping_address'\nseo='seo_description'\n",
        "svc/catalog.py":  "addr='shipping_address'\nseo='seo_description'\n",
    })
    checks.append(("E1: NO config_key minted from a Django fixture or a populatedb_data.json seed dump "
                   f"(minted={sorted(keys_e)})",
                   not ({"shipping_address", "price_excl_tax", "description_plaintext",
                         "seo_description", "model", "fields"} & keys_e)))
    checks.append(("E1: two unrelated files mentioning fixture FIELD NAMES are NOT config-coupled (no false pause) "
                   f"(pairs={[sorted(p) for p in pairs_e]})",
                   not _coupled(pairs_e, "svc/shipping.py", "svc/catalog.py")
                   and not [k for k, fs in by_key_e.items() if len(fs) >= 2]))

    # E2) RECALL UNTOUCHED — a REAL settings.yaml AND a real app.json next to the dumps still mint their keys
    #     and couple their readers. The skip is mint-SIDE on dump files only; genuine app-config is preserved.
    keys_e2, pairs_e2, _ = _graph({
        "config/settings.yaml": "stripe_webhook_secret: z\npayment_gateway_url: u\n",
        "config/app.json": '{"database": {"pool_size": 10}}',
        # a fixture sitting RIGHT NEXT to real config must NOT poison the pass (no global-pool exhaust either)
        "db/fixtures/seed.json": '[{"model": "x", "fields": {"shipping_address": 1}}]',
        "svc/pay.py":   "s='stripe_webhook_secret'\nu='payment_gateway_url'\np='database.pool_size'\n",
        "svc/store.py": "s='stripe_webhook_secret'\nu='payment_gateway_url'\np='database.pool_size'\n",
    })
    checks.append(("E2: REAL app-config keys (stripe_webhook_secret / payment_gateway_url / database.pool_size) "
                   f"ARE still minted alongside a fixture (recall preserved) (minted={sorted(keys_e2)})",
                   {"stripe_webhook_secret", "payment_gateway_url", "database.pool_size"} <= keys_e2))
    checks.append(("E2: readers of the REAL config keys ARE still coupled; the fixture field (shipping_address) is NOT minted "
                   f"(coupled={_coupled(pairs_e2, 'svc/pay.py', 'svc/store.py')}) "
                   f"(shipping_address_minted={'shipping_address' in keys_e2})",
                   _coupled(pairs_e2, "svc/pay.py", "svc/store.py") and "shipping_address" not in keys_e2))

    # E3) PATH-only signals: a fixtures/ dir file (NON-array JSON object, so ONLY the dir segment catches it)
    #     is skipped, while an identically-shaped NON-fixture config IS kept (the dir segment is the only diff).
    keys_e3, pairs_e3, _ = _graph({
        # same object SHAPE in both; only the directory differs → proves the skip keys on the path, recall-safely
        "tests/fixtures/account.json": '{"billing_address_line": "x", "tax_jurisdiction_id": "y"}',
        "config/billing.json":         '{"billing_address_line": "a", "tax_jurisdiction_id": "b"}',
        "svc/m.py": "a='billing_address_line'\nj='tax_jurisdiction_id'\n",
        "svc/n.py": "a='billing_address_line'\nj='tax_jurisdiction_id'\n",
    })
    # the keys exist in the graph (minted from config/billing.json), and the readers ARE coupled via that real
    # config file — the fixtures/ copy minted nothing, so it contributed no extra (false) reader to the key set.
    checks.append(("E3: a fixtures/ dir config is skipped while an identical non-fixture config is KEPT "
                   f"(minted={sorted(keys_e3)})",
                   {"billing_address_line", "tax_jurisdiction_id"} <= keys_e3))
    checks.append(("E3: readers couple via the REAL (non-fixture) config file (recall), with the fixtures/ copy contributing nothing "
                   f"(pairs={[sorted(p) for p in pairs_e3]})",
                   _coupled(pairs_e3, "svc/m.py", "svc/n.py")))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("CONFIG PRECISION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
