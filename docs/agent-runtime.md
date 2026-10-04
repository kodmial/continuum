# Prebuilt immutable agent runtime; zero-idle per-job ephemeral runners

Implements kodmial/continuum#179. Agent execution is fast **without keeping
live runner instances waiting between jobs**. The optimization target is
**warm image/cache, zero warm compute**:

```text
versioned Continuum runtime manifest
        ↓
build immutable golden image/template
        ↓
validate + store image by immutable digest
        ↓
job is queued
        ↓
create a fresh compute instance from the prepared image
        ↓
JIT-register one ephemeral runner for that one job
        ↓
run OpenCode / PR-Agent / delegated worker
        ↓
destroy runner + compute + per-job network attachment
```

This replaces any design that keeps a non-zero pool of already-running idle
instances. There is no step that replenishes a pool of already-running idle
machines; a later job repeats creation from the prepared image with a new
instance.

Reference implementation: `src/continuum/agent_runtime.py` (standard library
only). Deterministic coverage: `tests/test_agent_runtime.py` (20 cases from
the task specification). Contract coverage for the workflow half lives in
`scripts/test-continuum.rb`.

## Zero idle live instances (non-negotiable)

For strict ephemeral profiles:

- no VM/container/host remains running merely to wait for a future job;
- no already-registered runner remains online waiting for a future job;
- idle live capacity is always zero (`min_ready = 0`, `idle_count = 0`);
- every execution instance is created only after real queued demand exists;
- every instance may execute **exactly one job** (`max_uses_per_instance = 1`);
- after that job reaches a terminal state, the runner registration and the
  underlying instance are destroyed;
- cancellation, timeout, controller restart, lost webhook, failed JIT
  registration, or failed job startup must not leave a reusable or
  indefinitely waiting instance behind;
- a periodic external reconciler garbage-collects orphaned resources even if
  the normal teardown path never runs.

`idle_instances: 0` and `max_uses_per_instance: 1` are invariants, not tuning
suggestions: `resolve_profile` rejects any declaration that would turn a
strict profile into a persistent waiting runner.

A provider may later reissue the same public IP from its pool. IP uniqueness
between jobs is **not** required. Continuum must not deliberately preserve a
live compute/network identity between jobs and must not attach a long-lived
egress identity solely to make future jobs reuse it. For per-job ephemeral
egress profiles, jobs are never routed through a project-owned
persistent/shared NAT (or another deliberately pinned egress object) that
survives jobs. Qualification verifies lifecycle, not uniqueness: two
sequential jobs may coincidentally receive the same provider-assigned public
IP and still pass if no compute/network resource was deliberately persisted
or reused.

## Canonical runtime contract

Continuum owns one versioned manifest (`AgentManifest`,
`MANIFEST_SCHEMA_VERSION = 1`) per platform with exact pins:

| Component | Pin |
| --- | --- |
| OpenCode | `1.18.34` |
| PR-Agent | `0.46.0` |
| GitHub Actions runner | `2.329.0` |
| Python bridge deps | `3.12`, standard library only |
| Utilities | `bash curl git gh jq python3` |
| Base image | per `(os, arch)` in `BASE_IMAGES` |
| Probes | `opencode --version`, `pr-agent --version`, `run --version` |

The contract produces Linux, macOS, and Windows manifests from the same
schema — never one universal Linux image. Runner software updates happen by
rebuilding a new image generation, never by mutating an execution instance
in place. Bumping any pin changes `manifest_digest` and therefore the image
generation.

## Two-stage image model

```text
Continuum immutable agent-runtime base
        +
consumer-declared toolchain/profile inputs
        ↓
consumer-scoped derived immutable image/template
```

Continuum owns the manifest schema, pinned generic runtime, image/build
definitions, profile resolver, validation, provisioning/control-plane
implementation, lifecycle/reconciliation logic, and security rules. Each
consumer project owns its provider/account/project resources, image/template
generations, image/cache namespace (`ImageStore` per project), controller
state, concurrency/cost limits, and JIT runner registrations. A consumer
repository contains no hand-written provider lifecycle logic — only a small
runtime profile. There is no centralized multi-tenant live runner pool.

## Caching strategy

Caching accelerates reconstruction of a fresh runner; it never preserves the
runner.

- **Layer A — immutable golden image/template** (primary optimization):
  OpenCode, PR-Agent/bridge where applicable, Actions runner binary, generic
  stable runtime dependencies, and suitable consumer toolchain components are
  preinstalled. Normal job execution never reinstalls them: every install
  site probes the prepared runtime first (`command -v opencode` + exact
  version, `pr-agent --version` + exact pin) and only reconstructs
  deterministically on a validated cache miss.
- **Layer B — provider-native immutable image/snapshot cache**: validated
  generations are stored content-addressed by `image_digest` (resolved
  Continuum ref, normalized profile digest, base-image digest, declared
  toolchain inputs). Rebuild only when the desired digest changes or policy
  requires a base/security refresh. On GitHub-hosted backends the same
  content-addressed bundle is restored from the provider cache; it is the
  image equivalent, not warm compute.
- **Layer C — dependency/build caches** (`DependencyCache`): content-addressed
  dependency material with deterministic immutable/cache-version keys,
  project/repository scoped, secret-free, validated before use. Untrusted
  PR/fork contexts never publish trusted writable cache state. Cache failure
  falls back to deterministic reconstruction.

Never cached across jobs: runner working directory, checkout state, JIT
registration, repository credentials, model/provider session state,
job-specific temporary files, live VM/container state, live network identity.

## Consumer-facing configuration

Thin and declarative — a preset plus overrides:

```yaml
runtime:
  preset: agent-linux        # agent-linux | agent-macos | agent-windows ...
  lifecycle: per-job
  max_uses_per_instance: 1
  idle_instances: 0
  network:
    lifecycle: per-job
  cache:
    golden_image: true
    dependencies: true
```

`resolve_profile` enforces the strict invariants and rejects paid-provider
keys, unknown platforms, and persistent lifecycles. Consumers may select
provider backend (`github-hosted`, `gce`, `ec2`, `azure`, `custom`),
concurrency, timeouts, and cache policy — but cannot turn a strict profile
into a persistent waiting runner. Supported repository variables are listed
in [Consumer contract](consumer-variables.md) under Agent execution.

## Required provisioning path

For each queued job (`EphemeralController.run_next_job`):

1. GitHub queues the job for a Continuum runtime profile (`queue_job`
   creates no compute).
2. The controller resolves target repository, requested profile, pinned
   Continuum ref, and desired immutable image digest, and refuses to
   provision once the profile's `concurrency_limit` live instances already
   exist (queued demand is kept for a later attempt, never dropped).
3. `ensure-image(profile, digest)` verifies the prepared image exists.
4. If absent, it is built and validated automatically in trusted
   infrastructure.
5. The Layer C dependency cache is restored (validated, project/repository
   scoped, trusted entries only); a miss falls back to deterministic
   reconstruction, and a successful job publishes fresh cache state
   (fork contexts never publish trusted state).
6. A **new** compute instance is created from the prepared image.
7. The job-scoped network identity required by the profile is attached.
8. Short-lived GitHub JIT/ephemeral runner configuration is generated.
9. The instance is registered for the target repository/scale set.
10. Runtime/image identity is validated before accepting work.
11. Exactly one job executes.
12. On completion/failure/cancellation the instance and job-scoped network
    attachment are deregistered and destroyed.
13. Provider state is reconciled to verify deletion.

## Autoscaling / control plane

Preferred primitives: GitHub Runner Scale Set Client for custom VM/cloud/
on-prem autoscaling outside Kubernetes, ARC when Kubernetes is the selected
Linux backend, repository-scoped JIT registration for standalone
repositories, organization/runner-group scope where appropriate.
Event-driven demand is primary; scheduled reconciliation is recovery, not
the scheduler. Provider authentication uses OIDC/federated short-lived
credentials where supported. The controller is independent of the disposable
runner: a runner is never the sole authority that deletes itself.

## Hard anti-orphan lifecycle

Every provisioned resource carries durable controller-side lease metadata:
consumer/project id, repository/job/run id, profile digest, provider
instance id, `created_at`, `lease_expires_at`, lifecycle state. Bounded:
provisioning/startup timeout, maximum job/runtime lifetime, teardown
timeout, orphan grace period, global maximum instance age — no state means
"wait indefinitely". The controller provides idempotent delete, retry/backoff
for failed deletion, scheduled orphan sweep (`sweep_orphans`), stale JIT
registration cleanup, cleanup of instances with no valid live job lease,
cleanup after controller restart (`restart` preserves durable leases),
provider tags for discovery without process memory, and external durable
diagnostics. A missed completion event or crashed controller produces delayed
cleanup, not a hanging VM.

## Network lifecycle

For `network.lifecycle = per-job`: no deliberately persistent per-project
public-IP object is attached, no live instance waits to retain network
identity, no persistent/shared egress gateway is used, and the job-scoped
attachment is released/destroyed with the runner behind the generic
contract (`FakeProvider` models reissuable IPs; `NetworkAttachment`
records per-job lifecycle).

## Image rollout and rollback

Build immutable generation N+1, validate it, produce provenance/SBOM where
supported, atomically promote the active image pointer (`promote`), serve
new jobs from N+1 while running N jobs finish normally (no idle VM needs
rotation because no idle VM exists), and garbage-collect old generations
only after rollback retention expires. Rollback repoints at the still-stored
immutable digest; it never repairs a mutable runner.

## Platform requirements

| Profile | Image kind | Compute | Qualification |
| --- | --- | --- | --- |
| Linux | golden image | fresh instance per job, zero idle | deterministic + live Linux job |
| macOS | VM template/snapshot | fresh instance per job, zero idle | NanoDictate macOS fixture |
| Windows | prepared image | fresh instance per job, zero idle | deterministic contract qualification |

Unsupported platforms are never advertised (`canonical_manifest` fails
closed). NanoDictate remains the mandatory macOS + Swift/OpenCode
qualification fixture; no consumer name is hard-coded into generic logic.

## Security

No secrets/tokens are baked into images (validated by
`image_contains_secret`); JIT configuration is injected only at
provisioning time; runners are single-job; untrusted fork code never runs
on privileged pools; writable caches are project-scoped and restored caches
are untrusted input unless provenance is trusted; exact image/profile
identity is validated before registration; runner logs are forwarded
externally before destruction; normal jobs cannot silently install a
different unpinned agent runtime (every reconstruction path re-pins exact
versions and fails closed on mismatch). Core runs only on free anonymous
models and never requires, reads, or forwards a paid provider key.

## Live qualification

`live_qualification_evidence` proves the architecture with real controller
jobs for Linux and the NanoDictate macOS fixture: start at zero live
instances, queue a real job, record profile + image digest, prove a new
provider instance was created after demand, prove OpenCode/PR-Agent was
already present (validated image identity before task execution), prove no
per-run installer ran, prove exactly one job ran, prove the instance and
per-job network attachment were destroyed, prove idle count returned to
zero, run a second job on a different provider instance identity (without
requiring the public IP string to differ), prove no long-lived waiting
runner or persistent egress object exists, and prove orphan reconciliation
with one controlled stale-resource scenario. Record workflow run IDs, image
identity, provider instance identity, lifecycle timestamps, and cleanup
evidence; avoid publishing unnecessary raw network-address data.
