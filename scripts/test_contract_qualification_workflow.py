"""Executable regression contract for the Layer 2 qualification probe (#484).

The contract qualification workflow owns a "Run Layer 2 lifecycle integration
probe" step. This harness reads that step by name from the real workflow and
executes its unmodified shell body in a subprocess, with the real
``continuum.contract_observation`` / ``continuum.contract_qualification``
modules and only the external ``gh`` CLI replaced by a deterministic local
stub. No network calls, dispatches, sleeps or credentials are involved.

The four classes characterized here are existing behavior:

1. a healthy attributable reservation and a valid empty inventory pass;
2. a stranded ``automation:in-progress`` issue with no matching worker, PR or
   lease fails qualification;
3. unavailable exact-run evidence fails closed, while the existing
   owner-marker fallback recovers a still-running worker whose run was evicted
   from the recent-run window;
4. a failing ``gh`` command or malformed JSON is a non-success.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-contract-qualification.yml"
TMP_PARENT = ROOT / ".opencode-tmp"
STEP_NAME = "Run Layer 2 lifecycle integration probe"
FULL_SHA = "a" * 40
REPOSITORY = "kodmial/continuum"

#: The production run body is executed verbatim; only ``gh`` is stubbed.
FAKE_GH = """#!/usr/bin/env bash
set -euo pipefail
fixture_dir="${FAKE_GH_DIR:?}"
fail() {
  echo "gh: $1 unavailable" >&2
  exit 1
}
case "${1:-}" in
  issue)
    [[ "${2:-}" == "list" ]] || { echo "unexpected gh: $*" >&2; exit 1; }
    [[ ! -e "$fixture_dir/fail-issue" ]] || fail "issue list"
    cat "$fixture_dir/issues.json"
    ;;
  run)
    [[ "${2:-}" == "list" ]] || { echo "unexpected gh: $*" >&2; exit 1; }
    [[ ! -e "$fixture_dir/fail-run" ]] || fail "run list"
    cat "$fixture_dir/runs.json"
    ;;
  pr)
    [[ "${2:-}" == "list" ]] || { echo "unexpected gh: $*" >&2; exit 1; }
    [[ ! -e "$fixture_dir/fail-pr" ]] || fail "pr list"
    cat "$fixture_dir/pulls.json"
    ;;
  api)
    shift
    endpoint=""
    for argument in "$@"; do
      endpoint="$argument"
    done
    if [[ "$endpoint" == *"/actions/runs/"* ]]; then
      [[ ! -e "$fixture_dir/fail-run-api" ]] || fail "exact run evidence"
      cat "$fixture_dir/run.json"
    elif [[ "$endpoint" == *"/comments"* ]]; then
      [[ ! -e "$fixture_dir/fail-comments" ]] || fail "issue comments"
      cat "$fixture_dir/comments.json"
    else
      echo "unexpected gh api: $*" >&2
      exit 1
    fi
    ;;
  *)
    echo "unexpected gh invocation: $*" >&2
    exit 1
    ;;
esac
"""


def extracted_layer2_step() -> str:
    """The production run script of the Layer 2 lifecycle probe, unmodified."""

    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in data["jobs"]["qualify"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise AssertionError(f"{STEP_NAME} step not found")


def recent_timestamp() -> str:
    """A timestamp inside the default reservation lease window."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Layer2LifecycleProbeTest(unittest.TestCase):
    """Run the production Layer 2 step against deterministic fake GitHub state."""

    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="layer2-probe-", dir=str(TMP_PARENT))
        )
        self.bin = self.workdir / "bin"
        self.bin.mkdir()
        self.fixtures = self.workdir / "fixtures"
        self.fixtures.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)
        self.script = self.workdir / "layer2-step.sh"
        self.script.write_text(extracted_layer2_step(), encoding="utf-8")
        # An empty, valid observation by default; every test overrides the
        # fixtures it cares about.
        self.write_json("issues.json", [])
        self.write_json("runs.json", [])
        self.write_json("pulls.json", [])
        self.write_json("comments.json", [[]])

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def write_json(self, name: str, payload) -> None:
        (self.fixtures / name).write_text(json.dumps(payload), encoding="utf-8")

    def write_raw(self, name: str, text: str) -> None:
        (self.fixtures / name).write_text(text, encoding="utf-8")

    def fail_command(self, name: str) -> None:
        (self.fixtures / f"fail-{name}").write_text("", encoding="utf-8")

    def labelled_issue(self, number: int = 7) -> dict:
        return {
            "number": number,
            "state": "OPEN",
            "labels": [{"name": "automation:in-progress"}],
        }

    def owner_marker_comment(self, number: int, run_id: int) -> dict:
        return {
            "user": {"login": REPOSITORY.split("/", 1)[0]},
            "body": (
                "<!-- continuum-opencode-live repo={} issue={} run={} -->".format(
                    REPOSITORY, number, run_id
                )
            ),
        }

    def exact_run(
        self,
        number: int,
        run_id: int,
        *,
        status: str = "in_progress",
        path: str = ".github/workflows/opencode.yml",
        repository: str = REPOSITORY,
    ) -> dict:
        return {
            "id": run_id,
            "status": status,
            "path": path,
            "repository": {"full_name": repository},
            "head_branch": "opencode/issue{}-fix".format(number),
            "created_at": recent_timestamp(),
        }

    def invoke(self) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            {
                "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                "GITHUB_REPOSITORY": REPOSITORY,
                "TARGET_SHA": FULL_SHA,
                "RUNNER_TEMP": str(self.workdir / "runner-temp"),
                "GH_TOKEN": "test-token",
                "FAKE_GH_DIR": str(self.fixtures),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        # The stub must shadow any real gh binary; qualification never reaches
        # github.com in this harness.
        resolved = shutil.which("gh", path=env["PATH"])
        self.assertEqual(resolved, str(self.bin / "gh"), env["PATH"])
        return subprocess.run(
            ["bash", str(self.script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def assert_passed(self, completed: subprocess.CompletedProcess) -> str:
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn(
            "Layer 2 lifecycle probe passed for HEAD " + FULL_SHA, output
        )
        return output

    def assert_failed(
        self, completed: subprocess.CompletedProcess, expected: str
    ) -> str:
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn(expected, output)
        self.assertNotIn("Layer 2 lifecycle probe passed", output)
        return output

    # Class 1: healthy attributable reservation and a valid empty inventory.

    def test_valid_empty_inventory_passes_as_zero_reservations(self):
        completed = self.invoke()
        output = self.assert_passed(completed)
        self.assertIn("Observed 0 real reservation(s); none stranded.", output)

    def test_healthy_attributable_reservation_passes(self):
        self.write_json("issues.json", [self.labelled_issue(42)])
        self.write_json(
            "runs.json",
            [
                {
                    "databaseId": 900,
                    "status": "in_progress",
                    "createdAt": recent_timestamp(),
                    "headBranch": "opencode/issue42-test",
                }
            ],
        )
        completed = self.invoke()
        output = self.assert_passed(completed)
        self.assertIn("observed reservation issue=42", output)
        self.assertIn("verdict=progress", output)
        self.assertIn("Observed 1 real reservation(s); none stranded.", output)

    # Class 2: a stranded reservation fails qualification even when unrelated
    # workers and PRs exist for other issues.

    def test_stranded_reservation_without_worker_pr_or_lease_fails(self):
        self.write_json("issues.json", [self.labelled_issue(7)])
        self.write_json(
            "runs.json",
            [
                {
                    "databaseId": 901,
                    "status": "in_progress",
                    "createdAt": recent_timestamp(),
                    "headBranch": "opencode/issue99-other",
                }
            ],
        )
        self.write_json(
            "pulls.json",
            [
                {
                    "number": 99,
                    "state": "OPEN",
                    "body": "Closes #99",
                    "title": "unrelated work",
                }
            ],
        )
        completed = self.invoke()
        output = self.assert_failed(
            completed,
            "stranded automation:in-progress reservation(s): [7]",
        )
        self.assertIn("verdict=error", output)
        self.assertIn("issue=7", output)

    # Class 3: exact-run evidence and the existing fallback handling.

    def test_owner_marker_fallback_recovers_run_outside_recent_window(self):
        self.write_json("issues.json", [self.labelled_issue(7)])
        # The recent-run inventory is truncated and holds no worker for issue 7.
        self.write_json("runs.json", [])
        self.write_json(
            "comments.json", [[self.owner_marker_comment(7, 12345)]]
        )
        self.write_json("run.json", self.exact_run(7, 12345))
        completed = self.invoke()
        output = self.assert_passed(completed)
        self.assertIn(
            "observed exact issue #7 worker run 12345 outside recent window",
            output,
        )
        self.assertIn("verdict=progress", output)
        self.assertIn("Observed 1 real reservation(s); none stranded.", output)

    def test_unavailable_exact_run_evidence_is_non_success(self):
        self.write_json("issues.json", [self.labelled_issue(7)])
        self.write_json("runs.json", [])
        self.write_json(
            "comments.json", [[self.owner_marker_comment(7, 12345)]]
        )
        self.fail_command("run-api")
        completed = self.invoke()
        self.assert_failed(
            completed,
            "worker liveness evidence unreadable",
        )

    def test_unavailable_worker_comment_evidence_is_non_success(self):
        self.write_json("issues.json", [self.labelled_issue(7)])
        self.write_json("runs.json", [])
        self.fail_command("comments")
        completed = self.invoke()
        self.assert_failed(
            completed,
            "worker liveness evidence unreadable",
        )

    def test_untrusted_exact_run_identity_remains_stranded(self):
        self.write_json("issues.json", [self.labelled_issue(7)])
        self.write_json("runs.json", [])
        self.write_json(
            "comments.json", [[self.owner_marker_comment(7, 12345)]]
        )
        # Same id and issue identity, but the fetched Actions run belongs to a
        # different workflow: the fallback must not trust it.
        self.write_json(
            "run.json",
            self.exact_run(7, 12345, path=".github/workflows/other.yml"),
        )
        completed = self.invoke()
        output = self.assert_failed(
            completed,
            "stranded automation:in-progress reservation(s): [7]",
        )
        self.assertIn("verdict=error", output)
        self.assertNotIn("outside recent window", output)

    # Class 4: gh command failures and malformed JSON are non-success.

    def test_gh_issue_list_failure_is_non_success(self):
        self.fail_command("issue")
        completed = self.invoke()
        self.assert_failed(completed, "gh: issue list unavailable")

    def test_gh_run_list_failure_is_non_success(self):
        self.fail_command("run")
        completed = self.invoke()
        self.assert_failed(completed, "gh: run list unavailable")

    def test_malformed_issue_inventory_json_is_non_success(self):
        self.write_raw("issues.json", "{not json")
        completed = self.invoke()
        self.assert_failed(completed, "JSONDecodeError")

    def test_malformed_recent_run_json_is_non_success(self):
        self.write_raw("runs.json", "[")
        completed = self.invoke()
        self.assert_failed(completed, "JSONDecodeError")

    def test_malformed_worker_evidence_json_is_non_success(self):
        self.write_json("issues.json", [self.labelled_issue(7)])
        self.write_json("runs.json", [])
        self.write_raw("comments.json", "not-json")
        completed = self.invoke()
        self.assert_failed(completed, "JSONDecodeError")


if __name__ == "__main__":
    unittest.main()
