"""Executable deadline tests for the shipped delegated child worker.

The tests extract the deadline configuration and pass-loop functions from the
actual reusable workflow, source the actual checkpoint helper, and run them
against a disposable Git remote and a fake long-running OpenCode. No GitHub,
private provider, token, or model call is required.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-consumer-child-worker.yml"
HELPER = ROOT / ".github" / "scripts" / "delegated_checkpoint.sh"
TMP_PARENT = ROOT / ".opencode-tmp"


def extracted_block(start_marker: str, end_marker: str) -> str:
    body = WORKFLOW.read_text(encoding="utf-8")
    start = body.index(start_marker)
    start = body.rindex("\n", 0, start) + 1
    end = body.index(end_marker, start) + len(end_marker)
    return textwrap.dedent(body[start:end])


def deadline_config() -> str:
    return extracted_block(
        "# BEGIN continuum-delegated-deadline-config",
        "# END continuum-delegated-deadline-config",
    )


def deadline_functions() -> str:
    return extracted_block(
        "# BEGIN continuum-delegated-deadline-functions",
        "# END continuum-delegated-deadline-functions",
    )


class DelegatedSubpassDeadlineTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="deadline-", dir=str(TMP_PARENT)))
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.origin = self.workdir / "origin.git"
        self.bin = self.workdir / "bin"
        self.bin.mkdir()
        self.runner = self.workdir / "runner"
        self.runner.mkdir()
        self.spec = "b" * 64
        self.gh_log = self.workdir / "gh.log"
        self.term_log = self.workdir / "term.log"
        self.counter = self.workdir / "opencode-count"
        self.pid_file = self.workdir / "opencode.pid"
        self.env = {**os.environ}
        self.env.update(
            {
                "PATH": str(self.bin) + os.pathsep + self.env.get("PATH", ""),
                "CHECKPOINT_HELPER": str(HELPER),
                "SPEC": self.spec,
                "TASK_NUMBER": "42",
                "GITHUB_RUN_ID": "999",
                "OPENCODE_MODEL": "test-model",
                "CONTINUUM_ENGINE_ROOT": str(self.workdir / "engine"),
                "RUNNER_TEMP": str(self.runner),
                "MAX_AGENT_PASSES": "4",
                "CONTINUUM_CHECKPOINT_RESTORED": "false",
                "FAKE_GH_LOG": str(self.gh_log),
                "FAKE_TERM_LOG": str(self.term_log),
                "FAKE_COUNTER": str(self.counter),
                "FAKE_PID_FILE": str(self.pid_file),
                "FAKE_RESULT_FILE": str(self.runner / "result.md"),
                "GIT_AUTHOR_NAME": "continuum-test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "continuum-test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
        )
        self.env.pop("CONTINUUM_JOB_STARTED_EPOCH", None)
        self.write_fakes()
        self.run_cmd(self.workdir, "git", "init", "--bare", "-b", "main", str(self.origin))
        seed = self.workdir / "seed"
        self.run_cmd(self.workdir, "git", "init", "-b", "main", str(seed))
        (seed / "README.md").write_text("baseline\n", encoding="utf-8")
        self.run_cmd(seed, "git", "add", ".")
        self.run_cmd(seed, "git", "commit", "-m", "baseline")
        self.run_cmd(seed, "git", "remote", "add", "origin", str(self.origin))
        self.run_cmd(seed, "git", "push", "-u", "origin", "main")
        self.work = self.clone("work")

    def write_fakes(self):
        opencode = self.bin / "opencode"
        opencode.write_text(
            """#!/usr/bin/env bash
set -u
echo $$ > "$FAKE_PID_FILE"
count="$(cat "$FAKE_COUNTER" 2>/dev/null || echo 0)"
count=$((count + 1))
echo "$count" > "$FAKE_COUNTER"
mode="${FAKE_MODE:-sleep}"
if [[ "$mode" == "noop" ]]; then
  exit 0
fi
if [[ "$mode" == "resume" ]]; then
  [[ -f partial-1.txt && -f partial-2.txt ]] || { echo "missing restored work" >&2; exit 9; }
  printf 'resume verified\\n' > resume-verified.txt
  printf 'Acceptance: PASS\\n' > "$FAKE_RESULT_FILE"
  exit 0
fi
printf 'partial %s\\n' "$count" > "partial-$count.txt"
if [[ "$mode" == "pass-then-hang" ]]; then
  printf 'Acceptance: PASS\\n' > "$FAKE_RESULT_FILE"
fi
trap 'echo "$count" >> "$FAKE_TERM_LOG"; sleep 30' TERM
sleep 60
""",
            encoding="utf-8",
        )
        opencode.chmod(0o755)
        python3 = self.bin / "python3"
        python3.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        python3.chmod(0o755)
        gh = self.bin / "gh"
        gh.write_text(
            '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$FAKE_GH_LOG"\nexit 0\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)

    def run_cmd(self, cwd, *args, check=True, env=None, timeout=60):
        return subprocess.run(
            args,
            cwd=cwd,
            env=env or self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    def clone(self, name):
        work = self.workdir / name
        self.run_cmd(self.workdir, "git", "clone", str(self.origin), str(work))
        self.run_cmd(work, "git", "config", "user.name", "continuum-test")
        self.run_cmd(work, "git", "config", "user.email", "test@example.invalid")
        return work

    def invoke(self, cwd, *, budget, min_pass=1, max_subpass=2, grace=1,
               passes=4, prelude="", mode="sleep", result_file=None):
        prompt = self.runner / "prompt.txt"
        prompt.write_text("Execute delegated test task\n", encoding="utf-8")
        env = dict(self.env)
        env["MAX_AGENT_PASSES"] = str(passes)
        env["FAKE_MODE"] = mode
        env["CONTINUUM_JOB_STARTED_EPOCH"] = str(int(time.time()) - 2400 + budget)
        if result_file is not None:
            env["FAKE_RESULT_FILE"] = str(result_file)
        script = "\n".join(
            [
                "set -euo pipefail",
                'source "$CHECKPOINT_HELPER"',
                'continuum_assert_safe_push() { [[ "$1" == continuum-child/* && "$1" != main ]]; }',
                deadline_config(),
                f"min_agent_pass_seconds={min_pass}",
                f"max_subpass_seconds={max_subpass}",
                f"subpass_grace_seconds={grace}",
                deadline_functions(),
                'checkpoint_spec="$SPEC"',
                'checkpoint_ref="$(continuum_checkpoint_ref "$TASK_NUMBER" 1 "$SPEC")"',
                'child_repo="child/example"',
                f'transcript="{self.runner / "agent.log"}"',
                f'prompt="{prompt}"',
                f'result_file="{env["FAKE_RESULT_FILE"]}"',
                prelude,
                "run_rc=0",
                "continuum_run_delegated_passes || run_rc=$?",
                'printf "RUN_RC=%s\\n" "$run_rc"',
                "exit \"$run_rc\"",
            ]
        )
        started = time.monotonic()
        completed = self.run_cmd(cwd, "bash", "-c", script, check=False, env=env, timeout=90)
        return completed, time.monotonic() - started

    def checkpoint_ref(self):
        return f"continuum-child/checkpoint-task-42-g1-{self.spec}"

    def remote_heads(self, cwd):
        return self.run_cmd(cwd, "git", "ls-remote", "--heads", "origin").stdout

    def test_boundary_timeout_terminates_with_grace_saves_and_emits_retry(self):
        completed, elapsed = self.invoke(self.work, budget=12, passes=4)
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 30, output)
        self.assertIn("RUN_RC=30", output)
        self.assertIn(self.checkpoint_ref(), self.remote_heads(self.work))
        term_lines = self.term_log.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(term_lines), 2,
                                f"expected bounded TERM for timed-out passes: {output}")
        partials = sorted(self.work.glob("partial-*.txt"))
        self.assertGreaterEqual(len(partials), 2, "timed-out worktree changes were lost")
        checkpoint_commits = self.run_cmd(
            self.work, "git", "log", "--format=%s", f"origin/{self.checkpoint_ref()}"
        ).stdout
        self.assertIn("checkpoint task #42", checkpoint_commits)
        gh_text = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("<!-- continuum-child-needs-retry -->", gh_text)
        self.assertIn("checkpoint=saved", gh_text)
        self.assertNotIn("pr create", gh_text)
        pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        with self.assertRaises(ProcessLookupError, msg="timed-out agent was left running"):
            os.kill(pid, 0)
        # The inner deadline must fire long before the immutable 45-minute cap.
        self.assertLess(elapsed, 45 * 60)
        self.assertLess(elapsed, 30, f"boundary test took {elapsed:.1f}s")

    def test_second_run_restores_saved_work_without_duplicate_work_or_pr(self):
        first, _ = self.invoke(self.work, budget=8, passes=2)
        self.assertEqual(first.returncode, 30, first.stdout + first.stderr)
        self.assertIn(self.checkpoint_ref(), self.remote_heads(self.work))
        restored = self.clone("restored")
        self.gh_log.write_text("", encoding="utf-8")
        checkpoint_commits_before = self.run_cmd(
            restored, "git", "log", "--format=%s", f"origin/{self.checkpoint_ref()}"
        ).stdout
        self.assertEqual(checkpoint_commits_before.count("checkpoint task #42"), 2)
        second, _ = self.invoke(
            restored,
            budget=20,
            passes=4,
            mode="resume",
            prelude="\n".join(
                [
                    'continuum_checkpoint_restore "$checkpoint_ref" continuum-child/task-42-resume "$SPEC"',
                    '[[ "$CONTINUUM_CHECKPOINT_RESTORED" == true ]]',
                    "export CONTINUUM_CHECKPOINT_RESTORED",
                ]
            ),
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue((restored / "resume-verified.txt").exists())
        self.assertEqual((restored / "partial-1.txt").read_text(encoding="utf-8"), "partial 1\n")
        self.assertEqual((restored / "partial-2.txt").read_text(encoding="utf-8"), "partial 2\n")
        # Acceptance on the restored tree does not publish a checkpoint again.
        checkpoint_commits_after = self.run_cmd(
            restored, "git", "log", "--format=%s", "HEAD"
        ).stdout
        self.assertEqual(checkpoint_commits_after.count("checkpoint task #42"), 2)
        self.assertNotIn("continuum-child-needs-retry", self.gh_log.read_text(encoding="utf-8"))

    def test_timed_out_pass_cannot_claim_acceptance(self):
        completed, _ = self.invoke(
            self.work, budget=6, passes=1, mode="pass-then-hang"
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 30, output)
        gh_text = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("<!-- continuum-child-needs-retry -->", gh_text)
        self.assertIn("result=present-without-PASS", gh_text)
        self.assertIn("checkpoint=saved", gh_text)
        self.assertNotIn("pr create", gh_text)

    def test_zero_code_delta_never_creates_a_checkpoint_or_acceptance(self):
        completed, _ = self.invoke(self.work, budget=20, passes=2, mode="noop")
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 30, output)
        self.assertNotIn(self.checkpoint_ref(), self.remote_heads(self.work))
        self.assertNotIn("checkpoint task #42", self.run_cmd(
            self.work, "git", "log", "--format=%s"
        ).stdout)
        gh_text = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("checkpoint=no-code-delta", gh_text)
        self.assertIn("result=missing", gh_text)
        self.assertNotIn("Acceptance: PASS", gh_text)

    def test_expired_budget_finishes_normally_without_starting_an_agent(self):
        completed, elapsed = self.invoke(self.work, budget=0, min_pass=480, passes=4)
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 30, output)
        self.assertIn("budget exhausted", output)
        self.assertIn("budget=exhausted", self.gh_log.read_text(encoding="utf-8"))
        self.assertFalse(self.counter.exists(), "expired budget must not launch the agent")
        self.assertLess(elapsed, 10)
        self.assertNotIn(self.checkpoint_ref(), self.remote_heads(self.work))


class DelegatedWorkerDeadlineContractTest(unittest.TestCase):
    def test_shipped_worker_keeps_hard_cap_and_derives_real_remaining_time(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        job = body[body.index("  execute:"):body.index("    steps:")]
        self.assertIn("timeout-minutes: 45", job)
        self.assertIn("Record delegated job deadline anchor", body)
        self.assertIn('CONTINUUM_JOB_STARTED_EPOCH=$(date +%s)', body)
        config = deadline_config()
        self.assertIn("job_timeout_seconds=2700", config)
        self.assertIn("checkpoint_reserve_seconds=300", config)
        self.assertIn("job_started_epoch", config)
        self.assertIn("deadline_epoch=$(( job_started_epoch + job_timeout_seconds - checkpoint_reserve_seconds ))", config)
        self.assertIn("max_subpass_seconds=900", config)
        self.assertLess(900, 2700 - 300, "one subpass must fit before the reserve")

    def test_each_agent_run_is_bounded_and_persists_before_retrying(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        functions = deadline_functions()
        self.assertIn('timeout --signal=TERM --kill-after="${subpass_grace_seconds}s"', functions)
        self.assertIn('"${pass_timeout}s"', functions)
        self.assertIn("continuum_persist_incomplete_work || return $?", functions)
        self.assertLess(
            functions.index("continuum_persist_incomplete_work || return $?"),
            functions.index("echo \"Child task #$TASK_NUMBER pass $pass incomplete"),
        )
        self.assertLess(
            functions.index("timeout --signal=TERM"),
            functions.index("opencode run --auto"),
            "every opencode run must be wrapped by the bounded timeout",
        )
        self.assertEqual(body.count("opencode run --auto"), 1)
        self.assertIn("exit 30", body)


if __name__ == "__main__":
    unittest.main()
