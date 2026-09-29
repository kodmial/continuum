#!/usr/bin/env python3
"""Verify the exact OpenCode bytes used at each invocation.

The trusted install step publishes the measured SHA-256 through GITHUB_OUTPUT.
Later steps receive that runner-owned step output explicitly and re-hash the
executable immediately before starting it. GITHUB_STATE is intentionally not
used because it is for an action's pre/main/post lifecycle, not general
cross-step persistence.
"""
from __future__ import annotations
import argparse, hashlib, os, re, shutil, sys

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 1024 * 1024
OUTPUT_KEY = "runtime_sha256"

class VerificationError(Exception):
    pass

def digest_file(path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()

def resolve_opencode(name="opencode") -> str:
    path = shutil.which(name)
    if not path:
        raise VerificationError("No executable named {!r} is reachable on PATH.".format(name))
    if not os.access(path, os.X_OK):
        raise VerificationError("{} is not executable.".format(path))
    return path

def checked_digest(value, label) -> str:
    value = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(value):
        raise VerificationError("{} {!r} is not a full SHA-256.".format(label, value))
    return value

def write_output(value) -> None:
    destination = os.environ.get("GITHUB_OUTPUT")
    if not destination:
        raise VerificationError(
            "GITHUB_OUTPUT is not set, so the measured identity cannot be "
            "carried to the enforcing step."
        )
    with open(destination, "a", encoding="utf-8") as f:
        f.write("{}={}\n".format(OUTPUT_KEY, value))
        f.flush()
        os.fsync(f.fileno())

def install(args) -> int:
    path = resolve_opencode(args.name)
    actual = digest_file(path)
    if args.verified_sha256:
        expected = checked_digest(args.verified_sha256, "Verified digest")
        if actual != expected:
            raise VerificationError(
                "Resolved executable {} has digest {}, but release verification "
                "approved {}; refusing a shadowed or replaced binary.".format(
                    path, actual, expected
                )
            )
    write_output(actual)
    print("agent-runtime measured {} (sha256 {})".format(os.path.realpath(path), actual), file=sys.stderr)
    return 0

def verify(args) -> int:
    expected = checked_digest(args.expected_sha256, "Expected runtime digest")
    path = resolve_opencode(args.name)
    actual = digest_file(path)
    if actual != expected:
        raise VerificationError(
            "Refusing to run {}: {} has digest {}, but the trusted install step "
            "recorded {}. The executable changed or PATH resolves elsewhere."
            .format(args.name, path, actual, expected)
        )
    print("agent-runtime verified {} (sha256 {})".format(path, actual), file=sys.stderr)
    return 0

def build_parser():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("install")
    i.add_argument("--name", default="opencode")
    i.add_argument("--verified-sha256", default="")
    i.set_defaults(func=install)
    v = sub.add_parser("verify")
    v.add_argument("--name", default="opencode")
    v.add_argument("--expected-sha256", required=True)
    v.set_defaults(func=verify)
    return p

def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except VerificationError as exc:
        print("::error::{}".format(" ".join(str(exc).split())), file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
