"""Pinning a release to one version, and refusing to guess.

Versioning is where a release goes wrong quietly: two sources disagree, a
project file is edited after the tag, or a manual version is passed to a
repository that also has a file. The tests are therefore mostly about the
disagreement cases, because the agreeing case is the one that needs no test.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from continuum.release import version
from continuum.release.contract import ReleaseEvent
from continuum.release.version import (
    ExplicitVersion,
    ProjectFileVersion,
    ReleasePullRequestVersion,
    TagVersion,
    UnknownStrategy,
    VersionAgreement,
    VersionContext,
    VersionError,
    VersionPolicy,
    VersionProposal,
    VersionStrategy,
    agree,
    get_strategy,
    is_prerelease,
    is_version,
    normalize,
    require_agreement,
    strategies,
    tag_for,
    version_from_tag,
)

REPOSITORY = "example/widgets"


def event(sha: str = "a" * 40, tag: str = "v1.4.0", name: str = "tag-push", **attributes: str):
    return ReleaseEvent(
        repository=REPOSITORY,
        name=name,
        sha=sha,
        tag=tag,
        ref=f"refs/tags/{tag}" if tag else "",
        attributes=tuple(sorted(attributes.items())),
    )


def context(**overrides) -> VersionContext:
    values = {"event": event()}
    values.update(overrides)
    return VersionContext(**values)


def proposal(version_value: str, source: str = "project-file") -> VersionProposal:
    return VersionProposal(version=version_value, source=source)


class NormalisationTests(unittest.TestCase):
    def test_a_bare_version_is_recognised_and_a_tag_is_not(self):
        self.assertTrue(is_version("1.4.0"))
        self.assertTrue(is_version("1.4.0-rc.1"))
        self.assertFalse(is_version("v1.4.0"))
        self.assertFalse(is_version("1.4"))

    def test_a_prerelease_is_told_from_a_stable_version(self):
        self.assertTrue(is_prerelease("1.4.0-rc.1"))
        self.assertFalse(is_prerelease("1.4.0"))
        self.assertFalse(is_prerelease("1.4.0+17"))

    def test_a_tag_and_a_version_round_trip(self):
        self.assertEqual(tag_for("1.4.0"), "v1.4.0")
        self.assertEqual(version_from_tag("v1.4.0"), "1.4.0")
        self.assertEqual(tag_for("1.4.0", "release-"), "release-1.4.0")
        self.assertEqual(version_from_tag("release-1.4.0", "release-"), "1.4.0")

    def test_a_tag_that_is_not_a_version_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(VersionError):
            version_from_tag("release-candidate")

    def test_normalising_strips_the_tag_prefix_and_the_whitespace(self):
        self.assertEqual(normalize(" v1.4.0\n"), "1.4.0")


class PolicyTests(unittest.TestCase):
    def test_a_default_policy_admits_any_stable_version(self):
        policy = VersionPolicy()
        self.assertTrue(policy.admit("1.4.0"))
        self.assertTrue(policy.admit("0.0.1"))
        self.assertFalse(policy.admit("1.4"))
        self.assertFalse(policy.admit("v1.4.0"))

    def test_a_prerelease_needs_to_be_asked_for(self):
        self.assertFalse(VersionPolicy().admit("1.5.0-rc.1"))
        self.assertTrue(VersionPolicy(allow_prerelease=True).admit("1.5.0-rc.1"))

    def test_a_one_part_series_admits_every_minor_of_that_major(self):
        policy = VersionPolicy(allowed_series=("1",))
        self.assertTrue(policy.admit("1.0.0"))
        self.assertTrue(policy.admit("1.9.9"))
        self.assertFalse(policy.admit("2.0.0"))

    def test_a_two_part_series_admits_only_that_minor(self):
        policy = VersionPolicy(allowed_series=("1.4",))
        self.assertTrue(policy.admit("1.4.7"))
        self.assertFalse(policy.admit("1.5.0"))

    def test_a_series_that_is_not_a_series_is_refused_at_configuration_time(self):
        with self.assertRaises(VersionError):
            VersionPolicy(allowed_series=("1.4.0",))

    def test_a_tag_prefix_that_would_be_unsafe_in_a_filename_is_refused(self):
        with self.assertRaises(VersionError):
            VersionPolicy(tag_prefix="v 1.4.0/")

    def test_a_refused_version_names_what_was_allowed(self):
        with self.assertRaises(VersionError) as caught:
            VersionPolicy(allowed_series=("1",)).require("2.0.0")
        self.assertIn("2.0.0", str(caught.exception))
        self.assertEqual(VersionPolicy(allowed_series=("1",)).require("1.2.3"), "1.2.3")

    def test_a_refused_prerelease_suggests_the_opt_in_rather_than_a_rename(self):
        with self.assertRaises(VersionError) as caught:
            VersionPolicy().require("1.5.0-rc.1")
        self.assertIn("allow_prerelease", str(caught.exception))


class AgreementTests(unittest.TestCase):
    def test_sources_that_agree_are_one_answer(self):
        agreement = agree(proposal("1.4.0", "project-file"), proposal("1.4.0", "explicit"))
        self.assertTrue(agreement.ok)
        self.assertTrue(agreement.agreed)
        self.assertEqual(agreement.version, "1.4.0")
        self.assertEqual(agreement.sources, ("explicit", "project-file"))
        self.assertEqual(require_agreement(agreement), "1.4.0")

    def test_sources_that_disagree_stop_the_release(self):
        agreement = agree(proposal("1.4.0", "project-file"), proposal("1.5.0", "explicit"))
        self.assertFalse(agreement.ok)
        self.assertEqual(agreement.version, "")
        with self.assertRaises(VersionError) as caught:
            require_agreement(agreement)
        self.assertIn("1.4.0", str(caught.exception))
        self.assertIn("1.5.0", str(caught.exception))

    def test_a_disagreement_is_not_a_vote(self):
        # Two sources for 1.4.0 and one for 1.5.0 is still a disagreement: the
        # number that lost is not wrong because it lost.
        agreement = agree(
            proposal("1.4.0", "a"),
            proposal("1.4.0", "b"),
            proposal("1.5.0", "c"),
        )
        self.assertFalse(agreement.agreed)

    def test_a_version_with_no_source_at_all_is_refused(self):
        with self.assertRaises(VersionError):
            agree()

    def test_an_agreement_describes_itself_for_a_job_log(self):
        described = agree(proposal("1.4.0", "project-file")).describe()
        self.assertEqual(described["version"], "1.4.0")
        self.assertTrue(described["agreed"])
        self.assertEqual(described["sources"], ["project-file"])


class ProposalTests(unittest.TestCase):
    def test_a_proposal_carries_a_bare_version_not_a_tag(self):
        with self.assertRaises(VersionError):
            VersionProposal(version="v1.4.0", source="tag")

    def test_a_proposal_must_name_the_strategy_that_produced_it(self):
        with self.assertRaises(VersionError):
            VersionProposal(version="1.4.0", source="")

    def test_a_proposal_names_its_origin(self):
        described = VersionProposal(
            version="1.4.0", source="tag", origin="the tag that was pushed"
        ).describe()
        self.assertEqual(described["tag"], "v1.4.0")
        self.assertEqual(described["origin"], "the tag that was pushed")


class ExplicitVersionTests(unittest.TestCase):
    def test_a_manual_version_is_taken_as_given(self):
        found = ExplicitVersion().propose(context(requested="1.4.0"), VersionPolicy())
        self.assertEqual(found.version, "1.4.0")
        self.assertEqual(found.source, "explicit")

    def test_a_version_on_the_strategy_beats_one_on_the_dispatch(self):
        found = ExplicitVersion(requested="1.5.0").propose(
            context(requested="1.4.0"), VersionPolicy()
        )
        self.assertEqual(found.version, "1.5.0")

    def test_no_version_at_all_is_an_error_not_a_default(self):
        with self.assertRaises(VersionError) as caught:
            ExplicitVersion().propose(context(), VersionPolicy())
        self.assertIn("without a version", str(caught.exception))

    def test_it_reads_nothing(self):
        self.assertEqual(ExplicitVersion().collect(event(), ""), ("", ""))


class TagVersionTests(unittest.TestCase):
    def test_the_tag_is_the_version(self):
        strategy = TagVersion()
        text, source = strategy.collect(event(tag="v1.4.0"), "")
        found = strategy.propose(context(text=text, source_path=source), VersionPolicy())
        self.assertEqual(found.version, "1.4.0")
        self.assertEqual(found.source, "tag")
        self.assertEqual(found.tag, "v1.4.0")

    def test_a_tag_that_is_not_a_version_is_refused(self):
        with self.assertRaises(VersionError):
            TagVersion().propose(
                context(text="release-candidate"), VersionPolicy()
            )

    def test_a_tag_push_with_no_tag_has_nothing_to_take(self):
        strategy = TagVersion()
        text, _ = strategy.collect(event(name="push", tag=""), "")
        with self.assertRaises(VersionError):
            strategy.propose(context(text=text), VersionPolicy())


class ProjectFileVersionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "VERSION")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("1.4.0\n")

    def _propose(self, policy: VersionPolicy = None) -> VersionProposal:
        strategy = ProjectFileVersion(self.path)
        text, source = strategy.collect(event(), self.directory)
        return strategy.propose(
            context(text=text, source_path=source), policy or VersionPolicy()
        )

    def test_a_strategy_with_no_file_to_read_is_refused_at_configuration_time(self):
        with self.assertRaises(VersionError) as caught:
            ProjectFileVersion("")
        self.assertIn("guess", str(caught.exception))

    def test_the_file_is_the_version(self):
        found = self._propose()
        self.assertEqual(found.version, "1.4.0")
        self.assertEqual(found.source, "project-file")
        self.assertEqual(found.origin, self.path)

    def test_a_relative_path_is_resolved_against_the_workdir(self):
        strategy = ProjectFileVersion("VERSION")
        text, source = strategy.collect(event(), self.directory)
        self.assertEqual(source, self.path)
        self.assertEqual(text.strip(), "1.4.0")

    def test_a_file_the_policy_refuses_stops_the_release_at_the_file(self):
        with self.assertRaises(VersionError) as caught:
            self._propose(VersionPolicy(allowed_series=("2",)))
        self.assertIn("1.4.0", str(caught.exception))

    def test_a_missing_file_is_an_error_where_it_is_read(self):
        strategy = ProjectFileVersion(os.path.join(self.directory, "absent"))
        with self.assertRaises(VersionError) as caught:
            strategy.collect(event(), self.directory)
        self.assertIn("absent", str(caught.exception))

    def test_an_empty_file_is_refused_rather_than_read_as_nothing(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaises(VersionError):
            self._propose()

    def test_a_file_naming_a_pre_release_is_refused_by_a_stable_policy(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("1.5.0-rc.1\n")
        with self.assertRaises(VersionError):
            self._propose()
        self.assertEqual(
            self._propose(VersionPolicy(allow_prerelease=True)).version, "1.5.0-rc.1"
        )


class ReleasePullRequestVersionTests(unittest.TestCase):
    def test_the_merged_pull_request_names_the_version(self):
        strategy = ReleasePullRequestVersion(pr_number=17)
        text, source = strategy.collect(
            event(name="release-pr-merged", tag="", **{"release-pr-version": "1.4.0"}), ""
        )
        found = strategy.propose(
            context(event=event(name="release-pr-merged", tag=""), text=text, release_pr=17),
            VersionPolicy(),
        )
        self.assertEqual(found.version, "1.4.0")
        self.assertEqual(found.source, "release-pr")
        self.assertIn("17", found.origin + found.detail)

    def test_a_release_with_no_proposed_version_is_refused(self):
        with self.assertRaises(VersionError) as caught:
            ReleasePullRequestVersion(pr_number=17).propose(
                context(event=event(name="release-pr-merged", tag="")), VersionPolicy()
            )
        self.assertIn("no proposed version", str(caught.exception))

    def test_a_manifest_path_is_read_when_the_event_carries_no_version(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "release.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('version = "1.4.0"\n')
        strategy = ReleasePullRequestVersion(pr_number=17, manifest_path=path)
        text, source = strategy.collect(event(), directory)
        self.assertIn("1.4.0", text)
        self.assertEqual(source, path)

    def test_a_configured_manifest_that_is_not_there_is_refused(self):
        with self.assertRaises(VersionError) as caught:
            ReleasePullRequestVersion(manifest_path="release.toml").collect(event(), "/tmp")
        self.assertIn("release.toml", str(caught.exception))


class RegistryTests(unittest.TestCase):
    def test_the_four_strategies_are_registered(self):
        self.assertEqual(
            strategies(), ("explicit", "project-file", "release-pr", "tag")
        )

    def test_every_strategy_can_be_built_and_can_describe_itself(self):
        options = {
            "explicit": {},
            "tag": {},
            "project-file": {"path": "VERSION"},
            "release-pr": {},
        }
        for name, kwargs in options.items():
            strategy = get_strategy(name, **kwargs)
            self.assertIsInstance(strategy, VersionStrategy)
            self.assertEqual(strategy.name, name)
            self.assertTrue(strategy.intent())
            self.assertTrue(strategy.SUPPORTS_DRY_RUN)

    def test_an_unknown_strategy_is_refused_by_name(self):
        with self.assertRaises(UnknownStrategy) as caught:
            get_strategy("vibes")
        self.assertIn("vibes", str(caught.exception))

    def test_a_strategy_configured_with_options_it_does_not_take_is_refused(self):
        with self.assertRaises(UnknownStrategy):
            get_strategy("tag", nonsense="x")

    def test_a_strategy_that_will_not_name_itself_is_refused(self):
        class Anonymous:
            name = ""

            def intent(self):
                return "an unnamed strategy"

        try:
            version.register_strategy("anonymous-check", Anonymous)
            with self.assertRaises(UnknownStrategy) as caught:
                get_strategy("anonymous-check")
            self.assertIn("anonymous-check", str(caught.exception))
        finally:
            version._STRATEGIES.pop("anonymous-check", None)

    def test_registering_the_same_name_twice_is_refused(self):
        with self.assertRaises(UnknownStrategy) as caught:
            version.register_strategy("tag", TagVersion)
        self.assertIn("already registered", str(caught.exception))

    def test_a_strategy_registered_without_a_name_is_refused(self):
        with self.assertRaises(UnknownStrategy):
            version.register_strategy("", TagVersion)


class AgreementValueTests(unittest.TestCase):
    def test_an_agreement_with_no_sources_has_agreed_nothing(self):
        self.assertFalse(VersionAgreement(version="1.4.0").agreed)
        self.assertTrue(VersionAgreement(version="1.4.0").ok)


if __name__ == "__main__":
    unittest.main()
