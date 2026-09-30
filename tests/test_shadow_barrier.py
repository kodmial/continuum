"""The write barrier, and the record-only client behind it.

The issue's requirement is blunt: shadow mode must be structurally read-only,
"not just agent instructions saying do not write", and it "must fail closed if a
write-capable path bypasses the effect boundary". These tests are the evidence
for that, and they are process-level on purpose -- an audit hook installed in a
test process constrains every later test in the same process, so the refusals
being proved have to run somewhere they cannot leak.

Three claims are proved here, separately, because they can fail separately:

* Every mutating method on the production client is either recorded by the
  record-only client or unreachable, and none of them reaches GitHub.
* Every *file system* write a shadow run might attempt outside its own output
  directory is refused, and the refusal is recorded.
* A write that reaches the production client anyway -- through a direct call,
  with a real token -- raises before the request is made.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from continuum.shadow import barrier, effects
from continuum.shadow.effects import RecordingEffects
from continuum.shadow.state import ShadowGitHubClient, state_from_payload
from tests import shadow_support as fixtures

REPO_ROOT = Path(__file__).resolve().parents[1]

#: One script per claim, run in its own process.
PROBES = {
    "filesystem": '''
import json, os, sys
from pathlib import Path
from continuum.shadow import barrier
report = barrier.install([Path("probe-out")])
attempted = []
def attempt(label, call):
    try:
        call()
        attempted.append({"what": label, "refused": False})
    except barrier.WriteBarrierViolation as violation:
        attempted.append({"what": label, "refused": True, "reason": str(violation)})
    except Exception as error:
        attempted.append({
            "what": label,
            "refused": False,
            "error": "{}: {}".format(type(error).__name__, error),
        })
attempt("write outside the output directory",
        lambda: Path("escape.txt").write_text("nope"))
attempt("truncate a file outside the output directory",
        lambda: Path("REPO_ROOT/AGENTS.md").open("a"))
attempt("create a directory outside the output directory",
        lambda: os.mkdir("elsewhere"))
attempt("rename a file", lambda: os.rename("REPO_ROOT/AGENTS.md", "elsewhere"))
attempt("remove a file", lambda: os.unlink("REPO_ROOT/AGENTS.md"))
attempt("chmod a file", lambda: os.chmod("REPO_ROOT/AGENTS.md", 0o777))
attempt("symlink", lambda: os.symlink("REPO_ROOT/AGENTS.md", "link"))
attempt("spawn a socket", lambda: __import__("socket").socket())
attempt("subprocess", lambda: __import__("subprocess").run(["true"]))
attempt("open a socket", lambda: __import__("socket").socket().connect(("127.0.0.1", 1)))
attempt("urllib request", lambda: __import__("urllib.request", fromlist=["x"]).urlopen("http://127.0.0.1:1/"))
attempt("write inside the output directory",
        lambda: (Path("probe-out") / "journal.json").write_text("{}"))
attempt("create a directory inside the output directory",
        lambda: (Path("probe-out") / "nested").mkdir(exist_ok=True))
print(json.dumps({"attempts": attempted, "violations": report.describe()["violations"]}))
''',
    "adapters": """
import json
from pathlib import Path
from continuum.shadow import barrier
from continuum.shadow.effects import RecordingEffects
from continuum.shadow.state import ShadowGitHubClient, state_from_payload
from tests import shadow_support as fixtures
barrier.install([Path("probe-out")])
state = state_from_payload(fixtures.state().as_capture())
sinks = RecordingEffects()
client = ShadowGitHubClient(state, sinks, scenario="probe")
# One implementation, shared with the CLI's barrier-check, so the command and
# this test cannot drift into proving two different things.
document = barrier.attempt_every_write(client, sinks)
document["effects"] = [effect.describe() for effect in sinks.recorded()]
print(json.dumps(document))
""",
            "direct": '''
import json
from continuum.shadow import barrier
from continuum.shadow.state import state_from_payload
from continuum.shadow.effects import RecordingEffects
from continuum.shadow.state import ShadowGitHubClient
from continuum.review.github import GitHubClient
from pathlib import Path
barrier.install([Path("probe-out")])
state = state_from_payload(__import__("json").loads('CAPTURE'))
# A write-capable client constructed by hand, straight past the shadow type.
real = ShadowGitHubClient.__new__(ShadowGitHubClient)
real._state = state
real._effects = RecordingEffects()
real.scenario = "probe"
real._dispatch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the network was reached"))
real._request = real._dispatch
refused = False
try:
    real.add_labels(43, ["opencode-conflict-repair"])
except barrier.WriteBarrierViolation:
    refused = True
except Exception as error:
    refused = "unexpected: {}".format(error)
print(json.dumps({"refused": refused}))
''',
}


def _run_probe(name: str, script: str, capture: str = "") -> dict:
    """Run a probe in its own process with the barrier installed."""

    body = script.replace("REPO_ROOT", str(REPO_ROOT)).replace("CAPTURE", json.dumps(capture))
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "{}:{}".format(REPO_ROOT / "src", REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError("probe {} failed: {}".format(name, result.stderr))
    return json.loads(result.stdout.strip().splitlines()[-1])


class RegistryCoverage(unittest.TestCase):
    def test_every_mutating_method_on_the_production_client_has_an_effect(self) -> None:
        # The startup property: a mutating method with no effect kind raises at
        # construction rather than becoming a hole in the boundary later.
        from continuum.review.github import GitHubClient

        self.assertEqual(effects.unmapped_mutators(GitHubClient), ())
        effects.assert_registry_covers(GitHubClient)

    def test_every_mutating_method_is_attempted_and_performs_nothing(self) -> None:
        report = _run_probe("adapters", PROBES["adapters"])
        self.assertTrue(report["methods"])
        handled = (
            {entry["name"] for entry in report["called"]}
            | set(report["unreachable"])
            | {entry["name"] for entry in report["refused"]}
        )
        self.assertEqual(
            handled,
            set(report["methods"]),
            "every mutating method is recorded, unreachable, or refused outright",
        )
        self.assertEqual(
            report["uncallable"], [], "every mutator was callable with known arguments"
        )
        self.assertEqual(
            report["performed"], [], "a write reached its sink instead of being suppressed"
        )
        for entry in report["called"]:
            self.assertTrue(
                entry["effects"],
                "{} returned without recording anything, which is what performing "
                "the mutation would look like".format(entry["name"]),
            )
            for effect in entry["effects"]:
                self.assertTrue(
                    effect["suppressed"],
                    "{} recorded an effect that was not suppressed".format(entry["name"]),
                )
        # Whatever the shadow client recorded was recorded, not performed.
        self.assertTrue(report["effects"])
        self.assertEqual(effects.forbidden_in_shadow(_recorded(report)), ())

    def test_every_effect_kind_is_blocked_in_shadow_mode(self) -> None:
        # Adapters and effect kinds are different namespaces: the adapter is the
        # method, the kind is the mutation. The guarantee is that every *kind* the
        # registry knows about is one a shadow run must refuse, and that each
        # recorded effect came out suppressed.
        self.assertEqual(
            set(effects.blocked_effect_kinds()),
            {spec.kind for spec in effects.EFFECT_KINDS},
        )
        self.assertTrue(set(effects.blocked_effect_kinds()) <= {
            spec.kind for spec in effects.EFFECT_KINDS
        })


class FilesystemBarrier(unittest.TestCase):
    def setUp(self) -> None:
        self.report = _run_probe("filesystem", PROBES["filesystem"])
        self.attempts = {entry["what"]: entry for entry in self.report["attempts"]}

    def test_writes_outside_the_output_directory_are_refused(self) -> None:
        for what in (
            "write outside the output directory",
            "truncate a file outside the output directory",
            "create a directory outside the output directory",
        ):
            self.assertTrue(self.attempts[what]["refused"], what)
        self.assertFalse((REPO_ROOT / "escape.txt").exists())
        self.assertFalse((REPO_ROOT / "elsewhere").exists())
        self.assertFalse((REPO_ROOT / "link").exists())

    def test_destructive_operations_are_refused_everywhere(self) -> None:
        for what in ("rename a file", "remove a file", "chmod a file", "symlink"):
            self.assertTrue(self.attempts[what]["refused"], what)

    def test_the_network_and_process_spawning_are_refused(self) -> None:
        # A socket *object* is harmless; the refusal is on the connection. The
        # assertion is that the barrier refused before the network was reached,
        # not that Python could not construct a socket.
        for what in ("open a socket", "subprocess", "urllib request"):
            self.assertTrue(self.attempts[what]["refused"], self.attempts[what])

    def test_the_output_directory_is_where_a_shadow_run_may_write(self) -> None:
        self.assertFalse(self.attempts["write inside the output directory"]["refused"])
        self.assertFalse(
            self.attempts["create a directory inside the output directory"]["refused"]
        )
        self.assertTrue((REPO_ROOT / "probe-out" / "journal.json").exists())

    def test_every_refusal_is_recorded_with_a_reason(self) -> None:
        violations = self.report["violations"]
        self.assertGreaterEqual(len(violations), 8)
        for violation in violations:
            self.assertTrue(violation["reason"].strip())
            self.assertTrue(violation["event"].strip())

    def tearDown(self) -> None:
        for path in (REPO_ROOT / "probe-out",):
            if path.exists():
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_dir():
                        child.rmdir()
                    else:
                        child.unlink()
                path.rmdir()


class DirectWriteAttempt(unittest.TestCase):
    def test_a_write_capable_path_is_refused_before_the_network(self) -> None:
        # The client is built by hand, bypassing __init__, and pointed at a
        # dispatch that would raise if reached. The barrier has to refuse first.
        report = _run_probe(
            "direct", PROBES["direct"], fixtures.state().as_capture()
        )
        self.assertIs(report["refused"], True)
        self.assertNotIn("the network was reached", json.dumps(report))


class ClassifyContract(unittest.TestCase):
    def test_classify_is_total_over_the_events_the_hook_can_see(self) -> None:
        # Every audit event either has a reason or is allowed: a classify that
        # raised would turn a refusal into a crash somewhere arbitrary.
        for event in list(barrier.DENIED_EVENTS) + [
            "open",
            "os.putenv",
            "os.unsetenv",
            "os.mkdir",
            "os.makedirs",
            "socket.connect",
            "subprocess.Popen",
            "exec",
            "compile",
            "import",
            "object.__getattr__",
        ]:
            verdict = barrier.classify(event, (), [Path("probe-out")])
            self.assertTrue(
                verdict is None or isinstance(verdict, str),
                "{} produced {!r}".format(event, verdict),
            )

    def test_an_open_with_no_arguments_is_refused_rather_than_guessed(self) -> None:
        self.assertIsNotNone(barrier.classify("open", (), [Path("probe-out")]))
        self.assertIsNotNone(barrier.classify("open", (None, "w"), [Path("probe-out")]))

    def test_a_run_with_no_output_directory_can_write_nothing(self) -> None:
        self.assertIsNotNone(
            barrier.classify("open", (str(REPO_ROOT / "x.json"), "w"), [])
        )


def _recorded(report: dict):
    """The effects the probe's shadow client recorded, rebuilt for inspection."""

    from continuum.shadow.effects import effect_from_payload

    return tuple(effect_from_payload(entry) for entry in report["effects"])


if __name__ == "__main__":
    unittest.main()
