"""Publishing a Continuum release: what a release is, and what may be published.

A consumer holds one literal reference and gets a whole control plane from it,
so the producer side has one job and one honest answer to give: *this commit,
completely, or nothing*. Every test here is about the boundary of that promise.

Three kinds of test, in the order they matter:

* **Identity.** `v0.1.0` and a full commit SHA, or a refusal. A moving reference
  is refused here for the same reason ``continuum release pin`` refuses one on
  the consumer's side: the two classifications must not disagree about where the
  line is, so this one is asserted to be the *same function*.
* **Closure.** The graph is resolved from one commit and refuses to leave it --
  no other Continuum repository, no unpinned third-party action, no revision
  other than the running workflow's own commit. The real repository is asserted
  as a fixture, because a rule that only held against a synthetic tree would not
  hold against the one that ships.
* **Derivation.** The notes and the manifest are computed, not written, and are
  byte-identical for identical inputs. A release that could carry a stale or
  invented compatibility claim would defeat the point of publishing it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import List, Optional

from continuum import cli, pin
from continuum import publication

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
A_COMMIT = "a" * 40
B_COMMIT = "b" * 40
C_COMMIT = "c" * 40

ENTRYPOINT = publication.RELEASE_ENTRYPOINT


def write(root: pathlib.Path, relative: str, text: str) -> pathlib.Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def consumer_ingress(ref: str = "v0.1.0") -> str:
    """What a *consumer* writes. Not part of any release graph."""

    return (
        "name: Continuum\n"
        "\n"
        "on:\n"
        "  workflow_dispatch:\n"
        "\n"
        "jobs:\n"
        "  continuum:\n"
        f"    uses: {pin.CONTINUUM_REPOSITORY}/{pin.RELEASE_ENTRYPOINT}@{ref}\n"
        "    secrets: inherit\n"
    )


def release_entrypoint() -> str:
    """Continuum's own entrypoint: the file a consumer's reference selects.

    It reaches the control plane relatively, so everything it calls comes from
    this commit. The comment naming the consumer's reference is documentation, and
    a release graph that treated a comment as a second version selection would be
    unusable.
    """

    return (
        "name: Continuum\n"
        "\n"
        "# A consumer writes one reference:\n"
        f"#   {pin.CONTINUUM_REPOSITORY}/{pin.RELEASE_ENTRYPOINT}@v0.1.0\n"
        "\n"
        "on:\n"
        "  workflow_call:\n"
        "\n"
        "jobs:\n"
        "  dispatch:\n"
        "    uses: ./.github/workflows/dispatcher.yml\n"
        "    secrets: inherit\n"
    )


def minimal_release(root: pathlib.Path, *, entry: str = release_entrypoint()) -> None:
    """The smallest tree that is a complete, publishable release graph."""

    write(root, ENTRYPOINT, entry)
    write(root, ".github/workflows/dispatcher.yml", reusable("dispatcher", ["./.github/workflows/worker.yml"]))
    write(root, ".github/workflows/worker.yml", reusable("worker", ["actions/checkout@" + A_COMMIT]))
    write(
        root,
        ".github/workflows/extra.yml",
        reusable("extra", ["actions/setup-node@" + B_COMMIT]),
    )


def reusable(name: str, calls: List[str]) -> str:
    """A reusable controller that calls only what a release may contain."""

    jobs = "".join(
        "  {0}:\n    uses: {1}\n".format(name + "-" + str(index), value)
        for index, value in enumerate(calls)
    )
    return (
        "name: {0}\n"
        "\n"
        "on:\n"
        "  workflow_call:\n"
        "\n"
        "jobs:\n"
        "{1}".format(name, jobs)
    )


def copy_release(source: pathlib.Path) -> pathlib.Path:
    """A second checkout holding the same release, for a before-and-after claim."""

    target = pathlib.Path(tempfile.mkdtemp(prefix="continuum-previous-"))
    shutil.copytree(source, target, dirs_exist_ok=True)
    return target


def _jobs(body: str) -> str:
    """A reusable controller whose jobs are exactly ``body``."""

    return "name: worker\n\non:\n  workflow_call:\n\njobs:\n" + body


def _engine_job(ref: str) -> str:
    """A controller that checks the engine out at ``ref``."""

    return _jobs(
        "  engine:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@" + A_COMMIT + "\n"
        "        with:\n"
        f"          repository: {pin.CONTINUUM_REPOSITORY}\n"
        f"          ref: {ref}\n"
    )


class TreeFixture(unittest.TestCase):
    """A disposable checkout to publish from."""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="continuum-publication-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


class ReleaseTagTests(unittest.TestCase):
    def test_an_exact_semver_tag_is_the_only_release_identity(self):
        for tag in ("v0.1.0", "v1.0.0", "v10.20.30", "v0.0.0"):
            with self.subTest(tag=tag):
                self.assertEqual(publication.require_release_tag(tag), tag)

    def test_anything_a_consumer_cannot_pin_is_refused(self):
        for tag in (
            "v0.1",
            "v0.1.0-rc.1",
            "v0.1.0+build",
            "v1",
            "0.1.0",
            "main",
            A_COMMIT,
            "release-2026-09-30",
        ):
            with self.subTest(tag=tag):
                with self.assertRaises(publication.PublicationRefusal):
                    publication.require_release_tag(tag)

    def test_the_refusal_says_why_and_what_to_do(self):
        with self.assertRaises(publication.PublicationRefusal) as caught:
            publication.require_release_tag("main")
        self.assertEqual(caught.exception.reason, "version-not-exact")
        self.assertIn("ADR-0002", str(caught.exception))
        self.assertIn("v0.1.0", caught.exception.remediation)

    def test_a_missing_or_padded_tag_is_refused(self):
        with self.assertRaises(publication.PublicationRefusal):
            publication.require_release_tag("")
        with self.assertRaises(publication.PublicationRefusal):
            publication.require_release_tag(" v0.1.0")
        with self.assertRaises(publication.PublicationRefusal):
            publication.require_release_tag("v0.1.0 ")
        with self.assertRaises(publication.PublicationRefusal):
            publication.require_release_tag("v0.1.0\nv0.2.0")

    def test_the_classification_is_the_consumers_own(self):
        """Two definitions of a release tag would eventually disagree."""

        for tag in ("v0.1.0", "v0.1", "v0.1.0-rc.1", "main", A_COMMIT):
            with self.subTest(tag=tag):
                accepted = True
                try:
                    publication.require_release_tag(tag)
                except publication.PublicationRefusal:
                    accepted = False
                self.assertEqual(accepted, pin.classify(tag) == "release")


class ReleaseCommitTests(unittest.TestCase):
    def test_a_full_commit_sha_is_the_identity(self):
        self.assertEqual(publication.require_release_commit(A_COMMIT), A_COMMIT)

    def test_anything_that_names_more_than_one_commit_is_refused(self):
        for commit in (
            "abc1234",
            A_COMMIT[:-1],
            A_COMMIT + "a",
            A_COMMIT.upper(),
            "main",
            "HEAD",
            "",
            " refs/tags/v0.1.0",
        ):
            with self.subTest(commit=commit):
                with self.assertRaises(publication.PublicationRefusal):
                    publication.require_release_commit(commit)


class OrderingTests(unittest.TestCase):
    def test_only_a_newer_published_release_blocks_one(self):
        for tags in (
            [],
            ["main"],
            ["v0.2.0-rc.1"],
            ["v0.1.0"],
            ["v0.0.9", "v0.1.0"],
        ):
            with self.subTest(tags=tags):
                self.assertIsNone(publication.require_forward(tags, "v0.1.1"))

    def test_a_release_older_than_an_existing_one_is_refused(self):
        for tags, tag in (
            (["v0.2.0"], "v0.1.0"),
            (["v0.0.9", "v0.2.0"], "v0.1.5"),
        ):
            with self.subTest(tags=tags, tag=tag):
                with self.assertRaises(publication.PublicationRefusal) as caught:
                    publication.require_forward(tags, tag)
                self.assertEqual(caught.exception.reason, "release-goes-backwards")
                self.assertIn("v0.2.0", caught.exception.remediation)

    def test_prereleases_are_not_releases(self):
        self.assertEqual(
            publication.exact_release_tags(["v1.0.0", "v0.9.0-rc.1", "nope"]),
            ["v1.0.0"],
        )

    def test_the_predecessor_has_to_be_the_one_being_replaced(self):
        self.assertEqual(
            publication.require_previous("v0.1.0", ["v0.1.0"], "v0.3.0"), "v0.1.0"
        )
        with self.assertRaises(publication.PublicationRefusal) as caught:
            publication.require_previous("v0.1.0", ["v0.1.0", "v0.2.0"], "v0.3.0")
        self.assertEqual(caught.exception.reason, "previous-mismatch")
        self.assertIn("v0.2.0", caught.exception.remediation)

    def test_a_prerelease_tag_is_not_a_predecessor(self):
        self.assertEqual(
            publication.require_previous("v0.1.0", ["v0.1.0", "v0.1.1-rc.1"], "v0.1.1"),
            "v0.1.0",
        )

    def test_a_release_with_a_predecessor_cannot_omit_the_comparison(self):
        """Otherwise the compatibility section of the published notes would
        describe nothing while looking as though it had."""

        with self.assertRaises(publication.PublicationRefusal) as caught:
            publication.require_previous("", ["v0.1.0"], "v0.1.1")
        self.assertEqual(caught.exception.reason, "previous-missing")
        self.assertIn("v0.1.0", caught.exception.remediation)

    def test_a_first_release_needs_no_predecessor(self):
        self.assertEqual(publication.require_previous("", [], "v0.1.0"), "")
        self.assertEqual(publication.require_previous("", ["v0.1.0"], "v0.1.0"), "")
        with self.assertRaises(publication.PublicationRefusal) as caught:
            publication.require_previous("v0.0.9", ["v0.1.0"], "v0.1.0")
        self.assertEqual(caught.exception.reason, "previous-not-a-release")

    def test_a_predecessor_that_cannot_be_checked_is_refused(self):
        """Offering a comparison target without the tags to check it against is a
        claim nobody can confirm, and it ends up in the published notes."""

        with self.assertRaises(publication.PublicationRefusal) as caught:
            publication.require_previous("v0.1.0", [], "v0.2.0")
        self.assertEqual(caught.exception.reason, "previous-unverified")
        self.assertIn("--tags-file", caught.exception.remediation)


# --------------------------------------------------------------------------
# Immutability
# --------------------------------------------------------------------------


class ImmutableReleaseTests(unittest.TestCase):
    def enabled(self):
        return publication.ReleaseState({"enabled": True, "enforced_by_owner": False}, (), ())

    def test_a_repository_without_immutable_releases_cannot_publish(self):
        for setting in (None, {"enabled": False}, {}):
            with self.subTest(setting=setting):
                state = publication.ReleaseState(setting, (), ())
                with self.assertRaises(publication.PublicationRefusal) as caught:
                    publication.require_immutable_releases(state)
                self.assertEqual(caught.exception.reason, "immutable-releases-disabled")
                self.assertIn("ADR-0002", str(caught.exception))
                self.assertIn("immutable-releases", caught.exception.remediation)

    def test_enabled_immutable_releases_are_a_precondition(self):
        self.assertIsNone(publication.require_immutable_releases(self.enabled()))

    def test_a_setting_that_cannot_be_read_is_not_an_enabled_setting(self):
        """An endpoint that 404s and an endpoint that errors look alike from the
        outside, and only one of them means 'off'."""

        for setting in (None, {"enabled": False}):
            with self.subTest(setting=setting):
                with self.assertRaises(publication.PublicationRefusal):
                    publication.require_immutable_releases(
                        publication.ReleaseState(setting, (), ())
                    )


class TagMovementTests(unittest.TestCase):
    def state(self, published=(), tags=(), enabled=True):
        return publication.ReleaseState(
            {"enabled": enabled}, tuple(published), tuple(tags)
        )

    def test_a_fresh_tag_may_be_published(self):
        self.assertEqual(
            publication.require_untouched_tag(self.state(), "v0.1.0", A_COMMIT), "publish"
        )

    def test_republishing_the_same_release_changes_nothing(self):
        state = self.state(published=[{"tag": "v0.1.0", "commit": A_COMMIT}])
        self.assertEqual(
            publication.require_untouched_tag(state, "v0.1.0", A_COMMIT),
            "already-published",
        )

    def test_a_published_tag_is_never_moved(self):
        state = self.state(published=[{"tag": "v0.1.0", "commit": A_COMMIT}])
        with self.assertRaises(publication.PublicationRefusal) as caught:
            publication.require_untouched_tag(state, "v0.1.0", B_COMMIT)
        self.assertEqual(caught.exception.reason, "tag-already-published")
        self.assertIn(B_COMMIT, caught.exception.remediation)

    def test_an_unpublished_tag_is_never_moved_either(self):
        state = self.state(tags=["v0.1.0"])
        with self.assertRaises(publication.PublicationRefusal) as caught:
            publication.require_untouched_tag(state, "v0.1.0", B_COMMIT)
        self.assertEqual(caught.exception.reason, "tag-already-exists")

    def test_an_unresolvable_published_tag_is_a_refusal_not_agreement(self):
        """The observer could not see where an existing tag points, so it does
        not agree that publishing is safe."""

        state = self.state(published=[{"tag": "v0.1.0", "commit": ""}])
        with self.assertRaises(publication.PublicationRefusal) as caught:
            publication.require_untouched_tag(state, "v0.1.0", A_COMMIT)
        self.assertEqual(caught.exception.reason, "tag-target-unknown")

    def test_state_is_read_from_the_observed_document(self):
        state = publication.ReleaseState.from_payload(
            {
                "immutable_releases": {"enabled": True},
                "published_releases": [{"tag": "v0.1.0", "commit": A_COMMIT}],
                "tags": ["v0.1.0"],
            }
        )
        self.assertEqual(state.published_at("v0.1.0"), A_COMMIT)
        self.assertIsNone(state.published_at("v0.2.0"))


class DecisionTests(unittest.TestCase):
    """`decide` is the whole gate, in the order the gates apply."""

    def candidate(self) -> publication.ReleaseCandidate:
        candidate = publication.ReleaseCandidate(
            tag="v0.1.0",
            commit=A_COMMIT,
            graph=publication.ReleaseGraph(
                entrypoint=ENTRYPOINT,
                workflows=(ENTRYPOINT,),
                engine_actions=(),
                third_party_actions=(),
                files=((".github/workflows/consumer.yml", "0" * 64),),
                digest="0" * 64,
            ),
            tree="",
            previous="",
            config_changes=(),
            compatibility=publication.Compatibility("compatible", ()),
            notes="## Select this release\n",
            manifest={},
        )
        return candidate

    def test_every_gate_enabled_publishes(self):
        state = publication.ReleaseState({"enabled": True}, (), ())
        self.assertEqual(publication.decide(self.candidate(), state), "publish")

    def test_no_gate_is_skipped_by_a_later_one_passing(self):
        off = publication.ReleaseState({"enabled": False}, (), ())
        with self.assertRaises(publication.PublicationRefusal):
            publication.decide(self.candidate(), off)
        behind = publication.ReleaseState({"enabled": True}, (), ["v0.2.0"])
        with self.assertRaises(publication.PublicationRefusal):
            publication.decide(self.candidate(), behind)


# --------------------------------------------------------------------------
# Graph closure
# --------------------------------------------------------------------------


class GraphClosureTests(TreeFixture):
    def graph(self, root: Optional[pathlib.Path] = None):
        return publication.release_graph(root or self.root)

    def test_a_release_is_the_graph_reachable_from_one_commit(self):
        minimal_release(self.root)
        graph = self.graph()
        self.assertEqual(graph.entrypoint, ENTRYPOINT)
        self.assertEqual(
            sorted(graph.workflows),
            sorted(
                {
                    ENTRYPOINT,
                    ".github/workflows/dispatcher.yml",
                    ".github/workflows/worker.yml",
                }
            ),
        )
        self.assertEqual(
            graph.third_party_actions,
            ("actions/checkout@" + A_COMMIT,),
        )
        self.assertEqual(len(graph.files), 3)
        for name, digest in graph.files:
            with self.subTest(name=name):
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_the_digest_is_one_value_over_every_file(self):
        minimal_release(self.root)
        first = self.graph()
        second = self.graph()
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.digest, publication._digest_of(first.files))
        self.assertEqual(
            publication._digest_of(list(reversed(first.files))), first.digest
        )
        self.assertNotEqual(first.digest, publication._digest_of(first.files[:-1]))

    def test_a_changed_file_changes_the_digest(self):
        minimal_release(self.root)
        before = self.graph()
        write(
            self.root,
            ".github/workflows/worker.yml",
            reusable("worker", []).replace("jobs:\n", "jobs:\n") + "",
        )
        self.assertNotEqual(before.digest, self.graph().digest)

    def test_a_reachable_escape_is_refused(self):
        """A release that can reach another repository, or a revision of itself
        that is not this commit, is not one commit."""

        cases = {
            "another-repository": (
                "  other:\n"
                f"    uses: {pin.CONTINUUM_REPOSITORY}/.github/workflows/consumer.yml@v0.1.0\n",
                "graph-not-closed",
            ),
            "floating-continuum-ref": (
                "  other:\n"
                "    uses: ./.github/workflows/consumer.yml@main\n",
                "graph-not-closed",
            ),
            "unpinned-third-party": (
                "  other:\n"
                "    uses: actions/checkout@v4\n",
                "graph-not-closed",
            ),
            "escaping-relative-path": (
                "  other:\n"
                "    uses: ../../../other/.github/workflows/consumer.yml\n",
                "graph-not-closed",
            ),
            "missing-relative-target": (
                "  other:\n"
                "    uses: ./.github/workflows/absent.yml\n",
                "graph-incomplete",
            ),
            "engine-pinned-independently": (
                "  engine:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@" + A_COMMIT + "\n"
                f"        with:\n          repository: {pin.CONTINUUM_REPOSITORY}\n          ref: main\n",
                "engine-pinned-independently",
            ),
            "fetched-from-outside": (
                "  clone:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: git clone https://github.com/"
                + pin.CONTINUUM_REPOSITORY
                + ".git\n",
                "graph-escapes-release",
            ),
        }
        for name, (body, reason) in cases.items():
            with self.subTest(case=name):
                root = pathlib.Path(tempfile.mkdtemp(prefix="continuum-escape-"))
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                minimal_release(root)
                write(root, ".github/workflows/worker.yml", _jobs(body))
                with self.assertRaises(publication.PublicationRefusal) as caught:
                    self.graph(root)
                self.assertEqual(caught.exception.reason, reason)
                self.assertTrue(caught.exception.remediation)

    def test_a_repository_without_the_release_entrypoint_is_not_a_release(self):
        with self.assertRaises(publication.PublicationRefusal) as caught:
            self.graph()
        self.assertEqual(caught.exception.reason, "entrypoint-missing")

    def test_the_running_workflows_own_commit_is_the_only_accepted_revision(self):
        for ref in publication.RELEASE_SOURCES:
            with self.subTest(ref=ref):
                root = pathlib.Path(tempfile.mkdtemp(prefix="continuum-engine-"))
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                minimal_release(root)
                write(root, ".github/workflows/engine.yml", _engine_job(ref))
                write(
                    root,
                    ".github/workflows/dispatcher.yml",
                    reusable("dispatcher", ["./.github/workflows/engine.yml"]),
                )
                graph = self.graph(root)
                self.assertIn(".github/workflows/engine.yml", graph.workflows)

    def test_a_continuum_checkout_of_anything_else_is_refused(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="continuum-engine-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        minimal_release(root)
        write(root, ".github/workflows/engine.yml", _engine_job("main"))
        write(
            root,
            ".github/workflows/dispatcher.yml",
            reusable("dispatcher", ["./.github/workflows/engine.yml"]),
        )
        with self.assertRaises(publication.PublicationRefusal) as caught:
            self.graph(root)
        self.assertEqual(caught.exception.reason, "engine-pinned-independently")

    def test_a_local_action_directory_ships_with_the_release(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="continuum-action-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        minimal_release(root)
        write(
            root,
            ".github/actions/engine-path/action.yml",
            "name: engine-path\ndescription: resolve the engine\nruns:\n  using: composite\n"
            "  steps:\n    - run: echo engine\n      shell: bash\n",
        )
        write(
            root,
            ".github/workflows/worker.yml",
            reusable("worker", [publication.ENGINE_PREFIX + ".github/actions/engine-path"]),
        )
        graph = self.graph(root)
        self.assertEqual(graph.engine_actions, (".github/actions/engine-path",))
        self.assertIn(
            ".github/actions/engine-path/action.yml", [name for name, _ in graph.files]
        )

    def test_a_local_bytecode_cache_is_not_part_of_a_release(self):
        """A manifest is a statement about the release commit, and a generated
        cache is not in it."""

        minimal_release(self.root)
        before = self.graph().digest
        write(self.root, "src/continuum/__pycache__/publication.cpython-312.pyc", "junk")
        write(self.root, "src/continuum/loader.py", "x = 1\n")
        names = [name for name, _ in self.graph().files]
        self.assertNotIn("src/continuum/__pycache__/publication.cpython-312.pyc", names)
        self.assertEqual(self.graph().digest, before)

    def test_the_repository_ships_a_closed_release_graph(self):
        """The real repository, not a synthetic tree: the rule that holds here is
        the rule the first release is published under."""

        graph = publication.release_graph(REPO_ROOT)
        self.assertEqual(graph.entrypoint, ENTRYPOINT)
        self.assertIn(ENTRYPOINT, graph.workflows)
        self.assertGreaterEqual(len(graph.workflows), 11)
        for reference in graph.third_party_actions:
            with self.subTest(reference=reference):
                self.assertRegex(reference, r"@[0-9a-f]{40}$")
        self.assertEqual(
            sorted(graph.third_party_actions),
            sorted(set(graph.third_party_actions)),
            "a duplicated action reference would hide a dependency",
        )
        for name, digest in graph.files:
            with self.subTest(name=name):
                self.assertTrue(name.endswith((".yml", ".py", ".sh")), name)
                self.assertFalse(name.startswith(".."), name)


# --------------------------------------------------------------------------
# Derived notes and manifest
# --------------------------------------------------------------------------


class ConfigurationChangeTests(TreeFixture):
    def test_only_the_configuration_surface_is_a_schema_change(self):
        changed = [
            "README.md",
            ".continuum.yml",
            "src/continuum/publication.py",
            "docs/continuum-mvp-contract.md",
        ]
        self.assertEqual(
            publication.config_schema_changes(changed),
            (".continuum.yml", "docs/continuum-mvp-contract.md"),
        )

    def test_a_version_bump_is_a_schema_change(self):
        minimal_release(self.root)
        write(self.root, ".continuum.yml", "version: 1\n")
        self.assertEqual(publication.config_version(self.root), "1")
        self.assertEqual(publication.config_schema_changes(["src/continuum/cli.py"]), ())
        previous = pathlib.Path(tempfile.mkdtemp(prefix="continuum-old-"))
        self.addCleanup(shutil.rmtree, previous, ignore_errors=True)
        minimal_release(previous)
        write(previous, ".continuum.yml", "version: 2\n")
        candidate = publication.build_candidate(
            tag="v0.2.0",
            commit=B_COMMIT,
            root=self.root,
            previous_root=previous,
            previous_tag="v0.1.0",
            tags=["v0.1.0"],
            changed_paths=[".continuum.yml"],
        )
        self.assertEqual(candidate.config_changes, (".continuum.yml",))
        self.assertIn("Configuration schema changes", candidate.notes)
        self.assertIn("version", candidate.notes)


class CompatibilityTests(TreeFixture):
    def test_the_first_release_claims_nothing(self):
        minimal_release(self.root)
        candidate = publication.build_candidate(
            tag="v0.1.0", commit=A_COMMIT, root=self.root
        )
        self.assertEqual(candidate.compatibility.level, "compatible")
        self.assertTrue(
            any(
                "First Continuum release" in finding
                for finding in candidate.compatibility.findings
            )
        )

    def test_an_unchanged_graph_is_compatible(self):
        minimal_release(self.root)
        previous = copy_release(self.root)
        self.addCleanup(shutil.rmtree, previous, ignore_errors=True)
        candidate = publication.build_candidate(
            tag="v0.1.1",
            commit=B_COMMIT,
            root=self.root,
            previous_root=previous,
            previous_tag="v0.1.0",
            tags=["v0.1.0"],
        )
        self.assertEqual(candidate.compatibility.level, "compatible")

    def test_a_new_workflow_is_additive(self):
        minimal_release(self.root)
        write(
            self.root,
            ".github/workflows/dispatcher.yml",
            reusable("dispatcher", ["./.github/workflows/worker.yml", "./.github/workflows/extra.yml"]),
        )
        previous = pathlib.Path(tempfile.mkdtemp(prefix="continuum-old-"))
        self.addCleanup(shutil.rmtree, previous, ignore_errors=True)
        minimal_release(previous)
        candidate = publication.build_candidate(
            tag="v0.1.1",
            commit=B_COMMIT,
            root=self.root,
            previous_root=previous,
            previous_tag="v0.1.0",
            tags=["v0.1.0"],
        )
        self.assertEqual(candidate.compatibility.level, "additive")
        self.assertTrue(
            any(
                ".github/workflows/extra.yml" in finding
                for finding in candidate.compatibility.findings
            )
        )

    def test_a_changed_release_entrypoint_is_reviewed(self):
        """The entrypoint is what a consumer's ingress names, so a change to its
        bytes is consumer-visible.

        The change here is a comment, which proves the point: the classification
        is a statement about bytes, not about parsed semantics, because a
        byte-identical entrypoint is what makes an existing ingress provably
        still valid.
        """

        minimal_release(self.root)
        previous = copy_release(self.root)
        self.addCleanup(shutil.rmtree, previous, ignore_errors=True)
        write(self.root, ENTRYPOINT, release_entrypoint().replace("# A consumer", "# Consumers"))
        candidate = publication.build_candidate(
            tag="v0.1.1",
            commit=B_COMMIT,
            root=self.root,
            previous_root=previous,
            previous_tag="v0.1.0",
            tags=["v0.1.0"],
            changed_paths=[ENTRYPOINT],
        )
        self.assertEqual(candidate.compatibility.level, "review")
        self.assertTrue(
            any("review" in finding for finding in candidate.compatibility.findings)
        )

    def test_a_removed_workflow_is_breaking(self):
        minimal_release(self.root)
        (self.root / ".github/workflows/worker.yml").unlink()
        write(self.root, ".github/workflows/dispatcher.yml", reusable("dispatcher", []))
        previous = pathlib.Path(tempfile.mkdtemp(prefix="continuum-old-"))
        self.addCleanup(shutil.rmtree, previous, ignore_errors=True)
        minimal_release(previous)
        candidate = publication.build_candidate(
            tag="v0.2.0",
            commit=B_COMMIT,
            root=self.root,
            previous_root=previous,
            previous_tag="v0.1.0",
            tags=["v0.1.0"],
        )
        self.assertEqual(candidate.compatibility.level, "breaking")

    def test_the_notes_state_the_level_rather_than_leaving_it_to_be_inferred(self):
        minimal_release(self.root)
        candidate = publication.build_candidate(
            tag="v0.1.0", commit=A_COMMIT, root=self.root
        )
        self.assertIn(candidate.compatibility.level, candidate.notes)


class NotesTests(TreeFixture):
    def candidate(self, **overrides):
        params = dict(tag="v0.1.0", commit=A_COMMIT, root=self.root)
        params.update(overrides)
        return publication.build_candidate(**params)

    def test_every_required_section_is_present_and_ordered(self):
        minimal_release(self.root)
        notes = self.candidate().notes
        positions = []
        for section in publication.NOTES_SECTIONS:
            self.assertIn(section, notes, section)
            positions.append(notes.index(section))
        self.assertEqual(positions, sorted(positions))

    def test_the_notes_name_what_a_consumer_actually_pins(self):
        minimal_release(self.root)
        notes = self.candidate().notes
        self.assertIn(f"{pin.CONTINUUM_REPOSITORY}/{ENTRYPOINT}@v0.1.0", notes)
        self.assertIn(A_COMMIT, notes)
        self.assertIn(self.candidate().digest, notes)
        self.assertIn("release pin", notes)

    def test_the_notes_say_the_release_does_not_adopt_anything(self):
        minimal_release(self.root)
        notes = self.candidate().notes
        self.assertIn("no consumer", notes)
        self.assertIn("Upgrade", notes)

    def test_the_notes_are_derived_so_identical_inputs_give_identical_bytes(self):
        minimal_release(self.root)
        first = self.candidate().notes
        second = self.candidate().notes
        self.assertEqual(first, second)
        (self.root / ".github/workflows/dispatcher.yml").write_text(
            reusable("dispatcher", ["actions/setup-node@" + C_COMMIT]), encoding="utf-8"
        )
        self.assertNotEqual(self.candidate().notes, first)


class ManifestTests(TreeFixture):
    def candidate(self, **overrides):
        params = dict(tag="v0.1.0", commit=A_COMMIT, root=self.root)
        params.update(overrides)
        return publication.build_candidate(**params)

    def test_the_manifest_is_the_release_as_data(self):
        minimal_release(self.root)
        manifest = self.candidate(tree="t" * 40).manifest
        self.assertEqual(manifest["schema"], publication.MANIFEST_SCHEMA)
        self.assertEqual(manifest["release"], "v0.1.0")
        self.assertEqual(manifest["commit"], A_COMMIT)
        self.assertEqual(manifest["tree"], "t" * 40)
        self.assertEqual(manifest["entrypoint"], ENTRYPOINT)
        self.assertEqual(
            sorted(manifest["workflows"]),
            sorted(
                {
                    ENTRYPOINT,
                    ".github/workflows/dispatcher.yml",
                    ".github/workflows/worker.yml",
                }
            ),
        )
        self.assertRegex(manifest["digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(manifest["previous"], "")

    def test_the_document_records_the_decision_it_was_made_under(self):
        minimal_release(self.root)
        candidate = self.candidate()
        document = json.loads(publication.manifest_document(candidate, "publish"))
        self.assertEqual(document["decision"], "publish")
        self.assertEqual(document["digest"], candidate.digest)
        self.assertEqual(document["compatibility"], "compatible")

    def test_a_published_document_is_byte_identical_for_identical_input(self):
        minimal_release(self.root)
        first = publication.manifest_document(self.candidate(), "publish")
        second = publication.manifest_document(self.candidate(), "publish")
        self.assertEqual(first, second)
        self.assertEqual(publication.manifest_document(self.candidate(), "publish"), first)


class CandidateTests(TreeFixture):
    def test_a_candidate_is_the_release_and_nothing_more(self):
        minimal_release(self.root)
        candidate = publication.build_candidate(
            tag="v0.1.0", commit=A_COMMIT, root=self.root
        )
        self.assertEqual(candidate.tag, "v0.1.0")
        self.assertEqual(candidate.commit, A_COMMIT)
        self.assertEqual(candidate.previous, "")
        self.assertEqual(candidate.digest, candidate.graph.digest)
        self.assertTrue(candidate.notes.endswith("\n"))

    def test_building_refuses_before_it_reads_a_tree(self):
        with self.assertRaises(publication.PublicationRefusal):
            publication.build_candidate(
                tag="main", commit=A_COMMIT, root=pathlib.Path("/nonexistent")
            )
        with self.assertRaises(publication.PublicationRefusal):
            publication.build_candidate(
                tag="v0.1.0", commit="short", root=pathlib.Path("/nonexistent")
            )

    def test_the_repository_validates_as_a_first_release(self):
        candidate = publication.build_candidate(
            tag="v0.1.0", commit=A_COMMIT, root=REPO_ROOT
        )
        self.assertIn(ENTRYPOINT, candidate.graph.workflows)
        self.assertIn("## Upgrade", candidate.notes)


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


class PublishCommandTests(TreeFixture):
    """`continuum release publish` is how the workflow drives all of this."""

    def run_cli(self, argv: List[str]):
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue()

    def payload(self, stdout: str) -> dict:
        """The machine-facing document, which is printed before the notices."""

        start = stdout.find("{")
        self.assertGreaterEqual(start, 0, "no JSON document was printed: " + stdout)
        document, _ = json.JSONDecoder().raw_decode(stdout[start:])
        return document

    def write(self, relative: str, text: str) -> str:
        return str(write(self.root, relative, text))

    def state_file(self, document) -> str:
        return self.write("state.json", json.dumps(document))

    def publish_argv(self, *extra: str) -> List[str]:
        return [
            "release",
            "publish",
            "--tag",
            "v0.1.0",
            "--commit",
            A_COMMIT,
            "--root",
            str(self.root),
            *extra,
        ]

    def test_it_validates_a_candidate_and_writes_the_documents(self):
        minimal_release(self.root)
        out_dir = self.root / "out"
        code, stdout, _ = self.run_cli(
            self.publish_argv("--out-dir", str(out_dir))
        )
        self.assertEqual(code, 0, stdout)
        payload = self.payload(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["decision"], "candidate")
        self.assertEqual(payload["release"], "v0.1.0")
        self.assertIn("release-notes.md", os.listdir(out_dir))
        self.assertIn("release-manifest.json", os.listdir(out_dir))

    def test_validation_alone_never_decides_to_publish(self):
        """Without observed repository state there is nothing to decide, and a
        candidate must not read as a publication."""

        minimal_release(self.root)
        code, stdout, _ = self.run_cli(self.publish_argv())
        self.assertEqual(code, 0, stdout)
        self.assertEqual(self.payload(stdout)["decision"], "candidate")

    def test_an_unobservable_repository_state_is_a_refusal_with_a_reason(self):
        minimal_release(self.root)
        state = self.state_file({"immutable_releases": {"enabled": False}})
        code, stdout, _ = self.run_cli(self.publish_argv("--state", state))
        self.assertEqual(code, 1)
        payload = self.payload(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "immutable-releases-disabled")

    def test_a_matching_digest_publishes(self):
        minimal_release(self.root)
        candidate = publication.build_candidate(
            tag="v0.1.0", commit=A_COMMIT, root=self.root
        )
        state = self.state_file({"immutable_releases": {"enabled": True}})
        code, stdout, _ = self.run_cli(
            self.publish_argv("--state", state, "--expect-digest", candidate.digest)
        )
        self.assertEqual(code, 0, stdout)
        self.assertEqual(self.payload(stdout)["decision"], "publish")

    def test_a_digest_that_does_not_match_refuses_to_publish(self):
        """The deciding side has to be holding the same bytes the validating side
        held, or 'the validated release' describes a different one."""

        minimal_release(self.root)
        state = self.state_file({"immutable_releases": {"enabled": True}})
        code, stdout, _ = self.run_cli(
            self.publish_argv("--state", state, "--expect-digest", "0" * 64)
        )
        self.assertEqual(code, 1)
        self.assertEqual(self.payload(stdout)["reason"], "graph-digest-mismatch")

    def test_a_tag_that_already_exists_is_refused_not_moved(self):
        minimal_release(self.root)
        state = self.state_file(
            {"immutable_releases": {"enabled": True}, "tags": ["v0.1.0"]}
        )
        code, stdout, _ = self.run_cli(self.publish_argv("--state", state))
        self.assertEqual(code, 1)
        self.assertIn(
            "tag-already-exists", stdout
        )

    def test_a_release_behind_a_newer_one_is_refused(self):
        minimal_release(self.root)
        state = self.state_file(
            {"immutable_releases": {"enabled": True}, "tags": ["v0.2.0"]}
        )
        code, stdout, _ = self.run_cli(self.publish_argv("--state", state))
        self.assertEqual(code, 1)
        self.assertIn("release-goes-backwards", stdout)

    def test_a_refused_tag_never_writes_documents(self):
        minimal_release(self.root)
        out_dir = self.root / "out"
        code, stdout, _ = self.run_cli(
            self.publish_argv("--out-dir", str(out_dir), "--tag", "main")
        )
        self.assertEqual(code, 1)
        self.assertIn("version-not-exact", stdout)
        self.assertFalse(out_dir.exists())

    def test_changed_paths_and_tags_are_read_from_files(self):
        minimal_release(self.root)
        previous = pathlib.Path(tempfile.mkdtemp(prefix="continuum-old-"))
        self.addCleanup(shutil.rmtree, previous, ignore_errors=True)
        minimal_release(previous)
        write(previous, ".github/workflows/dispatcher.yml", reusable("dispatcher", []))
        write(self.root, "changed.txt", ".continuum.yml\nREADME.md\n")
        write(self.root, "tags.txt", "v0.1.0\nv0.1.1-rc.1\n")
        out_dir = self.root / "out"
        code, stdout, _ = self.run_cli(
            self.publish_argv(
                "--tag",
                "v0.2.0",
                "--previous-tag",
                "v0.1.0",
                "--tags-file",
                str(self.root / "tags.txt"),
                "--previous-root",
                str(previous),
                "--changed-from-file",
                str(self.root / "changed.txt"),
                "--out-dir",
                str(out_dir),
            )
        )
        self.assertEqual(code, 0, stdout)
        payload = self.payload(stdout)
        self.assertEqual(payload["previous"], "v0.1.0")
        self.assertEqual(payload["config_schema_changes"], [".continuum.yml"])
        notes = (out_dir / "release-notes.md").read_text(encoding="utf-8")
        self.assertIn("v0.1.0", notes)

    def run_process(self, argv: List[str]):
        """The command as an operator runs it, so its exit status is real."""

        environment = dict(os.environ, PYTHONPATH="src")
        return subprocess.run(
            [sys.executable, "-m", "continuum.cli", *argv],
            cwd=str(REPO_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_a_missing_state_file_is_an_error_not_a_decision(self):
        """"I could not see whether immutability is enabled" must not read as
        "it is not"."""

        minimal_release(self.root)
        out_dir = self.root / "out"
        result = self.run_process(
            [
                "release",
                "publish",
                "--tag",
                "v0.1.0",
                "--commit",
                A_COMMIT,
                "--root",
                str(self.root),
                "--state",
                str(self.root / "absent.json"),
                "--out-dir",
                str(out_dir),
            ]
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CONTINUUM_ERROR", result.stderr)
        self.assertNotIn("release-manifest.json", result.stdout)
        self.assertFalse(out_dir.exists())

    def test_a_state_file_that_is_not_an_object_is_an_error(self):
        minimal_release(self.root)
        state = self.write("state.json", "[]")
        code, stdout, _ = self.run_cli(self.publish_argv("--state", state))
        self.assertNotEqual(code, 0)
        self.assertNotIn("decision", stdout)


if __name__ == "__main__":
    unittest.main()