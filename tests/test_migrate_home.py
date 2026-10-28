"""PQC-34: migrate-home moves an old install to PM & QA Board safely (temp homes only, never the real one)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import migrate
import setup_flow

OLD = ".pm-qa-cockpit"
NEW = ".pm-qa-board"


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
    (old / "projects.json").write_text(json.dumps({"active_project": "p", "projects": {
        "p": {"name": "P", "slug": "p", "prefix": "P", "code_path": str(code), "docs_path": str(code / "docs"),
              "docs_mode": "in-repo", "gates": {"mode": "unprofiled"}}}}), encoding="utf-8")
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
])
def test_repoint_line_forms(old, new):
    assert migrate.repoint_line(old) == new


def test_repoint_line_leaves_other_lines_alone():
    assert migrate.repoint_line("uv run python C:/src/my-app/board/cli.py list") is None
    assert migrate.repoint_line("echo hello") is None


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
    res = migrate.apply(skip=("launchers",))
    assert not res["failed"]
    assert ".pm-qa-cockpit" in (code / "scripts" / "dash.ps1").read_text(encoding="utf-8")
    assert (home / NEW / "projects.json").is_file()


# -- skills ------------------------------------------------------------------------------------


def test_skills_manifest_converted_old_skill_removed_only_when_unedited(home, tmp_path):
    make_old_install(home, tmp_path)
    g = home / ".claude" / "skills"
    sync = make_skill(g, "pm-sync", "sync", {"source": "pm-qa-cockpit", "version": "0.1.0"})
    body = (sync / "SKILL.md").read_bytes()
    unedited = make_skill(g, migrate.OLD_SKILL, "the old skill", {"source": "pm-qa-cockpit", "version": "0.1.0"})
    edited_code = tmp_path / "proj" / ".claude" / "skills"
    edited = make_skill(edited_code, migrate.OLD_SKILL, "the old skill", {"source": "pm-qa-cockpit", "version": "0.1.0"}, edited=True)
    res = migrate.apply()
    assert not res["failed"], res
    # converted: new manifest name and source, old manifest gone, skill text untouched
    m = json.loads((sync / setup_flow.MANIFEST).read_text(encoding="utf-8"))
    assert m["source"] == setup_flow.SOURCE and not (sync / migrate.OLD_MANIFEST).exists()
    assert (sync / "SKILL.md").read_bytes() == body
    assert not unedited.exists()  # provably unedited: removed
    assert edited.is_dir() and "MY OWN EDIT" in (edited / "SKILL.md").read_text(encoding="utf-8")  # edited: kept


# -- shortcuts and CLI -------------------------------------------------------------------------


def test_shortcuts_repointed_through_one_call(home, tmp_path, monkeypatch):
    make_old_install(home, tmp_path)
    seen = []
    monkeypatch.setattr(migrate, "scan_shortcuts", lambda: ["C:/Users/x/Desktop/PM & QA Board.lnk"])
    monkeypatch.setattr(migrate, "_repoint_shortcut", lambda lnk: seen.append(lnk))
    assert not migrate.apply()["failed"]
    assert seen == ["C:/Users/x/Desktop/PM & QA Board.lnk"]


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
