# Optional external periodic wake with cron-job.org

[English](cron-job-org.md) | [Русский](cron-job-org.ru.md)

Continuum's native GitHub `schedule` is fully operational and remains the
authoritative timer. This page describes an **optional** manual backstop: a
cron-job.org job that sends an authenticated GitHub REST `workflow_dispatch` to
the installed **Issue scheduler** caller.

> [!WARNING]
> Treat the cron-job.org token as a credential. Use a short-lived fine-grained
> token with only **Actions: write** and store it exclusively in the
> `Authorization` header. Never put a token in a repository, URL, query string,
> issue, log, or screenshot.

Nothing is provisioned automatically. Continuum does not read a cron-job.org
API key, does not create or sync jobs, and does not change `install.sh` or
`.continuum.yml`. The owner configures this after the implementation is merged.

## Supported target

Use the consumer repository's installed caller:

```text
https://api.github.com/repos/OWNER/REPO/actions/workflows/continuum-issue-scheduler.yml/dispatches
```

This is a valid external target because a dispatch with
`{"inputs":{"periodic_wake":"true"}}` goes through the scheduler's cheap
periodic window gate before any checkout, issue pagination, runtime download,
or domain dispatch. A second periodic signal in an already-completed window is
a NOOP.

All of these are **not supported external target** choices:

- the reusable `continuum-issue-scheduler.yml` engine with only `workflow_call`
  (GitHub REST cannot dispatch it);
- `continuum-pr-agent-recovery.yml` (it has per-child recovery identity and is
  intentionally left on its native schedule);
- Continuum's own self-dogfood `automation.yml` **as an Issue Scheduler
  target**, even though it now accepts the same input:

  ```text
  https://api.github.com/repos/OWNER/REPO/actions/workflows/automation.yml/dispatches
  ```

  A default `workflow_dispatch` (without the narrow `auto_merge_only` selector)
  to `automation.yml` starts `scheduler` **and** the unrelated `watchdog`,
  `auto-merge`, and, for `periodic_wake=true`, `global-pr-watchdog`. The
  `periodic_wake` input gates only the scheduler job's work, so an external
  periodic wake there is not cheap and is not supported as an Issue Scheduler
  target. Use the installed `continuum-issue-scheduler.yml` caller instead. The
  separate, supported auto-merge-only self-dogfood backstop is documented in
  [Self-dogfood auto-merge-only backstop](#self-dogfood-auto-merge-only-backstop).

The caller must be installed at the repository's default branch and Actions
must be enabled. Do not configure this job in a verified child repository:
`CONTINUUM_ROLE=child` callers intentionally skip local scheduling and delegate
it to the parent. Target the parent that owns delegated scheduling.

## 1. Create a narrowly scoped token

Create a **fine-grained personal access token**, not a classic `TAP_PAT`.
Whatever label the owner gives it (for example "Continuum Wake"), the name is
only a human label and is not a contract:

- Repository access: the intended execution repository or repositories. One
  token may cover several repositories.
- Repository permission: **Actions: write** (Metadata read is added
  automatically).
- Expiry: a short, deliberate period (for example 30 or 90 days) with a
  calendar reminder to rotate it.

Do **not** paste a broad `TAP_PAT` into cron-job.org. `TAP_PAT` is a classic
repository-wide lifecycle credential. The temporary job-scoped
`GITHUB_TOKEN` is also unsuitable because it expires with a workflow run.
Store the fine-grained token only in cron-job.org's `Authorization` header.
Never put it in the repository, a URL, a query string, an issue, a log, or a
screenshot.

## 2. Configure the cron-job.org request

In the cron-job.org UI, create a job with:

- **URL**:
  `https://api.github.com/repos/OWNER/REPO/actions/workflows/continuum-issue-scheduler.yml/dispatches`
- **Method**: `POST`
- **Headers**:

  ```text
  Authorization: Bearer <FINE_GRAINED_PAT>
  Accept: application/vnd.github+json
  Content-Type: application/json
  ```

- **Body**:

  ```json
  {"ref":"main","inputs":{"periodic_wake":"true"}}
  ```

  Use the repository's actual default branch in `ref` if it is not `main`.
  `periodic_wake` is a mode selector, not an authorization bypass. Omitting it
  or sending anything other than `true` keeps ordinary full manual dispatch
  behavior.

- **Schedule**: UTC, matching the native cadence but a little later. The
  installed Issue scheduler cron is `7,17,27,37,47,57 * * * *`. A useful
  external backup is minute `9,19,29,39,49,59` (or "every 10 minutes" starting
  at :09). This lets a normal native run finish first and still covers a
  skipped or delayed native delivery.

Use HTTPS and do not enable a redirect that could forward the `Authorization`
header elsewhere.

## 3. How duplicate protection behaves

Both sources map onto the same native 10-minute window anchored at minute :07.
The domain key is the installed Issue scheduler caller's workflow on the
repository default branch, so a native run and a dispatched run share it. A
completed successful periodic run from **either** source is evidence for that
window, so the next periodic signal exits at the cheap gate before checkouts,
issue pagination, runtime downloads, or domain dispatch.

- Native -> external, external -> native, or simultaneous signals: the second
  periodic pass is a NOOP for that window. The concurrency group serializes
  runs, and a queued/replaced pending run never performs work.
- A failed, cancelled, timed-out, or crashed attempt is not evidence. The next
  tick retries under the existing bounded recovery policy.
- If the native run was skipped or delayed, the first due external signal runs
  the same reconcile logic immediately. It is not blocked by the previous
  window.
- Ordinary manual dispatches, authentic issue/PR/CI events, and time-critical
  recovery are never throttled by this periodic-only guard.

A direct REST dispatch still creates a small Actions run that performs the
cheap NOOP check. "No duplicate work" means no second substantial
reconciliation or agent/review/repair/merge side effect in the window, not the
absence of that small run. If the run-history lookup itself is unavailable
(for example a transient 403/429/5xx), the guard fails open and runs the pass;
the scheduler's existing state-based reservation and dispatch markers still
prevent duplicate domain side effects.

## 4. Verify the setup

1. Use cron-job.org's test button. GitHub returns **HTTP 204** when the
   dispatch event is accepted. `204` means accepted, not that the job finished.
2. Open the target repository's Actions run list for **Issue scheduler**.
   A periodic dispatch has a run name containing `[continuum-periodic]`.
   Workflow dispatches also carry `[continuum-wake:<input>]`; the gate trims
   and case-folds that raw token, so padded or differently-cased `true` inputs
   are deduplicated consistently with the caller marker.
3. Open the run and read the **Admit periodic wake for this scheduling window**
   step:
   - `window-due` means the pass ran;
   - `duplicate-window` means another periodic pass already completed this
     window and the domain steps were skipped;
   - `lookup-unavailable` means the guard could not read run history and
     deliberately ran the pass instead of stranding work.
4. Confirm the reconcile result is what you expect for the repository. A green
   gate alone does not prove that an issue was dispatched.

## 5. Rotate and disable

- To rotate the token, create a new fine-grained PAT, update the
  `Authorization` header in cron-job.org, verify one dispatch, then revoke the
  old token.
- To disable the backstop, pause or delete the cron-job.org job. No repository
  change is required; the native GitHub schedule continues as before.

## Troubleshooting

| Status | Likely cause | Check |
| --- | --- | --- |
| `401` | Token missing, expired, or revoked | Recreate/rotate the fine-grained token and update the header. |
| `403` | Token lacks **Actions: write**, is not allowed to the repository, or an organization SSO policy blocks it | Check repository access and authorize SSO if required. |
| `404` | Wrong owner/repo, caller missing on the default branch, or Actions disabled | Verify the installed caller URL and workflow state. |
| `422` | Invalid `ref` or an input the caller does not declare | Use the default branch and `periodic_wake: "true"`. |
| No run appears | The request never reached GitHub or was rejected | Check the cron-job.org response status and request headers. |
| Run is a NOOP | Another periodic pass already completed the window | Inspect the gate step reason; this is expected duplicate protection. |

## Self-dogfood auto-merge-only backstop

The Issue Scheduler setup above is the consumer-facing backstop. Continuum's own
repository has a second, narrower self-dogfood target: the existing
`automation.yml` caller can wake **only** its existing auto-merge job. Native
`schedule` stays enabled, and both native events and an external dispatch enter
the same `continuum-auto-merge.yml` reusable engine with the same inputs and the
same repository-global serialization (`group: <workflow>-auto-merge`,
`cancel-in-progress: false`). This is an optional operator-created backup for a
lost or delayed GitHub `schedule` when there are no new events; it is not a
second reconciler or merge writer.

Nothing here provisions or reads cron-job.org account settings. Creating,
testing, and maintaining the job is an explicit manual operator step.

### Dispatch target and payload

- **URL**:
  `https://api.github.com/repos/kodmial/continuum/actions/workflows/automation.yml/dispatches`
- **Method**: `POST`
- **Headers**:

  ```text
  Authorization: Bearer <FINE_GRAINED_PAT>
  Accept: application/vnd.github+json
  Content-Type: application/json
  ```

- **Body**:

  ```json
  {"ref":"main","inputs":{"periodic_wake":"true","auto_merge_only":"true"}}
  ```

Use the repository's actual default branch in `ref` if it is not `main`. Both
inputs are required for this backstop. `periodic_wake=true` is the existing
external periodic marker from the Issue Scheduler setup, and a broad dispatch
keeps the established `[continuum-periodic]` / `[continuum-wake:true]` markers.
`auto_merge_only=true` is a narrow job selector: it skips `scheduler`,
`watchdog`, and `global-pr-watchdog` (including when `periodic_wake=true`), while
`repair` already skips every dispatch. Because the narrow run never runs the
scheduler, it is stamped with `[continuum-auto-merge-only]` and
`[continuum-wake:auto-merge-only]` instead; the scheduler's periodic window gate
reads only `true` as periodic evidence, so a successful narrow run cannot make a
later periodic dispatch skip its window. The existing `auto-merge` caller, its
`uses` target, `with` inputs, permissions, and engine stay unchanged. Both
inputs are mode selectors, not authorization overrides.

Never send only `periodic_wake=true` to `automation.yml` as an external
auto-merge-only backstop. That keeps its original broad dispatch behavior:
`scheduler`, `watchdog`, `auto-merge`, and `global-pr-watchdog` all start. The
Issue Scheduler section above explains why that target is unsupported
for the scheduler. `auto_merge_only=true` may be sent alone for a narrow manual
test, but the cron-job.org payload uses both inputs.

### Token and schedule

Reuse the same narrow **fine-grained personal access token** guidance as the
Issue Scheduler setup: repository access to `kodmial/continuum`, repository
permission **Actions: write** (Metadata read is added automatically), and a
short deliberate expiry with a rotation reminder. Store it only in cron-job.org's
`Authorization` header. Never put a token in the repository, a URL, a query
string, an issue, a log, or a screenshot. Do not paste a broad `TAP_PAT` or the
run-scoped `GITHUB_TOKEN` into cron-job.org.

The native auto-merge safety-net cron is `17,47 * * * *`. A useful external
backup is UTC minute `:19` and `:49`, letting the native slot run first while
still covering a skipped or delayed delivery. HTTP `204` means GitHub accepted
the dispatch event, **not** that reconciliation succeeded or that any eligible
PR merged.

### Verification and troubleshooting

1. Use cron-job.org's test button and confirm the dispatch is accepted (`204`).
2. Open the `kodmial/continuum` Actions run list for **Continuum automation**.
   The external run should show `[continuum-auto-merge-only]` and
   `[continuum-wake:auto-merge-only]`, and only the `auto-merge` job should
   execute; `scheduler`, `watchdog`, `repair`, and `global-pr-watchdog` should
   be skipped.
3. Open the `auto-merge` job and read the canonical engine outcome. A truthful
   child-role skip is also valid. The reconciler may legitimately wake its own
   existing recovery/review workflows when ordinary reconciliation requires
   them; that is not a second merge writer.
4. Redundant native, event, and external runs are permitted. Serialization plus
   latest-state reconciliation and exact-HEAD idempotence prevent a duplicate
   merge or duplicate effects for unchanged state; they do not make a redundant
   run free.

| Status | Likely cause | Check |
| --- | --- | --- |
| `401` | Token missing, expired, or revoked | Recreate/rotate the fine-grained token and update the header. |
| `403` | Token lacks **Actions: write**, is not allowed to the repository, or an organization SSO policy blocks it | Check repository access and authorize SSO if required. |
| `404` | Wrong owner/repo, `automation.yml` missing on the default branch, or Actions disabled | Verify the exact URL and workflow state. |
| `422` | Invalid `ref` or an input the caller does not declare | Use the default branch and both `periodic_wake` and `auto_merge_only`. |
| No run appears | The request never reached GitHub or was rejected | Check the cron-job.org response status and request headers. |
| Token expiry | The fine-grained PAT reached its expiry date | Issue a new PAT, update the header, verify one dispatch, then revoke the old one. |

## Optional self-dogfood OpenCode recovery backstop

The native `*/15 * * * *` watchdog schedule remains active. To cover a
missing/delayed schedule without triggering the scheduler, auto-merge, or the
cross-repository watchdog, target the **same** `automation.yml` dispatch URL
and fine-grained **Actions: write** token described above, but send:

```json
{"ref":"main","inputs":{"periodic_wake":"true","watchdog_only":"true"}}
```

Do not set both narrow selectors: for backward compatibility
`auto_merge_only=true` takes precedence if both are true. The watchdog-only
run is marked `[continuum-watchdog-only] [continuum-wake:watchdog-only]`, so
it is never scheduler-window evidence. A useful external backup is UTC minute
`:03, :18, :33, :48`. Native and external eventless watchdog passes share one
concurrency lane; an actual completed catch-up at the same default-branch
SHA in the current 15-minute window is required to skip a redundant periodic
pass. A skipped job, failed run, or unverifiable inventory is not such
evidence. Ordinary event-driven recovery and manual watchdog-only dispatch
without `periodic_wake` are not deduplicated by the periodic gate. The
watchdog still independently checks holds, checkpoint identity, and existing
claims/runs before dispatching; a wake is not permission to restart a task.

An owner removing the configured pause label from an open issue also wakes
the same watchdog. This does not bypass holds or start work from the beginning;
a valid reserved checkpoint must still pass the existing recovery guards.

Verify HTTP 204, the watchdog-only run markers, and the actual
`watchdog / recover` job. All other jobs must be skipped. If the periodic
gate reports a prior completed catch-up, verify the cited run's attestation
step. To confirm recovery, require an observed fresh-VM run for the exact
operation/generation/checkpoint, not merely an accepted HTTP request or a
green controller. The external job is optional and must be configured by the
operator; Continuum neither stores its token nor claims a backup exists
until an actual external dispatch is observed.
