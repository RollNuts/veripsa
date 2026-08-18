#!/usr/bin/env python3
"""Per-delivery Check-write observation for the private deployment canary.

The webhook worker and GitHub client are separated by several routing layers and
installation-scoped client objects. A ContextVar keeps one content-free target
(repo + head SHA) attached to the worker's current delivery without adding those
fields to logs, database rows, or public output. Only an exact target match can
record whether Veripsa updated a Check Run or intentionally left an identical one
unchanged.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class CheckDeliveryObservation:
    repo: str
    sha: str
    outcome: str | None = None


_CURRENT: ContextVar[CheckDeliveryObservation | None] = ContextVar(
    "veripsa_check_delivery_observation",
    default=None,
)


def begin_check_delivery_observation(repo: str, sha: str):
    """Start one exact-target observation and return its state plus reset token."""
    observation = CheckDeliveryObservation(repo=str(repo).casefold(), sha=str(sha).casefold())
    return observation, _CURRENT.set(observation)


def note_check_delivery_outcome(repo: str, sha: str, outcome: str) -> None:
    """Record an exact-target Check API outcome for the current worker delivery.

    ``updated`` wins over ``noop`` when one event happens to touch the same target
    more than once: an actual API write requires fresh GitHub evidence even if a
    later render is identical.
    """
    observation = _CURRENT.get()
    if observation is None or outcome not in ("noop", "updated"):
        return
    if observation.repo != str(repo).casefold() or observation.sha != str(sha).casefold():
        return
    if observation.outcome != "updated":
        observation.outcome = outcome


def end_check_delivery_observation(token) -> None:
    """Restore the previous context after one worker delivery."""
    _CURRENT.reset(token)
