"""The configuration a shadow decision is made under, and its version.

A parity comparison is only meaningful if both sides were configured the same
way, so the journal records a ``config_version``: a digest of the effective
configuration, not of the file it came from. The difference matters when
configuration is assembled from more than one source -- a repository
``.continuum.yml`` plus the environment -- because two runs can then be reading
the same file and still be configured differently.

The default configuration is not a guess. Its values are the ones NanoDictate's
production workflows actually use, taken from the preserved snapshot in
``reference/nanodictate-workflows/``:

* the review provider is CodeRabbit, at its real bot login and status context;
* the review-queue ready label is ``review-ready``, which is the label
  ``add-review-label.yml`` adds and ``remove-review-label.yml`` removes;
* the required checks are ``CI`` and ``Packaging smoke``, the two workflows
  ``opencode-repair.yml`` watches for a completed failure;
* the repair lock and failure labels are the ones ``opencode-repair.yml`` and
  ``conflict_repair.py`` already share.

A shadow run that used a *different* configuration would not be comparing
decisions, it would be comparing two configurations.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .. import config as config_module
from ..config import ConfigError, ContinuumConfig, load_optional_config

SHADOW_CONFIG_SCHEMA = "continuum.shadow-config/v1"

#: The values taken from the NanoDictate snapshot. Named constants rather than
#: inline literals so a reader can check each one against
#: ``reference/nanodictate-workflows/`` without reading the whole module.
NANODICTATE_READY_LABEL = "review-ready"
NANODICTATE_REQUIRED_CHECKS: Tuple[str, ...] = ("CI", "Packaging smoke")
NANODICTATE_PRIORITY_LABELS: Tuple[str, ...] = ("priority:p0", "priority:p1", "priority:p2")
NANODICTATE_BLOCK_LABELS: Tuple[str, ...] = (
    "review-paused",
    "review-blocked",
    "no-review",
    "no-auto-merge",
)
NANODICTATE_DISPATCH_WORKFLOW = "coderabbit-retry.yml"
NANODICTATE_STATUS_CONTEXT = "CodeRabbit"
NANODICTATE_BOT_LOGIN = "coderabbitai[bot]"

#: The repair vocabulary the two controllers share. ``conflict_repair.py``
#: already defines these; they are repeated here only as documentation of what
#: the snapshot uses, and the planner reads the constants from the engine rather
#: than from this list.
LOCK_LABEL = "opencode-conflict-repair"
REPAIR_FAILED_LABEL = "opencode-repair-failed"
AUTO_MERGE_OPT_OUT_LABEL = "no-auto-merge"


@dataclasses.dataclass(frozen=True)
class ShadowConfig:
    """A decision configuration plus the provenance a journal needs."""

    config: ContinuumConfig
    #: Digest of the *effective* configuration. Two runs with the same value
    #: were configured identically even if they read it from different files.
    version: str
    source: str
    #: Whether the configuration came from a file or from the documented
    #: NanoDictate default. A parity report states this, because "Continuum
    #: disagreed" is a different claim when Continuum was configured differently.
    origin: str

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": SHADOW_CONFIG_SCHEMA,
            "version": self.version,
            "source": self.source,
            "origin": self.origin,
            "review_enabled": self.config.review.enabled,
            "provider": self.config.review.provider,
            "status_context": self.config.review.status_context,
            "queue": self.config.review.queue.describe(),
        }


def config_version(config: ContinuumConfig) -> str:
    """A digest of the effective configuration.

    Over the resolved model rather than the file bytes: two repositories whose
    files differ only in comments or key order are configured the same way, and
    a parity report should say so.
    """

    return "cfg-" + hashlib.sha256(
        json.dumps(
            config.describe(), sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()[:16]


def nanodictate_config() -> ContinuumConfig:
    """The configuration Continuum would run NanoDictate under.

    Review is on, because NanoDictate's production automation reviews; release
    is off, because the release path is exercised through the release core's
    dry run rather than through the release toggle, and a shadow run must not be
    able to publish by configuration alone.
    """

    return ContinuumConfig(
        version=config_module.SCHEMA_VERSION,
        review=config_module.ReviewSettings(
            provider=config_module.PROVIDER_CODERABBIT,
            block_merge=True,
            status_context=NANODICTATE_STATUS_CONTEXT,
            coderabbit=config_module.CodeRabbitSettings(
                bot_login=NANODICTATE_BOT_LOGIN,
                status_context=NANODICTATE_STATUS_CONTEXT,
            ),
            queue=config_module.QueueSettings(
                ready_label=NANODICTATE_READY_LABEL,
                block_labels=NANODICTATE_BLOCK_LABELS,
                priority_labels=NANODICTATE_PRIORITY_LABELS,
                tie_breakers=("source_issue", "pr_number"),
                required_checks=NANODICTATE_REQUIRED_CHECKS,
                require_green_ci=True,
                cooldown_minutes=0,
                in_flight_timeout_minutes=30,
                safety_margin_seconds=30,
                dispatch_workflow=NANODICTATE_DISPATCH_WORKFLOW,
            ),
        ),
    )


def load(
    path: Optional[str] = None,
    *,
    engine_root: Optional[Path] = None,
) -> ShadowConfig:
    """Load a shadow configuration.

    An explicit ``--config`` wins; otherwise the repository's own
    ``.continuum.yml`` is used when there is one; otherwise the documented
    NanoDictate default. The source is recorded either way, so a reader can tell
    which of the three produced a decision.
    """

    if path:
        candidate = Path(path)
        if not candidate.is_file():
            raise ConfigError("configuration file not found: {}".format(candidate))
        resolved = load_optional_config(str(candidate))
        if resolved is None:
            raise ConfigError("configuration file is empty: {}".format(candidate))
        return ShadowConfig(
            config=resolved,
            version=config_version(resolved),
            source=str(candidate),
            origin="file",
        )

    if engine_root is not None:
        default = engine_root / config_module.DEFAULT_CONFIG_PATH
        if default.is_file():
            resolved = load_optional_config(str(default))
            if resolved is not None:
                return ShadowConfig(
                    config=resolved,
                    version=config_version(resolved),
                    source=str(default),
                    origin="repository",
                )

    fallback = nanodictate_config()
    return ShadowConfig(
        config=fallback,
        version=config_version(fallback),
        source="<nanodictate-default>",
        origin="default",
    )


def describe_defaults() -> Dict[str, Any]:
    """The default configuration, for documentation and for the summary."""

    return ShadowConfig(
        config=nanodictate_config(),
        version=config_version(nanodictate_config()),
        source="<nanodictate-default>",
        origin="default",
    ).describe()
