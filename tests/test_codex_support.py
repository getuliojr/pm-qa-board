"""PQC-25: First-class OpenAI Codex support and ecosystem wiring."""

from __future__ import annotations

from pathlib import Path
import pytest

import doctor
import onboarding
import roster
import setup_flow
import wiring


def test_codex_md_exists_in_repo_root():
    root = Path(__file__).resolve().parent.parent
    codex_file = root / "CODEX.md"
    assert codex_file.is_file(), "CODEX.md must exist in the root of the tool repository"
    content = codex_file.read_text(encoding="utf-8")
    assert "CLAUDE.md" in content
    assert "pm-orchestrate" in content
    assert "never push" in content


def test_orchestration_files_includes_codex_md():
    assert "CODEX.md" in onboarding.ORCHESTRATION_FILES
    assert "CODEX.md" in wiring.ORCHESTRATION_FILES


def test_detect_agents_with_codex_md(tmp_path: Path):
    proj = tmp_path / "codex_proj"
    proj.mkdir()
    (proj / "CODEX.md").write_text("# My Codex Project\n", encoding="utf-8")
    agents = setup_flow.detect_agents(proj)
    assert "codex" in agents


def test_detect_agents_with_codex_dir(tmp_path: Path):
    proj = tmp_path / "codex_proj"
    (proj / ".codex").mkdir(parents=True)
    agents = setup_flow.detect_agents(proj)
    assert "codex" in agents


def test_codex_tool_labels_and_order():
    assert setup_flow.TOOL_LABELS["codex"] == "Codex"
    assert "codex" in setup_flow.TOOL_ORDER


def test_codex_roster_defaults_and_known_models():
    models = roster.DEFAULT_MODELS["codex"]
    assert models["lead"] == "gpt-5.6-terra"
    assert models["builder"] == "gpt-5.6-terra"
    assert models["advisor"] == "gpt-6.1-sol"
    assert models["reviewer"] == "gpt-6.1-sol"
    assert models["retrieval"] == "gpt-6-luna"
    assert models["pm-sync"] == "gpt-6-luna"

    for role in roster.ROLES:
        assert role in models
        pin_check = roster.check_pin("codex", role, models[role])
        assert pin_check is None, f"Expected pin for {role} to be valid, got {pin_check}"


def test_codex_renders_agent_file():
    text = roster.render_agent("codex", "advisor", "gpt-6.1-sol", "medium")
    fm = roster.frontmatter(text)
    assert fm["model"] == "gpt-6.1-sol"
    assert fm["name"] == roster.PREFIX + "advisor"
    assert "mainAgent" not in fm  # mainAgent is only for antigravity


def test_setup_flow_build_plan_wires_codex_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PM_QA_USER_HOME", str(tmp_path / "home"))
    proj = tmp_path / "my_project"
    proj.mkdir()
    (proj / "CODEX.md").write_text("# Instructions\n", encoding="utf-8")

    plan = setup_flow.build_plan(proj)
    assert "codex" in plan["tools"]
    # Check that a pointer action is scheduled for CODEX.md
    pointer_actions = [a for a in plan["actions"] if a["kind"] == "pointer"]
    assert any(a["file"] == "CODEX.md" for a in pointer_actions)
