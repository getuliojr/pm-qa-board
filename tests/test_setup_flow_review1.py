"""PQC-2 review round 1 regressions: nothing the user wrote may be replaced, rewritten or deleted
unless they approved exactly that."""

from __future__ import annotations

import json
import shutil

import pytest
from conftest import init_repo
from test_setup_flow import SKILLS, fake_globals  # noqa: F401  (fixture re-export)

import cli
import projects
import setup_flow


def _ps_style_install(src, dest, version="0.1.0", bom=True, sha=None):
    """What an older install.ps1 left behind: a copy plus a manifest (BOM, maybe no sha256)."""
    shutil.copytree(src, dest)
    manifest = {"source": "pm-qa-cockpit", "version": version}
    if sha:
        manifest["sha256"] = sha
    data = json.dumps(manifest).encode("utf-8")
    (dest / setup_flow.MANIFEST).write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)


def test_bom_manifest_is_read(tmp_path):
    dest = tmp_path / "pm-qa-setup"
    _ps_style_install(SKILLS / "pm-qa-setup", dest, version="0.2.0")
    assert setup_flow.read_manifest(dest)["version"] == "0.2.0"


def test_edited_copy_without_recorded_hash_is_never_outdated(tmp_path):
    """#1: an edited copy installed without a hash must ask (edited), not silently upgrade (outdated)."""
    dest = tmp_path / "pm-qa-setup"
    _ps_style_install(SKILLS / "pm-qa-setup", dest, version="0.1.0")
    (dest / "SKILL.md").write_text((dest / "SKILL.md").read_text(encoding="utf-8") + "\nMine.\n", encoding="utf-8")
    assert setup_flow.skill_state(dest, SKILLS / "pm-qa-setup")["state"] == "edited"
    assert setup_flow.install_skill(SKILLS / "pm-qa-setup", dest) == "skipped-edited"
    assert "Mine." in (dest / "SKILL.md").read_text(encoding="utf-8")


def test_unedited_copy_with_hash_upgrades(tmp_path):
    src = tmp_path / "src" / "pm-sync"
    shutil.copytree(SKILLS / "pm-sync", src)
    dest = tmp_path / "pm-sync"
    _ps_style_install(src, dest, version="0.2.0", sha=setup_flow.content_hash(src))
    (src / "SKILL.md").write_text((src / "SKILL.md").read_text(encoding="utf-8") + "\nShipped change.\n", encoding="utf-8")
    assert setup_flow.skill_state(dest, src)["state"] == "outdated"


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
def test_pointer_append_keeps_every_existing_byte(tmp_path, eol):
    """#2: appending must not convert the user's line endings."""
    f = tmp_path / "CLAUDE.md"
    original = b"# Rules" + eol + eol + b"Keep it short." + eol
    f.write_bytes(original)
    setup_flow.append_bytes(f, "<!-- pm-qa-cockpit:start -->\nnote\n<!-- pm-qa-cockpit:end -->\n")
    data = f.read_bytes()
    assert data.startswith(original)
    tail = data[len(original):]
    assert tail == eol + b"<!-- pm-qa-cockpit:start -->" + eol + b"note" + eol + b"<!-- pm-qa-cockpit:end -->" + eol
    if eol == b"\n":
        assert b"\r" not in data


def test_non_utf8_target_fails_before_anything_is_written(cockpit_home, fake_globals, tmp_path):  # noqa: F811
    """#2b: a cp1252 CLAUDE.md must not leave a half-applied setup."""
    code = init_repo(tmp_path / "app")
    (code / ".claude").mkdir()
    (code / "CLAUDE.md").write_bytes("# Règles\n".encode("cp1252"))
    plan = setup_flow.build_plan(code, name="App", prefix="APP")
    ids = [a["id"] for a in plan["actions"] if a["kind"] in ("install-skill", "pointer")]
    with pytest.raises(ValueError, match="not UTF-8"):
        setup_flow.apply_actions(plan, ids)
    assert not (code / ".claude" / "skills").exists()


def test_guard_catches_changed_paths_under_a_constant_title(cockpit_home, fake_globals, tmp_path, capsys):  # noqa: F811
    """#3: cleanup's title never changes; a new global folder must still block the apply."""
    code = init_repo(tmp_path / "p")
    shutil.copytree(SKILLS / "pm-sync", fake_globals["claude"] / "pm-sync")
    assert cli.main(["setup-plan", str(code), "--name", "P", "--prefix", "PP"]) == 0
    seen = json.loads(cli._plan_file(str(code)).read_text(encoding="utf-8"))
    cleanup = next(a for a in seen["actions"] if a["kind"] == "cleanup-global")
    shutil.copytree(SKILLS / "pm-review", fake_globals["antigravity"] / "pm-review")  # appears after "show"
    assert cli.main(["setup-apply", str(code), cleanup["id"]]) == 1
    assert "plan changed" in capsys.readouterr().out
    assert (fake_globals["claude"] / "pm-sync").exists() and (fake_globals["antigravity"] / "pm-review").exists()


def test_cleanup_never_deletes_edited_or_uncertain_copies(cockpit_home, fake_globals, tmp_path):  # noqa: F811
    """#4: only provably ours + unedited copies are offered; the rest become notes."""
    code = init_repo(tmp_path / "p")
    edited = fake_globals["claude"] / "pm-sync"
    setup_flow.install_skill(SKILLS / "pm-sync", edited)
    (edited / "SKILL.md").write_text((edited / "SKILL.md").read_text(encoding="utf-8") + "\nMine\n", encoding="utf-8")
    maybe = fake_globals["claude"] / "pm-review"
    maybe.mkdir()
    (maybe / "SKILL.md").write_text("---\nname: pm-review\n---\nMy review flow for the cockpit.\n", encoding="utf-8")
    ok = fake_globals["antigravity"] / "pm-orchestrate"
    setup_flow.install_skill(SKILLS / "pm-orchestrate", ok)

    plan = setup_flow.build_plan(code, name="P", prefix="PP")
    (cleanup,) = [a for a in plan["actions"] if a["kind"] == "cleanup-global"]
    assert cleanup["paths"] == [str(ok)]
    assert any(str(edited) in n and "edited" in n for n in plan["notes"])
    assert any(str(maybe) in n and "not provably ours" in n for n in plan["notes"])


def test_cleanup_names_projects_still_relying_on_globals(cockpit_home, fake_globals, tmp_path):  # noqa: F811
    other = init_repo(tmp_path / "other")
    projects.upsert_project(projects.build_project("Other", other, prefix="OTH"))
    setup_flow.install_skill(SKILLS / "pm-sync", fake_globals["claude"] / "pm-sync")
    plan = setup_flow.build_plan(init_repo(tmp_path / "p"), name="P", prefix="PP")
    (cleanup,) = [a for a in plan["actions"] if a["kind"] == "cleanup-global"]
    assert "other" in cleanup["detail"]


def test_per_action_conflict_choice(cockpit_home, fake_globals, tmp_path, capsys):  # noqa: F811
    """#5: keep one edited skill and replace another in the same apply."""
    code = init_repo(tmp_path / "app")
    sk = code / ".claude" / "skills"
    for s in ("pm-sync", "pm-review"):
        setup_flow.install_skill(SKILLS / s, sk / s)
        (sk / s / "SKILL.md").write_text((sk / s / "SKILL.md").read_text(encoding="utf-8") + "\nMine\n", encoding="utf-8")
    assert cli.main(["setup-plan", str(code), "--name", "App", "--prefix", "APP"]) == 0
    plan = json.loads(cli._plan_file(str(code)).read_text(encoding="utf-8"))
    ids = {a["skill"]: a["id"] for a in plan["actions"] if a["kind"] == "install-skill" and a["state"] == "edited"}
    assert cli.main(["setup-apply", str(code), ids["pm-sync"], f"{ids['pm-review']}=replace"]) == 0
    assert "Mine" in (sk / "pm-sync" / "SKILL.md").read_text(encoding="utf-8")
    assert "Mine" not in (sk / "pm-review" / "SKILL.md").read_text(encoding="utf-8")
    assert cli.main(["setup-apply", str(code), "A1=bogus"]) == 1


def test_cursor_only_project_gets_claude_skills_folder(cockpit_home, fake_globals, tmp_path):  # noqa: F811
    """#7: Cursor reads .claude/skills; a Cursor-only project must not end up with no skills."""
    code = init_repo(tmp_path / "c")
    (code / ".cursor").mkdir()
    plan = setup_flow.build_plan(code, name="C", prefix="CC")
    dirs = {a["dir"] for a in plan["actions"] if a["kind"] == "install-skill"}
    assert dirs == {".claude/skills"}
    assert [a["file"] for a in plan["actions"] if a["kind"] == "pointer"] == ["AGENTS.md"]


def test_profile_gates_command_names_the_project(cockpit_home, fake_globals, tmp_path):  # noqa: F811
    """#8"""
    plan = setup_flow.build_plan(init_repo(tmp_path / "g"), name="G", prefix="GG")
    (prof,) = [a for a in plan["actions"] if a["kind"] == "profile-gates"]
    assert "--project g " in prof["detail"]


def test_full_flag_overrides_register_only(cockpit_home, fake_globals, tmp_path):  # noqa: F811
    """#9"""
    code = init_repo(tmp_path / "mn")
    (code / ".claude" / "skills" / "x-orchestrate").mkdir(parents=True)
    (code / ".claude" / "skills" / "x-orchestrate" / "SKILL.md").write_text("x", encoding="utf-8")
    assert setup_flow.build_plan(code, name="M", prefix="MM")["mode"] == "register-only"
    forced = setup_flow.build_plan(code, name="M", prefix="MM", force_full=True)
    assert forced["mode"] == "full" and any(a["kind"] == "install-skill" for a in forced["actions"])


def test_exclude_works_in_a_worktree(cockpit_home, fake_globals, tmp_path):  # noqa: F811
    """#11: .git may be a file; the exclude goes to the right info/exclude."""
    from conftest import git

    main = init_repo(tmp_path / "main")
    (main / "f.txt").write_text("x", encoding="utf-8")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "i")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", str(wt))
    (wt / ".claude").mkdir()
    plan = setup_flow.build_plan(wt, name="W", prefix="WW", docs_path=tmp_path / "docs-w")
    exclude = next(a for a in plan["actions"] if a["kind"] == "exclude")
    setup_flow.apply_actions(plan, [exclude["id"]])
    target = setup_flow.exclude_file(wt)
    assert target and ".claude/skills/pm-sync/" in target.read_text(encoding="utf-8")
