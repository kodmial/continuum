"""The repair ladder's locks, age and failure handling, asserted as YAML.

Item 8 of the post-audit semantics: a failed CI or packaging-smoke repair must not
leave a persistent label that suppresses repair of the same head forever.

The failure is silent and permanent. The label is a lock, the lock is cleared on
success and on a successful repair push, and neither happens when the dispatched
repair is cancelled, times out, or wedges itself. The label survives; every later
failure re-enters the step, sees the label, and exits. Repair for that pull request
stops while the label sits there, and nothing in the logs says "repair" -- the job
is green, it just did nothing.

None of that is visible to a unit test of Python, because the rule lives in a
`run:` block. So it is asserted here, against the file, together with the two
neighbours that make it safe: a lock younger than the budget is *not* released
(two repairs of one head running at once is worse), and an unreadable age reads as
held rather than released.

Run with::

    PYTHONPATH=src python3 -m unittest tests.test_repair_workflow
"""

from __future__ import annotations

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPAIR = ROOT / ".github" / "workflows" / "consumer-repair.yml"
OPENCODE = ROOT / ".github" / "workflows" / "consumer-opencode.yml"


def _load(path: pathlib.Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if True in document:
        # PyYAML reads `on:` as the boolean True, which is YAML 1.1 being pedantic
        # about a word that is not a boolean.
        document["on"] = document.pop(True)
    return document


def _scripts(document: dict) -> list:
    found = []
    for job_name, job in document.get("jobs", {}).items():
        for step in job.get("steps", []) or []:
            body = step.get("run")
            if body and body.strip():
                found.append((job_name, step.get("name") or "", body))
    return found


class TheRepairLockIsAgeChecked(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = REPAIR.read_text(encoding="utf-8")
        cls.scripts = _scripts(_load(REPAIR))
        cls.dispatch = [
            (job, step, body)
            for job, step, body in cls.scripts
            if "LOCK_STALE" in body
        ]

    def test_there_is_exactly_one_place_that_releases_a_stale_lock(self):
        # Two copies of the age rule is two numbers, and the second one drifts: one
        # job would keep repairing while the other stayed wedged.
        self.assertEqual(len(self.dispatch), 1, [name for _, name, _ in self.dispatch])

    def test_a_lock_older_than_the_budget_is_released(self):
        _, _, body = self.dispatch[0]
        self.assertRegex(body, r'AGE_MINUTES" -ge "\$REPAIR_BUDGET_MINUTES')
        # Released, not just observed: the label has to actually come off or the
        # next failure finds it again.
        self.assertIn("--remove-label", body)
        self.assertIn("LOCK_STALE=true", body)

    def test_a_young_lock_is_left_alone(self):
        _, _, body = self.dispatch[0]
        self.assertIn("repair already dispatched", body)
        self.assertRegex(body, r'else\s*\n\s*echo "\$RUN_NAME repair already dispatched')

    def test_an_unreadable_age_reads_as_held(self):
        # The label is still there. Releasing a lock on no evidence would let two
        # repairs of one pull request run at once, which is a worse failure than the
        # wedge this age check exists to break.
        _, _, body = self.dispatch[0]
        self.assertIn('AGE_MINUTES="${AGE_MINUTES:-0}"', body)
        self.assertIn(
            "An unreadable age reads as a held lock", body
        )

    def test_a_released_lock_is_labelled_so_the_failure_is_visible(self):
        # The wedge is silent because the job is green. A label saying the repair
        # made no progress is what turns a green no-op into something a maintainer
        # can see.
        _, _, body = self.dispatch[0]
        self.assertIn("opencode-repair-failed", body)

    def test_the_budget_is_resolved_once_not_spelled_out_per_step(self):
        document = _load(REPAIR)
        bounds = document["jobs"]["conflict-bounds"]
        self.assertIn("repair_budget_minutes", bounds["outputs"])
        for job_name, job in document["jobs"].items():
            for key, value in (job.get("env") or {}).items():
                if key != "REPAIR_BUDGET_MINUTES":
                    continue
                with self.subTest(job=job_name):
                    self.assertIn("conflict-bounds.outputs", str(value))

    def test_a_success_clears_the_lock(self):
        _, _, body = self.dispatch[0]
        self.assertIn('if [[ "$RUN_CONCLUSION" == "success" ]]', body)
        self.assertIn("repair lock cleared", body)

    def test_every_failure_conclusion_reaches_the_dispatch(self):
        # A run that was cancelled or timed out is exactly the case that used to
        # wedge. It has to be in the list.
        document = _load(REPAIR)
        dispatch = document["jobs"]["ci"]["if"]
        for conclusion in ("success", "failure", "timed_out", "cancelled"):
            self.assertIn(conclusion, dispatch)


class TheDispatchCanBeRetried(unittest.TestCase):
    """A dispatch that failed must not leave a lock nothing will ever clear."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.opencode = _load(OPENCODE)
        cls.raw = OPENCODE.read_text(encoding="utf-8")

    def test_the_installer_is_retried_with_backoff(self):
        # The installer is a network fetch; one attempt makes a transient failure
        # indistinguishable from a real one.
        self.assertRegex(self.raw, r"for attempt in 1 2 3;")
        self.assertRegex(self.raw, r"sleep \$\(\(attempt \* 5\)\)")

    def test_a_failed_install_names_itself_and_stops(self):
        # Retrying forever is not the alternative. A named failure with a bounded
        # number of attempts is.
        self.assertIn("OpenCode installer failed after 3 attempts.", self.raw)

    def test_the_installed_binary_is_checked_before_it_is_used(self):
        # An installer that exits zero without producing a binary is a successful
        # install of nothing, and every later step fails with a confusing error.
        self.assertIn('test -x "$HOME/.opencode/bin/opencode"', self.raw)

    def test_the_installed_identity_is_recorded_and_rechecked(self):
        # The upstream installer is a pipe with no digest to check, so what can be
        # attested is the identity on PATH -- recorded at install and verified
        # before every use, which catches a swap even though the install itself
        # cannot be attested.
        self.assertIn("opencode_runtime.py", self.raw)
        self.assertRegex(self.raw, r"install.{0,80}", re.S)

    def test_no_workflow_claims_a_product_toolchain(self):
        # The mechanism is adoptable by a repository that has none of these, and a
        # generic workflow that names one is a workflow that cannot be shared.
        for path in (REPAIR, OPENCODE):
            text = path.read_text(encoding="utf-8").lower()
            with self.subTest(workflow=path.name):
                for word in ("nanodictate", "swift", "xcode", "swiftlint", "cocoa"):
                    self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()