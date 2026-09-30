"""MVP boundary checks for the PR-Agent provider material, and for the consumer
contract as a consumer would actually adopt it.

The engine-level behaviour of the two toggles is pinned in
``tests/test_review_repair.py``. What that cannot prove is that a *consumer* can
reach either posture, because a consumer's two halves live in different files:
the switches in ``.github/continuum.yml``, and the wiring in the thin workflows
it commits. The engine tests would still pass if every reusable workflow still
asked for a caller-supplied boolean, in which case ``review: true`` and
``release: true`` would be unreachable for anyone but Continuum itself.

So these read the fixture the way a consumer reads it — its contract file, its
workflow names, its dispatch surface — and drive the engine with those exact
values. A change that moves the switch out of the file, or names a reviewer or a
project on the way, fails here.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

import yaml

from continuum.config import load_config
from continuum.review import pr_agent as pr_agent_module
from continuum.review import reconcile as reconcile_module
from tests import support

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIVE = ROOT / ".github" / "workflows"
CONSUMER = ROOT / "fixtures" / "consumer-repo"
CONSUMER_WORKFLOWS = CONSUMER / ".github" / "workflows"


def _load(path: pathlib.Path) -> dict:
    """Read a workflow the way GitHub reads it."""
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if True in document:
        # PyYAML reads `on:` as the boolean True, which is YAML 1.1 being
        # pedantic about a word that is not a boolean.
        document["on"] = document.pop(True)
    return document


def with_inputs(text: str) -> dict:
    """The `with:` keys a reusable-workflow call passes, as a plain dict.

    A hand-rolled scan rather than a full YAML load, because these are read as
    text on purpose: the property under test is what a reviewer sees in the file,
    not what a parser recovers from it.
    """

    match = re.search(r"^\s+with:\n((?:\s{6,}\S.*(?:\n|$))+)", text, re.MULTILINE)
    if not match:
        return {}
    body = match.group(1)
    base = len(body) - len(body.lstrip(" "))
    keys = {}
    for line in body.splitlines():
        if not line.strip() or len(line) - len(line.lstrip(" ")) != base:
            continue
        key, _, value = line.strip().partition(":")
        keys[key] = value.strip()
    return keys


def dispatch_inputs(text: str) -> set:
    """The input names a consumer's own workflow_call surface declares."""
    match = re.search(
        r"^\s*workflow_call:\n\s*inputs:\n((?:\s{4,}\S.*(?:\n|$))+)", text, re.MULTILINE
    )
    if not match:
        return set()
    return set(re.findall(r"^\s*(\w+):\s*$", match.group(1), re.MULTILINE))


class MvpBoundaryTests(unittest.TestCase):
    def test_the_pr_agent_provider_surface_is_active(self):
        for name in ("pr-agent.yml", "pr-agent-comment.yml"):
            with self.subTest(workflow=name):
                self.assertTrue((ACTIVE / name).is_file(), name)

    def test_the_queue_dispatches_a_workflow_continuum_actually_ships(self):
        # The fixture's queue dispatches `pr-agent.yml` in the consumer's own
        # repository. That name is now a workflow Continuum publishes, so the
        # dispatch target is a reusable capability rather than a name with no
        # implementation behind it.
        config = load_config(str(CONSUMER / ".continuum.yml"))
        self.assertEqual(config.review.queue.dispatch_workflow, "pr-agent.yml")
        self.assertTrue((ACTIVE / config.review.queue.dispatch_workflow).is_file())

    def test_reference_material_is_non_executable_by_github_actions(self):
        for path in (ROOT / "reference").rglob("*.yml"):
            self.assertNotIn(".github/workflows", str(path))


class ConsumerToggleTests(unittest.TestCase):
    """The two-boolean contract, in both postures, from real fixture files."""

    CONTRACT = CONSUMER / ".github" / "continuum.yml"
    REPO_CONTRACT = ROOT / ".github" / "continuum.yml"

    def test_the_enabled_fixture_activates_the_single_review_implementation(self):
        enabled = load_config(str(self.CONTRACT))
        # The consumer does not choose; it only turns the loop on. The provider
        # is what `true` resolves to inside the engine, and no configuration
        # line in the consumer's own file names or selects one. Comments are
        # excluded deliberately: prose may explain the choice, but it may not
        # make one.
        self.assertEqual(enabled.review.provider, "coderabbit")
        declared = [
            line
            for line in self.CONTRACT.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            sorted(declared), ["release: false", "review: true"], declared
        )

    def test_the_disabled_fixture_resolves_to_no_traffic_at_all(self):
        # Continuum's own contract is the disabled posture: it is not a consumer
        # product, so adopting the contract declares nothing.
        disabled = load_config(str(self.REPO_CONTRACT))
        self.assertEqual(disabled.review.provider, "none")
        self.assertEqual(disabled.release.targets, ())

    def test_both_fixtures_declare_release_without_declaring_a_target(self):
        for path in (self.CONTRACT, self.REPO_CONTRACT):
            with self.subTest(path=path.name):
                config = load_config(str(path))
                self.assertEqual(config.release.targets, ())


class ConsumerConfigTests(unittest.TestCase):
    """The fixture is documentation that executes, so it must stay valid."""

    def setUp(self):
        self.config = load_config(str(CONSUMER / ".continuum.yml"))

    def test_the_fixture_selects_a_provider_and_a_queue(self):
        self.assertEqual(self.config.review.provider, "pr-agent")
        queue = self.config.review.queue
        self.assertEqual(queue.ready_label, "review-ready")
        self.assertIn("review-paused", queue.block_labels)
        self.assertEqual(queue.priority_labels[0], "priority:p0")
        self.assertEqual(queue.cooldown_minutes, 60)

    def test_the_fixture_still_resolves_provider_credentials_by_name(self):
        self.assertEqual(
            self.config.review.pr_agent.api_key_secret, "PR_AGENT_API_KEY"
        )


class ProviderConsumerTests(unittest.TestCase):
    """What adopting the provider costs a consumer: configuration and a pin.

    `fixtures/provider-consumer` is the repository that stopped implementing
    review. It is the acceptance criterion as a file, so the assertions here are
    about the *absence* of implementation: no provider image, no engine, no
    model routing, no diff, no gate. What remains has to be exactly the wiring
    GitHub forces a consumer to own -- the wake-up and the credential mapping.
    """

    FIXTURE = ROOT / "fixtures" / "provider-consumer"
    ENTRY = FIXTURE / ".github" / "workflows" / "pr-agent.yml"
    REUSABLE = ACTIVE / "pr-agent.yml"

    def setUp(self):
        self.config = load_config(str(self.FIXTURE / ".continuum.yml"))
        self.entry = _load(self.ENTRY)
        self.reusable = _load(self.REUSABLE)

    def test_the_repository_configures_the_provider_and_names_its_credentials(self):
        self.assertEqual(self.config.review.provider, "pr-agent")
        self.assertTrue(self.config.review.enabled)
        pr_agent = self.config.review.pr_agent
        self.assertEqual(pr_agent.api_base_var, "PR_AGENT_API_BASE")
        self.assertEqual(pr_agent.model_var, "PR_AGENT_MODEL")
        self.assertEqual(pr_agent.api_key_secret, "PR_AGENT_API_KEY")

    def test_the_entry_contains_no_implementation(self):
        # Anything here would be a second copy of something Continuum already
        # owns, and the copy would be the one that silently rots.
        for banned in (
            "docker://",
            "pragent/pr-agent",
            "actions/checkout",
            "continuum.cli",
            "PYTHONPATH",
            "openai/",
            "CONFIG.MODEL",
            "OPENAI.",
            "gh api",
            "git ",
        ):
            with self.subTest(needle=banned):
                self.assertNotIn(banned, self.ENTRY.read_text(encoding="utf-8"))
        job = self.entry["jobs"]["review"]
        # A job that only calls the shared surface has no steps to get wrong.
        self.assertNotIn("steps", job)
        self.assertNotIn("runs-on", job)

    def test_the_entry_calls_exactly_one_pinned_continuum_surface(self):
        job = self.entry["jobs"]["review"]
        uses = job["uses"]
        self.assertRegex(
            uses,
            r"^kodmial/continuum/\.github/workflows/pr-agent\.yml@[0-9a-f]{40}$",
            "the consumer must select one exact Continuum release, and the "
            "review provider is the surface it selects",
        )

    def test_the_entry_passes_exactly_what_the_shared_surface_declares(self):
        # Passing an input the surface does not declare is an interface that
        # has drifted from the implementation. Omitting one is only safe when
        # the surface already decided it, so an omitted input must carry a
        # default rather than being left to chance.
        job = self.entry["jobs"]["review"]
        call = self.reusable["on"]["workflow_call"]
        self.assertEqual(sorted(job["secrets"]), sorted(call["secrets"]))
        self.assertLessEqual(set(job["with"]), set(call["inputs"]))
        for name, declared in call["inputs"].items():
            if name in job["with"]:
                continue
            with self.subTest(input=name):
                self.assertIn("default", declared, "{} is neither passed nor defaulted".format(name))
        for name in ("pr_number", "head_sha"):
            with self.subTest(input=name):
                self.assertIn(name, job["with"])

    def test_the_entry_declares_exactly_what_the_queue_dispatches(self):
        # The queue is what calls this file, so its wake-up contract has to be
        # read from the same place the dispatcher builds it.
        request = pr_agent_module.queue_request(
            self.config.review.pr_agent,
            pr_number=7,
            head_sha="a" * 40,
            kind="retry",
        )
        self.assertEqual(request.workflow, "pr-agent.yml")
        self.assertEqual(request.workflow, self.config.review.queue.dispatch_workflow)
        dispatch = self.entry["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(sorted(request.inputs), sorted(dispatch))
        self.assertTrue(dispatch["pr_number"]["required"])

    def test_the_dispatch_declares_the_types_the_queue_actually_sends(self):
        # The queue dispatches over the REST API, where every input is a string,
        # and the reusable surface takes the same value onward. Declaring a
        # `number` here would depend on the API coercing it, which is exactly
        # the kind of undocumented behaviour that fails on the day it matters and
        # passes in a test.
        request = pr_agent_module.queue_request(
            self.config.review.pr_agent,
            pr_number=7,
            head_sha="a" * 40,
            kind="initial",
        )
        for name, value in request.inputs.items():
            with self.subTest(input=name):
                self.assertIsInstance(value, str)
        dispatch = self.entry["on"]["workflow_dispatch"]["inputs"]
        for name, value in request.inputs.items():
            with self.subTest(input=name):
                self.assertEqual(dispatch[name]["type"], "string")
        call = self.reusable["on"]["workflow_call"]["inputs"]
        for name in request.inputs:
            # `mode` stays in this repository, so only the inputs that actually
            # cross the call boundary have a second declared type to match.
            if name not in call:
                continue
            with self.subTest(input=name):
                self.assertEqual(call[name]["type"], "string")

    def test_the_entry_never_hands_a_secret_anything_but_a_name(self):
        # The endpoint, the model, and the credential all arrive as repository
        # variable and secret names, so a reviewer can read what the consumer
        # asks for without the file holding anything sensitive.
        job = self.entry["jobs"]["review"]
        wired = json.dumps(job["with"])
        for name in (
            self.config.review.pr_agent.api_base_var,
            self.config.review.pr_agent.model_var,
        ):
            with self.subTest(variable=name):
                self.assertIn(f"vars.{name}", wired)
        self.assertNotRegex(wired, r"https?://")
        self.assertEqual(
            job["secrets"]["pr_agent_api_key"],
            "${{ secrets.%s }}" % self.config.review.pr_agent.api_key_secret,
        )


class ConsumerWiringTests(unittest.TestCase):
    """The fixture's committed wiring, read the way a consumer reads it.

    A reusable workflow cannot subscribe to a consumer's own events, so the
    consumer necessarily writes entry workflows. Those files are the only place
    a reviewer will look to decide whether adopting Continuum means learning a
    reviewer, a provider credential, or a platform, so they are asserted to
    contain none of it: the consumer declares when to call and which of its own
    workflows to call, and the shared controller holds everything else.

    They are also the only place a consumer's trust boundary is written down, so
    what they pass is checked as carefully as what they omit.
    """

    def workflow_texts(self):
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(CONSUMER_WORKFLOWS.glob("*.yml"))
        }

    def test_no_consumer_workflow_names_a_reviewer_a_platform_or_a_project(self):
        for name, text in self.workflow_texts().items():
            lowered = text.lower()
            for banned in (
                "coderabbit",
                "pr-agent",
                "pr_agent",
                "review-bot",
                "macos",
                "xcode",
                "avfoundation",
            ):
                self.assertNotIn(banned, lowered, f"{name} names {banned}")
            # `swift_version` is allowed: a consumer may need a language
            # toolchain, and naming the language it needs is not naming the
            # platform Continuum assumes. The platform is what must not appear.
            #
            # The credential story is the same shape. The only secrets a consumer
            # supplies are the model key its own runner needs and the default
            # token; a reviewer credential would mean the boolean in the
            # contract file is not the whole of the switch.
            for secret in set(re.findall(r"secrets\.([A-Z0-9_]+)", text)):
                self.assertIn(secret, ("OPENCODE_API_KEY", "GITHUB_TOKEN"), name)

    def test_every_consumer_workflow_calls_a_pinned_continuum_surface(self):
        seen = 0
        for name, text in self.workflow_texts().items():
            for line in text.splitlines():
                if "uses:" not in line or "kodmial/continuum/" not in line:
                    continue
                seen += 1
                self.assertIn(".github/", line, name)
                # A branch ref is a moving target, and this token is write
                # authority. `@main` would mean the consumer's security model is
                # only as good as whatever `main` holds when the workflow next
                # fires, which is exactly the property a reviewer cannot check
                # by reading the file.
                ref = line.rsplit("@", 1)[1].strip()
                self.assertRegex(
                    ref,
                    r"^[0-9a-f]{40}$",
                    f"{name} pins {ref!r}, not an immutable commit",
                )
        # Guard against the loop vacuously passing on an empty fixture.
        self.assertGreater(seen, 0)

    def test_the_consumer_declares_when_to_call_and_never_what_the_outcome_is(self):
        # `with:` is where a consumer states the policy it owns: which of its own
        # workflows runs, which of its own gates block, where its contract file
        # lives. The merge posture is not in that set, because a second place to
        # declare `review` or `release` is a second thing that can disagree with
        # the file a reviewer reads.
        for name in ("continuum-auto-merge.yml", "review.yml", "continuum-scheduler.yml"):
            keys = with_inputs(self.workflow_texts()[name])
            self.assertNotIn("review", keys, name)
            self.assertNotIn("release", keys, name)
            self.assertTrue(keys, f"{name} passes no inputs at all")

    def test_the_consumer_names_its_own_workflows_where_the_controllers_need_them(self):
        merge = with_inputs(self.workflow_texts()["continuum-auto-merge.yml"])
        self.assertEqual(merge["config_path"], ".github/continuum.yml")
        # A release or scheduler hook that lives in another repository cannot be
        # dispatched by this one, so these names have to be the consumer's own
        # files, not `owner/repo/.github/workflows/thing.yml`.
        for hook in ("release_workflow", "scheduler_workflow"):
            self.assertEqual(merge[hook], f"continuum-{hook.split('_')[0]}.yml")

        scheduler = with_inputs(self.workflow_texts()["continuum-scheduler.yml"])
        self.assertEqual(scheduler["opencode_workflow"], "continuum-opencode.yml")
        # Scheduling policy is the consumer's, declared once as inputs rather
        # than duplicated in a config file the controller would have to find.
        self.assertIn("wip_limit", scheduler)
        self.assertIn("lease_minutes", scheduler)

    def test_the_fixture_reaches_the_review_loop_through_the_generic_surface(self):
        review = self.workflow_texts()["review.yml"]
        self.assertIn("consumer-review-gate.yml@", review)
        # The wake-up may name a candidate pull request, but nothing it carries
        # is authority: the gate re-derives trust, the HEAD, and the contract
        # from live state. In particular no event-derived ref or commit is
        # passed, because those are the two things an untrusted author controls.
        self.assertNotIn("github.event.pull_request.head", review)
        self.assertNotIn("head_ref", review)
        self.assertNotIn("head_sha", review)

    def test_the_fixture_owns_its_agent_runner_and_toolchain(self):
        agent = self.workflow_texts()["continuum-opencode.yml"]
        # Where the agent runs is an input with a default, so the same shared
        # workflow serves a consumer that needs a particular runner and one that
        # is content with the default.
        self.assertIn("runner:", agent)
        self.assertIn("default: ubuntu-latest", agent)
        # And the toolchain is an input too, defaulted to none.
        self.assertIn("swift_version:", agent)
        self.assertIn("review-fix", agent)
        # The agent workflow is dispatched by the controllers, so it must accept
        # exactly the inputs they send -- and nothing the consumer invented.
        declared = set(with_inputs(agent)) | dispatch_inputs(agent)
        self.assertEqual(
            sorted(declared),
            sorted(
                [
                    "mode",
                    "issue_number",
                    "pr_number",
                    "head_ref",
                    "run_id",
                    "rungs_ruled_out",
                    "runner",
                    "swift_version",
                    "model",
                ]
            ),
        )

    def test_the_fixture_release_hook_is_a_thin_entrypoint(self):
        release = self.workflow_texts()["continuum-release.yml"]
        # It receives the merge commit, so a re-driven dispatch after an
        # interrupted hand-off is deduplicable on the consumer's side rather
        # than publishing twice.
        self.assertIn("source_sha", release)
        self.assertIn("SOURCE_SHA", release)
        self.assertNotIn("uses: kodmial/continuum/", release)


class ConsumerPostureTests(unittest.TestCase):
    """The engine, driven with the fixture's own contract and workflow names.

    This is the acceptance proof for both postures: the switch lives in a file a
    consumer can commit, and the dispatch names a workflow the consumer owns.
    Nothing else has to be configured, so nothing else is checked.
    """

    OPEN = "This misses the null guard around the parsed payload."
    BODY = "Actionable review comments:\n\n- " + OPEN + "\n"
    HEAD_REF = "opencode/issue11-x"

    def reviewed_pull_request(self):
        """A pull request whose current HEAD carries one actionable finding."""
        head = support.HEAD_A
        return support.FakeGitHub(
            head=head,
            reviews=[
                support.coderabbit_review(
                    "CHANGES_REQUESTED", head_sha=head, body=self.BODY
                )
            ],
            threads=[
                support.coderabbit_thread(
                    "PRRT_a", body=self.OPEN, comment_id=500
                )
            ],
            review_comments=[
                support.review_comment(
                    self.OPEN,
                    path="app.py",
                    line=42,
                    login=support.CODERABBIT_LOGIN,
                    commit_id=head,
                    comment_id=501,
                )
            ],
        )

    def reconcile(self, contract: str):
        fake = self.reviewed_pull_request()
        fake.pr = {"number": 7, "head": {"sha": support.HEAD_A, "ref": self.HEAD_REF}}
        seen: list = []

        def dispatch(workflow, ref, inputs):
            seen.append({"workflow": workflow, "ref": ref, "inputs": inputs})

        plan = reconcile_module.reconcile(
            fake,
            load_config(contract),
            7,
            apply=True,
            dispatch=dispatch,
            # The consumer's own agent workflow, which is what the fixture
            # commits and what a real consumer would replace with its own.
            dispatch_workflow="continuum-opencode.yml",
            dispatch_ref="main",
        )
        return plan, fake, seen

    def test_the_disabled_posture_costs_the_consumer_nothing(self):
        plan, fake, seen = self.reconcile(str(ROOT / ".github" / "continuum.yml"))

        self.assertFalse(plan["enabled"])
        self.assertEqual(seen, [])
        # Silent is the whole requirement: a deliberate opt-out must not produce
        # provider traffic, a review status, or a comment on the consumer's pull
        # requests. Anything here is a consumer who said "no" and got "yes".
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.statuses_created, [])
        self.assertEqual(fake.comments_created, [])

    def test_the_enabled_posture_needs_nothing_but_the_boolean(self):
        contract = CONSUMER / ".github" / "continuum.yml"
        plan, _fake, seen = self.reconcile(str(contract))

        self.assertTrue(plan["enabled"])
        self.assertEqual(plan["repair"]["action"], "repair")
        # One bounded dispatch, to the consumer's own workflow, in the generic
        # mode. If this ever needs a provider name or a platform, the consumer
        # had to learn something the contract does not contain.
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["workflow"], "continuum-opencode.yml")
        self.assertEqual(seen[0]["inputs"]["mode"], "review-fix")
        self.assertEqual(seen[0]["inputs"]["pr_number"], "7")
        self.assertEqual(seen[0]["inputs"]["head_ref"], self.HEAD_REF)


if __name__ == "__main__":
    unittest.main()
