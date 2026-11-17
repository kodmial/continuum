#!/usr/bin/env bash
set -euo pipefail

ruby -E UTF-8 scripts/test-continuum.rb
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m continuum.trigger_manifest --check

mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"

# Changed-line coverage gate for the Python engine. The gate is mandatory: a
# shallow checkout or a missing coverage.py must fail loudly instead of
# turning the check into an inert skip.
coverage_base="${CONTINUUM_COVERAGE_BASE:-origin/main}"
if ! git rev-parse --verify --quiet "${coverage_base}^{commit}" >/dev/null; then
  if [[ "$coverage_base" == origin/* ]]; then
    base_branch="${coverage_base#origin/}"
    echo "coverage: base '$coverage_base' is missing; fetching it from origin" >&2
    if ! git fetch --no-tags origin \
      "+refs/heads/${base_branch}:refs/remotes/origin/${base_branch}" >&2; then
      echo "coverage: cannot fetch base '$coverage_base' from origin; refusing to skip the changed-line gate" >&2
      exit 1
    fi
  fi
fi
if ! git rev-parse --verify --quiet "${coverage_base}^{commit}" >/dev/null; then
  echo "coverage: base '$coverage_base' is unavailable; a shallow checkout cannot prove the changed-line gate. Fetch full history (actions/checkout fetch-depth: 0) or set CONTINUUM_COVERAGE_BASE to a reachable base commit." >&2
  exit 1
fi
if ! python3 -c 'import coverage' >/dev/null 2>&1; then
  echo "coverage: coverage.py is required for the changed-line gate; installing it" >&2
  if ! python3 -m pip install --quiet --disable-pip-version-check 'coverage>=7' >&2; then
    echo "coverage: cannot install coverage.py; refusing to skip the changed-line gate" >&2
    exit 1
  fi
fi
coverage_file="$PWD/.opencode-tmp/.coverage"
rm -f "$coverage_file" "$PWD/.opencode-tmp/coverage.json"
COVERAGE_FILE="$coverage_file" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m coverage run --source=src -m unittest discover -s tests
COVERAGE_FILE="$coverage_file" python3 -m coverage json \
  -o "$PWD/.opencode-tmp/coverage.json" >/dev/null
python3 - "$coverage_base" "$PWD/.opencode-tmp/coverage.json" <<'COVERAGE_PY'
import json
import re
import subprocess
import sys

base, report_path = sys.argv[1], sys.argv[2]
try:
    merge_base = subprocess.check_output(
        ["git", "merge-base", base, "HEAD"], text=True, stderr=subprocess.PIPE
    ).strip()
except subprocess.CalledProcessError as error:
    sys.exit(
        f"coverage: cannot compute merge-base for {base} and HEAD; "
        "fetch the full history (actions/checkout fetch-depth: 0)"
        + (f": {error.stderr.strip()}" if error.stderr else "")
    )
# Diff the merge base against the working tree, not HEAD: validation runs on
# uncommitted changes too, and a HEAD-only diff would silently measure the
# committed engine instead of the code coverage actually executed below.
diff = subprocess.check_output(
    ["git", "diff", "-U0", merge_base, "--", "src"], text=True
)
changed = {}
current = None
for line in diff.splitlines():
    if line.startswith("+++ b/"):
        current = line[len("+++ b/"):]
        changed.setdefault(current, set())
    elif current and line.startswith("@@"):
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if match:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            changed[current].update(range(start, start + count))

with open(report_path, encoding="utf-8") as handle:
    report = json.load(handle)
executed = 0
measured = 0
for path, lines in changed.items():
    entry = report["files"].get(path)
    if not entry:
        continue
    runnable = set(entry["executed_lines"]) | set(entry["missing_lines"])
    considered = lines & runnable
    measured += len(considered)
    executed += len(considered & set(entry["executed_lines"]))
if measured == 0:
    print("coverage: no executable changed Python lines")
else:
    percent = 100.0 * executed / measured
    print(f"coverage: {executed}/{measured} changed Python lines = {percent:.1f}%")
    if percent < 80.0:
        sys.exit("coverage: changed-line coverage below the 80% threshold")
COVERAGE_PY
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'

# Print the shared CodeRabbit chat-quota regression by name in the exact-HEAD
# gate log. CodeRabbit excludes tests/** from review, so the named result is
# the only reviewable evidence that a heading-only 90-minute reply on one PR
# still blocks a chat send from another PR.
printf '%s\n' \
  'CodeRabbit chat-quota regression evidence:' \
  'setup: two open PRs; one has a trusted heading-only "### Rate Limit Exceeded" reply with a 90-minute countdown, the other has a shorter 10-minute countdown.' \
  'assertions: the account-wide scan blocks a send from either PR one millisecond before the shared 90-minute deadline and allows the send at that deadline.'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_coderabbit_queue_lifecycle.CodeRabbitChatBudgetRegressionTests.test_account_wide_scan_uses_shared_heading_only_deadline

# Print the CodeRabbit chat-retry author-trust cases by name in the exact-HEAD
# gate log. CodeRabbit excludes tests/** from review, so the named
# trusted/untrusted/missing-author results are the only reviewable evidence
# that a copied or anonymous acknowledgment marker cannot suppress a retry.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_coderabbit_chat_quota.ChatQuotaParserTests.test_chat_retry_acknowledgement_accepts_trusted_author_and_rejects_untrusted_author_and_missing_author_at_exact_head \
  tests.test_coderabbit_chat_quota.ChatQuotaRuntimeParityTests.test_js_chat_retry_acknowledgement_accepts_trusted_author_and_rejects_untrusted_author_and_missing_author_at_exact_head \
  tests.test_coderabbit_chat_quota.AutoMergeThreadVerifierTests.test_forged_verification_marker_cannot_bypass_chat_quota

# Print the negated-quota regression by name in the exact-HEAD gate log.
# CodeRabbit excludes tests/** from review, so the named results are the only
# reviewable evidence that a finding which merely discusses an unexceeded chat
# quota is still forwarded to repair instead of being treated as a rate limit.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_coderabbit_chat_quota.ChatQuotaParserTests.test_negated_hourly_quota_in_a_finding_is_not_a_rate_limit \
  tests.test_coderabbit_chat_quota.ChatQuotaRuntimeParityTests.test_js_classifier_matches_python_for_real_and_edge_vectors \
  tests.test_coderabbit_chat_quota.UnresolvedBatcherChatQuotaTests.test_negated_quota_explanation_still_dispatches

# Print the repair-completion handoff regressions by name in the exact-HEAD
# gate log. CodeRabbit excludes tests/** from review, so the named results are
# the only reviewable evidence that a repair which leaves HEAD unchanged still
# hands verification to auto-merge instead of re-entering repair batches.
printf '%s\n' \
  'CodeRabbit repair-handoff regression evidence:' \
  'setup: a newer TAP_PAT verification-pending handoff follows an older trusted verification and UNRESOLVED answer at the same HEAD; the authenticated-login lookup then fails, returns an empty login, and finally succeeds.' \
  'setup: the repair-handoff comment list is also unreadable, so the decision must defer instead of redispatching the older UNRESOLVED answer.' \
  'assertions: an unresolved posting identity or unreadable handoff posts no verification reply and dispatches no repair batch, while the resolved identity sends one fresh exact-HEAD verification from the same handoff without dispatching repair.'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_coderabbit_chat_quota.AutoMergeThreadVerifierTests.test_unresolved_posting_identity_defers_handoff_verification_decision \
  tests.test_coderabbit_chat_quota.AutoMergeThreadVerifierTests.test_repair_handoff_triggers_fresh_verification_when_head_is_unchanged \
  tests.test_coderabbit_chat_quota.AutoMergeThreadVerifierTests.test_unreadable_repair_handoff_defers_the_repair_decision \
  tests.test_coderabbit_unresolved_dispatch.CodeRabbitBatchContractTests.test_repair_completion_handoff_is_durable_serialized_and_unforgeable \
  tests.test_coderabbit_unresolved_dispatch.CodeRabbitBatchContractTests.test_pending_repair_handoff_blocks_redispatch_until_reverified

# Print the lease-optional no-change regressions by name in the exact-HEAD gate
# log. CodeRabbit excludes tests/** from review, so the named results are the
# reviewable evidence that an ordinary full-review outcome and an
# executor-admitted subset both reconcile without an exact batch lease while
# run-scoped provenance and dispatch-order checks still gate suppression.
printf '%s\n' \
  'CodeRabbit no-change lease regression evidence:' \
  'setup: an ordinary full-review no-change outcome has no continuum-coderabbit-batch lease; an executor-admitted subset publishes a digest that differs from the original batch lease; a full-review outcome has no provenance artifact; a matching lease follows its repair run.' \
  'assertions: both the collection and pre-dispatch guards suppress verified full-review and admitted-subset no-change outcomes without an exact lease, still defer when run-scoped provenance is missing, and still reject a run that predates its matching lease.'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3 -m unittest -v \
  tests.test_coderabbit_no_change_guard.NoChangeGuardTests.test_run_scoped_outcome_verifies_without_a_dispatch_lease \
  tests.test_coderabbit_no_change_guard.NoChangeGuardTests.test_admitted_subset_outcome_reconciles_with_original_batch_lease \
  tests.test_coderabbit_no_change_guard.NoChangeGuardTests.test_outcome_without_a_dispatch_lease_still_requires_run_evidence \
  tests.test_coderabbit_no_change_guard.NoChangeGuardTests.test_matching_dispatch_lease_still_anchors_run_order

# Print the newer-outcome guard-preservation regressions by name in the
# exact-HEAD gate log. CodeRabbit excludes tests/** from review, so the named
# results are the reviewable evidence that a newer unverifiable changed=false
# outcome never erases an earlier verified no-change guard for the same HEAD.
printf '%s\n' \
  'CodeRabbit newer-outcome guard-preservation regression evidence:' \
  'setup: an earlier verified no-change outcome for the current HEAD is followed by a newer changed=false outcome whose run failed, was jobless, or skipped its repair step.' \
  'assertions: both the collection and pre-dispatch verifiers keep suppressing re-dispatch through the earlier verified run, while an unverified changed=true outcome still defers instead of releasing suppression.'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3 -m unittest -v \
  tests.test_coderabbit_no_change_guard.NoChangeGuardTests.test_newer_unverified_outcome_preserves_earlier_verified_no_change \
  tests.test_coderabbit_no_change_guard.NoChangeGuardTests.test_newer_jobless_or_admission_only_outcome_preserves_earlier_verified_no_change

# Print the contract-canary exact-HEAD snapshot regressions by name in the
# exact-HEAD gate log. CodeRabbit excludes tests/** from review, so the named
# results are the only reviewable evidence that a snapshot success cannot mask
# a newer failed or pending exact-HEAD run, and that both workflow fetches
# replace the snapshot records for that head.
printf '%s\n' \
  'Contract canary exact-HEAD snapshot regression evidence:' \
  'setup: the bounded run snapshot holds successful CI and Contract qualification runs for the canary implementation PR head.' \
  'assertions: merging a failed exact-HEAD CI run blocks completion with regression-evidence-completed-failure, merging a pending exact-HEAD CI run blocks completion with regression-evidence-in_progress-pending, records for other heads are preserved, and both the resolver and observation paths call merge_exact_head_runs instead of appending.'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_contract_canary.ExactHeadRunsTest.test_replaces_only_the_target_head_snapshot_records \
  tests.test_contract_canary.ExactHeadRunsTest.test_fresh_exact_head_failure_masks_the_snapshot_success \
  tests.test_contract_canary.ExactHeadRunsTest.test_fresh_exact_head_pending_masks_the_snapshot_success \
  tests.test_contract_canary.WorkflowContractTest.test_both_exact_head_fetches_replace_stale_snapshot_evidence

# Print the contract-canary worker-evidence and repair-attempt regressions by
# name in the exact-HEAD gate log. CodeRabbit excludes tests/** from review, so
# the named results are the only reviewable evidence that completion records an
# issue-bound worker run, rejects a closed owned issue with a merged PR and both
# exact-HEAD checks but no worker evidence, and pairs each repair commit with
# the receipt from its own attempt of the same run.
printf '%s\n' \
  'Contract canary worker-evidence regression evidence:' \
  'setup: a closed owned canary issue has a merged implementation PR with successful exact-HEAD CI and Contract qualification runs.' \
  'assertions: completion rejects the missing worker run with worker-evidence-absent, the selected worker run id is recorded in the completed outcome, both receipts from separate attempts of one repair run stay listed even when their names collide, two repair commits published by those attempts each keep the receipt matching their own base and head, and the workflow downloads repair receipts by artifact id instead of a same-named artifact from another attempt.'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_contract_canary.CompletionEvidenceTest.test_missing_worker_evidence_fails \
  tests.test_contract_canary.CompletionEvidenceTest.test_completion_worker_run_id_is_recorded \
  tests.test_contract_canary.CompletionEvidenceTest.test_two_repair_commits_from_separate_attempts_of_one_run_pass \
  tests.test_contract_canary.WorkflowContractTest.test_workflow_records_completion_worker_run_id \
  tests.test_contract_canary.WorkflowContractTest.test_workflow_binds_each_repair_status_to_its_own_commit_receipt \
  tests.test_contract_canary.WorkflowContractTest.test_repair_receipt_selection_matches_each_commit_from_a_rerun \
  tests.test_contract_canary.WorkflowContractTest.test_publication_artifacts_keep_both_rerun_attempts

# Print the resumed-generation 429 evacuation regression by name in the
# exact-HEAD gate log. The executable regression and its setup live in
# scripts/test_opencode_429_evacuator.py, which CodeRabbit reviews; the named
# result confirms that a generation-2 legacy resume carries its validated
# generation into the same-step evacuator, writes a generation-3 checkpoint,
# and leaves the existing generation-1 and generation-2 refs unchanged.
printf '%s\n' \
  'Resumed-generation 429 evacuation regression evidence:' \
  'setup: an ordinary issue dispatch resumes a generation-2 legacy checkpoint with an empty RECOVERY_GENERATION_IN; generation-1 and generation-2 checkpoint refs already exist; the run hits another 429.' \
  'assertions: the evacuator publishes generation 3 through the same-step carry and force-pushes neither the generation-1 nor the generation-2 ref.'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  scripts.test_opencode_429_evacuator.ProductionEvacuatorTest.test_resumed_generation_evacuates_next_generation_without_overwriting

# Print the durable checkpoint supersession and repository-provenance
# regressions by name in the exact-HEAD gate log. CodeRabbit excludes tests/**
# from review, so the named results are the reviewable evidence that a stale
# legacy-mirror generation is rejected before the baseline, an explicit
# recovery dispatch cannot replay a superseded generation, the current
# generation still resumes, generation-1 legacy continuation is unchanged, and
# a generation-2 durable ref or legacy alias is trusted only when the expected
# repository publishes the exact pinned SHA.
printf '%s\n' \
  'Durable checkpoint supersession and repository-provenance regression evidence:' \
  'setup: a legacy checkpoint generation is older than, equal to, or newer than the durable refs; the workflow supersession gate sees a stale mirror, the current generation, and an unreadable durable-state read; an explicit recovery identity carries an older generation while durable refs publish a newer one, or durable state is unreadable; a generation-2 durable checkpoint has matching or missing repository metadata, with matching, unpublished, mismatched, or wrong-origin refs, and a legacy alias with or without a published durable ref; generation 1 still resumes its legacy 429 ref.' \
  'assertions: a superseded generation fails closed before checkpoint work, a superseded explicit recovery identity and an unreadable durable-state read fail closed, the current generation resumes, generation-1 continuation is unchanged, historical metadata-less generation 2 resumes only through an expected-repository durable ref at the pinned SHA, and missing/unpublished/mismatched/wrong-origin provenance is rejected, including alias fallback without a published durable ref.'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_fresh_checkpoint_429_ref_is_resumed \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_superseded_generation_is_rejected_before_checkpoint_work \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_current_durable_generation_is_accepted \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_generation_2_durable_ref_resumes_historical_checkpoint_without_repository \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_generation_2_durable_ref_requires_repository_bound_provenance \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_generation_2_durable_ref_rejects_an_unpublished_remote_ref \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_generation_2_durable_ref_rejects_a_different_remote_repository \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_generation_2_legacy_mirror_alias_resumes_repository_bound_checkpoint \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_generation_2_alias_rejects_metadata_without_repository_binding \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_generation_2_alias_rejects_an_unpublished_durable_ref \
  tests.test_checkpoint_resume.CheckpointResumeTest.test_generation_2_alias_rejects_a_durable_ref_at_a_different_sha \
  tests.test_opencode_admission.DuplicateRunTests.test_superseded_recovery_identity_fails_closed \
  tests.test_opencode_admission.DuplicateRunTests.test_recovery_identity_requires_durable_generation_state \
  scripts.test_opencode_429_evacuator.LatestDurableGenerationTest.test_reads_the_highest_durable_generation \
  scripts.test_opencode_429_evacuator.LatestDurableGenerationTest.test_unreadable_durable_state_fails_closed \
  scripts.test_opencode_429_evacuator.IssueResumeSupersessionTest.test_stale_mirror_generation_fails_before_resume \
  scripts.test_opencode_429_evacuator.IssueResumeSupersessionTest.test_current_generation_resumes \
  scripts.test_opencode_429_evacuator.IssueResumeSupersessionTest.test_unreadable_durable_state_fails_closed

mkdir -p "$PWD/.opencode-tmp/bin"
# Pin both the installer URL and its content: the tag can move and a
# compromised download would otherwise execute with this script's privileges.
actionlint_installer="$PWD/.opencode-tmp/download-actionlint.bash"
actionlint_installer_sha256=72fa3e45ac20f3c3a512d6747b4fcf719e21f890e8c43e78d48a41fdfb900c4e
curl -fsSL --retry 3 -o "$actionlint_installer" \
  https://raw.githubusercontent.com/rhysd/actionlint/914e7df21a07ef503a81201c76d2b11c789d3fca/scripts/download-actionlint.bash
if command -v sha256sum >/dev/null 2>&1; then
  printf '%s  %s\n' "$actionlint_installer_sha256" "$actionlint_installer" | sha256sum -c -
else
  actual_sha256="$(shasum -a 256 "$actionlint_installer" | awk '{print $1}')"
  if [[ "$actual_sha256" != "$actionlint_installer_sha256" ]]; then
    echo "actionlint installer checksum mismatch" >&2
    exit 1
  fi
fi
bash "$actionlint_installer" 1.7.12 "$PWD/.opencode-tmp/bin"
.opencode-tmp/bin/actionlint -shellcheck= -pyflakes= \
  .github/workflows/*.yml \
  .github/caller-stubs/*.yml \
  .github/caller-stubs/tech/*.yml \
  .github/caller-stubs/parent/*.yml
