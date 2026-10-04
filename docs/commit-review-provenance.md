# Commit and review provenance (kodmial/continuum#260)

Every Continuum-owned `git commit` uses the GitHub Actions bot identity:

- name: `github-actions[bot]`;
- email: `41898282+github-actions[bot]@users.noreply.github.com`.

Every workflow-owned commit message carries one stable trailer on its own
line:

```text
Continuum-Component: <component>
```

`<component>` is a lowercase kebab-case producer name (for example
`opencode`, `pr-agent-repair`, `coderabbit-unresolved`, `pr-agent-canary`,
`delegation-worker`, `delegation-review`, `delegation-pr-review`) that
matches the comment-attribution component namespace. Trailers never contain
secrets, tokens, or private `owner/repo` identities. The canonical
definition lives in `src/continuum/commit_provenance.py` and is enforced by
`tests/test_commit_provenance.py` and `scripts/test-continuum.rb`.

## Ownership model

The workflow owns the final commit and sets the bot identity. Agent prompts
forbid the tool from committing, pushing, rebasing, resetting, or opening
pull requests directly ("the workflow owns Git state"); the workflow runs
`git add -A` followed by `git commit` with the bot identity plus the
trailer. No tool commit impersonates the repository owner, and
developer-authored commits are never rewritten merely for appearance.

## Review provenance

- Automation never manufactures a human-looking approval. No workflow,
  script, or engine module calls the pull-request review creation APIs
  (`pulls.createReview`, `reviews.create`, `submitReview`, `gh pr review`).
  Merge gating reads existing review decisions (`review.state`) and
  deterministic check/status gates instead.
- Machine review findings visibly identify the automated reviewer through
  the comment-attribution contract (`continuum-origin` marker plus the
  visible `Continuum` / `Agent Coder` / `Project` header).
- Where `GITHUB_TOKEN` cannot provide the required review/event semantics,
  the safe PAT credential is retained but the visible attribution contract
  still applies.
- Required merge gating relies on deterministic exact-HEAD checks: every
  `createCommitStatus` pins `HEAD_SHA`/`TARGET_SHA` and every `pulls.merge`
  passes an explicit `sha:`, so a stale reviewed HEAD can never be merged.

## Residual PAT usage and the future GitHub App

The following operations intentionally retain `TAP_PAT` because
`GITHUB_TOKEN` semantics cannot provide the required push/merge/dispatch
fan-out or cross-repository access. Each is a candidate for a future
Continuum GitHub App, which would attribute the same operation to an app
actor instead of the repository owner (see [Authenticating with a GitHub
App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/about-authentication-with-a-github-app)):

| Residual operation | Why PAT is retained | Future App attribution |
|---|---|---|
| `continuum-opencode` issue-branch push | Agent branches must trigger CI/review workflows; token pushes do not fan out. | Attribute task-branch publication to the App. |
| `continuum-coderabbit-unresolved` fix push | Fix pushes must trigger CI and status recomputation. | Attribute CodeRabbit fix pushes to the App. |
| `continuum-pr-agent-repair` exact-HEAD push | Repair pushes must trigger CI and exact-HEAD re-review. | Attribute repair pushes to the App. |
| `continuum-pr-agent-canary` disposable-branch pushes | Canary pushes must trigger CI on the canary PR. | Attribute canary pushes to the App. |
| `continuum-auto-merge` / `continuum-pr-agent-auto-merge` squash merges | Merge plus evidence comments share exact-HEAD revalidation that must fan out. | Attribute automated merges to the App, not the owner. |
| GitHub-native main-sync (`update-branch`) | Sync merge commits must trigger CI. | Attribute main-sync requests to the App. |
| Delegated child merge / `update-branch` | Cross-repository access requires PAT; `GITHUB_TOKEN` cannot reach the child. | Attribute delegated merges to a per-child App installation. |

The baseline implementation needs no new consumer secret: only the
pre-existing `TAP_PAT`, the Render key (Render API only), the delegated
child runtime token, and the automatic `github.token` are used.
