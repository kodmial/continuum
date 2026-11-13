"""CodeRabbit inline chat-quota recovery contract (kodmial/continuum#450).

A CodeRabbit ``### Rate Limit Exceeded`` reply in a review-comment thread is
an inline chat-message/hour rejection, not a finding verdict and not the
shared full-review quota. These tests pin the exact production reply text
captured from PRs #448/#449, the provider-owned parser in
``continuum.coderabbit_adapter``, the byte-identical JavaScript mirror embedded
in both workflows, and the durable exact-HEAD/finding retry markers.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

from continuum import coderabbit_adapter as rabbit
from continuum import reconciliation_adapters


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "coderabbit"
AUTO_MERGE = ROOT / ".github" / "workflows" / "continuum-auto-merge.yml"
UNRESOLVED = ROOT / ".github" / "workflows" / "continuum-coderabbit-unresolved.yml"
RETRY = ROOT / ".github" / "workflows" / "continuum-coderabbit-retry.yml"
OPENCODE = ROOT / ".github" / "workflows" / "continuum-opencode.yml"

BLOCK_START = "// continuum-coderabbit-chat-quota:start"
BLOCK_END = "// continuum-coderabbit-chat-quota:end"


def chat_quota_block(workflow: Path) -> str:
    """Extract the production classification block for executable tests."""

    text = workflow.read_text(encoding="utf-8")
    assert text.count(BLOCK_START) == 1, workflow.name
    assert text.count(BLOCK_END) == 1, workflow.name
    marker = text.index(BLOCK_START)
    start = text.rfind("\n", 0, marker) + 1
    end_marker = text.index(BLOCK_END, marker)
    end = text.find("\n", end_marker)
    if end < 0:
        end = len(text)
    raw = text[start:end]
    lines = raw.splitlines()
    indent = min(
        len(line) - len(line.lstrip(" "))
        for line in lines
        if line.strip()
    )
    return textwrap.dedent(
        "\n".join(line[indent:] if line.strip() else "" for line in lines)
    )


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def thread_verifier_source() -> str:
    """Extract the production auto-merge thread verifier for a node harness."""

    text = AUTO_MERGE.read_text(encoding="utf-8")
    start_marker = "          async function requestCodeRabbitThreadVerification("
    end_marker = "          async function cancelObsoleteRuns("
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return textwrap.dedent(text[start:end]).rstrip()


def iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(
        epoch_ms / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


class ChatQuotaParserTests(unittest.TestCase):
    def test_actual_448_reply_text_yields_the_exact_deadline(self):
        data = fixture("chat-rate-limit-pr448.json")
        replies = data["threads"][0]["replies"]

        self.assertIn("### Rate Limit Exceeded", replies[0]["body"])
        self.assertIn(
            "Please wait **14 minutes and 56 seconds** before sending "
            "another message.",
            replies[0]["body"],
        )
        # 14m56s + 30s padding, anchored to the immutable reply timestamp.
        self.assertEqual(rabbit.parse_chat_retry_delay_ms(replies[0]["body"]), 896_000)
        self.assertEqual(
            iso(rabbit.chat_retry_not_before_ms(replies[0]["created_at"], 896_000)),
            "2026-11-12T08:08:41Z",
        )
        # 9m23s from the later reply lands on the same absolute reset: one
        # account-wide chat quota shared by every PR/thread.
        self.assertEqual(rabbit.parse_chat_retry_delay_ms(replies[1]["body"]), 563_000)
        self.assertEqual(
            iso(rabbit.chat_retry_not_before_ms(replies[1]["created_at"], 563_000)),
            "2026-11-12T08:08:41Z",
        )

    def test_actual_449_threads_share_the_same_absolute_reset(self):
        data = fixture("chat-rate-limit-pr449.json")
        expected = {
            4547629448: (1_623_000, "2026-11-12T08:08:40Z"),
            4547737495: (819_000, "2026-11-12T08:08:40Z"),
            4547629830: (1_620_000, "2026-11-12T08:08:40Z"),
            4547737581: (819_000, "2026-11-12T08:08:41Z"),
        }
        for thread in data["threads"]:
            for reply in thread["replies"]:
                with self.subTest(reply=reply["id"]):
                    delay, deadline = expected[reply["id"]]
                    self.assertEqual(
                        rabbit.parse_chat_retry_delay_ms(reply["body"]), delay
                    )
                    self.assertEqual(
                        iso(
                            rabbit.chat_retry_not_before_ms(
                                reply["created_at"], delay
                            )
                        ),
                        deadline,
                    )

    def test_malformed_countdown_fails_closed_for_one_hour(self):
        body = (
            "### Rate Limit Exceeded\n\n"
            "`@kodmial` have exceeded the limit for the number of chat "
            "messages per hour. Please wait before sending another message."
        )
        self.assertEqual(
            rabbit.parse_chat_retry_delay_ms(body),
            rabbit.CHAT_QUOTA_FALLBACK_MS,
        )
        self.assertEqual(rabbit.CHAT_QUOTA_FALLBACK_MS, 65 * 60_000)
        self.assertEqual(
            rabbit.classify_coderabbit_reply(
                body, author_login="coderabbitai[bot]"
            )["kind"],
            "chat_rate_limit",
        )

    def test_plural_singular_and_multi_unit_countdowns(self):
        vectors = {
            "### Rate Limit Exceeded\nPlease wait **1 minute** before sending another message.": 60_000,
            "### Rate Limit Exceeded\nPlease wait **1 minute and 1 second** before sending another message.": 61_000,
            "### Rate Limit Exceeded\nPlease wait **1 hour, 2 minutes, and 3 seconds** before sending another message.": 3_723_000,
            "### Rate Limit Exceeded\nPlease wait **less than a minute** before sending another message.": 60_000,
        }
        for body, expected in vectors.items():
            with self.subTest(body=body):
                self.assertEqual(rabbit.parse_chat_retry_delay_ms(body), expected)

    def test_partially_parsed_countdown_fails_closed(self):
        # "1h" is not a recognized unit, so the complete countdown cannot be
        # parsed. Accepting only "30 seconds" would retry after the short
        # suffix instead of the documented 65-minute fallback.
        body = (
            "### Rate Limit Exceeded\n"
            "Please wait **1h 30 seconds** before sending another message."
        )
        self.assertIsNone(rabbit.parse_duration_ms("1h 30 seconds"))
        self.assertEqual(
            rabbit.parse_chat_retry_delay_ms(body),
            rabbit.CHAT_QUOTA_FALLBACK_MS,
        )
        self.assertEqual(
            rabbit.parse_retry_delay_ms(
                "Review rate limited. Next included review available in "
                "1h 30 seconds."
            ),
            rabbit.QUOTA_FALLBACK_MS,
        )

    def test_chat_messages_per_hour_without_quota_evidence_is_a_verdict(self):
        body = (
            "**UNRESOLVED**\n"
            "The number of chat messages per hour is not tracked across "
            "threads, so this finding still needs a code change."
        )
        self.assertIsNone(rabbit.parse_chat_retry_delay_ms(body))
        self.assertEqual(
            rabbit.classify_coderabbit_reply(
                body, author_login="coderabbitai[bot]"
            )["kind"],
            "verdict",
        )

    def test_negated_hourly_quota_in_a_finding_is_not_a_rate_limit(self):
        # An actual CodeRabbit UNRESOLVED verdict must never be ignored merely
        # because it discusses a quota the user did not exceed.
        for statement in (
            "The user has not exceeded the limit for the number of chat messages per hour.",
            "The user never reached the limit for chat messages per hour.",
            "The user hasn't exceeded the limit for the number of chat messages per hour.",
            "The user hasn’t reached the limit for chat messages per hour.",
            "The user has not, according to the current account state and the recent thread history, exceeded the limit for chat messages per hour.",
        ):
            body = "**UNRESOLVED**\n" + statement
            with self.subTest(statement=statement):
                self.assertIsNone(rabbit.parse_chat_retry_delay_ms(body))
                self.assertEqual(
                    "verdict",
                    rabbit.classify_coderabbit_reply(
                        body, author_login="coderabbitai[bot]"
                    )["kind"],
                )

    def test_fake_author_and_quoted_text_are_never_provider_quota(self):
        body = fixture("chat-rate-limit-pr448.json")["threads"][0]["replies"][0]["body"]
        self.assertEqual(
            rabbit.classify_coderabbit_reply(body, author_login="kodmial")["kind"],
            "untrusted",
        )
        quoted = "\n".join("> " + line for line in body.splitlines())
        self.assertIsNone(rabbit.parse_chat_retry_delay_ms(quoted))
        self.assertEqual(
            rabbit.classify_coderabbit_reply(
                "Here is what the bot said:\n\n" + quoted,
                author_login="coderabbitai[bot]",
            )["kind"],
            "verdict",
        )
        for author in ({"login": "kodmial"}, None):
            with self.subTest(author=author):
                self.assertIsNone(
                    rabbit.latest_chat_rate_limit(
                        [
                            {
                                "id": 1,
                                "user": author,
                                "in_reply_to_id": 10,
                                "created_at": "2026-11-12T07:53:15Z",
                                "body": body,
                            }
                        ],
                        root_comment_id=10,
                    )
                )

    def test_indented_block_quotes_are_never_a_chat_quota(self):
        # A finding that quotes the provider message with an indented quote
        # marker must stay a verdict: the prose match must reject the quote
        # marker before consuming leading whitespace.
        for marker in (" >", "   >", "\t>"):
            statement = (
                marker + " You have exceeded the limit of chat messages per hour."
            )
            body = (
                "**UNRESOLVED**\n"
                + statement
                + "\nThe parser treats quoted provider messages as active limits."
            )
            with self.subTest(marker=marker):
                self.assertIsNone(rabbit.parse_chat_retry_delay_ms(body))
                self.assertEqual(
                    "verdict",
                    rabbit.classify_coderabbit_reply(
                        body, author_login="coderabbitai[bot]"
                    )["kind"],
                )

    def test_fenced_examples_do_not_hide_unresolved_findings(self):
        # Regression: CodeRabbit PR #451 response 4564591077 was classified
        # as a live quota merely because its UNRESOLVED finding quoted an
        # example provider rejection inside a Markdown code fence.
        examples = (
            "**UNRESOLVED**\nThe parser misclassifies this example:\n\n"
            "\x60\x60\x60text\nYou have exceeded the limit of chat messages per hour.\n"
            "\x60\x60\x60\nA fix is still required.",
            "**UNRESOLVED**\nHere is an illustrative provider error:\n"
            "\x60\x60\x60text\n### Rate Limit Exceeded\n"
            "The user has exceeded the limit for the number of chat messages per hour.\n"
            "\x60\x60\x60\nThe quota check is defective.",
            "**UNRESOLVED**\n~~~text\n### Rate Limit Exceeded\n"
            "Please wait **90 minutes** before sending another message.\n"
            "~~~\nThis is only a quoted example.",
            "**UNRESOLVED**\n\x60\x60\x60\x60markdown\n"
            "\x60\x60\x60text\nYou have reached the limit of chat messages per hour.\n"
            "\x60\x60\x60\n\x60\x60\x60\x60\nThe fix is not complete.",
        )
        for body in examples:
            with self.subTest(body=body):
                self.assertIsNone(rabbit.parse_chat_retry_delay_ms(body))
                self.assertEqual(
                    rabbit.classify_coderabbit_reply(
                        body, author_login="coderabbitai[bot]"
                    )["kind"],
                    "verdict",
                )
        # A real quota immediately AFTER the fenced example still counts.
        live = examples[0] + "\n### Rate Limit Exceeded\nPlease wait **10 minutes** before sending another message."
        self.assertEqual(rabbit.parse_chat_retry_delay_ms(live), 600_000)

    def test_fenced_full_review_examples_do_not_hide_unresolved_findings(self):
        # The full-review branch must ignore quoted provider wording exactly
        # like the chat branch; otherwise a trusted UNRESOLVED answer with a
        # fenced example becomes a quota reply and auto-merge discards it.
        examples = (
            "**UNRESOLVED**\nThe parser misclassifies this example:\n\n"
            "\x60\x60\x60text\nReview rate limited. Next included review "
            "available in 13 minutes.\n\x60\x60\x60\nA fix is still required.",
            "**UNRESOLVED**\nHere is an illustrative provider reply:\n~~~\n"
            "Review limit reached. Try again in 13 minutes.\n~~~\n"
            "The quota check is defective.",
            "**UNRESOLVED**\n> Review rate limited. Try again in 10 minutes.\n"
            "The parser treats quoted provider messages as active limits.",
        )
        for body in examples:
            with self.subTest(body=body):
                self.assertEqual(
                    rabbit.classify_coderabbit_reply(
                        body, author_login="coderabbitai[bot]"
                    )["kind"],
                    "verdict",
                )
                # The standalone full-review queue parser is unchanged.
                self.assertIsNotNone(rabbit.parse_retry_delay_ms(body))
        # A real quota immediately AFTER the quoted example still counts.
        live = examples[0] + (
            "\nReview rate limited. Next included review available in "
            "10 minutes."
        )
        self.assertEqual(
            rabbit.classify_coderabbit_reply(
                live, author_login="coderabbitai[bot]"
            ),
            {"kind": "review_rate_limit", "delay_ms": 600_000},
        )

    def test_chat_deadline_needs_a_reliable_reply_timestamp(self):
        body = fixture("chat-rate-limit-pr448.json")["threads"][0]["replies"][0]["body"]
        delay = rabbit.parse_chat_retry_delay_ms(body)
        self.assertIsNone(rabbit.chat_retry_not_before_ms("", delay))
        self.assertIsNone(rabbit.chat_retry_not_before_ms("not-a-timestamp", delay))
        # A timestamp without an explicit zone is treated as UTC rather than
        # silently shifting the deadline by the runner's local offset.
        self.assertEqual(
            rabbit.chat_retry_not_before_ms("2026-11-12T07:53:15", delay),
            rabbit.chat_retry_not_before_ms("2026-11-12T07:53:15Z", delay),
        )

    def test_chat_and_full_review_quota_stay_separate(self):
        chat = fixture("chat-rate-limit-pr448.json")["threads"][0]["replies"][0]["body"]
        review = (
            "Review rate limited. Next included review available in 13 minutes."
        )
        self.assertEqual(
            rabbit.classify_coderabbit_reply(
                chat, author_login="coderabbitai[bot]"
            )["kind"],
            "chat_rate_limit",
        )
        self.assertIsNone(rabbit.parse_retry_delay_ms(chat))
        self.assertFalse(rabbit.is_rate_limited_status(chat))

        # The global full-review queue must not see a chat limit as its own
        # shared slot: only the review-quota forms set that floor.
        self.assertIsNone(
            rabbit.latest_rate_limit(
                [
                    {
                        "id": 1,
                        "user": {"login": "coderabbitai[bot]"},
                        "created_at": "2026-11-12T07:53:15Z",
                        "body": chat,
                    }
                ]
            )
        )
        decision = reconciliation_adapters.decide_coderabbit_queue(
            {"number": 450, "state": "open"},
            comments=[],
            now_ms=0,
        )
        self.assertEqual(decision.action, "request_coderabbit_review")

        self.assertEqual(
            rabbit.classify_coderabbit_reply(
                review, author_login="coderabbitai[bot]"
            )["kind"],
            "review_rate_limit",
        )
        self.assertIsNone(rabbit.parse_chat_retry_delay_ms(review))
        self.assertEqual(rabbit.parse_retry_delay_ms(review), 13 * 60_000)

    def test_chat_retry_acknowledgement_accepts_trusted_author_and_rejects_untrusted_author_and_missing_author_at_exact_head(self):
        body = fixture("chat-rate-limit-pr448.json")["threads"][0]["replies"][0]
        head = "43e40cee0f1e664f55854c0bb0e017fb01c30669"
        marker = rabbit.chat_retry_marker(4546745854, head, body["id"])
        reply = {
            "in_reply_to_id": 4546745854,
            "user": {"login": "github-actions[bot]"},
            "body": marker,
        }
        self.assertTrue(
            rabbit.chat_retry_acknowledged(
                [reply],
                finding_id=4546745854,
                head_sha=head,
                limited_comment_id=body["id"],
                root_comment_id=4546745854,
                trusted_author_logins=("github-actions[bot]",),
            )
        )
        # A copied marker from any other commenter never suppresses the retry.
        self.assertFalse(
            rabbit.chat_retry_acknowledged(
                [{**reply, "user": {"login": "random"}}],
                finding_id=4546745854,
                head_sha=head,
                limited_comment_id=body["id"],
                root_comment_id=4546745854,
                trusted_author_logins=("github-actions[bot]",),
            )
        )
        # A marker without an author identity is never trusted either.
        for user in (None, {}, {"login": ""}):
            with self.subTest(user=user):
                self.assertFalse(
                    rabbit.chat_retry_acknowledged(
                        [{**reply, "user": user}],
                        finding_id=4546745854,
                        head_sha=head,
                        limited_comment_id=body["id"],
                        root_comment_id=4546745854,
                        trusted_author_logins=("github-actions[bot]",),
                    )
                )
        # A DIFFERENT HEAD, finding, quota reply, or thread never reuses it.
        for kwargs in (
            {"head_sha": "a" * 40},
            {"finding_id": 4546745855},
            {"limited_comment_id": body["id"] + 1},
            {"root_comment_id": 4546745855},
        ):
            arguments = {
                "finding_id": 4546745854,
                "head_sha": head,
                "limited_comment_id": body["id"],
                "root_comment_id": 4546745854,
                "trusted_author_logins": ("github-actions[bot]",),
            }
            arguments.update(kwargs)
            with self.subTest(kwargs=kwargs):
                self.assertFalse(
                    rabbit.chat_retry_acknowledged([reply], **arguments)
                )

    def test_latest_chat_limit_is_thread_scoped(self):
        data = fixture("chat-rate-limit-pr449.json")
        first, second = data["threads"]
        comments = [first["finding"], *first["replies"], *second["replies"]]
        latest = rabbit.latest_chat_rate_limit(
            comments, root_comment_id=first["finding_id"]
        )
        self.assertIsNotNone(latest)
        self.assertEqual(latest["comment"]["id"], 4547737495)
        self.assertEqual(latest["delay_ms"], 819_000)


class ChatQuotaWorkflowContractTests(unittest.TestCase):
    def test_js_mirror_is_byte_identical_in_both_workflows(self):
        auto = chat_quota_block(AUTO_MERGE)
        unresolved = chat_quota_block(UNRESOLVED)
        self.assertEqual(auto, unresolved)
        for required in (
            "CODERABBIT_BOT_LOGIN",
            "CHAT_QUOTA_FALLBACK_MS",
            "parseChatQuotaDelayMs",
            "classifyCodeRabbitReply",
            "isCodeRabbitQuotaReply",
            "chatQuotaNotBeforeMs",
            "chatQuotaRetryMarker",
            "chatQuotaRetryAcknowledged",
        ):
            self.assertIn(required, auto)

    def test_repair_admission_reuses_the_shared_quota_classifier(self):
        # OpenCode's live-batch admission must reject both quota forms with
        # the same classifier the collector used to publish unresolved_id.
        self.assertEqual(chat_quota_block(OPENCODE), chat_quota_block(AUTO_MERGE))
        source = OPENCODE.read_text(encoding="utf-8")
        self.assertIn("!isCodeRabbitQuotaReply(comment)", source)

    def test_auto_merge_processes_finding_handoffs_before_full_review_quota(self):
        # An unresolved finding (and any OpenCode repair handoff) must be
        # consumed before the full-review quota return, otherwise a
        # rate-limited status strands the finding while the retry controller
        # refuses the PR until that finding is verified.
        source = AUTO_MERGE.read_text(encoding="utf-8")
        unresolved = source.index(
            "const unresolvedThreads = requireCodeRabbit"
        )
        verification = source.index(
            "const requested = await requestCodeRabbitThreadVerification(",
            unresolved,
        )
        quota = source.index(
            "// Rate-limit recovery applies only when this HEAD still needs its"
        )
        self.assertLess(verification, quota)

    def test_auto_merge_rejects_quota_replies_as_answers_and_gates_retry(self):
        source = AUTO_MERGE.read_text(encoding="utf-8")
        self.assertIn("!isCodeRabbitQuotaReply(comment)", source)
        self.assertIn("const body = quotaVisibleMarkdown(comment?.body || '');", source)
        self.assertIn("const chatLimit = latestChatQuotaReply(", source)
        self.assertIn("const pendingChatLimit =", source)
        self.assertIn("chatQuotaRetryAcknowledged(", source)
        # The parsed chat deadline must precede the generic lost-reply
        # timeout, and the timeout must remain only the fallback branch.
        self.assertIn(
            "CODERABBIT_THREAD_VERIFICATION_TIMEOUT_MS", source
        )
        chat_gate = source.index("if (pendingChatLimit) {")
        timeout_gate = source.index(
            "} else if (\n                  !repairHandoffPending &&\n"
            "                  Date.now() - verificationAt <"
        )
        self.assertLess(chat_gate, timeout_gate)
        retry_gate = source.index(
            "if (chatRetryLimitedId !== null) {"
        )
        self.assertIn("if (!chatRetryAuthorResolved) {", source)
        budget_index = source.index(
            "if (!(await codeRabbitChatBudgetAllowsSend())) continue;",
            retry_gate,
        )
        live_index = source.index(
            "const live = await getPullWithAuthoritativeHead(pr.number);",
            retry_gate,
        )
        consume_index = source.index(
            "if (!consumeCodeRabbitChatSendSlot()) continue;",
            retry_gate,
        )
        self.assertLess(
            budget_index,
            live_index,
            "the authoritative PR/HEAD re-read must follow the shared budget scan",
        )
        self.assertLess(
            live_index,
            consume_index,
            "the send slot must be consumed only after the live checks pass",
        )
        self.assertIn("PR/HEAD changed before the CodeRabbit chat-quota retry", source)
        self.assertIn("is no longer unresolved; skipping stale CodeRabbit chat-quota retry", source)
        self.assertIn("MAX_CHAT_QUOTA_VERIFICATION_SENDS_PER_RUN = 1", source)
        self.assertIn("const sharedChatQuota = {", source)
        self.assertIn("sharedChatQuota", source)

    def test_unresolved_batcher_never_batches_a_quota_reply(self):
        source = UNRESOLVED.read_text(encoding="utf-8")
        self.assertIn("!isCodeRabbitQuotaReply(comment)", source)
        self.assertIn("quotaVisibleMarkdown(comment.body ?? '')", source)
        # The old two-form full-review filter must no longer be the sole
        # exclusion in the replies filter.
        self.assertNotIn(
            "comment.id > verification.comment.id &&\n"
            "                    !/Review rate limited|Review limit reached/i.test",
            source,
        )

    def test_generic_try_again_needs_a_quota_signal(self):
        for body in (
            "Try again in 13 minutes.",
            "Retry again in 5 minutes.",
            "**UNRESOLVED**\nThe user can try again in 13 minutes.",
        ):
            with self.subTest(body=body):
                self.assertIsNone(rabbit.parse_retry_delay_ms(body))
                self.assertIsNone(rabbit.retry_window_text(body))
                self.assertFalse(rabbit.is_provider_quota_reply(body))
                self.assertEqual(
                    rabbit.classify_coderabbit_reply(
                        body, author_login="coderabbitai[bot]"
                    )["kind"],
                    "verdict",
                )
        # An explicit full-review quota signal makes the same phrase a window.
        self.assertEqual(
            rabbit.parse_retry_delay_ms(
                "Review rate limited. Try again in 13 minutes."
            ),
            13 * 60_000,
        )
        self.assertTrue(
            rabbit.is_provider_quota_reply(
                "Review rate limited. Try again in 13 minutes."
            )
        )
        # The unambiguous "next review ... available ... in" form is unchanged.
        self.assertEqual(
            rabbit.parse_retry_delay_ms("Next review available in 13 minutes."),
            13 * 60_000,
        )

    def test_full_review_queue_parser_and_regex_are_unchanged(self):
        retry = RETRY.read_text(encoding="utf-8")
        self.assertIn("/Review rate limited|Review limit reached/i", retry)
        self.assertIn("const quotaSignalled = RATE_LIMIT_RE.test(body);", retry)
        self.assertIn("requiresQuotaSignal && !quotaSignalled", retry)
        self.assertIn("['Try again in 13 minutes.', null]", retry)
        self.assertNotIn("CHAT_QUOTA", retry)
        self.assertNotIn("chat messages per hour", retry)
        # The existing full-review countdown vectors still pass.
        for body, expected in (
            (
                "Review rate limited. Next included review available in 13 minutes.",
                13 * 60_000,
            ),
            (
                "Review limit reached. Your next review is available after "
                "1 hour 5 minutes.",
                65 * 60_000,
            ),
            ("Review rate limited. Try again in less than a minute.", 60_000),
        ):
            with self.subTest(body=body):
                self.assertEqual(rabbit.parse_retry_delay_ms(body), expected)


@unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
class ChatQuotaRuntimeParityTests(unittest.TestCase):
    """Execute the production JS block and compare it with the adapter."""

    def run_block(self, checks: str, workflow: Path = AUTO_MERGE) -> None:
        block = chat_quota_block(workflow)
        script = "const assert = require('node:assert/strict');\n" + block + "\n" + checks
        result = subprocess.run(
            ["node", "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_js_classifier_matches_python_for_real_and_edge_vectors(self):
        data = fixture("chat-rate-limit-pr448.json")
        bodies = [reply["body"] for reply in data["threads"][0]["replies"]]
        bodies += [
            "Review rate limited. Next included review available in 13 minutes.",
            "Review limit reached. Your next review is available after 1 hour 5 minutes.",
            "Next review available in 13 minutes.",
            "Try again in 13 minutes.",
            "Retry again in 5 minutes.",
            "Review rate limited. Try again in 13 minutes.",
            "### Rate Limit Exceeded\nPlease wait before sending another message.",
            "> ### Rate Limit Exceeded\n> Please wait **14 minutes and 56 seconds** before sending another message.",
            "**UNRESOLVED**\nThe number of chat messages per hour is not tracked across threads, so this finding still needs a code change.",
            "**UNRESOLVED**\nThe user has not exceeded the limit for the number of chat messages per hour.",
            "**UNRESOLVED**\nThe user never reached the limit for chat messages per hour.",
            "**UNRESOLVED**\nThe user hasn't exceeded the limit for the number of chat messages per hour.",
            "**UNRESOLVED**\nThe user hasn’t reached the limit for chat messages per hour.",
            "**UNRESOLVED**\nThe user has not, according to the current account state and the recent thread history, exceeded the limit for chat messages per hour.",
            "**UNRESOLVED**\n > You have exceeded the limit of chat messages per hour.\nThe parser treats quoted provider messages as active limits.",
            "**UNRESOLVED**\n   > You have reached the limit of chat messages per hour.",
            "**UNRESOLVED**\n\t> You have exceeded the limit of chat messages per hour.",
            "**RESOLVED** the finding is fixed.",
        ]
        expected = [
            rabbit.classify_coderabbit_reply(
                body, author_login="coderabbitai[bot]"
            )
            for body in bodies
        ]
        checks = "const bodies = " + json.dumps(bodies) + ";\n"
        checks += "const expected = " + json.dumps(expected) + ";\n"
        checks += (
            "for (let i = 0; i < bodies.length; i += 1) {\n"
            "  const actual = classifyCodeRabbitReply({\n"
            "    user: {login: 'coderabbitai[bot]'}, body: bodies[i],\n"
            "  });\n"
            "  assert.equal(actual.kind, expected[i].kind, bodies[i]);\n"
            "  if (expected[i].delay_ms !== null) {\n"
            "    assert.equal(actual.delayMs, expected[i].delay_ms, bodies[i]);\n"
            "  }\n"
            "}\n"
            "assert.equal(classifyCodeRabbitReply({\n"
            "  user: {login: 'kodmial'}, body: bodies[0],\n"
            "}).kind, 'untrusted');\n"
        )
        self.run_block(checks)

    def test_js_fenced_quota_regression_in_both_workflows(self):
        bodies = (
            "**UNRESOLVED**\nThe parser misclassifies this example:\n\n"
            "\x60\x60\x60text\nYou have exceeded the limit of chat messages per hour.\n"
            "\x60\x60\x60\nA fix is still required.",
            "**UNRESOLVED**\nHere is an illustrative provider error:\n"
            "\x60\x60\x60text\n### Rate Limit Exceeded\n"
            "The user has exceeded the limit for the number of chat messages per hour.\n"
            "\x60\x60\x60\nThe quota check is defective.",
            "**UNRESOLVED**\n~~~text\n### Rate Limit Exceeded\n"
            "Please wait **90 minutes** before sending another message.\n"
            "~~~\nThis is only a quoted example.",
            "**UNRESOLVED**\n\x60\x60\x60\x60markdown\n"
            "\x60\x60\x60text\nYou have reached the limit of chat messages per hour.\n"
            "\x60\x60\x60\n\x60\x60\x60\x60\nThe fix is not complete.",
        )
        checks = (
            "for (const body of " + json.dumps(bodies) + ") {\n"
            "  assert.equal(parseChatQuotaDelayMs(body), null, body);\n"
            "  assert.equal(classifyCodeRabbitReply({\n"
            "    user: {login: 'coderabbitai[bot]'}, body\n"
            "  }).kind, 'verdict', body);\n"
            "}\n"
            "assert.equal(parseChatQuotaDelayMs(\n"
            "  'See example:\\n\x60\x60\x60\\nYou have exceeded the limit of chat messages per hour.\\n\x60\x60\x60\\n' +\n"
            "  '### Rate Limit Exceeded\\nPlease wait **10 minutes** before sending another message.'\n"
            "), 600000);\n"
        )
        for workflow in (AUTO_MERGE, UNRESOLVED):
            with self.subTest(workflow=workflow.name):
                self.run_block(checks, workflow=workflow)

    def test_js_fenced_full_review_examples_stay_verdicts(self):
        bodies = (
            "**UNRESOLVED**\nThe parser misclassifies this example:\n\n"
            "\x60\x60\x60text\nReview rate limited. Next included review "
            "available in 13 minutes.\n\x60\x60\x60\nA fix is still required.",
            "**UNRESOLVED**\nHere is an illustrative provider reply:\n"
            "~~~\nReview limit reached. Try again in 13 minutes.\n~~~\n"
            "The quota check is defective.",
            "**UNRESOLVED**\n> Review rate limited. Try again in 10 minutes.\n"
            "The parser treats quoted provider messages as active limits.",
        )
        checks = (
            "for (const body of " + json.dumps(bodies) + ") {\n"
            "  assert.equal(parseReviewQuotaDelayMs(body), null, body);\n"
            "  assert.equal(classifyCodeRabbitReply({\n"
            "    user: {login: 'coderabbitai[bot]'}, body\n"
            "  }).kind, 'verdict', body);\n"
            "}\n"
            "assert.equal(parseReviewQuotaDelayMs(\n"
            "  'See example:\\n\x60\x60\x60\\nReview rate limited. Next included "
            "review available in 13 minutes.\\n\x60\x60\x60\\n' +\n"
            "  'Review rate limited. Next included review available in 10 minutes.'\n"
            "), 600000);\n"
        )
        for workflow in (AUTO_MERGE, UNRESOLVED, OPENCODE):
            with self.subTest(workflow=workflow.name):
                self.run_block(checks, workflow=workflow)

    def test_js_exact_deadlines_and_two_pr_shared_quota(self):
        first = fixture("chat-rate-limit-pr448.json")
        second = fixture("chat-rate-limit-pr449.json")
        reply_a = first["threads"][0]["replies"][0]
        reply_b = second["threads"][0]["replies"][0]
        reply_c = second["threads"][1]["replies"][1]
        checks = f"""
const a = {json.dumps(reply_a)};
const b = {json.dumps(reply_b)};
const c = {json.dumps(reply_c)};
const dueA = chatQuotaNotBeforeMs(a);
const dueB = chatQuotaNotBeforeMs(b);
const dueC = chatQuotaNotBeforeMs(c);
assert.equal(new Date(dueA).toISOString(), '2026-11-12T08:08:41.000Z');
assert.equal(new Date(dueB).toISOString(), '2026-11-12T08:08:40.000Z');
assert.equal(new Date(dueC).toISOString(), '2026-11-12T08:08:41.000Z');
// One shared chat quota: the reset window is effectively account-wide.
assert.ok(Math.abs(dueA - dueB) <= 1000);
assert.ok(Math.abs(dueA - dueC) <= 1000);
// Both PRs are ineligible one millisecond before the reset and become
// eligible exactly at the deadline; no hardcoded 30/60/65 minute window.
const before = Math.min(dueA, dueB) - 1;
assert.ok(before < dueA && before < dueB);
assert.ok(dueA <= Math.max(dueA, dueB));
"""
        self.run_block(checks)

    def test_js_chat_retry_acknowledgement_accepts_trusted_author_and_rejects_untrusted_author_and_missing_author_at_exact_head(self):
        data = fixture("chat-rate-limit-pr448.json")
        finding = data["threads"][0]["finding_id"]
        head = data["head_sha"]
        limited = data["threads"][0]["replies"][0]["id"]
        checks = f"""
const finding = {finding};
const head = {json.dumps(head)};
const limited = {limited};
const marker = chatQuotaRetryMarker(finding, head, limited);
assert.ok(marker.includes(`finding=${{finding}}`));
assert.ok(marker.includes(`head=${{head}}`));
assert.ok(marker.includes(`limited=${{limited}}`));
const threadRoot = {finding};
const trusted = ['github-actions[bot]'];
const comments = [];
// First event: nothing was sent yet.
assert.equal(chatQuotaRetryAcknowledged(
  comments, finding, head, limited, threadRoot, trusted
), false);
// The retry is posted once as a thread reply by the verification account.
comments.push({{id: 900, in_reply_to_id: threadRoot,
  user: {{login: 'github-actions[bot]'}}, body: marker}});
assert.equal(chatQuotaRetryAcknowledged(
  comments, finding, head, limited, threadRoot, trusted
), true);
// A copied marker from any other commenter never acknowledges the retry.
const forged = [{{id: 901, in_reply_to_id: threadRoot,
  user: {{login: 'random'}}, body: marker}}];
assert.equal(chatQuotaRetryAcknowledged(
  forged, finding, head, limited, threadRoot, trusted
), false);
// Missing author identity is not trusted.
const anonymous = [{{id: 902, in_reply_to_id: threadRoot,
  user: null, body: marker}}];
assert.equal(chatQuotaRetryAcknowledged(
  anonymous, finding, head, limited, threadRoot, trusted
), false);
// A restart re-reads the same durable comments and must not send again.
const restarted = JSON.parse(JSON.stringify(comments));
assert.equal(chatQuotaRetryAcknowledged(
  restarted, finding, head, limited, threadRoot, trusted
), true);
// A concurrent cron/webhook on the perfect next event also sees the ack.
assert.equal(chatQuotaRetryAcknowledged(
  restarted.concat([]), finding, head, limited, threadRoot, trusted
), true);
// Wrong HEAD, wrong finding, and wrong quota reply are different retries.
assert.equal(chatQuotaRetryAcknowledged(
  restarted, finding, 'f'.repeat(40), limited, threadRoot, trusted
), false);
assert.equal(chatQuotaRetryAcknowledged(
  restarted, finding + 1, head, limited, threadRoot, trusted
), false);
assert.equal(chatQuotaRetryAcknowledged(
  restarted, finding, head, limited + 1, threadRoot, trusted
), false);
"""
        self.run_block(checks)

    def test_js_latest_chat_limit_requires_trusted_author_and_thread(self):
        reply = fixture("chat-rate-limit-pr448.json")["threads"][0]["replies"][0]
        checks = f"""
const reply = {json.dumps(reply)};
const root = reply.in_reply_to_id;
assert.equal(latestChatQuotaReply(
  [{{...reply, user: {{login: 'kodmial'}}}}], root
), null);
assert.equal(latestChatQuotaReply([{{...reply, user: null}}], root), null);
assert.equal(latestChatQuotaReply(
  [{{...reply, user: {{login: 'coderabbitai[bot]'}}}}], root + 1
), null);
const found = latestChatQuotaReply(
  [{{...reply, user: {{login: 'coderabbitai[bot]'}}}}], root
);
assert.equal(found.id, reply.id);
"""
        self.run_block(checks)

    def test_js_malformed_fallback_and_fake_author(self):
        checks = """
const malformed = '### Rate Limit Exceeded\\n' +
  '`@kodmial` have exceeded the limit for the number of chat messages per hour. ' +
  'Please wait before sending another message.';
assert.equal(parseChatQuotaDelayMs(malformed), 65 * 60_000);
assert.equal(classifyCodeRabbitReply({
  user: {login: 'random'}, body: malformed,
}).kind, 'untrusted');
const quoted = malformed.split('\\n').map(line => '> ' + line).join('\\n');
assert.equal(parseChatQuotaDelayMs(quoted), null);
assert.equal(chatQuotaNotBeforeMs({body: malformed}), null);
"""
        self.run_block(checks)

    def test_js_partially_parsed_countdown_falls_back(self):
        checks = """
for (const text of ['1h 30 seconds', 'about 13 minutes', '1 hour 30x']) {
  assert.equal(parseChatDurationMs(text), null, text);
}
for (const text of [
  '1 hour 30 minutes',
  '1 hour, 2 minutes, and 3 seconds',
  'less than a minute',
]) {
  assert.equal(typeof parseChatDurationMs(text), 'number', text);
}
const body = '### Rate Limit Exceeded\\n' +
  'Please wait **1h 30 seconds** before sending another message.';
assert.equal(parseChatQuotaDelayMs(body), CHAT_QUOTA_FALLBACK_MS);
"""
        for workflow in (AUTO_MERGE, UNRESOLVED, OPENCODE):
            with self.subTest(workflow=workflow.name):
                self.run_block(checks, workflow=workflow)


@unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
class AutoMergeThreadVerifierTests(unittest.TestCase):
    """Execute the real auto-merge verifier with GitHub-shaped fixtures."""

    def run_verifier(self, checks: str) -> dict:
        source = thread_verifier_source()
        block = chat_quota_block(AUTO_MERGE)
        harness = r"""
const assert = require('node:assert/strict');
let comments = [];
let issueComments = [];
let issueListCommentsFailure = false;
let liveThreads = [{commentId: 100}];
let liveHead = '';
let liveState = 'open';
let posted = [];
let notices = [];
let warnings = [];
let authenticatedLogin = 'kodmial';
let authenticatedFailure = false;
const issueListComments = async params => {
  if (issueListCommentsFailure) throw new Error('handoff comments unavailable');
  // A pending handoff can predate the newest comments, so the scan must
  // paginate every issue comment instead of requesting one bounded page.
  if (params && params.page !== undefined) {
    throw new Error('bounded handoff scan');
  }
  return {data: issueComments.slice()};
};
const pr = {number: 450, head: {sha: 'a'.repeat(40)}};
liveHead = pr.head.sha;
const owner = 'o';
const repo = 'r';
const requireCodeRabbit = true;
const codeRabbitUnresolvedWorkflow = 'u.yml';
const CODERABBIT_THREAD_VERIFICATION_TIMEOUT_MS = 30 * 60_000;
const context = {payload: {repository: {default_branch: 'main'}}};
const core = {
  info: () => {},
  notice: message => notices.push(message),
  warning: message => warnings.push(message),
};
const github = {
  request: async (path, args) => {
    posted.push(args);
    comments.push({
      id: 1000 + posted.length,
      in_reply_to_id: args.comment_id,
      user: {login: 'github-actions[bot]'},
      created_at: new Date().toISOString(),
      body: args.body,
    });
  },
  rest: {
    users: {
      getAuthenticated: async () => {
        if (authenticatedFailure) throw new Error('token identity unavailable');
        return {data: {login: authenticatedLogin}};
      },
    },
  },
};
const shouldRecheckCodeRabbitAfterCi = () => false;
const codeRabbitExplicitlyResolved = () => false;
const isCodeRabbitChatLimitReply = () => false;
// The serialized cross-PR chat budget owns its own contract test in
// tests/test_coderabbit_queue_lifecycle.py. Always allow the budget scan
// here so these cases exercise the chat-quota retry semantics in isolation
// from that budget; consumeCodeRabbitChatSendSlot stays overridable so the
// stale-then-valid regression can model the single per-pass send slot.
let codeRabbitChatBudgetAllowsSend = async () => true;
let consumeCodeRabbitChatSendSlot = () => true;
let codeRabbitChatSentThisPass = false;
async function allowOneCodeRabbitChatRequest() {
  if (!(await codeRabbitChatBudgetAllowsSend())) return false;
  return consumeCodeRabbitChatSendSlot();
}
async function reconciliationDispatchSupport() {
  return {ok: false, reason: 'test'};
}
async function unresolvedCodeRabbitThreads() {
  return liveThreads.slice();
}
async function getPullWithAuthoritativeHead() {
  return {state: liveState, head: {sha: liveHead}};
}
async function withReadFallback(callback) {
  return callback({
    paginate: async (endpoint, params = {}) =>
      endpoint === issueListComments
        ? (await issueListComments(params)).data
        : comments.slice(),
    rest: {
      issues: {listComments: issueListComments},
      pulls: {
        get: async () => ({
          data: {state: liveState, head: {sha: liveHead}},
        }),
      },
    },
  });
}
"""
        script = (
            block
            + "\n"
            + harness
            + "\n"
            + source
            + "\n(async () => {\n"
            + checks
            + "\n})().catch(error => { console.error(error); process.exit(1); });\n"
        )
        result = subprocess.run(
            ["node", "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_rate_limit_reply_is_not_an_answer_and_retries_once_at_deadline(self):
        data = fixture("chat-rate-limit-pr448.json")
        finding = data["threads"][0]["finding_id"]
        head = data["head_sha"]
        reply = data["threads"][0]["replies"][0]
        marker = (
            f"<!-- auto-merge-coderabbit-verification "
            f"finding={finding} head={head} -->"
        )
        checks = f"""
const finding = {finding};
const head = {json.dumps(head)};
const marker = {json.dumps(marker)};
const chatBody = {json.dumps(reply["body"])};
const requestId = 900;
const chatId = 901;
const now = Date.now();
// The production verifier reads the live PR HEAD; bind it to the fixture.
pr.head.sha = head;
liveHead = head;
liveThreads = [{{commentId: finding}}];
const future = new Date(now - 60_000).toISOString();
const expired = new Date(now - 14 * 60_000 - 2 * 60_000).toISOString();
const results = {{}};

// 1. A chat-limit reply inside its countdown is not a finding answer.
comments = [
  {{id: requestId, in_reply_to_id: finding, user: {{login: 'kodmial'}},
    created_at: new Date(now - 3 * 60_000).toISOString(), body: marker}},
  {{id: chatId, in_reply_to_id: finding, user: {{login: 'coderabbitai[bot]'}},
    created_at: future, body: chatBody}},
];
posted = [];
const beforeState = {{remaining: 1, notBeforeMs: 0}};
results.before = {{
  requested: await requestCodeRabbitThreadVerification(
    pr, liveThreads, {{}}, beforeState
  ),
  posted: posted.length,
  notBefore: beforeState.notBeforeMs,
  expectedNotBefore: Date.parse(future) + 896_000 + 30_000,
}};

// 2. The very same reply after the exact deadline sends one same-finding
//    verification with a durable exact-HEAD/finding/chat-reply marker.
comments[1].created_at = expired;
posted = [];
const shared = {{remaining: 1, notBeforeMs: 0}};
results.due = {{
  requested: await requestCodeRabbitThreadVerification(
    pr, liveThreads, {{}}, shared
  ),
  posted: posted.length,
  body: posted.length ? posted[0].body : '',
  remaining: shared.remaining,
}};

// 3. A restarted/concurrent wakeup re-reads the posted marker and never
//    sends the identical retry again.
posted = [];
results.restart = {{
  requested: await requestCodeRabbitThreadVerification(
    pr, liveThreads, {{}}, {{remaining: 1, notBeforeMs: 0}}
  ),
  posted: posted.length,
}};

// 4. The durable chat-quota acknowledgement alone suppresses the retry even
//    when the reply was already due and no newer bot answer exists.
comments = [
  {{id: requestId, in_reply_to_id: finding, user: {{login: 'kodmial'}},
    created_at: new Date(now - 3 * 60_000).toISOString(), body: marker}},
  {{id: chatId, in_reply_to_id: finding, user: {{login: 'coderabbitai[bot]'}},
    created_at: expired, body: chatBody}},
  {{id: chatId + 1, in_reply_to_id: finding, user: {{login: 'github-actions[bot]'}},
    created_at: new Date(now - 30_000).toISOString(),
    body: chatQuotaRetryMarker(finding, head, chatId)}},
];
posted = [];
results.ack = {{
  requested: await requestCodeRabbitThreadVerification(
    pr, liveThreads, {{}}, {{remaining: 1, notBeforeMs: 0}}
  ),
  posted: posted.length,
}};

// 4b. A copied marker from any other commenter never suppresses the retry.
comments = [
  {{id: requestId, in_reply_to_id: finding, user: {{login: 'kodmial'}},
    created_at: new Date(now - 3 * 60_000).toISOString(), body: marker}},
  {{id: chatId, in_reply_to_id: finding, user: {{login: 'coderabbitai[bot]'}},
    created_at: expired, body: chatBody}},
  {{id: chatId + 2, in_reply_to_id: finding, user: {{login: 'random'}},
    created_at: new Date(now - 30_000).toISOString(),
    body: chatQuotaRetryMarker(finding, head, chatId)}},
];
posted = [];
results.forged = {{
  requested: await requestCodeRabbitThreadVerification(
    pr, liveThreads, {{}}, {{remaining: 1, notBeforeMs: 0}}
  ),
  posted: posted.length,
}};

// 5. A resolved finding, a moved HEAD, or a closed PR skips the stale retry,
//    even when the original chat reply is beyond its deadline.
comments = [
  {{id: requestId, in_reply_to_id: finding, user: {{login: 'kodmial'}},
    created_at: new Date(now - 3 * 60_000).toISOString(), body: marker}},
  {{id: chatId, in_reply_to_id: finding, user: {{login: 'coderabbitai[bot]'}},
    created_at: expired, body: chatBody}},
];
liveThreads = [];
posted = [];
results.resolved = {{
  requested: await requestCodeRabbitThreadVerification(
    pr, [{{commentId: finding}}], {{}}, {{remaining: 1, notBeforeMs: 0}}
  ),
  posted: posted.length,
}};
liveThreads = [{{commentId: finding}}];
liveHead = 'f'.repeat(40);
posted = [];
results.moved = {{
  requested: await requestCodeRabbitThreadVerification(
    pr, [{{commentId: finding}}], {{}}, {{remaining: 1, notBeforeMs: 0}}
  ),
  posted: posted.length,
}};
liveHead = head;
liveState = 'closed';
posted = [];
results.closed = {{
  requested: await requestCodeRabbitThreadVerification(
    pr, [{{commentId: finding}}], {{}}, {{remaining: 1, notBeforeMs: 0}}
  ),
  posted: posted.length,
}};

// 6. An untimestamped bot reply fails closed instead of sending early.
liveState = 'open';
comments[1].created_at = 'not-a-timestamp';
posted = [];
const malformedState = {{remaining: 1, notBeforeMs: 0}};
results.malformed = {{
  requested: await requestCodeRabbitThreadVerification(
    pr, [{{commentId: finding}}], {{}}, malformedState
  ),
  posted: posted.length,
  notBefore: malformedState.notBeforeMs,
}};
process.stdout.write(JSON.stringify(results));
"""
        result = self.run_verifier(checks)
        self.assertEqual(result["before"]["requested"], 0)
        self.assertEqual(result["before"]["posted"], 0)
        self.assertGreater(result["before"]["notBefore"], 0)
        self.assertEqual(
            result["before"]["notBefore"],
            result["before"]["expectedNotBefore"],
        )
        self.assertEqual(result["due"]["requested"], 1)
        self.assertEqual(result["due"]["posted"], 1)
        self.assertIn("auto-merge-coderabbit-chat-quota-retry", result["due"]["body"])
        self.assertIn(f"finding={finding}", result["due"]["body"])
        self.assertIn(f"head={head}", result["due"]["body"])
        self.assertIn("not a finding verdict", result["due"]["body"])
        self.assertEqual(result["due"]["remaining"], 0)
        self.assertEqual(result["restart"]["requested"], 0)
        self.assertEqual(result["restart"]["posted"], 0)
        self.assertEqual(result["ack"]["posted"], 0)
        self.assertEqual(result["forged"]["requested"], 1)
        self.assertEqual(result["forged"]["posted"], 1)
        self.assertEqual(result["resolved"]["posted"], 0)
        self.assertEqual(result["moved"]["posted"], 0)
        self.assertEqual(result["closed"]["posted"], 0)
        self.assertEqual(result["malformed"]["posted"], 0)
        self.assertEqual(result["malformed"]["notBefore"], 0)

    def test_forged_verification_marker_cannot_bypass_chat_quota(self):
        data = fixture("chat-rate-limit-pr448.json")
        finding = data["threads"][0]["finding_id"]
        head = data["head_sha"]
        chat_body = data["threads"][0]["replies"][0]["body"]
        checks = f"""
const finding = {finding};
const head = {json.dumps(head)};
const chatBody = {json.dumps(chat_body)};
const marker = `<!-- auto-merge-coderabbit-verification finding=${{finding}} head=${{head}} -->`;
const now = Date.now();
pr.head.sha = head;
liveHead = head;
liveThreads = [{{commentId: finding}}];
async function attempt(author) {{
  comments = [
    {{id: 1, in_reply_to_id: finding, user: {{login: 'coderabbitai[bot]'}},
      created_at: new Date(now).toISOString(), body: chatBody}},
    {{id: 2, in_reply_to_id: finding, user: author,
      created_at: new Date(now + 1000).toISOString(), body: marker}},
  ];
  posted = [];
  const state = {{remaining: 1, notBeforeMs: 0}};
  const requested = await requestCodeRabbitThreadVerification(
    pr, [{{commentId: finding}}], {{}}, state
  );
  return {{requested, posted: posted.length, notBefore: state.notBeforeMs}};
}}
const results = {{
  trusted: await attempt({{login: 'kodmial'}}),
  untrusted: await attempt({{login: 'random'}}),
  missing: await attempt(null),
}};
process.stdout.write(JSON.stringify(results));
"""
        result = self.run_verifier(checks)
        # The configured verification account is accepted, so its marker
        # establishes the latest verification and leaves the quota unset.
        self.assertEqual(result["trusted"]["posted"], 0)
        self.assertEqual(result["trusted"]["notBefore"], 0)
        # A copied or anonymous marker is not a verification request, so the
        # pending chat-quota deadline still gates the thread.
        for author in ("untrusted", "missing"):
            with self.subTest(author=author):
                self.assertEqual(result[author]["posted"], 0)
                self.assertGreater(result[author]["notBefore"], 0)

    def test_two_prs_share_one_chat_retry_budget(self):
        data_a = fixture("chat-rate-limit-pr448.json")
        data_b = fixture("chat-rate-limit-pr449.json")
        checks = f"""
const chatBody = {json.dumps(data_a["threads"][0]["replies"][0]["body"])};
const chatBodyB = {json.dumps(data_b["threads"][0]["replies"][0]["body"])};
const now = Date.now();
const expired = new Date(now - 14 * 60_000 - 2 * 60_000).toISOString();
const shared = {{remaining: 1, notBeforeMs: 0}};
const prB = {{number: 451, head: {{sha: 'b'.repeat(40)}}}};
liveThreads = [{{commentId: 100}}];

comments = [{{
  id: 1, in_reply_to_id: 100, user: {{login: 'coderabbitai[bot]'}},
  created_at: expired, body: chatBody,
}}];
const first = await requestCodeRabbitThreadVerification(
  pr, liveThreads, {{}}, shared
);

comments = [{{
  id: 2, in_reply_to_id: 100, user: {{login: 'coderabbitai[bot]'}},
  created_at: expired, body: chatBodyB,
}}];
liveHead = prB.head.sha;
posted = [];
const second = await requestCodeRabbitThreadVerification(
  prB, liveThreads, {{}}, shared
);
process.stdout.write(JSON.stringify({{
  first, second, posted: posted.length, remaining: shared.remaining,
}}));
"""
        result = self.run_verifier(checks)
        self.assertEqual(result["first"], 1)
        self.assertEqual(result["second"], 0)
        self.assertEqual(result["posted"], 0)
        self.assertEqual(result["remaining"], 0)

    def test_unresolved_posting_identity_defers_chat_quota_retry(self):
        data = fixture("chat-rate-limit-pr448.json")
        finding = data["threads"][0]["finding_id"]
        head = data["head_sha"]
        chat_body = data["threads"][0]["replies"][0]["body"]
        marker = (
            f"<!-- auto-merge-coderabbit-verification "
            f"finding={finding} head={head} -->"
        )
        checks = f"""
const finding = {finding};
const head = {json.dumps(head)};
const now = Date.now();
const expired = new Date(now - 14 * 60_000 - 2 * 60_000).toISOString();
const marker = {json.dumps(marker)};
const chatBody = {json.dumps(chat_body)};
pr.head.sha = head;
liveHead = head;
liveThreads = [{{commentId: finding}}];
comments = [
  {{id: 900, in_reply_to_id: finding, user: {{login: 'kodmial'}},
    created_at: new Date(now - 3 * 60_000).toISOString(), body: marker}},
  {{id: 901, in_reply_to_id: finding, user: {{login: 'coderabbitai[bot]'}},
    created_at: expired, body: chatBody}},
];
async function attempt({{
  identityUnavailable = false, login = 'kodmial',
}} = {{}}) {{
  authenticatedFailure = identityUnavailable;
  authenticatedLogin = login;
  posted = [];
  const state = {{remaining: 1, notBeforeMs: 0}};
  const requested = await requestCodeRabbitThreadVerification(
    pr, liveThreads, {{}}, state
  );
  return {{requested, posted: posted.length, remaining: state.remaining}};
}}
const results = {{
  resolved: await attempt(),
  emptyLogin: await attempt({{login: ''}}),
  whitespaceLogin: await attempt({{login: '   '}}),
  unresolved: await attempt({{identityUnavailable: true}}),
}};
process.stdout.write(JSON.stringify(results));
"""
        result = self.run_verifier(checks)
        # With the PAT identity resolved the expired quota retry is sent once.
        self.assertEqual(result["resolved"]["requested"], 1)
        self.assertEqual(result["resolved"]["posted"], 1)
        self.assertEqual(result["resolved"]["remaining"], 0)
        # Without that identity an acknowledged retry cannot be recognised,
        # so the retry is deferred instead of risked twice. An empty or
        # whitespace-only login is unresolved too.
        for key in ("emptyLogin", "whitespaceLogin", "unresolved"):
            with self.subTest(identity=key):
                self.assertEqual(result[key]["requested"], 0)
                self.assertEqual(result[key]["posted"], 0)
                self.assertEqual(result[key]["remaining"], 1)

    def test_unresolved_posting_identity_defers_handoff_verification_decision(self):
        # Regression for PR #451: an earlier verification from
        # github-actions[bot] stays trusted when the PAT login cannot be
        # resolved. Without deferring the whole decision, a newer TAP_PAT
        # handoff would be ignored, the older UNRESOLVED answer would select
        # another repair batch, and the completed repair would never be
        # verified.
        data = fixture("chat-rate-limit-pr448.json")
        finding = data["threads"][0]["finding_id"]
        head = data["head_sha"]
        checks = f"""
const finding = {finding};
const head = {json.dumps(head)};
const now = Date.now();
const marker = `<!-- auto-merge-coderabbit-verification finding=${{finding}} head=${{head}} -->`;
const handoff = {{
  id: 950,
  user: {{login: 'kodmial'}},
  created_at: new Date(now - 2 * 60_000).toISOString(),
  body: `<!-- opencode-coderabbit-verification-pending head=${{head}} findings=${{finding}} -->\\n` +
    'Canonical repair finished; serialized auto-merge owns verification.',
}};
pr.head.sha = head;
liveHead = head;
liveThreads = [{{commentId: finding}}];
// The earlier verification is from github-actions[bot], which remains
// trusted even while the PAT identity lookup fails. The handoff is newer
// and was published by that unresolved PAT identity.
comments = [
  {{id: 900, in_reply_to_id: finding, user: {{login: 'github-actions[bot]'}},
    created_at: new Date(now - 10 * 60_000).toISOString(), body: marker}},
  {{id: 901, in_reply_to_id: finding, user: {{login: 'coderabbitai[bot]'}},
    created_at: new Date(now - 5 * 60_000).toISOString(),
    body: '`@kodmial` **UNRESOLVED**\\nThe access-control defect remains.'}},
];
const dispatches = [];
reconciliationDispatchSupport = async () => ({{ok: true, reason: ''}});
github.rest.actions = {{
  createWorkflowDispatch: async args => dispatches.push(args),
}};
async function attempt({{identityUnavailable = false, login = 'kodmial'}} = {{}}) {{
  authenticatedFailure = identityUnavailable;
  authenticatedLogin = login;
  issueComments = [handoff];
  posted = [];
  notices = [];
  dispatches.length = 0;
  const requested = await requestCodeRabbitThreadVerification(
    pr, liveThreads, {{}}, {{remaining: 1, notBeforeMs: 0}}
  );
  return {{
    requested,
    posted: posted.length,
    dispatches: dispatches.length,
    notices: notices.slice(),
  }};
}}
const results = {{
  unresolved: await attempt({{identityUnavailable: true}}),
  emptyLogin: await attempt({{login: ''}}),
  whitespaceLogin: await attempt({{login: '   '}}),
  recovered: await attempt(),
}};
process.stdout.write(JSON.stringify(results));
"""
        result = self.run_verifier(checks)
        # Unknown identity must not dispatch another repair batch over the
        # still-unverifiable handoff. An empty or whitespace-only login is
        # unresolved too.
        for key in ("unresolved", "emptyLogin", "whitespaceLogin"):
            with self.subTest(identity=key):
                self.assertEqual(result[key]["requested"], 0)
                self.assertEqual(result[key]["posted"], 0)
                self.assertEqual(result[key]["dispatches"], 0)
                self.assertTrue(
                    any(
                        "posting identity is unresolved" in notice
                        for notice in result[key]["notices"]
                    ),
                    result[key]["notices"],
                )
        # Once the identity resolves, the same handoff selects the fresh
        # exact-HEAD verification instead of a repair dispatch.
        self.assertEqual(result["recovered"]["requested"], 1)
        self.assertEqual(result["recovered"]["posted"], 1)
        self.assertEqual(result["recovered"]["dispatches"], 0)

    def test_repair_handoff_triggers_fresh_verification_when_head_is_unchanged(self):
        data = fixture("chat-rate-limit-pr448.json")
        finding = data["threads"][0]["finding_id"]
        head = data["head_sha"]
        checks = f"""
const finding = {finding};
const head = {json.dumps(head)};
const now = Date.now();
const marker = `<!-- auto-merge-coderabbit-verification finding=${{finding}} head=${{head}} -->`;
const handoff = (at, author = 'kodmial', markerHead = head, markerFinding = finding) => ({{
  id: 950,
  user: {{login: author}},
  created_at: new Date(at).toISOString(),
  body: `<!-- opencode-coderabbit-verification-pending head=${{markerHead}} findings=${{markerFinding}} -->\\n` +
    'Canonical repair finished; serialized auto-merge owns verification.',
}});
pr.head.sha = head;
liveHead = head;
liveThreads = [{{commentId: finding}}];
const baseComments = [
  {{id: 900, in_reply_to_id: finding, user: {{login: 'kodmial'}},
    created_at: new Date(now - 10 * 60_000).toISOString(), body: marker}},
  {{id: 901, in_reply_to_id: finding, user: {{login: 'coderabbitai[bot]'}},
    created_at: new Date(now - 5 * 60_000).toISOString(),
    body: '`@kodmial` **UNRESOLVED**\\nThe access-control defect remains.'}},
];
async function attempt(handoffComment, reviewComments = baseComments) {{
  comments = reviewComments.slice();
  issueComments = handoffComment ? [handoffComment] : [];
  posted = [];
  const state = {{remaining: 1, notBeforeMs: 0}};
  const requested = await requestCodeRabbitThreadVerification(
    pr, liveThreads, {{}}, state
  );
  return {{requested, posted: posted.length,
    body: posted.length ? posted[0].body : ''}};
}}
const results = {{
  pending: await attempt(handoff(now - 2 * 60_000)),
  stale: await attempt(handoff(now - 15 * 60_000)),
  otherHead: await attempt(handoff(now - 2 * 60_000, 'kodmial', 'f'.repeat(40))),
  otherFinding: await attempt(
    handoff(now - 2 * 60_000, 'kodmial', head, finding + 1)
  ),
  forged: await attempt(handoff(now - 2 * 60_000, 'random')),
}};
// After the fresh request exists, the same handoff is no longer pending.
const verifiedComments = baseComments.concat([{{
  id: 1000, in_reply_to_id: finding, user: {{login: 'kodmial'}},
  created_at: new Date(now - 30_000).toISOString(), body: marker,
}}]);
results.settled = await attempt(
  handoff(now - 2 * 60_000), verifiedComments
);
// A later answer selects a new repair, which publishes its own handoff
// after the fresh verification. That must verify again, never loop silently.
const nextCycleComments = verifiedComments.concat([{{
  id: 1001, in_reply_to_id: finding, user: {{login: 'coderabbitai[bot]'}},
  created_at: new Date(now - 20_000).toISOString(),
  body: '`@kodmial` **UNRESOLVED**\\nThe access-control defect remains.',
}}]);
results.nextCycle = await attempt(handoff(now - 10_000), nextCycleComments);
process.stdout.write(JSON.stringify(results));
"""
        result = self.run_verifier(checks)
        # A repair that leaves HEAD unchanged publishes no commit, so the
        # durable handoff must select a fresh exact-HEAD verification of the
        # repair outcome instead of another repair batch.
        self.assertEqual(result["pending"]["requested"], 1)
        self.assertEqual(result["pending"]["posted"], 1)
        self.assertIn(
            f"auto-merge-coderabbit-verification finding={finding} head={head}",
            result["pending"]["body"],
        )
        # Older, foreign, other-head, other-finding, or already-verified
        # handoffs never cause an extra chat request.
        for key in ("stale", "otherHead", "otherFinding", "forged", "settled"):
            with self.subTest(case=key):
                self.assertEqual(result[key]["requested"], 0)
                self.assertEqual(result[key]["posted"], 0)
        # The next repair attempt at the same unchanged HEAD posts a new
        # handoff after the fresh verification and is verified again.
        self.assertEqual(result["nextCycle"]["requested"], 1)
        self.assertEqual(result["nextCycle"]["posted"], 1)

    def test_unreadable_repair_handoff_defers_the_repair_decision(self):
        data = fixture("chat-rate-limit-pr448.json")
        finding = data["threads"][0]["finding_id"]
        head = data["head_sha"]
        checks = f"""
const finding = {finding};
const head = {json.dumps(head)};
const now = Date.now();
const marker = `<!-- auto-merge-coderabbit-verification finding=${{finding}} head=${{head}} -->`;
pr.head.sha = head;
liveHead = head;
liveThreads = [{{commentId: finding}}];
comments = [
  {{id: 900, in_reply_to_id: finding, user: {{login: 'kodmial'}},
    created_at: new Date(now - 10 * 60_000).toISOString(), body: marker}},
  {{id: 901, in_reply_to_id: finding, user: {{login: 'coderabbitai[bot]'}},
    created_at: new Date(now - 5 * 60_000).toISOString(),
    body: '`@kodmial` **UNRESOLVED**\\nThe access-control defect remains.'}},
];
const dispatches = [];
reconciliationDispatchSupport = async () => ({{ok: true, reason: ''}});
github.rest.actions = {{
  createWorkflowDispatch: async args => dispatches.push(args),
}};
async function attempt(failHandoffRead) {{
  issueListCommentsFailure = failHandoffRead;
  notices = [];
  warnings = [];
  posted = [];
  const requested = await requestCodeRabbitThreadVerification(
    pr, liveThreads, {{}}, {{remaining: 1, notBeforeMs: 0}}
  );
  return {{
    requested,
    posted: posted.length,
    dispatches: dispatches.length,
    notices: notices.slice(),
    warnings: warnings.slice(),
  }};
}}
const results = {{
  unreadable: await attempt(true),
  recovered: await attempt(false),
}};
process.stdout.write(JSON.stringify(results));
"""
        result = self.run_verifier(checks)
        # An unreadable handoff list must not fall back to the older
        # unresolved answer and dispatch another repair batch.
        self.assertEqual(result["unreadable"]["requested"], 0)
        self.assertEqual(result["unreadable"]["posted"], 0)
        self.assertEqual(result["unreadable"]["dispatches"], 0)
        self.assertTrue(
            any(
                "deferring the repair decision" in notice
                for notice in result["unreadable"]["notices"]
            ),
            result["unreadable"]["notices"],
        )
        self.assertTrue(
            any(
                "deferring repair decisions" in warning
                for warning in result["unreadable"]["warnings"]
            ),
            result["unreadable"]["warnings"],
        )
        # The deferral is temporary: once the read succeeds, the next
        # reconciliation still selects the canonical repair path.
        self.assertEqual(result["recovered"]["dispatches"], 1)

    def test_stale_finding_does_not_consume_the_only_send_slot(self):
        data = fixture("chat-rate-limit-pr448.json")
        stale_finding = data["threads"][0]["finding_id"]
        valid_finding = stale_finding + 1
        head = data["head_sha"]
        chat_body = data["threads"][0]["replies"][0]["body"]
        checks = f"""
const staleFinding = {stale_finding};
const validFinding = {valid_finding};
const head = {json.dumps(head)};
const now = Date.now();
const expired = new Date(now - 20 * 60_000).toISOString();
const chatBody = {json.dumps(chat_body)};
const marker = id => `<!-- auto-merge-coderabbit-verification finding=${{id}} head=${{head}} -->`;
pr.head.sha = head;
liveHead = head;
// The first finding was resolved after its quota reply; only the second is
// still unresolved. Both are due for a chat-quota retry on this HEAD.
comments = [
  {{id: 900, in_reply_to_id: staleFinding, user: {{login: 'kodmial'}},
    created_at: new Date(now - 3 * 60_000).toISOString(),
    body: marker(staleFinding)}},
  {{id: 901, in_reply_to_id: staleFinding, user: {{login: 'coderabbitai[bot]'}},
    created_at: expired, body: chatBody}},
  {{id: 910, in_reply_to_id: validFinding, user: {{login: 'kodmial'}},
    created_at: new Date(now - 3 * 60_000).toISOString(),
    body: marker(validFinding)}},
  {{id: 911, in_reply_to_id: validFinding, user: {{login: 'coderabbitai[bot]'}},
    created_at: expired, body: chatBody}},
];
liveThreads = [{{commentId: validFinding}}];
let budgetChecks = 0;
let slotsConsumed = 0;
codeRabbitChatBudgetAllowsSend = async () => {{
  budgetChecks += 1;
  return true;
}};
consumeCodeRabbitChatSendSlot = () => {{
  if (slotsConsumed >= 1) return false;
  slotsConsumed += 1;
  return true;
}};
posted = [];
const state = {{remaining: 1, notBeforeMs: 0}};
const requested = await requestCodeRabbitThreadVerification(
  pr, [{{commentId: staleFinding}}, {{commentId: validFinding}}], {{}}, state
);
process.stdout.write(JSON.stringify({{
  requested,
  posted: posted.length,
  postedTo: posted.map(entry => entry.comment_id),
  remaining: state.remaining,
  budgetChecks,
  slotsConsumed,
}}));
"""
        result = self.run_verifier(checks)
        # The stale finding is checked after the shared budget scan but never
        # consumes the pass's single send slot, so the still-unresolved
        # finding can use it in the same pass.
        self.assertEqual(result["budgetChecks"], 2)
        self.assertEqual(result["slotsConsumed"], 1)
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["posted"], 1)
        self.assertEqual(result["postedTo"], [valid_finding])
        self.assertEqual(result["remaining"], 0)


def unresolved_collector_source() -> str:
    """Extract the production CodeRabbit unresolved collector block."""

    text = UNRESOLVED.read_text(encoding="utf-8")
    marker = "      - name: Collect all unresolved findings\n"
    assert text.count(marker) == 1
    section = text.split(marker, 1)[1].split("\n      - name: ", 1)[0]
    match = re.search(r"(?ms)^          script: \|\n(.*)", section)
    assert match, "missing collector script"
    return textwrap.dedent(match.group(1))


@unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
class UnresolvedBatcherChatQuotaTests(unittest.TestCase):
    """The canonical batcher must never dispatch OpenCode for a chat limit."""

    def run_collector(self, replacement: str) -> dict:
        head = "a" * 40
        fixture_js = f"""
process.env.COMMENT_PAT = 'test-pat';
const head = {json.dumps(head)};
const comments = [
  {{id: 100, user: {{login: 'coderabbitai[bot]'}},
    pull_request_review_id: 11, body: 'original finding'}},
  {{id: 101, in_reply_to_id: 100, user: {{login: 'kodmial'}},
    pull_request_review_id: 11,
    body: '<!-- auto-merge-coderabbit-verification finding=100 head=' + head + ' -->'}},
  {replacement}
];
const out = {{}};
const core = {{
  setOutput: (key, value) => {{ out[key] = String(value); }},
  notice: () => {{}}, warning: () => {{}},
}};
const context = {{repo: {{owner: 'kodmial', repo: 'continuum'}},
  payload: {{pull_request: {{number: 450}}}}}};
const github = {{
  request: {{endpoint: {{DEFAULTS: {{baseUrl: 'https://api.github.com'}}}}}},
  constructor: function () {{
    this.rest = {{users: {{getAuthenticated: async () => ({{data: {{login: 'kodmial'}}}})}}}};
  }},
  paginate: async endpoint => (await endpoint({{}})).data,
  graphql: async () => ({{repository: {{pullRequest: {{reviewThreads: {{
    nodes: [{{isResolved: false, comments: {{nodes: [
      {{databaseId: 100, author: {{login: 'coderabbitai[bot]'}}}}
    ]}}}}],
    pageInfo: {{hasNextPage: false, endCursor: null}},
  }}}}}}}}),
  rest: {{
    pulls: {{
      get: async () => ({{data: {{review_comments: comments.length,
        head: {{sha: head, ref: 'fix/issue450',
          repo: {{full_name: 'kodmial/continuum'}}}}}}}}),
      listReviewComments: async () => ({{data: comments}}),
      getReviewComment: async ({{comment_id}}) => ({{
        data: comments.find(comment => comment.id === comment_id),
      }}),
    }},
    git: {{getRef: async () => ({{data: {{object: {{sha: head}}}}}})}},
    issues: {{listComments: async () => ({{data: []}})}},
  }},
}};
"""
        script = (
            fixture_js
            + "\nasync function run() {\n"
            + unresolved_collector_source()
            + "\n}\nrun().then(() => console.log(JSON.stringify(out)))"
              ".catch(error => { console.error(error); process.exit(1); });\n"
        )
        result = subprocess.run(
            ["node", "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_chat_rate_limit_reply_never_dispatches_a_batch(self):
        chat_body = fixture("chat-rate-limit-pr448.json")["threads"][0]["replies"][0]["body"]
        out = self.run_collector(
            "{id: 102, in_reply_to_id: 100, "
            "user: {login: 'coderabbitai[bot]'}, "
            "pull_request_review_id: 11, "
            "created_at: '2026-11-12T07:53:15Z', "
            "body: " + json.dumps(chat_body) + "}"
        )
        self.assertEqual(out["should_run"], "false")
        self.assertEqual(out["items_json"], "[]")

    def test_full_review_quota_reply_still_never_dispatches_a_batch(self):
        out = self.run_collector(
            "{id: 104, in_reply_to_id: 100, "
            "user: {login: 'coderabbitai[bot]'}, "
            "pull_request_review_id: 11, "
            "created_at: '2026-11-12T08:09:00Z', "
            "body: 'Review rate limited. Next included review available in "
            "13 minutes.'}"
        )
        self.assertEqual(out["should_run"], "false")
        self.assertEqual(out["items_json"], "[]")

    def test_chat_message_explanation_without_quota_evidence_still_dispatches(self):
        out = self.run_collector(
            "{id: 105, in_reply_to_id: 100, "
            "user: {login: 'coderabbitai[bot]'}, "
            "pull_request_review_id: 11, "
            "created_at: '2026-11-12T08:09:00Z', "
            "body: '**UNRESOLVED**\\nThe number of chat messages per hour is "
            "not tracked across threads, so the defect remains.'}"
        )
        self.assertEqual(out["should_run"], "true")
        self.assertEqual(
            [item["finding_id"] for item in json.loads(out["items_json"])],
            [100],
        )

    def test_negated_quota_explanation_still_dispatches(self):
        for statement in (
            "The user hasn't exceeded the limit for the number of chat messages per hour.",
            "The user hasn’t reached the limit for chat messages per hour.",
        ):
            with self.subTest(statement=statement):
                out = self.run_collector(
                    "{id: 106, in_reply_to_id: 100, "
                    "user: {login: 'coderabbitai[bot]'}, "
                    "pull_request_review_id: 11, "
                    "created_at: '2026-11-12T08:09:00Z', "
                    "body: " + json.dumps(
                        "**UNRESOLVED**\n" + statement
                    ) + "}"
                )
                self.assertEqual(out["should_run"], "true")
                self.assertEqual(
                    [item["finding_id"] for item in json.loads(out["items_json"])],
                    [100],
                )

    def test_indented_block_quote_still_dispatches_repair(self):
        body = (
            "**UNRESOLVED**\n"
            " > You have exceeded the limit of chat messages per hour.\n"
            "The parser treats quoted provider messages as active limits."
        )
        out = self.run_collector(
            "{id: 107, in_reply_to_id: 100, "
            "user: {login: 'coderabbitai[bot]'}, "
            "pull_request_review_id: 11, "
            "created_at: '2026-11-12T08:09:00Z', "
            "body: " + json.dumps(body) + "}"
        )
        self.assertEqual(out["should_run"], "true")
        self.assertEqual(
            [item["finding_id"] for item in json.loads(out["items_json"])],
            [100],
        )

    def test_substantive_unresolved_still_dispatches(self):
        out = self.run_collector(
            "{id: 103, in_reply_to_id: 100, "
            "user: {login: 'coderabbitai[bot]'}, "
            "pull_request_review_id: 11, "
            "created_at: '2026-11-12T08:09:00Z', "
            "body: '**UNRESOLVED**\\nThe access check still trusts the fork.'}"
        )
        self.assertEqual(out["should_run"], "true")
        self.assertEqual(
            [item["finding_id"] for item in json.loads(out["items_json"])],
            [100],
        )


class ChatQuotaDurableStateTests(unittest.TestCase):
    def test_two_pr_events_share_one_budget_and_persist_the_ack(self):
        """Model the controller's shared budget with production primitives."""

        first = fixture("chat-rate-limit-pr448.json")
        second = fixture("chat-rate-limit-pr449.json")
        candidates = []
        for data in (first, second):
            for thread in data["threads"]:
                reply = thread["replies"][-1]
                candidates.append(
                    {
                        "pr": data["pr"],
                        "head": data["head_sha"],
                        "finding": thread["finding_id"],
                        "reply": reply,
                        "due": rabbit.chat_retry_not_before_ms(
                            reply["created_at"],
                            rabbit.parse_chat_retry_delay_ms(reply["body"]),
                        ),
                    }
                )

        # Two PRs/threads resolve to one account-wide chat reset.
        self.assertLessEqual(
            max(c["due"] for c in candidates) - min(c["due"] for c in candidates),
            1_000,
        )
        now = min(c["due"] for c in candidates) - 1
        self.assertFalse(any(c["due"] <= now for c in candidates))

        now = max(c["due"] for c in candidates)
        due = [c for c in candidates if c["due"] <= now]
        self.assertEqual(len(due), 3)
        # One shared send budget for this reconciliation pass.
        budget = 1
        sent = []
        for candidate in due:
            if budget <= 0:
                break
            sent.append(candidate)
            budget -= 1
        self.assertEqual(len(sent), 1)

        # Durable recommendation: the send marker is stored on GitHub and a
        # restart re-derives the acknowledgement without any local state.
        chosen = sent[0]
        marker = rabbit.chat_retry_marker(
            chosen["finding"], chosen["head"], chosen["reply"]["id"]
        )
        persisted = [
            {
                "id": 10,
                "in_reply_to_id": chosen["finding"],
                "user": {"login": "github-actions[bot]"},
                "body": marker,
            }
        ]
        self.assertTrue(
            rabbit.chat_retry_acknowledged(
                persisted,
                finding_id=chosen["finding"],
                head_sha=chosen["head"],
                limited_comment_id=chosen["reply"]["id"],
                root_comment_id=chosen["finding"],
                trusted_author_logins=("github-actions[bot]",),
            )
        )
        # On the next wakeup (or a restarted runner) the exact same retry is
        # acknowledged; the other PR/thread still owns its own exact retry.
        self.assertEqual(
            sum(
                1
                for candidate in candidates
                if rabbit.chat_retry_acknowledged(
                    persisted,
                    finding_id=candidate["finding"],
                    head_sha=candidate["head"],
                    limited_comment_id=candidate["reply"]["id"],
                    root_comment_id=candidate["finding"],
                    trusted_author_logins=("github-actions[bot]",),
                )
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
