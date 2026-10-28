"""PQC-34: migrate-home moves an old install to PM & QA Board safely (temp homes only, never the real one)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

import migrate
import setup_flow

OLD = ".pm-qa-cockpit"
NEW = ".pm-qa-board"
win = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour (processes, shortcuts)")


def _fake_clone(url: str, dest: str) -> None:
    (Path(dest) / "board").mkdir(parents=True, exist_ok=True)
    (Path(dest) / "board" / "server.py").write_text("# new running copy\n", encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("PM_QA_USER_HOME", str(h))
    monkeypatch.delenv("PM_QA_BOARD_HOME", raising=False)
    monkeypatch.setattr(migrate, "dashboards_running", lambda: [])
    monkeypatch.setattr(migrate, "_clone", _fake_clone)
    return h


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _register(home: Path, *projects: tuple[str, Path, Path | None]) -> None:
    reg = {"active_project": projects[0][0], "projects": {
        slug: {"name": slug, "slug": slug, "prefix": slug[:3].upper(), "code_path": str(code),
               "docs_path": str(docs or code / "docs"), "docs_mode": "external" if docs else "in-repo",
               "gates": {"mode": "unprofiled"}} for slug, code, docs in projects}}
    (home / OLD).mkdir(parents=True, exist_ok=True)
    (home / OLD / "projects.json").write_text(json.dumps(reg), encoding="utf-8")


def make_old_install(home: Path, tmp_path: Path) -> Path:
    """An old home with a registry, caches, a project with a launcher and a hook, and installed skills."""
    old = home / OLD
    (old / "app" / "cockpit").mkdir(parents=True)
    (old / "app" / "cockpit" / "server.py").write_text("# old running copy\n", encoding="utf-8")
    (old / "cache").mkdir()
    (old / "cache" / "x.db").write_bytes(b"cache")
    (old / "plans").mkdir()
    (old / "plans" / "p.json").write_text("{}", encoding="utf-8")
    (old / "bin").mkdir()
    (old / "bin" / "cloudflared.exe").write_bytes(b"MZ")
    code = tmp_path / "proj"
    (code / "scripts").mkdir(parents=True)
    (code / "scripts" / "dash.ps1").write_text(
        '# starts the dashboard\n& powershell -File "$HOME\\.pm-qa-cockpit\\app\\dashboard.ps1"\n'
        'uv run python $HOME\\.pm-qa-cockpit\\app\\cockpit\\cli.py list\n', encoding="utf-8")
    (code / ".claude").mkdir()
    (code / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "uv run python ~/.pm-qa-cockpit/app/cockpit/cli.py list"}]}]}}, indent=2), encoding="utf-8")
    _register(home, ("p", code, None))
    return code


def make_skill(root: Path, name: str, body: str, manifest: dict | None = None, edited: bool = False) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    if manifest is not None:
        manifest = dict(manifest, sha256=migrate._hash_without_manifests(d))
        (d / migrate.OLD_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    if edited:
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\nMY OWN EDIT\n", encoding="utf-8")
    return d


# -- line rewriting ----------------------------------------------------------------------------


@pytest.mark.parametrize("old,new", [
    ('& "$HOME\\.pm-qa-cockpit\\app\\dashboard.ps1"', '& "$HOME\\.pm-qa-board\\app\\dashboard.ps1"'),
    ("uv run python $HOME\\.pm-qa-cockpit\\app\\cockpit\\cli.py list", "uv run python $HOME\\.pm-qa-board\\app\\board\\cli.py list"),
    ('"command": "uv run python C:\\\\Users\\\\a\\\\.pm-qa-cockpit\\\\app\\\\cockpit\\\\cli.py list"',
     '"command": "uv run python C:\\\\Users\\\\a\\\\.pm-qa-board\\\\app\\\\board\\\\cli.py list"'),
    ("python ~/.pm-qa-cockpit/app/cockpit/server.py", "python ~/.pm-qa-board/app/board/server.py"),
    ("$env:PM_QA_COCKPIT_PORT = 3333", "$env:PM_QA_BOARD_PORT = 3333"),
    # case differences on Windows paths
    ("python C:\\Users\\a\\.PM-QA-COCKPIT\\app\\Cockpit\\cli.py", "python C:\\Users\\a\\.pm-qa-board\\app\\board\\cli.py"),
])
def test_repoint_line_forms(old, new):
    assert migrate.repoint_line(old) == new


@pytest.mark.parametrize("line", [
    "uv run python C:/src/my-app/board/cli.py list",
    "echo hello",
    "cd webapp/cockpit/ && npm test",            # a project's own folder that happens to share the old name
    "import './src/app/cockpit/index.js'",
    "dev clone at ../pm-qa-cockpit/board/cli.py",  # the user's own clone folder is not the install path
])
def test_repoint_line_never_touches_unrelated_paths(line):
    assert migrate.repoint_line(line) is None


# -- plan is read-only -------------------------------------------------------------------------


def test_plan_lists_everything_and_changes_nothing(home, tmp_path):
    code = make_old_install(home, tmp_path)
    make_skill(home / ".claude" / "skills", "pm-sync", "sync", {"source": "pm-qa-cockpit", "version": "0.1.0"})
    before = (_tree(home), _tree(code))
    p = migrate.plan()
    assert (_tree(home), _tree(code)) == before
    assert p["state"] == "ready"
    assert {Path(m["src"]).name for m in p["moves"]} == {"projects.json", "cache", "plans", "bin"}
    assert p["clone"]["dest"].endswith(str(Path(NEW) / "app"))
    assert {e["file"] for e in p["launchers"]} == {"scripts/dash.ps1", ".claude/settings.json"}
    assert [s["action"] for s in p["skills"]] == ["convert-manifest"]
    assert not (home / NEW).exists()


def test_nothing_to_do_without_an_old_install(home):
    assert migrate.plan()["state"] == "nothing-to-do"
    assert migrate.legacy_state()["needs_migration"] is False


# -- apply -------------------------------------------------------------------------------------


def test_apply_moves_clones_and_repoints_but_keeps_the_old_folder(home, tmp_path):
    code = make_old_install(home, tmp_path)
    old_before = _tree(home / OLD)
    res = migrate.apply()
    assert not res["failed"], res
    new = home / NEW
    assert (new / "projects.json").is_file() and (new / "cache" / "x.db").read_bytes() == b"cache"
    assert (new / "bin" / "cloudflared.exe").is_file() and (new / "plans" / "p.json").is_file()
    assert (new / "app" / "board" / "server.py").is_file()
    assert _tree(home / OLD) == old_before  # the old folder is untouched (it holds running code)
    dash = (code / "scripts" / "dash.ps1").read_text(encoding="utf-8")
    assert ".pm-qa-cockpit" not in dash and "app\\board\\cli.py" in dash and ".pm-qa-board\\app\\dashboard.ps1" in dash
    hook = json.loads((code / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "~/.pm-qa-board/app/board/cli.py list" in hook["hooks"]["Stop"][0]["hooks"][0]["command"]
    backups = list((new / "migration-backup").rglob("dash.ps1"))
    assert backups and ".pm-qa-cockpit" in backups[0].read_text(encoding="utf-8")
    assert res["old_folder_remains"] is True


def test_apply_is_idempotent(home, tmp_path):
    make_old_install(home, tmp_path)
    assert not migrate.apply()["failed"]
    assert migrate.plan()["state"] == "already-migrated"
    again = migrate.apply()
    assert again["state_before"] == "already-migrated" and again["done"] == [] and again["failed"] == []


def test_blocked_while_a_dashboard_runs(home, tmp_path, monkeypatch):
    code = make_old_install(home, tmp_path)
    monkeypatch.setattr(migrate, "dashboards_running", lambda: [4242])
    p = migrate.plan()
    assert p["state"] == "blocked" and "4242" in p["blockers"][0]
    snapshot = (_tree(home), _tree(code))
    res = migrate.apply()
    assert res["failed"] and not res["done"]
    assert (_tree(home), _tree(code)) == snapshot and not (home / NEW).exists()


def test_failed_clone_leaves_launchers_alone(home, tmp_path, monkeypatch):
    code = make_old_install(home, tmp_path)

    def boom(url, dest):
        raise RuntimeError("no network")
    monkeypatch.setattr(migrate, "_clone", boom)
    res = migrate.apply()
    parts = {f["part"] for f in res["failed"]}
    assert "clone" in parts and "launchers" in parts
    assert ".pm-qa-cockpit" in (code / "scripts" / "dash.ps1").read_text(encoding="utf-8")


def test_never_overwrites_files_already_in_the_new_home(home, tmp_path):
    make_old_install(home, tmp_path)
    (home / NEW).mkdir()
    (home / NEW / "projects.json").write_text('{"projects": {}, "mine": true}', encoding="utf-8")
    assert not migrate.apply()["failed"]
    assert json.loads((home / NEW / "projects.json").read_text(encoding="utf-8"))["mine"] is True


def test_skip_parts(home, tmp_path):
    code = make_old_install(home, tmp_path)
    res = migrate.apply(skip=("launchers", "files", "agents"))
    assert not res["failed"]
    assert ".pm-qa-cockpit" in (code / "scripts" / "dash.ps1").read_text(encoding="utf-8")
    assert (home / NEW / "projects.json").is_file()


# -- a user who followed the new README before migrating ------------------------------------------


def test_cloning_the_new_copy_first_still_needs_migration_for_the_registry(home, tmp_path):
    make_old_install(home, tmp_path)
    _fake_clone("u", str(home / NEW / "app"))  # new running copy exists, old registry and tools do not move yet
    assert migrate.legacy_state()["needs_migration"] is True
    res = migrate.apply()
    assert not res["failed"] and (home / NEW / "projects.json").is_file()
    assert migrate.legacy_state()["needs_migration"] is False


# -- managed blocks and agent files written by the old setup --------------------------------------


def _block(inner: str) -> str:
    return f"<!-- pm-qa-cockpit:start -->\n{inner}\n<!-- pm-qa-cockpit:end -->"


def test_managed_blocks_are_rewritten_only_between_the_markers(home, tmp_path):
    code = make_old_install(home, tmp_path)
    docs = tmp_path / "docs-repo"
    docs.mkdir()
    _register(home, ("p", code, docs))
    (code / "CLAUDE.md").write_text(
        "# My rules\nI like the cockpit metaphor and my cockpit-notes.md file.\n"
        + _block("Dashboard: ~/.pm-qa-cockpit/app/dashboard.ps1\nSkill: pm-qa-cockpit (the PM & QA Cockpit)\nDocs: ../pm-qa-docs/pm-qa-cockpit/")
        + "\nMore of my text about cockpit.\n", encoding="utf-8")
    (docs / "ORCHESTRATION.md").write_text(
        "Intro\n<!-- pm-qa-cockpit:roster:start -->\n| lead | cockpit-lead |\n<!-- pm-qa-cockpit:roster:end -->\n", encoding="utf-8")
    assert not migrate.apply()["failed"]
    text = (code / "CLAUDE.md").read_text(encoding="utf-8")
    assert "<!-- pm-qa-board:start -->" in text and "<!-- pm-qa-board:end -->" in text
    assert "~/.pm-qa-board/app/dashboard.ps1" in text and "Skill: pm-qa-board (the PM & QA Board)" in text
    assert "../pm-qa-docs/pm-qa-cockpit/" in text  # the private docs folder keeps its name
    assert "I like the cockpit metaphor and my cockpit-notes.md file." in text and "More of my text about cockpit." in text
    roster = (docs / "ORCHESTRATION.md").read_text(encoding="utf-8")
    assert "<!-- pm-qa-board:roster:start -->" in roster and "board-lead" in roster and "Intro" in roster


def test_old_agent_files_are_renamed_and_never_overwrite(home, tmp_path):
    code = make_old_install(home, tmp_path)
    agents = code / ".claude" / "agents"
    agents.mkdir()
    (agents / "cockpit-reviewer.md").write_text("---\nname: cockpit-reviewer\nmodel: claude-opus-5-5\n---\nReview for the cockpit.\n", encoding="utf-8")
    (agents / "cockpit-builder.md").write_text("---\nname: cockpit-builder\n---\nbuild\n", encoding="utf-8")
    (agents / "board-builder.md").write_text("mine already\n", encoding="utf-8")
    res = migrate.apply()
    assert (agents / "board-reviewer.md").is_file() and not (agents / "cockpit-reviewer.md").exists()
    rev = (agents / "board-reviewer.md").read_text(encoding="utf-8")
    assert "name: board-reviewer" in rev and "model: claude-opus-5-5" in rev
    assert (agents / "board-builder.md").read_text(encoding="utf-8") == "mine already\n"  # not overwritten
    assert (agents / "cockpit-builder.md").is_file()  # left in place, reported
    assert any(f["part"] == "agents" and "already exists" in f["error"] for f in res["failed"])


# -- skills ------------------------------------------------------------------------------------


def test_skills_manifest_converted_old_skill_removed_only_when_unedited_and_replaced(home, tmp_path):
    make_old_install(home, tmp_path)
    g = home / ".claude" / "skills"
    sync = make_skill(g, "pm-sync", "sync", {"source": "pm-qa-cockpit", "version": "0.1.0"})
    body = (sync / "SKILL.md").read_bytes()
    unedited = make_skill(g, migrate.OLD_SKILL, "the old skill", {"source": "pm-qa-cockpit", "version": "0.1.0"})
    make_skill(g, "pm-qa-board", "the replacement")  # installed by pm-qa-setup
    proj_skills = tmp_path / "proj" / ".claude" / "skills"
    edited = make_skill(proj_skills, migrate.OLD_SKILL, "the old skill", {"source": "pm-qa-cockpit", "version": "0.1.0"}, edited=True)
    unreplaced = make_skill(tmp_path / "proj" / ".agents" / "skills", migrate.OLD_SKILL, "the old skill", {"source": "pm-qa-cockpit", "version": "0.1.0"})
    actions = {s["path"]: s["action"] for s in migrate.plan()["skills"]}
    assert actions[str(unedited)] == "remove-old-skill" and actions[str(edited)] == "leave-edited"
    assert actions[str(unreplaced)] == "keep-until-replaced"
    res = migrate.apply()
    assert not res["failed"], res
    m = json.loads((sync / setup_flow.MANIFEST).read_text(encoding="utf-8"))
    assert m["source"] == setup_flow.SOURCE and not (sync / migrate.OLD_MANIFEST).exists()
    assert (sync / "SKILL.md").read_bytes() == body
    assert not unedited.exists()  # unedited and its replacement is installed beside it: removed
    assert unreplaced.is_dir()    # no replacement yet: kept
    assert edited.is_dir() and "MY OWN EDIT" in (edited / "SKILL.md").read_text(encoding="utf-8")


# -- other files the plan must find, or hand back ----------------------------------------------


def test_utf16_launchers_are_reported_for_manual_edit_not_touched(home, tmp_path):
    code = make_old_install(home, tmp_path)
    f = code / "scripts" / "wide.ps1"
    raw = '& "$HOME\\.pm-qa-cockpit\\app\\dashboard.ps1"\r\n'.encode("utf-16")
    f.write_bytes(raw)
    p = migrate.plan()
    assert str(f) in p["manual"] and all(e["file"] != "scripts/wide.ps1" for e in p["launchers"])
    assert not migrate.apply()["failed"]
    assert f.read_bytes() == raw


def test_user_level_hooks_are_repointed(home, tmp_path):
    make_old_install(home, tmp_path)
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python ~/.pm-qa-cockpit/app/cockpit/cli.py list"}]}]}}', encoding="utf-8")
    assert not migrate.apply()["failed"]
    assert "~/.pm-qa-board/app/board/cli.py" in settings.read_text(encoding="utf-8")


def test_two_projects_with_the_same_folder_name_get_separate_backups(home, tmp_path):
    a, b = tmp_path / "a" / "app", tmp_path / "b" / "app"
    for c in (a, b):
        (c / "scripts").mkdir(parents=True)
        (c / "scripts" / "dash.ps1").write_text('& "$HOME\\.pm-qa-cockpit\\app\\dashboard.ps1"\n', encoding="utf-8")
    _register(home, ("a", a, None), ("b", b, None))
    assert not migrate.apply()["failed"]
    assert len(list((home / NEW / "migration-backup").rglob("dash.ps1"))) == 2


# -- the two Windows behaviours that must run for real, not through a stub ----------------------


@win
def test_dashboards_running_does_not_find_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_QA_USER_HOME", str(tmp_path / "home"))
    assert migrate.dashboards_running() == []


@win
def test_dashboards_running_finds_a_real_dashboard_process(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_QA_USER_HOME", str(tmp_path / "home"))
    app = tmp_path / "home" / OLD / "app" / "board"
    app.mkdir(parents=True)
    (app / "server.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(app / "server.py")])
    try:
        for _ in range(20):
            found = migrate.dashboards_running()
            if proc.pid in found:
                break
            import time
            time.sleep(0.5)
        assert proc.pid in found
    finally:  # kill the whole tree: the venv launcher stub would otherwise leave its child running
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


@win
def test_dashboards_running_only_matches_this_homes_old_folder(tmp_path, monkeypatch):
    other = tmp_path / "somebody-else" / OLD / "app" / "board"
    other.mkdir(parents=True)
    (other / "server.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setenv("PM_QA_USER_HOME", str(tmp_path / "home"))
    proc = subprocess.Popen([sys.executable, str(other / "server.py")])
    try:
        import time
        time.sleep(2)
        assert proc.pid not in migrate.dashboards_running()
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


@win
def test_repoint_shortcut_really_rewrites_a_lnk_even_with_spaces_and_quotes_in_its_name(tmp_path):
    lnk = tmp_path / "PM & QA 'Board' x.lnk"
    mk = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:L);"
          "$s.TargetPath='C:\\Windows\\System32\\cmd.exe';$s.Arguments='/c C:\\Users\\x\\.pm-qa-cockpit\\app\\dashboard.ps1';$s.Save()")
    import os
    env = {**os.environ, "L": str(lnk)}
    subprocess.run(["powershell", "-NoProfile", "-Command", mk], check=True, env=env, capture_output=True)
    migrate._repoint_shortcut(str(lnk))
    rd = "(New-Object -ComObject WScript.Shell).CreateShortcut($env:L).Arguments"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", rd], check=True, env=env, capture_output=True, text=True).stdout.strip()
    assert out == "/c C:\\Users\\x\\.pm-qa-board\\app\\dashboard.ps1"


def test_shortcuts_repointed_through_one_call(home, tmp_path, monkeypatch):
    make_old_install(home, tmp_path)
    seen = []
    monkeypatch.setattr(migrate, "scan_shortcuts", lambda: ["C:/Users/x/Desktop/PM & QA Board.lnk"])
    monkeypatch.setattr(migrate, "_repoint_shortcut", lambda lnk: seen.append(lnk))
    assert not migrate.apply()["failed"]
    assert seen == ["C:/Users/x/Desktop/PM & QA Board.lnk"]


# -- CLI ---------------------------------------------------------------------------------------


def _args(**kw):
    return argparse.Namespace(**{"apply": False, "skip": "", "json": False, **kw})


def test_cli_dry_run_is_default_and_prints_the_edits(home, tmp_path, capsys):
    code = make_old_install(home, tmp_path)
    before = _tree(code)
    assert migrate.cmd_migrate_home(_args()) == 0
    out = capsys.readouterr().out
    assert "State: ready" in out and "Nothing was changed" in out and ".pm-qa-board" in out
    assert _tree(code) == before and not (home / NEW).exists()


def test_cli_rejects_unknown_parts(home, capsys):
    assert migrate.cmd_migrate_home(_args(skip="launchers,bogus")) == 2


def test_cli_apply_then_message_to_delete_old_folder(home, tmp_path, capsys):
    make_old_install(home, tmp_path)
    assert migrate.cmd_migrate_home(_args(apply=True)) == 0
    assert "delete the old one" in capsys.readouterr().out
