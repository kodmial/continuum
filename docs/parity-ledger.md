# Cross-repository parity ledger

Status: **normative for classification, not for behaviour**. This file records
what happened to each incident found in the two upstream repositories, and which
of the five classifications applies to it. It exists so that "we fixed #136"
and "we already had #136" are distinguishable answers, and so that an audit of
this repository does not have to re-derive them from a diff.

Parent: kodmial/runtime-lab#59. Dispatched implementation: kodmial/runtime-lab#61.
Machine-readable account: [`reference/parity-ledger.json`](../reference/parity-ledger.json).
How the two are kept identical: `tests/test_parity_ledger.py`.

## The two accounts, and why there are two

| Account | Read by | Holds |
| --- | --- | --- |
| This file | a person | why each classification was reached, and what closed it |
| `reference/parity-ledger.json` | the drift checker | the audited commit range, the dispositions, and the paths each source tracks |

A document only a person reads cannot stop a repository drifting, and a document
only a machine reads cannot be reviewed. `tests/test_parity_ledger.py` compares
them incident by incident, so an edit to one that is not made to the other fails
the build rather than being discovered during an audit.

**Continuum is public, so a private repository cannot be named here.** The one
private classification below (`kodmai#35`) is therefore not in the committed
ledger at all: it lives in an operator-supplied overlay, which is the only file
that may name a private source. `reference/parity-ledger-overlay.example.json` is
the shape of that file, with a placeholder repository in place of a real one.
`docs/parity-drift.md` explains how the overlay and its least-privilege
credential are supplied to a scan.

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

The checker branches on exactly this vocabulary, and the two dispositions that
mean *nothing here has to change* are the reason a live repository that moves
every day does not produce a finding every day: `consumer-local` and
`not-applicable` classify the path itself rather than one commit, so they keep
being true as the file advances. The other three describe a state that was true
of one commit range, so an advance past it is new evidence and is triaged.

## The ledger

### kodmial/runtime-lab#23 — `must-port`

Lossless agent publication. The agent's work had to reach a pull request without
being guessed at, re-derived, or dropped.

Closed by `.github/scripts/agent_workspace.py` plus the recovery steps in
`.github/workflows/opencode.yml` and `.github/workflows/consumer-opencode.yml`:
the branch is derived from the issue number, provenance is proven by candidate
refs rather than by trusting the model's current branch, and the change is
materialized by `git commit-tree` from the workspace tree rather than by
rebasing commits that may no longer apply.

Tests: `.github/tests/test_agent_workspace.py`, including a real-git clone.

### kodmial/runtime-lab#99-write-back — `not-applicable`

Core cross-repository write-back, the first of two reports that share number 99.
Continuum's agent plane writes only to a branch on the repository it was
dispatched against, and the branch name is derived from the issue number by
policy. There is no second repository for it to write back into, so the mechanism
the report asks for would be a mechanism with nothing to point at.

Nothing in this repository answers it, and that is the classification: the ledger
asks for a Continuum implementation or test reference only for `absorbed`,
`must-port` and `superseded`.

### kodmial/runtime-lab#99-immutable-target — `absorbed`

Immutable-target / compare-and-swap, the second report numbered 99. The property
the report wants — that a push must not silently overwrite a moved target — is
enforced by `--force-with-lease="refs/heads/$HEAD_REF:$ORIGINAL_HEAD"` in both
agent workflows, and by the immutable release-tag input on the consumer workflow.
The mechanism differs; the property does not.

### kodmial/runtime-lab#131 — `absorbed`

Publication idempotence. Continuum queries for open pull requests in
`--state open` before doing anything, so a re-dispatched run finds the existing
pull request rather than opening a second one.

Absorbed rather than `must-port` because the behaviour is a consequence of the
recovery path already implemented for #23, not a separate feature.

### kodmial/runtime-lab#136 — `must-port`

Point-of-use OpenCode identity. Closed by `.github/scripts/opencode_runtime.py`.

The install verifies a digest, then makes the binary reachable by name. Every
check in between is real and none of them concerns the process that actually
executes, because `$GITHUB_PATH` **appends** while `PATH` resolution takes the
first match. Anything earlier on `PATH` providing a file called `opencode`
would run instead of the verified binary, with every digest check still green.

The script records the resolved binary's digest at install and re-checks it
immediately before each invocation in both agent workflows.

Tests: `.github/tests/test_opencode_runtime.py`.

### kodmial/runtime-lab#143 — `absorbed`

Trust boundary around the privileged agent job. Continuum gates the
write-capable job behind a separate `authorize` job that resolves the
dispatching credential, keeps `"git *":"deny"` on the agent's own OpenCode
permissions — so the model can inspect its workspace but cannot push, and
`curl`, `wget`, `printenv`, `env`, `set`, and the credential-reading `gh` and
`ssh` subcommands are denied too — and makes the model produce a commit and a PR
body rather than performing either.

Tests: `.github/tests/test_trust_policy.py` and the `AgentExecutionTests` /
`UntrustedEventTests` classes in `.github/tests/test_workflow_hardening.py`.

### kodmial/runtime-lab#145 — `must-port`

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

### kodmai#35 — `must-port`

**Overlay-only.** Recorded here for the human account; the machine account for it
lives in an uncommitted overlay, because naming a private repository in a public
file would disclose it. Its id in the operator's overlay is the full
`kodmial/kodmai#35`.

Agent workspace stranded on a branch the workflow does not own. When the model
commits to a branch other than the workflow's own, the work was invisible to the
recovery path, which only looked at the owned ref. `agent_workspace.py` now
detects stranded work, refuses to publish it under a guessed branch, and
salvages it to `opencode/issue<N>-salvage-${GITHUB_RUN_ID}`.

Tests: `SalvageTests` and `WorkspacePolicyTests` in
`.github/tests/test_agent_workspace.py`.

## Which paths of a tracked source are checked

The classification of an *incident* is not the classification of a *path*, so the
ledger records both. Each source's `paths` list says what an advance in a given
path means, and it is the only part of the ledger a machine branches on. An
advance is a finding when it lands on a path with no rule at all: a path nobody
classified is a question, never a pass.

For `kodmial/runtime-lab` the rules classify the automation Continuum mirrors
(`absorbed`), the one mechanism that cannot apply (`not-applicable` — the
cross-repository write-back of #99), and the repository's own configuration and
records (`consumer-local`). Anything else that changes in that repository is
unclassified drift, which is the correct answer for a file the sweep never
looked at.

## What is deliberately not here

- **No cross-repository write-back mechanism.** See kodmial/runtime-lab#99-write-back.
- **No second copy of the repair budget.** It is resolved through
  `.github/scripts/conflict_repair.py` in both workflows.
- **No manual publication step.** The workflow owns commit, push, and pull
  request. A human step would be a step that fails silently at 03:00.
- **No model permission to push or open pull requests.** See kodmial/runtime-lab#143.
- **No upstream code copied into Continuum.** A drift finding is a request to
  classify, not a diff to apply; see `docs/parity-drift.md`.

## Verification

```
python3 -m unittest discover -s .github/tests -p 'test_*.py'
PYTHONPATH=src python3 -m unittest discover -s tests -t . -p 'test_*.py'
PYTHONPATH=src python3 -m continuum.cli config-check --config .continuum.yml
PYTHONPATH=src python3 -m continuum.parity.cli ledger-check --ledger reference/parity-ledger.json
```