#!/usr/bin/env python3
"""Record *successful real task execution* separately from a synthetic probe."""
import json
import os
from pathlib import Path
import sys
from selector import (Config, ConfigError, GitHubState, state_key,
                      state_path, utc_now)

HERE = Path(__file__).resolve().parent

def main():
    approved = json.loads((HERE/"models.json").read_text())["approved_models"]
    raw = os.environ.get("INPUT_OPENCODE_MODELS") or ",".join(approved)
    config = Config.from_values(raw)
    if any(m not in approved for m in config.models):
        raise ConfigError("Unapproved model in execution state")
    model = os.environ.get("INPUT_SELECTED_MODEL", "")
    if model not in config.models:
        raise ConfigError("Recorded task model is not in the approved pool")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    profile = os.environ.get("INPUT_MODEL_PROFILE", "")
    identity = os.environ.get("INPUT_MODEL_IDENTITY", "anonymous")
    store = GitHubState(repo, os.environ.get("INPUT_STATE_TOKEN", ""))
    path = state_path(state_key(repo=repo, profile=profile,
                                identity_scope=identity, config=config))
    state, expected, _ = store.read(path)
    if not state or state.get("fingerprint") != config.fingerprint:
        raise ConfigError("No exact matching model selection state; refusing optimistic promotion")
    if state.get("last_probe_model") != model:
        raise ConfigError("Current model was not qualified at this execution checkpoint")
    state = dict(state)
    state.update(last_good=model, last_execution=utc_now())
    store.write(path, state, expected_head=expected)
    print("Continuum selected model recorded after successful real operation: " + model)

if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError) as e:
        print("::error::Cannot record verified model outcome: "+str(e),file=sys.stderr)
        raise SystemExit(2)
