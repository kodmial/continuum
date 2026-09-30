# OpenCode project instructions

- Treat the GitHub issue or pull request request as the task specification. Complete all applicable acceptance criteria and keep changes scoped to that task; do not introduce unrelated refactors.
- Continue through implementation and relevant verification until the task is complete. If a required check cannot run in the available execution environment, report the exact limitation instead of substituting an unrelated check or claiming success.
- The invoking workflow owns the Git lifecycle. Do not create or switch branches or open another pull request unless explicitly instructed. When a repair task explicitly requires commit/push, update only the current PR branch.
- GitHub Actions runs are headless. Never request interactive approval or wait for user input.
- Use `CONTRIBUTING.md` as the source of truth for project build, test, and release mechanics. Before completing any task that changes production code, run `swift build` and `swift run NanoDictateCoreTests`; do not use `swift test`. If either check fails, fix the cause and rerun the checks until they pass. Do not claim successful completion without passing checks. If a required check cannot run in the available environment, report the exact limitation instead of claiming success.
- New or changed behavior must include focused automated tests. Do not weaken or remove existing tests or coverage checks merely to make validation pass. CI is the source of truth for the minimum coverage threshold.
- Ordinary feature and fix tasks must not bump `Sources/NanoDictateCore/Version.swift` or `.release-please-manifest.json`; release automation owns version changes unless the task explicitly concerns release machinery.
- When a change depends on current provider, API, platform, or tooling behavior, verify the relevant current upstream documentation rather than relying on remembered behavior.
- For signing, deployment, TCC, Accessibility, or microphone-specific workflow rules, consult the relevant project documentation only when the task touches those areas.
- Keep agent-created temporary files and test fixtures inside the repository worktree. If temporary storage is needed, use `.opencode-tmp/`, remove it before finishing, and never commit it. Do not use `/tmp`, `/var/tmp`, the runner home directory, or other paths outside the worktree.
- If a command is blocked because it would access an external directory, rewrite it to operate entirely inside the repository and continue; do not retry the blocked path.
- All code comments, commit messages, pull-request text, and agent-authored repository documentation must be in English unless the task explicitly requires another language.
