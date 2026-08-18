# Veripsa GitHub App — the webhook server, container image (the always-on form of github-app/server.py).
# Build from the repo root:  docker build -f github-app/Dockerfile -t veripsa-app .
# Run (point a real GitHub App webhook straight at it; no smee):
#   docker run -p 8000:8000 \
#     -e VERIPSA_DSN \
#     -e GH_WEBHOOK_SECRET=... -e GH_APP_ID=... -e GH_INSTALLATION_ID=... \
#     -e GH_PRIVATE_KEY=/run/secrets/app.pem -v /path/app.pem:/run/secrets/app.pem:ro \
#     veripsa-app
FROM python:3.12-slim

WORKDIR /app

# HONEST BUILD LABEL — /healthz reports WHICH COMMIT is live, derived (never a hand-bumped env that goes stale).
# Pass the commit at build time:  docker build --build-arg GIT_SHA=$(git rev-parse HEAD) ...
# Render injects RENDER_GIT_COMMIT as a Docker build arg automatically. The App reads VERIPSA_BUILD_SHA first;
# the RUN below also bakes the exact full commit into /app/BUILD_SHA so a one-off migration job can prove the
# immutable artifact independently of its runtime environment. health_watchdog
# .build_version() reads VERIPSA_BUILD_SHA env -> /app/BUILD_SHA file -> legacy VERIPSA_VERSION -> "dev", so the
# label is honest no matter which path is wired, and degrades to "dev" (never crashes) when none is.
ARG GIT_SHA=""
ARG RENDER_GIT_COMMIT=""
ENV VERIPSA_BUILD_SHA=$GIT_SHA
# git: the co-change (logical-coupling) signal is computed from COMMIT HISTORY, which the tarball graph ingest
# does NOT carry — populate_cochange takes a blobless, no-checkout clone (github_rest.history_clone), so the
# runtime needs the git binary + CA certs for the HTTPS clone. (Content-free: the clone fetches commit/tree
# metadata + paths, never a file blob.) --no-install-recommends keeps the slim image slim; apt lists are pruned.
#
# postgresql-client: needed by github-app/scripts/predeploy_schema.sh, the Render preDeployCommand that
# AUTO-APPLIES db/schema.sql on every deploy. Closes the 2026-06-25 incident class (code shipped with new
# function signatures while the prod DB still had the old ones → every webhook aborted with SQLSTATE 42883).
# Without psql in the image the preDeploy step can't run; with it, schema apply is mechanical on every deploy.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Static graph extraction (tree-sitter grammars) + DB driver + live App-JWT signing. Pinned to ABI-compatible
# grammar versions (see requirements.txt rationale).
# EVERY direct and transitive runtime dep is listed with an upper bound. --no-deps is intentional: the Dart
# language-pack over-constrains the external C# wheel even though Veripsa uses its bundled Dart grammar. The
# explicit closure keeps C# on ABI-v14 0.21.3 and core on 0.23.2 without hiding any runtime dependency. These
# bounds and the install mode MUST match requirements.txt and the release workflows exactly.
RUN pip install --no-cache-dir --no-deps \
      "psycopg2-binary>=2.9,<2.10" \
      "tree-sitter==0.23.2" \
      "tree-sitter-javascript>=0.23,<0.24" "tree-sitter-typescript>=0.23,<0.24" "tree-sitter-go>=0.23,<0.24" \
      "tree-sitter-java>=0.23,<0.24" "tree-sitter-ruby>=0.23,<0.24" "tree-sitter-php>=0.23,<0.24" \
      "tree-sitter-c-sharp==0.21.3" "tree-sitter-html>=0.23,<0.24" \
      "tree-sitter-rust>=0.21,<0.22" "tree-sitter-cpp>=0.22,<0.23" "tree-sitter-c>=0.21,<0.22" \
      "tree-sitter-kotlin>=1.0.0,<1.1" "tree-sitter-swift>=0.0.1,<0.7" \
      "tree-sitter-svelte>=1.0.2,<1.1" \
      "tree-sitter-elixir>=0.3.0,<0.4" \
      "tree-sitter-language-pack==0.9.1" \
      "tree-sitter-embedded-template==0.23.2" "tree-sitter-yaml==0.7.0" \
      "PyYAML>=6.0.2,<7" \
      "pyjwt>=2.8,<3" "typing-extensions>=4,<5" \
      "cryptography>=42,<46" "cffi>=1.17,<3" "pycparser>=2.22,<3"

# The extractor is SPLIT into root modules — ALL must be in the image. code_graph_extract imports
# _cg_languages / _cg_schema / _cg_config / _cg_resolve AND cg_generated (the generated/vendored predicate group,
# #262); _cg_cochange is the standalone co-change extractor (imported by ingest.populate_cochange). Missing ANY =
# ImportError on the FIRST graph ingest (the core job) → EVERY webhook event fails. This is GLOB'd (not a hand-
# maintained list) on purpose: a hand list silently rotted when #262 added cg_generated.py — the file existed in
# the repo (gates green locally) but was absent from the image, so the live App ImportError'd on every event.
# The glob auto-includes any current/future `_cg_*.py` / `cg_*.py` root module. (gate: dockerfile_extractor_copy.)
COPY code_graph_extract.py _cg_*.py cg_*.py ./
COPY github-app/ ./github-app/
# db/ tree (schema.sql + the schema/*.sql modules \ir'd by it + db/roles.sql) — needed by the Render
# preDeployCommand `bash github-app/scripts/predeploy_schema.sh`, which auto-applies the schema on EVERY
# deploy and closes the "code shipped without its schema" incident class (#000 2026-06-25). Self-contained:
# no checkout / no git / no external assets at deploy time — psql + the SQL all live in the image.
COPY db/ ./db/

# Bake the honest build label as a durable artifact. Render's platform-provided
# RENDER_GIT_COMMIT is the normal source; GIT_SHA is the explicit local-build
# override. A supplied identity must be the exact lowercase 40-hex commit or
# the image build fails—an abbreviated or malformed label cannot authorize the
# post-promotion schema cutover. A source-less local build remains supported
# and simply has no artifact proof.
RUN BUILD_SHA_CANDIDATE="$GIT_SHA"; \
    if [ -z "$BUILD_SHA_CANDIDATE" ]; then BUILD_SHA_CANDIDATE="$RENDER_GIT_COMMIT"; fi; \
    if [ -n "$BUILD_SHA_CANDIDATE" ]; then \
      if [ "${#BUILD_SHA_CANDIDATE}" -ne 40 ]; then echo "build commit must be exactly 40 hex characters" >&2; exit 1; fi; \
      case "$BUILD_SHA_CANDIDATE" in *[!0-9a-f]*) echo "build commit must be lowercase hexadecimal" >&2; exit 1 ;; esac; \
      printf '%s\n' "$BUILD_SHA_CANDIDATE" > /app/BUILD_SHA; \
    fi

# Run as a NON-ROOT user (defense in depth — nothing here needs root at runtime).
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
ENV PORT=8000
# Container-level liveness (Render uses healthCheckPath for the web service; this also covers plain Docker).
# Background-worker/cron command overrides intentionally expose no HTTP port. Their PID 1 exiting already stops
# the container, so the role-aware helper skips the HTTP probe for those roles instead of causing false restarts.
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
  CMD env -u OWNER_DSN python3 github-app/container_healthcheck.py
# OWNER_DSN exists on the Render service only because the separate
# preDeployCommand needs DDL authority.  It must not survive into the serving
# process's initial environment: otherwise any runtime code execution could
# bypass FORCE RLS with the database owner credential.  `env -u` execs Python
# with a newly-built environment, so even /proc/<pid>/environ has no owner DSN.
CMD ["env", "-u", "OWNER_DSN", "python3", "github-app/server.py"]
