"""The scheduler's eligibility filter is a liveness contract.

The dispatch step intersects two gates: it keeps owner-authored issues that the
trust policy named. The policy hands its answer over as one comma-separated
string of decimal numbers, so the scheduler's allowlist is a ``Set`` of
*strings* while ``issue.number`` is a *number*. That boundary is invisible in
YAML and in every security test: with a mismatched comparison the run still
succeeds, still logs a healthy trust verdict, and simply reports zero eligible
issues, forever, with nothing dispatched.

So the predicate and the allowlist it tests are lifted straight out of the
workflow and evaluated by Node here, against a real producer payload. A
regression to the number/string mix-up fails this suite rather than the backlog.

Run with::

    PYTHONPATH=src python3 -m unittest tests.test_scheduler_workflows -v
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / ".github" / "workflows" / "issue-scheduler.yml"
TRUST_POLICY = ROOT / ".github" / "scripts" / "trust_policy.py"
TRUST_FIXTURES = ROOT / ".github" / "tests" / "fixtures"

sys.path.insert(0, str(TRUST_POLICY.parent))

import trust_policy  # noqa: E402
import continuum_labels  # noqa: E402

#: The label the scheduler filters on, read from the same registry the workflow
#: renders it from, so this harness cannot drift from what the run checks.
DISPATCH_LABEL = continuum_labels.DISPATCH_LABEL

OWNER = "kodmial"
REPOSITORY = "kodmial/continuum"

# The scheduler's two eligibility expressions, written the way the workflow
# spells them. Both are read out of the YAML below rather than restated here, so
# a helper that can no longer find them fails loudly instead of testing a copy.
ALLOWLIST_BINDING = "const trustedIssueNumbers = "
ELIGIBILITY_FILTER = "issues = issues.filter("


def script_source(text: str) -> str:
    """The `github-script` body, which the workflow declares exactly once."""
    marker = "\n          script: |\n"
    start = text.index(marker) + len(marker)
    return text[start:]


def balanced_expression(text: str, start: int) -> str:
    """The parenthesized expression that opens at `start`.

    Quoted runs are skipped so a parenthesis inside a string literal cannot
    unbalance the scan; without that, a reworded comment would silently change
    which characters this reads and could extract a truncated expression.
    """
    depth = 0
    quote = None
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError("unterminated expression at offset {}".format(start))


def allowlist_expression(script: str) -> str:
    """`new Set(...)`, the workflow's own construction of the allowlist."""
    start = script.index(ALLOWLIST_BINDING) + len(ALLOWLIST_BINDING)
    return balanced_expression(script, start)


def eligibility_call(script: str) -> str:
    """The whole `issues.filter(...)` argument list, not just its first clause.

    Balanced-parenthesis scanning stops at the first close, which for a
    multi-clause predicate is somewhere in the middle of it. Reading only that
    prefix is worse than not testing the predicate at all: the filter would
    pass while the clause that actually decides eligibility -- the dispatch
    label -- was never evaluated.
    """
    start = script.index(ELIGIBILITY_FILTER) + len("issues = ")
    return balanced_expression(script, start)


def eligibility_predicate(script: str) -> str:
    """The arrow function the scheduler filters its backlog with."""
    start = script.index(ELIGIBILITY_FILTER) + len(ELIGIBILITY_FILTER)
    return balanced_expression(script, start)


def helper_function(script: str, name: str) -> str:
    """A `function name(...) {...}` declaration, lifted from the workflow."""
    start = script.index("function {}(".format(name))
    end = script.index("}", start)
    return script[start : end + 1]


def issue(number: int, login: str = OWNER, **extra) -> dict:
    payload = {
        "number": number,
        "state": "open",
        "user": {"login": login},
        "labels": [{"name": DISPATCH_LABEL}],
    }
    payload.update(extra)
    return payload


def eligible(issues, trusted_issues: str = "") -> list:
    """Issue numbers the scheduler's own filter keeps.

    The three expressions below are the workflow's: the allowlist it builds from
    `TRUSTED_ISSUES`, the predicate it filters with, and the owner it filters
    against. Only the surrounding harness is this suite's.
    """
    node = shutil.which("node")
    if node is None:
        raise AssertionError(
            "node is required to evaluate the scheduler's eligibility filter"
        )
    script = script_source(SCHEDULER.read_text(encoding="utf-8"))
    program = "\n".join(
        [
            "const owner = {};".format(json.dumps(OWNER)),
            "const trustedIssueNumbers = {};".format(allowlist_expression(script)),
            "const issues = {};".format(json.dumps(issues)),
            "const dispatchLabel = {};".format(json.dumps(DISPATCH_LABEL)),
            helper_function(script, "labelNames"),
            "const eligible = {};".format(eligibility_call(script)),
            "console.log(JSON.stringify(eligible.map(issue => issue.number)));",
        ]
    )
    # Only what the workflow's own expression reads is exported, so an ambient
    # TRUSTED_ISSUES in the developer's shell cannot decide what is eligible.
    environment = {
        "PATH": str(pathlib.Path(node).parent),
        "TRUSTED_ISSUES": trusted_issues,
    }
    result = subprocess.run(
        [node, "-e", program],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    return json.loads(result.stdout)


def policy_allowlist(issues) -> str:
    """The `trusted_issues` payload the trust policy actually writes.

    Produced by the real subcommand against fixture issues, so this asserts the
    producer's wire format and not a hand-written guess at it.
    """
    environment = {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_TOKEN": "ghs_readonly",
        "GITHUB_OUTPUT": "/dev/null",
    }
    with unittest.mock.patch.dict("os.environ", environment, clear=True), \
            unittest.mock.patch.object(
                trust_policy, "fetch_open_issues", lambda repository, token: issues
            ), \
            unittest.mock.patch.object(trust_policy, "write_outputs") as outputs, \
            unittest.mock.patch("builtins.print"):
        code = trust_policy.main(["trusted-issues"])
    if code != 0 or not outputs.called:
        raise AssertionError("the trust policy produced no trusted-issues payload")
    return outputs.call_args[0][0]["trusted_issues"]


def fixture(name: str) -> dict:
    with open(TRUST_FIXTURES / (name + ".json"), encoding="utf-8") as handle:
        return json.load(handle)


class EligibilityFilterTests(unittest.TestCase):
    """Both gates must hold, and only because both gates are compared in kind."""

    def test_a_trusted_owner_authored_issue_reaches_dispatch(self):
        # The reported incident: the policy trusted #37, the scheduler matched
        # nothing, and no /oc was ever posted.
        self.assertEqual(eligible([issue(37)], "37"), [37])

    def test_an_issue_the_policy_did_not_name_is_rejected(self):
        self.assertEqual(eligible([issue(37), issue(38)], "37"), [37])

    def test_a_stranger_authored_issue_is_rejected_even_when_trusted(self):
        # The author gate is independent of the allowlist and must survive the
        # fix: a trusted issue number is not an invitation to dispatch others.
        self.assertEqual(eligible([issue(37, login="stranger")], "37"), [])

    def test_a_pull_request_is_rejected_even_when_trusted(self):
        pull_request = issue(37, pull_request={"url": "https://api.github.com/x"})
        self.assertEqual(eligible([pull_request], "37"), [])

    def test_the_allowlist_survives_the_workflows_own_trimming(self):
        # The parser trims, so the payload need not be tidy.
        self.assertEqual(eligible([issue(37)], " 37 , 38 "), [37])

    def test_the_current_backlog_is_not_reduced_to_zero(self):
        # 28 trusted issues is the size of the backlog that produced
        # "28 trusted -> 0 eligible". Any loss here is the incident returning.
        trusted = [number for number in range(37, 65)]
        payload = ",".join(str(number) for number in trusted)
        self.assertEqual(
            eligible([issue(number) for number in trusted], payload), trusted
        )

    def test_the_real_policy_payload_reaches_the_real_filter(self):
        # End to end across the boundary that broke: the policy's own output
        # string, consumed by the scheduler's own allowlist and predicate.
        issues = [
            fixture("issue_owner_labeled"),
            fixture("issue_stranger_authored_labeled"),
            fixture("issue_on_pull_request"),
        ]
        payload = policy_allowlist(issues)
        self.assertEqual(payload, "44")
        self.assertEqual(eligible(issues, payload), [44])

    def test_an_unlabeled_issue_is_not_a_dispatch_candidate(self):
        # The hand-off is the label, so the filter has to read it. Removing the
        # label has to make the issue ineligible in the scheduler as well as in
        # the policy, or the two sides disagree about what is dispatched.
        unlabeled = issue(37, labels=[{"name": "bug"}])
        self.assertEqual(eligible([unlabeled], "37"), [])

    def test_the_label_is_matched_the_way_the_api_matches_it(self):
        # GitHub folds label case, so the scheduler folding it too is what keeps
        # a renamed label from looking like a withdrawn one.
        self.assertEqual(
            eligible([issue(37, labels=[{"name": "Continuum:Dispatch"}])], "37"),
            [37],
        )

    def test_a_label_near_miss_is_not_a_hand_off(self):
        for name in ("continuum:dispatcher", "continuum:dispatch " + "x", "dispatch"):
            self.assertEqual(eligible([issue(37, labels=[{"name": name}])], "37"), [], name)

    def test_the_empty_allowlist_dispatches_nothing(self):
        # Not an error, and still not a dispatch: an empty allowlist is a
        # legitimate outcome, and the run must stay quiet about it.
        self.assertEqual(eligible([issue(37)], ""), [])


class ExtractionTests(unittest.TestCase):
    """The harness must read the workflow, not a copy of it."""

    def setUp(self):
        self.script = script_source(SCHEDULER.read_text(encoding="utf-8"))

    def test_the_declarations_this_suite_depends_on_still_exist(self):
        self.assertIn(ALLOWLIST_BINDING, self.script)
        self.assertIn(ELIGIBILITY_FILTER, self.script)
        # The slice begins after the block scalar's marker, so the marker is
        # gone: what is left is the workflow's JavaScript, not YAML.
        self.assertNotIn("script: |", self.script)

    def test_the_allowlist_is_built_from_the_environment_string(self):
        expression = allowlist_expression(self.script)
        self.assertTrue(expression.startswith("new Set("), expression)
        self.assertIn("process.env.TRUSTED_ISSUES", expression)

    def test_the_predicate_compares_one_kind_at_a_time(self):
        # Read the whole filter argument, not its first clause: the allowlist
        # comparison is the second clause, and a prefix-only read would stop
        # exactly before it.
        predicate = eligibility_call(self.script)
        # The regression this suite exists for: a bare `issue.number` is a
        # number tested against a Set of strings, which is always false.
        self.assertNotRegex(predicate, r"has\(\s*issue\.number\s*\)")
        self.assertRegex(predicate, r"has\(\s*String\(issue\.number\)\s*\)")

    def test_the_predicate_requires_the_dispatch_label(self):
        # The scheduler's own eligibility rule, not only the policy's: an issue
        # the policy listed must still carry the label to be dispatched here.
        predicate = eligibility_call(self.script)
        self.assertIn("dispatchLabel", predicate)
        self.assertIn("toLowerCase()", predicate)
        self.assertNotIn(DISPATCH_LABEL, self.script)

    def test_a_parenthesis_inside_a_string_cannot_unbalance_the_scan(self):
        # `')'` inside a literal must not close the expression early, and a
        # genuinely unterminated one must not be truncated into the harness.
        self.assertEqual(balanced_expression("f(g(1), ')')", 1), "(g(1), ')')")
        with self.assertRaises(AssertionError):
            balanced_expression("f(g(1)", 1)

    def test_the_filter_argument_is_not_truncated_at_its_first_clause(self):
        # The failure this guards against is silent: the harness would evaluate
        # a strict subset of the predicate and still report a pass.
        predicate = eligibility_call(self.script)
        self.assertEqual(self.script.count("issues.filter("), 1)
        for clause in ("!issue.pull_request", "issue.user?.login === owner", "dispatchLabel"):
            self.assertIn(clause, predicate)

    def test_the_workflow_declares_exactly_one_script_body(self):
        text = SCHEDULER.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"^\s*script: \|", text, re.MULTILINE)), 1)


if __name__ == "__main__":
    unittest.main()
