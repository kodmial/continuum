# Parity drift

`docs/parity-ledger.md` is the record of *what Continuum mirrored and why*.
This is what happens when the thing it mirrored moves.

The checker's whole job is to notice that upstream changed something Continuum
believes it mirrored, and to say so exactly once per source. It does not copy
code, open a pull request, or decide that an advance is fine because it looks
familiar.

```
ledger (committed) ─┐
                     ├─► fetch ─► scan ─► report ─┬─► one issue per drifting source
overlay (private)  ─┘                             └─► may this consumer pin move?
```

## The three questions

| Command | Question | Needs a credential | Fails when |
| --- | --- | --- | --- |
| `ledger-check` | is the committed ledger a trustworthy record? | no | a finding means the file is not the record it claims to be |
| `fetch` | what is true of each tracked source right now? | yes, for private sources | the plane could not do its job |
| `scan` | is any of that drift, and at what priority? | only if it reads live | the plane could not do its job |
| `issue` | what single issue should exist per drifting source? | no | the plan cannot be computed |
| `apply` | what commands carry that plan out? | no, and it writes | the plan cannot be applied |
| `promote` | may a consumer pin move? | no | it refuses *by answering*, and exits 1 |

```sh
PYTHONPATH=src python3 -m continuum.parity.cli ledger-check --ledger reference/parity-ledger.json
PYTHONPATH=src python3 -m continuum.parity.cli scan --out parity-out/drift-report.json --markdown parity-out/drift-report.md
PYTHONPATH=src python3 -m continuum.parity.cli issue --report parity-out/drift-report.json --existing issues.json
PYTHONPATH=src python3 -m continuum.parity.cli apply --plan parity-out/issue-plan.json --dry-run
PYTHONPATH=src python3 -m continuum.parity.cli promote --report parity-out/drift-report.json --consumer acme-widgets --fail-on-refused
```

**Drift is a result, not a failure.** An upstream repository moving is the normal
state of a live repository, so `scan` exits 0 when it finds P0 drift. Only a
broken plane — an unreadable ledger, an unwriteable artifact — is an error. The
one command meant to say no is `promote`, and it says no with an exit code and a
verdict document, not a crash.

`scan --fail-on-drift` is the exception a scheduled audit asks for, and it asks
the gate's question rather than a narrower one: it exits non-zero unless the
tracked set is clean **and fully read**. An unreadable source has no P0 to report,
so keying the refusal on P0 alone would let a run that read nothing pass looking
green — the exact failure this plane exists to prevent.

## What a scan decides

Every changed path in the audit range is matched against the source's path rules,
in order, and takes the first match:

| Disposition | Meaning | Priority |
| --- | --- | --- |
| `absorbed` | Continuum mirrors this and holds the claim | P0 until re-classified at the new head |
| `must-port` | Continuum owes a port | P0 |
| `superseded` | Continuum replaced it deliberately | P1, advisory |
| `consumer-local` | the source owns it | none — a green no-op |
| `not-applicable` | the mechanism does not apply here | none — a green no-op |
| *(no rule matched)* | nobody has looked at this file | P0 |

A path nobody classified is a question, never a pass. So are a **removed** tracked
path, a **truncated** change list (GitHub returns at most 300 files per compare),
and a **source that could not be read**: unaccounted-for is not the same as clean,
and it is reported separately from drift rather than quietly folded into it.

`absorbed` stays P0 on an advance *by design*. Continuum mirrored a specific
commit; that classification is evidence about that commit, not a standing promise
to keep mirroring whatever comes next. Clearing the finding means an operator
looked at the new commit and moved `audit.current_sha` forward — that is what makes
the advancement be on the record.

### Private sources fail towards "a human has to look"

A private source's advance is **always** unclassified P0, with its repository,
paths, commits and pull requests withheld. Not because the classification is
hard, but because it cannot be made from data this plane is allowed to publish: the
paths are private, so applying a rule to them would disclose them.

The only way to clear it is for the operator who holds the credential to review the
advance and move the audit range in their uncommitted overlay. The checker will not
pretend a private advance is fine by ignoring it.

## What may leave the plane

`assert_public` runs on every artifact before it is written, not as a review step:

- Source patches and commit bodies are dropped at the API parse boundary. The
  checker cannot print a diff it does not hold.
- A private source's repository, paths, commits and pull requests are withheld
  everywhere: the observations document, the drift report, and the issue body.
- Credentials are read from the environment **by variable name** and never taken as
  arguments, so they cannot appear in a process listing or a workflow log.
- Only the committed ledger and artifacts derived from it are published; the
  private overlay is never committed and never uploaded.

## One issue per source, forever

Each finding carries a marker in the issue body:

```
<!-- continuum-parity-drift source=runtime-lab head=7fe4fc955a54 -->
```

The next run matches on `source`, not on the head, so a source that advances twice
before anyone looks still has exactly one open issue — updated, not duplicated.
When a source returns to its audited commit the finding is gone and the issue is
closed; when it drifts again the checker reopens it, because a closed parity issue
that nobody reopens is a drift that has gone quiet.

The title deliberately omits the head. A title that changes on every scan is a
search result nobody can find.

Both states are read, not just the open ones, because the invariant has two halves:

- **Reopen** an issue that somebody closed while its source was still drifting.
  Whatever the person who closed it concluded has not survived contact with the
  ledger, and the ledger is the record.
- **Close** an issue whose tracked source has stopped drifting, with a comment
  saying why. Only a source the ledger still tracks can resolve an issue: a marker
  naming a repository that is no longer tracked is somebody's own note about a
  subject this checker can no longer evaluate, and it is left alone.

`apply` runs the plan as argument lists, never as a shell string, so an issue body
is one argument whatever is in it — and stops at the first failure. Re-running is
safe: an action that already landed plans as `none` next time.

The body is a classification request, and it says so:

> Nothing has been copied. This is a classification request, not a port.

## The promotion gate

`promote` answers one question: may a consumer's pin move to a commit that is
Continuum's own? Approved means the run may assert the exact string
`no unclassified P0 drift` over the **whole** tracked set — which requires:

- no unclassified P0 drift anywhere, and
- every tracked source readable.

The distinction is the point. `clean` is "no P0"; `can_claim_clean` is "no P0 *and*
nothing unaccounted for". A gate that claimed the first while a source was
unreadable would be claiming coverage it does not have. P1 is reported and does not
block, because a supersession is a recorded decision, not an unknown.

The verdict document carries the claim string itself so no caller has to quote a
paraphrase, and it rebuilds the report from its own document before judging it — a
report that understates its own `unclassified_p0` is rejected rather than trusted.

## Credentials

| Source | Variable | Scope |
| --- | --- | --- |
| public | `GITHUB_TOKEN` (default; `--public-token-env` to rename) | read-only, or anonymous |
| private | `CONTINUUM_PARITY_PRIVATE_TOKEN` (default; `--private-token-env`) | read access to that one repository |

Public and private sources never share a credential. A missing private credential
makes that source `unavailable` with the reason recorded — never a silent pass.

## Artifacts

| File | Contents |
| --- | --- |
| `ledger-check.json` | findings against the committed ledger |
| `observations.json` | publishable observations: one per source, redacted |
| `drift-report.json` | every source's verdict, plus `unclassified_p0` |
| `drift-report.md` | the reviewer-facing summary, also appended to `$GITHUB_STEP_SUMMARY` |
| `issue-plan.json` | `create` / `update` / `reopen` / `close` / `none` per source |
| `applied-issues.json` | the commands that carried the plan out |
| `promotion-verdict.json` | the claim string, the blockers, the advisories |

`scan --observations FILE` makes a scan a pure function of two files, so any
reported result can be reproduced afterwards without re-reading a moving source.

Job outputs are prefixed by what wrote them (`drift_p0`, `drift_unavailable`, …)
so two commands sharing a step group cannot overwrite each other's numbers
silently.

## The workflow

`.github/workflows/parity-drift.yml` runs daily and on demand, as three jobs
separated by what they may do:

- **audit** — read-only. Fetches, scans, uploads artifacts. Cannot write to the
  repository. Dispatch `fail_on_drift` to make it exit non-zero whenever the
  tracked set is not clean and fully read; the default leaves drift as a reported
  result.
- **record** — the only job that writes, and all it may write is issues. It applies
  a plan computed from the audit's report by read-only code in another job. It uses
  the ambient token, so its issues are authored by `github-actions[bot]`, which
  `issue-scheduler.yml` refuses to dispatch: an audit finding can never start an
  agent run.
- **gate** — read-only. Judges a pin promotion when dispatched with a `consumer`,
  and fails the run when refused.

Dispatch inputs: `overlay`, `fail_on_drift`, and `consumer` / `from_pin` / `to_pin`
for the gate. The private overlay is not in this repository, so a default run tracks
only the committed public sources.

## When the checker is wrong

It will be, about classifications. The fix is a classification in the ledger, in
the same commit as the reasoning — not a widened ignore list and not an edit to the
checker. If the checker's *mechanics* are wrong (a shape GitHub changed, a priority
that should not block), that is a bug in `src/continuum/parity/`, and it is fixed
with a test that fails first.

## Verification

```
PYTHONPATH=src python3 -m unittest tests.test_parity_ledger tests.test_parity_drift
PYTHONPATH=src python3 -m continuum.parity.cli ledger-check --ledger reference/parity-ledger.json
python3 -m unittest discover -s .github/tests -p 'test_*.py'
```