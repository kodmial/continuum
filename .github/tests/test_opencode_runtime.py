#!/usr/bin/env python3
"""Tests for OpenCode point-of-use executable identity."""
import hashlib, os, pathlib, stat, subprocess, sys, tempfile, textwrap, unittest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github" / "scripts"
WORKFLOWS = ROOT / ".github" / "workflows"
sys.path.insert(0, str(SCRIPTS))
import opencode_runtime  # noqa: E402

SCRIPT = SCRIPTS / "opencode_runtime.py"
NAMES = ("opencode.yml", "consumer-opencode.yml")

def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()

def executable(directory, note=""):
    p = pathlib.Path(directory) / "opencode"
    p.write_text("#!/bin/sh\necho fixture-{}\n".format(note), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.output = pathlib.Path(self.t.name) / "output"
        self.output.write_text("", encoding="utf-8")

    def runit(self, *args, path, output=True):
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(path), os.environ["PATH"]])
        if output:
            env["GITHUB_OUTPUT"] = str(self.output)
        else:
            env.pop("GITHUB_OUTPUT", None)
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True)

    def recorded(self):
        prefix = opencode_runtime.OUTPUT_KEY + "="
        for line in self.output.read_text(encoding="utf-8").splitlines():
            if line.startswith(prefix):
                return line[len(prefix):]
        return ""

    def test_install_publishes_digest(self):
        with tempfile.TemporaryDirectory() as d:
            b = executable(d)
            r = self.runit("install", path=d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.recorded(), digest(b))

    def test_install_checks_verified_digest(self):
        with tempfile.TemporaryDirectory() as d:
            b = executable(d)
            r = self.runit("install", "--verified-sha256", digest(b), path=d)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_install_rejects_shadow(self):
        with tempfile.TemporaryDirectory() as good, tempfile.TemporaryDirectory() as shadow:
            b = executable(good, "good")
            executable(shadow, "shadow")
            r = self.runit("install", "--verified-sha256", digest(b),
                           path=os.pathsep.join([shadow, good]))
            self.assertEqual(r.returncode, 1)
            self.assertIn("shadowed or replaced", r.stderr)

    def test_install_requires_output_channel(self):
        with tempfile.TemporaryDirectory() as d:
            executable(d)
            r = self.runit("install", path=d, output=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("GITHUB_OUTPUT is not set", r.stderr)

    def test_verify_accepts_same_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            b = executable(d)
            r = self.runit("verify", "--expected-sha256", digest(b), path=d)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_verify_rejects_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            b = executable(d, "before")
            expected = digest(b)
            executable(d, "after")
            r = self.runit("verify", "--expected-sha256", expected, path=d)
            self.assertEqual(r.returncode, 1)
            self.assertIn("changed or PATH resolves elsewhere", r.stderr)

    def test_verify_rejects_malformed_expected_digest(self):
        with tempfile.TemporaryDirectory() as d:
            executable(d)
            r = self.runit("verify", "--expected-sha256", "bad", path=d)
            self.assertEqual(r.returncode, 1)

def steps(name):
    doc = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    for job in (doc.get("jobs") or {}).values():
        yield from (job.get("steps") or [])

class WorkflowTests(unittest.TestCase):
    def test_install_steps_are_named_output_producers(self):
        for name in NAMES:
            found = [s for s in steps(name) if "opencode_runtime.py install" in (s.get("run") or "")]
            self.assertTrue(found, name)
            for step in found:
                self.assertTrue(step.get("id"), "{} {}".format(name, step.get("name")))

    def test_every_plain_invocation_uses_prior_step_output(self):
        for name in NAMES:
            for step in steps(name):
                run = step.get("run") or ""
                if "opencode run" not in run:
                    continue
                identity = str((step.get("env") or {}).get("OPENCODE_RUNTIME_SHA256") or "")
                self.assertIn("steps.", identity)
                self.assertIn("outputs.runtime_sha256", identity)
                guard = 'opencode_runtime.py verify --expected-sha256 "$OPENCODE_RUNTIME_SHA256"'
                self.assertIn(guard, run)
                self.assertLess(run.index(guard), run.index("opencode run"))
                self.assertNotRegex(run, r"timeout[^\n]*\\\s*\n\s*#")

    def test_github_state_is_not_used_for_cross_step_identity(self):
        self.assertNotIn('os.environ.get("GITHUB_STATE")', SCRIPT.read_text(encoding="utf-8"))
        for name in NAMES:
            self.assertNotIn("GITHUB_STATE", (WORKFLOWS / name).read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
