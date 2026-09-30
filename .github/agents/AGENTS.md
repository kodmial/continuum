<!-- continuum-global-instructions -->
# Continuum global instructions

Continuum installs this file into the global OpenCode instructions location
before every agent invocation, so it applies to every Continuum-managed
repository. It holds only policy that is genuinely universal.

**Project-specific rules belong in the repository's own `AGENTS.md`.** OpenCode
loads the global file and the repository file together, so nothing here
suppresses a project file. Where the two overlap on a project detail, the
repository's file wins: this layer decides how the work is conducted, the
repository decides what the work must satisfy.

## Task and scope

- The GitHub issue or pull request request is the task specification. Complete
  every applicable acceptance criterion and keep the change scoped to that task;
  do not introduce unrelated refactors.
- The repository's own `AGENTS.md`, contributing guide, and CI configuration are
  the source of truth for its build, test, and release mechanics. Follow them.
  Never guess a command, never substitute a nearby one, and never skip a check
  the project defines.
- Continue through implementation and the project's own verification until the
  task is complete.

## Honesty about results

- Never claim successful completion when a required check failed, or when a
  required check could not run in the available execution environment. Report the
  exact limitation instead of substituting an unrelated check.
- Report what was verified and what was not, rather than implying full coverage.

## Git lifecycle

- The invoking workflow owns the Git lifecycle. Do not create or switch branches
  and do not open another pull request unless the task explicitly requires it.
  A repair task that explicitly requires commit/push updates only the current
  pull request branch.

## Tests and checks

- New or changed behavior requires focused automated tests.
- Never weaken, skip, disable, or delete an existing test or coverage check to
  make validation pass. CI is the source of truth for the minimum coverage
  threshold.

## Execution environment

- These runs are headless. Never request interactive approval and never wait for
  user input.
- Keep agent-created temporary files and fixtures inside the repository worktree.
  Do not use `/tmp`, `/var/tmp`, the runner home directory, or any other path
  outside it. Remove them before finishing and never commit them.
- If a command is refused because it would read or write an external directory,
  rewrite it to operate entirely inside the worktree and carry on. Do not retry
  the blocked path.

## External facts

- When a change depends on current provider, API, platform, or tooling
  behaviour, verify the relevant current upstream documentation rather than
  relying on remembered behaviour.

## Language

- Code comments, commit messages, pull-request text, and agent-authored
  repository documentation are in English, unless the task explicitly requires
  another language.
