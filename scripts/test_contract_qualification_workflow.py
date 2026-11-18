"""Executable contract for the production Layer 2 qualification step (#480).

The pure unit suites in ``tests/test_contract_qualification.py`` and
``tests/test_contract_observation.py`` prove the helper decisions. They do not
execute the ``Run Layer 2 lifecycle integration probe`` step from
``.github/workflows/continuum-contract-qualification.yml``, so a regression in
its shell/``gh``/Python orchestration could still pass every helper test.

These tests extract that *exact* step by name from the production workflow and
run it as GitHub Actions would (``bash -e -o pipefail``) with an isolated
``gh`` on ``PATH`` and a minimal environment that carries no live workflow
context. Only the external GitHub boundary is faked; the observation and
qualification modules are the real ``src/continuum`` code. No network, dispatch,
issue write or sleep is involved.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

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

#: Fields the production inventory queries must request, keyed by the command
#: that issues them. Real ``gh`` omits every field absent from ``--json``, so
#: dropping any one of these silently narrows the observation contract.
INVENTORY_FIELDS = {
    "gh issue list": (
        "number",
        "state",
        "labels",
        "createdAt",
        "updatedAt",
        "closedAt",
        "body",
        "title",
    ),
    "gh run list": (
        "databaseId",
        "status",
        "conclusion",
        "createdAt",
        "startedAt",
        "headBranch",
        "displayTitle",
        "name",
    ),
    "gh pr list": (
        "number",
        "state",
        "body",
        "title",
        "headRefName",
        "updatedAt",
        "mergedAt",
        "reviewDecision",
    ),
}

#: The only inherited variable. Everything else is set explicitly below so the
#: live workflow context (``GITHUB_*``/``GH_*`` tokens, SHAs and event paths)
#: cannot leak into the harness and authenticate against real GitHub.
INHERITED_ENV = ("PATH",)

#: GitHub variables the production step legitimately needs, with fixture values.
FIXTURE_GITHUB_ENV = frozenset({"GITHUB_REPOSITORY", "GH_TOKEN"})

#: Inventory window the production queries must keep at minimum. The fake
#: ``gh`` guard rejects anything narrower; a wider window observes the same
#: fields and is therefore still the same contract.
MIN_INVENTORY_LIMIT = 200

#: Variables the Actions runner itself injects into every step, so the workflow
#: never declares them in ``env:``. Their fixture values are harness-owned:
#: a step that starts depending on a new ambient variable must add it here.
AMBIENT_RUNNER_ENV = frozenset(
    {"GITHUB_REPOSITORY", "HOME", "PATH", "RUNNER_TEMP", "TMPDIR"}
)

#: Fixture values for the variables the Layer 2 step declares in ``env:``. The
#: table is checked against the workflow in both directions (see
#: :func:`layer2_env_fixtures`), so renaming or removing a declaration fails the
#: harness instead of silently keeping a stale hardcoded name green.
DECLARED_ENV_FIXTURES = {
    "GH_TOKEN": "fixture-token-not-used",
    "TARGET_SHA": TARGET_SHA,
}

#: Harness plumbing, not step inputs. These never appear in the workflow.
HARNESS_INTERNAL_ENV = frozenset(
    {
        "FAKE_GH_FAIL",
        "FAKE_GH_STATE",
        "LANG",
        "LC_ALL",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
    }
)

#: Step inputs consumed by a tool rather than named in the script text. ``gh``
#: reads ``GH_TOKEN`` from the environment itself, so a text scan cannot see it.
TOOL_IMPLICIT_ENV = frozenset({"GH_TOKEN"})

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
#: Minimum list window; mirrors ``MIN_INVENTORY_LIMIT`` in the harness module.
MIN_LIMIT = 200


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
    for position, argument in enumerate(args):
        if argument == name:
            try:
                return args[position + 1]
            except IndexError:
                return None
        prefix = name + "="
        if argument.startswith(prefix):
            return argument[len(prefix):]
    return None


def exactly(expected):
    """Accept an option value equal to ``expected``."""

    return lambda value: value == expected


def at_least(minimum):
    """Accept a numeric option value at or above ``minimum``."""

    def check(value):
        try:
            return int(value) >= minimum
        except (TypeError, ValueError):
            return False

    return check


def require_inventory(kind, required_fields, required_options):
    """Refuse a list query that no longer asks for the production contract.

    Options are checked by meaning, not by spelling: ``--state open`` and
    ``--state=open`` are the same request, and a wider ``--limit`` still
    observes every field. Only a missing option, a different filter value or a
    limit below the required floor narrows the observation, so only those
    fail.
    """

    problems = []
    for name, check in required_options:
        value = option(name)
        if value is None or not check(value):
            problems.append("{} {!r}".format(name, value))
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
        (
            ("--state", exactly("open")),
            ("--label", exactly("automation:in-progress")),
            ("--limit", at_least(MIN_LIMIT)),
        ),
    )


def require_run_inventory():
    require_inventory("run list", RUN_FIELDS, (("--limit", at_least(MIN_LIMIT)),))


def require_pull_inventory():
    require_inventory(
        "pr list",
        PULL_FIELDS,
        (("--state", exactly("all")), ("--limit", at_least(MIN_LIMIT))),
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


JOB_ID = "qualify"


def load_layer2_job_and_step(data=None):
    """Return the production job and step objects selected by exact YAML name.

    ``data`` may be a pre-parsed workflow mapping, which lets tests mutate a
    declaration and prove the harness reacts to it.
    """

    if data is None:
        data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = data["jobs"][JOB_ID]
    matches = [step for step in job["steps"] if step.get("name") == STEP_NAME]
    if len(matches) != 1:
        raise AssertionError(
            "expected exactly one '%s' step, found %d" % (STEP_NAME, len(matches))
        )
    step = matches[0]
    if step.get("shell") != "bash":
        raise AssertionError("Layer 2 step no longer uses the bash shell")
    if "run" not in step:
        raise AssertionError("Layer 2 step has no inline run script")
    return job, step


def load_layer2_step() -> dict:
    """Return the production step object selected by its exact YAML name."""

    return load_layer2_job_and_step()[1]


def layer2_declared_env(data=None) -> dict:
    """Return the Layer 2 step's effective ``env:`` declarations.

    Job-level ``env:`` applies to every step, so the step's own declarations
    win over it, as in GitHub Actions.
    """

    job, step = load_layer2_job_and_step(data)
    declared = {}
    declared.update(job.get("env") or {})
    declared.update(step.get("env") or {})
    return declared


def layer2_env_fixtures(declared) -> dict:
    """Return fixture values for every variable the workflow declares.

    Requiring an exact match in both directions is the point: a renamed or
    removed workflow variable leaves a stale fixture entry behind, and a new
    declared variable with no fixture cannot be supplied, so neither case can
    keep the contract green while the production step is broken.
    """

    names = set(declared)
    stale = sorted(set(DECLARED_ENV_FIXTURES) - names)
    if stale:
        raise AssertionError(
            "harness supplies env inputs the Layer 2 step no longer declares: "
            + ", ".join(stale)
        )
    missing = sorted(names - set(DECLARED_ENV_FIXTURES))
    if missing:
        raise AssertionError(
            "Layer 2 step declares env inputs with no harness fixture: "
            + ", ".join(missing)
        )
    return {name: DECLARED_ENV_FIXTURES[name] for name in names}


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


def option_value(arguments, name):
    """Return the value of ``--name``/``--name=value`` from parsed tokens."""

    for position, argument in enumerate(arguments):
        if argument == name:
            if position + 1 >= len(arguments):
                raise AssertionError("%s without a value: %r" % (name, arguments))
            return arguments[position + 1]
        prefix = name + "="
        if argument.startswith(prefix):
            return argument[len(prefix):]
    raise AssertionError("no %s option in %r" % (name, arguments))


#: Shell and ``os.environ`` reads of an environment variable inside a step.
ENV_REFERENCE_PATTERNS = (
    re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"),
    re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"os\.environ\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]"),
    re.compile(r"os\.environ\.get\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"),
)

#: ``NAME=value``/``export NAME=value`` assignments, including the inline
#: ``NAME=value command`` prefix form. These names are script-local, not inputs.
ENV_ASSIGNMENT_PATTERN = re.compile(
    r"(?:^|[\s;(&|])(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)="
)


def environment_reads(script):
    """Return the environment variable names a step script reads."""

    reads = set()
    for pattern in ENV_REFERENCE_PATTERNS:
        reads.update(pattern.findall(script))
    return reads


def environment_assignments(script):
    """Return the variable names a step script assigns itself."""

    return set(ENV_ASSIGNMENT_PATTERN.findall(script))


def requested_json_fields(arguments):
    """Return the ``--json`` field list from a parsed argument vector."""

    for position, argument in enumerate(arguments):
        if argument == "--json":
            if position + 1 >= len(arguments):
                raise AssertionError("--json without a field list: %r" % (arguments,))
            return arguments[position + 1].split(",")
        if argument.startswith("--json="):
            return argument[len("--json="):].split(",")
    raise AssertionError("no --json option in %r" % (arguments,))


def command_line_tokens(script, command):
    """Return the source span and shell tokens of ``command``'s logical line.

    The production step is shell. Locating its arguments by raw string indexing
    breaks on a behavior-preserving reformat (quoting the field list, joining it
    with ``--json=``, or continuing it on the next line), so consume the whole
    logical line instead and tokenise it with :mod:`shlex`, as the shell does.
    Callers then work on arguments rather than on source text.
    """

    start = script.index(command)
    end = start
    quote = ""
    while end < len(script):
        character = script[end]
        if quote:
            if character == "\\" and quote == '"':
                end += 2
                continue
            if character == quote:
                quote = ""
            end += 1
            continue
        if character in "'\"":
            quote = character
            end += 1
            continue
        if character == "\\":
            end += 2
            continue
        if character == "\n":
            break
        end += 1
    tokens = shlex.split(script[start:end].replace("\\\n", " "))
    words = command.split()
    if tokens[: len(words)] != words:
        raise AssertionError(
            "could not parse %r invocation from %r" % (command, script[start:end])
        )
    return start, end, tokens


def json_option_span(script, command):
    """Return ``(marker, fields_start, fields_end, fields)`` for one command."""

    start, end, tokens = command_line_tokens(script, command)
    fields = requested_json_fields(tokens)
    marker = script.index("--json", start, end)
    field_list = ",".join(fields)
    fields_start = script.find(field_list, marker, end)
    if fields_start < 0:
        raise AssertionError(
            "could not locate --json fields for %r in %r" % (command, script[start:end])
        )
    fields_end = fields_start + len(field_list)
    return marker, fields_start, fields_end, fields


def drop_json_field(script, command, field):
    """Return ``script`` with ``field`` removed from the command's ``--json``."""

    _, fields_start, fields_end, fields = json_option_span(script, command)
    if field not in fields:
        raise AssertionError("%r does not request %r" % (command, field))
    remaining = [name for name in fields if name != field]
    if not remaining:
        raise AssertionError("%r must request more than %r" % (command, field))
    return script[:fields_start] + ",".join(remaining) + script[fields_end:]


def reformat_json_option(script, command, style):
    """Rewrite a command's ``--json`` option without changing its meaning."""

    marker, fields_start, fields_end, _ = json_option_span(script, command)
    fields_text = script[fields_start:fields_end]
    if style == "equals":
        option_text = "--json=" + fields_text
    elif style == "quoted":
        option_text = '--json "' + fields_text + '"'
    elif style == "continuation":
        option_text = "--json \\\n  " + fields_text
    else:
        raise AssertionError("unknown --json style %r" % (style,))
    # Replacing from the marker drops any original opening quote, so consume a
    # matching closing quote as well instead of leaving it behind.
    replacement_end = fields_end
    if script[replacement_end:replacement_end + 1] in ("'", '"'):
        replacement_end += 1
    return script[:marker] + option_text + script[replacement_end:]


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
        tmp_dir = workdir / "tmp"
        tmp_dir.mkdir()
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

        # Build a minimal deterministic environment instead of copying the
        # parent one. In CI the real workflow context is present in
        # ``os.environ``; inheriting it would let a direct ``$GITHUB_TOKEN``,
        # ``$GITHUB_SHA`` or ``$GITHUB_EVENT_PATH`` read inside the production
        # step (or ``src/continuum``) authenticate against live GitHub or
        # behave differently in CI than locally. Only ``PATH`` is inherited.
        #
        # Step inputs are derived from the workflow's own ``env:`` declarations
        # instead of a hardcoded dictionary: the harness supplies a fixture for
        # each declared name and refuses to run when the workflow and the
        # fixture table disagree, so a renamed or removed variable cannot keep
        # passing against a stale name.
        declared = layer2_declared_env()
        env = {key: os.environ[key] for key in INHERITED_ENV if key in os.environ}
        env.setdefault("PATH", os.defpath)
        env.update(
            {
                "HOME": str(home),
                "TMPDIR": str(tmp_dir),
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_REPOSITORY": REPOSITORY,
                "FAKE_GH_STATE": str(state),
                "FAKE_GH_FAIL": failure,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
        )
        env.update(layer2_env_fixtures(declared))
        env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
        step_inputs = set(env) - HARNESS_INTERNAL_ENV - AMBIENT_RUNNER_ENV
        if step_inputs != set(declared):
            self.fail(
                "harness supplies environment inputs the Layer 2 step does not "
                "declare: " + ", ".join(sorted(step_inputs - set(declared)))
            )
        leaked = sorted(
            key
            for key in env
            if key.startswith(("GH_", "GITHUB_")) and key not in FIXTURE_GITHUB_ENV
        )
        if leaked:
            self.fail(
                "harness environment leaks live GitHub context: " + ", ".join(leaked)
            )
        self.last_env = env
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
        issue_tokens = shlex.split(issue_list)
        self.assertEqual(option_value(issue_tokens, "--state"), "open")
        self.assertEqual(
            option_value(issue_tokens, "--label"), "automation:in-progress"
        )
        self.assertGreaterEqual(
            int(option_value(issue_tokens, "--limit")), MIN_INVENTORY_LIMIT
        )
        requested_fields = set(requested_json_fields(issue_tokens))
        self.assertLessEqual(
            set(INVENTORY_FIELDS["gh issue list"]),
            requested_fields,
            issue_list,
        )

    def test_inventory_queries_must_request_every_consumed_field(self):
        """Every consumed field must be load-bearing in every list query.

        Real ``gh`` omits fields absent from ``--json``; the harness must show
        the same gap rather than serving a fixture that is richer than the
        query. All three inventory queries are exercised, and every field is
        dropped in turn, including ``labels``: without the guard a dropped
        ``labels`` field would hide a stranded reservation and pass green.
        """

        production = load_layer2_step()["run"]
        for command, fields in INVENTORY_FIELDS.items():
            for field in fields:
                with self.subTest(command=command, field=field):
                    mutated = drop_json_field(production, command, field)
                    self.assertNotEqual(production, mutated)
                    completed = self.run_layer2(
                        issues=[issue()],
                        runs=[live_run()],
                        step_script=mutated,
                    )
                    self.assert_failed_without_pass(completed)
                    self.assertIn("dropped observation contract", completed.stderr)
                    self.assertIn(field, completed.stderr)

    def test_missing_labels_field_fails_closed_instead_of_hiding_a_reservation(self):
        """An unobservable reservation payload must never qualify green.

        The inventory query filters on ``automation:in-progress``, so an open
        issue in the payload is a reservation even when the payload omits its
        ``labels`` array. Production treats that omission as unreadable
        reservation state and fails instead of reporting zero reservations, so
        a relaxed query guard (or a real ``gh``/API change) cannot turn a
        stranded reservation into a silent pass.
        """

        unlabelled = issue()
        unlabelled.pop("labels")
        completed = self.run_layer2(
            issues=[unlabelled],
            runs=[],
            pulls=[],
        )
        self.assert_failed_without_pass(completed)
        self.assertIn(
            "issue inventory omitted labels (reservation state unobservable) for: "
            "{}".format(ISSUE_NUMBER),
            completed.stderr,
        )
        self.assertNotIn("Observed 0 real reservation(s)", completed.stdout)

    def test_behavior_preserving_query_options_keep_the_same_contract(self):
        """A wider limit or ``--option=value`` spelling is not a regression."""

        production = load_layer2_step()["run"]
        reformatted = production.replace("--limit 200", "--limit 500")
        reformatted = reformatted.replace("--state open", "--state=open")
        reformatted = reformatted.replace(
            "--label automation:in-progress", "--label=automation:in-progress"
        )
        self.assertNotEqual(production, reformatted)
        completed = self.run_layer2(
            issues=[issue()],
            runs=[live_run()],
            step_script=reformatted,
        )
        self.assert_passed(completed)

    def test_narrowed_inventory_limit_is_rejected(self):
        """A window below the required floor still narrows the observation."""

        production = load_layer2_step()["run"]
        narrowed = production.replace("--limit 200", "--limit 100")
        self.assertNotEqual(production, narrowed)
        completed = self.run_layer2(
            issues=[issue()],
            runs=[live_run()],
            step_script=narrowed,
        )
        self.assert_failed_without_pass(completed)
        self.assertIn("dropped observation contract", completed.stderr)

    def test_step_reads_only_declared_or_provided_environment(self):
        """Harness env must match the workflow and what the step reads.

        The harness supplies fixtures for the step's declared ``env:`` only. If
        the workflow renames a variable while the script still reads the old
        name, the read has no declaration (and the declaration has no reader),
        so this fails instead of passing against an injected stale name.
        """

        _, step = load_layer2_job_and_step()
        declared = set(layer2_declared_env())
        script = step["run"]
        reads = environment_reads(script) - environment_assignments(script)
        allowed = declared | AMBIENT_RUNNER_ENV | HARNESS_INTERNAL_ENV
        self.assertEqual(
            sorted(reads - allowed),
            [],
            "step reads environment inputs that are neither declared nor provided",
        )
        self.assertEqual(
            sorted(declared - reads - TOOL_IMPLICIT_ENV),
            [],
            "step declares environment inputs it never reads",
        )

    def test_workflow_env_rename_breaks_the_harness(self):
        """A declared-input rename or removal must fail, not stay green."""

        for change in ("rename", "remove"):
            with self.subTest(change=change):
                data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
                _, step = load_layer2_job_and_step(data)
                env = step["env"]
                if change == "rename":
                    env["TARGET_SHA_RENAMED"] = env.pop("TARGET_SHA")
                else:
                    env.pop("GH_TOKEN")
                with self.assertRaises(AssertionError) as caught:
                    layer2_env_fixtures(layer2_declared_env(data))
                expected = (
                    "TARGET_SHA" if change == "rename" else "GH_TOKEN"
                )
                self.assertIn(expected, str(caught.exception))

    def test_json_query_reformatting_keeps_the_same_contract(self):
        """A behavior-preserving ``--json`` reformat must not break the test."""

        production = load_layer2_step()["run"]
        for style in ("equals", "quoted", "continuation"):
            with self.subTest(style=style):
                reformatted = production
                for command in INVENTORY_FIELDS:
                    reformatted = reformat_json_option(reformatted, command, style)
                self.assertNotEqual(production, reformatted)
                completed = self.run_layer2(
                    issues=[issue()],
                    runs=[live_run()],
                    step_script=reformatted,
                )
                self.assert_passed(completed)

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

    def test_harness_does_not_inherit_live_github_context(self):
        """A real CI workflow context must not reach the faked step.

        The fake ``gh`` protects ``gh`` calls, but a direct ``$GITHUB_TOKEN`` or
        ``$GITHUB_EVENT_PATH`` read in the production step or ``src/continuum``
        must not be able to authenticate against live GitHub or observe runner
        state. Patch the parent environment with a live-looking context and
        prove none of it survives into the subprocess.
        """

        live = {
            "GITHUB_TOKEN": "live-workflow-token",
            "GITHUB_SHA": "f" * 40,
            "GITHUB_EVENT_PATH": "/live/event.json",
            "GITHUB_WORKSPACE": "/live/workspace",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ID": "123456",
            "GH_HOST": "github.example",
            "GH_ENTERPRISE_TOKEN": "live-enterprise-token",
            "GH_CONFIG_DIR": "/live/gh",
        }
        with mock.patch.dict(os.environ, live):
            completed = self.run_layer2(issues=[issue()], runs=[live_run()])
        self.assert_passed(completed)
        self.assertEqual(
            {key for key in self.last_env if key.startswith("GITHUB_")},
            {"GITHUB_REPOSITORY"},
        )
        self.assertEqual(
            {key for key in self.last_env if key.startswith("GH_")},
            {"GH_TOKEN"},
        )
        self.assertEqual(self.last_env["GITHUB_REPOSITORY"], REPOSITORY)
        self.assertEqual(self.last_env["GH_TOKEN"], "fixture-token-not-used")
        for key, value in live.items():
            self.assertNotIn(value, self.last_env.values(), key)

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
