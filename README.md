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
- generic bootstrap CI;
- conflict/CI repair;
- a temporary review-free auto-merge controller.

The complete NanoDictate workflow set is preserved verbatim under
`reference/nanodictate-workflows/`.

CodeRabbit and release automation are intentionally **not active workflows** yet.
Their implementations are preserved in the reference snapshot and will be
reintroduced as optional adapters after the configuration/module boundaries are
implemented.

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
