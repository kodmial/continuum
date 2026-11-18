# Continuum

[English](README.md) | [Русский](README.ru.md)

Continuum is a reusable GitHub Actions control plane that turns an admitted
issue into a pull request and merges it only when the current pull-request HEAD
passes the gates you configure. It is for maintainers of GitHub repositories
who want autonomous implementation with explicit human admission,
pull-request-only writes, and merge gates they can audit.

**Get started: [Quick Start](docs/quickstart.md)** — a copy-paste walkthrough
for one fresh, isolated test repository.

## How it works

```text
admitted issue -> OpenCode implementation -> pull request
  -> CI -> optional configured review
  -> bounded repair when needed
  -> exact-HEAD merge gate -> merge only when every gate passes
```

Admission is opt-in configuration: `AUTOMATION_READY_LABEL` defaults to empty,
and while it is empty the scheduler may dispatch any eligible open issue without
a label. Set it before enabling automation, for example to `automation:ready`,
and from then on you decide which issues are ready. Automation writes through
branches and pull requests, never directly to `main`. Merge is gated on evidence
for the exact current HEAD and happens only when every configured gate passes. A
missing, stale, or failing gate stops at the gate; a green run is never
guaranteed.

## What you get

- **Admission and scheduling** — labels, leases, WIP limits, blockers, and
  fail-closed reconciliation decide which issues may start.
- **PR-only implementation and repair** — OpenCode implements on a branch and
  opens a pull request, then bounded repair handles CI or review findings.
- **One project-owned CI contract** — your `ci.yml` stays yours and calls the
  shared validation engine; the merge gate requires exact-HEAD evidence.
- **Optional review providers** — CodeRabbit, PR-Agent, or no review provider
  at all.
- **Optional parent/child delegation** — an advanced multi-repository layer
  installed after the single-repository path works.

## Documentation

- [Quick Start](docs/quickstart.md) — the safe first run, including the negative
  admission check.
- [Technical overview](docs/technical-overview.md) — installation and
  upgrading, the secrets and variables contract, failure diagnosis, and the
  exact live-qualification status.
- [Consumer configuration variables](docs/consumer-variables.md) — the complete
  variable contract.
- [Parent/child delegated execution](docs/parent-child-delegation.md) — the
  advanced multi-repository scenario.
- [Contract qualification](docs/contract-qualification.md) — the exact-HEAD gate
  model.
- [Optional periodic wake](docs/cron-job-org.md) — a manual scheduler backstop
  after merge.
- [Release workflow reference templates](docs/release-templates.md) — historical,
  inactive examples; not a supported release feature.

## Requirements and safety

- A GitHub repository with Actions enabled and permission to add workflows.
- A **classic** personal access token with `repo` + `workflow` scopes, stored as
  `TAP_PAT`.
- Branch protection for `main`: required pull requests, required current-HEAD
  checks, no routine bypass, and force-push/deletion blocked. Continuum cannot
  protect `main` by itself.
- Core workflows use free anonymous OpenCode models. Those routes can be rate
  limited, unavailable, or changed by their provider, and no uptime, latency,
  reliability, or cost guarantee is offered.
- Linux on `ubuntu-latest` is the qualified default runner. Do not assume
  macOS or Windows hosted-cache support.

Start with a disposable repository and no valuable open issues. The
[safety model](docs/technical-overview.md#safety-model),
[status and limitations](docs/technical-overview.md#status-and-limitations), and
[what has been live-qualified](docs/technical-overview.md#what-has-been-live-qualified)
are documented in full.

## Secrets and variables

| Secret | Required when | Purpose |
| --- | --- | --- |
| `TAP_PAT` | Lifecycle workflows need authenticated GitHub writes | Classic PAT with `repo` + `workflow` scopes. |
| `RENDER_API_KEY` | Render execution is enabled | Render-only API credential with no fallback to `TAP_PAT`. |
| `CHILD_RUNTIME_TOKEN` | Parent callers invoke a child workflow | Delegated child access; parent callers map it from the parent's `TAP_PAT`. |
| `CHILD_RUNTIME_REPOSITORIES` | Legacy parent configuration only | Compatibility path for pre-variable child relationships. |
| `CONTINUUM_VERIFICATION_ATTESTATION_KEY` | Optional signed resumable verification evidence | HMAC-SHA256 key used only by the runner-side verification engine. |

Core workflows do not use paid provider keys (`OPENCODE_API_KEY`,
`ANTHROPIC_API_KEY`, `GROQ_API_KEY`, or `GROQ.KEY`) and run on free anonymous
models. No workflow reads them to authenticate; the optional PR-Agent canary
reads the `OPENCODE_API_KEY`, `ANTHROPIC_API_KEY`, and `GROQ_API_KEY` repository
variables only to reject the run if any is set. `secrets.GITHUB_TOKEN` is
minted by GitHub automatically. The
[technical overview](docs/technical-overview.md#secrets-and-variables) explains
each entry, the verification-key provisioning path, and the full repository
variable defaults.

Repository variables: `AUTOMATION_READY_LABEL` controls admission and
`CONTINUUM_REVIEW_PROVIDER` selects `none`, `coderabbit`, or `pr-agent`. The
[scheduler and review variables](docs/technical-overview.md#secrets-and-variables)
and the [consumer variables reference](docs/consumer-variables.md) cover the
rest.

## License

Continuum is **source-available**, not OSI-approved open source, under the
[Business Source License 1.1](LICENSE) (SPDX: `BUSL-1.1`). Non-production use is
allowed; the additional production-use grant is limited to noncommercial
purposes and must not provide a competing product or service. Each version
converts to the Apache License 2.0 on its Change Date; the [license
text](LICENSE) is authoritative.
