# Agent instructions

Continuum runs a coding model inside a repository it does not own. Two things
follow from that, and this document describes both: the instructions the model
reads, and how Continuum makes sure the right ones are loaded on every run.

## Two layers, not one

OpenCode loads instructions from the repository it is working in **and** from a
global location, and both apply at once
([rules documentation](https://opencode.ai/docs/rules/)). Continuum uses both
layers, each for something it alone can decide:

| Layer | File | Owner | Holds |
| --- | --- | --- | --- |
| Global | `.github/agents/AGENTS.md` | Continuum | How work is conducted, in every managed repository |
| Project | the repository's own `AGENTS.md` | that repository | What the work must satisfy: build, test, release, layout |

The separation matters because only one of the two can be correct everywhere. A
rule like "run the project's verification before finishing" is true in every
repository, but "run `swift test`" is true in exactly one. Continuum cannot
know a consumer's build commands, and a hard-coded guess is worse than nothing:
it silently runs the wrong check and reports success. So the global layer holds
only universal policy, and every project-specific rule stays where it belongs —
in the repository, where it is reviewed by the people who own that repository.

The canonical file lives at `.github/agents/AGENTS.md` rather than at the
repository root on purpose. At the root it would load as *this* repository's
project instructions too, which is a different thing entirely and would put
Continuum's own policy in the way of any project that ships its own.

## How the global layer reaches the runner

A fresh GitHub runner has no global instructions file, and the file is not part
of the repository being worked on, so it is installed at the start of every run
by `.github/scripts/opencode_instructions.py`:

```
opencode_instructions.py install --source .github/agents/AGENTS.md
```

OpenCode derives its global configuration directory from the environment, and
which variable wins is a property of the installed build rather than of this
repository. So rather than guessing one path, the installer resolves every
distinct candidate the installed OpenCode could be reading and writes to each
of them:

1. `$OPENCODE_CONFIG_DIR`
2. `$XDG_CONFIG_HOME/opencode`
3. `<home>/.config/opencode`, where `<home>` is `$OPENCODE_TEST_HOME`, else
   `$HOME`

Writes are byte-exact and idempotent, and each install publishes the resolved
paths and the payload digest as step outputs. If no path can be resolved at all
the install fails: an agent that runs with no policy does not report an error, it
just behaves differently, which is the failure mode this exists to make
impossible to miss.

### It refuses rather than guesses

Four conditions stop the install instead of proceeding:

- **A foreign file is already there.** The canonical document carries the marker
  `<!-- continuum-global-instructions -->`. A file at the target without it
  belongs to somebody else, and overwriting it would delete another owner's
  configuration while leaving the run under instructions Continuum does not
  control.
- **The target is not a regular file.** A symlink passes an `is_file` check while
  pointing anywhere at all, and writing through it would land outside every
  directory the installer was given.
- **The target is inside the worktree.** The runtime configuration can resolve
  into the repository. That would replace the repository's own `AGENTS.md` and
  every project rule in it, so the install stops. The working directory is
  always protected; `--forbid-root` adds to that list rather than replacing it,
  so forgetting the flag cannot widen the guarantee.
- **The source is empty or unmarked.** Without the marker the installer cannot
  tell its own document from a stranger's, and the ownership rule above would be
  unenforceable.

Because the installer is fail-closed, a `set -e` failure is a Continuum defect
rather than an agent failure to retry. That is why the point-of-use check below
sits **outside** the `set +e` block that wraps the agent in the child workflows.

### The check at the point of use

`install` writes the file; `verify` re-reads it and compares digests. The
workflows call `verify` immediately before each `opencode run`, in the same step:

```
opencode_runtime.py verify --expected-sha256 "$OPENCODE_RUNTIME_SHA256"
opencode_instructions.py verify --source .github/agents/AGENTS.md
```

The same shape as the executable gate, for the same reason. Anything can
happen between install and launch — a step, a container, a cache restore — and
the only thing that establishes the process about to start is the one verified
is the process that starts.

## Where it is wired

The installer runs in every workflow that starts a model, and nowhere else:

| Workflow | Runs the agent for |
| --- | --- |
| `opencode.yml` | bootstrap issues, conflict repair, CI repair |
| `consumer-opencode.yml` | the same three modes for a consumer repository |
| `consumer-child-worker.yml` | delegated implementation in a child repository |
| `consumer-child-review.yml` | delegated review and repair |

`opencode-repair.yml` and `consumer-repair.yml` are control planes: they
schedule and dispatch, and never start a model. That is what keeps this list
finite.

`.github/scripts/continuum_engine.py assert` requires both the script and the
canonical document, so a partial engine checkout fails at the assertion rather
than part way through an agent run with the policy missing. It is a script rather
than a composite action on purpose: GitHub downloads a remote action into its own
directory, so a composite action could only ever see the copy GitHub made of it
and not the checkout the next steps import.

## The guarantees, and their tests

`.github/tests/test_opencode_instructions.py` covers each of the claims above
that could fail silently:

- **Resolution** is asserted against the environment rather than assumed, for
  every precedence combination, including the empty-value case where naive path
  concatenation would put a *relative* global path inside the worktree.
- **Wiring** is discovered by parsing the workflows for launch sites, and the
  discovered set must equal the declared execution paths. A new path that
  forgets the install fails the test; so does a conditional install step, an
  unnamed source, or a `verify` moved inside a failure-tolerant block.
- **Repository preservation** runs the installer inside a real git worktree and
  asserts the worktree is byte-for-byte and status-for-status unchanged, with
  `AGENTS.md`, `CLAUDE.md`, and `.opencode/agent/*.md` present and untouched.
- **The canonical document** is asserted to name no consumer, provider, or
  platform from the same list the contract core is held to, to carry no
  project-specific build or release mechanics, to state every universal rule it
  is supposed to state, and to stay short enough to remain a policy layer rather
  than a second copy of a project document.

The workflow tests parse YAML rather than grepping text, and compare step
positions rather than counting occurrences, because the thing being controlled
is ordering: the install precedes the launch, the verify precedes the launch, and
the launch is not wrapped in a block that would discard the verify's exit status.
