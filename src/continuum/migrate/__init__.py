"""The trusted migration controller: install Continuum into a consumer, atomically.

Issue #28 is the cutover gate, and step 9 of its sequence is the operation this
package exists to make possible:

    Cut over this issue through #84's trusted migration controller in one
    controlled change: install NanoDictate thin callers/config pinned to one
    immutable Continuum SHA; disable/remove the legacy duplicate **writers** in
    the same cutover so old and new scheduler/repair/merge/release controllers
    never mutate production simultaneously.

Everything above this package -- the parity ledger, the rolling baseline, the
shadow window, the cutover gate -- *judges*. It reads and it reports, and it
refuses. None of it can change what the consumer looks like. A gate that could
install itself would be the thing it is supposed to be checking, so the two
halves are separate code, and this one is the only privileged writer in the
system.

The five stages, in the order :mod:`continuum.migrate.apply` runs them:

==========================  ====================================================
:mod:`~continuum.migrate.inventory`  what the repository is now
:mod:`~continuum.migrate.preflight`  whether it may be cut over
:mod:`~continuum.migrate.plan`      what the single atomic change contains
:mod:`~continuum.migrate.apply`     one pull request, validated, merged
:mod:`~continuum.migrate.rollback`  the way back, idempotently
==========================  ====================================================

Three properties are the reason this is one package rather than a documented
procedure.

**Writer roles are reviewed data, never read off a filename.** Which workflow
implements the scheduler, the merge controller or the release pipeline comes from
the parity ledger (``docs/parity-ledger.json``), the artifact #60 produced. A
controller that inferred ``release.yml`` means "release" from the name could be
walked past by a file called something else, and the single-writer invariant --
the one thing that must never be violated during a cutover -- would be enforced
against a guess.

**Every Continuum reference is a full immutable commit SHA.** ``@main`` is
refused by the planner, not by review, because the answer to "which code decided
to merge this" has to be a commit and not a branch that moves. The architecture
document calls this the versioning invariant and #92 owns keeping it current.

**Reads fail closed and absences are named.** A read that did not complete makes
the inventory incomplete, an incomplete inventory cannot produce a READY
preflight, and a secret is recorded as a *name and a presence*, never a value.
GitHub cannot tell the controller a secret's value, so a controller that
demanded one would be asking for something that does not exist; one that
guessed would be inventing a credential.
"""
