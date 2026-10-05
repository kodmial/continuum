"""Contract tests for the #259 credential-actor matrix.

Every same-repo mutation historically on TAP_PAT is classified in
``src/continuum/credential_matrix.py``. This suite proves the shipped
workflows honor that classification:

* migrated paths authenticate with ``github.token`` (github-actions[bot]);
* retained PAT paths still use TAP_PAT and carry a documented reason;
* no new user-created secret is introduced;
* reusable/caller permissions prove no elevation (caller grants each
  migrated write permission);
* no workflow/event chain is lost (every migrated entry declares
  needs_chaining=False while at least one retained chaining path exists);
* no global token replacement exists (both verdicts are present).
"""

from __future__ import annotations

import os
import re
import unittest

from continuum import credential_matrix as matrix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(ROOT, ".github", "workflows")
STUBS_DIR = os.path.join(ROOT, ".github", "caller-stubs")

SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")
ALLOWED_SECRETS = set(matrix.ALLOWED_SECRETS) | {"GITHUB_TOKEN"}

# Map matrix permission tokens to the secret-free caller permission keys.
PERMISSION_KEYS = ("actions", "contents", "issues", "pull-requests", "statuses")


def read_workflow(name):
    with open(os.path.join(WORKFLOWS_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def code_without_comment(line):
    """Strip a trailing `#` comment outside quotes (mirrors contract tests)."""
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index]
    return line


def step_window(text, step, span=6000):
    """Return the owning step block for `step`, not a blind char window.

    A fixed ±span slice bleeds into neighboring steps and doc comments that
    merely mention TAP_PAT, which is exactly how a migrated step looks
    non-compliant. Instead, slice from the marker line through the next step
    boundary (`      - name:` / `      - uses:` / job boundary), falling back
    to the bounded window only when no step structure is found.
    """
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if step in line),
        None,
    )
    assert start is not None, f"step marker gone: {step}"
    # Matrix entries may intentionally name a job-level mutation site
    # rather than a named step. Detect that structurally instead of using a
    # length heuristic: adding a legitimate job-level guard/comment must not
    # make the helper stop at the first child step and hide its evidence.
    if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[start]):
        job_end = len(lines)
        for i in range(start + 1, len(lines)):
            if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[i]) or re.match(r"^jobs:\s*$", lines[i]):
                job_end = i
                break
        block = "\n".join(lines[start:job_end])
    else:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            line = lines[i]
            if line.startswith("      - name: ") or line.startswith("      - uses: "):
                end = i
                break
            if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line) or re.match(r"^jobs:\s*$", line):
                end = i
                break
        block = "\n".join(lines[start:end])
    if len(block) > span * 2:
        block = block[: span * 2]
    return block


def credential_assignment_uses_pat(block):
    """True when executable (non-comment) YAML assigns TAP_PAT as a credential."""
    for line in block.splitlines():
        code = code_without_comment(line)
        stripped = code.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        if "secrets.TAP_PAT" not in code:
            continue
        # A doc comment mentioning TAP_PAT was already stripped above; what
        # remains is a real credential binding (token:/github-token:/GH_TOKEN:/
        # GITHUB_TOKEN:/TAP_PAT:/auth:).
        if re.search(
            r"(token|github-token|GH_TOKEN|GITHUB_TOKEN|TAP_PAT|auth)\s*:",
            code,
        ):
            return True
    return False


class MatrixShapeTests(unittest.TestCase):
    def test_matrix_is_valid(self):
        self.assertTrue(matrix.validate())

    def test_both_verdicts_present(self):
        self.assertTrue(matrix.migrated_entries(), "no GITHUB_TOKEN migration")
        self.assertTrue(matrix.retained_entries(), "global token replacement")

    def test_every_retained_entry_has_a_reason(self):
        for entry in matrix.retained_entries():
            with self.subTest(workflow=entry["workflow"], step=entry["step"]):
                self.assertTrue(entry["reason"].strip())
                self.assertEqual(entry["verdict"], matrix.KEEP_PAT)


class MigratedActorTests(unittest.TestCase):
    def test_migrated_paths_use_github_token(self):
        for entry in matrix.migrated_entries():
            with self.subTest(workflow=entry["workflow"], step=entry["step"]):
                text = read_workflow(entry["workflow"])
                # Step locality: every evidence literal must sit inside the
                # owning step block, not merely anywhere in the file where an
                # unrelated `github.token` could satisfy the check. The span
                # covers the largest migrated step end to end so tail evidence
                # (e.g. a dispatch helper) is not truncated away.
                window = step_window(text, entry["step"], span=20000)
                for evidence in entry["evidence"]:
                    self.assertIn(
                        evidence, window,
                        f"{entry['workflow']}::{entry['step']}: missing {evidence!r} in owning step",
                    )

    def test_migrated_steps_do_not_use_tap_pat_for_the_migrated_call(self):
        # The add-review-label split is the only migrated step that still
        # legitimately binds TAP_PAT (its PAT dispatch client). Every other
        # migrated step must not bind TAP_PAT as a credential; doc-comment
        # mentions are ignored by credential_assignment_uses_pat.
        for entry in matrix.migrated_entries():
            with self.subTest(workflow=entry["workflow"], step=entry["step"]):
                text = read_workflow(entry["workflow"])
                window = step_window(text, entry["step"], span=20000)
                if entry["workflow"] == "continuum-add-review-label.yml":
                    self.assertIn("patClient()", window)
                    # Split-actor step: label mutations must stay on the
                    # GITHUB_TOKEN client while workflow dispatches stay
                    # PAT-backed. Checking only `patClient()` presence would
                    # let label writes regress to PAT (or dispatches regress
                    # to GITHUB_TOKEN) without failing.
                    self.assertIn("github.rest.issues", window)
                    self.assertNotIn(
                        "patClient().rest.issues", window,
                        f"{entry['workflow']}::{entry['step']}: label writes must use github.rest.issues, not patClient()",
                    )
                    self.assertIn(
                        "patClient().rest.actions.createWorkflowDispatch", window,
                        f"{entry['workflow']}::{entry['step']}: dispatches must use patClient()",
                    )
                    self.assertNotIn(
                        "github.rest.actions.createWorkflowDispatch", window,
                        f"{entry['workflow']}::{entry['step']}: dispatches must not use the GITHUB_TOKEN client",
                    )
                    continue
                if "TARGET_IS_DELEGATED" in window or "target-aware" in window.lower():
                    # Conditional target-aware token: TAP_PAT appears only on
                    # the delegated branch of the expression.
                    self.assertIn("github.token", window)
                    continue
                self.assertFalse(
                    credential_assignment_uses_pat(window),
                    f"{entry['workflow']}::{entry['step']}: migrated path still binds TAP_PAT",
                )

    def test_no_migrated_entry_depends_on_chaining(self):
        for entry in matrix.migrated_entries():
            with self.subTest(workflow=entry["workflow"], step=entry["step"]):
                self.assertFalse(entry["needs_chaining"])
                self.assertFalse(entry["cross_repo"])
                self.assertFalse(entry["needs_push_dispatch"])


class RetainedActorTests(unittest.TestCase):
    def test_retained_paths_keep_pat(self):
        for entry in matrix.retained_entries():
            with self.subTest(workflow=entry["workflow"], step=entry["step"]):
                text = read_workflow(entry["workflow"])
                window = step_window(text, entry["step"], span=8000)
                self.assertIn(
                    "secrets.TAP_PAT", window,
                    f"{entry['workflow']}::{entry['step']}: retained PAT use lost its credential",
                )

    def test_retained_paths_document_why(self):
        for entry in matrix.retained_entries():
            with self.subTest(workflow=entry["workflow"], step=entry["step"]):
                text = read_workflow(entry["workflow"])
                # The reason is documented either inline in the workflow or in
                # the matrix reason itself (asserted non-empty above); at
                # least the matrix must name the mechanism.
                self.assertTrue(
                    any(
                        keyword in entry["reason"].lower()
                        for keyword in (
                            "chain", "dispatch", "push", "trigger",
                            "suppress", "delegat", "cross-repo", "merge",
                            "permission", "exactly-once", "fan out",
                        )
                    ),
                    f"{entry['workflow']}::{entry['step']}: reason does not name the mechanism",
                )
                self.assertTrue(text.strip())

    def test_chaining_paths_are_retained(self):
        chaining = [e for e in matrix.ENTRIES if e["needs_chaining"]]
        self.assertTrue(chaining, "no chaining path classified")
        for entry in chaining:
            with self.subTest(workflow=entry["workflow"], step=entry["step"]):
                self.assertEqual(entry["verdict"], matrix.KEEP_PAT)


class PermissionAndSecretTests(unittest.TestCase):
    def test_no_new_user_secret(self):
        for name in sorted(os.listdir(WORKFLOWS_DIR)):
            if not name.startswith("continuum-") or not name.endswith(".yml"):
                continue
            text = read_workflow(name)
            for secret in SECRET_RE.findall(text):
                with self.subTest(workflow=name, secret=secret):
                    self.assertIn(
                        secret, ALLOWED_SECRETS,
                        f"{name}: new user secret secrets.{secret} is not allowed by #259",
                    )

    def test_callers_grant_migrated_permissions(self):
        rank = {"none": 0, "read": 1, "write": 2}
        for entry in matrix.migrated_entries():
            with self.subTest(workflow=entry["workflow"], step=entry["step"]):
                permission = entry.get("permission", "")
                if ":" not in permission:
                    continue
                key, level = permission.split(":", 1)
                if key not in PERMISSION_KEYS:
                    continue
                stub = os.path.join(STUBS_DIR, entry["workflow"])
                if not os.path.exists(stub):
                    continue  # engine-only workflow without an installed caller
                import yaml

                with open(stub, encoding="utf-8") as handle:
                    caller = yaml.safe_load(handle)
                granted = str(caller.get("permissions", {}).get(key, "none"))
                self.assertGreaterEqual(
                    rank[granted], rank[level],
                    f"{entry['workflow']}: caller grants {key}={granted} "
                    f"but migrated path needs {level}",
                )


if __name__ == "__main__":
    unittest.main()
