"""PQC-2.2 review round 2 regressions: possible roles, separators, damaged markers, bracketed aliases."""

from __future__ import annotations

import pytest
from test_roster_2_2 import _agent, _roster_action, home, project  # noqa: F401 - fixtures

import doctor
import roster
import setup_flow


def test_a_description_naming_two_roles_hides_nothing(tmp_path):
    code = tmp_path / "p"
    _agent(code / ".claude" / "agents" / "helper.md", "helper", "claude-opus-5-5", "Hands work from the builder to the reviewer")
    st = roster.detect_tool(code, "claude")
    assert st["extra"] == [".claude/agents/helper.md"] and not st["possible"]
    assert {"builder", "reviewer"} <= set(st["missing"])


def test_underscores_plurals_and_hyphens_all_separate_words():
    for name, role in (("mn_builder", "builder"), ("code_reviewer", "reviewer"), ("team_retrieval", "retrieval"),
                       ("code-reviewers", "reviewer"), ("x_advisor", "advisor")):
        assert roster.match_role(name, "") == role, name
    assert roster.match_role("helper", "Reviews the diff cold") is None, "prose is not an identity"
    assert roster.match_role("coder", "Writes code; never reviews it") is None
    assert roster.match_role("helper", "The cold reviewer for each story") == "reviewer"
    assert roster.match_role("ui-preview", "") is None and roster.match_role("db-sync-worker", "") is None


def test_doctor_warns_about_a_possible_role_instead_of_passing(project):
    _agent(project / ".cursor/agents/builder-reviewer.md", "builder-reviewer", "inherit", "does both")
    plan = setup_flow.build_plan(project)
    files = {(f["tool"], f["role"]) for f in _roster_action(plan)["files"]}
    assert ("cursor", "builder") not in files and ("cursor", "reviewer") not in files
    setup_flow.apply_actions(plan, [_roster_action(plan)["id"]])
    c = doctor.run(str(project))["checks"][9]
    assert c["status"] == "warn" and "named for several roles" in c["detail"] and "pin not checked" in c["detail"]
    inv = next(g for g in setup_flow.build_plan(project)["inventory"] if g["group"] == "Roster")
    cursor = next(i for i in inv["items"] if i["what"] == "Agents for Cursor")
    assert "builder: possibly .cursor/agents/builder-reviewer.md" in cursor["now"] and "missing" not in cursor["now"].split("builder:")[1].split(";")[0]


def test_damaged_markers_are_reported_once_not_proposed_forever(project):
    orch = project / "docs/ORCHESTRATION.md"
    orch.write_text(orch.read_text(encoding="utf-8") + "\n" + roster.ROSTER_START + "\n", encoding="utf-8")  # a second start
    plan = setup_flow.build_plan(project)
    setup_flow.apply_actions(plan, [_roster_action(plan)["id"]])  # still writes the agent files
    again = setup_flow.build_plan(project)
    assert _roster_action(again) is None
    inv = next(g for g in again["inventory"] if g["group"] == "Roster")
    item = next(i for i in inv["items"] if i["what"].startswith("Roster table"))
    assert item["status"] == "review" and "damaged" in item["now"]


def test_a_table_outside_the_markers_does_not_count(project):
    orch = project / "docs/ORCHESTRATION.md"
    orch.write_text("| Role | Agent | Model |\n|---|---|---|\n" + orch.read_text(encoding="utf-8"), encoding="utf-8")
    assert setup_flow.read_roster(project, {"docs_path": str(project / "docs")}, ["claude"])["has_section"] is False


@pytest.mark.parametrize("tool,model", [("claude", "opus[1m]"), ("cursor", "auto[fast=false]")])
def test_bracketed_aliases_are_still_aliases(tool, model):
    assert roster.forbidden_pin(tool, "advisor", model) and roster.check_pin(tool, "advisor", model)


def test_alias_check_ignores_case():
    assert roster.forbidden_pin("claude", "advisor", "Opus[1m]") and roster.check_pin("claude", "advisor", "Opus")
