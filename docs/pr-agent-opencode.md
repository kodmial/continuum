# PR-Agent over the OpenCode backend

PR-Agent is Continuum's optional, independently configurable review
provider. It does not replace CodeRabbit: a repository enables either,
both, or neither. Consumers that never enable PR-Agent are unaffected —
the provider stays disabled unless `CONTINUUM_PR_AGENT_ENABLED` is `true`,
and the disabled run posts one explicit notice instead of reviewing.

## Architecture

PR-Agent itself is provider-neutral: it sends OpenAI-compatible chat
completions through LiteLLM. The first supported backend is OpenCode
Server, reached through a minimal compatibility bridge that runs on the
same runner:

```text
PR-Agent (pinned pip release, on the runner)
  -> POST 127.0.0.1:<bridge_port>/v1/chat/completions (this bridge)
  -> POST 127.0.0.1:<server_port>/session + /session/:id/message (opencode serve)
  -> configured OpenCode provider/model (default: the free anonymous route)
```

`opencode serve` is **not** OpenAI Chat Completions compatible: it speaks
a session/message API (`POST /session`, then `POST /session/:id/message`
with `{model, agent, system, parts}`, then `DELETE /session/:id`). The
bridge (`.github/scripts/pr_agent_bridge.py`, standard library only)
exposes exactly the OpenAI surface PR-Agent needs — `/v1/chat/completions`,
`/v1/models`, health — and translates each completion into one isolated
OpenCode session: a fresh session per request, deleted afterwards, so
requests never contaminate each other.

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
as an inference backend, not as a coding agent. Three verified layers hold
this (all proven against the free tier):

1. Every bridge request runs as the read-only `plan` agent with a
   reviewer-only system prompt. The bridge never sends a per-request
   `tools` map.
2. Each request gets a fresh session that is deleted afterwards.
3. The workflow proves the checkout is untouched after the run
   (`git status --porcelain` must be empty and no local commit may exist)
   and fails the run explicitly otherwise.

Two shapes are deliberately **not** used because the free tier rejects
them with `FreeTierError: OpenCode's free tier can only be used from
within OpenCode`: a per-request `tools` override map, and a hardened
server permission config via `OPENCODE_CONFIG_CONTENT`. If a future
OpenCode release changes this behavior, the bridge fails closed (HTTP 502
with the upstream status) instead of silently degrading.

## Configuration

Provider, model, and limit details are configuration, not product policy:

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `CONTINUUM_PR_AGENT_ENABLED` | `false` | Enable the optional PR-Agent provider. |
| `PR_AGENT_API_BASE` | `http://127.0.0.1:<bridge_port>/v1` | OpenAI-compatible backend base. |
| `PR_AGENT_MODEL` | `openai/continuum-review` | LiteLLM routing id (`openai/` prefix selects the bridge path). |
| `PR_AGENT_MAX_TOKENS` | `128000` | Custom-model context cap used by PR-Agent prompt budgeting. |
| `OPENCODE_MODEL` | `opencode/muse-spark-1.3-contributor-free` | Model the bridge infers with. |
| `PR_AGENT_BRIDGE_PORT` | `18000` | Loopback port of the bridge. |
| `PR_AGENT_OPENCODE_PORT` | `4096` | Loopback port of `opencode serve`. |
| `PR_AGENT_VERSION` | `0.46.0` | Pinned `pr-agent` release. |
| `AUTOMATION_PR_AGENT_TIMEOUT_MINUTES` | `60` | Job timeout. |

The same knobs exist as reusable-workflow inputs (`enabled`, `api_base`,
`model`, `max_tokens`, `opencode_model`, `bridge_port`, `server_port`,
`pr_agent_version`); an empty input falls back to the variable, then to the
default. Authentication is loopback-only by default and needs nothing;
LiteLLM still requires a key *string* on the OpenAI route, so the workflow
sends the documented `continuum-loopback` placeholder, which the bridge
ignores. No paid-provider secret is ever required, read, or forwarded.

`.pr_agent.toml` ships provider-neutral defaults (routing id, chunking,
coverage footer, reviewer behavior). A repository-local `.pr_agent.toml`
may tune review *behavior*, but provider routing is forced through the
`OPENAI__API_BASE` / `CONFIG__MODEL` environment, which override files.

## Commands

Comment on a pull request (owner only):

- `/review` — full review of the exact current HEAD: actionable
  correctness/regression/reliability/race/security/performance/
  maintainability/test findings, inline comments where supported, and a
  real upstream `APPROVED` / `CHANGES_REQUESTED` review.
- `/verify <finding-id>` — re-checks one inline finding (its review-comment
  id) against the current file content at the current HEAD through the
  bridge, and replies with machine-detectable `**RESOLVED**` or
  `**UNRESOLVED**` (`UNRESOLVED` always wins ties). A finding already
  verified at the same HEAD is not re-verified.
- `/describe` — PR title/summary/walkthrough.
- `/ask <question>` — question about the PR.

`/improve` is intentionally unsupported: the provider is reviewer-only.

## Failure modes (all explicit, never silent green)

- Disabled provider: one explanatory comment, no review.
- Bridge or `opencode serve` unreachable, or upstream error/timeout: the
  step fails with the upstream status; 5xx/429/408 are retryable.
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
