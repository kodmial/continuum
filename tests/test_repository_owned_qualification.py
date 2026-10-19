"""Repository-owned product qualifiers must not spawn generic LLM proof.

The generic worker cannot own provider/book/Telegram evidence or publish
a trusted product FAIL when the qualification issue names its own executor.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"


def test_generic_qualification_respects_explicit_repository_owned_marker() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    block = source.split("- name: Resolve qualification run identity", 1)[1].split(
        "- name: Run mandatory qualification at the exact required SHA", 1
    )[0]
    assert "continuum-qualification-executor" in block
    assert "repository-owned" in block
    assert "github.rest.issues.get" in block
    assert "core.setOutput('active', 'false')" in block
    assert "core.setOutput('repository_owned', 'true')" in block
    assert "core.setOutput('active', 'true')" in block
    assert block.index("repositoryOwned") < block.index("core.setOutput('active', 'true')")
    # The ordinary qualifier only runs when this identity step grants access.
    runner = source.split("- name: Run mandatory qualification at the exact required SHA", 1)[1]
    assert "steps.qualification_identity.outputs.active == 'true'" in runner
    assert "Publish exactly one canonical result marker" in runner


def test_consumer_qualification_result_is_not_manufactured_by_skip() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    block = source.split("- name: Resolve qualification run identity", 1)[1].split(
        "- name: Run mandatory qualification at the exact required SHA", 1
    )[0]
    owned = block.split("if (repositoryOwned)", 1)[1].split("core.setOutput('active', 'true')", 1)[0]
    assert "return;" in owned
    assert "result=pass" not in owned
    assert "result=fail" not in owned
