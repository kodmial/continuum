#!/usr/bin/env bash
# Durable operator STOP control-plane handler (kodmial/continuum#346).
#
# Runs in a cheap, separate control-plane job -- never in the heavy coding
# job. Revokes automation:ready first, persists the durable intent/fence and
# the shared terminal generation stop, cancels only runs with matching
# issue/workflow provenance, then observes the real terminal state.
#
# Required environment:
#   GH_TOKEN, REPO_FULL, ISSUE_NUMBER, COMMENT_BODY, COMMENT_AUTHOR,
#   COMMENT_ID, COMMENT_CREATED_AT, REPO_OWNER, ENGINE_ROOT,
#   WORKFLOW_PATHS (comma separated), GENERATION (default 1).
#
#   REPO_FULL owns the issue, comments, ready label and durable stop record.
#   RUN_REPO_FULL (default: REPO_FULL) owns the Actions runs to cancel, so a
#   private Child STOP can fence the Child issue while cancelling the Parent
#   delegated worker run keyed by `SubTask #<issue>`.
#
# Exit 0 only for STOPPED. CANCEL_PENDING after the bounded observation
# window, PARTIAL, and FAILED exit non-zero with an actionable ::error::.

set -uo pipefail

generation="${GENERATION:-1}"
ready_label="automation:ready"
engine="${ENGINE_ROOT:?ENGINE_ROOT is required}"
export ENGINE_ROOT="$engine"
stop_cli="$engine/.github/scripts/issue_stop.py"
command_cli="$engine/.github/scripts/opencode_command.py"
work="${RUNNER_TEMP:-/tmp}/continuum-stop-$ISSUE_NUMBER"
mkdir -p "$work"

fail() {
  echo "::error::$*"
  exit 1
}

[[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]] || fail "Invalid issue number '$ISSUE_NUMBER'."
[[ -n "${REPO_FULL:-}" ]] || fail "REPO_FULL is required."
[[ -n "${REPO_OWNER:-}" ]] || fail "REPO_OWNER is required."
run_repo="${RUN_REPO_FULL:-$REPO_FULL}"
[[ "$run_repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || \
  fail "Invalid RUN_REPO_FULL '$run_repo'."

# Exact command classification plus provenance. Prose, quotes, snapshots and
# machine comments never reach this point as authority.
command="$(python3 "$command_cli" is-owner-stop \
  --body "$COMMENT_BODY" --author "$COMMENT_AUTHOR" --owner "$REPO_OWNER" 2>/dev/null || echo false)"
[[ "$command" == "true" ]] || {
  echo "Not an authorized operator STOP; ignoring."
  exit 0
}

issue_json="$work/issue.json"
comments_json="$work/comments.json"
runs_json="$work/runs.json"
targets_json="$work/targets.json"

if ! gh api "repos/$REPO_FULL/issues/$ISSUE_NUMBER" >"$issue_json"; then
  fail "Could not read issue #$ISSUE_NUMBER for STOP."
fi

: >"$work/comments-pages.jsonl"
for page in 1 2 3; do
  if ! page_json="$(gh api "repos/$REPO_FULL/issues/$ISSUE_NUMBER/comments?per_page=100&page=$page")"; then
    fail "Could not read comments for issue #$ISSUE_NUMBER; refusing to report STOPPED."
  fi
  printf '%s\n' "$page_json" >>"$work/comments-pages.jsonl"
  count="$(jq 'length' <<<"$page_json" 2>/dev/null || echo 0)"
  [[ "$count" == "100" ]] || break
done
if ! jq -s 'add // []' "$work/comments-pages.jsonl" >"$comments_json"; then
  fail "Could not assemble issue comments for STOP."
fi

# 1) Revoke ready FIRST. Removing ready alone never cancels anything, but a
#    STOP must prevent every new start even while cancellation is in flight.
if ! gh api --method DELETE \
  "repos/$REPO_FULL/issues/$ISSUE_NUMBER/labels/${ready_label//:/%3A}" >/dev/null 2>&1; then
  # 404 means already absent, which is the desired state.
  :
fi
echo "Revoked $ready_label on #$ISSUE_NUMBER before cancellation."

# 2) Persist the durable intent/fence and the shared terminal stop BEFORE any
#    cancellation attempt. Repeated STOP is idempotent: an existing intent is
#    reused and no second fence is written.
intent_present="$(python3 - "$comments_json" "$REPO_FULL" "$ISSUE_NUMBER" "$generation" <<'PY'
import json, sys
sys.path.insert(0, __import__('os').environ.get('ENGINE_ROOT', '') + '/src')
from continuum.issue_stop import select_stop_intent
comments = json.load(open(sys.argv[1], encoding='utf-8'))
intent = select_stop_intent(comments, repo=sys.argv[2], issue=int(sys.argv[3]),
                            generation=int(sys.argv[4]), owner_login=sys.argv[2].split('/')[0])
print('yes' if intent else 'no')
PY
)"
if [[ "$intent_present" != "yes" ]]; then
  intent_body="$(python3 "$stop_cli" render-intent \
    --repo "$REPO_FULL" --issue "$ISSUE_NUMBER" --generation "$generation" \
    --comment-id "${COMMENT_ID:-0}" --comment-created-at "${COMMENT_CREATED_AT:-}")" || \
    fail "Could not render STOP intent."
  gh api --method POST "repos/$REPO_FULL/issues/$ISSUE_NUMBER/comments" \
    -f body="$intent_body" >/dev/null || fail "Could not persist STOP intent."
fi

stop_active="$(python3 "$stop_cli" is-active --comments-json "$comments_json" \
  --repo "$REPO_FULL" --issue "$ISSUE_NUMBER" --generation "$generation" \
  --owner "$REPO_OWNER" 2>/dev/null || echo active)"
if [[ "$stop_active" != "stopped" ]]; then
  spec="$(python3 - "$issue_json" <<'PY'
import json, sys
sys.path.insert(0, __import__('os').environ.get('ENGINE_ROOT', '') + '/src')
from continuum.task_snapshot import spec_digest
issue = json.load(open(sys.argv[1], encoding='utf-8'))
print(spec_digest(issue.get('title') or '', issue.get('body') or ''))
PY
)" || fail "Could not compute the STOP fingerprint."
  stop_body="$(python3 "$stop_cli" render-stop --repo "$REPO_FULL" \
    --issue "$ISSUE_NUMBER" --generation "$generation" \
    --pinned-spec "$spec" --live-spec "$spec" --reason operator)" || \
    fail "Could not render STOP state."
  gh api --method POST "repos/$REPO_FULL/issues/$ISSUE_NUMBER/comments" \
    -f body="$stop_body" >/dev/null || fail "Could not persist STOP state."
fi
echo "Persisted durable operator STOP for $REPO_FULL#$ISSUE_NUMBER generation $generation."

# 3) Discover only runs with matching issue + workflow provenance. Never cancel
#    all workflows, and never match a display title alone.
gh api "repos/$run_repo/actions/runs?per_page=100" >"$runs_json" || \
  fail "Could not list Actions runs for STOP; no cancellation was attempted."

paths_args=()
IFS=',' read -r -a paths <<<"${WORKFLOW_PATHS:-}"
for path in "${paths[@]}"; do
  path="$(printf '%s' "$path" | xargs)"
  [[ -n "$path" ]] && paths_args+=(--workflow-path "$path")
done

python3 "$stop_cli" select-targets --runs-json "$runs_json" \
  --issue "$ISSUE_NUMBER" --generation "$generation" "${paths_args[@]}" \
  >"$targets_json" || fail "Could not select cancellation targets."

target_ids="$(jq -c '.target_ids' "$targets_json")"
echo "STOP targets in $run_repo for #$ISSUE_NUMBER generation $generation: $target_ids"

cancel_failure=""
while IFS= read -r run_id; do
  [[ -n "$run_id" ]] || continue
  if ! gh api --method POST "repos/$run_repo/actions/runs/$run_id/cancel" >/dev/null 2>&1; then
    cancel_failure="${cancel_failure}run $run_id cancel failed; "
  fi
done < <(jq -r '.[]' <<<"$target_ids")

# 4) Observe until every target is terminal, bounded. A successful POST is not
#    proof that the process stopped.
state="CANCEL_PENDING"
for _attempt in 1 2 3 4 5 6; do
  sleep 5
  if ! gh api "repos/$run_repo/actions/runs?per_page=100" >"$work/runs-observed.json"; then
    cancel_failure="${cancel_failure}run inventory unavailable; "
    break
  fi
  printf '{"fence":"%s","repo":"%s","issue":%s,"generation":%s}\n' \
    "${COMMENT_ID:-0}" "$REPO_FULL" "$ISSUE_NUMBER" "$generation" >"$work/intent.json"
  observation="$(python3 "$stop_cli" observe --intent-json "$work/intent.json" \
      --targets-json "$targets_json" --runs-json "$work/runs-observed.json" \
      --cancel-failure "$cancel_failure")" || {
    state="FAILED"
    break
  }
  state="$(jq -r '.state' <<<"$observation")"
  [[ "$state" == "CANCEL_PENDING" ]] || break
done

echo "Operator STOP observation in $run_repo for #$ISSUE_NUMBER generation $generation: $state"
case "$state" in
  STOPPED)
    echo "::notice::STOPPED $REPO_FULL#$ISSUE_NUMBER generation $generation; targets $target_ids are terminal."
    exit 0
    ;;
  CANCEL_PENDING)
    echo "::error::CANCEL_PENDING for $REPO_FULL#$ISSUE_NUMBER generation $generation; targets $target_ids were not observed terminal within the bounded window."
    exit 1
    ;;
  PARTIAL)
    echo "::error::PARTIAL STOP for $REPO_FULL#$ISSUE_NUMBER generation $generation; some targets are missing or ended unexpectedly."
    exit 1
    ;;
  *)
    echo "::error::FAILED STOP for $REPO_FULL#$ISSUE_NUMBER generation $generation: ${cancel_failure:-cancellation API unavailable}."
    exit 1
    ;;
esac
