-- PHASE 6 — OWNER (the founder's cross-tenant cost/usage lens). THE DELIBERATE EXCEPTION to per-tenant RLS:
-- every other surface in this schema resolves the connection's OWN account and is walled to it; THIS ONE is
-- owner-context and sees ALL accounts at once — so the founder can SEE DB growth/cost per tenant and where the
-- "free up to here" line falls (a malicious or runaway account becomes visible; the free/paid boundary becomes
-- tangible). It is therefore the MOST privileged read in the system and is locked the SAME way the retention
-- sweep + the DR export are: SECURITY DEFINER (migrator), REVOKEd from PUBLIC + every tenant role, GRANTed
-- ONLY to the host's service identity (veripsa_app). A buyer seat calling it gets permission denied — by design.
--
-- CONTENT-FREE: account ids + COUNTS/bytes ONLY. Never a path, never a branch, never code. (A cost lens needs
-- volume, not contents — the moat is intact even for the owner.)
--
-- BILLING IS OUT OF SCOPE: the free line is a MECHANISM (a tunable threshold the owner sets), not a price list.
-- Nothing here charges, meters-for-invoice, or talks to Marketplace. It only answers "who is over the line".
-- ============================================================================================

-- ── FREE-TIER LINE — a TUNABLE policy, not a pricing decision. ────────────────────────────────────────────
-- The free thresholds live in the EXISTING owner-key config store (core.policy) under ONE reserved owner
-- account row, read through the SAME bounded-knob discipline (_policy_int: parse → CLAMP to a sane frame →
-- default on unset/garbage). The storage defaults are GENEROUS for a real small team + a WALL for abuse (they are
-- also the ENFORCED cap when unset — the gate's free-tier wall reads this same line, 30_gate.sql) — *** PO sets the
-- real numbers; a paid-tier per-account override arrives with billing *** (the surface reads whatever the owner has
-- tuned, clamped so a fat-fingered "0" or "10^9" can never make the over-line math lie). Keys (all under account =
-- core._owner_account()):
--   free_max_repos        : distinct repos (a HIGH sanity ceiling, not the storage wall).  default 1000     clamp 0..100000
--   free_max_graph_units  : graph nodes + edges a free account may hold (the primary cap).  default 200000   clamp 0..1000000000
--   free_max_events       : events (the fact ledger) a free account may hold.              default 50000    clamp 0..1000000000
--   free_max_seats        : SEATS (human agents) a free account may run before the value   default 2        clamp 0..100000
--                           line. The PLG conversion proxy (NOT a storage cap — it is the seat/MRR value line, not
--                           enforced by the gate's DB-growth wall): free for a solo/pair tasting it, the line comes
--                           the moment a real multi-agent fleet of HUMAN operators forms. AI agents do NOT count
--                           (the fleet-friendly stance — see _seat_count below); only humans consume seats.
-- The owner tunes these by pinning the owner account and calling set_policy_with_authority (see
-- set_free_line_with_authority below — a thin owner-only wrapper so the founder need not know the account id).

-- _owner_account: the reserved, NON-tenant account id that holds the host's own owner-level policy rows (the
-- free line). It is NOT a customer — no installation maps to it, no seat connects as it — it is just the key
-- under which the single global free-line config is stored in core.policy. A constant, in one place.
CREATE OR REPLACE FUNCTION core._owner_account() RETURNS text
    LANGUAGE sql IMMUTABLE AS $$ SELECT 'ACCT-VERIPSA-OWNER'::text $$;
ALTER FUNCTION core._owner_account() OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._owner_account() FROM PUBLIC;

-- _free_line: read the three free-line knobs as ONE jsonb, each clamped to its frame. It pins core.current_account
-- to the reserved owner account so the existing per-account _policy_int reads the GLOBAL owner config (the free
-- line is one number for the whole host, not per-tenant — pinning the owner account is how a per-account knob
-- store serves a global knob without a new table). Internal helper; owner-context only (never granted to a buyer).
CREATE OR REPLACE FUNCTION core._free_line() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_result jsonb;
BEGIN
  v_prev := current_setting('core.current_account', true);     -- remember the caller's pin (restore after)
  PERFORM set_config('core.current_account', core._owner_account(), true);   -- read the GLOBAL owner config
  -- DEFAULTS (the unset-fallback free line): GENEROUS for a real small team, a WALL for abuse. These are also the
  -- ENFORCED cap when nothing is tuned (the gate's free-tier wall reads this same line — 30_gate.sql). The TWO that
  -- actually bound STORAGE/COST are graph_units + events; repo COUNT is a weak proxy (an empty/onboarded-only repo
  -- costs ~nothing — its real footprint shows up as graph_units once it is indexed, and as events once it is pushed
  -- to), so the repos cap is a HIGH SANITY CEILING, not the storage wall:
  --   free_max_repos       1000   — a sanity ceiling; never walls a legit team (even a microservice org installing
  --                                 hundreds of repos), while an absurd register-everything abuser is still bounded.
  --                                 (Kept WELL above the App's own onboard fan-out, _ONBOARD_REPO_CAP=50, so a big
  --                                 org install never trips its own tenant.) graph_units/events catch storage abuse
  --                                 far earlier regardless of repo count.
  --   free_max_graph_units 200000 — the PRIMARY footprint: ≈ a few hundred-file repos of code+schema nodes/edges.
  --   free_max_events      50000  — the push/landing/collision fact ledger.
  -- *** PO tunes the real numbers via set_free_line_with_authority; a paid-tier per-account override arrives with
  -- billing. *** The clamp frames are unchanged (a fat-fingered tuned value still can't make the over-line math lie).
  v_result := jsonb_build_object(
    'max_repos',       core._policy_int('free_max_repos',        1000,     0, 100000),
    'max_graph_units', core._policy_int('free_max_graph_units',  200000,   0, 1000000000),
    'max_events',      core._policy_int('free_max_events',       50000,    0, 1000000000),
    -- SEAT (value) line — TIGHT default = 2 ("trial": a solo/pair). The seat/MRR VALUE line (a PLG conversion
    -- proxy), NOT a storage cap: the gate's DB-growth wall (30_gate.sql) enforces the three storage caps above,
    -- NOT this. PO tunes upward; clamped like the rest.
    'max_seats',       core._policy_int('free_max_seats',        2,        0, 100000)
  );
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);      -- restore the caller's account pin
  RETURN v_result;
END $$;
ALTER FUNCTION core._free_line() OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._free_line() FROM PUBLIC;

-- _plan_file_limit: the per-PLAN ceiling on ANALYZED FILES — the PUBLIC billing meter's line. Maps a plan LABEL
-- onto its in-scope-file allowance; NULL = UNLIMITED (Enterprise, or any paid label we don't bucket — never nudge
-- a payer we cannot place). The numbers are TUNABLE owner policy (read via _policy_int, the SAME clamp path as
-- _free_line) so the PO moves a line with one set_plan_limit_with_authority('pro', …) call — no redeploy.
-- This is the COMMERCIAL coverage line (how big a road network a plan watches), DISTINCT from the abuse wall in
-- 30_gate.sql (_free_line's graph_units/events) which stays a hard storage guard. ADVISORY: nothing here refuses
-- a write — account_coverage_surface reads it to NUDGE an upgrade. Content-free (a plan label → an integer).
-- 'free' is the SMALLEST line on purpose: a beginner's app crosses it early ("ここから課金か"), and because the
-- App is advisory (never blocks; an un-upgraded account is simply not watched beyond the line), a low line is not
-- predatory. Defaults: free 10 · starter 50 · pro 250 · scale 1500 · (enterprise / unknown → unlimited).
CREATE OR REPLACE FUNCTION core._plan_file_limit(p_plan text) RETURNS bigint
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_p text; v_limit bigint;
BEGIN
  v_p := lower(btrim(COALESCE(p_plan,'')));
  IF v_p = '' THEN v_p := 'free'; END IF;
  v_prev := current_setting('core.current_account', true);                    -- remember the caller's pin
  PERFORM set_config('core.current_account', core._owner_account(), true);    -- read the GLOBAL owner policy
  v_limit := CASE v_p
    WHEN 'free'    THEN core._policy_int('plan_files_free',      10, 0, 1000000000)
    WHEN 'starter' THEN core._policy_int('plan_files_starter',   50, 0, 1000000000)
    WHEN 'pro'     THEN core._policy_int('plan_files_pro',      250, 0, 1000000000)
    WHEN 'scale'   THEN core._policy_int('plan_files_scale',   1500, 0, 1000000000)
    ELSE NULL      -- enterprise / an unmapped paid label → unlimited (never nudge a payer)
  END;
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);      -- restore the caller's pin
  RETURN v_limit;
EXCEPTION WHEN OTHERS THEN
  RETURN NULL;     -- FAIL-SAFE: a broken line read must never wrongly nudge — treat as unlimited (no nudge)
END $$;
ALTER FUNCTION core._plan_file_limit(text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._plan_file_limit(text) FROM PUBLIC;

-- set_free_line_with_authority: the owner tunes one free-line knob WITHOUT needing to know the reserved owner
-- account id. A thin wrapper that pins the owner account and writes through the SAME governed set_policy path
-- (so the write is forgery-gated + RLS-checked exactly like any policy write). Only the three free-line keys are
-- accepted (a typo'd key can't silently create dead config). Owner-only (granted to veripsa_app at the bottom).
-- The value is clamped at READ time by _free_line (set stores the raw string; _policy_int clamps on the way out),
-- so even a stored junk/out-of-range value can never make the surface lie. Returns the now-clamped effective value.
CREATE OR REPLACE FUNCTION core.set_free_line_with_authority(p_key text, p_value int) RETURNS int
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_eff int;
BEGIN
  IF p_key NOT IN ('free_max_repos','free_max_graph_units','free_max_events','free_max_seats') THEN
    RAISE EXCEPTION 'set_free_line: unknown free-line key % (allowed: free_max_repos, free_max_graph_units, free_max_events, free_max_seats)', p_key USING ERRCODE='22023';
  END IF;
  IF p_value IS NULL THEN RAISE EXCEPTION 'set_free_line: value must not be null' USING ERRCODE='22023'; END IF;
  v_prev := current_setting('core.current_account', true);
  -- pin the reserved owner account so the governed write (RLS WITH CHECK + forgery token) admits the row into it.
  PERFORM set_config('core.current_account', core._owner_account(), true);
  PERFORM core.mark_governed_write('policy');
  INSERT INTO core.policy(account_id, policy_key, policy_value)
  VALUES (core._owner_account(), p_key, p_value::text)
  ON CONFLICT (account_id, policy_key) DO UPDATE SET policy_value=EXCLUDED.policy_value, set_at=now();
  -- G4 (hooked uniformly across every canonical policy writer): enqueue a refresh for the account written. This
  -- is a DELIBERATE NO-OP for the owner tuning setters — the write lands under the reserved owner account, which
  -- has no installation, so _enqueue_policy_refresh's live-install gate returns without an outbox row. A global
  -- owner knob affects every tenant's FUTURE (live-read) evaluations; a proactive all-tenant fan-out is a
  -- different, unbounded operation outside G4's tenant-scoped, bounded contract. (See 97_policy_refresh.sql.)
  PERFORM core._enqueue_policy_refresh(core._owner_account());
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);   -- restore caller pin BEFORE re-reading
  -- _free_line() keys are the SHORT names (max_repos, …); the policy key carries the 'free_' prefix. Strip it to
  -- read back the clamped, effective value now in force (so a caller sees the real number after clamping).
  v_eff := (core._free_line()->>regexp_replace(p_key, '^free_', ''))::int;
  RETURN v_eff;
END $$;
ALTER FUNCTION core.set_free_line_with_authority(text,int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.set_free_line_with_authority(text,int) FROM PUBLIC;

-- ── DEV-ONLY QUOTA EXEMPTION (the PO's own dogfood/demo accounts) — explicitly NOT a product tier. ───────────
-- The dogfood loop (Veripsa indexing its OWN Core) dies after ONE ingest on a free account (audit 实测: the demo
-- account ACCT-DEMO hit 17,771 graph_units — far past any free line). The PO's own dogfood/demo accounts therefore
-- need a quota BYPASS — but this is a DEV/OWNER CONVENIENCE, NOT a sellable plan: it is an allowlist of specific
-- account ids, stored as ADJUSTABLE owner POLICY (so the PO adds/removes a dogfood account without a redeploy, the
-- SAME governed knob discipline as the free line / plan lines), NEVER hardcoded into the wall. Default seeds the
-- PO's three accounts (the two real GitHub installs + the local demo). Read as a comma-separated id list via the
-- bounded _policy_text path under the reserved owner pin (length-capped; one global list, not per-tenant).
--
-- *** FAIL-CLOSED (the load-bearing inversion vs a feature-flag): a BROKEN allowlist read = NOT exempt = the wall
-- STAYS ENFORCED. *** This reader is a pure helper; the FAIL-CLOSED decision lives at the CALL SITE in
-- core._account_over_quota (30_gate.sql), which checks membership in its OWN sub-block so ANY error there falls
-- through to the wall — an exemption can ONLY ever be granted by a SUCCESSFUL read that POSITIVELY lists the id,
-- never by an error. (Contrast a product tier, which is read from core.account.plan and fails toward 'free'; this
-- is a narrower owner-list, but the same direction: a read failure NEVER opens the wall.) Content-free (account ids).
CREATE OR REPLACE FUNCTION core._dev_exempt_account_ids() RETURNS text
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_raw text;
BEGIN
  v_prev := current_setting('core.current_account', true);                    -- remember the caller's pin
  PERFORM set_config('core.current_account', core._owner_account(), true);    -- read the GLOBAL owner policy
  -- UNSET vs EXPLICITLY-EMPTY (root fix, small-findings sweep — the cleared-allowlist footgun). The generic
  -- _policy_text collapses BOTH "no policy row" AND "row present but blank" onto its DEFAULT — so storing an EMPTY
  -- allowlist (the owner's explicit "exempt NOBODY") fell through to the default TRIO and silently RE-EXEMPTED the
  -- PO's own three accounts instead of nobody. That is the WRONG direction for a wall knob: clearing the exemption
  -- must REMOVE exemptions, never resurrect them. So read core.policy DIRECTLY here (the owner is pinned, so
  -- FORCE-RLS + tenant_isolation scope the read to the owner row — the SAME visibility _policy_text relies on) and
  -- branch on ROW EXISTENCE (the plpgsql FOUND flag after SELECT INTO), NOT on blank-vs-non-blank:
  --   * NO row (FOUND=false)     → UNSET → the default dogfood trio (the documented unset seed).
  --   * row present (FOUND=true) → EXPLICITLY SET → use its value VERBATIM (length-capped). An EMPTY value =
  --                                "exempt NOBODY" (the setter stores '' for that), honored AS empty — NOT re-defaulted.
  -- FAIL-CLOSED is preserved end-to-end: the call site (core._account_over_quota, 30_gate.sql) grants an exemption
  -- ONLY on a SUCCESSFUL read that POSITIVELY lists the id, so an empty list exempts no one + any error still walls.
  SELECT left(policy_value, 4096) INTO v_raw
    FROM core.policy
   WHERE account_id = core._owner_account() AND policy_key = 'dev_exempt_account_ids';
  IF NOT FOUND THEN
    v_raw := 'ACCT-GH-42424242,ACCT-GH-43434343,ACCT-DEMO';   -- UNSET → the documented default dogfood trio
  END IF;
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);      -- restore the caller's pin
  RETURN COALESCE(v_raw, '');   -- a present-but-empty value → '' (exempt nobody), never the default
END $$;
ALTER FUNCTION core._dev_exempt_account_ids() OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._dev_exempt_account_ids() FROM PUBLIC;

-- set_dev_exempt_accounts_with_authority: the owner adjusts the dev-exemption allowlist WITHOUT a redeploy — the
-- SAME owner-pinned, governed, RLS-checked write path as set_free_line_with_authority (pin owner →
-- mark_governed_write → upsert core.policy → restore pin). Takes a comma-separated account-id list (each id bounded
-- + canonicalized: trim, drop blanks, cap length — membership is exact-match). To EXEMPT-NOBODY the owner stores
-- an EMPTY list (e.g. '' or '   '): the normalize below collapses it to '' and UPSERTS that empty value as an
-- EXPLICIT policy row — and the reader now distinguishes that present-but-empty row (→ exempt NOBODY) from a
-- never-set knob (no row → the default trio). So a cleared allowlist now exempts NO ONE (the corrected direction
-- for a wall knob), NOT the PO's trio. Storing an explicit non-empty list exempts EXACTLY those ids (exact-match,
-- so an id not on it is enforced). Stored as raw text (the reader length-caps on read). Returns the now-effective
-- stored list. App-/owner-delegation only (REVOKE PUBLIC; GRANT veripsa_app — a buyer seat can NEVER add itself to
-- the bypass). CONTENT-FREE: account ids only, never any customer data.
CREATE OR REPLACE FUNCTION core.set_dev_exempt_accounts_with_authority(p_csv text) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_norm text;
BEGIN
  -- Normalize: split on comma, trim each id, drop blanks, re-join. NULL/blank ⇒ '' which is UPSERT'd as an EXPLICIT
  -- empty row = "exempt NOBODY" (the reader reads ROW EXISTENCE, so '' is honored as empty, never re-defaulted).
  -- Bounded per id so a pathological value can never blow the stored string (the reader also length-caps on read).
  SELECT COALESCE(string_agg(left(btrim(t),512), ','), '')
    INTO v_norm
    FROM unnest(string_to_array(COALESCE(p_csv,''), ',')) AS t
   WHERE btrim(t) <> '';
  v_prev := current_setting('core.current_account', true);
  PERFORM set_config('core.current_account', core._owner_account(), true);   -- pin owner so the governed write lands in the owner row
  PERFORM core.mark_governed_write('policy');
  INSERT INTO core.policy(account_id, policy_key, policy_value)
  VALUES (core._owner_account(), 'dev_exempt_account_ids', v_norm)
  ON CONFLICT (account_id, policy_key) DO UPDATE SET policy_value=EXCLUDED.policy_value, set_at=now();
  PERFORM core._enqueue_policy_refresh(core._owner_account());   -- G4: no-op for the owner account (see 97_policy_refresh.sql)
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);     -- restore caller pin BEFORE re-reading
  RETURN core._dev_exempt_account_ids();                                      -- the effective list now in force
END $$;
ALTER FUNCTION core.set_dev_exempt_accounts_with_authority(text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.set_dev_exempt_accounts_with_authority(text) FROM PUBLIC;

-- set_plan_limit_with_authority: the owner tunes ONE plan's analyzed-file ceiling (the public billing line read by
-- _plan_file_limit) WITHOUT knowing the reserved owner account id — the SAME owner-pinned, governed, RLS-checked
-- write path as set_free_line_with_authority (pin owner → mark_governed_write → upsert core.policy → restore pin).
-- Only the four bucketed plans are tunable (enterprise is unlimited by design; a typo'd plan can't create dead
-- config). The value is clamped at READ time by _plan_file_limit (_policy_int), so a stored junk value can never
-- make the surface lie. Returns the now-effective limit. App-delegation only (granted to veripsa_app; REVOKE
-- PUBLIC) — a buyer seat cannot move its own billing line.
CREATE OR REPLACE FUNCTION core.set_plan_limit_with_authority(p_plan text, p_limit int) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_p text;
BEGIN
  v_p := lower(btrim(COALESCE(p_plan,'')));
  IF v_p NOT IN ('free','starter','pro','scale') THEN
    RAISE EXCEPTION 'set_plan_limit: unknown/untunable plan % (allowed: free, starter, pro, scale; enterprise is unlimited)', p_plan USING ERRCODE='22023';
  END IF;
  IF p_limit IS NULL THEN RAISE EXCEPTION 'set_plan_limit: limit must not be null' USING ERRCODE='22023'; END IF;
  v_prev := current_setting('core.current_account', true);
  PERFORM set_config('core.current_account', core._owner_account(), true);   -- pin owner so the governed write lands in the owner row
  PERFORM core.mark_governed_write('policy');
  INSERT INTO core.policy(account_id, policy_key, policy_value)
  VALUES (core._owner_account(), 'plan_files_'||v_p, p_limit::text)
  ON CONFLICT (account_id, policy_key) DO UPDATE SET policy_value=EXCLUDED.policy_value, set_at=now();
  PERFORM core._enqueue_policy_refresh(core._owner_account());   -- G4: no-op for the owner account (see 97_policy_refresh.sql)
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);     -- restore caller pin BEFORE re-reading
  RETURN core._plan_file_limit(v_p);                                          -- the clamped, effective limit now in force
END $$;
ALTER FUNCTION core.set_plan_limit_with_authority(text,int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.set_plan_limit_with_authority(text,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.set_plan_limit_with_authority(text,int) TO veripsa_app;

-- _plan_graph_units_limit: the per-PLAN HARD ceiling on graph_units (= Σ(node_count+edge_count) over
-- core.graph_version for the account) — the COMMERCIAL QUOTA LINE the gate ENFORCES (the cost wall), the
-- structural sibling of _plan_file_limit. Maps a plan LABEL onto its graph_units allowance, read through the
-- SAME bounded-knob path (_policy_int) under the reserved owner account pin, so the PO moves any line with ONE
-- set_plan_graph_units_limit_with_authority('pro', …) call — no redeploy. The PO-set lines (实测-CALIBRATED):
--   free 6000 · starter 30000 · pro 80000 · scale 200000 · enterprise 500000.
-- 实测 repo sizes (graph_units) anchor each tier to a REAL codebase it must clear with headroom: requests 3.2k /
-- flask 5.4k FIT free (a real small project, NOT a 39-file toy); fastapi 27k FITS starter (a mid service); ansible
-- 72k FITS pro (a large monorepo); django 151k FITS scale (a very large monorepo); enterprise 500000 is the TOP
-- (≈3.3× django) — NOT unlimited. enterprise 500000 is the "for-now" COST-SAFETY CEILING — raisable later (the PO
-- 解放s more as infra scales), with one setter call against 'graph_units_enterprise'. Clamp (0..1000000000) matches _plan_file_limit.
--
-- *** CRITICAL — THIS IS A HARD WALL, NOT AN ADVISORY METER (the load-bearing difference from _plan_file_limit). ***
-- _plan_file_limit is an ADVISORY nudge: an UNMAPPED/unknown plan → NULL = UNLIMITED, and its EXCEPTION → NULL =
-- unlimited (a broken nudge read must never wrongly nudge — failing toward "no nudge" is correct for a meter).
-- HERE the value gates a WRITE, so BOTH fail directions invert toward ENFORCEMENT — it must NEVER return a value
-- that lets unbounded ingestion through:
--   • an UNMAPPED / null / empty plan → 500000 (the for-now enterprise ceiling), NOT unlimited. An unrecognised
--     paid label is still bounded by the cost-safety ceiling (we never hand an unknown plan an unlimited graph).
--   • the EXCEPTION fail-safe returns a STRICT LOW value (the free line, 6000), NEVER NULL/unlimited: a broken
--     line read must fail TOWARD the wall (an over-account stays walled), the opposite of _plan_file_limit. The
--     worst a bug can do is wall a legit paid account too tightly (recoverable: the PO raises the line); it can
--     NEVER open the cost wall. (Contrast _plan_file_limit's EXCEPTION → NULL, fine for an advisory nudge.)
-- Content-free (a plan label → an integer). NEVER returns NULL (the gate treats NULL specially as "unlimited"
-- in the file-meter world; this fn is HARD and always yields a finite cap, so the gate compares numerically).
CREATE OR REPLACE FUNCTION core._plan_graph_units_limit(p_plan text) RETURNS bigint
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_p text; v_limit bigint;
BEGIN
  v_p := lower(btrim(COALESCE(p_plan,'')));
  IF v_p = '' THEN v_p := 'free'; END IF;
  v_prev := current_setting('core.current_account', true);                    -- remember the caller's pin
  PERFORM set_config('core.current_account', core._owner_account(), true);    -- read the GLOBAL owner policy
  v_limit := CASE v_p
    WHEN 'free'       THEN core._policy_int('graph_units_free',         6000, 0, 1000000000)   -- requests 3.2k / flask 5.4k FIT
    WHEN 'starter'    THEN core._policy_int('graph_units_starter',     30000, 0, 1000000000)   -- fastapi 27k FITS (a mid service)
    WHEN 'pro'        THEN core._policy_int('graph_units_pro',         80000, 0, 1000000000)   -- ansible 72k FITS (a large monorepo)
    WHEN 'scale'      THEN core._policy_int('graph_units_scale',      200000, 0, 1000000000)   -- django 151k FITS (very large monorepo)
    WHEN 'enterprise' THEN core._policy_int('graph_units_enterprise', 500000, 0, 1000000000)   -- the TOP (≈3.3× django); NOT unlimited
    -- UNMAPPED / unknown paid label → the for-now enterprise ceiling (500000), NOT unlimited. A HARD wall never
    -- hands an unrecognised plan an unbounded graph; the cost-safety ceiling still bounds it (tunable via the
    -- 'graph_units_enterprise' knob so raising the ceiling raises the unmapped fallback in lockstep).
    ELSE core._policy_int('graph_units_enterprise', 500000, 0, 1000000000)
  END;
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);      -- restore the caller's pin
  RETURN v_limit;
EXCEPTION WHEN OTHERS THEN
  -- FAIL TOWARD ENFORCEMENT (the inverse of _plan_file_limit's NULL=unlimited): a broken line read returns the
  -- STRICT FREE line (6000), never unlimited — an over-account stays walled. Best-effort to restore the caller's
  -- pin first so a mid-read failure cannot leave the owner account pinned for the rest of the statement.
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);
  RETURN 6000::bigint;     -- strictest finite cap = the free line; a quota-wall read must never fail OPEN
END $$;
ALTER FUNCTION core._plan_graph_units_limit(text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._plan_graph_units_limit(text) FROM PUBLIC;

-- set_plan_graph_units_limit_with_authority: the owner tunes ONE plan's HARD graph_units ceiling (the quota line
-- read by _plan_graph_units_limit) WITHOUT knowing the reserved owner account id — the SAME owner-pinned,
-- governed, RLS-checked write path as set_plan_limit_with_authority (pin owner → mark_governed_write → upsert
-- core.policy → restore pin). A THIN SIBLING, not an extension of set_plan_limit_with_authority, because that
-- setter is keyed to the 'plan_files_' prefix AND deliberately REJECTS 'enterprise' (the file meter is unlimited
-- there) — but the graph_units ceiling MUST be tunable for EVERY plan INCLUDING enterprise (raising the for-now
-- 200000 cost ceiling with ONE command is the whole point). So all FIVE plans are accepted here. The value is
-- clamped at READ time by _plan_graph_units_limit (_policy_int), so a stored junk value can never open the wall.
-- Returns the now-effective limit. App- + billing-delegation only (REVOKE PUBLIC; GRANT veripsa_app — a buyer
-- seat cannot move its own quota wall). The matching grant is at the bottom of this file with the other owner setters.
CREATE OR REPLACE FUNCTION core.set_plan_graph_units_limit_with_authority(p_plan text, p_limit int) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_p text;
BEGIN
  v_p := lower(btrim(COALESCE(p_plan,'')));
  IF v_p NOT IN ('free','starter','pro','scale','enterprise') THEN
    RAISE EXCEPTION 'set_plan_graph_units_limit: unknown plan % (allowed: free, starter, pro, scale, enterprise)', p_plan USING ERRCODE='22023';
  END IF;
  IF p_limit IS NULL THEN RAISE EXCEPTION 'set_plan_graph_units_limit: limit must not be null' USING ERRCODE='22023'; END IF;
  v_prev := current_setting('core.current_account', true);
  PERFORM set_config('core.current_account', core._owner_account(), true);   -- pin owner so the governed write lands in the owner row
  PERFORM core.mark_governed_write('policy');
  INSERT INTO core.policy(account_id, policy_key, policy_value)
  VALUES (core._owner_account(), 'graph_units_'||v_p, p_limit::text)
  ON CONFLICT (account_id, policy_key) DO UPDATE SET policy_value=EXCLUDED.policy_value, set_at=now();
  PERFORM core._enqueue_policy_refresh(core._owner_account());   -- G4: no-op for the owner account (see 97_policy_refresh.sql)
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);     -- restore caller pin BEFORE re-reading
  RETURN core._plan_graph_units_limit(v_p);                                   -- the clamped, effective limit now in force
END $$;
ALTER FUNCTION core.set_plan_graph_units_limit_with_authority(text,int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.set_plan_graph_units_limit_with_authority(text,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.set_plan_graph_units_limit_with_authority(text,int) TO veripsa_app;

-- ── SEATS — the PLG value line. distinct HUMAN agents active in 30d for the CURRENTLY-PINNED account. ──────
-- _seat_count: a "seat" is a HUMAN operator who has been active recently — the cleanest proxy for "a real
-- multi-agent fleet has formed, this is past trial". The fleet-friendly pricing stance: AI agents are FREE
-- (you may run as many bots as you like), only the humans behind them count toward the line. Two-part heuristic
-- for "human, not a bot" (documented so the over-line math is auditable):
--   (1) core.agent.agent_kind = 'human'  — the CANONICAL marker. The gate stamps every AI session 'ai' and a
--       human operator 'human'; this is the primary signal (an AI fleet is all 'ai' → zero seats consumed).
--   (2) AND the display_name does NOT end in '[bot]' (case-insensitive) — the GitHub bot-login convention, a
--       defensive belt so an automation miscatalogued as 'human' (e.g. a CI/app identity) is still excluded.
-- "Active in 30d" = the agent appears as agent_id in core.event (occurred_at) OR core.claim (claimed_at) within
-- the window — i.e. it actually DID something, not merely that a row exists. Counted under the CALLER'S pin
-- (owner_cost_surface pins each tenant before calling this), so it reads inside that tenant's RLS wall — no
-- cross-account leak. Content-free (a COUNT only — no name, no login, ever crosses out). Owner-context helper.
CREATE OR REPLACE FUNCTION core._seat_count(p_window_days int DEFAULT 30) RETURNS int
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_cutoff timestamptz; v_n int;
BEGIN
  v_account := current_setting('core.current_account', true);
  IF v_account IS NULL OR v_account = '' THEN RETURN 0; END IF;   -- no pin → no rows visible → no seats
  v_cutoff := now() - make_interval(days => GREATEST(1, p_window_days));
  WITH active_ids AS (
    SELECT agent_id FROM core.event WHERE account_id = v_account AND occurred_at > v_cutoff
    UNION
    SELECT agent_id FROM core.claim WHERE account_id = v_account AND claimed_at  > v_cutoff
  )
  SELECT count(*)::int INTO v_n
  FROM (SELECT DISTINCT ai.agent_id FROM active_ids ai) d
  JOIN core.agent a ON a.agent_id = d.agent_id AND a.account_id = v_account
  WHERE a.agent_kind = 'human'                                   -- (1) humans are seats; AI agents are free
    AND COALESCE(lower(a.display_name), '') !~ '\[bot\]$';       -- (2) defensive: exclude a '[bot]'-suffixed login
  RETURN COALESCE(v_n, 0);
END $$;
ALTER FUNCTION core._seat_count(int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._seat_count(int) FROM PUBLIC;

-- ── THE OWNER COST SURFACE — cross-tenant DB growth/cost, with the free line drawn. ───────────────────────
-- owner_cost_surface: the founder's one read. For EVERY tenant it returns content-free volume (repos, graph
-- nodes/edges, events, 7-day event growth) + that account's footprint as a % of the free line (the MAX of the
-- three ratios — an account is "over" if ANY single dimension exceeds its free cap), ordered biggest-consumer
-- first, plus the whole-DB byte size and how many accounts are over the line. It ALSO carries the SEAT (value)
-- dimension per account — active_agents (distinct HUMAN operators active in 30d; AI agents are free, see
-- _seat_count) and paid_seats (= max(0, active_agents − free_seat_line)) with an over_seat_line flag — plus the
-- rolled-up free_seat_line, paid_seats_total and over_seat_line_count at the top. This is the PLG conversion
-- lens: seats are the proxy for "a real multi-agent fleet has formed" (the value moment). VISIBILITY + PROJECTION
-- ONLY — nothing here charges or talks to Marketplace (the report turns paid_seats_total into an MRR estimate).
-- This is the cross-account read the
-- moat normally forbids — so it is owner-context by the SAME mechanism the retention sweep + DR export use:
--   * tenants are enumerated via core.installation_account — the no-RLS installation→account routing map (every
--     real GitHub tenant is registered there on its first webhook). It is the ONE place an owner-context fn can
--     read the full tenant list WITHOUT a per-account pin (every per-account table, incl. core.account, wears
--     FORCE RLS that hides all rows when nothing is pinned — even from the table owner; PROVEN: a migrator with
--     no pin sees zero accounts). DISTINCT: one account can hold several installations.
--   * per account, we PIN core.current_account to it and count under its own RLS wall (exactly like the prune
--     loop), so each tenant's numbers are read in that tenant's isolation context — no cross-account leak path
--     ever opens; the surface just visits each wall in turn. The pin is restored to '' between accounts.
-- SECURITY: SECURITY DEFINER (migrator owner). REVOKEd from PUBLIC + every tenant role; GRANTed ONLY to
-- veripsa_app (the host service identity that runs admin/retention) — a buyer seat gets permission denied. This
-- is the deliberate, single owner-only exception to per-tenant isolation. Content-free (ids + counts/bytes only).
-- Read-only (SELECTs + set_config pins; never writes — no forgery token armed, it cannot tamper).
--
-- BOUNDED PER CALL (audit P2-2 — owner sweeps were O(N accounts) sequential). This surface backs /readyz-adjacent
-- ops + the watchdog COST TICK + cost_report.py, doing ~6 count queries PER account inside a per-account pin loop
-- — O(N accounts) work that degrades the watchdog tick as Marketplace install count grows. The per-account DETAIL
-- rows were ALREADY thrown away past the top 200 (the final jsonb_agg LIMIT 200), so visiting EVERY account just
-- to discard all but 200 was pure waste. Fix: bound the per-account loop to p_cap accounts (default 200 = the
-- existing display cap, owner-tunable + clamped via _policy_int 'owner_cost_scan_cap' 1..1000000), visited in
-- deterministic account_id order. The detail rows + their derived rollups (over_line_count / paid_seats_total /
-- over_seat_line_count) are then over the SCANNED top-cap set, flagged honest with capped/accounts_scanned.
-- CORRECTNESS (why a cap is safe HERE — ADVISORY visibility/projection, NOT a write-correctness gate):
--   • db_total_bytes stays EXACT (one pg_database_size call, no per-account work) — so db_size_high, the real
--     disk-fill alert that protects the 256 MiB instance, is UNAFFECTED by the cap.
--   • account_count stays EXACT (a cheap count over the no-RLS routing map, no per-account pin) — the founder
--     always sees the true tenant total.
--   • the per-tenant over-the-FREE-LINE figures are a VISIBILITY nudge (account_over_line just says "review
--     footprint/billing"); the ENFORCED free-tier wall is _account_over_quota at the gate (30_gate.sql), called
--     per DB-growing write, and is NOT this surface — so a bounded scan here cannot let an over-quota tenant
--     write. paid_seats_total/MRR is an explicit PROJECTION. All bounded figures carry capped/accounts_scanned
--     so the report never silently claims a full-fleet total it did not compute.
-- Adding the p_cap arg via CREATE OR REPLACE would leave the OLD zero-arg overload behind on a re-apply (dead
-- code + an AMBIGUOUS owner_cost_surface() call once two overloads coexist) — DROP it first (the same
-- signature-change pattern as patch_graph_with_authority / record_collision_with_authority in 30_gate.sql).
DROP FUNCTION IF EXISTS core.owner_cost_surface();
CREATE OR REPLACE FUNCTION core.owner_cost_surface(p_cap int DEFAULT NULL) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_free jsonb; v_max_repos int; v_max_units int; v_max_events int; v_free_seats int;
  v_acct text; v_repos int; v_nodes int; v_edges int; v_units int; v_events int; v_events_7d int;
  v_pct numeric; v_over boolean;
  v_seats int; v_paid_seats int; v_over_seats boolean;     -- SEAT (value) dimension, per account
  v_rows jsonb := '[]'::jsonb;     -- per-account rows (collected, then sorted + capped at the end)
  v_cap int; v_account_count int := 0; v_scanned int := 0; v_over_count int := 0;
  v_paid_seats_total int := 0; v_over_seat_count int := 0; -- SEAT rollups (Σ paid seats, # accounts at/over the line)
BEGIN
  v_free       := core._free_line();
  v_max_repos  := (v_free->>'max_repos')::int;
  v_max_units  := (v_free->>'max_graph_units')::int;
  v_max_events := (v_free->>'max_events')::int;
  v_free_seats := (v_free->>'max_seats')::int;

  -- the per-call account-scan bound: the explicit arg if given (tests pin it), else the owner-tunable knob
  -- 'owner_cost_scan_cap' (default 200 = the existing top-N display cap; clamp 1..1000000). _free_line above
  -- already pinned + restored the owner account for its read; pin it again here so _policy_int reads the global
  -- knob (it lives under the owner account, like the free line), then disarm before the per-tenant loop.
  PERFORM set_config('core.current_account', core._owner_account(), true);
  v_cap := COALESCE(p_cap, core._policy_int('owner_cost_scan_cap', 200, 1, 1000000));
  PERFORM set_config('core.current_account', '', true);
  IF v_cap < 1 THEN v_cap := 1; END IF;
  -- EXACT full LIVE-tenant total (cheap: a count over the no-RLS routing map, NO per-account pin) — so
  -- account_count is always the true number even when the per-account DETAIL scan below is bounded to the top cap.
  -- Suspended/uninstalled installs keep their routing row for reinstall continuity, but must not keep firing the
  -- founder's account_over_line nudge or inflate the visible fleet count.
  SELECT count(DISTINCT account_id)::int INTO v_account_count
    FROM core.installation_account
   WHERE revoked_at IS NULL;

  -- visit the top-cap LIVE tenants' RLS walls in turn (the no-RLS routing map is the only cross-account-safe
  -- enumerator). BOUNDED: LIMIT v_cap caps the per-account pin loop to ≤cap iterations at any install count
  -- (the O(N accounts) → O(cap) fix). Deterministic account_id order. account_count above is the EXACT full total.
  FOR v_acct IN
      SELECT DISTINCT account_id FROM core.installation_account
       WHERE revoked_at IS NULL
       ORDER BY account_id
       LIMIT v_cap
  LOOP
    v_scanned := v_scanned + 1;
    PERFORM set_config('core.current_account', v_acct, true);   -- read THIS tenant inside its own isolation

    -- distinct repos this account tracks (a graph_version row exists per (account,repo,branch); count the repos).
    SELECT count(DISTINCT repo)::int INTO v_repos FROM core.graph_version WHERE account_id = v_acct;
    SELECT count(*)::int            INTO v_nodes FROM core.code_node      WHERE account_id = v_acct;
    SELECT count(*)::int            INTO v_edges FROM core.code_edge      WHERE account_id = v_acct;
    v_units := v_nodes + v_edges;
    SELECT count(*)::int            INTO v_events FROM core.event         WHERE account_id = v_acct;
    SELECT count(*)::int            INTO v_events_7d FROM core.event
      WHERE account_id = v_acct AND occurred_at > now() - interval '7 days';

    -- footprint_pct = the MAX of the three usage ratios (over the free line if ANY single dimension exceeds its
    -- cap). A cap of 0 means "nothing free on this axis": any usage there is infinitely over → treat as over the
    -- line (ratio 100% floor when usage>0, so a 0-cap axis with any rows flags). Rounded to 1 decimal for display.
    v_pct := GREATEST(
      CASE WHEN v_max_repos  > 0 THEN v_repos::numeric / v_max_repos  ELSE CASE WHEN v_repos  > 0 THEN 1 ELSE 0 END END,
      CASE WHEN v_max_units  > 0 THEN v_units::numeric / v_max_units  ELSE CASE WHEN v_units  > 0 THEN 1 ELSE 0 END END,
      CASE WHEN v_max_events > 0 THEN v_events::numeric / v_max_events ELSE CASE WHEN v_events > 0 THEN 1 ELSE 0 END END
    ) * 100;
    v_over := (v_repos > v_max_repos) OR (v_units > v_max_units) OR (v_events > v_max_events);
    IF v_over THEN v_over_count := v_over_count + 1; END IF;

    -- SEAT (value) line — distinct HUMAN agents active in 30d (the pin is still this tenant, so _seat_count
    -- reads inside its wall). paid_seats = the humans BEYOND the free line; over_seat_line = AT/over the line
    -- (active_agents >= free_seat_line) — the conversion-candidate signal (the buyer has hit the value moment).
    v_seats      := core._seat_count(30);
    v_paid_seats := GREATEST(0, v_seats - v_free_seats);
    v_over_seats := (v_seats >= v_free_seats) AND (v_free_seats >= 0) AND (v_seats > 0);
    v_paid_seats_total := v_paid_seats_total + v_paid_seats;
    IF v_over_seats THEN v_over_seat_count := v_over_seat_count + 1; END IF;

    v_rows := v_rows || jsonb_build_object(
      'account_id',     v_acct,
      'repos',          v_repos,
      'graph_nodes',    v_nodes,
      'graph_edges',    v_edges,
      'graph_units',    v_units,
      'events',         v_events,
      'events_7d',      v_events_7d,
      'footprint_pct',  round(v_pct, 1),
      'over_free_line', v_over,
      'active_agents',  v_seats,
      'paid_seats',     v_paid_seats,
      'over_seat_line', v_over_seats);

    PERFORM set_config('core.current_account', '', true);       -- disarm the pin before the next tenant
  END LOOP;

  RETURN jsonb_build_object(
    -- db_total_bytes + account_count are EXACT regardless of the scan cap (one size call + a count over the
    -- no-RLS routing map) — so the disk-fill alert and the true tenant total are never bounded away.
    'db_total_bytes',  pg_database_size(current_database()),
    'free_line',       jsonb_build_object('max_repos', v_max_repos, 'max_graph_units', v_max_units, 'max_events', v_max_events, 'max_seats', v_free_seats),
    'account_count',   v_account_count,
    -- over_line_count + the seat rollups are over the SCANNED top-cap set (the per-account detail computed this
    -- call). At cap=default(200) ≥ the install count this equals the full total; past the cap it is the top-cap
    -- view, flagged by capped/accounts_scanned below so the report never claims a full-fleet figure it did not
    -- compute. The ENFORCED free-tier wall is _account_over_quota at the gate (30_gate.sql), not this nudge.
    'over_line_count', v_over_count,
    -- SEAT (value) rollups: the free seat line, the projected billable seats (Σ over the line), and how many
    -- accounts are AT/over it (the conversion candidates). VISIBILITY + PROJECTION — the report turns
    -- paid_seats_total into an MRR estimate; nothing here charges.
    'free_seat_line',     v_free_seats,
    'paid_seats_total',   v_paid_seats_total,
    'over_seat_line_count', v_over_seat_count,
    -- bound honesty: was the per-account detail scan capped (a top-cap view, not the full fleet)? content-free ints.
    'capped',           (v_scanned < v_account_count),
    'cap',              v_cap,
    'accounts_scanned', v_scanned,
    -- top consumers first; the row list can never exceed the scan cap (the loop visited at most v_cap accounts).
    'accounts', COALESCE((
      SELECT jsonb_agg(e ORDER BY (e->>'footprint_pct')::numeric DESC, (e->>'events')::int DESC, e->>'account_id')
      FROM (SELECT e FROM jsonb_array_elements(v_rows) e
            ORDER BY (e->>'footprint_pct')::numeric DESC, (e->>'events')::int DESC, e->>'account_id'
            LIMIT v_cap) capped), '[]'::jsonb)
  );
END $$;
ALTER FUNCTION core.owner_cost_surface(int) OWNER TO veripsa_migrator;

-- owner_account_usage_surface — the narrow owner-admin projection consumed by the SEPARATE Platform.
-- The platform reader must not receive owner_cost_surface's DB bytes, graph node/edge counts, free-line policy,
-- paid-seat projection, or other operator-only fields. This SECURITY DEFINER wrapper executes the existing
-- bounded owner lens as its migrator owner, then allowlists only account ids + coarse activation counts. The
-- underlying owner_cost_surface remains DENIED to the platform reader, so future fields cannot widen this API.
-- Content-free: account ids and aggregate counts only; no repo names, paths, graph rows, source, diff, webhook
-- payload, IP, user-agent, free-line, quota, or paid-seat data. Read-only and hard-capped at 200 accounts even
-- when a caller supplies a larger p_cap, so the separate Platform cannot turn this into an unbounded tenant scan.
BEGIN;
CREATE OR REPLACE FUNCTION core.owner_account_usage_surface(p_cap int DEFAULT NULL) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  WITH source AS MATERIALIZED (
    SELECT core.owner_cost_surface(LEAST(GREATEST(COALESCE(p_cap, 200), 1), 200)) AS value
  )
  SELECT jsonb_build_object(
    'capped', COALESCE(source.value->'capped', 'false'::jsonb),
    'cap', source.value->'cap',
    'accounts_scanned', source.value->'accounts_scanned',
    'accounts', COALESCE((
      SELECT jsonb_agg(
        jsonb_build_object(
          'account_id', account_row.value->'account_id',
          'repos', account_row.value->'repos',
          'events', account_row.value->'events',
          'events_7d', account_row.value->'events_7d',
          'active_agents', account_row.value->'active_agents'
        ) ORDER BY account_row.value->>'account_id'
      )
      FROM jsonb_array_elements(COALESCE(source.value->'accounts', '[]'::jsonb)) AS account_row(value)
    ), '[]'::jsonb)
  )
  FROM source;
$$;
ALTER FUNCTION core.owner_account_usage_surface(int) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.owner_account_usage_surface(int) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.owner_account_usage_surface(int) TO example_platform_reader, veripsa_app;
COMMIT;

-- ── GRANTS: the owner cross-tenant lens is locked to the host service identity (the SAME lock as the retention
--    sweep + DR export). REVOKE from PUBLIC (so no tenant role inherits it); GRANT only to veripsa_app. Tenant
--    roles (veripsa_app aside, the demo agents inherit veripsa_writer, NOT veripsa_app) cannot reach it. The
--    owner-only knob writer is locked the same way. (REVOKE-from-PUBLIC is what actually walls the tenant roles
--    out — they hold no explicit grant; the comment in the goal about veripsa_reader/writer/demo_* is satisfied
--    by granting ONLY veripsa_app and never the tenant roles.)
REVOKE EXECUTE ON FUNCTION core.owner_cost_surface(int) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.owner_cost_surface(int) TO veripsa_app;  -- OWNER COST LENS: the host's cross-tenant DB-growth/cost view + the free line, BOUNDED per call (a buyer seat cannot read across tenants)
REVOKE EXECUTE ON FUNCTION core.set_free_line_with_authority(text,int) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.set_free_line_with_authority(text,int) TO veripsa_app;  -- the owner tunes the free-line thresholds (mechanism, not pricing)
REVOKE EXECUTE ON FUNCTION core.set_dev_exempt_accounts_with_authority(text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.set_dev_exempt_accounts_with_authority(text) TO veripsa_app;  -- the owner adjusts the DEV-ONLY quota-exemption allowlist (dogfood/demo accounts; a buyer seat can never self-exempt)

-- ============================================================================================

-- ── OWNER GRAPH-FRESHNESS LENS — bounded, fleet-complete rotating sample. ────────────────────────────────
-- /freshz and the watchdog need a cross-tenant sample, but a fixed first-p_cap scan permanently hides account
-- p_cap+1 and lets a graph-heavy account consume every coordinate slot.  This page is therefore keyed by the
-- durable host cursor in boot_reconcile_state(kind='graph_freshness_cursor').  It scans at most p_cap LIVE
-- accounts strictly after the persisted account high-water, including accounts which have no graph yet.  A
-- separate exact-CAS function advances the cursor only after the caller has consumed the page, so a crashed or
-- concurrent reader cannot silently skip it.
--
-- Each account contributes at most ONE coordinate.  On fleet wrap, cycle increments; coordinate
-- (cycle % graph_count), ordered by (repo,branch), is selected for that account.  Thus every account is visited
-- once per cycle, every repository is eventually sampled, and neither empty nor graph-heavy accounts monopolize
-- a bounded tick.  The exact durable GitHub installation generation accompanies both each scan entry and its
-- selected coordinate.  A legacy route with no generation is retained as an entry with a NULL tuple so the App
-- can report Unknown rather than silently treating it as absent.
--
-- The cursor state is content-free: only {after_account,cycle}.  Repository coordinates remain the same bounded
-- public-git metadata already exposed by graph_freshness_surface; no path, source, diff, payload, or PR body is
-- stored in the cursor.  Adding p_cap originally changed the signature, so retain the defensive zero-arg drop.
DROP FUNCTION IF EXISTS core.owner_graph_freshness_surface();
CREATE OR REPLACE FUNCTION core.owner_graph_freshness_surface(p_cap int DEFAULT NULL) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_cap int;
  v_cursor_fields jsonb;
  v_after_account text;
  v_cycle bigint := 0;
  v_prior_account text;
  v_route record;
  v_graph_count bigint;
  v_coordinate jsonb;
  v_entries jsonb := '[]'::jsonb;
  v_coordinates jsonb := '[]'::jsonb;
  v_accounts int := 0;
  v_has_more boolean := false;
  v_last_account text;
  v_next_after text;
  v_next_cycle bigint;
BEGIN
  v_prior_account := current_setting('core.current_account',true);
  PERFORM set_config('core.current_account', core._owner_account(), true);
  v_cap := LEAST(
    GREATEST(
      COALESCE(
        p_cap,
        core._policy_int('owner_freshness_cap',100,1,100000)
      ),
      1
    ),
    100000
  );
  PERFORM set_config('core.current_account', COALESCE(v_prior_account,''), true);

  SELECT fields INTO v_cursor_fields
    FROM core.boot_reconcile_state
   WHERE kind='graph_freshness_cursor';
  v_after_account := NULLIF(COALESCE(v_cursor_fields->>'after_account',''),'');
  IF v_after_account IS NOT NULL
     AND length(v_after_account) NOT BETWEEN 1 AND 200 THEN
    v_after_account := NULL;
  END IF;
  IF COALESCE(v_cursor_fields->>'cycle','') ~ '^[0-9]{1,18}$' THEN
    v_cycle := (v_cursor_fields->>'cycle')::bigint;
  ELSE
    -- A malformed operator/control row is never ordering authority.  Normalize the complete tuple so the
    -- exact-CAS writer can repair it only from the {NULL,0} expected state returned below.
    v_after_account := NULL;
    v_cycle := 0;
  END IF;

  -- DISTINCT ON selects the one durable installation generation for each live account deterministically.  Read
  -- cap+1 account rows: the extra row is only a bounded continuation witness and is never emitted or tenant-pinned.
  FOR v_route IN
    SELECT candidate.account_id,
           candidate.github_installation_id,
           candidate.github_installation_created_at
      FROM (
        SELECT DISTINCT ON (route.account_id COLLATE "C")
               route.account_id,
               route.github_installation_id,
               route.github_installation_created_at
          FROM core.installation_account route
         WHERE route.revoked_at IS NULL
           AND (v_after_account IS NULL
                OR route.account_id COLLATE "C" > v_after_account COLLATE "C")
           AND NOT EXISTS (
             SELECT 1
               FROM core.account_lifecycle_tombstone tombstone
              WHERE tombstone.account_id=route.account_id
                AND tombstone.active
           )
         ORDER BY route.account_id COLLATE "C",
                  route.github_installation_created_at DESC NULLS LAST,
                  route.github_installation_id COLLATE "C" DESC NULLS LAST,
                  route.installation_id COLLATE "C"
      ) candidate
     ORDER BY candidate.account_id COLLATE "C"
     LIMIT (v_cap+1)
  LOOP
    IF v_accounts >= v_cap THEN
      v_has_more := true;
      EXIT;
    END IF;
    v_accounts := v_accounts + 1;
    v_last_account := v_route.account_id;
    PERFORM set_config('core.current_account',v_route.account_id,true);
    SELECT count(*)::bigint INTO v_graph_count
      FROM core.graph_version
     WHERE account_id=v_route.account_id;
    v_coordinate := NULL;
    IF v_graph_count > 0 THEN
      SELECT jsonb_build_object(
               'account_id',v_route.account_id,
               'github_installation_id',v_route.github_installation_id,
               'github_installation_created_at',v_route.github_installation_created_at,
               'repo',graph.repo,
               'branch',graph.branch,
               'commit_sha',graph.commit_sha,
               'node_count',graph.node_count,
               'edge_count',graph.edge_count,
               'ingested_at',graph.ingested_at,
               'age_seconds',
                 GREATEST(0,round(EXTRACT(EPOCH FROM (now()-graph.ingested_at)))::bigint)
             )
        INTO v_coordinate
        FROM core.graph_version graph
       WHERE graph.account_id=v_route.account_id
       ORDER BY graph.repo COLLATE "C",graph.branch COLLATE "C"
       OFFSET (v_cycle % v_graph_count)
       LIMIT 1;
      v_coordinates := v_coordinates || jsonb_build_array(v_coordinate);
    END IF;
    v_entries := v_entries || jsonb_build_array(jsonb_build_object(
      'account_id',v_route.account_id,
      'github_installation_id',v_route.github_installation_id,
      'github_installation_created_at',v_route.github_installation_created_at,
      'graph_count',v_graph_count,
      'coordinate',v_coordinate
    ));
    PERFORM set_config('core.current_account',COALESCE(v_prior_account,''),true);
  END LOOP;

  v_next_after := CASE WHEN v_has_more THEN v_last_account ELSE NULL END;
  v_next_cycle := CASE WHEN v_has_more THEN v_cycle ELSE v_cycle+1 END;
  RETURN jsonb_build_object(
    'expected_cursor',jsonb_build_object(
      'after_account',v_after_account,
      'cycle',v_cycle
    ),
    'next_cursor',jsonb_build_object(
      'after_account',v_next_after,
      'cycle',v_next_cycle
    ),
    'entries',v_entries,
    'coordinates',v_coordinates,
    'coordinate_count',jsonb_array_length(v_coordinates),
    'max_age_seconds',
      NULLIF((
        SELECT max((coordinate->>'age_seconds')::bigint)
          FROM jsonb_array_elements(v_coordinates) AS coordinate
      ),NULL),
    'coverage_complete',NOT v_has_more,
    'capped',v_has_more,
    'cap',v_cap,
    'accounts_scanned',v_accounts
  );
END $$;
ALTER FUNCTION core.owner_graph_freshness_surface(int) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.owner_graph_freshness_surface(int) FROM PUBLIC, veripsa_writer;
GRANT  EXECUTE ON FUNCTION core.owner_graph_freshness_surface(int) TO veripsa_app;  -- OWNER FRESHNESS LENS: cross-tenant graph-staleness, BOUNDED per call (a buyer seat cannot read across tenants)

-- Advance only the exact page the caller consumed.  Same-cycle movement must increase the account key; a wrap
-- must clear it and increment cycle exactly once.  This rejects stale readers and malformed/skipping transitions.
CREATE OR REPLACE FUNCTION core.advance_graph_freshness_cursor_with_authority(
    p_expected_after text,
    p_expected_cycle bigint,
    p_next_after text,
    p_next_cycle bigint)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_fields jsonb;
  v_current_after text;
  v_current_cycle bigint := 0;
  v_inserted int := 0;
BEGIN
  IF p_expected_cycle IS NULL OR p_expected_cycle < 0
     OR p_next_cycle IS NULL OR p_next_cycle < 0
     OR (p_expected_after IS NOT NULL
         AND length(p_expected_after) NOT BETWEEN 1 AND 200)
     OR (p_next_after IS NOT NULL
         AND length(p_next_after) NOT BETWEEN 1 AND 200) THEN
    RAISE EXCEPTION 'graph freshness cursor is malformed' USING ERRCODE='22023';
  END IF;
  IF (
       p_next_after IS NULL
       AND p_next_cycle<>p_expected_cycle+1
     ) OR (
       p_next_after IS NOT NULL
       AND (
         p_next_cycle<>p_expected_cycle
         OR (
           p_expected_after IS NOT NULL
           AND p_next_after COLLATE "C" <= p_expected_after COLLATE "C"
         )
       )
     ) THEN
    RAISE EXCEPTION 'graph freshness cursor transition is malformed' USING ERRCODE='22023';
  END IF;

  SELECT fields INTO v_fields
    FROM core.boot_reconcile_state
   WHERE kind='graph_freshness_cursor'
   FOR UPDATE;
  IF NOT FOUND THEN
    IF p_expected_after IS NOT NULL OR p_expected_cycle<>0 THEN
      RETURN false;
    END IF;
    INSERT INTO core.boot_reconcile_state(kind,last_run_at,fields)
    VALUES (
      'graph_freshness_cursor',
      now(),
      jsonb_build_object(
        'after_account',p_next_after,
        'cycle',p_next_cycle
      )
    )
    ON CONFLICT (kind) DO NOTHING;
    GET DIAGNOSTICS v_inserted=ROW_COUNT;
    RETURN v_inserted=1;
  END IF;

  v_current_after := NULLIF(COALESCE(v_fields->>'after_account',''),'');
  IF v_current_after IS NOT NULL
     AND length(v_current_after) NOT BETWEEN 1 AND 200 THEN
    v_current_after := NULL;
  END IF;
  IF COALESCE(v_fields->>'cycle','') ~ '^[0-9]{1,18}$' THEN
    v_current_cycle := (v_fields->>'cycle')::bigint;
  ELSE
    v_current_after := NULL;
    v_current_cycle := 0;
  END IF;
  IF v_current_after IS DISTINCT FROM p_expected_after
     OR v_current_cycle IS DISTINCT FROM p_expected_cycle THEN
    RETURN false;
  END IF;

  UPDATE core.boot_reconcile_state
     SET last_run_at=now(),
         fields=jsonb_build_object(
           'after_account',p_next_after,
           'cycle',p_next_cycle
         )
   WHERE kind='graph_freshness_cursor';
  RETURN true;
END $$;
ALTER FUNCTION core.advance_graph_freshness_cursor_with_authority(text,bigint,text,bigint)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.advance_graph_freshness_cursor_with_authority(text,bigint,text,bigint)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.advance_graph_freshness_cursor_with_authority(text,bigint,text,bigint)
  TO veripsa_app;

-- ============================================================================================
-- ── OWNER COMPAT-SHADOW LENS — compat lane PR-5 (shadow reporting; docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §6). ───
-- THE GAP: the shadow compatibility analysis (VERIPSA_COMPAT_ANALYSIS, dogfood allowlist) records
-- 'compat_finding' events through record_compat_finding_with_authority (30_gate.sql) and emits ONE content-free
-- counts log line per analyzed event — but log lines are per-process and gone with the Render log window, and
-- the ledger rows sit behind per-tenant FORCE RLS. The weekly GTM review needs ONE owner-readable, cumulative,
-- content-free view of what the shadow lane has observed. This is that lens: a pure cross-tenant READ over the
-- existing 'compat_finding' event kind — NO NEW TABLE (the one-ledger law), same owner-only model + lock as
-- owner_cost_surface / owner_graph_freshness_surface (enumerate live tenants via the no-RLS
-- installation_account routing map UNION the caller's own resolved account, pin each tenant in turn, read
-- inside its RLS wall, disarm the pin between tenants).
--
-- STRICTLY AGGREGATE — the output carries COUNTS, RULE IDS and TIMESTAMPS ONLY: total observations,
-- distinct-repo COUNT, distinct head-pair COUNT, per-rule-id counts (the detail column — a bounded reason
-- CODE; the recorder's content-free wall already refuses anything body-shaped), and first/last observed
-- timestamps. NO repo names, NO paths, NO symbols, NO PR numbers, NO SHAs, NO fingerprints
-- ever appear in the output (unlike the freshness lens this one does not even emit per-coordinate rows — the
-- weekly review consumes totals, so totals are all it returns). Shadow honesty: these are OBSERVATION counts
-- from a shadow lane with zero customer-visible output — never proof, never a customer-effect claim
-- (docs/WEEKLY_GTM_REVIEW.md).
--
-- CORRECTIVE LANE S3a — TAXONOMY SPLIT (docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §2.4/§3 lane 3): the old
-- single findings_total CONFLATED observation telemetry with proven breakage. The report now reads the
-- explicit event.fact_class column (a column equality — never a reason-string parse) and returns the classes
-- SEPARATELY: contract_deltas_total / rebase_needed_total / divergent_definitions_total (observations —
-- never a breakage claim), incompatibilities_total (ONLY the evidence_backed_incompatibility class — the one
-- class that claims proven consumer breakage) with its per-detail breakdown and its own distinct head-pair
-- count, plus unclassified_total (pre-S3a append-only legacy rows — NULL fact_class; old-detector evidence,
-- never treated as current). findings_total is RENAMED observations_total (all classes) so no reader can
-- mistake the grand total for a breakage count; the class totals sum exactly to it.
--
-- BOUNDED PER CALL (the owner-sweep precedent, audit P2-2): at most p_cap accounts visited (default via the
-- owner-tunable knob 'owner_compat_scan_cap', 200, clamp 1..1000000), deterministic account_id order,
-- account_count exact (a cheap count over the routing map), capped/accounts_scanned carried so the report never
-- silently claims a full-fleet total it did not compute. Per-account work is a couple of aggregate reads over
-- the event kind — advisory visibility, not a write-correctness gate, so a bounded view is safe.
-- ZEROS WHEN EMPTY: with the shadow flags OFF (or before any finding exists) every count is 0 and the
-- timestamps are null — a read can never error just because the lane is dormant.
CREATE OR REPLACE FUNCTION core.owner_compat_shadow_surface(p_cap int DEFAULT NULL) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_acct text; v_cap int; v_account_count int := 0; v_scanned int := 0;
  v_findings bigint := 0; v_repos bigint := 0; v_pairs bigint := 0; v_accounts_with int := 0;
  v_contract bigint := 0; v_rebase bigint := 0; v_divergent bigint := 0; v_incompat bigint := 0;
  v_unclassified bigint := 0; v_pairs_incompat bigint := 0;
  v_first timestamptz; v_last timestamptz; v_rules jsonb := '{}'::jsonb; v_details jsonb := '{}'::jsonb;
  a_findings bigint; a_repos bigint; a_pairs bigint; a_first timestamptz; a_last timestamptz;
  a_contract bigint; a_rebase bigint; a_divergent bigint; a_incompat bigint;
  a_unclassified bigint; a_pairs_incompat bigint;
  r record;
BEGIN
  -- the per-call account-scan bound: the explicit arg if given (tests pin it), else the owner-tunable knob
  -- (it lives under the owner account, like the free line) — pin, read, disarm (the cost-surface pattern).
  PERFORM set_config('core.current_account', core._owner_account(), true);
  v_cap := COALESCE(p_cap, core._policy_int('owner_compat_scan_cap', 200, 1, 1000000));
  PERFORM set_config('core.current_account', '', true);
  IF v_cap < 1 THEN v_cap := 1; END IF;
  -- EXACT full tenant total (cheap, no per-account pin): live routed installs UNION the caller's own resolved
  -- account (the credential/dogfood account may have findings but no routing row — the freshness-lens shape).
  SELECT count(*)::int INTO v_account_count FROM (
      SELECT DISTINCT a FROM (
          SELECT account_id AS a FROM core.installation_account WHERE revoked_at IS NULL
          UNION
          SELECT account FROM core.resolve_session_identity() AS ri(agent, account)
      ) s WHERE a IS NOT NULL AND a <> ''
  ) t;
  -- visit ≤cap tenants' RLS walls in turn (deterministic account_id order — the bounded owner-sweep pattern).
  FOR v_acct IN
      SELECT DISTINCT a FROM (
          SELECT account_id AS a FROM core.installation_account WHERE revoked_at IS NULL
          UNION
          SELECT account FROM core.resolve_session_identity() AS ri(agent, account)
      ) s WHERE a IS NOT NULL AND a <> '' ORDER BY 1 LIMIT v_cap
  LOOP
    v_scanned := v_scanned + 1;
    PERFORM set_config('core.current_account', v_acct, true);   -- read THIS tenant inside its own RLS wall
    -- S3a: classification is the fact_class COLUMN (never a reason-string parse). NULL = pre-S3a legacy
    -- evidence, counted honestly as unclassified — the five class buckets sum exactly to the grand total.
    SELECT count(*),
           count(*) FILTER (WHERE fact_class = 'contract_delta_observation'),
           count(*) FILTER (WHERE fact_class = 'rebase_needed_observation'),
           count(*) FILTER (WHERE fact_class = 'divergent_definition_observation'),
           count(*) FILTER (WHERE fact_class = 'evidence_backed_incompatibility'),
           count(*) FILTER (WHERE fact_class IS NULL),
           count(DISTINCT repo), count(DISTINCT (commit_sha, counterparty_sha)),
           count(DISTINCT (commit_sha, counterparty_sha))
             FILTER (WHERE fact_class = 'evidence_backed_incompatibility'),
           min(occurred_at), max(occurred_at)
      INTO a_findings, a_contract, a_rebase, a_divergent, a_incompat, a_unclassified,
           a_repos, a_pairs, a_pairs_incompat, a_first, a_last
      FROM core.event WHERE account_id = v_acct AND kind = 'compat_finding';
    IF COALESCE(a_findings, 0) > 0 THEN
      v_accounts_with := v_accounts_with + 1;
      v_findings := v_findings + a_findings;
      v_contract := v_contract + a_contract;
      v_rebase := v_rebase + a_rebase;
      v_divergent := v_divergent + a_divergent;
      v_incompat := v_incompat + a_incompat;
      v_unclassified := v_unclassified + a_unclassified;
      -- repos/pairs are summed per-account DISTINCT counts: a repo/head-pair is a per-tenant fact (two tenants
      -- with the same repo name are genuinely two installations), so the sum is the honest owner-level count.
      v_repos := v_repos + a_repos;
      v_pairs := v_pairs + a_pairs;
      v_pairs_incompat := v_pairs_incompat + a_pairs_incompat;
      v_first := LEAST(COALESCE(v_first, a_first), a_first);
      v_last  := GREATEST(COALESCE(v_last, a_last), a_last);
      -- per-rule-id counts (ALL classes — observation telemetry), merged across tenants. The rule id IS the
      -- detail reason code (safe charset, recorder-enforced); an empty detail rolls up as 'unclassified'.
      FOR r IN SELECT COALESCE(NULLIF(detail, ''), 'unclassified') AS rule, count(*)::bigint AS n
                 FROM core.event WHERE account_id = v_acct AND kind = 'compat_finding' GROUP BY 1 LOOP
        v_rules := jsonb_set(v_rules, ARRAY[r.rule],
                             to_jsonb(COALESCE((v_rules->>r.rule)::bigint, 0) + r.n));
      END LOOP;
      -- per-detail counts for the EVIDENCE CLASS ONLY (S3a split): keyed by the full bounded detail code
      -- ('consumer_call_mismatch:<detail>') — the class filter is the fact_class column, never the prefix.
      FOR r IN SELECT COALESCE(NULLIF(detail, ''), 'unclassified') AS rule, count(*)::bigint AS n
                 FROM core.event WHERE account_id = v_acct AND kind = 'compat_finding'
                  AND fact_class = 'evidence_backed_incompatibility' GROUP BY 1 LOOP
        v_details := jsonb_set(v_details, ARRAY[r.rule],
                               to_jsonb(COALESCE((v_details->>r.rule)::bigint, 0) + r.n));
      END LOOP;
    END IF;
    PERFORM set_config('core.current_account', '', true);       -- disarm the pin before the next tenant
  END LOOP;
  RETURN jsonb_build_object(
    -- STRICTLY AGGREGATE (content-free): counts + rule ids + timestamps. Never a repo name, path, symbol,
    -- PR number, SHA or fingerprint.
    -- S3a: observations_total (RENAMED from findings_total) = ALL classes; the four class totals +
    -- unclassified sum exactly to it. incompatibilities_total is the ONLY breakage-claim count.
    'observations_total',           v_findings,
    'contract_deltas_total',        v_contract,
    'rebase_needed_total',          v_rebase,
    'divergent_definitions_total',  v_divergent,
    'incompatibilities_total',      v_incompat,
    'unclassified_total',           v_unclassified,
    'incompatibilities_by_detail',  v_details,
    'head_pairs_with_incompatibility', v_pairs_incompat,
    'repos_observed',         v_repos,
    'head_pairs_observed',    v_pairs,
    'accounts_with_findings', v_accounts_with,
    'findings_by_rule',       v_rules,
    'first_observed_at',      v_first,
    'last_observed_at',       v_last,
    -- bound honesty (the owner-sweep pattern): was this a top-cap view, not the full fleet? content-free ints.
    'account_count',          v_account_count,
    'capped',                 (v_scanned < v_account_count),
    'cap',                    v_cap,
    'accounts_scanned',       v_scanned
  );
END $$;
ALTER FUNCTION core.owner_compat_shadow_surface(int) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.owner_compat_shadow_surface(int) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.owner_compat_shadow_surface(int) TO veripsa_app;  -- OWNER COMPAT-SHADOW LENS: cross-tenant content-free aggregates over 'compat_finding' events (a buyer seat cannot read across tenants; strictly counts/rule-ids/timestamps)

-- ============================================================================================
-- ── OWNER ACTIVATION-FUNNEL LENS (Issue #648 / docs/PRODUCT_ACTIVATION_FUNNEL.md). ──────────────────────────
-- THE GAP: "a GitHub App install is not activation." The weekly review needs ONE owner-readable, content-free
-- view of how far live installations get down the activation path — install → selected a repo → first eligible
-- PR traffic → first non-Clear signal → repeat use — WITHOUT a web console and WITHOUT reading any tenant's
-- code. This is that lens: a pure cross-tenant READ over the EXISTING tables (installation_account / claim /
-- event / repository_lifecycle_activation) — NO NEW TABLE (the one-ledger law), the SAME owner-only model +
-- lock as owner_cost_surface / owner_compat_shadow_surface (enumerate live tenants via the no-RLS
-- installation_account routing map, pin each in turn, read inside its FORCE-RLS wall, disarm between tenants).
-- READ-ONLY REPORTING: it NEVER changes the write gate, webhook routing, or any runtime decision path — no
-- forgery token is armed, it cannot tamper. Reporting only.
--
-- STRICTLY AGGREGATE (content-free): COUNTS + latency SECONDS + TIMESTAMPS only. Never a repo name, path,
-- branch, SHA, PR number or fingerprint (it does not even emit per-account rows — the review consumes totals).
--
-- STAGE SOURCES (each verified against the schema; A-codes per the funnel doc). The GRAIN is the LIVE TENANT
-- (account) — one live account per GitHub App installation (enter_installation_with_authority derives
-- account_id='ACCT-GH-'||installation_id, 30_gate.sql), so "installations reaching a stage" == live accounts
-- reaching it. A1 also reports the raw installation-row totals for reconciliation.
--   A1 installs   — core.installation_account. installs_total (all rows) + installs_live (revoked_at IS NULL);
--                   live tenants = DISTINCT live account_id; the owner-vs-external split uses
--                   core._dev_exempt_account_ids() (external = a live account NOT on the dev-exempt allowlist,
--                   default ACCT-GH-42424242 / ACCT-GH-43434343 / ACCT-DEMO).
--   A2 selected   — core.repository_lifecycle_activation (35_lifecycle.sql): a tenant reached A2 if it holds
--                   >=1 activation for a repo with NO unsuperseded tombstone (a currently-selected repo).
--   A3 first PR   — PROXY: the earliest core.claim with change_id LIKE 'PR-%' (the App builds claim_id
--                   'PR-<n>:<path>' so change_id='PR-<n>' marks eligible PR traffic; 20_core.sql). Reached
--                   if >=1. Its claimed_at is the A3 timestamp for the A1->A3 latency below.
--   A5 first sig  — core.event kind 'warn_issued' (warn) / 'collision_held' (serialize). *** LIMITATION: a
--                   Clear or Unknown verdict writes NO event, so this measures the first WARN/SERIALIZE only,
--                   NEVER the first clear/unknown. *** Reached if >=1; the warn-vs-serialize split is reported.
--   A9 repeat-7d  — per current repo, a SECOND qualifying claim/event STRICTLY after the first and within 7
--                   days. Reached if >=1 repo shows it (the install is not a one-off).
--   latency A1->A3 — install(github_installation_created_at) -> first PR-claim, aggregated content-free
--                   (count-with-latency + median/p90 SECONDS + coarse buckets). No per-install value emitted.
--   A4 first check— per current repo, the earliest 'check_published' event on a PR head (first Veripsa Check Run
--                   actually posted on a PR), fenced exactly like A3/A5. Reached if >=1. Reported with the
--                   first-check signal split + two content-free latency aggregates (install->check, first-PR->
--                   check). NULL publication_failures (a failed post records no row → not DB-derivable).
--   A6/A7/A8      — PR-comment / ACK / required-check config are GITHUB-ONLY and NOT persisted in this DB.
--                   Returned as NULL sentinels + a note; NEVER fabricated from DB data.
--
-- CORRECTNESS (uninstall/reinstall + repo delete/recreate safety): every claim/event/activation read is fenced
-- by core._repository_generation_boundary(repo) + excludes repos carrying an UNSUPERSEDED
-- repository_lifecycle_tombstone, exactly as core.repos_for_installation does (40_surfaces.sql:329-348) — so a
-- reused PR number / recreated same-name repo cannot corrupt the counts. Per-tenant dedupe is on account_id
-- (one live account per installation); per-repo dedupe is the stable current-repo generation (repo name fenced
-- by the boundary), the same seam repos_for_installation relies on.
--
-- BOUNDED PER CALL (the owner-sweep precedent, audit P2-2): at most p_cap accounts visited (default via the
-- owner-tunable knob 'owner_activation_scan_cap', 200, clamp 1..1000000), deterministic account_id order. The
-- A1 install/account totals + the owner/external split stay EXACT (cheap counts over the no-RLS routing map,
-- no per-account pin); the A2..A9/latency figures cover the SCANNED set and carry capped/accounts_scanned so a
-- bounded read never claims a full-fleet figure it did not compute. ZEROS WHEN EMPTY: no installs / no traffic
-- reads as all-zeros (a dormant funnel can never error).
CREATE OR REPLACE FUNCTION core.owner_activation_funnel_surface(p_cap int DEFAULT NULL) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_cap int; v_account_count int := 0; v_scanned int := 0;
  v_installs_total bigint := 0; v_installs_live bigint := 0; v_live_accounts int := 0;
  v_external int := 0; v_owner int := 0; v_exempt text[];
  v_a2 int := 0; v_a3 int := 0; v_a5 int := 0; v_a9 int := 0;
  v_warn_total bigint := 0; v_serialize_total bigint := 0;
  v_lat numeric[] := '{}'; v_lat_count int := 0; v_median numeric; v_p90 numeric;
  v_b1h int := 0; v_b1d int := 0; v_b7d int := 0; v_bover int := 0;
  v_first timestamptz; v_last timestamptz; v_acct text;
  a_selected boolean; a_first_pr timestamptz; a_warn bigint; a_serialize bigint;
  a_repeat boolean; a_min timestamptz; a_max timestamptz; a_install_created timestamptz;
  -- A4 first PR Check published (Issue #648): the 'check_published' event fenced exactly like A3/A5.
  v_a4 int := 0;
  v_sig_clear int := 0; v_sig_heads int := 0; v_sig_wait int := 0; v_sig_unknown int := 0; v_sig_paused int := 0;
  v_lat_ic numeric[] := '{}';   -- install -> first check (A1->A4), content-free seconds
  v_lat_pc numeric[] := '{}';   -- first PR   -> first check (A3->A4), content-free seconds
  v_ic_n int := 0; v_ic_med numeric; v_ic_p90 numeric;
  v_pc_n int := 0; v_pc_med numeric; v_pc_p90 numeric;
  a_first_check timestamptz; a_first_check_sig text;
  -- HUMAN JUDGMENT accumulators (filled inside the per-tenant loop below, because core.event is under
  -- FORCE-RLS: a global read from the owner pin sees only the owner's own rows).
  v_hj_total int := 0; v_hj_ext int := 0; v_hj_useful_ext int := 0;
  v_hj_by jsonb := '{}'::jsonb; v_wa_by jsonb := '{}'::jsonb;
  a_is_exempt boolean; a_j text; a_a text;
BEGIN
  -- the per-call account-scan bound: the explicit arg if given (tests pin it), else the owner-tunable knob
  -- (it lives under the owner account, like the free line) — pin, read, disarm (the cost-surface pattern).
  PERFORM set_config('core.current_account', core._owner_account(), true);
  v_cap := COALESCE(p_cap, core._policy_int('owner_activation_scan_cap', 200, 1, 1000000));
  PERFORM set_config('core.current_account', '', true);
  IF v_cap < 1 THEN v_cap := 1; END IF;

  -- ── A1 (EXACT, no per-account pin) — install rows + the live-tenant count + the owner/external split. ──
  SELECT count(*), count(*) FILTER (WHERE revoked_at IS NULL)
    INTO v_installs_total, v_installs_live FROM core.installation_account;
  SELECT count(DISTINCT account_id) INTO v_live_accounts
    FROM core.installation_account WHERE revoked_at IS NULL;
  v_account_count := v_live_accounts;
  -- dev-exempt allowlist (owner's own dogfood/demo accounts) → canonicalize to a trimmed id array. external =
  -- a live account NOT on it; owner = the remainder. _dev_exempt_account_ids pins+restores the owner internally.
  SELECT COALESCE(array_agg(btrim(t)), '{}') INTO v_exempt
    FROM unnest(string_to_array(core._dev_exempt_account_ids(), ',')) t WHERE btrim(t) <> '';
  SELECT count(DISTINCT account_id) INTO v_external
    FROM core.installation_account WHERE revoked_at IS NULL AND NOT (account_id = ANY(v_exempt));
  v_owner := GREATEST(0, v_live_accounts - v_external);

  -- ── A2..A9 + latency (per-tenant, BOUNDED) — visit <=cap live tenants' RLS walls in deterministic order. ──
  FOR v_acct IN
      SELECT DISTINCT account_id FROM core.installation_account
       WHERE revoked_at IS NULL ORDER BY account_id LIMIT v_cap
  LOOP
    v_scanned := v_scanned + 1;
    PERFORM set_config('core.current_account', v_acct, true);   -- read THIS tenant inside its own FORCE-RLS wall

    -- A2: >=1 currently-selected repo (an activation whose repo has NO unsuperseded tombstone).
    SELECT EXISTS (
      SELECT 1 FROM core.repository_lifecycle_activation a
       WHERE a.account_id = v_acct
         AND NOT EXISTS (SELECT 1 FROM core.repository_lifecycle_tombstone t
                          WHERE t.account_id = v_acct AND t.repo = a.repo AND t.superseded_at IS NULL))
      INTO a_selected;

    -- A3: earliest PR-claim (change_id LIKE 'PR-%'), fenced by the generation boundary + tombstone exclusion.
    SELECT min(c.claimed_at) INTO a_first_pr
      FROM core.claim c
     WHERE c.account_id = v_acct AND c.change_id LIKE 'PR-%' AND c.repo <> ''
       AND NOT EXISTS (SELECT 1 FROM core.repository_lifecycle_tombstone t
                        WHERE t.account_id = v_acct AND t.repo = c.repo AND t.superseded_at IS NULL)
       AND c.claimed_at >= core._repository_generation_boundary(c.repo);

    -- A5: first non-Clear signal counts (warn_issued / collision_held), same fence.
    SELECT count(*) FILTER (WHERE e.kind = 'warn_issued'),
           count(*) FILTER (WHERE e.kind = 'collision_held')
      INTO a_warn, a_serialize
      FROM core.event e
     WHERE e.account_id = v_acct AND e.kind IN ('warn_issued','collision_held') AND e.repo <> ''
       AND NOT EXISTS (SELECT 1 FROM core.repository_lifecycle_tombstone t
                        WHERE t.account_id = v_acct AND t.repo = e.repo AND t.superseded_at IS NULL)
       AND e.occurred_at >= core._repository_generation_boundary(e.repo);

    -- A9: per current repo, a SECOND qualifying activity (PR-claim OR signal event) strictly after the first
    -- and within 7 days. Also the tenant's first/last activity timestamp (for the fleet first/last rollup).
    WITH acts AS (
      SELECT c.repo AS repo, c.claimed_at AS ts
        FROM core.claim c
       WHERE c.account_id = v_acct AND c.change_id LIKE 'PR-%' AND c.repo <> ''
         AND NOT EXISTS (SELECT 1 FROM core.repository_lifecycle_tombstone t
                          WHERE t.account_id = v_acct AND t.repo = c.repo AND t.superseded_at IS NULL)
         AND c.claimed_at >= core._repository_generation_boundary(c.repo)
      UNION ALL
      SELECT e.repo, e.occurred_at
        FROM core.event e
       WHERE e.account_id = v_acct AND e.kind IN ('warn_issued','collision_held') AND e.repo <> ''
         AND NOT EXISTS (SELECT 1 FROM core.repository_lifecycle_tombstone t
                          WHERE t.account_id = v_acct AND t.repo = e.repo AND t.superseded_at IS NULL)
         AND e.occurred_at >= core._repository_generation_boundary(e.repo)
    ),
    firsts AS (SELECT repo, min(ts) AS t1 FROM acts GROUP BY repo),
    seconds AS (SELECT a.repo, min(a.ts) AS t2 FROM acts a JOIN firsts f ON f.repo = a.repo AND a.ts > f.t1 GROUP BY a.repo)
    SELECT EXISTS (SELECT 1 FROM firsts f JOIN seconds s ON s.repo = f.repo WHERE s.t2 <= f.t1 + interval '7 days'),
           (SELECT min(ts) FROM acts), (SELECT max(ts) FROM acts)
      INTO a_repeat, a_min, a_max;

    -- latency A1->A3: install(github_installation_created_at) -> first PR-claim. installation_account is the
    -- no-RLS routing map so this reads regardless of pin; clamp to >=0 against clock skew. content-free seconds.
    SELECT min(github_installation_created_at) INTO a_install_created
      FROM core.installation_account WHERE account_id = v_acct AND revoked_at IS NULL;

    -- A4: earliest 'check_published' event on a PR head (first Veripsa Check actually posted on a PR — Issue
    -- #648), same generation/tombstone fence as A3/A5. Content-free: reads only occurred_at + the bounded signal.
    SELECT e.occurred_at, e.detail INTO a_first_check, a_first_check_sig
      FROM core.event e
     WHERE e.account_id = v_acct AND e.kind = 'check_published' AND e.repo <> '' AND e.path LIKE 'PR-%'
       AND NOT EXISTS (SELECT 1 FROM core.repository_lifecycle_tombstone t
                        WHERE t.account_id = v_acct AND t.repo = e.repo AND t.superseded_at IS NULL)
       AND e.occurred_at >= core._repository_generation_boundary(e.repo)
     ORDER BY e.occurred_at ASC LIMIT 1;

    IF a_selected THEN v_a2 := v_a2 + 1; END IF;
    IF a_first_pr IS NOT NULL THEN v_a3 := v_a3 + 1; END IF;
    IF COALESCE(a_warn, 0) + COALESCE(a_serialize, 0) > 0 THEN v_a5 := v_a5 + 1; END IF;

    -- LATEST human judgment per change for THIS tenant (append-only ledger: a re-answer adds a row, so
    -- take the most recent per change and never double-count the person who changed their mind).
    a_is_exempt := (v_acct = ANY(v_exempt));
    FOR a_j, a_a IN
      SELECT substring(detail FROM 'judgment=([a-z-]+)'), substring(detail FROM 'action=([a-z-]+)')
        FROM (SELECT DISTINCT ON (repo, branch, path) detail
                FROM core.event WHERE kind='human_judgment'
               ORDER BY repo, branch, path, occurred_at DESC) latest
    LOOP
      IF a_j IS NULL THEN CONTINUE; END IF;
      v_hj_total := v_hj_total + 1;
      v_hj_by := jsonb_set(v_hj_by, ARRAY[a_j],
                           to_jsonb(COALESCE((v_hj_by->>a_j)::int, 0) + 1), true);
      IF a_a IS NOT NULL THEN
        v_wa_by := jsonb_set(v_wa_by, ARRAY[a_a],
                             to_jsonb(COALESCE((v_wa_by->>a_a)::int, 0) + 1), true);
      END IF;
      -- Only a NON-allowlisted account can move the external counters. The owner's own dogfood judgment
      -- must never be able to make the product look externally validated.
      IF NOT a_is_exempt THEN
        v_hj_ext := v_hj_ext + 1;
        IF a_j = 'useful' THEN v_hj_useful_ext := v_hj_useful_ext + 1; END IF;
      END IF;
    END LOOP;
    IF a_repeat THEN v_a9 := v_a9 + 1; END IF;
    v_warn_total := v_warn_total + COALESCE(a_warn, 0);
    v_serialize_total := v_serialize_total + COALESCE(a_serialize, 0);
    IF a_min IS NOT NULL THEN v_first := LEAST(COALESCE(v_first, a_min), a_min); END IF;
    IF a_max IS NOT NULL THEN v_last := GREATEST(COALESCE(v_last, a_max), a_max); END IF;
    IF a_first_pr IS NOT NULL AND a_install_created IS NOT NULL THEN
      v_lat := v_lat || GREATEST(0, EXTRACT(EPOCH FROM (a_first_pr - a_install_created)))::numeric;
    END IF;

    -- A4 accumulation: count the tenant, bucket its first-check signal, and collect the two content-free
    -- latencies (install->first-check A1->A4, first-PR->first-check A3->A4). Clamp to >=0 against clock skew.
    IF a_first_check IS NOT NULL THEN
      v_a4 := v_a4 + 1;
      CASE a_first_check_sig
        WHEN 'clear'        THEN v_sig_clear   := v_sig_clear   + 1;
        WHEN 'heads_up'     THEN v_sig_heads   := v_sig_heads   + 1;
        WHEN 'wait_in_line' THEN v_sig_wait    := v_sig_wait    + 1;
        WHEN 'paused'       THEN v_sig_paused  := v_sig_paused  + 1;
        ELSE                     v_sig_unknown := v_sig_unknown + 1;
      END CASE;
      IF a_install_created IS NOT NULL THEN
        v_lat_ic := v_lat_ic || GREATEST(0, EXTRACT(EPOCH FROM (a_first_check - a_install_created)))::numeric;
      END IF;
      IF a_first_pr IS NOT NULL THEN
        v_lat_pc := v_lat_pc || GREATEST(0, EXTRACT(EPOCH FROM (a_first_check - a_first_pr)))::numeric;
      END IF;
    END IF;

    PERFORM set_config('core.current_account', '', true);       -- disarm the pin before the next tenant
  END LOOP;

  -- latency rollups over the collected content-free seconds (median/p90 + mutually-exclusive coverage buckets).
  v_lat_count := COALESCE(array_length(v_lat, 1), 0);
  IF v_lat_count > 0 THEN
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY x),
           percentile_cont(0.9) WITHIN GROUP (ORDER BY x)
      INTO v_median, v_p90 FROM unnest(v_lat) x;
    SELECT count(*) FILTER (WHERE x < 3600),
           count(*) FILTER (WHERE x >= 3600 AND x < 86400),
           count(*) FILTER (WHERE x >= 86400 AND x < 604800),
           count(*) FILTER (WHERE x >= 604800)
      INTO v_b1h, v_b1d, v_b7d, v_bover FROM unnest(v_lat) x;
  END IF;

  -- A4 latency rollups (median/p90 over the collected content-free seconds), same shape as the A1->A3 rollup.
  v_ic_n := COALESCE(array_length(v_lat_ic, 1), 0);
  IF v_ic_n > 0 THEN
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY x), percentile_cont(0.9) WITHIN GROUP (ORDER BY x)
      INTO v_ic_med, v_ic_p90 FROM unnest(v_lat_ic) x;
  END IF;
  v_pc_n := COALESCE(array_length(v_lat_pc, 1), 0);
  IF v_pc_n > 0 THEN
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY x), percentile_cont(0.9) WITHIN GROUP (ORDER BY x)
      INTO v_pc_med, v_pc_p90 FROM unnest(v_lat_pc) x;
  END IF;

  RETURN jsonb_build_object(
    -- A1 — EXACT regardless of the scan cap (counts over the no-RLS routing map, no per-account pin).
    'installs_total',    v_installs_total,
    'installs_live',     v_installs_live,
    'live_accounts',     v_live_accounts,
    'external_accounts', v_external,
    'owner_accounts',    v_owner,
    -- A2..A9 — over the SCANNED top-cap set (flagged by capped/accounts_scanned below).
    'a2_selected_repo',  v_a2,
    'a3_first_pr',       v_a3,
    'a5_first_signal',   v_a5,
    'a9_repeat_7d',      v_a9,
    -- A4 first PR Check published (Issue #648) — now DB-derivable from the 'check_published' event ledger. Counts
    -- over the SCANNED set (like A2..A9); the signal split + the two content-free latency aggregates alongside it.
    'a4_first_check',    v_a4,
    'a4_signal_distribution', jsonb_build_object(
        'clear', v_sig_clear, 'heads_up', v_sig_heads, 'wait_in_line', v_sig_wait,
        'unknown', v_sig_unknown, 'paused', v_sig_paused,
        -- a FAILED/skipped post never records a row (the recorder is called only after posted=True), so
        -- publication failures are not DB-derivable here — an explicit NULL, never fabricated from success rows.
        'publication_failures', NULL),
    'a3_to_a4_conversion', jsonb_build_object('a3', v_a3, 'a4', v_a4),
    'install_to_check_latency', jsonb_build_object(
        'count_with_latency', v_ic_n,
        'median_seconds', CASE WHEN v_ic_med IS NULL THEN NULL ELSE round(v_ic_med)::bigint END,
        'p90_seconds',    CASE WHEN v_ic_p90 IS NULL THEN NULL ELSE round(v_ic_p90)::bigint END),
    'pr_event_to_check_latency', jsonb_build_object(
        'count_with_latency', v_pc_n,
        'median_seconds', CASE WHEN v_pc_med IS NULL THEN NULL ELSE round(v_pc_med)::bigint END,
        'p90_seconds',    CASE WHEN v_pc_p90 IS NULL THEN NULL ELSE round(v_pc_p90)::bigint END),
    -- A5 signal split (warn vs serialize) — the only non-clear signals the DB records (clear/unknown write none).
    'signals',           jsonb_build_object('warn', v_warn_total, 'serialize', v_serialize_total),
    -- HUMAN JUDGMENT (the External Validation Gate's only market-validation signal). Counted separately
    -- for EXTERNAL vs INTERNAL accounts using the SAME allowlist that classifies installs above, because
    -- the owner's own dogfood judgments must never be able to make this look validated. `useful_external`
    -- is the number the honest-conclusion rule keys on: while it is 0, the truthful summary stays
    -- "Product-ready. External-validation-ready. Market value still unvalidated."
    -- Content-free: counts of two closed enums, no bodies, no quotes, no free text.
    'human_judgment', jsonb_build_object(
      'total', v_hj_total, 'external_total', v_hj_ext,
      'useful_external', v_hj_useful_ext, 'by_judgment', v_hj_by),
    'workflow_action', v_wa_by,
    -- A1->A3 latency (content-free seconds only) — count-with-latency + median/p90 + coverage buckets.
    'a1_to_a3_latency',  jsonb_build_object(
        'count_with_latency', v_lat_count,
        'median_seconds', CASE WHEN v_median IS NULL THEN NULL ELSE round(v_median)::bigint END,
        'p90_seconds',    CASE WHEN v_p90 IS NULL THEN NULL ELSE round(v_p90)::bigint END,
        'buckets', jsonb_build_object('under_1h', v_b1h, '1h_to_1d', v_b1d, '1d_to_7d', v_b7d, 'over_7d', v_bover)),
    -- A6/A7/A8 — GITHUB-ONLY, not persisted here: NULL sentinels + a note, NEVER fabricated. (A4 moved out — it
    -- is now DB-derivable above via the 'check_published' event; A6 PR-comment / A7 ACK / A8 required-check stay
    -- GitHub-only.)
    'github_only_stages', jsonb_build_object(
        'note', 'not DB-derivable (GitHub-only)',
        'a6_pr_comment',     NULL,
        'a7_first_ack',      NULL,
        'a8_required_check', NULL),
    'first_activity_at', v_first,
    'last_activity_at',  v_last,
    -- bound honesty (the owner-sweep pattern): was this a top-cap view, not the full fleet? content-free ints.
    'account_count',     v_account_count,
    'capped',            (v_scanned < v_account_count),
    'cap',               v_cap,
    'accounts_scanned',  v_scanned
  );
END $$;
ALTER FUNCTION core.owner_activation_funnel_surface(int) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.owner_activation_funnel_surface(int) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.owner_activation_funnel_surface(int) TO veripsa_app;  -- OWNER ACTIVATION-FUNNEL LENS: cross-tenant content-free activation counts (install -> selected -> first PR -> first signal -> repeat); a buyer seat cannot read across tenants

-- ============================================================================================
-- ── OWNER ALERT BOARD — "an alert logged but ignored is meaningless" (the PO's principle). ───────────────────
-- THE GAP: alerts.AlertSink is in-memory + stdout + an OPTIONAL outbound webhook. With NO VERIPSA_ALERT_WEBHOOK_URL
-- configured (the App's default), an ACTIVE alert (graph_stale / account_over_line / db_usage_high / the durable-
-- inbox DLQ + backlog depth alerts) lives ONLY in the per-process Render logs and the sink's in-memory `_firing`
-- set — invisible the moment nobody is tailing the log, and GONE on the next deploy/restart. So a real, persistent
-- warning (the prod graph_stale that spammed forever) was written and then ignored = useless. This table is the
-- DURABLE, OWNER-READABLE alert board: the watchdog persists every fire/resolve here, and the owner reads the LIVE
-- set via core.active_alerts_with_authority(). A real notification (email / the owner dashboard) hooks in by
-- POLLING that same surface — the surface is the join point, the channel is a deploy detail.
--
-- It is HOST/OPERATOR state, NOT a tenant fact: there is NO per-account RLS (the alert key — 'graph_stale' — is the
-- identity; some alerts are whole-fleet and carry no account at all). Like installation_account / webhook_delivery
-- it is reachable ONLY through the *_with_authority SECURITY DEFINER fns the App is granted; a buyer seat can
-- neither write nor read it. CONTENT-FREE: an alert row carries the key + level + a short, already-content-free
-- message + a small jsonb of the SAME counts the alert body emits (counts/percent/age) — never a repo, path, sha,
-- id, or body (the watchdog's fields are content-free by construction; _safe_alert_detail re-caps the message).
CREATE TABLE IF NOT EXISTS core.active_alert (
    alert_key text PRIMARY KEY,            -- the de-dupe key the AlertSink fires on (one row per condition)
    level text NOT NULL,                   -- 'warning' | 'critical' (matches the sink's levels)
    message text NOT NULL,                 -- the content-free one-line message (already counts-only)
    fields jsonb DEFAULT '{}'::jsonb NOT NULL,  -- the same content-free counts the alert body carries
    first_fired_at timestamptz DEFAULT now() NOT NULL,  -- when this condition STARTED firing (kept across re-fires)
    last_fired_at timestamptz DEFAULT now() NOT NULL,   -- the most recent fire (refreshed each edge)
    CONSTRAINT active_alert_key_len CHECK (length(alert_key) BETWEEN 1 AND 120),
    CONSTRAINT active_alert_level_check CHECK (level = ANY (ARRAY['info','warning','critical'])),
    CONSTRAINT active_alert_message_len CHECK (length(message) <= 400)
);
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'active_alert'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.active_alert OWNER TO veripsa_migrator;
  END IF;
END $$;
CREATE INDEX CONCURRENTLY IF NOT EXISTS active_alert_by_fired ON core.active_alert (last_fired_at DESC);
-- LOCK THE TABLE DOWN (same pattern as core.webhook_delivery): REVOKE ALL FROM PUBLIC so NO tenant role can read
-- or write it directly — every access goes through the *_with_authority SECURITY DEFINER fns (granted to the App
-- for write, to the App/owner for the read). There is NO per-account RLS (this is the host's cross-tenant alert
-- board, keyed by alert key, not a tenant fact), so the table-level REVOKE IS the wall here. The tamper-grants
-- gate proves a token-armed tenant is STILL refused a direct write by exactly this missing grant.
REVOKE ALL ON TABLE core.active_alert FROM PUBLIC;

-- raise_active_alert_with_authority: the watchdog's AlertSink calls this when an alert FIRES (the transition into a
-- bad state). UPSERT: a new condition inserts a row stamped first_fired_at=now; a re-fire of a still-firing
-- condition refreshes level/message/fields/last_fired_at but PRESERVES first_fired_at (so "how long has this been
-- firing?" stays answerable). Content-free + fail-safe: the message is re-capped, the fields kept as a bounded
-- jsonb. No per-account RLS on this table (operator board), so no account pin / token is needed — it is written
-- only by the App identity through this granted fn. App-delegation grant only (a buyer seat can never write it).
CREATE OR REPLACE FUNCTION core.raise_active_alert_with_authority(p_key text, p_level text, p_message text, p_fields jsonb DEFAULT '{}'::jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_key text; v_level text; v_msg text; v_fields jsonb;
BEGIN
  v_key := left(NULLIF(btrim(COALESCE(p_key,'')),''),120);
  IF v_key IS NULL THEN RAISE EXCEPTION 'alert key required' USING ERRCODE='23514'; END IF;
  -- clamp the level to the allowed set (a junk level FAILS SAFE to 'warning', never violates the CHECK / crashes
  -- the watchdog tick on a typo in a future caller).
  v_level := lower(COALESCE(p_level,''));
  IF v_level NOT IN ('info','warning','critical') THEN v_level := 'warning'; END IF;
  v_msg := left(COALESCE(p_message,''),400);
  -- keep fields ONLY when a real object; anything else → empty (defensive; the sink passes a counts object).
  v_fields := CASE WHEN jsonb_typeof(COALESCE(p_fields,'{}'::jsonb)) = 'object' THEN p_fields ELSE '{}'::jsonb END;
  INSERT INTO core.active_alert(alert_key, level, message, fields, first_fired_at, last_fired_at)
  VALUES (v_key, v_level, v_msg, v_fields, now(), now())
  ON CONFLICT (alert_key) DO UPDATE
    SET level = EXCLUDED.level, message = EXCLUDED.message, fields = EXCLUDED.fields, last_fired_at = now();
  RETURN jsonb_build_object('ok', true, 'key', v_key, 'level', v_level);
END $$;
ALTER FUNCTION core.raise_active_alert_with_authority(text,text,text,jsonb) OWNER TO veripsa_migrator;

-- clear_active_alert_with_authority: the AlertSink calls this when an alert RESOLVES (the condition cleared). DELETE
-- the row so the board reflects only what is CURRENTLY firing. Idempotent (clearing an absent key is a no-op).
-- App-delegation only.
CREATE OR REPLACE FUNCTION core.clear_active_alert_with_authority(p_key text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_key text; v_n int;
BEGIN
  v_key := left(NULLIF(btrim(COALESCE(p_key,'')),''),120);
  IF v_key IS NULL THEN RAISE EXCEPTION 'alert key required' USING ERRCODE='23514'; END IF;
  DELETE FROM core.active_alert WHERE alert_key = v_key;  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN jsonb_build_object('ok', true, 'key', v_key, 'cleared', v_n);
END $$;
ALTER FUNCTION core.clear_active_alert_with_authority(text) OWNER TO veripsa_migrator;

-- active_alerts_with_authority: the OWNER's read of the LIVE alert board — the surface that makes a logged-but-
-- ignored alert VISIBLE. Returns the currently-firing alerts (most-recent first) + a count + the worst level, so
-- the PO/platform (or a polling notifier) can SEE what is wrong RIGHT NOW without tailing Render logs. Read-only
-- (no token armed → cannot tamper). Content-free by construction (the rows are content-free). Owner/App-delegation
-- only (REVOKE PUBLIC; GRANT veripsa_app) — a buyer seat is DENIED, exactly like owner_cost_surface / the freshness
-- lens (this is the host's operational board, not a tenant surface). Bounded: at most p_limit rows (default 200 —
-- the alert keys are a small fixed set, so this is a generous ceiling, not a real cap).
CREATE OR REPLACE FUNCTION core.active_alerts_with_authority(p_limit int DEFAULT 200)
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_limit int; v_rows jsonb;
BEGIN
  v_limit := GREATEST(1, LEAST(COALESCE(p_limit, 200), 10000));
  SELECT COALESCE(jsonb_agg(s.a ORDER BY s.lf DESC), '[]'::jsonb) INTO v_rows FROM (
    SELECT jsonb_build_object(
      'key', alert_key, 'level', level, 'message', message, 'fields', fields,
      'first_fired_at', first_fired_at, 'last_fired_at', last_fired_at,
      'age_seconds', GREATEST(0, round(EXTRACT(EPOCH FROM (now() - first_fired_at)))::bigint)
    ) AS a, last_fired_at AS lf
    FROM core.active_alert ORDER BY last_fired_at DESC LIMIT v_limit
  ) s;
  RETURN jsonb_build_object(
    'alerts', v_rows,
    'active_count', jsonb_array_length(v_rows),
    -- the worst severity currently firing (critical > warning > info), so a dashboard can badge red vs amber at a
    -- glance; NULL when nothing is firing (a clean board).
    'worst_level', (SELECT CASE WHEN bool_or(level='critical') THEN 'critical'
                                WHEN bool_or(level='warning')  THEN 'warning'
                                WHEN count(*) > 0              THEN 'info' ELSE NULL END
                      FROM core.active_alert)
  );
END $$;
ALTER FUNCTION core.active_alerts_with_authority(int) OWNER TO veripsa_migrator;

REVOKE EXECUTE ON FUNCTION core.raise_active_alert_with_authority(text,text,text,jsonb) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.clear_active_alert_with_authority(text) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.active_alerts_with_authority(int) FROM PUBLIC, veripsa_writer;
GRANT  EXECUTE ON FUNCTION core.raise_active_alert_with_authority(text,text,text,jsonb) TO veripsa_app;  -- the watchdog persists a FIRING alert (App-delegation only)
GRANT  EXECUTE ON FUNCTION core.clear_active_alert_with_authority(text) TO veripsa_app;  -- the watchdog clears a RESOLVED alert (App-delegation only)
GRANT  EXECUTE ON FUNCTION core.active_alerts_with_authority(int) TO veripsa_app;  -- OWNER ALERT BOARD: the live firing-alert set (a buyer seat is DENIED, like the cost/freshness lenses)

-- ============================================================================================
-- ── BOOT-RECONCILE LAST-RUN TIMESTAMP — Render free-tier cold-start throttle. ─────────────────────────────────
-- THE GAP (perf follow-up audit, Round-2): boot_reconcile runs every WAKE. After the keep-warm pinger, real cold
-- starts are rare (the pinger keeps the service alive past Render's 15-min idle), so the per-wake reconcile is
-- LESS hot than it used to be — but it still scans tenants × open PRs on every boot (rolling deploys, every
-- platform recycle, every real cold start). When wakes happen close together (a flapping deploy / a manual
-- restart shortly after a planned deploy), boot_reconcile fires AGAIN even though the previous sweep finished
-- minutes ago and the live webhook path has been the primary self-heal since. The cap stops THIS sweep at
-- VERIPSA_BOOT_RECONCILE_CAP repos; the timestamp stops the NEXT sweep from re-running before its useful again.
--
-- IT IS HOST/OPERATOR STATE, NOT A TENANT FACT (same shape as core.active_alert): the boot reconcile is a global
-- restart-safety sweep across every installation the App has, not a per-tenant operation. NO per-account RLS;
-- reachable ONLY through *_with_authority SECURITY DEFINER fns the App is granted; a buyer seat can neither
-- read nor write it. CONTENT-FREE: one timestamp + the count the reconcile returned (repos seen) + the
-- installation count; the separate boot route and freshness cursors store only bounded opaque account/repository
-- ids and a numeric fleet cycle — no repo names, paths, PR ids, source, diff, payload, or code.
--
-- BOUNDED KV table (NOT one row per repo / per installation): one 'boot_reconcile' throttle row, one
-- 'boot_reconcile_cursor' repository keyset high-water, and one 'graph_freshness_cursor' account keyset/cycle.
-- The 'kind' column is the lone PK so a future host-level job can park its OWN bounded control row here.
CREATE TABLE IF NOT EXISTS core.boot_reconcile_state (
    kind text PRIMARY KEY,                  -- boot_reconcile / boot_reconcile_cursor / graph_freshness_cursor
    last_run_at timestamptz DEFAULT now() NOT NULL,  -- when the previous reconcile finished
    fields jsonb DEFAULT '{}'::jsonb NOT NULL,       -- content-free counts the previous run returned (reconciled/installations/repos)
    CONSTRAINT boot_reconcile_state_kind_len CHECK (length(kind) BETWEEN 1 AND 64)
);
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'boot_reconcile_state'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.boot_reconcile_state OWNER TO veripsa_migrator;
  END IF;
END $$;
-- LOCK THE TABLE DOWN (same pattern as core.active_alert / core.webhook_delivery): every access goes through the
-- *_with_authority SECURITY DEFINER fns. There is no per-account RLS (this is a host-level throttle, not a
-- tenant fact) so the table-level REVOKE IS the wall here.
REVOKE ALL ON TABLE core.boot_reconcile_state FROM PUBLIC;

-- read_boot_reconcile_last_run_with_authority: how many SECONDS since the previous boot_reconcile finished, or
-- NULL when the table is empty (no previous run on record — the first-install / first-deploy case). server_boot
-- consults this on EVERY boot before launching the reconcile thread; the caller compares against its own minimum
-- interval (VERIPSA_BOOT_RECONCILE_MIN_INTERVAL_MIN) and either skips with a log line or proceeds. App-delegation
-- only (a buyer seat is denied). Read-only (no token armed → cannot tamper).
CREATE OR REPLACE FUNCTION core.read_boot_reconcile_last_run_with_authority()
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_last timestamptz; v_age_seconds bigint; v_fields jsonb;
BEGIN
  SELECT last_run_at, fields INTO v_last, v_fields
    FROM core.boot_reconcile_state WHERE kind = 'boot_reconcile';
  IF v_last IS NULL THEN
    RETURN jsonb_build_object('found', false, 'age_seconds', NULL, 'last_run_at', NULL, 'fields', '{}'::jsonb);
  END IF;
  v_age_seconds := GREATEST(0, round(EXTRACT(EPOCH FROM (now() - v_last)))::bigint);
  RETURN jsonb_build_object('found', true, 'age_seconds', v_age_seconds, 'last_run_at', v_last,
                            'fields', COALESCE(v_fields, '{}'::jsonb));
END $$;
ALTER FUNCTION core.read_boot_reconcile_last_run_with_authority() OWNER TO veripsa_migrator;

-- mark_boot_reconcile_run_with_authority: server_boot calls this AFTER the reconcile finishes successfully. UPSERT
-- on the single 'boot_reconcile' row: stamp last_run_at=now() + store the content-free counts the reconcile
-- returned (reconciled / installations / repos), so the owner can see when the last sweep ran via the same surface.
-- Content-free + fail-safe: fields kept as a bounded jsonb. App-delegation only.
CREATE OR REPLACE FUNCTION core.mark_boot_reconcile_run_with_authority(p_fields jsonb DEFAULT '{}'::jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_fields jsonb;
BEGIN
  -- keep fields ONLY when a real object; anything else → empty (defensive; the caller passes the reconcile result).
  v_fields := CASE WHEN jsonb_typeof(COALESCE(p_fields,'{}'::jsonb)) = 'object' THEN p_fields ELSE '{}'::jsonb END;
  INSERT INTO core.boot_reconcile_state(kind, last_run_at, fields)
  VALUES ('boot_reconcile', now(), v_fields)
  ON CONFLICT (kind) DO UPDATE
    SET last_run_at = now(), fields = EXCLUDED.fields;
  RETURN jsonb_build_object('ok', true, 'kind', 'boot_reconcile');
END $$;
ALTER FUNCTION core.mark_boot_reconcile_run_with_authority(jsonb) OWNER TO veripsa_migrator;

REVOKE EXECUTE ON FUNCTION core.read_boot_reconcile_last_run_with_authority() FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.mark_boot_reconcile_run_with_authority(jsonb) FROM PUBLIC, veripsa_writer;
GRANT  EXECUTE ON FUNCTION core.read_boot_reconcile_last_run_with_authority() TO veripsa_app;  -- server_boot reads on EVERY boot (the throttle)
GRANT  EXECUTE ON FUNCTION core.mark_boot_reconcile_run_with_authority(jsonb) TO veripsa_app;  -- server_boot stamps AFTER a successful reconcile

-- ── DURABLE BOOT-RECONCILE ROUTE CURSOR ──────────────────────────────────────────────────────────────────
-- The App-level GET /app/installations inventory is deliberately bounded.  It is therefore not a correctness
-- enumerator: once an App has more rows than that operational cap, installations after the cap never appear, and
-- asking each visible installation for only its first `cap` repositories strands every later repository too.
--
-- Boot reconciliation now walks the live routes already admitted by durable lifecycle authority in PostgreSQL.
-- repository_lifecycle_activation is the current stable repository object for a tenant coordinate; joining it to
-- a non-revoked installation route gives the exact GitHub installation token the worker must mint.  The primary
-- webhook/durable-inbox path remains the discovery authority for a repository that has never reached PostgreSQL.
--
-- The cursor is a separate row in the existing host-control table.  Persist only bounded opaque account and stable
-- GitHub repository ids — never a repository name, path, PR id, source, diff, or payload.  A composite key is used
-- because a transfer can briefly leave the globally-stable repository id visible in two account lifecycles.  The
-- caller advances after EACH attempted repository, including a fail-soft attempt, so one poison route cannot
-- permanently starve every route after it.  When no key sorts after the cursor, the page wraps to the beginning.
CREATE OR REPLACE FUNCTION core.read_boot_reconcile_route_page_with_authority(
    p_limit int DEFAULT 200)
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_limit int;
  v_cursor_fields jsonb;
  v_cursor_account text;
  v_cursor_repository_id text;
  v_scan_account text;
  v_scan_repository_id text;
  v_prior_account text;
  v_routes jsonb := '[]'::jsonb;
  v_wrapped boolean := false;
  v_count int := 0;
  v_pass int;
  v_route record;
  v_activation record;
BEGIN
  v_limit := LEAST(GREATEST(COALESCE(p_limit,200),1),200);
  SELECT fields INTO v_cursor_fields
    FROM core.boot_reconcile_state
   WHERE kind='boot_reconcile_cursor';
  v_cursor_account := NULLIF(COALESCE(v_cursor_fields->>'account_id',''),'');
  v_cursor_repository_id := NULLIF(COALESCE(v_cursor_fields->>'repository_id',''),'');
  -- A malformed operator/control-plane row is not ordering authority.  Treat it as an empty cursor; the first
  -- successful CAS advance below overwrites it with canonical bounded ids.
  IF v_cursor_account IS NULL
     OR length(v_cursor_account)>200
     OR v_cursor_repository_id IS NULL
     OR length(v_cursor_repository_id)>32
     OR v_cursor_repository_id !~ '^[1-9][0-9]*$' THEN
    v_cursor_account := NULL;
    v_cursor_repository_id := NULL;
  END IF;
  v_scan_account := v_cursor_account;
  v_scan_repository_id := v_cursor_repository_id;
  v_prior_account := current_setting('core.current_account',true);

  -- FORCE-RLS is part of the tenant moat even for veripsa_migrator-owned SECURITY DEFINER functions. Enumerate
  -- only the non-RLS routing map globally, then pin each candidate account before reading its activation rows.
  -- The cursor prevents this bounded pass from restarting at the first tenant on every worker wake.
  FOR v_pass IN 1..2 LOOP
    v_routes := '[]'::jsonb;
    v_count := 0;
    FOR v_route IN
      SELECT DISTINCT ON (route.account_id COLLATE "C")
             route.account_id,
             route.github_installation_id,
             route.github_installation_created_at
        FROM core.installation_account route
       WHERE route.revoked_at IS NULL
         AND route.github_installation_id IS NOT NULL
         AND length(route.github_installation_id) BETWEEN 1 AND 32
         AND route.github_installation_id ~ '^[1-9][0-9]*$'
         AND route.github_installation_created_at IS NOT NULL
         AND (
           v_scan_account IS NULL
           OR route.account_id COLLATE "C" >= v_scan_account COLLATE "C"
         )
       ORDER BY route.account_id COLLATE "C",
                route.github_installation_created_at DESC,
                route.installation_id COLLATE "C"
    LOOP
      PERFORM set_config('core.current_account',v_route.account_id,true);
      FOR v_activation IN
        SELECT activation.repository_id,activation.repo
          FROM core.repository_lifecycle_activation activation
         WHERE activation.account_id=v_route.account_id
           AND length(activation.repository_id) BETWEEN 1 AND 32
           AND activation.repository_id ~ '^[1-9][0-9]*$'
           AND NOT EXISTS (
             SELECT 1
               FROM core.account_lifecycle_tombstone tombstone
              WHERE tombstone.account_id=v_route.account_id
                AND tombstone.active
           )
           AND (
             v_scan_account IS NULL
             OR v_route.account_id COLLATE "C" > v_scan_account COLLATE "C"
             OR (
               v_route.account_id=v_scan_account
               AND (
                 length(activation.repository_id)>length(v_scan_repository_id)
                 OR (
                   length(activation.repository_id)=length(v_scan_repository_id)
                   AND activation.repository_id COLLATE "C"
                       > v_scan_repository_id COLLATE "C"
                 )
               )
             )
           )
         ORDER BY length(activation.repository_id),
                  activation.repository_id COLLATE "C"
         LIMIT (v_limit-v_count)
      LOOP
        v_routes := v_routes || jsonb_build_array(jsonb_build_object(
          'account_id',v_route.account_id,
          'github_installation_id',v_route.github_installation_id,
          'github_installation_created_at',
            v_route.github_installation_created_at,
          'repository_id',v_activation.repository_id,
          'repo',v_activation.repo
        ));
        v_count := v_count+1;
        EXIT WHEN v_count>=v_limit;
      END LOOP;
      EXIT WHEN v_count>=v_limit;
    END LOOP;
    EXIT WHEN v_count>0 OR v_cursor_account IS NULL;
    -- No live key follows the persisted cursor: start the next cycle at the first key. Keep the original cursor
    -- in the response so the caller's first wrapped advance still CASes from the actual stored high-water.
    v_wrapped := true;
    v_scan_account := NULL;
    v_scan_repository_id := NULL;
  END LOOP;
  PERFORM set_config('core.current_account',COALESCE(v_prior_account,''),true);

  RETURN jsonb_build_object(
    'routes',v_routes,
    'cursor',jsonb_build_object(
      'account_id',v_cursor_account,
      'repository_id',v_cursor_repository_id
    ),
    'wrapped',v_wrapped
  );
END $$;
ALTER FUNCTION core.read_boot_reconcile_route_page_with_authority(int) OWNER TO veripsa_migrator;

-- Called only after the worker has pinned this account and taken the shared account-lifecycle SESSION lock.  Every
-- install generation mutation takes the exclusive counterpart, so this exact re-check closes page(A)→replace(B)
-- between the page read and GitHub writes.  It also re-checks current repository authority under the already-held
-- stable-id + coordinate locks.  Identity comes from the pinned session; callers cannot probe another tenant.
CREATE OR REPLACE FUNCTION core.boot_reconcile_route_is_current_with_authority(
    p_installation_id text,
    p_installation_created_at timestamptz,
    p_repo text,
    p_repository_id text)
    RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text;
BEGIN
  SELECT agent,account INTO v_agent,v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  RETURN EXISTS (
    SELECT 1
      FROM core.installation_account route
      JOIN core.repository_lifecycle_activation activation
        ON activation.account_id=route.account_id
       AND activation.repository_id=p_repository_id
       AND activation.repo=p_repo
     WHERE route.account_id=v_account
       AND route.revoked_at IS NULL
       AND route.github_installation_id=p_installation_id
       AND route.github_installation_created_at=p_installation_created_at
       AND NOT EXISTS (
         SELECT 1
           FROM core.account_lifecycle_tombstone tombstone
          WHERE tombstone.account_id=v_account
            AND tombstone.active
       )
  );
END $$;
ALTER FUNCTION core.boot_reconcile_route_is_current_with_authority(text,timestamptz,text,text)
  OWNER TO veripsa_migrator;

CREATE OR REPLACE FUNCTION core.advance_boot_reconcile_route_cursor_with_authority(
    p_expected_account text,
    p_expected_repository_id text,
    p_next_account text,
    p_next_repository_id text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_fields jsonb;
  v_current_account text;
  v_current_repository_id text;
  v_inserted int := 0;
BEGIN
  IF p_next_account IS NULL
     OR length(p_next_account) NOT BETWEEN 1 AND 200
     OR p_next_repository_id IS NULL
     OR length(p_next_repository_id) NOT BETWEEN 1 AND 32
     OR p_next_repository_id !~ '^[1-9][0-9]*$' THEN
    RAISE EXCEPTION 'boot reconcile cursor next key is malformed' USING ERRCODE='22023';
  END IF;
  IF (p_expected_account IS NULL) <> (p_expected_repository_id IS NULL)
     OR (
       p_expected_account IS NOT NULL
       AND (
         length(p_expected_account) NOT BETWEEN 1 AND 200
         OR length(p_expected_repository_id) NOT BETWEEN 1 AND 32
         OR p_expected_repository_id !~ '^[1-9][0-9]*$'
       )
     ) THEN
    RAISE EXCEPTION 'boot reconcile cursor expected key is malformed' USING ERRCODE='22023';
  END IF;

  SELECT fields INTO v_fields
    FROM core.boot_reconcile_state
   WHERE kind='boot_reconcile_cursor'
   FOR UPDATE;
  IF NOT FOUND THEN
    IF p_expected_account IS NOT NULL THEN
      RETURN jsonb_build_object('advanced',false,'reason','cursor changed');
    END IF;
    INSERT INTO core.boot_reconcile_state(kind,last_run_at,fields)
    VALUES (
      'boot_reconcile_cursor',
      now(),
      jsonb_build_object(
        'account_id',p_next_account,
        'repository_id',p_next_repository_id
      )
    )
    ON CONFLICT (kind) DO NOTHING;
    GET DIAGNOSTICS v_inserted=ROW_COUNT;
    RETURN jsonb_build_object(
      'advanced',v_inserted=1,
      'reason',CASE WHEN v_inserted=1 THEN NULL ELSE 'cursor changed' END
    );
  END IF;

  v_current_account := NULLIF(COALESCE(v_fields->>'account_id',''),'');
  v_current_repository_id := NULLIF(COALESCE(v_fields->>'repository_id',''),'');
  IF v_current_account IS NULL
     OR length(v_current_account)>200
     OR v_current_repository_id IS NULL
     OR length(v_current_repository_id)>32
     OR v_current_repository_id !~ '^[1-9][0-9]*$' THEN
    v_current_account := NULL;
    v_current_repository_id := NULL;
  END IF;
  IF v_current_account IS DISTINCT FROM p_expected_account
     OR v_current_repository_id IS DISTINCT FROM p_expected_repository_id THEN
    RETURN jsonb_build_object('advanced',false,'reason','cursor changed');
  END IF;

  UPDATE core.boot_reconcile_state
     SET last_run_at=now(),
         fields=jsonb_build_object(
           'account_id',p_next_account,
           'repository_id',p_next_repository_id
         )
   WHERE kind='boot_reconcile_cursor';
  RETURN jsonb_build_object('advanced',true,'reason',NULL);
END $$;
ALTER FUNCTION core.advance_boot_reconcile_route_cursor_with_authority(text,text,text,text) OWNER TO veripsa_migrator;

REVOKE EXECUTE ON FUNCTION core.read_boot_reconcile_route_page_with_authority(int)
  FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.boot_reconcile_route_is_current_with_authority(
  text,timestamptz,text,text) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.advance_boot_reconcile_route_cursor_with_authority(text,text,text,text)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.read_boot_reconcile_route_page_with_authority(int)
  TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.boot_reconcile_route_is_current_with_authority(
  text,timestamptz,text,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.advance_boot_reconcile_route_cursor_with_authority(text,text,text,text)
  TO veripsa_app;
