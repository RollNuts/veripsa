# Sibling stem contracts

This note records the first implementation slice after the AI-era recall sampler showed that the dominant miss class is not only backend/frontend contracts. The largest buckets include same-language sibling modules such as `internal/db <-> internal/server` and `frontend/src/components <-> frontend/src/pages`.

## What the detector does

The sibling-stem substrate emits a shared contract key when files meet all of these conditions:

- same non-generic file stem, for example `user.go` in two sibling directories
- same language family
- different directories
- same coarse package/app scope, such as `internal` or `frontend/src`
- group size is at most the resource-hub dampening threshold

Example:

```text
internal/db/user.go
internal/server/user.go
```

These emit a shared content-free key:

```text
sibling_stem::go::internal::user
```

## What it intentionally does not do

The detector stays silent for:

- generic stems such as `index`, `main`, `page`, `app`, `route`, `types`, and `utils`
- tests, specs, fixtures, generated folders, examples, docs, and vendored paths
- matches across unrelated coarse scopes, such as `cmd/...` and `internal/...`
- groups larger than the resource-hub threshold
- any source-body or diff-body content

## Why this is an actual detector, not only measurement

The sampler identified measured miss buckets. This detector promotes one narrow, path-only slice into the graph: same-stem sibling modules. It is not a broad folder-neighborhood detector, because it requires a stable shared stem and bounded group size.

## Remaining boundary

This will not cover all `no_graph_edge_xdir` misses. It should be re-measured with the AI-era recall panel before claiming product improvement.
