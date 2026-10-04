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
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class DelegationError(ValueError):
    pass


def _repository(value: object, where: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY_RE.match(value.strip()):
        raise DelegationError(f"{where} must be an owner/repository name")
    cleaned = value.strip()
    owner, _, name = cleaned.partition("/")
    # The charset class alone accepts dot-only components (`owner/..`,
    # `owner/.`) which are never valid GitHub identities. Fail closed.
    # Leading/trailing dots are otherwise rejected, except the reserved
    # `.github` repository name which legitimately starts with a dot.
    if not owner or not name or re.fullmatch(r"\.+", owner) or re.fullmatch(r"\.+", name):
        raise DelegationError(f"{where} must be an owner/repository name")
    if (
        owner.startswith(".")
        or owner.endswith(".")
        or (name != ".github" and (name.startswith(".") or name.endswith(".")))
    ):
        raise DelegationError(f"{where} must be an owner/repository name")
    return cleaned


def _slug(value: object, where: str) -> str:
    if not isinstance(value, str) or not _SLUG_RE.match(value.strip()):
        raise DelegationError(f"{where} must be a simple child id")
    return value.strip()


def parent_variable_ids(role: str, children_json: str) -> List[str]:
    if role != DELEGATION_PARENT:
        raise DelegationError("CONTINUUM_ROLE must be 'parent'")
    try:
        value = json.loads(children_json)
    except json.JSONDecodeError as exc:
        raise DelegationError(
            f"CONTINUUM_CHILDREN must be a JSON array: {exc.msg}"
        ) from None
    if not isinstance(value, list) or not value:
        raise DelegationError("CONTINUUM_CHILDREN must be a non-empty JSON array")
    children = [_slug(item, "CONTINUUM_CHILDREN entry") for item in value]
    if len(set(children)) != len(children):
        raise DelegationError("CONTINUUM_CHILDREN must contain unique child ids")
    return children


def verify_child_variables(
    *,
    role: str,
    declared_id: str,
    declared_parent: str,
    child_id: str,
    parent_repository: str,
) -> None:
    if role != DELEGATION_CHILD:
        raise DelegationError("child CONTINUUM_ROLE must be 'child'")
    expected_id = _slug(child_id, "expected child id")
    actual_id = _slug(declared_id, "CONTINUUM_CHILD_ID")
    if actual_id != expected_id:
        raise DelegationError("child id does not match the parent allowlist id")
    expected_parent = _repository(parent_repository, "parent repository")
    actual_parent = _repository(declared_parent, "CONTINUUM_PARENT")
    if actual_parent != expected_parent:
        raise DelegationError("child parent does not match the calling repository")


def validation_script_value(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("/") or "\x00" in value:
        raise DelegationError("CONTINUUM_VALIDATION_SCRIPT must be repository-relative")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise DelegationError("CONTINUUM_VALIDATION_SCRIPT contains an unsafe path segment")
    if not value.endswith(".sh"):
        raise DelegationError("CONTINUUM_VALIDATION_SCRIPT must name a .sh file")
    return value


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


def parent_child_ids(config_path: str) -> List[str]:
    config = load_config(config_path)
    if config.delegation.role != DELEGATION_PARENT:
        raise DelegationError(
            "delegation.role must be 'parent' before child discovery"
        )
    return list(config.delegation.children)


def assert_parent_allows_child(config_path: str, child_id: str) -> None:
    children = parent_child_ids(config_path)
    if child_id not in children:
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

    child_ids = sub.add_parser("parent-child-ids")
    child_ids.add_argument("--config", default=".continuum.yml")

    allowed = sub.add_parser("parent-allows-child")
    allowed.add_argument("--config", default=".continuum.yml")
    allowed.add_argument("--child-id", required=True)

    variable_ids = sub.add_parser("parent-variable-ids")
    variable_ids.add_argument("--role", required=True)
    variable_ids.add_argument("--children-json", required=True)

    variable_child = sub.add_parser("verify-child-variables")
    variable_child.add_argument("--role", required=True)
    variable_child.add_argument("--declared-id", required=True)
    variable_child.add_argument("--declared-parent", required=True)
    variable_child.add_argument("--child-id", required=True)
    variable_child.add_argument("--parent-repository", required=True)

    validation_value = sub.add_parser("validation-script-value")
    validation_value.add_argument("--value", default="")

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
        if args.command == "parent-child-ids":
            for child_id in parent_child_ids(args.config):
                sys.stdout.write(child_id + "\n")
            return 0
        if args.command == "parent-allows-child":
            assert_parent_allows_child(args.config, args.child_id)
            return 0
        if args.command == "parent-variable-ids":
            for child_id in parent_variable_ids(args.role, args.children_json):
                sys.stdout.write(child_id + "\n")
            return 0
        if args.command == "verify-child-variables":
            verify_child_variables(
                role=args.role,
                declared_id=args.declared_id,
                declared_parent=args.declared_parent,
                child_id=args.child_id,
                parent_repository=args.parent_repository,
            )
            return 0
        if args.command == "validation-script-value":
            sys.stdout.write(validation_script_value(args.value) + "\n")
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
