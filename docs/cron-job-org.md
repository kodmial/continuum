# Optional external periodic wake with cron-job.org

Continuum's native GitHub `schedule` is fully operational and remains the
authoritative timer. This page describes an **optional** manual backstop: a
cron-job.org job that sends an authenticated GitHub REST `workflow_dispatch` to
the installed **Issue scheduler** caller.

Nothing is provisioned automatically. Continuum does not read a cron-job.org
API key, does not create or sync jobs, and does not change `install.sh` or
`.continuum.yml`. The owner configures this after the implementation is merged.

## Supported target

Use the consumer repository's installed caller:

```text
https://api.github.com/repos/OWNER/REPO/actions/workflows/continuum-issue-scheduler.yml/dispatches
```

All of these are not supported external targets:

- the reusable `continuum-issue-scheduler.yml` engine with only `workflow_call`
  (GitHub REST cannot dispatch it);
- `continuum-pr-agent-recovery.yml` (it has per-child recovery identity and is
  intentionally left on its native schedule);
- Continuum's self-dogfood `automation.yml`, which fans out several unrelated
  jobs on an unqualified manual dispatch.

The caller must be installed at the repository's default branch and Actions
must be enabled. Do not configure this job in a verified child repository:
`CONTINUUM_ROLE=child` callers intentionally skip local scheduling so private
children allocate zero runner minutes. Target the parent that owns delegated
scheduling.

## 1. Create a narrowly scoped token

Create a **fine-grained personal access token**, not a classic `TAP_PAT`:

- Repository access: only the intended execution repository.
- Repository permission: **Actions: write** (Metadata read is added
  automatically).
- Expiry: a short, deliberate period (for example 30 or 90 days) with a
  calendar reminder to rotate it.

Do **not** paste a broad `TAP_PAT` into cron-job.org. `TAP_PAT` is a classic
repository-wide lifecycle credential. The temporary job-scoped
`GITHUB_TOKEN` is also unsuitable because it expires with a workflow run.
Store the fine-grained token only in cron-job.org's request header. Never put it
in a URL, query string, issue, repository file, screenshot, or log.

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
