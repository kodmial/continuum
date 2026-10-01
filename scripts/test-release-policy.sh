#!/usr/bin/env bash
# Test suite for scripts/release-policy.sh — the release policy used by
# .github/workflows/continuum-release-pr.yml to decide whether a merge into main
# needs a Release PR update.
#
# Runs on any bash + git host (CI runs it on ubuntu-latest in .github/workflows/continuum-ci.yml):
#
#   bash scripts/test-release-policy.sh
#
# Exits 0 when every case passes, 1 otherwise. The suite covers the parts of
# the policy that the workflows depend on:
#   * the explicit `skip-release` label (present -> green no-op, absent -> the
#     existing automatic policy applies),
#   * the path-based guard, asserted against REAL `git diff` output from a
#     throwaway repository so a broken grep cannot pass the pure-function cases,
#   * version ownership (only the Release PR branch may touch Version.swift or
#     the release-please manifest), and
#   * the changelog cut that keeps Version.swift and CHANGELOG.md in agreement.
#
# Temporary fixtures live in a per-run directory under .opencode-tmp/ inside the
# worktree (never in /tmp) and are removed on exit.

set -uo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel) || exit 1
# shellcheck source=scripts/release-policy.sh
source "${REPO_ROOT}/scripts/release-policy.sh"

# `mktemp -d` gives every run its own fixture repository, so two concurrent runs
# in the same worktree cannot delete each other's fixtures, and `cleanup` below
# only ever removes what this run created.
mkdir -p "${REPO_ROOT}/.opencode-tmp" || exit 1
TMP_DIR=$(mktemp -d "${REPO_ROOT}/.opencode-tmp/release-policy-tests.XXXXXX") || exit 1
PASSED=0
FAILED=0

cleanup() {
  rm -rf "$TMP_DIR"
  # Leave no trace in the worktree: drop the scratch parent too, but only when
  # nothing else is using it (rmdir refuses a non-empty directory).
  rmdir "${REPO_ROOT}/.opencode-tmp" 2>/dev/null || true
}
trap cleanup EXIT

fail() {
  FAILED=$(( FAILED + 1 ))
  printf 'not ok - %s\n' "$1"
  [[ $# -lt 2 ]] || printf '  %s\n' "$2"
}

pass() {
  PASSED=$(( PASSED + 1 ))
  printf 'ok - %s\n' "$1"
}

# assert_eq <name> <expected> <actual>
assert_eq() {
  if [[ "$2" == "$3" ]]; then
    pass "$1"
  else
    fail "$1" "expected '$2', got '$3'"
  fi
}

# assert_ok <name> <command...> — the command must exit 0
assert_ok() {
  local name=$1
  shift
  if "$@" >/dev/null 2>&1; then pass "$name"; else fail "$name" "command failed: $*"; fi
}

# assert_not_ok <name> <command...> — the command must exit non-zero
assert_not_ok() {
  local name=$1
  shift
  if "$@" >/dev/null 2>&1; then fail "$name" "command unexpectedly succeeded: $*"; else pass "$name"; fi
}

# --- 1. The `skip-release` label is an explicit, exact-match override ------
# A label array as GitHub sends it in `pull_request.labels`.

LABELED='[{"id":1,"name":"skip-release","color":"5319e7","description":"Merge this PR without bumping or publishing a NanoDictate version."}]'
LABELED_WITH_OTHERS='[{"name":"documentation"},{"name":"skip-release"},{"name":"review-ready"}]'
UNLABELED='[{"name":"documentation"},{"name":"chore"}]'
EMPTY='[]'
ABSENT='null'
NEAR_MISS='[{"name":"skip-release-please"}]'
PREFIXED='[{"name":"no-skip-release"}]'
DESCRIPTION_ONLY='[{"name":"docs","description":"use skip-release for release PRs"}]'

assert_ok 'label: skip-release is detected' \
  policy_pr_has_label "$LABELED" skip-release
assert_ok 'label: detected alongside other labels' \
  policy_pr_has_label "$LABELED_WITH_OTHERS" skip-release
assert_not_ok 'label: absent -> no skip' \
  policy_pr_has_label "$UNLABELED" skip-release
assert_not_ok 'label: empty array -> no skip' \
  policy_pr_has_label "$EMPTY" skip-release
assert_not_ok 'label: null payload -> no skip' \
  policy_pr_has_label "$ABSENT" skip-release
assert_not_ok 'label: empty payload -> no skip' \
  policy_pr_has_label '' skip-release
assert_not_ok 'label: skip-release-please is not skip-release' \
  policy_pr_has_label "$NEAR_MISS" skip-release
assert_not_ok 'label: no-skip-release is not skip-release' \
  policy_pr_has_label "$PREFIXED" skip-release
assert_not_ok 'label: a description mentioning skip-release does not skip' \
  policy_pr_has_label "$DESCRIPTION_ONLY" skip-release
assert_not_ok 'label: unrelated labels are not overloaded' \
  policy_pr_has_label "$UNLABELED" skip-release

assert_eq 'label names: one per line' \
  $'bug\ndocumentation' \
  "$(policy_label_names '[{"name":"bug"},{"name":"documentation"}]')"
assert_eq 'label names: an empty array yields nothing' \
  '' "$(policy_label_names '[]')"
assert_eq 'label names: a null payload yields nothing' \
  '' "$(policy_label_names 'null')"

# The runner image ships jq, but the fallback must not rot: a PATH whose jq is
# a directory (so `command -v jq` fails) still has to detect the label, because
# the workflow must not break on a runner without jq.
NOJQ_BIN="${TMP_DIR}/nojq-bin"
mkdir -p "${NOJQ_BIN}/jq"
for tool in bash grep sed; do ln -sf "$(command -v "$tool")" "${NOJQ_BIN}/${tool}"; done
FALLBACK_LABELS=$(PATH="$NOJQ_BIN" bash -c \
  'source "$1"; policy_label_names "$2"' _ "${REPO_ROOT}/scripts/release-policy.sh" "$LABELED")
assert_eq 'label names: jq-less fallback still extracts names' \
  'skip-release' "$FALLBACK_LABELS"
if PATH="$NOJQ_BIN" bash -c \
  'source "$1"; policy_pr_has_label "$2" skip-release' \
  _ "${REPO_ROOT}/scripts/release-policy.sh" "$LABELED"; then
  pass 'label: jq-less fallback detects skip-release'
else
  fail 'label: jq-less fallback detects skip-release'
fi

# --- 2. The label decides BEFORE any path classification ---------------------
# The label must win even when the diff is unambiguously releasable.

RELEASABLE_DIFF=$'Sources/NanoDictateCore/VAD.swift\nSources/NanoDictateCore/Version.swift'
assert_eq 'decide: labeled PR skips even with real code changes' \
  'skip-release' \
  "$(policy_decide "$LABELED" "$RELEASABLE_DIFF" '')"
assert_eq 'decide: unlabeled PR with real code changes is releasable' \
  'releasable' \
  "$(policy_decide "$UNLABELED" "$RELEASABLE_DIFF" '')"
assert_eq 'decide: labeled PR skips before looking at an empty diff' \
  'skip-release' \
  "$(policy_decide "$LABELED" '' '')"
assert_eq 'decide: unlabeled PR with an empty diff is no-changes' \
  'no-changes' \
  "$(policy_decide "$UNLABELED" '' '')"
assert_eq 'decide: null labels fall through to the automatic policy' \
  'releasable' \
  "$(policy_decide "$ABSENT" "$RELEASABLE_DIFF" '')"

# --- 3. Path-based exclusions still work without the label -------------------

assert_eq 'paths: only excluded paths are non-releasable' \
  'non-releasable' \
  "$(policy_decide "$EMPTY" $'CHANGELOG.md\nREADME.md\ndocs/packaging/homebrew.md\n.github/workflows/continuum-ci.yml\n.githooks/pre-commit\npackaging/homebrew/nanodictate.rb\nnanodictate.rb\nconfig.example.toml\nSECURITY.md\nCODE_OF_CONDUCT.md\nCONTRIBUTING.md\nLICENSE\n.gitignore\n.swift-format\n.swiftlint.yml\n.coderabbit.yaml\nrelease-please-config.json\n.release-please-manifest.json\nSources/NanoDictateCore/Version.swift' '')"
assert_eq 'paths: a single source change is releasable' \
  'releasable' \
  "$(policy_decide "$EMPTY" 'Sources/NanoDictateCore/Transcriber.swift' '')"
assert_eq 'paths: a source change among excluded files is still releasable' \
  'releasable' \
  "$(policy_decide "$EMPTY" $'CHANGELOG.md\nscripts/release-prep.rb\nREADME.md' '')"
assert_eq 'paths: the new policy script itself counts as a code change' \
  'releasable' \
  "$(policy_decide "$EMPTY" $'scripts/release-policy.sh\nscripts/test-release-policy.sh' '')"
assert_eq 'paths: .github is excluded, not a prefix-free match' \
  'non-releasable' \
  "$(policy_decide "$EMPTY" '.github/workflows/continuum-release-pr.yml' '')"
assert_eq 'paths: an unrelated nested file is not excluded' \
  'releasable' \
  "$(policy_decide "$EMPTY" 'Sources/NanoDictateCoreSupport/Helper.swift' '')"
assert_eq 'paths: whitespace-only diff list is no-changes' \
  'no-changes' \
  "$(policy_decide "$EMPTY" $'  \n' '')"

# The macports installer: a PIN_REV-only edit is generated, not a user change.
assert_eq 'installer: PIN_REV-only edit is non-releasable' \
  'non-releasable' \
  "$(policy_decide "$EMPTY" 'scripts/install-macports.sh' '')"
assert_eq 'installer: a real installer edit is releasable' \
  'releasable' \
  "$(policy_decide "$EMPTY" 'scripts/install-macports.sh' $'+-  local url="https://example.com"')"
assert_eq 'installer: PIN_REV-only plus a source change is releasable' \
  'releasable' \
  "$(policy_decide "$EMPTY" $'scripts/install-macports.sh\nSources/NanoDictateCore/Transcriber.swift' '')"

PIN_REV_ONLY_DIFF=$'--- a/scripts/install-macports.sh\n+++ b/scripts/install-macports.sh\n-PIN_REV=old\n+PIN_REV=new'
assert_eq 'installer: PIN_REV lines are filtered out of the diff' \
  '' \
  "$(printf '%s\n' "$PIN_REV_ONLY_DIFF" | policy_installer_diff_wo_pinrev)"
assert_eq 'installer: a real line survives the PIN_REV filter' \
  '-OLD_URL=x' \
  "$(printf '%s\n' $'--- a/scripts/install-macports.sh\n+++ b/scripts/install-macports.sh\n@@ -1 +1 @@\n-PIN_REV=old\n+PIN_REV=new\n-OLD_URL=x' | policy_installer_diff_wo_pinrev)"

# Hunk-aware filtering: diff/hunk headers carry no content, but a content line
# that itself starts with `++`/`--` is a real change and must survive.
assert_eq 'installer: diff and hunk headers are not mistaken for changes' \
  $'-OLD_URL=x\n-#-- a separator\n++ a leading plus' \
  "$(printf '%s\n' $'diff --git a/scripts/install-macports.sh b/scripts/install-macports.sh\nindex 1234567..89abcde 100755\n--- a/scripts/install-macports.sh\n+++ b/scripts/install-macports.sh\n@@ -1,2 +1,2 @@\n-OLD_URL=x\n-#-- a separator\n+PIN_REV=new\n++ a leading plus' | policy_installer_diff_wo_pinrev)"
assert_eq 'installer: a file-mode change counts as a real installer edit' \
  $'old mode 100644\nnew mode 100755' \
  "$(printf '%s\n' $'diff --git a/scripts/install-macports.sh b/scripts/install-macports.sh\nold mode 100644\nnew mode 100755' | policy_installer_diff_wo_pinrev)"
assert_eq 'installer: a mode-only change makes the installer releasable' \
  'releasable' \
  "$(policy_decide "$EMPTY" 'scripts/install-macports.sh' "$(printf '%s\n' $'old mode 100644\nnew mode 100755' | policy_installer_diff_wo_pinrev)")"

# --- 4. Version policy -------------------------------------------------------

assert_ok 'version: 0.0.1 is allowed' policy_version_is_allowed '0.0.1'
assert_ok 'version: 0.1.12 is allowed' policy_version_is_allowed '0.1.12'
assert_not_ok 'version: 1.0.0 is a policy error' policy_version_is_allowed '1.0.0'
assert_not_ok 'version: 0.2.0 is a policy error' policy_version_is_allowed '0.2.0'
assert_not_ok 'version: 0.0.08 (leading zero) is a policy error' policy_version_is_allowed '0.0.08'
assert_not_ok 'version: an empty version is a policy error' policy_version_is_allowed ''
assert_not_ok 'version: a non-semver string is a policy error' policy_version_is_allowed 'main'

assert_not_ok 'bump: 0.0.99 is below the cap' policy_automatic_bump_blocked '0.0.99'
assert_ok 'bump: 0.0.100 is at the 0.0.x cap' policy_automatic_bump_blocked '0.0.100'
assert_not_ok 'bump: 0.1.100 is not capped (0.1.x may grow)' policy_automatic_bump_blocked '0.1.100'
assert_not_ok 'bump: 0.1.101 is not capped' policy_automatic_bump_blocked '0.1.101'

assert_eq 'next version: 0.0.8 -> 0.0.9 (no octal)' \
  '0.0.9' "$(policy_next_version '0.0.8')"
assert_eq 'next version: 0.0.9 -> 0.0.10' \
  '0.0.10' "$(policy_next_version '0.0.9')"
assert_eq 'next version: 0.1.12 keeps its minor series' \
  '0.1.13' "$(policy_next_version '0.1.12')"

# --- 4a. Version ownership ---------------------------------------------------
# Only the automated Release PR branch may touch Version.swift or the
# release-please manifest. Ordinary PRs never reserve the next version, so
# concurrent merges cannot race or pick duplicate versions.

RELEASE_BRANCH='release-please--branches--main'

assert_ok 'ownership: the Release PR branch is recognised' \
  policy_is_release_branch "$RELEASE_BRANCH"
assert_not_ok 'ownership: a feature branch is not the Release PR branch' \
  policy_is_release_branch 'feature/my-change'
assert_not_ok 'ownership: an empty branch is not the Release PR branch' \
  policy_is_release_branch ''
assert_not_ok 'ownership: a prefix match is not the Release PR branch' \
  policy_is_release_branch 'release-please--branches--main-extra'

assert_eq 'ownership: Version.swift is version-owned' \
  'Sources/NanoDictateCore/Version.swift' \
  "$(policy_version_owned_touched 'Sources/NanoDictateCore/Version.swift')"
assert_eq 'ownership: the manifest is version-owned' \
  '.release-please-manifest.json' \
  "$(policy_version_owned_touched '.release-please-manifest.json')"
assert_eq 'ownership: a mix keeps only the version-owned paths' \
  $'Sources/NanoDictateCore/Version.swift\n.release-please-manifest.json' \
  "$(policy_version_owned_touched $'Sources/NanoDictateCore/Transcriber.swift\nSources/NanoDictateCore/Version.swift\n.release-please-manifest.json')"
assert_eq 'ownership: source-only changes touch nothing owned' \
  '' "$(policy_version_owned_touched 'Sources/NanoDictateCore/Transcriber.swift')"
assert_eq 'ownership: CHANGELOG.md is not version-owned (notes stay editable)' \
  '' "$(policy_version_owned_touched 'CHANGELOG.md')"
assert_eq 'ownership: an empty diff touches nothing owned' \
  '' "$(policy_version_owned_touched '')"

assert_eq 'ownership: the Release PR branch may touch Version.swift' \
  'ok' "$(policy_check_version_ownership "$RELEASE_BRANCH" 'Sources/NanoDictateCore/Version.swift')"
assert_eq 'ownership: the Release PR branch may touch the manifest' \
  'ok' "$(policy_check_version_ownership "$RELEASE_BRANCH" '.release-please-manifest.json')"
assert_eq 'ownership: an ordinary PR with source changes is allowed' \
  'ok' "$(policy_check_version_ownership 'feature/my-change' 'Sources/NanoDictateCore/Transcriber.swift')"
assert_eq 'ownership: an ordinary PR with changelog notes is allowed' \
  'ok' "$(policy_check_version_ownership 'feature/my-change' $'Sources/NanoDictateCore/Transcriber.swift\nCHANGELOG.md')"
assert_ok 'ownership: the Release PR branch check exits 0' \
  policy_check_version_ownership "$RELEASE_BRANCH" 'Sources/NanoDictateCore/Version.swift'
assert_not_ok 'ownership: an ordinary PR touching Version.swift fails' \
  policy_check_version_ownership 'feature/my-change' 'Sources/NanoDictateCore/Version.swift'
assert_not_ok 'ownership: an ordinary PR touching the manifest fails' \
  policy_check_version_ownership 'feature/my-change' '.release-please-manifest.json'
assert_not_ok 'ownership: a version bump hidden among source changes still fails' \
  policy_check_version_ownership 'feature/my-change' $'Sources/NanoDictateCore/Transcriber.swift\nSources/NanoDictateCore/Version.swift'
assert_eq 'ownership: the violation names the decision' \
  'version-ownership' \
  "$(policy_check_version_ownership 'feature/my-change' 'Sources/NanoDictateCore/Version.swift' || true)"

# --- 4a1. One-time release-please bootstrap seeding ---------------------------
# The PR that introduces the Release PR flow touches both version-owned files
# without reserving a new version (manifest seeded at the current Version.swift
# version, Version.swift annotation-only). The version-ownership gate must allow
# exactly that seeding, and nothing else.

BASE_SWIFT='public enum NanoDictateVersion {
  public static let string = "0.1.4"
}'
HEAD_SWIFT='public enum NanoDictateVersion {
  public static let string = "0.1.4" // x-release-please-version
}'
HEAD_MANIFEST=$'{\n  ".": "0.1.4"\n}'

assert_eq 'seeding: extracts the Version.swift triple' \
  '0.1.4' "$(policy_extract_version "$HEAD_SWIFT")"
assert_eq 'seeding: extracts the manifest triple' \
  '0.1.4' "$(policy_extract_version "$HEAD_MANIFEST")"
assert_eq 'seeding: empty content extracts nothing' \
  '' "$(policy_extract_version '')"
assert_ok 'seeding: the bootstrap (new manifest at current version, swift unchanged) is allowed' \
  policy_is_version_seeding "$BASE_SWIFT" "$HEAD_SWIFT" '' "$HEAD_MANIFEST"
assert_not_ok 'seeding: an existing manifest is not a seeding (strict gate resumes)' \
  policy_is_version_seeding "$BASE_SWIFT" "$HEAD_SWIFT" "$HEAD_MANIFEST" "$HEAD_MANIFEST"
assert_not_ok 'seeding: a Version.swift bump is not a seeding' \
  policy_is_version_seeding "$BASE_SWIFT" "${HEAD_SWIFT/0.1.4/0.1.5}" '' "$HEAD_MANIFEST"
assert_not_ok 'seeding: a manifest seeded at the wrong version is not a seeding' \
  policy_is_version_seeding "$BASE_SWIFT" "$HEAD_SWIFT" '' $'{\n  ".": "0.1.5"\n}'
assert_not_ok 'seeding: a missing head manifest is not a seeding' \
  policy_is_version_seeding "$BASE_SWIFT" "$HEAD_SWIFT" '' ''
assert_not_ok 'seeding: a missing head version is not a seeding' \
  policy_is_version_seeding "$BASE_SWIFT" 'no version here' '' "$HEAD_MANIFEST"

# --- 4b. The changelog cut a bump has to make ---------------------------------
# The invariant VersionTests.testVersionStringEqualsCurrentRelease enforces on
# macOS: Version.swift equals the first `## [<semver>]` header after
# `## [Unreleased]`. A bump that touches only Version.swift breaks it, so the cut
# below is what keeps a green bump from turning every build red.

CHANGELOG=$'# Changelog\n\n## [Unreleased]\n\n### Added\n\n- pending entry\n\n## [0.1.1] - 2026-09-26\n\n### Fixed\n\n- old entry\n\n[Unreleased]: https://github.com/kodmial/nanodictate/compare/v0.1.0...HEAD\n[0.1.1]: https://github.com/kodmial/nanodictate/compare/v0.0.16...v0.1.1\n[0.0.16]: https://github.com/kodmial/nanodictate/releases/tag/v0.0.16'
CHANGELOG_FILE="${TMP_DIR}/CHANGELOG.md"
printf '%s\n' "$CHANGELOG" > "$CHANGELOG_FILE"

CUT=$(printf '%s\n' "$CHANGELOG" | policy_changelog_cut_release 0.1.2 2026-09-26)
# The whole file, so a misplaced blank line or a rewritten note is caught too:
# the accumulated Unreleased body must become the body of the new release, an
# empty Unreleased is reopened above it, and nothing else moves.
assert_eq 'changelog: the Unreleased body is cut into a release section' \
  $'# Changelog\n\n## [Unreleased]\n\n## [0.1.2] - 2026-09-26\n\n### Added\n\n- pending entry\n\n## [0.1.1] - 2026-09-26\n\n### Fixed\n\n- old entry\n\n[Unreleased]: https://github.com/kodmial/nanodictate/compare/v0.1.2...HEAD\n[0.1.2]: https://github.com/kodmial/nanodictate/compare/v0.1.1...v0.1.2\n[0.1.1]: https://github.com/kodmial/nanodictate/compare/v0.0.16...v0.1.1\n[0.0.16]: https://github.com/kodmial/nanodictate/releases/tag/v0.0.16' \
  "$CUT"
assert_eq 'changelog: the new version is the first release header after Unreleased' \
  '0.1.2' \
  "$(printf '%s\n' "$CUT" | awk '
    /^## \[Unreleased\]$/ { after = 1; next }
    after && /^## \[[0-9]+\.[0-9]+\.[0-9]+\]/ {
      header = $0; sub(/^## \[/, "", header); sub(/\].*$/, "", header)
      print header; exit
    }')"
assert_eq 'changelog: the cut is idempotent — a second cut targets the next version' \
  '0.1.3' \
  "$(printf '%s\n' "$CUT" | policy_changelog_cut_release 0.1.3 2026-09-27 | awk '
    /^## \[Unreleased\]$/ { after = 1; next }
    after && /^## \[[0-9]+\.[0-9]+\.[0-9]+\]/ {
      header = $0; sub(/^## \[/, "", header); sub(/\].*$/, "", header)
      print header; exit
    }')"

# A changelog with no compare links is still cuttable (the links are simply
# left alone) — a missing link block must not fail a release.
NO_LINKS=$'# Changelog\n\n## [Unreleased]\n\n- pending entry\n\n## [0.1.1] - 2026-09-26\n'
assert_eq 'changelog: a changelog without compare links is still cut' \
  $'# Changelog\n\n## [Unreleased]\n\n## [0.1.2] - 2026-09-26\n\n- pending entry\n\n## [0.1.1] - 2026-09-26' \
  "$(printf '%s' "$NO_LINKS" | policy_changelog_cut_release 0.1.2 2026-09-26)"

# A changelog with no `## [Unreleased]` header is a policy error: the notes
# cannot be cut, and a silent no-op would publish a version with no section —
# the exact state that made CI red for v0.1.2.
NO_UNRELEASED=$'# Changelog\n\n## [0.1.1] - 2026-09-26\n'
assert_not_ok 'changelog: a changelog without an Unreleased header is a policy error' \
  policy_changelog_cut_release 0.1.2 2026-09-26 < <(printf '%s' "$NO_UNRELEASED")
assert_eq 'changelog: the failed cut writes nothing, so no half-written changelog' \
  '' \
  "$(policy_changelog_cut_release 0.1.2 2026-09-26 < <(printf '%s' "$NO_UNRELEASED") 2>/dev/null)"

# The version and the date are inputs, not decoration: an unpublishable version
# or a missing date is rejected instead of cutting a bogus header.
assert_not_ok 'changelog: an unpublishable version is a policy error' \
  policy_changelog_cut_release 1.0.0 2026-09-26 < "$CHANGELOG_FILE"
assert_not_ok 'changelog: a missing date is a policy error' \
  policy_changelog_cut_release 0.1.2 < "$CHANGELOG_FILE"

# A trailing newline must survive the cut, and a missing one (as in the
# repository's own CHANGELOG.md) must not be added: either way the automatic
# bump produces no whitespace-only hunk.
CUT_FILE="${TMP_DIR}/CHANGELOG.cut"
last_byte() { tail -c 1 "$1" | od -An -tx1 | tr -d ' \n'; }
policy_changelog_cut_release 0.1.2 2026-09-26 < "$CHANGELOG_FILE" > "$CUT_FILE"
assert_eq 'changelog: a trailing newline is kept' \
  "$(last_byte "$CHANGELOG_FILE")" "$(last_byte "$CUT_FILE")"
printf '%s' "$CHANGELOG" > "$CHANGELOG_FILE"
policy_changelog_cut_release 0.1.2 2026-09-26 < "$CHANGELOG_FILE" > "$CUT_FILE"
assert_eq 'changelog: a missing trailing newline is not added' \
  "$(last_byte "$CHANGELOG_FILE")" "$(last_byte "$CUT_FILE")"

# The real repository changelog must satisfy the same invariant the macOS test
# asserts, so the fix for it cannot silently regress here.
assert_eq 'changelog: the repository changelog matches Version.swift' \
  "$(grep -m1 -oE '[0-9]+\.[0-9]+\.[0-9]+' "${REPO_ROOT}/Sources/NanoDictateCore/Version.swift")" \
  "$(awk '
    /^## \[Unreleased\]$/ { after = 1; next }
    after && /^## \[[0-9]+\.[0-9]+\.[0-9]+\]/ {
      header = $0; sub(/^## \[/, "", header); sub(/\].*$/, "", header)
      print header; exit
    }' "${REPO_ROOT}/CHANGELOG.md")"

# --- 5. End-to-end against a real git diff ----------------------------------
# Replays exactly the shell the workflow runs, against a throwaway repository
# tagged like a published release, so a change in the grep patterns or in the
# git invocation itself cannot pass unnoticed.

FIXTURE="${TMP_DIR}/repo"
mkdir -p "$FIXTURE"
git -C "$FIXTURE" init -q -b main
git -C "$FIXTURE" config user.email 'test@example.com'
git -C "$FIXTURE" config user.name 'Release Policy Test'
mkdir -p "$FIXTURE/Sources/NanoDictateCore" "$FIXTURE/docs" "$FIXTURE/.github/workflows" "$FIXTURE/scripts"
printf 'let string = "0.0.1"\n' > "$FIXTURE/Sources/NanoDictateCore/Version.swift"
printf 'source\n' > "$FIXTURE/Sources/NanoDictateCore/Transcriber.swift"
printf 'PIN_REV=deadbeef\n' > "$FIXTURE/scripts/install-macports.sh"
git -C "$FIXTURE" add -A
git -C "$FIXTURE" commit -q -m 'base'
git -C "$FIXTURE" tag v0.0.1

# Classify the fixture the way the workflow does.
fixture_decide() {
  local labels_json=$1
  local all_changed installer_diff
  all_changed=$(git -C "$FIXTURE" diff --name-only v0.0.1..HEAD)
  installer_diff=$(git -C "$FIXTURE" diff v0.0.1..HEAD -- scripts/install-macports.sh \
    | policy_installer_diff_wo_pinrev)
  policy_decide "$labels_json" "$all_changed" "$installer_diff"
}

assert_eq 'fixture: an unchanged tree is no-changes' \
  'no-changes' "$(fixture_decide "$EMPTY")"

printf 'guide\n' > "$FIXTURE/docs/packaging.md"
printf 'name: CI\n' > "$FIXTURE/.github/workflows/continuum-ci.yml"
git -C "$FIXTURE" add -A
git -C "$FIXTURE" commit -q -m 'docs only'
assert_eq 'fixture: docs + .github only is non-releasable' \
  'non-releasable' "$(fixture_decide "$EMPTY")"
assert_eq 'fixture: the same docs-only diff is skipped when labeled' \
  'skip-release' "$(fixture_decide "$LABELED")"

printf 'more source\n' >> "$FIXTURE/Sources/NanoDictateCore/Transcriber.swift"
git -C "$FIXTURE" add -A
git -C "$FIXTURE" commit -q -m 'real code change'
assert_eq 'fixture: a real code change is releasable' \
  'releasable' "$(fixture_decide "$EMPTY")"
assert_eq 'fixture: a real code change is skipped when labeled' \
  'skip-release' "$(fixture_decide "$LABELED")"

# A PIN_REV-only refresh, on its own branch so no real code change is in range.
git -C "$FIXTURE" checkout -q -B pin-only v0.0.1
printf 'PIN_REV=cafebabe\n' > "$FIXTURE/scripts/install-macports.sh"
git -C "$FIXTURE" add -A
git -C "$FIXTURE" commit -q -m 'pin refresh only'
assert_eq 'fixture: a PIN_REV-only installer refresh is non-releasable' \
  'non-releasable' "$(fixture_decide "$EMPTY")"

printf 'https://example.com/real\n' >> "$FIXTURE/scripts/install-macports.sh"
git -C "$FIXTURE" add -A
git -C "$FIXTURE" commit -q -m 'real installer edit'
assert_eq 'fixture: a real installer edit is releasable' \
  'releasable' "$(fixture_decide "$EMPTY")"

# A chmod of the installer produces a mode-only diff (no +/- lines at all); it
# is still a real installer edit, so it must not be swallowed as PIN_REV-only.
git -C "$FIXTURE" checkout -q -B mode-only v0.0.1
chmod +x "$FIXTURE/scripts/install-macports.sh"
git -C "$FIXTURE" add -A
git -C "$FIXTURE" commit -q -m 'installer mode change only'
assert_eq 'fixture: a mode-only installer change is releasable' \
  'releasable' "$(fixture_decide "$EMPTY")"

# --- summary -----------------------------------------------------------------

printf '\n%s passed, %s failed\n' "$PASSED" "$FAILED"
[[ "$FAILED" -eq 0 ]]
