#!/usr/bin/env python3
"""Fail-closed parent/child delegation relationship checks.

The public parent configuration contains only opaque child ids. The actual
id -> owner/repository binding arrives as a secret JSON object at runtime.
A child independently declares its id and parent in its own .continuum.yml.
No execution is authorized until both declarations agree.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, Iterable, List

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from continuum.config import (  # noqa: E402
    ConfigError,
    DELEGATION_CHILD,
    DELEGATION_PARENT,
    load_config,
)

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


class DelegationError(ValueError):
    pass


def _repository(value: object, where: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY_RE.match(value.strip()):
        raise DelegationError(f"{where} must be an owner/repository name")
    return value.strip()


def load_repository_map(raw: str, allowed_ids: Iterable[str]) -> Dict[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DelegationError(
            f"CONTINUUM_CHILD_REPOSITORIES must be a JSON object: {exc.msg}"
        ) from None
    if not isinstance(value, dict):
        raise DelegationError("CONTINUUM_CHILD_REPOSITORIES must be a JSON object")

    allowed = list(allowed_ids)
    if set(value) != set(allowed):
        missing = sorted(set(allowed) - set(value))
        extra = sorted(set(value) - set(allowed))
        details: List[str] = []
        if missing:
            details.append("missing ids: " + ", ".join(missing))
        if extra:
            details.append("unconfigured ids: " + ", ".join(extra))
        raise DelegationError(
            "child repository map must exactly match delegation.children ("
            + "; ".join(details)
            + ")"
        )

    repositories: Dict[str, str] = {}
    seen = set()
    for child_id in allowed:
        repository = _repository(value[child_id], f"repository map entry {child_id!r}")
        if repository in seen:
            raise DelegationError(
                f"child repository map resolves more than one id to the same repository"
            )
        seen.add(repository)
        repositories[child_id] = repository
    return repositories


def parent_plan(config_path: str, repository_map_json: str) -> List[Dict[str, str]]:
    config = load_config(config_path)
    if config.delegation.role != DELEGATION_PARENT:
        raise DelegationError(
            "delegation.role must be 'parent' before child execution can be dispatched"
        )
    repositories = load_repository_map(
        repository_map_json,
        config.delegation.children,
    )
    return [
        {"id": child_id, "repository": repositories[child_id]}
        for child_id in config.delegation.children
    ]


def verify_child(
    config_path: str,
    *,
    child_id: str,
    parent_repository: str,
) -> None:
    parent = _repository(parent_repository, "parent repository")
    config = load_config(config_path)
    if config.delegation.role != DELEGATION_CHILD:
        raise DelegationError("child repository does not declare delegation.role: child")
    if config.delegation.id != child_id:
        raise DelegationError("child delegation id does not match the parent allowlist id")
    if config.delegation.parent != parent:
        raise DelegationError("child delegation parent does not match the calling repository")


def resolve_child(
    config_path: str,
    repository_map_json: str,
    *,
    child_id: str,
) -> str:
    for entry in parent_plan(config_path, repository_map_json):
        if entry["id"] == child_id:
            return entry["repository"]
    raise DelegationError("child id is not allowed by delegation.children")


def child_validation_script(config_path: str) -> str:
    config = load_config(config_path)
    if config.delegation.role != DELEGATION_CHILD:
        raise DelegationError(
            "delegation.role must be 'child' before reading validation_script"
        )
    return config.delegation.validation_script


def _emit_error(error: Exception) -> int:
    print(f"::error::{error}", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("parent-plan")
    plan.add_argument("--config", default=".continuum.yml")
    plan.add_argument("--repository-map-json", required=True)

    resolve = sub.add_parser("resolve-child")
    resolve.add_argument("--config", default=".continuum.yml")
    resolve.add_argument("--repository-map-json", required=True)
    resolve.add_argument("--child-id", required=True)

    verify = sub.add_parser("verify-child")
    verify.add_argument("--config", required=True)
    verify.add_argument("--child-id", required=True)
    verify.add_argument("--parent-repository", required=True)

    validation = sub.add_parser("validation-script")
    validation.add_argument("--config", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "parent-plan":
            json.dump(
                parent_plan(args.config, args.repository_map_json),
                sys.stdout,
                separators=(",", ":"),
            )
            sys.stdout.write("\n")
            return 0
        if args.command == "resolve-child":
            # This value is intentionally printed only for command substitution
            # inside a strict-mode shell step. Workflows must never echo it.
            sys.stdout.write(
                resolve_child(
                    args.config,
                    args.repository_map_json,
                    child_id=args.child_id,
                )
                + "\n"
            )
            return 0
        if args.command == "validation-script":
            sys.stdout.write(child_validation_script(args.config) + "\n")
            return 0
        verify_child(
            args.config,
            child_id=args.child_id,
            parent_repository=args.parent_repository,
        )
        return 0
    except (ConfigError, DelegationError, OSError) as error:
        return _emit_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
