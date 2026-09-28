"""Release adapters.

A release adapter turns a *validated* target from `.continuum.yml` into a
`ReleasePlan`: an ordered, inspectable list of steps plus the teardown that has
to happen whatever the outcome. The adapter owns the platform; the core owns
nothing but the plan's shape and the rules for running it.

The split is the point. `plan.py` and `run.py` never learn what a keychain, a
bundle identifier, or a timestamp is — they move named steps around and enforce
the invariants that hold everywhere (teardown always runs, a value that is
required to be pinned was actually pinned, a degraded plan says so out loud).
Everything platform-specific lives in an adapter module and is expressed as
argument vectors, so nothing an adapter emits is ever handed to a shell.
"""

from __future__ import annotations

from .plan import (
    PlanStep,
    ReleaseError,
    ReleasePlan,
    SigningUnavailable,
    TargetNotExecutable,
)

__all__ = [
    "PlanStep",
    "ReleaseError",
    "ReleasePlan",
    "SigningUnavailable",
    "TargetNotExecutable",
]
