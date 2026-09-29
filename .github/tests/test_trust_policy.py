#!/usr/bin/env python3
"""Regression tests for Continuum's public-repository trust boundary.

Each threat from the hardening issue has at least one negative test that fails
closed, and at least one positive test that proves the trusted path still works.
The malicious inputs live in ``fixtures/`` so the attack shapes are reviewable
data rather than prose: a stranger-authored issue, a stranger comment on an
owner's issue, a comment on a pull request, a fork pull request with an
agent-shaped branch name, a forged ``workflow_dispatch`` head ref, and a pull
request that edits the privilege boundary itself.

Run with::

    python3 -m unittest discover -s .github/tests -t .
"""

import copy
import http.server
import inspect
import json
import os
import pathlib
import re
import sys
import threading
import tempfile
import unittest
import unittest.mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))

import trust_policy  # noqa: E402

REPOSITORY = "kodmial/continuum"
OWNER = "kodmial"
BASE_REF = "main"

_load_cache = {}


def load_fixture(name):
    """Load a fixture by name, returning a private copy.

    Results are cached so the usage audit below can record which fixtures the
    suite actually consumes; each caller gets a deep copy so a test that mutates
    a payload cannot change what a later test sees.
    """
    if name not in _load_cache:
        with open(FIXTURE_DIR / (name + ".json"), encoding="utf-8") as handle:
            _load_cache[name] = json.load(handle)
    return copy.deepcopy(_load_cache[name])


def materialize(case):
    text = case.get("text", "")
    return text * int(case.get("repeat", 1))


class ActorTrustTests(unittest.TestCase):
    def test_repository_owner_is_trusted(self):
        self.assertTrue(trust_policy.is_trusted_actor("kodmial", OWNER))

    def test_owner_login_is_case_and_at_sign_insensitive(self):
        for variant in ("KodMial", "@kodmial", "  kodmial  "):
            self.assertTrue(trust_policy.is_trusted_actor(variant, OWNER), variant)

    def test_configured_actors_are_trusted(self):
        self.assertTrue(
            trust_policy.is_trusted_actor(
                "trusted-collab", OWNER, "trusted-collab, other-person"
            )
        )

    def test_stranger_is_not_trusted(self):
        self.assertFalse(trust_policy.is_trusted_actor("untrusted-stranger", OWNER))

    def test_collaborator_is_not_trusted_by_default(self):
        # Being able to comment is not authorization to spend the write token.
        self.assertFalse(trust_policy.is_trusted_actor("some-collaborator", OWNER))

    def test_bots_are_never_trusted(self):
        # A bot's body is frequently a verbatim echo of untrusted user content.
        for bot in ("coderabbitai[bot]", "dependabot[bot]", "github-actions[bot]"):
            self.assertFalse(trust_policy.is_trusted_actor(bot, OWNER), bot)

    def test_lookalike_logins_are_not_trusted(self):
        for lookalike in ("kodmial-evil", "kodmial.evil", "kodmial2", "xkodmial", "kod mia", ""):
            self.assertFalse(trust_policy.is_trusted_actor(lookalike, OWNER), lookalike)

    def test_missing_or_malformed_identity_is_untrusted(self):
        for value in (None, 7, [], {}, "  "):
            self.assertFalse(trust_policy.is_trusted_actor(value, OWNER), repr(value))

    def test_missing_owner_is_untrusted(self):
        self.assertFalse(trust_policy.is_trusted_actor("kodmial", ""))


class AgentBranchTests(unittest.TestCase):
    def test_agent_branches_are_recognized(self):
        for ref in (
            "opencode/issue1-x",
            "opencode/issue12-20260928135106",
            "opencode/issue345-a_b.c-1",
        ):
            self.assertTrue(trust_policy.is_agent_branch(ref), ref)
            self.assertTrue(trust_policy.is_safe_ref(ref), ref)
            self.assertGreater(trust_policy.issue_number_from_branch(ref), 0)

    def test_non_agent_branches_are_rejected(self):
        for ref in (
            "main",
            "release/1.0",
            "opencode",
            "opencode/",
            "opencode/issue12",
            "opencode/issue12-",
            "opencode/issue0-x",
            "opencode/issue01-x",
            "opencode/issue-1-x",
            "opencode/issue1-x/y",
            "OpenCode/issue1-x",
            "refs/heads/main",
            "",
            None,
        ):
            self.assertFalse(trust_policy.is_agent_branch(ref), repr(ref))

    def test_argument_and_traversal_refs_are_rejected(self):
        for ref in (
            "opencode/issue1--upload-pack=evil",
            "opencode/issue1-../../main",
            "opencode/issue1-x;rm -rf /",
            "opencode/issue1-$(id)",
            "opencode/issue1-`id`",
            "opencode/issue1-x|tee",
            "opencode/issue1-a//b",
            "opencode/issue1-a.lock",
        ):
            self.assertFalse(trust_policy.is_agent_branch(ref), ref)
            self.assertFalse(trust_policy.is_safe_ref(ref), ref)

    def test_option_like_refs_are_rejected(self):
        # A leading dash would be an option to git/gh, not a ref.
        for ref in ("-x", "--upload-pack=evil", "-rf"):
            self.assertFalse(trust_policy.is_safe_ref(ref), ref)


class IssueCommentTests(unittest.TestCase):
    def evaluate(self, fixture_name, **overrides):
        payload = load_fixture(fixture_name)
        kwargs = {
            "repository": REPOSITORY,
            "configured_actors": "",
            "base_ref": BASE_REF,
            "actor": (payload.get("sender") or {}).get("login", ""),
        }
        kwargs.update(overrides)
        return trust_policy.evaluate_event(
            "issue_comment", payload, **kwargs
        )

    def test_owner_comment_on_owner_issue_is_allowed(self):
        decision = self.evaluate("issue_comment_owner_on_owner_issue")
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(decision.issue_number, 44)
        self.assertEqual(decision.mode, "")
        self.assertEqual(decision.checkout_ref, "")

    def test_comment_on_owner_issue_gets_base_branch_checkout(self):
        decision = trust_policy.evaluate_event(
            "issue_comment",
            load_fixture("issue_comment_owner_on_owner_issue"),
            repository=REPOSITORY,
        )
        self.assertTrue(decision.allowed, decision.reason)
        decision = trust_policy._issue_comment_ref(decision, BASE_REF)
        self.assertEqual(decision.checkout_ref, BASE_REF)

    def test_stranger_authored_issue_is_denied(self):
        decision = self.evaluate("issue_comment_attacker_authored_issue")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "untrusted_issue_author")
        self.assertEqual(decision.checkout_ref, "")

    def test_stranger_comment_on_owner_issue_is_denied(self):
        decision = self.evaluate("issue_comment_attacker_on_owner_issue")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "untrusted_comment_author")

    def test_owner_comment_on_pull_request_is_denied(self):
        decision = self.evaluate("issue_comment_on_pull_request")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "comment_on_pull_request")

    def test_edited_comment_cannot_re_trigger_a_run(self):
        # "Edit issue content to inject agent instructions" must not be a
        # privileged trigger: only a newly created comment can start work.
        payload = load_fixture("issue_comment_owner_on_owner_issue")
        payload["action"] = "edited"
        decision = trust_policy.evaluate_event("issue_comment", payload, repository=REPOSITORY)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "comment_not_created")

    def test_contradictory_author_association_fails_closed(self):
        payload = load_fixture("issue_comment_owner_on_owner_issue")
        payload["issue"]["author_association"] = "NONE"
        decision = trust_policy.evaluate_event("issue_comment", payload, repository=REPOSITORY)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "untrusted_author_association")

    def test_configured_actor_may_work_on_own_issue(self):
        payload = load_fixture("issue_comment_attacker_authored_issue")
        for field in ("issue", "comment"):
            payload[field]["user"]["login"] = "trusted-collab"
            payload[field]["author_association"] = "MEMBER"
        payload["sender"]["login"] = "trusted-collab"
        decision = trust_policy.evaluate_event(
            "issue_comment",
            payload,
            repository=REPOSITORY,
            configured_actors="trusted-collab",
        )
        self.assertTrue(decision.allowed, decision.reason)

    def test_missing_comment_object_fails_closed(self):
        payload = load_fixture("issue_comment_owner_on_owner_issue")
        payload.pop("comment")
        decision = trust_policy.evaluate_event("issue_comment", payload, repository=REPOSITORY)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "missing_comment")

    def test_malformed_payload_fails_closed(self):
        for payload in (None, [], "{}", 5):
            decision = trust_policy.evaluate_event(
                "issue_comment", payload, repository=REPOSITORY
            )
            self.assertFalse(decision.allowed, repr(payload))


class DispatchTests(unittest.TestCase):
    def verify(self, fixture_name, pull_request=None):
        payload = load_fixture(fixture_name)
        return trust_policy.evaluate_event(
            "workflow_dispatch",
            payload,
            repository=REPOSITORY,
            actor=(payload.get("sender") or {}).get("login", ""),
            pull_request=pull_request,
        )

    def test_trusted_repair_dispatch_is_allowed(self):
        pr = load_fixture("pull_request_agent_branch")["pull_request"]
        decision = self.verify("workflow_dispatch_trusted_repair", pull_request=pr)
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(decision.mode, "resolve-conflict")
        self.assertEqual(decision.pr_number, 3)
        self.assertEqual(decision.head_ref, "opencode/issue12-20260928135106")
        # The only ref a privileged job may use comes from the verified PR.
        self.assertEqual(decision.checkout_ref, "opencode/issue12-20260928135106")

    def test_forged_head_ref_is_denied_before_any_lookup(self):
        decision = trust_policy.validate_dispatch_shape(
            load_fixture("workflow_dispatch_forged_head_ref")["inputs"],
            repository=REPOSITORY,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "untrusted_dispatch_ref")
        self.assertEqual(decision.checkout_ref, "")

    def test_forged_dispatch_from_a_stranger_is_denied(self):
        decision = self.verify("workflow_dispatch_forged_head_ref")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "untrusted_dispatcher")

    def test_dispatch_without_verified_pull_request_fails_closed(self):
        decision = self.verify("workflow_dispatch_trusted_repair")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "missing_pull_request")

    def test_dispatch_pointing_at_a_fork_fails_closed(self):
        pr = load_fixture("pull_request_from_fork")["pull_request"]
        decision = self.verify("workflow_dispatch_trusted_repair", pull_request=pr)
        self.assertFalse(decision.allowed)
        self.assertIn(decision.code, {"fork_pull_request", "pr_number_mismatch"})

    def test_head_ref_must_match_the_verified_pull_request(self):
        pr = json.loads(json.dumps(load_fixture("pull_request_agent_branch")["pull_request"]))
        pr["head"]["ref"] = "opencode/issue12-different-slug"
        decision = self.verify("workflow_dispatch_trusted_repair", pull_request=pr)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "head_ref_mismatch")

    def test_only_allowlisted_modes_are_accepted(self):
        for mode in ("coderabbit-fix", "release", "main", "", None, "CI-FIX"):
            decision = trust_policy.validate_dispatch_shape(
                {
                    "mode": mode,
                    "pr_number": "3",
                    "head_ref": "opencode/issue12-20260928135106",
                    "run_id": "1",
                },
                repository=REPOSITORY,
            )
            self.assertFalse(decision.allowed, repr(mode))
            self.assertEqual(decision.code, "unknown_dispatch_mode", repr(mode))

    def test_ci_fix_requires_a_run_id(self):
        decision = trust_policy.validate_dispatch_shape(
            {
                "mode": "ci-fix",
                "pr_number": "3",
                "head_ref": "opencode/issue12-20260928135106",
                "run_id": "",
            },
            repository=REPOSITORY,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "invalid_run_id")

    def test_numeric_inputs_reject_argument_smuggling(self):
        for pr_number in ("3 --metadata-file=/tmp/x", "3;id", "+3", " 3 4", "0x3", "1_0", "-3", ""):
            decision = trust_policy.validate_dispatch_shape(
                {
                    "mode": "ci-fix",
                    "pr_number": pr_number,
                    "head_ref": "opencode/issue12-20260928135106",
                    "run_id": "42",
                },
                repository=REPOSITORY,
            )
            self.assertFalse(decision.allowed, repr(pr_number))
            self.assertEqual(decision.code, "invalid_pr_input", repr(pr_number))

    def test_unhandled_events_are_denied(self):
        for event in ("push", "pull_request", "pull_request_target", "issues", "", None):
            decision = trust_policy.evaluate_event(
                event, {}, repository=REPOSITORY
            )
            self.assertFalse(decision.allowed, repr(event))

    def test_unknown_repository_fails_closed(self):
        for repository in ("", "continuum", "a/b/c", None):
            decision = trust_policy.evaluate_event(
                "workflow_dispatch",
                load_fixture("workflow_dispatch_trusted_repair"),
                repository=repository,
            )
            self.assertFalse(decision.allowed, repr(repository))
            self.assertEqual(decision.code, "unknown_repository", repr(repository))


class PullRequestTrustTests(unittest.TestCase):
    def test_agent_pull_request_is_trusted(self):
        pr = load_fixture("pull_request_agent_branch")["pull_request"]
        trusted, code, _ = trust_policy.is_trusted_pull_request(pr, REPOSITORY)
        self.assertTrue(trusted, code)

    def test_fork_pull_request_is_never_trusted(self):
        pr = load_fixture("pull_request_from_fork")["pull_request"]
        trusted, code, reason = trust_policy.is_trusted_pull_request(pr, REPOSITORY)
        self.assertFalse(trusted)
        self.assertEqual(code, "fork_pull_request")
        self.assertIn("fork", reason.lower())

    def test_deleted_fork_reports_no_head_repository(self):
        pr = json.loads(json.dumps(load_fixture("pull_request_from_fork")["pull_request"]))
        pr["head"]["repo"] = None
        trusted, code, _ = trust_policy.is_trusted_pull_request(pr, REPOSITORY)
        self.assertFalse(trusted)
        self.assertEqual(code, "fork_pull_request")

    def test_non_agent_branch_in_same_repository_is_not_trusted(self):
        pr = load_fixture("pull_request_non_agent_branch")["pull_request"]
        trusted, code, _ = trust_policy.is_trusted_pull_request(pr, REPOSITORY)
        self.assertFalse(trusted)
        self.assertEqual(code, "untrusted_head_ref")

    def test_closed_pull_request_is_not_trusted(self):
        pr = json.loads(json.dumps(load_fixture("pull_request_agent_branch")["pull_request"]))
        pr["state"] = "closed"
        trusted, code, _ = trust_policy.is_trusted_pull_request(pr, REPOSITORY)
        self.assertFalse(trusted)
        self.assertEqual(code, "pr_not_open")

    def test_non_default_base_branch_is_not_trusted(self):
        pr = json.loads(json.dumps(load_fixture("pull_request_agent_branch")["pull_request"]))
        pr["base"]["ref"] = "release/1.0"
        trusted, code, _ = trust_policy.is_trusted_pull_request(pr, REPOSITORY)
        self.assertFalse(trusted)
        self.assertEqual(code, "pr_targets_untrusted_base")

    def test_abbreviated_head_sha_is_not_trusted(self):
        pr = json.loads(json.dumps(load_fixture("pull_request_agent_branch")["pull_request"]))
        pr["head"]["sha"] = "3333333"
        trusted, code, _ = trust_policy.is_trusted_pull_request(pr, REPOSITORY)
        self.assertFalse(trusted)
        self.assertEqual(code, "untrusted_head_sha")

    def test_missing_pull_request_fails_closed(self):
        for pr in (None, {}, [], "pr"):
            trusted, code, _ = trust_policy.is_trusted_pull_request(pr, REPOSITORY)
            self.assertFalse(trusted, repr(pr))


class MergeEligibilityTests(unittest.TestCase):
    def test_trusted_green_agent_pr_is_mergeable(self):
        fixture = load_fixture("pull_request_agent_branch")
        decision = trust_policy.evaluate_merge(
            repository=REPOSITORY,
            pull_request=fixture["pull_request"],
            changed_files=fixture["files"],
            ci_green=True,
        )
        self.assertTrue(decision.allowed, decision.reason)

    def test_privilege_boundary_changes_block_automatic_merge(self):
        fixture = load_fixture("pull_request_workflow_change")
        decision = trust_policy.evaluate_merge(
            repository=REPOSITORY,
            pull_request=fixture["pull_request"],
            changed_files=fixture["files"],
            ci_green=True,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "trust_sensitive_changes")
        self.assertIn(".github/workflows/opencode.yml", decision.reason)
        self.assertIn(".github/scripts/trust_policy.py", decision.reason)

    def test_fork_pull_request_is_never_auto_merged(self):
        fixture = load_fixture("pull_request_from_fork")
        decision = trust_policy.evaluate_merge(
            repository=REPOSITORY,
            pull_request=fixture["pull_request"],
            changed_files=["README.md"],
            ci_green=True,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "fork_pull_request")

    def test_missing_or_failing_ci_blocks_merge(self):
        fixture = load_fixture("pull_request_agent_branch")
        for ci_green in (False, None, "true", 1):
            decision = trust_policy.evaluate_merge(
                repository=REPOSITORY,
                pull_request=fixture["pull_request"],
                changed_files=fixture["files"],
                ci_green=ci_green,
            )
            self.assertFalse(decision.allowed, repr(ci_green))
            self.assertEqual(decision.code, "ci_not_green", repr(ci_green))

    def test_draft_pull_request_is_never_auto_merged(self):
        fixture = json.loads(json.dumps(load_fixture("pull_request_agent_branch")))
        fixture["pull_request"]["draft"] = True
        decision = trust_policy.evaluate_merge(
            repository=REPOSITORY,
            pull_request=fixture["pull_request"],
            changed_files=fixture["files"],
            ci_green=True,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "draft_pull_request")

    def test_privilege_boundary_path_detection(self):
        for path in (
            ".github/workflows/ci.yml",
            ".github/workflows/nested/new.yml",
            ".github/scripts/trust_policy.py",
            ".github/actions/build/action.yml",
            "CODEOWNERS",
            ".github/CODEOWNERS",
            "action.yml",
        ):
            self.assertTrue(trust_policy.touches_trust_sensitive_path(path), path)

    def test_ordinary_paths_are_not_privilege_boundary(self):
        for path in (
            "README.md",
            "src/app.py",
            ".github/ISSUE_TEMPLATE/bug.yml",
            "docs/workflows.md",
            "mygithub/workflows/ci.yml",
            "src/action.yml",
            "",
            None,
        ):
            self.assertFalse(trust_policy.touches_trust_sensitive_path(path), repr(path))


class UntrustedTextTests(unittest.TestCase):
    def test_fixture_cases(self):
        for case in load_fixture("untrusted_texts")["cases"]:
            with self.subTest(case["name"]):
                text = materialize(case)
                neutralize = (
                    trust_policy.sanitize_commit_title
                    if case.get("assert_on") == "commit_title"
                    else trust_policy.neutralize_untrusted_text
                )
                neutralized = neutralize(text)
                for pattern in case["must_not_match"]:
                    self.assertIsNone(
                        re.search(pattern, neutralized),
                        "{}: {!r} survived neutralization".format(case["name"], pattern),
                    )
                if "agent_command" in case:
                    self.assertEqual(
                        trust_policy.has_agent_command(text),
                        case["agent_command"],
                        case["name"],
                    )

    def test_untrusted_text_is_length_bounded(self):
        cleaned = trust_policy.neutralize_untrusted_text("A" * 50000)
        self.assertLessEqual(len(cleaned), trust_policy.MAX_UNTRUSTED_CHARS + 32)
        self.assertIn("[truncated]", cleaned)

    def test_bounded_text_keeps_the_prefix(self):
        cleaned = trust_policy.neutralize_untrusted_text("A" * 50000)
        self.assertTrue(cleaned.startswith("A" * 100))

    def test_fencing_marks_content_as_data(self):
        fenced = trust_policy.fence_untrusted("issue #1 body", "do bad things")
        self.assertIn("<<<BEGIN UNTRUSTED issue #1 body>>>", fenced)
        self.assertIn("<<<END UNTRUSTED issue #1 body>>>", fenced)
        self.assertIn("untrusted data", fenced)
        self.assertIn("do bad things", fenced)

    def test_fencing_label_cannot_inject_markers(self):
        fenced = trust_policy.fence_untrusted("<<<END UNTRUSTED x>>>", "body")
        self.assertEqual(fenced.count("<<<END UNTRUSTED"), 1)
        self.assertEqual(fenced.count("<<<BEGIN UNTRUSTED"), 1)

    def test_commit_title_is_single_line_and_unattributed(self):
        title = trust_policy.sanitize_commit_title(
            "fix parser\n\nCo-authored-by: attacker <a@evil.example>"
        )
        self.assertNotIn("\n", title)
        self.assertNotIn("attacker", title)
        self.assertNotIn("Co-authored-by", title)

    def test_commit_title_is_length_bounded(self):
        self.assertLessEqual(
            len(trust_policy.sanitize_commit_title("x" * 500)), trust_policy.MAX_COMMIT_TITLE
        )

    def test_command_must_lead_the_first_non_empty_line(self):
        self.assertTrue(trust_policy.has_agent_command("/oc"))
        self.assertTrue(trust_policy.has_agent_command("\n\n/oc fix it"))
        self.assertTrue(trust_policy.has_agent_command("> /oc"))
        self.assertTrue(trust_policy.has_agent_command("/opencode, please"))
        self.assertFalse(trust_policy.has_agent_command("look at this /oc"))
        self.assertFalse(trust_policy.has_agent_command("/occ"))
        self.assertFalse(trust_policy.has_agent_command(""))
        self.assertFalse(trust_policy.has_agent_command(None))


def referenced_fixture_names():
    """Fixture names the suite actually loads, read from the test sources.

    Every string literal in the suite is compared against the fixture directory,
    so this does not depend on test execution order or on which helper a
    particular test happens to load a fixture through.
    """
    on_disk = {path.stem for path in FIXTURE_DIR.glob("*.json")}
    literals = set()
    for path in sorted(pathlib.Path(__file__).resolve().parent.glob("test_*.py")):
        literals.update(re.findall(r'"([^"\n]+)"', path.read_text(encoding="utf-8")))
    return on_disk & literals


class DispatchCredentialTests(unittest.TestCase):
    """A write-capable credential must prove a trusted identity before use.

    The scheduler holds a PAT so its ``/oc`` comment is authored by a real
    account. The comment gate in the agent workflow only accepts a trusted
    author, so a credential that acts as ``github-actions[bot]`` cannot
    produce work: it would consume an issue reservation and dispatch nothing.
    """

    def run_check(self, login, **environ):
        captured = {}

        def fake_request(path, token, **kwargs):
            captured["path"] = path
            captured["token"] = token
            return {"login": login}

        env = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_TOKEN": "ghs_readonly",
        }
        env.update(environ)
        args = trust_policy.build_parser().parse_args(["check-token"])
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch.object(trust_policy, "api_request", fake_request):
            code = trust_policy.main(["check-token"])
        return code, captured

    def test_owner_credential_is_accepted(self):
        code, captured = self.run_check(OWNER)
        self.assertEqual(code, 0)
        self.assertEqual(captured["path"], "/user")

    def test_configured_actor_credential_is_accepted(self):
        code, _ = self.run_check(
            "release-bot", AUTOMATION_TRUSTED_ACTORS="release-bot, docs-bot"
        )
        self.assertEqual(code, 0)

    def test_unrelated_credential_fails_closed(self):
        code, _ = self.run_check("drive-by")
        self.assertEqual(code, 1)

    def test_bot_credential_fails_closed(self):
        for login in ("github-actions[bot]", "dependabot[bot]"):
            code, _ = self.run_check(login)
            self.assertEqual(code, 1, login)

    def test_missing_or_unidentifiable_credential_fails_closed(self):
        for login in (None, "", "   "):
            code, _ = self.run_check(login)
            self.assertEqual(code, 1, repr(login))

    def test_api_failure_fails_closed(self):
        def boom(path, token, **kwargs):
            raise trust_policy.TrustPolicyError("401 Unauthorized")

        env = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_TOKEN": "ghs_bad"}
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch.object(trust_policy, "api_request", boom):
            self.assertEqual(trust_policy.main(["check-token"]), 1)

    def test_unknown_repository_fails_closed(self):
        env = {"GITHUB_REPOSITORY": "", "GITHUB_TOKEN": "ghs_readonly"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(trust_policy.main(["check-token"]), 1)

    def test_identity_lookup_never_writes(self):
        """The one step allowed to hold the write token must not act on it."""
        source = inspect.getsource(trust_policy.resolve_token_login)
        self.assertIn("/user", source)
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertNotIn(verb, source, verb)


class GithubOutputEncodingTests(unittest.TestCase):
    def test_multiline_output_uses_github_delimiter_syntax(self):
        value = "70\tallow\tfirst\n69\tallow\tsecond"
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as handle:
            with unittest.mock.patch.dict(
                os.environ, {"GITHUB_OUTPUT": handle.name}, clear=True
            ):
                trust_policy.write_outputs({"merge_plan": value, "approved_count": "2"})
            handle.seek(0)
            lines = handle.read().splitlines()

        self.assertEqual(lines[0], "merge_plan<<__CONTINUUM_OUTPUT_EOF__")
        self.assertEqual(lines[1], "70\tallow\tfirst")
        self.assertEqual(lines[2], "69\tallow\tsecond")
        self.assertEqual(lines[3], "__CONTINUUM_OUTPUT_EOF__")
        self.assertEqual(lines[4], "approved_count=2")

    def test_multiline_output_avoids_delimiter_collision(self):
        value = "first\n__CONTINUUM_OUTPUT_EOF__\nlast"
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as handle:
            with unittest.mock.patch.dict(
                os.environ, {"GITHUB_OUTPUT": handle.name}, clear=True
            ):
                trust_policy.write_outputs({"merge_plan": value})
            handle.seek(0)
            lines = handle.read().splitlines()

        self.assertEqual(lines[0], "merge_plan<<__CONTINUUM_OUTPUT_EOF___")
        self.assertEqual(lines[-1], "__CONTINUUM_OUTPUT_EOF___")


class SchedulerIssueAllowlistTests(unittest.TestCase):
    """The scheduler's own filtering is a hint; the policy allowlist wins."""

    def run_listing(self, issues, **environ):
        env = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_TOKEN": "ghs_readonly",
            "GITHUB_OUTPUT": os.devnull,
        }
        env.update(environ)
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch.object(
                    trust_policy, "fetch_open_issues", lambda repository, token: issues
                ), \
                unittest.mock.patch("builtins.print"):
            return trust_policy.main(["trusted-issues"])

    def test_only_trusted_issues_are_listed(self):
        issues = [
            load_fixture("issue_comment_owner_on_owner_issue")["issue"],
            load_fixture("issue_comment_attacker_authored_issue")["issue"],
            load_fixture("issue_comment_attacker_on_owner_issue")["issue"],
        ]
        self.assertEqual(self.run_listing(issues), 0)

    def test_stranger_authored_issue_never_reaches_the_allowlist(self):
        issues = [load_fixture("issue_comment_attacker_authored_issue")["issue"]]
        with unittest.mock.patch.object(
            trust_policy, "write_outputs"
        ) as outputs:
            self.assertEqual(self.run_listing(issues), 0)
        written = outputs.call_args[0][0] if outputs.call_args else {}
        self.assertEqual(written.get("trusted_issues", ""), "")

    def test_owner_issue_is_listed(self):
        issues = [load_fixture("issue_comment_owner_on_owner_issue")["issue"]]
        with unittest.mock.patch.object(
            trust_policy, "write_outputs"
        ) as outputs:
            self.assertEqual(self.run_listing(issues), 0)
        written = outputs.call_args[0][0] if outputs.call_args else {}
        self.assertEqual(written.get("trusted_issue_count"), "1")
        self.assertTrue(written.get("trusted_issues", "").isdigit())

    def test_pull_requests_are_excluded(self):
        issue = copy.deepcopy(load_fixture("issue_comment_owner_on_owner_issue")["issue"])
        issue["pull_request"] = {"url": "https://api.github.com/x"}
        with unittest.mock.patch.object(
            trust_policy, "write_outputs"
        ) as outputs:
            self.assertEqual(self.run_listing([issue]), 0)
        written = outputs.call_args[0][0] if outputs.call_args else {}
        self.assertEqual(written.get("trusted_issues", ""), "")

    def test_read_only_token_is_required(self):
        issues = [load_fixture("issue_comment_owner_on_owner_issue")["issue"]]
        self.assertEqual(self.run_listing(issues, GITHUB_TOKEN=""), 1)

    def test_api_failure_fails_closed(self):
        def boom(repository, token):
            raise trust_policy.TrustPolicyError("403 Forbidden")

        env = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_TOKEN": "ghs_readonly",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch.object(trust_policy, "fetch_open_issues", boom):
            self.assertEqual(trust_policy.main(["trusted-issues"]), 1)


class CiRunProvenanceTests(unittest.TestCase):
    """A green run is only authorization if it provably came from this repo.

    A workflow run on a fork, or one whose triggering event and originating
    repository the API does not report, must never authorize an automatic
    merge. Missing provenance is treated as hostile, not as neutral.
    """

    def run_green(self, run):
        captured = {}

        def fake_request(path, token, **kwargs):
            captured["path"] = path
            return {"workflow_runs": [run]}

        with unittest.mock.patch.object(trust_policy, "api_request", fake_request):
            return trust_policy.list_ci_is_green(REPOSITORY, "a" * 40, "ghs", BASE_REF)

    def proven_run(self, **overrides):
        run = {
            "id": 1,
            "status": "completed",
            "conclusion": "success",
            "event": "pull_request",
            "head_branch": "opencode/issue7-fix",
            "head_repository": {"full_name": REPOSITORY},
        }
        run.update(overrides)
        return run

    def test_provably_own_green_run_authorizes(self):
        self.assertTrue(self.run_green(self.proven_run()))

    def test_fork_run_never_authorizes(self):
        self.assertFalse(
            self.run_green(
                self.proven_run(head_repository={"full_name": "attacker/continuum"})
            )
        )

    def test_missing_head_repository_fails_closed(self):
        run = self.proven_run()
        del run["head_repository"]
        self.assertFalse(self.run_green(run))
        self.assertFalse(self.run_green(self.proven_run(head_repository=None)))

    def test_missing_event_fails_closed(self):
        run = self.proven_run()
        del run["event"]
        self.assertFalse(self.run_green(run))
        for event in ("push", "workflow_dispatch", "schedule", ""):
            self.assertFalse(self.run_green(self.proven_run(event=event)), event)

    def test_non_agent_branch_run_never_authorizes(self):
        for branch in ("main", "feature/anything", "opencode/", "opencode/issue7"):
            self.assertFalse(self.run_green(self.proven_run(head_branch=branch)), branch)

    def test_failure_and_incomplete_runs_never_authorize(self):
        for override in (
            {"conclusion": "failure"},
            {"conclusion": None},
            {"status": "in_progress"},
        ):
            self.assertFalse(self.run_green(self.proven_run(**override)), repr(override))

    def test_no_runs_at_all_never_authorizes(self):
        with unittest.mock.patch.object(
            trust_policy, "api_request", lambda path, token, **kwargs: {}
        ):
            self.assertFalse(
                trust_policy.list_ci_is_green(REPOSITORY, "a" * 40, "ghs", BASE_REF)
            )

    def test_newest_run_decides(self):
        older = self.proven_run(id=1, conclusion="failure", run_started_at="2026-01-01T00:00:00Z")
        newer = self.proven_run(id=2, conclusion="success", run_started_at="2026-01-02T00:00:00Z")
        with unittest.mock.patch.object(
            trust_policy,
            "api_request",
            lambda path, token, **kwargs: {"workflow_runs": [older, newer]},
        ):
            self.assertTrue(
                trust_policy.list_ci_is_green(REPOSITORY, "a" * 40, "ghs", BASE_REF)
            )


class PolicyHttpTests(unittest.TestCase):
    """Exercise the policy against a real HTTP server.

    The unit tests patch ``api_request``, which skips URL construction, the
    Authorization header, and the HTTP method. Those are exactly the details
    that decide whether verification is genuinely read-only, so the CLI is run
    end to end against a local server that records every request it receives.
    """

    def setUp(self):
        self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _respond(self):
                self.server.requests.append((self.command, self.path))
                for prefix, payload in self.server.routes.items():
                    if self.path.startswith(prefix):
                        body = json.dumps(payload).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = _respond
            do_POST = _respond
            do_PATCH = _respond
            do_PUT = _respond
            do_DELETE = _respond

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = self.requests
        self.server.routes = self.routes()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.api_url = "http://127.0.0.1:{}".format(self.server.server_port)

    @property
    def pr_number(self):
        return str(load_fixture("pull_request_agent_branch")["pull_request"]["number"])

    def routes(self):
        agent_pr = load_fixture("pull_request_agent_branch")
        number = self.pr_number
        # Keys are matched as prefixes of the request path, so more specific
        # paths must come first: /pulls/44 would otherwise shadow
        # /pulls/44/files.
        return {
            "/user": {"login": OWNER},
            "/repos/{}/pulls/{}/files".format(REPOSITORY, number): [
                {"filename": name} for name in agent_pr["files"]
            ],
            "/repos/{}/pulls/{}".format(REPOSITORY, number): agent_pr["pull_request"],
            "/repos/{}/pulls?state=open".format(REPOSITORY): [agent_pr["pull_request"]],
            "/repos/{}/issues?state=open".format(REPOSITORY): [
                load_fixture("issue_comment_owner_on_owner_issue")["issue"],
                load_fixture("issue_comment_attacker_authored_issue")["issue"],
            ],
            "/repos/{}/actions/workflows/ci.yml/runs".format(REPOSITORY): {
                "workflow_runs": [
                    {
                        "id": 5,
                        "status": "completed",
                        "conclusion": "success",
                        "event": "pull_request",
                        "head_branch": agent_pr["pull_request"]["head"]["ref"],
                        "head_repository": {"full_name": REPOSITORY},
                        "run_started_at": "2026-01-01T00:00:00Z",
                    }
                ]
            },
        }

    def run_cli(self, argv, **environ):
        env = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_API_URL": self.api_url,
            "GITHUB_OUTPUT": os.devnull,
        }
        env.update(environ)
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            return trust_policy.main(argv)

    def test_every_verification_request_is_a_get(self):
        self.assertEqual(
            self.run_cli(["check-token"], GITHUB_TOKEN="ghs_pat"), 0
        )
        self.assertEqual(
            self.run_cli(["trusted-issues"], GITHUB_TOKEN="ghs_readonly"), 0
        )
        self.assertEqual(
            self.run_cli(
                [
                    "verify-dispatch",
                    "--mode",
                    "ci-fix",
                    "--pr-number",
                    self.pr_number,
                    "--head-ref",
                    load_fixture("pull_request_agent_branch")["pull_request"]["head"]["ref"],
                    "--run-id",
                    "5",
                ],
                GITHUB_TOKEN="ghs_readonly",
            ),
            0,
        )
        self.assertEqual(
            self.run_cli(["merge-plan"], GITHUB_TOKEN="ghs_readonly"), 0
        )
        methods = {method for method, _path in self.requests}
        self.assertEqual(methods, {"GET"}, methods)
        self.assertTrue(self.requests, "no requests were made")

    def test_credential_identity_is_resolved_over_http(self):
        self.assertEqual(self.run_cli(["check-token"], GITHUB_TOKEN="ghs_pat"), 0)
        self.assertIn(("GET", "/user"), self.requests)

    def test_untrusted_credential_fails_closed_over_http(self):
        self.server.routes["/user"] = {"login": "drive-by"}
        self.assertEqual(self.run_cli(["check-token"], GITHUB_TOKEN="ghs_pat"), 1)
        self.assertIn(("GET", "/user"), self.requests)

    def test_bot_credential_fails_closed_over_http(self):
        self.server.routes["/user"] = {"login": "github-actions[bot]"}
        self.assertEqual(self.run_cli(["check-token"], GITHUB_TOKEN="github.token"), 1)

    def test_forged_pull_request_is_refused_over_http(self):
        self.server.routes["/repos/{}/pulls/{}".format(REPOSITORY, self.pr_number)] = (
            load_fixture("pull_request_from_fork")["pull_request"]
        )
        code = self.run_cli(
            [
                "verify-dispatch",
                "--mode",
                "ci-fix",
                "--pr-number",
                self.pr_number,
                "--head-ref",
                load_fixture("pull_request_from_fork")["pull_request"]["head"]["ref"],
                "--run-id",
                "5",
            ],
            GITHUB_TOKEN="ghs_readonly",
        )
        self.assertEqual(code, 1)

    def test_forged_head_ref_for_a_real_pr_is_refused(self):
        code = self.run_cli(
            [
                "verify-dispatch",
                "--mode",
                "resolve-conflict",
                "--pr-number",
                self.pr_number,
                "--head-ref",
                "attacker/pwn",
            ],
            GITHUB_TOKEN="ghs_readonly",
        )
        self.assertEqual(code, 1)

    def test_dispatch_for_another_repository_fails_closed(self):
        # The PR the server serves is trusted here; asking about a different
        # repository must be refused before any request is trusted as proof.
        code = self.run_cli(
            [
                "verify-dispatch",
                "--mode",
                "ci-fix",
                "--pr-number",
                self.pr_number,
                "--head-ref",
                load_fixture("pull_request_agent_branch")["pull_request"]["head"]["ref"],
                "--run-id",
                "5",
            ],
            GITHUB_REPOSITORY="attacker/continuum",
            GITHUB_TOKEN="ghs_readonly",
        )
        self.assertEqual(code, 1)

    def test_fork_pull_request_is_refused_over_http(self):
        fork = load_fixture("pull_request_from_fork")
        routes = dict(self.routes())
        routes["/pulls/44"] = fork["pull_request"]
        with unittest.mock.patch.dict(trust_policy.api_request.__globals__, routes, clear=False):
            pass
        with unittest.mock.patch.object(
            trust_policy, "fetch_pull_request", lambda repository, number, token: fork["pull_request"]
        ):
            code = self.run_cli(
                [
                    "verify-dispatch",
                    "--mode",
                    "ci-fix",
                    "--pr-number",
                    self.pr_number,
                    "--head-ref",
                    fork["pull_request"]["head"]["ref"],
                    "--run-id",
                    "5",
                ],
                GITHUB_TOKEN="ghs_readonly",
            )
        self.assertEqual(code, 1)


class FixtureUsageTests(unittest.TestCase):
    def test_every_fixture_is_exercised(self):
        """A fixture nobody reads is not a regression test.

        This fails closed on a new fixture that no test consumes, and on a
        fixture that was deleted but is still referenced by a test.
        """
        on_disk = {path.stem for path in FIXTURE_DIR.glob("*.json")}
        referenced = referenced_fixture_names()
        self.assertTrue(referenced, "no fixtures are referenced by the suite")
        self.assertEqual(on_disk - referenced, set(), "unused fixtures")
        self.assertEqual(referenced - on_disk, set(), "missing fixture files")

    def test_mandated_threat_fixtures_exist(self):
        required = {
            "issue_comment_attacker_authored_issue",
            "issue_comment_attacker_on_owner_issue",
            "issue_comment_on_pull_request",
            "pull_request_from_fork",
            "pull_request_workflow_change",
            "workflow_dispatch_forged_head_ref",
            "untrusted_texts",
        }
        on_disk = {path.stem for path in FIXTURE_DIR.glob("*.json")}
        self.assertEqual(required - on_disk, set())


class RuledOutRungTests(unittest.TestCase):
    """`rungs_ruled_out` tells the repair ladder where to start.

    It is attacker-reachable input on a public repository, so the interesting
    cases are the malformed ones. Each must be *refused*: a raised exception
    here would abort the read-only verification step instead of declining the
    dispatch, and a step that crashes is not a gate.
    """

    BASE = {
        "mode": "resolve-conflict",
        "pr_number": "66",
        "head_ref": "opencode/issue56-research",
    }

    def test_a_refusal_returns_the_same_shape_as_an_allowance(self):
        for value in (
            "issue",
            "re-run-the-issue",
            "update-branch,issue",
            "UPDATE-BRANCH",
            "update-branch,",
            ",update-branch",
            "update-branch,,replay-commits",
            ["update-branch"],
            {"update-branch": True},
            7,
            "update-branch replay-commits",
            "update-branch\nissue",
        ):
            with self.subTest(value=value):
                result = trust_policy.validate_ruled_out_rungs(value)
                self.assertEqual(
                    len(result),
                    3,
                    "a refusal must unpack as (rungs, code, reason): {!r}".format(value),
                )
                rungs, code, reason = result
                self.assertEqual(rungs, ())
                self.assertEqual(code, "untrusted_ruled_out_rungs")
                self.assertTrue(reason)

    def test_nothing_to_declare_is_allowed(self):
        for value in (None, ""):
            self.assertEqual(trust_policy.validate_ruled_out_rungs(value), ((), "", ""))

    def test_the_mechanical_rungs_are_allowed_in_any_order_and_deduplicated(self):
        self.assertEqual(
            trust_policy.validate_ruled_out_rungs("replay-commits,update-branch"),
            (("update-branch", "replay-commits"), "", ""),
        )
        self.assertEqual(
            trust_policy.validate_ruled_out_rungs("update-branch,update-branch"),
            (("update-branch",), "", ""),
        )

    def test_the_allowlist_cannot_name_a_model_rung_or_a_task_rerun(self):
        # `resolve-hunks` is a real rung but the controller is what chooses it:
        # letting a payload pick it would skip the mechanical work for free.
        self.assertNotIn("resolve-hunks", trust_policy.RULABLE_OUT_RUNGS)
        _, code, _ = trust_policy.validate_ruled_out_rungs("resolve-hunks")
        self.assertEqual(code, "untrusted_ruled_out_rungs")

    def test_a_refused_value_propagates_through_the_dispatch_decision(self):
        decision = trust_policy.validate_dispatch_shape(
            dict(self.BASE, rungs_ruled_out="issue"), repository="kodmial/continuum"
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "untrusted_ruled_out_rungs")
        self.assertEqual(decision.ruled_out_rungs, ())


if __name__ == "__main__":
    unittest.main()
