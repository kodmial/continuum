"""End-to-end tests for the serialized CI-repair admission executable (#446).

The pure module tests prove the decision logic. These tests run the actual
production wrapper with a stateful fake ``gh`` so the ordering guarantees are
observed, not assumed:

* the claim comment is persisted before any dispatch request;
* one exact HEAD produces at most one dispatch across repeated reports;
* a new HEAD gets its own attempt;
* definite rejection, legacy 204, and ambiguous acceptance are reconciled
  without a blind second dispatch;
* a crashed claim is recovered only from a complete empty run inventory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "ci_repair_admission.py"
REPO = "Kodmial/Continuum"
PR = 446
HEAD = "a" * 40
NEW_HEAD = "b" * 40
NOW = "2026-11-12T01:00:00Z"
LEGACY_WRITERS_VARIABLE = "CONTINUUM_CI_REPAIR_WRITERS"
WRITER_VARIABLE_PREFIX = "CONTINUUM_CI_REPAIR_WRITER_"
WRITERS_BASELINE_VARIABLE = "CONTINUUM_CI_REPAIR_WRITERS_BASELINE"
LEGACY_RETIRED_VARIABLE = "CONTINUUM_CI_REPAIR_LEGACY_RETIRED_AT"
#: An already-established registry baseline and retirement record. Comment
#: fixtures are created after both, so an unproved receipt is a post-baseline
#: lookalike unless its author has trusted writer evidence.
WRITERS_BASELINE = "2026-11-11T00:00:00Z"
LEGACY_RETIRED = "2026-11-11T00:00:00Z"

FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

state = pathlib.Path(os.environ["FAKE_GH_STATE"])
args = sys.argv[1:]

with (state / "gh.log").open("a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")

token_login = os.environ.get("FAKE_TOKEN_LOGIN", "github-actions[bot]")


def read_json(name, default):
    path = state / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(name, value):
    (state / name).write_text(json.dumps(value), encoding="utf-8")


def output(status, body):
    reason = {200: "OK", 204: "No Content", 403: "Forbidden",
              422: "Unprocessable Entity"}.get(status, "Error")
    sys.stdout.write("HTTP/2.0 %d %s\r\n\r\n%s" % (status, reason, body))
    sys.exit(0 if status < 400 else 1)


if args[:1] != ["api"]:
    sys.exit(0)
rest = args[1:]

VARIABLE_NAME = "CONTINUUM_CI_REPAIR_WRITERS"
if any("/actions/variables" in arg for arg in rest):
    variables = read_json("variables.json", {})
    if os.environ.get("FAKE_VARIABLES_READ_FAIL") == "1":
        if rest[:1] == ["--paginate"]:
            sys.stderr.write("simulated variable list failure\n")
            sys.exit(1)
        if rest[:1] == ["--include"]:
            output(500, json.dumps({"message": "simulated variable read failure"}))
    if rest[:1] == ["--paginate"]:
        page = {
            "total_count": len(variables),
            "variables": [
                {"name": name, "value": value}
                for name, value in variables.items()
            ],
        }
        print(json.dumps([page]))
        sys.exit(0)
    if rest[:1] == ["--include"]:
        url = next((arg for arg in rest if arg.startswith("repos/")), "")
        name = url.rsplit("/", 1)[-1]
        if name not in variables:
            output(404, json.dumps({"message": "Not Found"}))
        output(200, json.dumps({"name": name, "value": variables[name]}))
    if rest[:1] == ["--method"] and rest[1:2] == ["POST"]:
        if os.environ.get("FAKE_VARIABLES_WRITE_FAIL") == "1":
            output(500, json.dumps({"message": "simulated variable write failure"}))
        name = next(
            (arg[len("name="):] for arg in rest if arg.startswith("name=")),
            VARIABLE_NAME,
        )
        value = next(
            (arg[len("value="):] for arg in rest if arg.startswith("value=")),
            "",
        )
        if name in variables:
            output(422, json.dumps({"message": "Variable already exists"}))
        variables[name] = value
        write_json("variables.json", variables)
        output(201, "{}")
    if rest[:1] == ["--method"] and rest[1:2] == ["PATCH"]:
        name = rest[2].rsplit("/", 1)[-1]
        value = next(
            (arg[len("value="):] for arg in rest if arg.startswith("value=")),
            "",
        )
        variables[name] = value
        write_json("variables.json", variables)
        output(204, "")
    sys.exit(0)

if rest[:1] == ["user"]:
    if os.environ.get("FAKE_TOKEN_FAIL") == "1":
        sys.stderr.write("simulated token identity failure\n")
        sys.exit(1)
    print(json.dumps({"login": token_login}))
    sys.exit(0)

if rest[:1] == ["--paginate"]:
    url = next((arg for arg in rest if arg.startswith("repos/")), "")
    if "/issues/" in url and "/comments" in url:
        print(json.dumps(read_json("comments_pages.json", [[]])))
        sys.exit(0)
    if "/actions/workflows/" in url and "/runs" in url:
        if os.environ.get("FAKE_RUNS_FAIL") == "1":
            sys.stderr.write("simulated run inventory failure\n")
            sys.exit(1)
        print(json.dumps(read_json("runs_pages.json", [])))
        sys.exit(0)
    sys.exit(0)

if rest[:1] == ["--method"] and rest[1:2] == ["POST"]:
    url = rest[2]
    if url.endswith("/comments"):
        body = next(
            (arg[len("body="):] for arg in rest if arg.startswith("body=")),
            "",
        )
        pages = read_json("comments_pages.json", [[]])
        flat = [item for page in pages for item in page]
        created = os.environ.get("FAKE_NOW", "2026-11-12T00:05:00Z")
        comment = {
            "id": max([item["id"] for item in flat], default=0) + 1,
            "body": body,
            "created_at": created,
            "updated_at": created,
            "author_association": "NONE",
            "user": {"login": token_login},
        }
        pages.append([comment])
        write_json("comments_pages.json", pages)
        with (state / "posted.jsonl").open("a", encoding="utf-8") as posted:
            posted.write(json.dumps(comment) + "\n")
        # Simulate the PR branch advancing between the persisted claim and the
        # dispatch request of the same serialized pass.
        if (
            os.environ.get("FAKE_ADVANCE_HEAD_AFTER_CLAIM") == "1"
            and "status=claimed" in body
        ):
            pr = read_json("pr.json", {})
            head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
            head["sha"] = os.environ.get("FAKE_ADVANCED_HEAD", "")
            pr["head"] = head
            write_json("pr.json", pr)
        print(json.dumps(comment))
        sys.exit(0)
    sys.exit(0)

if rest[:1] == ["--include"]:
    joined = " ".join(rest)
    if (
        os.environ.get("FAKE_REJECT_ATTEMPT_INPUTS") == "1"
        and "inputs[ci_repair_identity]" in joined
    ):
        output(422, json.dumps({"message": "Unexpected inputs provided: [\"ci_repair_identity\"]"}))
    if os.environ.get("FAKE_CREATE_MARKED_RUN") == "1":
        identity = next(
            (arg.split("=", 1)[1] for arg in rest
             if arg.startswith("inputs[ci_repair_identity]=")),
            "",
        )
        try:
            packed = json.loads(identity) if identity else {}
        except ValueError:
            packed = {}
        attempt = str(packed.get("ci_attempt_id") or "")
        pull = next(
            (arg.split("=", 1)[1] for arg in rest
             if arg.startswith("inputs[pr_number]=")),
            "0",
        )
        head = str(packed.get("ci_head_sha") or "")
        pages = read_json("runs_pages.json", [])
        runs = [item for page in pages for item in page.get("workflow_runs", [])]
        runs.append({
            "id": 700,
            "display_title": "OpenCode repair PR #{} [ci-attempt:{}]".format(pull, attempt),
            "status": "queued",
            "conclusion": "",
            "html_url": "https://github.com/%s/actions/runs/700" % os.environ.get(
                "GITHUB_REPOSITORY", ""
            ).lower(),
            "created_at": os.environ.get("FAKE_NOW", "2026-11-12T00:05:00Z"),
            "head_sha": head,
        })
        write_json("runs_pages.json", [{"total_count": len(runs), "workflow_runs": runs}])
    status = int(os.environ.get("FAKE_DISPATCH_STATUS", "200"))
    body = os.environ.get("FAKE_DISPATCH_BODY", '{"workflow_run_id": 501}')
    if status == 204:
        output(204, "")
    output(status, body)

if "/pulls/" in rest[-1]:
    print(json.dumps(read_json("pr.json", {})))
    sys.exit(0)

sys.exit(0)
'''


def trusted_receipt(
    head=HEAD,
    attempt="ci-crashed-0001",
    status="claimed",
    author=None,
    run_id=None,
    author_association="NONE",
):
    marker = (
        "<!-- continuum-ci-repair-receipt v=1 repo={} pr={} head={} "
        "attempt={} status={}".format(REPO.lower(), PR, head, attempt, status)
    )
    if run_id is not None:
        marker += " run={} url=https://github.com/{}/actions/runs/{}".format(
            run_id, REPO.lower(), run_id
        )
    marker += " -->"
    return {
        "id": 1,
        "body": marker,
        "created_at": "2026-11-12T00:00:00Z",
        "updated_at": "2026-11-12T00:00:00Z",
        "author_association": author_association,
        "user": {"login": author or "github-actions[bot]"},
    }


class AdmissionExecutableTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(
            tempfile.mkdtemp(prefix="ci-repair-admission-", dir=str(self._tmp_root()))
        )
        self.addCleanup(shutil.rmtree, self.dir, True)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)
        self.bin = bin_dir
        self.write_pr(head=HEAD, comments=0)
        self.write_state("comments_pages.json", [[]])
        self.write_state("runs_pages.json", [])
        self.write_state("posted.jsonl", "")
        self.write_state("gh.log", "")
        # Lookalike classification only applies once the trusted retirement
        # record says older controllers stopped writing. Tests that exercise the
        # migration/overlap cases remove that record again.
        self.write_state(
            "variables.json",
            {
                WRITERS_BASELINE_VARIABLE: WRITERS_BASELINE,
                LEGACY_RETIRED_VARIABLE: LEGACY_RETIRED,
            },
        )

    @staticmethod
    def _tmp_root():
        root = ROOT / ".opencode-tmp"
        root.mkdir(exist_ok=True)
        return root

    def write_state(self, name, value):
        path = self.dir / name
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            path.write_text(json.dumps(value), encoding="utf-8")

    def write_pr(self, *, head=HEAD, comments=0, state="open"):
        self.write_state(
            "pr.json",
            {
                "state": state,
                "comments": comments,
                "head": {
                    "ref": "opencode/issue446-1",
                    "sha": head,
                    "repo": {"full_name": REPO},
                },
            },
        )

    def write_comments(self, comments):
        self.write_state("comments_pages.json", [list(comments)])

    def write_runs(self, runs):
        self.write_state(
            "runs_pages.json",
            [{"total_count": len(runs), "workflow_runs": list(runs)}],
        )

    def run_admission(self, *, head=HEAD, extra_env=None, extra_args=()):
        env = {
            "PATH": "{}:{}".format(self.bin, os.environ.get("PATH", "")),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GH_TOKEN": "test-token",
            "GITHUB_REPOSITORY": REPO,
            "FAKE_GH_STATE": str(self.dir),
            "FAKE_NOW": "2026-11-12T00:05:00Z",
            "FAKE_TOKEN_LOGIN": "github-actions[bot]",
        }
        env.update(extra_env or {})
        arguments = [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--repository",
            REPO,
            "--pull-number",
            str(PR),
            "--head-sha",
            head,
            "--head-ref",
            "opencode/issue446-1",
            "--workflow",
            "continuum-opencode.yml",
            "--failing-run-id",
            "1234",
            "--reported-run-id",
            "1234",
            "--owner-login",
            "kodmial",
            *extra_args,
        ]
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)
        return completed

    def posts(self):
        return [
            json.loads(line)
            for line in (self.dir / "posted.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def posted_bodies(self):
        return [entry["body"] for entry in self.posts()]

    def log(self):
        return (self.dir / "gh.log").read_text(encoding="utf-8").splitlines()

    def dispatches(self):
        return [line for line in self.log() if "inputs[mode]=ci-fix" in line]

    def variables(self):
        path = self.dir / "variables.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def writer_logins(self):
        logins = []
        for name, value in self.variables().items():
            if name != LEGACY_WRITERS_VARIABLE and not name.startswith(
                WRITER_VARIABLE_PREFIX
            ):
                continue
            for login in str(value).replace(",", " ").split():
                if login and login not in logins:
                    logins.append(login)
        return logins

    def admission_module(self):
        spec = importlib.util.spec_from_file_location(
            "ci_repair_admission_under_test", SCRIPT
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def assert_rotated_writer_receipt_is_not_redispatched(
        self, *, status, run_id=None, run_head=HEAD
    ):
        # The previous TAP_PAT account only left a claimed/ambiguous receipt, or
        # a dispatched receipt without run details. GitHub's actual run still
        # proves that account created this exact-HEAD attempt.
        attempt = "ci-old-account-0001"
        self.write_comments(
            [
                trusted_receipt(
                    attempt=attempt,
                    status=status,
                    author="old-automation",
                    run_id=run_id,
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs(
            [
                {
                    "id": 900,
                    "display_title": "OpenCode repair PR #{} [ci-attempt:{}]".format(
                        PR, attempt
                    ),
                    "status": "queued",
                    "conclusion": "",
                    "html_url": "https://github.com/{}/actions/runs/900".format(
                        REPO.lower()
                    ),
                    "created_at": "2026-11-12T00:00:00Z",
                    "head_sha": run_head,
                    "actor": {"login": "old-automation"},
                }
            ]
        )
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertIn("verified by its exact-HEAD dispatch run", completed.stdout)

    def test_a_previous_token_account_claim_is_never_redispatched(self):
        self.assert_rotated_writer_receipt_is_not_redispatched(status="claimed")

    def test_a_previous_token_account_ambiguous_receipt_is_never_redispatched(self):
        self.assert_rotated_writer_receipt_is_not_redispatched(status="ambiguous")

    def test_a_previous_token_account_unrun_dispatch_is_never_redispatched(self):
        self.assert_rotated_writer_receipt_is_not_redispatched(
            status="dispatched"
        )

    def test_a_dispatch_ref_run_keeps_the_previous_writer_authority(self):
        # A workflow_dispatch run reports the dispatch ref (normally main) as
        # its SHA, not the admitted PR HEAD. The previous writer has no
        # repository association after token rotation, so only the attempt
        # marker and actor can preserve its receipt.
        self.assert_rotated_writer_receipt_is_not_redispatched(
            status="claimed", run_head="c" * 40
        )

    def test_a_legacy_unmarked_run_keeps_the_unrun_dispatch_authority(self):
        # A legacy caller produces no attempt marker, and a 204 response may
        # leave the dispatched receipt without a run ID. Token rotation must
        # still recognize that writer from GitHub's canonical repair run.
        attempt = "ci-old-account-0001"
        self.write_comments(
            [
                trusted_receipt(
                    attempt=attempt,
                    status="dispatched",
                    author="old-automation",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs(
            [
                {
                    "id": 900,
                    "display_title": "OpenCode repair PR #{}".format(PR),
                    "status": "queued",
                    "conclusion": "",
                    "html_url": "https://github.com/{}/actions/runs/900".format(
                        REPO.lower()
                    ),
                    "created_at": "2026-11-12T00:00:00Z",
                    "head_sha": HEAD,
                    "actor": {"login": "old-automation"},
                }
            ]
        )
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertIn("verified by its exact-HEAD dispatch run", completed.stdout)

    def test_an_unmarked_dispatch_ref_run_holds_instead_of_redispatched(self):
        # A legacy unmarked title cannot bind a run at the dispatch-ref SHA to
        # the receipt's PR HEAD. The old writer has no association after token
        # rotation, so admission must hold rather than dispatch a duplicate.
        attempt = "ci-old-account-0001"
        self.write_comments(
            [
                trusted_receipt(
                    attempt=attempt,
                    status="dispatched",
                    author="old-automation",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs(
            [
                {
                    "id": 900,
                    "display_title": "OpenCode repair PR #{}".format(PR),
                    "status": "queued",
                    "conclusion": "",
                    "html_url": "https://github.com/{}/actions/runs/900".format(
                        REPO.lower()
                    ),
                    "created_at": "2026-11-12T00:00:00Z",
                    "head_sha": "c" * 40,
                    "actor": {"login": "old-automation"},
                }
            ]
        )
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_a_plausible_historical_writer_without_a_proving_run_holds(self):
        # A collaborator-owned previous TAP_PAT leaves receipts that look
        # authoritative but have no run to prove them. Admission must hold
        # instead of treating the spent exact HEAD as unused.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="claimed",
                    author="old-automation",
                    author_association="MEMBER",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs([])
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_a_deleted_previous_writer_run_stays_unverifiable(self):
        # The previous TAP_PAT account was recorded as a trusted writer before
        # rotation, but its repair run has since been deleted. The durable
        # registry still makes the old receipt plausible, so admission holds
        # instead of dispatching the same exact HEAD again.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="dispatched",
                    author="old-automation",
                    run_id=900,
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_state(
            "variables.json",
            {
                LEGACY_WRITERS_VARIABLE: "old-automation",
                WRITERS_BASELINE_VARIABLE: WRITERS_BASELINE,
            },
        )
        self.write_runs([])
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)
        writers = self.writer_logins()
        self.assertIn("old-automation", writers)
        self.assertIn("github-actions[bot]", writers)

    def test_a_missing_registry_with_a_deleted_run_holds_instead_of_redispatched(self):
        # The registry did not exist when the previous TAP_PAT wrote its
        # receipt, and its repair run was deleted before migration. Nothing can
        # prove that writer, so a missing baseline must hold rather than treat
        # the spent exact HEAD as unused and dispatch again.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="dispatched",
                    author="old-automation",
                    run_id=900,
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_state("variables.json", {})
        self.write_runs([])
        completed = self.run_admission(
            extra_args=("--now", "2026-11-12T00:05:00Z")
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)
        self.assertIn(WRITERS_BASELINE_VARIABLE, self.variables())

        # The baseline is durable: a later pass must not reclassify the same
        # pre-baseline receipt as an unrelated lookalike.
        completed = self.run_admission(
            extra_args=("--now", "2026-11-12T00:10:00Z")
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_a_pre_baseline_claim_with_a_deleted_run_holds_instead_of_redispatched(self):
        # A previous writer dispatched a repair successfully, but persisting the
        # outcome failed, so only its claim remains. The writer was never
        # registered, its account now has no repository association, and its run
        # was deleted. That is indistinguishable from a historical writer, so
        # the missing registry must hold instead of dispatching the same exact
        # HEAD again.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="claimed",
                    author="old-automation",
                    author_association="NONE",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_state("variables.json", {})
        self.write_runs([])
        completed = self.run_admission(
            extra_args=("--now", "2026-11-12T00:05:00Z")
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)
        self.assertIn(WRITERS_BASELINE_VARIABLE, self.variables())

        # The baseline is durable: a later pass must not reclassify the same
        # pre-baseline claim as an unrelated lookalike and dispatch again.
        completed = self.run_admission(
            extra_args=("--now", "2026-11-12T00:10:00Z")
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_a_pre_baseline_claim_from_a_plausible_writer_still_holds(self):
        # Repository write access is GitHub-attested evidence that the author
        # could be the previous automation account, even though its run is gone.
        # The migration frontier must not downgrade that writer to a lookalike.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="claimed",
                    author="old-automation",
                    author_association="MEMBER",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_state("variables.json", {})
        self.write_runs([])
        completed = self.run_admission(
            extra_args=("--now", "2026-11-12T00:05:00Z")
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_a_receipt_at_the_baseline_timestamp_holds_instead_of_redispatched(self):
        # The frontier has second precision, so a receipt created in the same
        # second as the baseline is ambiguous, not newer. Its missing run must
        # hold instead of authorizing a duplicate dispatch.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="dispatched",
                    author="old-automation",
                    run_id=900,
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_state(
            "variables.json",
            {WRITERS_BASELINE_VARIABLE: "2026-11-12T00:00:00Z"},
        )
        self.write_runs([])
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_a_post_baseline_legacy_claim_from_an_overlapping_controller_holds(self):
        # A new controller can establish the baseline while an older controller
        # for another exact HEAD is still running (or remains pinned). That
        # older controller writes an unproved receipt after the baseline. Its
        # token account still has repository write access, which is the trusted
        # evidence that separates it from an unrelated commenter, so the new
        # controller must hold instead of treating the spent HEAD as unused.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-overlap-0001",
                    status="claimed",
                    author="old-automation",
                    author_association="COLLABORATOR",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs([])
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

        # A later pass must keep the same receipt unverifiable rather than
        # reclassify it as a post-baseline lookalike.
        completed = self.run_admission(
            extra_args=("--now", "2026-11-12T00:10:00Z")
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_a_post_baseline_legacy_dispatch_from_an_overlapping_controller_holds(self):
        # The older overlapping controller can also have dispatched before
        # writing its receipt. That receipt must hold even though its creation
        # time is after the established baseline, because its token account
        # still has repository write access.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-overlap-0001",
                    status="dispatched",
                    author="old-automation",
                    run_id=900,
                    author_association="COLLABORATOR",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs([])
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_a_post_baseline_none_association_legacy_claim_holds_until_retirement(self):
        # Exact migration gap: an older controller writes a claim after the
        # baseline without registering its account, the token later rotates, the
        # account has no association, and its repair run is deleted. Until a
        # trusted retirement record proves that older controller is gone, the
        # receipt must hold instead of being treated as a lookalike and
        # authorizing a duplicate dispatch for the same exact HEAD.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-overlap-0001",
                    status="claimed",
                    author="old-automation",
                    author_association="NONE",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_state(
            "variables.json", {WRITERS_BASELINE_VARIABLE: WRITERS_BASELINE}
        )
        self.write_runs([])
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

        # A retirement record that still predates the receipt does not retire
        # the controller that wrote it, so the hold is unchanged.
        self.write_state(
            "variables.json",
            {
                WRITERS_BASELINE_VARIABLE: WRITERS_BASELINE,
                LEGACY_RETIRED_VARIABLE: "2026-11-12T00:00:00Z",
            },
        )
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

        # Once the trusted record shows all legacy controllers were retired
        # before the receipt, the same unproved NONE receipt is a lookalike and
        # must not suppress a repair.
        self.write_state(
            "variables.json",
            {
                WRITERS_BASELINE_VARIABLE: WRITERS_BASELINE,
                LEGACY_RETIRED_VARIABLE: LEGACY_RETIRED,
            },
        )
        self.run_admission()
        self.assertEqual(1, len(self.dispatches()))

    def test_a_post_baseline_none_association_legacy_dispatch_holds_until_retirement(self):
        # The older overlapping controller can also have dispatched before
        # writing its receipt. With no association, no registry entry, and a
        # deleted run, only the trusted retirement record can separate it from a
        # lookalike; without that record it must hold.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-overlap-0001",
                    status="dispatched",
                    author="old-automation",
                    run_id=900,
                    author_association="NONE",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_state(
            "variables.json", {WRITERS_BASELINE_VARIABLE: WRITERS_BASELINE}
        )
        self.write_runs([])
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

        self.write_state(
            "variables.json",
            {
                WRITERS_BASELINE_VARIABLE: WRITERS_BASELINE,
                LEGACY_RETIRED_VARIABLE: LEGACY_RETIRED,
            },
        )
        self.run_admission()
        self.assertEqual(1, len(self.dispatches()))

    def test_an_unusable_legacy_retirement_holds_without_dispatching(self):
        self.write_state(
            "variables.json",
            {
                WRITERS_BASELINE_VARIABLE: WRITERS_BASELINE,
                LEGACY_RETIRED_VARIABLE: "not-a-timestamp",
            },
        )
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("retirement record cannot be read", completed.stdout)

    def test_the_authenticated_writer_is_recorded_for_future_rotations(self):
        self.run_admission()
        self.assertIn("github-actions[bot]", self.writer_logins())

    def test_a_registry_write_failure_holds_without_dispatching(self):
        completed = self.run_admission(
            extra_env={"FAKE_VARIABLES_WRITE_FAIL": "1"}
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("not durably recorded", completed.stdout)

    def test_a_baseline_write_failure_holds_without_dispatching(self):
        # Without a durable migration frontier, pre-registry receipts cannot be
        # distinguished from lookalikes, so admission must not proceed.
        self.write_state("variables.json", {})
        completed = self.run_admission(
            extra_env={"FAKE_VARIABLES_WRITE_FAIL": "1"}
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("baseline is not durably recorded", completed.stdout)

    def test_an_unusable_baseline_holds_without_dispatching(self):
        self.write_state(
            "variables.json", {WRITERS_BASELINE_VARIABLE: "not-a-timestamp"}
        )
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("baseline cannot be read", completed.stdout)

    def test_a_registry_read_failure_holds_without_dispatching(self):
        completed = self.run_admission(
            extra_env={"FAKE_VARIABLES_READ_FAIL": "1"}
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be read", completed.stdout)
        # Recording the current writer is still attempted, so a later pass can
        # recover once the registry API is readable again.
        self.assertIn("github-actions[bot]", self.writer_logins())

    def test_a_missing_registry_migrates_a_verified_writer(self):
        # The registry predates this code in production. Bootstrap it from a
        # writer GitHub can prove, then keep that writer authoritative after
        # the token rotates and the proving run is deleted.
        attempt = "ci-old-account-0001"
        self.write_comments(
            [
                trusted_receipt(
                    attempt=attempt,
                    status="claimed",
                    author="old-automation",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_state("variables.json", {})
        self.write_runs(
            [
                {
                    "id": 900,
                    "display_title": "OpenCode repair PR #{} [ci-attempt:{}]".format(
                        PR, attempt
                    ),
                    "status": "queued",
                    "conclusion": "",
                    "html_url": "https://github.com/{}/actions/runs/900".format(
                        REPO.lower()
                    ),
                    "created_at": "2026-11-12T00:00:00Z",
                    "head_sha": HEAD,
                    "actor": {"login": "old-automation"},
                }
            ]
        )
        completed = self.run_admission(
            extra_args=("--now", "2026-11-12T00:05:00Z")
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertIn("verified by its exact-HEAD dispatch run", completed.stdout)
        self.assertIn("old-automation", self.writer_logins())
        self.assertIn("github-actions[bot]", self.writer_logins())
        self.assertIn(WRITERS_BASELINE_VARIABLE, self.variables())

        self.write_runs([])
        completed = self.run_admission(
            extra_args=("--now", "2026-11-12T00:10:00Z"),
            extra_env={"FAKE_TOKEN_LOGIN": "new-automation"},
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertIn("cannot be verified", completed.stdout)
        self.assertIn("old-automation", self.writer_logins())

    def test_concurrent_writer_registrations_cannot_overwrite_each_other(self):
        # Two exact-HEAD passes read the same stale registry and then register
        # different writers. A read-merge-write variable would let the second
        # write erase the first; independent per-writer variables must not.
        module = self.admission_module()
        env = {
            "PATH": "{}:{}".format(self.bin, os.environ.get("PATH", "")),
            "GH_TOKEN": "test-token",
            "FAKE_GH_STATE": str(self.dir),
            "FAKE_TOKEN_LOGIN": "github-actions[bot]",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            known_a, registered_a, healthy_a = (
                module._historical_writer_registry(REPO)
            )
            known_b, registered_b, healthy_b = (
                module._historical_writer_registry(REPO)
            )
            self.assertTrue(healthy_a)
            self.assertTrue(healthy_b)
            _, durable_a = module._persist_historical_writers(
                REPO, ("old-a",), known=known_a, registered=registered_a
            )
            _, durable_b = module._persist_historical_writers(
                REPO, ("old-b",), known=known_b, registered=registered_b
            )
            self.assertTrue(durable_a)
            self.assertTrue(durable_b)
            known, registered, healthy = module._historical_writer_registry(REPO)
        self.assertTrue(healthy)
        self.assertIn("old-a", known)
        self.assertIn("old-b", known)
        self.assertIn("old-a", registered)
        self.assertIn("old-b", registered)

    def test_a_same_head_run_actor_without_a_proof_holds_admission(self):
        # Even when GitHub reports no repository association, an account that
        # created a run for this exact HEAD is a plausible historical writer.
        # Its unproved receipt must hold, not authorize or suppress a repair.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="claimed",
                    author="old-automation",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs(
            [
                {
                    "id": 901,
                    "display_title": "OpenCode repair PR #{} [ci-attempt:{}]".format(
                        PR, "ci-other-0001"
                    ),
                    "status": "queued",
                    "conclusion": "",
                    "html_url": "https://github.com/{}/actions/runs/901".format(
                        REPO.lower()
                    ),
                    "created_at": "2026-11-12T00:00:00Z",
                    "head_sha": HEAD,
                    "actor": {"login": "old-automation"},
                }
            ]
        )
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_a_legacy_claim_lookalike_cannot_suppress_a_repair(self):
        # A lookalike receipt from an account with no repository write access,
        # no writer registration, and no same-HEAD run is not a plausible
        # historical writer. The receipt format cannot separate it from an
        # older controller either, because only the trusted writer evidence
        # does; without that evidence it must not turn an unused exact HEAD
        # into a permanent hold.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-lookalike-0001",
                    status="claimed",
                    author="passer-by",
                    author_association="NONE",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs([])
        self.run_admission()
        self.assertEqual(1, len(self.dispatches()))

    def test_a_legacy_dispatch_lookalike_cannot_suppress_a_repair(self):
        # The same applies to a copied dispatched receipt with run details that
        # no GitHub run record proves.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-lookalike-0001",
                    status="dispatched",
                    author="passer-by",
                    run_id=900,
                    author_association="NONE",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs([])
        self.run_admission()
        self.assertEqual(1, len(self.dispatches()))

    def test_unverifiable_historical_receipt_history_holds_without_claiming(self):
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="claimed",
                    author="old-automation",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        completed = self.run_admission(extra_env={"FAKE_RUNS_FAIL": "1"})
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_incomplete_historical_run_inventory_holds_without_claiming(self):
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="claimed",
                    author="old-automation",
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_state(
            "runs_pages.json",
            [{"total_count": 2, "workflow_runs": []}],
        )
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())
        self.assertIn("cannot be verified", completed.stdout)

    def test_first_failure_persists_the_claim_before_exactly_one_dispatch(self):
        self.run_admission()
        bodies = self.posted_bodies()
        self.assertTrue(any("status=claimed" in body for body in bodies), bodies)
        self.assertTrue(
            any("status=dispatched" in body and "run=501" in body for body in bodies),
            bodies,
        )
        self.assertEqual(1, len(self.dispatches()))
        claim_index = next(
            index for index, line in enumerate(self.log()) if "status=claimed" in line
        )
        dispatch_index = next(
            index for index, line in enumerate(self.log()) if "inputs[mode]=ci-fix" in line
        )
        self.assertLess(claim_index, dispatch_index)
        self.assertIn("return_run_details=true", self.dispatches()[0])
        self.assertIn("inputs[ci_repair_identity]=", self.dispatches()[0])

    def test_a_second_report_for_the_same_head_never_dispatches_again(self):
        self.run_admission()
        before = len(self.posts())
        self.run_admission()
        self.assertEqual(1, len(self.dispatches()), "the same exact HEAD dispatched twice")
        self.assertEqual(before, len(self.posts()), "a waiting pass must not mutate receipts")

    def test_a_new_head_gets_its_own_attempt(self):
        self.run_admission()
        self.write_pr(head=NEW_HEAD)
        self.run_admission(head=NEW_HEAD)
        self.assertEqual(2, len(self.dispatches()))
        claimed_heads = [
            body for body in self.posted_bodies() if "status=claimed" in body
        ]
        self.assertEqual(2, len(claimed_heads))
        self.assertTrue(any("head=" + NEW_HEAD in body for body in claimed_heads))

    def test_old_head_records_do_not_block_a_new_head(self):
        self.write_comments([trusted_receipt(head=HEAD)])
        self.write_pr(head=NEW_HEAD)
        self.run_admission(head=NEW_HEAD)
        self.assertEqual(1, len(self.dispatches()))
        self.assertTrue(
            any("head=" + NEW_HEAD in body and "status=claimed" in body
                for body in self.posted_bodies())
        )

    def test_optional_input_rejection_falls_back_without_a_duplicate_dispatch(self):
        self.run_admission(extra_env={"FAKE_REJECT_ATTEMPT_INPUTS": "1"})
        dispatches = self.dispatches()
        self.assertEqual(
            3,
            len(dispatches),
            "expected the combined shape, the input-only probe, and one fallback",
        )
        self.assertIn("inputs[ci_repair_identity]=", dispatches[0])
        self.assertIn("inputs[ci_repair_identity]=", dispatches[1])
        self.assertNotIn("inputs[ci_repair_identity]=", dispatches[2])
        self.assertTrue(
            any("status=dispatched" in body and "run=501" in body
                for body in self.posted_bodies())
        )

    def test_a_definite_rejection_is_recorded_and_not_retried_in_the_same_pass(self):
        self.run_admission(
            extra_env={
                "FAKE_DISPATCH_STATUS": "422",
                "FAKE_DISPATCH_BODY": '{"message":"Validation Failed"}',
            }
        )
        self.assertEqual(1, len(self.dispatches()))
        bodies = self.posted_bodies()
        self.assertTrue(any("status=rejected" in body for body in bodies), bodies)
        self.assertFalse(any("status=dispatched" in body for body in bodies), bodies)

    def test_ambiguous_acceptance_holds_and_a_later_pass_never_dispatches(self):
        self.run_admission(
            extra_env={
                "FAKE_DISPATCH_STATUS": "403",
                "FAKE_DISPATCH_BODY": '{"message":"rate limited"}',
            }
        )
        self.assertEqual(1, len(self.dispatches()))
        self.assertTrue(
            any("status=ambiguous" in body for body in self.posted_bodies())
        )
        self.run_admission()
        self.assertEqual(1, len(self.dispatches()), "ambiguous acceptance was retried blindly")

    def test_legacy_204_is_correlated_to_the_actual_marked_run(self):
        # GitHub created the run but the legacy dispatch response carried no
        # details. The fake gh materializes the marked run on dispatch; the
        # admission must correlate it and persist run details.
        self.run_admission(
            extra_env={
                "FAKE_DISPATCH_STATUS": "204",
                "FAKE_DISPATCH_BODY": "",
                "FAKE_CREATE_MARKED_RUN": "1",
            }
        )
        bodies = self.posted_bodies()
        self.assertTrue(any("status=dispatched" in body for body in bodies), bodies)
        self.assertTrue(any("run=700" in body for body in bodies), bodies)

    def test_a_stale_crashed_claim_is_recovered_once_from_complete_evidence(self):
        self.write_comments([trusted_receipt()])
        self.write_pr(head=HEAD, comments=1)
        self.run_admission(extra_args=("--now", NOW))
        bodies = self.posted_bodies()
        self.assertTrue(any("status=rejected" in body for body in bodies), bodies)
        self.assertTrue(any("status=claimed" in body for body in bodies), bodies)
        self.assertEqual(1, len(self.dispatches()))
        # The stale recovery is the resumed watchdog: it re-lists runs after
        # the claim observation. That listing must stay an idempotent GET; a
        # field alone would make ``gh api`` send a POST and break recovery.
        run_lists = [
            line for line in self.log()
            if "/actions/workflows/" in line and "/runs" in line
        ]
        self.assertTrue(run_lists, self.log())
        self.assertTrue(
            any("--method GET" in line and "created=>=2026-11-12T00:00:00Z" in line
                for line in run_lists),
            run_lists,
        )
        self.assertFalse(
            any("--method POST" in line for line in run_lists),
            "workflow-run reconciliation must never POST a listing",
        )

    def test_a_canceled_controller_is_resumed_once_by_the_next_serialized_pass(self):
        # The claim was persisted, then the controller run was canceled before
        # it could record a dispatch. The next serialized pass is the resumed
        # watchdog: it rejects the dead claim and dispatches once, and every
        # following pass waits.
        self.write_comments([trusted_receipt(status="claimed")])
        self.write_pr(head=HEAD, comments=1)
        self.run_admission(extra_args=("--now", NOW))
        self.assertEqual(1, len(self.dispatches()))
        bodies = self.posted_bodies()
        self.assertTrue(any("status=rejected" in body for body in bodies), bodies)
        self.assertTrue(any("status=claimed" in body for body in bodies), bodies)
        self.run_admission(extra_args=("--now", "2026-11-13T00:00:00Z"))
        self.assertEqual(1, len(self.dispatches()), "the resumed pass duplicated repair")

    def test_a_receipt_authored_by_the_authenticated_token_is_trusted(self):
        # A collaborator-owned PAT is a supported TAP_PAT shape. Receipts it
        # wrote carry COLLABORATOR/NONE association, so only the authenticated
        # token login can prove they are our own attempts. If that identity is
        # lost, a later pass would treat its own claim as untrusted and
        # dispatch the same exact HEAD a second time.
        self.write_comments([trusted_receipt(author="some-collaborator")])
        self.write_pr(head=HEAD, comments=1)
        self.run_admission(
            extra_env={"FAKE_TOKEN_LOGIN": "some-collaborator"},
            extra_args=("--now", NOW),
        )
        self.run_admission(extra_env={"FAKE_TOKEN_LOGIN": "some-collaborator"})
        self.assertEqual(1, len(self.dispatches()))

    def test_an_unprovable_token_identity_holds_without_a_claim(self):
        self.run_admission(extra_env={"FAKE_TOKEN_FAIL": "1"})
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())

    def test_a_stale_claim_with_unreadable_inventory_becomes_a_visible_hold(self):
        self.write_comments([trusted_receipt()])
        self.write_pr(head=HEAD, comments=1)
        self.run_admission(
            extra_args=("--now", NOW),
            extra_env={"FAKE_RUNS_FAIL": "1"},
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertTrue(
            any("status=ambiguous" in body for body in self.posted_bodies())
        )

    def test_a_stale_claim_with_an_unattributable_run_becomes_a_visible_hold(self):
        self.write_comments([trusted_receipt()])
        self.write_pr(head=HEAD, comments=1)
        self.write_runs(
            [
                {
                    "id": 800,
                    "display_title": "OpenCode repair PR #{}".format(PR),
                    "status": "queued",
                    "conclusion": "",
                    "html_url": "https://github.com/{}/actions/runs/800".format(REPO.lower()),
                    "created_at": "2026-11-12T00:10:00Z",
                    "head_sha": HEAD,
                }
            ]
        )
        self.run_admission(extra_args=("--now", NOW))
        self.assertEqual(0, len(self.dispatches()))
        self.assertTrue(
            any("status=ambiguous" in body for body in self.posted_bodies())
        )

    def test_a_closed_pr_is_a_safe_no_op(self):
        self.write_pr(head=HEAD, state="closed")
        self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertEqual([], self.posts())

    def test_untrusted_receipt_authors_cannot_suppress_a_dispatch(self):
        # An unrelated commenter with no writer evidence is a lookalike whether
        # or not its receipt copies a dispatched shape.
        spoof = trusted_receipt()
        spoof["user"] = {"login": "somebody"}
        spoof["author_association"] = "CONTRIBUTOR"
        self.write_comments([spoof])
        self.write_pr(head=HEAD, comments=1)
        self.run_admission()
        self.assertEqual(1, len(self.dispatches()))

    def test_dispatch_is_withheld_when_the_head_advances_after_the_claim(self):
        completed = self.run_admission(
            extra_env={
                "FAKE_ADVANCE_HEAD_AFTER_CLAIM": "1",
                "FAKE_ADVANCED_HEAD": NEW_HEAD,
            }
        )
        self.assertEqual(0, len(self.dispatches()))
        self.assertIn("dispatch withheld", completed.stdout)
        self.assertTrue(
            any("status=claimed" in body and "head=" + HEAD in body
                for body in self.posted_bodies())
        )
        # The persisted claim stays bound to its own exact HEAD, so the newer
        # HEAD still admits its own single attempt.
        self.run_admission(head=NEW_HEAD)
        self.assertEqual(1, len(self.dispatches()))
        self.assertTrue(
            any("status=claimed" in body and "head=" + NEW_HEAD in body
                for body in self.posted_bodies())
        )

    def test_a_previous_token_account_dispatch_is_never_redispatched(self):
        # TAP_PAT rotated to another account: the previous account's dispatched
        # receipt is no longer trusted by login alone. GitHub's run record
        # proves that account created this exact-HEAD run for this attempt, so
        # admission must keep waiting instead of dispatching a second repair.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="dispatched",
                    author="old-automation",
                    run_id=900,
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs(
            [
                {
                    "id": 900,
                    "display_title": "OpenCode repair PR #{} [ci-attempt:ci-old-account-0001]".format(PR),
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/{}/actions/runs/900".format(REPO.lower()),
                    "created_at": "2026-11-12T00:00:00Z",
                    "head_sha": HEAD,
                    "actor": {"login": "old-automation"},
                }
            ]
        )
        completed = self.run_admission()
        self.assertEqual(0, len(self.dispatches()))
        self.assertIn("verified by its exact-HEAD dispatch run", completed.stdout)

    def test_a_foreign_run_cannot_authenticate_an_untrusted_receipt(self):
        # The same receipt shape without GitHub attesting that account as the
        # run's actor is an unverified lookalike and must not block admission.
        self.write_comments(
            [
                trusted_receipt(
                    attempt="ci-old-account-0001",
                    status="dispatched",
                    author="old-automation",
                    run_id=900,
                )
            ]
        )
        self.write_pr(head=HEAD, comments=1)
        self.write_runs(
            [
                {
                    "id": 900,
                    "display_title": "OpenCode repair PR #{} [ci-attempt:ci-old-account-0001]".format(PR),
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/{}/actions/runs/900".format(REPO.lower()),
                    "created_at": "2026-11-12T00:00:00Z",
                    "head_sha": HEAD,
                    "actor": {"login": "somebody-else"},
                }
            ]
        )
        completed = self.run_admission()
        self.assertEqual(1, len(self.dispatches()))
        self.assertNotIn("verified by its exact-HEAD dispatch run", completed.stdout)


if __name__ == "__main__":
    unittest.main()
