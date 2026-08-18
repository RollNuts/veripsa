#!/usr/bin/env python3
"""GRAPH-INSIGHTS READER — the ALWAYS-PRESENT structural lens for the hosted platform dashboard.

THE NEED: every other reader-facing *_for_installation read (effect/now/repo_insights) is COLLISION-EVENT based,
so the customer's dashboard goes DARK whenever there are no recent collisions — even though the rich code graph
(core.code_node/code_edge) + the co-change cache are sitting right there. core.graph_insights_for_installation
surfaces that standing intelligence: ONE installation → its BUSIEST repo, the top co-change couplings, and the
most-depended-on (highest import fan-in) files. Non-empty as soon as a graph is ingested, with or without
contention. Mirrors account_coverage_for_installation's shape: SECURITY DEFINER, routed account pin, REVOKE
PUBLIC + GRANT EXECUTE to example_platform_reader + veripsa_app, never-raise → benign empty shape.

WHAT THIS GATE PROVES (live, on the ephemeral test Postgres — never grep; the only truth is the running DB):

  1. COUPLED + CENTRAL — with co_change pairs (some lift>=2 & co>=3, some BELOW the floor) and code_edge import
     fan-in seeded for the busiest repo, the read returns ONLY the qualifying coupled pairs (a<b, pct present),
     and central files ranked by dependents (in-repo import fan-in, internal file nodes only — stdlib dropped).

  2. CONTENT-FREE — no file BODY / secret string ever appears in the output: only paths, counts, and a pct. We
     deliberately seed a forbidden marker as code-node names / a dropped external import target and assert it
     never surfaces (the output carries only the structural projection).

  3. ROLE ALLOW — the example_platform_reader role (its grant) CAN EXECUTE the fn and gets the SAME shape (a
     dict with repo/coupled/central) — proving the least-privilege platform reader reaches exactly this surface.

  4. GRACEFUL EMPTY — an account with NO graph data, an UNKNOWN installation, and a BLANK id all return the
     benign {"repo":null,"coupled":[],"central":[]} (a broken/empty read can never break the dashboard).

  5. PER-REPO DETAIL — the OPTIONAL second arg p_repo scopes the SAME graph to the repo being viewed: NULL/1-arg
     keep the busiest repo (unchanged); a NON-busiest repo of the account returns ITS coupled/central; a repo NOT
     belonging to the account → benign empty (NEVER another tenant's data); content-free still holds when scoped.

Run:  python3 tests/test_graph_insights_reader.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# PROCESS-UNIQUE (parallel-safe): bootstrap + drop our OWN DB, like the sibling security gates — a FIXED name
# would let concurrent runs drop each other's DB mid-run.
DB = "veripsa_graphins_" + str(os.getpid())

READER = "example_platform_reader"

# A FORBIDDEN content marker: a code BODY / secret would look like this. We seed it as code-node NAMES + a
# DROPPED external import target so we can assert the structural output never leaks it.
SECRET = "TOP_SECRET_BODY_sk_live_DEADBEEF"

DENIED = ("permission denied",)

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def psql_mig(sql):
    """As the migrator (owner) — ground truth + fixtures (FORCE-RLS tables need the account pin + governed token)."""
    dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def psql_admin(sql):
    """The cluster admin/superuser (ADMIN_DSN) — the ONE thing the migrator cannot do: ALTER ROLE ... LOGIN."""
    dsn = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def psql_as(role, sql):
    """Run sql AS `role` (peer auth on localhost) — combined stdout+stderr so a permission-denied is visible."""
    dsn = f"postgresql://{role}@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def last_value(out):
    """The LAST non-empty line — a multi-statement `SET ...; SELECT ...` prints a 'SET' ack first."""
    lines = [ln for ln in out.splitlines() if ln.strip() != ""]
    return lines[-1].strip() if lines else ""


def insert_node(account, repo, branch, node_id, kind, path, name):
    """A FORCE-RLS + governed-write INSERT into core.code_node — pin the account (RLS WITH CHECK) + arm the
    forgery token, both txn-local, in ONE txn."""
    return psql_mig(
        "SET search_path=core; BEGIN; "
        f"SELECT set_config('core.current_account','{account}',true); "
        "SELECT core.mark_governed_write('code_node'); "
        "INSERT INTO core.code_node(account_id,node_id,node_kind,path,name,language,repo,branch) "
        f"VALUES ('{account}','{node_id}','{kind}','{path}','{name}','python','{repo}','{branch}'); "
        "COMMIT;")


def insert_edge(account, repo, branch, src, dst, kind):
    return psql_mig(
        "SET search_path=core; BEGIN; "
        f"SELECT set_config('core.current_account','{account}',true); "
        "SELECT core.mark_governed_write('code_edge'); "
        "INSERT INTO core.code_edge(account_id,repo,branch,src,dst,edge_kind) "
        f"VALUES ('{account}','{repo}','{branch}','{src}','{dst}','{kind}'); "
        "COMMIT;")


def insert_cochange(account, repo, a, b, co, n_a, n_b, strength, lift):
    return psql_mig(
        "SET search_path=core; BEGIN; "
        f"SELECT set_config('core.current_account','{account}',true); "
        "SELECT core.mark_governed_write('co_change'); "
        "INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total) "
        f"VALUES ('{account}','{repo}','{a}','{b}',{co},{n_a},{n_b},{strength},{lift},100) "
        "ON CONFLICT DO NOTHING; COMMIT;")


def bootstrap():
    """roles + schema.sql + demo seats; route inst 111→ACCT-DEMO; an EMPTY tenant ACCT-EMPTY (inst 333) with NO
    graph; seed the busiest repo's graph + co-change for ACCT-DEMO; LOGIN the reader (this ephemeral DB only)."""
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)

    # route the demo installation; a SECOND, EMPTY tenant (no graph) for the graceful-empty probe.
    psql_mig("SET search_path=core; "
             "INSERT INTO core.installation_account(installation_id,account_id) VALUES ('111','ACCT-DEMO') "
             "ON CONFLICT DO NOTHING;")
    psql_mig("SET search_path=core; "
             "SELECT core.provision_seat('ACCT-EMPTY','Empty Co','AG-E','empty','veripsa_empty_agent');")
    psql_mig("SET search_path=core; "
             "INSERT INTO core.installation_account(installation_id,account_id) VALUES ('333','ACCT-EMPTY') "
             "ON CONFLICT DO NOTHING;")
    # a THIRD tenant (ACCT-OTHER, inst 444) that OWNS a repo — the foreign-repo source for the cross-account guard.
    # (kept separate from ACCT-EMPTY so inst 333 stays genuinely graph-less for the graceful-empty probe.)
    psql_mig("SET search_path=core; "
             "SELECT core.provision_seat('ACCT-OTHER','Other Co','AG-O','other','veripsa_other_agent');")
    psql_mig("SET search_path=core; "
             "INSERT INTO core.installation_account(installation_id,account_id) VALUES ('444','ACCT-OTHER') "
             "ON CONFLICT DO NOTHING;")

    REPO = "demoorg/demo-repo"
    BR = "main"
    # ── the BUSIEST repo's FILE nodes (busiest by code_node count: this repo gets many; a DECOY repo gets few). ──
    # files: hub.py (imported by 3), util.py (imported by 2), api.py / models.py / auth.py / leaf.py (importers).
    files = [
        ("Nhub", "app/hub.py"), ("Nutil", "app/util.py"), ("Napi", "app/api.py"),
        ("Nmodels", "app/models.py"), ("Nauth", "app/auth.py"), ("Nleaf", "app/leaf.py"),
    ]
    for nid, p in files:
        # NOTE the node NAME carries the forbidden SECRET marker — proving the output never echoes node names/bodies.
        g = insert_node("ACCT-DEMO", REPO, BR, nid, "file", p, SECRET)
        if "ERROR" in g:
            print("[FAIL] could not seed a code_node row:"); print(g[-1500:]); sys.exit(2)
    # a couple of def nodes so 'busiest = most code_node rows' is unambiguous vs the decoy repo.
    for nid, p in [("Dh1", "app/hub.py"), ("Dh2", "app/hub.py"), ("Da1", "app/api.py")]:
        insert_node("ACCT-DEMO", REPO, BR, nid, "def", p, "fn")

    # a DECOY repo with FEWER nodes — must NOT be picked as busiest (so 'repo' == the demo repo).
    DECOY = "demoorg/tiny-repo"
    insert_node("ACCT-DEMO", DECOY, BR, "Td", "file", "tiny/x.py", "x")

    # ── a SECOND, NON-BUSIEST repo of the SAME account (demoorg/svc-repo) for the per-repo DETAIL probe (p_repo). ──
    # It has its OWN small graph + ONE floor-passing co-change pair + ONE central file, all DISTINCT from the
    # busiest repo's — so scoping to it must return ITS rows, never the busiest repo's. Fewer nodes than demo-repo.
    SVC = "demoorg/svc-repo"
    svc_files = [("Score", "svc/core.py"), ("Swork", "svc/worker.py"), ("Sutil", "svc/utils.py")]
    for nid, p in svc_files:
        g = insert_node("ACCT-DEMO", SVC, BR, nid, "file", p, SECRET)  # SECRET in name again — must not leak
        if "ERROR" in g:
            print("[FAIL] could not seed an svc-repo code_node row:"); print(g[-1500:]); sys.exit(2)
    #   svc/core.py ← worker.py, utils.py   (fan-in 2 — the most central IN THIS repo)
    for src, dst in [("svc/worker.py", "svc/core.py"), ("svc/utils.py", "svc/core.py")]:
        e = insert_edge("ACCT-DEMO", SVC, BR, src, dst, "imports")
        if "ERROR" in e:
            print("[FAIL] could not seed an svc-repo code_edge row:"); print(e[-1500:]); sys.exit(2)
    #   ONE floor-passing coupling in svc-repo (DISTINCT paths from demo-repo's pairs).
    cc = insert_cochange("ACCT-DEMO", SVC, "svc/core.py", "svc/worker.py", 6, 8, 7, 0.60, 3.2)
    if "ERROR" in cc:
        print("[FAIL] could not seed an svc-repo co_change row:"); print(cc[-1500:]); sys.exit(2)

    # ── a FOREIGN repo owned by a DIFFERENT account (ACCT-OTHER) — the cross-account leak guard for p_repo. ──────
    # Asking inst 111 (ACCT-DEMO) for this repo must yield the benign empty shape, NEVER this other tenant's data.
    FOREIGN = "otherorg/secret-repo"
    fg = insert_node("ACCT-OTHER", FOREIGN, BR, "Fg", "file", "other/leak.py", SECRET)
    if "ERROR" in fg:
        print("[FAIL] could not seed the foreign-account code_node row:"); print(fg[-1500:]); sys.exit(2)

    # ── import edges (edge_kind='imports'): dst = the imported file's PATH. fan-in = COUNT(DISTINCT src). ──
    #   hub.py  ← api.py, models.py, auth.py   (fan-in 3, the most central)
    #   util.py ← api.py, leaf.py              (fan-in 2)
    # plus a DUPLICATE edge (api→hub twice) to prove DISTINCT src (not raw edge count).
    # plus an EXTERNAL import target that has NO file node (the SECRET as a stdlib-ish dst) — MUST be dropped.
    edges = [
        ("app/api.py", "app/hub.py"), ("app/models.py", "app/hub.py"), ("app/auth.py", "app/hub.py"),
        ("app/api.py", "app/hub.py"),  # duplicate src → still fan-in 3 (DISTINCT)
        ("app/api.py", "app/util.py"), ("app/leaf.py", "app/util.py"),
        ("app/api.py", SECRET),        # external/stdlib target: no file node → dropped (never surfaces)
    ]
    for src, dst in edges:
        e = insert_edge("ACCT-DEMO", REPO, BR, src, dst, "imports")
        if "ERROR" in e:
            print("[FAIL] could not seed a code_edge row:"); print(e[-1500:]); sys.exit(2)

    # ── co-change pairs: TWO above the floor (lift>=2 AND co>=3), TWO below (sub-floor noise that must NOT surface). ──
    pairs = [
        # (a, b, co, n_a, n_b, strength, lift)  — canonical a<b
        ("app/api.py", "app/hub.py", 8, 10, 9, 0.80, 4.5),    # PASS: lift 4.5, co 8
        ("app/auth.py", "app/models.py", 5, 7, 6, 0.71, 3.0),  # PASS: lift 3.0, co 5
        ("app/api.py", "app/util.py", 4, 9, 8, 0.44, 1.2),     # FAIL lift (1.2 < 2)
        ("app/leaf.py", "app/util.py", 2, 5, 4, 0.40, 3.5),    # FAIL co (2 < 3)
    ]
    for a, b, co, na, nb, s, lift in pairs:
        cc = insert_cochange("ACCT-DEMO", REPO, a, b, co, na, nb, s, lift)
        if "ERROR" in cc:
            print("[FAIL] could not seed a co_change row:"); print(cc[-1500:]); sys.exit(2)

    # LOGIN the reader (this ephemeral DB only; prod sets LOGIN+password out-of-band). ALTER ROLE needs admin.
    login = psql_admin("ALTER ROLE example_platform_reader LOGIN;")
    if "ERROR" in login or "permission denied" in login:
        print("[FAIL] could not grant LOGIN to example_platform_reader via ADMIN_DSN:")
        print(login[-1500:]); sys.exit(2)


def drop():
    subprocess.run(["psql", os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres"),
                    "-tAc", "ALTER ROLE example_platform_reader NOLOGIN;"], capture_output=True, text=True)
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


def main():
    print("VERIPSA GRAPH-INSIGHTS READER — always-present structural lens for the hosted dashboard")
    print(f"(scratch DB: {DB})")
    bootstrap()

    # ── 1. COUPLED + CENTRAL — the owner (migrator, as a control) AND then the reader see the same shape. ──────
    out = None
    try:
        out = json.loads(last_value(psql_as(READER, "SET search_path=core; "
              "SELECT core.graph_insights_for_installation('111')::text;")))
    except Exception:
        out = None
    add("1 SHAPE: graph_insights_for_installation('111') returns a dict with repo/coupled/central",
        isinstance(out, dict) and {"repo", "coupled", "central"}.issubset(set(out.keys())))
    add("1 REPO: the BUSIEST repo (most code_node rows) is picked — demoorg/demo-repo, not the decoy",
        isinstance(out, dict) and out.get("repo") == "demoorg/demo-repo")

    coupled = out.get("coupled") if isinstance(out, dict) else None
    # exactly the TWO floor-passing pairs (lift>=2 AND co>=3); a<b; pct present (= round(strength*100)); AND the
    # HONEST DENOMINATOR pair support_n/observed_m (both ints) so the dashboard can render "N of M observed commits".
    coupled_ok = (isinstance(coupled, list) and len(coupled) == 2
                  and all(isinstance(c, dict) and set(c.keys()) == {"a", "b", "pct", "support_n", "observed_m"}
                          and c["a"] < c["b"] and isinstance(c["pct"], int)
                          and isinstance(c["support_n"], int) and isinstance(c["observed_m"], int)
                          for c in coupled))
    add("1 COUPLED: exactly the 2 floor-passing pairs (lift>=2 AND co>=3); each {a,b,pct,support_n,observed_m} "
        "with a<b and int pct/support_n/observed_m",
        coupled_ok)
    pair_set = {(c.get("a"), c.get("b")) for c in (coupled or [])}
    add("1 COUPLED: the qualifying pairs are present (api↔hub, auth↔models)",
        ("app/api.py", "app/hub.py") in pair_set and ("app/auth.py", "app/models.py") in pair_set)
    add("1 COUPLED: the sub-floor pairs are EXCLUDED (api↔util lift<2, leaf↔util co<3)",
        ("app/api.py", "app/util.py") not in pair_set and ("app/leaf.py", "app/util.py") not in pair_set)
    # strongest-first (lift desc): api↔hub (lift 4.5) before auth↔models (lift 3.0), and pct = round(strength*100).
    add("1 COUPLED: ranked strongest-first (lift desc) — api↔hub (pct 80) before auth↔models (pct 71)",
        isinstance(coupled, list) and len(coupled) == 2
        and coupled[0].get("a") == "app/api.py" and coupled[0].get("pct") == 80
        and coupled[1].get("a") == "app/auth.py" and coupled[1].get("pct") == 71)
    # HONEST DENOMINATOR (the new fields): support_n = co (#commits touching BOTH); observed_m = n_a+n_b-co (the
    # UNION = #commits touching EITHER, the observation basis). Seeded api↔hub: co=8,n_a=10,n_b=9 → 8 of 11;
    # auth↔models: co=5,n_a=7,n_b=6 → 5 of 8. M is the union, NOT n_total (=100): "N of <either>", not "N of <all>".
    by_pair = {(c.get("a"), c.get("b")): c for c in (coupled or [])}
    ah = by_pair.get(("app/api.py", "app/hub.py"))
    am = by_pair.get(("app/auth.py", "app/models.py"))
    add("1 DENOMINATOR: api↔hub carries support_n=8 (co) and observed_m=11 (n_a+n_b-co = 10+9-8 = the UNION), "
        "NOT 100 (n_total) — 'N of M' = of the commits touching EITHER, how many touched BOTH",
        isinstance(ah, dict) and ah.get("support_n") == 8 and ah.get("observed_m") == 11)
    add("1 DENOMINATOR: auth↔models carries support_n=5 and observed_m=8 (7+6-5)",
        isinstance(am, dict) and am.get("support_n") == 5 and am.get("observed_m") == 8)
    # INVARIANTS that make the pct HONEST: support_n is exactly the count, observed_m >= support_n (a union can
    # never be below its intersection — so N<=M always; "N of M" can never read more-than-100%), and observed_m
    # is the pair-local union (never the global n_total) so a quiet pair is not diluted by the whole repo's volume.
    add("1 DENOMINATOR: every coupled pair has observed_m >= support_n >= 1 (N<=M; the honest 'N of M' never lies "
        "above 100%)",
        isinstance(coupled, list) and len(coupled) == 2
        and all((c.get("support_n") or 0) >= 1 and (c.get("observed_m") or 0) >= (c.get("support_n") or 0)
                for c in coupled))

    central = out.get("central") if isinstance(out, dict) else None
    central_ok = (isinstance(central, list) and len(central) == 2
                  and all(isinstance(c, dict) and set(c.keys()) == {"path", "dependents"}
                          and isinstance(c["dependents"], int) for c in central))
    add("1 CENTRAL: returns {path,dependents} (int) entries for the internal file nodes",
        central_ok)
    # hub.py fan-in 3 (DISTINCT src, the dup edge does NOT double-count), util.py fan-in 2; ranked dependents desc.
    add("1 CENTRAL: ranked by in-repo import fan-in (DISTINCT importers) — hub.py(3) before util.py(2)",
        isinstance(central, list) and len(central) == 2
        and central[0].get("path") == "app/hub.py" and central[0].get("dependents") == 3
        and central[1].get("path") == "app/util.py" and central[1].get("dependents") == 2)
    cpaths = {c.get("path") for c in (central or [])}
    add("1 CENTRAL: the EXTERNAL import target (no file node) is DROPPED — only internal files surface",
        SECRET not in cpaths and "app/hub.py" in cpaths)

    # ── 2. CONTENT-FREE — the forbidden body/secret marker NEVER appears anywhere in the output. ───────────────
    raw = last_value(psql_as(READER, "SET search_path=core; "
                                     "SELECT core.graph_insights_for_installation('111')::text;"))
    add("2 CONTENT-FREE: the forbidden secret/body marker NEVER appears in the output (only paths/counts/pct)",
        raw != "" and SECRET not in raw)
    # belt-and-suspenders: the only string-typed leaves are file PATHS + the repo name (no node names, no bodies).
    leaf_strings_ok = isinstance(out, dict)
    if isinstance(out, dict):
        allowed_paths = {"app/hub.py", "app/util.py", "app/api.py", "app/models.py", "app/auth.py", "app/leaf.py"}
        for c in (out.get("coupled") or []):
            if c.get("a") not in allowed_paths or c.get("b") not in allowed_paths:
                leaf_strings_ok = False
        for c in (out.get("central") or []):
            if c.get("path") not in allowed_paths:
                leaf_strings_ok = False
    add("2 CONTENT-FREE: every string leaf is an EXPECTED file PATH (no names/bodies/secret leaked)",
        leaf_strings_ok)

    # ── 3. ROLE ALLOW — the reader has EXECUTE (a real grant) + the owner does too; non-reader buyers do NOT. ──
    add("3 ROLE ALLOW: the reader CAN execute graph_insights_for_installation and got a dict (no permission denied)",
        isinstance(out, dict) and "permission denied" not in raw)
    hp = psql_mig(f"SELECT has_function_privilege('{READER}',"
                  f"'core.graph_insights_for_installation(text,text)','EXECUTE');")
    add("3 ROLE ALLOW: has_function_privilege(reader, graph_insights_for_installation) is TRUE", hp == "t")
    hp_app = psql_mig("SELECT has_function_privilege('veripsa_app',"
                      "'core.graph_insights_for_installation(text,text)','EXECUTE');")
    add("3 ROLE ALLOW: the App service identity (veripsa_app) also HAS EXECUTE", hp_app == "t")
    # a NON-reader buyer/seat class must NOT (the PUBLIC default was revoked; grant is reader + app only).
    hp_writer = psql_mig("SELECT has_function_privilege('veripsa_writer',"
                         "'core.graph_insights_for_installation(text,text)','EXECUTE');")
    add("3 ROLE DENY: a buyer/seat class (veripsa_writer) has NO EXECUTE (reader + App only; PUBLIC revoked)",
        hp_writer == "f")

    # ── 4. GRACEFUL EMPTY — no graph, unknown id, blank id all → {"repo":null,"coupled":[],"central":[]}. ──────
    EMPTY = {"repo": None, "coupled": [], "central": []}

    def empty_via(arg_sql):
        try:
            return json.loads(last_value(psql_as(READER, "SET search_path=core; "
                  f"SELECT core.graph_insights_for_installation({arg_sql})::text;")))
        except Exception:
            return "ERR"

    add("4 EMPTY: an account with NO graph data (inst 333 → ACCT-EMPTY) → benign empty shape",
        empty_via("'333'") == EMPTY)
    add("4 EMPTY: an UNKNOWN installation id → benign empty shape", empty_via("'999'") == EMPTY)
    add("4 EMPTY: a BLANK id → benign empty shape", empty_via("''") == EMPTY)
    add("4 EMPTY: a NULL id → benign empty shape", empty_via("NULL") == EMPTY)

    # ── 5. PER-REPO DETAIL (optional p_repo) — the per-repo page scopes THIS SAME graph to the repo being viewed. ─
    def scoped(arg_sql):
        try:
            return json.loads(last_value(psql_as(READER, "SET search_path=core; "
                  f"SELECT core.graph_insights_for_installation({arg_sql})::text;")))
        except Exception:
            return "ERR"

    # (a) p_repo NULL / 1-arg keep the EXACT busiest-repo behavior (the 1-arg site keeps working via the DEFAULT).
    one_arg = scoped("'111'")
    null_arg = scoped("'111', NULL")
    add("5a UNCHANGED: explicit p_repo=NULL returns the busiest repo (demoorg/demo-repo), same as 1-arg",
        isinstance(null_arg, dict) and null_arg.get("repo") == "demoorg/demo-repo")
    add("5a UNCHANGED: 1-arg and (id, NULL) are byte-identical (the DEFAULT = the prior behavior exactly)",
        isinstance(one_arg, dict) and one_arg == null_arg)

    # (b) p_repo = a SPECIFIC NON-busiest repo of the account → scope coupled/central to THAT repo only.
    svc = scoped("'111', 'demoorg/svc-repo'")
    add("5b SCOPED: p_repo=demoorg/svc-repo (non-busiest, same account) returns THAT repo",
        isinstance(svc, dict) and svc.get("repo") == "demoorg/svc-repo")
    svc_pairs = {(c.get("a"), c.get("b")) for c in (svc.get("coupled") or [])} if isinstance(svc, dict) else set()
    add("5b SCOPED: coupled is svc-repo's OWN pair (svc/core.py↔svc/worker.py), NOT the busiest repo's",
        svc_pairs == {("svc/core.py", "svc/worker.py")})
    svc_central = {c.get("path") for c in (svc.get("central") or [])} if isinstance(svc, dict) else set()
    add("5b SCOPED: central is svc-repo's OWN file (svc/core.py, fan-in 2), NOT the busiest repo's hub.py",
        svc_central == {"svc/core.py"}
        and any(c.get("path") == "svc/core.py" and c.get("dependents") == 2 for c in (svc.get("central") or [])))
    # the busiest repo's data must NOT bleed into the scoped view.
    add("5b ISOLATION: the busiest repo's paths (app/hub.py, app/api.py) do NOT appear in the svc-repo view",
        "app/hub.py" not in svc_central and ("app/api.py", "app/hub.py") not in svc_pairs)

    # (c) p_repo = a repo that does NOT belong to the account (it's a DIFFERENT tenant's repo) → benign empty,
    #     NEVER that other account's data, NEVER an error (cross-account leak guard).
    foreign = scoped("'111', 'otherorg/secret-repo'")
    add("5c CROSS-ACCOUNT: a foreign repo (another tenant's) → benign empty shape (no leak)",
        foreign == EMPTY)
    # the guard is REAL: the OWNING tenant (inst 444 → ACCT-OTHER) DOES see that repo — so 5c's empty is the
    # account pin + membership check refusing it for inst 111, not the repo simply not existing anywhere.
    owner_view = scoped("'444', 'otherorg/secret-repo'")
    add("5c CROSS-ACCOUNT: the OWNING tenant DOES resolve that repo (proves the empty above is a real refusal)",
        isinstance(owner_view, dict) and owner_view.get("repo") == "otherorg/secret-repo")
    # also a repo string that exists for NO account at all → benign empty.
    add("5c UNKNOWN-REPO: a repo string belonging to no account → benign empty shape",
        scoped("'111', 'nobody/no-such-repo'") == EMPTY)
    add("5c CROSS-ACCOUNT: a BLANK p_repo falls back to busiest (not treated as a missing repo)",
        isinstance(scoped("'111', ''"), dict) and scoped("'111', ''").get("repo") == "demoorg/demo-repo")

    # (d) CONTENT-FREE still holds for the SCOPED view — the forbidden marker (seeded as svc node names + the
    #     foreign node name) never appears in the per-repo output either.
    svc_raw = last_value(psql_as(READER, "SET search_path=core; "
              "SELECT core.graph_insights_for_installation('111','demoorg/svc-repo')::text;"))
    add("5d CONTENT-FREE: the scoped (per-repo) output never leaks the secret/body marker — paths/counts/pct only",
        svc_raw != "" and SECRET not in svc_raw)
    svc_allowed = {"svc/core.py", "svc/worker.py", "svc/utils.py"}
    svc_leaf_ok = isinstance(svc, dict)
    if isinstance(svc, dict):
        for c in (svc.get("coupled") or []):
            if c.get("a") not in svc_allowed or c.get("b") not in svc_allowed:
                svc_leaf_ok = False
        for c in (svc.get("central") or []):
            if c.get("path") not in svc_allowed:
                svc_leaf_ok = False
    add("5d CONTENT-FREE: every string leaf in the scoped view is an EXPECTED svc-repo path (no leak)",
        svc_leaf_ok)

    # ── verdict ──────────────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} assertions passed "
          "(coupled+central ranked & floored · content-free · reader-execute · graceful empty · per-repo scope) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} assertion(s) FAILED — the graph-insights reader or its content-free wall "
              "has a hole:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nGRAPH-INSIGHTS READER GATE: FAIL")
        sys.exit(1)
    print("\nHONEST: graph_insights_for_installation surfaces the customer's OWN busiest-repo structure — top "
          "co-change couplings (lift-floored, a<b, pct) + most-imported files (in-repo fan-in) — content-free, "
          "reachable by the platform reader, and gracefully empty on no data / unknown id.")
    print("GRAPH-INSIGHTS READER GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        print(f"\n[FAIL] unexpected error: {e}")
        print("GRAPH-INSIGHTS READER GATE: FAIL")
        sys.exit(1)
