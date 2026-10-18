# OpenCode / PR-Agent hosted-runner toolchain cache (Work Lock #68)

This document describes the **new cache bootstrap component under qualification**. It is not evidence that the primary coder, review, repair, or consumer workflows have already migrated; those integrations and their live acceptance tests remain to be completed before #68 can close.

## Scope and execution trust

- `.github/actions/agent-toolchain` is a shared composite action built around the supported `actions/cache` backend. It uses a **fresh GitHub-hosted VM each job**, not a long-lived runner and not a custom golden VM image. It does not provision JIT self-hosted runners. The conceptual `src/continuum/agent_runtime.py` ImageStore is a different backend and is not magically installed on `ubuntu-latest`.
- The action currently qualifies **Linux x64 and Linux arm64**; Windows and macOS must not be advertised as deployed/verified until corresponding platform packages and CI runs succeed.
- No OpenCode auth/config/session, credentials, repository working tree, prompts or private Child identifiers enter the cache. Auth remains job-scoped.
- GitHub's default cache branch-scoping rules apply. Untrusted PRs cannot write the default-branch cache; consumer callers must not override GitHub's low-trust cache-write restrictions. Treat restored archives as untrusted executable material: the action checks version and recorded binary digest, and a cold release download is checked against the GitHub release asset SHA-256. A compromised cache writer that can alter the executable and its local digest is outside a digest-file-only threat model; enforce trusted cache authors and explicit privilege boundaries.
- No provider subscription or always-on host is required. The toolchain does not select models or probe Zen; model discovery remains Work Lock #67 and selection/failover remains #66.

## Configuration

The action's `toolchain.json` is the **action-local bootstrap manifest** (not yet the authoritative manifest for all existing Continuum jobs). Current audited pins on 2026-10-19: `opencode_version=1.19.3`, `pr_agent_version=0.46.0` for compatibility with the existing PR-Agent engine. Both are exact, vetted SemVer values; `latest` and malformed overrides fail immediately. The PR-Agent **top-level wheel** SHA-256 is pinned in `toolchain.json` to the official PyPI `pr_agent-0.46.0-py3-none-any.whl` SHA-256 (`6a37f507...839f247f5`); an override without a reviewed digest fails closed. This verifies the release wheel, **not the independently resolved transitive dependency graph**, which still needs a full hash-locked dependency manifest before claiming complete supply-chain integrity. The released PR-Agent is pinned independently; upgrading it must pass compatibility tests against the bridge and persistent review-state schema.

Usage once the composite action is published on a trusted Continuum ref:

```yaml
- uses: kodmial/continuum/.github/actions/agent-toolchain@main
  id: toolchain
  with:
    components: both  # opencode | pr-agent | both
    # opencode-version: 1.19.3
    # pr-agent-version: 0.46.0
- run: opencode --version
```

This step is not a drop-in replacement for the existing execution lifecycle until the actual `continuum-opencode.yml`, PR-Agent review/repair/recovery, delegated callers, and applicable Render contracts have been migrated and qualified. Do not set a fake `CONTINUUM_IMAGE_DIGEST` to imitate an image hit.

The cache key includes manifest epoch, OS, architecture, Python minor version, components and both pinned tool versions. A version bump yields a distinct immutable cache; bump `cache_epoch` deliberately after a corruption or supply-chain incident. Restored binaries are verified before execution. An OpenCode cold path fetches release metadata with the runner-scoped `github.token` (not an anonymous 60-request/IP call) and verifies the release asset digest. The token is neither cached nor logged. A PR-Agent cold path downloads the exact wheel first, verifies its checked-in SHA-256, then installs that wheel and its dependencies. Cache miss or failed integrity validation triggers a deterministic pinned install, not a silent unpinned update. Cached Python environments are tied to Python 3.12. The current PR-Agent environment is isolated from application global Python.

## Live qualification evidence (2026-10-19)

- **True cold**: [GitHub Actions run 39171507453](https://github.com/kodmial/continuum/actions/runs/39171507453) — no cache for key `continuum-toolchains-wl68-1-Linux-X64-py3.12-both-oc1.19.3-pa0.46.0`; verified OpenCode release-asset download and PR-Agent venv install, toolchain ready in approximately 69 seconds. Its post-step encountered a concurrent writer for the same immutable key, not an installation failure.
- **Independent hosted warm**: [run 39171701112](https://github.com/kodmial/continuum/actions/runs/39171701112) — 177 MB archive restored in approximately 3 seconds, both exact CLI versions and SHA-256 stamps checked, no download/reinstall, bootstrap approximately 4 seconds after restoration; `verified_cache_hit=true`.
- These measurements exclude workflow scheduling, VM provisioning, PR-Agent inference and model service latency. Both runs were on a feature-PR cache scope; their results do **not** prove the target consumers have production warm cache yet.

## Outstanding qualification

Work Lock #68 cannot close until every relevant real execution site uses one shared pinned/bootstrap contract and production tests cover corruption/eviction/version change/fork safety/concurrent writers, expected teardown/no idle compute, representative consumer coding + PR-Agent review/repair and exact-HEAD Contract Gate, independent review and merge. The existing PR-Agent on default `main` presently uses a retired model and may fail unrelated to cached toolchains; do not bypass its required review or conflate its model failure with cache verification.
