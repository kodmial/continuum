"""Coverage for the duplicated prepared-runtime probe logic.

The digest regex, stamp comparison, version grep, warning fallback, and
warm-path short-circuit are duplicated across every agent-runtime install
site. A typo in one copy, or a version-output format drift that breaks the
grep, would otherwise surface only as failed or silently cold agent jobs.
These tests execute each site's own shell (extracted verbatim from the
workflow file) in a sandbox for the hit, miss, and malformed-digest cases,
and statically pin the per-site wiring so no copy can drift.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORKFLOWS_DIR = os.path.join(ROOT, ".github", "workflows")

# Every agent-runtime install site: file -> one (tool, version) entry per
# installer line, in file order. This map locks the duplication surface: a
# new install site without probe coverage fails here until it is added.
EXPECTED_SITES = {
    "continuum-opencode.yml": [("opencode", "1.18.34"), ("opencode", "1.18.34")],
    "continuum-pr-agent.yml": [("pr-agent", "0.46.0"), ("opencode", "1.18.34")],
    "continuum-pr-agent-repair.yml": [("opencode", "1.18.34")],
    "continuum-coderabbit-unresolved.yml": [("opencode", "1.18.34")],
    "continuum-consumer-child-worker.yml": [("opencode", "1.18.34")],
    "continuum-consumer-child-review.yml": [("opencode", "1.18.34")],
    "continuum-consumer-child-pr-review.yml": [("opencode", "1.18.34")],
}

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def read_workflow(name):
    with open(os.path.join(WORKFLOWS_DIR, name), "r", encoding="utf-8") as handle:
        return handle.read()


def code_without_comment(line):
    """Return the code portion of a line with `#` comments stripped.

    Only a `#` outside single/double quotes starts a comment, so a quoted
    `#` (e.g. a URL fragment or `echo "#hi"`) is preserved while a trailing
    comment is not code. Mirrors the Ruby helper and
    `_stamp_code_without_comment`.
    """
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index]
    return line


def _shell_segments(code):
    """Split one shell line into `;`/`&&`/`||`/`|` segments outside quotes.

    A single `run:` line can both echo and execute the installer
    (e.g. `echo downloading; curl https://opencode.ai/install | sh`):
    the whole line must not be skipped just because one segment echoes.
    Splitting respects single/double quotes so `echo "a; b"` stays whole.
    """
    segments = []
    current: list = []
    in_single = False
    in_double = False
    index = 0
    while index < len(code):
        char = code[index]
        if char == "'" and not in_double:
            in_single = not in_single
            current.append(char)
        elif char == '"' and not in_single:
            in_double = not in_double
            current.append(char)
        elif not in_single and not in_double:
            two = code[index:index + 2]
            if char == ";":
                segments.append("".join(current))
                current = []
            elif two in ("&&", "||"):
                segments.append("".join(current))
                current = []
                index += 1
            elif char == "|":
                segments.append("".join(current))
                current = []
            else:
                current.append(char)
        else:
            current.append(char)
        index += 1
    segments.append("".join(current))
    return segments


def _is_echo_segment(segment):
    # A docs example that merely mentions an installer URL (`echo ...` /
    # `printf ...`, optionally via `sudo`) is not an executable site. Only
    # the echo segment itself is ignored; other segments on the same line
    # are still checked so `echo downloading; curl <installer> | sh`
    # counts as a site.
    return re.match(r"^\s*(sudo\s+)?(echo|printf)\b", segment) is not None


def _segment_is_installer(segment):
    if "https://opencode.ai/install" in segment:
        return True
    elif ("pip install" in segment or "pip3 install" in segment) and ("pr-agent" in segment or "opencode" in segment):
        return True
    elif "pipx install" in segment and ("pr-agent" in segment or "opencode" in segment):
        return True
    elif "uv tool install" in segment and ("pr-agent" in segment or "opencode" in segment):
        return True
    elif "uv pip install" in segment and ("pr-agent" in segment or "opencode" in segment):
        return True
    elif "cargo install" in segment and ("pr-agent" in segment or "opencode" in segment):
        return True
    elif ("npm install" in segment or "npm i " in segment or "npm ci" in segment) and "opencode" in segment:
        return True
    elif "brew install" in segment and "opencode" in segment:
        return True
    elif ("curl" in segment or "wget" in segment) and (
        "releases/download" in segment
        or ("github.com" in segment and "releases" in segment)
        or ("opencode" in segment and (".tar.gz" in segment or ".zip" in segment or "download" in segment))
    ):
        return True
    elif "gh release download" in segment and "opencode" in segment:
        return True
    elif "releases/download" in segment and "opencode" in segment:
        return True
    return False


def installer_indices(lines):
    found = []
    for i, line in enumerate(lines):
        code = code_without_comment(line)
        # Check each shell segment separately: an `echo`/`printf` docs
        # example that merely mentions an installer URL is not an
        # executable installer site, but a line that echoes and then runs
        # the installer is. Real sites run the installer in at least one
        # non-echo segment; they never only echo it.
        executable = [seg for seg in _shell_segments(code) if not _is_echo_segment(seg)]
        if not executable:
            continue
        if any(_segment_is_installer(seg) for seg in executable):
            found.append(i)
    return found


def site_bounds(installers):
    bounds = []
    prev = -1
    for at in installers:
        bounds.append((prev + 1, at))
        prev = at
    return bounds


def extract_guard(site_lines):
    """Return the 4-line malformed-digest guard block verbatim."""
    for i, line in enumerate(site_lines):
        if "CONTINUUM_IMAGE_DIGEST is malformed" in line:
            block = site_lines[i - 1:i + 3]
            text = "\n".join(block)
            assert '[[ -n "${CONTINUUM_IMAGE_DIGEST:-}" ]]' in block[0], text
            assert 'CONTINUUM_IMAGE_DIGEST=""' in block[2], text
            assert block[3].strip() == "fi", text
            return block
    raise AssertionError("malformed-digest guard block is missing from the install site")


def extract_condition(site_lines):
    """Return the digest-gated probe test expression verbatim.

    The warm-hit `if` is a single shell line; stripping the leading `if`
    and trailing `; then` yields the exact conjunction the workflow
    evaluates (digest shape, stamp comparison, binary probe, version grep).
    """
    for line in site_lines:
        stripped = line.strip()
        if (
            stripped.startswith("if [[")
            and "CONTINUUM_IMAGE_DIGEST" in line
            and ("command -v" in line or "--version" in line)
            and stripped.endswith("; then")
        ):
            return stripped[len("if "):-len("; then")]
    raise AssertionError("digest-gated warm-hit condition is missing from the install site")


def detect_tool_and_version(condition):
    if "command -v pr-agent" in condition:
        tool = "pr-agent"
    else:
        assert "command -v opencode" in condition, condition
        tool = "opencode"
    match = re.search(r"([0-9]+\\?\.[0-9]+\\?\.[0-9]+)", condition)
    assert match, condition
    return tool, match.group(1).replace("\\", "")


def _nth_warm_hit_if_index(full_lines, condition, occurrence):
    """Return the file index of the site's warm-hit `if` line.

    Identical probe copies share one condition string, so the Nth site
    (in file order) is the Nth occurrence of the `if <condition>; then`
    line.
    """
    wanted = ("if " + condition + "; then").strip()
    seen = 0
    for i, line in enumerate(full_lines):
        if line.strip() == wanted:
            if seen == occurrence:
                return i
            seen += 1
    raise AssertionError("warm-hit condition line not found in workflow file")


def _warm_hit_path_and_tail(full_lines, if_idx):
    """Split the warm-hit `if` into hit-path lines and the shared tail.

    Returns (hit_lines, tail_lines, has_else). `hit_lines` always run on
    a warm hit (the `if` branch). `tail_lines` (the lines right after the
    closing `fi`) run on a hit only for the `if/else` shape, where the
    hit falls through to shared code (consumer-child); for the `exit 0`
    shape the hit short-circuits, so the tail never runs on a hit and a
    cold-path-only export there must not count.
    """
    def _first_word(stripped):
        parts = stripped.split()
        return parts[0] if parts else ""

    def _opens_block(stripped):
        first = _first_word(stripped)
        return (
            first in ("if", "for", "while", "until", "select")
            or stripped.startswith("case ")
            or stripped == "case"
        )

    def _closes_block(stripped):
        first = _first_word(stripped)
        return first in ("fi", "done", "esac")

    depth = 1
    else_idx = None
    close_idx = None
    index = if_idx + 1
    while index < len(full_lines):
        stripped = code_without_comment(full_lines[index]).strip()
        if _closes_block(stripped):
            depth -= 1
            if depth == 0:
                close_idx = index
                break
        elif _opens_block(stripped):
            depth += 1
        elif depth == 1 and (
            stripped == "else"
            or stripped.startswith("else ")
            or stripped.startswith("else;")
            or _first_word(stripped) == "elif"
        ):
            if else_idx is None:
                else_idx = index
        index += 1
    if close_idx is None:
        raise AssertionError("warm-hit `if` starting at line {} is never closed".format(if_idx + 1))
    if else_idx is not None:
        hit_lines = full_lines[if_idx + 1:else_idx]
    else:
        hit_lines = full_lines[if_idx + 1:close_idx]
    tail_lines = full_lines[close_idx + 1:close_idx + 7]
    return hit_lines, tail_lines, else_idx is not None


class PreparedRuntimeProbeTests(unittest.TestCase):
    def sites(self, name):
        """Return one probe region per installer: the malformed-digest guard
        through the installer line, so assertions never bleed into
        unrelated steps earlier in the file."""
        body = read_workflow(name)
        lines = body.splitlines()
        installers = installer_indices(lines)
        self.assertTrue(installers, "{}: no installer site found".format(name))
        regions = []
        for at in installers:
            prev_installer = installers[installers.index(at) - 1] if at != installers[0] else -1
            cond_at = None
            for i in range(at - 1, prev_installer, -1):
                stripped = lines[i].strip()
                if (
                    stripped.startswith("if [[")
                    and "CONTINUUM_IMAGE_DIGEST" in lines[i]
                    and ("command -v" in lines[i] or "--version" in lines[i])
                    and stripped.endswith("; then")
                ):
                    cond_at = i
                    break
            self.assertIsNotNone(
                cond_at,
                "{}: no digest-gated warm-hit condition before installer line {}".format(name, at + 1))
            warn_at = None
            for i in range(cond_at - 1, prev_installer, -1):
                if "CONTINUUM_IMAGE_DIGEST is malformed" in lines[i]:
                    warn_at = i
                    break
            self.assertIsNotNone(
                warn_at,
                "{}: no malformed-digest guard before installer line {}".format(name, at + 1))
            regions.append(lines[warn_at - 1:at + 1])
        return regions

    def test_every_install_site_carries_the_full_probe(self):
        for name, expected in sorted(EXPECTED_SITES.items()):
            with self.subTest(workflow=name):
                sites = self.sites(name)
                self.assertEqual(
                    len(sites), len(expected),
                    "{}: expected {} install site(s), found {}".format(
                        name, len(expected), len(sites)))
                for site_pos, (site_lines, (tool, version)) in enumerate(zip(sites, expected)):
                    with self.subTest(site=tool):
                        text = "\n".join(site_lines)
                        # Digest shape gate on both the malformed guard and
                        # the warm-hit condition.
                        self.assertRegex(
                            text, r"CONTINUUM_IMAGE_DIGEST.*=~\s*\^\[0-9a-f\]\{64\}\$")
                        # Malformed digest warns and falls back, never fails
                        # closed: the guard clears the digest instead of
                        # exiting, so every `exit 1` left in the site is a
                        # post-install version verification (`|| exit 1`).
                        # Any other `exit 1` command form — bare, `; exit 1`,
                        # `&& exit 1`, or single-pipe `| exit 1` — would fail
                        # closed on a recoverable cache failure instead of
                        # warning and falling back.
                        self.assertIn(
                            "::warning::CONTINUUM_IMAGE_DIGEST is malformed", text)
                        self.assertIn('CONTINUUM_IMAGE_DIGEST=""', text)
                        for line in site_lines:
                            code = code_without_comment(line)
                            scrubbed = re.sub(r"\|\|\s*exit\s+1\b", "", code)
                            if re.search(r"(^|[;|&()])\s*exit\s+1\b", scrubbed):
                                self.fail(
                                    "{}: `exit 1` outside `|| exit 1` would fail closed on a "
                                    "cache failure: {}".format(name, line))
                        # Stamp binding: the hit requires the image-digest
                        # stamp to match the digest.
                        condition = extract_condition(site_lines)
                        self.assertIn("STAMP_FILE", condition)
                        self.assertIn("CONTINUUM_IMAGE_DIGEST", condition)
                        # Exact version grep with boundary anchors (plain
                        # substring matching false-hits on "1.18.340").
                        escaped = re.escape(version)
                        self.assertIn(
                            "grep -E -q \"(^|[^0-9.]){}([^0-9.]|$)\"".format(escaped),
                            condition)
                        self.assertIn("command -v {}".format(tool), condition)
                        # Warm hit is logged and short-circuits before any
                        # download (`exit 0`, or `else` on consumer-child).
                        hit_at = next(
                            i for i, line in enumerate(site_lines)
                            if "prepared-runtime hit" in line)
                        short_circuited = any(
                            code_without_comment(line).strip() in ("exit 0", "else")
                            for line in site_lines[hit_at:])
                        self.assertTrue(
                            short_circuited,
                            "{}: warm hit must short-circuit before the download".format(name))
                        # Warm-hit PATH export (opencode sites only: pr-agent
                        # is pip-installed onto PATH already, so its hit
                        # needs no GITHUB_PATH export). The warm-hit path
                        # must place `$HOME/.opencode/bin` on PATH for later
                        # steps via `$GITHUB_PATH`: either directly in the
                        # hit branch, or — for the consumer-child `if/else`
                        # shape — via the shared post-`fi` export that also
                        # runs on a hit. A site with a correct condition
                        # but a missing export would pass the hit/miss
                        # probe yet leave later steps without `opencode` on
                        # PATH on a warm hit. For the `exit 0` shape the
                        # hit short-circuits, so a cold-path-only export
                        # after the closing `fi` must not count.
                        if tool == "opencode":
                            full_lines = read_workflow(name).splitlines()
                            cond = extract_condition(site_lines)
                            occurrence = sum(
                                1 for j in range(site_pos)
                                if extract_condition(sites[j]) == cond
                            )
                            if_idx = _nth_warm_hit_if_index(full_lines, cond, occurrence)
                            hit_lines, tail_lines, has_else = _warm_hit_path_and_tail(
                                full_lines, if_idx)

                            def _is_path_export(line):
                                code = code_without_comment(line)
                                return "GITHUB_PATH" in code and ".opencode/bin" in code

                            hit_exports = any(_is_path_export(line) for line in hit_lines)
                            shared_exports = has_else and any(
                                _is_path_export(line) for line in tail_lines)
                            self.assertTrue(
                                hit_exports or shared_exports,
                                "{}: warm hit must export $HOME/.opencode/bin to $GITHUB_PATH "
                                "on the hit path".format(name))

    def run_probe(self, site_lines, digest, stamp, tool_version):
        """Evaluate the site's own guard + condition in a bash sandbox.

        Returns (marker, output) where marker is PROBE_HIT or PROBE_MISS.
        `digest` None means the variable is unset (empty-digest cold path);
        `stamp` None means no stamp file; `tool_version` None means the
        binary is absent from PATH.
        """
        tmp_root = os.path.join(ROOT, ".opencode-tmp")
        os.makedirs(tmp_root, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="continuum-probe-", dir=tmp_root)
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_dir = os.path.join(tmp, "bin")
        os.mkdir(bin_dir)
        tool, _ = detect_tool_and_version(extract_condition(site_lines))
        if tool_version is not None:
            with open(os.path.join(bin_dir, tool), "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\necho '{}'\n".format(tool_version))
            os.chmod(os.path.join(bin_dir, tool), 0o755)
        stamp_file = os.path.join(tmp, "image-digest")
        if stamp is not None:
            with open(stamp_file, "w", encoding="utf-8") as handle:
                handle.write(stamp)
        guard = extract_guard(site_lines)
        condition = extract_condition(site_lines)
        script = (
            "export PATH=\"{bin}:/usr/bin:/bin\"\n"
            "STAMP_FILE=\"{stamp}\"\n"
            "{unset}{export}"
            "{guard}\n"
            "if {cond}; then echo PROBE_HIT; else echo PROBE_MISS; fi\n"
        ).format(
            bin=bin_dir,
            stamp=stamp_file,
            unset="" if digest is not None else "unset CONTINUUM_IMAGE_DIGEST\n",
            export="" if digest is None else "CONTINUUM_IMAGE_DIGEST=\"{}\"\n".format(digest),
            guard="\n".join(guard),
            cond=condition,
        )
        completed = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        marker = "PROBE_HIT" if "PROBE_HIT" in completed.stdout else "PROBE_MISS"
        return marker, completed.stdout

    def check_all_sites(self, func):
        for name in sorted(EXPECTED_SITES):
            for site_lines in self.sites(name):
                with self.subTest(workflow=name):
                    func(site_lines)

    def test_hit_with_matching_stamp_and_exact_version(self):
        def check(site_lines):
            tool, version = detect_tool_and_version(extract_condition(site_lines))
            marker, _ = self.run_probe(site_lines, DIGEST, DIGEST, version)
            self.assertEqual(marker, "PROBE_HIT")
            # A version prefix around the exact pin still hits.
            marker, _ = self.run_probe(
                site_lines, DIGEST, DIGEST, "{}-release (abc123)".format(version))
            self.assertEqual(marker, "PROBE_HIT")
        self.check_all_sites(check)

    def test_miss_with_empty_digest(self):
        def check(site_lines):
            tool, version = detect_tool_and_version(extract_condition(site_lines))
            marker, _ = self.run_probe(site_lines, None, DIGEST, version)
            self.assertEqual(marker, "PROBE_MISS")
        self.check_all_sites(check)

    def test_malformed_digest_warns_and_misses(self):
        def check(site_lines):
            tool, version = detect_tool_and_version(extract_condition(site_lines))
            marker, output = self.run_probe(
                site_lines, "not-a-digest", DIGEST, version)
            self.assertEqual(marker, "PROBE_MISS")
            self.assertIn("CONTINUUM_IMAGE_DIGEST is malformed", output)
        self.check_all_sites(check)

    def test_miss_with_stale_stamp_or_missing_binary(self):
        def check(site_lines):
            tool, version = detect_tool_and_version(extract_condition(site_lines))
            marker, _ = self.run_probe(site_lines, DIGEST, OTHER_DIGEST, version)
            self.assertEqual(marker, "PROBE_MISS")
            marker, _ = self.run_probe(site_lines, DIGEST, None, version)
            self.assertEqual(marker, "PROBE_MISS")
            marker, _ = self.run_probe(site_lines, DIGEST, DIGEST, None)
            self.assertEqual(marker, "PROBE_MISS")
        self.check_all_sites(check)

    def test_version_drift_misses(self):
        # A typo in one copy's pin, or an upstream output-format drift that
        # appends digits ("1.18.340"), must miss instead of taking the warm
        # path on the wrong runtime.
        def check(site_lines):
            tool, version = detect_tool_and_version(extract_condition(site_lines))
            major, minor, patch = version.split(".")
            drifted = "{}.{}.{}0".format(major, minor, patch)
            marker, _ = self.run_probe(site_lines, DIGEST, DIGEST, drifted)
            self.assertEqual(marker, "PROBE_MISS")
            marker, _ = self.run_probe(site_lines, DIGEST, DIGEST, "9.9.9")
            self.assertEqual(marker, "PROBE_MISS")
        self.check_all_sites(check)


if __name__ == "__main__":
    unittest.main()
