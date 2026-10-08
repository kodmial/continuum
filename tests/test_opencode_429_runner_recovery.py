"""Contract tests for OpenCode 429 runner-death recovery (kodmial/continuum#290).

A model 429 is runner death, not task failure: the burned VM must never
invoke the model again, workflow-owned shell code must evacuate state to a
durable checkpoint ref, and the watchdog must dispatch a NEW
workflow_dispatch (never reRunWorkflow) carrying the explicit recovery
identity onto a fresh VM that restores the exact checkpoint and continues
from the last completed stage.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"
STUB = ROOT / ".github" / "caller-stubs" / "continuum-opencode.yml"


class OpenCode429RunnerRecoveryContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = WORKFLOW.read_text(encoding="utf-8")
        cls.watchdog = WATCHDOG.read_text(encoding="utf-8")
        cls.stub = STUB.read_text(encoding="utf-8")

    def test_all_agent_invocations_use_rate_limit_wrapper(self):
        self.assertIn("Install Continuum OpenCode rate-limit wrapper", self.body)
        self.assertIn("continuum-opencode github run", self.body)
        self.assertGreaterEqual(
            self.body.count('continuum-opencode run --auto --model "$OPENCODE_MODEL" "$PROMPT"'),
            4,
        )
        active_lines = [
            line.strip()
            for line in self.body.splitlines()
            if line.strip().startswith("opencode run ")
            or line.strip() == "run: opencode github run"
        ]
        self.assertEqual(active_lines, [])

    def test_429_burns_current_vm_immediately(self):
        # The wrapper refuses any second model invocation on a burned runner.
        for marker in (
            "FreeUsageLimitError",
            "continuum-opencode-429",
            "runner already burned",
            "refusing a second model invocation",
            "exit 75",
        ):
            self.assertIn(marker, self.body)
        self.assertIn("restart_required=true", self.body)
        self.assertIn("Publish OpenCode 429 recovery artifact", self.body)
        # No sleep/backoff retry of the model on the same VM: the burned
        # runner exits through the dedicated infrastructure outcome.
        self.assertIn("CONTINUUM_OPENCODE_429_RESTART_REQUIRED", self.body)

    def test_workflow_owned_evacuation_without_llm(self):
        # Shell/workflow code — not the model — persists recoverable state.
        for marker in (
            "Install 429 checkpoint evacuator",
            "continuum_429_evacuate",
            "opencode/429-checkpoint-",
            ".continuum-429-recovery.json",
            "operation_id",
            "head_sha",
            "Never invokes the model",
            "never asks the model to commit",
        ):
            self.assertIn(marker, self.body)
        # Every workflow_dispatch agent mode evacuates on 429.
        self.assertGreaterEqual(
            self.body.count("continuum_429_evacuate \""), 4
        )

    def test_fresh_run_restores_exact_checkpoint_and_skips_completed_stages(self):
        for marker in (
            "Restore 429 recovery checkpoint",
            "recovery_checkpoint_ref",
            "recovery_operation_id",
            "recovery_stage",
            "recovery_generation",
            "recovery_checkpoint_sha",
            "skip_opencode",
        ):
            self.assertIn(marker, self.body)
        # tests-passed/publish-pr checkpoints skip the agent and publish
        # directly instead of calling OpenCode again merely to "finish".
        self.assertIn("tests-passed|publish-pr", self.body)
        self.assertIn("skipping OpenCode", self.body)

    def test_watchdog_dispatches_new_run_instead_of_rerun(self):
        # The watchdog must not use reRunWorkflow as the 429 recovery
        # primitive; it dispatches a new trusted workflow_dispatch carrying
        # the explicit recovery identity.
        self.assertIn("recoverFrom429OnFreshVm", self.watchdog)
        self.assertIn("createWorkflowDispatch", self.watchdog)
        self.assertIn("recovery_operation_id", self.watchdog)
        self.assertIn("recovery_checkpoint_ref", self.watchdog)
        self.assertIn("recovery_checkpoint_sha", self.watchdog)
        self.assertIn("recovery_stage", self.watchdog)
        self.assertIn("recovery_generation", self.watchdog)
        self.assertIn("Evacuated 429 checkpoint", self.watchdog)
        self.assertIn(".continuum-429-recovery.json", self.watchdog)
        # The publish-race path keeps its same-input reRunWorkflow retry; the
        # 429 path must not rerun the same run.
        self.assertIn("CONTINUUM_OPENCODE_PUBLISH_RACE_RETRY_REQUIRED", self.watchdog)
        call_site = self.watchdog.split("if (runnerRestartRequired) {")[-1].split(
            "if (publishRaceRestartRequired) {"
        )[0]
        self.assertIn("recoverFrom429OnFreshVm", call_site)
        self.assertNotIn("reRunWorkflow", call_site)

    def test_429_recovery_is_bounded_and_never_pauses(self):
        self.assertIn("max_429_runner_restarts", self.watchdog)
        self.assertIn("AUTOMATION_WATCHDOG_MAX_429_RUNNER_RESTARTS", self.watchdog)
        self.assertIn("continuum-429-resumption", self.watchdog)
        self.assertIn("continuum-429-backoff", self.watchdog)
        self.assertIn("infrastructure backoff", self.watchdog)
        self.assertIn(
            "needs.opencode.outputs.restart_required != 'true'", self.body
        )
        # The 429 path never consumes the normal retry budget, never
        # applies automation:paused, and never requires manual /oc.
        recovery_fn = self.watchdog.split(
            "async function recoverFrom429OnFreshVm"
        )[-1].split("if (publishRaceRestartRequired)")[0]
        self.assertNotIn("pausedLabel", recovery_fn)
        self.assertNotIn("addLabel(pausedLabel)", recovery_fn)
        self.assertNotIn("retryMarker", recovery_fn)
        self.assertNotIn("previousRetries", recovery_fn)
        self.assertNotIn("reRunWorkflow", recovery_fn)

    def test_429_dedupe_and_opacity(self):
        # Duplicate watchdog events cannot launch competing resumptions.
        self.assertIn("was already resumed", self.watchdog)
        self.assertIn("will not", self.watchdog)
        self.assertIn("create a competing resumption", self.watchdog)
        # Public Parent logs keep Child identities opaque: the 429 recovery
        # comments carry numbers/operation ids, never repository names.
        recovery_fn = self.watchdog.split(
            "async function recoverFrom429OnFreshVm"
        )[-1].split("if (publishRaceRestartRequired)")[0]
        self.assertNotIn("repoFullName", recovery_fn)
        self.assertNotIn("${owner}/${repo}", recovery_fn)

    def test_publish_race_recovery_reruns_same_workflow_with_preserved_inputs(self):
        for marker in (
            "CONTINUUM_OPENCODE_PUBLISH_RACE_RETRY_REQUIRED",
            "publishRaceRestartRequired",
            "maxPublishRaceRestarts = 3",
            "reRunWorkflow",
            "with preserved inputs",
            "before issue-number routing",
        ):
            self.assertIn(marker, self.watchdog)

        self.assertIn(
            "CONTINUUM_OPENCODE_PUBLISH_RACE_RETRY_REQUIRED",
            self.body,
        )
        self.assertIn("exit 76", self.body)
        self.assertIn('REPAIR_BASE_SHA="$(git rev-parse HEAD)"', self.body)
        self.assertIn('git fetch --no-tags origin "${HEAD_REF}"', self.body)
        self.assertIn('REMOTE_HEAD="$(git rev-parse "origin/${HEAD_REF}")"', self.body)
        self.assertIn('git push origin "HEAD:${HEAD_REF}"', self.body)
        self.assertNotIn("git rebase", self.body)

    def test_recovery_inputs_are_a_published_contract(self):
        # New workflow_call inputs must be mirrored by the caller stub
        # (dispatch inputs + with forwarding) per the callable contract.
        for name in (
            "recovery_operation_id",
            "recovery_checkpoint_ref",
            "recovery_checkpoint_sha",
            "recovery_stage",
            "recovery_generation",
        ):
            self.assertIn(name, self.body)
            self.assertIn(name, self.stub)
            self.assertIn("inputs.{}".format(name), self.stub)


if __name__ == "__main__":
    unittest.main()
