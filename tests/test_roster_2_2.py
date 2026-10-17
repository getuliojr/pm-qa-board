"""PQC-2.2: who does each step - roster detection, defaults, setup action, doctor check 10."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sandbox_fixture
from conftest import init_repo

import doctor
import onboarding
import roster
import setup_flow

ID_RE = re.compile(r"\bA\d+\b")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_QA_USER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PM_QA_COCKPIT_HOME", raising=False)
    return tmp_path / "home"


def _agent(path: Path, name: str, model: str, description: str = "", effort: str = "") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: {description}\nmodel: {model}\n" + (f"effort: {effort}\n" if effort else "") + "---\n\nMine.\n"
    path.write_bytes(text.encode("utf-8"))
    return path.read_bytes()


@pytest.fixture
def project(tmp_path, home):
    """A full-mode project on Claude Code + Cursor with only its own builder agent for Claude."""
    code = init_repo(tmp_path / "proj")
    (code / "CLAUDE.md").write_text("# proj\nNever push.\n", encoding="utf-8")
    (code / ".cursor" / "rules").mkdir(parents=True)
    (code / ".cursor" / "rules" / "x.mdc").write_text("x\n", encoding="utf-8")
    (code / "docs").mkdir()
    (code / "docs" / "ORCHESTRATION.md").write_text(onboarding.ORCHESTRATION_TEMPLATE.format(
        name="proj", cards="cards/", prefix="PR", gate_mode="fast", gate_note="x"), encoding="utf-8")
    _agent(code / ".claude" / "agents" / "my-builder.md", "my-builder", "claude-sonnet-5-5", "Builds things")
    return code


def _roster_action(plan):
    return next((a for a in plan["actions"] if a["kind"] == "roster"), None)


# ---------------------------------------------------------------- detection


def test_roles_are_matched_by_name_then_description(tmp_path):
    code = tmp_path / "p"
    _agent(code / ".cursor" / "agents" / "retrieve.md", "retrieve", "composer-2.5[fast=false]")
    _agent(code / ".cursor" / "agents" / "x.md", "x", "grok-4.7[fast=false]", "The cold reviewer gate")
    _agent(code / ".cursor" / "agents" / "helper.md", "helper", "grok-4.6[fast=false]", "Does odd jobs")
    _agent(code / ".cursor" / "agents" / "builder-reviewer.md", "builder-reviewer", "grok-4.6[fast=false]", "does both")
    st = roster.detect_tool(code, "cursor")
    assert st["roles"]["retrieval"]["file"] == ".cursor/agents/retrieve.md"
    assert st["roles"]["reviewer"]["file"] == ".cursor/agents/x.md"
    assert st["extra"] == [".cursor/agents/helper.md"]
    assert st["possible"] == {"builder": [".cursor/agents/builder-reviewer.md"], "reviewer": [".cursor/agents/builder-reviewer.md"]}
    assert set(st["missing"]) == {"advisor", "pm-sync"}, "a file that could fill a role is never duplicated"


def test_matching_uses_whole_words():
    assert roster.match_role("ui-preview", "") is None
    assert roster.match_role("frontend", "Builds UI with async data loading") is None
    assert roster.match_role("db-sync-worker", "") is None
    assert roster.match_role("money-nest-builder", "") == "builder"
    assert roster.match_role("pm-sync", "") == "pm-sync" and roster.match_role("retrieve", "") == "retrieval"


def test_the_lead_row_round_trips_through_the_roster_section():
    labels = {"claude": "Claude Code", "cursor": "Cursor"}
    rows = {"claude": {"lead": {"agent": "(session pick)", "model": "claude-opus-5-5", "effort": "high"},
                       "builder": {"agent": "b", "model": "m", "effort": "-"}}}
    text = "intro\n" + roster.render_section(rows, labels)
    assert roster.parse_lead(text, labels) == {"claude": {"model": "claude-opus-5-5", "effort": "high"}}


def test_orchestration_template_has_an_empty_roster_section():
    t = onboarding.ORCHESTRATION_TEMPLATE
    assert roster.ROSTER_START in t and roster.ROSTER_END in t and "## Roster" in t


# ---------------------------------------------------------------- defaults and files


def test_defaults_are_full_ids_for_every_tool_and_known():
    assert roster.DEFAULT_MODELS["claude"] == {
        "lead": "claude-opus-5-5", "advisor": "claude-opus-5-5", "reviewer": "claude-opus-5-5",
        "builder": "claude-sonnet-5-5", "retrieval": "claude-haiku-5-5", "pm-sync": "claude-haiku-5-5"}
    assert roster.DEFAULT_EFFORT == {"lead": "high", "advisor": "medium", "reviewer": "high", "builder": "medium",
                                     "retrieval": "low", "pm-sync": "low"}
    for tool, models in roster.DEFAULT_MODELS.items():
        assert set(models) == set(roster.ROLES)
        for role, m in models.items():
            assert roster.check_pin(tool, role, m) is None, (tool, role, m)
    assert all("[fast=false]" in m for m in roster.DEFAULT_MODELS["cursor"].values())


def test_agent_file_names_never_clash_and_match_back_to_their_role():
    taken = set(setup_flow.PROJECT_SKILLS) | {setup_flow.BOOTSTRAP_SKILL}
    for tool in roster.WRITE_DIR:
        for role in roster.FILE_ROLES:
            fm = roster.frontmatter(roster.render_agent(tool, role, "x", "low"))
            assert fm["name"] == roster.PREFIX + role and fm["name"] not in taken
            assert roster.match_role(fm["name"], fm["description"]) == role


def test_effort_goes_in_frontmatter_only_where_verified():
    claude = roster.render_agent("claude", "advisor", "claude-opus-5-5", "medium")
    assert roster.frontmatter(claude)["effort"] == "medium"
    for tool in ("cursor", "antigravity"):
        text = roster.render_agent(tool, "advisor", "m", "medium")
        assert "effort" not in roster.frontmatter(text) and "Effort: **medium**" in text
    assert roster.frontmatter(roster.render_agent("claude", "retrieval", "m", "low"))["tools"].startswith("Read")
    assert "Edit" not in roster.frontmatter(roster.render_agent("claude", "reviewer", "m", "high"))["tools"]


# ---------------------------------------------------------------- setup


def test_partial_roster_offers_only_missing_roles_and_never_edits_a_file(project):
    mine = project / ".claude" / "agents" / "my-builder.md"
    before = mine.read_bytes()
    plan = setup_flow.build_plan(project)
    a = _roster_action(plan)
    got = {(f["tool"], f["role"]) for f in a["files"]}
    assert got == ({("claude", r) for r in ("advisor", "reviewer", "retrieval", "pm-sync")}
                   | {("cursor", r) for r in roster.FILE_ROLES})
    (d,) = [d for d in plan["decisions"] if d["theme"] == "roster"]
    assert d["question"].endswith("use the recommended roster?") and not ID_RE.search(d["question"] + d["recommendation"])
    assert [o["label"].split(" (")[0] for o in d["options"]] == ["Yes, use the recommended roster", "Customise", "Not now"]
    assert any(r["who"] == "yours" and r["role"] == "builder" for r in plan["roster_table"])
    assert len(plan["decisions"]) <= 5

    setup_flow.apply_actions(plan, [x["id"] for x in plan["actions"] if x["kind"] == "roster"])
    assert mine.read_bytes() == before
    adv = roster.frontmatter((project / ".claude/agents/cockpit-advisor.md").read_text(encoding="utf-8"))
    assert adv["model"] == "claude-opus-5-5" and adv["effort"] == "medium"
    cur = (project / ".cursor/agents/cockpit-reviewer.md").read_text(encoding="utf-8")
    assert roster.frontmatter(cur)["model"] == "grok-4.7[fast=false]"
    orch = (project / "docs/ORCHESTRATION.md").read_text(encoding="utf-8")
    assert orch.count(roster.ROSTER_START) == 1 and "| builder | my-builder |" in orch and "| lead | (session pick) |" in orch
    assert roster.parse_lead(orch, setup_flow.TOOL_LABELS)["claude"] == {"model": "claude-opus-5-5", "effort": "high"}

    again = setup_flow.build_plan(project)
    assert _roster_action(again) is None and not [d for d in again["decisions"] if d["theme"] == "roster"]
    assert doctor.run(str(project))["checks"][9]["status"] == "pass"


def test_customise_overrides_apply_and_bad_ones_change_nothing(project):
    plan = setup_flow.build_plan(project)
    aid = _roster_action(plan)["id"]
    qs = next(d for d in plan["decisions"] if d["theme"] == "roster")["questions"]
    assert {(q["tool"], q["role"]) for q in qs} >= {("claude", "lead"), ("claude", "advisor"), ("cursor", "builder")}
    assert qs[0]["models"][0] == qs[0]["recommendation"].split(",")[0]
    for bad in ({"claude": {"builder": {"model": "claude-opus-5-5"}}},  # builder is the user's: not part of the action
                {"claude": {"advisor": {"model": "x: y\nz"}}}, {"claude": {"advisor": {"effort": "huge"}}},
                {"antigravity": {"advisor": {"model": "pro"}}}, {"claude": {"advisor": {"tools": "Edit"}}}):
        with pytest.raises(ValueError):
            setup_flow.apply_actions(plan, [aid], roster_overrides=bad)
        assert not (project / ".claude/agents/cockpit-advisor.md").exists()
    setup_flow.apply_actions(plan, [aid], roster_overrides={
        "claude": {"advisor": {"model": "claude-sonnet-5-5", "effort": "high"}, "lead": {"model": "claude-fable-5-1", "effort": "xhigh"}}})
    adv = roster.frontmatter((project / ".claude/agents/cockpit-advisor.md").read_text(encoding="utf-8"))
    assert adv["model"] == "claude-sonnet-5-5" and adv["effort"] == "high"
    orch = (project / "docs/ORCHESTRATION.md").read_text(encoding="utf-8")
    assert roster.parse_lead(orch, setup_flow.TOOL_LABELS)["claude"] == {"model": "claude-fable-5-1", "effort": "xhigh"}


def test_an_existing_cockpit_file_is_kept_byte_identical(project):
    plan = setup_flow.build_plan(project)
    mine = project / ".claude/agents/cockpit-advisor.md"
    before = _agent(mine, "cockpit-advisor", "opus")  # appears between plan and apply
    res = setup_flow.apply_actions(plan, [_roster_action(plan)["id"]])
    assert mine.read_bytes() == before and "already there, kept" in res[0]["result"]


def test_crlf_orchestration_keeps_its_line_endings(project):
    orch = project / "docs/ORCHESTRATION.md"
    orch.write_bytes(orch.read_bytes().replace(b"\n", b"\r\n"))
    plan = setup_flow.build_plan(project)
    setup_flow.apply_actions(plan, [_roster_action(plan)["id"]])
    data = orch.read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b"") and b"| lead |" in data


def test_missing_orchestration_file_is_reported_not_created(project):
    (project / "docs/ORCHESTRATION.md").unlink()
    plan = setup_flow.build_plan(project)
    (res,) = setup_flow.apply_actions(plan, [_roster_action(plan)["id"]])
    assert not (project / "docs/ORCHESTRATION.md").exists() and "re-run setup" in res["result"]


def test_external_docs_keep_the_new_agent_files_out_of_git(project, tmp_path):
    ext = tmp_path / "ext-docs"
    ext.mkdir()
    (ext / "ORCHESTRATION.md").write_text(onboarding.ORCHESTRATION_TEMPLATE.format(
        name="p", cards="c", prefix="P", gate_mode="fast", gate_note="x"), encoding="utf-8")
    plan = setup_flow.build_plan(project, docs_path=ext)
    ex = next(a for a in plan["actions"] if a["kind"] == "exclude")
    assert ".claude/agents/cockpit-advisor.md" in ex["paths"] and ".cursor/agents/cockpit-builder.md" in ex["paths"]
    assert ".claude/agents/my-builder.md" not in ex["paths"]


# ---------------------------------------------------------------- register-only (money-nest-shaped)


@pytest.fixture
def money_nest(tmp_path, monkeypatch):
    paths = sandbox_fixture.build(tmp_path / "sbx", version=setup_flow.tool_version())
    monkeypatch.setenv("PM_QA_USER_HOME", str(paths["home"]))
    monkeypatch.delenv("PM_QA_COCKPIT_HOME", raising=False)
    return paths


def test_money_nest_roster_is_reported_and_nothing_is_written(money_nest):
    code = money_nest["project"]
    before = {p.relative_to(code).as_posix(): p.read_bytes() for p in code.rglob("*") if p.is_file() and ".git" not in p.parts}
    plan = setup_flow.build_plan(code)
    assert plan["mode"] == "register-only" and _roster_action(plan) is None
    assert not [d for d in plan["decisions"] if d["theme"] == "roster"]
    inv = next(g for g in plan["inventory"] if g["group"] == "Roster")
    claude = next(i for i in inv["items"] if i["what"] == "Agents for Claude Code")
    assert "advisor: opus (yours)" in claude["now"] and "reviewer: missing" in claude["now"] and claude["status"] == "review"
    cursor = next(i for i in inv["items"] if i["what"] == "Agents for Cursor")
    assert cursor["status"] == "ok" and "pm-sync: composer-2.5[fast=false] (yours)" in cursor["now"]
    setup_flow.apply_actions(plan, [a["id"] for a in plan["actions"] if a["kind"] not in ("register", "profile-gates")])
    after = {p.relative_to(code).as_posix(): p.read_bytes() for p in code.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert not [k for k in after if "agents/" in k and k not in before], "no agent file written"
    assert {k: v for k, v in before.items() if "agents/" in k} == {k: v for k, v in after.items() if "agents/" in k}


def test_money_nest_doctor_warns_on_gaps_and_the_alias_pin(money_nest):
    c = doctor.run("money-nest")["checks"][9]
    assert c["n"] == 10 and c["status"] == "warn"
    assert "no agent for Claude Code reviewer, Claude Code pm-sync" in c["detail"]
    assert "'opus' is an alias" in c["detail"] and "'sonnet' is an alias" in c["detail"]
    assert "add those agents by hand" in c["fix"]


# ---------------------------------------------------------------- doctor pins


def test_pins_warn_for_alias_unknown_and_inherit_but_never_fail(project):
    plan = setup_flow.build_plan(project)
    setup_flow.apply_actions(plan, [_roster_action(plan)["id"]])
    assert doctor.run(str(project))["checks"][9]["status"] == "pass"
    for rel, pin, expect in ((".claude/agents/cockpit-retrieval.md", "haiku", "alias"),
                             (".claude/agents/cockpit-advisor.md", "claude-opus-9-9", "not in the list"),
                             (".claude/agents/cockpit-reviewer.md", "inherit", "inherit"),
                             (".cursor/agents/cockpit-reviewer.md", "gpt-9", "not in the list")):
        f = project / rel
        original = f.read_text(encoding="utf-8")
        f.write_text(re.sub(r"(?m)^model: .*$", f"model: {pin}", original), encoding="utf-8")
        report = doctor.run(str(project))
        c = report["checks"][9]
        assert c["status"] == "warn" and expect in c["detail"], (rel, c)
        f.write_text(original, encoding="utf-8")
    f = project / ".claude/agents/my-builder.md"  # inherit is fine for a builder
    f.write_text(f.read_text(encoding="utf-8").replace("claude-sonnet-5-5", "inherit"), encoding="utf-8")
    assert doctor.run(str(project))["checks"][9]["status"] == "pass"


def test_cursor_parameters_do_not_make_a_known_model_unknown():
    assert roster.check_pin("cursor", "advisor", "grok-4.6[fast=false]") is None
    assert roster.check_pin("cursor", "advisor", "grok-4.6") is None
    assert roster.check_pin("antigravity", "reviewer", "inherit")
    assert roster.check_pin("antigravity", "reviewer", "pro") is None


# ---------------------------------------------------------------- review round 1 regressions


def test_fresh_install_records_the_table_after_registration(project):
    orch = project / "docs/ORCHESTRATION.md"
    orch.rename(project / "docs/hold.md")
    plan = setup_flow.build_plan(project)
    setup_flow.apply_actions(plan, [_roster_action(plan)["id"]])
    assert not orch.exists() and (project / ".claude/agents/cockpit-advisor.md").exists()
    (project / "docs/hold.md").rename(orch)  # `init --apply` has now written the template
    again = setup_flow.build_plan(project)
    a = _roster_action(again)
    assert a is not None and a["files"] == [] and "record the roster" in setup_flow._say(a, again)
    setup_flow.apply_actions(again, [a["id"]])
    assert "| lead | (session pick) |" in orch.read_text(encoding="utf-8")
    assert _roster_action(setup_flow.build_plan(project)) is None


def test_damaged_markers_are_never_refreshed_over_user_text(project):
    orch = project / "docs/ORCHESTRATION.md"
    text = orch.read_text(encoding="utf-8").replace(roster.ROSTER_END, "")
    mine = text + "\nMY OWN NOTES\n"
    orch.write_text(mine, encoding="utf-8")
    plan = setup_flow.build_plan(project)
    (res,) = setup_flow.apply_actions(plan, [_roster_action(plan)["id"]])
    assert "damaged" in res["result"] and orch.read_text(encoding="utf-8") == mine


def test_customise_refuses_aliases_inherit_and_wrong_shapes(project):
    plan = setup_flow.build_plan(project)
    aid = _roster_action(plan)["id"]
    for bad in ({"claude": {"advisor": {"model": "opus"}}}, {"claude": {"reviewer": {"model": "inherit"}}},
                {"claude": ["advisor"]}, {"claude": {"advisor": None}}, {"claude": {"advisor": ["model"]}}, ["x"]):
        with pytest.raises(ValueError):
            setup_flow.apply_actions(plan, [aid], roster_overrides=bad)
    assert not (project / ".claude/agents/cockpit-advisor.md").exists()


def test_cursor_customise_options_keep_the_fast_off_suffix(project):
    plan = setup_flow.build_plan(project)
    qs = next(d for d in plan["decisions"] if d["theme"] == "roster")["questions"]
    cursor = [m for q in qs if q["tool"] == "cursor" for m in q["models"]]
    assert cursor and all(m.endswith("[fast=false]") for m in cursor)
