#!/usr/bin/env python3
"""Regression suite for agent workspace materialization.

The incident
------------
kodmai #35 was closed by a pull request whose body read:

    "Recovered automatically after the OpenCode agent changed branches during
    issue #23 execution."

That sentence is the recovery step's own description of itself, written by the
step that fired after the model moved. The step did recover the work. It also
had three ways to lose it, and every one of them was silent:

1. It compared ``git branch --show-current`` against ``opencode/*`` and, on a
   mismatch, printed "nothing to recover" and exited ``0``. A model that left a
   non-agent branch, or detached HEAD where the command prints nothing, had its
   finished working tree discarded when the runner was torn down.
2. It pushed to whatever branch the model was standing on. So the *workflow*
   published to a ref the *model* picked -- a ref the trust policy does not
   recognise as an agent branch, and therefore one no controller will ever
   reconcile.
3. It asked ``gh pr list --state all``. A merged or closed pull request counted
   as "already published" and suppressed recovery for that branch permanently.

runtime-lab #23 is the same shape from the other end: the model's output lives
only in a runner workspace that is destroyed when the job ends, so publication
has to be a transaction the workflow owns rather than a step the model can skip.

The invariants encoded here
---------------------------
* the decision has no "discard" outcome (WorkspacePolicyTests),
* an open pull request makes publication idempotent, a closed one does not
  (PublicationIdempotenceTests),
* a model that changed branches, or detached HEAD, still gets its work
  published -- byte for byte -- on a branch the workflow owns
  (MaterializationGitTests, against real git),
* an ambiguous branch identity preserves work on a salvage branch instead of
  dropping it (SalvageTests),
* both planes call one policy, so they cannot drift apart (WorkflowWiringTests).

Run with::

    python3 -m unittest discover -s .github/tests -p 'test_*.py'
"""

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / ".github" / "scripts"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

sys.path.insert(0, str(SCRIPT_DIR))

import agent_workspace  # noqa: E402

POLICY_SCRIPT = SCRIPT_DIR / "agent_workspace.py"
AGENT_WORKFLOW = "opencode.yml"
CONSUMER_AGENT_WORKFLOW = "consumer-opencode.yml"

PLAN_STEP = "Plan the agent workspace recovery"
MATERIALIZE_STEP = "Materialize the agent workspace"
SALVAGE_STEP = "Preserve an unpublishable agent workspace"

#: The three branches the incident actually needed, named as the policy names
#: them. `main` is the drift: where the model left the work.
ISSUE = "23"
OWNED = "opencode/issue23-continuum"
DRIFTED = "opencode/issue23-sidequest"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _step(workflow, step_name):
    """The literal step from a workflow, as the runner would see it."""
    doc = yaml.safe_load((WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if step.get("name") == step_name:
                return step
    raise AssertionError("no step named {!r} in {}".format(step_name, workflow))


def _run_text(step):
    return step.get("run") or ""


#: ``${{ steps.<id>.outputs.<name> }}``, the only expression form the recovery
#: steps use, plus ``needs.authorize.outputs.*`` for the ones the test supplies
#: directly through the environment.
STEP_OUTPUT_RE = re.compile(r"\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_]+)\s*\}\}")


def render(text, outputs):
    """Substitute resolved step outputs, the way the Actions runner does.

    Without this the recovery steps could only be tested as source, and a
    source-level test is exactly what let the original bug through: the step
    read correctly and did the wrong thing.
    """

    def replace(match):
        return str(outputs.get(match.group(2), ""))

    return STEP_OUTPUT_RE.sub(replace, text)


def _resolved_env(step, outputs, extra):
    """The step's own ``env:`` block with its expressions resolved.

    ``extra`` wins over the step's block on purpose: the test supplies the
    *resolved* form of an upstream output (``needs.authorize.outputs.*``) that the
    runner would have filled in, and letting the step's own placeholder reappear
    would overwrite the value under test with an empty string.
    """
    environment = {}
    for name, value in (step.get("env") or {}).items():
        environment[name] = render(str(value), outputs)
    environment.update(extra)
    return environment


def executable_lines(text):
    """The lines of a ``run:`` body that are not shell comments.

    Comments are where the incident is *described*, including the mistakes it
    made. Asserting against them proves nothing, so the wiring tests look only
    at lines the shell would run.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _read_outputs(path):
    """Parse a ``GITHUB_OUTPUT`` file into a mapping."""
    mapping = {}
    if not os.path.exists(str(path)):
        return mapping
    with open(str(path), encoding="utf-8") as handle:
        for line in handle.read().splitlines():
            if "=" in line and not line.startswith("continuum_workspace_"):
                key, _, value = line.partition("=")
                mapping[key] = value
    return mapping


class _FakeGh:
    """A `gh` stand-in that answers the one question recovery asks.

    `gh pr list` prints the open pull request number for the branch, or nothing
    when there is none -- which is the whole contract the materialization steps
    depend on. `gh pr create` records the call, including the base it targeted,
    so a test can prove the pull request was opened against the configured base
    rather than a hard-coded one.
    """

    def __init__(self, directory, open_pr=""):
        self.log = pathlib.Path(directory) / "gh-calls.log"
        self.log.write_text("", encoding="utf-8")
        self.bodies = pathlib.Path(directory) / "gh-bodies.log"
        self.bodies.write_text("", encoding="utf-8")
        script = pathlib.Path(directory) / "gh"
        script.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os, sys
                args = sys.argv[1:]
                with open(os.environ["GH_CALL_LOG"], "a") as handle:
                    handle.write(" ".join(args) + "\\n")
                if args[:2] == ["pr", "list"]:
                    # The real `gh` prints nothing when the filter matches no
                    # open pull request, and that empty string is the signal
                    # the recovery step is written against.
                    print(os.environ.get("FAKE_OPEN_PR", ""))
                sys.exit(0)
                """
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        self.open_pr = open_pr

    def calls(self):
        return [
            line
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


# --------------------------------------------------------------------------- #
# Policy: there is no discard
# --------------------------------------------------------------------------- #


class WorkspacePolicyTests(unittest.TestCase):
    """The decision itself. No git, no network, no clock."""

    def _plan(self, **kwargs):
        defaults = dict(
            owned_branch=OWNED,
            current_branch=OWNED,
            base_branch="main",
            dirty=True,
            commits_ahead=1,
        )
        defaults.update(kwargs)
        return agent_workspace.evaluate_workspace(**defaults)

    def test_the_action_set_has_no_discard_outcome(self):
        # The original bug was a silent `exit 0`. Any action that means "there
        # is nothing here" has to be reachable only by *proving* there is
        # nothing there, so the action set is closed and contains no member
        # that skips publication.
        self.assertEqual(
            set(agent_workspace.WORKSPACE_ACTIONS),
            {"publish", "materialize", "already-published", "refuse"},
        )
        for action in ("discard", "skip", "none", "no-op"):
            self.assertNotIn(action, agent_workspace.WORKSPACE_ACTIONS)

    def test_a_model_that_changed_branch_still_materializes(self):
        # The incident. The work exists; the model is standing somewhere else.
        decision = self._plan(current_branch="main", head_sha="d" * 40)
        self.assertEqual(decision.action, "materialize")
        self.assertEqual(decision.code, "branch_drift")
        self.assertEqual(decision.salvaged_head, "d" * 40)
        self.assertTrue(decision.recoverable)

    def test_a_detached_head_materializes_rather_than_being_treated_as_empty(self):
        # `git branch --show-current` prints nothing on a detached HEAD, and the
        # old step read that as "not an agent branch, nothing to recover". A
        # detached head is a branch the workflow must still publish from.
        decision = self._plan(current_branch="")
        self.assertEqual(decision.action, "materialize")
        self.assertEqual(decision.code, "branch_drift")

    def test_the_published_branch_is_the_one_the_workflow_owns(self):
        # Not the branch the model chose. Publishing to a model-chosen ref is
        # how the workflow ends up owning nothing and a finished
        # implementation ends up on a branch no controller reconciles.
        for drifted in ("main", "develop", "feature/x", "", "release/1.0"):
            with self.subTest(drifted=drifted):
                self.assertEqual(self._plan(current_branch=drifted).publish_branch, OWNED)

    def test_drift_with_no_work_is_not_reported_as_a_recovery(self):
        # The other half of the old bug: recovering nothing and calling it a
        # recovery trains everyone to ignore the message. A model that changed
        # branches and wrote nothing produced no change, which is an outcome.
        decision = self._plan(current_branch="main", dirty=False, commits_ahead=0)
        self.assertEqual(decision.action, "publish")
        self.assertEqual(decision.code, "drift_without_work")

    def test_work_left_on_the_owned_branch_keeps_the_agents_own_commits(self):
        decision = self._plan(dirty=False, commits_ahead=3)
        self.assertEqual(decision.action, "publish")
        self.assertEqual(decision.code, "agent_stayed_on_branch")

    def test_an_unusable_branch_identity_refuses_to_publish(self):
        # Refusing is allowed. Publishing to a guessed ref is not.
        for candidate in ("", "main", "attacker/branch", "opencode/issue0-x", "opencode/issue23-../etc"):
            with self.subTest(candidate=candidate):
                decision = self._plan(owned_branch=candidate)
                self.assertEqual(decision.action, "refuse")
                self.assertFalse(decision.recoverable)
                self.assertTrue(decision.report_failure)
                self.assertEqual(decision.publish_branch, "")

    def test_refusal_is_only_reached_for_identity_never_for_content(self):
        # A refusal must not be a way to drop work. Every input combination that
        # has work in it either publishes it or says it could not.
        for drifted in ("main", "", OWNED):
            for dirty in (True, False):
                for ahead in (0, 1, 5):
                    with self.subTest(drifted=drifted, dirty=dirty, ahead=ahead):
                        decision = self._plan(
                            current_branch=drifted, dirty=dirty, commits_ahead=ahead
                        )
                        if (dirty or ahead > 0) and decision.action == "refuse":
                            self.fail("work was refused instead of published")

    def test_the_base_branch_is_configuration_not_a_literal(self):
        # The old step hard-coded `main` in three places while the rest of the
        # workflow honoured vars.AUTOMATION_BASE_BRANCH. A repository whose base
        # is `trunk` got a pull request aimed at the wrong branch.
        decision = self._plan(base_branch="trunk")
        self.assertEqual(decision.base_branch, "trunk")
        self.assertEqual(decision.as_outputs()["workspace_base_branch"], "trunk")
        # It reaches the reason a reader sees, not just the payload: "nothing to
        # publish" has to be a statement about *this* repository's base.
        explained = self._plan(base_branch="trunk", dirty=False, commits_ahead=0)
        self.assertIn("trunk", explained.as_outputs()["workspace_reason"])
        self.assertEqual(
            self._plan(base_branch="").base_branch, agent_workspace.DEFAULT_BASE_BRANCH
        )

    def test_a_malformed_salvage_head_is_reported_as_unknown_not_asserted(self):
        decision = self._plan(current_branch="main", head_sha="not-a-sha")
        self.assertEqual(decision.salvaged_head, "")


class BranchIdentityTests(unittest.TestCase):
    """Which branch the workflow owns, when it has to infer it."""

    def test_a_single_candidate_is_the_owned_branch(self):
        self.assertEqual(agent_workspace.select_owned_branch([OWNED]), OWNED)

    def test_nothing_in_the_agent_shape_is_a_refusal(self):
        # Never a non-agent ref. That is the whole point: publication identity
        # cannot be widened by widening the candidate list.
        for candidates in ([], [""], ["main"], ["feature/x"], ["refs/heads/main"]):
            with self.subTest(candidates=candidates):
                self.assertEqual(agent_workspace.select_owned_branch(candidates), "")

    def test_two_candidates_and_a_current_branch_that_is_one_of_them_is_still_a_refusal(self):
        # The subtle version of the incident. "The agent left the work on
        # branch X, so X must be the branch to publish to" looks reasonable and
        # is exactly the flaw: a model that can create a second `opencode/*`
        # branch picks the ref the workflow publishes to. Refusing costs a
        # review, because the work is still quarantined; publishing to a
        # model-chosen ref costs the merge gates, which is not recoverable.
        self.assertEqual(
            agent_workspace.select_owned_branch([OWNED, DRIFTED], current_branch=DRIFTED), ""
        )
        self.assertEqual(
            agent_workspace.select_owned_branch([OWNED, DRIFTED], current_branch=OWNED), ""
        )

    def test_two_candidates_and_no_way_to_tell_is_a_refusal_not_a_coin_flip(self):
        # Guessing here would publish a finished implementation to whichever
        # branch sorted first. The run is recoverable either way because a
        # refusal still salvages, so the correct answer is the cautious one.
        self.assertEqual(
            agent_workspace.select_owned_branch([OWNED, DRIFTED], current_branch="main"),
            "",
        )

    def test_duplicate_candidates_are_one_candidate(self):
        # `for-each-ref` over a ref prefix cannot produce duplicates, but the
        # count is what decides refuse-vs-publish, so it must be a set count
        # rather than a list length.
        self.assertEqual(agent_workspace.select_owned_branch([OWNED, OWNED, OWNED]), OWNED)

    def test_non_agent_candidates_never_join_the_count(self):
        # If junk counted toward ambiguity, an unrelated `opencode/issue23x-y`
        # ref would be enough to suppress recovery entirely.
        self.assertEqual(
            agent_workspace.select_owned_branch([OWNED, "main", "refs/heads/main"]), OWNED
        )

    def test_the_derived_branch_names_are_inside_the_agent_shape(self):
        # Both must be recognisable by the trust policy, or preserved work is
        # preserved somewhere nothing will ever look.
        self.assertEqual(agent_workspace.owned_branch_name("23"), OWNED)
        self.assertTrue(agent_workspace.is_agent_branch(agent_workspace.owned_branch_name("23")))
        salvage = agent_workspace.salvage_branch_name("23", "1234567890")
        self.assertTrue(agent_workspace.is_agent_branch(salvage))
        self.assertEqual(salvage, "opencode/issue23-salvage-1234567890")

    def test_a_re_dispatch_reuses_the_same_owned_branch(self):
        # One branch per issue, not per run: a second run for the same issue has
        # to find the first run's pull request instead of inventing a ref that
        # no controller will ever reconcile.
        self.assertEqual(
            agent_workspace.owned_branch_name("23"), agent_workspace.owned_branch_name(" 23 ")
        )

    def test_derived_names_refuse_nonsense_identifiers(self):
        for number in ("", "0", "-1", "abc", "23; rm -rf /"):
            with self.subTest(number=number):
                self.assertEqual(agent_workspace.owned_branch_name(number), "")
        for run in ("", "abc", "$(id)", "1;2"):
            with self.subTest(run=run):
                self.assertEqual(agent_workspace.salvage_branch_name("23", run), "")


# --------------------------------------------------------------------------- #
# Publication idempotence
# --------------------------------------------------------------------------- #


class PublicationIdempotenceTests(unittest.TestCase):
    """A failed wrapper step must not turn one task into two pull requests."""

    def _plan(self, **kwargs):
        defaults = dict(
            owned_branch=OWNED, current_branch=OWNED, base_branch="main", dirty=True, commits_ahead=1
        )
        defaults.update(kwargs)
        return agent_workspace.evaluate_workspace(**defaults)

    def test_an_open_pull_request_stops_publication_before_the_work_is_examined(self):
        # Checked before the work, deliberately. A pull request that already
        # exists is the reason to do nothing, whatever state the workspace is
        # in -- otherwise a retry that finds extra local commits would open a
        # second pull request for the same issue.
        decision = self._plan(open_pull_request=66, dirty=True, commits_ahead=4)
        self.assertEqual(decision.action, "already-published")
        self.assertEqual(decision.publish_branch, OWNED)

    def test_a_closed_pull_request_does_not_suppress_publication(self):
        # The `--state all` bug. A merged or closed pull request is history, not
        # a publication: treating it as one made a branch permanently
        # unrecoverable, and the finished work sat in the runner workspace until
        # the runner was destroyed.
        for closed in (0,):
            with self.subTest(closed=closed):
                self.assertEqual(self._plan(open_pull_request=closed).action, "publish")

    def test_a_second_run_that_finds_the_pull_request_never_creates_another(self):
        first = self._plan()
        self.assertEqual(first.action, "publish")
        second = self._plan(open_pull_request=1)
        self.assertEqual(second.action, "already-published")


# --------------------------------------------------------------------------- #
# Materialization against real git
# --------------------------------------------------------------------------- #


class MaterializationGitTests(unittest.TestCase):
    """Run the real recovery steps against real git.

    Every other test in this file checks a decision. This one runs the literal
    `run:` bodies from the workflow inside a repository with a real origin, and
    checks where the finished work actually ended up. A pure function can
    correctly decide to materialize while the shell around it drops the tree on
    the floor -- which is what the original step did.
    """

    def _git(self, *args, cwd, check=True):
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
        ).stdout.strip()

    def _repository(self, root, base="main"):
        """A bare origin, a clone, and the owned branch already created.

        Creating the branch up front is the point: the workflow owns it, so it
        exists before the model runs and does not depend on the model for its
        existence.
        """
        origin = root / "origin.git"
        clone = root / "clone"
        subprocess.run(
            ["git", "init", "--bare", "-b", base, str(origin)], capture_output=True, check=True
        )
        subprocess.run(["git", "clone", str(origin), str(clone)], capture_output=True, check=True)
        for key, value in (
            ("user.name", "Continuum Test"),
            ("user.email", "test@example.invalid"),
        ):
            self._git("config", key, value, cwd=clone)

        (clone / "README.md").write_text("base\n", encoding="utf-8")
        # The workflow's own policy script has to be present in the checkout:
        # the steps call it relative to the workspace, exactly as they do on a
        # runner. It is committed as part of the base rather than dropped in
        # afterwards, because an uncommitted file would make every workspace
        # look dirty and the recovery would materialize the harness instead of
        # the agent's work.
        (clone / ".github" / "scripts").mkdir(parents=True, exist_ok=True)
        (clone / ".github" / "scripts" / "agent_workspace.py").write_text(
            POLICY_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
        )
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-m", "base", cwd=clone)
        self._git("push", "-u", "origin", base, cwd=clone)
        self._git("checkout", "-b", OWNED, cwd=clone)
        self._git("push", "-u", "origin", OWNED, cwd=clone)
        return origin, clone

    def _run_step(self, clone, step, outputs, directory, open_pr="", extra_env=None):
        """Execute one workflow step in the clone with a fake `gh` on PATH."""
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                "GH_CALL_LOG": str(pathlib.Path(directory) / "gh-calls.log"),
                "GH_BODY_LOG": str(pathlib.Path(directory) / "gh-bodies.log"),
                "FAKE_OPEN_PR": str(open_pr),
                "GITHUB_REPOSITORY": "kodmial/continuum",
                "GITHUB_RUN_ID": "1234567890",
                "GITHUB_OUTPUT": str(pathlib.Path(directory) / "gh-output.txt"),
                "GH_TOKEN": "not-a-real-token",
                # The recovery steps call the policy by relative path from the
                # workspace, so the module must be importable from the clone.
                "PYTHONPATH": "",
            }
        )
        environment.update(_resolved_env(step, outputs, extra_env or {}))
        return subprocess.run(
            ["bash", "-c", render(_run_text(step), outputs)],
            cwd=str(clone),
            env=environment,
            capture_output=True,
            text=True,
        )

    def _recover(self, clone, directory, open_pr="", base="main", model_branch="main"):
        """Run the whole plan -> materialize sequence the way the workflow does."""
        plan_step = _step(AGENT_WORKFLOW, PLAN_STEP)
        materialize_step = _step(AGENT_WORKFLOW, MATERIALIZE_STEP)
        base_env = {"ISSUE_NUMBER": ISSUE, "BASE_BRANCH": base}

        plan = self._run_step(clone, plan_step, {}, directory, open_pr=open_pr, extra_env=base_env)
        self.assertEqual(plan.returncode, 0, "plan step failed:\n{}\n{}".format(plan.stdout, plan.stderr))
        outputs = self._outputs(directory)
        materialize = self._run_step(
            clone, materialize_step, outputs, directory, open_pr=open_pr, extra_env=base_env
        )
        if outputs.get("workspace_action") == "refuse":
            salvage_step = _step(AGENT_WORKFLOW, SALVAGE_STEP)
            salvage = self._run_step(
                clone, salvage_step, outputs, directory, open_pr=open_pr, extra_env=base_env
            )
            return outputs, salvage
        return outputs, materialize

    def _outputs(self, directory):
        """The step outputs under their real names.

        ``workspace_*`` is not stripped: that is the name the runner exposes and
        the name the next step's ``${{ steps.workspace.outputs.* }}``
        substitutions resolve against, so a test that renamed them would not be
        testing the wiring it claims to test.
        """
        return _read_outputs(environment_output(directory))

    def _published(self, origin, ref):
        return self._git("rev-parse", ref, cwd=origin, check=False)

    def _ref_sha(self, origin, ref):
        """The commit a ref points at, or "" when the ref does not exist.

        ``git rev-parse`` echoes its argument back when the ref is missing, so
        a truthiness check on its output cannot tell "exists" from "does not".
        """
        return subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
            cwd=str(origin),
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _file_at(self, origin, ref, path):
        """The exact bytes of one file in a published ref.

        Deliberately not stripped. "The tree is preserved byte for byte" is the
        claim these tests exist to check, and a test helper that silently
        trims trailing newlines would not be checking it.
        """
        return subprocess.run(
            ["git", "show", "{}:{}".format(ref, path)],
            cwd=str(origin),
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")

    def _paths_at(self, origin, ref):
        return self._git("ls-tree", "--name-only", ref, cwd=origin).split()

    # -- the incident ------------------------------------------------------- #

    def test_an_agent_that_left_its_branch_still_publishes_its_work(self):
        # kodmai #35. The model wrote the implementation, then moved to `main`.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root)
            (clone / "implementation.py").write_text("def solve():\n    return 42\n", encoding="utf-8")
            self._git("switch", "main", cwd=clone)

            _FakeGh(directory)
            outputs, result = self._recover(clone, directory)

            self.assertEqual(result.returncode, 0, "{}\n{}".format(result.stdout, result.stderr))
            self.assertEqual(outputs.get("workspace_action"), "materialize")
            self.assertEqual(outputs.get("workspace_publish_branch"), OWNED)

            # Published to the branch the workflow owns...
            self.assertTrue(self._published(origin, OWNED))
            # ...carrying the model's bytes exactly...
            published = self._file_at(origin, OWNED, "implementation.py")
            self.assertEqual(published, "def solve():\n    return 42\n")
            # ...and not to the branch the model chose.
            self.assertNotIn("implementation.py", self._paths_at(origin, "main"))

    def test_a_detached_head_publishes_rather_than_discards(self):
        # `git branch --show-current` is empty here. The old step read that as
        # "not an OpenCode issue branch; nothing to recover", printed it, and
        # exited 0 with a finished implementation in the tree.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root)
            (clone / "detached.py").write_text("value = 'kept'\n", encoding="utf-8")
            self._git("switch", "--detach", "HEAD", cwd=clone)
            self.assertEqual(self._git("branch", "--show-current", cwd=clone), "")

            _FakeGh(directory)
            outputs, result = self._recover(clone, directory)

            self.assertEqual(result.returncode, 0, "{}\n{}".format(result.stdout, result.stderr))
            self.assertEqual(outputs.get("workspace_action"), "materialize")
            self.assertEqual(
                self._file_at(origin, OWNED, "detached.py"), "value = 'kept'\n"
            )

    def test_commits_stranded_on_a_branch_the_model_abandoned_are_not_lost(self):
        # The hardest drift, and the one a tree-only recovery cannot see on its
        # own: the model commits to a branch, switches back to the branch the
        # workflow owns, and leaves the working tree clean. Every fact the
        # original step consulted -- current branch, `status --porcelain`,
        # `rev-list origin/base..HEAD` -- says there is nothing here, while the
        # work sits on a ref nobody is looking at.
        #
        # The plan step therefore enumerates the agent branches that hold
        # commits the base does not have, and the salvage step preserves the
        # tree of the one that holds the most. Losing the work here would have
        # been silent: the run would have reported "nothing to publish" and
        # exited 0.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root)
            self._git("switch", "-c", DRIFTED, cwd=clone)
            (clone / "stranded.md").write_text("finding\n", encoding="utf-8")
            self._git("add", "-A", cwd=clone)
            self._git("commit", "-m", "the work", cwd=clone)
            self._git("switch", OWNED, cwd=clone)
            self.assertEqual(self._git("status", "--porcelain", cwd=clone), "")
            self.assertEqual(
                self._git("rev-list", "--count", "origin/main..HEAD", cwd=clone), "0"
            )

            gh = _FakeGh(directory)
            outputs, result = self._recover(clone, directory)

            # Two agent branches and no provenance: the workflow will not guess
            # which one it owns, so it refuses and preserves.
            self.assertEqual(outputs.get("workspace_action"), "refuse")
            self.assertEqual(outputs.get("workspace_code"), "no_owned_branch")
            # It names the ref that actually holds the work, so the salvage step
            # preserves the abandoned branch rather than the clean tree.
            self.assertEqual(outputs.get("workspace_salvage_source"), DRIFTED)
            # It reports, rather than claiming a recovery it did not perform.
            self.assertEqual(result.returncode, 1, "{}\n{}".format(result.stdout, result.stderr))
            # And the abandoned branch's work is preserved verbatim.
            salvage = "opencode/issue{}-salvage-1234567890".format(ISSUE)
            self.assertEqual(self._file_at(origin, salvage, "stranded.md"), "finding\n")
            self.assertNotIn("pr create", "\n".join(gh.calls()))
            # Nothing was published to a branch the model chose.
            self.assertEqual(self._published(origin, OWNED), self._git("rev-parse", "main", cwd=origin))
            self.assertEqual(self._ref_sha(origin, DRIFTED), "")

    def test_work_the_model_left_on_a_branch_it_invented_is_quarantined_not_published(self):
        # The model created a second agent-shaped branch. Nothing in the
        # workspace distinguishes it from the branch the action created, so the
        # workflow does not guess which one owns the change -- publishing to
        # whichever branch the model is standing on is the kodmai #35 bug.
        # Refusing still preserves every byte on a salvage branch.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root)
            self._git("switch", "-c", DRIFTED, cwd=clone)
            (clone / "invented.md").write_text("work on a branch nobody owns\n", encoding="utf-8")
            self._git("add", "-A", cwd=clone)
            self._git("commit", "-m", "work on my own branch", cwd=clone)

            _FakeGh(directory)
            outputs, result = self._recover(clone, directory)

            self.assertEqual(outputs.get("workspace_action"), "refuse")
            # It reports a failure rather than claiming a recovery...
            self.assertEqual(result.returncode, 1, "{}\n{}".format(result.stdout, result.stderr))
            self.assertIn("::error::", result.stdout)
            # ...and neither the invented branch nor the owned one was published.
            self.assertNotIn("invented.md", self._paths_at(origin, "main"))
            # The work is on the salvage branch, in full.
            salvage = "opencode/issue{}-salvage-1234567890".format(ISSUE)
            self.assertEqual(
                self._file_at(origin, salvage, "invented.md"),
                "work on a branch nobody owns\n",
            )

    def test_publication_does_not_conflict_with_an_advanced_base(self):
        # The reason this is a tree copy and not a merge. Bringing drifted work
        # back with `git merge` would conflict here, at the end of a long run,
        # with no model left to resolve it -- and a conflict at that point loses
        # the work it was trying to save.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root)

            # The base moves on, rewriting the same file the model rewrote.
            writer = root / "writer"
            subprocess.run(["git", "clone", str(origin), str(writer)], capture_output=True, check=True)
            self._git("config", "user.name", "Writer", cwd=writer)
            self._git("config", "user.email", "writer@example.invalid", cwd=writer)
            (writer / "README.md").write_text("rewritten upstream, unrelated reason\n", encoding="utf-8")
            self._git("add", "-A", cwd=writer)
            self._git("commit", "-m", "unrelated main advance", cwd=writer)
            self._git("push", "origin", "main", cwd=writer)

            # The model rewrote the same file, on a branch it drifted to.
            self._git("switch", "main", cwd=clone)
            (clone / "README.md").write_text("the agent rewrote this\n", encoding="utf-8")
            (clone / "feature.py").write_text("x = 1\n", encoding="utf-8")
            self._git("fetch", "origin", "main", cwd=clone)

            _FakeGh(directory)
            outputs, result = self._recover(clone, directory)

            self.assertEqual(result.returncode, 0, "{}\n{}".format(result.stdout, result.stderr))
            self.assertEqual(outputs.get("workspace_action"), "materialize")
            self.assertNotIn("CONFLICT", result.stdout + result.stderr)
            # Published without a single conflict prompt, carrying the agent's
            # content rather than a merged compromise.
            self.assertEqual(
                self._file_at(origin, OWNED, "README.md"), "the agent rewrote this\n"
            )
            self.assertEqual(self._file_at(origin, OWNED, "feature.py"), "x = 1\n")
            # The base branch is untouched: a recovery pushes one ref.
            self.assertEqual(
                self._file_at(origin, "main", "README.md"), "rewritten upstream, unrelated reason\n"
            )

    # -- idempotence and ownership ------------------------------------------ #

    def test_an_open_pull_request_stops_the_push_and_the_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root)
            (clone / "already.py").write_text("done = True\n", encoding="utf-8")
            self._git("add", "-A", cwd=clone)
            self._git("commit", "-m", "work", cwd=clone)
            before = self._published(origin, OWNED)

            gh = _FakeGh(directory, open_pr="66")
            outputs, result = self._recover(clone, directory, open_pr="66")

            self.assertEqual(result.returncode, 0, "{}\n{}".format(result.stdout, result.stderr))
            self.assertEqual(outputs.get("workspace_action"), "already-published")
            # No second push, and above all no second pull request.
            self.assertNotIn("pr create", "\n".join(gh.calls()))
            self.assertEqual(self._published(origin, OWNED), before)

    def test_a_pull_request_is_opened_against_the_configured_base(self):
        # A repository whose base branch is not `main`. The old step hard-coded
        # `main`, so the pull request was aimed at a branch that does not exist
        # in the consumer and the work went nowhere.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root, base="trunk")
            (clone / "trunked.py").write_text("ok = True\n", encoding="utf-8")

            gh = _FakeGh(directory)
            outputs, result = self._recover(clone, directory, base="trunk")

            self.assertEqual(result.returncode, 0, "{}\n{}".format(result.stdout, result.stderr))
            created = [call for call in gh.calls() if call.startswith("pr create")]
            self.assertEqual(len(created), 1, gh.calls())
            self.assertIn("--base trunk", created[0])
            self.assertIn("--head {}".format(OWNED), created[0])
            self.assertEqual(
                self._file_at(origin, OWNED, "trunked.py"), "ok = True\n"
            )

    def test_the_push_is_never_a_force_push(self):
        # The owned branch is the workflow's own. Anything that moves it has
        # left evidence, and overwriting that silently is how a concurrent
        # human push is erased.
        for workflow, step in (
            (AGENT_WORKFLOW, MATERIALIZE_STEP),
            (AGENT_WORKFLOW, SALVAGE_STEP),
            (CONSUMER_AGENT_WORKFLOW, "Implement issue"),
        ):
            run = _run_text(_step(workflow, step))
            with self.subTest(workflow=workflow, step=step):
                self.assertNotRegex(executable_lines(run), r"push[^|;&\n]*--force")
                self.assertNotRegex(executable_lines(run), r"push[^|;&\n]*-f\b")

    def test_a_model_that_produced_nothing_publishes_nothing_and_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root)

            gh = _FakeGh(directory)
            outputs, result = self._recover(clone, directory)

            self.assertEqual(result.returncode, 0, "{}\n{}".format(result.stdout, result.stderr))
            self.assertEqual(outputs.get("workspace_code"), "no_work_to_publish")
            self.assertNotIn("pr create", "\n".join(gh.calls()))
            self.assertEqual(self._published(origin, OWNED), self._git("rev-parse", "main", cwd=origin))


def environment_output(directory):
    return pathlib.Path(directory) / "gh-output.txt"


# --------------------------------------------------------------------------- #
# Refusal still preserves
# --------------------------------------------------------------------------- #


class SalvageTests(unittest.TestCase):
    """A refusal must not quietly become a discard."""

    def _git(self, *args, cwd, check=True):
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
        ).stdout.strip()

    def test_the_salvage_branch_is_one_the_trust_policy_will_recognize(self):
        import trust_policy

        salvage = agent_workspace.salvage_branch_name("23", "1234567890")
        self.assertTrue(
            trust_policy.is_agent_branch(salvage),
            "work quarantined on an unrecognised ref is work nothing can find",
        )

    def test_the_salvage_step_pushes_the_finished_tree_to_the_salvage_branch(self):
        step = _step(AGENT_WORKFLOW, SALVAGE_STEP)
        run = _run_text(step)
        self.assertIn('SALVAGE="opencode/issue${ISSUE_NUMBER}-salvage-${GITHUB_RUN_ID}"', run)
        # The same verbatim tree copy the materialize path uses, because a
        # quarantine that merges is a quarantine that can conflict and fail.
        self.assertIn("git commit-tree", run)
        self.assertIn('git rev-parse "${SOURCE_HEAD}^{tree}"', run)
        self.assertIn("git push", run)
        # And it reports, rather than pretending the run succeeded.
        self.assertIn("::error::", run)
        self.assertIn("exit 1", run)

    def test_the_salvage_step_preserves_the_ref_named_by_the_policy(self):
        # The workspace is not always where the work is. If the salvage step
        # took the tree from whatever happened to be checked out, every salvage
        # of a clean-workspace-stranded run would preserve an empty tree and
        # report having preserved the work.
        run = _run_text(_step(AGENT_WORKFLOW, SALVAGE_STEP))
        self.assertIn("workspace_salvage_source", str(_step(AGENT_WORKFLOW, SALVAGE_STEP)["env"]))
        self.assertIn('SOURCE_HEAD="$SALVAGE_SOURCE"', run)
        self.assertIn("git show-ref --verify --quiet", run)

    def test_refusal_is_never_reported_as_a_successful_recovery(self):
        decision = agent_workspace.evaluate_workspace(
            owned_branch="", current_branch="main", dirty=True, commits_ahead=1
        )
        self.assertEqual(decision.action, "refuse")
        self.assertFalse(decision.recoverable)
        self.assertTrue(decision.report_failure)

    def test_a_clean_workspace_with_stranded_commits_is_not_reported_as_no_work(self):
        # The consumer plane, where the owned branch is derived rather than
        # inferred: identity is never in doubt, so the only question left is the
        # work. If a branch holds commits the base does not have, "clean" is not
        # the answer, and the salvage is told which ref to preserve.
        decision = agent_workspace.evaluate_workspace(
            owned_branch=OWNED,
            current_branch=OWNED,
            dirty=False,
            commits_ahead=0,
            work_branches=[DRIFTED],
        )
        self.assertEqual(decision.action, "refuse")
        self.assertEqual(decision.code, "work_stranded_on_another_branch")
        self.assertEqual(decision.salvage_source, DRIFTED)
        self.assertIn(DRIFTED, decision.as_outputs()["workspace_reason"])
        self.assertTrue(decision.report_failure)

    def test_stranded_commits_do_not_change_a_workspace_that_already_has_work(self):
        # The agent's own tree is the freshest thing in the workspace; stranded
        # commits are a fallback, not a reason to stop looking at what is
        # checked out.
        decision = agent_workspace.evaluate_workspace(
            owned_branch=OWNED, current_branch=OWNED, dirty=True, commits_ahead=1, work_branches=[DRIFTED]
        )
        self.assertEqual(decision.action, "publish")
        self.assertEqual(decision.code, "agent_stayed_on_branch")

    def test_a_branch_with_nothing_the_base_lacks_is_not_stranded_work(self):
        # Only the agent's own path, never a non-agent ref: a `feature/x` branch
        # with commits must not be able to suppress a normal publication.
        decision = agent_workspace.evaluate_workspace(
            owned_branch=OWNED, current_branch=OWNED, dirty=False, commits_ahead=0,
            work_branches=["main", "feature/x", ""],
        )
        self.assertEqual(decision.action, "publish")
        self.assertEqual(decision.code, "no_work_to_publish")

    def test_the_salvage_source_is_the_ref_that_holds_the_work(self):
        # On a drift the workspace itself holds the work, so the salvage copies
        # the tree the model actually left rather than re-deriving it.
        decision = agent_workspace.evaluate_workspace(
            owned_branch=OWNED, current_branch="main", dirty=True, commits_ahead=0
        )
        self.assertEqual(decision.salvage_source, "main")
        detached = agent_workspace.evaluate_workspace(
            owned_branch=OWNED, current_branch="", dirty=True, commits_ahead=1
        )
        self.assertEqual(detached.salvage_source, "")


# --------------------------------------------------------------------------- #
# Both planes, one policy
# --------------------------------------------------------------------------- #


class WorkflowWiringTests(unittest.TestCase):
    """The workflows actually ask the policy, and cannot drift from it."""

    def test_both_planes_call_the_same_policy(self):
        # Two hand-written recovery paths are two recovery paths to keep
        # correct. The consumer plane's original step had no existence check and
        # no drift handling at all; deriving both from one module is what keeps
        # the next fix from landing in only one of them.
        for workflow in (AGENT_WORKFLOW, CONSUMER_AGENT_WORKFLOW):
            with self.subTest(workflow=workflow):
                self.assertIn(
                    "agent_workspace.py", (WORKFLOW_DIR / workflow).read_text(encoding="utf-8")
                )

    def test_the_recovery_runs_plan_before_it_acts(self):
        text = (WORKFLOW_DIR / AGENT_WORKFLOW).read_text(encoding="utf-8")
        self.assertLess(
            text.index(PLAN_STEP),
            text.index(MATERIALIZE_STEP),
            "the plan decides where work is published; acting first would decide something else",
        )
        self.assertLess(text.index(MATERIALIZE_STEP), text.index(SALVAGE_STEP))

    def test_no_step_asks_for_pull_requests_in_any_state(self):
        # The `--state all` bug, asserted against the lines the shell runs. A
        # closed or merged pull request must never count as a publication, or
        # recovery for that branch is permanently suppressed.
        for workflow in (AGENT_WORKFLOW, CONSUMER_AGENT_WORKFLOW):
            text = (WORKFLOW_DIR / workflow).read_text(encoding="utf-8")
            with self.subTest(workflow=workflow):
                self.assertNotIn("--state all", executable_lines(text))
                self.assertIn("--state open", text)

    def test_the_consumer_plane_guards_its_own_pull_request_creation(self):
        # It had no existence check at all, so a re-dispatch opened a second
        # pull request for the same issue.
        run = executable_lines(_run_text(_step(CONSUMER_AGENT_WORKFLOW, "Implement issue")))
        self.assertIn("pr create", run)
        self.assertIn("already owns", run)

    def test_the_consumer_plane_creates_the_branch_before_the_model_runs(self):
        # The workflow owns branch publication, so it decides the ref *before*
        # the model has an opportunity to move somewhere else.
        run = executable_lines(_run_text(_step(CONSUMER_AGENT_WORKFLOW, "Implement issue")))
        self.assertLess(
            run.index("git switch -C"),
            run.index("opencode run --auto"),
            "the owned branch must exist before the model can leave it",
        )

    def test_the_consumer_plane_quarantines_instead_of_dropping_a_refusal(self):
        # It used to fold `refuse` into the same branch as `publish`, which is
        # how "I will not publish to a ref the model chose" turned into
        # "publish to the ref the model chose" two lines later.
        run = executable_lines(_run_text(_step(CONSUMER_AGENT_WORKFLOW, "Implement issue")))
        self.assertIn("refuse)", run)
        self.assertNotIn("publish|refuse)", run)
        self.assertIn("SALVAGE=", run)
        self.assertIn("workspace_salvage_source", str(_step(CONSUMER_AGENT_WORKFLOW, "Implement issue")["env"]) + run)
        self.assertIn("SALVAGE_SOURCE=", run)
        self.assertIn("::error::", run)

    def test_the_consumer_plane_takes_the_base_branch_from_configuration(self):
        # Every base reference in this plane used to be the literal `main`,
        # including the checkout, so a consumer repository whose base is
        # `trunk` was read from one branch and merged into another.
        text = (WORKFLOW_DIR / CONSUMER_AGENT_WORKFLOW).read_text(encoding="utf-8")
        self.assertNotIn("origin/main", text)
        self.assertNotIn("ref: main", text)
        self.assertNotIn("--base main", text)
        self.assertIn("BASE_BRANCH: ${{ inputs.base_branch }}", text)
        step = _step(CONSUMER_AGENT_WORKFLOW, "Implement issue")
        self.assertIn("--base-branch \"$BASE_BRANCH\"", _run_text(step))

    def test_no_recovery_step_hard_codes_the_base_branch(self):
        # The old step hard-coded `main` while the rest of the workflow honoured
        # vars.AUTOMATION_BASE_BRANCH, so a `trunk` repository got a pull request
        # aimed at a branch that does not exist there.
        for workflow, step in (
            (AGENT_WORKFLOW, PLAN_STEP),
            (AGENT_WORKFLOW, MATERIALIZE_STEP),
            (AGENT_WORKFLOW, SALVAGE_STEP),
        ):
            run = executable_lines(_run_text(_step(workflow, step)))
            with self.subTest(step=step):
                self.assertIn("$BASE_BRANCH", run)
                self.assertNotRegex(run, r"origin/main|--base main")

    def test_no_recovery_step_publishes_to_the_branch_the_model_chose(self):
        # The push target is the policy's output, never `git branch
        # --show-current`. That substitution is the incident.
        run = _run_text(_step(AGENT_WORKFLOW, MATERIALIZE_STEP))
        self.assertIn('git push -u origin "HEAD:$OWNED_BRANCH"', run)
        self.assertNotIn('git push -u origin "HEAD:$CURRENT_BRANCH"', run)

    def test_the_model_is_still_denied_writes_in_both_planes(self):
        # Materialization is the safety net, not the permission. A model that
        # could commit and push would still decide what a reviewer sees. Each
        # plane enforces that differently, so each is checked against its own
        # mechanism: the self plane denies git in the model's permission config,
        # the consumer plane states it in the prompt.
        agent_text = (WORKFLOW_DIR / AGENT_WORKFLOW).read_text(encoding="utf-8")
        self.assertIn('"git *":"deny"', agent_text)
        consumer_text = (WORKFLOW_DIR / CONSUMER_AGENT_WORKFLOW).read_text(encoding="utf-8")
        self.assertIn("Do not create a pull request", consumer_text)

    def test_no_workflow_splices_an_expression_into_a_shell_script(self):
        # runtime-lab #145. The runner substitutes `${{ }}` *before* the shell
        # parses anything, so an expression inside a `run:` body is text the
        # shell treats as code. Every value reachable that way -- workflow
        # inputs, event payload fields, step outputs -- is text a caller or a
        # repository can control, and the surrounding quotes are not a boundary:
        # a value containing a double quote ends the quoted region and the rest
        # of it is parsed.
        #
        # The fix is always the same: declare the value in the step's `env:` and
        # read it as a shell variable. `env:` is a value the runner quotes once
        # and the shell expands later, so nothing the caller supplies is ever
        # code.
        offenders = []
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_name, job in (doc.get("jobs") or {}).items():
                for step in job.get("steps") or []:
                    run = step.get("run") or ""
                    for number, line in enumerate(run.splitlines(), 1):
                        if "${{" in line and not line.lstrip().startswith("#"):
                            offenders.append(
                                "{}:{}:{}:{}".format(
                                    path.name, job_name, step.get("name"), number
                                )
                            )
        self.assertEqual(offenders, [], "expressions in run bodies:\n" + "\n".join(offenders))

    def test_the_fixture_consumer_workflows_hold_the_same_line(self):
        # The consumer plane ships as a fixture that real repositories copy. A
        # guard that only covers this repository's own workflows would let the
        # unsafe pattern be published to every consumer that syncs it.
        for path in sorted((REPO_ROOT / "fixtures" / "consumer-repo" / ".github" / "workflows").glob("*.yml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_name, job in (doc.get("jobs") or {}).items():
                for step in job.get("steps") or []:
                    run = step.get("run") or ""
                    for number, line in enumerate(run.splitlines(), 1):
                        self.assertNotIn(
                            "${{",
                            line,
                            "{}:{}:{}:{}".format(path.name, job_name, step.get("name"), number),
                        )

    def test_the_policy_is_standard_library_only(self):
        # It runs on a stock runner with no dependency installation step, so an
        # import that needs a package is an import that fails in production.
        source = POLICY_SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("import yaml", "import requests", "import numpy"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
