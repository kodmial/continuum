"""The Apple adapter's decisions, and the plan they produce.

These tests are the review surface for a release signature. They run on a Linux
runner and assert on the *plan*: which identity is used, which argument vectors
are emitted, in what order, what is asserted, and what is removed afterwards.
The two properties that are expensive to get wrong and cheap to check here are
the pin — what was signed must be provably the configured identity — and the
teardown, which must happen even when the signing failed.
"""

from __future__ import annotations

import json
import os
import unittest

from continuum import config as config_module
from continuum.release import adapters, apple
from continuum.release import plan as plan_module
from continuum.release import run as run_module
from continuum.release.plan import ReleasePlan, SigningUnavailable, TargetNotExecutable

from . import release_support as support


def plan_for(target, environment=None) -> ReleasePlan:
    return adapters.plan_for(target, environment=environment if environment is not None else support.environment())


def names(plan: ReleasePlan):
    return plan.step_names()


def argv_for(plan: ReleasePlan, name: str):
    return list(plan.step(name).argv)


class SigningProfileTests(unittest.TestCase):
    def test_stable_profile_signs_and_pins_the_configured_identity(self):
        plan = plan_for(support.target())
        self.assertFalse(plan.degraded)
        self.assertTrue(plan.signature_pinned)
        self.assertEqual(plan.signing_mode, config_module.SIGNING_SELF_SIGNED_STABLE)
        self.assertEqual(plan.pinned_identity, support.IDENTITY)
        self.assertEqual(plan.signing_identity, support.IDENTITY)

    def test_the_same_identity_is_used_for_every_binary_and_for_the_bundle(self):
        plan = plan_for(support.target())
        signed = [
            step
            for step in plan.steps
            if step.name.startswith("sign-")
        ]
        self.assertEqual(len(signed), 3, "two binaries and the bundle")
        for step in signed:
            argv = list(step.argv)
            self.assertEqual(argv[argv.index("--sign") + 1], support.IDENTITY)

    def test_binaries_are_signed_before_the_bundle_is_sealed(self):
        order = names(plan_for(support.target()))
        self.assertLess(order.index("sign-com.nanodictate.agent"), order.index("sign-bundle"))
        self.assertLess(order.index("sign-com.nanodictate.ctl"), order.index("sign-bundle"))
        # The bundle is signed over code that is already signed, never instead
        # of it: --deep here would re-seal the nested binaries.
        self.assertNotIn("--deep", argv_for(plan_for(support.target()), "sign-bundle"))

    def test_the_bundle_is_sealed_with_the_agent_identity_and_entitlements(self):
        plan = plan_for(support.target())
        argv = argv_for(plan, "sign-bundle")
        self.assertEqual(argv[argv.index("--identifier") + 1], support.AGENT)
        self.assertEqual(
            argv[argv.index("--entitlements") + 1],
            "Resources/com.nanodictate.agent.entitlements",
        )

    def test_hardened_runtime_is_applied_to_every_signature(self):
        plan = plan_for(support.target())
        for step in plan.steps:
            if not step.name.startswith("sign-"):
                continue
            argv = list(step.argv)
            self.assertEqual(argv[argv.index("--options") + 1], "runtime")

    def test_hardened_runtime_can_be_declared_off_and_is_then_absent(self):
        plan = plan_for(support.target(hardened_runtime=False))
        self.assertFalse(plan.hardened_runtime)
        for step in plan.steps:
            if step.name.startswith("sign-"):
                self.assertNotIn("--options", list(step.argv))

    def test_no_secure_timestamp_by_default(self):
        plan = plan_for(support.target())
        self.assertFalse(plan.timestamp)
        for step in plan.steps:
            self.assertNotIn("--timestamp", list(step.argv))

    def test_a_timestamp_appears_only_when_explicitly_configured(self):
        plan = plan_for(support.target(signing=support.signing(timestamp=True)))
        self.assertTrue(plan.timestamp)
        signed = [step for step in plan.steps if step.name.startswith("sign-")]
        self.assertTrue(signed)
        for step in signed:
            self.assertIn("--timestamp", list(step.argv))

    def test_the_certificate_is_imported_into_a_temporary_keychain(self):
        plan = plan_for(support.target())
        order = names(plan)
        self.assertLess(order.index("import-p12"), order.index("sign-com.nanodictate.agent"))
        import_argv = argv_for(plan, "import-p12")
        self.assertEqual(import_argv[:2], [apple.SECURITY, "import"])
        self.assertEqual(import_argv[import_argv.index("-k") + 1], apple.keychain_path())
        # The password is a substitution slot, never a value in the plan.
        self.assertEqual(import_argv[import_argv.index("-P") + 1], f"${{secret:{apple.P12_PASSWORD_ENV}}}")
        self.assertIn(apple.P12_PASSWORD_ENV, plan.step("import-p12").uses_secrets)

    def test_the_plan_carries_secret_names_and_never_secret_values(self):
        plan = plan_for(support.target())
        rendered = repr(plan.describe())
        self.assertIn(support.P12_SECRET, rendered)
        self.assertIn(support.PASSWORD_SECRET, rendered)
        self.assertNotIn(support.FAKE_P12, rendered)
        self.assertNotIn(support.FAKE_PASSWORD, rendered)

    def test_the_stored_certificate_is_written_by_a_materialize_step(self):
        step = plan_for(support.target()).step("materialize-p12")
        self.assertEqual(step.kind, plan_module.STEP_MATERIALIZE)
        self.assertEqual(step.encoding, plan_module.ENCODING_BASE64)
        self.assertEqual(step.source_env, apple.P12_ENV)
        self.assertIn(apple.p12_path(), plan_for(support.target()).scratch_files())


class PinAndVerificationTests(unittest.TestCase):
    def test_every_signed_artifact_is_pinned_and_verified(self):
        plan = plan_for(support.target())
        expected = [
            "pin-com.nanodictate.agent",
            "pin-com.nanodictate.ctl",
            "pin-app-bundle",
            "verify-com.nanodictate.agent",
            "verify-com.nanodictate.ctl",
            "verify-app-bundle",
        ]
        self.assertEqual([step.name for step in plan.assert_steps()], expected[:3])
        for name in expected:
            self.assertIn(name, names(plan))

    def test_the_pin_names_the_expected_authority(self):
        step = plan_for(support.target()).step("pin-com.nanodictate.agent")
        self.assertEqual(step.kind, plan_module.STEP_ASSERT)
        self.assertEqual(step.expect, f"Authority={support.IDENTITY}")
        self.assertEqual(list(step.argv)[:2], [apple.CODESIGN, "-d"])

    def test_the_bundle_is_verified_deep_and_the_binaries_are_not(self):
        plan = plan_for(support.target())
        self.assertIn("--deep", argv_for(plan, "verify-app-bundle"))
        self.assertNotIn("--deep", argv_for(plan, "verify-com.nanodictate.agent"))

    def test_pinning_happens_after_signing_and_before_teardown(self):
        order = names(plan_for(support.target()))
        self.assertLess(order.index("sign-bundle"), order.index("pin-app-bundle"))
        self.assertLess(order.index("verify-app-bundle"), order.index("delete-keychain"))

    def test_a_plan_cannot_claim_a_pin_it_does_not_name(self):
        with self.assertRaises(plan_module.ReleaseError):
            ReleasePlan(
                target="macos",
                adapter="apple",
                platform="macos",
                build_strategy="swiftpm",
                distribution="direct",
                step_list=(plan_module.PlanStep(name="a", argv=["true"]),),
                signing_mode="self-signed-stable",
                signature_pinned=True,
            )


class TeardownTests(unittest.TestCase):
    def test_a_stable_plan_always_removes_its_keychain(self):
        plan = plan_for(support.target())
        teardown = [step.name for step in plan.teardown]
        self.assertEqual(teardown, ["delete-keychain"])
        self.assertEqual(argv_for(plan, "delete-keychain")[-1], apple.keychain_path())

    def test_a_plan_without_teardown_is_refused(self):
        with self.assertRaises(plan_module.ReleaseError):
            ReleasePlan(
                target="macos",
                adapter="apple",
                platform="macos",
                build_strategy="swiftpm",
                distribution="direct",
                step_list=(plan_module.PlanStep(name="a", argv=["true"]),),
                signing_mode="self-signed-stable",
                pinned_identity="CI",
            )

    def test_a_degraded_plan_cannot_delete_a_keychain_it_never_created(self):
        plan = plan_for(
            support.target(signing=config_module.ReleaseSigningSettings(mode="adhoc")),
            environment={},
        )
        self.assertEqual([step.name for step in plan.teardown], [])
        self.assertEqual(plan.scratch_files(), ())


class MissingMaterialTests(unittest.TestCase):
    def test_a_missing_p12_fails_the_run_rather_than_degrading_silently(self):
        target = support.target()
        with self.assertRaises(SigningUnavailable) as caught:
            adapters.plan_for(target, environment=support.environment(p12=None))
        message = str(caught.exception)
        self.assertIn(support.P12_SECRET, message)
        self.assertIn(support.PASSWORD_SECRET, message)
        self.assertIn("allow_adhoc_fallback", message)

    def test_an_empty_secret_counts_as_missing(self):
        # GitHub hands a fork the variables with nothing in them, so a check for
        # "is the variable set" would pass and sign with no certificate at all.
        with self.assertRaises(SigningUnavailable):
            adapters.plan_for(support.target(), environment=support.environment(p12=""))
        with self.assertRaises(SigningUnavailable):
            adapters.plan_for(support.target(), environment=support.environment(password="   "))

    def test_a_partial_pair_is_not_a_certificate(self):
        with self.assertRaises(SigningUnavailable):
            adapters.plan_for(
                support.target(), environment=support.environment(password=None)
            )

    def test_an_explicitly_configured_fallback_is_allowed_and_says_so(self):
        plan = adapters.plan_for(
            support.target(signing=support.signing(allow_adhoc_fallback=True)),
            environment=support.environment(p12=None),
        )
        self.assertTrue(plan.degraded)
        self.assertFalse(plan.signature_pinned)
        self.assertEqual(plan.pinned_identity, "")
        self.assertEqual(plan.signing_identity, "")
        self.assertIn("stable identity", plan.degradation_reason)
        self.assertTrue(plan.notes)


class AdhocFallbackTests(unittest.TestCase):
    def adhoc_plan(self) -> ReleasePlan:
        return adapters.plan_for(
            support.target(signing=config_module.ReleaseSigningSettings(mode="adhoc")),
            environment=support.environment(),
        )

    def test_adhoc_signs_with_a_dash_and_never_claims_an_identity(self):
        plan = self.adhoc_plan()
        self.assertTrue(plan.degraded)
        self.assertFalse(plan.signature_pinned)
        self.assertEqual(plan.signing_identity, "")
        self.assertEqual(plan.pinned_identity, "")
        for step in plan.steps:
            if step.name.startswith("sign-"):
                argv = list(step.argv)
                self.assertEqual(argv[argv.index("--sign") + 1], "-")

    def test_adhoc_never_asks_for_authority_because_adhoc_has_none(self):
        plan = self.adhoc_plan()
        self.assertEqual(plan.assert_steps(), [])
        self.assertNotIn("pin-app-bundle", names(plan))
        # Verification still runs: an ad-hoc signature is still a signature, and
        # a corrupt one is still wrong.
        self.assertIn("verify-com.nanodictate.agent", names(plan))
        self.assertIn("verify-app-bundle", names(plan))

    def test_adhoc_does_no_keychain_work_at_all(self):
        plan = self.adhoc_plan()
        for absent in ("create-keychain", "import-p12", "materialize-p12", "delete-keychain"):
            self.assertNotIn(absent, names(plan))

    def test_adhoc_states_that_permissions_are_not_preserved(self):
        plan = self.adhoc_plan()
        self.assertIn("designated requirement", plan.degradation_reason)
        self.assertIn("permissions", plan.degradation_reason)

    def test_a_degraded_plan_may_not_claim_a_pinned_signature(self):
        with self.assertRaises(plan_module.ReleaseError):
            ReleasePlan(
                target="macos",
                adapter="apple",
                platform="macos",
                build_strategy="swiftpm",
                distribution="direct",
                step_list=(
                    plan_module.PlanStep(name="a", argv=["true"]),
                    plan_module.PlanStep(name="b", argv=["true"], teardown=True),
                ),
                signing_mode="adhoc",
                pinned_identity="CI",
                signature_pinned=True,
                degraded=True,
            )


class DeclaredButUnbuiltTests(unittest.TestCase):
    def test_ios_is_valid_configuration_but_not_a_buildable_target(self):
        target = support.target(platform=config_module.PLATFORM_IOS)
        with self.assertRaises(TargetNotExecutable) as caught:
            adapters.plan_for(target, environment=support.environment())
        self.assertIn("ios", str(caught.exception))
        self.assertIn("not built yet", str(caught.exception))

    def test_xcode_archive_is_valid_configuration_but_not_a_build_strategy_yet(self):
        target = support.target(build_strategy=config_module.BUILD_STRATEGY_XCODE_ARCHIVE)
        with self.assertRaises(TargetNotExecutable):
            adapters.plan_for(target, environment=support.environment())

    def test_developer_id_is_refused_without_blocking_the_self_signed_profile(self):
        target = support.target(
            signing=support.signing(mode=config_module.SIGNING_DEVELOPER_ID, team_id_var="TEAM_ID")
        )
        with self.assertRaises(TargetNotExecutable) as caught:
            adapters.plan_for(target, environment=support.environment())
        message = str(caught.exception)
        self.assertIn("not", message)
        self.assertIn("follow-up", message)
        # The MVP profile is unaffected by its unimplemented sibling.
        self.assertTrue(plan_for(support.target()).signature_pinned)

    def test_app_store_distribution_is_refused(self):
        target = support.target(distribution=config_module.DISTRIBUTION_APP_STORE)
        with self.assertRaises(TargetNotExecutable):
            adapters.plan_for(target, environment=support.environment())


class FailureClassificationTests(unittest.TestCase):
    def classify(self, step: str, detail: str):
        return adapters.explain_failure("apple", step, detail)

    def test_a_wrong_password_is_told_apart_from_a_bad_certificate(self):
        explained = self.classify(
            "import-p12",
            "security: SecKeychainItemImport: errSecAuthFailed: "
            "The user name or passphrase you entered is not correct.",
        )
        self.assertEqual(explained.code, "import-authentication-failed")
        self.assertIn("password", explained.message)
        self.assertIn("password_secret", explained.remediation)

    def test_unreadable_material_is_a_different_fix_again(self):
        explained = self.classify(
            "import-p12", "security: could not decode the provided PKCS#12 blob"
        )
        self.assertEqual(explained.code, "import-payload-invalid")
        self.assertIn("PKCS#12", explained.message)
        self.assertIn("p12_secret", explained.remediation)

    def test_an_identity_the_keychain_does_not_have_is_reported_as_such(self):
        explained = self.classify(
            "sign-com.nanodictate.agent",
            "codesign: errSecInternalComponent | No identity found",
        )
        self.assertEqual(explained.code, "sign-identity-missing")
        self.assertIn("identity", explained.message)

    def test_an_ambiguous_identity_is_reported_as_such(self):
        explained = self.classify(
            "sign-com.nanodictate.agent", "codesign: multiple identities match"
        )
        self.assertEqual(explained.code, "sign-identity-ambiguous")

    def test_a_failed_pin_explains_the_permission_consequence(self):
        explained = self.classify(
            "pin-app-bundle", "Executable=... Identifier=com.nanodictate.agent"
        )
        self.assertEqual(explained.code, "identity-not-pinned")
        self.assertIn("permissions", explained.remediation)
        self.assertIn("designated requirement", explained.remediation)

    def test_a_failed_verification_is_reported_as_corruption_not_as_identity(self):
        explained = self.classify("verify-app-bundle", "invalid signature for bundle")
        self.assertEqual(explained.code, "signature-verification-failed")
        self.assertIn("modified", explained.remediation)

    def test_classification_never_echoes_the_password_back(self):
        explained = self.classify(
            "import-p12",
            "security: import failed while using -P hunter2-is-the-password",
        )
        self.assertNotIn("hunter2", repr(explained.describe()))


class AdapterSurfaceTests(unittest.TestCase):
    def test_only_the_apple_adapter_is_registered(self):
        self.assertEqual(adapters.supported(), ("apple",))

    def test_an_unknown_adapter_is_refused_by_name(self):
        with self.assertRaises(adapters.UnsupportedAdapter):
            adapters.get("windows")

    def test_the_adapter_reports_its_own_name_for_diagnostics(self):
        self.assertEqual(apple.ADAPTER_NAME, "apple")

    def test_binding_material_is_joined_on_the_configured_secret_names(self):
        bound = apple.bind_material(support.signing(), support.environment())
        self.assertEqual(bound[apple.P12_ENV], support.FAKE_P12)
        self.assertEqual(bound[apple.P12_PASSWORD_ENV], support.FAKE_PASSWORD)

    def test_binding_material_leaves_a_blank_secret_blank(self):
        bound = apple.bind_material(support.signing(), support.environment(p12=None))
        self.assertNotIn(apple.P12_ENV, bound)

    def test_the_certificate_is_never_looked_for_on_a_non_apple_runner(self):
        # Planning must work anywhere; only running needs the toolchain.
        self.assertTrue(plan_for(support.target()).step_list)
        if not apple.available():
            with self.assertRaises(TargetNotExecutable):
                adapters.ensure_toolchain(apple)


class ArtifactContractTests(unittest.TestCase):
    def test_the_plan_reports_what_it_was_asked_to_produce(self):
        plan = plan_for(support.target(universal=True, architectures=("arm64", "x86_64")))
        self.assertEqual(plan.artifacts, ("tar.gz", "zip"))
        self.assertEqual(plan.architectures, ("arm64", "x86_64"))
        self.assertTrue(plan.universal)
        self.assertEqual(plan.publishers, ("homebrew", "macports"))
        self.assertEqual(plan.platform, "macos")
        self.assertEqual(plan.build_strategy, "swiftpm")

    def test_publishers_are_declared_but_never_executed(self):
        # Homebrew and MacPorts are a separate concern: the adapter hands off
        # signed artifacts, it does not push a formula or a port.
        plan = plan_for(support.target())
        for step in plan.step_list:
            self.assertNotIn("homebrew", " ".join(step.argv))
            self.assertNotIn("macports", " ".join(step.argv))

    def test_the_bundle_version_is_stamped_before_the_seal(self):
        order = names(plan_for(support.target()))
        self.assertLess(order.index("stamp-bundle-version"), order.index("sign-bundle"))

    def test_no_step_is_ever_a_shell_fragment(self):
        plan = plan_for(support.target())
        for step in plan.step_list:
            for token in step.argv:
                self.assertNotIn("&&", token)
                self.assertNotIn("|", token)
                self.assertNotIn(";", token)
                self.assertNotIn("$(", token)
                self.assertNotIn("`", token)
                self.assertNotIn(">", token)


class BundleVersionTests(unittest.TestCase):
    """The version stamp, which is easy to write in a way that never works.

    There is no shell in an argument vector, so a `Set :CFBundleVersion $VAR`
    command does not expand anything: PlistBuddy writes the literal string
    `$CONTINUUM_RELEASE_VERSION` into a shipped `Info.plist`, the step exits 0,
    and the release looks like it worked. The value therefore arrives through a
    declared slot, and the plan names what it needs.
    """

    def test_the_version_is_a_slot_not_a_dollar_sign(self):
        argv = argv_for(plan_for(support.target()), "stamp-bundle-version")
        joined = " ".join(argv)
        self.assertIn(plan_module.env_slot(apple.VERSION_ENV), joined)
        # Nothing is left for a shell to expand, because there is no shell. Every
        # `$` belongs to a declared slot, so the residue after removing them is
        # empty rather than merely short.
        residue = plan_module.SLOT_PATTERN.sub("", joined)
        self.assertNotIn("$", residue)

    def test_both_version_keys_are_stamped_from_the_one_declared_value(self):
        # `CFBundleVersion` is the build number and `CFBundleShortVersionString`
        # is the version the user sees and LaunchServices compares. Stamping
        # only the first ships a bundle that claims to be a new release and
        # reports itself as the old one.
        argv = argv_for(plan_for(support.target()), "stamp-bundle-version")
        for key in ("CFBundleVersion", "CFBundleShortVersionString"):
            self.assertIn(f"Set :{key} {plan_module.env_slot(apple.VERSION_ENV)}", argv)
        self.assertEqual(argv.count("-c"), 2)

    def test_both_keys_are_stamped_by_one_step_in_order(self):
        # Two steps would let the first write succeed and the second fail,
        # leaving a half-stamped Info.plist on the runner; "stamped before the
        # seal" would then be a property of two independently scheduled
        # commands rather than one.
        plan = plan_for(support.target())
        step = plan.step("stamp-bundle-version")
        self.assertEqual(list(step.argv)[0], apple.PLISTBUDDY)
        self.assertEqual(len(step.argv), 1 + 2 * len(apple._BUNDLE_VERSION_KEYS) + 1)
        joined = " ".join(step.argv)
        self.assertLess(
            joined.index("CFBundleVersion "), joined.index("CFBundleShortVersionString ")
        )

    def test_a_failed_version_stamp_is_not_reported_as_a_signing_failure(self):
        # PlistBuddy failing here is a missing Info.plist key. Telling the
        # reader to check the entitlements file sends them to the one file that
        # is not at fault.
        explained = adapters.explain_failure(
            "apple",
            "stamp-bundle-version",
            'Set: Entry, ":CFBundleShortVersionString", Does Not Exist',
        )
        self.assertEqual(explained.code, "bundle-version-stamp-failed")
        self.assertIn("Info.plist", explained.message)
        self.assertIn("CFBundleShortVersionString", explained.remediation)
        self.assertNotIn("entitlement", explained.remediation)

    def test_the_plist_inside_the_bundle_is_written_not_the_bundle(self):
        argv = argv_for(plan_for(support.target()), "stamp-bundle-version")
        target = argv[-1]
        self.assertTrue(target.endswith(os.path.join("Contents", "Info.plist")), target)

    def test_the_plan_declares_the_version_it_needs(self):
        plan = plan_for(support.target())
        self.assertIn(apple.VERSION_ENV, plan.required_env)
        self.assertIn(apple.VERSION_ENV, json.dumps(plan.describe()))

    def test_a_target_without_a_bundle_needs_no_version(self):
        plan = plan_for(support.target(app_bundle=None))
        self.assertEqual(plan.required_env, ())
        self.assertNotIn("stamp-bundle-version", names(plan))

    def test_the_stamped_value_reaches_plistbuddy(self):
        """End to end through the runner, because the bug was in the join."""

        plan = plan_for(support.target())
        step = plan.step("stamp-bundle-version")
        runner = run_module.Runner(
            environment={**support.environment(), apple.VERSION_ENV: "9.9.9"}
        )
        argv, _values = runner.resolve(step)
        self.assertIn("Set :CFBundleVersion 9.9.9", argv)
        self.assertNotIn("$", " ".join(argv))

    def test_a_missing_version_is_reported_rather_than_stamped_empty(self):
        plan = plan_for(support.target())
        runner = run_module.Runner(environment=support.environment(version=None))
        with self.assertRaises(plan_module.StepFailure) as caught:
            runner.resolve(plan.step("stamp-bundle-version"))
        self.assertEqual(caught.exception.code, "missing-value")
        self.assertIn(apple.VERSION_ENV, caught.exception.remediation)


if __name__ == "__main__":
    unittest.main()
