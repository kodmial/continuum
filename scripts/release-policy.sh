#!/usr/bin/env bash
# Release policy for the automated Release PR flow
# (.github/workflows/continuum-tech-swift-release-pr.yml).
#
# This file is a SOURCEABLE library, not an executable: the workflow does
#   source scripts/release-policy.sh
# so that every decision that answers "does this merge need a Release PR
# update?" is plain, testable shell instead of inline YAML. The test suite is
# scripts/test-release-policy.sh (run by .github/workflows/continuum-tech-swift-ci.yml).
#
# Two INDEPENDENT gates decide the outcome, in this order:
#
#   1. policy_pr_has_label — the deliberate opt-out. A merged PR labelled
#      `skip-release` never triggers a Release PR update, even when its file
#      paths would count as releasable. The label is read from the pull_request
#      event payload (no API call), so this gate costs nothing.
#   2. policy_releasable_paths — the path-based guard. A diff limited to
#      Version.swift, the release-please config/manifest, CHANGELOG.md,
#      README.md, SECURITY.md, LICENSE, docs/, .github/, .githooks/, the root
#      meta-dotfiles and packaging files would publish an empty release, so it
#      is classified non-releasable.
#
# The label is an OVERRIDE, not a replacement: the path guard keeps working for
# unlabeled PRs, and unrelated labels (`chore`, `documentation`, `ci`, ...)
# are deliberately NOT overloaded — the release decision stays explicit.
#
# Every no-release outcome is a GREEN no-op (exit 0, nothing written, no
# release PR update). Only a broken policy/configuration input — a
# version that cannot be parsed or a version outside the allowed series — is
# allowed to fail the workflow, because a human error that must be
# surfaced rather than silently swallowed.
#
# The CHANGELOG cut is also here, not in the workflow: policy_next_version
# computes the bumped version and policy_changelog_cut_release cuts the matching
# CHANGELOG section, so the Version.swift constant and the changelog a test
# compares it against can never drift apart. The Release PR workflow applies
# the cut on the release-please branch after release-please bumps Version.swift.
#
# Version ownership (policy_check_version_ownership) is enforced by CI: only
# the automated Release PR branch may touch Version.swift or
# .release-please-manifest.json. Ordinary PRs never reserve the next version.

# Files whose diff alone must never publish a version. Full paths are anchored
# on both ends; a bare `LICENSE` must not swallow `packaging/LICENSE.txt`.
POLICY_EXCLUDED_PATH_RE='^(Sources/NanoDictateCore/Version\.swift|CHANGELOG\.md|README\.md|\.github/.*|\.githooks/.*|docs/.*|packaging/.*|nanodictate\.rb|config\.example\.toml|SECURITY\.md|CODE_OF_CONDUCT\.md|CONTRIBUTING\.md|LICENSE|\.gitignore|\.swift-format|\.swiftlint\.yml|\.coderabbit\.yaml|release-please-config\.json|\.release-please-manifest\.json)$'

# The macports installer is pinned to the just-synced tree by a one-line PIN_REV
# edit that the release workflow pushes to main after every tag. That generated
# line is not a user code change.
POLICY_INSTALLER_PATH='scripts/install-macports.sh'

# The only version series this project publishes (user policy): 0.0.x / 0.1.x,
# no leading zeros. Anything else is a configuration error, not a no-op.
POLICY_VERSION_RE='^0\.(0|1)\.(0|[1-9][0-9]*)$'

# Automatic bumps in the 0.0.x series stop at 0.0.100; 0.1.x may grow past it.
POLICY_0X_CAP=100

# policy_label_names <labels-json>
#
# Print the label names of a pull_request event payload, one per line. jq is
# used when available; the grep/sed fallback keeps the function working on a
# runner image without jq. Matching on names (not on the raw JSON) is what makes
# `skip-release` distinct from `skip-release-please` or from a label whose
# DESCRIPTION mentions skip-release. Always exits 0: "no labels" is a valid
# answer, not an error (callers such as policy_pr_has_label apply their own
# match on the output).
policy_label_names() {
  local payload=${1:-}
  [[ -n "$payload" && "$payload" != "null" ]] || return 0
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$payload" | jq -r 'if type == "array" then .[].name else empty end' || true
  else
    printf '%s' "$payload" \
      | grep -oE '"name"[[:space:]]*:[[:space:]]*"[^"]*"' \
      | sed -E 's/^"name"[[:space:]]*:[[:space:]]*"(.*)"$/\1/' || true
  fi
}

# policy_pr_has_label <labels-json> <label-name>
#
# Exit 0 when the merged PR carries the label, 1 otherwise. An exact,
# whole-line match: `skip-release` never matches `no-skip-release`.
policy_pr_has_label() {
  local payload=${1:-} label=${2:-}
  [[ -n "$label" ]] || return 1
  policy_label_names "$payload" | grep -qxF -- "$label"
}

# policy_installer_diff_wo_pinrev
#
# stdin: `git diff <tag>..HEAD -- scripts/install-macports.sh` output.
# stdout: the added/removed lines that are NOT the generated PIN_REV edit, i.e.
# the evidence of a real installer change (empty = PIN_REV-only).
#
# Filtering is hunk-aware: `diff --git` / `---` / `+++` headers carry no
# content, so only the +/- lines INSIDE a `@@` hunk are considered. A plain
# `grep -vE '^(\+\+\+|---)'` would also throw away real content that itself
# starts with `++` or `--` (an added `+foo` line, a removed `-- bar` line), and
# it would miss a mode-only change (chmod) entirely, because `old mode` /
# `new mode` lines are not +/- lines. Both are real installer edits, so a
# file-mode change is kept as evidence.
policy_installer_diff_wo_pinrev() {
  awk '
    /^(old mode|new mode|new file mode|deleted file mode) / { print; next }
    /^diff --git / { in_hunk = 0; next }
    /^@@ / { in_hunk = 1; next }
    in_hunk && /^[+-]/ && $0 !~ /^[+-]PIN_REV=/ { print }
  '
}

# policy_releasable_paths <installer-diff-wo-pinrev>
#
# stdin: newline-separated `git diff --name-only` paths.
# stdout: the subset that counts as a user-visible change (possibly empty).
# The installer is dropped only when it is the ONLY remaining candidate and its
# diff is PIN_REV-only — a real installer edit keeps counting.
policy_releasable_paths() {
  local installer_diff=${1:-}
  local remaining
  remaining=$(grep -vE "$POLICY_EXCLUDED_PATH_RE" | grep -v '^$' || true)
  if [[ -z "$installer_diff" ]] \
    && printf '%s\n' "$remaining" | grep -qxF -- "$POLICY_INSTALLER_PATH"; then
    remaining=$(printf '%s\n' "$remaining" | grep -v "^${POLICY_INSTALLER_PATH}\$" || true)
  fi
  printf '%s\n' "$remaining" | grep -v '^$' || true
}

# policy_decide <labels-json> <all-changed-paths> <installer-diff-wo-pinrev>
#
# The single entry point the workflow calls. Prints exactly one decision:
#
#   skip-release   the merged PR carries the `skip-release` label  (green no-op)
#   no-changes     nothing changed since the previous release tag  (green no-op)
#   non-releasable only excluded paths changed                   (green no-op)
#   releasable     a real code change — bump and release
#
# Always exits 0: a no-release outcome is a normal result, not a failure. The
# label gate is evaluated FIRST, before any path classification, so a labeled
# PR never even needs a diff.
policy_decide() {
  local labels=${1:-} all_changed=${2:-} installer_diff=${3:-}

  if policy_pr_has_label "$labels" skip-release; then
    printf 'skip-release\n'
    return 0
  fi
  if [[ -z "${all_changed//[[:space:]]/}" ]]; then
    printf 'no-changes\n'
    return 0
  fi
  local releasable
  releasable=$(printf '%s\n' "$all_changed" | policy_releasable_paths "$installer_diff")
  if [[ -z "${releasable//[[:space:]]/}" ]]; then
    printf 'non-releasable\n'
    return 0
  fi
  printf 'releasable\n'
}

# policy_version_is_allowed <version>
#
# Exit 0 for a publishable version (0.0.x / 0.1.x, no leading zeros), 1
# otherwise. A failure here is a deliberate policy error and DOES fail the
# workflow: silently skipping a release would hide a broken Version.swift.
policy_version_is_allowed() {
  local version=${1:-}
  [[ "$version" =~ $POLICY_VERSION_RE ]]
}

# policy_automatic_bump_blocked <version>
#
# Exit 0 when the automatic patch bump must not happen because the 0.0.x series
# already reached its 0.0.100 cap. 0.1.x is never blocked (0.1.101 is fine).
policy_automatic_bump_blocked() {
  local version=${1:-}
  [[ "$version" == 0.0.* ]] || return 1
  local patch=${version##*.}
  (( 10#$patch >= POLICY_0X_CAP ))
}

# policy_next_version <version>
#
# Print the automatic patch bump of <version>, preserving the major.minor
# prefix: 0.1.12 -> 0.1.13, never rewritten into the 0.0.x series. 10# forces
# base-10 so a stray leading zero is not read as octal.
policy_next_version() {
  local version=${1:-}
  printf '%s.%s\n' "${version%.*}" "$(( 10#${version##*.} + 1 ))"
}

# The automated Release PR branch owned by release-please. Only commits on
# this branch may touch the version-owned files below; every other branch is
# an ordinary development branch and must leave the next version alone.
POLICY_RELEASE_BRANCH='release-please--branches--main'

# Files whose content is owned by the Release PR flow: the version constant
# (bumped by release-please through its generic extra-files updater) and the
# release-please manifest (rewritten by release-please on every Release PR
# update). Ordinary PRs must not touch them solely to reserve the next version.
POLICY_VERSION_OWNED_RE='^(Sources/NanoDictateCore/Version\.swift|\.release-please-manifest\.json)$'

# policy_is_release_branch <branch>
#
# Exit 0 when <branch> is the automated Release PR branch, 1 otherwise.
policy_is_release_branch() {
  local branch=${1:-}
  [[ "$branch" == "$POLICY_RELEASE_BRANCH" ]]
}

# policy_version_owned_touched <changed-paths>
#
# stdin alternative: newline-separated `git diff --name-only` paths are passed
# as $1. stdout: the subset that is owned by the Release PR flow (possibly
# empty). Always exits 0: "nothing owned touched" is a valid answer.
policy_version_owned_touched() {
  local all_changed=${1:-}
  printf '%s\n' "$all_changed" | grep -E "$POLICY_VERSION_OWNED_RE" | grep -v '^$' || true
}

# policy_check_version_ownership <head-branch> <changed-paths>
#
# The single entry point CI calls. Prints exactly one decision and exits 0
# when the change is allowed, 1 when an ordinary branch touches version-owned
# files:
#
#   ok                  the Release PR branch, or no version-owned file touched
#   version-ownership   an ordinary branch touches Version.swift or the manifest
#
# A non-zero exit is a deliberate policy error and DOES fail CI: reserving the
# next version from a feature PR would race with the Release PR serialization.
policy_check_version_ownership() {
  local branch=${1:-} all_changed=${2:-}
  local owned
  owned=$(policy_version_owned_touched "$all_changed")
  if [[ -z "${owned//[[:space:]]/}" ]]; then
    printf 'ok\n'
    return 0
  fi
  if policy_is_release_branch "$branch"; then
    printf 'ok\n'
    return 0
  fi
  printf 'version-ownership\n'
  return 1
}

# policy_extract_version <content>
#
# stdout: the first `<major>.<minor>.<patch>` triple in <content> (possibly
# empty). Always exits 0: "no version found" is a valid answer, not an error
# (callers decide whether an empty version is a policy error). The same
# extraction the workflows use on Version.swift
# (`grep -m1 -oE '[0-9]+\.[0-9]+\.[0-9]+'`).
policy_extract_version() {
  printf '%s' "${1:-}" | grep -m1 -oE '[0-9]+\.[0-9]+\.[0-9]+' || true
}

# policy_is_version_seeding <base-swift> <head-swift> <base-manifest> <head-manifest>
#
# Exit 0 only for the one-time release-please bootstrap: the manifest did not
# exist at base (empty content), the head manifest is seeded at the current
# Version.swift version, and Version.swift itself kept its version
# (annotation-only change). Every other version-owned touch — a real bump of
# Version.swift, a manifest rewrite to a different version, a manifest seeded
# at the wrong version — exits 1.
#
# CI needs this because the PR that introduces the Release PR flow itself
# touches both version-owned files without reserving a new version; the
# version-ownership gate would otherwise fail its own bootstrap. After the
# bootstrap the manifest exists at base, so this exemption never fires again
# and ordinary PRs stay strictly barred from version-owned files.
policy_is_version_seeding() {
  local base_swift=${1:-} head_swift=${2:-} base_manifest=${3:-} head_manifest=${4:-}
  [[ -z "${base_manifest//[[:space:]]/}" ]] || return 1
  [[ -n "${head_manifest//[[:space:]]/}" ]] || return 1
  local base_v head_v head_m
  base_v=$(policy_extract_version "$base_swift")
  head_v=$(policy_extract_version "$head_swift")
  head_m=$(policy_extract_version "$head_manifest")
  [[ -n "$base_v" && -n "$head_v" && -n "$head_m" ]] || return 1
  [[ "$base_v" == "$head_v" ]] || return 1
  [[ "$head_m" == "$head_v" ]] || return 1
  return 0
}

# policy_changelog_cut_release <version> <date>
#
# stdin:  the whole CHANGELOG.md
# stdout: the CHANGELOG with the `## [Unreleased]` block closed into a
#         `## [<version>] - <date>` release section, a fresh empty
#         `## [Unreleased]` reopened above it, and the compare links
#         (`[Unreleased]`, `[<version>]`) refreshed.
#
# The bump step in .github/workflows/continuum-tech-swift-release-pr.yml calls this together with
# the Version.swift rewrite, because the two are one release: a version bump
# without the matching changelog section is what makes
# VersionTests.testVersionStringEqualsCurrentRelease fail — the test compares
# Version.swift against the first `## [<semver>]` header after `## [Unreleased]`.
# The compare links are part of the same story: without this they would keep
# spanning the already published version.
#
# The Unreleased body is NOT copied or reworded: it simply stays where it is and
# thereby becomes the body of the new release section, which is what
# "release what has accumulated" means. Nothing is inserted when the header is
# missing — a changelog without `## [Unreleased]` is a policy error (exit 1) and
# must fail the workflow instead of publishing a version with no notes.
#
# A missing or malformed `## [Unreleased]` is the only failure; a changelog
# without compare-link definitions is accepted (the links are simply left
# alone), and so is a missing trailing newline, which is preserved byte for byte
# so an automatic bump never produces a whitespace-only diff.
policy_changelog_cut_release() {
  local version=${1:-} date=${2:-}
  if [[ -z "$version" || -z "$date" ]]; then
    echo "policy_changelog_cut_release: <version> and <date> are required" >&2
    return 1
  fi
  if ! policy_version_is_allowed "$version"; then
    echo "policy_changelog_cut_release: '$version' is not a publishable version" >&2
    return 1
  fi

  # One read of stdin: the previous release (the left side of every new compare
  # link) has to be known BEFORE the section is cut, and the transformation then
  # needs the same bytes again. The trailing sentinel survives the command
  # substitution's trailing-newline stripping, so the content stays byte exact.
  local content previous
  content=$(cat; printf 'x') || return 1
  content=${content%x}
  local final_newline=1
  [[ "$content" == *$'\n' ]] || final_newline=0

  # The previous release is the first semver header after `## [Unreleased]` —
  # the same header the Swift test picks.
  previous=$(printf '%s' "$content" | awk '
    /^## \[Unreleased\]$/ { after = 1; next }
    after && /^## \[[0-9]+\.[0-9]+\.[0-9]+\]/ {
      header = $0
      sub(/^## \[/, "", header)
      sub(/\].*$/, "", header)
      print header
      exit
    }
  ')

  # Output is buffered and only printed once the changelog is known to be
  # cuttable, so a failure never leaves a half-written changelog on stdout.
  printf '%s' "$content" | awk -v version="$version" -v date="$date" \
    -v previous="$previous" -v final_newline="$final_newline" '
    {
      if (!cut && $0 ~ /^## \[Unreleased\]$/) {
        # Close the accumulated notes into a release section and reopen an empty
        # Unreleased above it. The blank line that followed the header in the
        # input becomes the separator after the new section header.
        out = out $0 "\n\n## [" version "] - " date "\n"
        cut = 1
        next
      }
      # Link references live in a trailing block; a line starting with `[` can
      # never be the `## [Unreleased]` header, so there is no ambiguity. The
      # label and the compare path are both stripped to recover the repository
      # URL, which is the only part that is reused.
      if (previous != "" && $0 ~ /^\[Unreleased\]:/) {
        base = $0
        sub(/^\[[^]]*\]:[[:space:]]*/, "", base)
        sub(/\/compare\/.*$/, "", base)
        # The reopened Unreleased now spans "since the release just cut", so it
        # starts at v<version>; the new release itself spans the old one.
        out = out "[Unreleased]: " base "/compare/v" version "...HEAD\n"
        out = out "[" version "]: " base "/compare/v" previous "...v" version "\n"
        next
      }
      out = out $0 "\n"
    }
    END {
      if (!cut) { exit 2 }
      if (!final_newline) { sub(/\n$/, "", out) }
      printf "%s", out
    }
  '
}
