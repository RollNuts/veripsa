#!/usr/bin/env python3
"""Veripsa GitHub App — the INSTALLATION / ACCOUNT-RESOLUTION surface of the GitHub REST client.

Split OUT of github_rest.py's ~30-method GitHubREST god-class (Veripsa's own split_candidates flags it a
structural hotspot — fan_in 15, every caller couples to it): the methods that ENUMERATE this App's
installations and RESOLVE a Veripsa account_id → the per-tenant client that owns it — the multi-tenant
background-loop substrate — are a cohesive group. Continues the same finer-files=finer-collision-point
leaf discipline as github_rest_prread / github_rest_prsurface / github_rest_contentfetch.

The group, and the ONE thing each does:
  • list_app_installations  — GET /app/installations (App-JWT): a bounded [{installation_id,
    account_id}] resolver inventory for legacy/account lookup. Caps + alerts on saturation; production
    boot_reconcile walks its durable DB route cursor instead of treating this list as complete.
  • app_account_installation_identity — a proof-only, complete App-installations scan keyed by GitHub's stable
    account id.  Lifecycle delete/suspend uses it so an account rename cannot turn a replacement into absence.
  • for_account             — Veripsa account_id (`ACCT-GH-<owner id>`) → the per-tenant client (via
    for_installation), so a background loop can call GitHub through the installation that actually owns a
    coordinate (the multi-installation self-heal).
  • _rebuild_account_install_map — (re)builds the cached {gh-account-id: installation-id} map for_account
    reads, fail-soft (a transient list error keeps the prior map, never caches a partial/empty one).
  • app_installations_reachability — the watchdog-safe App-JWT reachability sample, reusing the SAME install-map
    attempt timestamp + TTL so the watchdog cannot add a second /app/installations storm during a 403.
  • installation_account_id  — the OWNING-account id of THIS installation (read off the first repo of
    GET /installation/repositories) — the STABLE tenant key boot/live both pin to.
  • installation_repos       — bounded repo full_names THIS installation can see (legacy/operator compatibility;
    production boot reconcile uses durable DB lifecycle routes).
  • repo_default_branch_head — (default_branch, head_sha) for a repo — backfill_repo's baseline-graph anchor.

These depend ONLY on seams the host class provides — `self._req`, `self._jwt`, `self._api`,
`self.for_installation`, and the `ACCOUNT_PREFIX` / `ACCOUNT_MAP_TTL_SECONDS` class attrs — all resolved via
MRO from GitHubREST, which inherits this mixin. They also read two MODULE-LEVEL knobs that live in
github_rest.py (`_APP_INSTALLATIONS_CAP`, `_alert_sink`); those STAY there (the alert sink is a module
singleton; the cap is read at call-time), resolved lazily through the same `import github_rest` seam
github_rest_contentfetch / github_rest_prread use, to avoid the mutual-import cycle (github_rest imports this
module, so a top-level cross-import would form a cycle at load).

Behaviour-preserving: callers use GitHubREST.<method> unchanged (inherited); a test patching
GitHubREST._urlopen/_sleep/_jwt or GitHubREST.installation_repos (class-level) still works — the seams stay on
GitHubREST (or resolve from it via MRO no matter which class defines the calling method), and a class-level
patch on GitHubREST overrides the mixin in MRO exactly as before. Content-free (ids/paths/counts only)."""
from __future__ import annotations

from datetime import datetime


class InvalidDeliveryCursor(RuntimeError):
    """GitHub rejected or malformed an opaque App-delivery pagination cursor."""


def _bounded_proof_id(value, label: str) -> str:
    if value in (None, "") or isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        raise RuntimeError(f"{label} omitted or malformed")
    text = str(value).strip()
    if not text or len(text) > 64:
        raise RuntimeError(f"{label} omitted or malformed")
    return text


def _iso_created_at(value, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} omitted or malformed")
    text = value.strip()
    if not text or len(text) > 80:
        raise RuntimeError(f"{label} omitted or malformed")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError as exc:
        raise RuntimeError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{label} must include a timezone")
    return text


def _installation_identity(raw, context: str) -> dict:
    if not isinstance(raw, dict):
        raise RuntimeError(f"{context} returned malformed authority")
    account = raw.get("account")
    if not isinstance(account, dict):
        raise RuntimeError(f"{context} omitted account authority")
    if "suspended_at" not in raw:
        raise RuntimeError(f"{context}.suspended_at omitted or malformed")
    suspended_at = raw.get("suspended_at")
    if suspended_at is not None:
        _iso_created_at(suspended_at, f"{context}.suspended_at")
    return {
        "installation_id": _bounded_proof_id(raw.get("id"), f"{context}.id"),
        "account_id": _bounded_proof_id(account.get("id"), f"{context}.account.id"),
        "created_at": _iso_created_at(raw.get("created_at"), f"{context}.created_at"),
        "suspended": suspended_at is not None,
    }


def _gh_installs_helpers():
    """Lazy import of the two module-level knobs that live in github_rest.py. Called at use-time rather than
    module-load-time to avoid the mutual import cycle: github_rest imports this module, so a top-level
    `from github_rest import …` here would cause a circular import error on Python's first pass."""
    try:
        import github_rest as _gr
    except ImportError:                       # imported as a package
        from . import github_rest as _gr
    return _gr._APP_INSTALLATIONS_CAP, _gr._alert_sink


class _GitHubInstallationsMixin:
    """Installations enumeration · account→installation resolution · installation/repo metadata. The host class
    MUST provide self._req / self._jwt / self._api / self.for_installation and the ACCOUNT_PREFIX +
    ACCOUNT_MAP_TTL_SECONDS class attrs — GitHubREST does. This class is never instantiated on its own."""

    @staticmethod
    def _app_delivery_next_cursor(link_header: str) -> str | None:
        """Extract only the opaque ``cursor`` from Link's rel=next URL; never persist/log the full URL."""
        import urllib.parse
        if not link_header:
            return None
        for part in str(link_header).split(","):
            if 'rel="next"' not in part and "rel=next" not in part:
                continue
            left, right = part.find("<"), part.find(">")
            if left < 0 or right <= left:
                raise InvalidDeliveryCursor("GitHub delivery next link is malformed")
            values = urllib.parse.parse_qs(
                urllib.parse.urlparse(part[left + 1:right]).query, keep_blank_values=True).get("cursor")
            if not values or len(values) != 1 or not values[0] or len(values[0]) > 2048:
                raise InvalidDeliveryCursor("GitHub delivery next link omitted its cursor")
            return values[0]
        return None

    def list_app_hook_deliveries(
        self,
        cursor: str | None = None,
        per_page: int = 100,
        *,
        include_redelivery: bool = False,
    ) -> dict:
        """List App webhook delivery *metadata* only.  Never calls the per-delivery detail/body endpoint."""
        import urllib.error
        import urllib.parse
        if isinstance(per_page, bool) or not isinstance(per_page, int) or not 1 <= per_page <= 100:
            raise ValueError("GitHub delivery page size must be in 1..100")
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048):
            raise InvalidDeliveryCursor("GitHub delivery cursor is malformed")
        if not isinstance(include_redelivery, bool):
            raise ValueError("include_redelivery must be a boolean")
        url = f"/app/hook/deliveries?per_page={per_page}"
        if cursor is not None:
            url += "&cursor=" + urllib.parse.quote(cursor, safe="")
        try:
            raw, link = self._req_with_link("GET", url, self._jwt())
        except urllib.error.HTTPError as exc:
            # GitHub documents both 400 and 422 for this cursor endpoint. Either
            # response against a stored opaque cursor invalidates that resume
            # point; restart from page one without claiming from a partial scan.
            if exc.code in (400, 422) and cursor is not None:
                raise InvalidDeliveryCursor("GitHub rejected the delivery cursor") from exc
            raise
        if not isinstance(raw, list) or len(raw) > per_page:
            raise RuntimeError("GitHub delivery list returned malformed pagination metadata")
        # Minimize immediately: event/action/install/repository metadata from the list is unnecessary, and a body
        # is not present on this endpoint.  The recovery boundary validates each retained field before DB storage.
        deliveries = []
        for item in raw:
            if not isinstance(item, dict):
                raise RuntimeError("GitHub delivery list returned a malformed item")
            fields = ["id", "guid", "delivered_at", "status_code", "status"]
            if include_redelivery:
                fields.append("redelivery")
            deliveries.append({k: item.get(k) for k in fields})
        return {"deliveries": deliveries, "next_cursor": self._app_delivery_next_cursor(link)}

    def redeliver_app_hook_delivery_once(
        self, delivery_id: int, *, app_jwt: str | None = None
    ) -> int:
        """Issue exactly one App-JWT redelivery POST, with no implicit retry on an ACK-ambiguous failure."""
        import urllib.request
        if isinstance(delivery_id, bool) or not isinstance(delivery_id, int) or delivery_id <= 0:
            raise ValueError("GitHub delivery id must be a positive integer")
        token = self._jwt() if app_jwt is None else app_jwt
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise ValueError("GitHub App JWT is malformed")
        req = urllib.request.Request(
            self.API + f"/app/hook/deliveries/{delivery_id}/attempts", data=b"", method="POST")
        # The shared transport follows same-origin redirects for ordinary reads.
        # A redelivery mutation cannot do that: 307/308 would issue a second
        # POST after the first request's acknowledgement became ambiguous.
        setattr(req, "_veripsa_no_redirect", True)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "Veripsa-GitHub-App")
        # Deliberately bypass _req: it retries 5xx/network failures, which is unsafe after an ambiguous POST.
        with self._urlopen(req) as response:
            raw_status = getattr(response, "status", None)
            if isinstance(raw_status, bool) or not isinstance(raw_status, int):
                raise RuntimeError("GitHub redelivery response omitted its status")
            status = raw_status
            response.read(1)  # response is empty for 202; bounded by the shared socket timeout
            return status

    def list_app_installations(self, cap: int | None = None) -> list[dict]:
        """Every INSTALLATION of THIS App, as a content-free [{installation_id, account_id}] list — authed with
        the App JWT (NOT an installation token: GET /app/installations is an APP-level endpoint). This is the
        AUTHORITATIVE installation↔account map the background loops need: a coordinate carries the tenant's
        OWNING-account id (repository.owner.id, the SAME id _event_account_key keys by), and each installation
        entry pairs that account id (`account.id`) with the GitHub INSTALLATION id (`id`) — the value
        for_installation() needs to mint the right per-tenant token. We read it straight from GitHub (not from
        core.installation_account, which stores the owner id under its `installation_id` column — NOT the GitHub
        installation id for_installation requires) so the resolver is correct by construction. Content-free: only
        the two ids per installation (the same id class as the install id the webhook already trusts). Bounded by
        `cap` (defaults to VERIPSA_APP_INSTALLATIONS_CAP, default 500; raise the knob for a larger fleet).
        ALERT: if the result reaches the cap the App may be TRUNCATING further tenants — a content-free WARNING
        fires (count + knob name) so the operator knows to raise VERIPSA_APP_INSTALLATIONS_CAP. per_page=100, paginated."""
        _APP_INSTALLATIONS_CAP, _alert_sink = _gh_installs_helpers()
        if cap is None:
            cap = _APP_INSTALLATIONS_CAP
        out, page, raw_seen = [], 1, 0
        complete = False
        malformed = False
        while raw_seen < cap:
            r = self._req("GET", f"/app/installations?per_page=100&page={page}", self._jwt())
            if not isinstance(r, list):
                raise RuntimeError(
                    "App installations endpoint returned a malformed page")
            if not r:
                complete = True
                break
            remaining = cap - raw_seen
            bounded_page = r[:remaining]
            raw_seen += len(bounded_page)
            for inst in bounded_page:
                if not isinstance(inst, dict):
                    malformed = True
                    continue
                iid = inst.get("id")
                acct = (inst.get("account") or {}).get("id") if isinstance(inst.get("account"), dict) else None
                iid = str(iid).strip() if not isinstance(iid, bool) and iid is not None else ""
                acct = str(acct).strip() if not isinstance(acct, bool) and acct is not None else ""
                if (
                    iid.isascii()
                    and iid.isdigit()
                    and not iid.startswith("0")
                    and len(iid) <= 64
                    and acct.isascii()
                    and acct.isdigit()
                    and not acct.startswith("0")
                    and len(acct) <= 64
                ):
                    out.append(
                        {"installation_id": iid, "account_id": acct})
                else:
                    malformed = True
            # A short GitHub page proves the endpoint ended even when the
            # exact configured raw-row cap was reached.  A full/cropped page
            # does not: there may be another installation, so absence remains
            # Unknown.  Count raw rows, not accepted entries; malformed rows
            # must neither defeat the I/O cap nor turn truncation into
            # authoritative absence.
            if len(r) < 100:
                complete = len(r) <= remaining
                break
            page += 1
        result = out
        complete = complete and not malformed
        self._last_app_installations_complete = complete
        self._last_app_installations_raw_count = raw_seen
        # TRUNCATION ALERT: this bounded list cannot prove an account-level resolver miss. Production
        # boot_reconcile is independent (durable DB cursor), but legacy/account lookup callers must preserve
        # Unknown. Alert content-free (count + knob name only — no tenant ids or org names).
        if not complete:
            _alert_sink.fire(
                "installations_cap_hit", "warning",
                f"App installations list hit the cap ({cap}) — account-level resolver misses are Unknown; "
                f"raise VERIPSA_APP_INSTALLATIONS_CAP or use exact installation routing",
                {"installations_count": cap, "knob": "VERIPSA_APP_INSTALLATIONS_CAP"},
            )
        else:
            _alert_sink.resolve("installations_cap_hit")
        return result

    def app_installation_identity(self, installation_id: str) -> dict | None:
        """Point-read one current App installation with App-JWT generation authority.

        A durable ``installation.created`` delivery can outlive an uninstall/GDPR erase.  Old schema versions
        deleted its idempotency row, so a later redelivery alone cannot prove it belongs to a new lifecycle.  This
        endpoint returns only the installation id, owning account id, immutable ``created_at``, and suspension bit;
        404 is authoritative absence, while permission/transient/malformed responses raise for durable retry.
        """
        import urllib.error
        import urllib.parse

        iid = str(installation_id or "").strip()
        if not iid:
            raise RuntimeError("installation generation point read needs an installation id")
        try:
            raw = self._req(
                "GET", f"/app/installations/{urllib.parse.quote(iid, safe='')}", self._jwt())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return _installation_identity(raw, "installation generation point read")

    def app_account_installation_identity(self, account_id: str, cap: int | None = None) -> dict | None:
        """Return the App's current installation for one *stable* GitHub account id.

        Destructive lifecycle deliveries can be delayed past both an account rename and a replacement install.
        GitHub's ``/{orgs|users}/{login}/installation`` lookup is therefore not absence authority: the old login in
        the durable webhook legitimately becomes 404 after a rename.  Scan ``GET /app/installations`` with App-JWT
        and match ``installation.account.id`` instead.  Only a completely consumed, well-formed scan may prove
        either a current generation or absence.  A transient response, malformed entry, duplicate account, or cap
        saturation raises so the durable delivery retries; none can be converted into destructive absence.

        The scan and its errors are content-free.  In particular no account or installation id is interpolated into
        an exception that the worker may log.
        """
        _APP_INSTALLATIONS_CAP, _alert_sink = _gh_installs_helpers()
        target = _bounded_proof_id(account_id, "current-account installation scan account.id")
        if cap is None:
            cap = _APP_INSTALLATIONS_CAP
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise RuntimeError("current-account installation scan cap is malformed")

        match = None
        seen = 0
        page = 1
        while True:
            raw_page = self._req("GET", f"/app/installations?per_page=100&page={page}", self._jwt())
            if not isinstance(raw_page, list) or len(raw_page) > 100:
                raise RuntimeError("current-account installation scan returned malformed pagination authority")
            if not raw_page:
                break

            # A page that exceeds the proof cap is not authority.  Exactly meeting the cap is still allowed: when
            # it is a full page, one final *empty-page probe* below can prove EOF without processing past the cap.
            # Any non-empty successor fails closed before its entries are consumed.
            next_seen = seen + len(raw_page)
            if seen >= cap or next_seen > cap:
                _alert_sink.fire(
                    "installations_proof_cap_hit", "warning",
                    "Current-account installation proof hit its cap before the App installation scan completed; "
                    "raise VERIPSA_APP_INSTALLATIONS_CAP",
                    {"installations_count": cap, "knob": "VERIPSA_APP_INSTALLATIONS_CAP"},
                )
                raise RuntimeError("current-account installation scan hit its configured cap before completion")

            for raw in raw_page:
                current = _installation_identity(raw, "current-account installation scan entry")
                if current["account_id"] != target:
                    continue
                if match is not None:
                    raise RuntimeError("current-account installation scan returned duplicate account authority")
                match = current
            seen += len(raw_page)
            if len(raw_page) < 100:
                break
            page += 1

        _alert_sink.resolve("installations_proof_cap_hit")
        return match

    def for_account(self, account_id: str):
        """The per-tenant client for the Veripsa ACCOUNT id `account_id` (`ACCT-GH-<owning-account id>`), or None
        if this App has no installation for that account. The resolver the BACKGROUND loops use to become
        installation-aware: a loop iterates coordinates/repos that may belong to ANY installation, but it only
        holds the SINGLETON client (pinned to the primary installation) — so it asks here for the client scoped to
        the installation that actually owns the coordinate, then calls GitHub through THAT (exactly as the webhook
        worker does with for_installation(payload.installation.id)). Without it a non-primary tenant's repos 404/403
        on the primary's token (the stale-graph / error-spam symptom this fixes).

        RESOLUTION: strip the 'ACCT-GH-' prefix to recover the bare GitHub owner id, look it up in the
        account→installation map (built ONCE from list_app_installations, then cached on this client + reused for
        every later account this pass — one App-installations read per loop, not per repo), and return
        for_installation(<that installation id>). Returns None — so the caller SKIPS the coordinate content-free
        (no 404-spam, no crash) — when: the account_id is empty/oddly-shaped, has no 'ACCT-GH-' prefix (a
        dogfood/credential-account coordinate that the singleton already serves correctly, or a non-GH account),
        OR no installation maps to it (uninstalled/never-installed). NEVER-CRASH: any error building the map
        (a transient GET /app/installations failure) caches NOTHING and returns None for this call — the caller
        skips this coordinate and the NEXT pass retries the map (fail-soft, never a crash, never a wrong token).
        Called on the singleton by the loops; siblings don't resolve accounts (they already are one installation)."""
        if not isinstance(account_id, str) or not account_id:
            return None
        prefix = self.ACCOUNT_PREFIX
        if not account_id.startswith(prefix):
            return self                                       # not a GH-installation tenant key (e.g. ACCT-DEMO / a
            # dogfood/credential-account coordinate like the self-hosted core repo): the PRIMARY (singleton)
            # installation owns these repos, so SERVE them with it — returning None here made the freshness loop
            # skip the dogfooded core coordinate as "unservable" (the post-deploy regression). The singleton is the
            # correct client for any non-ACCT-GH tenant key, exactly as this method's contract documents.
        owner_id = account_id[len(prefix):]
        if not owner_id:
            return None
        # build the map on first use; FAIL-SOFT (a transient list error leaves it None → this call returns None,
        # the caller skips the coordinate, the next pass retries). Never a partial/empty map cached on error.
        # Throttle the cold rebuild too: a sustained GET /app/installations failure
        # e.g. a GitHub secondary-rate-limit 403 — leaves the map None, and WITHOUT this guard every for_account
        # call (per repo, per freshness pass) re-attempted the list → that storm SUSTAINS the rate limit, so the
        # keeps the secondary-rate-limit response from clearing. Now
        # the cold path obeys the SAME TTL the MISS path already uses: re-attempt at most once per TTL, else fail
        # soft (caller skips the coordinate). Stopping the storm lets the secondary limit expire → the 403 self-heals.
        if self._account_install_map is None:
            import time as _t
            # `_account_install_map_at` is 0.0 until the FIRST attempt (the "never" sentinel) — must NOT be read as
            # "attempted at t=0" (on a freshly-booted box monotonic() can itself be < TTL, which would wrongly
            # throttle the very first build). So only throttle once a real attempt has been stamped.
            if self._account_install_map_at and _t.monotonic() - self._account_install_map_at < self.ACCOUNT_MAP_TTL_SECONDS:
                return None                                   # attempted recently + still blind — don't re-storm
            if not self._rebuild_account_install_map():
                return None
        iid = self._account_install_map.get(owner_id)
        if iid is None:
            # MISS: maybe a freshly-installed tenant the cached map predates. Rebuild ONCE (throttled to the TTL so
            # a genuinely-orphaned account — always a miss — can't storm /app/installations every tick) and retry.
            import time as _t
            if _t.monotonic() - self._account_install_map_at >= self.ACCOUNT_MAP_TTL_SECONDS:
                if self._rebuild_account_install_map():
                    iid = self._account_install_map.get(owner_id)
        if iid is None:
            return None                                       # no installation for this account (uninstalled/unknown)
        return self.for_installation(iid)

    def _rebuild_account_install_map(self) -> bool:
        """(Re)build the {gh-account-id: installation-id} map from GET /app/installations and stamp the build time.
        Returns True on success (map replaced atomically), False on a transient list error (the OLD map — possibly
        None — is KEPT, never overwritten with a partial/empty one; for_account then fails soft). The stamp is set
        even on a successful empty result (an App with zero installations) AND on FAILURE (2026-06-26) so the TTL
        throttle applies to failed attempts too — a persistent failure must not be re-stormed on every for_account."""
        import time as _t
        try:
            cap, _alert_sink = _gh_installs_helpers()
            installations = self.list_app_installations()
            new_map = {e["account_id"]: e["installation_id"]
                       for e in installations
                       if e.get("account_id") and e.get("installation_id")}
            now = _t.monotonic()
            self._account_install_map = new_map
            self._account_install_map_at = now
            self._account_install_map_reachable = True
            # Saturation is reachable-but-incomplete, never authoritative
            # absence. Hits remain safe; a miss must retry or use the durable
            # installation id carried by the scheduler claim.
            observed_complete = getattr(
                self, "_last_app_installations_complete", None)
            self._account_install_map_complete = (
                observed_complete
                if isinstance(observed_complete, bool)
                else len(installations) < cap
            )
            self._account_install_map_last_ok_at = now
            self._account_install_map_last_error = ""
            self._account_install_map_count = len(new_map)
            return True
        except Exception as e:
            # STAMP THE ATTEMPT even on failure → the cold + miss paths both throttle to ACCOUNT_MAP_TTL_SECONDS,
            # so a sustained 403 can't storm /app/installations (the self-perpetuating-rate-limit loop). At most one
            # attempt — and thus one ALERT line — per TTL. ALERT[ prefix so a log-drain alert fires on the systemic
            # install-blindness (all background loops go blind for non-primary tenants until it clears). Content-free:
            # error class + truncated message only; no tenant ids, repo names, or token material.
            now = _t.monotonic()
            self._account_install_map_at = now
            self._account_install_map_reachable = False
            self._account_install_map_complete = False
            self._account_install_map_last_error_at = now
            self._account_install_map_last_error = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"ALERT[warning] app_installations_unreachable: could not list App installations "
                  f"({str(e)[:120]}) — keeping the prior map; non-primary tenants unservable until it clears", flush=True)
            return False

    def app_installations_reachability(self) -> dict:
        """Watchdog-safe App-JWT reachability sample for GET /app/installations.

        This deliberately reuses the account-install map's `_account_install_map_at` + ACCOUNT_MAP_TTL_SECONDS
        instead of calling list_app_installations() directly. The freshness watchdog runs after graph freshness,
        and graph freshness may already have called for_account(); on a persistent /app/installations 403, a raw
        watchdog probe would add a SECOND attempt in the same tick and bypass #534's storm throttle. This helper
        returns the cached last observation inside the TTL, and performs at most one rebuild when never attempted
        or expired. Content-free: booleans, counts, monotonic timestamps, and a truncated error class/message only."""
        import time as _t

        attempted_at = getattr(self, "_account_install_map_at", 0.0) or 0.0
        reachable = getattr(self, "_account_install_map_reachable", None)
        now = _t.monotonic()
        if not attempted_at or reachable is None or now - attempted_at >= self.ACCOUNT_MAP_TTL_SECONDS:
            self._rebuild_account_install_map()
            attempted_at = getattr(self, "_account_install_map_at", 0.0) or 0.0
            reachable = getattr(self, "_account_install_map_reachable", None)
        return {
            "reachable": reachable,
            "complete": getattr(
                self, "_account_install_map_complete", None),
            "installations_count": getattr(self, "_account_install_map_count", None),
            "attempted_at": attempted_at,
            "last_ok_at": getattr(self, "_account_install_map_last_ok_at", 0.0),
            "last_error_at": getattr(self, "_account_install_map_last_error_at", 0.0),
            "last_error": getattr(self, "_account_install_map_last_error", ""),
        }

    def installation_account_id(self) -> str | None:
        """The OWNING ACCOUNT (org/user) id of THIS installation — the STABLE tenant key. An installation belongs
        to exactly ONE account, so every repo it can see carries the SAME owner; we read it from the first repo of
        GET /installation/repositories (owner.id). Returned as a str so it matches EXACTLY what the live webhook
        path keys the tenant by: _event_account_key uses repository.owner.id (server.py), and
        enter_installation_with_authority derives the account as 'ACCT-GH-'||<that id>. The boot self-heal has no
        webhook payload (no repository.owner.id), so it resolves the same id HERE and pins the same tenant — boot
        and live therefore land in the IDENTICAL ACCT-GH-<owner_id> (never a split tenant). per_page=1: we only
        need one repo to read the owner. None when the installation can see no repos (nothing to reconcile anyway)
        or the owner id is absent (malformed response) → the caller fails closed (skips that repo) rather than
        mis-routing. Same stable-id rationale as _event_account_key: keyed by the account, NOT the ephemeral
        installation.id, so it survives uninstall/reinstall."""
        r = self._api("GET", "/installation/repositories?per_page=1")
        repos = r.get("repositories", []) if isinstance(r, dict) else []
        if not repos:
            return None
        oid = (repos[0].get("owner") or {}).get("id") if isinstance(repos[0], dict) else None
        return str(oid) if oid not in (None, "") else None

    def installation_repo_entries(self, cap: int = 200) -> list[dict]:
        """Current installation repositories as bounded ``{full_name, id?}`` metadata.

        Account-level install events need the stable repository id to distinguish an existing selected repository
        from a stale same-name object. Keep the id alongside the full name for those lifecycle callers; no source,
        diff, or repository body leaves GitHub. Older name-only callers use ``installation_repos`` below.
        """
        out, page = [], 1
        while len(out) < cap:
            r = self._api("GET", f"/installation/repositories?per_page=100&page={page}")
            repos = r.get("repositories", []) if isinstance(r, dict) else []
            for raw in repos:
                if not isinstance(raw, dict) or not raw.get("full_name"):
                    continue
                entry = {"full_name": raw["full_name"]}
                if raw.get("id") not in (None, ""):
                    entry["id"] = raw["id"]
                out.append(entry)
            if len(repos) < 100:                                 # last page
                break
            page += 1
        return out[:cap]

    def installation_repos(self, cap: int = 200) -> list[str]:
        """The repo full_names THIS installation can see, retained for boot-reconcile compatibility."""
        return [entry["full_name"] for entry in self.installation_repo_entries(cap=cap)]

    def repo_current_identity(self, repo: str) -> dict | None:
        """Current installation-visible ``{full_name, id, owner_id}``, or ``None`` on a real 404.

        This bounded lifecycle read resolves a pre-fix deletion that omitted repository.id and proves the current
        owner of a delayed cross-account transfer. An installation token can access only repositories granted to
        that installation, so a visible object is current authority; a removed/deleted object returns 404.
        Permission/transient failures are re-raised. Content-free metadata only; no source or diff is retained.
        """
        import urllib.error
        try:
            raw = self._api("GET", f"/repos/{repo}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        if not isinstance(raw, dict):
            raise RuntimeError("repository identity point read returned a malformed response")
        full_name = raw.get("full_name")
        repository_id = raw.get("id")
        owner_id = raw.get("owner", {}).get("id") if isinstance(raw.get("owner"), dict) else None
        if (not isinstance(full_name, str) or not full_name
                or repository_id in (None, "") or owner_id in (None, "")):
            raise RuntimeError("repository identity point read omitted full_name, id, or owner.id")
        return {"full_name": full_name[:512], "id": repository_id, "owner_id": owner_id}

    def repo_current_identity_via_app_installation(self, repo: str) -> dict | None:
        """Resolve an onward-transferred private repository through App-level installation authority.

        An A→B transfer delivery runs with B's installation token. If the repository already moved B→C, a
        private C repository correctly returns 404 to B even though this App is installed for C and received that
        later transfer. On that narrow 404 fallback, App JWT asks GitHub which installation currently owns the
        repository, then the corresponding installation token performs the same bounded identity read. No App-JWT
        repository body is read. A real App-level 404 means this App has no current installation and returns None;
        malformed/transient responses retry. If installation resolution succeeds but the scoped token then 404s,
        treat the two-read race as retryable rather than falsely confirming absence.
        """
        import urllib.error
        try:
            installation = self._req("GET", f"/repos/{repo}/installation", self._jwt())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        if not isinstance(installation, dict) or installation.get("id") in (None, ""):
            raise RuntimeError("repository installation point read returned malformed authority")
        current = self.for_installation(str(installation["id"])).repo_current_identity(repo)
        if current is None:
            raise RuntimeError("repository installation changed during private identity proof")
        return current

    def repo_onboarding_head_info(self, repo: str) -> dict:
        """Return canonical identity plus a strict HEAD/empty proof for durable onboarding.

        GitHub GraphQL's explicit ``Repository.isEmpty`` is the only empty proof. REST ``size`` is storage in
        kilobytes and is never treated as emptiness. In every non-empty case the default branch must also resolve
        successfully; REST/GraphQL 404/409/410 is intentionally propagated as ambiguity.
        """
        import urllib.parse
        raw = self._api("GET", f"/repos/{repo}")
        if not isinstance(raw, dict):
            raise RuntimeError("onboarding repository metadata returned a malformed response")
        full_name = raw.get("full_name")
        repository_id = raw.get("id")
        owner_id = raw.get("owner", {}).get("id") if isinstance(raw.get("owner"), dict) else None
        branch = raw.get("default_branch")
        if (not isinstance(full_name, str) or not full_name
                or repository_id in (None, "") or owner_id in (None, "")
                or not isinstance(branch, str) or not branch):
            raise RuntimeError("onboarding repository metadata omitted canonical authority")
        common = {
            "full_name": full_name[:512],
            "repository_id": repository_id,
            "owner_id": owner_id,
            "default_branch": branch[:512],
        }
        owner_name, separator, repo_name = full_name.partition("/")
        if not separator or not owner_name or not repo_name:
            raise RuntimeError("onboarding canonical repository name was malformed")
        proof = self._api(
            "POST", "/graphql",
            {
                "query": (
                    "query($owner:String!,$name:String!){"
                    "repository(owner:$owner,name:$name){databaseId nameWithOwner isEmpty}}"
                ),
                "variables": {"owner": owner_name, "name": repo_name},
            },
        )
        if (not isinstance(proof, dict) or proof.get("errors")
                or not isinstance(proof.get("data"), dict)
                or not isinstance(proof["data"].get("repository"), dict)):
            raise RuntimeError("onboarding empty proof was unavailable")
        repository = proof["data"]["repository"]
        is_empty = repository.get("isEmpty")
        if (repository.get("databaseId") != repository_id
                or repository.get("nameWithOwner") != full_name
                or not isinstance(is_empty, bool)):
            raise RuntimeError("onboarding empty proof did not match REST authority")
        if is_empty:
            return {**common, "head_sha": None, "empty": True}
        encoded = urllib.parse.quote(branch, safe="/")
        branch_raw = self._api("GET", f"/repos/{full_name}/branches/{encoded}")
        if not isinstance(branch_raw, dict):
            raise RuntimeError("onboarding default branch returned a malformed response")
        commit = branch_raw.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not sha:
            raise RuntimeError("onboarding default branch omitted HEAD")
        return {**common, "head_sha": sha, "empty": False}

    def repo_branch_head(self, repo: str, branch: str) -> str:
        """One authoritative point read for a known repository branch.

        Durable onboarding uses this once in each fair planned-PR turn, after the current PR object identifies
        the repository's current default branch. Keeping the point read separate from full REST+GraphQL onboarding
        discovery prevents a stale frozen target without repeating the heavier identity/empty proof every turn.
        """
        import urllib.parse
        if (not isinstance(repo, str) or not repo or len(repo) > 512
                or not isinstance(branch, str) or not branch or len(branch) > 512):
            raise ValueError("repository branch coordinate is malformed")
        encoded = urllib.parse.quote(branch, safe="/")
        raw = self._api("GET", f"/repos/{repo}/branches/{encoded}")
        if not isinstance(raw, dict):
            raise RuntimeError("repository branch returned a malformed response")
        commit = raw.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not sha:
            raise RuntimeError("repository branch omitted HEAD")
        return sha

    def repo_default_branch_head_info_at(self, repo: str):
        """(default_branch, head_sha, canonical_full_name, head_committed_at) for a repo.

        GitHub redirects old full_names after owner/repo renames and returns the current `full_name` in the repo
        body. Freshness uses that canonical name to identify renamed-away coordinates that should be treated as
        orphan surface rows, while the legacy two-value wrapper below keeps existing callers stable.
        """
        import urllib.parse
        r = self._api("GET", f"/repos/{repo}")
        branch = r.get("default_branch") or "main"
        canonical = r.get("full_name") or repo
        # URL-encode the branch before interpolating it into the path: a default-branch name can carry
        # URL-significant characters (?, #, %, space) that would otherwise alter or break the request. safe="/"
        # (NOT "" as in compare_changed_paths) because here the branch is the TERMINAL path segment of
        # /branches/{branch}, which GitHub matches with LITERAL slashes (an encoded %2F 404s) — whereas compare's
        # refs are infix around `...` and must encode the slash. Defence-in-depth: branch comes from GitHub's own
        # API, but a customer controls their own default-branch name, so never trust it into a URL raw.
        bq = urllib.parse.quote(branch, safe="/")
        b = self._api("GET", f"/repos/{repo}/branches/{bq}")
        commit = (b.get("commit") or {}) or {}
        sha = commit.get("sha") or ""
        # The head commit's TIME is already in this same response and used to be discarded. It is the
        # delivery-order clock a payload-less re-ingest (self-heal / backfill) needs so its write cannot erase
        # the stored clock and disarm the reordered-delivery guard. Public git metadata, content-free, and FREE
        # here — reading it costs no extra GitHub call. Committer date (not author date): it is the one that
        # advances on rebase/cherry-pick, so it orders DELIVERY, which is what the guard compares.
        inner = (commit.get("commit") or {}) or {}
        committed_at = ((inner.get("committer") or {}) or {}).get("date")
        if not isinstance(committed_at, str) or not committed_at:
            committed_at = None
        return branch, sha, canonical, committed_at

    def repo_default_branch_name(self, repo: str) -> str:
        """Return GitHub's authoritative non-empty default-branch name.

        Unlike ``repo_default_branch_head_info`` this strict metadata point never guesses ``main``. Branch-lane
        reconciliation is a release-by-difference operation, so an absent/malformed default branch is uncertainty
        and must skip every release. A successful repository response with a concrete string is the only evidence
        accepted by that path. No branch contents are read.
        """
        raw = self._api("GET", f"/repos/{repo}")
        if not isinstance(raw, dict):
            raise RuntimeError("repository metadata returned a non-object")
        branch = raw.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise RuntimeError("repository metadata omitted default_branch")
        return branch

    def repo_default_branch_head_info(self, repo: str):
        """(default_branch, head_sha, canonical_full_name) — the long-standing 3-value shape every existing
        caller unpacks. Delegates to the 4-value variant so there is exactly ONE GitHub read and one parse."""
        branch, sha, canonical, _at = self.repo_default_branch_head_info_at(repo)
        return branch, sha, canonical

    def repo_default_branch_head(self, repo: str):
        """(default_branch, head_sha) for a repo — what backfill_repo ingests as main's baseline graph."""
        branch, sha, _canonical = self.repo_default_branch_head_info(repo)
        return branch, sha
