"""Long-spec false-drift + terminal generation-stop regressions (#309).

Hermetic proof for the AA #315 incident shape: a ~55,021-character issue
body (Cyrillic, Markdown tables, JSON, newlines, XML-like tags) with a
legitimate trusted snapshot must admit exactly once with zero false
drift reports, while a one-codepoint edit (or title edit) fails closed
with exactly one terminal failure and no redispatch of that generation.

Covers both the same-repo issue-comment admission path and the
delegated child dispatch path (same engine, child identity), concurrent
admission with first-writer-wins, five consecutive scheduler ticks plus
manual wakeups without repeated /oc, ghost-WIP prevention, unrelated
task non-starvation, and successor scheduling while the old generation
stays immutable and stopped.
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
from continuum.task_snapshot import (
    body_digest,
    build_snapshot,
    decide_admission,
    generation_stop_skip_reason,
    is_generation_terminally_stopped,
    is_trusted_snapshot_comment,
    parse_generation_recover,
    parse_generation_stop,
    render_generation_recover,
    render_generation_stop,
    render_snapshot_comment,
    select_generation_stop,
    spec_digest,
    title_digest,
)


def build_long_body(target_chars: int = 55021) -> str:
    chunk = (
        "# Спецификация задачи\n\n"
        "Подробное описание с кириллицей, проверкой кодировки UTF-8 и NFC.\n\n"
        "| Колонка A | Колонка B | Статус |\n"
        "| --- | --- | --- |\n"
        "| значение один | значение два | готово |\n\n"
        "```json\n{\"task\": \"пример\", \"n\": 123, \"ok\": true}\n```\n\n"
        "<custom-spec attr=\"1\">тело тега</custom-spec>\n\n"
        "- [ ] пункт один\n- [ ] пункт два\n\n"
    )
    parts = []
    total = 0
    counter = 0
    while total < target_chars:
        counter += 1
        line = f"Строка {counter}: {chunk}\n"
        parts.append(line)
        total += len(line)
    body = "".join(parts)
    # Trim to exactly the target character count without splitting a
    # surrogate pair (all BMP here, plain slice is safe).
    return body[:target_chars]


LONG_TITLE = "P0: длинная спецификация с кириллицей и таблицами"
LONG_BODY = build_long_body()
assert len(LONG_BODY) == 55021, len(LONG_BODY)

REPO = "kodmial/aa"
ISSUE = 315
CHILD_REPO = "kodmial/aa"
OWNER = "kodmial"


def bot_comment(body, created_at="2026-10-09T10:00:00Z", comment_id=1):
    return {
        "body": body,
        "author_association": "NONE",
        "user": {"login": "github-actions[bot]"},
        "created_at": created_at,
        "updated_at": created_at,
        "id": comment_id,
    }


def owner_comment(body, created_at="2026-10-09T10:00:00Z", comment_id=1):
    return {
        "body": body,
        "author_association": "OWNER",
        "user": {"login": OWNER},
        "created_at": created_at,
        "updated_at": created_at,
        "id": comment_id,
    }


class LongBodyAdmissionTests(unittest.TestCase):
    def test_matching_long_body_passes_once_with_zero_false_drift(self):
        snapshot = build_snapshot(REPO, ISSUE, LONG_TITLE, LONG_BODY)
        comments = [bot_comment(render_snapshot_comment(snapshot))]
        # Exactly one valid admission for the identical 55k body.
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=LONG_TITLE,
            live_body=LONG_BODY, comments=comments, owner_login=OWNER,
        )
        self.assertEqual(decision.action, "proceed")
        # Repeated verification (five wakeups) stays clean, no drift text.
        for _ in range(5):
            again = decide_admission(
                repo=REPO, issue=ISSUE, live_title=LONG_TITLE,
                live_body=LONG_BODY, comments=comments, owner_login=OWNER,
            )
            self.assertEqual(again.action, "proceed")
            self.assertEqual(again.diagnostic, "")

    def test_one_codepoint_edit_fails_closed(self):
        snapshot = build_snapshot(REPO, ISSUE, LONG_TITLE, LONG_BODY)
        comments = [bot_comment(render_snapshot_comment(snapshot))]
        edited = LONG_BODY[:-1] + ("X" if LONG_BODY[-1] != "X" else "Y")
        self.assertEqual(len(edited), len(LONG_BODY))
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=LONG_TITLE,
            live_body=edited, comments=comments, owner_login=OWNER,
        )
        self.assertEqual(decision.action, "drift_blocked")
        self.assertIn("follow-up", decision.diagnostic)

    def test_title_edit_fails_closed(self):
        snapshot = build_snapshot(REPO, ISSUE, LONG_TITLE, LONG_BODY)
        comments = [bot_comment(render_snapshot_comment(snapshot))]
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=LONG_TITLE + "!",
            live_body=LONG_BODY, comments=comments, owner_login=OWNER,
        )
        self.assertEqual(decision.action, "drift_blocked")
        self.assertIn("title", decision.reason)

    def test_delegated_child_dispatch_matches_same_engine(self):
        # Delegated child admission uses the same canonical engine with a
        # child identity; identical long content proceeds, edited stops.
        snapshot = build_snapshot(CHILD_REPO, 315, LONG_TITLE, LONG_BODY)
        comments = [bot_comment(render_snapshot_comment(snapshot))]
        ok = decide_admission(
            repo=CHILD_REPO, issue=315, live_title=LONG_TITLE,
            live_body=LONG_BODY, comments=comments, owner_login=OWNER,
        )
        self.assertEqual(ok.action, "proceed")
        bad = decide_admission(
            repo=CHILD_REPO, issue=315, live_title=LONG_TITLE,
            live_body=LONG_BODY + " ", comments=comments, owner_login=OWNER,
        )
        self.assertEqual(bad.action, "drift_blocked")

    def test_concurrent_identical_pins_converge(self):
        first = build_snapshot(REPO, ISSUE, LONG_TITLE, LONG_BODY)
        second = build_snapshot(REPO, ISSUE, LONG_TITLE, LONG_BODY)
        comments = [
            bot_comment(render_snapshot_comment(first), comment_id=1),
            bot_comment(render_snapshot_comment(second), comment_id=2),
        ]
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=LONG_TITLE,
            live_body=LONG_BODY, comments=comments, owner_login=OWNER,
        )
        self.assertEqual(decision.action, "proceed")

    def test_concurrent_divergent_pins_first_writer_wins(self):
        first = build_snapshot(REPO, ISSUE, LONG_TITLE, LONG_BODY)
        second = build_snapshot(
            REPO, ISSUE, LONG_TITLE, LONG_BODY + " racer",
        )
        comments = [
            bot_comment(
                render_snapshot_comment(first),
                created_at="2026-10-09T10:00:00Z", comment_id=1,
            ),
            bot_comment(
                render_snapshot_comment(second),
                created_at="2026-10-09T10:00:01Z", comment_id=2,
            ),
        ]
        self.assertEqual(
            decide_admission(
                repo=REPO, issue=ISSUE, live_title=LONG_TITLE,
                live_body=LONG_BODY, comments=comments, owner_login=OWNER,
            ).action,
            "proceed",
        )
        self.assertEqual(
            decide_admission(
                repo=REPO, issue=ISSUE, live_title=LONG_TITLE,
                live_body=LONG_BODY + " racer", comments=comments,
                owner_login=OWNER,
            ).action,
            "drift_blocked",
        )

    def test_env_truncation_hypothesis_shape(self):
        # The incident hypothesis: a 55k body carried through step
        # outputs/env arrives truncated and hashes differently. The
        # engine must hash the exact authoritative text: full text
        # verifies, any truncation fails closed (never silently passes).
        snapshot = build_snapshot(REPO, ISSUE, LONG_TITLE, LONG_BODY)
        comments = [bot_comment(render_snapshot_comment(snapshot))]
        truncated = LONG_BODY[:40000]
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=LONG_TITLE,
            live_body=truncated, comments=comments, owner_login=OWNER,
        )
        self.assertEqual(decision.action, "drift_blocked")
        self.assertNotEqual(body_digest(truncated), body_digest(LONG_BODY))


class TerminalStopTests(unittest.TestCase):
    def _drift_stop_comments(self):
        snapshot = build_snapshot(REPO, ISSUE, LONG_TITLE, LONG_BODY)
        pinned = snapshot["spec_sha256"]
        live = spec_digest(LONG_TITLE, LONG_BODY + " tamper")
        stop = render_generation_stop(
            REPO, ISSUE, generation=1,
            pinned_spec=pinned, live_spec=live, reason="drift",
        )
        return snapshot, pinned, live, [bot_comment(render_snapshot_comment(snapshot), comment_id=1),
                                        bot_comment(stop, created_at="2026-10-09T11:00:00Z", comment_id=2)]

    def test_stop_marker_carries_no_text_leakage(self):
        _snapshot, pinned, live, comments = self._drift_stop_comments()
        stop_body = comments[1]["body"]
        self.assertIn("continuum-task-generation-stop", stop_body)
        self.assertIn(pinned, stop_body)
        self.assertIn(live, stop_body)
        # Privacy: no dynamic task content leaks into the stop record.
        self.assertNotIn("Спецификация", stop_body)
        self.assertNotIn(LONG_BODY[:64], stop_body)
        self.assertNotIn(LONG_TITLE, stop_body.replace("Continuum terminal generation stop", ""))

    def test_stop_round_trip_and_trust(self):
        _snapshot, pinned, live, comments = self._drift_stop_comments()
        parsed = parse_generation_stop(comments[1]["body"])
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.pinned_spec, pinned)
        self.assertEqual(parsed.live_spec, live)
        self.assertEqual(parsed.reason, "drift")
        self.assertTrue(is_trusted_snapshot_comment(comments[1], owner_login=OWNER))
        forged = {
            "body": comments[1]["body"],
            "author_association": "NONE",
            "user": {"login": "random-fork-user"},
            "created_at": "2026-10-09T11:00:00Z",
            "id": 99,
        }
        self.assertFalse(is_trusted_snapshot_comment(forged))
        self.assertIsNone(select_generation_stop([forged], repo=REPO, issue=ISSUE))
        selected = select_generation_stop(comments, repo=REPO, issue=ISSUE, owner_login=OWNER)
        self.assertIsNotNone(selected)
        self.assertTrue(
            is_generation_terminally_stopped(comments, repo=REPO, issue=ISSUE, owner_login=OWNER)
        )

    def test_recovery_re_arms_only_after_stop(self):
        _snapshot, _pinned, _live, comments = self._drift_stop_comments()
        recover = render_generation_recover(REPO, ISSUE, generation=1)
        self.assertIsNotNone(parse_generation_recover(recover))
        # Recovery before the stop never cancels it.
        early = [owner_comment(recover, created_at="2026-10-09T09:00:00Z", comment_id=0)] + comments
        self.assertTrue(
            is_generation_terminally_stopped(early, repo=REPO, issue=ISSUE, owner_login=OWNER)
        )
        # Recovery strictly after the stop re-arms the generation.
        late = comments + [owner_comment(recover, created_at="2026-10-09T12:00:00Z", comment_id=3)]
        self.assertFalse(
            is_generation_terminally_stopped(late, repo=REPO, issue=ISSUE, owner_login=OWNER)
        )

    def test_new_generation_and_successor_stay_schedulable(self):
        _snapshot, _pinned, _live, comments = self._drift_stop_comments()
        # A new generation number is a different key: not stopped.
        self.assertFalse(
            is_generation_terminally_stopped(
                comments, repo=REPO, issue=ISSUE, generation=2, owner_login=OWNER
            )
        )
        # A successor issue is a different key: not stopped.
        self.assertFalse(
            is_generation_terminally_stopped(
                comments, repo=REPO, issue=327, owner_login=OWNER
            )
        )
        reason = generation_stop_skip_reason(
            REPO, ISSUE, generation=1, pinned_spec="a" * 64,
            live_spec="b" * 64, reason="drift",
        )
        self.assertIn("terminal generation stop", reason)
        self.assertIn("successor", reason)

    def test_invalid_stop_inputs_fail_closed(self):
        from continuum.task_snapshot import TaskSnapshotError

        with self.assertRaises(TaskSnapshotError):
            render_generation_stop(REPO, ISSUE, pinned_spec="xyz", live_spec="b" * 64)
        with self.assertRaises(TaskSnapshotError):
            render_generation_stop(REPO, ISSUE, pinned_spec="a" * 64, live_spec="b" * 64, reason="oops")


class SchedulerStopGatingTests(unittest.TestCase):
    def _states(self):
        now = datetime.now(timezone.utc)
        issues = {
            315: IssueState(315, LONG_TITLE[:80], LONG_BODY[:200], {"automation:ready"}, True),
            400: IssueState(400, "P1: unrelated work", "Do it.", {"priority:p1"}, True),
        }
        return now, issues

    def test_five_ticks_no_redispatch_no_ghost_wip(self):
        now, issues = self._states()
        config = SchedulerConfig(wip_limit=2)
        stopped = {315}
        dispatches: list = []
        comments: dict = {}
        for tick in range(5):
            result = reconcile(
                now + timedelta(minutes=tick), issues, set(), set(),
                comments, {}, {}, set(), config,
                dispatch_hook=lambda n: dispatches.append(n),
                terminal_stopped=stopped,
            )
            self.assertNotIn(315, result.dispatched)
            self.assertIn(315, result.skip_reasons)
            self.assertIn("terminal generation stop", result.skip_reasons[315])
            self.assertFalse(result.failed)
        # Only the healthy issue dispatched (once); the stopped one left
        # no reservation, no dispatch marker, no ghost WIP.
        self.assertEqual(dispatches, [400])
        self.assertNotIn(config.in_progress_label, issues[315].labels)
        markers = [c for c in comments.get(315, []) if config.dispatch_marker in c.body]
        self.assertEqual(markers, [])

    def test_stopped_issue_does_not_starve_unrelated_task(self):
        now, issues = self._states()
        config = SchedulerConfig(wip_limit=1)
        result = reconcile(
            now, issues, set(), set(), {}, {}, {}, set(), config,
            terminal_stopped={315},
        )
        self.assertEqual(result.dispatched, [400])
        self.assertIn("terminal generation stop", result.skip_reasons[315])
        self.assertFalse(result.failed)

    def test_dependency_gated_successor_enters_queue(self):
        now = datetime.now(timezone.utc)
        config = SchedulerConfig()
        issues = {
            315: IssueState(315, "old", "old", {"automation:ready"}, True),
            327: IssueState(327, "P0: successor", "Follows #315.", {"priority:p0"}, True),
        }
        # Successor declares a dependency on the stopped generation: it
        # stays gated until #315 closes, then enters automatically while
        # the old generation remains stopped and immutable.
        gated = reconcile(
            now, issues, set(), set(), {}, {327: [315]}, {}, set(), config,
            terminal_stopped={315},
        )
        self.assertEqual(gated.dispatched, [])
        self.assertIn("terminal generation stop", gated.skip_reasons[315])
        self.assertIn("#315", gated.skip_reasons[327])
        issues[315].is_open = False
        open_again = reconcile(
            now + timedelta(minutes=5), issues, set(), set(), {}, {}, {}, set(),
            config, terminal_stopped={315},
        )
        self.assertEqual(open_again.dispatched, [327])


if __name__ == "__main__":
    unittest.main()
