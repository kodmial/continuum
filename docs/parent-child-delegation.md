# Parent/child delegated execution

Continuum can execute work for a child repository from a parent repository
without treating public/private visibility as a role.

## Relationship storage

Concrete relationship values live in GitHub Actions **repository variables**,
not in tracked repository files.

Parent repository variables:

- `CONTINUUM_ROLE=parent`
- `CONTINUUM_CHILDREN` — JSON array of opaque child ids, for example
  `["child-a","child-b"]`

Child repository variables:

- `CONTINUUM_ROLE=child`
- `CONTINUUM_CHILD_ID` — the id present in the parent's
  `CONTINUUM_CHILDREN`
- `CONTINUUM_PARENT` — exact `owner/repository` name of the parent
- `CONTINUUM_VALIDATION_SCRIPT` — optional repository-relative `.sh` path
  for deterministic validation

The repository's tracked `.continuum.yml` does not need to contain a role,
child list, child id, or parent name. A neutral file such as the following is
sufficient:

```yaml
version: 1
```

This keeps concrete parent/child relationships out of the source tree. The thin
parent workflows do not pass relationship values as workflow inputs or job
environment values. The Continuum runtime reads the caller repository's Actions
variables directly through the GitHub API inside the runner.

## Discovery and bidirectional verification

The parent uses its runtime token to enumerate repositories owned by the token
holder **without any visibility filter**. For each candidate it reads the
candidate's GitHub Actions repository variables and accepts the repository only
when all of these conditions hold:

1. the requested child id is present in the parent's `CONTINUUM_CHILDREN`;
2. the candidate has `CONTINUUM_ROLE=child`;
3. its `CONTINUUM_CHILD_ID` equals that requested id;
4. its `CONTINUUM_PARENT` equals the calling parent repository exactly.

Zero matches or multiple matches fail closed. Repository visibility is never a
routing signal.

The old `.continuum.yml` relationship declaration and optional
`CONTINUUM_CHILD_REPOSITORIES` secret remain readable only as a compatibility
path while existing consumers migrate. New integrations should use repository
variables.

## Parent workflow

A parent repository writes one file:
`fixtures/delegation-parent/.github/workflows/continuum.yml`. It names the
release entrypoint at a single exact Continuum reference and dispatches into it
by `mode`:

- `mode: child:worker` reaches `consumer-child-worker.yml`
- `mode: child:review` reaches `consumer-child-review.yml`
- `mode: child:pr-review` reaches `consumer-child-pr-review.yml`
- an empty `mode` reconciles the parent's own control plane, which includes
  `consumer-child-dispatcher.yml`

Those are reached through the entrypoint's same-repository relative references,
so one line in the parent's ingress selects the dispatcher and all three child
implementations as one snapshot. They used to be three separate parent entry
workflows, each carrying its own Continuum reference; that is the shape ADR-0002
rules out, because three references that agree today are three places that
decide which Continuum a parent runs and can be moved independently.

The parent's ingress declares the union of inputs every controller dispatches
into it: the four agent modes the scheduler and the repair controller send, the
two repair budgets, and the three child modes. GitHub rejects a dispatch whose
input the target workflow does not declare, so an incomplete union is a dispatch
that fails in the parent's repository rather than a mistake that fails in review.

Reconciliation runs on the parent's schedule and whenever its own configuration
changes. It does not run on `workflow_run`. A called workflow's jobs execute
inside the caller's run, so the only workflow name such a filter could match is
the ingress's own name — and a run that completes because of `workflow_run`
completes in a way that triggers `workflow_run` again. The old per-child files
were separate workflows and could be named; after the cutover they are jobs, so
that trigger could only have been either dead or a loop.

The wrapper passes no parent/child relationship values. Continuum reads
`CONTINUUM_ROLE` and `CONTINUUM_CHILDREN` directly from the parent repository
through the GitHub API after the runner starts. This avoids exposing the child
list in reusable-workflow inputs or job environment metadata. The child
repository name is resolved only inside the runner and is not committed to the
parent repository.

A token with access to the child repositories is still required through the
ingress's child-runtime secret.

## Deterministic child validation

When `CONTINUUM_VALIDATION_SCRIPT` is set on the child, delegated review loads
that script from the child's **base branch**, never from the pull-request head,
and executes it against the candidate worktree with a minimal `env -i`
environment.

GitHub tokens, repository bindings, and model credentials are not inherited by
the project validation process. Validation output remains runner-local. Merge
requires both independent review acceptance and successful deterministic
validation.

## Task opt-in

A child task is opt-in per issue. Add:

```html
<!-- continuum-child-owned -->
```

The legacy `<!-- runtime-worker-owned -->` marker remains accepted during
migration. The local scheduler skips both markers so local private Actions do
not race the parent execution path.

## Visibility is not routing

All of these relationships are valid when the credential has access:

- public parent -> private child
- public parent -> public child
- private parent -> private child
- private parent -> public child

Being private does not make a repository a child. Being public does not make a
repository a parent. Only the explicit repository-variable relationship does.

## Multiple children

One parent may configure multiple opaque child ids in
`CONTINUUM_CHILDREN`. Each child has an independent repository binding and
task/review concurrency key. Issue numbers may overlap between children.

The current contract allows one parent per child. A repository is not
simultaneously parent and child under this contract.
