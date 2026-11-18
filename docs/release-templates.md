# Release workflow reference templates (historical, inactive)

[English](release-templates.md) | [Русский](release-templates.ru.md)

> [!WARNING]
> **Status: ARCHIVED / HISTORICAL / INACTIVE / UNQUALIFIED.**
>
> Everything on this page describes reference material under
> [`examples/release-templates/`](../examples/release-templates/). It is not a
> supported release feature, not an installer set, and not evidence that a
> release has run on current main. Nothing here adds an active release trigger,
> a publication credential, or a consumer migration. The production-release and
> cutover work remains deferred.

Continuum's current supported contract is the issue-to-PR control plane
described in the [README](../README.md). Release automation is not part of that
supported contract today. This page exists so a future implementation can start
from the historical design instead of rebuilding it from memory or copying one
consumer's application-specific automation.

## What was restored

The examples were recovered from one historical commit:

```text
kodmial/continuum@0bbf57dbb8834d0aaef2cfb83f08ead7da623985
```

Three workflow examples are copies of their historical source bodies with an
ARCHIVED header prepended. The generic workflow keeps the historical job logic
unchanged, but two comment blocks in its body were edited: the privilege-split
description now names the remaining transaction trust gap, and a paragraph
describing that gap was added. The consumer hand-off and Bun example bodies are
unmodified copies of their historical source. The macOS caller, target
configuration, `Info.plist`, and entitlements file are new, product-neutral
examples built against the historical `workflow_call` surface and configuration
schema. No live workflow, engine module, installer rule, or consumer repository
was changed.

## Architecture

The historical design separated one release into stages that can be audited and
stopped independently:

```text
release selection
  -> version (optional Release PR)
  -> exact source commit
  -> resolve the target matrix
  -> per-target build (no publishing credential)
  -> sign
  -> verify artifacts, checksums, and signing identity
  -> transaction: GitHub draft -> publish once
  -> optional downstream publishers (Homebrew / MacPorts)
```

The generic workflow in
[`generic-release-workflow.yml`](../examples/release-templates/generic-release-workflow.yml)
is the orchestration layer. It is `workflow_call`-only: it has no event trigger
of its own, so the consumer's selection workflow decides when it runs. It is
deliberately not a macOS script:

- **resolve** reads the policy and writes the matrix every later job shares. It
  holds no publishing token.
- **target** runs once per matrix row, builds from the pinned `source_sha`, and
  writes one fragment. It holds no publishing token and cannot construct a
  destination.
- **transaction** is the only job with a write token. It merges every fragment,
  refuses an incomplete set, and publishes once behind the caller-selected
  `environment` (the example uses `release`, whose protection rules are the
  caller's responsibility).

The privilege split reduces exposure, but it is not by itself the security
boundary. A source tree being built is untrusted input, so the target job that
runs it is given no publishing credential and no release destination. The
archived workflow grants `contents: read` by default and raises only the
transaction job to `contents: write`. Every secret is declared by name, and each
one is bound to the adapter that reads it.

One gap remains, and it is part of why this material is unqualified. The
transaction job also checks out `source_sha` and runs
`PYTHONPATH=src python3 -m continuum.cli` from that tree with `GITHUB_TOKEN` in
its environment, set from `secrets.RELEASE_TOKEN`. A modified CLI in the source
tree can therefore read the publishing credential, so keeping the token out of
the target jobs does not establish the boundary on its own. A later qualified
design must run every job that holds `RELEASE_TOKEN` from a separately trusted
engine, such as a Continuum checkout pinned to a reviewed ref, instead of
executing the release source tree. Until that engine-distribution decision is
implemented and qualified, the split is only a reduction in exposure.

The historical release core behind that workflow had this stage chain:

```text
eligibility -> version -> release-pr -> validation -> source
            -> build -> sign -> verify -> draft -> publish -> sync
```

`release-pr` was optional: a repository that cuts releases from a tag has no
release pull request. The archived workflow takes `version` and `source_sha`
directly, so it does not itself open a Release PR; a consumer that wants one
must ensure the version is already on the exact source commit before dispatch.

Idempotency was keyed by the release, not by a workflow run. A second dispatch,
a re-run, or another tag watcher therefore could not publish the same version
twice. The transaction concurrency group was deliberately
`cancel-in-progress: false`, because cancelling a publish midway leaves a draft
the next attempt must recognise.

## The four different layers

The historical material uses the word "release" for four separate things. Keeping
them apart is the main thing these templates preserve.

| Layer | What it owns | Where it lives here |
| --- | --- | --- |
| Generic orchestration | Carrying out a selected release event, pinning the source, building the matrix, isolating the token, merging fragments, publishing once | `generic-release-workflow.yml`, `consumer-release-handoff.yml` |
| macOS adapter / target | The typed policy for a macOS product: architectures, bundle identity, signing profile, verification pins, declared artifact formats and publishers | `macos-target.continuum.yml`, with the historical `apple.py` and `config.py` as schema provenance |
| Consumer-specific build scripts | How a particular project invokes SwiftPM, assembles its `.app`, and packages its files | Not included. This is consumer-owned and deliberately not invented here |
| Downstream publishers | Homebrew tap or MacPorts port-tree updates after a GitHub release exists | Declared only as `homebrew` / `macports` in the target policy. No publisher implementation is included |

A consumer-specific build script is not the generic workflow. A downstream
publisher is not a signing adapter. A target policy is not an Actions workflow.

## Template map

| File | Recovered from | Role | Required surface | Current-main gap |
| --- | --- | --- | --- | --- |
| [`generic-release-workflow.yml`](../examples/release-templates/generic-release-workflow.yml) | `.github/workflows/release.yml` at `0bbf57dbb8834d0aaef2cfb83f08ead7da623985` | Generic `workflow_call` release orchestration with a per-target matrix; historical job logic unchanged, body comments edited to state the transaction trust gap | Inputs `version`, `source_sha`; secret `RELEASE_TOKEN`; optional channel, tag, environment, signing and attestation inputs | No `src/continuum/release/` engine, no release CLI, no active `release.yml` |
| [`consumer-release-handoff.yml`](../examples/release-templates/consumer-release-handoff.yml) | `fixtures/consumer-repo/.github/workflows/continuum-release.yml` at `0bbf57dbb8834d0aaef2cfb83f08ead7da623985` | Thin consumer entrypoint that records a hand-off; it intentionally implements no release | Input `source_sha`; no secrets | Not installed by any installer set; not a release implementation |
| [`bun-binary-release-workflow.yml`](../examples/release-templates/bun-binary-release-workflow.yml) | `.github/workflows/release-bun-binary.yml` at `0bbf57dbb8834d0aaef2cfb83f08ead7da623985` | Separate optional Bun standalone-binary example, not the default architecture | Inputs `source_sha`, `package_dir`, `binary_path`, `asset_name`; optional `RELEASE_TOKEN` | No active workflow; Bun toolchain is technology-specific and not part of the generic release path; workflow-level `contents: write` plus checkout's persisted credential expose `github.token` to build code |
| [`macos-release-caller.yml`](../examples/release-templates/macos-release-caller.yml) | New example against the historical `workflow_call` surface | Manual macOS release selection that hands `version` and `source_sha` to the generic workflow | Inputs `version`, `source_sha`; secrets `RELEASE_TOKEN`, `CONTINUUM_APPLE_P12`, `CONTINUUM_APPLE_P12_PASSWORD` | Called workflow, engine, and CLI are all absent on current main |
| [`macos-target.continuum.yml`](../examples/release-templates/macos-target.continuum.yml) | New example against `.continuum.yml` and `config.py` at `0bbf57dbb8834d0aaef2cfb83f08ead7da623985` | Policy/configuration, **not** a workflow: SwiftPM macOS `.app` for `arm64` and `x86_64` | Names the two Apple signing secret names; no secret values | The schema parses, but no runtime consumes release targets on current main |
| [`macos/Info.plist`](../examples/release-templates/macos/Info.plist), [`macos/ExampleApp.entitlements`](../examples/release-templates/macos/ExampleApp.entitlements) | New illustrative inputs | Neutral files referenced by the target policy | None | Placeholders only; replace them with your project's real files |

## The historical macOS example

The target example describes one direct-distribution SwiftPM product:

- **Two architectures:** `arm64` and `x86_64`, with `universal: false`. The
  policy declares two per-architecture products rather than one merged
  universal binary; the consumer build must produce them, because the historical
  adapter did not build either one.
- **One identity:** the executable `ExampleApp` and the bundle `ExampleApp.app`
  both use the placeholder identifier `com.example.app`. The historical adapter
  signs the binaries first, then seals the bundle with the same identity,
  identifier, and entitlements. A different identifier would make macOS treat
  the next release as a different application.
- **Signing:** `self-signed-stable`, the historical first-class profile. A
  purpose-made certificate is imported into a throwaway keychain for one job.
  The plan pins the certificate's exact common name and verifies it after
  signing. This is not Developer ID and does not claim notarization.
- **Reproducibility:** `timestamp: false` by default. A secure timestamp breaks
  byte-identical re-runs and invalidates checksums already recorded by a
  downstream manifest. It is a deliberate choice, not an omission.
- **Fail-closed material:** `allow_adhoc_fallback: false`. A missing certificate
  fails the release instead of silently producing an ad-hoc signature with no
  stable identity.
- **Hardened runtime:** `hardened_runtime: true`. The entitlement set is only
  meaningful under that promise.
- **Artifact formats:** `tar.gz` and `zip` are declared in policy. The
  historical Apple adapter signed and verified binaries and bundles; it did not
  create archives, disk images, installers, or packages for you.
- **Publishers:** `homebrew` and `macports` are declarations. The Apple adapter
  never uploads to a tap or a port tree, and no publisher is included in these
  examples.
- **Versioning:** the generic workflow takes an explicit `version` and an exact
  40-character `source_sha`. The historical bundle plan stamps both
  `CFBundleVersion` and `CFBundleShortVersionString` from the release version
  before sealing. A tag-push event does not verify the pushed tag in the
  historical flow: the workflow passes `tag`, but `cmd_transaction` does not
  forward it to `load_event`, and `version_checks_for` derives `v<version>` from
  the matrix, so the comparison only shows that the matrix agrees with itself.
  Qualification requires an independent tag check, such as reading the tag ref
  and comparing it to the requested version outside that code path. A
  dispatched release supplies the only version and has no tag. The selected
  environment's protection rules, which the caller must configure, can require
  a human approval before publishing, but that approval is a possible gate, not
  an independent version source or a version comparison.

The historical `Info.plist` keys in the example are the minimum an `.app` is
identified by. Replace the placeholder values, keep both version keys present,
and keep the bundle identifier stable across releases. One limitation is worth
stating plainly: the historical Apple plan did **not** read the configured
`app_bundle.info_plist` path. It stamped both version keys into the built
bundle's own `Contents/Info.plist` and then sealed it. `macos/Info.plist` is
therefore an illustrative bundle source, not a path the adapter consumes, and
the plan resolved `<binary>` and `<bundle>.app` under the fixed
`.build/release/` directory rather than a configurable output path.

### Placeholder replacement

Every value below is a placeholder, not a production default.

| Placeholder | Replace with | Constraint |
| --- | --- | --- |
| `ExampleApp` | Your executable/product name | A single file name, not a path |
| `ExampleApp.app` | Your app bundle name | Must end in `.app` |
| `com.example.app` | Your reverse-DNS bundle identifier | Use the same stable identifier for the binary and the bundle; changing it changes the application identity |
| `ExampleApp CI Signing` | The exact common name of your signing certificate | The historical plan pins this exact string; `self-signed-stable` does not use a team ID |
| `macos/ExampleApp.entitlements` | Your entitlements file path | Repository-relative, no `..`, no leading `/` |
| `macos/Info.plist` | Your bundle's source `Info.plist` | Repository-relative placeholder; the historical adapter stamped the built bundle's `Contents/Info.plist`, not this configured path |
| `CONTINUUM_APPLE_P12` | The repository secret holding the base64 PKCS#12 certificate | The historical Apple entrypoint reads this name |
| `CONTINUUM_APPLE_P12_PASSWORD` | The repository secret holding the certificate passphrase | Paired with the P12 secret |
| `0bbf57dbb8834d0aaef2cfb83f08ead7da623985` in the caller's `uses:` | A reviewed Continuum ref **after** the engine and CLI are restored and qualified | Do not point this example at current `main` and call it working |

## Honest platform scope

The historical Apple adapter's primary supported executable scenario was
**macOS SwiftPM**. The following were not automatically qualified, and are not
made qualified by these examples:

- Developer ID signing, notarization, and Gatekeeper distribution;
- App Store distribution and the `ios` / `xcode-archive` combinations;
- `pkg` or `dmg` installers and other platform-specific packaging;
- archive creation for `tar.gz` or `zip`;
- Homebrew tap or MacPorts port-tree publication;
- Android, JVM, or a generic non-Apple build path as a working release.

The historical entrypoint table contained Android, JVM, and generic rows, and it
is the extension point the generic matrix is written against. But the
configuration schema accepted only `adapter: apple`, while the historical
release chain's executable adapter table contained only Android and JVM. The
generic workflow therefore expressed a multi-target architecture that could not
complete an Apple release at that commit. That gap is part of the reason these
files are archived rather than presented as a working feature.

The Bun example is separate and optional. It must not become the default
architecture for a future release implementation.

Its own credential exposure is part of why it is unqualified. The archived Bun
workflow declares `contents: write` at workflow level, and its
`actions/checkout` step does not set `persist-credentials: false`. Checkout
therefore leaves `github.token`, a write-scoped repository token, in the
checked-out `.git/config`. The next steps run `bun install --frozen-lockfile`
and then the consumer build script from that same tree, so source-controlled
code at the release commit can read the token while it builds. Because that same
job also publishes, moving the publish step to a later point in the job does not
help. Disabling checkout credential persistence is not sufficient on its own:
build code can write to `GITHUB_PATH` so a later step resolves a modified `gh`
or another tool, and can leave other state the publish step inherits. A future
Bun release must run publication in a separate trusted job on a clean runner
that receives only verified artifacts, so the build never shares a runner or
environment with a publishing credential.

## What current main is missing

A real release is not one YAML file away. These dependencies are absent or
unqualified today:

1. **No release engine.** Current main still has the release *configuration*
   schema in `src/continuum/config.py`, but it has no `src/continuum/release/`
   package: no contract, state machine, version agreement, release core,
   provenance, GitHub destination, transaction, or release plane.
2. **No release CLI.** Current main has no `src/continuum/cli.py`, and therefore
   no `release resolve`, `release target`, `release transaction`, `release plan`,
   or `release sign` commands. The archived workflow invokes
   `PYTHONPATH=src python3 -m continuum.cli` from the checked-out source tree.
3. **No active release workflows or installer support.** There is no
   `.github/workflows/release.yml` or `release-bun-binary.yml` on current main,
   and `install.sh` has no release set. Nothing under `examples/` is installed.
4. **No walkable Apple release adapter.** The historical Apple module was a
   signing/verification plan. The release chain's `CORE_ADAPTERS` table held
   Android and JVM only, so an Apple target was refused at the target stage with
   "a plan but no release adapter the chain can walk". A plan produces no
   manifest; the transaction would have nothing to merge.
5. **No SwiftPM build or packaging implementation.** The Apple plan signs
   products that already exist. It does not run `swift build`, assemble an
   `.app` from a package, create `tar.gz`/`zip` archives, or build a `pkg`/`dmg`.
6. **No Developer ID, notarization, or App Store path.** The schema names
   `developer-id`, `app-store`, `ios`, and `xcode-archive`, but the historical
   adapter refused them as unimplemented. Schema support is not implementation.
7. **No downstream publisher.** `homebrew` and `macports` are configuration
   values here. Publisher modules existed elsewhere in the historical tree, but
   they were not qualified against current main and are not restored by this
   task.
8. **No engine-distribution decision.** The archived workflow checks out the
   caller's `source_sha` and expects the release CLI under `src/continuum` in
   that tree. Current Continuum consumers install thin callers and keep engine
   code in Continuum; they do not vendor `src/continuum`. A future design must
   decide how a separately trusted, pinned release engine reaches the job before
   the workflow can run, because the transaction executes with `RELEASE_TOKEN`
   in scope.
9. **No current qualification evidence.** Historical tests and historical
   release-core documentation are not live evidence for current main. They also
   do not demonstrate a complete macOS build, sign, package, draft, and publish
   path.

## Prerequisites for real qualification later

A future change should satisfy all of these before any example is called
working:

1. Restore or reimplement the release engine and CLI, with the exact stage and
   idempotency contracts described in the historical
   `docs/release-core-contract.md`.
2. Add a walkable Apple release adapter that builds a SwiftPM product, assembles
   the `.app`, signs and verifies it, emits manifests with digests and signing
   evidence, and can be merged by the transaction.
3. Decide and implement the consumer engine-distribution model for the reusable
   workflow.
4. Add active workflows and installer support only in a reviewed change, with a
   disposable consumer repository as the target, a protected release
   environment, and a disposable test signing certificate. Do not migrate a
   production consumer in the same step.
5. Qualify secret isolation and establish a signing trust boundary: resolve and
   target jobs hold no publishing token; only the transaction does. The Apple
   target job does run the source-tree CLI with `CONTINUUM_APPLE_P12` and
   `CONTINUUM_APPLE_P12_PASSWORD` in its environment, so modified source can
   read the signing key and passphrase. Restricting those secrets to the Apple
   row does not isolate them from the code being built, and a trusted
   transaction engine does not protect the target job. Run the transaction from
   a separately trusted engine, not from the release source tree, because the
   source CLI executes with `RELEASE_TOKEN` in scope. Do not expose signing
   material to a job that executes untrusted source either; require an
   established signing trust boundary, such as a separately trusted engine or
   an isolated signing step that receives only verified artifacts, before
   qualification.
6. Qualify failure and recovery semantics: incomplete fragment sets are refused,
   mid-publish interruption resumes or reports safely, and a duplicate event
   cannot publish twice.
7. Qualify the exact-head chain: selection -> version -> source commit -> build
   -> sign -> verify -> draft -> publish, with checksums, an independent check
   that the repository's pushed tag matches the released version, and an
   independent review of the artifacts.
8. Treat Developer ID, notarization, App Store, installers, and downstream
   publishers as separate qualification work. Do not infer them from a passing
   self-signed macOS build.
9. Qualify the optional Bun example separately, including its credential
   exposure: workflow-level `contents: write` plus checkout's default credential
   persistence leaves `github.token` readable by the source-controlled code that
   `bun install` and the consumer build script run. Disabling credential
   persistence on checkout is not enough, because build code can write to
   `GITHUB_PATH` and make a later step run a modified tool. Publication must
   happen in a separate trusted job on a clean runner that receives only
   verified artifacts, so no build step shares a runner with a publishing
   credential, before calling that path qualified.

Until then, the correct status is **INACTIVE / HISTORICAL / UNQUALIFIED**.

## Safety boundaries

- No file here is under `.github/workflows/` or `.github/caller-stubs/`.
- No installer rule, active workflow, engine module, scheduler, review, merge,
  or consumer repository was changed.
- No active release trigger, tag, upload, publication, or consumer migration is
  added. The example caller's manual dispatch is inert under `examples/`.
- No credential value appears in the examples; only secret names and expressions
  appear.
- No first-consumer product name, bundle identifier, binary name, tap, port
  name, or private path appears in the examples. The historical schema was read
  for shape only, and every example value is a neutral placeholder.
- No functionality is claimed that the historical source did not contain. Where
  the source was incomplete, the gap is named above.

## Provenance

All historical paths below are relative to
`kodmial/continuum@0bbf57dbb8834d0aaef2cfb83f08ead7da623985`.

| Path | Git blob | Use here |
| --- | --- | --- |
| `.github/workflows/release.yml` | `8bee5ff721dd853fd8115b076b62b30ff371f287` | Copied to `generic-release-workflow.yml` |
| `fixtures/consumer-repo/.github/workflows/continuum-release.yml` | `7d82c2a095fbf8e16b2e6caedaeb307a46edb536` | Copied to `consumer-release-handoff.yml` |
| `.github/workflows/release-bun-binary.yml` | `d87bd147aececda2e13dd6233cfe49cbb406742f` | Copied to `bun-binary-release-workflow.yml` |
| `docs/release-core-contract.md` | `15c449711427508782119c542076e2ea3aa959f7` | Source for the stage, idempotency, draft/publish, and permission description above |
| `src/continuum/release/apple.py` | `43560fcb122c76ef4598b94335bb71f5f18c7580` | Source for the macOS signing/verification and version-stamping description |
| `src/continuum/release/entrypoints.py` | `0fea44c0bf88832bb9a3dbdff6d3e9c4f4ff0271` | Source for the runner, adapter, and secret table |
| `src/continuum/release/plan.py` | `3ee7ea3d891cdc73aae908f369606cbecd2404e9` | Source for plan/sealing/verification constraints |
| `src/continuum/config.py` | `5d98dc854ba741dfd3036bbf1a5447accb218679` | Source for the target configuration schema |
| `.continuum.yml` | `315416193ef79936f34bbed48693f0eb10ab4742` | Source for the shape of the target example only; its product-specific values were not copied |

A static contract test,
[`tests/test_release_templates.py`](../tests/test_release_templates.py),
checks the archive status, provenance headers, inertness, the generic
`workflow_call` surface, the macOS caller's input/secret correspondence, and
that the macOS target parses through the current `continuum.config` schema
without first-consumer identifiers. `actionlint` applies to the workflow
templates only; it correctly treats `macos-target.continuum.yml` as a non-workflow
and must not be pointed at that policy file.
