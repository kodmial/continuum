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

## kodmial/nanodictate — the rolling workflow baseline

The sections above are incident findings, one per report. This one is the other
kind of entry: not a list of what was found, but the state of a repository's
`.github/**` at one head, which a gate re-reads and compares. `docs/parity-ledger.json`
is the normative copy and the block below is rendered from it; the JSON is what the
gate reads, so the prose cannot drift away from what was actually classified.

It is regenerated by comparing the ledger with itself, which proves the document
renders and that every entry still resolves, but it is *not* evidence that the
consumer is still at this head. That is the live reading's job, and it is taken at
the moment the gate runs — see `docs/shadow-validation.md`.

<!-- BEGIN GENERATED: docs/parity-ledger.json -->
<!-- Generated by continuum.shadow.baseline.render_markdown from docs/parity-ledger.json. -->

## NanoDictate workflow ledger (#60)

Audited head: `d9e6dcc2e0c9c43bc87cd1963b75c097dec7ea1c`  
Audited at: `2026-09-30T00:00:00Z`  
Discovery issue for unclassified differences: `#60`

| Workflow | Blob | Classification | Owner | Rationale |
| --- | --- | --- | --- | --- |
| `.github/workflows/add-review-label.yml` | `6d0967539a12e72adf7675dc0f608601ee1b6dd2` | `absorbed` | #11 | Per-head review lock is taken when a request is scheduled and released on a decisive review; absorbed by continuum.review.queue. |
| `.github/workflows/auto-merge.yml` | `f641a89943c58cac62d783fa04c1c6dc98c7d1df` | `absorbed` | #11 | Exact-head decisive review supersedes earlier nitpick-only COMMENTED reviews, and the commit status is treated as operational state only. Both are absorbed by continuum.review.coderabbit.head_verdict and status_reason, with regressions in tests/test_coderabbit.py. |
| `.github/workflows/bootstrap-runtime-secret.yml` | `e6295c930867472d88b30e98c208b24427258129` | `not-applicable` | #60 | A migration-era bootstrap diagnostic for the shadow bridge token. Continuum installs the bridge from .github/workflows/continuum-shadow.yml instead, so there is no runtime parity writer to port and nothing is left behind that Continuum removes. |
| `.github/workflows/ci.yml` | `86b9a651e101f13b6fb31e8268225c93e05e6c66` | `consumer-local` | #27 | The coverage threshold and test matrix belong to the product's own suite. Continuum treats required CI as an exact-head blocking gate -- the same head check continuum.release.provenance performs against the candidate run -- through its configured required checks, without naming any product's checks. |
| `.github/workflows/coderabbit-retry.yml` | `a03f3431347b60000733b9c3ea94c0ea1d2f1984` | `absorbed` | #11 | The global quota controller is cancel-in-progress:false, so a repeated wake-up cannot starve a head that is waiting out a cooldown; single flight comes from the shared concurrency group instead. Absorbed by .github/workflows/review-queue.yml, with regressions in tests/test_queue_workflows.py for both halves. |
| `.github/workflows/coderabbit-unresolved.yml` | `67a2df34d7e902441ce7c0d633accf52a2ed9471` | `absorbed` | #11 | Verdict parsing strips collapsed <details> blocks, fenced code and block-quoted history before reading a token, so a quoted older verdict cannot override the visible one. Absorbed by continuum.review.coderabbit._visible_conclusion, with regressions in tests/test_coderabbit.py including the truncated-details case. |
| `.github/workflows/continuum-migration-preflight.yml` | `7b00e14db22d1e42981d6605436be34adfe15a2b` | `consumer-local` | #60 | Added after the 2026-09-30 audit point. Reports what a migration would have to carry before cutover, from the consumer's own tree. It reads and reports, so it has no generic semantics to port and no write Continuum has to preserve; the generic migration report is .github/workflows/continuum-shadow.yml. |
| `.github/workflows/continuum-shadow-bridge.yml` | `34cc3ebbb8cb21a3e892158ec3bbe21b0a024bc9` | `absorbed` | #27 | Added after the 2026-09-30 audit point. The thin lifecycle bridge Continuum documents for installation; the matching implementation and its test are reference/nanodictate-workflows/shadow-bridge.yml and tests/test_shadow_workflow.py. |
| `.github/workflows/issue-scheduler.yml` | `bc3f8c175210ab26c1f6033572e678356ec4d788` | `absorbed` | #27 | Byte-identical to the preserved snapshot. Priority ordering and the dependency-blocked refusal are absorbed by continuum.shadow.planner. |
| `.github/workflows/opencode-repair.yml` | `56d08b852ed6f4c1c1b0456eee6d313a34e79a45` | `absorbed` | #27 | A stale persistent repair lock is released on age rather than left to suppress every later repair of the same head, an unreadable age is treated as held rather than released, and the age budget is resolved once in conflict-bounds instead of being spelled out per step. Absorbed by the age-checked lock in .github/workflows/consumer-repair.yml; asserted in tests/test_repair_workflow.py. |
| `.github/workflows/opencode.yml` | `e5656c07b5758d4974540912f52a60525be889f8` | `consumer-local` | #27 | The generic lifecycle -- dispatch, install retry with backoff, and recovery from a failed dispatch that clears the lock so a later run can retry -- is absorbed by the dispatch/repair ladder in .github/workflows/consumer-repair.yml. The macOS and Swift toolchain and the runner image that provides them are consumer capability configured per repository; Continuum's workflows run on ubuntu-latest and name neither. |
| `.github/workflows/packaging-smoke.yml` | `c6b99cc0e6c2ed11cf2e1b9886eac86c47272429` | `consumer-local` | #21 | Candidate, post-publish and canary smoke are the consumer's own production validation. Continuum consumes the candidate run as an exact-head blocking gate and as the release candidate's artifact source, which is continuum.release.provenance; neither half names a product, and the smoke cases themselves stay consumer-local. |
| `.github/workflows/pr-agent.yml` | `53485dcafb1e06318c56b4dd1f2e64c22cedd5c0` | `superseded` | #27 | A manually dispatched advisory reviewer, outside MVP orchestration. Retained in the consumer through cutover rather than deleted, and Continuum's own PR-Agent capability in continuum.review.pr_agent supersedes it. |
| `.github/workflows/release-automation-merge.yml` | `75ae83ffd55b11c2e9b49bc91e1920bf24e83484` | `absorbed` | #21 | Rejects active unfilled template tokens while ignoring the generated files' explanatory comments, including a token named inside one. Absorbed by continuum.release.publishers.validation.unfilled_template_tokens, with regressions in tests/test_release_publishers.py. The main-push wake-up is consumer-local: it decides which events wake this consumer's release automation and names no product behaviour Continuum should port. |
| `.github/workflows/release-pr.yml` | `c0870464a47c698a22acc206aa77eee068cb8b44` | `absorbed` | #21 | A completed release wakes release-PR reconciliation so a deferred update cannot strand. The derivation is absorbed by continuum.release.state.owed_reconciliation (with wake_key scoping one wake per source head) and is asserted in tests/test_release_state.py; deciding which events dispatch that wake is the consumer's release-automation workflow, so Continuum supplies the rule and not a trigger. |
| `.github/workflows/release.yml` | `f699aaa4ea70d431962daae48ca4a8c6842088e7` | `must-port` | #22 | Phase B requirement: publication must reuse the exact artifact bytes validated by Packaging smoke for the same immutable source head. NanoDictate keeps its existing release workflow as the sole writer during Phase A; #22 must prove and migrate this invariant before the release writer is replaced. |
| `.github/workflows/remove-review-label.yml` | `26f05b0c80934195f2d9c52663c3a059fd902210` | `absorbed` | #11 | Per-head review and repair locks are reset together when a new head arrives; absorbed by the head-scoped lock reset in continuum.review.queue. |

### Open pull requests touching `.github/**`

| Pull request | Paths | Classification | Owner | Rationale |
| --- | --- | --- | --- | --- |
| `#67` | `.github/pull_request_template.md`, `.github/workflows/ci.yml`, `.github/workflows/packaging-smoke.yml`, `.github/workflows/release.yml`, `.github/workflows/rust.yml` | `consumer-local` | #60 | A shared Rust engine build and portability gates. Product behavior with no generic semantic, and none of the five paths reintroduces a scheduler, review, repair, merge or release writer Continuum removes, so it cannot restore pre-cutover orchestration. It stays classified so the gate keeps proving that. |

<!-- END GENERATED: docs/parity-ledger.json -->
