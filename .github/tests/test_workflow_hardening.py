#!/usr/bin/env python3
"""Static audit of the active workflows' privilege boundary.

The trust policy decides *what* is trusted. These tests decide whether the
workflows are even wired to ask it. A hardening change that leaves a
``pull_request_target`` job holding write credentials, a privileged job
checkoutting a ref straight from an event payload, or a policy file nobody
calls is a silent regression, so each of those is an explicit assertion here.

Workflow YAML is parsed with Ruby's Psych, which is already part of this
repository's CI toolchain, so the audit needs no third-party Python package.

Run with::

    python3 -m unittest discover -s .github/tests -p 'test_*.py'
"""

import json
import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
POLICY_PATH = ".github/scripts/trust_policy.py"

#: The trust policy subcommand that resolves a write-capable credential to the
#: account it speaks for. It is the only policy step allowed to hold the PAT,
#: because no read-only token can answer the question.
IDENTITY_CHECK = "check-token"

#: Anything that mutates the repository or dispatches work. No step that
#: verifies trust or identity may contain one of these.
WRITE_CALLS = (
    "--method POST",
    "--method PUT",
    "--method PATCH",
    "--method DELETE",
    "-X POST",
    "-X PUT",
    "-X PATCH",
    "-X DELETE",
    "createComment",
    "createLabel",
    "addLabels",
    "removeLabel",
    "issues.update",
    "issues.create",
    "pulls.merge",
    "update-branch",
    "/dispatches",
    "git push",
)

WRITE_SCOPES = ("actions", "checks", "contents", "deployments", "issues", "packages", "pull-requests")

#: The automation plane this repository runs for itself, and therefore the set of
#: workflows that must ask the trust policy before doing anything privileged.
AGENT_PLANE = (
    "auto-merge.yml",
    "ci.yml",
    "issue-scheduler.yml",
    "opencode-repair.yml",
    "opencode.yml",
)

#: The workflows that are deliberately outside that plane.
#:
#: These are the shared implementations and adapters Continuum *offers* to a
#: consumer repository: they are entered through `workflow_call` by the
#: consumer's own entry workflow, they never execute pull request code, and
#: their write scopes are intrinsic to the job itself (reconciling a queue,
#: publishing a release). Demanding that they call `.github/scripts/trust_policy.py`
#: would assert a contract a consumer repository has not opted into, and
#: `review-queue.yml` in particular *must* hold write authority on
#: `pull_request_review`: a submitted review is what releases the provider slot
#: the queue is waiting on, so dropping the trigger would stall the queue.
#:
#: The list is spelled out rather than derived, and `AuditScopeTests` asserts it
#: is exactly the complement of `AGENT_PLANE`, so a workflow cannot enter this
#: directory - or leave the agent plane - without a reviewer deciding so.
#:
#: The invariants that are unconditionally true of *any* workflow in this
#: repository stay repository-wide: declared permissions, commit-pinned actions,
#: strict shell mode, no residue of the disabled CodeRabbit path, and a
#: tokenless privileged checkout.
NOT_AGENT_PLANE = (
    "consumer-auto-merge.yml",
    "consumer-child-dispatcher.yml",
    "consumer-child-review.yml",
    "consumer-child-worker.yml",
    "consumer-opencode.yml",
    "consumer-repair.yml",
    "consumer-scheduler.yml",
    "release-bun-binary.yml",
    "review-queue.yml",
)

#: Events whose payload and code are contributed by whoever opened the pull
#: request. Nothing privileged may run on these.
UNTRUSTED_EVENTS = ("pull_request", "pull_request_review", "pull_request_review_comment")

RUBY_YAML_TO_JSON = r"""
require "yaml"
require "json"
files = Dir[File.join(ARGV[0], "*.yml")] + Dir[File.join(ARGV[0], "*.yaml")]
files.sort.each do |file|
  data = YAML.safe_load(File.read(file), aliases: true) || {}
  # Psych resolves the `on:` key to the boolean true (YAML 1.1).
  data["on"] = data.delete(true) if data.key?(true)
  puts JSON.generate({"file" => file, "data" => data})
end
"""


def load_workflows():
    result = subprocess.run(
        ["ruby", "-e", RUBY_YAML_TO_JSON, str(WORKFLOW_DIR)],
        capture_output=True,
        text=True,
        check=True,
    )
    workflows = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        workflows[pathlib.Path(entry["file"]).name] = entry["data"]
    return workflows


def trigger_names(document):
    raw = document.get("on") or {}
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return list(raw)
    return list(raw.keys())


def permission_map(value):
    """Normalize a ``permissions`` value to ``{scope: level}``."""
    if value is None:
        return {}
    if isinstance(value, str):
        return {"*": value}
    return dict(value)


def workflow_permissions(document):
    return permission_map(document.get("permissions"))


def job_permissions(document, job):
    if "permissions" in job:
        return permission_map(job.get("permissions"))
    return workflow_permissions(document)


def write_scopes(permissions):
    return sorted(
        scope
        for scope, level in permissions.items()
        if isinstance(level, str) and level.lower() == "write"
    )


def steps_of(job):
    return job.get("steps") or []


def uses(step, needle):
    return needle in (step.get("uses") or "")


def run_text(step):
    return step.get("run") or ""


def job_text(document, job):
    """Everything statically knowable about a job, as one searchable string."""
    parts = [json.dumps(job, sort_keys=True)]
    for step in steps_of(job):
        parts.append(run_text(step))
        parts.append(json.dumps(step.get("with") or {}, sort_keys=True))
        parts.append(json.dumps(step.get("env") or {}, sort_keys=True))
    return "\n".join(parts)


class WorkflowAuditBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflows = load_workflows()
        cls.raw = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(WORKFLOW_DIR.glob("*.yml"))
        }

    def job(self, workflow_name, job_name):
        self.assertIn(workflow_name, self.workflows, workflow_name)
        self.assertIn(job_name, self.workflows[workflow_name].get("jobs") or {})
        return self.workflows[workflow_name]["jobs"][job_name]

    def agent_plane(self):
        """The workflows the trust-policy contract is asserted for."""
        return {name: self.workflows[name] for name in AGENT_PLANE}

    def steps_matching(self, job, needle):
        """Every step that invokes the needle, not just the first.

        A job may legitimately call the policy more than once (an allowlist step
        and a credential check, say), and a write token smuggled into any one of
        those steps has to be caught.
        """
        return [
            step
            for step in steps_of(job)
            if needle in run_text(step) or needle in json.dumps(step)
        ]

    def step_matching(self, job, needle):
        matches = self.steps_matching(job, needle)
        return matches[0] if matches else None


class AuditScopeTests(WorkflowAuditBase):
    """The scope declaration itself is a control, so it is audited too.

    Narrowing the agent-plane assertions is only safe while the narrowing stays
    honest: every workflow in the directory is named on exactly one side of the
    split, and the agent plane is never empty.
    """

    def test_every_workflow_is_classified(self):
        self.assertEqual(
            sorted(set(AGENT_PLANE) | set(NOT_AGENT_PLANE)),
            sorted(self.workflows),
            "a workflow is missing from the audit scope declaration",
        )

    def test_no_workflow_is_claimed_by_both_sides(self):
        self.assertEqual(sorted(set(AGENT_PLANE) & set(NOT_AGENT_PLANE)), [])

    def test_the_agent_plane_is_never_empty(self):
        for name in AGENT_PLANE:
            self.assertIn(name, self.workflows, name)
        self.assertTrue(AGENT_PLANE, "the agent plane must not be empty")

    def test_every_classified_workflow_calls_the_trust_policy_or_exempt(self):
        """An agent-plane workflow that never asks the policy has no boundary."""
        for name, document in self.agent_plane().items():
            calls_policy = POLICY_PATH in self.raw[name]
            self.assertTrue(
                calls_policy or "permissions" in document,
                "{} is in the agent plane but neither calls the policy nor "
                "declares permissions".format(name),
            )


class UntrustedEventTests(WorkflowAuditBase):
    def test_no_workflow_writes_on_pull_request_code(self):
        """A fork pull request must never meet a write-capable token."""
        for name, document in self.agent_plane().items():
            triggers = trigger_names(document)
            for event in UNTRUSTED_EVENTS:
                if event not in triggers:
                    continue
                self.assertEqual(
                    write_scopes(workflow_permissions(document)),
                    [],
                    "{} runs on {} and must not hold write permissions".format(name, event),
                )
                for job_name, job in (document.get("jobs") or {}).items():
                    self.assertEqual(
                        write_scopes(job_permissions(document, job)),
                        [],
                        "{}/{} holds write permissions on a {} event".format(
                            name, job_name, event
                        ),
                    )
                    for step in steps_of(job):
                        self.assertNotIn(
                            "secrets.",
                            json.dumps(step.get("with") or {})
                            + json.dumps(step.get("env") or {}),
                            "{}/{} passes a secret on a {} event".format(
                                name, job_name, event
                            ),
                        )
                        self.assertNotIn(
                            "secrets.", run_text(step), "{}/{} uses a secret in run on {}".format(
                                name, job_name, event
                            )
                        )

    def test_agent_workflow_never_triggers_on_pull_request_events(self):
        document = self.workflows["opencode.yml"]
        for event in UNTRUSTED_EVENTS + ("pull_request_target", "issues", "push", "schedule"):
            self.assertNotIn(event, trigger_names(document), event)

    def test_agent_workflow_accepts_only_comment_and_dispatch(self):
        self.assertEqual(
            sorted(trigger_names(self.workflows["opencode.yml"])),
            ["issue_comment", "workflow_dispatch"],
        )

    def test_comment_trigger_is_create_only(self):
        triggers = self.workflows["opencode.yml"]["on"]["issue_comment"]
        self.assertEqual(triggers, {"types": ["created"]})


class PermissionsTests(WorkflowAuditBase):
    def test_every_workflow_declares_permissions_explicitly(self):
        # The implicit default is read/write for a new workflow; relying on it
        # is how a workflow silently becomes privileged later.
        for name, document in self.workflows.items():
            self.assertIn("permissions", document, name)
            self.assertNotEqual(
                permission_map(document.get("permissions")).get("*"),
                "write-all",
                name,
            )

    def test_ci_fix_step_never_runs_for_conflict_repair_dispatch(self):
        document = self.agent_plane()["opencode.yml"]
        step = next(
            step
            for step in document["jobs"]["opencode"]["steps"]
            if step.get("name") == "Fix failed blocking workflow"
        )
        condition = str(step.get("if") or "")
        self.assertIn("github.event_name == 'workflow_dispatch'", condition)
        self.assertIn("inputs.mode == 'ci-fix'", condition)
        self.assertIn("needs.authorize.outputs.trust_code == 'trusted_dispatch'", condition)

    def test_privileged_credentials_are_not_reachable_from_fork_events(self):
        for name, document in self.agent_plane().items():
            if "secrets.TAP_PAT" not in self.raw[name]:
                continue

            for event in UNTRUSTED_EVENTS:
                self.assertNotIn(
                    event, trigger_names(document), "{} reaches TAP_PAT via {}".format(name, event)
                )

    def test_only_expected_workflows_hold_write_scopes(self):
        writers = {}
        for name, document in self.agent_plane().items():
            for job_name, job in (document.get("jobs") or {}).items():
                scopes = write_scopes(job_permissions(document, job))
                if scopes:
                    writers["{}/{}".format(name, job_name)] = scopes
        # The write-capable surface is deliberately tiny and fully audited.
        # auto-merge/issue-scheduler/agent are the three automation roles; the
        # repair jobs are label-and-dispatch/watchdog control planes that never
        # check out or execute pull-request code.
        self.assertEqual(
            sorted(writers),
            [
                "auto-merge.yml/reconcile",
                "issue-scheduler.yml/schedule",
                "opencode-repair.yml/ci-repair",
                "opencode-repair.yml/recover-failed-issue-run",
                "opencode-repair.yml/repair-watchdog",
                "opencode-repair.yml/sync-current-pr",
                "opencode-repair.yml/sync-stale-prs",
                "opencode.yml/opencode",
            ],
        )
        for job_key, scopes in writers.items():
            workflow = job_key.split("/")[0]
            self.assertIn(
                POLICY_PATH,
                self.raw[workflow],
                "{} holds {} but does not call the trust policy".format(job_key, scopes),
            )


class PullRequestTargetTests(WorkflowAuditBase):
    def target_workflows(self):
        return {
            name: document
            for name, document in self.workflows.items()
            if "pull_request_target" in trigger_names(document)
        }

    def test_pull_request_target_checkouts_stay_on_trusted_code(self):
        """A privileged event may read the base branch, never PR code."""
        self.assertTrue(self.target_workflows(), "expected at least one pull_request_target workflow")
        for name, document in self.target_workflows().items():
            for job_name, job in (document.get("jobs") or {}).items():
                for step in steps_of(job):
                    if not uses(step, "actions/checkout"):
                        continue
                    with_block = step.get("with") or {}
                    label = "{}/{}".format(name, job_name)
                    # YAML parses an unquoted false as a bool; both spellings
                    # must disable the persisted token.
                    self.assertIn(
                        with_block.get("persist-credentials"),
                        (False, "false"),
                        "{} must not leave a token in the checkout".format(label),
                    )
                    token = str(with_block.get("token") or "")
                    self.assertNotIn("secrets.", token, "{} checks out with a secret".format(label))
                    ref = str(with_block.get("ref") or "")
                    self.assertIn(
                        ref,
                        ("", "${{ github.event.repository.default_branch }}"),
                        "{} checks out an untrusted ref {}".format(label, ref),
                    )

    def test_pull_request_target_never_runs_untrusted_code(self):
        for name, document in self.target_workflows().items():
            for job_name, job in (document.get("jobs") or {}).items():
                label = "{}/{}".format(name, job_name)
                self.assertNotIn(
                    "opencode run", job_text(document, job), label
                )
                self.assertNotIn("anomalyco/opencode", job_text(document, job), label)

    def test_privileged_verify_step_uses_a_read_only_token(self):
        """Trust verification must not itself require write authority.

        The one exception is the credential-identity check: learning which
        account a write-capable token speaks for cannot be done with a
        read-only token, so that single step is required to carry it. What it
        may never do is act on the repository, which is asserted here.
        """
        found = 0
        identity_checks = 0
        for name, document in self.workflows.items():
            for job_name, job in (document.get("jobs") or {}).items():
                steps = self.steps_matching(job, POLICY_PATH)
                if not steps:
                    continue
                found += len(steps)
                label = "{}/{}".format(name, job_name)
                for step in steps:
                    block = json.dumps(step.get("with") or {}) + json.dumps(step.get("env") or {})
                    run = run_text(step)
                    if IDENTITY_CHECK in run:
                        identity_checks += 1
                        # It must actually verify the credential, and must not
                        # write anything while holding it.
                        self.assertIn("check-token", run, label)
                        for verb in WRITE_CALLS:
                            self.assertNotIn(verb, run, "{}: {}".format(label, verb))
                        continue
                    if label == "opencode-repair.yml/sync-stale-prs":
                        # This job is unreachable from pull-request events and
                        # exists specifically to make trusted base-sync writes
                        # emit a real synchronize/CI event. Its job-level gate
                        # is asserted in the credential reachability test.
                        condition = str(job.get("if") or "")
                        self.assertIn("github.event_name == 'workflow_run'", condition)
                        self.assertIn("github.event.workflow_run.event == 'push'", condition)
                        self.assertIn("github.event.workflow_run.conclusion == 'success'", condition)
                        self.assertIn(
                            "github.event.workflow_run.head_branch == github.event.repository.default_branch",
                            condition,
                        )
                        continue
                    self.assertNotIn("secrets.TAP_PAT", block, "{}: {}".format(label, step.get("name")))
                    self.assertNotIn("secrets.", run, "{}: {}".format(label, step.get("name")))
        self.assertGreater(found, 0, "no workflow calls the trust policy")
        self.assertGreater(identity_checks, 0, "no workflow verifies its dispatch credential")


class AgentExecutionTests(WorkflowAuditBase):
    def test_privileged_agent_job_is_gated_by_the_authorize_job(self):
        agent = self.job("opencode.yml", "opencode")
        self.assertIn("authorize", (agent.get("needs") or []))
        self.assertIn("needs.authorize.outputs.authorized", str(agent.get("if")))

    def test_authorize_job_is_read_only(self):
        authorize = self.job("opencode.yml", "authorize")
        self.assertEqual(write_scopes(job_permissions(self.workflows["opencode.yml"], authorize)), [])

    def test_privileged_job_checks_out_only_the_verified_ref(self):
        agent = self.job("opencode.yml", "opencode")
        checkouts = [step for step in steps_of(agent) if uses(step, "actions/checkout")]
        self.assertEqual(len(checkouts), 1)
        ref = (checkouts[0].get("with") or {}).get("ref")
        self.assertEqual(ref, "${{ needs.authorize.outputs.checkout_ref }}")

    def test_privileged_job_never_interpolates_raw_dispatch_inputs(self):
        agent = self.job("opencode.yml", "opencode")
        text = job_text(self.workflows["opencode.yml"], agent)
        for expression in ("inputs.head_ref", "inputs.pr_number", "inputs.run_id"):
            self.assertNotIn(expression, text, expression)

    def test_agent_tasks_have_a_substantial_bounded_runtime(self):
        # Task execution and repair execution are different kinds of work and
        # get different bounds. The agent job's own timeout is a policy output,
        # not a literal, because conflict repair must not inherit the source
        # task's runtime: that inheritance is what stranded runtime-lab #66,
        # where a 35-minute re-run of a finished task hit the task timeout.
        self_agent = self.job("opencode.yml", "opencode")
        self.assertEqual(
            self_agent.get("timeout-minutes"),
            "${{ fromJSON(needs.authorize.outputs.task_timeout_minutes) }}",
        )
        self.assertIn("steps.plan.outputs.task_timeout_minutes", self.raw["opencode.yml"])

        consumer_agent = self.job("consumer-opencode.yml", "opencode")
        self.assertEqual(
            consumer_agent.get("timeout-minutes"),
            "${{ inputs.mode == 'issue' && inputs.task_timeout_minutes || inputs.repair_timeout_minutes }}",
        )
        consumer_inputs = self.workflows["consumer-opencode.yml"]["on"]["workflow_call"]["inputs"]
        self.assertEqual(consumer_inputs["task_timeout_minutes"]["default"], 180)
        # The repair bound is a separate, much smaller input for the same
        # reason: a conflict repair must not inherit the task's runtime.
        self.assertLess(
            consumer_inputs["repair_timeout_minutes"]["default"],
            consumer_inputs["task_timeout_minutes"]["default"],
        )
        self.assertLess(
            consumer_inputs["repair_agent_timeout_seconds"]["default"],
            consumer_inputs["repair_timeout_minutes"]["default"] * 60,
        )

    def test_issue_runs_record_durable_run_ownership_before_agent_execution(self):
        for name in ("opencode.yml", "consumer-opencode.yml"):
            text = self.raw[name]
            self.assertIn("continuum-opencode-run:$GITHUB_RUN_ID", text)
            self.assertIn("Record issue execution ownership", text)
            execution_token = (
                "uses: anomalyco/opencode"
                if name == "opencode.yml"
                else "opencode run --auto"
            )
            self.assertLess(
                text.index("Record issue execution ownership"),
                text.index(execution_token),
            )

    def test_upstream_installer_is_bounded_and_verified(self):
        for name in ("opencode.yml", "consumer-opencode.yml"):
            text = self.raw[name]
            self.assertIn("OpenCode install attempt $attempt/3", text)
            self.assertIn('test -x "$HOME/.opencode/bin/opencode"', text)
            self.assertIn("sleep $((attempt * 5))", text)

    def test_consumer_allows_anonymous_models_without_api_key(self):
        text = self.raw["consumer-opencode.yml"]
        self.assertNotIn("Require model credential", text)
        self.assertNotIn("OPENCODE_API_KEY is not configured", text)
        self.assertIn("OPENCODE_MODEL", text)

    def test_comment_gate_requires_a_trusted_author_and_a_real_issue(self):
        condition = str(self.job("opencode.yml", "authorize").get("if"))
        self.assertIn("github.event.comment.user.login", condition)
        self.assertIn("github.repository_owner", condition)
        self.assertIn("github.event.issue.pull_request == null", condition)

    def test_inactive_review_integration_is_fully_removed(self):
        # The disabled CodeRabbit path handed a write-capable token an
        # attacker-supplied PR ref. No automation workflow may keep any residue
        # of it. ci.yml is exempt: its snapshot guard only asserts the disabled
        # reference tree is still present on disk and executes none of it.
        for name, document in self.workflows.items():
            if name == "ci.yml":
                continue
            for job_name, job in (document.get("jobs") or {}).items():
                self.assertNotIn(
                    "coderabbit",
                    json.dumps(job).lower(),
                    "{}/{} still references CodeRabbit".format(name, job_name),
                )

    def test_ci_only_asserts_the_disabled_snapshot(self):
        # The reference tree stays as documentation of the inactive path. The
        # guard may name those files, but every mention must be an inert
        # existence check: it may not run them, dispatch them, or read their
        # inputs into a job.
        job = self.job("ci.yml", "bootstrap-validation")
        snapshot_mentions = 0
        for step in steps_of(job):
            for line in run_text(step).splitlines():
                if "reference/" not in line:
                    continue
                snapshot_mentions += 1
                self.assertRegex(
                    line.strip(),
                    r"^test -f reference/",
                    "{} touches the reference tree: {}".format(step.get("name"), line.strip()),
                )
            self.assertNotIn("reference/", json.dumps(step.get("with") or {}), str(step.get("name")))
        self.assertGreater(snapshot_mentions, 0, "the disabled snapshot is no longer guarded")


class RepairControllerTests(WorkflowAuditBase):
    def test_dispatch_head_refs_are_policy_verified(self):
        for name in AGENT_PLANE:
            if "inputs[head_ref]" not in self.raw[name]:
                continue
            self.assertIn(
                "verify-dispatch",
                self.raw[name],
                "{} dispatches head_ref without verify-dispatch".format(name),
            )

    def test_repair_dispatch_is_validated_before_it_is_sent(self):
        text = self.raw["opencode-repair.yml"]
        self.assertIn(POLICY_PATH, text)
        self.assertLess(
            text.index("verify-dispatch"),
            text.index("actions/workflows/opencode.yml/dispatches"),
            "verification must happen before the privileged dispatch",
        )

    def test_repair_controller_ignores_fork_pull_requests(self):
        text = self.raw["opencode-repair.yml"]
        self.assertIn("trust_policy.py", text)
        self.assertIn("pull_request_target", text)

    def test_non_agent_ci_is_a_green_repair_noop(self):
        text = self.raw["opencode-repair.yml"]
        self.assertIn("Repair controller ignored non-agent PR", text)
        self.assertIn('print("pr_number=")', text)
        self.assertIn("raise SystemExit(0)", text)
        self.assertNotIn(
            'print("::error::Trust policy denied this pull request:',
            text,
            "workflow-command diagnostics must never be redirected to GITHUB_OUTPUT",
        )

    def test_failed_issue_runs_release_reservations_by_event_not_only_lease(self):
        text = self.raw["opencode-repair.yml"]
        self.assertIn('"OpenCode agent"', text)
        self.assertIn("recover-failed-issue-run", text)
        self.assertIn("continuum-opencode-run:$RUN_ID", text)
        self.assertIn("automation:in-progress", text)
        self.assertIn("automation:paused", text)
        self.assertIn("gh workflow run issue-scheduler.yml", text)
        self.assertIn("MAX_ATTEMPTS", text)

        consumer = self.raw["consumer-repair.yml"]
        self.assertIn("issue-run-recovery", consumer)
        self.assertIn("continuum-opencode-run:$RUN_ID", consumer)
        self.assertIn("continuum-dispatch", consumer)
        self.assertIn('gh workflow run "$SCHEDULER_WORKFLOW"', consumer)

    def test_consumer_blocking_workflows_are_generic_and_repairable(self):
        merge = self.raw["consumer-auto-merge.yml"]
        repair = self.raw["consumer-repair.yml"]
        self.assertIn("additional_blocking_workflows", merge)
        self.assertIn("optionalBlockingWorkflowsGreen", merge)
        self.assertIn("if (!run) continue", merge)
        self.assertIn("additional_blocking_workflows", repair)
        self.assertIn("opencode-gate-repair-", repair)
        self.assertNotIn("Packaging smoke", merge)
        self.assertNotIn("Packaging smoke", repair)
        self.assertNotIn("Packaging smoke", self.raw["opencode-repair.yml"])

    def test_retry_budget_resets_after_explicit_unpause(self):
        for name in (
            "issue-scheduler.yml",
            "opencode-repair.yml",
            "consumer-scheduler.yml",
            "consumer-repair.yml",
        ):
            text = self.raw[name]
            self.assertIn("/events", text, name)
            self.assertIn("automation:paused", text, name)
            self.assertIn("unlabeled", text, name)

    def test_failed_agent_run_does_not_retry_when_open_pr_exists(self):
        for name in ("opencode-repair.yml", "consumer-repair.yml"):
            text = self.raw[name]
            self.assertIn("Usable open PR #$open_pr already owns issue", text, name)
            self.assertIn('pulls?state=open&per_page=100', text, name)
            self.assertIn('startswith("opencode/issue', text, name)

    def test_merge_controller_uses_the_trusted_merge_plan(self):
        text = self.raw["auto-merge.yml"]
        self.assertIn("merge-plan", text)
        self.assertNotIn("head.repo?.full_name !== ", text)

    def test_repair_controller_has_no_human_stop_for_agent_repairs(self):
        text = self.raw["opencode-repair.yml"]
        self.assertNotIn("flag-human", text)
        self.assertNotIn("opencode-human-review-required", text)
        self.assertNotIn("privilege_boundary_change", text)
        self.assertIn("--mode resolve-conflict", text)
        self.assertIn("--mode ci-fix", text)

    def test_merge_titles_are_sanitized(self):
        text = self.raw["auto-merge.yml"]
        self.assertIn("commit-title", text)


class SelfProtectionTests(WorkflowAuditBase):
    def test_stale_prs_can_never_restore_completed_bootstrap_policy(self):
        self_merge = self.raw["auto-merge.yml"]
        consumer_merge = self.raw["consumer-auto-merge.yml"]

        # Regression for the 2026-09-28 #35 -> #32 incident: a stale PR
        # restored this temporary guard after the bootstrap it protected had
        # already completed.
        self.assertNotIn("issue25", self_merge.lower())

        # Both merge surfaces bind eligibility to the current base and bind the
        # merge write to the exact head SHA. Green CI on an old base is not
        # sufficient.
        self.assertIn("compare/$base_sha...$head_sha", self_merge)
        self.assertIn('-f "sha=$head_sha"', self_merge)
        self.assertIn("compareCommitsWithBasehead", consumer_merge)
        self.assertIn("sha:pr.head.sha", consumer_merge)

    def test_main_advancement_reconciles_stale_agent_branches(self):
        self_repair = self.raw["opencode-repair.yml"]
        consumer_repair = self.raw["consumer-repair.yml"]

        self.assertIn("sync-stale-prs", self_repair)
        self.assertIn("update-branch", self_repair)
        self.assertIn("base-sync", consumer_repair)
        self.assertIn("update-branch", consumer_repair)

    def test_merge_finalization_is_idempotent_after_source_issue_autoclose(self):
        self.assertIn("already closed", self.raw["auto-merge.yml"])
        self.assertIn("already closed", self.raw["consumer-auto-merge.yml"])

    def test_child_relationships_are_read_inside_the_runner(self):
        resolver = (REPO_ROOT / ".github/scripts/delegation_repository.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('repository_variables "$GITHUB_REPOSITORY"', resolver)
        self.assertIn("CONTINUUM_ROLE", resolver)
        self.assertIn("CONTINUUM_CHILDREN", resolver)
        for name in (
            "consumer-child-dispatcher.yml",
            "consumer-child-worker.yml",
            "consumer-child-review.yml",
        ):
            text = self.raw[name]
            self.assertNotIn("parent_role:", text)
            self.assertNotIn("parent_children:", text)
            self.assertNotIn("PARENT_ROLE:", text)
            self.assertNotIn("PARENT_CHILDREN:", text)
            self.assertNotIn("contents/.continuum.yml", text)

    def test_child_validation_gate_uses_base_script_without_secrets(self):
        text = self.raw["consumer-child-review.yml"]
        self.assertIn('git show "origin/$base_ref:$validation_script"', text)
        self.assertIn("env -i", text)
        self.assertIn('run_validation', text)
        self.assertIn('validation gate failed', text)
        self.assertNotIn('cat "$validation_log"', text)
        self.assertNotIn('tail "$validation_log"', text)

    def test_custom_runtime_release_can_be_pinned_exactly(self):
        runtime = self.raw["consumer-opencode.yml"]
        for token in (
            "opencode_release_tag",
            "opencode_release_sha256",
            "opencode_release_version",
            "opencode_release_source_sha",
        ):
            self.assertIn(token, runtime)
        self.assertIn("Exact source verification requires the release metadata sidecar", runtime)
        self.assertIn("Release binary SHA-256 does not match the pinned identity", runtime)

    def test_bun_release_adapter_supports_shell_free_exact_build_argv(self):
        release = self.raw["release-bun-binary.yml"]
        for token in (
            "build_argv_json",
            "build_env_json",
            "expected_version",
            "subprocess.run(argv, env=child_env, check=True)",
            '"source_sha": "$SOURCE_SHA"',
            '"binary_version": "$BINARY_VERSION"',
        ):
            self.assertIn(token, release)
        self.assertNotIn("shell=True", release)

    def test_ci_runs_the_security_suite(self):
        text = self.raw["ci.yml"]
        self.assertIn("unittest", text)
        self.assertIn(POLICY_PATH, text)

    def test_action_inputs_never_contain_nested_mappings(self):
        # GitHub Actions action inputs are scalar values. A nested mapping such
        # as `with: { env: {...} }` is valid YAML but invalid workflow schema
        # and produces a zero-job failure before runner assignment.
        for workflow_name, document in self.workflows.items():
            for job_name, job in (document.get("jobs") or {}).items():
                for step in steps_of(job):
                    values = step.get("with") or {}
                    if not isinstance(values, dict):
                        continue
                    for input_name, value in values.items():
                        self.assertNotIsInstance(
                            value,
                            (dict, list),
                            "{}/{} action input {} is nested rather than scalar".format(
                                workflow_name, job_name, input_name
                            ),
                        )

    def test_ci_runs_pinned_semantic_workflow_validation(self):
        text = self.raw["ci.yml"]
        self.assertIn("actionlint", text)
        self.assertIn("ACTIONLINT_VERSION", text)
        self.assertIn("ACTIONLINT_SHA256", text)
        self.assertIn("sha256sum -c", text)

    def test_ci_keeps_the_active_workflow_allowlist(self):
        # The allowlist is a control: an unreviewed new workflow must not be
        # able to arrive with write permissions unnoticed.
        text = self.raw["ci.yml"]
        match = re.search(r"allowed='\^\((?P<names>[^)]*)\)", text)
        self.assertIsNotNone(match, "could not read the workflow allowlist")
        allowed = {name.strip() for name in match.group("names").split("|")}
        self.assertEqual(
            {name.rsplit(".", 1)[0] for name in self.workflows},
            allowed,
            "the active workflow set and the CI allowlist disagree",
        )

    def test_every_action_is_pinned_to_a_commit_sha(self):
        pattern = re.compile(r"^\s*uses:\s*([^\s@]+)(@(\S+))?", re.M)
        for name, source in self.raw.items():
            for match in pattern.finditer(source):
                reference = match.group(3)
                self.assertIsNotNone(
                    reference, "{}: {} is not pinned".format(name, match.group(1))
                )
                self.assertRegex(
                    reference,
                    r"^[0-9a-f]{40}$",
                    "{}: {} is not pinned to a full commit sha".format(name, match.group(1)),
                )

    def test_shell_steps_are_strict_mode(self):
        """Every run block sets its own error handling rather than relying on
        the caller's shell, so a failed check cannot be ignored."""
        for name, document in self.workflows.items():
            for job_name, job in (document.get("jobs") or {}).items():
                for step in steps_of(job):
                    script = run_text(step)
                    if not script.strip():
                        continue
                    if script.lstrip().startswith("#!"):
                        continue
                    self.assertTrue(
                        "set -euo pipefail" in script or "set -eu" in script,
                        "{}/{} step {!r} does not use strict shell mode".format(
                            name, job_name, step.get("name", "<unnamed>")
                        ),
                    )


class DelegatedChildLivenessTests(WorkflowAuditBase):
    """Delegated child work must recover from controller metadata failures."""

    def test_dispatcher_reopens_closed_unmerged_child_pull_requests(self):
        text = self.raw["consumer-child-dispatcher.yml"]
        self.assertIn('gh pr list --repo "$child_repo" --state closed', text)
        self.assertIn('gh pr reopen "$stale_pr" --repo "$child_repo"', text)

    def test_dispatcher_clears_stale_child_pause_labels(self):
        text = self.raw["consumer-child-dispatcher.yml"]
        self.assertIn('labels/automation%3Apaused', text)
        self.assertIn('Cleared stale automation:paused on child task', text)

    def test_child_review_reopens_pr_and_defers_merge_for_retry(self):
        text = self.raw["consumer-child-review.yml"]
        self.assertIn('gh pr reopen "$PR_NUMBER" --repo "$child_repo"', text)
        self.assertIn('continuum-child-review-merge-retry', text)
        self.assertNotIn('exit 32', text)

    def test_child_review_surfaces_base_drift_before_acceptance(self):
        text = self.raw["consumer-child-review.yml"]
        self.assertIn('git merge --no-edit "origin/$base_ref"', text)
        self.assertIn('git diff --name-only --diff-filter=U', text)


if __name__ == "__main__":
    unittest.main()
