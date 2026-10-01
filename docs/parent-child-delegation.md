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

## Parent workflows

Use the four thin entry workflows in
`.github/caller-stubs/parent/`. They call:

- `continuum-consumer-child-dispatcher.yml`
- `continuum-consumer-child-worker.yml`
- `continuum-consumer-child-review.yml`
- `continuum-consumer-child-pr-review.yml`

The wrapper passes no parent/child relationship values. Continuum reads
`CONTINUUM_ROLE` and `CONTINUUM_CHILDREN` directly from the parent repository
through the GitHub API after the runner starts. This avoids exposing the child
list in reusable-workflow inputs or job environment metadata. The child
repository name is resolved only inside the runner and is not committed to the
parent repository.

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

## Install alongside existing automation

The installer has three explicit sets:

```sh
# Core task-domain callers: scheduling, review, repair, auto-merge.
bash install.sh /path/to/myapp main core

# Opt-in technology library: CI, release, release PR, packaging smoke.
bash install.sh /path/to/myapp main tech

# Add parent execution to any repository without replacing its own workflows.
bash install.sh /path/to/parent main parent
```

For production, replace `main` with a tested full Continuum commit SHA. Both
`uses:` and `engine_ref` are set to that revision. The parent set installs
only `continuum-child-dispatcher.yml`, `continuum-child-worker.yml`,
`continuum-child-review.yml`, and `continuum-child-pr-review.yml`. It preserves
existing CI, issue scheduling, OpenCode,
release, Render, and artifact workflows. The token is supplied through the
parent's existing `TAP_PAT` secret; it must be able to read the relevant Actions
repository variables and operate on the selected child repositories.

Do not install the core set in a child repository as a migration
shortcut. Its local scheduler and merge controller could compete with the
parent. Existing children keep their opt-in ownership markers, neutral config,
repository variables, and trusted base-branch validation script. Installing a
parent set does not set variables or enroll repositories automatically.

The parent dispatcher wakes on main pushes, every ten minutes, manual dispatch,
and completion of child task/review workflows. A repository's own scheduler can
also dispatch `continuum-child-dispatcher.yml`; Runtime Lab already does this. Task
execution and independent review remain separate; completed tasks are not
resurrected, dependency-blocked tasks stay blocked, and owner-created PRs use
the independent PR review workflow.

## Migration baseline

The delegation runtime was restored from Continuum commit
`7aa2aec38a97a2639e96a2f2e1168b0571afcc0d`, already used by Runtime Lab. The
worker implementation matches its separately pinned
`954aa575ab385b6785f6923937fceb7ec108c966` version. This preserves the existing
recovery and review behavior while making those entry points available beside
the NanoDictate workflows again.

Two resolver guards are tightened: an invalid parent declaration fails the
plan, and explicit child variables cannot fall back to a conflicting legacy
file. Legacy files remain supported when relationship variables are absent.

This migration deliberately keeps Runtime Lab's Render lifecycle, knowledge
sync, artifact delivery, publication-conflict handling, CI status context, and
local repair controllers in Runtime Lab. They have different contracts from
NanoDictate's macOS build and release automation. The shared boundary for this
first integration is delegated execution; a later migration can extract those
other controllers with their existing tests.

Worker and review workflows now select Python explicitly through the optional
`python_version` input (default `3.12`). Callers can override it without changing
the shared engine. This keeps child validation independent of runner-image
Python defaults.
