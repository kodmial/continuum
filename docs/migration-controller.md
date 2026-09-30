# The migration controller

Status: **implemented, not yet run against a live repository**. The controller
below is complete and tested; what has not happened yet is the first dispatch
against a real cutover. Everything that says "ready" below is a claim about the
controller, not about any repository that has been migrated.

The controller is #28: a trusted, read-only-until-proven process that moves a
repository off its own hand-maintained workflows and onto the Continuum control
plane, in one commit, with a way back.

Scope: this document is about the *controller*. The evidence that justifies
running it — parity, liveness, the scenario classes, the cutover gate — is in
[shadow-validation.md](shadow-validation.md). The engine that reads a repository
is `src/continuum/migrate/`.

## Why it is a controller and not a script

Every design choice here exists to answer one question: *how would somebody
notice if this had quietly done the wrong thing?* A migration that installs five
callers, retires five writers, and merges is a very large change to make on the
strength of a plan somebody read once. So:

* **Nothing is written until everything is decided.** `inventory`, `preflight`
  and `plan` run with a read-only token and no write scopes at all. A cutover is
  rendered in full — every path, every action, every writer role — before a
  single byte is written, and the render is an artifact a reviewer can read.
* **The change is atomic.** One tree, one commit, one pull request. There is no
  window in which five callers are installed and one legacy writer has not yet
  been retired, or in which a retired writer's replacement is not yet on the
  branch.
* **The decision is bound to a head.** A cutover gate verdict is not "yes,
  migrate" in the abstract. It names a head, a phase, and an exact set of file
  changes, and `apply` re-derives all three and refuses if any differ. An
  authorization that survives a re-plan is an authorization for a cutover nobody
  looked at.
* **Every step is reversible, and reversibility is recorded.** The commit writes
  `.github/continuum-cutover.json` naming the exact revision to return to, so the
  rollback does not depend on this controller's memory, on a workflow artifact,
  or on anybody remembering what happened.

## Running it

```sh
# Read the repository. No token with write scopes is even reachable here.
PYTHONPATH=src python3 -m continuum.migrate.cli inventory \
    --repo owner/name --ledger docs/parity-ledger.json

# Decide whether it may be cut over, and render the change.
PYTHONPATH=src python3 -m continuum.migrate.cli plan \
    --repo owner/name --ledger docs/parity-ledger.json \
    --candidate <40-hex sha> --phase phase-a

# Judge a window with the change set the plan produced.
PYTHONPATH=src python3 -m continuum.shadow.cli cutover \
    --parity ... --liveness ... --ledger ... \
    --change-set migration-out/change-set.json

# Publish, verify, wait, merge.
PYTHONPATH=src python3 -m continuum.migrate.cli apply \
    --repo owner/name --ledger docs/parity-ledger.json \
    --candidate <40-hex sha> --gate migration-out/decision.json

# Put it back.
PYTHONPATH=src python3 -m continuum.migrate.cli rollback \
    --repo owner/name --ledger docs/parity-ledger.json \
    --record .github/continuum-cutover.json
```

Or dispatch `.github/workflows/continuum-migrate.yml`, which is the same five
commands behind the trust policy.

## The five decisions, in order

### 1. `inventory` — what is actually there

A digest of every workflow, its writer role, its pin, its triggers, its display
name; the declared configuration; the repository's Actions variables; its secret
**names**; its labels; and whether a cutover record is already present.

Two properties matter more than the content:

* **A file that could not be read is not an empty file.** A 404 means the
  repository does not have it. A 403, a 500, or a rate limit means the controller
  does not know, and the reading is marked incomplete. A rate limit that reads as
  "this file does not exist" is how a controller plans on top of a repository it
  never saw.
* **A pin is a full commit SHA or it is not a pin.** `uses: ...@main` is recorded
  as a floating reference, because that is what it is: a promise that the
  repository will run whatever that branch points at when each workflow starts.

### 2. `preflight` — whether the answer is good enough to act on

`READY` or `NOT_READY`, with every reason collected rather than the first one
found. A controller that reported one problem at a time would take a week to
converge on a repository with three.

The requirements are derived from the consumer's own `.github/continuum.yml`, not
from a table this repository keeps about consumers. A controller with a second
copy of a consumer's configuration would need a diff the next time that
consumer's configuration changed.

Only secret *names* are ever read. The controller needs to know that
`OPENCODE_API_KEY` exists before it installs a caller that references it — a
cutover that installed one and then failed at the first run would leave the
repository with no repair and no agent — and it needs nothing more than the name.

### 3. `plan` — the atomic change

The thing under review. For each phase:

* five thin callers, each one event declaration and one `uses:` line at a pinned
  Continuum commit, and nothing else;
* the retirement of every independent implementation of a writer this phase
  replaces;
* `.github/continuum-cutover.json`, so the rollback is recorded in the same commit
  as the change;
* the parity-ledger overlay, so the audit and the change land together.

The invariant the plan is built around is **one writer per role, ever**. A plan
that would leave a maintained workflow beside a generated one is refused
(`duplicated_role`), and a workflow no reviewed ledger names a writer for is
refused too (`unclassified_workflow`) — a single-writer check over an inventory
whose contents are unknown is vacuous.

**Phase A does not touch release.** The `release` role is retained, not
replaced: a generated release caller would replace the thing Phase B exists to
preserve. `phase-b` is refused outright with `phase_not_implemented`, naming #21
and #22, because there is no generated release caller to install yet. A phase
that is not implemented does not get a partial implementation.

### 4. `apply` — publish, verify, wait, merge

Ordered, and each stage is what makes the next one safe:

| Stage | What it establishes |
| --- | --- |
| `prepare` | the label exists, so the checks below can reference something |
| `publish` | one commit on `continuum/cutover`, one pull request |
| `change-set` | the pull request's *actual* files, read back from GitHub |
| `head-gates` | every blocking check green on the newly published head |
| `cutover-gate` | the gate's verdict, bound to that head and that change set |
| `merge` | the merge, with GitHub's expected-head SHA as the condition |

The `change-set` stage reads the pull request back rather than trusting the plan,
because a plan can be right and a branch can still be different: a merge
conflict, a base that moved, a resolution somebody edited. A gate that was
authorised for a change nobody re-read is a gate nobody checked.

Waiting is on the **newly published head**, never on the default branch. A green
default branch says the previous commit was green.

**Resuming.** The controller may find `continuum/cutover` already holding a
commit. That is its own work if the commit's **tree** is the tree this run just
built, because a tree is the identity of a change set while a commit SHA is not —
git commits carry the time they were made, so two runs of one plan never produce
the same SHA. A commit whose tree differs is somebody else's and is refused. This
is why resuming does not need a state file: a run cancelled between the push and
the artifact upload comes back and recognises its own branch.

### 5. `rollback` — one commit, one way back

`compute_restore` reads the recorded revision's actual tree and rebuilds exactly
what the cutover removed, from the record and the recorded revision, and refuses
in three cases:

* the recorded revision is not a commit this repository has;
* a file the record says was retired was *not* there at that revision;
* restoring would leave a role with two implementations — somebody adopted the
  job while the cutover was in flight, and restoring over their work would be
  picking a winner between two live implementations without saying so.

It is one tree, one commit, one pull request, on `continuum/rollback`, and a
second run reports that there is nothing to undo rather than opening a second
pull request.

## The trust boundary

`.github/workflows/continuum-migrate.yml` is the only way this runs in CI, and it
is structured so that a mistake in the read half cannot reach the write half.

| Job | Permissions | Token |
| --- | --- | --- |
| `authorize` | none | none |
| `plan` | `contents: read` and other reads | `github.token` |
| `act` | `contents: write`, `pull-requests: write`, `issues: write` | `TAP_PAT` |

* **`authorize` holds no credentials at all.** It resolves the
  `workflow_dispatch` sender through `trust_policy.py check-dispatcher` and
  fails closed unless the sender is the repository owner or a configured
  automation account. A manual trigger is not reviewed the way a pull request is.
* **`act` verifies the credential separately.** `check-token` resolves the PAT to
  an account and refuses one that is not trusted. A trusted maintainer pressing
  the button is not a licence for an untrusted token, so the two checks are
  separate checks.
* **The candidate is proven, not trusted.** It must be a full 40-character SHA
  that exists in this repository and that the default branch contains. A fork, a
  dangling ref, or a typo would otherwise be pinned by a consumer.
* **Every checkout is at a ref GitHub chose**, never one a caller supplied.
* **Evidence is uploaded with `if: always()`.** A crashed run is evidence.

`.github/tests/test_workflow_hardening.py::MigrationControllerTests` asserts each
of those, because a boundary nobody tests is a boundary that quietly stops
holding.

## Every refusal, and what it means

Each of these is a code in a run's log, so an operator can search for it. None of
them is a failure of the controller: they are the controller working.

| Code | Raised when |
| --- | --- |
| `candidate_not_immutable` | the candidate is not a full 40-character commit SHA |
| `missing_ledger` | a subcommand that decides something was run without the reviewed ledger |
| `unclassified_workflow` | no reviewed audit names a writer for a workflow in the repository |
| `phase_violation` | the change itself is not valid for the phase it claims: a role still implemented twice, a role left with no writer at all, or a release writer touched in Phase A |
| `phase_not_implemented` | the phase has no generated caller to install yet (Phase B) |
| `change_set_not_bound` | a change set was asked for before the plan named a head |
| `gate_change_set_missing` | the gate's verdict names no change set, so no head to authorise |
| `gate_head_mismatch` | the gate authorised a different head |
| `gate_phase_mismatch` | the gate judged a different phase |
| `gate_change_set_mismatch` | the plan and the gate's change set are not the same set of paths |
| `cutover_gate_required` | `apply` was asked to run with no gate at all |
| `migration_branch_owned_elsewhere` | the cutover branch holds a commit this controller cannot prove is its own |
| `ref_moved_externally` | the branch moved between the read and the write |
| `rollback_target_missing` | the cutover record names a revision that is not a commit SHA |
| `rollback_target_missing_file` | the record says a file was retired, but it was not there at the recorded revision |
| `rollback_target_not_immutable` | the recorded revision is not in this repository |
| `record_not_actionable` | there is no cutover to undo |
| `ledger_revision_mismatch` | the ledger was audited at a different commit than the one being restored |
| `stranded_generated_caller` | restoring would leave a role with two live implementations |
| `tree_truncated` | the recorded revision's tree came back incomplete, so it cannot be restored from |
| `unlisted_write` | the controller asked the client for a method that is not on the write allowlist |

## What the controller will not do

* **It will not run without a ledger.** Writer roles come from the reviewed
  parity ledger, never from a filename. A controller that guessed which file was
  "the scheduler" would be guessing about a deletion.
* **It will not apply without the cutover gate's verdict**, and it will not accept
  a verdict that names a different head, a different phase, or a change set with
  one path more or less than the one it is about to build.
* **It will not overwrite a branch it cannot prove it owns.**
* **It will not read a secret's value**, and `get_ref`/`read_file_at_ref` treat a
  500 as an error rather than as an absent file.
* **It will not install a caller in place of a writer it cannot see**, and it will
  not restore a role that somebody else has taken over.
* **It will not merge on the strength of a count.** The gate is the scenario
  requirements, and the controller reads the gate's verdict rather than
  re-implementing it.

## Phase B

`phase-b` is refused. It needs a generated release caller, which means a decision
about who owns a release hook and what a "release" is when the product release is
the consumer's own — #21 and #22. When that lands, `build_plan` gains the role
and `phase_writers` already names the writers it replaces; nothing else in the
controller assumes Phase A's five callers.

## Related

* [shadow-validation.md](shadow-validation.md) — the evidence and the gate
* [architecture/adr/0001-centralized-reusable-control-plane.md](architecture/adr/0001-centralized-reusable-control-plane.md) — why a central control plane at all
* [parity-ledger.md](parity-ledger.md) — what a ledger entry means
* `src/continuum/migrate/` — the controller
* `tests/test_migrate.py`, `.github/tests/test_workflow_hardening.py` — the tests
