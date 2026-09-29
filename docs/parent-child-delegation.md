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
parent workflows reference only variable names such as
`${{ vars.CONTINUUM_ROLE }}` and `${{ vars.CONTINUUM_CHILDREN }}`.

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

## Parent workflows

Use the three thin entry workflows in
`fixtures/delegation-parent/.github/workflows/`. They call:

- `consumer-child-dispatcher.yml`
- `consumer-child-worker.yml`
- `consumer-child-review.yml`

The wrapper passes `CONTINUUM_ROLE` and `CONTINUUM_CHILDREN` from the
parent's GitHub repository variables. The child repository name is resolved only
inside the runner and is not committed to the parent repository.

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
