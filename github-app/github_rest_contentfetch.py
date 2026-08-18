#!/usr/bin/env python3
"""Veripsa GitHub App — the CONTENT-FETCH surface of the GitHub REST client.

Split OUT of github_rest.py's ~40-method GitHubREST god-class (Veripsa's own split_candidates flags it a
structural hotspot): the methods that FETCH repo content — a single file (get_file_at), a tarball
(_tarball_fetch_once / download_tarball), and a blobless history clone (history_clone) — are the cohesive
group used exclusively by ingest / co-change, and depend ONLY on:
  • self._itoken() / self._invalidate_token_for_retry() / self._api()  — resolved via MRO from GitHubREST
  • _read_capped / _verify_complete_gzip / _HTTP_TIMEOUT               — module-level helpers in github_rest.py
    (they stay there because tests import and patch them directly on that module; the mixin resolves them
    lazily at call time via `import github_rest` / `from . import github_rest` to avoid a circular import
    at module load — github_rest imports this module, so a top-level cross-import would form a cycle)

Behaviour-preserving: callers use GitHubREST.<method> unchanged (inherited); a test patching
GitHubREST._urlopen/_sleep/_tarball_fetch_once still works — the seams stay on GitHubREST (or are resolved
from it via MRO). Content-free discipline preserved: download_tarball/history_clone are the only places file
bytes touch disk — kept exactly."""
from __future__ import annotations

import contextvars
import os
import signal
import subprocess
import threading
import time


_HISTORY_CLONE_KILL_WAIT_SECONDS = 1.0
# One blobless git child at a time per App process. Co-change normally has one scheduler worker, but sync
# backfills/tests/operator paths can overlap it. The lock covers queued+running+reaping lifetime; a timed-out
# child handed to the daemon retains the slot until wait() confirms it is actually gone.
_HISTORY_CLONE_CHILD_LOCK = threading.Lock()
# Preserve the historical two-argument `_tarball_fetch_once(repo, sha)` seam
# while binding one exact installation token to each real attempt. An instance
# attribute would race across keyed workers; ContextVar is per execution context.
_TARBALL_ATTEMPT_TOKEN = contextvars.ContextVar(
    "veripsa_tarball_attempt_token",
    default=None,
)


def _history_clone_daemon_reap(proc: subprocess.Popen) -> None:
    """Drain/reap a killed git process off the event worker."""
    try:
        try:
            proc.communicate()
        except Exception:
            try:
                proc.wait()
            except Exception:
                pass
    finally:
        _HISTORY_CLONE_CHILD_LOCK.release()


def _kill_history_clone(proc: subprocess.Popen, event_budget) -> bool:
    """Kill git's process group and bound synchronous pipe-drain/reaping.

    Returns True when synchronously reaped.  A False result means a daemon owns
    the final communicate/wait, so an uninterruptible kernel I/O state cannot
    turn timeout cleanup into another unlimited worker stall.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass

    # The work deadline has necessarily just elapsed.  Cleanup gets a separate
    # short grace outside event context; inside an event it may consume only
    # the actual remaining delivery budget.
    cleanup_seconds = _HISTORY_CLONE_KILL_WAIT_SECONDS
    event_remaining = event_budget.remaining()
    if event_remaining is not None:
        cleanup_seconds = min(cleanup_seconds, max(0.001, event_remaining))
    try:
        proc.communicate(timeout=cleanup_seconds)
        return True
    except Exception:
        # TimeoutExpired is the expected path.  Treat any other pipe-drain/wait
        # failure the same way: the absence of a successful reap means the
        # caller must not release the one-child slot yet.
        reaper = threading.Thread(
            target=_history_clone_daemon_reap,
            args=(proc,),
            name="veripsa-history-clone-reaper",
            daemon=True,
        )
        reaper.start()
        return False


_TARGET_TREE_WANTED_PATH_CAP = 1_000
_TARGET_TREE_ENTRY_CAP = 100_000
_TARGET_TREE_MODE_TYPES = frozenset({
    ("100644", "blob"),    # regular file
    ("100755", "blob"),    # executable regular file
    ("120000", "blob"),    # symbolic link
    ("040000", "tree"),    # directory
    ("160000", "commit"),  # gitlink / submodule
})


def _gh_helpers():
    """Lazy import of the module-level helpers that live in github_rest.py. Called at use-time rather
    than module-load-time to avoid the mutual import cycle: github_rest imports this module, so a top-level
    `from github_rest import …` here would cause a circular import error on Python's first pass.

    Returns (_read_capped, _verify_complete_gzip, _HTTP_TIMEOUT, _HTTP_TOTAL_TIMEOUT): the streamed cap reader,
    the gzip-completeness check, the PER-OP socket timeout (connect + each recv), and the TOTAL-CALL wall-clock
    deadline budget (the bound that actually caps a trickling socket — see github_rest._HTTP_TOTAL_TIMEOUT)."""
    try:
        import github_rest as _gr
    except ImportError:
        from . import github_rest as _gr
    return _gr._read_capped, _gr._verify_complete_gzip, _gr._HTTP_TIMEOUT, _gr._HTTP_TOTAL_TIMEOUT


class _GitHubContentFetchMixin:
    """Content-fetch surface: single-file fetch · tarball download · blobless history clone.
    The host class MUST provide self._itoken(), self._invalidate_token_for_retry(), and self._api()
    (also self.API for the endpoint base URL) — GitHubREST does. This class is never instantiated on its own."""

    def get_file_at(self, repo: str, path: str, ref: str) -> bytes | None:
        """Raw bytes of ONE file @ref via the contents API (raw media type — no base64, up to 100MB), or None
        if it does not exist there (404 = the file was deleted at this sha). Incremental ingest uses this for
        both changed paths and the bounded retained resolution context at the immutable target SHA, avoiding a
        whole-repo tarball without pretending that changed files alone are a complete extraction universe."""
        import urllib.parse
        import urllib.error
        url = f"{self.API}/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"
        try:
            return self._api("GET", url, accept="application/vnd.github.raw")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def target_file_modes(self, repo: str, ref: str, paths) -> dict:
        """Return bounded Git-tree ``mode``/``type`` evidence for wanted paths.

        The Contents API dereferences some symlinks, so raw bytes alone cannot
        prove that an incremental input is a regular file.  Git's recursive
        Trees API exposes the authoritative entry mode without returning file
        bodies.  This method retains only the requested path plus ``mode`` and
        ``type``; blob SHAs, sizes, URLs and unrelated paths are discarded.

        ``complete`` is true only for a structurally valid, non-truncated
        response.  Callers must fail closed when it is false.  The companion
        ``truncated``, ``malformed`` and ``over_cap`` flags expose why the
        evidence is incomplete.  When ``complete`` is true, a wanted path
        absent from ``entries`` is authoritatively missing at ``ref``.
        """
        result = {
            "complete": False,
            "truncated": False,
            "malformed": False,
            "over_cap": False,
            "entries": {},
        }
        if (
            not isinstance(repo, str)
            or not repo
            or len(repo) > 512
            or not isinstance(ref, str)
            or not ref
            or len(ref) > 512
            or isinstance(paths, (str, bytes))
        ):
            result["malformed"] = True
            return result

        wanted = set()
        try:
            for path in paths or ():
                if (
                    not isinstance(path, str)
                    or not path
                    or len(path) > 1_024
                    or "\x00" in path
                ):
                    result["malformed"] = True
                    return result
                wanted.add(path)
                if len(wanted) > _TARGET_TREE_WANTED_PATH_CAP:
                    result["over_cap"] = True
                    return result
        except TypeError:
            result["malformed"] = True
            return result

        if not wanted:
            result["complete"] = True
            return result

        import urllib.parse
        encoded_ref = urllib.parse.quote(ref, safe="")
        response = self._api(
            "GET",
            f"/repos/{repo}/git/trees/{encoded_ref}?recursive=1",
        )
        if not isinstance(response, dict):
            result["malformed"] = True
            return result
        truncated = response.get("truncated")
        tree = response.get("tree")
        if not isinstance(truncated, bool) or not isinstance(tree, list):
            result["malformed"] = True
            return result
        result["truncated"] = truncated
        if len(tree) > _TARGET_TREE_ENTRY_CAP:
            result["over_cap"] = True
            return result

        entries = {}
        seen_paths = set()
        malformed = False
        for entry in tree:
            if not isinstance(entry, dict):
                malformed = True
                break
            path = entry.get("path")
            mode = entry.get("mode")
            entry_type = entry.get("type")
            if (
                not isinstance(path, str)
                or not path
                or not isinstance(mode, str)
                or not isinstance(entry_type, str)
                or (mode, entry_type) not in _TARGET_TREE_MODE_TYPES
                or path in seen_paths
            ):
                malformed = True
                break
            seen_paths.add(path)
            if path in wanted:
                entries[path] = {"mode": mode, "type": entry_type}

        result["entries"] = entries
        result["malformed"] = malformed
        result["complete"] = not truncated and not malformed
        return result

    def _tarball_fetch_once(self, repo: str, sha: str) -> bytes:
        """ONE attempt to fetch the @sha tarball on the CURRENT installation token. The tarball endpoint
        302-redirects to codeload.github.com with a SIGNED URL; urllib must NOT re-send our Authorization/Accept
        headers there (codeload 415s on them). So we do the redirect by hand: ask the API (auth'd) without
        following, then GET the signed Location cleanly. A 401 here propagates so the caller (download_tarball)
        can apply the SAME reactive remint+retry every other installation-token call gets."""
        import urllib.request
        import urllib.error
        try:
            import github_rest as _gr
        except ImportError:
            from . import github_rest as _gr
        _read_capped, _verify_complete_gzip, _HTTP_TIMEOUT, _HTTP_TOTAL_TIMEOUT = _gh_helpers()

        # A direct test call owns a deadline; download_tarball's auth-retry wrapper already owns one, so both
        # fetch attempts, any token mint, the API hop, and codeload consume the same absolute budget.
        with _gr._logical_call_scope() as deadline:
            token_override = _TARBALL_ATTEMPT_TOKEN.get()
            attempt_token = (
                token_override[1]
                if token_override is not None and token_override[0] is self
                else self._itoken()
            )
            api_req = urllib.request.Request(f"{self.API}/repos/{repo}/tarball/{sha}", method="GET")
            api_req.add_header("Authorization", f"Bearer {attempt_token}")
            api_req.add_header("Accept", "application/vnd.github+json")
            api_req.add_header("X-GitHub-Api-Version", "2022-11-28")
            api_req.add_header("User-Agent", _gr._USER_AGENT)
            setattr(api_req, _gr._REQUEST_DEADLINE_ATTR, deadline)

            try:
                # Use the same http.client transport/watchdog as every REST call. It follows same-origin API
                # redirects but deliberately surfaces a cross-origin codeload redirect as HTTPError, so the
                # installation Authorization header is never replayed to the signed host.
                with self._urlopen(api_req) as response:
                    return _read_capped(response, deadline=deadline)
            except urllib.error.HTTPError as exc:
                location = (exc.headers or {}).get("Location")
                if exc.code in (301, 302, 303, 307, 308) and location:
                    codeload_req = urllib.request.Request(location, method="GET")
                    codeload_req.add_header("User-Agent", _gr._USER_AGENT)
                    setattr(codeload_req, _gr._REQUEST_DEADLINE_ATTR, deadline)
                    # Codeload has no installation credential; it still uses the common absolute header
                    # watchdog, including when keepalive is disabled (one-shot mode).
                    with self._urlopen(codeload_req) as response:
                        return _read_capped(response, deadline=deadline)
                raise

    def download_tarball(self, repo: str, sha: str) -> bytes:
        """The repo @sha as a tar.gz, with the SAME token lifecycle as every other installation-token call.
        Proactive remint (via _itoken inside the fetch) keeps the token fresh; REACTIVELY, a 401 on the API-side
        request (the token revoked EARLY — key rotation / suspend-resume — mid- or just-before this expensive
        ingest) invalidates the cache, remints, and retries ONCE. Before this, the tarball fetch was the ONE
        installation-token call WITHOUT the reactive net: an early revocation here failed the whole ingest
        delivery instead of recovering, even though the cheap check/comment calls beside it self-healed."""
        import urllib.error
        try:
            import github_rest as _gr
        except ImportError:
            from . import github_rest as _gr
        with _gr._logical_call_scope() as deadline:
            attempt_token = self._itoken()
            attempt_context = _TARBALL_ATTEMPT_TOKEN.set((self, attempt_token))
            try:
                try:
                    data = self._tarball_fetch_once(repo, sha)
                finally:
                    _TARBALL_ATTEMPT_TOKEN.reset(attempt_context)
            except urllib.error.HTTPError as e:
                if e.code == 401 or self._is_auth_failure_403(e):
                    self._invalidate_token_for_retry(attempt_token)
                    _gr._raise_if_logical_deadline_expired(deadline)
                    retry_token = self._itoken()
                    retry_context = _TARBALL_ATTEMPT_TOKEN.set((self, retry_token))
                    try:
                        data = self._tarball_fetch_once(repo, sha)
                    finally:
                        _TARBALL_ATTEMPT_TOKEN.reset(retry_context)
                else:
                    raise
            # Verify completeness before accepting bytes; its CPU loop checks the enclosing event budget.
            _read_capped, _verify_complete_gzip, _HTTP_TIMEOUT, _HTTP_TOTAL_TIMEOUT = _gh_helpers()
            _verify_complete_gzip(data)
            _gr._raise_if_logical_deadline_expired(deadline)
            return data

    def history_clone(self, repo: str, branch: str, dest: str, timeout: int = 120) -> str:
        """A CONTENT-FREE blobless clone of <repo>@<branch> into <dest> — for the co-change signal ONLY.

        The graph is ingested from a TARBALL (a snapshot at one sha — it has NO commit history), so the
        "files that keep changing together" signal (the coupling the structural graph can't see) needs the
        repo's CHANGE HISTORY, which only a clone carries. We fetch the LEAST that history needs:
          • `--filter=blob:none` — fetch the commit + TREE objects (the per-commit FILE PATHS that
            `git log --name-only` reads) but NEVER a file BLOB. Not one line of code is downloaded — this is
            strictly MORE content-free than the graph path, whose tarball does read file bodies.
          • `--no-checkout` — never materialise a working tree (a checkout would lazily fault IN the blobs we
            just declined, defeating the filter). `git log --name-only` works fine with no work-tree.
          • `--single-branch --branch <branch>` — only the protected branch's history, nothing else.

        The installation token is injected through GIT_CONFIG_* env as an http.extraHeader (the actions/checkout
        pattern) — NOT in the clone URL or argv — so it never lands in `ps`, a git error line, or a log.
        GIT_TERMINAL_PROMPT=0 fails fast instead of blocking on a credential prompt. Returns <dest>; raises on
        failure (the sole caller, ingest.populate_cochange, is fail-open — co-change is an advisory 2nd signal)."""
        import base64
        try:
            import event_budget as _event_budget
        except ImportError:
            from . import event_budget as _event_budget
        try:
            import github_rest as _gr
        except ImportError:
            from . import github_rest as _gr

        # Token mint is an HTTP logical call and keeps that layer's existing
        # deadline.  The clone itself is not an HTTP request: retain its
        # caller-supplied 120s default outside webhook context, while an active
        # event deadline still caps the absolute clone wall time.
        with _gr._logical_call_scope():
            token = self._itoken()
        clone_deadline = _event_budget.deadline_for(timeout)
        _event_budget.raise_if_expired()
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        # SECRET-FREE SUBPROCESS ENV (audit:secrets-to-subprocess): pass git ONLY what it needs — PATH/HOME
        # plus TLS/proxy/locale vars actually set — NOT the inherited os.environ, which on the host also
        # carries VERIPSA_DSN, GH_PRIVATE_KEY, GH_WEBHOOK_SECRET, GH_APP_ID, etc. The one installation
        # credential rides GIT_CONFIG_* http.extraHeader below, never argv or clone URL.
        _GIT_ENV_PASSTHROUGH = (
            "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "GIT_SSL_CAINFO", "SSL_CERT_FILE",
            "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
            "https_proxy", "http_proxy", "no_proxy",
        )
        env = {k: os.environ[k] for k in _GIT_ENV_PASSTHROUGH if k in os.environ}
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        })
        cmd = [
            "git", "clone", "--filter=blob:none", "--no-checkout", "--single-branch",
            "--branch", branch, f"https://github.com/{repo}.git", dest,
        ]
        slot_timeout = _event_budget.timeout_for(
            max(0.001, clone_deadline - time.monotonic()))
        if not _HISTORY_CLONE_CHILD_LOCK.acquire(timeout=slot_timeout):
            _event_budget.raise_if_expired()
            raise subprocess.TimeoutExpired(cmd, slot_timeout)
        release_child_slot = True
        try:
            # Waiting for the singleton slot consumed the same absolute allowance. Recompute before spawn so
            # queueing + git together can never exceed the caller/event deadline.
            effective_timeout = _event_budget.timeout_for(
                max(0.001, clone_deadline - time.monotonic()))
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                release_child_slot = _kill_history_clone(proc, _event_budget)
                # Prefer the typed delivery timeout when the containing event expired; otherwise preserve
                # TimeoutExpired for the caller's local clone ceiling.
                _event_budget.raise_if_expired()
                raise
            _event_budget.raise_if_expired()
            if proc.returncode != 0:
                # git can echo the auth header in a verbose error. Scrub both direct token and encoded Basic
                # credential before logging.
                msg = (stderr or stdout or "")[-200:].replace(token, "***").replace(basic, "***")
                raise RuntimeError(
                    f"history clone failed for {repo}@{branch} (rc={proc.returncode}): {msg}")
            return dest
        finally:
            if release_child_slot:
                _HISTORY_CLONE_CHILD_LOCK.release()
