# OpenCode / PR-Agent hosted-runner toolchain cache (Work Lock #68)

The common cache action shipped with Continuum PR #318. **Work Lock #67** owns production adoption of that action in the shared coder, PR-Agent review/repair and delegated Parent/Child workflows. These workflows now restore the checked runtime on GitHub-hosted Linux runners. This does **not** by itself certify a full real consumer coding/review cycle or standalone provider-native golden images; that live acceptance evidence is tracked in Work Lock #67.

## Scope and execution trust

- `.github/actions/agent-toolchain` is a shared composite action built around the supported `actions/cache` backend. It uses a **fresh GitHub-hosted VM each job**, not a long-lived runner and not a custom golden VM image. It does not provision JIT self-hosted runners. The conceptual `src/continuum/agent_runtime.py` ImageStore is a different backend and is not magically installed on `ubuntu-latest`.
- The action currently qualifies **Linux x64 and Linux arm64**; Windows and macOS must not be advertised as deployed/verified until corresponding platform packages and CI runs succeed.
- No OpenCode auth/config/session, credentials, repository working tree, prompts or private Child identifiers enter the cache. Auth remains job-scoped.
- GitHub's default cache branch-scoping rules apply. Untrusted PRs cannot write the default-branch cache; consumer callers must not override GitHub's low-trust cache-write restrictions. Treat restored archives as untrusted executable material: the action checks version and recorded binary digest, and a cold release download is checked against the GitHub release asset SHA-256. A compromised cache writer that can alter the executable and its local digest is outside a digest-file-only threat model; enforce trusted cache authors and explicit privilege boundaries.
- No provider subscription or always-on host is required. The toolchain does not select models or probe Zen; model discovery remains Work Lock #67 and selection/failover remains #66.

## Configuration

The action's `toolchain.json` is the authoritative pinned manifest for the migrated **GitHub-hosted Linux execution paths** (not for Render workers or unsupported native-image providers). Current audited pins on 2026-10-19: `opencode_version=1.19.3`, `pr_agent_version=0.46.0` for compatibility with the existing PR-Agent engine. Both are exact, vetted SemVer values; `latest` and malformed overrides fail immediately. The PR-Agent **top-level wheel** SHA-256 is pinned in `toolchain.json` to the official PyPI `pr_agent-0.46.0-py3-none-any.whl` SHA-256 (`6a37f507...839f247f5`); an override without a reviewed digest fails closed. This verifies the release wheel, **not the independently resolved transitive dependency graph**, which still needs a full hash-locked dependency manifest before claiming complete supply-chain integrity. The released PR-Agent is pinned independently; upgrading it must pass compatibility tests against the bridge and persistent review-state schema.

Usage in the migrated shared workflow jobs:

```yaml
- uses: kodmial/continuum/.github/actions/agent-toolchain@main
  id: toolchain
  with:
    components: both  # opencode | pr-agent | both
    # opencode-version: 1.19.3
    # pr-agent-version: 0.46.0
- run: opencode --version
```

The action has been wired into `continuum-opencode.yml`, `continuum-pr-agent.yml`, `continuum-pr-agent-repair.yml`, `continuum-consumer-child-worker.yml`, `continuum-consumer-child-review.yml` and `continuum-consumer-child-pr-review.yml`. The PR-Agent reviewer installs both tools in one composite invocation (not two network installs); its repair job and coder use the same OpenCode component. Parent/Child delegated scripts retain exact-version verification, isolated worktrees, and their original failure attribution.

`CONTINUUM_OPENCODE_VERSION` as a repository variable overrides the checked manifest only when set to an exact SemVer; an invalid value fails closed. The PR-Agent version uses the existing `pr_agent_version` input/repository variable; any new version must also have a reviewed wheel digest in the action manifest. The shared action is not a cache for Render workers: Render's provisioning and application runtime use their own infrastructure. Do not set a fake `CONTINUUM_IMAGE_DIGEST` to imitate a cache hit. Linux is the presently qualified cache platform. Unsupported macOS/Windows profiles fail explicitly and need a separately reviewed provider-specific qualification.

The cache key includes manifest epoch, OS, architecture, Python minor version, components and both pinned tool versions. A version bump yields a distinct immutable cache; bump `cache_epoch` deliberately after a corruption or supply-chain incident. Restored binaries are verified before execution. An OpenCode cold path fetches release metadata with the runner-scoped `github.token` (not an anonymous 60-request/IP call) and verifies the release asset digest. The token is neither cached nor logged. A PR-Agent cold path downloads the exact wheel first, verifies its checked-in SHA-256, then installs that wheel and its dependencies. Cache miss or failed integrity validation triggers a deterministic pinned install, not a silent unpinned update. Cached Python environments are tied to Python 3.12. The current PR-Agent environment is isolated from application global Python.

## Live qualification evidence (2026-10-19)

- **True cold**: [GitHub Actions run 39171507453](https://github.com/kodmial/continuum/actions/runs/39171507453) — no cache for key `continuum-toolchains-wl68-1-Linux-X64-py3.12-both-oc1.19.3-pa0.46.0`; verified OpenCode release-asset download and PR-Agent venv install, toolchain ready in approximately 69 seconds. Its post-step encountered a concurrent writer for the same immutable key, not an installation failure.
- **Independent hosted warm**: [run 39171701112](https://github.com/kodmial/continuum/actions/runs/39171701112) — 177 MB archive restored in approximately 3 seconds, both exact CLI versions and SHA-256 stamps checked, no download/reinstall, bootstrap approximately 4 seconds after restoration; `verified_cache_hit=true`.
- These measurements exclude workflow scheduling, VM provisioning, PR-Agent inference and model service latency. Both runs were on a feature-PR cache scope; their results do **not** prove the target consumers have production warm cache yet.


## Compatibility and trust decisions for hosted adoption

- **OpenCode version upgrade is intentional, not implicit:** existing live coder and delegated workflows installed `1.18.34` before this migration; the merged verified hosted-runner action's manifest sets `1.19.3` (unless `CONTINUUM_OPENCODE_VERSION` is explicitly set). The action enforces exact release version and official GitHub asset digest on a cache miss, checks the cached executable's checksum and version on a hit, and rejects malformed values. Evidence for 1.19.3 CLI execution: [cold qualification](https://github.com/kodmial/continuum/actions/runs/39171507453), [warm qualification](https://github.com/kodmial/continuum/actions/runs/39171701112), and [real anonymous GLM 5.3 Flash response](https://github.com/kodmial/continuum/actions/runs/39173704046). **These establish binary compatibility and an actual small inference, not a complete coding/repair regression proof;** Work Lock #67 requires independent real consumer E2E before declaring production rollout fully qualified. If E2E fails, retain/reinstate the known-good version only through an explicit reviewed rollback with an action-supported digest.
- **Mutable trusted Continuum main:** all consumers were intentionally designed to follow the single reviewed Continuum `main` instead of per-repository hardcoded version SHA/tags. Accordingly `uses: kodmial/continuum/.github/actions/agent-toolchain@main` follows the same trust boundary as the existing shared reusable workflows. This makes **review, CI/Contract Gate, repo access controls, audit and PR-only merge mandatory for every change to that action**. It cannot offer the same supply-chain immutability as a commit-pinned action and should not be advertised as such. The checked-out task/Child working tree is never an action source. If the organization changes policy to allow immutable action refs, a separate reviewed configuration/version-policy migration is necessary.
- **Failure attribution:** delegated jobs explicitly supply their Child ID, issue/PR number and token to the bootstrap-failure reporter from their already authorized job inputs/secrets (not a later task step). No Child repository name, credential or token is printed in public logs, and existing issue/PR attribution markers are preserved.

## Remaining live qualification — Work Lock #67

The initial cache was verified in #318 (fresh cold/warm host jobs), and the execution-site migration is being reviewed in #319. The remaining acceptance evidence must separately demonstrate an actual task coder, PR-Agent review/repair and representative Parent/Child consumer; new main-branch cold/warm cache hits after integration; expiry/eviction/corrupt cache/version override and untrusted/fork behavior. No claim that model inference latency or GitHub VM queue time was accelerated follows from binary cache timings. The core automation timeout/runner selection still belongs to each consumer; do not claim macOS/Windows or true custom JIT image support without provider evidence. No result in this document selects production models: Work Lock #67 independently verifies anonymous models, followed by explicit user selection before Work Lock #66.

The existing model default in some workflows can be retired; an independent PR-Agent review must use a separately verified, explicitly passed working free model until Work Lock #66 is authorized. Never weaken exact-HEAD CI/Contract Gate or auto-merge policies during a model outage.
