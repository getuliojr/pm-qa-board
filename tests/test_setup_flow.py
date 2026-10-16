"""PQC-2: pm-qa-setup engine — never overwrite, EOL-insensitive, exact excludes, register-only, approvals."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import git, init_repo

import cli
import projects
import setup_flow

SKILLS = setup_flow.TOOL_ROOT / "skills"


@pytest.fixture
def fake_globals(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.claude, ~/.agents, ~/.cursor."""
    roots = {k: tmp_path / "home" / v for k, v in
             {"claude": ".claude/skills", "antigravity": ".agents/skills", "cursor": ".cursor/skills-cursor"}.items()}
    for r in roots.values():
        r.mkdir(parents=True)
    (roots["claude"] / "grill-me").mkdir()
    (roots["claude"] / "grill-me" / "SKILL.md").write_text("---\nname: grill-me\n---\n", encoding="utf-8")
    monkeypatch.setattr(setup_flow, "GLOBAL_ROOTS", roots)
    return roots


def crlf_copy(src: Path, dest: Path) -> None:
    shutil.copytree(src, dest)
    for f in dest.rglob("*"):
        if f.is_file():
            f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


# ---------------------------------------------------------------- versions / states


def test_crlf_copy_is_current_not_changed(tmp_path):
    dest = tmp_path / "pm-sync"
    crlf_copy(SKILLS / "pm-sync", dest)
    assert setup_flow.skill_state(dest, SKILLS / "pm-sync")["state"] == "current"


def test_every_shipped_skill_carries_version_and_source():
    version = setup_flow.tool_version()
    for d in SKILLS.iterdir():
        meta = setup_flow.skill_metadata(d / "SKILL.md")
        assert meta == {"version": version, "source": "pm-qa-cockpit"}, d.name


def test_edited_and_foreign_are_never_overwritten(tmp_path):
    dest = tmp_path / "pm-review"
    assert setup_flow.install_skill(SKILLS / "pm-review", dest) == "installed"
    (dest / "SKILL.md").write_text((dest / "SKILL.md").read_text(encoding="utf-8") + "\nMy own rule.\n", encoding="utf-8")
    assert setup_flow.skill_state(dest, SKILLS / "pm-review")["state"] == "edited"
    assert setup_flow.install_skill(SKILLS / "pm-review", dest) == "skipped-edited"
    assert "My own rule." in (dest / "SKILL.md").read_text(encoding="utf-8")
    new_dir = setup_flow.new_copy_dir(dest)
    assert setup_flow.install_skill(SKILLS / "pm-review", dest, on_conflict="new") == f"wrote-new:{new_dir}"
    assert "My own rule." in (dest / "SKILL.md").read_text(encoding="utf-8")
    assert (new_dir / "SKILL.md").is_file() and new_dir.parent.name.startswith(".")
    assert setup_flow.skill_state(dest, SKILLS / "pm-review")["state"] == "edited"  # .new lives outside

    foreign = tmp_path / "pm-sync"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("---\nname: pm-sync\ndescription: someone else's\n---\nmine\n", encoding="utf-8")
    assert setup_flow.skill_state(foreign, SKILLS / "pm-sync")["state"] == "foreign"
    assert setup_flow.install_skill(SKILLS / "pm-sync", foreign, on_conflict="replace") == "skipped-foreign"
    assert "mine" in (foreign / "SKILL.md").read_text(encoding="utf-8")


def test_unedited_older_version_is_outdated_and_upgrades(tmp_path):
    src = tmp_path / "src" / "pm-sync"
    shutil.copytree(SKILLS / "pm-sync", src)
    dest = tmp_path / "installed" / "pm-sync"
    setup_flow.install_skill(src, dest)
    md = src / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8").replace("version: 0.2.0", "version: 0.3.0") + "\nNew rule.\n",
                  encoding="utf-8")
    assert setup_flow.skill_state(dest, src)["state"] == "outdated"
    assert setup_flow.install_skill(src, dest) == "upgraded"
    assert "New rule." in (dest / "SKILL.md").read_text(encoding="utf-8")
    assert json.loads((dest / setup_flow.MANIFEST).read_text(encoding="utf-8"))["version"] == "0.3.0"


# ---------------------------------------------------------------- plans


def test_money_nest_shaped_project_is_register_only(cockpit_home, fake_globals, tmp_path):
    code = init_repo(tmp_path / "mn")
    (code / ".claude" / "skills" / "mn-orchestrate").mkdir(parents=True)
    (code / ".claude" / "skills" / "mn-orchestrate" / "SKILL.md").write_text("x", encoding="utf-8")
    (code / "CLAUDE.md").write_text("# rules\nNever push.\n", encoding="utf-8")
    (code / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (code / ".cursor").mkdir()
    plan = setup_flow.build_plan(code, name="MN", prefix="MN")
    assert plan["mode"] == "register-only"
    kinds = [a["kind"] for a in plan["actions"]]
    assert "install-skill" not in kinds and "exclude" not in kinds
    pointers = [a for a in plan["actions"] if a["kind"] == "pointer"]
    assert [p["file"] for p in pointers] == ["CLAUDE.md"]
    assert "This file's rules always win" in pointers[0]["preview"]


def test_full_setup_applies_only_approved_and_keeps_user_text(cockpit_home, fake_globals, tmp_path):
    code = init_repo(tmp_path / "app")
    (code / ".claude").mkdir()
    (code / ".cursor").mkdir()
    (code / "CLAUDE.md").write_text("# My rules\n\nAlways write tests.\n", encoding="utf-8")
    docs = tmp_path / "pm-docs" / "app"
    plan = setup_flow.build_plan(code, name="App", prefix="APP", docs_path=docs)
    assert plan["mode"] == "full" and plan["agents"] == ["claude", "cursor"]
    by_kind: dict[str, list] = {}
    for a in plan["actions"]:
        by_kind.setdefault(a["kind"], []).append(a)
    assert {a["skill"] for a in by_kind["install-skill"]} == set(setup_flow.PROJECT_SKILLS)
    assert all(a["dir"] == ".claude/skills" for a in by_kind["install-skill"]), "Cursor reads .claude/skills; no 2nd copy"
    assert sorted(a["file"] for a in by_kind["pointer"]) == ["AGENTS.md", "CLAUDE.md"]
    (exclude,) = by_kind["exclude"]
    assert exclude["paths"] == sorted(f".claude/skills/{s}/" for s in setup_flow.PROJECT_SKILLS)

    approved = [a["id"] for a in by_kind["install-skill"]] + [a["id"] for a in by_kind["pointer"] if a["file"] == "CLAUDE.md"]
    results = setup_flow.apply_actions(plan, approved + [exclude["id"]])
    assert all(r["result"] in ("installed", "written") or r["result"].startswith("excluded") for r in results)

    claude_md = (code / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude_md.startswith("# My rules\n\nAlways write tests.\n")
    assert setup_flow.POINTER_START in claude_md
    assert not (code / "AGENTS.md").exists(), "AGENTS.md pointer was not approved"
    for s in setup_flow.PROJECT_SKILLS:
        assert (code / ".claude" / "skills" / s / "SKILL.md").is_file()
    # the installed skills are excluded and the docs are external: git sees only the (untracked) CLAUDE.md
    assert git(code, "status", "--porcelain").strip() == "?? CLAUDE.md"

    again = setup_flow.build_plan(code, name="App", prefix="APP", docs_path=docs)
    assert [a["file"] for a in again["actions"] if a["kind"] == "pointer"] == ["AGENTS.md"]
    assert not [a for a in again["actions"] if a["kind"] in ("install-skill", "exclude")]


def test_legacy_global_copies_offered_for_cleanup(cockpit_home, fake_globals, tmp_path):
    code = init_repo(tmp_path / "p")
    legacy = fake_globals["claude"] / "pm-sync"
    shutil.copytree(SKILLS / "pm-sync", legacy)
    md = legacy / "SKILL.md"  # a pre-versioning copy: no metadata block
    md.write_text(md.read_text(encoding="utf-8").replace("metadata:\n  version: 0.2.0\n  source: pm-qa-cockpit\n", ""),
                  encoding="utf-8")
    (fake_globals["claude"] / "pm-review").mkdir()
    (fake_globals["claude"] / "pm-review" / "SKILL.md").write_text("---\nname: pm-review\n---\nunrelated\n", encoding="utf-8")
    plan = setup_flow.build_plan(code, name="P", prefix="PP")
    (cleanup,) = [a for a in plan["actions"] if a["kind"] == "cleanup-global"]
    assert cleanup["paths"] == [str(legacy)], "a same-named skill that isn't ours is never offered"
    setup_flow.apply_actions(plan, [cleanup["id"]])
    assert not legacy.exists() and (fake_globals["claude"] / "pm-review").exists()
    assert (fake_globals["claude"] / "grill-me").exists()


def test_setup_apply_refuses_when_plan_changed(cockpit_home, fake_globals, tmp_path, capsys):
    code = init_repo(tmp_path / "app")
    (code / ".claude").mkdir()
    assert cli.main(["setup-plan", str(code), "--name", "App", "--prefix", "APP"]) == 0
    first = json.loads(cli._plan_file(str(code)).read_text(encoding="utf-8"))
    first_skill = next(a for a in first["actions"] if a["kind"] == "install-skill")
    # the project changes between "show" and "apply": the user writes their own pm-qa-cockpit skill
    own = code / ".claude" / "skills" / "pm-qa-cockpit"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("---\nname: pm-qa-cockpit\n---\nmine\n", encoding="utf-8")
    assert cli.main(["setup-apply", str(code), first_skill["id"]]) == 1
    assert "plan changed" in capsys.readouterr().out
    assert (own / "SKILL.md").read_text(encoding="utf-8").endswith("mine\n")


def test_setup_apply_without_plan_refuses(cockpit_home, fake_globals, tmp_path, capsys):
    code = init_repo(tmp_path / "x")
    assert cli.main(["setup-apply", str(code), "A1"]) == 1
    assert "run `cli.py setup-plan` first" in capsys.readouterr().out


# ---------------------------------------------------------------- bootstrap installer


def _run_install(home: Path, *extra: str) -> str:
    res = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(setup_flow.TOOL_ROOT / "install.ps1"), "-TargetHome", str(home), *extra],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell not available")
def test_bootstrap_installs_only_setup_and_never_overwrites(tmp_path):
    home = tmp_path / "home"
    out = _run_install(home, "-DryRun")
    assert "would install" in out and not (home / ".claude").exists()

    _run_install(home)
    claude = home / ".claude" / "skills"
    assert sorted(p.name for p in claude.iterdir()) == ["pm-qa-setup"]
    assert setup_flow.skill_state(claude / "pm-qa-setup", SKILLS / "pm-qa-setup")["state"] == "current"

    # CRLF re-checkout: still current
    md = claude / "pm-qa-setup" / "SKILL.md"
    md.write_bytes(md.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert "[current" in _run_install(home)

    # a user's edit is reported and left alone
    md.write_text(md.read_text(encoding="utf-8") + "\nMy note.\n", encoding="utf-8")
    out = _run_install(home)
    assert "left untouched" in out
    assert "My note." in md.read_text(encoding="utf-8")
