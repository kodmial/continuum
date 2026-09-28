"""Release adapter registry.

The analogue of the review provider registry, and deliberately the same shape:
adding a platform means adding a module, and nothing above this file changes.
A consumer asks for a target's plan by target, not by adapter, so the workflow
that calls Continuum never names a platform either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from .. import config as config_module
from . import apple
from .plan import ReleaseError, ReleasePlan, SigningUnavailable, TargetNotExecutable

APPLE = config_module.ADAPTER_APPLE

# Only the modules an adapter registry entry needs are imported here. An adapter
# is a plan builder and a failure explainer; everything else about it is its own
# business.
_ADAPTERS: Dict[str, Any] = {
    APPLE: apple,
}


class UnsupportedAdapter(ReleaseError):
    """Raised when configuration names an adapter Continuum cannot run."""


def supported() -> Tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def get(name: str) -> Any:
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        raise UnsupportedAdapter(
            f"unsupported release adapter {name!r}; supported: {', '.join(supported())}"
        )
    return adapter


def bind_material(
    target: config_module.ReleaseTarget, environment: Dict[str, str]
) -> Dict[str, str]:
    """Join the job's secrets to the names the adapter's own steps read.

    Configuration names a secret (`NANODICTATE_SIGNING_P12`); the adapter's
    steps read a name of their own (`CONTINUUM_APPLE_P12`). The join is the
    adapter's business, so it happens here rather than in a caller that would
    have to know both names.

    The result is returned rather than applied in place because the caller that
    is about to *run* the plan needs the very same mapping: a plan built from a
    bound environment and then run against the raw one fails to find the secret
    it was planned around, which looks exactly like a secret that was never
    configured.
    """

    adapter = get(target.adapter)
    binder: Optional[Callable[..., Dict[str, str]]] = getattr(adapter, "bind_material", None)
    bound = dict(environment or {})
    if binder is None:
        return bound
    return binder(target.signing, bound)


def plan_for(
    target: config_module.ReleaseTarget,
    *,
    environment: Optional[Dict[str, str]] = None,
    require_toolchain: bool = False,
) -> ReleasePlan:
    """Build the plan for one validated target.

    The caller hands over the job's environment as it is. If the adapter needs
    its certificate under its own name before it can plan — which it does,
    because a secret is only meaningful under the name configuration gives it —
    the adapter is the one that performs that join. Nothing above this file
    learns a variable name.
    """

    adapter = get(target.adapter)
    if require_toolchain:
        ensure_toolchain(adapter)
    return adapter.plan_for(target, environment=bind_material(target, environment or {}))


def ensure_toolchain(adapter: Any) -> None:
    """Refuse to plan a run this machine cannot perform.

    Planning and running are separated so that configuration and plan review
    happen anywhere — including a Linux CI job that never touches a signing
    tool. The caller that is about to *run* the plan asks for the toolchain
    explicitly, and gets a clear refusal instead of a plan that fails halfway
    through on a missing binary.
    """

    if not adapter.available():
        raise TargetNotExecutable(
            f"the {getattr(adapter, 'ADAPTER_NAME', 'release')} adapter needs a "
            "toolchain this runner does not provide; run the plan on a runner that "
            "has it"
        )


def has_material(target: config_module.ReleaseTarget, environment: Dict[str, str]) -> bool:
    """Whether the signing key material a stable profile needs is present.

    An empty secret counts as absent, not as an empty value: a fork receives
    the variables with nothing in them, and treating that as "the certificate
    is available" is how a release gets signed by nobody without a single error
    being printed.
    """

    adapter = get(target.adapter)
    probe: Optional[Callable[..., bool]] = getattr(adapter, "material_present", None)
    if probe is None:
        return False
    return bool(probe(target.signing, dict(environment or {})))


@dataclass(frozen=True)
class FailureExplanation:
    """What went wrong, and what to do about it, in a form a caller can branch on."""

    code: str
    message: str
    remediation: str = ""

    def describe(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


def explain_failure(adapter_name: str, step: str, detail: str) -> FailureExplanation:
    """Turn a failed step into a code, a message, and something to do about it.

    The generic runner can only say "this command failed". Only the adapter
    knows that a failed certificate import means the stored password is wrong
    rather than the keychain being locked, and that difference has a completely
    different fix.
    """

    adapter = get(adapter_name)
    explainer: Optional[Callable[[str, str], Tuple[str, str, str]]] = getattr(
        adapter, "classify_failure", None
    )
    if explainer is None:
        return FailureExplanation(code="step-failed", message=f"step {step!r} failed")
    code, message, remediation = explainer(step, detail)
    return FailureExplanation(code=code, message=message, remediation=remediation)


__all__ = [
    "APPLE",
    "FailureExplanation",
    "SigningUnavailable",
    "TargetNotExecutable",
    "UnsupportedAdapter",
    "bind_material",
    "ensure_toolchain",
    "explain_failure",
    "get",
    "has_material",
    "plan_for",
    "supported",
]
