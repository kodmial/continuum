"""Cutting a repository's release over to Continuum, and proving nothing was lost.

The tests are grouped by the four questions a cutover has to answer, because each
one fails differently and a single assertion per mechanism would hide that.

The ledger is about *coverage*: whether this release of Continuum actually
supplies what the consumer's own writers used to. The comparison is about
*agreement*, and specifically about the difference between a measured match and a
silent one. The canary is about *repeatability*, and it runs a real release
twice through the real core rather than asserting that a mock was not called.
The rollback pin is about the way back, and its tests are mostly about refusing:
the pin is the one thing whose premature deletion is invisible until it is
needed.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest

from continuum.release import adoption
from continuum.release.adoption import (
    AdoptionError,
    Preserved,
    ReleaseFacts,
    canary,
    compare,
    ledger,
    require_retained,
    require_retirable,
    retention,
)
from continuum.release.core import ReleaseComponents, ReleaseCore, ReleaseRequest
from continuum.release.publishers import homebrew, macports, parse_settings
from continuum.release.publishers.assets import InMemoryAssetFetcher
from continuum.release.publishers.contract import (
    ARCH_ARM64,
    ARCH_X86_64,
    ArtifactManifest,
    PublishRequest,
    PublishedArtifact,
    ReleaseIdentity,
)
from continuum.release.publishers.validation import GeneratedFile

from . import release_core_support as support

VERSION = support.VERSION
SHA = support.SHA
OTHER_SHA = support.OTHER_SHA
REPOSITORY = support.REPOSITORY

RELEASE_BASE = f"https://github.com/acme/widget/releases/download/v{VERSION}"


def published(
    name: str, *, kind: str = "archive", architecture: str = "", data: bytes = b"payload"
) -> PublishedArtifact:
    return PublishedArtifact(
        name=name,
        url=f"{RELEASE_BASE}/{name}",
        sha256=hashlib.sha256(data).hexdigest(),
        kind=kind,
        architecture=architecture,
    )


def tap_settings(**overrides):
    settings = {
        "token": "widget",
        "destination": {
            "repository": "acme/homebrew-tap",
            "branch": "main",
            "mode": "trusted",
            "credential_secret": "TAP_TOKEN",
        },
        "formula": {
            "desc": "A widget",
            "homepage": "https://github.com/acme/widget",
            "license": "MIT",
            "install_paths": ["widget"],
        },
    }
    settings.update(overrides)
    return parse_settings("homebrew", settings)


def port_settings(**overrides):
    settings = {
        "name": "widget",
        "tree": {
            "repository": "acme/macports-widget",
            "branch": "main",
            "mode": "trusted",
            "credential_secret": "TREE_TOKEN",
        },
        "portfile": {
            "name": "widget",
            "category": "sysutils",
            "license": "MIT",
            "maintainers": "{example.com:user @user} openmaintainer",
            "description": "A widget",
        },
    }
    settings.update(overrides)
    return parse_settings("macports", settings)


def fetcher_for(*artifacts: PublishedArtifact) -> InMemoryAssetFetcher:
    fetcher = InMemoryAssetFetcher()
    for item in artifacts:
        fetcher.add(item.url, b"payload")
    return fetcher


def generated(path: str, content: str, surface: str = "formula") -> GeneratedFile:
    return GeneratedFile(path=path, content=content, surface=surface)


class LedgerTests(unittest.TestCase):
    """Whether this release supplies what the consumer's writers supplied."""

    def test_every_preserved_behaviour_names_an_owner_this_release_supplies(self):
        # The ledger is only useful if it is checked. An owner that no longer
        # resolves is a behaviour that quietly stopped being delivered while the
        # consumer's own copy of it was deleted.
        book = ledger()

        self.assertTrue(book.ok, [item.key for item in book.unresolved])
        self.assertEqual(book.unresolved, ())
        self.assertEqual(sorted(book.keys()), sorted(item.key for item in adoption.PRESERVED))

    def test_every_owned_entry_names_something_that_still_exists(self):
        # Resolution, not callability: an owner may be a function or a
        # constant, and what must hold is that the name is still there.
        for entry in ledger().owned:
            with self.subTest(entry.key):
                self.assertIsNotNone(adoption._resolve(entry.owner), entry.owner)

    def test_the_rust_library_is_named_as_deferred_rather_than_owned(self):
        # PR #67 has not landed, so this entry claims nothing. Recording it as
        # owned would be the one entry in the ledger that reads true and is not.
        book = ledger()

        self.assertEqual([item.key for item in book.deferred], ["rust-static-library"])
        self.assertNotIn(
            "rust-static-library", [item.key for item in book.owned]
        )

    def test_an_owner_that_no_longer_resolves_is_reported_rather_than_hidden(self):
        book = ledger(
            [
                Preserved(
                    key="signing",
                    summary="the stable signing identity",
                    owner="continuum.release.apple:bind_material",
                ),
                Preserved(
                    key="gone",
                    summary="a behaviour whose owner was deleted",
                    owner="continuum.release.apple:removed_by_a_refactor",
                ),
            ]
        )

        self.assertFalse(book.ok)
        self.assertEqual([item.key for item in book.unresolved], ["gone"])

    def test_two_entries_may_not_claim_one_key(self):
        entry = Preserved(key="homebrew", summary="the formula", owner="continuum:thing")

        with self.assertRaises(AdoptionError) as caught:
            ledger([entry, entry])

        self.assertIn("listed twice", str(caught.exception))

    def test_an_entry_needs_exactly_one_of_an_owner_and_a_wait(self):
        with self.assertRaises(AdoptionError) as caught:
            Preserved(key="homebrew", summary="the formula")
        self.assertIn("exactly one", str(caught.exception))

        with self.assertRaises(AdoptionError) as caught:
            Preserved(
                key="homebrew",
                summary="the formula",
                owner="continuum:thing",
                deferred_to="#67",
            )
        self.assertIn("exactly one", str(caught.exception))

    def test_the_ledger_describes_itself_with_both_halves(self):
        payload = ledger().describe()

        self.assertEqual(payload["schema"], adoption.ADOPTION_SCHEMA)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["unresolved"], [])
        self.assertEqual(payload["deferred"], ["rust-static-library"])


class FactsTests(unittest.TestCase):
    """What one release says, reduced to the facts two implementations share."""

    def test_facts_need_the_version_they_describe(self):
        with self.assertRaises(AdoptionError) as caught:
            ReleaseFacts(version="")

        self.assertIn("version", str(caught.exception))

    def test_an_axis_nobody_listed_is_refused(self):
        facts = ReleaseFacts(version=VERSION)

        with self.assertRaises(AdoptionError) as caught:
            facts.axis("artifacts")

        self.assertIn("unknown release axis", str(caught.exception))

    def test_the_facts_are_read_out_of_a_walk_the_core_performed(self):
        outcome = self.release_outcome()

        facts = ReleaseFacts.from_outcome(outcome)

        self.assertEqual(facts.version, VERSION)
        # The fixture builds one artifact and its checksum listing, so two
        # candidates is the whole of what this release carries.
        self.assertEqual(len(facts.candidates), 2)
        self.assertIn(f"fixture-{VERSION}-{support.CLASSIFIER}.txt", facts.candidates)
        self.assertEqual(len(facts.digests), 2)
        for name, digest in facts.digests:
            self.assertIn(name, facts.candidates)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_every_collection_is_stored_sorted_so_order_cannot_disguise_a_difference(self):
        outcome = self.release_outcome()

        facts = ReleaseFacts.from_outcome(outcome)

        self.assertEqual(list(facts.candidates), sorted(facts.candidates))
        self.assertEqual(list(facts.digests), sorted(facts.digests))

    def test_a_plan_names_no_digest_rather_than_reporting_one_it_did_not_compute(self):
        planned = self.core().plan(self.request())
        facts = ReleaseFacts.from_outcome(planned)

        self.assertEqual(facts.version, VERSION)
        self.assertEqual(facts.digests, ())

    def test_a_real_publisher_plan_survives_into_the_facts_unchanged(self):
        # The publisher plan is the only place the generated files come from, so
        # if the facts did not carry it the comparison would be comparing plans
        # against nothing.
        arm = published("widget-1.4.0-x86_64.tar.gz", architecture=ARCH_X86_64)
        intel = published("widget-1.4.0-arm64.tar.gz", architecture=ARCH_ARM64)
        formula = homebrew.plan(
            PublishRequest(manifest=self.manifest(arm, intel), settings=tap_settings()),
            fetcher=fetcher_for(arm, intel),
        )
        # MacPorts pins one source archive, which is the shape a Portfile has.
        source = published("widget-1.4.0.tar.gz")
        port = macports.plan(
            PublishRequest(manifest=self.manifest(source), settings=port_settings()),
            fetcher=fetcher_for(source),
        )
        generated = tuple(formula) + tuple(port)

        facts = ReleaseFacts.from_outcome(self.release_outcome(), generated=generated)

        self.assertEqual(
            facts.publishers,
            tuple(sorted((item.surface, item.path) for item in generated)),
        )
        self.assertEqual(
            facts.templates,
            tuple(
                sorted(
                    (item.path, hashlib.sha256(item.content.encode("utf-8")).hexdigest())
                    for item in generated
                )
            ),
        )
        # Both surfaces the preserved behaviours name are present: the formula
        # Homebrew installs and the Portfile MacPorts builds.
        self.assertEqual(
            [surface for surface, _ in facts.publishers], ["formula", "portfile"]
        )

    def manifest(self, *artifacts: PublishedArtifact) -> ArtifactManifest:
        return ArtifactManifest(
            release=ReleaseIdentity(
                repository="acme/widget", version=VERSION, download_base=RELEASE_BASE
            ),
            artifacts=tuple(artifacts),
        )

    # -- a release to read ----------------------------------------------------

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.repository = support.MemoryReleaseRepository()
        self.adapter = support.FixtureAdapter(workdir=self.directory)
        self.publisher = support.github_publisher(self.repository)
        self._core = ReleaseCore(self.components())

    def components(self) -> ReleaseComponents:
        return support.components(adapter=self.adapter, publishers=(self.publisher,))

    def core(self) -> ReleaseCore:
        return self._core

    def request(self, **overrides) -> ReleaseRequest:
        values = {
            "event": support.event(),
            "targets": support.targets(),
            "workdir": self.directory,
            "requested_version": VERSION,
        }
        values.update(overrides)
        return ReleaseRequest(**values)

    def release_outcome(self):
        return self._core.execute(self.request())


class ComparisonTests(unittest.TestCase):
    """Whether the new release is the same release as the one already out there."""

    def facts(self, **overrides) -> ReleaseFacts:
        values = {
            "version": VERSION,
            "candidates": ("widget-1.4.0-x86_64.tar.gz", "widget-1.4.0-SHA256SUMS.txt"),
            "digests": (
                ("widget-1.4.0-SHA256SUMS.txt", "a" * 64),
                ("widget-1.4.0-x86_64.tar.gz", "b" * 64),
            ),
            "templates": (("widget.rb", "c" * 64),),
            "publishers": (("formula", "widget.rb"),),
        }
        values.update(overrides)
        return ReleaseFacts(**values)

    def test_two_identical_releases_agree_on_every_axis(self):
        result = compare(self.facts(), self.facts())

        self.assertTrue(result.ok)
        self.assertTrue(result.complete)
        self.assertEqual(result.unevaluated, ())
        self.assertEqual(
            [verdict for _, verdict in result.verdicts],
            [adoption.AGREED] * len(adoption.AXES),
        )

    def test_a_changed_version_is_a_divergence(self):
        result = compare(self.facts(), self.facts(version="1.5.0"))

        self.assertFalse(result.ok)
        self.assertEqual(result.verdict(adoption.AXIS_VERSION), adoption.DIVERGED)
        self.assertIn("1.4.0", result.divergences[0].summary)
        self.assertIn("1.5.0", result.divergences[0].summary)

    def test_a_candidate_the_new_release_does_not_carry_is_a_divergence(self):
        result = compare(
            self.facts(),
            self.facts(candidates=("widget-1.4.0-x86_64.tar.gz",)),
        )

        self.assertEqual(result.verdict(adoption.AXIS_CANDIDATES), adoption.DIVERGED)
        self.assertIn("SHA256SUMS", result.divergences[0].summary)

    def test_different_bytes_for_the_same_name_are_a_divergence(self):
        # The name still matches, so this is the difference that only a digest
        # comparison catches, and it is the one that installs a broken binary.
        result = compare(
            self.facts(),
            self.facts(digests=(("widget-1.4.0-SHA256SUMS.txt", "a" * 64),)),
        )

        self.assertEqual(result.verdict(adoption.AXIS_DIGESTS), adoption.DIVERGED)
        self.assertIn("digest", result.divergences[0].summary)

    def test_a_different_publisher_plan_is_a_divergence(self):
        result = compare(
            self.facts(),
            self.facts(publishers=(("formula", "widget.rb"), ("cask", "widget.rb"))),
        )

        self.assertEqual(result.verdict(adoption.AXIS_PUBLISHERS), adoption.DIVERGED)

    def test_an_axis_nobody_measured_is_unevaluated_and_not_agreed(self):
        # A dry run produces no bytes, so it can say nothing about digests. The
        # dangerous reading is "nothing to report", which is why this is its own
        # verdict: agreement has to be earned by both sides having said something.
        planned = ReleaseFacts(version=VERSION, candidates=self.facts().candidates)

        result = compare(self.facts(), planned)

        self.assertEqual(result.verdict(adoption.AXIS_DIGESTS), adoption.UNEVALUATED)
        self.assertEqual(result.verdict(adoption.AXIS_TEMPLATES), adoption.UNEVALUATED)
        # Nothing disagreed, and nothing was checked either. Only `complete`
        # tells those apart, which is why the cutover gate reads that one.
        self.assertTrue(result.ok)
        self.assertFalse(result.complete)
        self.assertEqual(result.divergences, ())

    def test_a_comparison_of_two_empties_is_not_complete(self):
        empty = ReleaseFacts(version=VERSION)

        result = compare(empty, ReleaseFacts(version=VERSION))

        self.assertTrue(result.ok)
        self.assertFalse(result.complete)
        self.assertEqual(
            sorted(result.unevaluated),
            [
                adoption.AXIS_CANDIDATES,
                adoption.AXIS_DIGESTS,
                adoption.AXIS_PUBLISHERS,
                adoption.AXIS_TEMPLATES,
            ],
        )

    def test_the_report_carries_both_sides(self):
        result = compare(self.facts(), self.facts(version="1.5.0"))

        payload = result.describe()
        self.assertEqual(payload["schema"], adoption.ADOPTION_SCHEMA)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["expected"]["version"], VERSION)
        self.assertEqual(payload["actual"]["version"], "1.5.0")
        self.assertEqual(payload["divergences"][0]["axis"], adoption.AXIS_VERSION)


class CanaryTests(unittest.TestCase):
    """Whether a release that already happened can be driven again safely."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.repository = support.MemoryReleaseRepository()
        self.adapter = support.FixtureAdapter(workdir=self.directory)
        self.publisher = support.github_publisher(self.repository)
        self.core = ReleaseCore(
            support.components(adapter=self.adapter, publishers=(self.publisher,))
        )

    def request(self) -> ReleaseRequest:
        return ReleaseRequest(
            event=support.event(),
            targets=support.targets(),
            workdir=self.directory,
            requested_version=VERSION,
        )

    def test_the_first_run_publishes_and_is_not_a_canary(self):
        outcome = self.core.execute(self.request())

        verdict = canary(outcome)
        self.assertTrue(outcome.published)
        self.assertFalse(verdict.ok)
        self.assertIn("published", verdict.summary)

    def test_replaying_the_same_release_publishes_nothing(self):
        first = self.core.execute(self.request())
        before = list(self.repository.calls)

        # A resumed run is given the manifests back. Without them the core
        # refuses rather than rebuilding, because the journal says these bytes
        # were already produced and re-running the build could produce others.
        second = self.core.execute(
            self.request(), journal=first.journal, manifests=first.manifests
        )

        verdict = canary(second)
        self.assertFalse(second.published, second.describe())
        self.assertTrue(verdict.ok, verdict.summary)
        # The destination is not asked to do anything twice. This is the
        # property a cutover on top of a live release depends on.
        self.assertEqual(self.repository.calls, before)

    def test_a_replay_that_cannot_be_given_its_manifests_fails_rather_than_rebuilding(self):
        # The other half of the property above: refusing is what makes the
        # digest comparison meaningful, since a rebuilt artifact is new bytes.
        first = self.core.execute(self.request())

        second = self.core.execute(self.request(), journal=first.journal)

        self.assertTrue(second.failed)
        self.assertFalse(second.published)
        self.assertFalse(canary(second).ok)

    def test_a_replay_says_why_it_published_nothing(self):
        first = self.core.execute(self.request())

        second = self.core.execute(
            self.request(), journal=first.journal, manifests=first.manifests
        )

        verdict = canary(second)
        self.assertTrue(verdict.summary, "a canary has to explain itself")
        self.assertEqual(verdict.release, first.release)
        self.assertEqual(verdict.version, VERSION)

    def test_a_dry_run_is_not_a_canary_and_still_publishes_nothing(self):
        outcome = self.core.plan(self.request())

        verdict = canary(outcome)
        self.assertFalse(outcome.published)
        self.assertTrue(outcome.dry_run)
        # A plan may look at the destination to describe what it would find, so
        # what it must not do is write: no draft, no upload, no promotion.
        self.assertEqual(
            [call for call in self.repository.calls if not call.startswith("find:")], []
        )
        # A plan never claims it published, which is the property that keeps a
        # cutover rehearsal from being mistaken for the real thing.
        self.assertFalse(verdict.published)

    def test_a_failed_run_is_not_a_pass_however_little_it_published(self):
        self.adapter.fail_verify = True

        outcome = self.core.execute(self.request())

        verdict = canary(outcome)
        self.assertTrue(outcome.failed)
        self.assertFalse(verdict.ok)
        self.assertTrue(verdict.failed)


class RollbackTests(unittest.TestCase):
    """The commit to go back to, and the one moment it stops being kept."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.repository = support.MemoryReleaseRepository()
        self.adapter = support.FixtureAdapter(workdir=self.directory)
        self.publisher = support.github_publisher(self.repository)
        self.core = ReleaseCore(
            support.components(adapter=self.adapter, publishers=(self.publisher,))
        )

    def request(self, **overrides) -> ReleaseRequest:
        values = {
            "event": support.event(),
            "targets": support.targets(),
            "workdir": self.directory,
            "requested_version": VERSION,
        }
        values.update(overrides)
        return ReleaseRequest(**values)

    def journal_after_a_real_release(self):
        return self.core.execute(self.request()).journal

    def journal_after_a_plan(self):
        return self.core.plan(self.request()).journal

    def test_a_pin_with_nothing_replacing_it_is_retained(self):
        pin = retention(REPOSITORY, OTHER_SHA)

        self.assertTrue(pin.retained)
        self.assertEqual(pin.sha, OTHER_SHA)

    def test_a_pin_cannot_be_dropped_before_any_production_release(self):
        pin = retention(REPOSITORY, OTHER_SHA, produced=())

        with self.assertRaises(AdoptionError) as caught:
            require_retirable(pin, journal=self.journal_after_a_plan())

        self.assertIn(OTHER_SHA, str(caught.exception))

    def test_a_production_release_only_a_plan_claims_is_not_evidence(self):
        # A rehearsal walks every stage and produces a journal. If a plan could
        # release the pin, a dry run would retire the way back out from under a
        # cutover that has not happened yet.
        pin = retention(REPOSITORY, OTHER_SHA, produced=[VERSION])

        with self.assertRaises(AdoptionError) as caught:
            require_retirable(pin, journal=self.journal_after_a_plan())

        self.assertIn("no real run recorded", str(caught.exception))

    def test_a_pin_named_as_replaced_by_a_version_nobody_ran_is_not_evidence(self):
        pin = retention(REPOSITORY, OTHER_SHA, produced=["9.9.9"])

        with self.assertRaises(AdoptionError) as caught:
            require_retirable(pin, journal=self.journal_after_a_real_release())

        self.assertIn("9.9.9", str(caught.exception))

    def test_a_pin_is_droppable_once_a_real_release_replaced_it(self):
        pin = retention(REPOSITORY, OTHER_SHA, produced=[VERSION])

        require_retirable(pin, journal=self.journal_after_a_real_release())

        self.assertFalse(pin.retained)

    def test_the_first_replacing_release_is_the_one_recorded(self):
        pin = retention(REPOSITORY, OTHER_SHA, produced=[VERSION, "1.5.0"])

        self.assertEqual(pin.superseded_by, VERSION)

    def test_a_released_pin_cannot_be_restored(self):
        pin = retention(REPOSITORY, OTHER_SHA, produced=[VERSION])

        with self.assertRaises(AdoptionError) as caught:
            require_retained(pin)

        self.assertIn(VERSION, str(caught.exception))

    def test_a_retained_pin_is_still_there_to_be_required(self):
        require_retained(retention(REPOSITORY, OTHER_SHA))

    def test_a_pin_needs_a_full_commit(self):
        with self.assertRaises(AdoptionError):
            retention(REPOSITORY, "abc1234")

    def test_an_empty_version_cannot_release_a_pin(self):
        with self.assertRaises(AdoptionError) as caught:
            retention(REPOSITORY, OTHER_SHA, produced=[""])

        self.assertIn("empty version", str(caught.exception))


if __name__ == "__main__":
    unittest.main()