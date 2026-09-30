"""MVP boundary checks for post-MVP PR-Agent material, and for the consumer
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

import pathlib
import re
import unittest

from continuum.config import load_config
from continuum.review import reconcile as reconcile_module
from tests import support

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIVE = ROOT / ".github" / "workflows"
REFERENCE = ROOT / "reference" / "post-mvp-pr-agent-workflows"
CONSUMER = ROOT / "fixtures" / "consumer-repo"
CONSUMER_WORKFLOWS = CONSUMER / ".github" / "workflows"


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


class MvpBoundaryTests(unittest.TestCase):
    def test_pr_agent_workflows_are_not_active_in_mvp(self):
        self.assertFalse((ACTIVE / "pr-agent.yml").exists())
        self.assertFalse((ACTIVE / "pr-agent-comment.yml").exists())

    def test_post_mvp_pr_agent_reference_is_preserved(self):
        self.assertTrue((REFERENCE / "pr-agent.yml").is_file())
        self.assertTrue((REFERENCE / "pr-agent-comment.yml").is_file())

    def test_reference_is_non_executable_by_github_actions(self):
        for path in REFERENCE.glob("*.yml"):
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
        # One reference, naming the release entrypoint, at something immutable.
        # This is the whole selection surface ADR-0002 asks for, and the property
        # worth defending is the *count*: a second reference is a second place that
        # decides which Continuum this repository runs, and two references that
        # happen to agree today can be moved independently tomorrow.
        seen = []
        for name, text in self.workflow_texts().items():
            for line in text.splitlines():
                if "uses:" not in line or "kodmial/continuum/" not in line:
                    continue
                if line.lstrip().startswith("#"):
                    # A comment documenting the line an operator edits is not a
                    # reference; GitHub reads code, and so does this.
                    continue
                seen.append((name, line))
                self.assertIn(".github/", line, name)
                # A branch ref is a moving target, and this token is write
                # authority. `@main` would mean the consumer's security model is
                # only as good as whatever `main` holds when the workflow next
                # fires, which is exactly the property a reviewer cannot check
                # by reading the file. A bare `v1` is the same hazard wearing a
                # version number.
                ref = line.rsplit("@", 1)[1].strip()
                self.assertRegex(
                    ref,
                    r"^(?:v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
                    r"|[0-9a-f]{40})$",
                    f"{name} pins {ref!r}, not an exact release or a full commit",
                )
        # Guard against the loop vacuously passing on an empty fixture.
        self.assertEqual(len(seen), 1, seen)
        self.assertIn(".github/workflows/consumer.yml@", seen[0][1])

    def test_the_consumer_declares_when_to_call_and_never_what_the_outcome_is(self):
        # `with:` is where a consumer states the policy it owns: which of its own
        # workflows runs, which of its own gates block, where its contract files
        # live. The merge posture is not in that set, because a second place to
        # declare `review` or `release` is a second thing that can disagree with
        # the file a reviewer reads.
        keys = with_inputs(self.workflow_texts()["continuum.yml"])
        self.assertNotIn("review", keys)
        self.assertNotIn("release", keys)
        self.assertTrue(keys, "the ingress passes no inputs at all")

    def test_the_consumer_names_its_own_workflows_where_the_controllers_need_them(self):
        keys = with_inputs(self.workflow_texts()["continuum.yml"])
        # Both halves of the control plane are entered by dispatching this same
        # file, because GitHub cannot dispatch a reusable workflow. A dispatch
        # carrying a `mode` runs the agent; a bare dispatch reconciles.
        self.assertEqual(keys["opencode_workflow"], "continuum.yml")
        self.assertEqual(keys["reconciler_workflow"], "continuum.yml")
        # A release hook that lives in another repository cannot be dispatched by
        # this one, so this name has to be the consumer's own file, not
        # `owner/repo/.github/workflows/thing.yml`.
        self.assertEqual(keys["release_workflow"], "continuum-release.yml")
        # And the contract files are the consumer's, versioned with the code.
        self.assertEqual(keys["config_path"], ".github/continuum.yml")
        self.assertEqual(keys["queue_config_path"], ".continuum.yml")
        # Scheduling policy is the consumer's, declared once as inputs rather
        # than duplicated in a config file the controller would have to find.
        for policy in ("wip_limit", "lease_minutes", "max_attempts"):
            self.assertIn(policy, keys)

    def test_the_fixture_reaches_the_review_loop_through_the_generic_surface(self):
        ingress = self.workflow_texts()["continuum.yml"]
        # Nothing the wake-up carries into the control plane is authority: the gate
        # re-derives trust, the HEAD, and the contract from live state. In
        # particular no event-derived ref or commit reaches it, because those are
        # the two things an untrusted author controls. With one ingress this is a
        # whole-file check, which is stronger than it was when each controller had
        # its own file.
        self.assertNotIn("github.event.pull_request.head", ingress)
        self.assertNotIn("head_sha", ingress)
        # `head_ref` is declared, because a dispatch carrying a `mode` runs the
        # agent and the agent has to be told which branch to work on. It is not
        # *passed*: the reconciling jobs get it from a live dispatch, not from
        # the event payload.
        self.assertNotIn("head_ref", with_inputs(ingress))
        self.assertIn("head_ref:", ingress)

    def test_the_fixture_owns_its_agent_runner_and_toolchain(self):
        # Where the agent runs and which toolchain it gets are consumer policy, so
        # they are inputs. With one ingress they are inputs of the release
        # entrypoint, and the fixture leaves them at their defaults -- a consumer
        # that needs a different runner writes one line in the same file it
        # already edits to move the version.
        entrypoint = (ACTIVE / "consumer.yml").read_text(encoding="utf-8")
        for name in ("runner:", "swift_version:", "model:"):
            self.assertIn(name, entrypoint)
        self.assertIn("default: ubuntu-latest", entrypoint)
        # The agent modes are dispatched through the same ingress, so the entry
        # has to declare them as its own `workflow_dispatch` inputs for the
        # controllers' dispatches to be accepted.
        for mode in ("issue_number", "pr_number", "head_ref", "run_id", "rungs_ruled_out"):
            self.assertIn(mode + ":", self.workflow_texts()["continuum.yml"])

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
