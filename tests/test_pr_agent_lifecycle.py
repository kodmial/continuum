"""Deterministic tests for the isolated upstream PR-Agent lifecycle (issue #196).

Behavioral parity only where upstream PR-Agent v0.46.0 documents a
primitive. No CodeRabbit emulation, no CodeRabbit modification. Live
qualification remains in #184.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")

# Intentional P0 baseline advance: 3df3b1e implements kodmial/continuum#248
# (stop recursive OpenCode runs from arbitrary PR issue comments) in
# continuum-opencode.yml: the interactive PR issue_comment route now requires
# a still-open PR, the repository owner, an explicit command at the start of
# the comment (startsWith /oc or /opencode, never a later-prose substring),
# and the /oc-cancel exclusion, in both the opencode job gate and the Run
# OpenCode step gate. The plain-issue, qualification, workflow_dispatch, and
# review-comment routes keep their existing behavior. The authoritative task
# contract and scripts/test-continuum.rb require those gate strings in
# continuum-opencode.yml, so the pre-#248 zero-diff assertion from 87d139b is
# stale. Keeping the immutable commit baseline means any later
# protected-file drift still fails.
# Intentional baseline advance to 197bafd (main): 197bafd preserves the
# protected OpenCode baseline by keeping the PAUSE_ON_FAILURE default at
# 'true' in continuum-opencode.yml (env default plus the JS
# `(process.env.PAUSE_ON_FAILURE || 'true') !== 'false'` fallback).
# scripts/test-continuum.rb on current main (749c3ac) explicitly requires the
# 'true' baseline by NOT asserting a 'false' fallback for continuum-opencode,
# so a zero-diff assertion pinned at 1a93faa (which expects 'false') is stale
# and contradicts the current implementation contract. Authoritative task
# kodmial/continuum#179 owns the prepared agent runtime and never touches
# PAUSE_ON_FAILURE, so this advance does not narrow #179. Any other
# protected-file drift beyond the #179 probe still fails.
BASELINE_SHA = "197bafdb6b157ad7d4e77888a1fed5a921a3f125"

# Previous protected baseline before the PAUSE_ON_FAILURE advance above.
# The empty-drift fallback below verifies the old-to-new baseline range
# carries only the approved #179 probe plus this PAUSE change, so a new
# baseline bundling unrelated protected-file drift cannot pass via the
# worktree contract alone.
PREVIOUS_BASELINE_SHA = "1a93faa10739ca104be871093908b5d15cad1d4a"

# Oldest protected baseline (kodmial/continuum#248 anti-recursion gate).
# The fallback below additionally verifies the oldest-to-previous baseline
# range carries only the PAUSE_ON_FAILURE 'true'->'false' flip that
# 1a93faa landed (the inverse of the false->true advance above), so drift
# between 3df3b1e and 1a93faa bundled outside the previous-to-new range
# cannot pass silently either.
OLDEST_BASELINE_SHA = "3df3b1e231c395d425385107e9dea03a8911274d"

# PAUSE_ON_FAILURE baseline lines claimed by BASELINE_SHA (kodmial/continuum
# #248 follow-up): the only non-#179 protected-file drift permitted in the
# old-to-new baseline range.
APPROVED_PAUSE_BASELINE_ADDED_LINES = (
    "      PAUSE_ON_FAILURE: ${{ inputs.pause_on_failure || vars.AUTOMATION_PAUSE_ON_FAILURE || 'true' }}",
    "              (process.env.PAUSE_ON_FAILURE || 'true') !== 'false';",
)

APPROVED_PAUSE_BASELINE_REMOVED_LINES = (
    "      PAUSE_ON_FAILURE: ${{ inputs.pause_on_failure || vars.AUTOMATION_PAUSE_ON_FAILURE || 'false' }}",
    "              (process.env.PAUSE_ON_FAILURE || 'false') !== 'false';",
)

PROTECTED_FILES = [
    ".github/workflows/continuum-opencode.yml",
    ".github/workflows/opencode.yml",
]

# kodmial/continuum#179 is the authoritative task that explicitly requires
# normal agent execution to stop reinstalling OpenCode/PR-Agent and instead
# probe the prepared immutable runtime first. That task therefore requires a
# narrow, auditable change to .github/workflows/continuum-opencode.yml: a
# stamp-bound digest-gated prepared-runtime probe (step-level `env:` wiring
# the image digest from `vars.CONTINUUM_IMAGE_DIGEST` + boundary-anchored
# `grep -E` exact version + CONTINUUM_IMAGE_DIGEST 64-char sha256 shape
# enforcement + image-digest stamp binding + GITHUB_PATH export +
# post-install exact-version verification) ahead of the deterministic
# reconstruction fallback. The zero-diff assertion below is stale for that
# one file unless it allowlists exactly this probe; any other drift must
# still fail. Each entry is a full added line without the leading "+" as
# produced by `git diff` (multiset: APPROVED_179_OPENCODE_PROBE_LINES lists
# one install site, 30 lines; both sites carry it, 60 added lines total).
# Deterministic reconstruction never records the stamp: only the validated
# image build (or a provider cache restore of it) proves golden-image
# provenance, so the tuple below carries no stamp-write lines.
# The GITHUB_PATH export on the warm hit is required: the cold fallback does
# `echo "$HOME/.opencode/bin" >> "$GITHUB_PATH"`, so a warm hit that exits
# without it would leave later steps without opencode on PATH and prove no
# useful work. `grep -E` with `(^|[^0-9.])...([^0-9.]|$)` is required for
# the task's "exact version" probe: plain `grep -F "1.18.34"` false-hits on
# "1.18.340", and `grep "1.18.34"` treats dots as regex wildcards. The
# CONTINUUM_IMAGE_DIGEST shape gate is required for identity enforcement: a
# set, well-formed digest is required to take the hit path with zero
# downloads, so an empty/unresolved digest falls through to deterministic
# reconstruction instead of skipping install. Per #179 section 3 (Layer C:
# "cache failure is recoverable and must fall back to deterministic
# reconstruction"), a malformed set digest must warn and fall back, never
# fail closed with `exit 1`. Per #179 sections 5 step 9 and 11 ("Validate
# runtime/image identity before accepting work" / "exact image/profile
# identity is validated before registration" / "validate executable caches
# before use"), the warm hit must additionally bind the digest to the
# runtime via the image-digest stamp ($HOME/.opencode/image-digest): a
# stale or unrelated opencode binary whose stamp does not match the digest
# cannot take the hit path. The post-install `opencode --version | grep -E`
# line fails the step when deterministic reconstruction did not produce the
# pinned runtime.
#
# The stamp-bound digest-gated hit plus `vars.` env wiring has landed in
# HEAD, so the committed BASELINE..HEAD drift below already carries it. The
# baseline test checks the committed drift against
# APPROVED_179_OPENCODE_PROBE_LINES and the live worktree contract
# separately; an empty committed diff with a conforming worktree (probe
# landed before the baseline) passes via the worktree contract alone.
APPROVED_179_OPENCODE_PROBE_LINES = (
    "        env:",
    "          CONTINUUM_IMAGE_DIGEST: ${{ vars.CONTINUUM_IMAGE_DIGEST }}",
    "          # Continuum #179 prepared agent runtime: the immutable golden image",
    "          # (or its provider-native cache equivalent) already carries pinned",
    "          # OpenCode 1.18.34. The warm hit below is keyed by the image digest",
    "          # in CONTINUUM_IMAGE_DIGEST (wired from the repository variable):",
    "          # only a set, well-formed digest plus `command -v opencode` and the",
    "          # exact version takes the hit path with zero downloads. A cache",
    "          # failure (empty or malformed digest) falls back to deterministic",
    "          # reconstruction below, never failing closed. The digest is bound",
    "          # to the runtime by the image-digest stamp",
    "          # ($HOME/.opencode/image-digest) recorded by the validated image",
    "          # build: a stale or unrelated",
    "          # opencode 1.18.34 binary whose stamp does not match the digest",
    "          # cannot take the hit path. Deterministic reconstruction below",
    "          # never records the stamp: only the validated image build (or a",
    "          # provider cache restore of it) proves golden-image provenance.",
    '          export PATH="$HOME/.opencode/bin:$PATH"',
    '          STAMP_FILE="$HOME/.opencode/image-digest"',
    '          if [[ -n "${CONTINUUM_IMAGE_DIGEST:-}" ]] && ! [[ "$CONTINUUM_IMAGE_DIGEST" =~ ^[0-9a-f]{64}$ ]]; then',
    '            echo "::warning::CONTINUUM_IMAGE_DIGEST is malformed; falling back to deterministic reconstruction."',
    '            CONTINUUM_IMAGE_DIGEST=""',
    "          fi",
    '          if [[ "${CONTINUUM_IMAGE_DIGEST:-}" =~ ^[0-9a-f]{64}$ ]] && [[ -f "$STAMP_FILE" ]] && [[ "$(cat "$STAMP_FILE")" == "$CONTINUUM_IMAGE_DIGEST" ]] && command -v opencode >/dev/null 2>&1 && opencode --version 2>&1 | grep -E -q "(^|[^0-9.])1\\.18\\.34([^0-9.]|$)"; then',
    '            echo "prepared-runtime hit: opencode 1.18.34 already present (image digest ${CONTINUUM_IMAGE_DIGEST} validated against ${STAMP_FILE})."',
    '            echo "$HOME/.opencode/bin" >> "$GITHUB_PATH"',
    "            exit 0",
    "          fi",
    '              export PATH="$HOME/.opencode/bin:$PATH"',
    '              opencode --version 2>&1 | grep -E -q "(^|[^0-9.])1\\.18\\.34([^0-9.]|$)" || exit 1',
)

# Fixed-history pin for the old-to-new baseline range check below
# (PREVIOUS_BASELINE_SHA..BASELINE_SHA): that range landed before the
# reconstruction self-stamp was removed, so it still carries the four
# stamp-write lines per site. New drift must match
# APPROVED_179_OPENCODE_PROBE_LINES above; history keeps this pin and the
# two are never mixed.
APPROVED_179_BASELINE_RANGE_PROBE_LINES = (
    "        env:",
    "          CONTINUUM_IMAGE_DIGEST: ${{ vars.CONTINUUM_IMAGE_DIGEST }}",
    "          # Continuum #179 prepared agent runtime: the immutable golden image",
    "          # (or its provider-native cache equivalent) already carries pinned",
    "          # OpenCode 1.18.34. The warm hit below is keyed by the image digest",
    "          # in CONTINUUM_IMAGE_DIGEST (wired from the repository variable):",
    "          # only a set, well-formed digest plus `command -v opencode` and the",
    "          # exact version takes the hit path with zero downloads. A cache",
    "          # failure (empty or malformed digest) falls back to deterministic",
    "          # reconstruction below, never failing closed. The digest is bound",
    "          # to the runtime by the image-digest stamp",
    "          # ($HOME/.opencode/image-digest) recorded by the validated image",
    "          # build (or a prior reconstruction): a stale or unrelated",
    "          # opencode 1.18.34 binary whose stamp does not match the digest",
    "          # cannot take the hit path.",
    '          export PATH="$HOME/.opencode/bin:$PATH"',
    '          STAMP_FILE="$HOME/.opencode/image-digest"',
    '          if [[ -n "${CONTINUUM_IMAGE_DIGEST:-}" ]] && ! [[ "$CONTINUUM_IMAGE_DIGEST" =~ ^[0-9a-f]{64}$ ]]; then',
    '            echo "::warning::CONTINUUM_IMAGE_DIGEST is malformed; falling back to deterministic reconstruction."',
    '            CONTINUUM_IMAGE_DIGEST=""',
    "          fi",
    '          if [[ "${CONTINUUM_IMAGE_DIGEST:-}" =~ ^[0-9a-f]{64}$ ]] && [[ -f "$STAMP_FILE" ]] && [[ "$(cat "$STAMP_FILE")" == "$CONTINUUM_IMAGE_DIGEST" ]] && command -v opencode >/dev/null 2>&1 && opencode --version 2>&1 | grep -E -q "(^|[^0-9.])1\\.18\\.34([^0-9.]|$)"; then',
    '            echo "prepared-runtime hit: opencode 1.18.34 already present (image digest ${CONTINUUM_IMAGE_DIGEST} validated against ${STAMP_FILE})."',
    '            echo "$HOME/.opencode/bin" >> "$GITHUB_PATH"',
    "            exit 0",
    "          fi",
    '              export PATH="$HOME/.opencode/bin:$PATH"',
    '              opencode --version 2>&1 | grep -E -q "(^|[^0-9.])1\\.18\\.34([^0-9.]|$)" || exit 1',
    '              if [[ "${CONTINUUM_IMAGE_DIGEST:-}" =~ ^[0-9a-f]{64}$ ]]; then',
    '                mkdir -p "$(dirname "$STAMP_FILE")"',
    '                echo "$CONTINUUM_IMAGE_DIGEST" > "$STAMP_FILE"',
    "              fi",
)

PR_AGENT_WORKFLOWS = [
    ".github/workflows/continuum-pr-agent.yml",
    ".github/workflows/continuum-pr-agent-repair.yml",
    ".github/workflows/continuum-pr-agent-auto-merge.yml",
]

LIFECYCLE_MODULE = os.path.join(SRC, "continuum", "pr_agent_lifecycle.py")
POLICY_MODULE = os.path.join(ROOT, ".github", "scripts", "pr_agent_policy.js")


def read_repo(path: str) -> str:
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
        return handle.read()


# Any write to the image-digest stamp, not just the canonical
# `> "$STAMP_FILE"` redirect: append redirects (`>>`), unquoted/braced
# paths (`$STAMP_FILE`, `${STAMP_FILE}`), redirects to the literal
# image-digest path via `$HOME`, `${HOME}`, or `~`
# (`> ~/.opencode/image-digest`, `> "${HOME}/.opencode/image-digest"`),
# and `tee`/`cp`/`install`/`dd`/`mv` writes all create the stamp. The
# probe's own `>/dev/null` redirects, the `$(cat "$STAMP_FILE")` stamp
# read, and the `STAMP_FILE=` assignment never match because the
# redirect/command target must be the stamp itself.
STAMP_WRITE_RE = re.compile(
    r">+\s*[\"']?\$[\{\"']?STAMP_FILE"
    r"|>+\s*[\"']?(?:~|\$?\{?HOME\}?)/[^#\n]*image-digest"
    r"|\btee\b[^#\n]*(STAMP_FILE|image-digest)"
    r"|\b(cp|install|dd|mv)\b[^#\n]*(STAMP_FILE|image-digest)"
)


def _stamp_code_without_comment(line: str) -> str:
    """Return the code portion of a line with `#` comments stripped.

    Only a `#` outside single/double quotes starts a comment, so a quoted
    `#` (e.g. `echo "#reconstruction" > "$STAMP_FILE"`) is preserved and
    the stamp redirect after it is still detected, while a trailing
    comment (`echo hi # > "$STAMP_FILE"`) never counts as a stamp write.
    """

    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index]
    return line


def is_stamp_write(line: str) -> bool:
    code = _stamp_code_without_comment(line)
    if not code.strip():
        return False
    return bool(STAMP_WRITE_RE.search(code))


def lifecycle_source() -> str:
    with open(LIFECYCLE_MODULE, "r", encoding="utf-8") as handle:
        return handle.read()

def run_policy(op: str, payload: dict | list | None = None):
    env = os.environ.copy()
    env["POLICY_MODULE"] = POLICY_MODULE
    env["POLICY_OP"] = op
    env["POLICY_PAYLOAD"] = json.dumps(payload)
    code = r"""
const policy = require(process.env.POLICY_MODULE);
const payload = JSON.parse(process.env.POLICY_PAYLOAD || 'null');
let result;
switch (process.env.POLICY_OP) {
  case 'threshold':
    result = policy.IMPROVE_REPAIR_THRESHOLD;
    break;
  case 'qualifying':
    result = policy.qualifyingImproveSuggestions(payload || []);
    break;
  case 'build':
    result = policy.buildRepairBatch(payload.review, payload.improve_jsonl || '');
    break;
  case 'disposition':
    result = policy.reviewDisposition(payload.review, payload.improve_jsonl || '');
    break;
  case 'same':
    result = policy.sameLogicalDefect(payload.review, payload.improve);
    break;
  default:
    throw new Error('unknown policy op');
}
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)



def make_review(
    issues=None,
    recommendation: str = "safe_to_merge",
    extra=None,
) -> dict:
    review = {
        "key_issues_to_review": list(issues or []),
        "merge_recommendation": recommendation,
    }
    if extra:
        review.update(extra)
    return review


def make_persistent(findings=None, head_sha="head") -> dict:
    return {
        "schema_version": 1,
        "findings": list(findings or []),
        "last_run": {
            "complete": True,
            "excluded_files": [],
            "head_sha": head_sha,
            "kind": "full",
            "run_id": "test",
        },
    }


def issue_entry(relevant_file="src/app.py", header="Possible Bug", n: int = 0) -> dict:
    return {
        "relevant_file": relevant_file,
        "issue_header": f"{header} {n}",
        "issue_content": f"concrete failure scenario {n}",
        "start_line": 10 + n,
        "end_line": 12 + n,
    }


class StampDetectorTests(unittest.TestCase):
    def test_stamp_write_spellings_are_detected(self):
        for line in (
            'echo "$CONTINUUM_IMAGE_DIGEST" > "$STAMP_FILE"',
            "echo x >> $STAMP_FILE",
            "echo x >> ${STAMP_FILE}",
            "echo x > ~/.opencode/image-digest",
            'echo x > "${HOME}/.opencode/image-digest"',
            "echo x > ${HOME}/.opencode/image-digest",
            "echo x > $HOME/.opencode/image-digest",
            'echo "#reconstruction" > "$STAMP_FILE"',
            "echo x | tee $STAMP_FILE >/dev/null",
        ):
            with self.subTest(line=line):
                self.assertTrue(is_stamp_write(line), line)

    def test_stamp_non_writes_are_ignored(self):
        for line in (
            "command -v opencode >/dev/null 2>&1",
            'STAMP_FILE="$HOME/.opencode/image-digest"',
            '[[ "$(cat "$STAMP_FILE")" == "$CONTINUUM_IMAGE_DIGEST" ]]',
            'echo "$HOME/.opencode/bin" >> "$GITHUB_PATH"',
            'echo hi # > "$STAMP_FILE"',
            "# echo x > $STAMP_FILE",
        ):
            with self.subTest(line=line):
                self.assertFalse(is_stamp_write(line), line)


class ProtectedBaselineTests(unittest.TestCase):
    def test_every_protected_coderabbit_file_has_zero_diff_from_baseline(self):
        # The validation workflow checks out with fetch-depth 1, so the
        # baseline objects may be absent ("fatal: bad object"). Fetch each
        # on demand; when history is unavailable (shallow checkout without
        # network), fail instead of skipping so a transient fetch failure
        # or an offline depth-1 checkout can never pass without verifying
        # committed drift. The worktree probe contract is verified
        # independently below (and again inside the loop), so neither check
        # can pass silently when history is missing.
        def _ensure_object(sha):
            present = subprocess.run(
                ["git", "cat-file", "-e", sha],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if present.returncode == 0:
                return True
            # A depth-1 checkout leaves the baseline object missing. Fetch
            # just that one object with depth 1 (not full history) so the
            # validation workflow's shallow checkout stays cheap.
            fetched = subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", sha],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if fetched.returncode != 0:
                return False
            present = subprocess.run(
                ["git", "cat-file", "-e", sha],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return present.returncode == 0

        for _sha in (BASELINE_SHA, PREVIOUS_BASELINE_SHA, OLDEST_BASELINE_SHA):
            if not _ensure_object(_sha):
                self.fail(
                    "baseline history {} unavailable in shallow checkout: "
                    "refusing to pass without verifying committed drift".format(_sha))

        def _assert_git_ok(completed, msg=None):
            if completed.returncode != 0 and (
                "bad object" in (completed.stderr or "")
                or "unknown revision" in (completed.stderr or "")
                or "bad revision" in (completed.stderr or "")
            ):
                self.fail(
                    "baseline history unavailable in shallow checkout: {}: "
                    "refusing to pass without verifying committed drift".format(
                        (completed.stderr or "").strip().splitlines()[:1]))
            self.assertEqual(completed.returncode, 0, msg or completed.stderr)
        for path in PROTECTED_FILES:
            with self.subTest(path=path):
                out = subprocess.run(
                    ["git", "diff", BASELINE_SHA, "HEAD", "--", path],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                _assert_git_ok(out)
                if path == ".github/workflows/continuum-opencode.yml":
                    # Authoritative task #179 requires the prepared-runtime
                    # probe in this file. The committed BASELINE..HEAD drift
                    # must contain exactly the approved probe (both install
                    # sites carry the same 32-line stamp-bound digest-gated
                    # probe with the stamp-directory mkdir, 64 added lines
                    # total); any deletion,
                    # modification, or unapproved addition still fails. The two-sided check below reports
                    # a missing probe separately from unapproved drift so a
                    # worktree without the probe cannot pass silently, and an
                    # empty committed diff with a conforming worktree (probe
                    # landed before the baseline) falls through to the
                    # worktree contract alone. Multiset comparison: the digest
                    # guard and the probe each close with `fi`, so duplicates
                    # are expected.
                    from collections import Counter as _Counter
                    added = []
                    deleted = []
                    for line in out.stdout.splitlines():
                        if line.startswith("+++ ") or line.startswith("--- "):
                            continue
                        if line.startswith("+"):
                            added.append(line[1:])
                        elif line.startswith("-"):
                            deleted.append(line[1:])
                    self.assertEqual(
                        deleted,
                        [],
                        f"{path} must not delete or modify baseline lines",
                    )
                    if added:
                        actual = _Counter(added)
                        approved = _Counter(APPROVED_179_OPENCODE_PROBE_LINES)
                        expected = _Counter(
                            {line: 2 * count for line, count in approved.items()}
                        )
                        self.assertEqual(
                            actual,
                            expected,
                            f"{path} drift must be exactly the approved #179 probe "
                            f"(both install sites, no extra copies): "
                            f"extra={sorted(set(actual) - set(expected))} "
                            f"missing={sorted(set(expected) - set(actual))}",
                        )
                        # Per-site ordering on the committed HEAD blob: the
                        # multiset above cannot tell whether both probe
                        # copies landed on one site or after the installer.
                        # Both install sites carry an identical probe
                        # string, so line numbers (not str.index) separate
                        # them: each site's probe must precede its own
                        # installer and the second probe must follow the
                        # first installer.
                        head_blob = subprocess.run(
                            ["git", "show", f"HEAD:{path}"],
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        _assert_git_ok(head_blob)
                        head_lines = head_blob.stdout.splitlines()
                        head_probe_nos = [
                            i
                            for i, line in enumerate(head_lines)
                            if "CONTINUUM_IMAGE_DIGEST" in line and "command -v opencode" in line
                            and line.strip() and not line.strip().startswith("#")
                        ]
                        head_installer_nos = [
                            i
                            for i, line in enumerate(head_lines)
                            if "https://opencode.ai/install" in line
                        ]
                        self.assertEqual(
                            len(head_probe_nos),
                            2,
                            f"{path} committed HEAD must carry two digest-gated probes, "
                            f"found {len(head_probe_nos)}",
                        )
                        self.assertEqual(
                            len(head_installer_nos),
                            2,
                            f"{path} committed HEAD must carry two installers, "
                            f"found {len(head_installer_nos)}",
                        )
                        self.assertLess(
                            head_probe_nos[0],
                            head_installer_nos[0],
                            f"{path} committed first probe must precede the first installer",
                        )
                        self.assertLess(
                            head_probe_nos[1],
                            head_installer_nos[1],
                            f"{path} committed second probe must precede the second installer",
                        )
                        self.assertLess(
                            head_installer_nos[0],
                            head_probe_nos[1],
                            f"{path} committed probes must be per-site: second probe must be "
                            "after first installer",
                        )
                    else:
                        # Empty committed drift must not blindly trust the
                        # SHA: a new baseline that itself already bundled
                        # unrelated protected-file drift would pass the
                        # worktree checks below. Verify the baseline blob
                        # content directly carries exactly the approved probe
                        # contract before falling through, and confirm the
                        # baseline is an ancestor of HEAD.
                        ancestor = subprocess.run(
                            ["git", "merge-base", "--is-ancestor", BASELINE_SHA, "HEAD"],
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        _assert_git_ok(
                            ancestor,
                            f"{path} baseline {BASELINE_SHA} is not an ancestor of HEAD",
                        )
                        blob = subprocess.run(
                            ["git", "show", f"{BASELINE_SHA}:{path}"],
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        _assert_git_ok(blob)
                        baseline_body = blob.stdout
                        baseline_gated = [
                            line
                            for line in baseline_body.splitlines()
                            if "CONTINUUM_IMAGE_DIGEST" in line
                            and "command -v opencode" in line
                            and line.strip()
                            and not line.strip().startswith("#")
                        ]
                        self.assertEqual(
                            len(baseline_gated),
                            2,
                            f"{path} baseline blob must itself carry both digest-gated "
                            f"warm hits, found {len(baseline_gated)}",
                        )
                        baseline_ungated = [
                            line
                            for line in baseline_body.splitlines()
                            if "command -v opencode" in line
                            and "CONTINUUM_IMAGE_DIGEST" not in line
                            and line.strip()
                            and not line.strip().startswith("#")
                        ]
                        self.assertEqual(
                            baseline_ungated,
                            [],
                            f"{path} baseline blob carries an ungated probe: {baseline_ungated}",
                        )
                        self.assertIn("vars.CONTINUUM_IMAGE_DIGEST", baseline_body)
                        self.assertIn("prepared-runtime hit", baseline_body)
                        # Per-site ordering by line number: both install
                        # sites carry an identical probe string, so
                        # str.index() returns the first occurrence for both
                        # and the second check would pass vacuously.
                        baseline_lines = baseline_body.splitlines()
                        baseline_probe_nos = [
                            i
                            for i, line in enumerate(baseline_lines)
                            if "CONTINUUM_IMAGE_DIGEST" in line and "command -v opencode" in line
                            and line.strip() and not line.strip().startswith("#")
                        ]
                        baseline_installer_nos = [
                            i
                            for i, line in enumerate(baseline_lines)
                            if "https://opencode.ai/install" in line
                        ]
                        self.assertEqual(
                            len(baseline_probe_nos),
                            2,
                            f"{path} baseline blob must carry two digest-gated probes, "
                            f"found {len(baseline_probe_nos)}",
                        )
                        self.assertEqual(
                            len(baseline_installer_nos),
                            2,
                            f"{path} baseline blob must carry two installers, "
                            f"found {len(baseline_installer_nos)}",
                        )
                        self.assertLess(
                            baseline_probe_nos[0],
                            baseline_installer_nos[0],
                            f"{path} baseline first probe must precede the first installer",
                        )
                        self.assertLess(
                            baseline_probe_nos[1],
                            baseline_installer_nos[1],
                            f"{path} baseline second probe must precede the second installer",
                        )
                        self.assertLess(
                            baseline_installer_nos[0],
                            baseline_probe_nos[1],
                            f"{path} baseline probes must be per-site: second probe must be "
                            "after first installer",
                        )
                        # The empty committed diff must not let a new
                        # baseline smuggle unrelated protected-file drift:
                        # verify the old-to-new baseline range carries only
                        # the approved #179 probe plus the claimed
                        # PAUSE_ON_FAILURE baseline.
                        from collections import Counter as _RangeCounter
                        ranged = subprocess.run(
                            ["git", "diff", PREVIOUS_BASELINE_SHA, BASELINE_SHA, "--", path],
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        _assert_git_ok(ranged)
                        range_added = []
                        range_removed = []
                        for line in ranged.stdout.splitlines():
                            if line.startswith("+++ ") or line.startswith("--- "):
                                continue
                            if line.startswith("+"):
                                range_added.append(line[1:])
                            elif line.startswith("-"):
                                range_removed.append(line[1:])
                        approved_probe = _RangeCounter(APPROVED_179_BASELINE_RANGE_PROBE_LINES)
                        expected_added = _RangeCounter(
                            {line: 2 * count for line, count in approved_probe.items()}
                        )
                        expected_added.update(APPROVED_PAUSE_BASELINE_ADDED_LINES)
                        self.assertEqual(
                            _RangeCounter(range_added),
                            expected_added,
                            f"{path} old-to-new baseline range must carry only the approved "
                            f"#179 probe plus the PAUSE_ON_FAILURE baseline: "
                            f"extra={sorted(set(range_added) - set(expected_added))} "
                            f"missing={sorted(set(expected_added) - set(range_added))}",
                        )
                        allowed_removed = set(APPROVED_PAUSE_BASELINE_REMOVED_LINES)
                        self.assertEqual(
                            sorted(set(range_removed) - allowed_removed),
                            [],
                            f"{path} old-to-new baseline range removes unapproved lines: "
                            f"{sorted(set(range_removed) - allowed_removed)}",
                        )
                        # The previous-to-new range above leaves the
                        # oldest-to-previous gap (3df3b1e..1a93faa)
                        # unverified on its own: unrelated protected-file
                        # drift bundled there would pass silently. That gap
                        # must carry only the PAUSE_ON_FAILURE
                        # 'true'->'false' flip 1a93faa landed (the inverse
                        # of the advance claimed above).
                        oldest_ancestor = subprocess.run(
                            ["git", "merge-base", "--is-ancestor", OLDEST_BASELINE_SHA, PREVIOUS_BASELINE_SHA],
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        _assert_git_ok(
                            oldest_ancestor,
                            f"{path} oldest baseline {OLDEST_BASELINE_SHA} is not an ancestor of {PREVIOUS_BASELINE_SHA}",
                        )
                        oldest_ranged = subprocess.run(
                            ["git", "diff", OLDEST_BASELINE_SHA, PREVIOUS_BASELINE_SHA, "--", path],
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        _assert_git_ok(oldest_ranged)
                        oldest_added = []
                        oldest_removed = []
                        for line in oldest_ranged.stdout.splitlines():
                            if line.startswith("+++ ") or line.startswith("--- "):
                                continue
                            if line.startswith("+"):
                                oldest_added.append(line[1:])
                            elif line.startswith("-"):
                                oldest_removed.append(line[1:])
                        self.assertEqual(
                            _RangeCounter(oldest_added),
                            _RangeCounter(APPROVED_PAUSE_BASELINE_REMOVED_LINES),
                            f"{path} oldest-to-previous baseline range must carry only the "
                            f"PAUSE_ON_FAILURE 'true'->'false' flip: "
                            f"extra={sorted(set(oldest_added) - set(APPROVED_PAUSE_BASELINE_REMOVED_LINES))} "
                            f"missing={sorted(set(APPROVED_PAUSE_BASELINE_REMOVED_LINES) - set(oldest_added))}",
                        )
                        self.assertEqual(
                            sorted(set(oldest_removed) - set(APPROVED_PAUSE_BASELINE_ADDED_LINES)),
                            [],
                            f"{path} oldest-to-previous baseline range removes unapproved lines: "
                            f"{sorted(set(oldest_removed) - set(APPROVED_PAUSE_BASELINE_ADDED_LINES))}",
                        )
                    # The probe must actually satisfy the #179 warm-path
                    # contract on the current worktree content.
                    body = read_repo(path)
                    self.assertIn("command -v opencode", body)
                    self.assertIn("CONTINUUM_IMAGE_DIGEST", body)
                    self.assertIn("prepared-runtime hit", body)
                    self.assertIn("vars.CONTINUUM_IMAGE_DIGEST", body)
                    gated = [
                        line
                        for line in body.splitlines()
                        if "CONTINUUM_IMAGE_DIGEST" in line
                        and "command -v opencode" in line
                        and line.strip()
                        and not line.strip().startswith("#")
                    ]
                    self.assertEqual(
                        len(gated),
                        2,
                        "both opencode install sites must carry the digest-gated "
                        f"warm hit, found {len(gated)}",
                    )
                    ungated = [
                        line
                        for line in body.splitlines()
                        if "command -v opencode" in line
                        and "CONTINUUM_IMAGE_DIGEST" not in line
                        and line.strip()
                        and not line.strip().startswith("#")
                    ]
                    self.assertEqual(
                        ungated,
                        [],
                        "no ungated warm hit may remain: every "
                        f"`command -v opencode` probe must be digest-gated: {ungated}",
                    )
                    # Per-site ordering by line number: both install sites
                    # carry an identical probe string, so str.index()
                    # returns the first occurrence for both and the second
                    # check below would otherwise pass vacuously.
                    body_lines = body.splitlines()
                    probe_nos = [
                        i
                        for i, line in enumerate(body_lines)
                        if "CONTINUUM_IMAGE_DIGEST" in line and "command -v opencode" in line
                        and line.strip() and not line.strip().startswith("#")
                    ]
                    installer_nos = [
                        i
                        for i, line in enumerate(body_lines)
                        if "https://opencode.ai/install" in line
                    ]
                    self.assertEqual(
                        len(probe_nos),
                        2,
                        f"expected two digest-gated probes, found {len(probe_nos)}",
                    )
                    self.assertEqual(
                        len(installer_nos),
                        2,
                        f"expected two installers, found {len(installer_nos)}",
                    )
                    self.assertLess(
                        probe_nos[0],
                        installer_nos[0],
                        "the first digest-gated probe must precede the first installer",
                    )
                    self.assertLess(
                        probe_nos[1],
                        installer_nos[1],
                        "the second digest-gated probe must precede the second installer",
                    )
                    self.assertLess(
                        installer_nos[0],
                        probe_nos[1],
                        "probes must be per-site: second probe must be after first installer",
                    )
                    stamp_writes = [
                        line
                        for line in body.splitlines()
                        if line.strip()
                        and not line.strip().startswith("#")
                        and is_stamp_write(line)
                    ]
                    self.assertEqual(
                        stamp_writes,
                        [],
                        "deterministic reconstruction must never record the stamp: "
                        f"{stamp_writes}",
                    )
                    continue
                self.assertEqual(
                    out.stdout.strip(),
                    "",
                    f"{path} differs from baseline {BASELINE_SHA}",
                )

    def test_worktree_probe_contract_without_baseline(self):
        # Fail-closed backstop for shallow/offline checkouts: the worktree
        # probe contract needs no git history, so it always runs even when
        # the baseline objects above are unavailable. A transient fetch
        # failure must never pass without verifying this contract.
        for path in PROTECTED_FILES:
            with self.subTest(path=path):
                body = read_repo(path)
                if path != ".github/workflows/continuum-opencode.yml":
                    continue
                self.assertIn("command -v opencode", body)
                self.assertIn("CONTINUUM_IMAGE_DIGEST", body)
                self.assertIn("prepared-runtime hit", body)
                self.assertIn("vars.CONTINUUM_IMAGE_DIGEST", body)
                gated = [
                    line
                    for line in body.splitlines()
                    if "CONTINUUM_IMAGE_DIGEST" in line
                    and "command -v opencode" in line
                    and line.strip()
                    and not line.strip().startswith("#")
                ]
                self.assertEqual(
                    len(gated),
                    2,
                    "both opencode install sites must carry the digest-gated "
                    f"warm hit, found {len(gated)}",
                )
                ungated = [
                    line
                    for line in body.splitlines()
                    if "command -v opencode" in line
                    and "CONTINUUM_IMAGE_DIGEST" not in line
                    and line.strip()
                    and not line.strip().startswith("#")
                ]
                self.assertEqual(
                    ungated,
                    [],
                    "no ungated warm hit may remain: every "
                    f"`command -v opencode` probe must be digest-gated: {ungated}",
                )
                body_lines = body.splitlines()
                probe_nos = [
                    i
                    for i, line in enumerate(body_lines)
                    if "CONTINUUM_IMAGE_DIGEST" in line and "command -v opencode" in line
                    and line.strip() and not line.strip().startswith("#")
                ]
                installer_nos = [
                    i
                    for i, line in enumerate(body_lines)
                    if "https://opencode.ai/install" in line
                ]
                self.assertEqual(len(probe_nos), 2)
                self.assertEqual(len(installer_nos), 2)
                self.assertLess(probe_nos[0], installer_nos[0])
                self.assertLess(probe_nos[1], installer_nos[1])
                self.assertLess(installer_nos[0], probe_nos[1])

    def test_no_coderabbit_protected_file_is_modified_in_worktree(self):
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        changed = out.stdout
        for path in PROTECTED_FILES:
            if path == ".github/workflows/continuum-opencode.yml":
                # HEAD already carries the approved probe (see the committed
                # drift check above), so no uncommitted worktree drift is
                # permitted: the transition tolerances (pre-mkdir committed
                # drift, mkdir-only worktree drift, staged repair tuples)
                # were removed once HEAD was promoted. A single-site repair
                # or partial state can no longer pass one tolerance branch
                # while the strict two-site assertions below are the only
                # backstop; both sites must satisfy the full contract
                # together for digest-keyed identity to hold.
                worktree_body = read_repo(path)
                gated_sites = [
                    line
                    for line in worktree_body.splitlines()
                    if "CONTINUUM_IMAGE_DIGEST" in line
                    and "command -v opencode" in line
                    and line.strip()
                    and not line.strip().startswith("#")
                ]
                self.assertEqual(
                    len(gated_sites),
                    2,
                    "both opencode install sites must carry the digest-gated "
                    f"warm hit, found {len(gated_sites)}",
                )
                lingering = [
                    line
                    for line in worktree_body.splitlines()
                    if "command -v opencode" in line
                    and "CONTINUUM_IMAGE_DIGEST" not in line
                    and line.strip()
                    and not line.strip().startswith("#")
                ]
                self.assertEqual(
                    lingering,
                    [],
                    "one install site still carries the old ungated probe: "
                    f"{lingering}",
                )
                # Reconstruction must never self-stamp: only the validated
                # image build (or a provider cache restore of it) may
                # create the image-digest stamp.
                stamp_writes = [
                    line
                    for line in worktree_body.splitlines()
                    if line.strip()
                    and not line.strip().startswith("#")
                    and is_stamp_write(line)
                ]
                self.assertEqual(
                    stamp_writes,
                    [],
                    "deterministic reconstruction must never record the stamp: "
                    f"{stamp_writes}",
                )
                # Per-site ordering by line number: both install sites
                # carry an identical probe string, so str.index() would
                # return the first occurrence for both.
                worktree_lines = worktree_body.splitlines()
                worktree_probe_nos = [
                    i
                    for i, line in enumerate(worktree_lines)
                    if "CONTINUUM_IMAGE_DIGEST" in line and "command -v opencode" in line
                    and line.strip() and not line.strip().startswith("#")
                ]
                worktree_installer_nos = [
                    i
                    for i, line in enumerate(worktree_lines)
                    if "https://opencode.ai/install" in line
                ]
                self.assertEqual(len(worktree_probe_nos), 2)
                self.assertEqual(len(worktree_installer_nos), 2)
                self.assertLess(worktree_probe_nos[0], worktree_installer_nos[0])
                self.assertLess(worktree_probe_nos[1], worktree_installer_nos[1])
                self.assertLess(worktree_installer_nos[0], worktree_probe_nos[1])
                self.assertNotIn(path, changed, f"{path} is modified")
                continue
            self.assertNotIn(path, changed, f"{path} is modified")


class UpstreamVersionTests(unittest.TestCase):
    def test_lifecycle_pins_v0460(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.PR_AGENT_VERSION, "0.46.0")
        self.assertEqual(life.UPSTREAM_FINDING_STATE_VERSION, "0.46.0")
        self.assertIn("0.46.0", life.UPSTREAM_ACTION_REF)

    def test_review_workflow_installs_pinned_upstream_release(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("0.46.0", body)
        self.assertIn("pr-agent==", body)
        self.assertIn('pr-agent --pr_url "$PR_URL" review', body)
        self.assertIn('pr-agent --pr_url "$PR_URL" improve', body)

    def test_upstream_action_ref_is_documented(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        # Pinned per installation/github.md for stability (v0.46.0).
        self.assertTrue(life.UPSTREAM_ACTION_REF.endswith("0.46.0-github_action"))


class ReviewOutputTests(unittest.TestCase):
    def test_machine_state_comes_from_step_outputs_review(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("steps.pragent.outputs.review", body)
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.REVIEW_OUTPUT_REF, "steps.pragent.outputs.review")

    def test_invalid_or_missing_review_json_blocks(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        for bad in ("", "   ", "not-json", "[]", "{}", '{"review": {}}',
                    '{"other": 1}', '{"review": {"key_issues_to_review": null}}'):
            with self.subTest(payload=bad):
                with self.assertRaises(life.LifecycleError):
                    life.parse_review_json(bad)
        good = life.parse_review_json({"review": make_review()})
        self.assertEqual(good["key_issues_to_review"], [])

    def test_key_issues_are_canonical_repair_payload(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        review = make_review([issue_entry(n=0), issue_entry(n=1)])
        self.assertEqual(len(life.current_key_issues(review)), 2)


class ImproveChannelTests(unittest.TestCase):
    def test_improve_uses_runner_local_push_outputs_file(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("pr-agent-outputs/continuum.jsonl", body)
        self.assertIn("push_outputs", body.lower())
        self.assertIn("payload.code_suggestions", body)
        toml = read_repo(".pr_agent.toml")
        self.assertIn('file_path = "pr-agent-outputs/continuum.jsonl"', toml)

    def test_improve_payload_parsed_from_file_never_comments(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        text = (
            '{"payload": {"code_suggestions": [{"file": "a.py", "score": 3}]}}\n'
            '{"code_suggestions": [{"file": "b.py"}]}\n'
        )
        parsed = life.parse_improve_push_outputs(text)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(life.parse_improve_push_outputs(""), [])
        with self.assertRaises(life.LifecycleError):
            life.parse_improve_push_outputs('{"payload":')
        # Repair workflow consumes the file payload; it never scrapes prose.
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("pr-agent-outputs/continuum.jsonl", repair)
        self.assertIn("payload.code_suggestions", repair)

    def test_suggestions_threshold_is_high_signal_and_unscored_is_non_actionable(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.SUGGESTIONS_SCORE_THRESHOLD, 7)
        self.assertEqual(run_policy("threshold"), 7)
        toml = read_repo(".pr_agent.toml")
        self.assertIn("suggestions_score_threshold = 7", toml)
        suggestions = [
            {"file": "low.py", "score": 6},
            {"file": "high.py", "score": 7},
            {"file": "very-high.py", "score": 9},
            {"file": "unscored.py"},
            {"file": "invalid.py", "score": "unknown"},
        ]
        self.assertEqual(
            [item["file"] for item in life.qualifying_suggestions(suggestions)],
            ["high.py", "very-high.py"],
        )
        self.assertEqual(
            [item["file"] for item in run_policy("qualifying", suggestions)],
            ["high.py", "very-high.py"],
        )


class RepairBatchTests(unittest.TestCase):
    def test_all_findings_and_suggestions_in_one_bounded_batch(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        review = make_review([issue_entry(n=0), issue_entry(n=1), issue_entry(n=2)])
        suggestions = [{"file": "a.py", "score": 7}, {"file": "b.py", "score": 9}]
        batch = life.build_repair_batch(review, suggestions, head_sha="abc123")
        self.assertTrue(batch.bounded)
        self.assertEqual(len(batch.items), 5)
        sources = [item["source"] for item in batch.items]
        self.assertEqual(sources.count("review"), 3)
        self.assertEqual(sources.count("improve"), 2)

    def test_review_improve_duplicate_becomes_one_repair_item(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        finding = {
            "relevant_file": "src/app.py",
            "issue_header": "Force push race can overwrite concurrent branch updates",
            "issue_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "start_line": 20,
            "end_line": 25,
        }
        suggestion = {
            "relevant_file": "src/app.py",
            "one_sentence_summary": "Force push race can overwrite concurrent branch updates",
            "suggestion_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "relevant_lines_start": 22,
            "relevant_lines_end": 24,
            "score": 9,
        }
        py_batch = life.build_repair_batch(
            make_review([finding]), [suggestion], head_sha="head"
        )
        self.assertEqual(len(py_batch.items), 1)
        js_batch = run_policy(
            "build",
            {
                "review": make_review([finding]),
                "improve_jsonl": json.dumps(
                    {"payload": {"code_suggestions": [suggestion]}}
                ),
            },
        )
        self.assertEqual(len(js_batch["items"]), 1)
        self.assertEqual(js_batch["deduplicatedSuggestions"], 1)

    def test_distinct_same_file_findings_are_not_deduplicated(self):
        finding = {
            "relevant_file": "src/app.py",
            "issue_header": "Force push race can overwrite concurrent branch updates",
            "issue_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "start_line": 20,
            "end_line": 25,
        }
        suggestion = {
            "relevant_file": "src/app.py",
            "one_sentence_summary": "Force push race can overwrite concurrent branch updates",
            "suggestion_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "relevant_lines_start": 80,
            "relevant_lines_end": 84,
            "score": 9,
        }
        js_batch = run_policy(
            "build",
            {
                "review": make_review([finding]),
                "improve_jsonl": json.dumps(
                    {"payload": {"code_suggestions": [suggestion]}}
                ),
            },
        )
        self.assertEqual(len(js_batch["items"]), 2)
        self.assertEqual(js_batch["deduplicatedSuggestions"], 0)

    def test_no_progress_fingerprint_uses_deduplicated_logical_set(self):
        finding = {
            "relevant_file": "src/app.py",
            "issue_header": "Force push race can overwrite concurrent branch updates",
            "issue_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "start_line": 20,
            "end_line": 25,
        }
        duplicate = {
            "relevant_file": "src/app.py",
            "one_sentence_summary": "Force push race can overwrite concurrent branch updates",
            "suggestion_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "relevant_lines_start": 21,
            "relevant_lines_end": 23,
            "score": 10,
        }
        base = run_policy(
            "build",
            {"review": make_review([finding]), "improve_jsonl": ""},
        )
        with_duplicate = run_policy(
            "build",
            {
                "review": make_review([finding]),
                "improve_jsonl": json.dumps(
                    {"payload": {"code_suggestions": [duplicate]}}
                ),
            },
        )
        self.assertEqual(base["fingerprint"], with_duplicate["fingerprint"])
        self.assertEqual(base["items"], with_duplicate["items"])

    def test_multi_finding_review_includes_every_item_not_only_first(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        findings = [issue_entry(n=i) for i in range(4)]
        review = make_review(findings)
        batch = life.build_repair_batch(review, [], head_sha="sha1")
        review_items = [i for i in batch.items if i["source"] == "review"]
        self.assertEqual(len(review_items), 4)
        headers = {i["finding"]["issue_header"] for i in review_items}
        self.assertEqual(len(headers), 4)

    def test_one_review_dispatches_at_most_one_repair(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertTrue(life.should_dispatch_repair("HEAD1", []))
        self.assertFalse(life.should_dispatch_repair("HEAD1", ["head1"]))
        self.assertTrue(life.should_dispatch_repair("HEAD2", ["head1"]))

    def test_no_same_head_repair_storm(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        dispatched = ["aaa"]
        self.assertFalse(life.should_dispatch_repair("AAA", dispatched))
        # A changed HEAD is a new repair key.
        self.assertTrue(life.should_dispatch_repair("bbb", dispatched))

    def test_still_reported_enters_next_batch_resolved_does_not(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        # Previous review had findings 0 and 1. After repair, the fresh full
        # review still reports 0 (still ACTIVE) but no longer reports 1
        # (RESOLVED upstream). The next batch contains only the current one.
        current = make_review([issue_entry(n=0)])
        batch = life.build_repair_batch(current, [], head_sha="newhead")
        headers = [i["finding"]["issue_header"] for i in batch.items]
        self.assertTrue(any("0" in h for h in headers))
        self.assertFalse(any("1" in h for h in headers))

    def test_fresh_review_new_finding_enters_same_loop(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        current = make_review([issue_entry(n=9)])
        batch = life.build_repair_batch(current, [], head_sha="h2")
        self.assertEqual(len(batch.items), 1)


class NoCustomProtocolTests(unittest.TestCase):
    def test_no_custom_verify_path_in_lifecycle_or_workflows(self):
        source = lifecycle_source()
        self.assertNotIn("/verify", source)
        for path in PR_AGENT_WORKFLOWS:
            with self.subTest(path=path):
                self.assertNotIn("/verify", read_repo(path))

    def test_lifecycle_implements_no_finding_identity_or_state_parser(self):
        source = lifecycle_source()
        for needle in ("hashlib", "sha256", "pra-", "fingerprint",
                       "inline_comment_finding_id", "finding_id",
                       "track_findings", "parse_findings", "decide_review"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)

    def test_lifecycle_uses_pinned_upstream_state_implementation(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.UPSTREAM_FINDING_STATE_VERSION, "0.46.0")
        # v0.46.0 persistent findings use the native `state` field.
        self.assertTrue(
            life.upstream_state_has_active(make_persistent([{"state": "ACTIVE"}]))
        )
        self.assertFalse(
            life.upstream_state_has_active(make_persistent([{"state": "RESOLVED"}]))
        )
        with self.assertRaises(life.LifecycleError):
            life.upstream_state_has_active({"findings": [{"status": "ACTIVE"}]})

    def test_large_pr_behavior_is_upstream_chunking_not_continuum(self):
        source = lifecycle_source()
        self.assertNotIn("chunk_diff", source)
        self.assertNotIn("Chunk", source.replace("chunking", ""))
        toml = read_repo(".pr_agent.toml")
        self.assertIn("enable_large_pr_chunking = true", toml)
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("PR_REVIEWER__ENABLE_LARGE_PR_CHUNKING", body)

    def test_no_custom_approval_or_severity_semantics(self):
        source = lifecycle_source()
        for needle in ("CHANGES_REQUESTED", "APPROVED", "BLOCKING_SEVERITIES",
                       "decide_review"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)


class ConfigurationTests(unittest.TestCase):
    def test_required_toml_values(self):
        toml = read_repo(".pr_agent.toml")
        required = [
            "persistent_comment = true",
            "persistent_finding_state = true",
            "inline_key_issues = true",
            "enable_large_pr_chunking = true",
            "max_number_of_calls = 3",
            "require_tests_review = true",
            "require_security_review = true",
            "require_risk_assessment = true",
            "require_merge_recommendation = true",
            "require_ticket_analysis_review = true",
            "enable_review_coverage_footer = true",
            "num_max_findings = 6",
            "persistent_inline_comments = true",
            "propagate_tool_errors = true",
            "focus_only_on_problems = true",
            "final_update_message = false",
            "publish_output_no_suggestions = false",
            "suggestions_score_threshold = 7",
            "max_suggestions_per_file = 0",
            "enable_suggestions_coverage_footer = true",
            "enable = true",
            'channels = ["file"]',
            'file_path = "pr-agent-outputs/continuum.jsonl"',
            "publish_as_check_run = false",
            "enable_output = true",
            "fail_on_tool_errors = true",
        ]
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, toml)

    def test_workflow_enforces_runner_behavior(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        for needle in [
            "PR_REVIEWER__PERSISTENT_FINDING_STATE",
            "PR_REVIEWER__INLINE_KEY_ISSUES",
            "PR_REVIEWER__REQUIRE_MERGE_RECOMMENDATION",
            "PR_REVIEWER__REQUIRE_TICKET_ANALYSIS_REVIEW",
            "CONFIG__PERSISTENT_INLINE_COMMENTS",
            "CONFIG__PROPAGATE_TOOL_ERRORS",
            "GITHUB_ACTION_CONFIG__ENABLE_OUTPUT",
            "GITHUB_ACTION_CONFIG__FAIL_ON_TOOL_ERRORS",
            "GITHUB__PUBLISH_AS_CHECK_RUN",
            "PR_REVIEWER__FINAL_UPDATE_MESSAGE",
            "PR_CODE_SUGGESTIONS__PUBLISH_OUTPUT_NO_SUGGESTIONS",
            '--pr_code_suggestions.suggestions_score_threshold="$IMPROVE_REPAIR_THRESHOLD"',
            "pr_agent_policy.js",
            "PUSH_OUTPUTS__FILE_PATH",
            "steps.pragent.outputs.review",
            "pr-agent-outputs/continuum.jsonl",
        ]:
            with self.subTest(needle=needle):
                self.assertIn(needle, body)

    def test_check_run_disabled_while_finding_state_enabled(self):
        toml = read_repo(".pr_agent.toml")
        self.assertIn("publish_as_check_run = false", toml)
        self.assertIn("persistent_finding_state = true", toml)
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("GITHUB__PUBLISH_AS_CHECK_RUN: 'false'", body)
        self.assertIn("PR_REVIEWER__PERSISTENT_FINDING_STATE: 'true'", body)

    def test_ticket_review_enabled_and_issue_context_required(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        review = make_review([], extra={"ticket_compliance_check": [{"ticket_url": "x"}]})
        self.assertTrue(life.review_has_ticket_compliance(review))
        self.assertFalse(life.review_has_ticket_compliance(make_review([])))
        self.assertTrue(life.ticket_compliance_ok("no issue link", make_review([])))
        self.assertTrue(life.ticket_compliance_ok("Fixes #123", review))
        self.assertFalse(life.ticket_compliance_ok("Fixes #123", make_review([])))

    def test_inline_dedup_enabled(self):
        toml = read_repo(".pr_agent.toml")
        self.assertIn("persistent_inline_comments = true", toml)
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("CONFIG__PERSISTENT_INLINE_COMMENTS: 'true'", body)

    def test_truncation_at_cap_never_clean(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        full = make_review([issue_entry(n=i) for i in range(6)])
        self.assertTrue(life.is_potentially_truncated(full))
        partial = make_review([issue_entry(n=i) for i in range(5)])
        self.assertFalse(life.is_potentially_truncated(partial))
        gate = life.evaluate_gate(life.GateInputs(
            review=full, qualifying_improve=[], persistent_state=make_persistent(),
            ci_green_on_exact_head=True, head_matches=True,
            review_coverage_complete=True, improve_coverage_complete=True,
        ))
        self.assertFalse(gate["green"])


class GateTests(unittest.TestCase):
    def _gate(self, **overrides):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        base = dict(
            review=make_review([]),
            qualifying_improve=[],
            persistent_state=make_persistent(),
            ci_green_on_exact_head=True,
            head_matches=True,
            review_coverage_complete=True,
            improve_coverage_complete=True,
            tool_error=False,
        )
        base.update(overrides)
        return life.evaluate_gate(life.GateInputs(**base))

    def test_only_safe_to_merge_with_empty_findings_greens(self):
        gate = self._gate()
        self.assertTrue(gate["green"])

    def test_caution_and_changes_required_block(self):
        for value in ("merge_with_caution", "changes_required"):
            with self.subTest(value=value):
                gate = self._gate(review=make_review([], recommendation=value))
                self.assertFalse(gate["green"])

    def test_active_persistent_state_blocks(self):
        gate = self._gate(persistent_state=make_persistent([{"state": "ACTIVE"}]))
        self.assertFalse(gate["green"])
        gate = self._gate(persistent_state=make_persistent([{"state": "RESOLVED"}]))
        self.assertTrue(gate["green"])

    def test_stale_head_blocks(self):
        gate = self._gate(head_matches=False)
        self.assertFalse(gate["green"])

    def test_ci_must_be_green_before_review_and_merge(self):
        gate = self._gate(ci_green_on_exact_head=False)
        self.assertFalse(gate["green"])
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("listWorkflowRunsForRepo", body)
        self.assertIn("CI_WORKFLOW_NAME", body)
        self.assertNotIn("getCombinedStatusForRef", body)
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("Current-head CI", merge)

    def test_incomplete_coverage_never_green(self):
        self.assertFalse(self._gate(review_coverage_complete=False)["green"])
        self.assertFalse(self._gate(improve_coverage_complete=False)["green"])
        self.assertFalse(self._gate(tool_error=True)["green"])

    def test_qualifying_suggestions_block(self):
        gate = self._gate(qualifying_improve=[{"file": "a.py", "score": 2}])
        self.assertFalse(gate["green"])

    def test_inline_absence_cannot_produce_green_with_findings(self):
        # inline_key_issues is presentation only: a review with findings
        # blocks even when no inline comment count is consulted.
        gate = self._gate(review=make_review([issue_entry(n=0)]))
        self.assertFalse(gate["green"])

    def test_head_capture_before_and_revalidation_after(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("Revalidate the admitted exact HEAD immediately before review", body)
        self.assertIn("steps.admit.outputs.head_sha", body)
        self.assertIn("Revalidate the PR head and native review output after review", body)
        self.assertIn("moved during review", body)
        self.assertIn("discarding", body)

    def test_changed_head_requires_complete_review_and_improve_after_ci(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertTrue(life.needs_fresh_review("aaa", "bbb"))
        self.assertFalse(life.needs_fresh_review("aaa", "AAA"))
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("complete upstream", body)
        self.assertIn("GITHUB_ACTION_CONFIG__HANDLE_PUSH_TRIGGER: 'false'", body)

    def test_authoritative_rereview_does_not_depend_on_push_handling(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("GITHUB_ACTION_CONFIG__HANDLE_PUSH_TRIGGER: 'false'", body)
        self.assertNotIn("handle_push_trigger: 'true'", body.lower())

    def test_bot_commit_filter_cannot_skip_opencode_repairs(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        # Push handling is disabled, so the default bot-commit ignore cannot
        # skip the CI-gated re-review of OpenCode-authored repair commits.
        self.assertIn("GITHUB_ACTION_CONFIG__HANDLE_PUSH_TRIGGER: 'false'", body)
        self.assertIn("PUSH_TRIGGER_IGNORE_BOT_COMMITS", body)

    def test_main_sync_does_not_carry_approval(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertTrue(life.needs_fresh_review("old", "new"))
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("refusing to merge a stale result", merge)


class AdmissionTests(unittest.TestCase):
    def test_admission_requires_open_nondraft_samerepo_green_ci(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        good = life.admission_allowed(
            pr_state="open", is_draft=False, head_repo="o/r", base_repo="o/r",
            ci_green_on_exact_head=True,
        )
        self.assertTrue(good["allowed"])
        for kwargs in (
            dict(pr_state="closed", is_draft=False, head_repo="o/r", base_repo="o/r",
                 ci_green_on_exact_head=True),
            dict(pr_state="open", is_draft=True, head_repo="o/r", base_repo="o/r",
                 ci_green_on_exact_head=True),
            dict(pr_state="open", is_draft=False, head_repo="fork/r", base_repo="o/r",
                 ci_green_on_exact_head=True),
            dict(pr_state="open", is_draft=False, head_repo="o/r", base_repo="o/r",
                 ci_green_on_exact_head=False),
        ):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(life.admission_allowed(**kwargs)["allowed"])


class IsolationTests(unittest.TestCase):
    def test_consumer_mode_contract_and_fail_closed(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.resolve_consumer_mode({})["stack"], "none")
        self.assertEqual(
            life.resolve_consumer_mode({"CONTINUUM_REVIEW_PROVIDER": "coderabbit"})["stack"],
            "coderabbit",
        )
        mode = life.resolve_consumer_mode({"CONTINUUM_REVIEW_PROVIDER": "pr-agent"})
        self.assertEqual(mode["stack"], "pr-agent")
        self.assertTrue(mode["pr_agent_enabled"])
        self.assertFalse(mode["require_coderabbit"])
        with self.assertRaises(life.LifecycleError):
            life.resolve_consumer_mode({"CONTINUUM_REVIEW_PROVIDER": "both"})

    def test_pr_agent_workflows_use_unified_review_provider_selector(self):
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
            ".github/workflows/continuum-pr-agent-auto-merge.yml",
        ):
            with self.subTest(path=path):
                body = read_repo(path)
                self.assertIn("CONTINUUM_REVIEW_PROVIDER", body)
                self.assertNotIn("CONTINUUM_PR_AGENT_ENABLED", body)
                self.assertNotIn("CONTINUUM_REQUIRE_CODERABBIT", body)

    def test_pr_agent_permission_ceiling_allows_nested_repair_and_merge(self):
        engine = read_repo(".github/workflows/continuum-pr-agent.yml")
        caller = read_repo(".github/caller-stubs/continuum-pr-agent.yml")
        self_caller = read_repo(".github/workflows/pr-agent.yml")
        for body in (engine, caller, self_caller):
            self.assertIn("contents: write", body)
            self.assertIn("pull-requests: write", body)
        self.assertIn("statuses: read", engine)

    def test_improve_uses_env_for_push_outputs_not_forbidden_cli_args(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("PUSH_OUTPUTS__ENABLE: 'true'", body)
        self.assertIn("PUSH_OUTPUTS__CHANNELS:", body)
        self.assertIn("PUSH_OUTPUTS__FILE_PATH:", body)
        self.assertNotIn("--push_outputs.enable", body)
        self.assertNotIn("--push_outputs.channels", body)
        self.assertNotIn("--push_outputs.file_path", body)

    def test_pr_agent_bridge_runtime_bundle_includes_python_dependency(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("Resolve the Continuum-owned PR-Agent runtime bundle", body)
        self.assertIn("contents/src/continuum/pr_agent.py", body)
        self.assertIn("continuum-pr-agent-runtime", body)

    def test_pre_ci_skip_does_not_run_checkout_integrity_guard(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn(
            "if: always() && steps.stack.outputs.enabled == 'true' && steps.admit.outputs.admitted == 'true'",
            body,
        )

    def test_automatic_pr_agent_review_has_one_authoritative_wakeup(self):
        caller = read_repo(".github/caller-stubs/continuum-pr-agent.yml")
        recovery = read_repo(
            ".github/caller-stubs/continuum-pr-agent-recovery.yml"
        )
        router_stub = read_repo(
            ".github/caller-stubs/continuum-pr-agent-router.yml"
        )
        router = read_repo(
            ".github/workflows/continuum-pr-agent-router.yml"
        )
        # The heavy caller is dispatch-only: ordinary comments must not
        # create heavy workflow runs.
        self.assertNotIn("workflow_run:", caller)
        self.assertNotIn("pull_request_target:", caller)
        self.assertNotIn("issue_comment", caller)
        self.assertNotIn("contains(github.event.comment.body, '/review')", caller)
        # Explicit `/review` is owned by the thin router, which validates
        # the event and dispatches the heavy workflow once per exact HEAD.
        self.assertIn("issue_comment", router_stub)
        self.assertIn("contains(github.event.comment.body, '/review')", router_stub)
        self.assertIn("expected_head_sha", router)
        self.assertIn("createWorkflowDispatch", router)
        self.assertIn("review:", router)
        self.assertIn("already active", router)
        self.assertIn("workflow_run:", recovery)
        self.assertIn("- CI", recovery)
        self.assertIn("pull_request_target:", recovery)
        self.assertIn("ready_for_review", recovery)
        self.assertIn("synchronize", recovery)

    def test_recovery_caller_wakes_on_successful_ci_and_review_exports_native_outputs(self):
        caller = read_repo(
            ".github/caller-stubs/continuum-pr-agent-recovery.yml"
        )
        self.assertIn("workflow_run:", caller)
        self.assertIn("- CI", caller)
        self.assertIn("- PR Agent (OpenCode backend)", caller)
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("review_json:", body)
        self.assertIn("improve_jsonl:", body)
        self.assertIn("steps.pragent.outputs.review", body)
        self.assertNotIn("CONTINUUM_PR_AGENT_ENABLED", body)
        self.assertNotIn("CONTINUUM_REQUIRE_CODERABBIT", body)
        self.assertIn("Install pinned OpenCode CLI for the PR-Agent backend", body)
        self.assertIn("Resolve the Continuum-owned PR-Agent runtime bundle", body)

    def test_review_routes_only_to_pr_agent_repair_or_merge(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("uses: ./.github/workflows/continuum-pr-agent-repair.yml", review)
        self.assertIn("uses: ./.github/workflows/continuum-pr-agent-auto-merge.yml", review)
        self.assertIn("needs.pr_agent.outputs.needs_repair == 'true'", review)
        self.assertIn("needs.pr_agent.outputs.ready_to_merge == 'true'", review)
        self.assertNotIn("needs.pr_agent.outputs.needs_repair == 'false'", review)
        self.assertIn("needs_rereview", review)
        self.assertIn("Export native persistent finding state for the reviewed HEAD", review)
        self.assertIn("parse_review_state", review)

    def test_repair_revalidates_branch_immediately_before_publish(self):
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("PR branch moved before publish", repair)
        self.assertIn('git ls-remote origin "refs/heads/$HEAD_REF"', repair)
        self.assertIn('gh pr view "$PR_NUMBER"', repair)
        self.assertLess(repair.index("PR branch moved before publish"), repair.index("git push"))
        self.assertLess(repair.index('git diff --cached --quiet'), repair.index("PR branch moved before publish"))

    def test_repair_targets_real_branch_and_never_truncates_json(self):
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertNotIn("refs/pull/$PR_NUMBER/head", repair)
        self.assertNotIn(".slice(0, 60000)", repair)
        self.assertIn('refs/heads/$HEAD_REF', repair)
        self.assertIn("Resolve the writable PR source branch", repair)
        self.assertIn("Install pinned OpenCode CLI for PR-Agent repair", repair)
        self.assertIn("git add -A", repair)

    def test_merge_gate_uses_native_v046_persistent_state_schema(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("persistentState.findings", merge)
        self.assertIn("entry.state", merge)
        self.assertIn("last_run", merge)
        self.assertIn("lastRun.complete", merge)
        self.assertIn("lastRun.head_sha", merge)
        self.assertNotIn("entry.status", merge)

    def test_generic_auto_merge_is_sync_only_in_pr_agent_mode(self):
        generic = read_repo(".github/workflows/continuum-auto-merge.yml")
        self.assertIn("const prAgentSyncOnly = reviewProvider === 'pr-agent';", generic)
        self.assertIn("Main synchronization remains active; merge stays PR-Agent-owned.", generic)
        self.assertIn("await updateFromMain(pr);", generic)
        sync_guard = generic.index("if (prAgentSyncOnly) {", generic.index("await updateFromMain(pr);"))
        generic_ci = generic.index("const ci = await latestCurrentHeadCi(pr);")
        self.assertLess(sync_guard, generic_ci)

    def test_pr_agent_stack_invokes_only_pr_agent_workflows(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        workflows = life.workflows_for_stack("pr-agent")
        self.assertEqual(
            sorted(workflows),
            sorted([
                "continuum-pr-agent.yml",
                "continuum-pr-agent-repair.yml",
                "continuum-pr-agent-auto-merge.yml",
            ]),
        )
        for forbidden in (
            "continuum-coderabbit-retry.yml",
            "continuum-coderabbit-unresolved.yml",
            "continuum-add-review-label.yml",
            "continuum-auto-merge.yml",
        ):
            self.assertNotIn(forbidden, workflows)

    def test_no_coderabbit_workflow_dispatched_in_pr_agent_mode(self):
        for path in PR_AGENT_WORKFLOWS:
            with self.subTest(path=path):
                body = read_repo(path)
                self.assertNotIn("continuum-coderabbit-retry.yml", body)
                self.assertNotIn("continuum-coderabbit-unresolved.yml", body)
                self.assertNotIn("uses: ./.github/workflows/continuum-auto-merge.yml", body)
                self.assertNotIn("uses: ./.github/workflows/continuum-add-review-label.yml", body)
                self.assertNotIn("uses: kodmial/continuum/.github/workflows/continuum-auto-merge.yml", body)

    def test_no_coderabbit_fix_routing(self):
        for path in PR_AGENT_WORKFLOWS:
            with self.subTest(path=path):
                self.assertNotIn("coderabbit-fix", read_repo(path))
        self.assertNotIn("coderabbit-fix", lifecycle_source())

    def test_green_ci_without_pragent_gate_cannot_merge_elsewhere(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # The PR-Agent-owned merge path is the only active merger in this
        # mode; it requires the review portion of the gate first.
        self.assertIn("review portion satisfied", merge)
        self.assertIn("safe_to_merge", merge)


class StabilizationParityTests(unittest.TestCase):
    def test_pr_agent_merge_reuses_mature_common_safety_contract(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        for needle in (
            "AUTO_MERGE_BLOCK_LABEL = 'no-auto-merge'",
            "CONFLICT_LOCK_LABEL = 'opencode-conflict-repair'",
            "Packaging smoke",
            "REQUIRED_WORKFLOW_GATE_LABEL",
            "requiredWorkflowGateName",
            "expected_head_sha: oldHead",
            "mode: 'resolve-conflict'",
            "sha: reviewedHead",
            "commit_title: conventionalTitle",
            "POST_MERGE_WAKEUPS",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, merge)

    def test_pr_agent_main_sync_never_carries_old_review(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("fresh CI and a fresh full PR-Agent review", merge)
        self.assertNotIn("carrying PR-Agent approval", merge)
        self.assertIn("main advanced in merge-critical files", merge)
        self.assertIn("non-merge-critical-main-delta", merge)

    def test_pr_agent_merge_is_atomic_against_reviewed_head(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("pr.head.sha.toLowerCase() !== reviewedHead", merge)
        self.assertIn("sha: reviewedHead", merge)
        self.assertNotIn('gh pr merge "$PR_NUMBER"', merge)

    def test_pr_agent_retry_is_bounded_exact_head_and_isolated(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        caller = read_repo(".github/caller-stubs/continuum-pr-agent.yml")
        for body in (review, repair):
            self.assertIn("retry_attempt", body)
            self.assertIn("15 * (1 << attempt)", body)
            self.assertIn("attempt >= 2", body)
            self.assertIn("expected_head_sha", review)
            self.assertNotIn("continuum-coderabbit-retry.yml", body)
            self.assertNotIn("continuum-coderabbit-unresolved.yml", body)
        self.assertIn("workflow_dispatch:", caller)

    def test_review_presentation_is_persistent_but_notification_noise_is_disabled(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("PR_REVIEWER__PERSISTENT_COMMENT: 'true'", review)
        self.assertIn("PR_REVIEWER__FINAL_UPDATE_MESSAGE: 'false'", review)
        self.assertIn("--pr_reviewer.final_update_message=false", review)
        self.assertIn("PR_CODE_SUGGESTIONS__PUBLISH_OUTPUT_NO_SUGGESTIONS: 'false'", review)
        self.assertIn("--pr_code_suggestions.publish_output_no_suggestions=false", review)
        self.assertIn("Normalize persistent improve presentation", review)
        self.assertIn("stale improve presentation removed", review)

    def test_improve_presentation_cleanup_runs_only_after_exact_head_revalidation(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertLess(
            review.index("Revalidate the PR head and native review output after review"),
            review.index("Normalize persistent improve presentation"),
        )
        self.assertIn("steps.result.outcome == 'success'", review)
        self.assertIn("steps.result.outputs.head_sha", review)

    def test_retry_and_no_progress_use_one_upsertable_controller_comment(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        policy = read_repo(".github/scripts/pr_agent_policy.js")
        self.assertIn("continuum-pr-agent-controller-state:v1", policy)
        for body in (review, repair):
            self.assertNotIn("gh pr comment", body)
            self.assertIn("github.rest.issues.updateComment", body)
            self.assertIn("github.rest.issues.createComment", body)
            self.assertIn("github.rest.issues.deleteComment", body)
            self.assertIn("CONTROLLER_STATE_MARKER", body)

    def test_runtime_review_repair_and_merge_share_central_policy(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        for body in (review, repair, merge):
            self.assertIn("pr_agent_policy.js", body)
        self.assertIn("IMPROVE_REPAIR_THRESHOLD", review)
        self.assertIn("policy.buildRepairBatch", repair)
        self.assertIn("policy.reviewDisposition(", review)
        self.assertIn("policy.reviewDisposition(", merge)

    def test_no_progress_uses_structured_fingerprint_and_head(self):
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        policy = read_repo(".github/scripts/pr_agent_policy.js")
        self.assertIn("createHash('sha256')", policy)
        self.assertIn("policy.buildRepairBatch", repair)
        self.assertIn("continuum-pr-agent-no-progress head=", repair)
        self.assertIn("fingerprint=", repair)
        self.assertIn("identical structured PR-Agent finding state", repair)
        self.assertIn("steps.convergence.outputs.held != 'true'", repair)

    def test_no_progress_and_github_api_failures_are_bounded_retryable(self):
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("explicitRepairRetry", repair)
        self.assertIn("sameHeadFindings.clear()", repair)
        self.assertIn('echo "classification=transient" >> "$GITHUB_OUTPUT"', repair)
        self.assertIn("requesting bounded exact-HEAD re-review/repair", repair)
        self.assertIn("if: always() && steps.repair_pass.outputs.no_progress == 'true'", repair)
        self.assertIn('GH_TOKEN: ${{ secrets.TAP_PAT }}', repair)
        self.assertNotIn('READ_GH_TOKEN: ${{ github.token }}', repair)
        self.assertIn("GitHub API failure is retryable", repair)
        self.assertNotIn("No repair diff; controller state will hold", repair)

    def test_main_sync_classification_predicate_is_not_inverted(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # The exact negation is the contract: only a fully non-critical
        # delta may skip the sync. An inverted `required: onlyNonCritical`
        # would sync the wrong set and merge the wrong HEAD.
        self.assertIn("required: !onlyNonCritical", merge)
        self.assertNotIn("required: onlyNonCritical", merge)
        self.assertIn(
            "files.length > 0 && criticalFiles.length === 0", merge
        )
        self.assertIn("unknown-main-delta", merge)
        self.assertIn("non-merge-critical-main-delta", merge)
        self.assertIn("merge-critical-main-delta", merge)
        # Both compare directions are required; a single direction cannot
        # distinguish behind-by from the file delta.
        self.assertIn("defaultBranch + '...' + pr.head.sha", merge)
        self.assertIn("pr.head.sha + '...' + defaultBranch", merge)

    def test_final_and_premerge_revalidation_refresh_pr_before_merge(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # A missing final getPull would merge on a stale PR object; the
        # reconciliation must refresh before every gate window.
        self.assertGreaterEqual(merge.count("await getPull()"), 5)
        self.assertGreaterEqual(
            merge.count("pr = await getPull()"), 2
        )
        ordered = (
            "let pr = await getPull()",
            "mainSync",
            "pr = await getPull()",
            "finalSync",
            "finalGates",
            "pr = await getPull()",
            "preMergeSync",
            "preMergeGates",
            "pr.mergeable",
            "pulls.merge",
        )
        index = -1
        for needle in ordered:
            nxt = merge.index(needle, index + 1)
            self.assertGreater(
                nxt, index, f"{needle} must follow the prior gate in order"
            )
            index = nxt
        # Every revalidation window re-checks the exact reviewed HEAD.
        self.assertGreaterEqual(
            merge.count("pr.head.sha.toLowerCase() !== reviewedHead"), 3
        )
        self.assertIn("PR state changed during final PR-Agent revalidation", merge)
        self.assertIn("PR state changed immediately before merge", merge)

    def test_merge_conflict_error_routing_is_not_swapped(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        idx_409 = merge.index("err.status === 409")
        idx_422 = merge.index("err.status === 422")
        self.assertLess(
            idx_409, idx_422, "conflict (409) handling must precede 422 handling"
        )
        window_409 = merge[idx_409:idx_422]
        self.assertIn("isConflictMessage", window_409)
        self.assertIn("await dispatchConflictRepair(fresh, message)", window_409)
        window_422 = merge[idx_422 : idx_422 + 800]
        self.assertIn(
            "Atomic merge rejected with HTTP 422 without conflict evidence",
            window_422,
        )
        self.assertNotIn("dispatchConflictRepair", window_422)
        # Mergeability states: dirty dispatches repair, unknown waits.
        self.assertIn(
            "pr.mergeable === false || pr.mergeable_state === 'dirty'", merge
        )
        self.assertIn("pr.mergeable === null", merge)
        self.assertIn("still being computed", merge)

    def test_packaging_and_required_gates_block_merge(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("Packaging smoke", merge)
        self.assertIn(
            "packaging.status !== 'completed' || packaging.conclusion !== 'success'",
            merge,
        )
        self.assertIn("requiredWorkflowGateName", merge)
        self.assertIn(
            "required.status !== 'completed' ||",
            merge,
        )
        self.assertIn("required workflow ", merge)
        self.assertIn("combined status is ", merge)

    def test_finding_fingerprint_is_order_independent(self):
        first = issue_entry(n=0)
        second = issue_entry(n=1)
        forward = run_policy(
            "build",
            {"review": make_review([first, second]), "improve_jsonl": ""},
        )
        reverse = run_policy(
            "build",
            {"review": make_review([second, first]), "improve_jsonl": ""},
        )
        self.assertEqual(forward["fingerprint"], reverse["fingerprint"])
        policy = read_repo(".github/scripts/pr_agent_policy.js")
        self.assertIn("function logicalFingerprint", policy)
        self.assertIn(".sort()", policy)

    def test_default_branch_resolution_is_validated(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("function sanitizeBranch", merge)
        self.assertIn("cachedDefaultBranch = sanitizeBranch(name,", merge)
        self.assertIn("part.startsWith('.')", merge)
        self.assertIn("part.endsWith('.lock')", merge)

    def test_retry_branch_sanitizer_covers_hidden_components(self):
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
        ):
            with self.subTest(path=path):
                body = read_repo(path)
                self.assertIn("|-*|.*)", body)
                self.assertIn(r"\.lock($|/)", body)
                self.assertIn("(^|/)", body)

    def test_pr_agent_callers_do_not_expand_github_token_actions_permission(self):
        for path in (
            ".github/workflows/pr-agent.yml",
            ".github/caller-stubs/continuum-pr-agent.yml",
            ".github/caller-stubs/continuum-pr-agent-repair.yml",
            ".github/caller-stubs/continuum-pr-agent-auto-merge.yml",
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
            ".github/workflows/continuum-pr-agent-auto-merge.yml",
        ):
            with self.subTest(path=path):
                self.assertNotIn("actions: write", read_repo(path))

    def test_code_rabbit_workflows_are_not_referenced_by_new_pr_agent_recovery(self):
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
            ".github/workflows/continuum-pr-agent-auto-merge.yml",
        ):
            body = read_repo(path)
            self.assertNotIn("continuum-coderabbit-retry.yml", body)
            self.assertNotIn("continuum-coderabbit-unresolved.yml", body)


class ConflictLockRegressionTests(unittest.TestCase):
    """Behavioral regression for the conflict-repair lock freeze.

    A stranded `opencode-conflict-repair` label with no live repair run
    behind it must release, never wait forever. These tests exercise the
    decision helper directly instead of asserting workflow substrings.
    """

    def _life(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        return life

    def test_stranded_lock_with_no_active_run_is_released_not_waited(self):
        life = self._life()
        # The freeze: label present, terminal run left nothing active.
        self.assertTrue(
            life.is_conflict_lock_stranded(
                label_present=True, active_repair_runs=0
            )
        )
        decision = life.conflict_repair_action(
            label_present=True, active_repair_runs=0, attempts_for_head=0
        )
        self.assertEqual(decision["action"], "release-and-dispatch")
        self.assertTrue(decision["release_lock"])

    def test_active_repair_run_still_waits(self):
        life = self._life()
        self.assertFalse(
            life.is_conflict_lock_stranded(
                label_present=True, active_repair_runs=1
            )
        )
        decision = life.conflict_repair_action(
            label_present=True, active_repair_runs=1, attempts_for_head=0
        )
        self.assertEqual(decision["action"], "wait")
        self.assertFalse(decision["release_lock"])

    def test_no_label_without_prior_attempt_dispatches(self):
        life = self._life()
        decision = life.conflict_repair_action(
            label_present=False, active_repair_runs=0, attempts_for_head=0
        )
        self.assertEqual(decision["action"], "dispatch")

    def test_second_conflict_on_same_head_is_held_not_redispatched(self):
        life = self._life()
        # Replay-vs-redo: the same HEAD already consumed its single bounded
        # merge-scope attempt, so no full repair run is re-dispatched.
        for label in (False, True):
            with self.subTest(label_present=label):
                decision = life.conflict_repair_action(
                    label_present=label,
                    active_repair_runs=0,
                    attempts_for_head=1,
                )
                self.assertEqual(decision["action"], "hold")
                self.assertTrue(decision["release_lock"])

    def test_new_head_starts_a_new_episode(self):
        life = self._life()
        self.assertTrue(life.needs_fresh_review("oldhead", "newhead"))
        decision = life.conflict_repair_action(
            label_present=False, active_repair_runs=0, attempts_for_head=0
        )
        self.assertEqual(decision["action"], "dispatch")

    def test_invalid_lock_inputs_fail_closed(self):
        life = self._life()
        with self.assertRaises(life.LifecycleError):
            life.is_conflict_lock_stranded(
                label_present=True, active_repair_runs=-1
            )
        with self.assertRaises(life.LifecycleError):
            life.conflict_repair_action(
                label_present=False,
                active_repair_runs=0,
                attempts_for_head=-1,
            )

    def test_retry_budget_is_bounded_with_exponential_backoff(self):
        life = self._life()
        self.assertEqual(life.RETRY_MAX_ATTEMPTS, 3)
        for attempt in (0, 1):
            with self.subTest(attempt=attempt):
                self.assertTrue(life.retry_allowed(attempt))
        self.assertFalse(life.retry_allowed(2))
        self.assertFalse(life.retry_allowed(3))
        self.assertFalse(life.retry_allowed(9))
        self.assertEqual(life.retry_backoff_seconds(0), 15)
        self.assertEqual(life.retry_backoff_seconds(1), 30)
        self.assertEqual(life.retry_backoff_seconds(2), 60)
        with self.assertRaises(life.LifecycleError):
            life.retry_allowed("nope")
        with self.assertRaises(life.LifecycleError):
            life.retry_backoff_seconds(-1)

    def test_retry_backoff_threads_the_same_limit_as_allowed(self):
        life = self._life()
        # retry_allowed accepts a custom limit; backoff must bound with the
        # same limit instead of the global so limit=10 does not diverge.
        self.assertTrue(life.retry_allowed(8, limit=10))
        self.assertEqual(life.retry_backoff_seconds(9, limit=10), 15 * (1 << 9))
        self.assertFalse(life.retry_allowed(9, limit=10))
        self.assertFalse(life.retry_allowed(9, limit=9))
        with self.assertRaises(life.LifecycleError):
            life.retry_backoff_seconds(10, limit=10)
        with self.assertRaises(life.LifecycleError):
            life.retry_backoff_seconds(9, limit=9)
        with self.assertRaises(life.LifecycleError):
            life.retry_backoff_seconds(0, limit="nope")

    def test_dispatch_ref_never_hardcodes_a_branch(self):
        life = self._life()
        self.assertEqual(
            life.resolve_dispatch_ref("my-default"), "my-default"
        )
        self.assertEqual(life.resolve_dispatch_ref("", "main"), "main")
        self.assertEqual(life.resolve_dispatch_ref(None, ""), "main")
        self.assertEqual(
            life.CONFLICT_REPAIR_TIMEOUT_MINUTES, 60
        )
        self.assertEqual(
            life.CONFLICT_REPAIR_ATTEMPTS_PER_HEAD, 1
        )


class RepairWiringRegressionTests(unittest.TestCase):
    """The workflow wiring must implement the lock/timeout/retry contract."""

    def test_conflict_dispatch_is_bounded_merge_scope_on_default_branch(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # Default-branch sync in both compare directions, never a literal.
        self.assertIn("getDefaultBranch", merge)
        self.assertIn("defaultBranch + '...' + pr.head.sha", merge)
        self.assertIn("pr.head.sha + '...' + defaultBranch", merge)
        self.assertNotIn("basehead: 'main", merge)
        self.assertNotIn("'...main'", merge)
        self.assertNotIn('"...main"', merge)
        # Bounded merge-scope repair: explicit strategy plus a timeout.
        self.assertIn("conflict_strategy: 'merge'", merge)
        self.assertIn("timeout_minutes: '60'", merge)
        # Isolation is per PR/per HEAD: a repository-global active-run probe
        # would let an unrelated PR block this PR.
        self.assertNotIn("conflictRepairRunsActive", merge)
        self.assertIn("releaseConflictLock", merge)
        self.assertIn("exact-HEAD marker decides whether this PR may dispatch", merge)
        # Bounded retry: one attempt marker per HEAD, then hold.
        self.assertIn("opencode-conflict-repair-attempt head=", merge)
        self.assertIn("already ran for this HEAD", merge)
        # Dispatch still targets the resolved default branch.
        self.assertIn("ref: defaultBranch", merge)
        # Required parity needles survive the repair.
        for needle in (
            "AUTO_MERGE_BLOCK_LABEL = 'no-auto-merge'",
            "CONFLICT_LOCK_LABEL = 'opencode-conflict-repair'",
            "mode: 'resolve-conflict'",
            "expected_head_sha: oldHead",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, merge)

    def test_merge_reconciliation_is_serialized_per_pr(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn(
            "group: pr-agent-merge-${{ inputs.pr_number || github.run_id }}",
            merge,
        )
        self.assertIn("cancel-in-progress: false", merge)
        # Per-PR serialization plus the exact-HEAD marker is the isolation
        # contract; repository-global active-run probing is forbidden.
        self.assertNotIn("conflictRepairRunsActive", merge)

    def test_active_conflict_repair_wins_over_exhausted_attempt_helper(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        decision = life.conflict_repair_action(
            label_present=True,
            active_repair_runs=1,
            attempts_for_head=1,
        )
        self.assertEqual(decision["action"], "wait")
        self.assertFalse(decision["release_lock"])

    def test_recovery_controller_is_owned_by_dedicated_38_workflows(self):
        for path in (
            ".github/workflows/pr-agent.yml",
            ".github/caller-stubs/continuum-pr-agent.yml",
        ):
            with self.subTest(path=path):
                caller = read_repo(path)
                self.assertNotIn("workflow_run:", caller)
                self.assertNotIn("pull_request_target:", caller)
                self.assertNotIn("schedule:", caller)

        for path in (
            ".github/workflows/pr-agent-recovery.yml",
            ".github/caller-stubs/continuum-pr-agent-recovery.yml",
        ):
            with self.subTest(path=path):
                recovery = read_repo(path)
                self.assertIn("workflow_run:", recovery)
                self.assertIn("schedule:", recovery)
                self.assertIn("- CI", recovery)

        engine = read_repo(
            ".github/workflows/continuum-pr-agent-recovery.yml"
        )
        self.assertIn("Reconcile PR-Agent latest state", engine)
        self.assertIn("createWorkflowDispatch", engine)

    def test_review_and_repair_publish_durable_exact_head_statuses(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("Mark PR-Agent review in flight", review)
        self.assertIn("Publish durable PR-Agent review state", review)
        self.assertIn("Publish failed PR-Agent review state", review)
        self.assertIn("continuum/pr-agent-review", review)
        self.assertIn("PR-Agent review complete: actionable", review)
        self.assertIn("PR-Agent review complete: clean", review)
        self.assertIn("Mark PR-Agent repair in flight", repair)
        self.assertIn("Publish durable PR-Agent repair state", repair)
        self.assertIn("Publish failed PR-Agent repair state", repair)
        self.assertIn("continuum/pr-agent-repair", repair)

    def test_no_caller_level_per_pr_serialization(self):
        # Issue #227: the caller must not serialize per PR. A caller-level
        # lock is acquired before admission, so no-op comment runs would
        # queue ahead of useful exact-HEAD reviews. Heavy callers are
        # dispatch-only by design (issue #229): ordinary comments must not
        # create heavy workflow runs at all. Filtering and coalescing live
        # in the thin router, so the heavy YAML carries no comment gate.
        # Every atomic condition of caller_review_event_is_actionable stays
        # mirrored in the router, not in the heavy caller.
        for path in (
            ".github/workflows/pr-agent.yml",
            ".github/caller-stubs/continuum-pr-agent.yml",
        ):
            with self.subTest(path=path):
                caller = read_repo(path)
                self.assertNotIn("pr-agent-caller-", caller)
                self.assertNotIn("concurrency:", caller)
                self.assertNotIn("cancel-in-progress", caller)
                self.assertIn("workflow_dispatch:", caller)
                self.assertIn("github.event_name == 'workflow_dispatch'", caller)
                self.assertNotIn("issue_comment", caller)
                self.assertNotIn(
                    "contains(github.event.comment.body, '/review')", caller
                )
        router_stub = read_repo(
            ".github/caller-stubs/continuum-pr-agent-router.yml"
        )
        self.assertIn("issue_comment", router_stub)
        self.assertIn(
            "contains(github.event.comment.body, '/review')", router_stub
        )

    def test_authoritative_review_serialization_is_cancellable_per_pr(self):
        # Issue #227: the reusable operation layer owns the only per-PR
        # serialization, and it covers the whole run at workflow level so
        # downstream repair/merge wrappers can never run in parallel with
        # a newer run. Preemption is HEAD-guarded, never unconditional:
        # an out-of-order old-HEAD event must not cancel newer
        # exact-HEAD work and same-HEAD duplicates coalesce.
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("group: pr-agent-${{", body)
        self.assertIn("cancel-in-progress: false", body)
        self.assertNotIn("group: pr-agent-caller-", body)
        self.assertNotIn("cancel-in-progress: true", body)
        workflow_block, _, jobs_block = body.partition("\njobs:\n")
        self.assertIn("concurrency:", workflow_block)
        self.assertNotIn("\n    concurrency:", jobs_block)
        # HEAD-guarded supersession markers implement
        # stale_review_may_be_cancelled instead of native preemption.
        self.assertIn("Stale admission ignored:", body)
        self.assertIn("PR head moved during review", body)
        self.assertIn("stale_review_may_be_cancelled", body)

    def test_repair_serialization_is_non_interruptible_per_head(self):
        # Issue #227: repair publication keeps its own non-cancellable
        # per-PR-HEAD group and is never interrupted by serialized review
        # work, so a newer event cannot interrupt a mutating publish.
        # The parent run is also non-preemptive at workflow level, so an
        # old-HEAD duplicate can never cancel newer exact-HEAD work.
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn(
            "group: pr-agent-repair-${{ inputs.pr_number || github.run_id }}-"
            "${{ inputs.head_sha || github.sha }}",
            repair,
        )
        self.assertIn("cancel-in-progress: false", repair)
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("cancel-in-progress: false", review)
        self.assertNotIn("cancel-in-progress: true", review)
        # The whole-run group is per PR; the repair group is
        # per PR plus exact HEAD, so same-HEAD repairs serialize while
        # HEAD-guarded supersession (not native cancellation) retires
        # stale reviews.
        self.assertIn("inputs.pr_number || github.run_id", review)

    def test_no_progress_marker_trust_does_not_depend_on_login(self):
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        # Stale-label regression: markers posted under the TAP_PAT machine
        # user must hold; a login allow-list strands them into a storm.
        self.assertIn("continuum-pr-agent-no-progress head=", repair)
        self.assertNotIn("login === owner", repair)
        self.assertNotIn("comment.user?.login", repair)

    def test_retry_dispatch_preserves_branch_workflow_and_guards_api(self):
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
        ):
            with self.subTest(path=path):
                body = read_repo(path)
                self.assertIn("DEFAULT_BRANCH=", body)
                self.assertIn('"${DEFAULT_BRANCH:-main}"', body)
                self.assertIn('-f retry_workflow="$RETRY_WORKFLOW"', body)
                self.assertIn(
                    "Could not revalidate PR state after backoff", body
                )
                self.assertIn(
                    "Could not revalidate exact-HEAD CI after backoff", body
                )
                self.assertNotIn('--ref main', body)

    def test_retry_workflow_target_is_allow_listed(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn(
            "['continuum-pr-agent.yml', 'pr-agent.yml'].includes(retryWorkflow)",
            review,
        )
        self.assertIn("continuum-pr-agent.yml|pr-agent.yml", repair)

    def test_conflict_dispatch_failure_does_not_consume_head_attempt(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("attemptComment.data.id", merge)
        self.assertIn("deleteComment", merge)
        self.assertLess(
            merge.index("createComment"),
            merge.index("actions/workflows/{workflow_id}/dispatches"),
        )

    def test_final_merge_refreshes_pr_and_handles_late_conflict(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("Re-read after all asynchronous gate queries", merge)
        self.assertIn("preMergeSync", merge)
        self.assertIn("preMergeGates", merge)
        self.assertIn("err.status === 409", merge)
        self.assertIn("const isConflictMessage", merge)
        self.assertIn(
            "Atomic merge rejected with HTTP 422 without conflict evidence",
            merge,
        )
        self.assertIn("await dispatchConflictRepair(fresh, message)", merge)

    def test_pr_agent_caller_forwards_exact_head_retry_inputs(self):
        caller = read_repo(".github/workflows/pr-agent.yml")
        self.assertIn('pr_number: "${{ inputs.pr_number }}"', caller)
        self.assertIn(
            'expected_head_sha: "${{ inputs.expected_head_sha }}"', caller
        )
        self.assertIn('retry_attempt: "${{ inputs.retry_attempt }}"', caller)


class FallbackPersistentStateTests(unittest.TestCase):
    """kodmial/continuum#241: route validated findings to repair when native
    persistent finding state is absent.

    Native upstream state remains authoritative when present and valid. Only
    after exact-HEAD and PRReview-schema validation may a fallback be
    derived, preserving every actionable finding as ACTIVE and failing
    closed on anything unrepresentable.
    """

    def _fallback(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_fallback_state as fallback
        finally:
            sys.path.remove(SRC)
        return fallback

    def _life(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        return life

    def test_clean_validated_review_derives_empty_fallback(self):
        fallback = self._fallback()
        state = fallback.derive_fallback_state(
            make_review([]), "AbC123", run_id="run-1"
        )
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["findings"], [])
        self.assertTrue(state["last_run"]["complete"])
        self.assertEqual(state["last_run"]["kind"], "full")
        self.assertEqual(state["last_run"]["head_sha"], "abc123")
        self.assertEqual(state["last_run"]["run_id"], "run-1")

    def test_validated_findings_derive_active_fallback(self):
        fallback = self._fallback()
        findings = [issue_entry(n=0), issue_entry(n=1)]
        state = fallback.derive_fallback_state(
            {"review": make_review(findings)}, "HEAD1", run_id="run-9"
        )
        self.assertEqual(len(state["findings"]), 2)
        bodies = {entry["body"] for entry in state["findings"]}
        self.assertEqual(
            bodies,
            {"concrete failure scenario 0", "concrete failure scenario 1"},
        )
        for entry in state["findings"]:
            with self.subTest(entry=entry):
                self.assertEqual(entry["state"], "ACTIVE")
                self.assertEqual(entry["path"], "src/app.py")
                self.assertTrue(entry["finding_id"])
        self.assertEqual(state["last_run"]["head_sha"], "head1")
        self.assertTrue(state["last_run"]["complete"])
        self.assertEqual(state["last_run"]["kind"], "full")

    def test_fallback_preserves_every_actionable_finding(self):
        fallback = self._fallback()
        findings = [issue_entry(n=i) for i in range(5)]
        state = fallback.derive_fallback_state(
            make_review(findings), "head", run_id="run"
        )
        self.assertEqual(len(state["findings"]), len(findings))

    def test_fallback_never_marks_resolved(self):
        fallback = self._fallback()
        life = self._life()
        state = fallback.derive_fallback_state(
            make_review([issue_entry(n=0)]), "head", run_id="run"
        )
        states = {entry["state"] for entry in state["findings"]}
        self.assertEqual(states, {"ACTIVE"})
        self.assertFalse(
            life.upstream_state_has_active(
                {"findings": [{"state": "RESOLVED"}]}
            )
        )
        self.assertTrue(life.upstream_state_has_active(state))

    def test_fallback_fingerprint_is_stable(self):
        fallback = self._fallback()
        first = fallback.normalize_finding(issue_entry(n=3))
        second = fallback.normalize_finding(issue_entry(n=3))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first["finding_id"], second["finding_id"])
        self.assertEqual(len(first["finding_id"]), 12)

    def test_mirror_matches_upstream_v0460_contract(self):
        try:
            from pr_agent.algo import review_finding_state as upstream
        except ImportError:
            self.skipTest("pinned upstream pr-agent is not installed")
        fallback = self._fallback()
        findings = [issue_entry(n=i) for i in range(3)]
        mine = fallback.derive_fallback_state(
            make_review(findings), "abc123", run_id="run"
        )
        reconciled = upstream.reconcile_review_findings(
            None, findings, allow_resolution=True,
            head_sha="abc123", run_id="run",
        )
        self.assertEqual(
            sorted(entry["finding_id"] for entry in mine["findings"]),
            sorted(entry["finding_id"] for entry in reconciled.state["findings"]),
        )
        marker = upstream.serialize_review_state(mine)
        parsed = upstream.parse_review_state("presentation\n" + marker + "\n")
        self.assertTrue(parsed.present)
        self.assertTrue(parsed.valid)

    def test_malformed_review_fails_closed(self):
        fallback = self._fallback()
        for bad in (
            None,
            "not-a-review",
            ["key_issues_to_review"],
            {"review": "not-an-object"},
            {},
            {"key_issues_to_review": None},
            {"key_issues_to_review": "not-a-list"},
            {"key_issues_to_review": ["not-an-object"]},
            {"key_issues_to_review": [None]},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(fallback.FallbackStateError):
                    fallback.derive_fallback_state(bad, "head")

    def test_unrepresentable_finding_fails_closed(self):
        fallback = self._fallback()
        for bad in (
            {"relevant_file": "", "issue_content": "real body",
             "start_line": 1, "end_line": 1},
            {"relevant_file": "src/app.py", "issue_content": "  ",
             "start_line": 1, "end_line": 1},
            {"relevant_file": "src/app.py", "issue_header": "no body",
             "start_line": 1, "end_line": 1},
            {"issue_content": "no path", "start_line": 1, "end_line": 1},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(fallback.FallbackStateError):
                    fallback.derive_fallback_state(
                        make_review([bad]), "head"
                    )

    def test_fallback_requires_the_exact_reviewed_head(self):
        fallback = self._fallback()
        life = self._life()
        with self.assertRaises(fallback.FallbackStateError):
            fallback.derive_fallback_state(make_review([]), "")
        state = fallback.derive_fallback_state(
            make_review([issue_entry(n=0)]), "AbC", run_id="run"
        )
        self.assertTrue(life.is_same_head(state["last_run"]["head_sha"], "abc"))
        self.assertFalse(life.is_same_head(state["last_run"]["head_sha"], "def"))

    def test_native_state_remains_authoritative(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        # A native marker is authoritative only when it is valid, exact-head,
        # complete/full, and has a valid findings list. Missing/incomplete
        # native state falls back to the validated structured review.
        self.assertIn("parse_review_state", body)
        self.assertIn("derive_fallback_state", body)
        self.assertIn("native_usable = (", body)
        self.assertIn("if native_usable:", body)
        self.assertIn("if state is None:", body)
        parse_at = body.index("parsed = parse_review_state")
        usable_at = body.index("native_usable = (")
        fallback_at = body.index("state = derive_current_fallback()")
        self.assertLess(parse_at, usable_at)
        self.assertLess(usable_at, fallback_at)

    def test_validated_findings_route_reaches_repair(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        policy = read_repo(".github/scripts/pr_agent_policy.js")
        # The persistent export (now with fallback) precedes routing, and one
        # canonical policy decides repair / bounded re-review / merge.
        self.assertLess(
            body.index("Export native persistent finding state"),
            body.index("Route the current PR-Agent result"),
        )
        route = body[body.index("Route the current PR-Agent result"):]
        self.assertIn("policy.reviewDisposition(", route)
        self.assertIn("needs_repair", route)
        self.assertIn("key_issues_to_review", policy)
        self.assertIn("qualifyingImproveSuggestions", policy)
        # Actionable results reach repair; only the explicit ready_to_merge
        # disposition can reach the merge workflow.
        self.assertIn("needs.pr_agent.outputs.needs_repair == 'true'", body)
        self.assertIn("needs.pr_agent.outputs.ready_to_merge == 'true'", body)
        repair_use = body.index(
            "continuum-pr-agent-repair.yml",
            body.index("needs.pr_agent.outputs.needs_repair == 'true'"),
        )
        self.assertLess(
            body.index("needs.pr_agent.outputs.needs_repair == 'true'"), repair_use
        )

    def test_workflow_fails_closed_on_unrepresentable_finding(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("FallbackStateError", body)
        self.assertIn("derive_fallback_state", body)
        self.assertIn("Cannot derive fallback persistent state", body)
        self.assertIn("No validated reviewed HEAD", body)
        self.assertIn("pr_agent_fallback_state.py", body)
        self.assertIn("fallback_pythonpath", body)

    def test_stale_head_is_rejected(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("Persistent state is stale", body)
        self.assertIn("PR head moved during review", body)
        self.assertIn("the review is stale", body)

    def test_improve_only_repair_path_remains_functional(self):
        suggestion = {
            "relevant_file": "src/app.py",
            "one_sentence_summary": "Unclosed resource handle leaks on error",
            "suggestion_content": "Use a context manager so the handle closes on error.",
            "relevant_lines_start": 30,
            "relevant_lines_end": 34,
            "score": 8,
        }
        batch = run_policy(
            "build",
            {
                "review": make_review([]),
                "improve_jsonl": json.dumps(
                    {"payload": {"code_suggestions": [suggestion]}}
                ),
            },
        )
        self.assertEqual(len(batch["items"]), 1)
        self.assertEqual(batch["items"][0]["source"], "improve")

    def test_review_repair_rereview_progression(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # Repair consumes the canonical review/improve outputs and publishes
        # a new HEAD when changes are required.
        self.assertIn("review_json: ${{ needs.pr_agent.outputs.review_json }}", review)
        self.assertIn("REVIEW_JSON: ${{ inputs.review_json }}", repair)
        self.assertIn("git push", repair)
        # The merge gate still requires persistent state for the exact HEAD
        # and stays closed until a clean exact-HEAD review.
        self.assertIn(
            "persistent_state_json: ${{ needs.pr_agent.outputs.persistent_state_json }}",
            review,
        )
        self.assertIn("PR-Agent gate requires native persistent finding state", merge)
        self.assertIn("safe_to_merge", merge)

class SchedulingSemanticsTests(unittest.TestCase):
    """Issue #227: latest-useful-work scheduling without double-queueing.

    No-op issue_comment events must never hold a per-PR lock, superseded
    reviews may be cancelled/coalesced, and in-flight repair publication
    must never be interrupted by a newer review event.
    """

    def _life(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        return life

    def test_noop_comment_events_are_not_actionable(self):
        life = self._life()
        # Owner /review on a PR and workflow_dispatch carry useful work.
        self.assertTrue(
            life.caller_review_event_is_actionable(
                "workflow_dispatch",
            )
        )
        self.assertTrue(
            life.caller_review_event_is_actionable(
                "issue_comment",
                is_pull_request_comment=True,
                actor_is_owner=True,
                comment_body="/review please",
            )
        )
        # Everything else is a no-op: plain comments, non-owner actors,
        # non-PR issues, and unrelated events must not invoke the operation
        # layer, so they can never delay a useful exact-HEAD review.
        self.assertFalse(
            life.caller_review_event_is_actionable(
                "issue_comment",
                is_pull_request_comment=True,
                actor_is_owner=True,
                comment_body="looks good, thanks!",
            )
        )
        self.assertFalse(
            life.caller_review_event_is_actionable(
                "issue_comment",
                is_pull_request_comment=True,
                actor_is_owner=False,
                comment_body="/review",
            )
        )
        self.assertFalse(
            life.caller_review_event_is_actionable(
                "issue_comment",
                is_pull_request_comment=False,
                actor_is_owner=True,
                comment_body="/review",
            )
        )
        self.assertFalse(
            life.caller_review_event_is_actionable(
                "issue_comment",
                is_pull_request_comment=True,
                actor_is_owner=True,
                comment_body="",
            )
        )
        self.assertFalse(life.caller_review_event_is_actionable("schedule"))
        self.assertFalse(life.caller_review_event_is_actionable(""))

    def test_superseded_review_may_be_cancelled_same_head_coalesces(self):
        life = self._life()
        old = "a" * 40
        new = "b" * 40
        # A moved HEAD supersedes older review work: cancel/coalesce it.
        self.assertTrue(life.stale_review_may_be_cancelled(old, new))
        self.assertTrue(life.needs_fresh_review(old, new))
        # Same HEAD duplicates coalesce: exact-HEAD work is idempotent.
        self.assertFalse(life.stale_review_may_be_cancelled(old, old.upper()))
        self.assertFalse(life.needs_fresh_review(old, old.upper()))
        # Missing SHAs never cancel: fail closed, never discard blindly.
        self.assertFalse(life.stale_review_may_be_cancelled("", new))
        self.assertFalse(life.stale_review_may_be_cancelled(old, ""))
        self.assertFalse(life.stale_review_may_be_cancelled(None, new))

    def test_repair_publication_is_non_interruptible(self):
        life = self._life()
        # Repair mutates/publishes under exact-HEAD revalidation plus
        # force-with-lease; an active same-HEAD repair blocks review
        # preemption, while no repair (or a different HEAD) does not.
        self.assertTrue(
            life.repair_is_protected_from_review_preemption(
                repair_active=True, repair_head_sha="a" * 40, review_head_sha="a" * 40
            )
        )
        self.assertTrue(
            life.repair_is_protected_from_review_preemption(repair_active=True)
        )
        self.assertFalse(
            life.repair_is_protected_from_review_preemption(repair_active=False)
        )
        self.assertFalse(
            life.repair_is_protected_from_review_preemption(
                repair_active=True,
                repair_head_sha="a" * 40,
                review_head_sha="b" * 40,
            )
        )
        # The scheduling decision wires all three predicates so none is
        # dead code: non-actionable events ignore, active repair waits,
        # moved HEAD supersedes, same HEAD coalesces, and a current HEAD
        # with no running review proceeds.
        old, new = "a" * 40, "b" * 40
        self.assertEqual(
            life.review_supersession_decision(
                old, new, event_name="schedule"
            )["action"],
            "ignore",
        )
        self.assertEqual(
            life.review_supersession_decision(
                old,
                new,
                repair_active=True,
                repair_head_sha=new,
                review_head_sha=new,
            )["action"],
            "wait",
        )
        self.assertEqual(
            life.review_supersession_decision(old, new)["action"], "supersede"
        )
        self.assertEqual(
            life.review_supersession_decision(old, old)["action"], "coalesce"
        )
        # First-ever review with no running SHA proceeds to admission;
        # a missing current HEAD still fails closed to wait.
        self.assertEqual(
            life.review_supersession_decision("", new)["action"], "proceed"
        )
        self.assertEqual(
            life.review_supersession_decision(None, new)["action"], "proceed"
        )
        self.assertEqual(
            life.review_supersession_decision(old, "")["action"], "wait"
        )
        # Python wiring is a real call site, not documentation-only.
        source = lifecycle_source()
        decision = source.split("def review_supersession_decision", 1)[1]
        self.assertIn("caller_review_event_is_actionable(", decision)
        self.assertIn("stale_review_may_be_cancelled(", decision)
        self.assertIn("repair_is_protected_from_review_preemption(", decision)
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("force-with-lease", repair)
        self.assertIn("PR branch moved before publish", repair)

    def test_router_holds_no_per_pr_lock(self):
        # Issue #227: the thin router filters before any delay. A
        # workflow-level lock is acquired before the job `if` / early-exit
        # filter, so a plain non-`/review` comment run would queue ahead of
        # a useful dispatch for the same PR. Duplicates coalesce via the
        # operation-key active-run check instead.
        for path in (
            ".github/caller-stubs/continuum-pr-agent-router.yml",
            ".github/workflows/continuum-pr-agent-router.yml",
            ".github/workflows/pr-agent-router.yml",
        ):
            with self.subTest(path=path):
                body = read_repo(path)
                self.assertNotIn("concurrency:", body)
                self.assertNotIn("cancel-in-progress", body)


class ReviewDispositionIntegrationTests(unittest.TestCase):
    """Issues #252/#257: one policy owns repair/merge/re-review routing."""

    def test_policy_disposition_is_total_and_fail_closed(self):
        self.assertEqual(
            run_policy("disposition", {"review": make_review([])}).get("action"),
            "merge",
        )
        self.assertEqual(
            run_policy(
                "disposition",
                {"review": make_review([], recommendation="merge_with_caution")},
            ).get("action"),
            "rereview",
        )
        self.assertEqual(
            run_policy(
                "disposition",
                {"review": make_review([], recommendation="changes_required")},
            ).get("action"),
            "rereview",
        )
        self.assertEqual(
            run_policy(
                "disposition",
                {"review": make_review([issue_entry(n=1)], recommendation="changes_required")},
            ).get("action"),
            "repair",
        )
        suggestion = {
            "relevant_file": "src/app.py",
            "one_sentence_summary": "Fix race",
            "suggestion_content": "Use an atomic update",
            "score": 8,
        }
        self.assertEqual(
            run_policy(
                "disposition",
                {
                    "review": make_review([]),
                    "improve_jsonl": json.dumps(
                        {"payload": {"code_suggestions": [suggestion]}}
                    ),
                },
            ).get("action"),
            "repair",
        )

    def test_route_and_merge_gate_share_the_same_policy(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        recovery = read_repo(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("policy.reviewDisposition(", review)
        self.assertIn("policy.reviewDisposition(", merge)
        self.assertIn("ready_to_merge", review)
        self.assertIn("needs_rereview", review)
        self.assertIn(
            "needs.pr_agent.outputs.ready_to_merge == 'true'",
            review,
        )
        self.assertIn(
            "PR-Agent review complete: blocking; recovery eligible",
            review,
        )
        self.assertIn("blocking; recovery eligible", recovery)
        self.assertIn(
            "PR-Agent blocking review: recovery eligible",
            recovery,
        )
        self.assertNotIn(
            "needs.pr_agent.outputs.needs_repair == 'false'",
            review,
        )

    def test_runtime_fallback_is_loaded_from_continuum_ref(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn(
            "contents/src/continuum/pr_agent_fallback_state.py",
            review,
        )
        self.assertIn('FALLBACK_PYTHONPATH:', review)
        self.assertIn('PYTHONPATH="$FALLBACK_PYTHONPATH" python3', review)
        self.assertIn("return derive_fallback_state(", review)
        self.assertIn("state = derive_current_fallback()", review)
        self.assertNotIn("state = reconciled.state", review)


class IncompleteNativePersistentStateContractTests(unittest.TestCase):
    """Issue #252: an incomplete native marker cannot dead-end routing."""

    def test_incomplete_native_marker_falls_back_to_validated_exact_head_review(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("native_usable = (", body)
        self.assertIn("if native_usable:", body)
        self.assertIn("if state is None:", body)
        self.assertIn("state = derive_current_fallback()", body)
        self.assertIn("last.get(\"complete\") is True", body)
        self.assertIn('str(last.get("kind") or "") == "full"', body)
        self.assertIn(
            "incomplete marker is not authoritative",
            body,
        )

if __name__ == "__main__":
    unittest.main()
