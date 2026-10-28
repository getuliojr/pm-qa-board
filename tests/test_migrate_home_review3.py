"""PQC-34 review round 3 follow-ups."""
from __future__ import annotations

import json
from pathlib import Path

import migrate
import setup_flow
from test_migrate_home import NEW, OLD, _register, make_old_install  # noqa: F401
from test_migrate_home_review2 import home, sandbox_home  # noqa: F401  (fixtures)

BS = chr(92)


def test_setup_does_not_append_a_second_roster_table_when_an_old_roster_exists(sandbox_home):
    sb, _ = sandbox_home
    plan0 = setup_flow.build_plan(sb["project"], force_full=True)
    orch = Path(plan0["roster"]["orchestration"])
    orch.parent.mkdir(parents=True, exist_ok=True)
    orch.write_text("# Orchestration\n" + migrate.OLD_ROSTER_START + "\nold roster\n", encoding="utf-8")
    plan = setup_flow.build_plan(sb["project"], force_full=True)
    action = next((a for a in plan["actions"] if a["kind"] == "roster"), None)
    assert action is not None, "a missing role still gets its agent file written"
    result = setup_flow.apply_roster(sb["project"], plan, action, {})
    text = orch.read_text(encoding="utf-8")
    assert "older version wrote" in result and text.count(migrate.OLD_ROSTER_START) == 1
    assert "pm-qa-board:roster:start" not in text  # no second section next to the old one


def test_one_folder_registered_twice_is_scanned_and_edited_once(home, tmp_path):
    code = make_old_install(home, tmp_path)
    reg = json.loads((home / OLD / "projects.json").read_text(encoding="utf-8"))
    twin = dict(reg["projects"]["p"], slug="p-again", code_path=str(code).replace("\\", "/"))  # same folder, other slug and slashes
    reg["projects"]["p-again"] = twin
    (home / OLD / "projects.json").write_text(json.dumps(reg), encoding="utf-8")
    p = migrate.plan()
    keys = [(e["file"], e["line"]) for e in p["launchers"]]
    assert len(keys) == len(set(keys)) == 3
    res = migrate.apply()
    assert not res["failed"], res


def test_a_slug_that_is_part_of_the_old_name_does_not_shield_the_markers():
    assert migrate.broad_line("<!-- pm-qa-cockpit:start -->", ("cockpit",)) == "<!-- pm-qa-board:start -->"
    assert migrate.broad_line("Skill: `pm-qa-cockpit` at ~/.pm-qa-cockpit/app", ("cockpit", "qa-cockpit")) == "Skill: `pm-qa-board` at ~/.pm-qa-board/app"
    assert migrate.broad_line("my cockpit project", ("cockpit",)) is None  # the whole-token slug itself is kept


def test_protected_paths_match_regardless_of_case():
    registered = "C:" + BS + "work" + BS + "flight-cockpit"
    line = "docs in c:" + BS + "work" + BS + "flight-cockpit" + BS + "x"
    assert migrate.broad_line(line, (), (registered,)) is None


def test_the_old_folder_name_with_a_suffix_is_not_the_install_path():
    assert migrate.repoint_line("C:" + BS + "work" + BS + ".pm-qa-cockpit-fork" + BS + "run.ps1") is None
    assert migrate.repoint_line("$HOME" + BS + ".pm-qa-cockpit" + BS + "app" + BS + "x.ps1") == "$HOME" + BS + ".pm-qa-board" + BS + "app" + BS + "x.ps1"


def test_apply_shows_the_warning_without_a_dry_run_first(home, tmp_path, monkeypatch, capsys):
    make_old_install(home, tmp_path)
    monkeypatch.setattr(migrate, "dashboards_running", lambda warn=None: (warn.append("could not check for running dashboards (test)") or []))
    import argparse
    assert migrate.cmd_migrate_home(argparse.Namespace(apply=True, skip="", json=False)) == 0
    out = capsys.readouterr().out
    assert "WARNING could not check for running dashboards (test)" in out and "done" in out
