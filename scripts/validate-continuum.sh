#!/usr/bin/env bash
set -euo pipefail

ruby -E UTF-8 scripts/test-continuum.rb

mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'

mkdir -p .opencode-tmp/bin
GOBIN="$PWD/.opencode-tmp/bin" go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12
.opencode-tmp/bin/actionlint -shellcheck= -pyflakes= \
  .github/workflows/*.yml \
  .github/caller-stubs/*.yml \
  .github/caller-stubs/tech/*.yml \
  .github/caller-stubs/parent/*.yml
