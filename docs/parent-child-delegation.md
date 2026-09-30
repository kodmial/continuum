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

The old `.continuum.yml` relationship declaration remains readable only as a
compatibility path while existing consumers migrate. New integrations should use
repository variables.

## Which Continuum release runs

Every child workflow is a reusable workflow the parent pins by one literal
`uses:` reference. The parent also passes `engine_repository` — the same
`owner/repository` name already in that `uses:` line, because GitHub does not
allow an expression there — and the workflow checks out the commit GitHub
selected for the called file (`job.workflow_sha`), not a branch, tag, or input.
The workflow verifies the checkout is exactly that commit before any privileged
step. The two spellings of the repository are proved to agree by construction:
if they disagreed, the checkout would contain a different tree than the one that
defines the run. There is no input by which a caller can select a different
Continuum release.

## Child repository bindings are a secret

The map from an opaque child id to the child's `owner/repository` is the private
part of the relationship. It is supplied to the child workflows through the
`CHILD_RUNTIME_REPOSITORIES` secret (declared by every child workflow and exposed
to the resolver as the `CHILD_REPOSITORIES` environment variable), never through
a repository variable, a workflow input, or a tracked file. A missing, empty, or
malformed map fails closed, the map must name exactly the ids the parent
declared, two ids may not resolve to one repository, and the resolved repository
is verified against the child's own `CONTINUUM_PARENT` before any work runs.

## Parent workflows

Use the three thin entry workflows in
`fixtures/delegation-parent/.github/workflows/`. They call:

- `consumer-child-dispatcher.yml`
- `consumer-child-worker.yml`
- `consumer-child-review.yml`

The wrapper passes no parent/child relationship values, only the
`engine_repository` name that its own `uses:` line already carries. Continuum
reads `CONTINUUM_ROLE` and `CONTINUUM_CHILDREN` directly from the parent
repository through the GitHub API after the runner starts. This avoids exposing
the child list in reusable-workflow inputs or job environment metadata. The child
repository name is resolved only inside the runner, from the
`CHILD_RUNTIME_REPOSITORIES` secret, and is not committed to the parent
repository.

A token with access to the child repositories is still required through the
wrapper's child-runtime secret.

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

The marker text and the branch prefix an earlier release used are the consumer's
to change: `CONTINUUM_TASK_OPT_IN_MARKER`, `CONTINUUM_LEGACY_TASK_MARKER`, and
`CONTINUUM_LEGACY_TASK_BRANCH_PREFIX` are repository variables, and the
dispatcher refuses to run without the opt-in marker set. `continuum-child/`
remains the branch prefix Continuum writes, so only a migration needs the legacy
prefix.

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
