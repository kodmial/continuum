#!/usr/bin/env python3
"""Static audit of how Continuum publishes a release of itself.

`tests/test_publication.py` proves the publication rules offline: an exact tag, a
closed graph, immutability, and never moving a tag. This file proves the wiring
that runs them, which is where a correct rule stops being enforced:

* the release is dispatched by hand and is not reachable any other way, so
  nothing that runs untrusted can publish a version a consumer will pin;
* the commit published is the commit that was validated -- read out of a job
  output, checked out by full SHA, and proved merged;
* publication is gated on the observed immutability setting rather than on
  anything this repository believes about itself;
* nothing in the workflow can move a tag, force anything, or touch a consumer.

Both halves matter and neither is sufficient alone. A workflow that ran the right
checks against the wrong commit publishes a correct answer to the wrong
question, and every assertion here is about the wiring rather than about the
rules, because the rules are already asserted elsewhere.

Run with::

    python3 -m unittest discover -s .github/tests -p 'test_*.py'
"""

import json
import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
SCRIPT_DIR = REPO_ROOT / ".github" / "scripts"

sys.path.insert(0, str(REPO_ROOT / "src"))

#: The one workflow that publishes Continuum.
RELEASE = "publish-continuum.yml"

#: What it may and may not be entered by. Publishing is an operator's decision
#: about a version, so it is dispatched and named; it is deliberately not
#: reusable, because a `workflow_call` entry point would let a consumer's own
#: workflow -- or anything else that can call one -- cut a release.
ALLOWED_TRIGGERS = ("workflow_dispatch",)

RUBY_YAML_TO_JSON = r"""
require "yaml"
require "json"
data = YAML.safe_load(File.read(ARGV[0]), aliases: true) || {}
data["on"] = data.delete(true) if data.key?(true)
puts JSON.generate(data)
"""


def load_document(name: str) -> dict:
    import subprocess

    result = subprocess.run(
        ["ruby", "-e", RUBY_YAML_TO_JSON, str(WORKFLOW_DIR / name)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def jobs(document: dict) -> dict:
    return document.get("jobs") or {}


def steps_of(job: dict) -> list:
    return job.get("steps") or []


def run_text(step: dict) -> str:
    return str(step.get("run") or "")


def step_json(step: dict) -> str:
    """A step as text, so an assertion can cover `env:` as well as `run:`."""

    return json.dumps(step)


def scripts(document: dict) -> str:
    """Every shell body in the workflow, joined, for whole-file assertions."""

    return "\n".join(
        run_text(step) for job in jobs(document).values() for step in steps_of(job)
    )


def step_named(job: dict, needle: str) -> dict:
    for step in steps_of(job):
        if needle.lower() in str(step.get("name") or "").lower():
            return step
    raise AssertionError("no step named like {!r} in {}".format(needle, job.get("name")))


def permission_map(value) -> dict:
    """Normalise `permissions:` whether it was written as a map or a list."""

    if value is None:
        return {}
    if isinstance(value, list):
        return {name: "write" for name in value}
    if value == "write-all":
        return {"*": "write-all"}
    return dict(value)


class ReleaseWorkflowBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = WORKFLOW_DIR / RELEASE
        cls.source = cls.path.read_text(encoding="utf-8")
        cls.document = load_document(RELEASE)
        cls.validate = jobs(cls.document)["validate"]
        cls.publish = jobs(cls.document)["publish"]
        cls.body = scripts(cls.document)


class TriggerTests(ReleaseWorkflowBase):
    def test_publishing_is_dispatched_and_named(self):
        triggers = self.document.get("on") or {}
        self.assertIsInstance(triggers, dict, "the release workflow has no triggers")
        self.assertEqual(
            sorted(triggers), sorted(ALLOWED_TRIGGERS), "a release is cut by hand"
        )

    def test_it_is_not_reusable_by_a_consumer_or_anything_else(self):
        """`workflow_call` would make the release decision reachable from a
        repository Continuum does not control."""

        triggers = self.document.get("on") or {}
        self.assertNotIn("workflow_call", triggers)

    def test_nothing_untrusted_can_publish(self):
        triggers = self.document.get("on") or {}
        for event in (
            "pull_request",
            "pull_request_target",
            "pull_request_review",
            "issue_comment",
            "schedule",
            "push",
        ):
            with self.subTest(event=event):
                self.assertNotIn(event, triggers)

    def test_the_release_is_named_by_the_operator(self):
        inputs = (self.document.get("on") or {})["workflow_dispatch"]["inputs"]
        self.assertIn("version", inputs)
        self.assertTrue(inputs["version"]["required"])
        self.assertEqual(inputs["version"]["type"], "string")

    def test_the_commit_is_named_or_defaults_to_the_dispatched_one(self):
        inputs = (self.document.get("on") or {})["workflow_dispatch"]["inputs"]
        self.assertIn("commit", inputs)
        self.assertFalse(inputs["commit"].get("required", False))
        self.assertEqual(inputs["commit"].get("default", ""), "")


class IdentityTests(ReleaseWorkflowBase):
    def test_the_release_commit_is_checked_out_by_revision(self):
        for name, job in (("validate", self.validate), ("publish", self.publish)):
            with self.subTest(job=name):
                checkout = step_named(job, "check out the release commit")
                self.assertTrue(
                    str(checkout.get("uses") or "").startswith("actions/checkout@"), name
                )
                reference = (checkout.get("with") or {}).get("ref", "")
                self.assertNotIn("@main", str(reference))
                self.assertTrue(
                    "github.sha" in str(reference)
                    or "validate.outputs.commit" in str(reference),
                    "{} checks out {} rather than a named commit".format(name, reference),
                )

    def test_the_full_history_is_fetched_because_the_release_is_about_commits(self):
        """Tags decide which release exists, and the predecessor decides what the
        compatibility section says, so a shallow clone would make both wrong."""

        for name, job in (("validate", self.validate), ("publish", self.publish)):
            with self.subTest(job=name):
                checkout = step_named(job, "check out the release commit")
                self.assertEqual((checkout.get("with") or {}).get("fetch-depth"), 0)

    def test_the_release_commit_must_be_on_the_default_branch(self):
        identity = run_text(step_named(self.validate, "resolve the release identity"))
        self.assertIn("merge-base --is-ancestor", identity)
        self.assertIn("origin/main", identity)

    def test_the_requested_commit_is_the_checked_out_commit(self):
        """Otherwise the operator's input and the published commit could differ,
        and the run log would name one while the release named the other."""

        identity = run_text(step_named(self.validate, "resolve the release identity"))
        self.assertIn("REQUESTED_COMMIT", identity)
        self.assertIn("git rev-parse HEAD", identity)

    def test_dispatch_inputs_are_named_not_interpolated(self):
        """An expression inside a shell body is a value the workflow splices into
        a script; an `env:` entry is a value the script reads. The difference is
        quoting, so it is enforced the same way the agent workflows enforce it."""

        for name, job in (("validate", self.validate), ("publish", self.publish)):
            for step in steps_of(job):
                with self.subTest(job=name, step=step.get("name")):
                    self.assertNotRegex(run_text(step), r"\$\{\{")


class ValidationTests(ReleaseWorkflowBase):
    def test_validation_runs_before_anything_is_written(self):
        self.assertEqual(self.publish["needs"], "validate")
        self.assertEqual(
            self.validate["permissions"], {"contents": "read"}, name := self.validate.get("name")
        )

    def test_the_release_script_runs_against_the_release_commit(self):
        step = step_named(self.validate, "prove the release commit")
        self.assertIn(".github/scripts/release_validation.sh", run_text(step))

    def test_publication_is_gated_on_the_digest_validation_produced(self):
        """The deciding job recomputes the graph, so the only thing tying what
        ships to what was proved is the digest."""

        decide = step_json(step_named(self.publish, "decide"))
        self.assertIn("--expect-digest", decide)
        self.assertIn("needs.validate.outputs.digest", decide)
        self.assertIn("--state", decide)

    def test_the_bytes_that_ship_are_the_bytes_that_were_validated(self):
        validate = step_json(step_named(self.validate, "build the release"))
        decide = step_json(step_named(self.publish, "decide"))
        self.assertIn("--out-dir", validate)
        self.assertIn("sha256sum", validate)
        self.assertIn("--out-dir", decide)
        self.assertIn("sha256sum -c", decide)
        self.assertIn("needs.validate.outputs.notes", decide)

    def test_the_release_documents_are_derived_from_the_release_commit(self):
        for name, job, needle in (
            ("validate", self.validate, "build the release"),
            ("publish", self.publish, "decide"),
        ):
            with self.subTest(job=name):
                body = run_text(step_named(job, needle))
                for argument in (
                    "--tag",
                    "--commit",
                    "--tree",
                    "--previous-tag",
                    "--tags-file",
                    "--changed-from-file",
                ):
                    self.assertIn(argument, body)


class PublicationTests(ReleaseWorkflowBase):
    def test_immutability_is_observed_not_assumed(self):
        observe = run_text(step_named(self.publish, "observe the repository state"))
        self.assertIn("immutable-releases", observe)
        self.assertIn("gh api", observe)
        self.assertIn('"enabled"', observe)
        self.assertIn("404", observe)

    def test_an_unreadable_setting_is_not_read_as_permission(self):
        observe = run_text(step_named(self.publish, "observe the repository state"))
        self.assertIn("the immutable releases setting could not be read", observe)

    def test_the_tag_is_created_explicitly_at_the_validated_commit(self):
        """`gh release create --target` is ignored when the tag already exists, so
        the tag is created here first: that call fails rather than retargeting."""

        publish = run_text(step_named(self.publish, "publish the release"))
        self.assertIn("git/refs", publish)
        self.assertIn('ref="refs/tags/$RELEASE_TAG"', publish)
        self.assertIn('sha="$RELEASE_COMMIT"', publish)
        self.assertNotIn("--target", publish)
        self.assertNotIn("--verify-tag", publish)

    def test_the_notes_and_the_manifest_are_the_release_itself(self):
        publish = run_text(step_named(self.publish, "publish the release"))
        self.assertIn("release-notes.md", publish)
        self.assertIn("release-manifest.json", publish)
        self.assertIn("--notes-file", publish)

    def test_the_published_release_is_proved_afterwards(self):
        proof = run_text(step_named(self.publish, "prove the published release"))
        self.assertIn("immutable", proof)
        self.assertIn("rev-list", proof)
        self.assertIn("release-manifest.json", proof)
        self.assertIn("cmp", proof)

    def test_a_release_that_is_already_published_changes_nothing(self):
        report = run_text(step_named(self.publish, "already published"))
        self.assertIn("not moved", report)
        self.assertIn("no consumer", report)


class NeverTests(ReleaseWorkflowBase):
    """What the workflow must never do, stated as absences."""

    def test_it_never_moves_or_deletes_a_tag(self):
        for needle in (
            "git tag -f",
            "--force",
            "git update-ref",
            "--delete",
            "git push",
            "gh release delete",
            "gh api --method PATCH",
            "gh api --method DELETE",
        ):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.body)

    def test_it_never_touches_a_consumer_repository(self):
        for needle in (
            "release pin",
            "continuum pin",
            "fixtures/consumer-repo",
            "delegation-parent",
        ):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.body)

    def test_it_holds_no_scope_beyond_the_release(self):
        for name, job in (("validate", self.validate), ("publish", self.publish)):
            with self.subTest(job=name):
                scopes = permission_map(job.get("permissions"))
                self.assertNotEqual(scopes.get("*"), "write-all")
                for scope, level in scopes.items():
                    if level == "write":
                        self.assertEqual(
                            (name, scope),
                            ("publish", "contents"),
                            "{} may not hold {}: write".format(name, scope),
                        )

    def test_the_workflow_document_never_claims_a_wide_default(self):
        scopes = permission_map(self.document.get("permissions"))
        self.assertNotEqual(scopes.get("*"), "write-all")

    def test_it_does_not_generate_notes_behind_the_derived_ones(self):
        self.assertNotIn("--generate-notes", self.body)

    def test_releases_are_serialized_and_never_cancelled(self):
        self.assertFalse(self.document["concurrency"]["cancel-in-progress"])


class ReleaseScriptTests(unittest.TestCase):
    """The release script is a second copy of CI's obligations, so it is checked
    against CI rather than trusted."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.path = SCRIPT_DIR / "release_validation.sh"
        cls.source = cls.path.read_text(encoding="utf-8")
        cls.ci = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")

    def test_it_is_strict_mode(self):
        self.assertIn("set -euo pipefail", self.source)

    def test_all_four_obligations_are_proved(self):
        """Workflow syntax, contracts, fixtures, and entrypoint integrity. A
        release that skipped one of them would publish something CI never
        accepted."""

        for needle in (
            "YAML.parse_file",
            "actionlint",
            "unittest discover -s .github/tests",
            "unittest discover -s tests",
            "config-check",
            "test_continuum_config.py",
            "test_delegation_runtime.py",
            "contract-forbidden-names.txt",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.source)

    def test_the_fixtures_are_validated_as_workflows(self):
        self.assertIn("fixtures/*/.github/workflows/*.yml", self.source)

    def test_the_actionlint_pin_is_the_same_one_ci_pins(self):
        """Two versions would mean two answers to 'is this workflow valid', and
        the release-time answer is the one that matters."""

        # CI writes the pin as a workflow env value, the release script as a shell
        # variable, so both spellings are read rather than assumed.
        def pin(text: str, name: str):
            version = re.search(
                r'(?:ACTIONLINT_VERSION|actionlint_version)\s*[:=]\s*"([0-9.]+)"', text
            )
            digest = re.search(
                r'(?:ACTIONLINT_SHA256|actionlint_sha256)\s*[:=]\s*"([0-9a-f]{64})"', text
            )
            self.assertIsNotNone(version, name + " pins no actionlint version")
            self.assertIsNotNone(digest, name + " pins no actionlint digest")
            return version.group(1), digest.group(1)

        self.assertEqual(
            pin(self.ci, "ci.yml"), pin(self.source, "release_validation.sh")
        )

    def test_the_active_workflow_allowlist_is_the_same_one_ci_enforces(self):
        pattern = re.compile(r"allowed='\^\((?P<names>[^)]*)\)")
        ci = pattern.search(self.ci)
        script = pattern.search(self.source)
        self.assertIsNotNone(ci)
        self.assertIsNotNone(script, "the release script has no workflow allowlist")
        self.assertEqual(ci.group("names"), script.group("names"))

    def test_the_consumer_agnostic_contract_is_checked_the_same_way(self):
        self.assertIn(".github/scripts/continuum_config.py", self.source)
        self.assertIn(".github/scripts/contract-forbidden-names.txt", self.source)


class DocumentNameTests(unittest.TestCase):
    """The filenames the workflow and the module agree on are a contract: a
    manifest published under a name the consumer does not expect is not found."""

    def test_the_workflow_publishes_the_filenames_the_module_defines(self):
        from continuum import publication

        workflow = (WORKFLOW_DIR / RELEASE).read_text(encoding="utf-8")
        for name in (publication.NOTES_NAME, publication.MANIFEST_NAME):
            with self.subTest(name=name):
                self.assertIn(name, workflow)

    def test_the_schema_is_versioned(self):
        from continuum import publication

        self.assertTrue(publication.MANIFEST_SCHEMA.endswith("/v1"))


if __name__ == "__main__":
    unittest.main()