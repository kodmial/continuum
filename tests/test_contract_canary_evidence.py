"""Tests for the GitHub evidence reader shared by the canary workflow."""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
import zipfile
from unittest import mock

from continuum import contract_canary
from continuum import contract_canary_evidence as evidence

REPO = contract_canary.CANARY_OWNER_REPO
RUN_ID = 9001
PR_NUMBER = 41
BRANCH = "opencode/issue100-9001"
COMMIT = "a" * 40
PARENT = "b" * 40
OTHER_COMMIT = "c" * 40


def gh_result(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def publication_receipt(head, base=PARENT, branch=BRANCH, run_id=RUN_ID):
    return {
        "schema_version": 1,
        "repo": REPO,
        "run_id": str(run_id),
        "run_attempt": "1",
        "pr_number": PR_NUMBER,
        "branch": branch,
        "base_sha": base,
        "head_sha": head,
    }


def receipt_zip(payload):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("publication.json", json.dumps(payload))
    return buffer.getvalue()


class GhFailureTest(unittest.TestCase):
    def test_failed_read_fails_closed_with_the_canonical_diagnostic(self):
        with mock.patch.object(
            evidence.subprocess,
            "run",
            return_value=gh_result(returncode=1, stderr="boom"),
        ):
            with self.assertRaisesRegex(
                SystemExit,
                "could not read publication artifacts for run 9001: boom",
            ):
                evidence.worker_publication(REPO, RUN_ID)


class ImplementationCommitsTest(unittest.TestCase):
    def test_commit_history_and_repair_statuses_are_read_together(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            target = argv[-1]
            if "pulls/41/commits?per_page=100" in target:
                return gh_result(
                    json.dumps([[{"sha": COMMIT, "parents": [{"sha": PARENT}]}]])
                )
            if "commits/{}/statuses".format(COMMIT) in target:
                return gh_result(
                    json.dumps(
                        [
                            [
                                {
                                    "context": (
                                        contract_canary
                                        .CANARY_REPAIR_STATUS_CONTEXT
                                    ),
                                    "state": "success",
                                    "description": "pr=41 base={} head={}".format(
                                        PARENT[:12], COMMIT[:12]
                                    ),
                                    "creator": {"login": "kodmial"},
                                    "target_url": (
                                        "https://github.com/{}/actions/runs/{}"
                                        .format(REPO, RUN_ID)
                                    ),
                                }
                            ]
                        ]
                    )
                )
            if "actions/runs/9001/artifacts?per_page=100" in target:
                # An expired receipt cannot attest anything.
                return gh_result(
                    "7\tcontinuum-repair-publication-1\ttrue\n"
                )
            raise AssertionError("unexpected gh call: {}".format(argv))

        with mock.patch.object(
            evidence.subprocess, "run", side_effect=fake_run
        ):
            commits = evidence.implementation_commits(REPO, PR_NUMBER, BRANCH)

        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["sha"], COMMIT)
        self.assertEqual(
            commits[0]["parents"], [{"sha": PARENT}]
        )
        self.assertEqual(len(commits[0]["statuses"]), 1)
        self.assertIsNone(commits[0]["statuses"][0]["publication"])
        self.assertTrue(
            any("--slurp" in call for call in calls)
        )

    def test_missing_pr_returns_no_commits_without_calling_github(self):
        with mock.patch.object(evidence.subprocess, "run") as run:
            self.assertEqual(evidence.implementation_commits(REPO, 0, BRANCH), [])
        run.assert_not_called()

    def test_unparsable_history_fails_closed(self):
        with mock.patch.object(
            evidence.subprocess, "run", return_value=gh_result("not-json")
        ):
            with self.assertRaisesRegex(
                SystemExit, "could not be parsed"
            ):
                evidence.implementation_commits(REPO, PR_NUMBER, BRANCH)


class WorkerPublicationTest(unittest.TestCase):
    def test_absent_or_expired_receipt_is_none(self):
        for listing in (
            "",
            "7\tcontinuum-task-publication-1\ttrue\n",
            "8\tsome-other-artifact\tfalse\n",
        ):
            with self.subTest(listing=listing):
                with mock.patch.object(
                    evidence.subprocess,
                    "run",
                    return_value=gh_result(listing),
                ):
                    self.assertIsNone(
                        evidence.worker_publication(REPO, RUN_ID)
                    )

    def test_worker_receipt_is_downloaded_and_parsed(self):
        payload = publication_receipt(COMMIT)

        def fake_run(argv, **kwargs):
            if "artifacts?per_page=100" in argv[-1]:
                return gh_result(
                    "7\tcontinuum-task-publication-1\tfalse\n"
                )
            if argv[:3] == ["gh", "run", "download"]:
                target = argv[argv.index("--dir") + 1]
                with open(
                    os.path.join(target, "publication.json"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    json.dump(payload, handle)
                return gh_result("")
            raise AssertionError("unexpected gh call: {}".format(argv))

        with mock.patch.object(
            evidence.subprocess, "run", side_effect=fake_run
        ):
            self.assertEqual(
                evidence.worker_publication(REPO, RUN_ID), payload
            )

    def test_unreadable_receipt_fails_closed(self):
        def fake_run(argv, **kwargs):
            if "artifacts?per_page=100" in argv[-1]:
                return gh_result(
                    "7\tcontinuum-task-publication-1\tfalse\n"
                )
            return gh_result("")

        with mock.patch.object(
            evidence.subprocess, "run", side_effect=fake_run
        ):
            with self.assertRaisesRegex(SystemExit, "is unreadable"):
                evidence.worker_publication(REPO, RUN_ID)


class RepairPublicationTest(unittest.TestCase):
    def commit(self, sha=COMMIT, parent=PARENT):
        return {"sha": sha, "parents": [{"sha": parent}], "statuses": []}

    def test_no_repair_receipt_is_none(self):
        with mock.patch.object(
            evidence.subprocess,
            "run",
            return_value=gh_result(""),
        ):
            self.assertIsNone(
                evidence.repair_publication(
                    REPO, RUN_ID, self.commit(), PR_NUMBER, BRANCH
                )
            )

    def test_matching_receipt_is_selected_by_artifact_id(self):
        matching = publication_receipt(COMMIT)
        later = publication_receipt(OTHER_COMMIT)
        def fake_run(argv, **kwargs):
            if "artifacts?per_page=100" in argv[-1]:
                return gh_result(
                    "8\tcontinuum-repair-publication-8\tfalse\n"
                    "9\tcontinuum-repair-publication-9\tfalse\n"
                )
            if "actions/artifacts/8/zip" in argv[-1]:
                return gh_result(receipt_zip(matching))
            if "actions/artifacts/9/zip" in argv[-1]:
                return gh_result(receipt_zip(later))
            raise AssertionError("unexpected gh call: {}".format(argv))

        with mock.patch.object(
            evidence.subprocess, "run", side_effect=fake_run
        ):
            chosen = evidence.repair_publication(
                REPO, RUN_ID, self.commit(), PR_NUMBER, BRANCH
            )
        self.assertEqual(chosen, matching)

    def test_corrupt_receipt_archive_fails_closed(self):
        def fake_run(argv, **kwargs):
            if "artifacts?per_page=100" in argv[-1]:
                return gh_result(
                    "8\tcontinuum-repair-publication-8\tfalse\n"
                )
            return gh_result(b"not-a-zip")

        with mock.patch.object(
            evidence.subprocess, "run", side_effect=fake_run
        ):
            with self.assertRaisesRegex(SystemExit, "cannot be read"):
                evidence.repair_publication(
                    REPO, RUN_ID, self.commit(), PR_NUMBER, BRANCH
                )


if __name__ == "__main__":
    unittest.main()
