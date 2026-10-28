"""`init` onboarding (report-first, stealth) and the 6-minute Fast vs Heavy gate profiler."""

from __future__ import annotations

import argparse
import json

from conftest import git

import gate_profiler
import onboarding
import projects


def _init_args(code, **kw):
    base = dict(code_path=str(code), name="Story App", prefix="APP", slug=None, docs_path=None, stealth=False, apply=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_init_report_only_writes_nothing(board_home, external_project, capsys):
    code, docs = external_project
    assert onboarding.cmd_init(_init_args(code, docs_path=str(docs))) == 0
    out = capsys.readouterr().out
    assert "Loop alignment checklist" in out and "Report only" in out
    assert not docs.exists()
    assert not projects.registry_path().exists()


def test_init_apply_external_keeps_code_repo_clean(board_home, external_project):
    code, docs = external_project
    assert onboarding.cmd_init(_init_args(code, docs_path=str(docs), apply=True)) == 0
    assert (docs / "ORCHESTRATION.md").is_file() and (docs / ".git").is_dir()
    assert (docs / "PLAN.md").is_file() and (docs / "cards").is_dir()
    assert git(code, "status", "--porcelain") == ""
    assert projects.get_active_project()["slug"] == "story-app"


def test_init_stealth_in_repo_excludes_docs(board_home, external_project):
    code, _ = external_project
    (code / ".claude").mkdir()
    (code / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Edit"}], "PostToolUse": [{}, {}]}}), encoding="utf-8"
    )
    (code / "CLAUDE.md").write_text("Run grill-me first. Never push.\n", encoding="utf-8")
    report = onboarding.inspect_project(code)
    assert report["hooks"] == {"PreToolUse": 1, "PostToolUse": 2}
    assert report["loop_signals"]["design before code (grill-me)"]
    assert report["loop_signals"]["commit locally, never push"]
    assert not report["loop_signals"]["cold review gate"]

    assert onboarding.cmd_init(_init_args(code, stealth=True, apply=True)) == 0
    exclude = (code / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "docs/" in exclude
    # untracked in-repo PM docs are invisible to git; only the files the test itself added show up
    status = git(code, "status", "--porcelain")
    assert "docs" not in status
    # idempotent
    assert onboarding.cmd_init(_init_args(code, stealth=True, apply=True)) == 0
    assert (code / ".git" / "info" / "exclude").read_text(encoding="utf-8").count("docs/") == 1


def test_detect_stack_python_and_node(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text(json.dumps({"scripts": {"lint": "x", "test": "y"}}), encoding="utf-8")
    d = gate_profiler.detect_stack(tmp_path)
    assert "python (.)" in d["stacks"] and "node (web)" in d["stacks"]
    assert "pytest" in d["full_cmd"] and "npm --dir web test" in d["full_cmd"]
    assert "npm --dir web run lint" in d["fast_cmd"]


def test_declared_split_is_heavy_without_measuring(tmp_path):
    (tmp_path / "scripts").mkdir()
    for f in ("gates.ps1", "finalgates.ps1"):
        (tmp_path / "scripts" / f).write_text("", encoding="utf-8")
    d = gate_profiler.detect_stack(tmp_path)
    g = gate_profiler.decide(d, None, 360)
    assert g["mode"] == "heavy" and g["full"].endswith("finalgates.ps1")


def test_six_minute_threshold_decides_mode():
    d = {"stacks": ["python (.)"], "fast_cmd": "f", "full_cmd": "t", "declared_heavy": False}
    assert gate_profiler.decide(d, None, 360)["mode"] == "unprofiled"
    assert gate_profiler.decide(d, {"seconds": 359.0, "timed_out": False, "exit_code": 0}, 360)["mode"] == "fast"
    assert gate_profiler.decide(d, {"seconds": 361.0, "timed_out": False, "exit_code": 0}, 360)["mode"] == "heavy"
    assert gate_profiler.decide(d, {"seconds": 365.0, "timed_out": True, "exit_code": None}, 360)["mode"] == "heavy"


def test_measure_stops_at_threshold(tmp_path):
    fast = gate_profiler.measure("exit 0", tmp_path, threshold=30)
    assert not fast["timed_out"] and fast["exit_code"] == 0
    slow = gate_profiler.measure('python -c "import time; time.sleep(30)"', tmp_path, threshold=0)
    assert slow["timed_out"]


def test_heavy_script_goes_to_docs_for_external(board_home, external_project):
    code, docs = external_project
    project = projects.build_project("Story App", code, prefix="APP", docs_path=docs)
    gates = gate_profiler.decide(
        {"stacks": ["python (.)"], "fast_cmd": "f", "full_cmd": "uv run pytest -q", "declared_heavy": False},
        {"seconds": 400.0, "timed_out": False, "exit_code": 0},
        360,
    )
    path = gate_profiler.write_heavy_script(project, gates)
    assert path == docs / "gates" / "finalgates.ps1"
    assert "uv run pytest -q" in path.read_text(encoding="utf-8")
    assert git(code, "status", "--porcelain") == ""


def test_reprofile_keeps_existing_heavy_script(board_home, external_project, monkeypatch):
    """Review round 1 #2: a second `profile-gates --apply` must not drop heavy_script."""
    code, docs = external_project
    project = projects.build_project("Story App", code, prefix="APP", docs_path=docs)
    projects.upsert_project(project)
    monkeypatch.setattr(
        gate_profiler, "detect_stack",
        lambda _p: {"stacks": ["python (.)"], "fast_cmd": "f", "full_cmd": "uv run pytest -q", "declared_heavy": False},
    )
    monkeypatch.setattr(gate_profiler, "measure", lambda *_a: {"seconds": 400.0, "timed_out": False, "exit_code": 0})
    args = argparse.Namespace(fast_cmd=None, full_cmd=None, threshold_seconds=360, measure=True, apply=True)
    for _ in range(2):
        assert gate_profiler.cmd_profile_gates(projects.load_registry()["projects"]["story-app"], args) == 0
        saved = projects.load_registry()["projects"]["story-app"]["gates"]
        assert saved["mode"] == "heavy"
        assert saved["heavy_script"] == str(docs / "gates" / "finalgates.ps1")
