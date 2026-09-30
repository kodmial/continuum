#!/usr/bin/env bash
#
# Release validation: everything that has to hold of a commit before it may be
# tagged as a Continuum release.
#
# CI proves the same four things on every pull request, and this proves them
# again against the exact commit a release names. The duplication is deliberate
# in one direction only: a release has to be validated by something the operator
# can run against a commit without opening a pull request, and it must validate
# the release commit rather than the head of a branch. `.github/tests/
# test_release_publication.py` fails the build if the literals the two share --
# the actionlint pin and the active-workflow allowlist -- drift apart, because
# those are the two places where "the same check" can quietly become two
# different checks.
#
# The four obligations, and where each is proved:
#
#   1. workflow syntax      -- YAML parse plus actionlint below
#   2. contracts            -- both unittest suites, config-check, the two-toggle
#                              contract, and the consumer-agnostic contract core
#   3. fixtures             -- the shipped consumer repositories, exercised by the
#                              unit suites and validated as workflows here
#   4. entrypoint integrity -- .github/tests/test_release_selection.py, which is
#                              what refuses a graph that reaches outside the
#                              release commit
#
# Usage: .github/scripts/release_validation.sh [repository-root]

set -euo pipefail

root="${1:-.}"
cd "$root"

allowed='^(auto-merge|ci|issue-scheduler|review-gate|review-queue|opencode|opencode-repair|continuum-shadow|consumer|consumer-scheduler|consumer-opencode|consumer-repair|consumer-auto-merge|consumer-review-gate|consumer-child-dispatcher|consumer-child-worker|consumer-child-review|consumer-child-pr-review|release-bun-binary|release|publish-continuum)\.ya?ml$'
actionlint_version="1.7.12"
actionlint_sha256="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"

step() {
  printf '== %s\n' "$1"
}

step "workflow syntax"
ruby -e '
  require "yaml"
  files = Dir[".github/workflows/*.{yml,yaml}"] +
          Dir[".github/actions/*/action.yml"] +
          Dir["fixtures/*/.github/workflows/*.yml"]
  files.sort.each do |file|
    YAML.parse_file(file)
    puts "validated #{file}"
  end
'

step "GitHub Actions semantics"
archive="actionlint_${actionlint_version}_linux_amd64.tar.gz"
url="https://github.com/rhysd/actionlint/releases/download/v${actionlint_version}/${archive}"
scratch="${RUNNER_TEMP:-.}/release-validation"
mkdir -p "$scratch"
curl -fsSL --retry 3 --retry-all-errors "$url" -o "$scratch/$archive"
printf '%s  %s\n' "$actionlint_sha256" "$scratch/$archive" | sha256sum -c -
tar -xzf "$scratch/$archive" -C "$scratch" actionlint
"$scratch/actionlint" -color \
  -ignore 'shellcheck reported issue' \
  -ignore 'property "workflow_sha" is not defined'
"$scratch/actionlint" -color \
  -ignore 'shellcheck reported issue' \
  -ignore 'property "workflow_sha" is not defined' \
  fixtures/consumer-repo/.github/workflows/*.yml

step "active workflow boundary"
bad=0
while IFS= read -r file; do
  base="$(basename "$file")"
  if ! grep -Eq "$allowed" <<<"$base"; then
    echo "::error::Unexpected active workflow: $file"
    bad=1
  fi
done < <(find .github/workflows -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' \) | sort)
test "$bad" -eq 0

step "trust policy and release selection"
test -f .github/scripts/trust_policy.py
python3 -m unittest discover -s .github/tests -p 'test_*.py' -v

step "engine unit tests"
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v

step "repository configuration"
PYTHONPATH=src python3 -m continuum.cli config-check --config .continuum.yml

step "disabled NanoDictate snapshot"
test -f reference/nanodictate-workflows/release.yml
test -f reference/nanodictate-workflows/coderabbit-retry.yml
test -f reference/nanodictate-workflows/auto-merge.yml
test -f reference/nanodictate-workflows/issue-scheduler.yml

step "two-toggle MVP contract"
python3 .github/scripts/test_continuum_config.py
PYTHONPATH=.github/scripts python3 .github/scripts/test_delegation_runtime.py

step "configuration resolves as a consumer would"
python3 .github/scripts/continuum_config.py

step "contract core stays consumer-agnostic"
core='.github/scripts/continuum_config.py'
# `grep -f` has no comment syntax, so '#' lines are stripped before they are
# used as patterns; otherwise a comment would match every commented line.
if grep -Eino -f <(grep -v '^[[:space:]]*#' .github/scripts/contract-forbidden-names.txt) "$core"; then
  echo "::error::Consumer/provider/platform name found in the contract core: $core"
  exit 1
fi
echo "validated $core"

printf '== release validation passed\n'

actionlint_version="1.7.12"
actionlint_sha256="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"

step() {
  printf '== %s\n' "$1"
}

step "workflow syntax"
ruby -e '
  require "yaml"
  files = Dir[".github/workflows/*.{yml,yaml}"] +
          Dir[".github/actions/*/action.yml"] +
          Dir["fixtures/*/.github/workflows/*.yml"]
  files.sort.each do |file|
    YAML.parse_file(file)
    puts "validated #{file}"
  end
'

step "GitHub Actions semantics"
archive="actionlint_${actionlint_version}_linux_amd64.tar.gz"
url="https://github.com/rhysd/actionlint/releases/download/v${actionlint_version}/${archive}"
scratch="${RUNNER_TEMP:-.}/release-validation"
mkdir -p "$scratch"
curl -fsSL --retry 3 --retry-all-errors "$url" -o "$scratch/$archive"
printf '%s  %s\n' "$actionlint_sha256" "$scratch/$archive" | sha256sum -c -
tar -xzf "$scratch/$archive" -C "$scratch" actionlint
"$scratch/actionlint" -color \
  -ignore 'shellcheck reported issue' \
  -ignore 'property "workflow_sha" is not defined'
"$scratch/actionlint" -color \
  -ignore 'shellcheck reported issue' \
  -ignore 'property "workflow_sha" is not defined' \
  fixtures/consumer-repo/.github/workflows/*.yml

step "active workflow boundary"
bad=0
while IFS= read -r file; do
  base="$(basename "$file")"
  if ! grep -Eq "$allowed" <<<"$base"; then
    echo "::error::Unexpected active workflow: $file"
    bad=1
  fi
done < <(find .github/workflows -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' \) | sort)
test "$bad" -eq 0

step "trust policy and release selection"
test -f .github/scripts/trust_policy.py
python3 -m unittest discover -s .github/tests -p 'test_*.py' -v

step "engine unit tests"
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v

step "repository configuration"
PYTHONPATH=src python3 -m continuum.cli config-check --config .continuum.yml

step "disabled NanoDictate snapshot"
test -f reference/nanodictate-workflows/release.yml
test -f reference/nanodictate-workflows/coderabbit-retry.yml
test -f reference/nanodictate-workflows/auto-merge.yml
test -f reference/nanodictate-workflows/issue-scheduler.yml

step "two-toggle MVP contract"
python3 .github/scripts/test_continuum_config.py
PYTHONPATH=.github/scripts python3 .github/scripts/test_delegation_runtime.py

step "configuration resolves as a consumer would"
python3 .github/scripts/continuum_config.py

step "contract core stays consumer-agnostic"
core='.github/scripts/continuum_config.py'
# `grep -f` has no comment syntax, so '#' lines are stripped before they are
# used as patterns; otherwise a comment would match every commented line.
if grep -Eino -f <(grep -v '^[[:space:]]*#' .github/scripts/contract-forbidden-names.txt) "$core"; then
  echo "::error::Consumer/provider/platform name found in the contract core: $core"
  exit 1
fi
echo "validated $core"

printf '== release validation passed\n'
