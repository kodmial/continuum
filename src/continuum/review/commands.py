"""Comment command parsing under Continuum's trusted-actor contract.

Only a trusted actor may drive a review run, and only the first line of a
comment is ever interpreted. Command arguments are validated against a strict
grammar, so no untrusted text can reach a shell, an expression, or a prompt
control channel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

COMMAND_REVIEW = "review"
COMMAND_VERIFY = "verify"
COMMAND_DESCRIBE = "describe"
SUPPORTED_COMMANDS = (COMMAND_REVIEW, COMMAND_VERIFY, COMMAND_DESCRIBE)

# Only a finding id may be accepted as an argument. No paths, no free text, no
# shell metacharacters.
FINDING_ID_RE = re.compile(r"^(?i:crv|pra)-[0-9A-Fa-f]{8}$")

MAX_BODY_SCAN_CHARS = 4096


class CommandError(ValueError):
    """Raised when a comment is a review command but malformed."""


@dataclass(frozen=True)
class Command:
    command: str
    finding: Optional[str] = None
    args: str = ""

    @property
    def is_supported(self) -> bool:
        return self.command in SUPPORTED_COMMANDS


def first_line(body: Optional[str]) -> str:
    """First line of a comment, whitespace-stripped and length-bounded."""

    text = (body or "")[:MAX_BODY_SCAN_CHARS]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def is_trusted_actor(actor: Optional[str], repository_owner: Optional[str]) -> bool:
    """Continuation of the repository-owner trust policy used by the agent."""

    left = (actor or "").strip()
    right = (repository_owner or "").strip()
    if not left or not right:
        return False
    return left.lower() == right.lower()


def parse_command(body: Optional[str]) -> Optional[Command]:
    """Parse a review command, or return None when the comment is not one.

    A command that is recognized but malformed raises `CommandError` so the
    caller can report the exact usage instead of silently doing nothing.
    """

    line = first_line(body)
    if not line.startswith("/"):
        return None
    if not re.match(r"^/[a-z-]{2,20}(\s|$)", line):
        return None
    verb, _, remainder = line.partition(" ")
    verb = verb[1:].lower()
    argument = remainder.strip()
    if verb not in SUPPORTED_COMMANDS:
        return None
    if verb == COMMAND_VERIFY:
        if not FINDING_ID_RE.match(argument):
            raise CommandError(
                "usage: /verify <finding-id> (for example /verify CRV-1A2B3C4D); "
                f"got {argument[:64]!r}"
            )
        return Command(command=COMMAND_VERIFY, finding=argument.upper())
    return Command(command=verb, args=argument[:200])
