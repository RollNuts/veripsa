# Role-Feature Contracts

Veripsa uses role-feature contracts to recover a narrow class of cross-directory
coupling that has no import/call/schema edge.

This detector does not couple whole directories. It only emits a shared
content-free key when files are in an allowlisted role pair and share a stable
path-derived feature token.

Examples:

- `internal/db/usage.go` and `internal/server/huma_routes_usage.go`
- `frontend/src/components/SettingsLayout.jsx` and `frontend/src/pages/Settings.jsx`
- `frontend/src/components/SettingsPanel.tsx` and `frontend/src/pages/settings/index.tsx`
- `src-tauri/src/commands/agent.rs` and `src/pages/agents.js`

Precision boundaries:

- known role pairs only: selected Go `internal/*` pairs, frontend
  `components`/`pages`, and Tauri `commands`/`pages`
- path-derived tokens only; no source bodies or diff bodies
- frontend `pages` files with generic or dynamic basenames may use the nearest
  stable route directory segment, such as `settings` in
  `frontend/src/pages/settings/index.tsx`
- generic tokens, tests, fixtures, generated, vendor, docs, and examples are
  ignored
- groups above the resource-hub threshold stay silent
- no broad folder-neighborhood coupling

The detector reuses existing `queries` shared-resource edges and does not
require a DB schema change.
