"""Live PR-Agent signal-policy canary for Work Lock #37."""


def can_delete_project(actor_id: str, owner_id: str) -> bool:
    """Return True only when the actor owns the project."""

    return actor_id == owner_id
