"""Regression: recovery uses authenticated admitted issue routes, not quoted markers."""

from __future__ import annotations

import unittest
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/continuum-opencode.yml"



class SchedulerRecoveryOriginContractTests(unittest.TestCase):
    def test_recovery_admission_is_not_a_marker_substring_gate(self) -> None:
        body = WORKFLOW.read_text(encoding="utf-8")
        header = body.split("  recover-scheduled-issue:", 1)[1].split("    runs-on:", 1)[0]
        assert "needs: [admit, opencode]" in header
        assert "needs.admit.result == 'success'" in header
        assert "needs.admit.outputs.route == 'issue'" in header
        assert "inputs.mode == 'issue'" in header
        assert "contains(github.event.comment.body" not in header


    def test_scheduler_attempts_are_canonical_and_not_quoted_markers(self) -> None:
        body = WORKFLOW.read_text(encoding="utf-8")
        start = "            function isCanonicalSchedulerDispatch(comment) {"
        end = "            // Local consumers may still derive pause_on_failure from public"
        assert start in body and end in body
        snippet = body.split(start, 1)[1].split(end, 1)[0]
        source = start + snippet
        source = textwrap.dedent(source)
        script = r"""
    const assert = require('node:assert/strict');
    const owner = 'kodmial';
    let marker = '<!-- issue-scheduler-dispatch -->';
    """ + source + r"""
    const receipt = (priority = 'priority:p0') => [
      marker,
      '⚡ **Continuum · issue-scheduler**',
      '<!-- continuum-origin role=continuum component=issue-scheduler -->',
      'Automatically dispatched by the issue scheduler (' + priority + ').',
    ].join('\n');
    const trusted = body => ({user:{login:owner}, body});
    const valid = receipt();
    assert.equal(isCanonicalSchedulerDispatch(trusted('/oc\n\n' + valid)), true);
    assert.equal(isCanonicalSchedulerDispatch(trusted(valid)), true);
    assert.equal(isCanonicalSchedulerDispatch(trusted(receipt('no explicit priority'))), true);
    assert.equal(isCanonicalSchedulerDispatch(trusted('/oc\n\n' + marker)), false);
    assert.equal(isCanonicalSchedulerDispatch(trusted('/oc\n\n> ' + valid.replaceAll('\n','\n> '))), false);
    assert.equal(isCanonicalSchedulerDispatch(trusted('/oc\n\n' + valid + '\nquoted comment')), false);
    assert.equal(isCanonicalSchedulerDispatch({user:{login:'stranger'},body:valid}), false);
    assert.equal(isCanonicalSchedulerDispatch({user:{login:'github-actions[bot]'},body:valid}), true);
    marker = '<!-- custom-scheduler-dispatch -->';
    assert.equal(isCanonicalSchedulerDispatch(trusted(receipt())), true);
    assert.equal(isCanonicalSchedulerDispatch(trusted(valid)), false);
    """
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


    def test_issue_scheduler_uses_the_same_canonical_receipt_in_every_control_path(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        scheduler = (ROOT / ".github/scripts/issue_scheduler.js").read_text(encoding="utf-8")
        start = "            function isCanonicalSchedulerDispatch(comment) {"
        end = "            // Local consumers may still derive pause_on_failure from public"
        workflow_fn = textwrap.dedent(start + workflow.split(start, 1)[1].split(end, 1)[0]).strip()
        scheduler_start = "  function isCanonicalSchedulerDispatch(comment) {"
        scheduler_end = "\n  const postPauseComment"
        scheduler_fn = textwrap.dedent(
            scheduler_start + scheduler.split(scheduler_start, 1)[1].split(scheduler_end, 1)[0]
        ).strip()
        assert scheduler_fn == workflow_fn.replace("lines[0] === marker", "lines[0] === dispatchMarker")
        assert "body.includes(dispatchMarker)" not in scheduler
        assert "comments.filter(isCanonicalSchedulerDispatch)" in scheduler
        assert "if (isCanonicalSchedulerDispatch(comment)) {" in scheduler
        assert "return isCanonicalSchedulerDispatch(comment) || ownerOpenCodeCommand;" in scheduler
        assert "if (trustedFencer && isCanonicalSchedulerDispatch(comment)) return true;" in scheduler


    def test_quoted_marker_cannot_force_nonterminal_manual_recovery(self) -> None:
        body = WORKFLOW.read_text(encoding="utf-8")
        script = body.split("  recover-scheduled-issue:", 1)[1]
        assert "let pauseOnFailure =" in script
        assert "const triggeringBody = String(context.payload.comment?.body || '');" in script
        assert "triggeringBody.includes(marker) &&" in script
        assert "!isCanonicalSchedulerDispatch(context.payload.comment)" in script
        assert "pauseOnFailure = true;" in script
        assert "const attempts = comments.filter(isCanonicalSchedulerDispatch).length;" in script
