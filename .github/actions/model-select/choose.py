#!/usr/bin/env python3
"""Single-route selection before coding/review side effects (Work Lock #66)."""
import argparse
import json
import os
from pathlib import Path
import sys
from selector import Config, ConfigError, GitHubState, NoRoute, blank_state, select, state_key, state_path

HERE = Path(__file__).resolve().parent


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["choose"])
    parser.parse_args()
    settings = json.loads((HERE / "models.json").read_text(encoding="utf-8"))
    approved = settings["approved_models"]
    requested = os.environ.get("INPUT_OPENCODE_MODELS", "").strip()
    pool = requested or ",".join(settings["default_order"])
    pin = os.environ.get("INPUT_OPENCODE_MODEL", "").strip()
    config = Config.from_values(
        pool, pin,
        ttl=os.environ.get("INPUT_PROBE_TTL", ""),
        cooldown=os.environ.get("INPUT_MODEL_COOLDOWN", ""),
        timeout=os.environ.get("INPUT_PROBE_TIMEOUT", ""),
    )
    if any(model not in approved for model in config.models):
        raise ConfigError("Model is not in the explicitly user-approved model allowlist")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("INPUT_STATE_TOKEN", "")
    profile = os.environ.get("INPUT_MODEL_PROFILE", "coder")
    identity = os.environ.get("INPUT_MODEL_IDENTITY", "anonymous")
    if not profile or "/" in profile or not repo or not token:
        raise ConfigError("A scoped repo, profile and authorized state token are required")
    key = state_key(repo=repo, profile=profile, identity_scope=identity, config=config)
    path = state_path(key)
    store = GitHubState(repo, token)
    previous, expected_head, _ = store.read(path)
    if previous and previous.get("schema") != 1:
        raise ConfigError("Unknown durable model state schema")
    try:
        model, state, attempts = select(config, previous or blank_state(config))
    except NoRoute:
        print("::error::No approved OpenCode model passed live semantic qualification.", file=sys.stderr)
        return 1
    # If this can't be persisted, fail before invoking the stateful coding agent.
    store.write(path, state, expected_head=expected_head)
    output = os.environ.get("GITHUB_OUTPUT", "")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"model={model}\n")
            handle.write(f"source={'explicit-pin' if config.explicit_pin else 'ordered-sticky-pool'}\n")
    print("Continuum approved route ready: " + model)
    for attempt in attempts:
        print("Model preflight " + attempt["model"] + ": " +
              ("PASS" if attempt["ok"] else "FAILED") +
              " reason=" + attempt["reason"] + " seconds=" + str(attempt["seconds"]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (ConfigError, RuntimeError, OSError, ValueError) as exc:
        print("::error::OpenCode model selection failed: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
