"""Cron-only autonomous dispatch regression tests (kodmial/continuum#280).

NanoDictate issue #156 (open P0, unblocked, eligible since 2026-10-03)
received no automatic reservation or dispatch across many scheduled
reconciler passes and stayed idle until an owner manually posted ``/oc``
on 2026-10-05. The manual command immediately reserved the issue,
proving the consumer issue-comment path was alive while the automatic
reconcile/backstop made zero progress -- and every such pass still
reported success.

These tests pin the executable reconcile contract in
``src/continuum/scheduler_reconcile.py`` (which mirrors the
``continuum-issue-scheduler.yml`` github-script semantics):

- a lone eligible P0 is selected, reserved, and dispatched exactly once
  with no repository event after creation (cron-only recovery);
- a second reconcile is idempotent and does not duplicate dispatch;
- stale reservations expire and failed dispatches roll back and retry
  under the existing bounded/backoff policy;
- WIP starvation names every waiter and holder and fails instead of
  reporting success;
- priority ordering and Blocked-By semantics are preserved.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from continuum.scheduler_reconcile import (
    CommentRecord,
    IssueState,
    SchedulerConfig,
    reconcile,
)

try:
    from tests.scheduler_workflow import scheduler_workflow_text as scheduler_body
except ModuleNotFoundError:
    from scheduler_workflow import scheduler_workflow_text as scheduler_body


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def p0_issue(number: int = 156) -> IssueState:
    return IssueState(
        number=number,
        title="Fix cancellation races and Homebrew quarantine regression",
        body="## Priority\n\nP0 repair task.",
        labels={"priority:p0"},
        is_open=True,
    )


class CronOnlyRecoveryTests(unittest.TestCase):
    """The #156 fixture: no event after creation, cron must recover alone."""

    def test_eligible_p0_selected_reserved_and_dispatched_once(self):
        config = SchedulerConfig()
        issues = {156: p0_issue()}
        comments: dict = {}
        now = utcnow()

        first = reconcile(
            now, issues, set(), set(), comments, {}, {}, set(), config
        )

        self.assertEqual(first.dispatched, [156])
        self.assertEqual(first.reservations_added, [156])
        self.assertIn(config.in_progress_label, issues[156].labels)
        markers = [
            c for c in comments[156] if config.dispatch_marker in c.body
        ]
        self.assertEqual(len(markers), 1)
        self.assertFalse(first.failed)
        # Every non-dispatched issue would carry a reason; here nothing waits.
        self.assertEqual(first.skip_reasons, {})

    def test_active_run_suppresses_dispatch_across_repeated_reconciles(self):
        config = SchedulerConfig()
        issues = {156: p0_issue()}
        comments: dict = {}
        now = utcnow()

        for offset in (0, 1):
            result = reconcile(
                now + timedelta(minutes=offset),
                issues,
                set(),
                {156},
                comments,
                {},
                {},
                set(),
                config,
            )
            self.assertEqual(result.dispatched, [])
            self.assertIn(config.in_progress_label, issues[156].labels)
            self.assertFalse(result.failed)

        self.assertEqual(comments.get(156, []), [])

    def test_open_pr_suppresses_dispatch_across_repeated_reconciles(self):
        config = SchedulerConfig(count_open_prs_as_wip=False)
        issues = {156: p0_issue()}
        comments: dict = {}
        now = utcnow()

        for offset in (0, 1):
            result = reconcile(
                now + timedelta(minutes=offset),
                issues,
                {156},
                set(),
                comments,
                {},
                {},
                set(),
                config,
            )
            self.assertEqual(result.dispatched, [])
            self.assertIn(config.in_progress_label, issues[156].labels)
            self.assertIn("open implementation PR must not be duplicated", result.skip_reasons[156])
            self.assertFalse(result.failed)

        self.assertEqual(comments.get(156, []), [])

    def test_second_reconcile_is_idempotent(self):
        config = SchedulerConfig()
        issues = {156: p0_issue()}
        comments: dict = {}
        now = utcnow()

        reconcile(now, issues, set(), set(), comments, {}, {}, set(), config)
        second = reconcile(
            now + timedelta(minutes=1),
            issues, set(), set(), comments, {}, {}, set(), config
        )

        self.assertEqual(second.dispatched, [])
        markers = [
            c for c in comments[156] if config.dispatch_marker in c.body
        ]
        self.assertEqual(len(markers), 1)
        # The held reservation explains the skip deterministically.
        self.assertIn(156, second.skip_reasons)
        self.assertIn("WIP slot held", second.skip_reasons[156])
        self.assertFalse(second.failed)


class StaleReservationAndRetryTests(unittest.TestCase):
    def test_lease_expiry_releases_and_retries_automatically(self):
        config = SchedulerConfig(lease_minutes=45, max_dispatch_attempts=2)
        now = utcnow()
        issues = {156: p0_issue()}
        issues[156].labels.add(config.in_progress_label)
        comments = {
            156: [
                CommentRecord(
                    body=config.dispatch_marker + "\nAutomatic dispatch.",
                    author_is_owner=True,
                    created_at=now - timedelta(minutes=60),
                )
            ]
        }

        result = reconcile(
            now, issues, set(), set(), comments, {}, {}, set(), config
        )

        # Stale lease released, then the same pass reselects and redispatches.
        self.assertIn(156, result.reservations_removed)
        self.assertEqual(result.dispatched, [156])
        self.assertIn(config.in_progress_label, issues[156].labels)
        markers = [
            c for c in comments[156] if config.dispatch_marker in c.body
        ]
        self.assertEqual(len(markers), 2)
        self.assertNotIn(config.pause_label, issues[156].labels)

    def test_reservation_without_dispatch_record_is_released_immediately(self):
        config = SchedulerConfig()
        now = utcnow()
        issues = {156: p0_issue()}
        # A manual/stale reservation with no dispatch evidence (e.g. the
        # comment post failed and the label leaked, or a human added it).
        issues[156].labels.add(config.in_progress_label)

        result = reconcile(
            now, issues, set(), set(), {}, {}, {}, set(), config
        )

        self.assertIn(156, result.reservations_removed)
        # Released stale state makes the issue dispatchable again: the same
        # pass releases the stale lease and redispatches (both are logged).
        self.assertEqual(result.dispatched, [156])

    def test_dead_reservation_recovers_then_replay_is_idempotent(self):
        config = SchedulerConfig()
        now = utcnow()
        issues = {156: p0_issue()}
        issues[156].labels.add(config.in_progress_label)
        comments: dict = {}

        recovered = reconcile(
            now, issues, set(), set(), comments, {}, {}, set(), config
        )
        self.assertIn(156, recovered.reservations_removed)
        self.assertEqual(recovered.dispatched, [156])
        self.assertIn(config.in_progress_label, issues[156].labels)

        replay = reconcile(
            now + timedelta(minutes=1),
            issues,
            set(),
            set(),
            comments,
            {},
            {},
            set(),
            config,
        )
        self.assertEqual(replay.dispatched, [])
        markers = [
            c for c in comments[156] if config.dispatch_marker in c.body
        ]
        self.assertEqual(len(markers), 1)
        self.assertIn("WIP slot held", replay.skip_reasons[156])

    def test_exhausted_attempts_pause_only_when_configured(self):
        now = utcnow()
        old = now - timedelta(minutes=120)

        for pause_on_failure, expect_paused in ((True, True), (False, False)):
            with self.subTest(pause_on_failure=pause_on_failure):
                config = SchedulerConfig(
                    lease_minutes=45,
                    max_dispatch_attempts=2,
                    pause_on_failure=pause_on_failure,
                )
                issues = {156: p0_issue()}
                issues[156].labels.add(config.in_progress_label)
                comments = {
                    156: [
                        CommentRecord(
                            body=config.dispatch_marker, author_is_owner=True,
                            created_at=old,
                        ),
                        CommentRecord(
                            body=config.dispatch_marker, author_is_owner=True,
                            created_at=old + timedelta(minutes=50),
                        ),
                    ]
                }
                result = reconcile(
                    now, issues, set(), set(), comments, {}, {}, set(), config
                )
                if expect_paused:
                    self.assertIn(156, result.paused)
                    self.assertIn(config.pause_label, issues[156].labels)
                    self.assertEqual(result.dispatched, [])
                else:
                    # Autonomous consumers keep retrying without a terminal
                    # human-required pause path for recoverable failures.
                    self.assertNotIn(156, result.paused)
                    self.assertNotIn(config.pause_label, issues[156].labels)
                    self.assertEqual(result.dispatched, [156])

    def test_failed_dispatch_rolls_back_and_cron_retries(self):
        config = SchedulerConfig()
        now = utcnow()
        issues = {156: p0_issue()}
        comments: dict = {}
        calls: list = []

        def flaky(number: int) -> None:
            calls.append(number)
            if len(calls) == 1:
                raise RuntimeError("comment post failed")

        with self.assertRaises(RuntimeError):
            reconcile(
                now, issues, set(), set(), comments, {}, {}, set(), config,
                dispatch_hook=flaky,
            )
        # The reservation is rolled back so no stale lease strands the issue.
        self.assertNotIn(config.in_progress_label, issues[156].labels)
        self.assertEqual(comments.get(156, []), [])

        # The next scheduled pass retries automatically and dispatches once.
        retry = reconcile(
            now + timedelta(minutes=10),
            issues, set(), set(), comments, {}, {}, set(), config,
            dispatch_hook=flaky,
        )
        self.assertEqual(retry.dispatched, [156])
        self.assertEqual(len(calls), 2)


class WipStarvationVisibilityTests(unittest.TestCase):
    """The #156 stall: 3 lower-priority PRs held 3/2 WIP slots for days."""

    def test_starved_p0_is_explained_and_fails_instead_of_success(self):
        config = SchedulerConfig(wip_limit=2)
        now = utcnow()
        issues = {
            25: IssueState(25, "P2 work one", "", {"priority:p2"}, True),
            35: IssueState(35, "P2 work two", "", {"priority:p2"}, True),
            77: IssueState(77, "P1 work three", "", {"priority:p1"}, True),
            156: p0_issue(),
        }
        open_prs = {25, 35, 77}

        result = reconcile(
            now, issues, open_prs, set(), {}, {}, {}, set(), config
        )

        self.assertEqual(result.dispatched, [])
        # The eligible P0 waiter is named with its priority and the holders.
        self.assertIn(156, result.skip_reasons)
        reason = result.skip_reasons[156]
        self.assertIn("waiting for a free WIP slot", reason)
        self.assertIn("priority:p0", reason)
        for holder in ("#25", "#35", "#77"):
            self.assertIn(holder, reason)
        # Zero progress for eligible backlog must not report success.
        self.assertTrue(result.failed)
        self.assertTrue(result.failed_message)

    def test_full_wip_without_waiters_stays_success(self):
        config = SchedulerConfig(wip_limit=2)
        now = utcnow()
        issues = {
            25: IssueState(25, "P2 work one", "", {"priority:p2"}, True),
            35: IssueState(35, "P2 work two", "", {"priority:p2"}, True),
        }
        result = reconcile(
            now, issues, {25, 35}, set(), {}, {}, {}, set(), config
        )
        self.assertEqual(result.dispatched, [])
        self.assertFalse(result.failed)


class OrderingAndBlockerTests(unittest.TestCase):
    def test_priority_ordering_preserved(self):
        config = SchedulerConfig(wip_limit=1)
        now = utcnow()
        issues = {
            10: IssueState(10, "P2 work", "", {"priority:p2"}, True),
            11: IssueState(11, "P0 work", "", {"priority:p0"}, True),
            12: IssueState(12, "P1 work", "", {"priority:p1"}, True),
        }
        result = reconcile(
            now, issues, set(), set(), {}, {}, {}, set(), config
        )
        self.assertEqual(result.dispatched, [11])
        self.assertIn("waiting for a free WIP slot", result.skip_reasons[12])

    def test_declared_and_native_blockers_still_gate(self):
        config = SchedulerConfig()
        now = utcnow()
        issues = {156: p0_issue(), 99: p0_issue(99)}
        result = reconcile(
            now, issues, set(), set(), {}, {156: [99]}, {}, set(), config
        )
        self.assertEqual(result.dispatched, [99])
        self.assertIn("declared blocked by #99", result.skip_reasons[156])

        issues = {156: p0_issue()}
        result = reconcile(
            now, issues, set(), set(), {}, {}, {156: [42]}, set(), config
        )
        self.assertEqual(result.dispatched, [])
        self.assertIn("blocked by #42", result.skip_reasons[156])
        self.assertFalse(result.failed)

    def test_owner_command_grace_prevents_duplicate_dispatch(self):
        config = SchedulerConfig(command_grace_minutes=5)
        now = utcnow()
        issues = {156: p0_issue()}
        comments = {
            156: [
                CommentRecord(
                    body="/oc\nplease", author_is_owner=True,
                    created_at=now - timedelta(minutes=1),
                )
            ]
        }
        result = reconcile(
            now, issues, set(), set(), comments, {}, {}, set(), config
        )
        self.assertEqual(result.dispatched, [])
        self.assertIn("race window", result.skip_reasons[156])


class WorkflowContractPinTests(unittest.TestCase):
    """The shipped workflow must mirror this contract byte-for-byte in
    semantics: deterministic skip reasons, visible WIP starvation, no
    PR-Agent introduction, no paid provider keys in the core path."""

    def test_scheduler_workflow_explains_every_skip(self):
        body = scheduler_body()
        self.assertIn("WIP slot held", body)
        self.assertIn("open implementation PR must not be duplicated", body)
        self.assertIn("waiting for a free WIP slot", body)
        self.assertIn("active local issues", body)

    def test_wip_saturation_keeps_backlog_queued_without_failing(self):
        body = scheduler_body()
        self.assertIn("waiting for a free WIP slot", body)
        self.assertIn("WIP is saturated;", body)
        self.assertIn("eligible issue(s) remain queued", body)
        self.assertNotIn(
            "Issue scheduler WIP exhausted with eligible backlog waiting.", body
        )

    def test_no_review_gate_or_credential_regression(self):
        body = scheduler_body()
        # CodeRabbit stays the consumer review gate: the local automatic
        # implementation dispatch path must not route through PR-Agent.
        # (Delegated parent-side PR-Agent fan-out for child tasks predates
        # this change under issue #244 and is out of scope here.)
        local_dispatch = body[
            body.index("Re-check mutable state immediately before dispatch."):
            body.index("Wake the configured downstream dispatcher")
        ]
        self.assertNotIn("pr-agent", local_dispatch.lower())
        self.assertIn("opencodeDispatch", local_dispatch)
        for needle in ("OPENCODE_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY"):
            self.assertNotIn(needle, body)
        self.assertIn("github-token: ${{ secrets.TAP_PAT }}", body)
        self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", body)



class CheckpointResumeHoldRegressionTests(unittest.TestCase):
    """Exercise the shipped scheduler guard against real comment/ref shapes."""

    def test_scheduler_hold_is_not_released_by_automatic_owner_oc(self):
        from pathlib import Path
        import shutil
        import subprocess

        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is needed for scheduler regression")
        root = Path(__file__).resolve().parents[1]
        source = (root / ".github/scripts/issue_scheduler.js").read_text(
            encoding="utf-8"
        )
        start = source.index("  const checkpointResumeBlockedMarkerRe =")
        end = source.index("  const ownerResumeMarkerRe =", start)
        helper = source[start:end]
        harness = r"""
const assert = require('assert');
const { execFileSync } = require('child_process');
const dispatchMarker = '<!-- issue-scheduler-dispatch -->';
const core = { warning: () => {} };
function isTrustedStopComment(c, owner) {
  return ['OWNER', 'MEMBER', 'COLLABORATOR'].includes(c.author_association) ||
    ['github-actions[bot]', owner].includes(c.user?.login);
}
function stopOrderKey(c) {
  return String(c.created_at || '') + '#' +
    String(c.id || 0).padStart(12, '0');
}
function classifyComments(bodies) {
  const output = execFileSync(
    'python3',
    ['.github/scripts/opencode_command.py', 'classify-json'],
    { input: JSON.stringify(bodies), encoding: 'utf8' }
  );
  return JSON.parse(output);
}
""" + helper + r"""
(async () => {
  const sha = 'a'.repeat(40);
  const marker = '<!-- continuum-checkpoint-resume-blocked ' +
    'repo=kodmial/continuum issue=442 generation=1 ' +
    'operation=opencode-429-issue-issue442 ' +
    'checkpoint=opencode/checkpoint-issue442 ' +
    'checkpoint_sha=' + sha + ' base_ref=main base_sha=' +
    'b'.repeat(40) + ' snapshot=' + 'c'.repeat(64) +
    ' reason=verification-failed -->';
  const snapshot = (spec, generation = 1) =>
    '<!-- continuum-task-snapshot repo=kodmial/continuum issue=442 ' +
    'generation=' + generation + ' title-sha256=' + 'd'.repeat(64) +
    ' body-sha256=' + 'e'.repeat(64) + ' spec-sha256=' + spec + ' -->';
  const item = (id, body, login = 'kodmial') => ({
    id, body, created_at: '2026-11-12T04:' + String(id).padStart(2, '0') + ':00Z',
    user: { login }, author_association: login === 'outsider' ? 'NONE' : 'OWNER'
  });
  const saved = item(1, marker);
  const auto = item(2, '/oc\n\n<!-- issue-scheduler-dispatch -->');
  const watchdog = item(3, '/oc\n\n<!-- opencode-watchdog-retry -->');
  const manual = item(4, '/oc\ncheck again');
  const client = (tip = sha) => ({
    rest: { git: { getRef: async () => ({ data: { object: { sha: tip } } }) } }
  });
  const hold = (comments, cl = client()) =>
    checkpointResumeHold('kodmial', 'continuum', 442, comments, cl);
  assert.ok(await hold([saved]), 'recorded failure must hold');
  assert.ok(await hold([saved, auto]), 'scheduler /oc must not waive hold');
  assert.ok(await hold([saved, watchdog]), 'watchdog /oc must not waive hold');
  assert.ok(
    await hold([saved, item(5, '/oc\n\n<!-- custom-retry -->\n' +
      '<!-- continuum-origin role=continuum component=opencode-watchdog -->')]),
    'watchdog origin marker must exclude a configured retry marker'
  );
  assert.ok(
    await hold([saved, item(6, '```\n/oc\n```')]),
    'a fenced /oc example must not waive the hold'
  );
  assert.ok(
    await hold([saved, item(7, 'Some explanation first.\n/oc')]),
    'an explained /oc mention must not waive the hold'
  );
  assert.ok(
    await hold([saved, item(8, '    /oc')]),
    'an indented /oc code block must not waive the hold'
  );
  assert.equal(await hold([saved, manual]), null, 'manual owner /oc may resume');
  assert.equal(await hold([saved], client('d'.repeat(40))), null,
    'changed checkpoint SHA releases hold');
  assert.equal(await hold([item(1, marker, 'outsider')]), null,
    'untrusted authors cannot impose a hold');
  assert.ok(await hold([saved], {rest: {git: {
    getRef: async () => { throw {status: 503}; }
  }}}), 'failed GitHub ref lookup must fail closed');
  assert.ok(
    await hold([item(1, marker.replace('generation=1', 'generation=2'))]),
    'a rejection from recovery generation 2 must still hold'
  );
  assert.ok(
    await hold([item(0, snapshot('f'.repeat(64))), saved]),
    'an older different-spec snapshot must not release the hold'
  );
  assert.ok(
    await hold([item(1, marker + '\n' + snapshot('f'.repeat(64)))]),
    'a snapshot inside the rejection comment must not release the hold'
  );
  assert.ok(
    await hold([saved, item(5, snapshot('c'.repeat(64)))]),
    'a later snapshot with the same spec must not release the hold'
  );
  assert.ok(
    await hold([saved, item(5, snapshot('f'.repeat(64), 2))]),
    'a later snapshot for another task generation must not release the hold'
  );
  assert.equal(
    await hold([saved, item(5, snapshot('f'.repeat(64)))]),
    null,
    'a strictly later different-spec snapshot releases the hold'
  );
  console.log('checkpoint-resume-hold-regression: PASS');
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", harness], cwd=root,
            capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checkpoint-resume-hold-regression: PASS", result.stdout)

    def test_scheduler_and_watchdog_check_before_dispatch(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        scheduler = (root / ".github/scripts/issue_scheduler.js").read_text(
            encoding="utf-8"
        )
        watchdog = (
            root / ".github/workflows/continuum-opencode-watchdog.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(scheduler.count("await checkpointResumeHold("), 3)
        self.assertIn("await removeLabel(issueNumber, inProgressLabel)", scheduler)
        self.assertIn("!body.includes(dispatchMarker)", scheduler)
        self.assertIn("!body.includes(watchdogOriginMarker)", scheduler)
        self.assertIn("!body.includes('<!-- opencode-watchdog-retry -->')", scheduler)
        self.assertIn("ownerClasses[index] === 'run'", scheduler)
        self.assertIn("checkpointResumeBlockedRe", watchdog)
        self.assertIn("async function checkpointResumeHold(", watchdog)
        self.assertIn("function classifyComments(bodies)", watchdog)
        self.assertIn("if (body.includes(dispatchMarker)) continue;", watchdog)
        self.assertIn("if (body.includes(retryMarker)) continue;", watchdog)
        self.assertIn(
            "if (body.includes(watchdogAutomationMarker)) continue;", watchdog
        )
        self.assertIn(
            "const hold = await checkpointResumeHold(issueText, comments);",
            watchdog,
        )
        self.assertLess(
            watchdog.index("const checkpointResumeBlockedRe ="),
            watchdog.index("// A scheduler dispatch comment newer")
        )



if __name__ == "__main__":
    unittest.main()
