# Continuum Quick Start

This walkthrough takes a **fresh, isolated test repository** from zero to one
autonomous issue implementation. It deliberately starts with a negative check:
an ordinary issue must not be dispatched before you explicitly admit it.

> **Do not run this against an existing consumer repository.** Use a disposable
> repository with no valuable work. The Create/Use/Actions steps in this document
> were **not executed live** for this repository generation; see
> [Live verification status](#live-verification-status).

## What you will prove

1. The installer writes the core callers and leaves your project-owned `ci.yml`
   alone.
2. An ordinary issue is not dispatched while `AUTOMATION_READY_LABEL` is set.
3. A harmless issue that carries the ready label is dispatched, implemented on a
   branch, opened as a PR, validated by CI, and merged only when the gates pass.

## Before you start

You need:

- a GitHub account and a repository you can administer;
- GitHub Actions enabled;
- a branch to protect, normally `main`;
- a **classic** PAT with `repo` + `workflow` scopes, stored as `TAP_PAT`;
- optionally, the CodeRabbit app or the PR-Agent configuration if you want a
  review provider.

Core uses a free anonymous OpenCode model route. It needs no paid provider key.
Free routes can be rate-limited or unavailable; the walkthrough does not promise
that every implementation or review succeeds.

## 1. Create an isolated test consumer

From a Continuum checkout, set `CONTINUUM_DIR` to its path. Then create the
test repository and copy only the sample project files:

```sh
export CONTINUUM_DIR=/path/to/continuum
export TEST_REPO=<owner>/continuum-quickstart-test

gh repo create "$TEST_REPO" --private --clone
cd continuum-quickstart-test

cp "$CONTINUUM_DIR/examples/minimal-consumer/app.py" .
cp -R "$CONTINUUM_DIR/examples/minimal-consumer/tests" .
mkdir -p .github/workflows
cp "$CONTINUUM_DIR/examples/minimal-consumer/.github/workflows/ci.yml" \
  .github/workflows/ci.yml
```

The example project is intentionally tiny: one Python module and one standard
library test. Its `ci.yml` is project-owned and calls Continuum's reusable
validation engine.

## 2. Inspect and pin Continuum

Choose a reviewed revision. A full commit SHA is best for a reproducible test;
a tag or `main` is acceptable only when you understand that it moves.

```sh
export CONTINUUM_REF="$(git -C "$CONTINUUM_DIR" rev-parse HEAD)"

curl -fsSL \
  "https://raw.githubusercontent.com/kodmial/continuum/${CONTINUUM_REF}/install.sh" \
  -o ../continuum-install.sh
less ../continuum-install.sh
sha256sum ../continuum-install.sh
```

Pin the project-owned CI to the same revision. The installer rewrites the
callers, but it deliberately never edits your `ci.yml`.

```sh
python3 - "$CONTINUUM_REF" <<'PY'
from pathlib import Path
import re
import sys

ref = sys.argv[1]
path = Path(".github/workflows/ci.yml")
text = path.read_text(encoding="utf-8")

text, uses_count = re.subn(
    r"(continuum-validation\.yml@)[^\s\"']+",
    lambda match: match.group(1) + ref,
    text,
)
text, ref_count = re.subn(
    r"(?m)^(\s*continuum_ref:\s*)[^\s#]+",
    lambda match: match.group(1) + ref,
    text,
)

if uses_count != 1 or ref_count != 1:
    raise SystemExit(
        "expected exactly one continuum-validation.yml ref and one "
        f"continuum_ref in {path}; found {uses_count} and {ref_count}"
    )

path.write_text(text, encoding="utf-8")
PY
```

The script fails instead of silently skipping a replacement when the example
`ci.yml` drifts, so CI cannot be left unpinned by an unmatched constant.

You can also create the installer locally from the checkout and pass the ref
explicitly:

```sh
bash "$CONTINUUM_DIR/install.sh" . "$CONTINUUM_REF" core
```

Use the downloaded `../continuum-install.sh` instead if you are not working from
a trusted local checkout.

## 3. Install the core callers

```sh
bash ../continuum-install.sh . "$CONTINUUM_REF" core
rm ../continuum-install.sh
ls .github/workflows/continuum-*.yml
```

Expected result:

- the `continuum-*.yml` core callers are present;
- your own `.github/workflows/ci.yml` is still present and unchanged;
- no second `CI` workflow was created.

The installer never installs into Continuum's own checkout, and on a fresh
repository there is nothing to prune.

## 4. Commit and push the baseline

The new repository is still empty on the remote. GitHub Actions only schedules
workflows that exist on the default branch, and branch protection needs an
existing `main`, so commit the sample project, `ci.yml`, and installed callers,
then push them before opening any issue:

```sh
git add -A
git commit -m "Add Quick Start baseline and Continuum core callers"
git branch -M main
git push -u origin main
```

Confirm the remote now has `main` and the installed workflows:

```sh
git ls-remote --heads origin main
gh workflow list --repo "$TEST_REPO"
```

## 5. Set admission and credentials before opening any issue

```sh
gh variable set AUTOMATION_READY_LABEL \
  --repo "$TEST_REPO" --body "automation:ready"

gh secret set TAP_PAT --repo "$TEST_REPO"
```

When prompted, paste the classic PAT with `repo` + `workflow` scopes. Do not put
the token in a tracked file.

`AUTOMATION_READY_LABEL` is the important safety setting:

- **set** to `automation:ready`: only issues carrying that label are
  automatically implemented;
- **empty** (the historical default): the scheduler may enqueue any eligible
  open issue.

Set it **before the first issue**. If you want to use a different label, set that
value instead; the examples use `automation:ready`.

Optional review provider (only if you have it installed):

```sh
# CodeRabbit
gh variable set CONTINUUM_REVIEW_PROVIDER --repo "$TEST_REPO" --body coderabbit

# Or the optional PR-Agent stack
gh variable set CONTINUUM_REVIEW_PROVIDER --repo "$TEST_REPO" --body pr-agent
```

The default is `none`. See [PR-Agent review](pr-agent-opencode.md) and
[consumer variables](consumer-variables.md) for provider-specific configuration.
No paid provider key is required by core.

## 6. Protect `main`

In the repository settings, add a branch protection rule or ruleset for `main`:

- require a pull request before merging;
- require status checks by the exact names the runs publish. Validation comes
  from the reusable workflow's `Validate` job (plus the `continuum/validation`
  commit-status context); no check run is literally named `CI`. Copy the real
  names from the first pull-request run before saving the rule;
- require branches to be up to date before merging;
- block force pushes and deletion;
- do not grant routine bypass permissions.

Continuum's merge path reads current-HEAD evidence, but server-side protection is
what prevents a human or misconfigured workflow from writing directly to `main`.

## 7. Negative check: an unadmitted issue must not dispatch

Open an ordinary issue with no admission label:

```sh
ISSUE="$(gh issue create \
  --repo "$TEST_REPO" \
  --title "Quick Start negative check: no automation:ready" \
  --body "This issue is a negative admission check. It must not be dispatched." \
  | awk '{print $NF}')"
echo "$ISSUE"
```

The issue-opened event may run the scheduler. That is expected. Check that the
scheduler did **not** dispatch implementation:

```sh
gh issue view "$ISSUE" --repo "$TEST_REPO" --json labels,comments,state
gh run list --repo "$TEST_REPO" --workflow continuum-issue-scheduler.yml --limit 5
gh run list --repo "$TEST_REPO" --workflow continuum-opencode.yml --limit 5
```

Expected:

- the scheduler may show a completed run;
- there is **no** `automation:in-progress` label;
- there is no dispatch comment;
- there is no `OpenCode agent` implementation run.

The exact skip reason is visible in the scheduler run log. The current expected
reason is that the issue is missing the configured ready label.

Close the negative-check issue before continuing:

```sh
gh issue close "$ISSUE" --repo "$TEST_REPO"
```

The same behavior is covered offline by the deterministic fixture in
[`examples/minimal-consumer/verification/`](../examples/minimal-consumer/verification/).
Run it from the Continuum checkout:

```sh
cd "$CONTINUUM_DIR"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s examples/minimal-consumer/verification -v
```

## 8. Positive check: admit one harmless issue

Create the admission label if it does not exist yet, then open a small issue and
explicitly add the label:

```sh
gh label create automation:ready \
  --repo "$TEST_REPO" \
  --description "Admits this issue to automatic implementation by the issue scheduler." \
  --color 0E8A16 || true

ISSUE="$(gh issue create \
  --repo "$TEST_REPO" \
  --title "Quick Start: add a friendly farewell" \
  --body "Add a small, tested farewell helper in app.py, for example farewell(name) returning 'Goodbye, <name>!'. Keep the change minimal and keep the existing tests passing." \
  | awk '{print $NF}')"
echo "$ISSUE"

gh issue edit "$ISSUE" --repo "$TEST_REPO" --add-label automation:ready
```

The label event wakes the scheduler. GitHub can delay scheduled events, so allow
several minutes rather than re-labelling repeatedly.

Expected observable sequence:

1. the scheduler reserves the issue with `automation:in-progress`;
2. a dispatch comment appears;
3. an `OpenCode agent` workflow run starts;
4. the run creates an `opencode/issue<N>-...` branch and opens a PR containing
   `Closes #N`;
5. the project-owned `CI` workflow runs on the PR HEAD;
6. if a review provider is configured, its review path runs; with `none`, no
   external review gate is added;
7. the exact-HEAD merge gate merges the PR only when its configured gates pass;
8. the merged PR closes the issue.

Inspect the result:

```sh
gh issue view "$ISSUE" --repo "$TEST_REPO" --json state,labels,comments
gh pr list --repo "$TEST_REPO" --state all --limit 10
gh run list --repo "$TEST_REPO" --limit 10
```

If the run fails, read the failed step. Common causes are a missing/under-scoped
`TAP_PAT`, a rate-limited free model, insufficient issue detail, branch
protection, or a failing `CI` check. Do not assume a repair or review will always
succeed.

## 9. Clean up

For a disposable test repository:

```sh
gh repo delete "$TEST_REPO" --yes
```

This command needs the `delete_repo` scope, which the default `gh` OAuth token
does not carry; run `gh auth refresh -h github.com -s delete_repo` first, or
delete the repository from the GitHub web UI instead.

At minimum, close any remaining issue and delete leftover `opencode/*` branches.
Do not point this Quick Start at an existing consumer repository.

## Live verification status

This repository generation is **not** a live end-to-end proof for an external
consumer:

- no sandbox repository was created;
- no credentials were configured;
- no GitHub Actions runs were dispatched;
- no consumer repository was modified.

The offline checks that were run are the repository contract suite, Python/
unittest suites, installer syntax, `actionlint` on the workflows and the sample
`ci.yml`, and the deterministic admission fixture. A live Quick Start requires
the owner to separately approve the test repository, its credentials, and its
Actions runs. Until that happens, treat this document as a reproducible
procedure, not a successful live E2E claim.

## Files used by this walkthrough

| File | Role |
| --- | --- |
| [`examples/minimal-consumer/app.py`](../examples/minimal-consumer/app.py) | Sample application code. |
| [`examples/minimal-consumer/tests/test_app.py`](../examples/minimal-consumer/tests/test_app.py) | Sample standard-library test. |
| [`examples/minimal-consumer/.github/workflows/ci.yml`](../examples/minimal-consumer/.github/workflows/ci.yml) | Full project-owned `CI` workflow that calls the reusable validation engine. |
| [`examples/minimal-consumer/verification/test_ready_admission.py`](../examples/minimal-consumer/verification/test_ready_admission.py) | Deterministic negative/positive admission fixture. |
