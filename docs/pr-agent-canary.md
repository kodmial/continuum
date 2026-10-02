# PR-Agent canary/E2E rerun (issue #174)

This is the repeatable live proof for Continuum's OpenCode-backed PR-Agent.
It exercises the real enabled GitHub path from a disposable PR without
enabling PR-Agent globally and without changing CodeRabbit.

## Guarantees

* Disabled by default. `CONTINUUM_PR_AGENT_ENABLED` stays `false`.
  The canary opts in per-run only via `CONTINUUM_PR_AGENT_CANARY_ENABLED=1`
  plus the disposable workflow `enabled=true` input.
* Reviewer-only. The bridge pins the read-only `plan` agent; the harness
  fails the run when `git status --porcelain` is non-empty or `HEAD`
  moves under the reviewer.
* Free route only. The harness refuses paid-provider secrets
  (`OPENCODE_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`) and requires
  the default `opencode/muse-spark-1.3-contributor-free` route.
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

## Executable canary (opt-in, disposable only)

The durable executable path is the `continuum-pr-agent-canary.yml`
reusable workflow plus its caller stub of the same name. It is disabled
by default and never enables PR-Agent globally: `CONTINUUM_PR_AGENT_ENABLED`
stays `false`, and only a manual `workflow_dispatch` run with
`canary_enabled=true` (or a one-run
`CONTINUUM_PR_AGENT_CANARY_ENABLED=true` variable) creates the disposable
PR, posts the real `/review` and `/verify <finding-id>` commands as the
owner, waits for the real event-driven `continuum-pr-agent.yml` runs,
proves blocking finding -> correction -> `RESOLVED` -> clean-HEAD
`APPROVED`, uploads the `pr-agent-canary-evidence.json` bundle, then
closes the PR and deletes the branch in a `finally` block even when a
gate fails. CodeRabbit is never referenced or changed, and any
paid-provider credential or non-free route fails the run closed.

```sh
gh workflow run continuum-pr-agent-canary.yml \
  --repo kodmial/continuum --ref main -f canary_enabled=true
```

## Live rerun (maintainer, disposable only)

1. Record the exact current-main SHA:
   `git rev-parse HEAD`.
2. Dispatch the executable canary above (preferred), or manually:
   create a disposable branch/PR from that SHA containing only the known
   blocking defect (for example `canary/blocking-fixture.txt` with a
   `missing-null-check` at line 10).
3. Dispatch the existing `continuum-pr-agent.yml` path for that PR with
   the disposable input `enabled=true` (repository variable stays
   `false`). Post `/review` as the repository owner.
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
   remove canary labels/comments, leave the repo variable disabled, and
   confirm no paid-provider secret was added. Only the reusable harness
   (`src/continuum/pr_agent_canary.py`) and its tests remain.
