"""Gate: CONFIG-coupling RECALL preservation + the data-dump skip is correctly scoped (precision).

PR #339 added `_cg_config._is_data_dump_file`, which SKIPS data-dump / fixture / seed / locale
files at config MINT time (their "keys" are model FIELD NAMES or i18n message ids, not a shared
app-config resource — audit: 65-91% of config couples on real Django/Rails repos were these
fixture-field-name FALSE couples). It also raised the per-file distinct-key cap 500 -> 2000.

This gate is the recall+precision REGRESSION LOCK for that skip. An OVER-SKIP audit (measured on
crafted repos, mirroring build_graph's wiring _iter_config_files + _iter_source_files -> _config_graph)
found NO real over-skip dropping genuine coupling: every skip signal is either load-bearing for the
#339 precision gain or loses zero recall. The detail (with the exact numbers) is in the gate file. So
NO change was made to _cg_config.py; this gate freezes the measured behaviour so a future "rescue a
real config out of a dump path/name" narrowing cannot silently re-admit the field-name flood #339
removed (the measured tradeoff: gating the basename signal on array-shape rescued 1 real couple but
re-admitted a keyed-object dump's `shipping_address` false couple).

Properties proven (offline, no Postgres; content-free — only key NAMES + paths are read):
  (R1) RECALL — a real key/value OBJECT config (the universal real-config shape) couples its two
       readers via a SPECIFIC key. This is the genuine coupling the whole detector exists to find.
  (R2) RECALL UNDER PER-FILE CAP — a 2100-key GENUINE config (> the 2000 per-file cap) still couples
       on an early specific key (the cap truncates a tail, never the whole file; #339 raised it from
       500 specifically because discourse's site_settings.yml has 1061 real keys).
  (P1) PRECISION — a Django fixture (`fixtures/` dir, array-of-`{model,fields}` rows) mints NO
       config_key, so two files mentioning the same model FIELD NAME do NOT false-couple.
  (P2) PRECISION — an UN-NAMED row dump caught only by CONTENT SHAPE (top-level array of objects,
       not under any fixture dir, not `*_data.json`) is still skipped: removing the content probe
       re-couples its field name (measured) — the probe earns its keep.
  (P3) PRECISION — a locale tree (`locales/` dir) mints NO config_key (translation ids are not a
       config resource), so two files mentioning the same message id do NOT couple.
  (REC-SCOPE) The skip fires ONLY on dump shapes/paths — a real config OBJECT ('{') is NEVER caught
       by the content probe (a genuine array config exposes only generic field NAMES as keys, and the
       real identifiers are VALUES the content-free extractor never reads), so the object-config path
       keeps full recall regardless of the probe.
  (NC) NEVER-CRASH — _is_data_dump_file degrades any malformed / oversized / binary input to a bool.

Prints CONFIG RECALL GATE: PASS on success, ... FAIL on any failure.
"""
import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import _cg_config as C
import code_graph_extract as X


def _write(root, path, body):
    fp = os.path.join(root, path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as fh:
        fh.write(body)


def _couples(files):
    """Build the config graph for a crafted repo exactly as build_graph wires it, and return the
    set of (a, b) file pairs that read a SHARED config key (a >=2-reader key == a coupling)."""
    root = tempfile.mkdtemp(prefix="config_recall_")
    try:
        for p, b in files.items():
            _write(root, p, b)
        cfg_files = list(X._iter_config_files(root))     # SAME guarded walk the product uses
        src_files = list(X._iter_source_files(root))
        _nodes, edges = C._config_graph(root, cfg_files, src_files)
        by_key = {}
        for e in edges:
            by_key.setdefault(e["dst"], set()).add(e["src"].replace(os.sep, "/"))
        couples = set()
        for _k, srcs in by_key.items():
            if len(srcs) >= 2:
                ss = sorted(srcs)
                for i in range(len(ss)):
                    for j in range(i + 1, len(ss)):
                        couples.add((ss[i], ss[j]))
        return couples
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    failures = []

    # (R1) RECALL: a real key/value OBJECT config couples its two readers via a SPECIFIC key.
    r1 = _couples({
        "config/app.json": '{"stripe_webhook_secret": "x", "database_pool_size": 5}',
        "a.py": "v = cfg['stripe_webhook_secret']\n",
        "b.py": "v = cfg['stripe_webhook_secret']\n",
    })
    if r1 != {("a.py", "b.py")}:
        print(f"FAIL [R1 recall]: real config must couple its 2 readers; got {sorted(r1)!r}")
        failures.append("real-config-recall")

    # (R2) RECALL UNDER PER-FILE CAP: a 2100-key genuine config still couples on an early key.
    keys = "".join(f"app_setting_{i}: v{i}\n" for i in range(2100))
    r2 = _couples({
        "config/site_settings.yml": keys,
        "a.py": "v = cfg['app_setting_3']\n",
        "b.py": "v = cfg['app_setting_3']\n",
    })
    if r2 != {("a.py", "b.py")}:
        print(f"FAIL [R2 cap recall]: a >2000-key genuine config must still couple on an early "
              f"specific key (per-file cap truncates only a tail); got {sorted(r2)!r}")
        failures.append("percap-recall")

    # (P1) PRECISION: a Django fixture under fixtures/ mints no key -> no field-name false couple.
    p1 = _couples({
        "app/fixtures/orders.json":
            '[{"model":"shop.order","fields":{"shipping_address":"x","price_excl_tax":1}},'
            '{"model":"shop.order","fields":{"shipping_address":"y","price_excl_tax":2}}]',
        "a.py": "v = row['shipping_address']\n",
        "b.py": "v = row['shipping_address']\n",
    })
    if p1 != set():
        print(f"FAIL [P1 precision]: a fixture's field name must NOT false-couple (#339); got {sorted(p1)!r}")
        failures.append("fixture-false-couple")

    # (P2) PRECISION: an UN-NAMED row dump caught only by content-shape is still skipped.
    p2 = _couples({
        "exported_rows.json":   # no fixture dir, no *_data.json — only the content probe catches it
            '[{"customer_billing_email":"a@x","order_tracking_number":"T1"},'
            '{"customer_billing_email":"b@x","order_tracking_number":"T2"}]',
        "a.py": "v = row['customer_billing_email']\n",
        "b.py": "v = row['customer_billing_email']\n",
    })
    if p2 != set():
        print(f"FAIL [P2 content-shape]: an un-named array-of-objects row dump must stay skipped "
              f"(content probe earns its keep); got {sorted(p2)!r}")
        failures.append("contentshape-false-couple")

    # (P3) PRECISION: a locale tree mints no key -> shared message id does NOT couple.
    p3 = _couples({
        "config/locales/en.json": '{"auth_login_title": "x", "errors_not_found": "y"}',
        "a.py": "t = t('auth_login_title')\n",
        "b.py": "t = t('auth_login_title')\n",
    })
    if p3 != set():
        print(f"FAIL [P3 locale precision]: a locale message id must NOT couple; got {sorted(p3)!r}")
        failures.append("locale-false-couple")

    # (REC-SCOPE) The content probe fires ONLY on dump SHAPES: a real config OBJECT is never caught,
    # so the object-config path keeps full recall regardless of the probe. Asserts the predicate
    # directly (a '{' key/value config is not a dump; a '[' array-of-objects IS).
    if C._is_data_dump_file("config/app.json", '{"webhook_secret":"x"}'):
        print("FAIL [REC-SCOPE]: a real OBJECT config ('{') must NOT be classified as a dump")
        failures.append("object-config-overskip")
    if not C._is_data_dump_file("rows.json", '[{"a":1},{"a":2}]'):
        print("FAIL [REC-SCOPE]: an array-of-objects row dump ('[') must be classified as a dump")
        failures.append("array-dump-underskip")

    # (NC) NEVER-CRASH: malformed / oversized / binary inputs degrade to a bool, never raise.
    for path, text in [
        ("x.json", "[{not valid json"), ("y.json", "["), ("z.json", "[]"),
        ("a.json", "[1, 2, 3]"), ("b.json", "\x00\x01binary-ish"),
        ("c.json", "[" + "{}," * 50000 + "{}]"), ("d.yml", "k:\n" * 4000), ("e.po", ""),
    ]:
        try:
            r = C._is_data_dump_file(path, text)
            if not isinstance(r, bool):
                print(f"FAIL [NC]: _is_data_dump_file({path!r}) returned non-bool {r!r}")
                failures.append("nevercrash-nonbool")
        except Exception as ex:
            print(f"FAIL [NC]: _is_data_dump_file({path!r}) raised {ex!r}")
            failures.append("nevercrash-raised")

    if failures:
        print(f"CONFIG RECALL GATE: FAIL (failures: {failures})")
        sys.exit(1)
    print("CONFIG RECALL GATE: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
