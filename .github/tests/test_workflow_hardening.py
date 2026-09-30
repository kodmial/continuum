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
    "review-gate.yml",
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
#: `consumer.yml` is here for a different reason than the rest: it is the release
#: entrypoint itself, the single reusable workflow a consumer's ingress names at
#: one exact release reference. It holds no `steps` of its own -- it only routes
#: to the implementations below through same-repository relative references -- so
#: there is no consumer code for the trust policy to have an opinion about.
#: `ReleaseSelectionTests` in `test_release_selection.py` is what holds it to
#: ADR-0002 instead.
#:
#: `parity-drift.yml` is a reporting plane: it reads other repositories' workflow
#: heads on a schedule, and its one write scope is `issues: write` in a single job
#: that opens or updates one parity issue. It executes no pull request code and
#: dispatches no agent, so the trust-policy contract -- which is about what happens
#: when pull request code reaches a privileged job -- has nothing to say about it.
#: What *is* asserted about it is below, in `ProvenanceDriftWorkflowTests`: no
#: private-source credential reference, no code copy, and a write scope confined to
#: the publish job.
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
    "consumer-child-pr-review.yml",
    "consumer-child-review.yml",
    "consumer-child-worker.yml",
    "consumer-opencode.yml",
    "consumer-repair.yml",
    "consumer-review-gate.yml",
    "consumer-scheduler.yml",
    "consumer.yml",
    "continuum-shadow.yml",
    "parity-drift.yml",
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
                "review-gate.yml/gate",
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


class ReviewGateTests(WorkflowAuditBase):
    """The loop orchestrator: thin, positional trust, and provider-neutral."""

    def gate_workflow(self):
        return self.workflows["review-gate.yml"]

    def test_the_control_plane_is_read_only_and_the_gate_is_not(self):
        document = self.gate_workflow()
        self.assertEqual(
            write_scopes(job_permissions(document, document["jobs"]["authorize"])), []
        )
        self.assertEqual(
            write_scopes(job_permissions(document, document["jobs"]["reverify"])), []
        )
        self.assertIn("authorize", document["jobs"]["gate"].get("needs") or [])
        self.assertIn("reverify", document["jobs"]["gate"].get("needs") or [])

    def test_the_gate_runs_only_on_the_control_planes_decision(self):
        # There is deliberately no second path that re-derives trust from the
        # event payload. An event-supplied pull-request ref is exactly the value
        # that must never reach a write-capable step unvalidated, so the gate's
        # only condition is the read-only job's own verdict.
        condition = str(self.gate_workflow()["jobs"]["gate"].get("if"))
        self.assertIn("needs.authorize.outputs.proceed == 'true'", condition)
        self.assertIn("needs.reverify.outputs.allowed == 'true'", condition)
        self.assertNotIn("github.event.pull_request", condition)

    def test_authority_is_re_verified_with_a_read_only_token(self):
        job = self.gate_workflow()["jobs"]["reverify"]
        steps = steps_of(job)
        verify = [s for s in steps if POLICY_PATH in run_text(s)]
        self.assertEqual(len(verify), 1, "expected one trust-policy re-verification")
        step = verify[0]
        body = run_text(step) + json.dumps(step.get("env") or {})
        self.assertIn("--mode review-fix", run_text(step))
        # GitHub permissions are a job boundary, not a step boundary. The
        # verification job therefore owns no write scope and receives no secret.
        self.assertNotIn("secrets.", body)
        self.assertEqual(
            write_scopes(job_permissions(self.gate_workflow(), job)),
            [],
            "the re-verification job is not read-only",
        )

    def test_the_engine_comes_from_the_base_branch_not_the_pull_request(self):
        # A pull request must not be able to choose the code that reviews it.
        for job in self.gate_workflow()["jobs"].values():
            for step in steps_of(job):
                if uses(step, "actions/checkout"):
                    self.assertEqual(
                        (step.get("with") or {}).get("ref"),
                        "${{ github.event.repository.default_branch }}",
                    )

    def test_every_wake_up_resolves_to_a_target_and_nothing_else(self):
        # The event varies, the target does not, and a wake-up that resolves no
        # trusted pull request has to be a silent no-op rather than a fallback
        # that guesses one.
        body = job_text(
            self.gate_workflow(), self.gate_workflow()["jobs"]["authorize"]
        )
        self.assertIn("is_trusted_pull_request", body)
        self.assertIn("emit_skip", body)
        for event in (
            "pull_request_target",
            "workflow_run",
            "status",
            "schedule",
            "workflow_dispatch",
        ):
            self.assertIn(event, body, "{} is not a resolved wake-up".format(event))

    def test_the_consumer_contract_is_the_configuration_of_record(self):
        # `review` is the repository's declared switch. The gate reads the
        # consumer contract rather than this repository's own engine file, so
        # the toggle a maintainer edits is the toggle that decides.
        body = job_text(self.gate_workflow(), self.gate_workflow()["jobs"]["gate"])
        self.assertIn(".github/continuum.yml", body)
        self.assertIn("--config \"$CONTINUUM_CONFIG\"", body)

    def test_review_events_are_not_a_privileged_wake_up(self):
        document = self.gate_workflow()
        triggers = trigger_names(document)
        self.assertNotIn("pull_request_review", triggers)
        raw = self.raw["review-gate.yml"]
        # Submitted reviews remain durable evidence consumed by the engine; the
        # privileged workflow is merely not entered from their untrusted event.
        self.assertIn("submitted reviews", raw.lower())
        self.assertIn("review", raw.lower())

    def test_the_workflow_names_no_provider(self):
        # There is no user-facing provider selector. The provider is reached
        # through the engine, which resolves it from `review: true`.
        self.assertNotIn("coderabbit", self.raw["review-gate.yml"].lower())
        self.assertNotIn("pr-agent", self.raw["review-gate.yml"].lower())

    def test_provider_runs_are_serialized_and_never_cancelled(self):
        concurrency = self.gate_workflow().get("concurrency") or {}
        self.assertEqual(concurrency.get("cancel-in-progress"), False)
        self.assertTrue(str(concurrency.get("group")))

    def test_the_agent_step_reads_the_instruction_the_controller_stored(self):
        # The repair must not rebuild its own instruction from the event: the
        # normalized, deduplicated findings are what the controller wrote for
        # this exact commit, and that is the only thing the model may be given.
        agent = self.job("opencode.yml", "opencode")
        step = next(
            s for s in steps_of(agent) if s.get("name") == "Repair the open review findings"
        )
        self.assertIn("review prompt", run_text(step))
        self.assertIn("--head \"$BEFORE_SHA\"", run_text(step))
        # The step must not go to the provider itself, and must not read
        # findings out of the event payload: both are how a repair would end up
        # answering something other than what the controller decided.
        self.assertNotIn("review reconcile", run_text(step))
        self.assertNotIn("coderabbit", run_text(step).lower())
        self.assertNotIn("github.event", json.dumps(step.get("env") or {}))

    def test_the_agent_mode_is_generic_and_the_engine_is_trusted_code(self):
        document = self.workflows["opencode.yml"]
        agent = document["jobs"]["opencode"]
        condition = json.dumps(steps_of(agent))
        self.assertIn("inputs.mode == 'review-fix'", condition)
        for step in steps_of(agent):
            if step.get("name") == "Checkout the trusted review engine":
                self.assertEqual(
                    (step.get("with") or {}).get("ref"),
                    "${{ github.event.repository.default_branch }}",
                )
                self.assertEqual((step.get("with") or {}).get("path"), "engine")


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
        self.assertGreaterEqual(len(checkouts), 1)
        # The pull request's tree is the only checkout whose ref may come from a
        # validated job output. Every other checkout must be a fixed, untrusted
        # input - the base branch - because it supplies trusted code (the engine)
        # or trusted policy to a job that is about to spend write authority. A
        # pull request must not be able to choose the code that reviews it, and
        # it must not be able to shadow the workspace root.
        for step in checkouts:
            with_block = step.get("with") or {}
            ref = with_block.get("ref")
            if ref == "${{ needs.authorize.outputs.checkout_ref }}":
                continue
            self.assertEqual(
                ref,
                "${{ github.event.repository.default_branch }}",
                "opencode.yml checks out {} from {}".format(with_block.get("path"), ref),
            )
            self.assertNotEqual(
                with_block.get("path"),
                None,
                "a trusted checkout must not land on the workspace root",
            )

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

    def test_authorize_plan_uses_trusted_checked_out_helper(self):
        """Issue-mode authorization must not depend on privileged-job env."""
        document = self.agent_plane()["opencode.yml"]
        authorize = document["jobs"]["authorize"]
        step = next(
            step
            for step in (authorize.get("steps") or [])
            if step.get("name") == "Plan the repair ladder"
        )
        text = json.dumps(step, sort_keys=True)
        self.assertIn("python3 .github/scripts/conflict_repair.py", text)
        self.assertNotIn("CONTINUUM_CONFLICT_REPAIR_HELPER", text)

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

    def test_no_privileged_job_names_the_review_provider(self):
        # This repository used to carry a disabled CodeRabbit path, and the
        # audit for it banned the provider name outright because that path handed
        # a write-capable token an attacker-supplied pull-request ref. The
        # provider is the MVP review implementation now, so the name is
        # expected -- but nowhere that matters. It is resolved inside the engine
        # from `review: true`, and no workflow selects, names, or dispatches it.
        #
        # The tripwire matters more than the current state: the day a job does
        # name a provider, that job must either be read-only or go through the
        # trust policy before spending anything.
        for name, document in self.workflows.items():
            for job_name, job in (document.get("jobs") or {}).items():
                body = json.dumps(job).lower()
                if "coderabbit" not in body and "pr_agent" not in body:
                    continue
                label = "{}/{}".format(name, job_name)
                if not write_scopes(job_permissions(document, job)):
                    continue
                self.fail(
                    "{} holds {} and names a review provider. The provider is "
                    "resolved by the engine from the review toggle; a "
                    "workflow that names it has re-opened the ref-trust "
                    "hazard.".format(label, write_scopes(job_permissions(document, job)))
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

    def test_consumer_infrastructure_failures_have_a_separate_retry_budget(self):
        scheduler = self.raw["consumer-scheduler.yml"]
        repair = self.raw["consumer-repair.yml"]

        self.assertIn("continuum-infra-retry:", scheduler)
        self.assertIn("semanticAttempts:Math.max(0, dispatches.length - infraRetries.length)", scheduler)
        self.assertIn("retry.latestInfraRetry.nextRetryAt", scheduler)
        self.assertNotIn("dispatches.length >= maxAttempts", scheduler)

        self.assertIn("failure_retry.py", repair)
        self.assertIn("transient_infra", repair)
        self.assertIn("continuum-infra-retry:", repair)
        self.assertIn("semantic_attempts=$(( attempts - infra_attempts ))", repair)
        self.assertIn("Infrastructure retry", repair)
        self.assertIn("semantic attempt budget remains untouched", repair)
        self.assertLess(
            repair.index("Load Continuum engine", repair.index("issue-run-recovery:")),
            repair.index("failure_retry.py", repair.index("issue-run-recovery:")),
        )

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

    def test_consumer_merge_reconciliation_is_single_flight(self):
        job = self.job("consumer-auto-merge.yml", "merge")
        concurrency = job.get("concurrency") or {}
        # One group, constant for the repository, derived from no payload field:
        # that is what makes "at most one reconciliation owns side effects" a
        # property of the runner rather than a convention.
        self.assertEqual(
            concurrency.get("group"),
            "continuum-auto-merge-${{ github.repository }}",
        )
        # Cancelling a reconciler between its merge write and its release
        # hand-off would drop a release on the floor, so a newer wake-up queues
        # instead of replacing. A queued run is stale the moment it starts, which
        # is safe only because every write revalidates; that is asserted below.
        self.assertFalse(concurrency.get("cancel-in-progress"))

    def test_consumer_merge_resolves_the_toggles_from_the_versioned_contract(self):
        # A second way to declare `review`/`release` is a second thing that can
        # disagree with the file a reviewer reads, so the entrypoint may not
        # carry them. They are resolved once per run, from the base branch.
        document = self.workflows["consumer-auto-merge.yml"]
        inputs = (document.get(True) or document.get("on") or {}).get("workflow_call", {})
        declared = set((inputs.get("inputs") or {}).keys())
        self.assertNotIn("review", declared)
        self.assertNotIn("release", declared)
        self.assertIn("config_path", declared)

        contract = self.job("consumer-auto-merge.yml", "contract")
        # Read-only, so a job that reports a merge posture cannot have changed
        # one.
        self.assertEqual(write_scopes(job_permissions(document, contract)), [])
        self.assertEqual(job_permissions(document, contract)["contents"], "read")

        text = self.raw["consumer-auto-merge.yml"]
        self.assertIn("continuum_config.py", text)
        self.assertIn("continuum-contract.yml", text)
        # The engine's resolver publishes to the step summary itself; the
        # contract requires the resolved posture to be visible without a log.
        resolver = REPO_ROOT / ".github" / "scripts" / "continuum_config.py"
        self.assertIn("GITHUB_STEP_SUMMARY", resolver.read_text(encoding="utf-8"))

    def test_consumer_merge_revalidates_every_gate_immediately_before_the_write(self):
        # Single-flight bounds how many reconcilers run; revalidation is what
        # makes a queued or superseded one harmless.
        text = self.raw["consumer-auto-merge.yml"]
        write = text.index("github.rest.pulls.merge(")
        window = text[:write]
        for gate in (
            "await baseFresh(pr.head.sha)",
            "await currentHeadCiGreen(pr.head.sha)",
            "await optionalBlockingWorkflowsGreen(pr.head.sha)",
            "await reviewGreen(pr.head.sha)",
        ):
            self.assertIn(gate, window, gate)
        # GitHub performs the final compare-and-swap: a head that moved after the
        # re-check refuses the merge rather than merging something else.
        self.assertIn("sha:pr.head.sha", text.replace(" ", ""))

    def test_consumer_release_handoff_is_durable_and_never_duplicated_by_a_rerun(self):
        text = self.raw["consumer-auto-merge.yml"]
        # Recorded before dispatch, confirmed after: "recorded but unconfirmed" is
        # the recoverable state, and a later run re-drives it instead of the
        # merge silently losing its release.
        self.assertIn("continuum-release-pending:", text)
        self.assertIn("continuum-release-dispatched:", text)
        self.assertIn("recoverReleaseHandoffs", text)
        # Bounded, so a permanently failing hand-off cannot be retried forever.
        self.assertIn("RELEASE_PENDING_ATTEMPTS", text)
        self.assertIn("RELEASE_RECOVERY_WINDOW_HOURS", text)
        # The consumer's entrypoint receives the merge commit, which is what
        # makes a re-driven dispatch deduplicable on its side.
        self.assertIn("source_sha", text)

    def test_repair_controller_has_no_human_stop_for_agent_repairs(self):
        text = self.raw["opencode-repair.yml"]
        self.assertNotIn("flag-human", text)
        self.assertNotIn("opencode-human-review-required", text)
        self.assertNotIn("privilege_boundary_change", text)
        self.assertIn("--mode resolve-conflict", text)
        self.assertIn("--mode ci-fix", text)

    def test_consumer_repair_uses_attempt_budget_not_persistent_failure_label(self):
        text = self.raw["consumer-repair.yml"]
        self.assertNotIn("opencode-repair-failed", text)
        self.assertIn("MAX_ATTEMPTS", text)
        self.assertIn("REPAIR_BUDGET_MINUTES", text)
        self.assertIn("--remove-label \"$LABEL\"", text)

    def test_merge_titles_are_sanitized(self):
        text = self.raw["auto-merge.yml"]
        self.assertIn("commit-title", text)


class ConsumerReviewSurfaceTests(WorkflowAuditBase):
    """The consumer review loop: one generic repair mode, one resolved switch.

    The self plane already runs this loop, and a workflow that only worked here
    would make `review: true` unreachable for anyone adopting Continuum. What is
    asserted is therefore not "the loop exists" but the three properties that
    made it safe when it was written, re-asserted on the reusable surface: the
    mode is generic, the instruction is the engine's and bound to one commit, and
    the switch comes from the consumer's own versioned file.
    """

    def agent_modes(self):
        condition = str(self.job("consumer-opencode.yml", "opencode").get("if"))
        match = re.search(r"\[\s*\"[^\]]+\]", condition)
        self.assertIsNotNone(match, "the agent job's mode allowlist is gone")
        return json.loads(match.group(0))

    def test_the_reusable_agent_surface_admits_the_generic_review_repair(self):
        # A closed list of four modes, and `review-fix` is one of them. The
        # absence of anything provider-specific is the point: the mode reaches
        # the trust-policy allowlist, and an allowlist entry named after a
        # reviewer would be a provider in the control plane.
        modes = self.agent_modes()
        self.assertEqual(
            sorted(modes),
            ["ci-fix", "issue", "resolve-conflict", "review-fix"],
        )
        for provider in ("coderabbit", "pr_agent", "pr-agent", "nano"):
            self.assertNotIn(provider, json.dumps(modes).lower())

    def test_the_repair_instruction_is_the_engine_s_and_bound_to_one_commit(self):
        job = self.job("consumer-opencode.yml", "opencode")
        step = next(
            s
            for s in steps_of(job)
            if s.get("name") == "Repair the open review findings"
        )
        run = run_text(step)
        # Reads the instruction the controller stored for this exact commit, and
        # fails closed when there is none.
        self.assertIn("continuum.cli review prompt", run)
        self.assertIn('--head "$BEFORE_SHA"', run)
        self.assertIn('if [[ -z "$PROMPT" ]]', run)
        # The engine is read from the pinned Continuum checkout, never from the
        # pull request's own tree: a change under review cannot decide its own
        # repair scope.
        self.assertIn('PYTHONPATH="$CONTINUUM_ENGINE_ROOT/src"', run)
        # It must not talk to the reviewer or rebuild findings from the event.
        self.assertNotIn("review reconcile", run)
        self.assertNotIn("coderabbit", run.lower())
        self.assertNotIn("github.event", json.dumps(step.get("env") or {}))

    def test_the_review_repair_is_bounded_and_does_not_retry_itself(self):
        job = self.job("consumer-opencode.yml", "opencode")
        step = next(
            s
            for s in steps_of(job)
            if s.get("name") == "Repair the open review findings"
        )
        run = run_text(step)
        self.assertIn(
            'timeout --signal=TERM "$REVIEW_AGENT_TIMEOUT_SECONDS"', run
        )
        # An unchanged HEAD is an answer the controller already planned for, so
        # this step must not push, re-run, or spend a second attempt.
        self.assertIn('if [[ "$AFTER_SHA" == "$BEFORE_SHA" ]]', run)
        self.assertLess(
            run.index('if [[ "$AFTER_SHA" == "$BEFORE_SHA" ]]'),
            run.index('git push origin "HEAD:${HEAD_REF}"'),
        )

    def test_the_execution_environment_belongs_to_the_consumer(self):
        job = self.job("consumer-opencode.yml", "opencode")
        self.assertEqual(job.get("runs-on"), "${{ inputs.runner }}")
        inputs = self.workflows["consumer-opencode.yml"]["on"]["workflow_call"]["inputs"]
        self.assertEqual(inputs["runner"]["default"], "ubuntu-latest")
        self.assertEqual(inputs["swift_version"]["default"], "")
        # The toolchain is installed only when the consumer asked for one, so a
        # consumer that needs no language toolchain pays nothing for the option.
        setup = next(
            s for s in steps_of(job) if uses(s, "swift-actions/setup-swift")
        )
        self.assertEqual(setup.get("if"), "inputs.swift_version != ''")
        # No platform, product, or vendor vocabulary anywhere in the shared
        # surface: the same file has to serve a repository that needs a
        # particular toolchain and one that needs none.
        text = self.raw["consumer-opencode.yml"].lower()
        for name in ("macos", "xcode", "swift 6", "avfoundation", "nano", "kodmai"):
            self.assertNotIn(name, text, name)

    def test_the_consumer_review_gate_names_no_reviewer(self):
        text = self.raw["consumer-review-gate.yml"].lower()
        self.assertNotIn("coderabbit", text)
        self.assertNotIn("pr-agent", text)
        self.assertNotIn("pr_agent", text)

    def test_the_review_gate_splits_decision_from_authority(self):
        document = self.workflows["consumer-review-gate.yml"]
        for job_name in ("authorize", "reverify"):
            job = document["jobs"][job_name]
            self.assertEqual(
                write_scopes(job_permissions(document, job)), [], job_name
            )
        gate = document["jobs"]["gate"]
        self.assertIn("authorize", gate.get("needs") or [])
        self.assertIn("reverify", gate.get("needs") or [])
        self.assertTrue(write_scopes(job_permissions(document, gate)))
        # The dispatch the gate spends authority on is re-verified against the
        # live pull request by the read-only policy first.
        reverify = json.dumps(document["jobs"]["reverify"])
        self.assertIn("--mode review-fix", reverify)
        self.assertIn("verify-dispatch", reverify)

    def test_the_review_gate_reads_the_switch_from_the_base_branch(self):
        # A pull request does not get to declare that review is off, and the
        # entrypoint does not get to pass it in either.
        document = self.workflows["consumer-review-gate.yml"]
        inputs = document["on"]["workflow_call"]["inputs"]
        self.assertNotIn("review", inputs)
        self.assertIn("config_path", inputs)
        gate = json.dumps(document["jobs"]["gate"])
        self.assertIn("continuum_config.py", gate)
        self.assertIn("ref=$BASE_BRANCH", gate)
        # The status context the gate publishes and the one the merge controller
        # waits for are both engine-owned, so the workflow must not name either:
        # if it named one, the two surfaces could drift and `review: true` would
        # wait forever for a context nobody publishes.
        self.assertNotIn("continuum/review", self.raw["consumer-review-gate.yml"])
        merge_inputs = self.workflows["consumer-auto-merge.yml"]["on"]["workflow_call"]["inputs"]
        self.assertEqual(
            merge_inputs["review_status_context"]["default"], "continuum/review"
        )

    def test_the_review_gate_serializes_provider_quota(self):
        concurrency = self.workflows["consumer-review-gate.yml"].get("concurrency") or {}
        self.assertEqual(
            concurrency.get("group"),
            "continuum-consumer-review-gate-${{ github.repository }}",
        )
        # Cancelling mid-run can abandon a provider request that was already
        # issued, which spends quota for no answer.
        self.assertFalse(concurrency.get("cancel-in-progress"))

    def test_the_review_gate_never_checks_out_pull_request_code(self):
        # The gate is the most privileged surface in the repository, so the
        # invariant it has to keep is about *what* it checks out rather than
        # whether it checks anything out. Every checkout has to resolve to
        # Continuum's own release commit; a checkout of the pull request's own
        # code would let the change under review decide the gate that judges it.
        job = self.job("consumer-review-gate.yml", "gate")
        checkouts = [s for s in steps_of(job) if uses(s, "actions/checkout")]
        self.assertTrue(checkouts, "the gate loads the engine it evaluates the gate with")
        for step in checkouts:
            values = step.get("with") or {}
            self.assertEqual(
                values.get("repository"),
                "kodmial/continuum",
                "the gate checks out a repository other than Continuum",
            )
            self.assertEqual(
                values.get("ref"),
                "${{ job.workflow_sha }}",
                "the gate must evaluate the release commit its caller pinned",
            )
        # Everything it needs from the pull request itself is read through the
        # API from the base branch, not materialized as code on disk.
        self.assertEqual(job_permissions(self.workflows["consumer-review-gate.yml"], job)["contents"], "read")


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
        # A relative reference carries no revision of its own: GitHub resolves
        # `./.github/...` against the commit the calling workflow itself was
        # loaded from, so there is nothing to pin and nothing extra to trust.
        # That is the mechanism ADR-0002 depends on -- it is how one consumer
        # pin reaches the whole control plane -- so the exemption is deliberate
        # and narrow rather than a relaxed pattern. Anything reached over the
        # network still has to be a full commit.
        pattern = re.compile(r"^\s*uses:\s*([^\s@]+)(@(\S+))?", re.M)
        for name, source in self.raw.items():
            for match in pattern.finditer(source):
                action = match.group(1)
                if action.startswith("./"):
                    self.assertNotIn(
                        "..",
                        action,
                        "{}: {} reaches outside the caller's repository".format(
                            name, action
                        ),
                    )
                    continue
                reference = match.group(3)
                self.assertIsNotNone(
                    reference, "{}: {} is not pinned".format(name, action)
                )
                self.assertRegex(
                    reference,
                    r"^[0-9a-f]{40}$",
                    "{}: {} is not pinned to a full commit sha".format(name, action),
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


class ProvenanceDriftWorkflowTests(WorkflowAuditBase):
    """The drift checker's boundary: read everything, write one issue, copy nothing.

    The claims asserted here are the ones a reviewer cannot check by reading the
    engine, because they are about the workflow: that the plane cannot reach a
    private source with a broad credential, that its write scope is one scope in
    one job, and that it never turns a source's movement into a copy.
    """

    NAME = "parity-drift.yml"

    def test_it_runs_on_a_schedule_and_on_demand(self):
        # The issue asks for a manual trigger as well as a schedule: registering a
        # source is a human decision, and nobody should have to wait a week for it.
        triggers = trigger_names(self.workflows[self.NAME]) + list(
            (self.workflows[self.NAME].get(True) or {})
        )
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)

    def test_the_write_scope_is_one_scope_in_one_job(self):
        document = self.workflows[self.NAME]
        writers = {}
        for job_name, job in (document.get("jobs") or {}).items():
            scopes = write_scopes(job_permissions(document, job))
            if scopes:
                writers[job_name] = scopes
        self.assertEqual(writers, {"publish": ["issues"]})

    def test_the_reading_job_cannot_write(self):
        # Splitting the jobs is the point: the job that reads other repositories'
        # workflow files has no way to change anything, so a bug in the classifier
        # has nothing to escalate into.
        audit = self.job(self.NAME, "audit")
        self.assertEqual(write_scopes(job_permissions(self.workflows[self.NAME], audit)), [])

    def test_it_never_names_a_private_source_credential(self):
        # The whole point of the private-source rule is that no credential is
        # reachable from this file: a private source is read only through its own
        # least-privilege variable, and the engine refuses to read it without one.
        # If a secret reference for a private token ever appears here, a reader can
        # no longer confirm that by reading, so this fails loudly instead.
        self.assertNotIn("secrets.", self.raw[self.NAME])
        self.assertIn("PRIVATE_SOURCE_READ_TOKEN", self.raw[self.NAME])

    def test_it_never_copies_from_a_source(self):
        # No fetch of another repository's code, and no `cp` of anything it read.
        text = self.raw[self.NAME]
        for forbidden in ("git clone", "actions/checkout@11d5960a326750d5838078e36cf38b85af677262\n          with", "ref: refs/heads/"):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertIn("Nothing here copies code from a source", text)

    def test_the_publish_job_runs_only_after_an_audit_that_found_something(self):
        publish = self.job(self.NAME, "publish")
        self.assertEqual(publish["needs"], "audit")
        condition = str(publish.get("if") or "")
        # Not merely "the audit ran": an audit that found nothing must not touch the
        # issue tracker, or the weekly schedule becomes weekly noise.
        self.assertIn("drifted", condition)
        self.assertIn("incomplete", condition)

    def test_a_clean_audit_still_uploads_its_evidence(self):
        # A report that is only uploaded on drift is a report nobody can compare a
        # drift against afterwards.
        upload = next(
            step
            for step in steps_of(self.job(self.NAME, "audit"))
            if uses(step, "actions/upload-artifact")
        )
        self.assertIn("always()", str(upload.get("if") or ""))


if __name__ == "__main__":
    unittest.main()
