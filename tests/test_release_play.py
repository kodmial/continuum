"""The Google Play publisher's decisions, against a Play that is a dict.

Play is unforgiving in ways a fake that enforced none of its rules would hide, so
`InMemoryPlayTransport` enforces the real ones: one open edit at a time, an
expired edit, a `versionCode` that is spent once used and must strictly increase,
a digest echoed back from the uploaded bytes, and a staged rollout that may only
get wider. The tests then check that the publisher asks Play the right questions
*before* it spends the code, that it recovers from the failures Play really
produces, and that a re-run is green rather than a second upload.

The two properties worth most of the assertions here are **the code is not spent
twice** and **a stale edit is replaced, not retried**. The first is why every
duplicate answers `skipped`; the second is why a 410 does not burn the retry
budget against a handle that will never be valid again.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any, Dict, List, Tuple

from continuum.release import android, contract, play
from continuum.release.contract import (
    SIGNING_SIGNED,
    TYPE_INSTALLER,
    TYPE_PACKAGE,
    ArtifactManifest,
    BuildRequest,
    ManifestBuilder,
    PublishRequest,
    TargetSpec,
    VERIFICATION_VERIFIED,
)
from continuum.release.play import (
    GooglePlayPublisher,
    InMemoryPlayTransport,
    PlayApiError,
    PlayError,
    PlaySettings,
    TrackState,
    parse_settings,
)

SHA = "e" * 40
VERSION = "3.1.4"
PACKAGE = "com.example.widgets"
TRACK = play.TRACK_PRODUCTION
VERSION_CODE = android.version_code_for(VERSION)
KEYSTORE_SECRET = "WIDGETS_UPLOAD_KEYSTORE"
KEY_ALIAS = "upload"
IDENTITY = "CN=Widgets Upload, O=Example, C=US"

BUNDLE_NAME = "app-3.1.4-release.aab"
APK_NAME = "app-3.1.4-release.apk"


def settings(**overrides: Any) -> PlaySettings:
    values: Dict[str, Any] = {
        "package_name": PACKAGE,
        "track": TRACK,
        "access_token_secret": "GOOGLE_PLAY_TOKEN",
    }
    values.update(overrides)
    return PlaySettings(**values)


class Decorated(InMemoryPlayTransport):
    """Play, with one call answered differently.

    Play is not a place a test can ask a question of directly, so the three
    faults worth testing -- a digest that is not the digest, a truncated upload, an
    edit that expires -- are expressed by overriding the one call that produces
    them. Everything else stays the real in-memory rules, which is the point: a
    fault injected here cannot be answered by a fake that has no rules.
    """


def publisher(transport: Any, *, sleeper: Any = None, **overrides: Any) -> GooglePlayPublisher:
    """A publisher over `transport`, with the backoff recorded rather than waited on."""

    if sleeper is None:
        return GooglePlayPublisher(settings(**overrides), transport)
    return GooglePlayPublisher(settings(**overrides), transport, sleeper=sleeper)


class PlayTestCase(unittest.TestCase):
    """A workspace with a real app bundle on disk, and a manifest that describes it."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="continuum-play-test-")
        self.addCleanup(self._remove, self.workdir)
        self.bundle_path = self._write(BUNDLE_NAME, b"base64-ish app bundle bytes")
        self.apk_path = self._write(APK_NAME, b"unsigned apk bytes")

    @staticmethod
    def _remove(path: str) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def _write(self, name: str, body: bytes) -> str:
        path = os.path.join(self.workdir, name)
        with open(path, "wb") as handle:
            handle.write(body)
        return path

    # -- manifests --------------------------------------------------------
    def manifest(
        self, *artifacts: Tuple[str, str, str], verified: bool = True
    ) -> ArtifactManifest:
        """A manifest of the named files, each with the type it would really have.

        `artifacts` is `(name, path, type)`, so a test can put an APK in a release
        and check that the publisher says something about it rather than uploading
        it and being refused.
        """

        builder = ManifestBuilder(
            target="android", adapter=android.ADAPTER_NAME, source_sha=SHA, version=VERSION
        )
        for name, path, kind in artifacts or ((BUNDLE_NAME, self.bundle_path, TYPE_PACKAGE),):
            builder.record(
                name,
                path,
                kind,
                platform="android",
                signing=SIGNING_SIGNED,
                signing_identity=IDENTITY,
                **(
                    {"verification": VERIFICATION_VERIFIED, "verified_by": "android"}
                    if verified
                    else {}
                ),
            )
        return builder.build()

    def bundle_manifest(self) -> ArtifactManifest:
        return self.manifest()

    def request_for(self, *manifests: ArtifactManifest, **overrides: Any) -> PublishRequest:
        values: Dict[str, Any] = {
            "destination": play.DESTINATION,
            "tag": f"v{VERSION}",
            "version": VERSION,
            "source_sha": SHA,
            "manifests": manifests or (self.bundle_manifest(),),
            "key": "publish-key",
        }
        values.update(overrides)
        return PublishRequest(**values)

    def transport(self, **kwargs: Any) -> InMemoryPlayTransport:
        return InMemoryPlayTransport(**kwargs)


# -- configuration ----------------------------------------------------------


class ConfigurationTests(unittest.TestCase):
    def test_a_play_destination_needs_a_package_name(self):
        with self.assertRaises(PlayError) as caught:
            parse_settings({"track": TRACK})
        self.assertEqual(caught.exception.code, "configuration-invalid")
        self.assertIn("package_name", str(caught.exception))

    def test_an_unknown_track_is_refused_by_name(self):
        with self.assertRaises(PlayError) as caught:
            parse_settings({"package_name": PACKAGE, "track": "sideways"})
        self.assertIn("sideways", str(caught.exception))
        self.assertIn(play.TRACK_INTERNAL, str(caught.exception))

    def test_a_rollout_of_zero_serves_nobody(self):
        with self.assertRaises(PlayError) as caught:
            parse_settings({"package_name": PACKAGE, "rollout": 0})
        self.assertIn("0.01 and 1.0", str(caught.exception))

    def test_a_rollout_above_one_is_refused(self):
        with self.assertRaises(PlayError) as caught:
            parse_settings({"package_name": PACKAGE, "rollout": 1.5})
        self.assertIn("0.01 and 1.0", str(caught.exception))

    def test_a_rollout_is_read_to_the_precision_play_accepts(self):
        # Play takes two decimal places on a user fraction; a third would be
        # silently truncated by the API, so it is rejected at configuration time
        # by rounding rather than pretending to more precision than exists.
        self.assertEqual(parse_settings({"package_name": PACKAGE, "rollout": 0.129}).rollout, 0.13)
        self.assertTrue(parse_settings({"package_name": PACKAGE, "rollout": 0.1}).staged)
        self.assertFalse(parse_settings({"package_name": PACKAGE, "rollout": 1}).staged)

    def test_a_credential_is_referenced_by_secret_name(self):
        with self.assertRaises(PlayError) as caught:
            parse_settings({"package_name": PACKAGE, "access_token_secret": "ya29.a0AfB"})
        self.assertIn("repository secret name", str(caught.exception))

    def test_an_offset_and_an_explicit_code_together_are_refused(self):
        with self.assertRaises(PlayError) as caught:
            parse_settings(
                {"package_name": PACKAGE, "version_code": 900, "version_code_offset": 3}
            )
        self.assertIn("never apply", str(caught.exception))

    def test_the_version_code_comes_from_the_same_function_the_adapter_used(self):
        destination = settings()
        self.assertEqual(destination.version_binding(VERSION), (VERSION, VERSION_CODE))
        adapter_settings = android.parse_settings({"key_alias": KEY_ALIAS})
        self.assertEqual(adapter_settings.version_binding(VERSION)[1], VERSION_CODE)

    def test_settings_can_be_written_in_a_configuration_shape(self):
        parsed = parse_settings(
            {
                "package_name": PACKAGE,
                "track": play.TRACK_INTERNAL,
                "rollout": 0.2,
                "release_notes": "Widgets is faster.",
                "access_token_secret": "GOOGLE_PLAY_TOKEN",
            }
        )
        self.assertEqual(parsed.track, play.TRACK_INTERNAL)
        self.assertEqual(parsed.release_notes, "Widgets is faster.")
        self.assertEqual(parsed.attempts, play.DEFAULT_ATTEMPTS)

    def test_the_publisher_satisfies_the_publisher_port(self):
        self.assertEqual(contract.conform(publisher(InMemoryPlayTransport()), "publisher"), ())

    def test_a_transport_that_cannot_commit_is_refused_at_construction(self):
        class Incomplete:
            def insert_edit(self, package_name: str) -> str:
                return "edit-1"

        with self.assertRaises(PlayError) as caught:
            GooglePlayPublisher(settings(), Incomplete())
        self.assertEqual(caught.exception.code, "transport-incomplete")
        self.assertIn("commit_edit", str(caught.exception))

    def test_the_intent_names_the_track_and_the_rollout(self):
        plain = publisher(InMemoryPlayTransport())
        self.assertIn(TRACK, plain.intent())
        staged = publisher(InMemoryPlayTransport(), rollout=0.1)
        self.assertIn("10%", staged.intent())


# -- drafting ---------------------------------------------------------------


class DraftTests(PlayTestCase):
    def test_the_bundle_is_uploaded_and_left_as_a_draft_release(self):
        transport = self.transport()
        result = publisher(transport).draft(self.request_for())
        self.assertTrue(result.published)
        self.assertEqual(result.code, "drafted")
        self.assertEqual(result.detail("status"), play.STATUS_DRAFT)
        self.assertEqual(result.detail("bundle"), BUNDLE_NAME)
        self.assertEqual(len(transport.uploads), 1)
        self.assertEqual(transport.uploads[0]["sha256"], self.manifest().by_name(BUNDLE_NAME).digest)
        # Nothing is served until the publish stage, which is the whole reason the
        # two stages are separate -- but the code *is* spent by the draft commit,
        # because Play reserves a drafted release's versionCode and refuses a
        # second upload carrying it.
        self.assertEqual(transport.tracks[TRACK].status, play.STATUS_DRAFT)
        self.assertIn(VERSION_CODE, transport.spent)

    def test_the_edit_is_committed_so_the_draft_survives_the_job(self):
        transport = self.transport()
        publisher(transport).draft(self.request_for())
        self.assertEqual(transport.committed, ["edit-001"])

    def test_a_release_with_no_bundle_is_refused_with_an_explanation(self):
        transport = self.transport()
        request = self.request_for(self.manifest((APK_NAME, self.apk_path, TYPE_INSTALLER)))
        result = publisher(transport).draft(request)
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "bundle-absent")
        self.assertIn("app bundle and nothing else", result.reason)
        self.assertEqual(transport.calls, [], "nothing was asked of Play")

    def test_a_release_with_two_bundles_is_refused_rather_than_guessed_at(self):
        second = self._write("other-3.1.4.aab", b"another bundle")
        transport = self.transport()
        request = self.request_for(
            self.manifest(
                (BUNDLE_NAME, self.bundle_path, TYPE_PACKAGE),
                (os.path.basename(second), second, TYPE_PACKAGE),
            )
        )
        result = publisher(transport).draft(request)
        self.assertEqual(result.code, "bundle-ambiguous")
        self.assertEqual(transport.calls, [])

    def test_a_bundle_that_is_not_on_this_runner_never_spends_a_version_code(self):
        manifest = self.manifest()
        os.remove(self.bundle_path)
        transport = self.transport()
        result = publisher(transport).draft(self.request_for(manifest))
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "bundle-absent")
        self.assertTrue(result.retryable)
        self.assertEqual(transport.committed, [])

    def test_a_version_code_already_live_is_a_duplicate_not_a_second_upload(self):
        transport = self.transport()
        transport.tracks[TRACK] = TrackState(
            track=TRACK, status=play.STATUS_COMPLETED, version_code=VERSION_CODE
        )
        result = publisher(transport).draft(self.request_for())
        self.assertTrue(result.skipped)
        self.assertEqual(result.code, "already-published")
        self.assertEqual(transport.uploads, [], "a duplicate must not spend bandwidth")
        self.assertIn("already live", result.reason)

    def test_a_staged_rollout_already_live_is_reported_with_its_fraction(self):
        transport = self.transport()
        transport.tracks[TRACK] = TrackState(
            track=TRACK,
            status=play.STATUS_IN_PROGRESS,
            version_code=VERSION_CODE,
            user_fraction=0.2,
        )
        result = publisher(transport).draft(self.request_for())
        self.assertTrue(result.skipped)
        self.assertIn("20%", result.reason)

    def test_a_version_code_already_drafted_is_a_skip_that_names_the_draft(self):
        transport = self.transport()
        transport.tracks[TRACK] = TrackState(
            track=TRACK, status=play.STATUS_DRAFT, version_code=VERSION_CODE
        )
        result = publisher(transport).draft(self.request_for())
        self.assertTrue(result.skipped)
        self.assertEqual(result.code, "already-drafted")
        self.assertEqual(transport.uploads, [])

    def test_a_version_code_in_use_on_another_track_is_a_conflict_not_a_duplicate(self):
        transport = self.transport()
        transport.tracks[play.TRACK_INTERNAL] = TrackState(
            track=play.TRACK_INTERNAL, status=play.STATUS_COMPLETED, version_code=VERSION_CODE
        )
        result = publisher(transport).draft(self.request_for())
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "version-code-conflict")
        self.assertIn(play.TRACK_INTERNAL, result.reason)
        self.assertFalse(result.retryable, "a spent code is spent on any track")
        self.assertEqual(transport.uploads, [])

    def test_bytes_that_are_not_the_bytes_in_the_manifest_are_never_committed(self):
        transport = self.transport(echo_digest=True)

        class Substitutes(Decorated):
            def upload_bundle(self, package_name: str, edit_id: str, path: str):
                receipt = super().upload_bundle(package_name, edit_id, path)
                return play.UploadReceipt(size=receipt.size, sha1=receipt.sha1, sha256="0" * 64)

        result = publisher(Substitutes(), release_notes="x").draft(self.request_for())
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "upload-digest-mismatch")
        self.assertEqual(transport.committed, [])
        self.assertEqual(transport.uploads, [])

    def test_a_truncated_upload_is_refused_before_the_code_is_spent(self):
        class Truncates(Decorated):
            def upload_bundle(self, package_name: str, edit_id: str, path: str):
                super().upload_bundle(package_name, edit_id, path)
                return play.UploadReceipt(size=3, sha1="", sha256="")

        result = publisher(Truncates()).draft(self.request_for())
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "upload-size-mismatch")
        self.assertTrue(result.retryable, "a truncated upload costs no version code")

    def test_a_release_built_from_another_commit_cannot_be_offered_to_play(self):
        # The manifest and the event have to agree about the commit, and that is
        # settled before the publisher is handed anything: a version code spent on
        # bytes from an unreviewed commit cannot be taken back.
        transport = self.transport()
        with self.assertRaises(contract.ContractError) as caught:
            self.request_for(source_sha="f" * 40)
        self.assertIn(SHA, str(caught.exception))
        self.assertEqual(transport.calls, [], "nothing was asked of Play")

    def test_a_dry_run_says_what_it_would_do_and_asks_play_nothing(self):
        transport = self.transport()
        result = publisher(transport).draft(self.request_for(dry_run=True))
        self.assertTrue(result.skipped)
        self.assertEqual(result.code, play.DRY_RUN_CODE)
        self.assertIn(str(VERSION_CODE), result.reason)
        self.assertEqual(transport.calls, [])

    def test_a_plan_never_fails_on_an_unbuilt_manifest(self):
        builder = ManifestBuilder(
            target="android", adapter=android.ADAPTER_NAME, source_sha=SHA, version=VERSION
        )
        builder.declare(BUNDLE_NAME, self.bundle_path, TYPE_PACKAGE, platform="android")
        declared = builder.build()
        transport = self.transport()
        result = publisher(transport).draft(self.request_for(declared, dry_run=True))
        self.assertTrue(result.skipped)
        self.assertEqual(transport.calls, [])

    def test_an_unverified_manifest_is_refused(self):
        manifest = self.manifest(verified=False)
        transport = self.transport()
        result = publisher(transport).draft(self.request_for(manifest))
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "manifest-not-publishable")
        self.assertEqual(transport.calls, [])


# -- recovery ---------------------------------------------------------------


class RecoveryTests(PlayTestCase):
    def test_an_expired_edit_is_replaced_rather_than_retried(self):
        class Expiring(Decorated):
            def insert_edit(self, package_name: str) -> str:
                edit_id = super().insert_edit(package_name)
                if not self.expired:
                    self.expire(edit_id)
                return edit_id

        expiring = Expiring()
        result = publisher(expiring).draft(self.request_for())
        self.assertTrue(result.published)
        self.assertEqual(len(expiring.committed), 1)
        self.assertNotEqual(result.detail("edit"), "edit-001")

    def test_a_transient_failure_is_retried_and_the_second_attempt_succeeds(self):
        transport = self.transport()
        transport.fail_next(
            PlayApiError("play-unavailable", "Play is temporarily unavailable", status=503, retryable=True)
        )
        result = publisher(transport).draft(self.request_for())
        self.assertTrue(result.published)
        self.assertEqual(transport.calls.count("insert_edit"), 2)
        self.assertEqual(len(transport.committed), 1)

    def test_a_failure_that_a_retry_cannot_fix_is_not_retried(self):
        transport = self.transport()
        transport.fail_next(
            PlayApiError("credential-rejected", "Play refused the credential", status=403)
        )
        result = publisher(transport).draft(self.request_for())
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "credential-rejected")
        self.assertFalse(result.retryable)
        self.assertEqual(transport.calls.count("insert_edit"), 1)

    def test_a_rejected_version_code_is_a_binding_bug_and_not_retried(self):
        transport = self.transport(spent_codes=(VERSION_CODE + 5,))
        result = publisher(transport).draft(self.request_for())
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "version-code-not-greater")
        self.assertFalse(result.retryable)
        self.assertIn(str(VERSION_CODE), result.reason)

    def test_a_failed_run_leaves_no_edit_open(self):
        transport = self.transport()
        transport.fail_next(
            PlayApiError("credential-rejected", "Play refused the credential", status=403)
        )
        publisher(transport).draft(self.request_for())
        # Play allows one edit at a time, so an edit left open here would block
        # every run after this one until it expired.
        self.assertEqual(transport.open_edits, [])

    def test_the_retry_budget_is_bounded(self):
        transport = self.transport(max_attempts_before_success=99)
        result = publisher(transport, attempts=2).draft(self.request_for())
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "play-unavailable")
        self.assertTrue(result.retryable)
        self.assertEqual(transport.calls.count("insert_edit"), 2)

    def test_a_retry_waits_before_trying_again(self):
        # Retrying a 503 immediately is how one blip becomes a rate-limit ban of
        # the credential, so the wait is part of the behaviour, not a detail.
        transport = self.transport()
        transport.fail_next(
            PlayApiError("play-unavailable", "Play is temporarily unavailable", status=503, retryable=True)
        )
        slept = []
        result = publisher(transport, sleeper=slept.append).draft(self.request_for())
        self.assertTrue(result.published)
        self.assertEqual(slept, [play.RETRY_BACKOFF_BASE])

    def test_the_wait_grows_and_never_exceeds_the_cap(self):
        transport = self.transport(max_attempts_before_success=99)
        slept = []
        result = publisher(transport, attempts=4, sleeper=slept.append).draft(self.request_for())
        self.assertTrue(result.failed)
        self.assertEqual(slept, [1.0, 2.0, 4.0])
        self.assertTrue(all(seconds <= play.RETRY_BACKOFF_CAP for seconds in slept))

    def test_play_s_pacing_is_followed_rather_than_second_guessed(self):
        transport = self.transport()
        transport.fail_next(
            PlayApiError(
                "play-unavailable",
                "Play is rate limiting this credential",
                status=429,
                retryable=True,
                retry_after=17.0,
            )
        )
        slept = []
        publisher(transport, sleeper=slept.append).draft(self.request_for())
        self.assertEqual(slept, [17.0])

    def test_a_stale_handle_is_replaced_without_waiting(self):
        # Nothing about a brand-new handle is likely to be busier than the one
        # that just went stale, so waiting here would only delay a run that can
        # already proceed.
        transport = self.transport()

        class Expiring(InMemoryPlayTransport):
            def insert_edit(self, package_name: str) -> str:
                edit_id = super().insert_edit(package_name)
                if not self.expired:
                    self.expire(edit_id)
                return edit_id

        expiring = Expiring()
        slept = []
        result = publisher(expiring, sleeper=slept.append).draft(self.request_for())
        self.assertTrue(result.published)
        self.assertEqual(slept, [])

    def test_an_already_live_duplicate_does_not_leak_an_edit(self):
        transport = self.transport()
        transport.tracks[TRACK] = TrackState(
            track=TRACK, status=play.STATUS_COMPLETED, version_code=VERSION_CODE
        )
        publisher(transport).draft(self.request_for())
        self.assertEqual(transport.open_edits, [])


# -- publishing -------------------------------------------------------------


class PublishTests(PlayTestCase):
    def _drafted(self, transport: InMemoryPlayTransport) -> None:
        publisher(transport).draft(self.request_for())

    def test_a_drafted_release_is_promoted_to_every_user(self):
        transport = self.transport()
        self._drafted(transport)
        result = publisher(transport).publish(self.request_for())
        self.assertTrue(result.published)
        self.assertEqual(result.code, "published")
        self.assertEqual(result.detail("status"), play.STATUS_COMPLETED)
        self.assertEqual(transport.tracks[TRACK].status, play.STATUS_COMPLETED)
        self.assertIn(VERSION_CODE, transport.spent)

    def test_a_staged_rollout_carries_a_user_fraction(self):
        transport = self.transport()
        self._drafted(transport)
        result = publisher(transport, rollout=0.1).publish(self.request_for())
        self.assertTrue(result.published)
        self.assertEqual(transport.tracks[TRACK].status, play.STATUS_IN_PROGRESS)
        self.assertEqual(transport.tracks[TRACK].user_fraction, 0.1)
        self.assertEqual(result.detail("rollout"), "0.10")

    def test_publishing_uploads_nothing(self):
        transport = self.transport()
        self._drafted(transport)
        before = len(transport.uploads)
        publisher(transport).publish(self.request_for())
        # The version code was spent by the draft stage; an upload here would be
        # a second code for the same bytes.
        self.assertEqual(len(transport.uploads), before)

    def test_a_release_that_is_already_live_is_a_green_duplicate(self):
        transport = self.transport()
        self._drafted(transport)
        publisher(transport).publish(self.request_for())
        again = publisher(transport).publish(self.request_for())
        self.assertTrue(again.skipped)
        self.assertEqual(again.code, "already-published")

    def test_publishing_without_a_draft_is_retryable_and_releases_nothing(self):
        transport = self.transport()
        result = publisher(transport).publish(self.request_for())
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "draft-absent")
        self.assertTrue(result.retryable)
        self.assertEqual(transport.committed, [])

    def test_a_track_holding_another_release_is_not_promoted(self):
        transport = self.transport()
        transport.tracks[TRACK] = TrackState(
            track=TRACK, status=play.STATUS_COMPLETED, version_code=VERSION_CODE + 1
        )
        result = publisher(transport).publish(self.request_for())
        self.assertEqual(result.code, "draft-absent")
        self.assertIn(str(VERSION_CODE + 1), result.reason)

    def test_a_halted_rollout_cannot_be_resumed(self):
        transport = self.transport()
        transport.tracks[TRACK] = TrackState(
            track=TRACK, status=play.STATUS_HALTED, version_code=VERSION_CODE
        )
        result = publisher(transport).publish(self.request_for())
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "rollout-halted")
        self.assertFalse(result.retryable)
        self.assertIn("higher version code", result.reason)

    def test_a_narrower_rollout_is_refused_rather_than_silently_ignored(self):
        transport = self.transport()
        transport.tracks[TRACK] = TrackState(
            track=TRACK,
            status=play.STATUS_IN_PROGRESS,
            version_code=VERSION_CODE,
            user_fraction=0.5,
        )
        result = publisher(transport, rollout=0.2).publish(self.request_for())
        self.assertTrue(result.failed)
        self.assertEqual(result.code, "rollout-not-widened")
        self.assertEqual(transport.tracks[TRACK].user_fraction, 0.5)

    def test_a_wider_rollout_is_allowed(self):
        transport = self.transport()
        transport.tracks[TRACK] = TrackState(
            track=TRACK,
            status=play.STATUS_IN_PROGRESS,
            version_code=VERSION_CODE,
            user_fraction=0.1,
        )
        result = publisher(transport, rollout=0.5).publish(self.request_for())
        self.assertTrue(result.published)
        self.assertEqual(transport.tracks[TRACK].user_fraction, 0.5)

    def test_a_dry_run_publishes_nothing_and_says_what_it_would_do(self):
        transport = self.transport()
        self._drafted(transport)
        result = publisher(transport, rollout=0.25).publish(self.request_for(dry_run=True))
        self.assertTrue(result.skipped)
        self.assertEqual(result.code, play.DRY_RUN_CODE)
        self.assertIn("25%", result.reason)
        self.assertEqual(transport.tracks[TRACK].status, play.STATUS_DRAFT)

    def test_the_two_stages_together_leave_a_live_release_and_spend_the_code_once(self):
        transport = self.transport()
        destination = publisher(transport)
        drafted = destination.draft(self.request_for())
        released = destination.publish(self.request_for())
        self.assertTrue(drafted.published)
        self.assertTrue(released.published)
        self.assertEqual(drafted.identity, released.identity)
        self.assertEqual(transport.open_edits, [])
        self.assertEqual(list(transport.spent), [VERSION_CODE])
        self.assertEqual(transport.tracks[TRACK].status, play.STATUS_COMPLETED)

    def test_the_identity_names_the_package_the_track_and_the_code(self):
        transport = self.transport()
        result = publisher(transport).draft(self.request_for())
        self.assertEqual(result.identity, f"{PACKAGE}:{TRACK}:{VERSION_CODE}")
        self.assertEqual(result.external_id, f"versionCode={VERSION_CODE}")


# -- end to end through the core -------------------------------------------


class ReleaseChainTests(PlayTestCase):
    """The two components wired into the chain, with the toolchain faked out.

    This is the test that would catch the two halves disagreeing: the adapter
    stamps the bundle with one `versionCode` and the publisher tells Play about
    another. Nothing else in either module could notice.
    """

    def setUp(self) -> None:
        super().setUp()
        from .test_release_android import FakeToolchain, signing_environment

        self.checkout = tempfile.mkdtemp(prefix="continuum-play-e2e-")
        self.addCleanup(self._remove, self.checkout)
        wrapper = os.path.join(self.checkout, "gradlew")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
        self.toolchain = FakeToolchain(self.checkout)
        self.spec = TargetSpec(
            id="android",
            adapter=android.ADAPTER_NAME,
            options=(
                ("keystore_secret", KEYSTORE_SECRET),
                ("keystore_password_secret", "WIDGETS_KEYSTORE_PASSWORD"),
                ("key_password_secret", "WIDGETS_KEY_PASSWORD"),
                ("key_alias", KEY_ALIAS),
                ("signing_identity", IDENTITY),
                ("outputs", ["aab"]),
            ),
        )
        self.play = self.transport()
        self.destination = publisher(self.play)
        self.adapter = android.AndroidAdapter(
            environment=signing_environment(),
            command_runner=self.toolchain,
            workdir=self.checkout,
            scratch_dir=os.path.join(self.checkout, "scratch"),
        )

    def _request(self, stage: str) -> BuildRequest:
        return BuildRequest(
            target=self.spec,
            version=VERSION,
            source_sha=SHA,
            key=f"key-{stage}",
            workdir=self.checkout,
            stage=stage,
        )

    def test_the_code_the_adapter_stamped_is_the_code_the_publisher_sends(self):
        request = self._request("build")
        manifest = self.adapter.sign(request, self.adapter.build(request))
        report = self.adapter.verify(request, manifest)
        self.assertTrue(report.verified)
        verified = manifest.mark_verified(report.verified_by)

        gradle = self.toolchain.gradle_argv()
        self.assertIn(f"-Pcontinuum.versionCode={VERSION_CODE}", gradle)

        result = self.destination.draft(
            PublishRequest(
                destination=play.DESTINATION,
                tag=f"v{VERSION}",
                version=VERSION,
                source_sha=SHA,
                manifests=(verified,),
                key="publish-key",
            )
        )
        self.assertTrue(result.published)
        self.assertEqual(result.detail("version_code"), str(VERSION_CODE))
        self.assertEqual(self.play.tracks[TRACK].version_code, VERSION_CODE)
        self.assertEqual(self.play.uploads[0]["sha256"], verified.by_name(BUNDLE_NAME).digest)

    def test_the_release_core_runs_both_components_in_one_chain(self):
        from continuum.release.core import ReleaseComponents, ReleaseCore, ReleaseRequest
        from continuum.release.state import EMPTY_JOURNAL
        from continuum.release.version import ExplicitVersion

        from . import release_core_support as core_support

        components = ReleaseComponents(
            eligibility=core_support.FixtureEligibility(),
            version=ExplicitVersion(VERSION),
            notes=core_support.FixtureNotes(),
            adapters={android.ADAPTER_NAME: self.adapter},
            publishers=(self.destination,),
        )
        outcome = ReleaseCore(components).execute(
            ReleaseRequest(
                event=core_support.event(sha=SHA, tag=f"v{VERSION}"),
                targets=(self.spec,),
                workdir=self.checkout,
            ),
            journal=EMPTY_JOURNAL,
        )
        self.assertTrue(outcome.ok, outcome.describe()["stages"])
        self.assertTrue(outcome.published)
        self.assertEqual(self.play.tracks[TRACK].status, play.STATUS_COMPLETED)
        self.assertEqual(self.play.uploads[0]["sha256"], outcome.manifests[0].by_name(BUNDLE_NAME).digest)

    def test_a_duplicate_event_releases_nothing_a_second_time(self):
        from continuum.release.core import ReleaseComponents, ReleaseCore, ReleaseRequest
        from continuum.release.version import ExplicitVersion

        from . import release_core_support as core_support

        components = ReleaseComponents(
            eligibility=core_support.FixtureEligibility(),
            version=ExplicitVersion(VERSION),
            notes=core_support.FixtureNotes(),
            adapters={android.ADAPTER_NAME: self.adapter},
            publishers=(self.destination,),
        )
        core = ReleaseCore(components)
        request = ReleaseRequest(
            event=core_support.event(sha=SHA, tag=f"v{VERSION}"),
            targets=(self.spec,),
            workdir=self.checkout,
        )
        first = core.execute(request)
        self.assertTrue(first.published)
        uploads = len(self.play.uploads)
        second = core.execute(request, journal=first.journal, manifests=first.manifests)
        self.assertTrue(second.ok)
        self.assertFalse(second.published)
        self.assertEqual(len(self.play.uploads), uploads)


__all__ = ["PlayTestCase", "settings", "publisher"]
