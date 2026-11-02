#!/usr/bin/env bash
# Durable, private, generation-scoped work checkpoints for delegated tasks.
# Sourced by the reusable worker after the authoritative task snapshot is verified.
# The shared ``continuum_pr_staging.sh`` boundary is mandatory for checkpoint
# save, so it is loaded from this script's own directory when the caller has not
# sourced it already. A checkpoint never falls back to plain ``git add -A``.
# Never upload code, patches or agent transcripts to the public parent.

if ! declare -F continuum_stage_checkpoint >/dev/null; then
  continuum_checkpoint_dir="${BASH_SOURCE[0]%/*}"
  # Sourcing by bare file name leaves no slash, so ``%/*`` keeps the whole
  # name; fall back to the current directory in that case.
  [[ "$continuum_checkpoint_dir" == "${BASH_SOURCE[0]}" ]] && continuum_checkpoint_dir=.
  continuum_checkpoint_staging_lib="$continuum_checkpoint_dir/continuum_pr_staging.sh"
  if [[ -r "$continuum_checkpoint_staging_lib" ]]; then
    # shellcheck source=/dev/null
    source "$continuum_checkpoint_staging_lib"
  fi
fi

continuum_checkpoint_ref() {
  local task="$1" generation="$2" spec="$3"
  [[ "$task" =~ ^[1-9][0-9]*$ ]] || return 2
  [[ "$generation" =~ ^[1-9][0-9]*$ ]] || return 2
  [[ "$spec" =~ ^[0-9a-f]{64}$ ]] || return 2
  printf 'continuum-child/checkpoint-task-%s-g%s-%s\n' "$task" "$generation" "$spec"
}

# Resolve the Child repository's default branch once per operation. A
# hardcoded origin/main fails closed on a repository whose default branch has
# another name (and has no origin/main at all). Prefer the local remote-HEAD
# symref a clone records, then ask the remote; never guess a branch name.
continuum_checkpoint_default_branch() {
  local remote_head target
  # The local origin/HEAD symref can be stale (a clone records it once and
  # never refreshes it). Refresh it from the remote before trusting it; when
  # the refresh fails, fall back to the explicit remote lookup below.
  if git remote set-head origin --auto >/dev/null 2>&1; then
    remote_head="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)" || remote_head=""
  else
    remote_head=""
  fi
  if [[ -n "$remote_head" ]]; then
    printf '%s\n' "${remote_head#origin/}"
    return 0
  fi
  target="$(git ls-remote --symref origin HEAD 2>/dev/null \
    | sed -n 's|^ref: refs/heads/\([^[:space:]]*\)[[:space:]]*HEAD$|\1|p' \
    | head -n 1)"
  if [[ -n "$target" ]]; then
    printf '%s\n' "$target"
    return 0
  fi
  echo "::error::Delegated checkpoint cannot resolve the child repository default branch." >&2
  return 4
}

# A shallow clone does not contain the common ancestor, so a shared-history
# check would reject a valid checkpoint. Deepen the checkout once, only when
# it actually is shallow. The deepening is bounded, so it may still leave the
# ancestor outside the fetched history; the shared-history guard treats that
# unresolved case as inconclusive rather than as unrelated history.
continuum_checkpoint_ensure_history() {
  if [[ "$(git rev-parse --is-shallow-repository 2>/dev/null)" != "true" ]]; then
    return 0
  fi
  git fetch --quiet --unshallow origin >/dev/null 2>&1 \
    || git fetch --quiet --deepen=100 origin >/dev/null 2>&1 \
    || return 4
}

# In a complete checkout ``git merge-base`` exits 1 only when the two commits
# genuinely share no common ancestor. A shallow checkout can also exit 1 when
# the common ancestor merely lies beyond the deepening fallback, so that case
# is reported separately instead of claiming the checkpoint is unrelated.
# Any other non-zero status (a missing default-branch ref, a corrupt object
# database, a broken repository) is infrastructure damage, and reporting it as
# "unrelated history" would mask the real failure.
continuum_checkpoint_assert_shared_history() {
  local default_ref="$1" checkpoint_sha="$2" merge_base_rc=0
  git merge-base "$default_ref" "$checkpoint_sha" >/dev/null 2>&1 \
    || merge_base_rc=$?
  case "$merge_base_rc" in
    0)
      return 0
      ;;
    1)
      if [[ "$(git rev-parse --is-shallow-repository 2>/dev/null)" == "true" ]]; then
        echo "::error::Delegated checkpoint history is inconclusive: the checkout is shallow and the common ancestor may lie beyond the fetched history; fetch a deeper history and retry." >&2
      else
        echo "::error::Delegated checkpoint history is unrelated to the target default branch." >&2
      fi
      return 4
      ;;
    *)
      echo "::error::Delegated checkpoint history check failed: git merge-base exited with status $merge_base_rc; refusing to continue." >&2
      return 4
      ;;
  esac
}

# A remote lookup failure is NOT evidence of a missing checkpoint: fail closed,
# rather than discarding work after rate limits or a transport interruption.
continuum_checkpoint_restore() {
  local checkpoint_ref="$1" branch="$2" spec="$3"
  local remote_line default_branch default_ref
  CONTINUUM_CHECKPOINT_RESTORED=false
  default_branch="$(continuum_checkpoint_default_branch)" || return 4
  default_ref="refs/remotes/origin/$default_branch"
  # The remote lookup returns only a branch name and creates no local
  # remote-tracking ref, so fetch the resolved branch before using it.
  git fetch --quiet origin "+refs/heads/$default_branch:$default_ref" >/dev/null 2>&1 || return 4
  if ! remote_line="$(git ls-remote --heads origin "refs/heads/$checkpoint_ref" 2>/dev/null)"; then
    echo "::error::Delegated checkpoint lookup unavailable; refusing a fresh start." >&2
    return 4
  fi
  if [[ -z "$remote_line" ]]; then
    git switch -c "$branch" "$default_ref" >/dev/null 2>&1 || return 4
    return 0
  fi

  local checkpoint_sha
  git fetch --quiet origin "refs/heads/$checkpoint_ref" >/dev/null 2>&1 || return 4
  # Deepening the checkout fetches again and overwrites FETCH_HEAD, so pin the
  # checkpoint commit now and use this SHA for every later check and switch.
  checkpoint_sha="$(git rev-parse FETCH_HEAD 2>/dev/null)" || return 4
  if ! git log -1 --format=%B "$checkpoint_sha" | grep -Fxq "Continuum-Checkpoint: $spec"; then
    echo "::error::Delegated checkpoint has no matching frozen task specification." >&2
    return 4
  fi
  continuum_checkpoint_ensure_history || return 4
  # A checkpoint only has to share history with the target branch: it may
  # legitimately lag behind an advanced default branch, so requiring the
  # default branch to be its ancestor would discard valid resumed work.
  continuum_checkpoint_assert_shared_history "$default_ref" "$checkpoint_sha" || return 4
  if git diff --no-ext-diff "$default_ref...$checkpoint_sha" -- . | grep -Eiq '(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|rnd_[A-Za-z0-9]+|-----BEGIN [A-Z ]*PRIVATE KEY-----)'; then
    echo "::error::Token-like content detected; checkpoint restore refused." >&2
    return 31
  fi
  git switch -c "$branch" "$checkpoint_sha" >/dev/null 2>&1 || return 4
  CONTINUUM_CHECKPOINT_RESTORED=true
  echo "::notice::Resumed verified generation-scoped delegated work checkpoint."
}

# The worker, not the agent, owns all Git mutations. The caller supplies its
# canonical non-default-branch guard and only invokes this after snapshot admission.
continuum_checkpoint_save() {
  local checkpoint_ref="$1" spec="$2" child_repo="$3" task="$4"
  local attempt=0 checkpoint_sha default_branch default_ref
  if [[ -z "$(git status --porcelain)" ]]; then
    return 0
  fi
  # The shared staging boundary removes workflow-owned engine checkouts and
  # rejects undeclared Gitlinks. A plain `git add -A` would let a remaining
  # checkout enter the checkpoint commit, so require the library explicitly
  # rather than silently falling back to plain staging.
  if ! declare -F continuum_stage_checkpoint >/dev/null; then
    echo "::error::Checkpoint staging boundary is unavailable." >&2
    return 4
  fi
  default_branch="$(continuum_checkpoint_default_branch)" || return 4
  default_ref="refs/remotes/origin/$default_branch"
  git fetch --quiet origin "+refs/heads/$default_branch:$default_ref" >/dev/null 2>&1 || return 4
  continuum_stage_checkpoint || return 4
  if git diff --cached --no-ext-diff | grep -Eiq '(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|rnd_[A-Za-z0-9]+|-----BEGIN [A-Z ]*PRIVATE KEY-----)'; then
    echo "::error::Token-like content detected; checkpoint publication refused." >&2
    return 31
  fi
  if git diff --cached --quiet; then
    return 0
  fi
  git commit -m "checkpoint task #$task: unaccepted work" \
    -m "Continuum-Component: delegation-worker" \
    -m "Continuum-Checkpoint: $spec" >/dev/null || return 4
  continuum_assert_safe_push "$checkpoint_ref" "$child_repo" || return 4
  # Concurrent runners share one ref per frozen generation. A plain push
  # loses the race: the loser fails non-fast-forward and discards its
  # delta. Fetch the winner and replay locally, then retry, so both
  # deltas survive when they do not conflict.
  while true; do
    if git push origin "HEAD:refs/heads/$checkpoint_ref" >/dev/null 2>&1; then
      break
    fi
    attempt=$((attempt + 1))
    if [[ "$attempt" -ge 3 ]]; then
      return 4
    fi
    git fetch --quiet origin "refs/heads/$checkpoint_ref" >/dev/null 2>&1 || continue
    # Pin the winner before deepening: the later fetch overwrites FETCH_HEAD.
    checkpoint_sha="$(git rev-parse FETCH_HEAD 2>/dev/null)" || continue
    if ! git log -1 --format=%B "$checkpoint_sha" | grep -Fxq "Continuum-Checkpoint: $spec"; then
      echo "::error::Delegated checkpoint changed to an untrusted revision; refusing to overwrite." >&2
      return 4
    fi
    continuum_checkpoint_ensure_history || return 4
    # Same shared-history rule as restore: the concurrent winner may lag
    # behind an advanced default branch and is still safe to rebase onto.
    continuum_checkpoint_assert_shared_history "$default_ref" "$checkpoint_sha" || return 4
    if ! git rebase "$checkpoint_sha" >/dev/null 2>&1; then
      git rebase --abort >/dev/null 2>&1 || true
      # The losing runner's delta is still only local; the ephemeral workdir
      # is discarded on exit, so preserve it on a side ref instead of
      # silently dropping one side of the conflict. The winner checkpoint
      # is left untouched for fail-closed manual reconciliation.
      conflict_sha="$(git rev-parse --short HEAD 2>/dev/null || echo local)"
      conflict_ref="${checkpoint_ref}-conflict-${conflict_sha}"
      if continuum_assert_safe_push "$conflict_ref" "$child_repo"; then
        git push origin "HEAD:refs/heads/$conflict_ref" >/dev/null 2>&1 || true
      fi
      echo "::error::Delegated checkpoint update conflicts with concurrent work; losing delta preserved at $conflict_ref; refusing to overwrite." >&2
      return 4
    fi
  done
  echo "::notice::Saved delegated work checkpoint on the child repository; not accepted and no PR created."
}

continuum_checkpoint_cleanup() {
  local checkpoint_ref="$1" child_repo="$2"
  continuum_assert_safe_push "$checkpoint_ref" "$child_repo" || return 4
  # Only called after a successfully created accepted PR, whose own branch
  # contains the accumulated work. A failed cleanup must not invalidate the PR.
  git push origin --delete "$checkpoint_ref" >/dev/null 2>&1 || true
}
