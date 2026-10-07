import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class PrAgentTargetWorkflowContractTests(unittest.TestCase):
    def test_delegated_target_resolution_does_not_depend_on_stale_repository_map_secret(self):
        for path in [
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
            ".github/workflows/continuum-pr-agent-recovery.yml",
        ]:
            with self.subTest(path=path):
                body = read(path)
                self.assertNotIn("secrets.CONTINUUM_CHILD_REPOSITORIES", body)

    reusable = (
        ".github/workflows/continuum-pr-agent.yml",
        ".github/workflows/continuum-pr-agent-repair.yml",
        ".github/workflows/continuum-pr-agent-auto-merge.yml",
        ".github/workflows/continuum-pr-agent-recovery.yml",
    )

    callers = (
        ".github/caller-stubs/continuum-pr-agent.yml",
        ".github/caller-stubs/continuum-pr-agent-repair.yml",
        ".github/caller-stubs/continuum-pr-agent-auto-merge.yml",
        ".github/caller-stubs/continuum-pr-agent-recovery.yml",
        ".github/workflows/pr-agent.yml",
        ".github/workflows/pr-agent-recovery.yml",
    )

    def test_reusable_workflows_accept_only_opaque_target_identity(self):
        for path in self.reusable:
            with self.subTest(path=path):
                body = read(path)
                self.assertIn("target_child_id:", body)
                # Only the opaque id is ever an input; concrete repository
                # identity is resolved runner-local and never appears as an
                # input, run name, or committed config value.
                self.assertNotIn("target_repository:", body)
                self.assertNotIn("target_owner:", body)
        helper = read(".github/scripts/pr_agent_target.sh")
        self.assertIn('bash "$resolver" resolve "$child_id"', helper)
        self.assertIn('bash "$resolver" verify "$child_id" "$target_repository"', helper)
        self.assertIn('target_repository="$GITHUB_REPOSITORY"', helper)

    def test_review_targets_child_data_but_retries_in_execution_repository(self):
        body = read(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_REPOSITORY", body)
        self.assertIn('gh pr view "$PR_NUMBER" --repo "$CONTINUUM_PR_AGENT_TARGET_REPOSITORY"', body)
        self.assertIn("repos/$CONTINUUM_PR_AGENT_TARGET_REPOSITORY/actions/runs", body)
        self.assertIn("owner: process.env.CONTINUUM_PR_AGENT_TARGET_OWNER", body)
        # Retry dispatch stays in the parent execution repository while
        # carrying the opaque child identity forward.
        self.assertIn('gh workflow run "$RETRY_WORKFLOW" --repo "$GITHUB_REPOSITORY"', body)
        self.assertIn('-f target_child_id="$TARGET_CHILD_ID"', body)
        # Concurrency is scoped by the opaque id only, never a concrete name,
        # while preserving the exact local group string when empty.
        self.assertIn("inputs.target_child_id && format('pr-agent-child-{0}-{1}'", body)
        self.assertIn("format('pr-agent-{0}'", body)
        self.assertNotIn("target_repository", body.lower().replace("continuum_pr_agent_target_repository", ""))

    def test_repair_targets_child_branch_and_stays_pat_backed_for_push(self):
        body = read(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_REPOSITORY", body)
        self.assertIn('gh api "repos/$CONTINUUM_PR_AGENT_TARGET_REPOSITORY/pulls/$PR_NUMBER"', body)
        self.assertIn('"$HEAD_REPO" != "$CONTINUUM_PR_AGENT_TARGET_REPOSITORY"', body)
        self.assertNotIn('repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER', body)
        self.assertNotIn('gh pr view "$PR_NUMBER" --repo "$GITHUB_REPOSITORY"', body)
        # Checkout targets the verified repository via an opaque env
        # reference (no concrete name in committed config); push still uses
        # force-with-lease and the retry dispatch stays in the parent.
        self.assertIn("repository: ${{ env.CONTINUUM_PR_AGENT_TARGET_REPOSITORY }}", body)
        self.assertIn("token: ${{ secrets.TAP_PAT }}", body)
        self.assertIn("git push --force-with-lease=", body)
        self.assertIn('gh workflow run "$RETRY_WORKFLOW" --repo "$GITHUB_REPOSITORY"', body)
        self.assertIn('-f target_child_id="$TARGET_CHILD_ID"', body)
        # Repair concurrency preserves the exact local group string when
        # empty and scopes delegated runs by the opaque id only.
        self.assertIn("format('pr-agent-repair-child-{0}-{1}-{2}'", body)
        self.assertIn("format('pr-agent-repair-{0}-{1}'", body)
        # Exact-HEAD holds end to end: the writable branch must match the
        # reviewed SHA, the checkout must equal the reviewed SHA, a long
        # repair run revalidates before publication, and the new HEAD must
        # descend from the reviewed HEAD.
        self.assertIn('"$CURRENT_SHA" != "$HEAD_SHA"', body)
        self.assertIn('if [[ "$(git rev-parse HEAD)" != "$HEAD_SHA" ]]', body)
        self.assertIn('if [[ "$CURRENT_SHA" != "$HEAD_SHA" || "$REMOTE_SHA" != "$HEAD_SHA" ]]', body)
        self.assertIn('git merge-base --is-ancestor "$HEAD_SHA" HEAD', body)
        self.assertIn('git push --force-with-lease="refs/heads/$HEAD_REF:$HEAD_SHA"', body)
        # Privacy: only the opaque child id is ever an input or concurrency
        # scope; concrete repository identity stays runner-local via env.
        self.assertIn("target_child_id:", body)
        self.assertNotIn("target_repository:", body)
        # Local behavior is unchanged: the empty-id fast path resolves from
        # the execution repository and exits before CONTINUUM_REF is required.
        resolve_at = body.index("Resolve PR-Agent target context")
        resolve = body[resolve_at:resolve_at + 6000]
        self.assertIn('if [[ -z "${TARGET_CHILD_ID:-}" ]]; then', resolve)
        self.assertIn('CONTINUUM_REF is required for pinned target resolution', resolve)
        self.assertLess(
            resolve.index('if [[ -z "${TARGET_CHILD_ID:-}" ]]; then'),
            resolve.index('CONTINUUM_REF is required for pinned target resolution'),
        )
        # The parent child-map config lives in the execution repository,
        # whose default branch is fetched: the Continuum engine pin may not
        # exist there.
        self.assertIn('contents/.continuum.yml" --jq', resolve)
        self.assertNotIn('contents/.continuum.yml" -f ref="$CONTINUUM_REF"', resolve)

    def test_repair_validates_target_before_checkout(self):
        body = read(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("Validate PR-Agent target context", body)
        resolve_at = body.index("Resolve PR-Agent target context")
        validate_at = body.index("Validate PR-Agent target context")
        checkout_at = body.index("Checkout the writable PR source branch")
        self.assertLess(resolve_at, validate_at)
        self.assertLess(validate_at, checkout_at)
        validate = body[validate_at:validate_at + 4000]
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_REPOSITORY:?", validate)
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_OWNER:?", validate)
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_REPO:?", validate)
        self.assertIn("PR-Agent target repository identity is invalid", validate)
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", validate)
        self.assertIn("Delegated PR-Agent execution requires TAP_PAT", validate)
        self.assertIn("refusing to fall back to github.token", validate)

    def test_merge_keeps_target_and_execution_repository_distinct(self):
        body = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("const executionOwner = context.repo.owner;", body)
        self.assertIn("const owner = process.env.CONTINUUM_PR_AGENT_TARGET_OWNER;", body)
        self.assertIn("const repoFullName = process.env.CONTINUUM_PR_AGENT_TARGET_REPOSITORY;", body)
        self.assertIn("owner: executionOwner", body)
        self.assertIn("repo: executionRepo", body)
        # Delegated conflicts dispatch target-aware repair in the parent
        # execution repository with the opaque child id; the repair
        # workflow resolves the child target runner-local.
        self.assertIn("workflow_id: 'continuum-pr-agent-repair.yml'", body)
        self.assertIn("target_child_id: targetChildId", body)

    def test_recovery_reads_target_but_dispatches_parent_workflow(self):
        body = read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("const executionOwner = context.repo.owner;", body)
        self.assertIn("const owner = process.env.CONTINUUM_PR_AGENT_TARGET_OWNER;", body)
        self.assertIn("owner: executionOwner", body)
        self.assertIn("repo: executionRepo", body)
        # Delegated recovery forwards the opaque child selection while local
        # runs dispatch bare: a custom reviewWorkflow without the
        # target_child_id workflow_dispatch input would reject an empty
        # value, breaking local recovery. This mirrors the auto-merge
        # delegated-wakeup contract (never retries bare for delegated,
        # dispatches bare for local).
        self.assertIn("const recoveryChildId = String(process.env.TARGET_CHILD_ID || '')", body)
        self.assertIn("if (recoveryChildId)", body)
        self.assertIn("recoveryInputs.target_child_id = recoveryChildId", body)
        self.assertIn("TARGET_CHILD_ID: ${{ inputs.target_child_id || vars.CONTINUUM_PR_AGENT_TARGET_CHILD_ID }}", body)
        # A review workflow missing the target_child_id input fails closed
        # explicitly only when the 422 names the opaque input; other 422s
        # (invalid pr_number/expected_head_sha, ref, payload validation)
        # rethrow verbatim so the operator fixes the true cause.
        self.assertIn("dispatchStatus === 422", body)
        self.assertIn("/target_child_id/i", body)
        self.assertNotIn("/input/i.test(dispatchMessage)", body)
        self.assertIn("refusing bare retry to preserve the delegated target", body)

    def test_recovery_resolves_locally_scopes_concurrency_and_stays_exact_head(self):
        body = read(".github/workflows/continuum-pr-agent-recovery.yml")
        # Local fast path: empty id resolves from the execution repository
        # and exits before CONTINUUM_REF is required for pinned resolution.
        resolve_at = body.index("Resolve PR-Agent target context")
        resolve = body[resolve_at : resolve_at + 6000]
        self.assertIn('if [[ -z "${TARGET_CHILD_ID:-}" ]]; then', resolve)
        self.assertIn("CONTINUUM_REF is required for pinned target resolution", resolve)
        self.assertLess(
            resolve.index('if [[ -z "${TARGET_CHILD_ID:-}" ]]; then'),
            resolve.index("CONTINUUM_REF is required for pinned target resolution"),
        )
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_REPOSITORY", resolve)
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", resolve)
        self.assertIn("PR-Agent target context resolved locally.", resolve)
        self.assertIn("exit 0", resolve)
        # The parent child-map config lives in the execution repository,
        # whose default branch is fetched: the Continuum engine pin may not
        # exist there.
        self.assertIn('contents/.continuum.yml" --jq', resolve)
        self.assertNotIn('contents/.continuum.yml" -f ref="$CONTINUUM_REF"', resolve)
        # Concurrency scopes delegated wakeups by the opaque id (input, then
        # repository variable for schedule/workflow_run) while preserving the
        # exact local group string when empty.
        self.assertIn("format('pr-agent-recovery-child-", body)
        self.assertIn("format('pr-agent-recovery-", body)
        self.assertIn(
            "inputs.target_child_id || vars.CONTINUUM_PR_AGENT_TARGET_CHILD_ID",
            body,
        )
        # Exact-HEAD: CI evidence and dispatch identity are per exact HEAD;
        # only same-repository PRs against the resolved target are considered.
        self.assertIn("head_sha: head", body)
        self.assertIn("run.head_sha === head", body)
        self.assertIn("pr.head.repo.full_name !== repoFullName", body)
        # Privacy: only the opaque child id is ever an input; concrete
        # repository identity stays runner-local via env.
        self.assertIn("target_child_id:", body)
        self.assertNotIn("target_repository:", body)

    def test_callers_preserve_opaque_child_id_across_retries(self):
        for path in self.callers:
            with self.subTest(path=path):
                body = read(path)
                self.assertIn("target_child_id", body)
                self.assertNotIn("target_repository", body)

    def test_recovery_retries_failed_run_left_with_pending_status(self):
        body = read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("const retryableConclusions = new Set([", body)
        self.assertIn("'failure',", body)
        self.assertIn("pending operation is not stale", body)

    def test_pr_agent_caller_accepts_forwarded_continuum_ref(self):
        body = read(".github/caller-stubs/continuum-pr-agent.yml")
        dispatch = body.split("workflow_dispatch:", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("continuum_ref:", dispatch)
        self.assertIn("default: main", dispatch)
        self.assertIn("continuum_ref: main", body)

    def test_delegated_identity_is_masked_and_target_checkout_is_quiet(self):
        helper = read(".github/scripts/pr_agent_target.sh")
        # Resolver stderr is suppressed so delegated identity cannot leak
        # before the ::add-mask:: calls are installed.
        self.assertIn('resolve "$child_id" 2>/dev/null', helper)
        self.assertIn("echo \"::add-mask::$target_repository\"", helper)
        self.assertIn("echo \"::add-mask::$target_repo\"", helper)
        review = read(".github/workflows/continuum-pr-agent.yml")
        # Review checkout is a quiet manual fetch of the exact HEAD without
        # exposing delegated metadata; delegated tool output stays runner-local.
        self.assertIn('git remote add origin "https://github.com/$CONTINUUM_PR_AGENT_TARGET_REPOSITORY.git"', review)
        self.assertIn(">/dev/null 2>&1", review)
        self.assertIn("git fetch --depth=1 --no-tags origin", review)
        self.assertIn("git checkout --detach FETCH_HEAD", review)
        self.assertIn("detailed output remains runner-local", review)
        # The manual checkout gate admits only a full-length HEAD SHA (40
        # hex, plus 64-hex SHA-256 where supported): a short prefix must
        # never pass the fail-closed gate and fetch an unintended object.
        self.assertIn("^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})$", review)
        self.assertNotIn("{4,64}", review)

    def test_review_checkout_validates_pr_number_before_pull_refspec(self):
        review = read(".github/workflows/continuum-pr-agent.yml")
        start = review.index("Checkout the pull request head without exposing")
        window = review[start:start + 4000]
        # HEAD_SHA is fail-closed hex-gated; PR_NUMBER must be fail-closed
        # numeric-gated before it reaches `pull/$PR_NUMBER/head`, mirroring
        # the HEAD_SHA validation directly above it.
        self.assertIn("^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})$", window)
        self.assertIn("^[0-9]+$", window)
        self.assertIn("PR number is malformed", window)
        self.assertIn('pull/$PR_NUMBER/head', review)

    def test_shell_retry_dispatch_detects_unknown_target_input(self):
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
        ):
            with self.subTest(path=path):
                body = read(path)
                # The delegated retry carries the opaque id, but a retry
                # workflow without the input rejects it as 422: that case
                # fails closed with an explicit message (mirroring the
                # merge-wakeup/recovery 422 detection) instead of a generic
                # shell failure.
                self.assertIn("dispatch_isolated_retry", body)
                self.assertIn("422", body)
                self.assertIn("target_child_id", body)
                self.assertIn(
                    "refusing bare retry to preserve the delegated target",
                    body,
                )

    def test_recovery_replays_clean_review_when_source_run_failed(self):
        body = read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("let cleanReviewRun = null", body)
        self.assertIn("cleanReviewRun = await statusRunMetadata(reviewStatus)", body)
        self.assertIn("retryableConclusions.has(cleanRunConclusion)", body)
        self.assertIn("clean review belongs to an incomplete orchestration run", body)
        self.assertIn("PR-Agent clean review lifecycle incomplete: recovery eligible", body)
        self.assertIn("cleanReviewRun || await statusRunMetadata(reviewStatus)", body)

    def test_recovery_read_fallback_covers_inaccessible_repo_404(self):
        body = read(".github/workflows/continuum-pr-agent-recovery.yml")
        # Cross-repository target reads with a repository-scoped token
        # conventionally surface as 404, so the PAT fallback must cover it
        # alongside 401/403/429.
        self.assertIn("async function withReadFallback(fn)", body)
        self.assertIn("status === 404", body)

    def test_coderabbit_workflows_remain_outside_target_context(self):
        for path in (
            ".github/workflows/continuum-coderabbit-retry.yml",
            ".github/workflows/continuum-coderabbit-unresolved.yml",
        ):
            with self.subTest(path=path):
                body = read(path)
                self.assertNotIn("target_child_id", body)
                self.assertNotIn("CONTINUUM_PR_AGENT_TARGET", body)

    def test_delegated_reads_fail_closed_without_pat(self):
        body = read(".github/workflows/continuum-pr-agent.yml")
        # A dedicated guard fails the run before any delegated read when
        # TAP_PAT is empty, and the credential itself yields an empty token
        # so actions/github-script auth fails closed before the embedded
        # script runs (never silently reading as github.token).
        fail_closed_token = "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' && secrets.TAP_PAT || (env.CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED != 'true' && github.token || '')"
        self.assertIn("Fail closed when delegated without PAT", body)
        self.assertIn("Delegated PR-Agent execution requires TAP_PAT", body)
        self.assertIn("refusing to fall back to github.token", body)
        for step in (
            "Admit only a review-ready PR with green CI on the exact HEAD",
            "Revalidate the admitted exact HEAD immediately before review",
            "Revalidate the PR head and native review output after review",
            "Fail closed on a moved head",
        ):
            with self.subTest(step=step):
                # Scope assertions to the step's own block: a fixed-width
                # window bleeds into the following step (e.g. the review
                # tool, which legitimately runs on github.token), turning
                # a neighbor's repository-token credential into a false
                # failure for this step.
                start = body.index("      - name: " + step)
                following = body.find("\n      - name: ", start + 1)
                window = body[start:following if following != -1 else start + 4000]
                self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", window)
                self.assertIn("TAP_PAT", window)
                self.assertIn("requires TAP_PAT", window)
                # Presence-only checks would pass a step that reads cross-repo
                # with an unconditional github.token while mentioning TAP_PAT
                # elsewhere, so each step must carry the empty-token
                # conditional and the fail-closed guard.
                self.assertIn(
                    fail_closed_token,
                    window,
                )
                self.assertIn("refusing to fall back to github.token", window)
                self.assertNotIn("github-token: ${{ github.token }}", window)
                self.assertNotIn("GH_TOKEN: ${{ github.token }}", window)
                self.assertNotIn("secrets.TAP_PAT || github.token }}", window)

    def test_post_review_target_operations_use_delegated_pat(self):
        body = read(".github/workflows/continuum-pr-agent.yml")
        token = "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' && secrets.TAP_PAT || (env.CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED != 'true' && github.token || '')"
        for step in (
            "Export native persistent finding state for the reviewed HEAD",
            "Decide whether automatic improve can be skipped",
            "Normalize persistent improve presentation",
            "Clear settled PR-Agent controller state",
            "Publish durable PR-Agent review state",
            "Publish failed PR-Agent review state",
            "Update persistent PR-Agent retry controller state",
        ):
            with self.subTest(step=step):
                start = body.index("      - name: " + step)
                following = body.find("\n      - name: ", start + 1)
                window = body[start:following if following != -1 else start + 12000]
                self.assertIn(token, window)
                self.assertNotIn("github-token: ${{ github.token }}", window)
        persistent_at = body.index("Export native persistent finding state for the reviewed HEAD")
        persistent = body[persistent_at:persistent_at + 5000]
        self.assertIn("Delegated PR-Agent persistent-state export requires TAP_PAT", persistent)

    def test_checkout_credential_is_scoped_to_resolved_target(self):
        body = read(".github/workflows/continuum-pr-agent.yml")
        start = body.index("Checkout the pull request head without exposing")
        window = body[start:start + 4000]
        # Local checkout uses github.token; delegated checkout requires
        # TAP_PAT and yields an empty token without it (auth itself fails
        # closed before the guard).
        self.assertIn("github.token", window)
        self.assertIn("secrets.TAP_PAT", window)
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", window)
        self.assertIn("requires TAP_PAT", window)
        self.assertIn(
            "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' && secrets.TAP_PAT || (env.CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED != 'true' && github.token || '')",
            window,
        )
        self.assertIn("refusing to fall back to github.token", window)
        self.assertNotIn("GH_TOKEN: ${{ github.token }}", window)
        self.assertNotIn("secrets.TAP_PAT || github.token }}", window)

    def test_repair_writable_branch_resolution_is_fail_closed_and_target_scoped(self):
        body = read(".github/workflows/continuum-pr-agent-repair.yml")
        start = body.index("Resolve the writable PR source branch")
        window = body[start:start + 4000]
        # Delegated PR resolution must use the resolved target with an
        # empty-token fallback so auth itself fails closed without PAT.
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", window)
        self.assertIn(
            "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' && secrets.TAP_PAT || (env.CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED != 'true' && github.token || '')",
            window,
        )
        self.assertNotIn("secrets.TAP_PAT || github.token }}", window)
        self.assertIn("refusing to fall back to github.token", window)
        self.assertIn("repos/$CONTINUUM_PR_AGENT_TARGET_REPOSITORY/pulls/$PR_NUMBER", window)
        self.assertNotIn("repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER", window)
        self.assertIn('"$HEAD_REPO" != "$CONTINUUM_PR_AGENT_TARGET_REPOSITORY"', window)

    def test_recovery_dispatch_error_names_target_child_id_only(self):
        body = read(".github/workflows/continuum-pr-agent-recovery.yml")
        # Misleading-error guard: only a 422 naming target_child_id is
        # reported as a missing delegated input; other 422s rethrow verbatim.
        self.assertIn("isMissingTargetInput", body)
        self.assertIn("/target_child_id/i.test(dispatchMessage)", body)
        self.assertIn("dispatchStatus === 422", body)
        self.assertNotIn("/input/i.test(dispatchMessage)", body)

    def test_delegated_wakeup_never_retries_bare(self):
        body = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("dispatchParams.inputs = { target_child_id: targetChildId }", body)
        # A delegated wakeup whose target workflow rejects the opaque input
        # refuses the bare retry and warns best-effort: the merge already
        # succeeded, so the run must never fail the merge nor dispatch bare
        # against the parent execution repository.
        self.assertIn("refusing bare retry to preserve the delegated target", body)
        self.assertIn("if (targetChildId)", body)
        self.assertNotIn("skipping bare retry to preserve the delegated target", body)
        self.assertNotIn("retrying bare", body)
        # Narrow input-rejection: only the opaque input plus
        # unexpected/unknown-input wording. Generic invalid-inputs or
        # inputs-not-accepted wording would misclassify ref/payload 422s.
        self.assertIn("/(target_child_id|unexpected", body)
        self.assertIn("unknown\\s+inputs?", body)
        self.assertNotIn("invalid\\s+inputs?", body)
        self.assertNotIn("unrecognized", body)
        self.assertNotIn("inputs?\\s+not\\s+(accepted", body)

    def test_delegated_conflict_repair_dispatch_handles_422(self):
        body = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        start = body.index("async function dispatchConflictRepair")
        window = body[start : start + 8000]
        self.assertIn("workflow_id: 'continuum-pr-agent-repair.yml'", window)
        self.assertIn("target_child_id: targetChildId", window)
        # Like recovery/wakeup: a 422 naming the opaque input fails closed
        # explicitly; any other dispatch error rethrows transiently so the
        # outer cleanup removes the marker/lock and recovery reconciles it.
        self.assertIn("isMissingTargetInput", window)
        self.assertIn("/target_child_id/i.test(dispatchMessage)", window)
        self.assertIn("dispatchStatus === 422", window)
        self.assertIn("refusing bare retry to preserve the delegated target", window)
        self.assertIn("recovery per #224", window)
        repair = read(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("target_child_id:", repair)

    def test_delegated_repair_uses_private_comment_handoff_not_raw_outputs(self):
        review = read(".github/workflows/continuum-pr-agent.yml")
        repair = read(".github/workflows/continuum-pr-agent-repair.yml")
        repair_job = review[review.index("\n  repair:"):review.index("\n  merge:")]
        self.assertIn("repair_batch_comment_id: ${{ needs.pr_agent.outputs.repair_batch_comment_id }}", repair_job)
        self.assertNotIn("review_json: ${{ needs.pr_agent.outputs.review_json }}", repair_job)
        self.assertNotIn("improve_jsonl: ${{ needs.pr_agent.outputs.improve_jsonl }}", repair_job)
        self.assertIn("Publish private exact-head repair handoff", review)
        self.assertIn("continuum-pr-agent-repair-batch:v1", review)
        self.assertIn("REPAIR_BATCH_COMMENT_ID: ${{ inputs.repair_batch_comment_id }}", repair)
        self.assertIn("getComment", repair)
        self.assertIn("logicalFingerprint", repair)
        self.assertNotIn("REVIEW_JSON: ${{ steps.pragent.outputs.review }}", review)

    def test_delegated_merge_uses_scalar_attestation_not_raw_outputs(self):
        review = read(".github/workflows/continuum-pr-agent.yml")
        merge = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        merge_job = review[review.index("\n  merge:"):]
        self.assertIn("merge_gate_verified: ${{ steps.merge_evidence.outputs.green }}", review)
        self.assertIn("needs.pr_agent.outputs.merge_gate_verified == 'true'", merge_job)
        self.assertIn("gate_verified: ${{ needs.pr_agent.outputs.merge_gate_verified }}", merge_job)
        self.assertNotIn("review_json: ${{ needs.pr_agent.outputs.review_json }}", merge_job)
        self.assertNotIn("persistent_state_json: ${{ needs.pr_agent.outputs.persistent_state_json }}", merge_job)
        self.assertIn("Validate upstream exact-head merge attestation", merge)
        self.assertIn("PR-Agent review complete: clean", merge)
        self.assertIn("continuum/pr-agent-review", merge)
        self.assertIn("inputs.gate_verified != 'true'", merge)

    def test_auto_merge_exact_head_and_privacy(self):
        body = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # Exact-HEAD: only the reviewed SHA may merge; any moved HEAD fails.
        self.assertIn("pr.head.sha.toLowerCase() !== reviewedHead", body)
        self.assertIn("sha: reviewedHead", body)
        self.assertIn("refusing to merge a stale result", body)
        # Delegated-vs-local branching: opaque child id only, never a
        # concrete repository input.
        self.assertIn("target_child_id:", body)
        self.assertNotIn("target_repository:", body)
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", body)
        # Privacy: delegated conflict repair dispatches target-aware in the
        # parent execution repository with the opaque child id, never a
        # target-local run in the child.
        self.assertIn("workflow_id: 'continuum-pr-agent-repair.yml'", body)
        self.assertIn("target_child_id: targetChildId", body)
        self.assertIn("dispatched delegated conflict repair via parent", body)

    def test_review_resolves_locally_before_pinned_ref(self):
        body = read(".github/workflows/continuum-pr-agent.yml")
        # Local-unchanged path: empty id resolves from the execution
        # repository and exits before CONTINUUM_REF is required, so a
        # previously local-only review never depends on resolver fetches.
        resolve_at = body.index("Resolve PR-Agent target context")
        resolve = body[resolve_at:resolve_at + 6000]
        self.assertIn('if [[ -z "${TARGET_CHILD_ID:-}" ]]; then', resolve)
        self.assertIn("CONTINUUM_REF is required for pinned target resolution", resolve)
        self.assertLess(
            resolve.index('if [[ -z "${TARGET_CHILD_ID:-}" ]]; then'),
            resolve.index("CONTINUUM_REF is required for pinned target resolution"),
        )
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_REPOSITORY", resolve)
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", resolve)
        self.assertIn("PR-Agent target context resolved locally.", resolve)
        self.assertIn("exit 0", resolve)
        # Delegated-must-use-target: the parent config is fetched from the
        # execution repository default branch, never the engine pin.
        self.assertIn('contents/.continuum.yml" --jq', resolve)
        self.assertNotIn('contents/.continuum.yml" -f ref="$CONTINUUM_REF"', resolve)

    def test_auto_merge_resolves_locally_before_pinned_ref(self):
        body = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        resolve_at = body.index("Resolve PR-Agent target context")
        resolve = body[resolve_at:resolve_at + 6000]
        self.assertIn('if [[ -z "${TARGET_CHILD_ID:-}" ]]; then', resolve)
        self.assertIn("CONTINUUM_REF is required for pinned target resolution", resolve)
        self.assertLess(
            resolve.index('if [[ -z "${TARGET_CHILD_ID:-}" ]]; then'),
            resolve.index("CONTINUUM_REF is required for pinned target resolution"),
        )
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_REPOSITORY", resolve)
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", resolve)
        self.assertIn("PR-Agent target context resolved locally.", resolve)
        self.assertIn("exit 0", resolve)
        self.assertIn('contents/.continuum.yml" --jq', resolve)
        self.assertNotIn('contents/.continuum.yml" -f ref="$CONTINUUM_REF"', resolve)

    def test_delegated_target_bootstrap_uses_canonical_engine_and_local_read_token(self):
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
            ".github/workflows/continuum-pr-agent-auto-merge.yml",
            ".github/workflows/continuum-pr-agent-recovery.yml",
        ):
            with self.subTest(path=path):
                body = read(path)
                resolve_at = body.index("Resolve PR-Agent target context")
                resolve = body[resolve_at:resolve_at + 7000]
                self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", resolve)
                self.assertIn('CONTINUUM_SOURCE_REPO="kodmial/continuum"', resolve)
                self.assertNotIn("GITHUB_WORKFLOW_REF", resolve)
                self.assertIn(
                    'GH_TOKEN="$READ_GITHUB_TOKEN" gh api --method GET "repos/$CONTINUUM_SOURCE_REPO/contents/$path"',
                    resolve,
                )
                self.assertIn(
                    'GH_TOKEN="$READ_GITHUB_TOKEN" gh api --method GET "repos/$GITHUB_REPOSITORY/contents/.continuum.yml"',
                    resolve,
                )

    def test_verified_child_pr_agent_accepts_intentionally_skipped_child_ci(self):
        review = read(".github/workflows/continuum-pr-agent.yml")
        recovery = read(".github/workflows/continuum-pr-agent-recovery.yml")
        merge = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("delegatedSkippedCi", review)
        self.assertIn("conclusion == \"skipped\"", review)
        self.assertIn("const ciRunAccepted = (run)", recovery)
        self.assertIn("delegatedTarget && run.conclusion === 'skipped'", recovery)
        self.assertIn("delegatedSkippedCi", merge)
        self.assertIn("ci.conclusion === 'skipped'", merge)

    def test_delegated_target_bootstrap_fetches_config_runtime_dependencies(self):
        for path in self.reusable:
            with self.subTest(path=path):
                body = read(path)
                resolve_at = body.index("Resolve PR-Agent target context")
                resolve = body[resolve_at : resolve_at + 9000]
                self.assertIn(
                    'mkdir -p "$RUNTIME_ROOT/.github/scripts" "$RUNTIME_ROOT/src/continuum"',
                    resolve,
                )
                self.assertIn(
                    'fetch_runtime_file "src/continuum/__init__.py" "$RUNTIME_ROOT/src/continuum/__init__.py"',
                    resolve,
                )
                self.assertIn(
                    'fetch_runtime_file "src/continuum/config.py" "$RUNTIME_ROOT/src/continuum/config.py"',
                    resolve,
                )
                self.assertIn(
                    'fetch_runtime_file "src/continuum/yamlmini.py" "$RUNTIME_ROOT/src/continuum/yamlmini.py"',
                    resolve,
                )

    def test_target_identity_rejects_dot_only_components(self):
        helper = read(".github/scripts/pr_agent_target.sh")
        self.assertIn("pr_agent_valid_target_repository", helper)
        self.assertIn(r"^\.+$", helper)
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
            ".github/workflows/continuum-pr-agent-auto-merge.yml",
            ".github/workflows/continuum-pr-agent-recovery.yml",
        ):
            with self.subTest(path=path):
                body = read(path)
                self.assertIn(r"^\.+$", body)
        runtime = read(".github/scripts/delegation_runtime.py")
        self.assertIn(r"\.+", runtime)
        config = read("src/continuum/config.py")
        self.assertIn('strip(".")', config)

    def test_recovery_dogfood_forwards_opaque_child_id(self):
        body = read(".github/workflows/pr-agent-recovery.yml")
        self.assertIn("target_child_id:", body)
        self.assertIn("target_child_id: \"${{ inputs.target_child_id }}\"", body)
        self.assertNotIn("target_repository", body)


if __name__ == "__main__":
    unittest.main()
