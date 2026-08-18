#!/usr/bin/env python3
"""Shared harness for the webhook SERVER gate (tests/test_server.py).

This is the SELF-CONTAINED test infrastructure for the server gate — extracted from test_server.py verbatim to
shrink that god-file + cut its merge-conflict surface (following Veripsa's OWN god-file signal). It holds:
  * the FakeGitHub harness — records what the App WOULD post and serves PR files + a repo tarball from the
    sample_app fixture (fakes GitHub's REST I/O so the live loop runs offline, no deploy / no GitHub account);
  * the per-process DB name + the fixed REPO/SHA coordinates the scenarios key off;
  * the small shared helpers (make_db / _json_scalar / pr_payload / _try_put) every scenario reuses.

test_server.py imports these and runs every scenario over the REAL gate, printing 'SERVER GATE: PASS'. Behavior
is identical to the pre-split single file — this is a pure move + import, no test/assertion was changed.
"""
from __future__ import annotations

import io
import hashlib
import json
import os
import tarfile

import psycopg2

# ROOT is computed from THIS file's location; _server_harness.py sits alongside test_server.py under tests/, so
# dirname(dirname(__file__)) is the repo root — the SAME value test_server.py computed before the split.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets two concurrent
# runs (e.g. parallel CI shards, or several agents each running run_gates) drop each other's DB mid-run →
# "database does not exist" crashes. Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$) + run_gates (veripsa_gates_$$).
# Defined here (one source) and imported by test_server.py so make_db() + the __main__ dropdb share one name.
DB = "veripsa_servertest_" + str(os.getpid())
REPO = "acme/app"
SHA = "a" * 40
# The OWNING-ACCOUNT id for the harness's repo. This is the STABLE tenant key the live path routes by
# (repository.owner.id → ACCT-GH-<owner_id>) — NOT the installation id. It is the value pr_payload stamps into
# repository.owner.id so a payload run through make_db_processor resolves to ACCT-GH-4242 via the HONEST first-choice
# source. (It happens to equal the install id below, which is exactly why the old phantom-tenant bug — routing by
# installation.id when owner.id was absent — stayed masked: dropping that fallback now requires owner.id be present,
# as it always is on a real GitHub payload. FakeGitHub.owner_id defaults to this same 4242, so boot + live agree.)
OWNER_ID = 4242
REPO_ID = 7000


def _fixture_repo_id(repo: str) -> int:
    """Stable positive GitHub-style repository id for a fake full_name."""
    if repo == REPO:
        return REPO_ID
    digest = hashlib.sha256(repo.encode("utf-8")).digest()
    return 10_000 + int.from_bytes(digest[:6], "big")


def make_db(role):
    def run(sql, args=()):
        conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    return run


def seed_processing_delivery(dsn, event_type, payload, delivery_key):
    """Mimic DurableDeliveryQueue's already-claimed row before calling make_db_processor directly."""
    installation = payload.get("installation") if isinstance(payload, dict) else {}
    installation = installation if isinstance(installation, dict) else {}
    account = installation.get("account") if isinstance(installation.get("account"), dict) else {}
    account_key = account.get("id")
    stored = dict(payload)
    stored.pop("_veripsa_delivery_key", None)
    conn = psycopg2.connect(dsn.replace("veripsa_app@", "veripsa_migrator@"))
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "INSERT INTO core.webhook_delivery("
                "delivery_key,event_type,account_key,payload,status,received_at) "
                "VALUES (%s,%s,%s,%s::jsonb,'processing',clock_timestamp())",
                (delivery_key, event_type, str(account_key) if account_key is not None else None,
                 json.dumps(stored)),
            )
    finally:
        conn.close()
    payload["_veripsa_delivery_key"] = delivery_key
    return payload


def _json_scalar(v):
    """A db() scalar that is a jsonb function result — psycopg2 adapts jsonb → dict, but a str (no adapter) is
    also tolerated. Returns a dict (or {} for None)."""
    if v is None:
        return {}
    return v if isinstance(v, dict) else json.loads(v)


class FakeGitHub:
    """Records what the App WOULD post, and serves PR files + a repo tarball from the sample_app fixture."""
    def __init__(self, files_by_pr, open_prs=None, owner_id=4242):
        self.files_by_pr = files_by_pr
        self.open_prs = open_prs or []
        self.checks, self.comments = [], []
        self.check_patches = []
        self.patches = []
        self.installations = []
        self.owner_id = owner_id            # the installation's owning-account id (boot reconcile keys the tenant by it)
        self.pr_objects = {}                          # number → authoritative PR object (for get_pull_request / the rerun path)
        self._comment_id = 1000
        self._check_id = 2000

    def for_installation(self, installation_id):
        self.installations.append(str(installation_id))
        return self

    def app_installation_identity(self, installation_id):
        return {"installation_id": str(installation_id), "account_id": str(self.owner_id),
                "created_at": "2026-01-01T00:00:00Z", "suspended": False}

    def app_account_installation_identity(self, account_id):
        return None

    def installation_account_id(self):
        """The owning-account id the boot self-heal pins its tenant by — the SAME id the live path reads from
        repository.owner.id, so boot lands in the identical ACCT-GH-<owner_id>. (Real client: github_rest.py.)"""
        return str(self.owner_id) if self.owner_id is not None else None

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return self.files_by_pr.get(number, [])

    def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
        files = list(self.files_by_pr.get(number, []))
        return {"changed": files, "changed_ranges": {p: [] for p in files},
                "added_paths": [], "conflict_markers": [], "raw_entry_count": len(files)}

    def post_check(self, repo, sha, conclusion, title, summary):
        self._check_id += 1
        self.checks.append({"id": self._check_id, "sha": sha, "conclusion": conclusion, "title": title, "summary": summary, "name": "Veripsa"})

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, check_run_id, conclusion, title, summary):
        for c in self.checks:
            if c["id"] == check_run_id:
                c.update({"conclusion": conclusion, "title": title, "summary": summary})
                self.check_patches.append(check_run_id)
                return c
        raise AssertionError(f"check not found: {check_run_id}")

    def upsert_check(self, repo, sha, conclusion, title, summary):
        existing = self.list_check_runs(repo, sha)
        if existing:
            cur = existing[0]                       # NO-CHURN (mirrors github_rest.upsert_check): skip a PATCH
            if (cur.get("conclusion") == conclusion and (cur.get("title") or "") == (title or "")
                    and (cur.get("summary") or "") == (summary or "")):
                return cur                          # identical check → no patch (not counted in check_patches)
            return self.patch_check(repo, cur["id"], conclusion, title, summary)
        return self.post_check(repo, sha, conclusion, title, summary)

    def post_comment(self, repo, number, body):
        self._comment_id += 1
        self.comments.append({"id": self._comment_id, "number": number, "body": body, "user": {"type": "Bot"}})

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def patch_comment(self, repo, comment_id, body):
        for c in self.comments:
            if c["id"] == comment_id:
                c["body"] = body
                self.patches.append(comment_id)
                return c
        raise AssertionError(f"comment not found: {comment_id}")

    def upsert_comment(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"] or (c["body"].startswith("### Veripsa") and c.get("user", {}).get("type") == "Bot"):
                if c["body"] == body:               # NO-CHURN (mirrors github_rest.upsert_comment): identical
                    return c                        # body → no patch (not counted in self.patches = no spam)
                return self.patch_comment(repo, c["id"], body)
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                new_body = body() if callable(body) else body
                if c["body"] != new_body:           # NO-CHURN: skip the patch when the body is unchanged
                    self.patch_comment(repo, c["id"], new_body)
                return True
        return False

    def list_open_pull_requests(self, repo, limit=None):
        raw = self.open_prs if limit is None else self.open_prs[:limit]
        # GitHub's list-PR response carries the same base SHA/repository identity needed to authenticate a
        # synthetic replay.  Normalize the compact scenario declarations through get_pull_request instead of
        # teaching production code to accept the old name-only test shorthand.
        return [self.get_pull_request(repo, pr.get("number")) if isinstance(pr, dict) and pr.get("number") is not None
                else pr for pr in raw]

    def list_open_pull_requests_for_head(self, repo, head_branch, base=None, limit=None):
        out = []
        for pr in self.open_prs:
            pr_base = (pr.get("base") or {}).get("ref")
            pr_head = pr.get("head") or {}
            pr_head_repo = pr_head.get("repo") or {}
            if base and pr_base != base:
                continue
            if pr_head.get("ref") != head_branch:
                continue
            if pr_head_repo.get("full_name") and pr_head_repo.get("full_name") != repo:
                continue
            out.append(pr)
            if limit is not None and len(out) >= limit:
                break
        return out

    def pull_request_head(self, repo, number):
        return self.get_pull_request(repo, number)["head"]["sha"]

    def get_pull_request(self, repo, number):
        # The AUTHORITATIVE PR object (mirrors github_rest.get_pull_request): the check-rerun path re-fetches the
        # real PR rather than trusting a sparse check ref. Tests register the truth in self.pr_objects[number];
        # absent → a default open PR on main keyed off the number (head sha = number, no recorded author).
        raw = self.pr_objects.get(number)
        if raw is None:
            raw = next((pr for pr in self.open_prs
                        if isinstance(pr, dict) and pr.get("number") == number), {})
        files = self.files_by_pr.get(number, [])
        repo_id = _fixture_repo_id(repo)
        base_raw = raw.get("base") if isinstance(raw.get("base"), dict) else {}
        head_raw = raw.get("head") if isinstance(raw.get("head"), dict) else {}
        out = dict(raw)
        out.update({"number": raw.get("number", number),
                    "changed_files": raw.get("changed_files", max(1, len(files))),
                    "state": raw.get("state", "open"), "draft": raw.get("draft", False),
                    "merged": raw.get("merged", False), "user": raw.get("user") or {}})
        out["base"] = {**base_raw, "ref": base_raw.get("ref", "main"), "sha": base_raw.get("sha", SHA),
                       "repo": base_raw.get("repo") or {"id": repo_id, "full_name": repo}}
        out["head"] = {**head_raw, "sha": head_raw.get("sha", f"{number:040x}"),
                       "repo": head_raw.get("repo") or {"id": repo_id, "full_name": repo}}
        return out

    def repo_default_branch_head(self, repo):
        return "main", "c" * 40

    def repo_default_branch_name(self, repo):
        return "main"

    def repo_current_identity(self, repo):
        return {"id": _fixture_repo_id(repo), "owner_id": self.owner_id, "full_name": repo}

    def compare_changed_paths_strict(self, repo, base_sha, head_ref):
        return []

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        src = os.path.join(ROOT, "tests", "fixtures", "sample_app")
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(src, arcname="acme-app-" + sha[:7])     # GitHub nests under one top dir
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        """ONE file's bytes from the sample_app fixture (the incremental-ingest per-file fetch), or None if
        the path does not exist there (the App treats a 404 as 'deleted at this sha')."""
        full = os.path.join(ROOT, "tests", "fixtures", "sample_app", path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()

    def target_file_modes(self, repo, ref, paths):
        """Exact target-tree mode proof required before the incremental path reads fixture bytes."""
        entries = {}
        fixture = os.path.join(ROOT, "tests", "fixtures", "sample_app")
        for path in paths:
            full = os.path.join(fixture, path)
            if os.path.isfile(full):
                entries[path] = {"mode": "100644", "type": "blob"}
        return {
            "complete": True,
            "truncated": False,
            "malformed": False,
            "over_cap": False,
            "entries": entries,
        }


def pr_payload(action, number, author, head_sha=SHA, merged=False):
    if head_sha == SHA:
        head_sha = f"{number:040x}"
    return {"action": action, "number": number,
            "installation": {"id": 4242},
            # repository.owner.id is the STABLE tenant key the live path routes by (→ ACCT-GH-4242). A real GitHub PR
            # payload always carries it; include it here so make_db_processor routes by the HONEST first-choice source
            # (not the dropped installation.id fallback — see OWNER_ID). owner.id == install id == 4242 by design.
            "repository": {"full_name": REPO, "default_branch": "main", "id": REPO_ID,
                           "owner": {"id": OWNER_ID, "login": "acme"}},
            # A live pull_request payload authenticates the protected-branch graph coordinate with both the
            # base ref and its exact commit SHA.  Keep the fixture equally strict: synthesizing a missing SHA in
            # production would let a structural event enqueue work for a guessed/stale repository generation.
            "pull_request": {"base": {"ref": "main", "sha": SHA,
                                      "repo": {"id": REPO_ID, "full_name": REPO}},
                             "head": {"sha": head_sha, "repo": {"id": REPO_ID, "full_name": REPO}},
                             "user": {"login": author}, "merged": merged}}


def _try_put(fq, item) -> bool:
    """put_nowait wrapper for the fairness checks: True if enqueued, False if the queue/account cap rejected it
    (the exact True/False contract EventQueue.submit exposes to do_POST → 202 vs 503)."""
    import queue as _queue
    try:
        fq.put_nowait(item)
        return True
    except _queue.Full:
        return False
