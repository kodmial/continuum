#!/usr/bin/env python3
"""Static audit of Continuum's release-selection graph.

ADR-0002 states the requirement in one sentence: a consumer selects the whole
Continuum implementation with one literal exact release reference, and nothing
inside the selected release may reach outside that reference. This file is the
mechanical form of that sentence.

It exists because every failure it catches is invisible in review. A workflow
that calls ``.github/actions/engine-path`` at its own SHA still reads as a
reasonable line; a reusable-workflow input that defaults to ``main`` still reads
as an ordinary defaulted input. Both mean a consumer that pinned ``v0.1.0`` can
execute code from a revision nobody selected, and neither appears in the
consumer's own file, so the consumer cannot review it.

Three classes of escape path are asserted against:

* **A floating internal reference.** Anything in a production workflow that
  resolves Continuum code from a branch, a moving tag, or an abbreviated SHA.
  ``job.workflow_sha`` is the only acceptable source, because it is the commit
  the consumer's single pin already selected.
* **An independent Continuum revision pin.** A ``uses:`` or a checkout ``ref:``
  naming Continuum at a revision other than the running workflow's own commit.
  Two pins that happen to agree today are still two places that decide which
  implementation a consumer runs.
* **A per-module pin.** A consumer ingress naming a Continuum workflow other
  than the release entrypoint, or naming more than one Continuum reference.

Run with::

    python3 -m unittest discover -s .github/tests -p 'test_*.py'
"""

import json
import pathlib
import re
import subprocess
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The pin tool ships as part of the release a consumer pins, so the audit is
#: allowed to import it and assert that it agrees. It lives under `src/`, and CI
#: runs this suite without `PYTHONPATH=src`, so the path is added here rather
#: than assumed.
sys.path.insert(0, str(REPO_ROOT / "src"))
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACTION_DIR = REPO_ROOT / ".github" / "actions"

#: The Continuum repository, as a consumer's ingress names it.
CONTINUUM = "kodmial/continuum"

#: The one file a consumer may name. ADR-0002 makes the release entrypoint the
#: whole selection surface; naming any other Continuum workflow is a per-module
#: pin, which lets a repository hold a mixed-version control plane.
RELEASE_ENTRYPOINT = ".github/workflows/consumer.yml"

#: The release entrypoint itself. Checked separately: it is the file every
#: consumer names, so its own routing is what makes one pin select a coherent
#: graph.
ENTRYPOINT = "consumer.yml"

#: Workflows that execute a consumer's control plane. Every one of them has to
#: reach Continuum code from the release commit the consumer pinned, so the
#: whole set is held to the rule rather than the ones someone remembered.
PRODUCTION_CONSUMER_WORKFLOWS = (
    "consumer-auto-merge.yml",
    "consumer-child-dispatcher.yml",
    "consumer-child-pr-review.yml",
    "consumer-child-review.yml",
    "consumer-child-worker.yml",
    "consumer-opencode.yml",
    "consumer-repair.yml",
    "consumer-review-gate.yml",
    "consumer-scheduler.yml",
    "release.yml",
)

#: Continuum dogfooding the same control plane in this repository, held to the
#: same rule for the same reason: a floating ref here is the shape an engineer
#: copies into a consumer.
OWN_CONTROL_PLANE = (
    "auto-merge.yml",
    "issue-scheduler.yml",
    "opencode-repair.yml",
    "opencode.yml",
    "review-gate.yml",
    "review-queue.yml",
)

#: Continuum's own build, verification, and publication. These run against
#: *this* repository at its own commit and never resolve Continuum code from
#: another revision, so the release-selection rules do not bind them; they are
#: listed so that a new workflow cannot appear without a reviewer deciding which
#: side it is on.
OWN_REPOSITORY_TOOLING = (
    "ci.yml",
    "continuum-migrate.yml",
    "release-bun-binary.yml",
    "publish-continuum.yml",
)

#: The shadow harness is the one workflow allowed to name a Continuum revision,
#: and it may: it replays a *recorded* run and therefore has to be able to name
#: the revision that recorded it. What it must never do is accept a branch, and
#: it must resolve from the running workflow's own commit when it is called
#: rather than dispatched. See `ShadowEngineSelectionTests`.
SHADOW = "continuum-shadow.yml"

#: Consumer repositories Continuum ships as documentation. These are the
#: ingresses a new repository is written from, so the rules are asserted against
#: them exactly as they are against Continuum's own workflows: a rule that only
#: covered this repository would let the unsafe shape reach every consumer that
#: copies the fixture.
CONSUMER_REPOSITORIES = {
    "consumer-repo": REPO_ROOT / "fixtures" / "consumer-repo" / ".github" / "workflows",
    "delegation-parent": REPO_ROOT / "fixtures" / "delegation-parent" / ".github" / "workflows",
}

#: The exact SemVer the shipped consumer ingresses are expected to select. A
#: literal, because the version is consumer-owned dependency state and belongs
#: in the consumer's reviewable file rather than in Continuum's tests.
EXPECTED_CONSUMER_REF = "v0.1.0"

#: `uses: <value>` with an optional trailing comment.
USES = re.compile(r"^\s*uses:\s*(?P<value>[^\s#]+)")

#: A full commit SHA. Accepted alongside an exact release tag, and equally
#: immutable -- which is why it is not floating.
COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: An exact SemVer release tag.
EXACT_RELEASE = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")

#: A branch. It moves without the reference changing, so naming it selects a
#: different commit after every merge.
BRANCH = re.compile(r"^(?:main|master|HEAD|develop|trunk)$")

#: A bare or compatibility tag -- `v1`, `v1.2`. It names a *range* of releases,
#: so the commit it resolves to changes without the reference changing.
MOVING_TAG = re.compile(r"^v\d+(?:\.\d+){0,1}$")

#: A shortened commit. GitHub resolves one today, but it names more than one
#: revision for as long as it is shorter than the full forty characters.
ABBREVIATED_COMMIT = re.compile(r"^[0-9a-f]{7,39}$")

#: Anything that cannot name exactly one immutable commit. Used where the point
#: is the negative case, so it deliberately overlaps `EXACT_RELEASE`'s
#: neighbourhood: an unrecognised version-shaped string is floating by default.
FLOATING = re.compile(
    r"^(?:main|master|HEAD|develop|trunk)$|^v\d+(?:\.\d+)*$|^[0-9a-f]{7,39}$"
)

#: An input name that would be a version selector rather than repository policy.
VERSION_INPUT = re.compile(r"(?:^|_)(?:ref|sha|commit|version|pin)(?:$|_)")

#: Version-shaped inputs that are *not* Continuum version selectors, classified
#: explicitly. Naming them here rather than widening the audit to "any input with
#: `ref` or `version` in its name" keeps the audit pointed at the decision
#: ADR-0002 rules out -- which Continuum revision a run executes -- instead of at
#: every string that happens to end in `_ref`. The list is closed on purpose: a
#: new name here needs a sentence explaining why it is not a version selector,
#: and `TestContinuumVersionInputs` asserts the classification stays exhaustive.
#:
#: - `head_ref` is the consumer's pull request branch. It is repository state,
#:   and it is what the agent is asked to repair.
#: - `swift_version` is the toolchain to build the runtime with. It selects a
#:   Swift release, not a Continuum one.
#: - `opencode_release_*` describe the agent binary's *distribution* release --
#:   the OpenCode executable a consumer installs, verified by digest. That is the
#:   tool the agent runs on, not the control plane that runs the agent.
#: - `version` and `source_sha` on `release.yml` identify the *consumer
#:   product release* being built. They are bound to the consumer's tag/commit,
#:   not a selector for the Continuum implementation executing the workflow.
#: - `engine_ref` is the shadow harness's, and is handled on its own terms: see
#:   `ShadowEngineSelectionTests`.
NON_CONTINUUM_VERSION_INPUTS = frozenset(
    {
        "head_ref",
        "swift_version",
        "opencode_release_tag",
        "opencode_release_tag_prefix",
        "opencode_release_asset",
        "opencode_release_repo",
        "opencode_release_sha256",
        "opencode_release_version",
        "opencode_release_source_sha",
        "version",
        "source_sha",
    }
)

#: The shadow harness's one permitted Continuum revision input.
SHADOW_ENGINE_INPUT = "engine_ref"

RUBY_YAML_TO_JSON = r"""
require "yaml"
require "json"
data = YAML.safe_load(File.read(ARGV[0]), aliases: true) || {}
data["on"] = data.delete(true) if data.key?(true)
puts JSON.generate(data)
"""


def load_document(path: pathlib.Path) -> dict:
    result = subprocess.run(
        ["ruby", "-e", RUBY_YAML_TO_JSON, str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def jobs(document: dict) -> dict:
    return document.get("jobs") or {}


def steps_of(job: dict) -> list:
    return job.get("steps") or []


def with_map(step: dict) -> dict:
    return step.get("with") or {}


def workflow_call_inputs(document: dict) -> dict:
    triggers = document.get("on") or {}
    if not isinstance(triggers, dict):
        return {}
    return (triggers.get("workflow_call") or {}).get("inputs") or {}


def workflow_dispatch_inputs(document: dict) -> dict:
    triggers = document.get("on") or {}
    if not isinstance(triggers, dict):
        return {}
    return (triggers.get("workflow_dispatch") or {}).get("inputs") or {}


def triggers_of(document: dict) -> dict:
    triggers = document.get("on") or {}
    return triggers if isinstance(triggers, dict) else {}


#: The `*_WORKFLOW` variables the release dispatches *into*. Every one of them
#: names a file in the consumer's own repository -- the controllers read them
#: from the ingress's `with:` block -- so each is a dispatch into a consumer
#: ingress and therefore an input the ingress has to declare.
#:
#: `RELEASE_WORKFLOW` is deliberately absent: it names the consumer's own
#: release hook, which is a product fact and is not a Continuum surface.
CONTINUUM_DISPATCH_TARGETS = (
    "OPENCODE_WORKFLOW",
    "WORKER_WORKFLOW",
    "REVIEW_WORKFLOW",
    "MANUAL_PR_REVIEW_WORKFLOW",
    "SCHEDULER_WORKFLOW",
    "RECONCILER_WORKFLOW",
)

#: A dispatch command, with its `-f` flags, up to the end of the command. Flags
#: are written both one-per-line with `\`-continuations and folded onto a single
#: long line, so a command ends at a blank line, at a redirection, or at a shell
#: keyword that starts its own command -- never mid-argument.
_COMMAND_END = r"(?=\n[ \t]*\n|\n[ \t]*(?:fi\b|done\b|esac\b|>|\|)|$)"

#: A dispatch command, with its `-f` flags, up to the end of the command.
_GH_WORKFLOW_RUN = re.compile(
    r'gh workflow run "\$(?P<target>\w*WORKFLOW)"(?P<body>.*?)' + _COMMAND_END, re.S
)
_API_DISPATCH = re.compile(
    r"actions/workflows/\$(?P<target>\w*WORKFLOW)/dispatches(?P<body>.*?)" + _COMMAND_END, re.S
)
_JS_DISPATCH = re.compile(
    r"workflow_id:\s*process\.env\.(?P<target>\w*WORKFLOW),(?P<body>.{0,600}?)\}\)", re.S
)
_BASH_INPUT = re.compile(r'-f\s+"(?:inputs\[)?(?P<name>[A-Za-z_]\w*)[\]=]')
_BASH_FLAG = re.compile(r"-f\s+(?P<name>[A-Za-z_]\w*)=")
_JS_INPUTS = re.compile(r"inputs:\s*\{(?P<body>[^}]*)\}", re.S)
_JS_KEY = re.compile(r"\b(?P<name>[A-Za-z_]\w*):")

#: Fields the dispatch endpoints take *besides* `inputs`. `-f ref=main` selects
#: the branch to run the dispatch on; it is not an input of the dispatched
#: workflow, and declaring one named `ref` would be meaningless.
DISPATCH_ENDPOINT_FIELDS = frozenset({"ref"})

#: Every `workflow_dispatch` input the shipped release dispatches into a
#: consumer ingress, by hand, so a new controller cannot add an input the
#: fixtures have not caught up with. `DispatchInputSweepTests` keeps this list
#: honest against the workflows themselves.
EXPECTED_INGRESS_DISPATCH_INPUTS = frozenset(
    {
        "mode",
        "issue_number",
        "pr_number",
        "head_ref",
        "run_id",
        "rungs_ruled_out",
        "repair_timeout_minutes",
        "repair_agent_timeout_seconds",
        "child_id",
        "task_number",
    }
)


def dispatched_input_names() -> dict:
    """Input names the shipped release dispatches, keyed by target variable."""

    found: dict = {}
    for path in sorted(WORKFLOW_DIR.glob("consumer-*.yml")):
        text = path.read_text(encoding="utf-8")
        for pattern in (_GH_WORKFLOW_RUN, _API_DISPATCH):
            for match in pattern.finditer(text):
                target = match.group("target")
                if target not in CONTINUUM_DISPATCH_TARGETS:
                    continue
                body = match.group("body")
                names = found.setdefault(target, set())
                names.update(m.group("name") for m in _BASH_INPUT.finditer(body))
                names.update(m.group("name") for m in _BASH_FLAG.finditer(body))
        for match in _JS_DISPATCH.finditer(text):
            target = match.group("target")
            if target not in CONTINUUM_DISPATCH_TARGETS:
                continue
            inputs = _JS_INPUTS.search(match.group("body"))
            if inputs is not None:
                found.setdefault(target, set()).update(
                    m.group("name") for m in _JS_KEY.finditer(inputs.group("body"))
                )
    for target, names in found.items():
        found[target] = names - DISPATCH_ENDPOINT_FIELDS
    return found


class ReleaseSurfaceBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflows = {
            path.name: load_document(path) for path in sorted(WORKFLOW_DIR.glob("*.yml"))
        }
        cls.raw = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(WORKFLOW_DIR.glob("*.yml"))
        }

    def continuum_pins(self, text: str) -> list:
        """Every `uses:` value that names the Continuum repository."""

        found = []
        for line in text.splitlines():
            match = USES.match(line)
            if match is None:
                continue
            value = match.group("value")
            if value.startswith("./"):
                # A same-repository relative reference is resolved from the
                # caller's commit, so it is not a second version selection.
                continue
            if value.startswith(CONTINUUM + "/"):
                found.append(value)
        return found

    def engine_checkouts(self, name: str):
        """Every step that checks Continuum out, with the ref it names."""

        found = []
        for job_name, job in jobs(self.workflows[name]).items():
            for step in steps_of(job):
                values = with_map(step)
                if values.get("repository") != CONTINUUM:
                    continue
                found.append((job_name, step.get("name"), values.get("ref")))
        return found

    def is_reusable(self, name: str) -> bool:
        return "workflow_call" in (self.workflows[name].get("on") or {})


class ProductionSurfaceTests(ReleaseSurfaceBase):
    """The surface declaration is itself a control, so it is audited too."""

    def test_the_production_surface_is_never_empty(self):
        self.assertTrue(PRODUCTION_CONSUMER_WORKFLOWS)
        for name in PRODUCTION_CONSUMER_WORKFLOWS:
            self.assertIn(name, self.workflows, name)

    def test_the_release_entrypoint_exists_and_is_the_file_consumers_name(self):
        # The path is the contract: a consumer that names anything else is making
        # a per-module choice, so this file has to exist under exactly the name
        # ADR-0002 tells consumers to write.
        self.assertIn(ENTRYPOINT, self.workflows)
        self.assertEqual(RELEASE_ENTRYPOINT, ".github/workflows/" + ENTRYPOINT)

    def test_the_declared_surfaces_are_reusable_not_event_workflows(self):
        # A reusable workflow cannot subscribe to another repository's events, so
        # every controller reached from a consumer's ingress has to be
        # `workflow_call`. An event trigger on one of these means a consumer's
        # control plane can also be entered from Continuum's own repository,
        # which is a second entry point nobody reviews.
        for name in PRODUCTION_CONSUMER_WORKFLOWS + (ENTRYPOINT,):
            with self.subTest(name=name):
                self.assertTrue(self.is_reusable(name), name + " is not reusable")

    def test_every_workflow_is_classified(self):
        classified = (
            set(PRODUCTION_CONSUMER_WORKFLOWS)
            | {ENTRYPOINT, SHADOW}
            | set(OWN_CONTROL_PLANE)
            | set(OWN_REPOSITORY_TOOLING)
        )
        present = {path.name for path in sorted(WORKFLOW_DIR.glob("*.yml"))}
        self.assertEqual(
            sorted(classified),
            sorted(present),
            "a workflow is missing from the release-selection surface declaration",
        )

    def test_the_classified_surfaces_do_not_overlap(self):
        groups = [
            set(PRODUCTION_CONSUMER_WORKFLOWS),
            {ENTRYPOINT, SHADOW},
            set(OWN_CONTROL_PLANE),
            set(OWN_REPOSITORY_TOOLING),
        ]
        for index, left in enumerate(groups):
            for right in groups[index + 1 :]:
                self.assertEqual(sorted(left & right), [])

    def test_the_own_control_plane_reaches_no_continuum_revision_at_all(self):
        # Continuum's own control plane runs from the commit it was checked out
        # at, so it should never reach out for a Continuum revision -- not in a
        # `uses:`, and not in a checkout. Any occurrence means a decision was
        # added about which Continuum a run executes.
        for name in OWN_CONTROL_PLANE + OWN_REPOSITORY_TOOLING:
            with self.subTest(name=name):
                self.assertEqual(self.continuum_pins(self.raw[name]), [])
                for job_name, step_name, _ref in self.engine_checkouts(name):
                    self.fail(f"{name}/{job_name} checks Continuum out; a workflow "
                              "already running from this repository has no business "
                              "selecting a Continuum revision")


class IndependentPinTests(ReleaseSurfaceBase):
    """No workflow may select a Continuum revision other than its own commit."""

    RELEASE_SOURCES = ("${{ job.workflow_sha }}", "${{ github.workflow_sha }}")

    def test_no_workflow_calls_a_continuum_action_at_a_pinned_revision(self):
        # A remote `kodmial/continuum/...@<sha>` reference is a second,
        # independently maintained pin. The only Continuum action a production
        # workflow may call is a *local* one, read out of the checkout it just
        # verified, so it needs no ref of its own.
        for name, text in self.raw.items():
            for value in self.continuum_pins(text):
                with self.subTest(name=name):
                    self.assertTrue(
                        value.startswith("./"),
                        f"{name} calls {value} at a revision of its own; the engine "
                        "must come from the running workflow's release commit",
                    )

    def test_no_reusable_workflow_exposes_a_continuum_version_selector(self):
        # An input that names a *Continuum* revision would let a caller choose
        # which Continuum a run executes. That is the decision ADR-0002 gives the
        # consumer's own file and to nobody else. Inputs that name something
        # else -- the pull request branch, the Swift toolchain, the agent
        # binary's distribution release -- are not that decision, and are
        # classified exhaustively in `NON_CONTINUUM_VERSION_INPUTS`.
        for name in PRODUCTION_CONSUMER_WORKFLOWS + (ENTRYPOINT,):
            for input_name in workflow_call_inputs(self.workflows[name]):
                if not VERSION_INPUT.search(input_name):
                    continue
                with self.subTest(name=name, input=input_name):
                    self.assertIn(
                        input_name,
                        NON_CONTINUUM_VERSION_INPUTS,
                        f"{name} exposes {input_name!r}, a version-shaped input "
                        "that is not classified as repository policy; classify it "
                        "with a reason rather than leaving it unclassified",
                    )

    def test_no_production_surface_selects_a_continuum_revision(self):
        # The direct assertion behind the classification above: nothing on the
        # consumer-facing surface may take the engine revision as an argument.
        # `engine_ref` and its kin are the shape this rules out.
        for name in PRODUCTION_CONSUMER_WORKFLOWS + (ENTRYPOINT,):
            inputs = set(workflow_call_inputs(self.workflows[name]))
            with self.subTest(name=name):
                for banned in ("engine_ref", "continuum_ref", "continuum_sha",
                               "continuum_version", "engine_sha", "engine_version"):
                    self.assertNotIn(banned, inputs)

    def test_the_only_continuum_revision_input_belongs_to_the_shadow_harness(self):
        # One documented exception, held to its own terms. If a second workflow
        # ever grows an engine-revision input, the exception has stopped being
        # an exception and ADR-0002 needs to be revisited rather than widened.
        holders = sorted(
            name
            for name in self.workflows
            if SHADOW_ENGINE_INPUT in workflow_call_inputs(self.workflows[name])
        )
        self.assertEqual(holders, [SHADOW])

    def test_every_continuum_checkout_uses_the_running_workflow_commit(self):
        for name in PRODUCTION_CONSUMER_WORKFLOWS:
            for job_name, step_name, ref in self.engine_checkouts(name):
                with self.subTest(workflow=name, job=job_name, step=step_name):
                    self.assertIn(
                        ref,
                        self.RELEASE_SOURCES,
                        f"{name}/{job_name} checks Continuum out at {ref!r}; a "
                        "consumer pinned at v0.1.0 must not execute a revision "
                        "nobody selected",
                    )

    def test_a_continuum_checkout_without_a_ref_is_refused(self):
        # `actions/checkout` defaults to the repository's default branch. A
        # Continuum checkout with no `ref:` is therefore a checkout of Continuum's
        # `main`, whatever the consumer pinned.
        for name in PRODUCTION_CONSUMER_WORKFLOWS:
            for job_name, step_name, ref in self.engine_checkouts(name):
                with self.subTest(workflow=name, job=job_name, step=step_name):
                    self.assertIsNotNone(
                        ref, f"{name}/{job_name} checks Continuum out with no ref"
                    )

    def test_no_workflow_names_a_floating_continuum_revision(self):
        for name, text in self.raw.items():
            for value in self.continuum_pins(text):
                ref = value.rsplit("@", 1)[-1] if "@" in value else ""
                with self.subTest(name=name, value=value):
                    self.assertFalse(
                        bool(FLOATING.match(ref)),
                        f"{name} pins Continuum at the floating reference {ref!r}",
                    )
            for job_name, step_name, ref in self.engine_checkouts(name):
                with self.subTest(name=name, job=job_name, step=step_name):
                    self.assertFalse(
                        bool(FLOATING.match(str(ref))),
                        f"{name}/{job_name} checks Continuum out at the floating "
                        f"reference {ref!r}",
                    )

    def loads_engine(self, name: str) -> bool:
        """Whether a workflow executes Continuum engine code at all."""
        return bool(self.engine_checkouts(name)) or "./.continuum-engine/" in self.raw[name]

    def test_engine_loading_is_used_from_the_checkout_it_verifies(self):
        # A workflow that runs engine code must reach it through the checkout it
        # just made, verified against the commit the consumer's pin already
        # selected. A remote `kodmial/continuum/.github/actions/engine-path@<sha>`
        # would look equivalent in review and would not be: it is a second pin.
        for name in PRODUCTION_CONSUMER_WORKFLOWS:
            if not self.loads_engine(name):
                continue
            text = self.raw[name]
            with self.subTest(name=name):
                self.assertIn("uses: ./.continuum-engine/.github/actions/engine-path", text)
                self.assertIn("release-sha: ${{ job.workflow_sha }}", text)

    def test_a_workflow_that_loads_no_engine_never_names_one(self):
        # The scheduler only reconciles a queue through the API, so it has no
        # engine to resolve. Asserted rather than left to a list so that adding a
        # checkout to a workflow that did not need one has to be a decision.
        for name in PRODUCTION_CONSUMER_WORKFLOWS:
            if self.loads_engine(name):
                continue
            with self.subTest(name=name):
                self.assertEqual(self.engine_checkouts(name), [])
                self.assertNotIn(".continuum-engine", self.raw[name])

    def test_every_engine_checkout_names_the_same_commit_as_its_engine_action(self):
        # Within one workflow, the commit checked out and the commit asserted to
        # the action have to be the same expression. Deriving both from the
        # running workflow is what makes them equal; spelling one out twice would
        # make it a review question.
        for name in PRODUCTION_CONSUMER_WORKFLOWS:
            if not self.loads_engine(name):
                continue
            checkouts = [ref for _job, _step, ref in self.engine_checkouts(name)]
            with self.subTest(name=name):
                self.assertTrue(checkouts)
                for ref in checkouts:
                    self.assertIn(
                        ref,
                        self.RELEASE_SOURCES,
                        f"{name} resolves the engine at {ref!r}",
                    )
                self.assertIn(
                    "release-sha: ${{ job.workflow_sha }}",
                    self.raw[name],
                    f"{name} must assert the same commit to the engine action",
                )

    def test_the_engine_action_refuses_a_checkout_from_another_commit(self):
        action = (ACTION_DIR / "engine-path" / "action.yml").read_text(encoding="utf-8")
        verifier = (ACTION_DIR / "engine-path" / "verify.py").read_text(encoding="utf-8")
        # A release tag is human-readable; the checkout behind it is a commit.
        # Verifying the two agree is what stops a moved tag or a wrong path from
        # running different engine code than the release the consumer selected.
        #
        # Two separate facts have to hold, and `ref:` alone only proves one of
        # them. `job.workflow_sha` describes the workflow *file* GitHub loaded;
        # it says nothing about which repository the engine was fetched from, so
        # the checkout's own origin is verified too. Without that, a fork at the
        # same commit passes every commit-shaped check.
        for needle in ("rev-parse", "remote.origin.url"):
            self.assertIn(needle, verifier)
        self.assertIn('COMMIT = re.compile(r"^[0-9a-f]{40}$")', verifier)
        # And the checkout is kept out of the consumer's own commits, because the
        # agent commits with `git add -A`.
        self.assertIn('"info"', verifier)
        self.assertIn("exclude", verifier)
        # The action itself is only the shell that calls the verifier and exports
        # the result. Verification logic inlined in YAML is not reviewable, and
        # `test_engine_path.py` can only test what is importable.
        self.assertIn("verify.py", action)
        self.assertIn(">> \"$GITHUB_ENV\"", action)
        self.assertNotIn("rev-parse", action)

    def test_the_engine_action_is_never_referenced_across_repositories(self):
        # A remote reference to the action would need its own ref, which is the
        # second pin ADR-0002 rules out. Only a relative reference, read out of
        # the verified checkout, is allowed to name it.
        for path in sorted(ACTION_DIR.glob("*/action.yml")):
            for line in path.read_text(encoding="utf-8").splitlines():
                match = USES.match(line)
                if match is None or CONTINUUM not in match.group("value"):
                    continue
                self.fail(f"{path.name} is referenced across repositories: {line.strip()}")


class EntrypointRoutingTests(ReleaseSurfaceBase):
    """One pin has to select a coherent graph, which means relative references."""

    DISPATCHED_SURFACES = ("agent", "child_worker", "child_review", "child_pr_review")
    RECONCILING_SURFACES = (
        "scheduler",
        "review",
        "review_queue",
        "repair",
        "merge",
        "children",
    )

    def test_the_entrypoint_names_no_external_continuum_reference(self):
        self.assertEqual(self.continuum_pins(self.raw[ENTRYPOINT]), [])

    def test_every_nested_call_is_a_same_repository_relative_reference(self):
        for job_name, job in jobs(self.workflows[ENTRYPOINT]).items():
            uses = job.get("uses")
            if uses is None:
                continue
            with self.subTest(job=job_name):
                # A relative reference is resolved from the same commit as the
                # `consumer.yml` the consumer pinned. Anything else -- a branch,
                # a tag, a SHA, another repository -- reintroduces a version
                # decision the consumer did not make.
                self.assertTrue(
                    uses.startswith("./.github/workflows/"),
                    f"{ENTRYPOINT}/{job_name} calls {uses!r} instead of a "
                    "same-repository relative reference",
                )
                self.assertNotIn("@", uses)

    def test_the_entrypoint_selects_the_whole_control_plane(self):
        # ADR-0002 requires one pin to cover the scheduler, agent, repair,
        # review, merge, and delegated child work. A controller reachable from
        # nowhere in the entrypoint is one a consumer cannot use, and one removed
        # from here is one a consumer silently loses.
        nested = {
            job["uses"].rsplit("/", 1)[-1]
            for job in jobs(self.workflows[ENTRYPOINT]).values()
            if job.get("uses")
        }
        for required in (
            "consumer-scheduler.yml",
            "consumer-opencode.yml",
            "consumer-repair.yml",
            "consumer-review-gate.yml",
            "consumer-auto-merge.yml",
            "review-queue.yml",
            "consumer-child-dispatcher.yml",
            "consumer-child-worker.yml",
            "consumer-child-review.yml",
            "consumer-child-pr-review.yml",
        ):
            with self.subTest(surface=required):
                self.assertIn(required, nested)

    def test_the_entrypoint_grants_only_what_its_nested_workflows_ask_for(self):
        # Permissions can only narrow through a call chain, so the entrypoint's
        # grant is the ceiling. A nested job asking for something the
        # entrypoint does not hold is a surface that cannot run, and the failure
        # appears as a permission error rather than as a wiring mistake.
        document = self.workflows[ENTRYPOINT]
        ceiling = document.get("permissions") or {}
        self.assertTrue(ceiling, ENTRYPOINT + " must declare its permission ceiling")
        for job_name, job in jobs(document).items():
            granted = job.get("permissions")
            if granted is None:
                continue
            for scope, level in granted.items():
                with self.subTest(job=job_name, scope=scope):
                    self.assertIn(
                        scope,
                        ceiling,
                        f"{ENTRYPOINT}/{job_name} requests {scope}, which the "
                        "entrypoint does not grant",
                    )
                    if level == "write":
                        self.assertEqual(
                            ceiling.get(scope),
                            "write",
                            f"{ENTRYPOINT}/{job_name} writes {scope}, which the "
                            "entrypoint does not grant as write",
                        )

    def test_every_reconciling_surface_is_entered_by_the_same_ingress(self):
        # The agent and the three delegated child surfaces are entered by
        # dispatch, because GitHub cannot dispatch a reusable workflow. Routing
        # them through the entrypoint rather than through further consumer-side
        # files is what keeps a consumer's Continuum reference count at one.
        conditions = {
            name: str(job.get("if", "")) for name, job in jobs(self.workflows[ENTRYPOINT]).items()
        }
        for job_name in self.DISPATCHED_SURFACES:
            with self.subTest(job=job_name):
                self.assertIn(job_name, conditions)
                self.assertIn("github.event_name == 'workflow_dispatch'", conditions[job_name])
        for job_name in self.RECONCILING_SURFACES:
            with self.subTest(job=job_name):
                self.assertIn(job_name, conditions)
                self.assertIn("inputs.mode ==", conditions[job_name])

    def test_a_dispatched_agent_run_never_reconciles_as_well(self):
        # Both halves are safe alone and unsafe together: an agent run that also
        # entered the merge reconciler would let an in-flight repair race the gate
        # that is supposed to judge its result.
        conditions = {
            name: str(job.get("if", "")) for name, job in jobs(self.workflows[ENTRYPOINT]).items()
        }
        for job_name in self.RECONCILING_SURFACES:
            condition = conditions[job_name]
            with self.subTest(job=job_name):
                self.assertTrue(
                    "github.event_name != 'workflow_dispatch'" in condition
                    and "inputs.mode == ''" in condition,
                    f"{ENTRYPOINT}/{job_name} would also run for a dispatched agent",
                )

    def test_the_delegated_child_surfaces_are_selected_by_a_distinct_mode(self):
        # One dispatcher, one consumer ingress, three child implementations. If
        # two surfaces shared a mode, one of them would be unreachable and the
        # dispatcher would silently stop repairing delegated work.
        modes = {}
        for job_name in ("child_worker", "child_review", "child_pr_review"):
            condition = str(jobs(self.workflows[ENTRYPOINT])[job_name].get("if", ""))
            found = re.search(r"inputs\.mode == '([^']+)'", condition)
            self.assertIsNotNone(found, f"{job_name} is not selected by a mode")
            modes[job_name] = found.group(1)
        self.assertEqual(len(set(modes.values())), len(modes), modes)
        for job_name, mode in modes.items():
            with self.subTest(job=job_name):
                nested = jobs(self.workflows[ENTRYPOINT])[job_name]["uses"]
                self.assertIn(mode, self.raw[ENTRYPOINT])
                self.assertTrue(nested.startswith("./.github/workflows/consumer-child-"))

    def test_the_dispatcher_routes_child_work_through_the_declared_modes(self):
        # The mode has to be sent, or the child jobs above are unreachable and
        # delegated work is dispatched into a workflow that ignores it.
        dispatcher = self.raw["consumer-child-dispatcher.yml"]
        for mode in ("child:worker", "child:review", "child:pr-review"):
            with self.subTest(mode=mode):
                self.assertIn(f"-f mode={mode}", dispatcher)

    def agent_modes(self) -> set:
        """The launch modes the shared agent implementation accepts.

        Read from the agent's own gate rather than from a list here, so the two
        cannot drift: the agent refuses a mode it does not implement, and an
        entrypoint that routed one would produce a failed run rather than a
        working one.
        """

        match = re.search(
            r"contains\(fromJSON\('\[(?P<modes>[^\]]+)\]'\),\s*inputs\.mode\)",
            self.raw["consumer-opencode.yml"],
        )
        self.assertIsNotNone(
            match, "the agent no longer declares its launch modes as a literal list"
        )
        return {mode.strip().strip("\"'") for mode in match.group("modes").split(",")}

    def test_the_agent_modes_are_exactly_the_modes_the_agent_implements(self):
        # The entrypoint routes a dispatch to the agent, and the agent refuses a
        # mode it does not implement. A test that only excluded `child:*` would
        # pass on a typo in an agent mode and dispatch a run that immediately
        # fails deep inside the agent, far from the cause.
        agent_modes = self.agent_modes()
        condition = str(jobs(self.workflows[ENTRYPOINT])["agent"].get("if", ""))
        routed = set(re.findall(r"inputs\.mode == '([^']+)'", condition))
        self.assertEqual(routed, agent_modes)

    def test_every_dispatch_mode_routes_to_exactly_one_surface(self):
        # GitHub cannot dispatch a reusable workflow, so every control-plane
        # dispatch arrives at the entrypoint and has to be routed by mode. Two
        # surfaces claiming one mode means both run for one dispatch; a mode no
        # surface claims means that dispatch does nothing and looks successful.
        routed: dict = {}
        for job_name in self.DISPATCHED_SURFACES:
            condition = str(jobs(self.workflows[ENTRYPOINT])[job_name].get("if", ""))
            for mode in re.findall(r"inputs\.mode == '([^']+)'", condition):
                self.assertNotIn(
                    mode,
                    routed,
                    f"{job_name} and {routed.get(mode)} both claim mode {mode!r}",
                )
                routed[mode] = job_name

        for mode in self.agent_modes():
            self.assertEqual(
                routed.get(mode),
                "agent",
                f"mode {mode!r} is implemented by the agent but routed elsewhere",
            )

        # And the dispatchers send exactly the modes the entrypoint routes.
        for dispatcher in (
            "consumer-child-dispatcher.yml",
            "consumer-scheduler.yml",
            "consumer-repair.yml",
            "consumer-review-gate.yml",
        ):
            for mode in set(re.findall(r"mode=([a-z][a-z:-]*)", self.raw[dispatcher])):
                with self.subTest(dispatcher=dispatcher, mode=mode):
                    self.assertIn(
                        mode,
                        routed,
                        f"{dispatcher} dispatches mode {mode!r}, which the "
                        "entrypoint routes to no surface",
                    )

    def test_the_entrypoint_declares_no_concurrency(self):
        # One ingress covers several reconcilers that must not serialize behind
        # each other. A single group here would fuse them into one queue, so a
        # review repair waiting on an agent would also stall the merge reconciler
        # that would release it. Each nested controller owns its own constant
        # group instead.
        self.assertNotIn("concurrency", self.workflows[ENTRYPOINT])


class ShadowEngineSelectionTests(ReleaseSurfaceBase):
    """The one workflow allowed to name a Continuum revision, and its limits."""

    def test_the_shadow_engine_resolves_from_the_callers_commit(self):
        self.assertIn("JOB_WORKFLOW_SHA: ${{ job.workflow_sha }}", self.raw[SHADOW])

    def test_the_shadow_engine_refuses_anything_but_a_full_commit(self):
        text = self.raw[SHADOW]
        self.assertIn("grep -Eq '^[0-9a-f]{40}$'", text)
        self.assertIn("the engine ref must be a full commit sha", text)
        for _job, _step, ref in self.engine_checkouts(SHADOW):
            with self.subTest(step=_step):
                self.assertIn("steps.engine.outputs.sha", str(ref))

    def test_the_shadow_engine_is_not_reached_from_a_consumer_ingress(self):
        # The shadow harness judges recorded consumer runs. It is not part of the
        # control plane a consumer executes, so a consumer pin must not be able
        # to drag it in.
        for name in PRODUCTION_CONSUMER_WORKFLOWS + (ENTRYPOINT,):
            with self.subTest(name=name):
                self.assertNotIn(SHADOW, self.raw[name])


class ConsumerIngressTests(ReleaseSurfaceBase):
    """The generated ingresses a new repository is written from."""

    def read(self, repository: str, name: str = "continuum.yml") -> str:
        path = CONSUMER_REPOSITORIES[repository] / name
        self.assertTrue(path.is_file(), f"{path} is missing")
        return path.read_text(encoding="utf-8")

    def test_each_consumer_holds_exactly_one_continuum_reference(self):
        for repository in CONSUMER_REPOSITORIES:
            with self.subTest(repository=repository):
                self.assertEqual(len(self.continuum_pins(self.read(repository))), 1)

    def test_that_reference_is_the_exact_release_entrypoint(self):
        expected = f"{CONTINUUM}/{RELEASE_ENTRYPOINT}@{EXPECTED_CONSUMER_REF}"
        for repository in CONSUMER_REPOSITORIES:
            with self.subTest(repository=repository):
                self.assertEqual(self.continuum_pins(self.read(repository)), [expected])

    def test_the_pinned_reference_is_exact_and_immutable_by_construction(self):
        # `uses:` accepts no expressions, so the version has to be a literal. A
        # literal is only a selection if it cannot move, so the rule is an exact
        # SemVer tag: not a branch, not a bare `v1` compatibility tag, not an
        # abbreviated commit. Each of those names more than one revision.
        for repository in CONSUMER_REPOSITORIES:
            value = self.continuum_pins(self.read(repository))[0]
            ref = value.rsplit("@", 1)[1]
            with self.subTest(repository=repository):
                self.assertRegex(ref, EXACT_RELEASE)
                self.assertNotRegex(ref, BRANCH)
                self.assertNotRegex(ref, MOVING_TAG)
                self.assertNotRegex(ref, ABBREVIATED_COMMIT)

    def test_the_shipped_tool_accepts_the_pins_the_shipped_fixtures_declare(self):
        # The audit and the tool have to agree on what a usable pin is, or the
        # fixtures document a selection `release pin verify` would reject. This
        # is the check that would catch the two drifting apart.
        from continuum.pin import classify

        for repository in CONSUMER_REPOSITORIES:
            value = self.continuum_pins(self.read(repository))[0]
            ref = value.rsplit("@", 1)[1]
            with self.subTest(repository=repository):
                self.assertEqual(classify(ref), "release")

    def test_no_consumer_ingress_holds_a_second_continuum_reference(self):
        # One reference is the whole selection surface. A second one that happens
        # to agree today is still a second place that decides which Continuum a
        # repository runs, and the two can be changed independently.
        for repository, directory in CONSUMER_REPOSITORIES.items():
            for path in sorted(directory.glob("*.yml")):
                with self.subTest(repository=repository, file=path.name):
                    self.assertLessEqual(
                        len(self.continuum_pins(path.read_text(encoding="utf-8"))),
                        1,
                    )

    def test_a_consumer_ingress_contains_no_version_selection_mechanism(self):
        # GitHub does not allow expressions in `jobs.<job_id>.uses`, which is why
        # the version is a literal rather than a variable. If an ingress ever grew
        # a repository variable or a `CONTINUUM_VERSION` environment entry, the
        # pin would stop being reviewable dependency state and become
        # configuration a pull request could move without appearing in the diff
        # a reviewer reads.
        for repository in CONSUMER_REPOSITORIES:
            document = load_document(CONSUMER_REPOSITORIES[repository] / "continuum.yml")
            for job_name, job in jobs(document).items():
                for key in (job.get("with") or {}):
                    with self.subTest(repository=repository, job=job_name, key=key):
                        self.assertIsNone(
                            VERSION_INPUT.search(key),
                            f"{repository} passes a version selector to "
                            f"{job_name} as {key!r}",
                        )
            for number, line in enumerate(self.read(repository).splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                for token in ("CONTINUUM_VERSION", "vars.CONTINUUM", "${{ vars."):
                    with self.subTest(repository=repository, line=number):
                        self.assertNotIn(
                            token,
                            line,
                            f"{repository} line {number} selects a version through {token}",
                        )

    def test_a_consumer_ingress_holds_no_continuum_checkout(self):
        # A consumer ingress is an event adapter. The moment it also checks
        # Continuum out, the ingress holds a second opinion about which revision
        # the run should execute.
        for repository in CONSUMER_REPOSITORIES:
            document = load_document(CONSUMER_REPOSITORIES[repository] / "continuum.yml")
            for job_name, job in jobs(document).items():
                for step in steps_of(job):
                    with self.subTest(repository=repository, job=job_name):
                        self.assertNotEqual(with_map(step).get("repository"), CONTINUUM)

    def test_a_consumer_ingress_carries_no_controller_logic(self):
        # A generated ingress may declare events, statically-matched names,
        # permissions, and the release reference. Anything else is a second
        # implementation of a controller Continuum already owns.
        for repository in CONSUMER_REPOSITORIES:
            document = load_document(CONSUMER_REPOSITORIES[repository] / "continuum.yml")
            for job_name, job in jobs(document).items():
                with self.subTest(repository=repository, job=job_name):
                    self.assertIsNone(job.get("steps"), f"{job_name} runs steps in the ingress")
                    self.assertNotIn("runs-on", job)
                    self.assertIsNone(job.get("env"), f"{job_name} declares job environment")

    def document(self, repository: str) -> dict:
        return load_document(CONSUMER_REPOSITORIES[repository] / "continuum.yml")

    def test_the_consumer_fixture_keeps_only_its_own_release_entrypoint(self):
        # The one remaining file is a product fact: what is built, signed, and
        # published for this repository is its own business, and it must not call
        # Continuum at all.
        directory = CONSUMER_REPOSITORIES["consumer-repo"]
        present = sorted(path.name for path in directory.glob("*.yml"))
        self.assertEqual(present, ["continuum-release.yml", "continuum.yml"])
        self.assertEqual(self.continuum_pins(self.read("consumer-repo", "continuum-release.yml")), [])

    def test_every_ingress_declares_every_input_its_dispatch_targets_send(self):
        # GitHub rejects a dispatch carrying an input the target workflow does
        # not declare, so an ingress that is missing one does not fail review --
        # it fails at dispatch, on a schedule, in the consumer's repository. The
        # required set is derived from the ingress's own `with:` block: a
        # repository that never points `worker_workflow` at itself is never sent
        # a child input, so declaring one there would be noise.
        swept = dispatched_input_names()
        self.assertTrue(swept, "the dispatch sweep found nothing to check")
        for repository in CONSUMER_REPOSITORIES:
            declared = set(workflow_dispatch_inputs(self.document(repository)))
            reachable = self.self_dispatch_targets(repository)
            self.assertTrue(reachable, f"{repository} is dispatched into nowhere")
            with self.subTest(repository=repository):
                self.assertEqual(reachable, set(reachable) & set(CONTINUUM_DISPATCH_TARGETS))
                for target in reachable:
                    self.assertEqual(swept.get(target, set()) - declared, set(), target)
                # A parent receives every child mode on top of the agent ones,
                # so it carries the whole union. Asserting it in full is what
                # keeps a new controller input from reaching the fixtures only
                # through the sweep's ability to recognise its shape.
                if "WORKER_WORKFLOW" in reachable:
                    self.assertEqual(EXPECTED_INGRESS_DISPATCH_INPUTS - declared, set())

    def test_the_hand_written_dispatch_union_matches_the_release(self):
        # `EXPECTED_INGRESS_DISPATCH_INPUTS` is the readable statement of the
        # contract; the sweep is the check that it still describes the release.
        # Either one drifting alone has to fail.
        swept = dispatched_input_names()
        union = set().union(*swept.values()) if swept else set()
        self.assertEqual(union, set(EXPECTED_INGRESS_DISPATCH_INPUTS))

    def self_dispatch_targets(self, repository: str) -> set:
        """The `*_workflow` names an ingress points back at its own file.

        The ingress spells these in lower case; the controllers read them as
        upper-case environment variables, so they are compared upper-cased to
        line up with `CONTINUUM_DISPATCH_TARGETS`.
        """

        document = self.document(repository)
        values = {}
        for job in jobs(document).values():
            values.update(job.get("with") or {})
        return {
            key.upper()
            for key, value in values.items()
            if key.upper() in CONTINUUM_DISPATCH_TARGETS and value == "continuum.yml"
        }

    def test_a_parent_ingress_declares_the_child_inputs_too(self):
        # The three child modes are dispatched into the parent's own ingress, so
        # an ingress that reconciles children but cannot receive them is a parent
        # that dispatches into a 422.
        parent = set(workflow_dispatch_inputs(self.document("delegation-parent")))
        for name in ("child_id", "task_number", "pr_number"):
            with self.subTest(name=name):
                self.assertIn(name, parent)

    def test_no_consumer_ingress_names_itself_in_a_workflow_run_filter(self):
        # A called workflow's jobs run inside the caller's run, so the only
        # workflow name a `workflow_run` filter could match here is the
        # ingress's own. A workflow that completes because of `workflow_run`
        # then completes in a way that triggers `workflow_run` again, so
        # self-matching is not a redundancy -- it is an unbounded chain of runs.
        for repository in CONSUMER_REPOSITORIES:
            document = self.document(repository)
            declared = (triggers_of(document).get("workflow_run") or {}).get("workflows") or []
            with self.subTest(repository=repository):
                self.assertNotIn(document.get("name"), declared)

    def test_a_workflow_run_filter_never_names_a_continuum_job(self):
        # The entrypoint gives its jobs display names ("Continuum delegated child
        # task"). `workflow_run.workflows` matches *workflow* names, so listing
        # one there is a filter that can never fire -- the reconcile it was
        # written for silently happens only on the schedule.
        entrypoint_jobs = {
            job.get("name")
            for job in jobs(self.workflows[ENTRYPOINT]).values()
            if job.get("name")
        }
        self.assertTrue(entrypoint_jobs, "the entrypoint names no jobs")
        for repository in CONSUMER_REPOSITORIES:
            declared = (triggers_of(self.document(repository)).get("workflow_run") or {}).get(
                "workflows"
            ) or []
            for name in declared:
                with self.subTest(repository=repository, name=name):
                    self.assertNotIn(name, entrypoint_jobs)


class NoAutomaticRepinTests(ReleaseSurfaceBase):
    """Nothing may move a consumer's pin because a newer release appeared."""

    #: Operational vocabulary. If a workflow ever treats a release reference as
    #: state it can read, compare, or publish, it is on its way to repinning.
    OPERATIONAL = ("CONTINUUM_VERSION", "release pin", "repin", "latest continuum")

    def test_no_workflow_treats_a_release_reference_as_operational_state(self):
        for name, text in self.raw.items():
            for token in self.OPERATIONAL:
                for line in text.splitlines():
                    if token in line and not line.lstrip().startswith("#"):
                        self.fail(f"{name} treats {token!r} as state: {line.strip()}")

    def test_no_workflow_assembles_a_release_reference_at_runtime(self):
        for name, document in self.workflows.items():
            for job_name, job in jobs(document).items():
                for step in steps_of(job):
                    script = step.get("run") or ""
                    if not script:
                        continue
                    with self.subTest(workflow=name, job=job_name):
                        self.assertNotIn(
                            "consumer.yml@${{",
                            script,
                            f"{name}/{job_name} assembles a release reference at runtime",
                        )

    def test_no_workflow_writes_to_a_consumer_ingress(self):
        # A generated ingress is a committed file. The only writer ADR-0002
        # permits for a consumer's release selection is a person editing one
        # line, so no workflow may write to an ingress path at all.
        for name, document in self.workflows.items():
            for job_name, job in jobs(document).items():
                for step in steps_of(job):
                    script = step.get("run") or ""
                    if not script:
                        continue
                    for line in script.splitlines():
                        if "workflows/continuum.yml" not in line:
                            continue
                        with self.subTest(workflow=name, job=job_name):
                            self.assertNotIn(
                                "git push", line, f"{name}/{job_name} pushes an ingress change"
                            )

    def test_the_release_pin_is_not_a_workflow_input(self):
        # An input would make the version a caller-supplied value, which is the
        # thing ADR-0002 rules out: the selection is consumer-owned dependency
        # state in a reviewed file, not something a workflow passes in.
        inputs = set(workflow_call_inputs(self.workflows[ENTRYPOINT]))
        for banned in ("continuum_ref", "continuum_version", "continuum_release", "pin", "ref"):
            self.assertNotIn(banned, inputs)


if __name__ == "__main__":
    unittest.main()
