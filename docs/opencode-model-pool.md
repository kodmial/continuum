# Approved OpenCode model pool (Work Lock #66)

The human-approved anonymous free allowlist, ordered by priority:

1. opencode/deepseek-v4.1-flash-free — first primary
2. opencode/glm-5.3-flash-free — second primary
3. opencode/nemotron-3-ultra-free — reserve

The reviewed configuration is in .github/actions/model-select/models.json.
No automatic catalog discovery, new provider route or paid fallback may change
this allowlist; changes require a separate explicit human decision and reviewed PR.

## Runtime selection contract

Each trusted execution restores the SHA-verified official OpenCode binary,
then uses the shared model-select action to do an actual inference against an
approved route BEFORE modifying worktrees or posting review findings.
opencode models lists alone never qualify availability. The selected route
is pinned for the attempt; a failed attempt cannot be blindly replayed after
side effects. Failed candidates are cooled down, and exhaustion fails closed.

Precedence: an explicit OPENCODE_MODEL override pins exactly one already
approved model, with no fallback; otherwise OPENCODE_MODELS configures an
ordered subset of approved models. Both unset => human-reviewed default order.
Invalid, duplicate, unauthorized or non-free IDs are rejected. A retired
legacy Muse Spark override should be removed by its operator rather than
silently substituted.

The dedicated branch continuum-model-state, never main, stores opaque
profile-keyed schema-versioned state: pool fingerprint, synthetic probe time,
last actually completed execution and bounded cooldowns. An optimistic
exact-head, non-force ref update rejects stale writers. Probe subprocesses
use a temporary HOME/XDG with no GitHub state credentials, provider keys or
private Child metadata. Token authority is restricted to existing trusted
caller contexts; untrusted forks cannot write state. Real task success must
be recorded separately after acceptance — never infer it from a probe.

## Version controls and acceptance

The reviewed action manifest defaults OpenCode to 1.19.3; the exact
CONTINUUM_OPENCODE_VERSION repository variable overrides it only when valid.
A representative Parent/Child execution must prove its explicit version and
profile handling before adoption. Version upgrade and rollback must check
actual coder, reviewer and repair compatibility, the binary/version digest,
cache key and all real consumer acceptance gates; never use moving latest.

GitHub cache is not a Render image. Render is a separate runtime boundary
and must be qualified independently. Outstanding acceptance includes
real repeated ephemeral runs with sticky state, cold/warm, invalid version
and model override, corruption/eviction, fork trust boundaries, provider
429/5xx simulation, real coding/tool use and Parent/Child full E2E.

## Live qualification checkpoints (2026-10-19)

- GitHub Actions 39270807290: clean hosted runner, exact official CLI,
  real DeepSeek semantic probe and actual file-editing task; recorded post-task.
- GitHub Actions 39272596838: a TEST-ONLY command shim deliberately returned
  synthetic HTTP 503 for the first approved route. The selector skipped it,
  queried the REAL official GLM 5.3 Flash Free backend, the model completed
  actual file-editing acceptance, and its successful task was stored.
  This proves selection/fallback control flow; it does NOT claim the actual
  DeepSeek upstream returned HTTP 503.
- A following distinct clean runner must read the stored GLM success, then
  qualify its own call before any work. These runs test the isolated canary,
  not yet all production Parent/Child and Render execution paths.
