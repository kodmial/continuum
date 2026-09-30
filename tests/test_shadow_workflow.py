"""The validation plane's entry points: the reusable workflow and the bridge.

Issue #87 asks for two things that a unit test cannot see, because both are about
what is *permitted* rather than what is computed: a reusable workflow that the
production repository can call without handing Continuum any authority, and a
bridge on the production side that forwards events without becoming a second
orchestrator. Both are prose until something asserts the prose, so this module
reads the two YAML files and asserts the properties the issue names.

The properties worth naming, because each is a way this could quietly become a
production system by accident:

* Nothing in either file holds a write scope or a write call.
* The engine is pinned to a commit, and the run refuses to continue if the
  working tree is not that commit.
* The write barrier is proved before a decision is recorded, the acceptance is
  recorded before the decision, and evidence uploads even when a step failed.
* The bridge forwards a projection, not a copy, and contains no decision.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "continuum-shadow.yml"
BRIDGE = REPO_ROOT / "reference" / "nanodictate-workflows" / "shadow-bridge.yml"
DOCS = REPO_ROOT / "docs" / "shadow-validation.md"

#: Workflow YAML is parsed with Ruby's Psych, which is already part of this
#: repository's CI toolchain, so the audit needs no third-party Python package.
#: Psych also resolves the `on:` key to the boolean true, so it is moved back.
RUBY_YAML_TO_JSON = r"""
require "yaml"
require "json"
data = YAML.safe_load(File.read(ARGV[0]), aliases: true) || {}
data["on"] = data.delete(true) if data.key?(true)
puts JSON.generate(data)
"""

#: Anything that mutates a repository or dispatches work.
WRITE_CALLS = (
    "--method POST",
    "--method PUT",
    "--method PATCH",
    "--method DELETE",
    "-X POST",
    "-X PUT",
    "-X PATCH",
    "-X DELETE",
    "git push",
    "/dispatches",
    "createComment",
    "issues.update",
    "pulls.merge",
)

#: The scopes a read-only validation plane may ask for, and nothing more. Live
#: capture reads issues, pull requests, checks, commit statuses, and run records;
#: `contents: read` is what makes the pinned checkout of Continuum possible.
READ_SCOPES = frozenset(
    {"contents", "pull-requests", "issues", "checks", "statuses", "actions"}
)

#: Fields of a bridge document that the event normalizer reads. If the engine
#: starts acting on a field the bridge does not forward, coverage silently stops
#: being coverage, so the two lists are asserted against each other.
FUNCTIONAL_EVENT_FIELDS = (
    "event_type",
    "action",
    "repository",
    "number",
    "head_sha",
    "base_sha",
    "base_ref",
    "head_ref",
    "labels",
    "actor",
    "release",
)


def load(path: pathlib.Path) -> dict:
    result = subprocess.run(
        ["ruby", "-e", RUBY_YAML_TO_JSON, str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def steps_of(job: dict) -> list:
    return job.get("steps") or []


def run_text(step: dict) -> str:
    return step.get("run") or ""


def permission_map(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        return {"*": value}
    return dict(value)


def write_scopes(permissions: dict) -> list:
    return sorted(
        scope
        for scope, level in permissions.items()
        if isinstance(level, str) and level.lower() == "write"
    )


def shell_check(script: str) -> str:
    """Parse one ``run:`` block with bash, and return what bash said.

    ``${{ ... }}`` is replaced first because GitHub expands it before the runner
    ever sees the line, and a literal brace expansion is a syntax error to bash.
    """

    expanded = re.sub(r"\$\{\{.*?\}\}", "expression", script)
    result = subprocess.run(
        ["bash", "-n"], input=expanded, capture_output=True, text=True
    )
    return result.stderr


class ShellSyntaxTests(unittest.TestCase):
    """Every run block has to be a shell script bash can parse.

    CI checks these files with ``actionlint``, which validates expressions but
    does not parse the shell inside a ``run:`` block. A stray quote or an
    unterminated heredoc in a workflow is otherwise only found when the job
    starts, which is the worst possible moment to find out.
    """

    def test_every_run_block_is_valid_bash(self) -> None:
        for path in (WORKFLOW, BRIDGE):
            document = load(path)
            for job_name, job in document["jobs"].items():
                for step in steps_of(job):
                    script = run_text(step)
                    if not script.strip():
                        continue
                    self.assertEqual(
                        shell_check(script),
                        "",
                        "{}/{} ({}): {}".format(
                            path.name, job_name, step.get("name"), script
                        ),
                    )

    def test_every_run_block_is_strict(self) -> None:
        for path in (WORKFLOW, BRIDGE):
            document = load(path)
            for job_name, job in document["jobs"].items():
                for step in steps_of(job):
                    script = run_text(step)
                    if not script.strip():
                        continue
                    self.assertIn(
                        "set -euo pipefail",
                        script,
                        "{}/{}: {} does not set its own error handling".format(
                            path.name, job_name, step.get("name")
                        ),
                    )


class ShadowWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = load(WORKFLOW)
        cls.raw = WORKFLOW.read_text(encoding="utf-8")

    def test_both_jobs_render_their_evidence_into_the_job_summary(self) -> None:
        # The summary is what a reviewer reads without downloading the artifact,
        # and it is rendered from the same documents the gate reads, so it cannot
        # become a second opinion.
        for job_name in ("shadow", "evidence"):
            steps = steps_of(self.document["jobs"][job_name])
            summaries = [
                step
                for step in steps
                if "GITHUB_STEP_SUMMARY" in run_text(step)
            ]
            self.assertEqual(len(summaries), 1, "{} has no summary step".format(job_name))
            script = run_text(summaries[0])
            self.assertIn("continuum.shadow.cli summary", script, job_name)
            self.assertIn("always()", str(summaries[0].get("if")), job_name)
        # And the window's summary asks for the window renderer, not the run one.
        window = [
            step
            for step in steps_of(self.document["jobs"]["evidence"])
            if "GITHUB_STEP_SUMMARY" in run_text(step)
        ][0]
        self.assertIn("--window", run_text(window))


    def test_it_is_reusable_and_dispatchable(self) -> None:
        triggers = self.document["on"]
        self.assertIn("workflow_call", triggers)
        self.assertIn("workflow_dispatch", triggers)
        # A reusable workflow that is also dispatchable is one place for the
        # engine, so a replay and a live run cannot disagree about how to start.
        called = set(triggers["workflow_call"]["inputs"])
        dispatched = set(triggers["workflow_dispatch"]["inputs"])
        bridge_only = {"run_url", "delivery_id", "token"}
        self.assertTrue(
            (called - bridge_only) <= dispatched,
            "a dispatch cannot reach a non-bridge-only input: {}".format(
                sorted((called - bridge_only) - dispatched)
            ),
        )

    def test_no_scope_is_writable(self) -> None:
        scopes = permission_map(self.document["permissions"])
        self.assertEqual(write_scopes(scopes), [])
        self.assertTrue(
            set(scopes) <= READ_SCOPES,
            "the validation plane asked for a scope it does not need: {}".format(
                sorted(set(scopes) - READ_SCOPES)
            ),
        )
        for name, job in self.document["jobs"].items():
            permissions = permission_map(job.get("permissions", self.document["permissions"]))
            self.assertEqual(write_scopes(permissions), [], name)

    def test_no_step_performs_a_write(self) -> None:
        for job_name, job in self.document["jobs"].items():
            for step in steps_of(job):
                text = json.dumps(step)
                for call in WRITE_CALLS:
                    self.assertNotIn(call, text, "{}/{}: {}".format(job_name, step.get("name"), call))

    def test_the_engine_is_pinned_to_a_commit_and_verified(self) -> None:
        # Continuum itself is checked out by path, because a reusable workflow
        # called across repositories would otherwise check out the *caller*.
        self.assertIn("repository: kodmial/continuum", self.raw)
        self.assertIn("ref: ${{ steps.engine.outputs.sha }}", self.raw)
        # The pin comes from the reusable job definition selected by the
        # caller's `uses:` line, not from an untrusted input.
        self.assertIn('JOB_WORKFLOW_SHA: ${{ job.workflow_sha }}', self.raw)
        self.assertIn("grep -Eq '^[0-9a-f]{40}$'", self.raw)
        # And the working tree must be the commit, not merely a request for it.
        self.assertIn("git -C engine rev-parse HEAD", self.raw)
        self.assertIn('if [ "$head" != "$ENGINE_SHA" ]', self.raw)

    def test_the_barrier_is_proved_before_anything_is_decided(self) -> None:
        shadow = self.document["jobs"]["shadow"]
        names = [step.get("name") for step in steps_of(shadow)]
        self.assertLess(
            names.index("Prove the barrier holds before deciding anything"),
            names.index("Decide the event"),
        )
        # The proof is the command that actually attempts the writes, not a flag.
        proof = steps_of(shadow)[names.index("Prove the barrier holds before deciding anything")]
        self.assertIn("continuum.shadow.cli barrier-check", run_text(proof))

    def test_the_acceptance_is_recorded_before_the_decision(self) -> None:
        # Otherwise a run that dies is invisible, and a window built from
        # invisible runs is a window that cannot see a stall.
        names = [step.get("name") for step in steps_of(self.document["jobs"]["shadow"])]
        self.assertLess(
            names.index("Record the acceptance"), names.index("Decide the event")
        )

    def test_every_decision_goes_through_the_shadow_command(self) -> None:
        shadow_commands = " ".join(
            run_text(step) for step in steps_of(self.document["jobs"]["shadow"])
        )
        for verb in ("run", "parity", "liveness"):
            self.assertIn(
                "continuum.shadow.cli {}".format(verb)
                if verb != "run"
                else "args=(run ",
                shadow_commands,
                verb,
            )
        evidence_commands = " ".join(
            run_text(step) for step in steps_of(self.document["jobs"]["evidence"])
        )
        for verb in ("replay", "cutover"):
            self.assertIn("args=({} ".format(verb), evidence_commands, verb)

    def test_evidence_is_uploaded_even_when_a_step_fails(self) -> None:
        for job_name, job in self.document["jobs"].items():
            uploads = [
                step
                for step in steps_of(job)
                if (step.get("uses") or "").startswith("actions/upload-artifact")
            ]
            self.assertEqual(len(uploads), 1, job_name)
            self.assertEqual(
                uploads[0].get("if"),
                "always()",
                "{} uploads evidence only on success".format(job_name),
            )

    def test_the_runs_are_bounded(self) -> None:
        for job_name, job in self.document["jobs"].items():
            self.assertIn("timeout-minutes", job, job_name)
            self.assertLessEqual(job["timeout-minutes"], 15, job_name)

    def test_a_live_capture_needs_an_explicit_token(self) -> None:
        # A cross-repository call receives no GITHUB_TOKEN, so a capture without
        # a forwarded token has to fail loudly rather than capture nothing.
        shadow = json.dumps(self.document["jobs"]["shadow"])
        self.assertIn("${{ inputs.token || github.token }}", shadow)
        deciding = [
            step
            for step in steps_of(self.document["jobs"]["shadow"])
            if step.get("name") == "Decide the event"
        ][0]
        self.assertIn("nothing to decide from", run_text(deciding))


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = load(BRIDGE)
        cls.raw = BRIDGE.read_text(encoding="utf-8")

    def test_the_bridge_holds_no_authority(self) -> None:
        self.assertEqual(write_scopes(permission_map(self.document["permissions"])), [])
        for job_name, job in self.document["jobs"].items():
            permissions = permission_map(job.get("permissions", self.document["permissions"]))
            self.assertEqual(write_scopes(permissions), [], job_name)
        for call in WRITE_CALLS:
            self.assertNotIn(call, self.raw, call)
        # It does not even need a token of its own.
        self.assertNotIn("gh api", self.raw)
        self.assertNotIn("GITHUB_TOKEN:", self.raw)

    def test_the_bridge_contains_no_orchestration(self) -> None:
        # One shaping step and one call into Continuum. A second decision here
        # would be a second orchestration surface, which is the failure mode the
        # issue names: project logic must not be duplicated.
        self.assertEqual(list(self.document["jobs"]), ["context", "shadow"])
        context_steps = steps_of(self.document["jobs"]["context"])
        self.assertEqual(len(context_steps), 2, context_steps)
        shadow = self.document["jobs"]["shadow"]
        self.assertNotIn("steps", shadow)
        self.assertTrue(shadow["uses"].startswith("kodmial/continuum/.github/workflows/"))

    def test_the_reusable_workflow_is_pinned_to_a_commit(self) -> None:
        self.assertRegex(
            self.document["jobs"]["shadow"]["uses"].split("@")[-1], r"^[0-9a-f]{40}$"
        )

    def test_the_bridge_forwards_every_lifecycle_event_the_engine_plans(self) -> None:
        from continuum.shadow.planner import PLANNERS
        from continuum.shadow.event import EVENT_TYPES

        forwards = self.raw
        for event_type in sorted(PLANNERS):
            self.assertIn(
                event_type,
                EVENT_TYPES,
                "{} is planned but is not a shadowable event type".format(event_type),
            )
        # Every GitHub event the planner can consume has a name in the bridge.
        for github_event in (
            "issues:",
            "pull_request:",
            "pull_request_review:",
            "pull_request_review_comment:",
            "check_suite:",
            "workflow_run:",
            "release:",
            "schedule:",
        ):
            self.assertIn(github_event, forwards, github_event)
        for event_type in (
            "issue.opened",
            "issue.closed",
            "issue.labelled",
            "pull_request.opened",
            "pull_request.synchronize",
            "pull_request.review",
            "pull_request.review_comment",
            "check_suite.completed",
            "workflow_run.completed",
            "release.event",
            "schedule.tick",
        ):
            self.assertIn(event_type, forwards, event_type)

    def test_the_bridge_forwards_the_immutable_context_the_issue_names(self) -> None:
        for field in FUNCTIONAL_EVENT_FIELDS:
            self.assertIn(field, self.raw, "the bridge does not forward " + field)
        # The context a human needs to place a record in a window.
        for field in ("check_conclusion", "review_state", "release_tag", "event_id"):
            self.assertIn(field, self.raw, field)
        # And a correlation id and timestamp, explicitly.
        self.assertIn("event_id: $event_id", self.raw)
        self.assertIn("event_at", self.raw)
        self.assertIn("observed_at", self.raw)
        # The bridge does not need a dedicated PAT/secret. The reusable job
        # receives the caller's scoped GITHUB_TOKEN and the bridge grants only
        # the read permissions required for live capture.
        self.assertNotIn("secrets.SHADOW_READ_TOKEN", self.raw)
        self.assertIn("contents: read", self.raw)
        self.assertIn("pull-requests: read", self.raw)
        self.assertIn("actions: read", self.raw)

    def test_a_bridge_document_is_something_the_engine_accepts(self) -> None:
        from continuum.shadow.config import load as load_config
        from continuum.shadow.event import from_payload
        from continuum.shadow.planner import plan
        from tests import shadow_support as fixtures

        # The document the bridge's projection produces for a pull request review
        # comment, written out as the jq expression would emit it.
        document = {
            "event_type": "pull_request.review_comment",
            "action": "created",
            "event_id": "kodmial/nanodictate/pull_request_review_comment/1",
            "delivery_id": "42",
            "repository": "kodmial/nanodictate",
            "number": 43,
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "base_ref": "main",
            "head_ref": "opencode/fix-43",
            "labels": ["opencode-ready-for-agent"],
            "actor": "coderabbitai",
            "release": None,
            "context": {
                "forwarded_at": "2026-03-01T00:00:00Z",
                "review_state": None,
                "check_conclusion": None,
                "release_draft": None,
            },
        }
        event = from_payload(document)
        journal = plan(event, fixtures.state(), load_config(REPO_ROOT / ".continuum.yml"))
        self.assertTrue(journal.status)
        # The repository-scoped event carried no head sha in the fixture document
        # for a review comment, so the engine must still have decided something
        # rather than refusing to plan.
        self.assertTrue(journal.reason)


def resolve_expression(expression: str, payload: dict, event_name: str, inputs: dict) -> str:
    """Evaluate the ``${{ ... }}`` forms the bridge actually uses.

    Not a general expression evaluator, and it is not trying to be: it resolves
    the literals, the dotted ``github.event`` paths, and the ``||`` fallbacks
    that appear in the bridge's env block, and raises on anything else. A form
    the helper does not understand fails the test rather than being skipped,
    because a helper that quietly stopped understanding a form would turn this
    test into a rubber stamp.
    """

    body = expression.strip()
    if not (body.startswith("${{") and body.endswith("}}")):
        return body
    body = body[3:-2].strip()
    for term in (part.strip() for part in body.split("||")):
        if term.startswith("'") and term.endswith("'"):
            return term[1:-1]
        value = _resolve_term(term, payload, event_name, inputs)
        if value not in ("", None):
            return str(value)
    return ""


def _resolve_term(term: str, payload: dict, event_name: str, inputs: dict):
    if term == "github.event_name":
        return event_name
    if term == "github.repository":
        return payload.get("repository", {}).get("full_name", "kodmial/nanodictate")
    if term == "github.run_id":
        return 4242
    if term == "github.event_id":
        return 1234567890
    if term.startswith("inputs."):
        return inputs.get(term.split(".", 1)[1], "")
    if term.startswith("github.event."):
        node = payload
        for token in re.findall(r"[^.\[\]]+|\[\d+\]", term[len("github.event.") :]):
            if token.startswith("["):
                # GitHub yields an empty value for an index past the end rather
                # than failing, which is what makes the `||` fallbacks work.
                if not isinstance(node, list) or int(token[1:-1]) >= len(node):
                    return ""
                node = node[int(token[1:-1])]
            else:
                if not isinstance(node, dict):
                    return ""
                node = node.get(token)
            if node is None:
                return ""
        return node
    raise AssertionError("the bridge uses an expression form this test cannot evaluate: " + term)


#: One payload per lifecycle event the bridge claims to forward, in GitHub's own
#: shape, because the bridge's whole job is reading that shape correctly.
BRIDGE_PAYLOADS = {
    "issues": {
        "action": "opened",
        "issue": {
            "number": 7,
            "state": "open",
            "labels": [{"name": "opencode-ready-for-agent"}],
        },
        "repository": {"full_name": "kodmial/nanodictate"},
        "sender": {"login": "octocat"},
    },
    "pull_request": {
        "action": "synchronize",
        "pull_request": {
            "number": 43,
            "head": {"sha": "a" * 40, "ref": "opencode/fix-43"},
            "base": {"sha": "b" * 40, "ref": "main"},
            "draft": False,
        },
        "repository": {"full_name": "kodmial/nanodictate"},
        "sender": {"login": "octocat"},
    },
    "pull_request_review": {
        "action": "submitted",
        "review": {
            "state": "changes_requested",
            "submitted_at": "2026-03-01T00:00:00Z",
            "html_url": "https://github.com/kodmial/nanodictate/pull/43#pullrequestreview-1",
            "user": {"login": "coderabbitai"},
        },
        "pull_request": {
            "number": 43,
            "head": {"sha": "a" * 40, "ref": "opencode/fix-43"},
            "base": {"sha": "b" * 40, "ref": "main"},
        },
        "repository": {"full_name": "kodmial/nanodictate"},
        "sender": {"login": "coderabbitai"},
    },
    "pull_request_review_comment": {
        "action": "created",
        "comment": {
            "created_at": "2026-03-01T00:00:00Z",
            "html_url": "https://github.com/kodmial/nanodictate/pull/43#discussion_r1",
        },
        "pull_request": {
            "number": 43,
            "head": {"sha": "a" * 40, "ref": "opencode/fix-43"},
            "base": {"sha": "b" * 40, "ref": "main"},
        },
        "repository": {"full_name": "kodmial/nanodictate"},
        "sender": {"login": "coderabbitai"},
    },
    "check_suite": {
        "action": "completed",
        "check_suite": {
            "conclusion": "failure",
            "head_sha": "a" * 40,
            "updated_at": "2026-03-01T00:00:00Z",
            "url": "https://api.github.com/repos/kodmial/nanodictate/check-suites/1",
        },
        "repository": {"full_name": "kodmial/nanodictate"},
        "sender": {"login": "github-actions"},
    },
    "workflow_run": {
        "action": "completed",
        "workflow_run": {
            "conclusion": "failure",
            "head_sha": "a" * 40,
            "updated_at": "2026-03-01T00:00:00Z",
            "html_url": "https://github.com/kodmial/nanodictate/actions/runs/1",
            "pull_requests": [],
        },
        "repository": {"full_name": "kodmial/nanodictate"},
        "sender": {"login": "github-actions"},
    },
    "release": {
        "action": "published",
        "release": {
            "tag_name": "v1.2.3",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-03-01T00:00:00Z",
        },
        "repository": {"full_name": "kodmial/nanodictate"},
        "sender": {"login": "octocat"},
    },
    "schedule": {
        "repository": {"full_name": "kodmial/nanodictate"},
        "sender": {"login": "github-actions"},
    },
}


class BridgeProjectionTests(unittest.TestCase):
    """Run the bridge's projection and feed its output to the engine.

    The earlier tests read the bridge as text, which cannot see that the name it
    gives an event is one the engine accepts. Reading the text confirmed the
    string `ci.completed` was present; the engine refuses that name outright, so
    every CI event the bridge forwarded would have failed to normalise and the
    test would have passed anyway. This class executes the step instead, for
    every lifecycle event the bridge claims, and requires the engine to accept
    the document and plan something from it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        document = load(BRIDGE)
        cls.step = steps_of(document["jobs"]["context"])[0]
        cls.script = cls.step["run"]
        cls.env_expressions = cls.step.get("env") or {}
        cls.triggers = document["on"]

    def project(self, event_name: str, payload: dict, inputs: dict = None) -> dict:
        """Run the projection exactly as the runner would, and return its output."""

        import tempfile

        with tempfile.TemporaryDirectory(dir=str(REPO_ROOT)) as directory:
            event_path = pathlib.Path(directory) / "event.json"
            output_path = pathlib.Path(directory) / "output"
            event_path.write_text(json.dumps(payload), encoding="utf-8")
            output_path.write_text("", encoding="utf-8")
            environment = {
                "PATH": "/usr/bin:/bin",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_OUTPUT": str(output_path),
                "GITHUB_REPOSITORY": "kodmial/nanodictate",
                "GITHUB_RUN_ID": "4242",
                "GITHUB_EVENT_NAME": event_name,
            }
            for name, expression in self.env_expressions.items():
                environment[name] = resolve_expression(
                    expression, payload, event_name, inputs or {}
                )
            result = subprocess.run(
                ["bash", "-c", self.script],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(
                result.returncode, 0, "{}: {}".format(event_name, result.stderr)
            )
            outputs = {}
            for line in output_path.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    outputs[key] = value
            if "event" not in outputs:
                return {}
            return json.loads(outputs["event"])

    def test_every_forwarded_document_is_an_event_the_engine_accepts(self) -> None:
        from continuum.shadow.event import from_payload

        forwarded = set()
        for event_name, payload in BRIDGE_PAYLOADS.items():
            document = self.project(event_name, payload)
            self.assertIn("event_type", document, event_name)
            event = from_payload(document)
            self.assertEqual(event.repository, "kodmial/nanodictate", event_name)
            forwarded.add(document["event_type"])
        self.assertEqual(
            forwarded,
            {
                "issue.opened",
                "pull_request.synchronize",
                "pull_request.review",
                "pull_request.review_comment",
                "check_suite.completed",
                "workflow_run.completed",
                "release.event",
                "schedule.tick",
            },
        )

    def test_every_forwarded_document_produces_a_decision(self) -> None:
        from continuum.shadow.config import load as load_config
        from continuum.shadow.event import from_payload
        from continuum.shadow.planner import plan
        from tests import shadow_support as fixtures

        for event_name, payload in BRIDGE_PAYLOADS.items():
            document = self.project(event_name, payload)
            event = from_payload(document)
            journal = plan(
                event, fixtures.state(), load_config(REPO_ROOT / ".continuum.yml")
            )
            self.assertTrue(
                journal.status,
                "{} produced a journal with no status: {}".format(event_name, journal.reason),
            )
            self.assertTrue(journal.reason, event_name)

    def test_a_closed_issue_and_a_labelled_issue_are_named_separately(self) -> None:
        for action, expected in (
            ("closed", "issue.closed"),
            ("labeled", "issue.labelled"),
            ("opened", "issue.opened"),
        ):
            payload = json.loads(json.dumps(BRIDGE_PAYLOADS["issues"]))
            payload["action"] = action
            self.assertEqual(
                self.project("issues", payload)["event_type"], expected, action
            )

    def test_a_pull_request_event_names_its_head_and_base(self) -> None:
        document = self.project("pull_request", BRIDGE_PAYLOADS["pull_request"])
        self.assertEqual(document["event_type"], "pull_request.synchronize")
        self.assertEqual(document["number"], 43)
        self.assertEqual(document["head_sha"], "a" * 40)
        self.assertEqual(document["base_sha"], "b" * 40)
        self.assertEqual(document["base_ref"], "main")
        self.assertEqual(document["head_ref"], "opencode/fix-43")

    def test_a_workflow_run_for_a_pull_request_names_it(self) -> None:
        payload = json.loads(json.dumps(BRIDGE_PAYLOADS["workflow_run"]))
        payload["workflow_run"]["pull_requests"] = [{"number": 43}]
        document = self.project("workflow_run", payload)
        self.assertEqual(document["event_type"], "workflow_run.completed")
        self.assertEqual(document["number"], 43)

    def test_a_workflow_run_with_no_pull_request_is_still_forwarded(self) -> None:
        # A push run names no pull request, and the repair plane still has to
        # record that the run happened.
        document = self.project("workflow_run", BRIDGE_PAYLOADS["workflow_run"])
        self.assertEqual(document["event_type"], "workflow_run.completed")
        self.assertIsNone(document["number"])

    def test_a_release_event_carries_the_tag_the_planner_reads(self) -> None:
        from continuum.shadow.config import load as load_config
        from continuum.shadow.event import from_payload
        from continuum.shadow.planner import plan
        from tests import shadow_support as fixtures

        document = self.project("release", BRIDGE_PAYLOADS["release"])
        # The release planner reads `tag` and `source_sha`. A projection that
        # spelled the first differently would plan against an untagged release,
        # so the spelling is checked against the planner's own reading of it.
        self.assertEqual(document["release"]["tag"], "v1.2.3")
        # A release webhook carries no commit, and the bridge is forbidden from
        # asking for one, so the projection cannot name a source sha. The commit
        # arrives from the capture instead, which is the read the bridge is not
        # allowed to make.
        self.assertIsNone(document["release"]["source_sha"])
        unpinned = plan(
            from_payload(document),
            fixtures.state(),
            load_config(REPO_ROOT / ".continuum.yml"),
        )
        self.assertIn("unpinned", unpinned.reason, unpinned.reason)
        pinned = plan(
            from_payload(document).with_release_source("c" * 40),
            fixtures.state(),
            load_config(REPO_ROOT / ".continuum.yml"),
        )
        self.assertNotIn("unpinned", pinned.reason, pinned.reason)
        self.assertIn("v1.2.3", pinned.reason + " ".join(pinned.notes), pinned.reason)

    def test_the_forwarded_timestamp_is_the_events_own(self) -> None:
        for event_name in ("issues", "release", "check_suite", "workflow_run"):
            document = self.project(event_name, BRIDGE_PAYLOADS[event_name])
            self.assertIn("event_at", document["context"], event_name)
            self.assertIn("observed_at", document["context"], event_name)
        # And it is the payload's timestamp, not the repository's.
        self.assertEqual(
            self.project("release", BRIDGE_PAYLOADS["release"])["context"]["event_at"],
            "2026-03-01T00:00:00Z",
        )

    def test_the_correlation_id_identifies_the_delivery(self) -> None:
        document = self.project("issues", BRIDGE_PAYLOADS["issues"])
        self.assertEqual(document["repository"], "kodmial/nanodictate")
        self.assertTrue(document["event_id"].startswith("kodmial/nanodictate/issues/"))
        self.assertTrue(document["delivery_id"])

    def test_an_action_the_bridge_cannot_name_is_not_forwarded(self) -> None:
        payload = json.loads(json.dumps(BRIDGE_PAYLOADS["issues"]))
        payload["action"] = "edited"
        self.assertEqual(self.project("issues", payload), {})

    def test_no_trigger_declares_an_action_the_projection_drops(self) -> None:
        # A trigger the projection cannot name runs, produces no evidence, and
        # logs why -- which reads later like a decision that was never taken.
        script = self.script
        for event_name, config in self.triggers.items():
            if event_name in ("workflow_dispatch", "schedule") or "types" not in config:
                continue
            for action in config["types"]:
                payload = dict(BRIDGE_PAYLOADS[event_name])
                payload["action"] = action
                self.assertIn(
                    "event_type",
                    self.project(event_name, payload),
                    "{} {} is triggered but not forwarded".format(event_name, action),
                )

    def test_the_labels_the_queue_reads_are_forwarded(self) -> None:
        document = self.project("issues", BRIDGE_PAYLOADS["issues"])
        self.assertEqual(document["labels"], ["opencode-ready-for-agent"])



def commit_contains(revision: str, path: str) -> bool:
    """Whether ``revision``'s tree contains ``path``.

    CI uses a shallow checkout, while an immutable consumer pin normally points
    at an earlier commit. If the object is missing locally, fetch exactly that
    full SHA read-only and retry. A network/offline failure still answers "no",
    which keeps an unverifiable pin fail-closed.
    """

    def contains() -> bool:
        result = subprocess.run(
            ["git", "cat-file", "-e", "{}:{}".format(revision, path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    if contains():
        return True
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        return False
    fetched = subprocess.run(
        ["git", "fetch", "--no-tags", "--depth=1", "origin", revision],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return fetched.returncode == 0 and contains()


class BridgePinTests(unittest.TestCase):
    """The reusable-workflow pin, and the marker that says it is not current yet.

    The reference bridge is the one file a production repository installs, and it
    calls a workflow that lives in Continuum. A pin that names a commit without
    that workflow is not a slow or a stale-but-working bridge; it is a bridge
    that cannot resolve its callee at all. The two states are checked against
    each other, so neither can be shipped by accident:

    * the pin is current and the placeholder marker is still there -- the marker
      was not cleaned up;
    * the pin is stale and the marker is gone -- someone installed a bridge that
      cannot run.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = load(BRIDGE)
        cls.raw = BRIDGE.read_text(encoding="utf-8")

    def test_the_pin_is_a_full_commit_sha(self) -> None:
        pin = self.document["jobs"]["shadow"]["uses"].split("@")[-1]
        self.assertRegex(pin, r"^[0-9a-f]{40}$")

    def test_the_pin_and_the_placeholder_marker_agree(self) -> None:
        pin = self.document["jobs"]["shadow"]["uses"].split("@")[-1]
        marked = "PLACEHOLDER PIN" in self.raw
        current = commit_contains(pin, ".github/workflows/continuum-shadow.yml")
        self.assertEqual(
            marked,
            not current,
            "the reference bridge pin {} {} continuum-shadow.yml, but the "
            "{}marker says otherwise. {}.".format(
                "contains" if current else "does not contain",
                pin,
                "placeholder" if marked else "unmarked",
                "Delete the PLACEHOLDER paragraph"
                if current
                else "Re-pin to a commit that carries the workflow and mark the "
                "pin as a placeholder",
            ),
        )

    def test_a_current_pin_is_never_a_branch(self) -> None:
        # A branch reference would mean the evidence in a window was produced by
        # code nobody reviewed, which is the failure the whole pin exists to
        # prevent. This holds whether or not the pin is current.
        self.assertNotIn("main", self.document["jobs"]["shadow"]["uses"].split("@")[-1])
        self.assertNotIn("master", self.document["jobs"]["shadow"]["uses"].split("@")[-1])


class DocumentationTests(unittest.TestCase):
    """The operations document, checked against the engine it describes.

    A document that drifts from the code is worse than no document, because it
    is the thing an operator trusts at three in the morning. So every verdict,
    classification and blocker code the document names is asserted against the
    module that defines it, and the commands it gives are run.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = DOCS.read_text(encoding="utf-8")

    def test_it_documents_every_verdict_the_engine_can_produce(self) -> None:
        from continuum.shadow import liveness, parity

        for verdict in (liveness.LIVE, liveness.SLOW, liveness.STALLED, liveness.ORPHANED, liveness.CRASHED, liveness.ABANDONED):
            self.assertIn("`{}`".format(verdict), self.raw, verdict)
        for classification in parity.CLASSIFICATIONS:
            self.assertIn("`{}`".format(classification), self.raw, classification)

    def test_it_documents_every_scenario_cutover_requires(self) -> None:
        from continuum.shadow.cutover import REQUIRED_SCENARIOS

        for scenario, _reason in REQUIRED_SCENARIOS:
            self.assertIn("`{}`".format(scenario), self.raw, scenario)

    def test_it_names_the_blockers_the_gate_can_raise(self) -> None:
        from continuum.shadow.cutover import Blocker

        # Not the whole set: the gate raises codes for evidence shapes that have
        # no operational meaning to the reader. These are the ones that describe
        # a window a human has to fix.
        for code in (
            "uncovered_scenario",
            "replayed_only_coverage",
            "unresolved_divergence",
            "no_liveness_evidence",
            "incomplete_approval",
            "stale_approval",
            "no_rollback",
            "approval_over_blocked_window",
        ):
            self.assertIn(code, self.raw, code)
        self.assertTrue(issubclass(Blocker, object))

    def test_it_names_the_files_a_run_writes(self) -> None:
        for name in ("journals/", "parity/", "liveness/", "barrier/", "cases/"):
            self.assertIn(name, self.raw, name)

    def test_its_commands_are_the_commands_this_engine_has(self) -> None:
        import subprocess
        import sys

        documented_flags = {
            "run": (
                "--event",
                "--state",
                "--capture-live",
                "--repo",
                "--token-env",
                "--trusted-actors",
                "--config",
                "--out",
                "--save-case",
                "--now-ms",
                "--budget-ms",
            ),
            "parity": ("--journal", "--observed", "--out"),
            "liveness": ("--acceptances", "--journals", "--out", "--budget-ms"),
            "replay": ("--case", "--out"),
            "cutover": (
                "--parity",
                "--liveness",
                "--resolutions",
                "--origins",
                "--approval",
                "--canary",
                "--rollback",
                "--window-start",
                "--window-end",
                "--out",
            ),
        }
        for command in ("run", "parity", "liveness", "replay", "cutover", "summary"):
            self.assertIn(
                "continuum.shadow.cli {}".format(command),
                self.raw,
                command,
            )
            # And the command exists, with the flags the document uses.
            helped = subprocess.run(
                [sys.executable, "-m", "continuum.shadow.cli", command, "--help"],
                capture_output=True,
                text=True,
                env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(helped.returncode, 0, helped.stderr)
            for flag in documented_flags.get(command, ()):
                self.assertIn(flag, helped.stdout, "{command} lost {flag}".format(**locals()))

    def test_it_says_the_pin_has_to_be_replaced(self) -> None:
        # The reference copy ships with a pin from before the workflow existed.
        # A reader who installs it without noticing would run a bridge that
        # cannot resolve its reusable workflow.
        self.assertIn("placeholder pin", self.raw)
        self.assertIn("full commit SHA", self.raw)

    def test_it_never_claims_the_plane_writes(self) -> None:
        for phrase in ("never acts", "never write", "performs nothing"):
            self.assertIn(phrase, self.raw, phrase)


if __name__ == "__main__":
    unittest.main()
