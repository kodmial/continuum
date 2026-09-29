# Credential capabilities

Continuum runs on a public repository, so every input its automation reads is
attacker-supplyable. This document is the answer to one question: **for each
thing Continuum does to GitHub, which credential performs it, and why is that
credential the narrowest one that works?**

The short version: most steps need nothing more than the job's own
`github.token`, scoped by the job's `permissions` block. Three things cannot be
expressed that way, and those three use a short-lived GitHub App installation
token minted for a named capability. A personal access token exists only as an
explicit, opt-in migration fallback and is not part of the model.

## The three tiers

| Tier | What it is | Lifetime | Who may see it |
| --- | --- | --- | --- |
| **Ambient** | `${{ github.token }}`, scoped by the job's `permissions` block | The job | Any step in that job that references it |
| **Capability** | A GitHub App installation token, minted per job for one named capability, scoped to this repository | One hour | Only the steps that name it; one of them may be a model |
| **Fallback** | `TAP_PAT`, a classic personal access token | Until rotated by hand | Never, unless the repository explicitly opts in |

The order is fixed and enforced in code: a capability is attempted first, the
fallback is consulted only if the App is *unconfigured* **and** the repository
variable `CONTINUUM_ALLOW_PAT_FALLBACK` is `true`, and any other failure — a
wrong identity, an over-granted token, a revoked key — is a hard stop. A
transient problem with the narrow credential must never be allowed to resolve
into a broader one.

## Why the tiers exist at all

`github.token` is not merely "the safe option"; it has two structural limits
that this repository depends on.

**It does not start CI for changes it makes.** A pull request created or updated
with `github.token` produces a `pull_request` run in *approval-required* state
with zero jobs. The change would sit unverified forever, which is precisely the
state Continuum's repair controller is built to notice and rescue. A branch
update and an agent push both need a credential whose events start a real CI run.

**It speaks as `github-actions[bot]`, and the receiving workflow refuses that.**
Continuum's own agent workflow accepts a `/oc` comment and a `workflow_dispatch`
from a trusted identity, and `github-actions[bot]` is not one. A scheduler
dispatching with `github.token` would consume an issue reservation and produce no
work at all.

Both limits are properties of *identity and events*, not of permissions, so no
`permissions:` block can fix them. A capability-scoped installation token can,
and that is the only reason a non-ambient credential exists here.

## The registry

The registry is the single declaration of what a capability may do, and it is
code rather than documentation:
[`.github/scripts/app_credentials.py`](../.github/scripts/app_credentials.py).

Print it with:

```console
$ python3 .github/scripts/app_credentials.py capabilities
```

| Capability | Credential | Permissions | Why it is not the ambient token |
| --- | --- | --- | --- |
| `read-only` | `github.token` | actions, contents, issues, pull-requests: read | It *is* the ambient token. Every gate, plan, and verification step in this repository. |
| `issue-write` | App | contents, pull-requests: read; **issues: write** | The scheduler posts the `/oc` comment that starts the agent. That comment must survive the agent's own author gate, and `github.token` cannot author it. |
| `pull-request-write` | `github.token` | contents, issues: read; **pull-requests: write** | The job's own scope. Recording a repair outcome, releasing a lock, and commenting on a pull request. |
| `contents-write` | `github.token` | **contents: write**; pull-requests: read | An ordinary branch push. The workflow-file capability is separate, so this one is never granted `workflows`. |
| `workflow-files-write` | `github.token` | **contents, workflows: write**; pull-requests: read | The job's own scope. A push that touches `.github/workflows/**` needs `workflows: write`, which `github.token` can hold. |
| `actions-dispatch` | `github.token` | **actions: write**; contents, pull-requests: read | Re-running a failed run and waking a workflow are the two events `github.token` triggers normally. It escalates to `repair-control` only when the *receiving* workflow gates on the sender. |
| `merge` | `github.token` | **contents, pull-requests: write**; issues: read | Squash-merging an approved pull request is exactly the job's declared scope. |
| `repair-control` | App | **actions, contents, issues, pull-requests: write** | The repair controller does both things `github.token` cannot: it updates branches in a way that starts CI, and it dispatches the agent as a trusted sender. |
| `agent-authoring` | App | **contents, workflows, pull-requests: write**; issues: read | The agent pushes its own branch and opens its own pull request, which has to start CI automatically. This is the only credential a model can read. |

`assert_registry()` in that module refuses a registry that has stopped being a
policy: at least one capability must still be served by `github.token`, and every
minted capability's reason has to name what `github.token` cannot do for it. A
capability the ambient token already serves cannot be minted at all.

## What each workflow does, and with what

### `issue-scheduler.yml` — decides, then dispatches

| Operation | Credential |
| --- | --- |
| Compute the allowlist of issues that may be dispatched | `github.token` (read-only job) |
| Prove the dispatch credential is a trusted identity | `issue-write` capability, `check-token`, one `GET` and no write |
| Comment, label, and release issue state | `issue-write` capability |

The control plane is read-only and holds no write scope at all. The execution
plane writes only through the minted capability, so the workflow's own token
cannot comment, label, or dispatch even if a later edit added a call to the wrong
step.

### `opencode.yml` — the agent

| Operation | Credential |
| --- | --- |
| Authorize the event, plan the repair ladder | `github.token` (read-only job) |
| Push the agent branch, open its pull request, comment on it, change workflow files | `agent-authoring` capability — **this is what the model sees** |
| Record issue ownership, publish a repair outcome | `github.token` (`issues`/`pull-requests: write`) |
| Re-run a failed CI run | `github.token` (`actions: write`) |
| Mint the capability | `agent-authoring` capability |

The job's ambient token can talk to the issue tracker and to a workflow run and
can read code. It cannot push: every write to the repository goes through the
capability, so the authority a compromised model holds is a capability rather
than a job's ambient scope.

### `opencode-repair.yml` — the repair controller

| Job | Operation | Credential |
| --- | --- | --- |
| `recover-failed-issue-run` | Comment, relabel, wake the scheduler | `github.token` — the scheduler's control plane does not gate on its dispatcher |
| `sync-stale-prs` | `update-branch` stale branches, record attempts, dispatch the agent | `repair-control` capability |
| `sync-current-pr` | `update-branch`, lock labels, dispatch the agent | `repair-control` capability |
| `ci-repair` | Lock labels, dispatch the agent | `repair-control` capability |
| `repair-watchdog` | Release stale locks and opt-outs, comment | `github.token` |
| every job | Every trust decision | `github.token` (read-only), never the capability |

In the three jobs that hold a capability, `GITHUB_TOKEN` stays the read-only
ambient token and `GH_TOKEN` carries the capability, so `trust_policy.py
verify-dispatch` can never be looking at the credential it is verifying.

### `auto-merge.yml` and `review-queue.yml`

Both act with the job's own `github.token`. Merging an approved pull request,
closing the issue it fixed, and waking the scheduler are all inside the declared
`permissions` block, and the scheduler does not gate on its dispatcher.
`review-queue.yml` still accepts an optional `continuum_token` secret for a
consumer repository that needs a different scope; it no longer falls back to
`TAP_PAT`.

## Trust identity is not a capability

Two separate questions, deliberately kept apart:

* **Capability** — what a credential is allowed to do. Enforced by the registry,
  by the `permissions` block, and by GitHub itself.
* **Identity** — who a credential speaks as, and whether this repository trusts
  that account to *start* work. Enforced by `trust_policy.py`, which refuses
  every bot in `AUTOMATION_TRUSTED_ACTORS` and trusts exactly one bot, the
  configured `CONTINUUM_AUTOMATION_LOGIN`.

That separation is why a PAT is not simply "the credential with everything":
`check-token` will accept it only if it resolves to a configured trusted
account, and a token that is perfectly valid but speaks as an account this
repository does not name is refused anyway. Trust is a property of the
configuration, not of the credential's power.

A bot's comment is routinely a verbatim echo of what a stranger wrote, so no bot
may be added to `AUTOMATION_TRUSTED_ACTORS`; `parse_actor_list` drops bot
logins so that mistake cannot be armed by configuration.

## The model-facing boundary

`agent-authoring` is the only credential a model can read. It carries no issue
and no actions authority, so a compromised model can push code to its own pull
request and nothing else — it cannot move labels, dispatch a workflow, or
re-run CI.

It may change workflow files, because without `workflows: write` a push touching
`.github/workflows/**` is rejected and an agent fixing its own pipeline would
have to stop and ask a human. The other half of that grant is that this
repository refuses to auto-repair or auto-merge a pull request that touches the
privilege boundary: any change under `.github/workflows/`, `.github/scripts/`,
`.github/actions/`, `CODEOWNERS`, or `action.yml` gets
`opencode-human-review-required` instead. The audit asserts both halves
together, so removing either one fails CI.

## Setup

1. Create a GitHub App. It needs no callback URL and no webhook. Grant these
   repository permissions:

   | Permission | Level | Used by |
   | --- | --- | --- |
   | Contents | Read and write | `agent-authoring`, `repair-control` |
   | Issues | Read and write | `issue-write`, `repair-control` |
   | Pull requests | Read and write | `agent-authoring`, `repair-control` |
   | Workflows | Read and write | `agent-authoring` |
   | Actions | Read and write | `repair-control` |

   The App's own permission set is an upper bound: each minted token requests a
   subset, and a token that comes back with more than the capability asked for is
   refused rather than used.

2. Install the App on this repository only, and note its bot login
   (`<slug>[bot]`, e.g. `continuum-automation[bot]`).

3. Set the configuration:

   | Name | Kind | Purpose |
   | --- | --- | --- |
   | `CONTINUUM_APP_ID` | variable | The App's numeric ID, from its settings page. |
   | `CONTINUUM_APP_PRIVATE_KEY` | secret | The generated `.pem`. Literal `\n` escapes are accepted. |
   | `CONTINUUM_AUTOMATION_LOGIN` | variable | The App's bot login, e.g. `continuum-automation[bot]`. Trusted as a dispatch sender and as the scheduler's comment author. |
   | `CONTINUUM_ALLOW_PAT_FALLBACK` | variable | Migration only. Set to `true` to permit `TAP_PAT` while the App is being set up. Remove it afterwards. |

4. Verify with a manual `workflow_dispatch` on `issue-scheduler.yml` and on
   `opencode-repair.yml`. Both fail closed with a named reason if the App is not
   installed or the identity does not match.

## Rotation, expiry, and failure

**Expiry.** An installation token lives one hour. Every job mints its own at the
start of the phase that needs it, so no token outlives the job that minted it and
there is nothing to schedule. The one job that can run longer than an hour — the
agent, bounded at 180 minutes by policy — may find a capability that expired
mid-run; the failure is a `401` from GitHub on a single API call, the run fails,
and the repair controller re-dispatches it. If that is not acceptable for a given
repository, `CONTINUUM_ALLOW_PAT_FALLBACK=true` is the supported answer during
migration; there is no longer-lived minted credential to fall back on.

**Key rotation.** Replace `CONTINUUM_APP_PRIVATE_KEY`. There is nothing to
restart: each job mints from the current secret. The App's *installation* is
separate from the key — uninstalling the App, or revoking it, fails every mint
with GitHub's own 404, which is not treated as "App unconfigured" and therefore
does not silently fall back to a PAT.

**Failure behaviour.**

| Failure | What happens |
| --- | --- |
| App not installed on the repository | Mint fails with GitHub's 404 naming the repository. Hard stop; a PAT is *not* used, because this is a real failure rather than a missing configuration. |
| `CONTINUUM_APP_ID` or `CONTINUUM_APP_PRIVATE_KEY` unset | Mint fails naming both variables, plus what to set. Falls back to `TAP_PAT` only if the fallback variable is `true`. |
| Token comes back with more authority than requested | Refused. The error lists the extra scopes; this is the condition that would indicate a misconfigured App, so it never falls back. |
| Token speaks as a different account than `CONTINUUM_AUTOMATION_LOGIN` | Refused, naming both identities. Never falls back. |
| Fallback enabled but `TAP_PAT` empty | Refused, naming the two ways to fix it. |
| `openssh`/`openssl` unavailable or key unparseable | Refused; the temporary key file is removed on every path. |
| Any of the above inside the agent or the scheduler | The run fails before the checkout or before the first write, so a partial change is not possible. The scheduler's `authorize` job fails first, so `schedule` never starts. |

**Logs.** The token is written to exactly one place, an `::add-mask::` line,
because Actions requires it. It goes to the environment through `GITHUB_ENV`;
only non-secret metadata (source, kind, identity, capability, expiry, granted
permissions) is written to `GITHUB_OUTPUT`. The model's bash allowlist denies
`printenv`, `env`, `curl`, `wget`, `gh auth`, and `ssh`, and steps that run the
model never reference `${{ github.token }}` at all.

## Reference snapshot

`reference/nanodictate-workflows/` is an inert snapshot of the workflows this
repository was derived from. It is not part of the active workflow set — CI's
allowlist and `test_ci_only_asserts_the_disabled_snapshot` both assert that —
and its `TAP_PAT` references are historical. They are not a live credential and
are not migrated.
