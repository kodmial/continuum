# Continuum

Reusable GitHub automation and project-lifecycle platform.

Continuum is being extracted from automation that has already been exercised in
real repositories. The initial reference implementation is an exact snapshot of
the NanoDictate workflows at commit
`64e89a3bcbab671513a933855e7495a07fac56bb`.

## Bootstrap state

The repository currently dogfoods a deliberately small active control plane:

- dependency-aware issue scheduling and priority labels;
- OpenCode execution/repair plumbing derived from NanoDictate;
- generic bootstrap CI (unit tests + configuration validation);
- conflict/CI repair;
- a temporary review-free auto-merge controller.

The complete NanoDictate workflow set is preserved verbatim under
`reference/nanodictate-workflows/`.

CodeRabbit and release automation are intentionally **not active workflows** yet.
Their implementations are preserved in the reference snapshot and will be
reintroduced as optional adapters after the configuration/module boundaries are
implemented.

## Review gate

The first reusable review adapter (`review.provider: pr-agent`) is, as of the
P0 issue #25, now **the blocking review signal** for the merge controller. The
imported NanoDictate reviewer is extracted into a provider-agnostic Continuum
adapter:

- One normalized result document (`continuum.review-gate/v1`) and one commit
  status contract: `verdict=<V> state=<S> provider=<P> head=<sha>` in the
  context named by `review.status_context` (default `continuum/review`).
- `auto-merge.yml` reads **only** that contract; it refuses to merge a PR whose
  current HEAD lacks a green, current-HEAD gate. `review.provider: none` is a
  deliberate zero-traffic opt-out and a free pass.
- The adapter never hard-codes an endpoint, model, or credential: `.continuum.yml`
  references repository **variable/secret names** only, and workflows request
  exactly those credentials (`pr-agent.yml` and `pr-agent-comment.yml`). Manual
  `/review` and `/verify <finding-id>` commands follow the trusted-actor contract
  (repository owner only) and parse via the Continuum module, never a shell or
  a GitHub expression.
- CodeRabbit is a sibling adapter behind the same normalized gate
  (`review.provider: coderabbit`).

To enable in another repository, validate a `.continuum.yml`, then call the
reusable workflow — see `fixtures/consumer-repo/` for the reference consumer.

## Public-repository safety

During bootstrap, automatic issue execution is limited to issues opened by the
repository owner. The scheduler also refuses to reserve/dispatch work when the
workflow-capable `TAP_PAT` secret is not configured.

This prevents a public issue from becoming a privileged agent prompt.

## Roadmap

GitHub Issues are the execution plan. Native `blocked by` relationships define
the dependency DAG, while `priority:p0`, `priority:p1`, and `priority:p2`
define scheduling priority.

The first phase creates feature flags and security boundaries. The next phase
performs an evidence-based industry benchmark. Reusable workflows, authentication
hardening, reliability, CodeRabbit, release adapters, fault injection,
observability, versioning, and migration tooling follow from that baseline.

## Reference snapshot

See [reference/nanodictate-workflows](reference/nanodictate-workflows/README.md).
