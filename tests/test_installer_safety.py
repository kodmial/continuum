import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
STUB_ROOT = ROOT / ".github" / "caller-stubs"
CORE_STUBS = sorted(
    path for path in STUB_ROOT.glob("*.yml")
    if path.name != "continuum-opencode-unresolved.yml"
)
CORE_NAMES = [path.name for path in CORE_STUBS]


def consumed_caller(name, ref="main"):
    return (
        f"name: {name}\n"
        "on:\n"
        "  workflow_call:\n"
        "    inputs: {}\n"
        "jobs:\n"
        "  call:\n"
        f"    uses: kodmial/continuum/.github/workflows/{name}@{ref}\n"
        "    secrets: inherit\n"
    )


def pin_continuum_refs(text, ref):
    text = re.sub(
        r'(uses:\s*kodmial/continuum/\.github/workflows/[^@\s"\']*)@main',
        rf"\1@{ref}",
        text,
    )
    return re.sub(
        r"((?:continuum_ref|engine_ref):\s*)main",
        rf'\1"{ref}"',
        text,
    )


def snapshot(root):
    result = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            result[rel] = ("symlink", os.readlink(path))
        elif path.is_dir():
            result[rel] = ("dir",)
        else:
            info = path.stat()
            result[rel] = (
                "file",
                path.read_bytes(),
                stat.S_IMODE(info.st_mode),
                info.st_mtime_ns,
            )
    return result


class InstallerSafetyTest(unittest.TestCase):
    def fixture(self):
        parent = ROOT / ".opencode-tmp"
        parent.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="installer-safety-", dir=parent)
        self.addCleanup(directory.cleanup)
        self.addCleanup(
            lambda: parent.rmdir() if parent.exists() and not any(parent.iterdir()) else None
        )
        return Path(directory.name)

    def run_install(self, args, env=None, cwd=None):
        process_env = os.environ.copy()
        process_env.update(env or {})
        completed = subprocess.run(
            ["bash", str(INSTALL), *map(str, args)],
            cwd=cwd,
            env=process_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        return completed.returncode, completed.stdout

    def fake_curl(self, directory, overrides=None, fail_at=None):
        bin_dir = directory / "bin"
        overrides_dir = directory / "curl-overrides"
        bin_dir.mkdir(parents=True, exist_ok=True)
        overrides_dir.mkdir(parents=True, exist_ok=True)
        for name, body in (overrides or {}).items():
            (overrides_dir / name).write_text(body, encoding="utf-8")
        count_file = directory / "curl-count"
        script = "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -eu",
                'name="${@: -1}"',
                'name="${name##*/}"',
                f"count_file={shlex.quote(str(count_file))}",
                'count=0',
                'if [ -f "$count_file" ]; then count=$(cat "$count_file"); fi',
                "count=$((count + 1))",
                'printf \'%s\\n\' "$count" > "$count_file"',
                f"if [ {fail_at or 0} -gt 0 ] && [ \"$count\" -eq {fail_at or 0} ]; then",
                '  echo "injected curl failure for $name" >&2',
                "  exit 22",
                "fi",
                'if [ -f "$CURL_OVERRIDES/$name" ]; then',
                '  cat "$CURL_OVERRIDES/$name"',
                "  exit 0",
                "fi",
                'for dir in "$CURL_STUB_DIR" "$CURL_STUB_DIR/tech" "$CURL_STUB_DIR/parent"; do',
                '  if [ -f "$dir/$name" ]; then cat "$dir/$name"; exit 0; fi',
                "done",
                "exit 22",
                "",
            ]
        )
        curl = bin_dir / "curl"
        curl.write_text(script, encoding="utf-8")
        curl.chmod(0o755)
        return {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CURL_OVERRIDES": str(overrides_dir),
            "CURL_STUB_DIR": str(STUB_ROOT),
        }

    def fake_command(self, directory, command, fail_at):
        real = shutil.which(command)
        self.assertIsNotNone(real, f"{command} must exist for the test")
        bin_dir = directory / f"{command}-bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        count_file = directory / f"{command}-count"
        script = "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -eu",
                f"count_file={shlex.quote(str(count_file))}",
                'count=0',
                'if [ -f "$count_file" ]; then count=$(cat "$count_file"); fi',
                "count=$((count + 1))",
                'printf \'%s\\n\' "$count" > "$count_file"',
                f'if [ "$count" -eq {fail_at} ]; then',
                f'  echo "injected {command} failure" >&2',
                "  exit 1",
                "fi",
                f'exec {shlex.quote(real)} "$@"',
                "",
            ]
        )
        wrapper = bin_dir / command
        wrapper.write_text(script, encoding="utf-8")
        wrapper.chmod(0o755)
        return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    def test_dry_run_lists_the_exact_plan_without_touching_the_target(self):
        directory = self.fixture()
        target = directory / "consumer"
        workflows = target / ".github" / "workflows"
        workflows.mkdir(parents=True)

        keep_body = (STUB_ROOT / "continuum-add-review-label.yml").read_text(encoding="utf-8")
        (workflows / "continuum-add-review-label.yml").write_text(keep_body, encoding="utf-8")
        (workflows / "continuum-auto-merge.yml").write_text(
            consumed_caller("continuum-auto-merge.yml", "v0.1.0"), encoding="utf-8"
        )
        (workflows / "continuum-removed-caller.yml").write_text(
            consumed_caller("continuum-removed-caller.yml"), encoding="utf-8"
        )
        before = snapshot(target)

        status, output = self.run_install([target, "--dry-run"])

        self.assertEqual(status, 0, output)
        self.assertIn("add: continuum-bootstrap-runtime-secret.yml", output)
        self.assertIn("update: continuum-auto-merge.yml", output)
        self.assertIn("keep: continuum-add-review-label.yml", output)
        self.assertIn("prune: continuum-removed-caller.yml", output)
        self.assertIn("dry-run: no files were changed", output)
        self.assertEqual(before, snapshot(target), "a preview must not create, delete, or modify target files")

    def test_remote_ref_preview_declares_the_network_requirement(self):
        directory = self.fixture()
        target = directory / "consumer"
        env = self.fake_curl(directory)

        status, output = self.run_install([target, "v9.9.9", "core", "--dry-run"], env)

        self.assertEqual(status, 0, output)
        self.assertIn("network access required", output)
        self.assertIn("no credentials are used", output)
        self.assertFalse(target.exists(), "a preview must not create the consumer tree")

    def test_failed_preflight_fetch_makes_no_changes(self):
        directory = self.fixture()
        target = directory / "consumer"
        workflows = target / ".github" / "workflows"
        workflows.mkdir(parents=True)
        sentinel = workflows / "ci.yml"
        sentinel.write_text("name: CI\n", encoding="utf-8")
        stale = workflows / "continuum-removed-caller.yml"
        stale.write_text(consumed_caller("continuum-removed-caller.yml"), encoding="utf-8")
        before = snapshot(target)
        env = self.fake_curl(directory, fail_at=5)

        status, output = self.run_install([target, "v9.9.9", "core"], env)

        self.assertNotEqual(status, 0, output)
        self.assertIn("failed to fetch template", output)
        self.assertEqual(before, snapshot(target), "a failed fetch must happen before any target mutation")

    def test_empty_or_non_caller_template_fails_before_mutation(self):
        cases = {
            "empty": ("continuum-opencode.yml", ""),
            "no jobs": ("continuum-opencode.yml", "name: broken\n"),
            "no continuum call": (
                "continuum-opencode.yml",
                "name: broken\njobs:\n  call:\n    runs-on: ubuntu-latest\n",
            ),
        }
        for label, (name, body) in cases.items():
            with self.subTest(case=label):
                directory = self.fixture()
                target = directory / "consumer"
                workflows = target / ".github" / "workflows"
                workflows.mkdir(parents=True)
                (workflows / "ci.yml").write_text("name: CI\n", encoding="utf-8")
                before = snapshot(target)
                env = self.fake_curl(directory, overrides={name: body})

                status, output = self.run_install([target, "v9.9.9", "core"], env)

                self.assertNotEqual(status, 0, output)
                self.assertIn("invalid template", output)
                self.assertEqual(before, snapshot(target), f"{label}: corrupt template must not mutate the target")

    def test_path_collision_and_unowned_file_are_rejected_without_writes(self):
        for label in ("directory collision", "unowned file"):
            with self.subTest(case=label):
                directory = self.fixture()
                target = directory / "consumer"
                workflows = target / ".github" / "workflows"
                workflows.mkdir(parents=True)
                (workflows / "ci.yml").write_text("name: CI\n", encoding="utf-8")
                collision = workflows / "continuum-opencode.yml"
                if label == "directory collision":
                    collision.mkdir()
                else:
                    collision.write_text(
                        "name: My own workflow\non:\n  workflow_dispatch:\n",
                        encoding="utf-8",
                    )
                before = snapshot(target)

                status, output = self.run_install([target, "--yes"])

                self.assertNotEqual(status, 0, output)
                self.assertIn("ownership-conflict: continuum-opencode.yml", output)
                self.assertIn("refusing to install", output)
                self.assertIn("Move or rename", output)
                self.assertEqual(before, snapshot(target),
                                 "an ownership conflict must fail closed before any write or prune")

    def test_controlled_apply_failure_restores_the_exact_tree_and_retry_converges(self):
        directory = self.fixture()
        target = directory / "consumer"
        workflows = target / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI\n", encoding="utf-8")
        old_update = consumed_caller("continuum-auto-merge.yml", "v0.1.0")
        (workflows / "continuum-auto-merge.yml").write_text(old_update, encoding="utf-8")
        stale = workflows / "continuum-removed-caller.yml"
        stale.write_text(consumed_caller("continuum-removed-caller.yml"), encoding="utf-8")
        before = snapshot(target)

        status, output = self.run_install(
            [target, "--yes"], self.fake_command(directory, "mv", fail_at=3)
        )

        self.assertNotEqual(status, 0, output)
        self.assertIn("failed to install", output)
        self.assertIn("restored the pre-install target files", output)
        self.assertEqual(before, snapshot(target),
                         "a controlled write failure must restore the exact original tree")

        status, output = self.run_install([target, "--yes"])
        self.assertEqual(status, 0, output)
        self.assertFalse(stale.exists(), "retry must converge and prune the superseded caller")
        self.assertEqual(
            (STUB_ROOT / "continuum-auto-merge.yml").read_bytes(),
            (workflows / "continuum-auto-merge.yml").read_bytes(),
            "retry must upgrade the pre-existing Continuum-owned caller",
        )
        for name in CORE_NAMES:
            self.assertTrue((workflows / name).is_file(), f"retry must install {name}")

    def test_controlled_prune_failure_restores_pruned_and_written_files_then_retry_converges(self):
        directory = self.fixture()
        target = directory / "consumer"
        workflows = target / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI\n", encoding="utf-8")
        stale = workflows / "continuum-removed-caller.yml"
        stale.write_text(consumed_caller("continuum-removed-caller.yml"), encoding="utf-8")
        before = snapshot(target)

        status, output = self.run_install(
            [target, "--yes"], self.fake_command(directory, "rm", fail_at=1)
        )

        self.assertNotEqual(status, 0, output)
        self.assertIn("failed to prune: continuum-removed-caller.yml", output)
        self.assertEqual(before, snapshot(target),
                         "a controlled prune failure must restore added, updated, and pruned files")

        status, output = self.run_install([target, "--yes"])
        self.assertEqual(status, 0, output)
        self.assertFalse(stale.exists())
        for name in CORE_NAMES:
            self.assertTrue((workflows / name).is_file(), f"retry must install {name}")

    def test_repeated_install_is_byte_idempotent_and_sets_stay_independent(self):
        directory = self.fixture()
        target = directory / "consumer"
        env = self.fake_curl(directory).copy()
        env["CONTINUUM_INSTALL_ASSUME_YES"] = "1"

        status, output = self.run_install([target, "main", "core"], env)
        self.assertEqual(status, 0, output)
        first = snapshot(target)

        status, output = self.run_install([target, "main", "core"], env)
        self.assertEqual(status, 0, output)
        self.assertEqual(first, snapshot(target), "a same-version reinstall must yield zero byte diff")

        status, output = self.run_install([target, "main", "tech"], env)
        self.assertEqual(status, 0, output)
        after_tech = snapshot(target)
        for name in CORE_NAMES:
            key = f".github/workflows/{name}"
            self.assertEqual(first[key], after_tech[key], f"tech install changed core caller {name}")
        self.assertTrue((target / ".github/workflows/continuum-tech-swift-ci.yml").is_file())

        status, output = self.run_install([target, "main", "parent"], env)
        self.assertEqual(status, 0, output)
        after_parent = snapshot(target)
        for name in CORE_NAMES:
            key = f".github/workflows/{name}"
            self.assertEqual(first[key], after_parent[key], f"parent install changed core caller {name}")
        for name in (
            "continuum-child-pr-review.yml",
            "continuum-child-review.yml",
            "continuum-child-run-cleanup.yml",
            "continuum-child-worker.yml",
        ):
            self.assertTrue((target / ".github" / "workflows" / name).is_file())

    def test_rerun_after_an_interrupted_install_converges(self):
        directory = self.fixture()
        target = directory / "consumer"
        workflows = target / ".github" / "workflows"
        workflows.mkdir(parents=True)
        keep_body = (STUB_ROOT / "continuum-add-review-label.yml").read_text(encoding="utf-8")
        (workflows / "continuum-add-review-label.yml").write_text(keep_body, encoding="utf-8")
        (workflows / "continuum-auto-merge.yml").write_text(
            pin_continuum_refs(
                (STUB_ROOT / "continuum-auto-merge.yml").read_text(encoding="utf-8"),
                "v0.0.1",
            ),
            encoding="utf-8",
        )
        stale = workflows / "continuum-removed-caller.yml"
        stale.write_text(consumed_caller("continuum-removed-caller.yml"), encoding="utf-8")

        status, output = self.run_install([target, "--yes"])

        self.assertEqual(status, 0, output)
        self.assertFalse(stale.exists(), "a re-run must finish the interrupted prune")
        for name in CORE_NAMES:
            self.assertEqual(
                (STUB_ROOT / name).read_bytes(),
                (workflows / name).read_bytes(),
                f"a re-run must converge {name} to the canonical caller",
            )

    def test_old_pinned_continuum_callers_are_recognised_and_upgraded(self):
        directory = self.fixture()
        target = directory / "consumer"
        workflows = target / ".github" / "workflows"
        workflows.mkdir(parents=True)
        canonical = (STUB_ROOT / "continuum-opencode.yml").read_text(encoding="utf-8")
        (workflows / "continuum-opencode.yml").write_text(
            pin_continuum_refs(canonical, "v0.0.1"), encoding="utf-8"
        )

        status, output = self.run_install([target])
        self.assertEqual(status, 0, output)
        self.assertEqual(
            canonical.encode(),
            (workflows / "continuum-opencode.yml").read_bytes(),
            "a differently pinned Continuum-owned caller must remain upgradable",
        )

        env = self.fake_curl(directory)
        for ref in ("v1.2.3", "v1.2.4"):
            status, output = self.run_install([target, ref, "core"], env)
            self.assertEqual(status, 0, output)
            body = (workflows / "continuum-opencode.yml").read_text(encoding="utf-8")
            self.assertIn(f"continuum-opencode.yml@{ref}", body)
            self.assertIn(f'continuum_ref: "{ref}"', body)
            self.assertNotIn("@main", body)


if __name__ == "__main__":
    unittest.main()
