"""Executable contract for the production Layer 2 qualification step (#480).

The pure unit suites in ``tests/test_contract_qualification.py`` and
``tests/test_contract_observation.py`` prove the helper decisions. They do not
execute the ``Run Layer 2 lifecycle integration probe`` step from
``.github/workflows/continuum-contract-qualification.yml``, so a regression in
its shell/``gh``/Python orchestration could still pass every helper test.

These tests extract that *exact* step by name from the production workflow and
run it as GitHub Actions would (``bash -e -o pipefail``) with an isolated
``gh`` on ``PATH``. Only the external GitHub boundary is faked; the observation
and qualification modules are the real ``src/continuum`` code. No network,
dispatch, issue write or sleep is involved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-contract-qualification.yml"
STEP_NAME = "Run Layer 2 lifecycle integration probe"
TMP_PARENT = ROOT / ".opencode-tmp"
REPOSITORY = "kodmial/continuum"
OWNER = REPOSITORY.split("/", 1)[0]
TARGET_SHA = "4" * 40
ISSUE_NUMBER = 42
RUN_ID = 777
PASS_LINE = "Layer 2 lifecycle probe passed for HEAD " + TARGET_SHA

FAKE_GH = r'''#!/usr/bin/env python3
"""Deterministic stand-in for the GitHub CLI inventory/read boundary."""

import json
import os
import pathlib
import sys

state = pathlib.Path(os.environ["FAKE_GH_STATE"])
args = sys.argv[1:]
with (state / "gh.log").open("a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\n")


ISSUE_FIELDS = (
    "number",
    "state",
    "labels",
    "createdAt",
    "updatedAt",
    "closedAt",
    "body",
    "title",
)
RUN_FIELDS = (
    "databaseId",
    "status",
    "conclusion",
    "createdAt",
    "startedAt",
    "headBranch",
    "displayTitle",
    "name",
)
PULL_FIELDS = (
    "number",
    "state",
    "body",
    "title",
    "headRefName",
    "updatedAt",
    "mergedAt",
    "reviewDecision",
)


def emit(name):
    sys.stdout.write((state / name).read_text(encoding="utf-8"))
    return 0


def emit_requested(name):
    """Emit a fixture carrying only the fields the caller asked ``gh`` for.

    Real ``gh`` omits every field that is missing from ``--json``, so the
    fixture must too: a query that silently drops a field the production step
    consumes has to break here instead of passing against a richer object.
    """

    text = (state / name).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except ValueError:
        # Hand a malformed fixture to the production step unchanged so its own
        # JSON handling fails exactly as it would against a broken ``gh``.
        sys.stdout.write(text)
        return 0
    requested = set((option("--json") or "").split(","))
    if isinstance(payload, list):
        payload = [
            {key: value for key, value in row.items() if key in requested}
            if isinstance(row, dict) else row
            for row in payload
        ]
    sys.stdout.write(json.dumps(payload))
    return 0


def fail(kind):
    sys.stderr.write("fake gh failure %s: %s\n" % (kind, " ".join(args)))
    return 1


def option(name):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None


def require_inventory(kind, required_fields, required_options):
    """Refuse a list query that no longer asks for the production contract."""

    problems = []
    for name, expected in required_options:
        if option(name) != expected:
            problems.append("{} {}".format(name, expected))
    requested_fields = set((option("--json") or "").split(","))
    missing_fields = sorted(set(required_fields) - requested_fields)
    if missing_fields:
        problems.append("--json fields " + ",".join(missing_fields))
    if problems:
        sys.stderr.write(
            "fake gh: %s dropped observation contract (%s): %s\n"
            % (kind, "; ".join(problems), " ".join(args))
        )
        sys.exit(3)


def require_issue_inventory():
    require_inventory(
        "issue list",
        ISSUE_FIELDS,
        (("--state", "open"), ("--label", "automation:in-progress"),
         ("--limit", "200")),
    )


def require_run_inventory():
    require_inventory("run list", RUN_FIELDS, (("--limit", "200"),))


def require_pull_inventory():
    require_inventory(
        "pr list", PULL_FIELDS, (("--state", "all"), ("--limit", "200"))
    )


failure = os.environ.get("FAKE_GH_FAIL", "")
if args[:2] == ["issue", "list"]:
    if failure == "issue-list":
        sys.exit(fail("issue-list"))
    require_issue_inventory()
    sys.exit(emit_requested("issues.json"))
if args[:2] == ["run", "list"]:
    if failure == "run-list":
        sys.exit(fail("run-list"))
    require_run_inventory()
    sys.exit(emit_requested("runs.json"))
if args[:2] == ["pr", "list"]:
    if failure == "pr-list":
        sys.exit(fail("pr-list"))
    require_pull_inventory()
    sys.exit(emit_requested("pulls.json"))
if args[:3] == ["api", "--paginate", "--slurp"]:
    sys.exit(fail("api-comments") if failure == "api-comments" else emit("comments.json"))
if args[:1] == ["api"] and "/actions/runs/" in args[-1]:
    sys.exit(fail("api-run") if failure == "api-run" else emit("actions-run.json"))
sys.stderr.write("fake gh: unexpected invocation: %s\n" % " ".join(args))
sys.exit(2)
'''


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_layer2_step() -> dict:
    """Return the production step object selected by its exact YAML name."""

    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    matches = [
        step
        for step in data["jobs"]["qualify"]["steps"]
        if step.get("name") == STEP_NAME
    ]
    if len(matches) != 1:
        raise AssertionError(
            "expected exactly one '%s' step, found %d" % (STEP_NAME, len(matches))
        )
    step = matches[0]
    if step.get("shell") != "bash":
        raise AssertionError("Layer 2 step no longer uses the bash shell")
    if "run" not in step:
        raise AssertionError("Layer 2 step has no inline run script")
    return step


def issue(number: int = ISSUE_NUMBER, labelled: bool = True) -> dict:
    stamp = now_iso()
    return {
        "number": number,
        "state": "open",
        "labels": [{"name": "automation:in-progress"}] if labelled else [],
        "createdAt": stamp,
        "updatedAt": stamp,
        "closedAt": None,
        "body": "Layer 2 contract fixture",
        "title": "Layer 2 reservation fixture",
    }


def live_run(number: int = ISSUE_NUMBER, run_id: int = RUN_ID) -> dict:
    """Shape produced by ``gh run list --json ...`` for an active worker."""

    return {
        "databaseId": run_id,
        "status": "in_progress",
        "conclusion": "",
        "createdAt": now_iso(),
        "startedAt": now_iso(),
        "headBranch": "opencode/issue{}-fix".format(number),
        "displayTitle": "opencode issue #{}".format(number),
        "name": "OpenCode worker",
    }


def exact_worker_run(number: int = ISSUE_NUMBER, run_id: int = RUN_ID) -> dict:
    """Shape returned by ``gh api repos/.../actions/runs/<id>``."""

    return {
        "id": run_id,
        "databaseId": run_id,
        "path": ".github/workflows/opencode.yml",
        "repository": {"full_name": REPOSITORY},
        "status": "in_progress",
        "conclusion": "",
        "created_at": now_iso(),
        "head_branch": "opencode/issue{}-fix".format(number),
        "display_title": "opencode issue #{}".format(number),
        "name": "OpenCode worker",
    }


def owner_comment(
    number: int = ISSUE_NUMBER, run_id: int = RUN_ID, login: str = OWNER
) -> dict:
    return {
        "user": {"login": login},
        "body": (
            "<!-- continuum-opencode-live repo={} issue={} run={} -->".format(
                REPOSITORY, number, run_id
            )
        ),
    }


class Layer2Harness:
    """Shared subprocess harness; mixes into ``unittest.TestCase`` classes."""

    maxDiff = None

    def run_layer2(
        self,
        issues=(),
        runs=(),
        pulls=(),
        comments=(),
        action_run=None,
        failure="",
        fixture_overrides=None,
        step_script=None,
    ):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="layer2-", dir=str(TMP_PARENT)))
        self.addCleanup(shutil.rmtree, workdir, True)

        state = workdir / "state"
        state.mkdir()
        bin_dir = workdir / "bin"
        bin_dir.mkdir()
        runner_temp = workdir / "runner-temp"
        runner_temp.mkdir()
        home = workdir / "home"
        home.mkdir()
        (state / "gh.log").write_text("", encoding="utf-8")

        fixtures = {
            "issues.json": json.dumps(list(issues)),
            "runs.json": json.dumps(list(runs)),
            "pulls.json": json.dumps(list(pulls)),
            "comments.json": json.dumps([list(comments)] if comments else []),
            "actions-run.json": json.dumps(
                action_run if action_run is not None else exact_worker_run()
            ),
        }
        fixtures.update(fixture_overrides or {})
        for name, body in fixtures.items():
            (state / name).write_text(body, encoding="utf-8")

        fake_gh = bin_dir / "gh"
        fake_gh.write_text(textwrap.dedent(FAKE_GH), encoding="utf-8")
        fake_gh.chmod(0o755)

        step_script = step_script or load_layer2_step()["run"]
        script_path = workdir / "step.sh"
        script_path.write_text(step_script, encoding="utf-8")

        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "GITHUB_REPOSITORY",
                "TARGET_SHA",
                "RUNNER_TEMP",
                "GH_TOKEN",
                "PYTHONPATH",
            }
        }
        env.update(
            {
                "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                "HOME": str(home),
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_REPOSITORY": REPOSITORY,
                "TARGET_SHA": TARGET_SHA,
                "GH_TOKEN": "fixture-token-not-used",
                "FAKE_GH_STATE": str(state),
                "FAKE_GH_FAIL": failure,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
        )
        completed = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-e",
                "-o",
                "pipefail",
                str(script_path),
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        self.last_gh_log = (state / "gh.log").read_text(encoding="utf-8")
        return completed

    def assert_passed(self, completed):
        self.assertEqual(
            completed.returncode,
            0,
            "Layer 2 step failed unexpectedly:\nstdout:\n{}\nstderr:\n{}".format(
                completed.stdout, completed.stderr
            ),
        )
        self.assertIn(PASS_LINE, completed.stdout)
        self.assertNotIn("stranded automation:in-progress", completed.stderr)

    def assert_failed_without_pass(self, completed):
        self.assertNotEqual(
            completed.returncode,
            0,
            "Layer 2 step unexpectedly passed:\nstdout:\n{}".format(completed.stdout),
        )
        self.assertNotIn(PASS_LINE, completed.stdout)


class Layer2WorkflowStepTest(Layer2Harness, unittest.TestCase):
    """Run the unmodified production Layer 2 shell step against fake ``gh``."""

    def test_healthy_live_worker_run_is_accepted(self):
        completed = self.run_layer2(
            issues=[issue()],
            runs=[live_run()],
        )
        self.assert_passed(completed)
        self.assertIn("verdict=progress", completed.stdout)
        self.assertIn("Observed 1 real reservation(s); none stranded.", completed.stdout)
        for command in ("issue list", "run list", "pr list"):
            self.assertIn(command, self.last_gh_log)
        issue_list = next(
            line
            for line in self.last_gh_log.splitlines()
            if line.startswith("issue list ")
        )
        self.assertIn("--state open", issue_list)
        self.assertIn("--label automation:in-progress", issue_list)
        requested_fields = set(issue_list.split("--json ", 1)[1].split(","))
        self.assertLessEqual(
            {
                "number",
                "state",
                "labels",
                "createdAt",
                "updatedAt",
                "closedAt",
                "body",
                "title",
            },
            requested_fields,
            issue_list,
        )

    def test_inventory_queries_must_request_every_consumed_field(self):
        """The fake ``gh`` rejects a list query that dropped a consumed field.

        Real ``gh`` omits fields absent from ``--json``; the harness must show
        the same gap rather than serving a fixture that is richer than the
        query. Both the run and PR inventory queries are exercised.
        """

        production = load_layer2_step()["run"]
        for command, field in (
            ("gh run list", "status"),
            ("gh pr list", "headRefName"),
        ):
            with self.subTest(command=command, field=field):
                start = production.index(command)
                fields_start = production.index("--json ", start) + len("--json ")
                field_line = production[fields_start:].split("\n", 1)[0]
                requested = field_line.split(",")
                self.assertIn(field, requested, field_line)
                mutated = (
                    production[:fields_start]
                    + ",".join(name for name in requested if name != field)
                    + production[fields_start + len(field_line):]
                )
                self.assertNotEqual(production, mutated)
                completed = self.run_layer2(
                    issues=[issue()],
                    runs=[live_run()],
                    step_script=mutated,
                )
                self.assert_failed_without_pass(completed)
                self.assertIn("dropped observation contract", completed.stderr)
                self.assertIn(field, completed.stderr)

    def test_healthy_open_implementation_pr_is_accepted(self):
        completed = self.run_layer2(
            issues=[issue()],
            pulls=[
                {
                    "number": 99,
                    "state": "open",
                    "body": "Closes #{}".format(ISSUE_NUMBER),
                    "title": "Implement issue",
                    "headRefName": "opencode/issue42-fix",
                    "updatedAt": now_iso(),
                    "mergedAt": None,
                    "reviewDecision": "",
                }
            ],
        )
        self.assert_passed(completed)
        self.assertIn("verdict=progress", completed.stdout)
        self.assertIn("Observed 1 real reservation(s); none stranded.", completed.stdout)

    def test_empty_observation_set_is_accepted(self):
        completed = self.run_layer2(issues=[], runs=[], pulls=[])
        self.assert_passed(completed)
        self.assertIn("Observed 0 real reservation(s); none stranded.", completed.stdout)

    def test_stranded_reservation_fails_with_the_issue_id(self):
        # The independent deterministic assertions in the same step still pass;
        # only the real stranded-reservation observation can fail this run.
        completed = self.run_layer2(issues=[issue()], runs=[], pulls=[])
        self.assert_failed_without_pass(completed)
        self.assertIn("verdict=error", completed.stdout)
        self.assertIn(
            "stranded automation:in-progress reservation(s): [{}]".format(
                ISSUE_NUMBER
            ),
            completed.stderr,
        )
        self.assertIn(str(ISSUE_NUMBER), completed.stderr)

    def test_missing_recent_worker_uses_verified_exact_run_fallback(self):
        completed = self.run_layer2(
            issues=[issue()],
            runs=[],
            comments=[owner_comment()],
            action_run=exact_worker_run(),
        )
        self.assert_passed(completed)
        self.assertIn(
            "observed exact issue #{} worker run {} outside recent window".format(
                ISSUE_NUMBER, RUN_ID
            ),
            completed.stdout,
        )
        self.assertIn(
            "api --paginate --slurp repos/{}/issues/{}/comments?per_page=100".format(
                REPOSITORY, ISSUE_NUMBER
            ),
            self.last_gh_log,
        )
        self.assertIn(
            "api repos/{}/actions/runs/{}".format(REPOSITORY, RUN_ID),
            self.last_gh_log,
        )

    def test_unverified_exact_run_fallback_cannot_invent_a_pass(self):
        forged = exact_worker_run()
        forged["path"] = ".github/workflows/not-the-worker.yml"
        completed = self.run_layer2(
            issues=[issue()],
            runs=[],
            comments=[owner_comment()],
            action_run=forged,
        )
        self.assert_failed_without_pass(completed)
        self.assertNotIn("outside recent window", completed.stdout)
        self.assertIn(
            "stranded automation:in-progress reservation(s): [{}]".format(
                ISSUE_NUMBER
            ),
            completed.stderr,
        )

    def test_untrusted_comment_marker_is_not_worker_evidence(self):
        completed = self.run_layer2(
            issues=[issue()],
            runs=[],
            comments=[owner_comment(login="untrusted-contributor")],
            action_run=exact_worker_run(),
        )
        self.assert_failed_without_pass(completed)
        self.assertNotIn("/actions/runs/", self.last_gh_log)
        self.assertIn(
            "stranded automation:in-progress reservation(s): [{}]".format(
                ISSUE_NUMBER
            ),
            completed.stderr,
        )

    def test_unreadable_exact_run_provenance_fails_without_pass(self):
        completed = self.run_layer2(
            issues=[issue()],
            runs=[],
            comments=[owner_comment()],
            failure="api-run",
        )
        self.assert_failed_without_pass(completed)
        self.assertIn("worker liveness evidence unreadable", completed.stderr)

    def test_required_inventory_failure_cannot_qualify(self):
        for failure in ("issue-list", "run-list", "pr-list"):
            with self.subTest(failure=failure):
                completed = self.run_layer2(
                    issues=[issue()],
                    runs=[live_run()],
                    failure=failure,
                )
                self.assert_failed_without_pass(completed)
                self.assertIn(
                    "fake gh failure {}".format(failure),
                    completed.stderr,
                )

    def test_invalid_required_inventory_output_cannot_qualify(self):
        completed = self.run_layer2(
            issues=[issue()],
            runs=[live_run()],
            fixture_overrides={"runs.json": "{not-json"},
        )
        self.assert_failed_without_pass(completed)
        self.assertIn("JSONDecodeError", completed.stderr)


if __name__ == "__main__":
    unittest.main()
