# PR-Agent canary/E2E rerun (issue #174)

This page documents the repeatable live canary for Continuum's
OpenCode-backed PR-Agent. It exercises the real enabled GitHub path from a
disposable PR without enabling PR-Agent globally and without changing
CodeRabbit; it does not assert that a live run has already happened. Run it
only in a disposable consumer repository: the provider setting is
repository-wide, so `CONTINUUM_REVIEW_PROVIDER=pr-agent` selects PR-Agent for
that entire repository, not only for the disposable PR.

## Guarantees

* Provider selection is repository-level
  `CONTINUUM_REVIEW_PROVIDER=none|coderabbit|pr-agent`. The deployed callers
  have no `CONTINUUM_PR_AGENT_ENABLED` switch and no `enabled` input on the
  ordinary `continuum-pr-agent.yml` caller. Instructions that still mention
  either as the enable control are obsolete; only the offline reference
  harness described below retains its own legacy flags.
* Opt-in per dispatch. The installed canary caller takes
  `canary_enabled=true` for one run. The repository variable
  `CONTINUUM_PR_AGENT_CANARY_ENABLED` is only a fallback; it is persistent,
  does not reset after a run, and keeps every dispatched canary enabled until
  you unset it. Prefer the explicit dispatch input.
* Reviewer-only. The bridge pins the read-only `plan` agent; the harness
  fails the run when `git status --porcelain` is non-empty or `HEAD`
  moves under the reviewer.
* No paid provider. The canary refuses the repository variables
  `OPENCODE_API_KEY`, `ANTHROPIC_API_KEY`, and `GROQ_API_KEY`, and it refuses
  named paid-provider references in the explicit `opencode_model` dispatch
  input, the legacy `OPENCODE_MODEL` repository variable, and the request
  model. It does not read GitHub secrets with those names, so a secret alone
  does not trigger the refusal; audit secrets separately. Entries in the
  trusted `OPENCODE_MODELS` pool are not screened for paid providers;
  configure only free routes there. It does not hard-code a model: the deployed
  `model-route` step resolves the trusted `OPENCODE_MODELS` pool, the legacy
  `OPENCODE_MODEL` single-route fallback when no pool is configured, or an
  explicit `opencode_model` dispatch input that must be an allowed pool route.
  A free route can still be rate-limited or unavailable; the canary fails
  closed instead of promising that inference succeeds.
* Fail closed. Stale HEAD, partial chunk coverage, bridge/OpenCode
  failure, reviewer mutation, a missing blocking finding, an unresolved
  verification, or incomplete evidence all fail instead of reporting
  success.

## Offline gate check (no network)

```sh
mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_pr_agent_canary -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -c "from continuum.pr_agent_canary import self_check; print(self_check().describe())"
```

The offline `self_check` reference harness still validates its own
`opencode/muse-spark-1.3-contributor-free` default route and legacy enable
gate; those constants are not the deployed canary's model or provider
policy. The live workflow uses the routed configuration described below.

## Executable canary (opt-in, disposable only)

The reusable engine `.github/workflows/continuum-pr-agent-canary.yml` in
`kodmial/continuum` exposes only `workflow_call`, so it cannot be dispatched
in the engine repository. The dispatchable caller is
`.github/caller-stubs/continuum-pr-agent-canary.yml`; install the `core`
caller set into a disposable consumer repository and run the installed
`continuum-pr-agent-canary.yml` there.

Prerequisites in that consumer repository:

* the core callers are installed and the installed canary caller is on the
  selected ref;
* `TAP_PAT` is configured;
* `CONTINUUM_REVIEW_PROVIDER=pr-agent`, a repository variable that selects
  PR-Agent for the whole consumer repository, not only the disposable PR, so
  the installed PR-Agent router and recovery callers handle the `/review`
  commands;
* a working project-owned `CI` workflow runs on the disposable PR, because
  the review path admits an exact CI-qualified HEAD;
* `OPENCODE_MODELS` (preferred) or the legacy `OPENCODE_MODEL` contains a
  trusted route, with no paid-provider key configured.

The installed caller accepts these `workflow_dispatch` inputs:
`canary_enabled`, `base_ref`, `opencode_model`, `model`, `max_tokens`,
`api_base`, `bridge_port`, `server_port`, and `pr_agent_version`. Empty inputs
fall back to the trusted repository configuration and documented defaults.
`max_tokens` is retained but is not passed to the `/review` run, so it does
not control review budgeting.

```sh
export CONSUMER_REPO="YOUR_OWNER/YOUR_DISPOSABLE_REPO"
gh workflow run continuum-pr-agent-canary.yml \
  --repo "$CONSUMER_REPO" --ref main -f canary_enabled=true
```

The workflow is disabled by default and never enables PR-Agent globally. A
manual dispatch with `canary_enabled=true` creates the disposable PR, posts
the real `/review` and `/verify <finding-id>` commands as the owner, waits for
the real event-driven `continuum-pr-agent.yml` runs, proves blocking finding
-> correction -> `RESOLVED` -> clean-HEAD `APPROVED`, uploads the
`pr-agent-canary-evidence.json` bundle, then attempts to close the PR and
delete the branch in a `finally` block even when a gate fails. The evidence
bundle's cleanup claim is written before those attempts, and cleanup errors
are only warnings, so after every run verify that the disposable PR is closed
and its branch is deleted, and manually remove either resource if it remains.
CodeRabbit is never referenced or changed.

## Live rerun (maintainer, disposable only)

1. Record the exact current `main` SHA in the **consumer** repository, not the
   Continuum checkout used for the offline check: use
   `gh api "repos/$CONSUMER_REPO/commits/main" --jq .sha`, or
   `git rev-parse HEAD` from a consumer checkout at `main`.
2. Choose the dispatch path (preferred) or the manual path:
   * **Dispatch:** run the installed canary caller above with
     `canary_enabled=true`, monitor that workflow run
     (`gh run list --repo "$CONSUMER_REPO" --workflow continuum-pr-agent-canary.yml`
     and `gh run view <run-id> --repo "$CONSUMER_REPO"`), and collect its
     evidence, including the `pr-agent-canary-<run-id>` artifact and the
     evidence comment. The workflow already posts the review and verification
     commands, closes the PR, and deletes the branch; do not repeat steps 3-8.
     Cleanup failures are warnings, so verify that the PR and branch were
     actually removed and remove either resource if it remains.
   * **Manual:** create a disposable branch/PR from the consumer `main` SHA
     recorded above, containing only a known blocking defect (for example
     `canary/blocking-fixture.py` with an unguarded `None` dereference and an
     unsafe shell sink), then perform steps 3-8.
3. Ensure `CONTINUUM_REVIEW_PROVIDER=pr-agent` in the same repository, then
   post `/review` as the repository owner. The installed router/recovery
   callers dispatch the event-driven `continuum-pr-agent.yml` run; do not
   dispatch the reusable-only workflow directly.
4. Confirm the bridge (`127.0.0.1:<bridge_port>/v1/models`) and
   `opencode serve` health, capture the review/comment IDs, and confirm
   the stable finding id `pra-<sha12>` for the defect.
5. Push the correction commit, post `/verify <finding-id>`, and confirm
   `**RESOLVED**` at the new HEAD.
6. Confirm the clean current HEAD reaches `APPROVED` with full chunk
   coverage (`reviewed == total`).
7. Record every ID in the evidence bundle validated by
   `continuum.pr_agent_canary.require_evidence` (main SHA, canary PR,
   review/verify command IDs, review ID, workflow run IDs, bridge/OpenCode
   evidence, finding id, verify verdict, final outcome, cleanup).
8. Clean up: close the disposable PR, delete the disposable branch,
   remove canary labels/comments, unset any persistent
   `CONTINUUM_PR_AGENT_CANARY_ENABLED` variable, and confirm no
   paid-provider secret was added. Only the reusable harness
   (`src/continuum/pr_agent_canary.py`) and its tests remain.
