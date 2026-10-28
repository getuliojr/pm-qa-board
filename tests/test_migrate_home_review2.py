"""PQC-34 review round 2: regressions and gaps found after the first fix commit."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import migrate
import setup_flow
import wiring
from test_migrate_home import NEW, OLD, _fake_clone, _register, make_old_install, _tree  # noqa: F401  (fixtures below)

BS = chr(92)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("PM_QA_USER_HOME", str(h))
    monkeypatch.delenv("PM_QA_BOARD_HOME", raising=False)
    monkeypatch.setattr(migrate, "dashboards_running", lambda *a, **k: [])
    monkeypatch.setattr(migrate, "_clone", _fake_clone)
    return h


# -- wiring: the old running copy is never a dev clone ---------------------------------------------


def test_the_old_running_copy_is_not_classified_as_a_dev_clone(home, tmp_path):
    (home / OLD / "app" / "board").mkdir(parents=True)
    (home / OLD / "app" / "board" / "server.py").write_text("# old running copy\n", encoding="utf-8")
    code = tmp_path / "proj"
    code.mkdir()
    ps1 = 'uv run python "$HOME' + BS + OLD + BS + "app" + BS + "cockpit" + BS + 'cli.py" list'
    sh = "uv run python ~/" + OLD + "/app/cockpit/cli.py list"
    assert wiring.classify_line(ps1, [code]) == []
    assert wiring.classify_line(sh, [code]) == []


def test_dot_slash_paths_in_a_board_checkout_are_not_dev_clone_references(tmp_path):
    code = tmp_path / "checkout"
    (code / "board").mkdir(parents=True)
    (code / "board" / "server.py").write_text("# this project is itself a board checkout\n", encoding="utf-8")
    assert wiring.classify_line("powershell -File ./scripts/gates.ps1", [code]) == []
    assert wiring.classify_line("powershell -File .\\scripts\\gates.ps1", [code]) == []


# -- managed blocks: user data that happens to contain the old name stays -------------------------


def test_registered_slug_and_docs_paths_inside_a_block_are_kept(home, tmp_path):
    code = make_old_install(home, tmp_path)
    other = tmp_path / "flight"
    other.mkdir()
    docs = tmp_path / "pm-qa-docs" / "flight-cockpit"
    docs.mkdir(parents=True)
    _register(home, (migrate.OLD_SKILL, code, None), ("flight-cockpit", other, docs))
    (code / "CLAUDE.md").write_text(
        "<!-- pm-qa-cockpit:start -->\n"
        "Dashboard: powershell -File ~/.pm-qa-cockpit/app/dashboard.ps1 -Project pm-qa-cockpit\n"
        "Skill: `pm-qa-cockpit`\n"
        "Docs: ..\\pm-qa-docs\\pm-qa-cockpit\\ and " + str(docs) + "\n"
        "Other project: flight-cockpit\n"
        "<!-- pm-qa-cockpit:end -->\n", encoding="utf-8")
    assert not migrate.apply()["failed"]
    text = (code / "CLAUDE.md").read_text(encoding="utf-8")
    assert "-Project pm-qa-cockpit" in text                     # the registered slug still resolves
    assert "Skill: `pm-qa-board`" in text                       # the skill itself was renamed
    assert "~/.pm-qa-board/app/dashboard.ps1" in text
    assert "..\\pm-qa-docs\\pm-qa-cockpit\\" in text            # private docs folder, backslash form too
    assert str(docs) in text and "Other project: flight-cockpit" in text


def test_an_unbalanced_marker_never_swallows_the_users_text(home, tmp_path):
    code = make_old_install(home, tmp_path)
    (code / "AGENTS.md").write_text(
        "<!-- pm-qa-cockpit:start -->\nPointer text with cockpit\n"            # the end marker was deleted by the user
        "USER cockpit text that is not ours\n"
        "<!-- pm-qa-cockpit:roster:start -->\nroster cockpit\n<!-- pm-qa-cockpit:roster:end -->\n", encoding="utf-8")
    assert not migrate.apply()["failed"]
    text = (code / "AGENTS.md").read_text(encoding="utf-8")
    assert "USER cockpit text that is not ours" in text
    assert "roster board" in text and "<!-- pm-qa-board:roster:start -->" in text


# -- agent files keep their line endings ----------------------------------------------------------


def test_crlf_agent_files_keep_crlf(home, tmp_path):
    code = make_old_install(home, tmp_path)
    agents = code / ".claude" / "agents"
    agents.mkdir()
    (agents / "cockpit-lead.md").write_bytes(b"---\r\nname: cockpit-lead\r\n---\r\nLead the cockpit.\r\n")
    assert not migrate.apply()["failed"]
    raw = (agents / "board-lead.md").read_bytes()
    assert raw == b"---\r\nname: board-lead\r\n---\r\nLead the board.\r\n"


# -- the registry: cloned first, then setup ran, then migrate -----------------------------------------


def test_old_registry_projects_are_merged_into_a_registry_setup_already_created(home, tmp_path):
    code = make_old_install(home, tmp_path)   # registers "p" in the OLD registry only
    new = home / NEW
    _fake_clone("u", str(new / "app"))
    created = tmp_path / "created-by-setup"
    created.mkdir()
    (new / "projects.json").write_text(json.dumps({"active_project": "fresh", "projects": {
        "fresh": {"name": "Fresh", "slug": "fresh", "prefix": "FRE", "code_path": str(created),
                  "docs_path": str(created / "docs"), "docs_mode": "in-repo", "gates": {"mode": "unprofiled"}}}}), encoding="utf-8")
    state = migrate.legacy_state()
    assert state["needs_migration"] is True
    p = migrate.plan()
    merge = [m for m in p["moves"] if m.get("kind") == "merge-registry"]
    assert merge and merge[0]["add"] == ["p"]
    assert {e["file"] for e in p["launchers"]} == {"scripts/dash.ps1", ".claude/settings.json"}  # project p is scanned too
    assert not migrate.apply()["failed"]
    merged = json.loads((new / "projects.json").read_text(encoding="utf-8"))
    assert set(merged["projects"]) == {"fresh", "p"} and merged["active_project"] == "fresh"
    assert list((new / "migration-backup").rglob("projects.json.before-merge"))
    assert migrate.legacy_state()["needs_migration"] is False
    assert migrate.plan()["state"] == "already-migrated"


# -- a process check that cannot run must say so --------------------------------------------------------


def test_a_failing_process_check_adds_a_warning_instead_of_silently_passing(home, monkeypatch):
    def boom(*a, **k):
        raise OSError("powershell is not available")
    monkeypatch.undo()  # use the real dashboards_running for this test
    monkeypatch.setenv("PM_QA_USER_HOME", str(home))
    monkeypatch.setattr(migrate.subprocess, "run", boom)
    warn: list[str] = []
    assert migrate.dashboards_running(warn) == []
    assert warn and "could not check for running dashboards" in warn[0]


def test_the_plan_shows_the_warning(home, tmp_path, monkeypatch, capsys):
    make_old_install(home, tmp_path)
    monkeypatch.setattr(migrate, "dashboards_running", lambda warn=None: (warn.append("could not check for running dashboards (test)") or []))
    p = migrate.plan()
    assert p["state"] == "ready" and p["warnings"]
    assert "WARNING:" in migrate.format_plan(p)


# -- setup never adds a second section next to an un-migrated one --------------------------------------


def test_setup_does_not_add_a_second_pointer_section_next_to_an_old_one(sandbox_home):
    sb, plan0 = sandbox_home
    target = sb["project"] / "CLAUDE.md"
    target.write_text(migrate.OLD_POINTER_START + "\nold section\n" + migrate.OLD_POINTER_START.replace(":start", ":end") + "\n", encoding="utf-8")
    plan = setup_flow.build_plan(sb["project"])
    assert not [a for a in plan["actions"] if a["kind"] == "pointer" and a.get("file") == "CLAUDE.md"]
    assert any("older version wrote" in n and "CLAUDE.md" in n for n in plan["notes"])


def test_setup_leaves_an_old_roster_for_migrate_home(sandbox_home):
    sb, plan0 = sandbox_home
    orch = Path(plan0["roster"]["orchestration"])
    orch.parent.mkdir(parents=True, exist_ok=True)
    orch.write_text("# Orchestration\n" + migrate.OLD_ROSTER_START + "\nold roster\n", encoding="utf-8")
    plan = setup_flow.build_plan(sb["project"])
    assert plan["roster"]["markers"] == "legacy"
    assert any("roster written by an older version" in n for n in plan["notes"])


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    import sandbox_fixture
    paths = sandbox_fixture.build(tmp_path / "sbx", version=setup_flow.tool_version())
    monkeypatch.setenv("PM_QA_USER_HOME", str(paths["home"]))
    monkeypatch.delenv("PM_QA_BOARD_HOME", raising=False)
    return paths, setup_flow.build_plan(paths["project"])
