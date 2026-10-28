"""PQC-2.1: setup speaks the user's language - inventory, decisions, wiring and doctor on the
2026-10-16 sandbox (fake HOME + money-nest-shaped project)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import sandbox_fixture

import cli
import doctor
import migrate
import projects
import setup_flow
import wiring

ID_RE = re.compile(r"\bA\d+\b")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    def make(dev_first: bool = False) -> dict[str, Path]:
        paths = sandbox_fixture.build(tmp_path / "sbx", dev_first=dev_first, version=setup_flow.tool_version())
        monkeypatch.setenv("PM_QA_USER_HOME", str(paths["home"]))
        monkeypatch.delenv("PM_QA_BOARD_HOME", raising=False)
        return paths

    return make


def _group(plan, prefix):
    return next(g for g in plan["inventory"] if g["group"].startswith(prefix))


def _items(plan, prefix, what_prefix):
    return [i for i in _group(plan, prefix)["items"] if i["what"].startswith(what_prefix)]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


# ---------------------------------------------------------------- inventory


def test_inventory_says_grill_me_is_missing_for_claude_code_and_keeps_the_project_copy(sandbox):
    sb = sandbox()
    plan = setup_flow.build_plan(sb["project"])
    assert plan["tools"] == ["claude", "antigravity", "cursor"]
    (item,) = _items(plan, "Global folder: Claude Code", "grill-me")
    assert item["status"] == "change"
    assert "missing for Claude Code" in item["now"] and "only in this project" in item["now"]
    assert "~/.claude/skills" in item["belongs"] and "project copy stays" in item["belongs"]
    (copy,) = [a for a in plan["actions"] if a["kind"] == "copy-generic"]
    assert copy["tool"] == "claude" and copy["id"] in item["actions"]
    assert {c["skill"] for c in copy["copies"]} == {"grill-me", "grilling"}
    assert all(Path(c["from"]).parent == sb["project"] / ".claude" / "skills" for c in copy["copies"])
    # Antigravity has it globally; Cursor reads ~/.agents/skills too, so neither needs a copy
    assert _items(plan, "Global folder: Antigravity", "grill-me")[0]["status"] == "ok"
    assert _items(plan, "Global folder: Cursor", "grill-me")[0]["status"] == "ok"

    setup_flow.apply_actions(plan, [copy["id"]])
    assert (sb["home"] / ".claude" / "skills" / "grill-me" / "SKILL.md").is_file()
    assert (sb["project"] / ".claude" / "skills" / "grill-me" / "SKILL.md").is_file(), "copy only, never a move"


def test_older_shipped_pm_orchestrate_is_recognised_as_ours(sandbox):
    sb = sandbox()
    plan = setup_flow.build_plan(sb["project"])
    old = [i for g in plan["inventory"] if g["group"].startswith("Global folder")
           for i in g["items"] if i["what"] == "Old board skill pm-orchestrate"]
    assert len(old) == 3, "one per global folder"
    assert all(i["status"] == "change" and "release 0.1.0, unedited" in i["now"] and "safe to remove" in i["why"] for i in old)
    assert not [n for n in plan["notes"] if "not provably ours" in n or "edited" in n]
    (cleanup,) = [a for a in plan["actions"] if a["kind"] == "cleanup-global"]
    # three project skills x three global folders; the old-named skill is no longer a project skill
    # (migrate-home removes it)
    assert len(cleanup["paths"]) == 9
    (st,) = [a for a in plan["actions"] if a["kind"] == "upgrade-bootstrap" and a["agent"] == "claude"]
    assert st["state"] == "outdated" and st["detail"].startswith("0.2.0 ->")


def test_money_nest_launcher_reads_ok(sandbox):
    sb = sandbox()
    plan = setup_flow.build_plan(sb["project"])
    (item,) = _items(plan, "Launchers", "Launcher scripts/pm.ps1")
    assert item["status"] == "ok" and "running copy first" in item["now"]
    assert not [a for a in plan["actions"] if a["kind"] == "launcher-edit"]


def test_dev_clone_first_launcher_is_flagged_and_fixed_by_one_exact_line(sandbox):
    sb = sandbox(dev_first=True)
    pm = sb["project"] / "scripts" / "pm.ps1"
    pm.write_bytes(pm.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))  # a CRLF checkout
    before = pm.read_bytes()
    plan = setup_flow.build_plan(sb["project"])
    (item,) = _items(plan, "Launchers", "Launcher scripts/pm.ps1")
    assert item["status"] == "change" and "dev clone first" in item["now"]
    (edit,) = [a for a in plan["actions"] if a["kind"] == "launcher-edit"]
    assert edit["edit"]["old"] == sandbox_fixture.DEV_FIRST_LINE
    assert edit["edit"]["new"] == ('$BoardRepo = if (Test-Path "$HOME\\.pm-qa-board\\app\\board\\server.py") '
                                   '{ "$HOME\\.pm-qa-board\\app" }')
    assert "pm-qa-board\\board" in edit["preview"] and edit["preview"].startswith("- ")
    (d,) = [d for d in plan["decisions"] if d["theme"] == "wiring"]
    assert edit["id"] in d["actions"]

    setup_flow.apply_actions(plan, [edit["id"]])
    after = pm.read_bytes()
    assert after == before.replace(sandbox_fixture.DEV_FIRST_LINE.encode(), edit["edit"]["new"].encode())
    assert [lw["verdict"] for lw in wiring.scan_launchers(sb["project"]) if lw["file"] == "scripts/pm.ps1"] == ["ok"]


def test_launcher_edit_rechecks_the_line_before_writing(sandbox):
    sb = sandbox(dev_first=True)
    plan = setup_flow.build_plan(sb["project"])
    (edit,) = [a for a in plan["actions"] if a["kind"] == "launcher-edit"]
    pm = sb["project"] / "scripts" / "pm.ps1"
    pm.write_text(pm.read_text(encoding="utf-8").replace('{ "..\\pm-qa-board" }', '{ "..\\my-board" }'), encoding="utf-8")
    changed = pm.read_bytes()
    with pytest.raises(ValueError, match="changed since the plan"):
        setup_flow.apply_actions(plan, [edit["id"]])
    assert pm.read_bytes() == changed


def test_a_line_is_rewritten_whole_or_not_at_all():
    line = '$x = if (Test-Path "..\\pm-qa-board\\board\\server.py") { Join-Path $Root "pm-qa-board" }'
    refs = [r for r in wiring.classify_line(line) if r["kind"] == "dev"]
    assert len(refs) == 2 and refs[1]["segment"] is None  # the bare Join-Path name can't be rewritten


def test_relative_path_in_join_path_or_single_quotes_is_not_rewritten():
    joined = '$Dev = Join-Path $PSScriptRoot "..\\..\\pm-qa-board"'
    quoted = "$Dev = '..\\..\\pm-qa-board\\board'"
    plain = '$Dev = "..\\..\\pm-qa-board\\board"'
    for line, expect_rewrite in ((joined, False), (quoted, False), (plain, True)):
        refs = [r for r in wiring.classify_line(line) if r["kind"] == "dev" and r["segment"] is not None]
        assert refs, line
        assert (wiring.proposed_line(line, refs[0], ".ps1") is not None) == expect_rewrite, line


def test_docs_folder_named_like_the_tool_is_not_a_dev_clone(tmp_path):
    (tmp_path / "pm-qa-docs" / "pm-qa-board").mkdir(parents=True)
    code = tmp_path / "tool"
    code.mkdir()
    assert wiring.classify_line("see `../pm-qa-docs/pm-qa-board/PLAN.md`", [code]) == []
    assert wiring.classify_line('source = "pm-qa-board"; version = 1') == []


# ---------------------------------------------------------------- decisions


def test_first_reply_decisions_are_plain_and_capped(sandbox):
    sb = sandbox(dev_first=True)
    plan = setup_flow.build_plan(sb["project"])
    assert 1 <= len(plan["decisions"]) <= 4
    assert [d["theme"] for d in plan["decisions"]] == ["pm-data", "skills", "wiring"]  # fixed order, gates profiled
    for d in plan["decisions"]:
        texts = [d["title"], d["question"], d["recommendation"]] + [t for o in d["options"] for t in (o["label"], o["description"])]
        for t in texts:
            assert not ID_RE.search(t), f"an action id leaked into the user-facing text: {t!r}"
        assert d["options"][0]["label"].endswith("(recommended)")
    assert {a for d in plan["decisions"] for a in d["actions"]} == {a["id"] for a in plan["actions"]}
    assert not ID_RE.search(" ".join(plan["target"]))
    for g in plan["inventory"]:
        for i in g["items"]:
            assert not ID_RE.search(" ".join((i["what"], i["now"], i["belongs"], i["why"])))


def test_an_edited_skill_gets_its_own_question(sandbox):
    sb = sandbox()
    skill = sb["home"] / ".claude" / "skills" / "pm-qa-setup"
    # installed by the tool (so it carries an install manifest), then edited by the user
    (skill / setup_flow.MANIFEST).write_text(json.dumps({"source": setup_flow.SOURCE, "version": "0.2.0",
                                                         "sha256": setup_flow.content_hash(skill)}), encoding="utf-8")
    md = skill / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "\nMy own rule.\n", encoding="utf-8")
    plan = setup_flow.build_plan(sb["project"])
    (d,) = [d for d in plan["decisions"] if d["theme"] == "edited"]
    assert "pm-qa-setup" in d["question"] and [o["approve"][0].split("=")[-1] for o in d["options"]][0] == "new"
    skills = next(x for x in plan["decisions"] if x["theme"] == "skills")
    assert d["actions"][0] not in skills["actions"]


def test_setup_plan_text_leads_with_the_inventory(sandbox, capsys):
    sb = sandbox()
    assert cli.main(["setup-plan", str(sb["project"])]) == 0
    out = capsys.readouterr().out
    inv, target, decide, agent = (out.index(h) for h in ("== What you have", "== Recommended target", "== Decisions", "== For the agent only"))
    assert inv < target < decide < agent
    assert not ID_RE.search(out[:agent]), "ids appear only in the agent-only appendix"


# ---------------------------------------------------------------- doctor


def test_doctor_after_the_recommended_setup_is_all_pass(sandbox, capsys):
    sb = sandbox()
    plan = setup_flow.build_plan(sb["project"])
    approve = [i for d in plan["decisions"] for i in d["options"][0]["approve"]]
    setup_flow.apply_actions(plan, [i.split("=")[0] for i in approve])
    report = doctor.run(str(sb["project"]))
    assert [c["n"] for c in report["checks"]] == list(range(1, 12))
    # money-nest keeps its own roster (Story 2.2): its gaps and alias pins are reported, never fixed by setup
    assert report["counts"] == {"pass": 10, "warn": 1, "fail": 0}, [c for c in report["checks"] if c["status"] != "pass"]
    assert [c["n"] for c in report["checks"] if c["status"] != "pass"] == [10]
    assert cli.main(["doctor", "money-nest"]) == 0
    assert "10 pass, 1 warn, 0 fail" in capsys.readouterr().out
    assert not setup_flow.build_plan(sb["project"])["decisions"], "nothing left to decide"


def test_doctor_before_setup_warns_with_fix_hints_and_exits_0(sandbox):
    sb = sandbox()
    report = doctor.run(str(sb["project"]))
    assert report["ok"] and report["counts"]["fail"] == 0
    by = {c["n"]: c for c in report["checks"]}
    assert by[7]["status"] == "warn" and "outdated" in by[7]["detail"] and by[7]["fix"]
    assert by[6]["status"] == "pass" and "from this project only" in by[6]["detail"]
    assert by[4]["status"] == "pass" and "scripts/pm.ps1 calls the running copy first" in by[4]["detail"]
    assert by[5]["status"] == "pass" and "register-only" in by[5]["detail"]


def test_doctor_flags_a_dev_clone_first_launcher(sandbox):
    sb = sandbox(dev_first=True)
    (c4,) = [c for c in doctor.run(str(sb["project"]))["checks"] if c["n"] == 4]
    assert c4["status"] == "warn" and "dev clone first" in c4["detail"] and "exact line change" in c4["fix"]


def test_doctor_exits_1_on_a_fail(sandbox, capsys):
    sb = sandbox()
    (sb["board_home"] / "app" / "board" / "server.py").unlink()
    assert cli.main(["doctor", str(sb["project"])]) == 1
    out = capsys.readouterr().out
    assert "[fail] 3. Running copy" in out and "fix: git clone" in out
    assert cli.main(["doctor", str(sb["project"]), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["counts"]["fail"] >= 1


def test_doctor_on_an_unregistered_folder_fails(sandbox, tmp_path):
    sandbox()
    other = tmp_path / "other"
    other.mkdir()
    (c1,) = [c for c in doctor.run(str(other))["checks"] if c["n"] == 1]
    assert c1["status"] == "fail" and "pm-qa-setup" in c1["fix"]


def test_doctor_is_read_only(sandbox, tmp_path):
    sb = sandbox(dev_first=True)
    before = _snapshot(tmp_path / "sbx")
    doctor.run(str(sb["project"]))
    doctor.run("money-nest")
    assert _snapshot(tmp_path / "sbx") == before


def test_doctor_warns_on_a_hook_calling_the_dev_clone(sandbox):
    sb = sandbox()
    settings = sb["project"] / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "uv run python C:/src/pm-qa-board/board/cli.py list"}]}]}}), encoding="utf-8")
    (c9,) = [c for c in doctor.run(str(sb["project"]))["checks"] if c["n"] == 9]
    assert c9["status"] == "warn" and "dev clone" in c9["detail"]


def test_cursor_reads_the_claude_global_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_QA_USER_HOME", str(tmp_path / "home"))
    code = tmp_path / "c"
    (code / ".cursor").mkdir(parents=True)
    g = tmp_path / "home" / ".claude" / "skills" / "grill-me"
    g.mkdir(parents=True)
    (g / "SKILL.md").write_text("---\nname: grill-me\n---\n", encoding="utf-8")
    assert setup_flow.generic_reach(code, "cursor")["global"] == [str(g)]
    assert setup_flow.generic_reach(code, "claude")["global"] == [str(g)]
    assert setup_flow.generic_reach(code, "antigravity")["global"] == []


def test_doctor_and_wiring_support_board_directory(sandbox):
    sb = sandbox()
    app = sb["board_home"] / "app"
    # Migrate stub running copy from board/server.py to board/server.py
    (app / "board" / "server.py").unlink()
    board_server = app / "board" / "server.py"
    board_server.parent.mkdir(parents=True, exist_ok=True)
    board_server.write_text("# running copy stub in board\n", encoding="utf-8")

    # Doctor checks 3 and 4 should pass with board/server.py
    report = doctor.run(str(sb["project"]))
    (c3,) = [c for c in report["checks"] if c["n"] == 3]
    (c4,) = [c for c in report["checks"] if c["n"] == 4]
    assert c3["status"] == "pass"
    assert c4["status"] == "pass"

    # Wiring verdict should be "ok"
    assert wiring.running_copy().joinpath("board", "server.py").is_file()
    launchers = wiring.scan_launchers(sb["project"])
    assert any(lw["verdict"] == "ok" for lw in launchers)

    # dev_clones detects checkouts with board/setup_flow.py
    clone = sb["home"] / "dev-clone"
    clone_setup = clone / "board" / "setup_flow.py"
    clone_setup.parent.mkdir(parents=True, exist_ok=True)
    clone_setup.write_text("# dev clone setup\n", encoding="utf-8")
    reg = projects.load_registry()
    reg["projects"]["dev-clone"] = {"slug": "dev-clone", "name": "Dev Clone", "code_path": str(clone), "docs_path": str(clone)}
    projects.save_registry(reg)
    assert clone in wiring.dev_clones()

    # setup_flow recognizes running_copy with board/server.py
    plan = setup_flow.build_plan(sb["project"])
    assert not any(a["id"] == "clone-running-copy" for a in plan.get("actions", []))

    # Verify board/ backward-compatibility shims exist and load
    repo_root = Path(__file__).resolve().parent.parent
    cli_shim = repo_root / "board" / "cli.py"
    server_shim = repo_root / "board" / "server.py"
    assert cli_shim.is_file()
    assert server_shim.is_file()
    import importlib.util
    for path, symbol in [(cli_shim, "main"), (server_shim, "PMRequestHandler")]:
        spec = importlib.util.spec_from_file_location(path.stem, str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, symbol)


# -- PQC-34: an install under the product's previous name is pointed at migrate-home -------------


def test_doctor_warns_and_points_to_migrate_home_when_an_old_folder_is_left(sandbox):
    sb = sandbox()
    (sb["home"] / migrate.OLD_DIR).mkdir()
    by = {c["n"]: c for c in doctor.run(str(sb["project"]))["checks"]}
    assert by[3]["status"] == "warn" and "migrate-home" in by[3]["fix"]


def test_doctor_fails_with_a_migrate_hint_when_only_the_old_install_exists(sandbox):
    sb = sandbox()
    (sb["home"] / migrate.OLD_DIR).mkdir()
    shutil.rmtree(sb["home"] / ".pm-qa-board" / "app")
    by = {c["n"]: c for c in doctor.run(str(sb["project"]))["checks"]}
    assert by[3]["status"] == "fail" and "migrate-home" in by[3]["fix"] and "git clone" not in by[3]["fix"]


def test_setup_plan_reports_a_legacy_install(sandbox):
    sb = sandbox()
    assert setup_flow.build_plan(sb["project"])["legacy_home"] == {
        "old_home_exists": False, "new_home_exists": True, "needs_migration": False}
    (sb["home"] / migrate.OLD_DIR).mkdir()
    shutil.rmtree(sb["home"] / ".pm-qa-board" / "app")
    legacy = setup_flow.build_plan(sb["project"])["legacy_home"]
    assert legacy["old_home_exists"] and legacy["needs_migration"]

