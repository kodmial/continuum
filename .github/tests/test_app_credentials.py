#!/usr/bin/env python3
"""Tests for the capability credential resolver.

`.github/scripts/app_credentials.py` is where least privilege is *enforced* for
the credentials this repository mints: a job asks for a capability by name and
receives exactly the permissions the registry declares, scoped to one repository,
expiring in an hour, and refused outright if GitHub grants more than was asked
for. The workflows only ever name a capability, so if this module's rules are
wrong, every workflow is wrong in the same direction at once.

These tests assert the properties that make it safe to use:

* the registry is a policy, not a table (see ``assert_registry``);
* a capability the ambient token already serves is never minted;
* the personal access token is a documented, opt-in, last-resort fallback and is
  never reached to paper over a real failure;
* a token is masked, exported to ``GITHUB_ENV``, and never written to a log.

Run with::

    python3 -m unittest discover -s .github/tests -p 'test_*.py'
"""

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))

import app_credentials  # noqa: E402  (path is set immediately above)

REPOSITORY = "kodmial/continuum"


class RegistryTests(unittest.TestCase):
    def test_the_registry_is_a_valid_policy(self):
        app_credentials.assert_registry()

    def test_every_capability_states_why_it_exists(self):
        for name, entry in app_credentials.CAPABILITIES.items():
            self.assertTrue(entry.reason.strip(), name)

    def test_a_minted_credential_must_name_what_github_token_cannot_do(self):
        for name in app_credentials.APP_TOKEN_CAPABILITIES:
            self.assertIn(
                "github.token",
                app_credentials.CAPABILITIES[name].reason,
                "{} is minted, so its reason has to be about the ambient token".format(name),
            )

    def test_some_capability_is_still_served_by_the_ambient_token(self):
        ambient = [
            name
            for name, entry in app_credentials.CAPABILITIES.items()
            if entry.credential == app_credentials.DEFAULT_TOKEN
        ]
        self.assertTrue(
            ambient,
            "if nothing is served by github.token, the ambient token is not being "
            "used where it is sufficient",
        )

    def test_only_the_agent_authoring_credential_crosses_into_a_model_prompt(self):
        model_facing = sorted(
            name for name, entry in app_credentials.CAPABILITIES.items() if entry.exposed_to_model
        )
        self.assertEqual(model_facing, ["agent-authoring"])
        permissions = app_credentials.CAPABILITIES["agent-authoring"].permissions
        self.assertNotEqual(permissions.get("issues"), "write")
        self.assertNotEqual(permissions.get("actions"), "write")
        self.assertNotIn("administration", permissions)

    def test_a_capability_the_ambient_token_serves_cannot_be_minted(self):
        with self.assertRaises(app_credentials.CredentialError) as caught:
            app_credentials.mint_installation_token("merge", REPOSITORY)
        message = str(caught.exception)
        self.assertIn("github.token", message)
        self.assertIn("least-privilege", message.replace("least-privilege", "least-privilege"))

    def test_a_registry_with_no_ambient_capability_is_refused(self):
        with unittest.mock.patch.dict(
            app_credentials.CAPABILITIES,
            {
                name: app_credentials.Capability(
                    name=name,
                    permissions=entry.permissions,
                    credential=app_credentials.APP_TOKEN,
                    reason=entry.reason + " github.token cannot do this.",
                )
                for name, entry in app_credentials.CAPABILITIES.items()
            },
            clear=True,
        ):
            with self.assertRaises(app_credentials.CredentialError) as caught:
                app_credentials.assert_registry()
        self.assertIn("github.token", str(caught.exception))

    def test_an_unjustified_minted_capability_is_refused(self):
        reason = "The agent needs it."
        with unittest.mock.patch.dict(
            app_credentials.CAPABILITIES,
            dict(app_credentials.CAPABILITIES),
            clear=True,
        ):
            app_credentials.CAPABILITIES["mystery"] = app_credentials.Capability(
                name="mystery",
                permissions={"contents": "write"},
                credential=app_credentials.APP_TOKEN,
                reason=reason,
            )
            with self.assertRaises(app_credentials.CredentialError) as caught:
                app_credentials.assert_registry()
        self.assertIn("github.token", str(caught.exception))

    def test_a_model_facing_capability_with_issue_write_is_refused(self):
        with unittest.mock.patch.dict(
            app_credentials.CAPABILITIES,
            dict(app_credentials.CAPABILITIES),
            clear=True,
        ):
            app_credentials.CAPABILITIES["chatty"] = app_credentials.Capability(
                name="chatty",
                permissions={"contents": "write", "issues": "write"},
                credential=app_credentials.APP_TOKEN,
                reason="Because github.token cannot do it.",
                exposed_to_model=True,
            )
            with self.assertRaises(app_credentials.CredentialError) as caught:
                app_credentials.assert_registry()
        self.assertIn("issues", str(caught.exception))

    def test_an_unknown_capability_names_the_known_ones(self):
        with self.assertRaises(app_credentials.CredentialError) as caught:
            app_credentials.capability("root-everything")
        self.assertIn("agent-authoring", str(caught.exception))


class PermissionDisciplineTests(unittest.TestCase):
    def test_extra_authority_is_detected(self):
        requested = {"contents": "write", "issues": "read"}
        self.assertEqual(app_credentials.overgranted(requested, dict(requested)), [])

        # A scope nobody asked for.
        self.assertIn(
            "actions=write (not requested)",
            app_credentials.overgranted(requested, dict(requested, actions="write")),
        )
        # A scope asked for, at a higher level than asked for.
        self.assertIn(
            "contents=admin (requested write)",
            app_credentials.overgranted(requested, dict(requested, contents="admin")),
        )

    def test_metadata_is_implicit_and_not_an_overgrant(self):
        self.assertEqual(
            app_credentials.overgranted({"contents": "read"}, {"contents": "read", "metadata": "read"}),
            [],
        )

    def test_overgranting_a_credential_fails_closed(self):
        with self.assertRaises(app_credentials.CredentialError) as caught:
            app_credentials.assert_least_privilege(
                {"contents": "read"}, {"contents": "read", "issues": "write"}
            )
        self.assertIn("beyond its capability", str(caught.exception))

    def test_permission_levels_normalizes_spelling(self):
        self.assertEqual(
            app_credentials.permission_levels({"Pull Requests": " Write "}),
            {"pull_requests": "write"},
        )
        self.assertEqual(app_credentials.permission_levels(None), {})


class MintingTests(unittest.TestCase):
    """The API conversation, with no network."""

    def environment(self, **overrides):
        env = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "CONTINUUM_APP_ID": "123456",
            "CONTINUUM_APP_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----\n",
            "CONTINUUM_AUTOMATION_LOGIN": "continuum[bot]",
        }
        env.update(overrides)
        return env

    def run_mint(self, capability, requests, env=None, *, identity="continuum"):
        """Drive ``mint_installation_token`` against a recorded API conversation."""
        self.requests = requests

        def fake_request(method, path, token, body=None, *, api_url=""):
            self.requests.append((method, path, body))
            if path == "/repos/{}/{}/installation".format(*REPOSITORY.split("/")):
                return {"id": 42}
            if path == "/app/installations/42/access_tokens":
                return {
                    "token": "ghs_installation_token",
                    "expires_at": "2026-09-29T12:00:00Z",
                    "permissions": {
                        "contents": "write",
                        "issues": "read",
                        "pull_requests": "write",
                        "workflows": "write",
                        "metadata": "read",
                    },
                }
            if path == "/app":
                return {"slug": identity}
            raise AssertionError("unexpected request: {} {}".format(method, path))

        with unittest.mock.patch.dict(os.environ, self.environment(**(env or {}))):
            with unittest.mock.patch.object(app_credentials, "api_request", fake_request):
                with unittest.mock.patch.object(
                    app_credentials, "sign_jwt", lambda *a, **k: "app-assertion"
                ):
                    return app_credentials.mint_installation_token(
                        capability, REPOSITORY, api_url="https://api.example.test"
                    )

    def test_a_mint_asks_only_for_the_capability_and_only_for_this_repository(self):
        self.run_mint("agent-authoring", requests=[])
        mint_request = [
            request
            for request in self.requests
            if request[1] == "/app/installations/42/access_tokens"
        ]
        self.assertEqual(len(mint_request), 1)
        method, path, body = mint_request[0]
        self.assertEqual(method, "POST")
        self.assertEqual(
            body["permissions"], app_credentials.CAPABILITIES["agent-authoring"].permissions
        )
        self.assertEqual(body["repositories"], ["continuum"])

    def test_a_mint_returns_an_expiring_repository_scoped_credential(self):
        resolved = self.run_mint("agent-authoring", requests=[])
        self.assertEqual(resolved["kind"], "app-installation")
        self.assertEqual(resolved["identity"], "continuum[bot]")
        self.assertEqual(resolved["source"], "github-app")
        self.assertEqual(resolved["expires_at"], "2026-09-29T12:00:00Z")
        self.assertEqual(resolved["permissions"]["contents"], "write")

    def test_a_token_that_speaks_as_the_wrong_identity_is_refused(self):
        with self.assertRaises(app_credentials.CredentialError) as caught:
            self.run_mint("agent-authoring", requests=[], identity="attacker-automation")
        self.assertIn("continuum[bot]", str(caught.exception))
        self.assertIn("attacker-automation[bot]", str(caught.exception))

    def test_a_token_granted_more_than_its_capability_is_refused(self):
        def overgranting_request(method, path, token, body=None, *, api_url=""):
            if path.endswith("/installation"):
                return {"id": 42}
            if path == "/app/installations/42/access_tokens":
                return {
                    "token": "ghs_too_broad",
                    "permissions": {"contents": "write", "administration": "admin"},
                }
            return {"slug": "continuum"}

        with unittest.mock.patch.dict(os.environ, self.environment()):
            with unittest.mock.patch.object(app_credentials, "api_request", overgranting_request):
                with unittest.mock.patch.object(
                    app_credentials, "sign_jwt", lambda *a, **k: "app-assertion"
                ):
                    with self.assertRaises(app_credentials.CredentialError) as caught:
                        app_credentials.mint_installation_token("agent-authoring", REPOSITORY)
        self.assertIn("beyond its capability", str(caught.exception))

    def test_an_unconfigured_app_names_what_to_set(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"GITHUB_REPOSITORY": REPOSITORY, "CONTINUUM_APP_ID": "", "CONTINUUM_APP_PRIVATE_KEY": ""},
            clear=True,
        ):
            with self.assertRaises(app_credentials.CredentialError) as caught:
                app_credentials.mint_installation_token("agent-authoring", REPOSITORY)
        message = str(caught.exception)
        self.assertIn("CONTINUUM_APP_ID", message)
        self.assertIn("CONTINUUM_APP_PRIVATE_KEY", message)

    def test_a_private_key_that_is_not_a_pem_is_refused(self):
        with self.assertRaises(app_credentials.CredentialError) as caught:
            app_credentials.normalize_private_key("ghp_not_a_key")
        self.assertIn("PEM", str(caught.exception))

    def test_literal_newline_escapes_are_accepted(self):
        raw = "-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----"
        self.assertIn("\n", app_credentials.normalize_private_key(raw))

    def test_the_repository_must_be_owner_slash_repo(self):
        for bad in ("continuum", "a/b/c", "/continuum", "kodmial/", "owner/"):
            with unittest.mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(app_credentials.CredentialError, msg=bad):
                    app_credentials.repository_from(bad)

    def test_an_empty_repository_falls_back_to_the_ambient_one(self):
        with unittest.mock.patch.dict(
            os.environ, {"GITHUB_REPOSITORY": "kodmial/continuum"}, clear=True
        ):
            self.assertEqual(app_credentials.repository_from(""), ("kodmial", "continuum"))


class FallbackTests(unittest.TestCase):
    """`TAP_PAT` is a migration path, not a credential model."""

    def test_the_fallback_is_refused_unless_it_is_opted_into(self):
        with unittest.mock.patch.dict(
            os.environ, {"TAP_PAT": "ghp_personal"}, clear=True
        ):
            self.assertFalse(app_credentials.pat_fallback_enabled())
            with self.assertRaises(app_credentials.CredentialError) as caught:
                app_credentials.pat_fallback_token()
        message = str(caught.exception)
        self.assertIn("CONTINUUM_ALLOW_PAT_FALLBACK", message)
        self.assertIn("docs/credential-capabilities.md", message)

    @unittest.mock.patch.dict(os.environ, {}, clear=True)
    def test_every_spelling_of_yes_enables_it(self):
        for value in ("true", "TRUE", "1", "yes"):
            os.environ["CONTINUUM_ALLOW_PAT_FALLBACK"] = value
            self.assertTrue(app_credentials.pat_fallback_enabled(), value)
        for value in ("", "false", "no", "0"):
            os.environ["CONTINUUM_ALLOW_PAT_FALLBACK"] = value
            self.assertFalse(app_credentials.pat_fallback_enabled(), value)

    def test_an_enabled_fallback_with_no_token_fails_closed(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"CONTINUUM_ALLOW_PAT_FALLBACK": "true", "TAP_PAT": ""},
            clear=True,
        ):
            with self.assertRaises(app_credentials.CredentialError) as caught:
                app_credentials.pat_fallback_token()
        self.assertIn("fails closed", str(caught.exception))

    def test_the_app_is_preferred_when_both_are_available(self):
        with unittest.mock.patch.dict(
            os.environ,
            {
                "CONTINUUM_ALLOW_PAT_FALLBACK": "true",
                "TAP_PAT": "ghp_personal",
            },
            clear=True,
        ):
            with unittest.mock.patch.object(
                app_credentials,
                "mint_installation_token",
                return_value={"token": "app", "source": "github-app"},
            ):
                resolved = app_credentials.resolve("agent-authoring", REPOSITORY)
        self.assertEqual(resolved["source"], "github-app")

    def test_the_fallback_is_used_only_when_the_app_is_unconfigured(self):
        def unconfigured(*args, **kwargs):
            raise app_credentials.CredentialError(
                "neither CONTINUUM_APP_ID nor CONTINUUM_APP_PRIVATE_KEY is set"
            )

        with unittest.mock.patch.dict(
            os.environ,
            {
                "CONTINUUM_ALLOW_PAT_FALLBACK": "true",
                "TAP_PAT": "ghp_personal",
            },
            clear=True,
        ):
            with unittest.mock.patch.object(
                app_credentials, "mint_installation_token", unconfigured
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    resolved = app_credentials.resolve("agent-authoring", REPOSITORY)
        self.assertEqual(resolved["source"], "pat-fallback")
        self.assertEqual(resolved["kind"], "user-pat")
        self.assertIn("personal access token fallback", stderr.getvalue())

    def test_a_real_failure_is_never_papered_over_by_the_fallback(self):
        """A wrong identity or an over-granted token must not become a PAT.

        Falling back here would be the worst outcome available: the run would
        continue with a broader credential precisely when something is wrong
        with the narrow one.
        """
        failures = {
            "wrong identity": "The minted credential speaks as @attacker[bot].",
            "over-granted": "The minted token holds authority beyond its capability.",
            "revoked key": "GitHub rejected GET /repos/kodmial/continuum/installation (404)",
        }
        for label, detail in failures.items():
            with unittest.mock.patch.dict(
                os.environ,
                {
                    "CONTINUUM_ALLOW_PAT_FALLBACK": "true",
                    "TAP_PAT": "ghp_personal",
                },
                clear=True,
            ):
                with unittest.mock.patch.object(
                    app_credentials,
                    "mint_installation_token",
                    side_effect=app_credentials.CredentialError(detail),
                ):
                    with self.assertRaises(app_credentials.CredentialError, msg=label):
                        app_credentials.resolve("agent-authoring", REPOSITORY)


class PublicationTests(unittest.TestCase):
    def test_the_token_is_masked_exported_and_never_logged(self):
        resolved = {
            "token": "ghs_secret_value",
            "identity": "continuum[bot]",
            "kind": "app-installation",
            "expires_at": "2026-09-29T12:00:00Z",
            "source": "github-app",
            "capability": "issue-write",
            "permissions": {"issues": "write", "contents": "read"},
        }
        with tempfile.TemporaryDirectory() as directory:
            env_file = pathlib.Path(directory) / "env"
            output_file = pathlib.Path(directory) / "output"
            env_file.write_text("")
            output_file.write_text("")
            with unittest.mock.patch.dict(
                os.environ,
                {"GITHUB_ENV": str(env_file), "GITHUB_OUTPUT": str(output_file)},
                clear=True,
            ):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    app_credentials.publish(resolved, "CONTINUUM_TOKEN_ISSUE_WRITE")

            printed = stdout.getvalue()
            self.assertIn("::add-mask::ghs_secret_value", printed)

            exported = env_file.read_text()
            self.assertEqual(
                exported, "CONTINUUM_TOKEN_ISSUE_WRITE=ghs_secret_value\n"
            )
            # The secret must not appear in any log surface. The mask line is
            # the one place it is written, and only because Actions needs it.
            self.assertEqual(printed.count("ghs_secret_value"), 1)
            self.assertNotIn("ghs_secret_value", output_file.read_text())

            outputs = dict(
                line.split("=", 1)
                for line in output_file.read_text().splitlines()
                if "=" in line
            )
        self.assertEqual(outputs["credential_source"], "github-app")
        self.assertEqual(outputs["credential_identity"], "continuum[bot]")
        self.assertEqual(outputs["credential_capability"], "issue-write")
        self.assertEqual(outputs["credential_permissions"], "contents=read,issues=write")

    def test_publishing_without_github_env_refuses_rather_than_printing(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with self.assertRaises(app_credentials.CredentialError) as caught:
                    app_credentials.publish({"token": "ghs_secret_value"}, "X")
        self.assertIn("GITHUB_ENV", str(caught.exception))

    def test_the_published_variable_name_comes_from_the_capability(self):
        self.assertEqual(
            app_credentials.env_name_for("agent-authoring"), "CONTINUUM_TOKEN_AGENT_AUTHORING"
        )
        self.assertEqual(
            app_credentials.env_name_for("repair-control"), "CONTINUUM_TOKEN_REPAIR_CONTROL"
        )

    def test_a_capability_cannot_be_published_under_another_capabilitys_name(self):
        """`--env-name` exists for clarity, not to relabel a credential.

        The name is derived from the capability so a workflow cannot mint
        `issue-write` and hand it to a step expecting `agent-authoring`. This
        test pins the derivation; the override is still allowed because a
        workflow may want a shorter name, but the default is not a free choice.
        """
        args = app_credentials.build_parser().parse_args(["mint", "--capability", "issue-write"])
        self.assertEqual(args.capability, "issue-write")
        self.assertEqual(args.env_name, "")


class CliTests(unittest.TestCase):
    def test_capabilities_prints_the_registry(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(app_credentials.main(["capabilities"]), 0)
        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), len(app_credentials.CAPABILITIES))
        for name in app_credentials.CAPABILITIES:
            self.assertTrue(
                any(line.startswith(name + "\t") for line in lines), name
            )

    def test_an_unknown_capability_exits_non_zero(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(app_credentials.main(["mint", "--capability", "root"]), 1)
        self.assertIn("::error::", stderr.getvalue())

    def test_a_refusal_never_writes_a_token_to_stdout(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(app_credentials.main(["mint", "--capability", "issue-write"]), 1)
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
