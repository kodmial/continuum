"""Contract regression for the reusable Bun binary release adapter."""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-bun-binary.yml"


class BunReleaseWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_exact_build_and_publish_identity_are_available(self):
        for token in (
            "build_argv_json",
            "build_env_json",
            "expected_version",
            "expected_sha256",
            "expected_size_bytes",
            "subprocess.run(argv, env=child_env, check=True)",
            "produced binary SHA-256 does not match expected_sha256",
            "produced binary size does not match expected_size_bytes",
            '"source_sha": "$SOURCE_SHA"',
            '"binary_version": "$BINARY_VERSION"',
        ):
            self.assertIn(token, self.text)

    def test_build_argv_never_uses_shell_execution(self):
        self.assertNotIn("shell=True", self.text)

    def test_the_adapter_is_reachable_only_by_an_explicit_call(self):
        # A release that reacts to a push can react to a push it caused itself.
        # The source repository's release workflow triggers on tags and on main
        # and has to reason at length about why that does not loop; this adapter
        # has no event triggers at all, so the question cannot arise. It is the
        # structural form of "a release must not re-enter the merge reconciler",
        # and the merge reconciler's `workflow_run` allowlist is the other half.
        triggers = self._declared_triggers()
        self.assertEqual(
            set(triggers),
            {"workflow_call"},
            "a release adapter must not subscribe to repository events",
        )

    def test_a_run_of_the_adapter_cannot_appear_in_a_workflow_run_event(self):
        # `workflow_run` names workflows by their `name:`, so the only way this
        # run could re-enter anything is if something subscribed to this file.
        # Nothing may, because a call is the sole trigger and a call produces no
        # `workflow_run` event of its own.
        match = re.search(r"^name:\s*(.+?)\s*$", self.text, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertNotIn("workflow_run", self.text)

    def _declared_triggers(self):
        """Top-level keys of the `on:` block, at the same indent as `workflow_call`.

        Nested keys are deliberately not collected: `inputs:`, `secrets:`, and
        everything under them belong to a trigger, they are not triggers. Only
        two-space indentation counts, so the depth of a trigger's own body
        cannot turn its fields into triggers.
        """

        lines = self.text.splitlines()
        for index, line in enumerate(lines):
            if line.rstrip() != "on:":
                continue
            found = []
            for tail in lines[index + 1 :]:
                if not tail.strip() or tail.lstrip().startswith("#"):
                    continue
                indent = len(tail) - len(tail.lstrip())
                if indent < 2:
                    break
                if indent == 2:
                    found.append(tail.strip().split(":", 1)[0])
            return found
        raise AssertionError("the workflow declares no trigger block")


if __name__ == "__main__":
    unittest.main()
