## What changed

-

## Why

-

## How it was verified

- [ ] `ruby -E UTF-8 scripts/test-continuum.rb`
- [ ] `bash -n install.sh`
- [ ] `actionlint -shellcheck= -pyflakes= .github/workflows/*.yml .github/caller-stubs/*.yml .github/caller-stubs/parent/*.yml`
- [ ] `PYTHONPATH=src python3 -m unittest discover -s tests`
- [ ] Manual check (describe):

## Checklist

- [ ] Branch from `main`, targeting `main`
- [ ] No secrets/keys in the diff
- [ ] Caller templates in `.github/caller-stubs/` and the contract tests in `scripts/test-continuum.rb` updated if a workflow interface changed
- [ ] Workflow `name:` values unchanged (controllers and `workflow_run` triggers match on them)
- [ ] Docs updated if behavior changed

Related issues: Closes #

