#!/usr/bin/env bash
# Durable, private, generation-scoped work checkpoints for delegated tasks.
# Sourced by the reusable worker after the authoritative task snapshot is verified.
# Never upload code, patches or agent transcripts to the public parent.

continuum_checkpoint_ref() {
  local task="$1" generation="$2" spec="$3"
  [[ "$task" =~ ^[1-9][0-9]*$ ]] || return 2
  [[ "$generation" =~ ^[1-9][0-9]*$ ]] || return 2
  [[ "$spec" =~ ^[0-9a-f]{64}$ ]] || return 2
  printf 'continuum-child/checkpoint-task-%s-g%s-%s\n' "$task" "$generation" "$spec"
}

# A remote lookup failure is NOT evidence of a missing checkpoint: fail closed,
# rather than discarding work after rate limits or a transport interruption.
continuum_checkpoint_restore() {
  local checkpoint_ref="$1" branch="$2" spec="$3"
  local remote_line
  CONTINUUM_CHECKPOINT_RESTORED=false
  if ! remote_line="$(git ls-remote --heads origin "refs/heads/$checkpoint_ref" 2>/dev/null)"; then
    echo "::error::Delegated checkpoint lookup unavailable; refusing a fresh start." >&2
    return 4
  fi
  if [[ -z "$remote_line" ]]; then
    git switch -c "$branch" origin/main >/dev/null 2>&1 || return 4
    return 0
  fi

  git fetch --quiet origin "refs/heads/$checkpoint_ref" >/dev/null 2>&1 || return 4
  if ! git log -1 --format=%B FETCH_HEAD | grep -Fxq "Continuum-Checkpoint: $spec"; then
    echo "::error::Delegated checkpoint has no matching frozen task specification." >&2
    return 4
  fi
  if ! git merge-base FETCH_HEAD origin/main >/dev/null; then
    echo "::error::Delegated checkpoint history is unrelated to the target default branch." >&2
    return 4
  fi
  git switch -c "$branch" FETCH_HEAD >/dev/null 2>&1 || return 4
  CONTINUUM_CHECKPOINT_RESTORED=true
  echo "::notice::Resumed verified generation-scoped delegated work checkpoint."
}

# The worker, not the agent, owns all Git mutations. The caller supplies its
# canonical non-default-branch guard and only invokes this after snapshot admission.
continuum_checkpoint_save() {
  local checkpoint_ref="$1" spec="$2" child_repo="$3" task="$4"
  local attempt=0
  if [[ -z "$(git status --porcelain)" ]]; then
    return 0
  fi
  git add -A || return 4
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
    if ! git log -1 --format=%B FETCH_HEAD | grep -Fxq "Continuum-Checkpoint: $spec"; then
      echo "::error::Delegated checkpoint changed to an untrusted revision; refusing to overwrite." >&2
      return 4
    fi
    if ! git merge-base FETCH_HEAD origin/main >/dev/null; then
      echo "::error::Delegated checkpoint history is unrelated to the target default branch." >&2
      return 4
    fi
    if ! git rebase FETCH_HEAD >/dev/null 2>&1; then
      git rebase --abort >/dev/null 2>&1 || true
      echo "::error::Delegated checkpoint update conflicts with concurrent work; refusing to overwrite." >&2
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
