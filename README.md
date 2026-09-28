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

This repository is public, so every input the automation reads is attacker-supplyable:
anyone can open an issue, comment on one, open a pull request from a fork, and
forge a `workflow_dispatch` payload. The rules below are enforced by
[`.github/scripts/trust_policy.py`](.github/scripts/trust_policy.py) and audited by
[`.github/tests/`](.github/tests), which CI runs on every pull request.

**Who is trusted.** The repository owner, plus anyone listed in the
`AUTOMATION_TRUSTED_ACTORS` repository variable. Collaborators and bots are *not*
trusted by default — `author_association: COLLABORATOR` is a self-declared field on
an issue, so it is not evidence of identity. A `GITHUB_TOKEN` acts as
`github-actions[bot]`, whose comments the agent workflow refuses.

**What starts work.** Only a newly *created* comment whose first non-empty line is
`/oc` or `/opencode`, on a non-pull-request issue that a trusted actor opened.
Editing an existing comment cannot trigger execution.

**What gets executed.** Only same-repository pull requests against the base branch,
on an `opencode/issue<N>-<slug>` branch, at a full commit SHA. The privileged job
checks out `${{ needs.authorize.outputs.checkout_ref }}` — the base branch, or a
SHA the policy verified — never the pull request's own code.

**Separation of powers.** Each automation is split so that deciding is never the
same privilege as acting:

| Workflow | Decides (read-only) | Acts (write) |
| --- | --- | --- |
| `opencode.yml` | `authorize` | `opencode` |
| `opencode-repair.yml` | trust gate + decision step in each job | label/dispatch step |
| `auto-merge.yml` | `plan` | `reconcile` |
| `issue-scheduler.yml` | `authorize` | `schedule` |

A verification step never holds a write-capable credential, with one deliberate
exception: `check-token` must carry the PAT, because no read-only token can report
which account a credential speaks for. It performs a single `GET /user` and no
write, and the audit asserts that.

**Human review is never optional for the trust boundary.** Any change to
`.github/workflows/`, `.github/scripts/`, `.github/actions/`, `CODEOWNERS`, or
`action.yml` is refused automatic merge and automatic repair; those pull requests
get `opencode-human-review-required` instead.

**Untrusted text stays data.** Issue bodies, comments, and pull-request titles are
neutralized (terminal escapes, forged `::` workflow commands, bidirectional
overrides, zero-width characters, control bytes) and fenced before reaching a model
prompt, and titles are sanitized before becoming commit messages.

The policy fails closed throughout: a missing field, an absent API response, a
failed check, or an unrecognised shape is a denial, never a fallback to trusting
the input.

### Configuration

| Name | Kind | Default | Purpose |
| --- | --- | --- | --- |
| `TAP_PAT` | secret | — | Required. Write-capable credential for the agent, repair, merge, and scheduler steps. |
| `AUTOMATION_TRUSTED_ACTORS` | variable | repository owner | Comma-separated logins trusted in addition to the owner. |
| `AUTOMATION_WIP_LIMIT` | variable | `4` | Concurrent scheduled issues. |
| `AUTOMATION_LEASE_MINUTES` | variable | `45` | Reservation lease before an issue is retried. |
| `AUTOMATION_MAX_DISPATCH_ATTEMPTS` | variable | `2` | Dispatches before an issue is paused for a human. |
| `AUTOMATION_TRUSTED_ACTORS` must not include bots: a bot-authored comment or dispatch can never satisfy the author gate. |


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
