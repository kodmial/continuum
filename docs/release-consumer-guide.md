# Opt-in application releases

Continuum can cut an application release from an exact merged commit: agree a
version, optionally maintain a release pull request, build and sign every
declared target from that one commit, verify the whole asset set, create a draft,
publish once, and run downstream publishers. It is a separate, opt-in layer. It
is **not** installed by the `core` set and never changes an existing consumer's
release pipeline.

## 1. Install the release caller

The caller lives outside the core set:

```sh
bash install.sh /path/to/consumer "$CONTINUUM_REF" release
```

The installed file is `.github/workflows/continuum-release.yml`. It is a thin
caller: the reusable workflow file has the same name, so the installer writes it
verbatim and rewrites its `uses:` and `continuum_ref` pins to `$CONTINUUM_REF`.
Pin `CONTINUUM_REF` to a full audited commit SHA during adoption; `main` moves.

Installing the caller is the only opt-in action Continuum performs. Everything
else is repository configuration.

## 2. Configure the repository

A repository with no `release` block is a verified green no-op: the workflow's
plan job runs, reports `release-disabled`, and starts no build or publish job.
To release, declare either a plan-only target or a hooks document:

```yaml
version: 1
release:
  hooks: .github/release-hooks.json
  # Where the version lives in the released commit. A push-triggered release
  # has no dispatch input, so this is repository configuration rather than a
  # workflow argument. Omit it only when every release is tag-triggered or
  # manually dispatched with `version`.
  version_file: Sources/App/Version.swift
  # Optional. The default matches a semantic version in the file.
  version_pattern: 'static let version = "([0-9]+\.[0-9]+\.[0-9]+)"'
```

`release.targets` in `.continuum.yml` can describe the platforms, architectures,
artifact shapes, and signing identity for planning. It cannot build: product
build and signing commands belong to the consumer, so a real release needs
`release.hooks`. A targets-only release is reported as `config-only` and fails
with a message naming `release.hooks`.

### The hooks document

```json
{
  "schema": "continuum.release-hooks/v1",
  "targets": [
    {
      "id": "macos-arm64",
      "platform": "macos",
      "runner": "macos-14",
      "build": {"command": ["bash", ".github/release/build.sh", "arm64"]},
      "sign": {"command": ["bash", ".github/release/sign.sh"], "identity": "acme-ci-signing"},
      "verify": {"command": ["bash", ".github/release/verify.sh"], "identity": "acme-ci-signing"},
      "artifacts": [
        {
          "name": "widget-{version}-macos-arm64.tar.gz",
          "path": "dist/widget.tar.gz",
          "type": "archive",
          "classifier": "macos-arm64",
          "platform": "macos",
          "arch": "arm64"
        }
      ]
    },
    {
      "id": "windows-x64",
      "platform": "windows",
      "runner": "windows-latest",
      "build": {"command": ["pwsh", ".github/release/build.ps1"]},
      "artifacts": [
        {
          "name": "widget-{version}-windows-x64.zip",
          "path": "dist/widget.zip",
          "type": "archive",
          "classifier": "windows-x64",
          "platform": "windows",
          "arch": "x64"
        }
      ]
    }
  ],
  "release_pr": {"command": ["bash", ".github/release/release-pr.sh"]},
  "publishers": {
    "homebrew": {"command": ["bash", ".github/release/homebrew.sh"]}
  },
  "syncs": {
    "macports": {"command": ["bash", ".github/release/macports.sh"]}
  }
}
```

Rules that are enforced, not merely documented:

- commands are argument vectors, never shell strings;
- `{version}`, `{tag}`, `{target}`, and `{source_sha}` are the only templates;
- a token-looking literal in the document is refused;
- every declared artifact must exist after the build, and its size and digest
  are measured from the bytes;
- after signing the bytes are re-measured, and `verify` must agree with the
  pinned `identity` when one is declared.

`release_pr` is optional. When present it runs once in the preparation job, not
once per target. Publishers run after the GitHub Release is complete, and a tap
or port failure is reported as a sync failure rather than as "the release did
not publish".

## 3. Supply credentials

The reusable workflow accepts `TAP_PAT` as a declared secret. It uses that token
to create the tag and GitHub Release and to let a release-PR push trigger normal
workflows. A `GITHUB_TOKEN` event cannot do that, which is why a classic PAT with
`repo` and `workflow` scopes is the contract.

Signing and publisher credentials are named by `hook_secret_env`, a space- or
comma-separated list of environment variable names:

```yaml
jobs:
  call:
    uses: kodmial/continuum/.github/workflows/continuum-release.yml@<audited-sha>
    secrets: inherit
    with:
      source_sha: "${{ github.sha }}"
      hook_secret_env: "ACME_SIGNING_P12 ACME_SIGNING_PASSWORD"
```

Only the named secrets are copied from the inherited secret set into the job
environment. A named secret that is absent fails the job before a build runs.
Build jobs do not receive `TAP_PAT` unless it is named, and hook commands never
inherit the job's `GITHUB_TOKEN`.

A `release_pr` hook is the one hook that must push a branch. Give it the release
token explicitly instead of relying on the job token:

```yaml
with:
  hook_secret_env: "TAP_PAT"
```

The hook then reads `$TAP_PAT` (or exports it as `GITHUB_TOKEN`/`GH_TOKEN` for
the tool it drives). That push is what makes the release pull request run normal
CI: an event produced by the job's `GITHUB_TOKEN` does not start other
workflows, while the classic PAT does. Keep the hook's own `GITHUB_TOKEN` mapping
inside the consumer script; Continuum never hands the job token to a hook.

## 3a. Require exact-HEAD evidence (optional)

The preparation job can require that the release commit already carries green
checks, a review, or a merged release pull request:

```yaml
with:
  release_pr: "123"
  required_checks: "CI / Validate, Packaging smoke"
  require_review: true
  require_merged_pr: true
```

`required_checks` is comma- or space-separated. A check run that belongs to a
different commit is stale and does not count; a missing, pending, or failed
check is refused. `require_review` accepts only an approval whose `commit_id` is
the release commit, and `require_merged_pr` refuses a release that is not
downstream of a merged pull request. Evidence is enforced in the `prepare` job,
which is the first job holding the release token; the `plan` job stays
token-free and write-free.

## 4. Triggers and the release PR

The installed caller runs on:

- `push` to `main`;
- `push` of a `v*` tag;
- `workflow_dispatch` with an optional exact SHA, version, tag, and event.

A consumer whose default branch is not `main` should edit its installed caller's
`branches:` and pass `default_branch`. A repository that wants releases only on
demand can delete the `push` trigger; the reusable workflow does not decide
triggers for the consumer.

A `push` to `main` carries no dispatch inputs, so it needs a version source the
commit itself carries: `release.version_file` in `.continuum.yml`, or a tag
release. Without either, the plan fails with `version-source-unconfigured`
instead of guessing a version.

When `release_pr` is configured, a merged source change that passes eligibility
runs the release-PR hook once. The hook owns branch creation and the PR; Continuum
owns the version agreement, provenance, and the refusal to build from a stale
head. The release itself is built from the merged source SHA, not from the
release-PR branch.

## 5. What a run does

| job | what it proves |
| --- | --- |
| `plan` | whether the event may release, the agreed version/tag, and the target matrix. Writes nothing and reads no token. |
| `prepare` | eligibility, version, release PR, notes, exact-HEAD evidence, validation, and the source pin. No build. |
| `build` | one matrix leg: exact SHA, build, sign, verify, measured manifest, and a bundle with the target receipt. |
| `publish` | imports every build receipt, requires the whole asset set, creates the draft, uploads, verifies, publishes, then syncs publishers. |

A disabled, docs-only, or duplicate event ends in the plan job as a green no-op.
A duplicate execution cannot upload a second asset set or publish twice: the
receipts and the destination both refuse it.

Exit codes are meaningful to a retry policy:

- `0` — released, prepared, planned, or a verified no-op;
- `1` — fatal, including a missing token, missing secret, wrong signer, or an
  invalid digest;
- `75` — retryable (rate limit, 5xx, unavailable evidence);
- `78` — blocked (tag/source disagreement, stale head, asset conflict) and not
  retryable without a human decision.

## 6. Resume and recovery

Every job writes a receipt. Build legs upload their bundle with the target
receipt; the publish job imports all of them. The publish job also writes a
receipt generation onto the release it describes, so a re-run from another
runner can read what the release already did. A missing artifact, a receipt whose
commit changed, or a receipt whose digest changed is refused before upload rather
than silently shrinking the asset set.

Concurrency is serialized per repository and `cancel-in-progress` is false. A
duplicate event waits for the run that is publishing; it does not cancel it, so
an interrupted upload is never the normal path.

## 7. macOS signing and downstream packages

The historical Apple plan adapter remains in `src/continuum/release/apple.py`
with its own tests: it pins a stable self-signed identity, signs binaries then
the bundle, verifies `Authority=` and the designated requirement, and refuses an
ad-hoc fallback unless explicitly allowed. A consumer adopting the generic
workflow expresses the same contract in its `sign` and `verify` hooks, so the
identity and checksum semantics stay the consumer's own.

Homebrew and MacPorts are downstream. They run as publisher/sync hooks after the
GitHub Release is complete; they never decide whether the release itself
published. Platform-neutral targets mean a Windows build is an ordinary matrix
leg, not a Continuum special case.
