# Cross-repository parity ledger

Status: **normative for classification, not for behaviour**. This file records
what happened to each incident found in the two upstream repositories, and which
of the five classifications applies to it. It exists so that "we fixed #136"
and "we already had #136" are distinguishable answers, and so that an audit of
this repository does not have to re-derive them from a diff.

Parent: runtime-lab #59. Dispatched implementation: runtime-lab #61.

## The five classifications

| Classification | Meaning |
| --- | --- |
| `must-port` | The gap is real here and Continuum does not yet close it. |
| `absorbed` | Continuum already had the property, by a different mechanism. Nothing to add. |
| `consumer-local` | The fix belongs in one consumer's configuration, not in Continuum. |
| `superseded` | The report describes a state that no longer exists. |
| `not-applicable` | The situation cannot arise in Continuum's design, so porting it would add a mechanism for a case that does not exist. |

A classification is a claim about *this* repository. The same gap in a different
repository is a different claim, and `not-applicable` is a design statement
rather than a judgement on the original report.

## The ledger

### runtime-lab #23 — `must-port`

Lossless agent publication. The agent's work had to reach a pull request without
being guessed at, re-derived, or dropped.

Closed by `.github/scripts/agent_workspace.py` plus the recovery steps in
`.github/workflows/opencode.yml` and `.github/workflows/consumer-opencode.yml`:
the branch is derived from the issue number, provenance is proven by candidate
refs rather than by trusting the model's current branch, and the change is
materialized by `git commit-tree` from the workspace tree rather than by
rebasing commits that may no longer apply.

Tests: `.github/tests/test_agent_workspace.py`, including a real-git clone.

### runtime-lab #99 — split

Two distinct reports share this number.

**Core cross-repository write-back: `not-applicable`.** Continuum's agent plane
writes only to a branch on the repository it was dispatched against, and the
branch name is derived from the issue number by policy. There is no second
repository for it to write back into, so the mechanism the report asks for would
be a mechanism with nothing to point at.

**Immutable-target / compare-and-swap: `absorbed`.** The property the report
wants — that a push must not silently overwrite a moved target — is enforced by
`--force-with-lease="refs/heads/$HEAD_REF:$ORIGINAL_HEAD"` in both agent
workflows, and by the immutable release-tag input on the consumer workflow. The
mechanism differs; the property does not.

### runtime-lab #131 — `absorbed`

Publication idempotence. Continuum queries for open pull requests in
`--state open` before doing anything, so a re-dispatched run finds the existing
pull request rather than opening a second one.

Absorbed rather than `must-port` because the behaviour is a consequence of the
recovery path already implemented for #23, not a separate feature.

### runtime-lab #136 — `must-port`

Point-of-use OpenCode identity. Closed by `.github/scripts/opencode_runtime.py`.

The install verifies a digest, then makes the binary reachable by name. Every
check in between is real and none of them concerns the process that actually
executes, because `$GITHUB_PATH` **appends** while `PATH` resolution takes the
first match. Anything earlier on `PATH` providing a file called `opencode`
would run instead of the verified binary, with every digest check still green.

The script records the resolved binary's digest at install and re-checks it
immediately before each invocation in both agent workflows.

Tests: `.github/tests/test_opencode_runtime.py`.

### runtime-lab #143 — `absorbed`

Trust boundary around the privileged agent job. Continuum gates the
write-capable job behind a separate `authorize` job that resolves the
dispatching credential, keeps `"git *":"deny"` on the agent's own OpenCode
permissions — so the model can inspect its workspace but cannot push, and
`curl`, `wget`, `printenv`, `env`, `set`, and the credential-reading `gh` and
`ssh` subcommands are denied too — and makes the model produce a commit and a PR
body rather than performing either.

Tests: `.github/tests/test_trust_policy.py` and the `AgentExecutionTests` /
`UntrustedEventTests` classes in `.github/tests/test_workflow_hardening.py`.

### runtime-lab #145 — `must-port`

Repair lock with a release path. The gate-repair label was added on dispatch and
removed on a passing run or a successful repair push. A dispatched repair that
was cancelled, timed out, or wedged produced neither, so the label survived and
every later failure re-entered the step, saw the label, and exited. CI repair
for that pull request was then over for good while the pull request stayed open
and blocking.

Closed by an age check on the lock in `.github/workflows/consumer-repair.yml`:
a lock older than the repair budget is released, annotated, and re-dispatched. A
lock with an unreadable age is treated as **held**, because releasing a lock on
no evidence would let two repairs of one pull request run at once. The budget
comes from `conflict-bounds` rather than a second literal, so the bound cannot
drift between workflows.

Tests: `GateRepairLockTests` in `.github/tests/test_conflict_repair.py`, which
runs the real step's shell against a `gh` stand-in that honours `--jq` and
rejects flags the real `gh` does not have.

### kodmai #35 — `must-port`

Agent workspace stranded on a branch the workflow does not own. When the model
commits to a branch other than the workflow's own, the work was invisible to the
recovery path, which only looked at the owned ref. `agent_workspace.py` now
detects stranded work, refuses to publish it under a guessed branch, and
salvages it to `opencode/issue<N>-salvage-${GITHUB_RUN_ID}`.

Tests: `SalvageTests` and `WorkspacePolicyTests` in
`.github/tests/test_agent_workspace.py`.

## What is deliberately not here

- **No cross-repository write-back mechanism.** See runtime-lab #99.
- **No second copy of the repair budget.** It is resolved through
  `.github/scripts/conflict_repair.py` in both workflows.
- **No manual publication step.** The workflow owns commit, push, and pull
  request. A human step would be a step that fails silently at 03:00.
- **No model permission to push or open pull requests.** See runtime-lab #143.

## Verification

```
python3 -m unittest discover -s .github/tests -p 'test_*.py'
PYTHONPATH=src python3 -m unittest discover -s tests -t . -p 'test_*.py'
PYTHONPATH=src python3 -m continuum.cli config-check --config .continuum.yml
```
