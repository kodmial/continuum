# The release core contract

The release control plane turns one accepted source change into one immutable
GitHub Release. It is opt-in: a repository with no `release.targets` and no
`release.hooks` in `.continuum.yml` gets a green no-op that reads no token,
builds nothing, and writes nothing.

This document is the contract a new platform, a workflow author, or a consumer's
hooks document is written against. It describes `src/continuum/release/`, not a
particular product.

## Layers

| module | what it is |
| --- | --- |
| `contract.py` | ports, values, and the invariants every adapter must satisfy |
| `state.py` | the chain, its keys, its outcomes, and the journal |
| `version.py` | how one release agrees on one version number |
| `eligibility.py` | whether an event may release, and where the notes come from |
| `evidence.py` | checks and reviews of the exact commit, when required |
| `core.py` | the walk: stage order, failure policy, resume policy |
| `hooks.py` | generic platform-neutral build/sign/verify/publisher commands |
| `targets.py` | planning adapters for `.continuum.yml` targets, and the recorded adapter |
| `github.py` | the GitHub Release destination |
| `github_api.py` | the GitHub API transport and the release-attached receipt store |
| `receipt.py` | the durable record, merge rules, and file/asset stores |
| `cli.py` | the workflow-facing `plan`, `prepare`, `build`, `publish`, `execute` phases |
| `apple.py`, `plan.py`, `run.py`, `adapters.py` | the historical plan adapters, kept tested and separate |

The core holds no adapter, opens no client, and reads no file except through a
component. That is what makes a plan possible for a release nobody has run, and
what makes a new platform an addition rather than a fork of the loop.

## The chain

```
eligibility -> version -> release-pr -> notes -> validation -> source
            -> build -> sign -> verify -> draft -> publish -> sync
```

The order is the argument, and each link is a place a release can be stopped:

- **eligibility** — may this event become a release at all?
- **version** — one number, agreed from independent sources.
- **release-pr** — make sure a release pull request exists and carries it.
- **notes** — the changelog section for the agreed version, not another one.
- **validation** — every target has an adapter, before anything is built.
- **source** — pin the release to one commit.
- **build** — build every target from that commit.
- **sign** — sign what was built, and record who signed it.
- **verify** — check the artifacts against their digests and their pins.
- **draft** — create the release that will hold the assets, unpublished.
- **publish** — publish, but only once the whole asset set verifies.
- **sync** — bring downstream records up to date.

`sign` and `verify` are separate because they are separate claims: a signature
that verifies is not a signature from the identity a release pins, and a release
has to be able to fail *between* them. That is the moment where a wrong signing
identity is still a failed job rather than a published release.

A new destination is a new publisher inside an existing stage, not a new stage.
A stage that is not on the chain has no place to be enforced from.

### Two kinds of nothing-to-do

`Stage.optional` and `Stage.terminal` are not the same flag:

- an **optional, non-terminal** stage is passed through (a repository with no
  release PR has no release-PR hook, and that is still a valid release);
- a **terminal** stage ends the release when it has nothing to do (an
  ineligible event, or a destination that already holds the release).

Either way the run is green. A no-op is a policy decision, not a failure, and it
is recorded with a code and a reason so a reader can tell a switched-off release
from a duplicate.

## Idempotency is a key, not a flag

Every transition computes a key from what it is acting on:

```
continuum-ck-<digest>            # a stage, one target, or one destination
continuum-release/<repo>/<ver>   # the release's identity
continuum-release/<repo>/<chan>  # a concurrency group, not cancel-in-progress
```

The digest form is SHA-256 over the identifying parts, joined with a separator
that cannot appear in a part. A stage already recorded as complete is **not run
again**. The key is scoped to the release, never to a run: a re-dispatch, a
re-run of a failed job, and a second workflow watching the same tag are three
runs and one release.

A stage that did nothing (no-op or block) may be asked again for free.

### What a plan may not do

A dry run writes to the journal like any other pass, so a job can report what it
would do. What it must not do is **count**. `Journal.trusted` is the last
non-plan entry per key:

```python
journal.trusted           # {key: the last entry a real run wrote}
journal.completed_keys    # the keys a later run refuses to repeat
journal.resume_point()    # the stage a resumed run starts from
```

Dropping the last entry rather than the last non-plan entry would let a plan of
an already-published release persuade the next run to publish it again.

## One repository + one version = one commit and one asset set

A receipt carries the release's journal, its manifests, and its identity:

```
(repository, version, tag, source_sha, channel, asset name -> digest)
```

Two receipts may be merged only when all of those agree. A receipt whose source
commit disagrees with the event, or whose artifact digest for a name changed, is
refused before an upload. A published receipt is immutable: assets cannot be
added to or removed from a release that already exists.

The durable stores are deliberately small:

- `FileReceiptStore` writes one JSON document atomically on the runner;
- `ReleaseAssetReceiptStore` appends a generation to the release it describes;
- `TagReceiptStore` adapts the tag-addressed asset store;
- `CompositeReceiptStore` saves to all of them and loads the first that exists.

A receipt is data, not a cache. A missing or malformed receipt is never read as
"nothing has been done"; it is refused, because that would re-open a completed
release.

## The workflow phases

The reusable workflow cannot run one process per release and also build targets
on different runners. The CLI therefore has explicit phases:

| phase | what it records | what it must not do |
| --- | --- | --- |
| `plan` | a dry-run journal and the declared target matrix | write to a destination, read a token, run a hook |
| `prepare` | everything through the source pin | build, sign, publish |
| `build` | one selected target's build/sign/verify, plus a bundle | publish |
| `publish` | the union of the build receipts, then draft/publish/sync | rebuild or re-sign |
| `execute` | the whole chain in one job | — |

`prepare` is what keeps a build matrix honest. Without it, every matrix leg
would run the release-PR stage, so one release could open or update several
release pull requests. `prepare` stops after `source`; its receipt is imported by
every build leg, and each leg records only its own target.

`--target` restricts one job to one declared target. `--matrix` writes the
declared `{id, runner, platform}` rows for `strategy.matrix.include`. A publish
job imports every build receipt with `--receipt-import` and rebases the recorded
artifact paths to the directory it downloaded with `--artifact-dir`. The digest,
size, signature, and verification are still the recorded values; only the path
changes.

## Failure vocabulary

Every failure carries a code and a `retryable` bit. The CLI exit codes are:

| exit | meaning |
| --- | --- |
| `0` | released, planned, prepared, or a verified no-op |
| `1` | fatal; a person has to change something |
| `2` | usage or contract error |
| `75` | retryable: the destination or evidence was temporarily unavailable |
| `78` | blocked: a contradiction (stale head, source conflict, asset conflict) that re-running cannot fix |

Failures are classified by the component that raised them, not by the workflow.
A `403` is fatal, a `429` and a `5xx` are retryable, and a tag that already
points at another commit is blocked.

## What a hooks document may contain

A hooks document is repository-owned JSON validated before anything runs:

```json
{
  "schema": "continuum.release-hooks/v1",
  "targets": [
    {
      "id": "linux-x64",
      "platform": "linux",
      "runner": "ubuntu-latest",
      "build": {"command": ["bash", "scripts/build.sh"]},
      "sign": {"command": ["bash", "scripts/sign.sh"], "identity": "acme-ci"},
      "verify": {"command": ["bash", "scripts/verify.sh"], "identity": "acme-ci"},
      "artifacts": [
        {"name": "widget-{version}-linux-x64.tar.gz",
         "path": "dist/widget.tar.gz", "type": "archive",
         "classifier": "linux-x64"}
      ]
    }
  ],
  "release_pr": {"command": ["bash", "scripts/release-pr.sh"]},
  "publishers": {"homebrew": {"command": ["bash", "scripts/tap.sh"]}},
  "syncs": {"macports": {"command": ["bash", "scripts/port.sh"]}}
}
```

Four properties are enforced rather than documented:

1. **An argument vector, never a shell.** A command is a list. Templates are
   limited to `{version}`, `{tag}`, `{target}`, and `{source_sha}`.
2. **Secrets are references, never values.** The document has no token field,
   and a token-looking literal is refused before anything runs.
3. **The artifact set is measured, not claimed.** Every declared artifact must
   exist; its size and digest are read from the bytes.
4. **Verification is independent of the command.** The verify command may check
   a signature; Continuum re-hashes every artifact and checks the pinned signer.
   A hook that prints `ok` cannot make a wrong digest pass.

A configured `release.targets` entry is enough to plan, but it is not enough to
build: its build and signing commands are the repository's own. A release with
targets but no hooks fails with `hooks-required` instead of inventing a build.
The reusable workflow reports this as `config-only` and fails the plan job with
an explicit message rather than pretending to release.
