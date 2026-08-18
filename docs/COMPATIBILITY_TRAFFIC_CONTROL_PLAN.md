# Compatibility traffic-control contract

This document describes the source-level compatibility analysis boundary. It is
not a hosted-service availability or deployment claim.

## Purpose

Structural overlap is not the only way parallel pull requests interact. One PR
may change a producer contract while another changes a consumer. Veripsa models
that relationship as content-free evidence that can inform landing order.

## Content-free facts

Compatibility analysis may retain normalized structural facts such as path,
symbol, parameter names, required/optional arity, contract kind, detector
version, line range, and a shape fingerprint. It must not retain or render file
bodies, diff bodies, default-value expressions, annotation source, prompts,
secrets, or customer payloads.

## Conservative semantics

- Candidate pairs must be narrowed by existing structural evidence; do not run
  an unbounded all-pairs analysis.
- Findings bind to both pull-request heads and expire when either head changes.
- Unsupported language features, reflection, unresolved types, stale graphs,
  and base mismatches produce `Unknown`, never a guessed `Clear`.
- A compatibility finding is evidence for an existing traffic signal, not a new
  verdict and not proof that either change is correct.
- Landing obligations are directed. Cycles require human design coordination
  rather than an invented linear order.

## Security and quality gates

Every detector must be bounded by time, file count, symbol count, and output
size. Parser failures isolate to the affected finding. Tests must cover
content-free output, tenant isolation, head freshness, ACK invalidation,
determinism, false-positive suppression, and performance on synthetic fixtures.

The public source includes implementation and tests for this contract. Any
customer-visible rollout or hosted-service resumption requires separate release
and deployment verification.
