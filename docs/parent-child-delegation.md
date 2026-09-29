# Parent/child delegated execution

Continuum can delegate issue execution from one repository (the **parent**) to
another repository (the **child**) without using repository visibility as a
routing signal.

The relationship is explicit and bidirectional:

- the parent enables delegation and allowlists opaque child ids;
- the parent may store explicit `id -> owner/repository` bindings in the
  optional `CONTINUUM_CHILD_REPOSITORIES` secret; without that secret it
  discovers owned repositories by their declared Continuum roles;
- each child declares its own id and exact parent repository;
- every dispatcher, worker, and review run verifies both declarations before it
  reads a task or writes to the child.

A repository with no `delegation` block has role `none`. It neither donates
Actions capacity nor delegates tasks.

## Parent configuration

```yaml
version: 1

delegation:
  role: parent
  children:
    - child-a
    - child-b
```

The parent configuration intentionally contains no child repository names.
Configure `CONTINUUM_CHILD_TOKEN` on the parent with access to the intended
children. `CONTINUUM_CHILD_REPOSITORIES` is optional. When supplied, it is a
JSON object whose keys exactly match `delegation.children`, for example
`{"child-a":"owner/private-repo","child-b":"owner/public-repo"}`.

When the map is omitted, Continuum enumerates repositories owned by the token
holder **without any visibility filter**, reads only their `.continuum.yml`,
and selects the unique repository whose child id and parent declaration match
the parent's allowlist. Zero or multiple matches fail closed. This means
`private` is never a role or routing signal.

The exact-key requirement in explicit-map mode remains a safety property: a
secret entry cannot silently turn an unconfigured repository into a child, and
a configured child cannot silently disappear from the runtime binding.

Use the three thin parent entry workflows in
`fixtures/delegation-parent/.github/workflows/`. They call:

- `consumer-child-dispatcher.yml`
- `consumer-child-worker.yml`
- `consumer-child-review.yml`

The visible workflow identity contains only the child id, never the repository
binding. A public parent can therefore donate Actions to a private child
without publishing the private repository name in workflow inputs or run names.

## Child configuration

```yaml
version: 1

delegation:
  role: child
  id: child-a
  parent: owner/parent-repository
```

The child id must be present in the parent's allowlist and must resolve through
the parent's secret map to the repository containing this configuration.
The child must name the calling parent exactly. If either side disagrees, that
child fails closed and no task is executed.


A child may also declare a deterministic validation gate:

```yaml
delegation:
  role: child
  id: child-a
  parent: owner/parent-repository
  validation_script: automation/continuum-child-ci.sh
```

The delegated review loads that script from the child's **base branch**, not
from the pull-request head, then executes it against the candidate worktree
with a minimal environment created by `env -i`. GitHub tokens, child repository
bindings, and model credentials are therefore not inherited by project tests.
Validation stdout/stderr stays in a runner-local file and is never copied to the
public parent logs. A review cannot merge until both the independent agent review
and this deterministic gate pass.

A child task is opt-in per issue. Add:

```html
<!-- continuum-child-owned -->
```

to the issue body. The legacy `<!-- runtime-worker-owned -->` marker remains
accepted during migration. The reusable local scheduler skips both markers, so
a delegated issue cannot race with the child's ordinary OpenCode scheduler.

## Visibility is not routing

Continuum does not enumerate repositories by `visibility=private` and does
not infer relationships from public/private state. These are all valid when the
credential can access both repositories:

- public parent -> private child
- public parent -> public child
- private parent -> private child
- private parent -> public child

Being private does not make a repository a child. Being public does not make a
repository a parent. Only the explicit, bidirectionally verified delegation
contract does.

## Multiple children

One parent may list multiple child ids. Each child has an independent
repository binding and independent task/review concurrency key. Issue numbers
may overlap across children because active runs are keyed by
`child id + task number`.

The initial contract intentionally allows one parent per child. A repository
cannot be both parent and child under the same configuration. Supporting chains
or bridges is a separate role and must not be inferred implicitly.

## Migration

The worker/review path accepts legacy `runtime-worker/task-...` pull-request
branches and the legacy issue ownership marker. New work uses
`continuum-child/task-...` and `continuum-child-owned`.

Repository visibility checks are not part of the new mechanism. Existing
systems should migrate relationship declaration first, verify the bidirectional
handshake, then remove their old discovery-by-visibility code.
