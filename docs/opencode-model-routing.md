# OpenCode model routing (#66)

The only authoritative production route policy is trusted repository/environment
variable `OPENCODE_MODELS` on the **executing caller**. Continuum does not embed a
production route list or attempt dynamic model discovery. Set a comma-separated
ordered list; the first healthy route is preferred. The three routes approved
for a particular rollout belong in repository Variables, **not** in code.

- `OPENCODE_MODELS`: ordered, unique, 1–8 provider/model identifiers.
  Invalid/duplicate/empty entries fail closed.
- `OPENCODE_MODEL`: backward-compatible single model **only when no pool exists**.
  It never supersedes an existing pool.
- Trusted explicit per-run model pin: must belong to the configured pool;
  disables failover. Untrusted issue/PR text must never be treated as a pin.
- `OPENCODE_MODEL_STRATEGY=sticky-failover` (only supported strategy),
  `OPENCODE_PROBE_TTL` (default 3600s), `OPENCODE_MODEL_COOLDOWN` (900s),
  `OPENCODE_PROBE_TIMEOUT` (90s). Strict range checks apply.
- Do not infer that a model is free from its name. Consumer policy determines
  credentials/billing permissions; routing never adds unknown paid fallbacks.

Single canonical implementation: `src/continuum/model_routing.py`.
`.github/actions/model-route/action.yml` is only an adapter. Selection probes
the official verified OpenCode binary, with an isolated temporary HOME and
without GitHub/PAT/provider credentials. The review class checks a read-only
bug review; coder/repair checks a real file-writing tool action. A
nonempty HTTP response or `/models` is **not** sufficient. If a usable
selection is confirmed, the action emits a frozen model. Failed actual tasks
are never automatically replayed with a different model after side effects.

A dedicated **orphan** `refs/heads/continuum-model-state` stores JSON per opaque
SHA-256 scope; `main` is not written. A scope includes the target repository,
execution class/profile, credential scope, pool fingerprint and binary digest.
Only an existing contents-write token for the repository chosen by the trusted
caller is used. Private Child state must be written in that Child repository,
never in a public Parent. CAS updates are non-forced fast-forward writes with
bounded retry. Selection issues a per-run-attempt opaque completion receipt
(held outside `main`); semantic TTL is checked **at selection**, and a
real task finishing after that TTL may still be attested within a bounded
six-hour lifetime of its original run-scoped receipt. Completing another
run, changing the model, replaying a consumed receipt or recording without
verified selection all fail closed. Concurrent runs retain separate receipts.
State separates `last_successful_probe` from
`last_successful_real_execution`, with per-route negative cooldowns.

GitHub state-store failures keep permission/auth 403, CAS conflicts and
malformed state deterministic. A 403 with exhausted
`X-RateLimit-Remaining` or a corroborating secondary-limit `Retry-After`, a
429, a transient 5xx, or a transport failure instead publishes only
`CONTINUUM_MODEL_STATE_TRANSIENT` (the sanitized classifier) and a
reset-aware `CONTINUUM_MODEL_STATE_NOT_BEFORE_EPOCH` through `GITHUB_ENV`.
No token, state repository URL, response header or Git object content is
emitted. PR-Agent's post-review attestation consumes that classified signal so
the existing reconciler defers to the reset epoch instead of marking the review
deterministically failed, dispatching early, or cooling down a healthy model.

The existing 429 runner-burn file `$RUNNER_TEMP/continuum-opencode-429` is
respected: on a live 429 the probe marks the VM burned and exits 75 without
testing another route there; orchestration must use the existing durable
recovery/evacuation path. Running the adapter again on the same VM after burn
is disallowed by the caller.

OpenCode versions remain sourced from the verified agent-toolchain manifest,
unless a trusted exact SemVer override is supplied. The state fingerprint binds
the *actual SHA-256* and version; a change starts a fresh qualification epoch.
Roll back by reverting the version override to the previously verified manifest
version and restarting on a new runner; do not copy old success state into a
new epoch.

**Required before production rollout:** prove GitHub Actions (not merely a
connected admin account) can create/read/CAS the orphan ref using the
actual consumer tokens; run two-runner sticky/expired/cooldown/collision checks,
private Child isolation, exact-HEAD coder/full-review/repair E2E, fork/override
and cache failure scenarios, and exact-HEAD protected-baseline Contract Gate.
This documentation does not assert these tests have passed.
