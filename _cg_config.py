"""Config graph extraction for the code-graph extractor.

Owns: _CONFIG_EXTS, _CONFIG_SKIP_FILES, _CONFIG_STOPKEYS, _config_keys,
_specific_key, _config_graph.

CONFIG GRAPH (the other coupling code-only tools miss): a config KEY is a shared
resource. Two files that read the same key (e.g. `db.pool_size`) are coupled with NO
code edge between them. Two passes: (1) parse config files → the set of SPECIFIC keys
(+ config_file/config_key nodes); (2) scan code for string literals / env-vars matching
a KNOWN key → `reads_config` edges (the key-set gives precision — only real config keys
match). Static + regex, no deps, content-free (key names only, never values).
"""
import json
import os
import re

from _cg_io import _mark_incomplete, _read_capped
from _cg_languages import _GRAMMAR_BY_EXT

_CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".env", ".ini", ".cfg", ".dockerfile"}
_CONFIG_SKIP_FILES = {
    # Machine-generated dependency lock files: their keys are third-party package names /
    # checksums / resolved URLs — never app config keys two source files share. Including them
    # floods the config pass with thousands of package names that couple every file importing a
    # common dependency (e.g. every file that uses "express" falsely couples to every other).
    "package-lock.json",   # npm
    "yarn.lock",           # yarn (also binary format — parser produces garbage keys anyway)
    "pnpm-lock.yaml",      # pnpm
    "composer.lock",       # php/composer
    "poetry.lock",         # python/poetry
    "cargo.lock",          # rust/cargo
    "gemfile.lock",        # ruby/bundler
    "go.sum",              # go modules (hashes, not config)
    "pipfile.lock",        # python/pipenv
    "bun.lockb",           # bun (binary)
    "packages.lock.json",  # .NET NuGet
}
_CONFIG_STOPKEYS = frozenset({
    "name", "type", "id", "key", "value", "path", "url", "host", "port", "user",
    "password", "token", "title", "description", "version", "enabled", "default",
    "true", "false", "null",
    "data", "string", "number", "object", "array", "items", "properties", "required",
    "format",
    # infra / structural keys (docker-compose · CI · MCP manifests) — present in configs
    # but NOT app keys whose change couples code; matching them in code is noise (e.g.
    # the word "command").
    "command", "args", "env", "image", "ports", "volumes", "build", "services",
    "depends", "environment", "networks", "restart", "stage", "steps", "run", "uses",
    "with", "jobs", "needs", "script", "command_",
})

# Dependency-declaration sections of package/build manifests: their CHILD keys are third-party
# PACKAGE names, not app config keys. Two files merely importing the SAME dependency are NOT
# coupled by it — minting those package names as config_key floods reads_config with spurious
# pairs (real-repo finding: one `supertest` dep produced 3224 bogus `express` pairs because every
# file requiring express matched the minted "express" key). Skip these sections entirely (same
# spirit as _CONFIG_STOPKEYS: manifest structure ≠ app config). Matched on a section's last
# dotted segment, lowercased — so `[tool.poetry.dependencies]` and a JSON `"dependencies"` block
# both match. App config keys (e.g. `db.pool_size` in a prod.yaml) are untouched.
_DEP_SECTIONS = frozenset({
    "dependencies", "devdependencies", "peerdependencies", "optionaldependencies",   # package.json (npm)
    "bundleddependencies", "bundledependencies",
    "require", "require-dev",                                                         # composer.json (php)
    "dev-dependencies", "build-dependencies",                                         # Cargo.toml (rust)
    "packages", "dev-packages",                                                       # Pipfile (python)
})

# Bound on DISTINCT config_key nodes minted from one repo's config pass (the config analogue of
# _cg_schema._MAX_TABLES / build_graph's _PER_FILE_SYMBOL_CAP). Without it the config pass had NO output
# ceiling: a crafted/large config (a JSON object with N distinct deeply-dotted keys, or a yaml/env with N
# distinct ALL-CAPS names) would mint N config_key nodes + the keyset that seeds N `reads_config` edges with
# no limit — an unbounded-output blowup of the whole tenant's graph (memory, the JSON ingest payload, and the
# O(pairs) shared-resource adjacency downstream). Real configs run dozens → low hundreds of SPECIFIC keys, so
# this ceiling never clips a genuine repo; it only truncates a flood. Degrade gracefully: keep the first
# _MAX_CONFIG_KEYS distinct keys (deterministic, walk-order), then stop minting + stop adding to the keyset
# (so pass 2 also never mints edges to an un-minted key — neither orphan nodes nor orphan edges).
_MAX_CONFIG_KEYS = 20_000

# Per-file cap on DISTINCT specific keys minted from a single config file.  Its job is to stop a single
# file from exhausting the global _MAX_CONFIG_KEYS pool (and silently dropping every SUBSEQUENT real
# config file's keys — the HIGH SILENT-DROP class: two files reading the same real env-var reported as
# UNRELATED because that env-var was never minted).  Applied AFTER _specific_key / _is_ubiquitous_config_key,
# so only coupling-bearing keys count toward the limit.
#
# RAISED 500 -> 2000 (measured 2026-06-20 on discourse).  The old comment claimed "any key minted from a
# real config file is well within 500; files exceeding 500 specific keys are data dumps" — that premise is
# MEASURABLY FALSE for a large but GENUINE app-config file: discourse's `config/site_settings.yml` declares
# 1061 SPECIFIC keys (every real Discourse site setting code reads).  At 500 it was truncated, so ~561 real
# settings were never minted — and the only reason their couplings survived was that the i18n locale files
# (`config/locales/*.yml`) REDUNDANTLY re-minted those names as translation keys.  The data-dump skip
# (_is_data_dump_file) correctly removes those locale files (a translation tree is not a config resource code
# reads), which EXPOSED the truncation: measured recall-loss of 151 real config couples (44 distinct keys,
# 43/44 cap victims of site_settings.yml).  Raising the cap to 2000 restored them (recall-loss 151 -> 1) with
# ZERO change to false-couple removal (the ~2880 fixture-field-name false couples stay gone).  This is safe
# now precisely BECAUSE the real flood sources (data dumps / fixtures / seeds / locales) are skipped at mint
# by name/shape — so a file still reaching 2000 specific keys is a genuinely huge config, not a dump.  The
# global _MAX_CONFIG_KEYS (20_000) is the real overflow guard; this per-file cap need only exceed the largest
# legitimate single config file, which 2000 does with headroom.
_MAX_CONFIG_KEYS_PER_FILE = 2000

# Suffixes that identify files whose JSON content is a SCHEMA DEFINITION (TextMate grammar /
# JSON Schema / OpenAPI), not application config.  These files mint thousands of keys like
# `properties.X.description` / `properties.X.type` — structural vocabulary of the schema format
# itself — which appear in no application source file via os.environ / process.env / config.get.
# They carry ZERO cross-file application coupling and exhausting the global key pool with them
# causes real app-coupling keys (env vars, app settings) processed later to be silently dropped.
# Suffix-matched on the lowercased filename basename so `foo.schema.json` and
# `Foo.tmLanguage.json` are both caught without touching the walk logic.
_CONFIG_SCHEMA_SUFFIXES = (
    ".schema.json",      # JSON Schema definition files (e.g. nx-schema.json, tsconfig.schema.json)
    "-schema.json",      # same pattern with hyphen separator (e.g. angular-schema.json)
    "_schema.json",      # same pattern with underscore separator
    ".tmlanguage.json",  # TextMate grammar definitions — pure syntax metadata, no app config
)

# Exact basenames that are definitionally schema/non-app-config files regardless of content.
# `schema.json` is the most common: used throughout nx, angular, and many tool ecosystems as
# the JSON Schema definition file for an executor / generator / command — never an app config
# file that source code reads via os.environ / config.get / process.env.
_CONFIG_SCHEMA_NAMES = frozenset({
    "schema.json",     # executor/generator schema (nx, angular, vitest, vite, …)
    "swagger.json",    # OpenAPI / Swagger spec
    "openapi.json",    # OpenAPI spec
})


def _is_json_schema_file(fn, text):
    """True when `fn` (basename) / `text` (full file contents) identify a JSON SCHEMA DEFINITION
    file — i.e. a file whose keys are JSON-Schema structural vocabulary, NOT application config
    keys two source files share.

    Three complementary signals (any is sufficient):
      (1) FILENAME SUFFIX in _CONFIG_SCHEMA_SUFFIXES — a *.schema.json, *-schema.json, or
          *.tmLanguage.json is definitionally a schema, regardless of content.
      (2) EXACT BASENAME in _CONFIG_SCHEMA_NAMES — `schema.json` (nx/angular executor schemas),
          `swagger.json`, `openapi.json` — never carry real app-coupling config keys.
      (3) CONTENT MARKER — a JSON object whose top-level dict contains a `$schema` key.  The
          JSON Schema specification (draft-04 onward) uses a `$schema` URI to identify the
          meta-schema; its presence is a near-certain indicator this file IS a schema definition.

    Only called for files whose cfg_ext is `.json`; callers MUST gate on that before calling.
    Recall-safe: only suppresses files that are definitionally non-coupling.  Real config files
    (app.json, settings.json, package.json, tsconfig.json) either lack a `$schema` key or are
    already handled by _CONFIG_SKIP_FILES / _DEP_SECTIONS.  Content-free: reads only the top-level
    key names from the JSON object, never values."""
    fn_lower = fn.lower()
    # (1) Suffix patterns
    for sfx in _CONFIG_SCHEMA_SUFFIXES:
        if fn_lower.endswith(sfx):
            return True
    # (2) Exact known basenames
    if fn_lower in _CONFIG_SCHEMA_NAMES:
        return True
    # (3) Content detection: $schema key at the top level of the JSON object
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "$schema" in obj:
            return True
    except Exception:
        pass
    return False


# Path SEGMENTS that declare a file's whole subtree is FIXTURE / SEED / RECORDED data — not a shared
# application-config resource. A file under fixtures/ / seeds/ / factories/ / cassettes/ is a CANNED DATA
# RECORD (serialized model rows, factory output, recorded HTTP), so its "keys" are MODEL FIELD NAMES
# (`shipping_address`, `content_object`, `price_excl_tax`) — compound enough to slip past the
# distinctive/ubiquitous guards, then false-couple every unrelated file that happens to mention that
# field name. Matched case-insensitively against each path segment (mirrors _cg_schema._is_test_path),
# at ANY depth (e.g. `apps/orders/fixtures/…`, `spec/cassettes/…`).
_DATA_DUMP_SEGS = frozenset({
    "fixtures", "fixture", "seed", "seeds", "factories", "cassettes", "vcr_cassettes",
})

# i18n / locale TRANSLATION trees: a `locales/` / `locale/` dir or a `.po` file. Translation keys
# (`auth.login.title`, `errors.not_found`) are UI message ids a renderer interpolates — NOT a config
# resource source code "reads" to make two files structurally coupled. They are compound (dotted) so
# they slip the distinctive guard, then couple every file mentioning the same message id. (A `.po`
# gettext catalog is matched by extension; the directory forms are matched as segments.)
_LOCALE_SEGS = frozenset({"locales", "locale"})

# Bound on bytes inspected for the JSON-shape content probe. A fixture/seed dump can be MEGABYTES; we
# only need the OPENING token (`[` then the first object's `model`/`fields` shape) to decide. Cap the
# slice we hand to json.loads on the content path so a huge dump is never fully parsed just to classify
# it (the path/name signals already catch most; content is the recall-safety net for un-named dumps).
_DUMP_PROBE_BYTES = 65_536


def _is_data_dump_file(path, text):
    """True when `path` (a repo-relative-or-absolute path) / `text` (file contents) identify a
    DATA-DUMP / FIXTURE / SEED / LOCALE file — a CANNED DATA RECORD whose "keys" are model FIELD NAMES
    or UI message ids, NOT a shared application-config resource two source files read.

    Mirrors `_is_json_schema_file` (skip at MINT time, same spirit as `_cg_schema._is_test_path`):
    such a file's keys (`shipping_address`, `content_object`, `price_excl_tax`, `description_plaintext`)
    are compound, so they pass the distinctive/ubiquitous guards, then FALSE-COUPLE every unrelated file
    that merely mentions the same field name (real-repo audit: 65-91% of config couples were these). A
    fixture/dump is by definition NOT a shared app-config resource, so skipping its mint loses 0 real
    coupling (recall-safe).

    Any of these signals is sufficient:
      (1) PATH SEGMENT in _DATA_DUMP_SEGS (fixtures/ seed/ factories/ cassettes/) — at any depth;
      (2) LOCALE: a path segment in _LOCALE_SEGS (locales/ locale/) or a `.po` gettext catalog;
      (3) BASENAME pattern — `*_data.json`, `populatedb*`, `dump*`, `*.fixture.json`/`*.fixtures.json`;
      (4) CONTENT shape (JSON only, bounded peek): the top level is a JSON ARRAY of objects (a row
          dump, not a key/value config), OR a Django/Rails fixture shape
          (`[{"model": ..., "fields": {...}}, ...]`).

    Bounded + never-crash: the content probe reads at most _DUMP_PROBE_BYTES and treats any parse error
    as "not a dump" (fall through — a malformed file is handled by the existing guards downstream, never
    skipped on a guess). Content-free: inspects only top-level structure / key NAMES, never values."""
    p = str(path).replace("\\", "/").lower()
    segs = p.split("/")
    fn = segs[-1] if segs else p
    # (1) fixture / seed / factory / cassette directory segment (any depth). Check directory
    #     segments only (exclude the basename) so a file literally named `seed.py` is not swept —
    #     it is the DIRECTORY that declares the subtree is canned data.
    if any(seg in _DATA_DUMP_SEGS for seg in segs[:-1]):
        return True
    # (2) i18n locale tree (directory) or a .po gettext catalog (by extension).
    if any(seg in _LOCALE_SEGS for seg in segs[:-1]):
        return True
    if fn.endswith(".po"):
        return True
    # (3) basename patterns for seed dumps / fixtures emitted at the repo root or anywhere.
    if (fn.endswith("_data.json")
            or fn.startswith("populatedb")
            or fn.startswith("dump")
            or fn.endswith(".fixture.json")
            or fn.endswith(".fixtures.json")):
        return True
    # (4) CONTENT shape — only meaningful for JSON; the bounded peek decides ARRAY-of-objects /
    #     Django-Rails fixture shape. Never fully parse a huge dump: cap the slice handed to json.loads.
    if fn.endswith(".json"):
        try:
            probe = text[:_DUMP_PROBE_BYTES].lstrip()
            # Cheap short-circuit: a key/value config opens with `{`; a row dump opens with `[`.
            # If it does not open with `[`, it is not the array-of-rows shape we skip on content.
            if not probe.startswith("["):
                return False
            obj = json.loads(text)   # full parse only reached for files that OPEN as a JSON array
            if isinstance(obj, list) and obj:
                first = obj[0]
                # array of dicts == a row dump (serialized records), not key/value config; the
                # Django/Rails fixture shape (`{"model":..,"fields":{..}}`) is a strict subset.
                if isinstance(first, dict):
                    return True
        except Exception:
            return False   # malformed / oversized-to-parse → do NOT skip on a guess (recall-safe)
    return False


# `.env` / `.env.production` / `.env.local` are dotfiles: os.path.splitext(".env") returns
# ("" , ".env")-ish only for bare ext detection and ("", ".env") DOES NOT yield ".env" as the
# extension (it's a leading-dot name). So a real `.env` file was never recognized as a config
# file and its declared env-var NAMES coupled 0 readers. Resolve the config extension by FILE
# NAME for the env family, falling back to the normal suffix for everything else.
#
# A `Dockerfile` has the SAME no-extension miss: os.path.splitext("Dockerfile")[1] is "" (and a
# `*.dockerfile` suffix is not in _CONFIG_EXTS either), so a Dockerfile's `ENV`/`ARG` env-var
# NAMES — read by app code via os.environ[...] / process.env.X — coupled 0 readers. The filename
# family is `Dockerfile`, `Dockerfile.<tag>` (e.g. Dockerfile.prod), and `*.dockerfile`. Map all
# of them to the synthetic `.dockerfile` config extension so the walk recognizes them.
def _config_ext(fn):
    """Config extension for a filename, treating the `.env` and Dockerfile families by name."""
    base = fn.lower()
    if base == ".env" or base.startswith(".env."):   # .env, .env.production, .env.local, …
        return ".env"
    if base == "dockerfile" or base.startswith("dockerfile.") or base.endswith(".dockerfile"):
        return ".dockerfile"                          # Dockerfile, Dockerfile.prod, web.dockerfile
    return os.path.splitext(fn)[1]


def _config_keys(
    ext,
    text,
    incomplete_paths_out=None,
    relative_path=None,
):
    """The keys declared in one config file. JSON → dotted + leaf; env → env-var NAMEs;
    ini/cfg → `KEY=`; yaml/toml → line keys.

    Returns a LIST of keys in deterministic WALK ORDER (insertion order, deduped) so that
    the per-file cap in the caller always keeps the same walk-order head regardless of
    PYTHONHASHSEED. A dict is used as an ordered set (keys[x] = None) instead of a plain
    set(), which has hash-order iteration that varies across process restarts. The helpers
    _dockerfile_env_var_names / _yaml_env_var_names return plain sets; their items are
    sorted() before insertion so their contribution is also deterministic."""
    keys = {}   # ordered-set: insertion order preserved, O(1) membership; value is always None
    if ext == ".json":
        try:
            obj = json.loads(text)
        except Exception:
            _mark_incomplete(incomplete_paths_out, relative_path)
            return list(keys)

        def flat(o, prefix=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    if str(k).lower() in _DEP_SECTIONS:   # manifest dep section → children are package names, skip
                        continue
                    dotted = prefix + str(k)
                    keys[dotted] = None
                    keys[str(k)] = None
                    flat(v, dotted + ".")
            elif isinstance(o, list):
                for it in o:
                    flat(it, prefix)
        flat(obj)
    elif ext == ".env":
        # A `.env` file DECLARES the names of environment variables that app code reads via
        # `os.environ["NAME"]` / `process.env.NAME` / `ENV["NAME"]` — the same coupling the yaml
        # env-var idiom captures, but `.env` is the more common form. Take only ALL-CAPS env-style
        # NAMES (the universal convention), so a lowercase `lower_key=...` (an ordinary local, not
        # a shared env var) is not swept and the VALUE after `=` is never captured (content-free).
        # `export KEY=...` (the POSIX-shell form) is handled by the optional leading `export`.
        for line in text.splitlines():
            m = re.match(r'\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*=', line)
            if m:
                keys[m.group(1)] = None
    elif ext == ".dockerfile":
        for name in sorted(_dockerfile_env_var_names(text)):   # sorted: set → deterministic order
            keys[name] = None
    elif ext in (".ini", ".cfg"):
        for line in text.splitlines():
            m = re.match(r'\s*([A-Za-z_][\w.\-]*)\s*[=:]', line)
            if m:
                keys[m.group(1)] = None
    else:  # yaml / toml — recall-biased line regex (no dep), section-aware to skip dep tables
        # A TOML `[dependencies]` / `[tool.poetry.dependencies]` / `[dev-dependencies]` table lists
        # third-party PACKAGE names, not app config keys (see _DEP_SECTIONS). Track the current
        # `[table]` header and skip keys inside a dep table. YAML has no `[...]` table syntax, so
        # `section` stays None for yaml and every yaml key is kept (a yaml `dependencies:` block is
        # rare and not the false-coupling source the TOML manifests are).
        section = None
        for line in text.splitlines():
            sm = re.match(r'\s*\[+\s*([\w.\- ]+?)\s*\]+\s*$', line)   # [deps] / [[x]] / [tool.poetry.dependencies]
            if sm:
                section = sm.group(1).split(".")[-1].strip().lower()
                continue
            if section in _DEP_SECTIONS:                              # inside a dependency table → skip its keys
                continue
            m = re.match(r'\s*([A-Za-z_][\w.\-]*)\s*[:=]', line)
            if m:
                keys[m.group(1)] = None
        if ext in (".yaml", ".yml"):
            for name in sorted(_yaml_env_var_names(text)):   # sorted: set → deterministic order
                keys[name] = None
    return list(keys)   # walk-order list; caller iterates and applies per-file cap


# Common YAML idioms that DECLARE the name of an environment variable (the real config
# resource code reads via `os.environ["NAME"]` / `process.env.NAME`). The line-key regex
# above misses these: in `- key: SOME_VAR` the NAME is the VALUE of `key:`, not a YAML
# key; in a compose/k8s `environment:` block it's a list item or a mapping under the block.
# Capturing the NAMES (never values) couples the code files that read the same env var —
# e.g. two files that both call os.environ.get on the SAME name. Content-free: names only.
# Env-block header (`envVars:` / `environment:` / `env:`), matched against a SINGLE physical line so
# block detection iterates splitlines() directly. The earlier whole-text-offset approach mapped each
# header's byte offset back to a line index, which desynced on CRLF (`\r\n` is one line to
# splitlines() but the offset math counted it as a single char, so the header offset was off-by-N and
# the lookup raised an unhandled StopIteration → a silent partial config graph, or an outright
# build_graph crash when os.walk had nothing left to absorb the StopIteration as walk-end).
_ENV_BLOCK_LINE_RE = re.compile(r'^(\s*)(?:envVars|environment|env)\s*:\s*$')
_KEY_FIELD_RE = re.compile(r'(?m)^\s*-?\s*key\s*:\s*["\']?([A-Z][A-Z0-9_]{2,})["\']?\s*(?:#.*)?$')


def _yaml_env_var_names(text):
    """Env-var NAMES declared in a yaml config (Render blueprint / CI / docker-compose / k8s).

    Two idioms: (1) `- key: NAME` (Render/Actions list-of-{key,value}); (2) an
    `environment:`/`env:` block whose entries are `- NAME=val`, `- NAME`, or `NAME: val`.
    Only ALL-CAPS env-style names (the universal convention) are taken, so ordinary
    yaml values never leak in. Block membership is bounded by indentation, so a `NAME:`
    elsewhere in the file is not swept in (precision)."""
    names = set()
    for m in _KEY_FIELD_RE.finditer(text):
        names.add(m.group(1))
    # Iterate the PHYSICAL lines directly (splitlines() handles \n, \r\n, and the Unicode line
    # separators uniformly). For each env-block header line, scan the indented lines under it until
    # a dedent. No whole-text byte offsets → no offset/line desync, so CRLF (or any non-\n
    # terminator) can never raise StopIteration or silently truncate the walk.
    lines = text.splitlines()
    for i, header in enumerate(lines):
        hm = _ENV_BLOCK_LINE_RE.match(header)
        if not hm:
            continue
        indent = len(hm.group(1))
        for ln in lines[i + 1:]:
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            cur = len(ln) - len(ln.lstrip())
            if cur <= indent:            # dedented out of the block → done
                break
            e = re.match(r'-\s*["\']?([A-Z][A-Z0-9_]{2,})["\']?\s*(?:[=:].*)?$', ln.strip())
            if not e:
                e = re.match(r'["\']?([A-Z][A-Z0-9_]{2,})["\']?\s*:', ln.strip())
            if e:
                names.add(e.group(1))
    return names


# A Dockerfile DECLARES env-var NAMES via `ENV` and build-arg NAMES via `ARG`. App code reads
# these via os.environ[...] / process.env.X — the SAME differentiated coupling the `.env` and yaml
# idioms capture. Two forms of `ENV` exist: the legacy space form `ENV KEY value` (exactly one
# name + the rest is the value), and the `=` form `ENV K1=v1 K2=v2 ...` (one or more name=value
# pairs on a line). `ARG` is `ARG NAME` or `ARG NAME=default`. Take only ALL-CAPS env-style NAMES
# (the universal convention) and NEVER the value/default after `=` or the space (content-free).
# Only `ENV` / `ARG` instructions are inspected, so `FROM` / `RUN` / `COPY` / `CMD` etc. (whose
# arguments are not env-var names) are ignored.
_DOCKER_INSTR_RE = re.compile(r'^\s*(ENV|ARG)\s+(.*?)\s*$', re.IGNORECASE)
_DOCKER_NAME = r'[A-Z][A-Z0-9_]{2,}'


def _dockerfile_env_var_names(text):
    """Env-var / build-arg NAMES declared by a Dockerfile's `ENV` and `ARG` instructions.

    `ENV K=V` (one or more pairs) and `ARG K`/`ARG K=V` capture the names left of `=`; the
    legacy `ENV K V` (space form) captures the single first token. Names only, never values."""
    names = set()
    for line in text.splitlines():
        m = _DOCKER_INSTR_RE.match(line)
        if not m:
            continue
        instr, rest = m.group(1).upper(), m.group(2)
        if not rest:
            continue
        if "=" in rest:
            # `=` form: `K1=v1 K2=v2 ...` for ENV, `K=default` for ARG. Each `NAME=` left side
            # is a declared name; the value to its right is never captured (content-free).
            for pm in re.finditer(r'(?:^|\s)(' + _DOCKER_NAME + r')\s*=', rest):
                names.add(pm.group(1))
        else:
            # space form: ENV `KEY value...` → first token is the name, the rest is the value;
            # ARG `NAME` → the lone token is the name.
            first = rest.split()[0]
            if re.fullmatch(_DOCKER_NAME, first):
                names.add(first)
    return names


# UBIQUITOUS config-key WORDS — generic words that appear as a config key in nearly every config
# file (and as a string/identifier in nearly every code file), so two UNRELATED files both reading
# `timeout` / `debug` / `level` are NOT really coupled — they just both happen to have a timeout.
# This is the config analogue of the proven ubiquitous-CALL-name stoplist (tests/recall_measure.py's
# STOP, and the multi-definer drop in test_false_coupling_precision.py): a shared name that is too
# common to carry coupling signal.
#
# CARDINAL RULE — recall is sacred. A SPECIFIC key (`STRIPE_WEBHOOK_SECRET`, `DATABASE_POOL_SIZE`,
# `FEATURE_X_ENABLED`) is a LEGITIMATE coupling and must NEVER be suppressed. So this WORD set is
# consulted ONLY by `_is_ubiquitous_config_key`, which classifies a key as ubiquitous ONLY when the
# ENTIRE key is made of ubiquitous words — i.e. there is no distinguishing token anywhere. A compound
# like `webhook_secret` or `pool.size` has a distinguishing segment (`webhook` / `pool`) and is KEPT.
# When in doubt → KEPT (the predicate returns ubiquitous only on a fully-generic key).
#
# These supplement (not replace) _CONFIG_STOPKEYS / the len>=6 floor in _specific_key: those already
# drop short/structural words at MINT time so they never become config_key nodes. The words here are
# the ones that survive _specific_key (len>=6 OR they got matched as a leaf of a compound) yet are
# still pure-generic — e.g. `timeout`, `enabled`, `disabled`, `interval`, `verbose`. The predicate is
# also segment-aware so a fully-generic DOTTED key (`config.default`, `log.level`) is caught while any
# specific compound is preserved.
_UBIQUITOUS_CONFIG_WORDS = frozenset({
    # presence / on-off flags (every component has one)
    "enabled", "disabled", "active", "inactive", "on", "off", "flag",
    # generic time / size / count knobs (a timeout/limit is not a shared resource)
    "timeout", "interval", "delay", "retries", "retry", "ttl", "expiry", "expires",
    "limit", "max", "min", "size", "count", "length", "capacity", "buffer", "batch",
    # generic location / identity words (already partly in _CONFIG_STOPKEYS; here for the leaf test)
    "name", "id", "key", "value", "path", "url", "uri", "host", "hostname", "port",
    "address", "endpoint", "prefix", "suffix", "scheme", "protocol",
    # generic logging / mode / level / env / status words
    "level", "mode", "debug", "verbose", "quiet", "loglevel", "logging", "log",
    "env", "environment", "stage", "status", "state", "type", "kind", "format",
    "config", "settings", "options", "option", "default", "defaults", "version",
    "region", "zone", "locale", "language", "lang", "encoding", "charset",
    # generic io / data words
    "input", "output", "source", "target", "dest", "destination", "data", "content",
    "file", "dir", "directory", "folder", "tmp", "temp", "cache",
    # generic credential WORDS — note: only ubiquitous when the WHOLE key is just this word; a
    # compound like `stripe_secret` / `github_token` keeps its distinguishing prefix and is KEPT.
    "user", "username", "password", "secret", "token", "auth",
    "enable", "disable", "use",
})

# Token splitter for a config key: split on the structural separators . _ - and also camelCase
# boundaries, lowercased. `DATABASE_POOL_SIZE` → {database, pool, size}; `logLevel` → {log, level};
# `db.pool_size` → {db, pool, size}. A token is kept only if it is alphabetic (a numeric segment like
# `v2` or `0` carries no distinguishing word but must not, on its own, make a key "specific").
_CAMEL_RE = re.compile(r'[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+')


def _key_word_tokens(k):
    """The set of lowercase ALPHA word-tokens in a config key (split on . _ - and camelCase)."""
    toks = set()
    for seg in re.split(r'[._\-]+', k):
        for m in _CAMEL_RE.finditer(seg):
            t = m.group(0).lower()
            if t.isalpha():
                toks.add(t)
    return toks


def _is_ubiquitous_config_key(k):
    """A config key whose EVERY word-token is a ubiquitous generic word — so two files both reading
    it are NOT really coupled (they just both have a `timeout` / `log.level`). Recall-safe: returns
    True ONLY when there is NO distinguishing token anywhere in the key. A key with even one specific
    token (`webhook` in `webhook_secret`, `stripe` in `stripe.api_key`, `pool` in `db.pool_size`) is
    NOT ubiquitous and is KEPT. An empty/odd token set (e.g. all-numeric) is treated as NOT ubiquitous
    (when in doubt, KEEP)."""
    toks = _key_word_tokens(k)
    if not toks:
        return False
    return toks <= _UBIQUITOUS_CONFIG_WORDS


# A camelCase boundary (`poolSize`, `logLevel`) — a structural separator just like . _ -, so a key
# carrying one IS a compound (a distinguishing multi-word name), even without a literal . _ -.
_CAMEL_BOUNDARY_RE = re.compile(r'[a-z][A-Z]|[A-Z][A-Z][a-z]')


def _is_distinctive_config_key(k):
    """True when a config key has DISTINGUISHING STRUCTURE that lets it carry real cross-file coupling.
    A key is distinctive when it is EITHER:
      • a COMPOUND — it has a structural separator (`.` `_` `-`) or a camelCase boundary
        (`db.pool_size`, `webhook_secret`, `poolSize`, `logLevel`): a multi-word name whose
        co-occurrence in two files is unlikely to be coincidence; OR
      • an ALL-CAPS ENV-VAR name (`VERIPSA_DSN`, `BROTLI`, `DATABASE`): the universal convention for an
        environment variable app code reads via `os.environ[...]` / `process.env.X` — even a single such
        token names a specific shared resource.

    A BARE single dictionary/programming word (`module`, `dotenv`, `public`, `website`, `pattern`,
    `failure`, `pathname`, `metadata`, `typescript`, `browser`) is NOT distinctive. Such a word is minted
    as a config_key from some config file (a manifest/compiler-option/dependency word — `"module"` in
    tsconfig, `"browser"`/`"website"` in package.json, `"dotenv"` in pyproject) yet ALSO occurs in code
    as an ordinary identifier or string literal (`import from 'module'`, `type="module"`, `load_dotenv`),
    so its `reads_config` matches are COINCIDENTAL — they couple files that share no real config resource.
    This is the single-token generalization of `_is_ubiquitous_config_key`: a key with no distinguishing
    structure carries no coupling signal. Recall-safe — MEASURED on flask/httpx/zustand/axios + Veripsa:
    dropping bare-word keys removed 21 spurious file-pairs (e.g. `module`→13, `dotenv`→1, `public`/
    `metadata`→7) and 0 real ones (every genuine key — `VERIPSA_DSN`, `webhook_secret`,
    `payment_gateway_url`, `pull_requests` — is compound or ALL-CAPS and is KEPT)."""
    if any(c in k for c in "._-"):
        return True
    if _CAMEL_BOUNDARY_RE.search(k):     # camelCase compound (poolSize / logLevel / HTTPServer)
        return True
    if k.isupper() and len(k) >= 3:      # ALL-CAPS env-var name (VERIPSA_DSN handled by . _ - above; BROTLI/DATABASE)
        return True
    return False                         # a bare single lower/Capitalized word — no distinguishing structure


def _specific_key(k):
    """Worth matching in code: compound (has . _ -) or long enough, and not a too-common
    bare word — so a generic `"name"` string in code never false-matches a 'config key'.

    RECALL-SAFE UBIQUITOUS-KEY SUPPRESSION (measured 2026-06-19 on airflow/netbox/redash): a key
    whose EVERY word-token is generic (`timeout`, `log.level`, `max_size`, `config.default`) carries
    NO coupling signal — files sharing it co-change ~3x LESS than files sharing a specific key
    (mean lift 2.36 vs 7.79 on airflow), and demoting all 99k such pairs across the three repos cost
    0 real recall (the 2 strong-co-change ubiq-only pairs were themselves spurious — `source` / a
    `__type` serialization marker — and co-changed for unrelated reasons). So such a key is never
    minted as a config_key node (no node → no `reads_config` edge → no spurious pair). A key with even
    ONE distinguishing token (`webhook` in `webhook_secret`, `pool` in `db.pool_size`) is NOT
    ubiquitous and stays a config_key — specific couplings are untouched (when in doubt, KEEP).

    RECALL-SAFE BARE-WORD SUPPRESSION (measured 2026-06-20 on flask/httpx/zustand/axios + Veripsa): a
    key with no DISTINGUISHING STRUCTURE — a bare single dictionary/programming word like `module`,
    `dotenv`, `public`, `website`, `pattern` — is minted from a manifest/compiler-option word in some
    config file yet matches COINCIDENTAL code occurrences (`import from 'module'`, `type="module"`,
    `load_dotenv`), coupling files that share no real config resource. `_is_distinctive_config_key`
    keeps only COMPOUND (separator / camelCase) or ALL-CAPS ENV-VAR keys, where co-occurrence is a real
    signal; it dropped 21 spurious pairs and 0 real ones across the measured repos. The earlier `len>=6`
    floor minted EVERY such bare word — this replaces that floor with a structure test, not a length one."""
    if k.lower() in _CONFIG_STOPKEYS:
        return False
    if _is_ubiquitous_config_key(k):   # fully-generic key — no distinguishing token → not a coupling-bearing key
        return False
    return _is_distinctive_config_key(k)   # bare single word (no separator/camelCase/ALL-CAPS) → not coupling-bearing


def _config_graph(root, config_files, source_files, incomplete_paths_out=None):
    """Backward-compatible diagnostics wrapper for the config substrate."""
    try:
        return _config_graph_impl(
            root,
            config_files,
            source_files,
            incomplete_paths_out=incomplete_paths_out,
        )
    except Exception:
        for path, _cfg_ext in config_files:
            _mark_incomplete(
                incomplete_paths_out,
                os.path.relpath(path, root).replace(os.sep, "/"),
            )
        for path, ext in source_files:
            if ext == ".py" or ext in _GRAMMAR_BY_EXT:
                _mark_incomplete(
                    incomplete_paths_out,
                    os.path.relpath(path, root).replace(os.sep, "/"),
                )
        return [], []


def _config_graph_impl(
    root,
    config_files,
    source_files,
    incomplete_paths_out=None,
):
    """Return (config_nodes, reads_config_edges) for `root`. Two passes: (1) parse config files →
    config_file/config_key nodes + the KNOWN-key set; (2) scan code for string literals / env-var refs
    matching a KNOWN key → `reads_config` edges (the key-set gives precision — only real keys match).

    `config_files` is the (path, cfg_ext) list and `source_files` the (path, ext) list build_graph ALREADY
    filtered through the SAME source-path guards (size cap, NUL-binary, generated/vendored, symlinks). The pass
    reads ONLY those lists — it does NOT re-walk the tree, so an oversized / binary-renamed / generated config
    or code file can no longer be read + parsed in FULL outside those guards (the old independent os.walk did).

    OUTPUT CAP (_MAX_CONFIG_KEYS): minting stops once the per-repo key ceiling is hit (a crafted/large config
    can otherwise mint unbounded config_key nodes + the keyset behind unbounded reads_config edges). Above the
    cap a key is neither minted NOR added to the keyset, so pass 2 also never emits an edge to an un-minted key.

    JSON SCHEMA SKIP (_is_json_schema_file): JSON Schema definition files (*.schema.json,
    *.tmLanguage.json, any JSON file with a top-level `$schema` key) are skipped entirely.
    They mint thousands of structural vocabulary keys (properties.X.description / .type …) that
    are NOT application config keys any source file reads — they exhaust the global pool and silently
    drop real coupling keys processed later (HIGH SILENT-DROP: two files reading the same env-var
    appear unrelated because the env-var key was never minted).

    PER-FILE CAP (_MAX_CONFIG_KEYS_PER_FILE): at most _MAX_CONFIG_KEYS_PER_FILE distinct specific
    keys are minted from any single config file.  Real app configs stay well under this limit;
    data-dump / fixture / migration JSON files that list thousands of project or task names are
    capped so they cannot exhaust the global pool before the real config files are processed.

    Pass 2 RE-READS each code file on demand (streaming) rather than buffering every code-file's text in memory
    between the passes — bounding peak memory to ~one file at a time on a huge repo."""
    nodes, edges = [], []
    keyset = set()
    capped = False   # True once _MAX_CONFIG_KEYS distinct keys are minted → stop minting (degrade gracefully)
    definition_incomplete = False
    for path, cfg_ext in config_files:   # pass 1: config files → keys (guarded list, no re-walk)
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        read_loss = set()
        text = _read_capped(path, read_loss, rel)
        if read_loss:
            definition_incomplete = True
            _mark_incomplete(incomplete_paths_out, rel)
        if text is None:
            continue
        # Skip JSON Schema definition files: they carry no cross-file app-coupling keys and
        # exhausting the pool with their structural vocabulary silently drops real coupling keys.
        fn = os.path.basename(path)
        if cfg_ext == ".json" and _is_json_schema_file(fn, text):
            continue
        # Skip DATA-DUMP / FIXTURE / SEED / LOCALE files: their keys are model FIELD NAMES (Django
        # fixtures, seed dumps, schema.rb) or UI message ids (i18n) — compound enough to pass the
        # distinctive/ubiquitous guards, then FALSE-COUPLE unrelated files that mention the same field
        # name. Audit: 65-91% of config couples on real Django/Rails repos were these. A fixture/dump is
        # not a shared app-config resource, so skipping its mint loses 0 real coupling (recall-safe). Same
        # mint-side spirit as _is_json_schema_file above / _cg_schema._is_test_path. Pass the relpath so
        # the path/segment signals (fixtures/, seeds/, locales/, *_data.json) match.
        if _is_data_dump_file(rel, text):
            continue
        parse_loss = set()
        ks = _config_keys(cfg_ext, text, parse_loss, rel)
        if parse_loss:
            definition_incomplete = True
            _mark_incomplete(incomplete_paths_out, rel)
        if ks:
            nodes.append({"id": rel, "kind": "config_file", "path": rel,
                          "language": "config"})
        per_file_minted = 0   # per-file cap: prevents a single data-dump from exhausting the global pool
        for k in ks:
            if _specific_key(k) and k not in keyset:
                if len(keyset) >= _MAX_CONFIG_KEYS:   # global ceiling — stop minting + growing the keyset
                    capped = True
                    definition_incomplete = True
                    _mark_incomplete(incomplete_paths_out, rel)
                    break
                if per_file_minted >= _MAX_CONFIG_KEYS_PER_FILE:   # per-file ceiling — skip remainder of THIS file
                    definition_incomplete = True
                    _mark_incomplete(incomplete_paths_out, rel)
                    break
                keyset.add(k)
                per_file_minted += 1
                nodes.append({"id": f"cfgkey::{rel}::{k}", "kind": "config_key",
                              "name": k, "path": rel, "language": "config"})
        if capped:
            break
    if capped:
        for path, _cfg_ext in config_files:
            _mark_incomplete(
                incomplete_paths_out,
                os.path.relpath(path, root).replace(os.sep, "/"),
            )
    if definition_incomplete:
        # A source file may reference any key omitted from a malformed/truncated
        # definition catalog.  Conservatively degrade the candidate consumers.
        for path, ext in source_files:
            if ext == ".py" or ext in _GRAMMAR_BY_EXT:
                _mark_incomplete(
                    incomplete_paths_out,
                    os.path.relpath(path, root).replace(os.sep, "/"),
                )
    for path, ext in source_files:   # pass 2: code references to KNOWN config keys (re-read, do not buffer)
        if not (ext == ".py" or ext in _GRAMMAR_BY_EXT):
            continue
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        text = _read_capped(path, incomplete_paths_out, rel)
        if text is None:
            continue
        seen = set()
        for m in re.finditer(r'["\']([\w.\-]{4,})["\']', text):
            k = m.group(1)
            if k in keyset and k not in seen:
                seen.add(k)
                edges.append({"src": rel, "dst": k, "kind": "reads_config"})
        for m in re.finditer(r'\b([A-Z][A-Z0-9_]{3,})\b', text):  # env-var style refs
            k = m.group(1)
            if k in keyset and k not in seen:
                seen.add(k)
                edges.append({"src": rel, "dst": k, "kind": "reads_config"})
    return nodes, edges
