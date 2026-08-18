"""Versioned runtime/schema compatibility tokens exposed by /healthz.

These are behavior protocols, not build versions. Deployment and rollback
automation uses them to decide whether an older image can safely operate after
the current additive database schema has been published.
"""

REPOSITORY_OFFBOARDING_PROTOCOL = 2
DURABLE_RETRY_PROTOCOL = 3
