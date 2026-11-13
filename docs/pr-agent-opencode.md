# PR-Agent over the OpenCode backend

PR-Agent is Continuum's optional review provider. Review-provider selection
has exactly one repository-level source of truth:
`CONTINUUM_REVIEW_PROVIDER=none|coderabbit|pr-agent`. Selecting
`pr-agent` activates the PR-Agent stack; selecting `coderabbit` leaves the
existing CodeRabbit stack authoritative. Legacy true/false provider switches
are not part of the PR-Agent contract.

## Architecture

PR-Agent itself is provider-neutral: it sends OpenAI-compatible chat
completions through LiteLLM. The first supported backend is OpenCode
Server, reached through a minimal compatibility bridge that runs on the
same runner:

```text
PR-Agent (pinned pip release, on the runner)
  -> POST 127.0.0.1:<bridge_port>/v1/chat/completions (this bridge)
  -> POST /api/session; POST /api/session/:id/agent and /model
  -> POST /api/session/:id/prompt (durable enqueue, not completion)
  -> POST /api/experimental/session/:id/wait (idle barrier)
  -> GET /api/session/:id (terminal outcome must be succeeded)
  -> GET /api/session/:id/message?type=assistant (completed final answer)
  -> configured OpenCode provider/model (default: the free anonymous route)
```

`opencode serve` is **not** OpenAI Chat Completions compatible. OpenCode v2.2.6
uses an authenticated `/api/` surface, independent selection of the read-only
`plan` agent and model, and durable prompt enqueue rather than a synchronous
response. The bridge waits for the v2 idle barrier, then requires the session's
recorded terminal outcome to be `succeeded` and the newest assistant record to
be a completed final answer (`finish="stop"`). An enqueue receipt, a failed or
interrupted turn, an intermediate `finish="tool-calls"` step, or an older text
record can never count as a successful review. A fresh, random job-only
`OPENCODE_PASSWORD` protects the loopback server; `opencode` is its Basic user. The bridge (`.github/scripts/pr_agent_bridge.py`,
standard library only) exposes exactly the OpenAI surface PR-Agent needs —
`/v1/chat/completions`, `/v1/models`, health — and translates each completion
into one isolated OpenCode session: a fresh session per request, deleted
afterwards, so requests never contaminate each other.

## Networking

A Docker action cannot reach a runner-local `127.0.0.1` process, so the
workflow does **not** use the upstream Docker action. It installs the
pinned `pr-agent` PyPI release (default `0.46.0`, the same release the old
Docker pin tracked) directly on the runner, where loopback is shared with
`opencode serve` and the bridge. Both servers bind `127.0.0.1` only; the
bridge exposes just the chat surface, never the full OpenCode control API.
Never use `host.docker.internal` here: it is undocumented on
GitHub-hosted runners.

## Inference hardening (reviewer-only)

PR-Agent must never modify source code, and OpenCode is used here purely
as an inference backend, not as a coding agent. Three layers hold this:

1. Every bridge request runs as the read-only `plan` agent with a
   reviewer-only system prompt. The bridge also creates the session with an
   explicit session-level `Permission.Ruleset` that denies `bash` and `task`,
   and fails closed unless the server returns those rules; session rules are
   evaluated after agent rules, so shell execution and subagent delegation
   stay denied. The bridge never sends a per-request `tools` map.
2. Each request gets a fresh session that is deleted afterwards.
3. The workflow proves the checkout is untouched after the run
   (`git status --porcelain` must be empty and no local commit may exist)
   and fails the run explicitly otherwise.

Two shapes are deliberately **not** used because the free tier rejects
them with `FreeTierError: OpenCode's free tier can only be used from
within OpenCode`: a per-request `tools` override map, and a hardened
server permission config via `OPENCODE_CONFIG_CONTENT`. The session-level
ruleset above uses the versioned v2 session API instead and is verified on
the real server before every prompt. If a future OpenCode release changes
this behavior, the bridge fails closed (HTTP 502 with the upstream status)
instead of silently degrading.

## Configuration

Provider, model, and limit details are configuration, not product policy:

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `CONTINUUM_REVIEW_PROVIDER` | `none` | Select the review stack: `none`, `coderabbit`, or `pr-agent`. |
| `PR_AGENT_API_BASE` | `http://127.0.0.1:<bridge_port>/v1` | OpenAI-compatible backend base. |
| `PR_AGENT_MODEL` | `openai/continuum-review` | LiteLLM routing id (`openai/` prefix selects the bridge path). |
| `PR_AGENT_MAX_TOKENS` | `128000` | Custom-model context cap used by PR-Agent prompt budgeting. |
| `OPENCODE_MODEL` | `opencode/muse-spark-1.3-contributor-free` | Model the bridge infers with. |
| `PR_AGENT_BRIDGE_PORT` | `18000` | Loopback port of the bridge. |
| `PR_AGENT_OPENCODE_PORT` | `4096` | Loopback port of `opencode serve`. |
| `PR_AGENT_VERSION` | `0.46.0` | Pinned `pr-agent` release. |
| `AUTOMATION_PR_AGENT_TIMEOUT_MINUTES` | `60` | Job timeout. |

The same knobs exist as reusable-workflow inputs (`review_provider`,
`api_base`, `model`, `max_tokens`, `opencode_model`, `bridge_port`,
`server_port`, `pr_agent_version`); an empty input falls back to the
repository variable, then to the default. `review_provider` is an explicit
override for controlled calls; ordinary consumers set only
`CONTINUUM_REVIEW_PROVIDER`. Authentication is loopback-only by default and needs nothing;
LiteLLM still requires a key *string* on the OpenAI route, so the workflow
sends the documented `continuum-loopback` placeholder, which the bridge
ignores. No paid-provider secret is ever required, read, or forwarded.

`.pr_agent.toml` ships provider-neutral defaults (routing id, chunking,
coverage footer, reviewer behavior). A repository-local `.pr_agent.toml`
may tune review *behavior*, but provider routing is forced through the
`OPENAI__API_BASE` / `CONFIG__MODEL` environment, which override files.

PR-Agent v0.46.0 treats the whole `[push_outputs]` section as host-only:
every key from a repository-local `.pr_agent.toml` is dropped, because the
sinks can write host files or reach arbitrary hosts. The reusable workflow
therefore supplies `PUSH_OUTPUTS__ENABLE`, `PUSH_OUTPUTS__CHANNELS`, and
`PUSH_OUTPUTS__FILE_PATH` in its own environment, and the
`pr-agent-outputs/continuum.jsonl` file it points at is created before the
run and consumed by the lifecycle parser.

## Review lifecycle

The installed `continuum-pr-agent-recovery.yml` caller wakes on PR events and
again when the repository's `CI` workflow completes for a pull request, then
dispatches the `continuum-pr-agent.yml` caller (which itself exposes only
`workflow_dispatch`). The reusable workflow
admits one exact CI-qualified HEAD, runs upstream PR-Agent v0.46.0 full
`review`, reads the native v0.46.0 persistent finding state for that exact
HEAD, and runs a conditional automatic `improve`: a clean authoritative
review (`safe_to_merge`, zero `key_issues_to_review`, complete full
persistent state for the exact HEAD, no blocking security/tool/coverage
signal) skips the second LLM pass and routes directly to the merge gate,
while a review with actionable findings still runs `improve` for additional
repair suggestions. The result is routed automatically:

```text
CI -> review -> clean? merge gate : improve
   -> actionable items -> PR-Agent repair -> writable PR branch -> CI -> fresh review
   -> no actionable items -> PR-Agent exact-HEAD merge gate
```

The merge gate requires the native persistent state to be a complete full review
for the same HEAD and accepts only `safe_to_merge` with no current findings or
qualifying suggestions. Generic auto-merge is disabled in PR-Agent mode. The
bridge is resolved from the selected Continuum revision; consumers do not copy
Continuum-internal bridge code into their repositories.

## Failure modes (all explicit, never silent green)

- Disabled provider: one explanatory comment, no review.
- Bridge or `opencode serve` unreachable, or upstream error/timeout: the
  step fails with the upstream status; 5xx/429/408 are retryable. The
  preflight protocol check uses the same split: a retryable 429/5xx failure
  is eligible for the bounded retry, while a v1/v2 protocol mismatch fails
  deterministically without spending reviewer budget.
- PR head moved mid-review: explicit stale-head failure; the review is
  valid only for the exact HEAD it evaluated.
- Checkout modified by the run: explicit reviewer-only failure.
- Partial large-PR coverage: PR-Agent chunks large diffs
  (`enable_large_pr_chunking`) and prints a coverage footer; the engine
  (`src/continuum/pr_agent.py`) refuses to approve unknown or partial
  coverage.
- Issue/task compliance, repeated-review finding tracking, and the
  approve/request-changes decision live in `src/continuum/pr_agent.py`
  with deterministic unit tests in `tests/test_pr_agent.py`.
