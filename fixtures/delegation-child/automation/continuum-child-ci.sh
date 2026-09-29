#!/usr/bin/env bash
set -euo pipefail

# Project-owned deterministic validation. The parent executes this exact script
# from the child base branch against the candidate PR worktree, with a minimal
# environment that contains no GitHub or child-runtime credentials.
python3 -m pytest -q
