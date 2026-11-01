import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"

BOOTSTRAP_WORKFLOWS = (
    ".github/workflows/continuum-pr-agent.yml",
    ".github/workflows/continuum-pr-agent-repair.yml",
    ".github/workflows/continuum-pr-agent-recovery.yml",
    # auto-merge carries the identical delegated target bootstrap, so it
    # shares the same runtime dependency closure.
    ".github/workflows/continuum-pr-agent-auto-merge.yml",
)


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def bootstrap_sources(workflow: str) -> dict:
    sources = {}
    for source, dest in re.findall(
        r'fetch_runtime_file "([^"]+)" "\$RUNTIME_ROOT/([^"]+)"', workflow
    ):
        sources[dest] = source
    return sources


def continuum_imports(path: str, root: Path = ROOT) -> set:
    def package_parts(source: str) -> tuple:
        try:
            relative = Path(source).relative_to("src")
        except ValueError:
            return ()
        parts = list(relative.with_suffix("").parts)
        if parts:
            parts.pop()
        return tuple(parts)

    def module_path(module: str):
        base = Path("src", *module.split("."))
        if (root / base / "__init__.py").is_file():
            return str(base / "__init__.py")
        candidate = base.with_suffix(".py")
        if (root / candidate).is_file():
            return str(candidate)
        return None

    def node_modules(node, source: str) -> list:
        if isinstance(node, ast.ImportFrom):
            if node.level:
                package = package_parts(source)
                keep = len(package) - (node.level - 1)
                if keep < 0:
                    return []
                base = list(package[:keep])
                if node.module:
                    base.extend(node.module.split("."))
                    return [".".join(base)]
                return [
                    ".".join(base + [alias.name]) for alias in node.names
                ]
            modules = [node.module] if node.module else []
            if node.module == "continuum":
                modules.extend(
                    "continuum." + alias.name for alias in node.names
                )
            return modules
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        return []

    imported = set()
    pending = [path]
    visited = set()
    # Resolve the full transitive closure, including lazy imports nested in
    # function bodies: each fetched module is parsed just like the entry file.
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        source = (root / current).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            for module in node_modules(node, current):
                if module == "continuum" or module.startswith("continuum."):
                    resolved = module_path(module)
                    if resolved is None or resolved in imported:
                        continue
                    imported.add(resolved)
                    pending.append(resolved)
    return imported


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

    def test_delegated_bootstrap_runtime_supplies_python_dependency_closure(self):
        # delegation_runtime.py imports continuum.api_budget and
        # continuum.delegation_discovery at module load time, and those
        # modules have their own transitive runtime imports. Each delegated
        # bootstrap downloads a hand-picked file set into an ephemeral
        # RUNTIME_ROOT, so an omitted module fails before any CLI command runs
        # (the reported generic exit 2). The environment below is exactly that
        # file set with no checkout, PYTHONPATH, or site-packages.
        imported = continuum_imports(".github/scripts/delegation_runtime.py")
        self.assertIn("src/continuum/api_budget.py", imported)
        self.assertIn("src/continuum/delegation_discovery.py", imported)
        # config.py imports yamlmini, so a direct-import-only closure would
        # miss a dependency the ephemeral runtime actually needs.
        self.assertIn("src/continuum/yamlmini.py", imported)
        TMP_PARENT.mkdir(exist_ok=True)
        for path in BOOTSTRAP_WORKFLOWS:
            with self.subTest(path=path):
                sources = bootstrap_sources(read(path))
                self.assertTrue(sources, f"{path} has no bootstrap file set")
                missing = sorted(imported - set(sources.values()))
                self.assertEqual(
                    missing, [], f"{path} bootstrap omits runtime imports {missing}"
                )
                with tempfile.TemporaryDirectory(
                    prefix="pr-agent-bootstrap-", dir=str(TMP_PARENT)
                ) as tmp:
                    runtime = Path(tmp)
                    for dest, source in sources.items():
                        target = runtime / dest
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes((ROOT / source).read_bytes())
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-S",
                            str(runtime / ".github/scripts/delegation_runtime.py"),
                            "parent-variable-ids",
                            "--role",
                            "parent",
                            "--children-json",
                            '["child-a"]',
                        ],
                        cwd=runtime,
                        env={"PATH": os.environ.get("PATH", "")},
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(completed.stdout.strip(), "child-a")

    def test_runtime_dependency_closure_follows_transitive_lazy_imports(self):
        # The static closure must recurse into fetched modules. A lazy import
        # hidden in a function body of one of those modules is not executed by
        # the CLI smoke yet still breaks the ephemeral runtime when that code
        # path runs, so it must be part of the required file set.
        TMP_PARENT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="runtime-closure-", dir=str(TMP_PARENT)
        ) as tmp:
            root = Path(tmp)
            package = root / "src/continuum"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "entry.py").write_text(
                "from continuum import lazy\n", encoding="utf-8"
            )
            (package / "lazy.py").write_text(
                "def use():\n    from . import deep\n", encoding="utf-8"
            )
            (package / "deep.py").write_text("", encoding="utf-8")
            (root / "runtime.py").write_text(
                "from continuum import entry\n", encoding="utf-8"
            )
            imported = continuum_imports("runtime.py", root=root)
            self.assertIn("src/continuum/deep.py", imported)

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

    def test_persistent_state_uses_runner_local_review_file(self):
        body = read(".github/workflows/continuum-pr-agent.yml")
        start = body.index("Export native persistent finding state for the reviewed HEAD")
        end = body.index("\n      - name: ", start + 1)
        block = body[start:end]
        self.assertIn("REVIEW_FILE: ${{ steps.pragent.outputs.review_file }}", block)
        self.assertIn("review_file = os.environ.get(\"REVIEW_FILE\", \"\").strip()", block)
        self.assertIn("current_review = load_current_review()", block)
        self.assertNotIn("os.environ.get(\"REVIEW_JSON\"", block)

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
        self.assertIn("const decision = decideExactHeadCi({", body)
        self.assertIn("headSha: head", body)
        self.assertIn("decision.state === 'green'", body)
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
        self.assertIn("conclusion === 'skipped'", review)
        self.assertIn("delegated && newest.conclusion === 'skipped'", recovery)
        self.assertIn("delegated", merge)
        self.assertIn("conclusion === 'skipped'", merge)

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

    def test_review_completion_watchdog_and_outer_timeout_are_aligned(self):
        review = read(".github/workflows/continuum-pr-agent.yml")
        # Upstream reviews of exact current HEADs were reproducibly cut off at
        # 600s by the bridge although the runner/job had ample remaining time.
        self.assertIn("PR_AGENT_TIMEOUT_SECONDS: '1200'", review)
        self.assertEqual(review.count("CONFIG__AI_TIMEOUT: '1260'"), 2)
        self.assertNotIn("PR_AGENT_TIMEOUT_SECONDS: '600'", review)
        self.assertNotIn("CONFIG__AI_TIMEOUT: '660'", review)
        self.assertIn("timeout-minutes: ${{ fromJSON(vars.AUTOMATION_PR_AGENT_TIMEOUT_MINUTES || '60') }}", review)

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


class ExactHeadCiProofRegressionTests(unittest.TestCase):
    """Issue #368: one exact-HEAD CI proof across review, merge and recovery."""

    HEAD = "a" * 40
    OTHER = "b" * 40

    @classmethod
    def _ci_run(cls, **overrides):
        run = {
            "id": 101,
            "name": "CI",
            "head_sha": cls.HEAD,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-10-22T10:00:00Z",
        }
        run.update(overrides)
        return run

    @classmethod
    def _ci_scenarios(cls):
        run = cls._ci_run
        head = cls.HEAD
        return [
            (
                "proof-green-empty-listing",
                {"proofRun": run(), "runs": [], "workflowName": "CI", "headSha": head},
                "green",
                101,
            ),
            (
                "proof-green-stale-older-red",
                {
                    "proofRun": run(),
                    "runs": [
                        run(
                            id=99,
                            conclusion="failure",
                            created_at="2026-10-22T09:00:00Z",
                        )
                    ],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "green",
                101,
            ),
            (
                "newer-red-blocks",
                {
                    "proofRun": run(),
                    "runs": [
                        run(
                            id=102,
                            conclusion="failure",
                            created_at="2026-10-22T11:00:00Z",
                        )
                    ],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "red",
                102,
            ),
            (
                "newer-pending-blocks",
                {
                    "proofRun": run(),
                    "runs": [
                        run(
                            id=102,
                            status="in_progress",
                            conclusion=None,
                            created_at="2026-10-22T11:00:00Z",
                        )
                    ],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "pending",
                102,
            ),
            (
                "empty-all-is-unavailable",
                {"proofRun": None, "runs": [], "workflowName": "CI", "headSha": head},
                "unavailable",
                None,
            ),
            (
                "wrong-workflow-is-not-proof",
                {
                    "proofRun": run(name="Other"),
                    "runs": [],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "unavailable",
                None,
            ),
            (
                "moved-head-is-not-proof",
                {
                    "proofRun": run(head_sha=cls.OTHER),
                    "runs": [],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "unavailable",
                None,
            ),
            (
                "non-pr-event-is-not-proof",
                {
                    "proofRun": run(event="push"),
                    "runs": [],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "unavailable",
                None,
            ),
            (
                "missing-event-is-not-proof",
                {
                    "proofRun": run(event=None),
                    "runs": [],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "unavailable",
                None,
            ),
            (
                "listing-only-green",
                {"proofRun": None, "runs": [run()], "workflowName": "CI", "headSha": head},
                "green",
                101,
            ),
            (
                "same-run-completed-state-wins",
                {
                    "proofRun": run(
                        status="in_progress",
                        conclusion=None,
                        updated_at="2026-10-22T10:01:00Z",
                    ),
                    "runs": [run(updated_at="2026-10-22T10:05:00Z")],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "green",
                101,
            ),
            (
                "listing-only-red",
                {
                    "proofRun": None,
                    "runs": [run(conclusion="failure")],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "red",
                101,
            ),
            (
                "delegated-skipped-accepted",
                {
                    "proofRun": run(conclusion="skipped"),
                    "runs": [],
                    "workflowName": "CI",
                    "headSha": head,
                    "delegated": True,
                },
                "green",
                101,
            ),
            (
                "skipped-not-accepted-locally",
                {
                    "proofRun": run(conclusion="skipped"),
                    "runs": [],
                    "workflowName": "CI",
                    "headSha": head,
                    "delegated": False,
                },
                "red",
                101,
            ),
            (
                "older-green-does-not-bypass-newer-red",
                {
                    "proofRun": None,
                    "runs": [
                        run(id=99, created_at="2026-10-22T09:00:00Z"),
                        run(
                            id=102,
                            conclusion="failure",
                            created_at="2026-10-22T11:00:00Z",
                        ),
                    ],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "red",
                102,
            ),
            (
                "trimmed-head-is-proof",
                {
                    "proofRun": run(head_sha="  " + head + "  "),
                    "runs": [],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "green",
                101,
            ),
            (
                "offset-created-at-orders-chronologically",
                {
                    "proofRun": run(created_at="2026-10-22T09:00:00-05:00"),
                    "runs": [
                        run(
                            id=102,
                            conclusion="failure",
                            created_at="2026-10-22T10:00:00Z",
                        )
                    ],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "green",
                101,
            ),
            (
                "custom-workflow-name-is-proof",
                {
                    "proofRun": run(name="Build and test"),
                    "runs": [],
                    "workflowName": "Build and test",
                    "headSha": head,
                },
                "green",
                101,
            ),
            (
                "custom-workflow-name-rejects-default-ci",
                {
                    "proofRun": run(name="CI"),
                    "runs": [run(name="CI")],
                    "workflowName": "Build and test",
                    "headSha": head,
                },
                "unavailable",
                None,
            ),
        ]

    @classmethod
    def _listing_scenarios(cls):
        run = cls._ci_run
        head = cls.HEAD
        return [
            (
                "green-proof-unreadable-listing",
                {
                    "proofRun": run(),
                    "runs": None,
                    "workflowName": "CI",
                    "headSha": head,
                },
                "unavailable",
                None,
            ),
            (
                "green-proof-empty-listing",
                {
                    "proofRun": run(),
                    "runs": [],
                    "workflowName": "CI",
                    "headSha": head,
                },
                "green",
                101,
            ),
            (
                "red-proof-unreadable-listing",
                {
                    "proofRun": run(conclusion="failure"),
                    "runs": None,
                    "workflowName": "CI",
                    "headSha": head,
                },
                "unavailable",
                None,
            ),
            (
                "pending-proof-unreadable-listing",
                {
                    "proofRun": run(status="in_progress", conclusion=None),
                    "runs": None,
                    "workflowName": "CI",
                    "headSha": head,
                },
                "unavailable",
                None,
            ),
            (
                "missing-proof-unreadable-listing",
                {
                    "proofRun": None,
                    "runs": None,
                    "workflowName": "CI",
                    "headSha": head,
                },
                "unavailable",
                None,
            ),
        ]

    @staticmethod
    def _extract(source: str, name: str, start: int = 0) -> str:
        import re

        match = re.search(
            r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(",
            source[start:],
        )
        if match is None:
            raise AssertionError(f"JS function {name} not found")
        offset = start + match.start()
        # Skip the parameter list (which may contain destructuring braces)
        # and start counting at the function body brace.
        opening = source.index(") {", start + match.end()) + 2
        depth = 0
        for index in range(opening, len(source)):
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return source[offset : index + 1]
        raise AssertionError(f"JS function {name} is unterminated")

    @staticmethod
    def _extract_retry_decision(source: str) -> str:
        import re
        import textwrap

        anchor = source.index('CI_STATE="$(python3 - "$PROOF_JSON"')
        heredoc = source.index("<<'PY'", anchor)
        body_start = source.index("\n", heredoc) + 1
        terminator = re.search(r"^\s*PY\s*$", source[body_start:], re.MULTILINE)
        if terminator is None:
            raise AssertionError("retry-dispatch CI Python heredoc is unterminated")
        body = source[body_start : body_start + terminator.start()]
        return textwrap.dedent(body).strip("\n") + "\n"

    def test_canonical_decision_rejects_the_incident_paradox_for_every_consumer(self):
        import json
        import re
        import shutil
        import subprocess
        import tempfile

        review = read(".github/workflows/continuum-pr-agent.yml")
        merge = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        recovery = read(".github/workflows/continuum-pr-agent-recovery.yml")

        review_starts = [
            match.start()
            for match in re.finditer(r"function decideExactHeadCi\b", review)
        ]
        self.assertGreaterEqual(len(review_starts), 2)
        decisions = [
            self._extract(review, "decideExactHeadCi", start)
            for start in review_starts
        ]
        decisions.append(self._extract(merge, "decideExactHeadCi"))
        decisions.append(self._extract(recovery, "decideExactHeadCi"))

        helpers = "\n".join(
            self._extract(review, name)
            for name in (
                "ciRunCreatedAt",
                "ciRunUpdatedAt",
                "ciRunMatchesExactHead",
                "ciRunProofState",
            )
        )
        renamed = []
        for index, source in enumerate(decisions):
            renamed.append(re.sub(r"\bdecideExactHeadCi\b", f"decideExactHeadCi{index}", source))

        scenario_source = (
            "const scenarios = "
            + json.dumps(self._ci_scenarios())
            + ";\n"
            + """
for (let fnIndex = 0; fnIndex < DECISION_COUNT; fnIndex += 1) {
  for (const [name, payload, expectedState, expectedRunId] of scenarios) {
    const decision = decisionFns[fnIndex](payload);
    assert.strictEqual(decision.state, expectedState, name + ' state');
    assert.strictEqual(decision.runId, expectedRunId, name + ' run id');
  }
}
console.log('CI_PROOF_OK');
"""
        )
        harness = (
            "const assert = require('assert');\n"
            + helpers
            + "\n"
            + "\n".join(renamed)
            + "\nconst decisionFns = ["
            + ", ".join(f"decideExactHeadCi{i}" for i in range(len(renamed)))
            + "];\nconst DECISION_COUNT = "
            + str(len(renamed))
            + ";\n"
            + scenario_source
        )
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required to execute the shipped CI proof")
        temp_dir = ROOT / ".opencode-tmp"
        temp_dir.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", dir=str(temp_dir), delete=False
        ) as handle:
            handle.write(harness)
            script = handle.name
        try:
            completed = subprocess.run(
                [node, script], capture_output=True, text=True, timeout=30
            )
        finally:
            import os

            os.unlink(script)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("CI_PROOF_OK", completed.stdout)

    def test_unreadable_listing_fails_closed_for_review_and_merge(self):
        import json
        import os
        import re
        import shutil
        import subprocess
        import tempfile

        review = read(".github/workflows/continuum-pr-agent.yml")
        merge = read(".github/workflows/continuum-pr-agent-auto-merge.yml")

        helpers = "\n".join(
            self._extract(review, name)
            for name in (
                "ciRunCreatedAt",
                "ciRunUpdatedAt",
                "ciRunMatchesExactHead",
                "ciRunProofState",
            )
        )
        review_decision = self._extract(review, "decideExactHeadCi")
        merge_decision = self._extract(merge, "decideExactHeadCi")
        review_wrapper = self._extract(review, "decideWithCiListing")
        merge_wrapper = self._extract(merge, "decideWithCiListing")
        review_reader = self._extract(review, "readRuns")
        merge_reader = self._extract(merge, "listCiRunsForHead")

        def rename(source, decision_name, wrapper_name=None):
            source = re.sub(r"\bdecideExactHeadCi\b", decision_name, source)
            if wrapper_name:
                source = re.sub(
                    r"\bdecideWithCiListing\b", wrapper_name, source
                )
            return source

        review_decision = rename(review_decision, "decideReviewCi")
        merge_decision = rename(merge_decision, "decideMergeCi")
        review_wrapper = rename(
            review_wrapper, "decideReviewCi", "decideReviewWithListing"
        )
        merge_wrapper = rename(
            merge_wrapper, "decideMergeCi", "decideMergeWithListing"
        )

        scenario_source = (
            "const scenarios = "
            + json.dumps(self._listing_scenarios())
            + ";\n"
            + """
process.env.CONTINUUM_PR_AGENT_TARGET_OWNER = 'owner';
process.env.CONTINUUM_PR_AGENT_TARGET_REPO = 'repo';
const owner = 'owner';
const repo = 'repo';
const admittedSha = scenarios[0][1].headSha;
const core = { info() {} };
const github = {
  rest: {
    actions: {
      listWorkflowRunsForRepo: async () => {
        throw new Error('transient API failure');
      },
    },
  },
};
const wrappers = [decideReviewWithListing, decideMergeWithListing];
(async () => {
  for (const wrapper of wrappers) {
    for (const [name, payload, expectedState, expectedRunId] of scenarios) {
      const decision = wrapper(payload);
      assert.strictEqual(decision.state, expectedState, name + ' state');
      assert.strictEqual(decision.runId, expectedRunId, name + ' run id');
    }
  }
  const readers = [readRuns, listCiRunsForHead];
  for (const reader of readers) {
    const runs = await reader(null, admittedSha);
    assert.strictEqual(runs, null, 'failed listing read must return null');
    for (const wrapper of wrappers) {
      const decision = wrapper({
        proofRun: scenarios[0][1].proofRun,
        runs,
        workflowName: scenarios[0][1].workflowName,
        headSha: admittedSha,
      });
      assert.strictEqual(
        decision.state,
        'unavailable',
        'a failed API read must fail closed even with green proof'
      );
      assert.strictEqual(decision.runId, null);
    }
  }
  console.log('CI_LISTING_FAIL_CLOSED_OK');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
        )
        harness = (
            "const assert = require('assert');\n"
            + helpers
            + "\n"
            + "const delegated = false;\n"
            + review_decision
            + "\n"
            + merge_decision
            + "\n"
            + review_wrapper
            + "\n"
            + merge_wrapper
            + "\n"
            + review_reader
            + "\n"
            + merge_reader
            + "\n"
            + scenario_source
        )
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required to execute the shipped CI proof")
        temp_dir = ROOT / ".opencode-tmp"
        temp_dir.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", dir=str(temp_dir), delete=False
        ) as handle:
            handle.write(harness)
            script = handle.name
        try:
            completed = subprocess.run(
                [node, script], capture_output=True, text=True, timeout=30
            )
        finally:
            os.unlink(script)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("CI_LISTING_FAIL_CLOSED_OK", completed.stdout)

    def test_retry_dispatch_python_decision_matches_canonical_scenarios(self):
        import json
        import shutil

        source = read(".github/workflows/continuum-pr-agent.yml")
        decision = self._extract_retry_decision(source)
        python = shutil.which("python3")
        self.assertIsNotNone(
            python, "python3 is required to execute the shipped retry CI proof"
        )
        TMP_PARENT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="exact-head-ci-", dir=str(TMP_PARENT)
        ) as temp:
            script_path = Path(temp) / "decision.py"
            script_path.write_text(decision, encoding="utf-8")
            proof_path = Path(temp) / "proof.json"
            list_path = Path(temp) / "runs.json"
            scenarios = self._ci_scenarios() + self._listing_scenarios()
            for name, payload, expected_state, _ in scenarios:
                with self.subTest(scenario=name):
                    proof = payload.get("proofRun")
                    if proof is None:
                        proof_path.unlink(missing_ok=True)
                    else:
                        proof_path.write_text(json.dumps(proof), encoding="utf-8")
                    runs = payload.get("runs")
                    if runs is None:
                        # A failed `gh api` listing read deletes $CI_JSON, so
                        # the shipped decision must see a missing file.
                        list_path.unlink(missing_ok=True)
                    else:
                        list_path.write_text(
                            json.dumps({"workflow_runs": runs}),
                            encoding="utf-8",
                        )
                    delegated = (
                        "true" if payload.get("delegated") else "false"
                    )
                    completed = subprocess.run(
                        [
                            python,
                            str(script_path),
                            str(proof_path),
                            str(list_path),
                            payload["workflowName"],
                            payload["headSha"],
                            delegated,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        completed.stderr or completed.stdout,
                    )
                    self.assertEqual(
                        completed.stdout.strip(), expected_state, name
                    )

    def test_run_id_is_carried_from_admission_to_merge_and_recovery(self):
        review = read(".github/workflows/continuum-pr-agent.yml")
        merge = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        recovery = read(".github/workflows/continuum-pr-agent-recovery.yml")
        stub = read(".github/caller-stubs/continuum-pr-agent-auto-merge.yml")

        self.assertIn("core.setOutput('ci_run_id'", review)
        self.assertIn("ci_run_id: ${{ steps.admit.outputs.ci_run_id }}", review)
        self.assertIn("CI_RUN_ID: ${{ steps.admit.outputs.ci_run_id }}", review)
        self.assertIn("ADMITTED_CI_RUN_ID: ${{ steps.admit.outputs.ci_run_id }}", review)
        self.assertIn("ci_run_id: ${{ needs.pr_agent.outputs.ci_run_id }}", review)
        self.assertIn("github.rest.actions.getWorkflowRun", review)

        self.assertIn("ci_run_id:", merge)
        self.assertIn("CI_RUN_ID: ${{ inputs.ci_run_id }}", merge)
        self.assertIn("github.rest.actions.getWorkflowRun", merge)
        self.assertIn("resolveExactHeadCi", merge)

        # The merge gate resolves the same configurable CI workflow name as
        # admission and recovery (issue #368 follow-up): a non-default
        # CONTINUUM_CI_WORKFLOW_NAME must not degrade the verified run-id proof
        # to 'unavailable' on a missing 'CI' run name.
        resolved_ci_name = (
            "inputs.ci_workflow_name || vars.CONTINUUM_CI_WORKFLOW_NAME || 'CI'"
        )
        self.assertIn(resolved_ci_name, review)
        self.assertIn("ci_workflow_name: ${{ " + resolved_ci_name + " }}", review)
        self.assertIn("ci_workflow_name:", merge)
        self.assertIn("CI_WORKFLOW_NAME: ${{ " + resolved_ci_name + " }}", merge)
        self.assertIn(
            "resolveExactHeadCi(pr, pr.head.sha, ciWorkflowName)", merge
        )
        self.assertNotIn("resolveExactHeadCi(pr, pr.head.sha, 'CI')", merge)
        self.assertIn("CI_WORKFLOW_NAME: ${{ " + resolved_ci_name + " }}", recovery)

        self.assertIn("function decideExactHeadCi", recovery)
        self.assertIn("decideExactHeadCi({", recovery)

        self.assertIn("ci_run_id:", stub)
        self.assertIn('ci_run_id: "${{ inputs.ci_run_id }}"', stub)
        self.assertIn("ci_workflow_name:", stub)
        self.assertIn(
            'ci_workflow_name: "${{ inputs.ci_workflow_name }}"', stub
        )


if __name__ == "__main__":
    unittest.main()
