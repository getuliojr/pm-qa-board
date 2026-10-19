"""PMParser: split code/docs paths, dual Story/Sprint schema, close-out artifact detection."""

from __future__ import annotations

from conftest import git, seed_external_docs

import projects
from parser import PMParser


def test_legacy_single_root_layout_still_parses(legacy_project):
    p = PMParser(legacy_project)
    assert p.docs_path == legacy_project / "docs"
    assert p.plan_path == legacy_project / "PLAN.md"  # fallback to code root
    assert not p.docs_external
    (card,) = p.get_all_cards()
    assert card["mn_id"] == "MN-1.2" and card["id"] == "MN-1.2"
    assert card["sprint"] == "1.2" and card["story"] == "1.2"
    assert card["status"] == "sprint-backlog"
    assert card["schema"] == "sprint"
    assert card["file_path"] == "docs/cards/MN-1.2 - Legacy sprint card.md"
    assert not card["missing_plan_dod"], "## Sprint DoD in PLAN.md must still satisfy the backlog gate"
    assert [r["sprint"] for r in p.parse_plan()["roadmap"]]


def test_story_schema_groups_with_legacy_backlog(cockpit_home, external_project):
    code, docs = external_project
    project = projects.build_project("Story App", code, prefix="APP", docs_path=docs)
    projects.ensure_docs_root(project)
    seed_external_docs(docs)

    p = PMParser(project=project)
    assert p.docs_external
    (card,) = p.get_all_cards()
    assert card["mn_id"] == "APP-3.1"
    assert card["sprint"] == "3.1"
    # story-backlog lands in the same column as legacy sprint-backlog
    assert card["status"] == "sprint-backlog"
    assert card["schema"] == "story"
    assert card["dod_total"] == 2 and card["dod_checked"] == 1
    assert not card["missing_plan_dod"], "## Story DoD in PLAN.md must satisfy the backlog gate"
    assert p._extract_sprint_dod("3.1")[0]["text"] == "Story plan item"
    roadmap = p.parse_plan()["roadmap"]
    assert roadmap and "3.1" in roadmap[0]["sprint"]


def test_external_docs_writes_never_touch_code_repo(cockpit_home, external_project):
    code, docs = external_project
    project = projects.build_project("Story App", code, prefix="APP", docs_path=docs)
    projects.ensure_docs_root(project)
    seed_external_docs(docs)
    git(docs, "config", "user.email", "test@example.com")
    git(docs, "config", "user.name", "Test")

    p = PMParser(project=project)
    ok, _ = p.toggle_dod_item("APP-3.1", 0, True)
    assert ok
    ok, _ = p.toggle_qa_item("APP-3.1", 0, True)
    assert ok
    ok, _ = p.validate_card("APP-3.1", notes="QA ok", do_commit=True)
    assert ok

    assert git(code, "status", "--porcelain") == "", "code repo must stay clean"
    assert sorted(x.name for x in code.iterdir()) == [".git", "main.py"]
    assert "validate APP-3.1" in git(docs, "log", "--oneline", "-1")
    text = (docs / "cards" / "APP-3.1 - New story card.md").read_text(encoding="utf-8")
    assert "- [x] Story item one" in text and "status: completed" in text
    # code activity still comes from the code repo
    assert p.get_git_activity(limit=5)["commits"][0]["message"] == "code init"


def test_closeout_artifact_detected(legacy_project):
    art = legacy_project / "docs" / "artifacts"
    art.mkdir()
    (art / "MN-1.2-closeout.html").write_text("<html></html>", encoding="utf-8")
    (card,) = PMParser(legacy_project).get_all_cards()
    assert card["closeout"] == {
        "file_name": "MN-1.2-closeout.html",
        "file_path": "docs/artifacts/MN-1.2-closeout.html",
        "url": "/artifacts/MN-1.2-closeout.html",
    }


def test_registry_roundtrip_and_path_lookup(cockpit_home, legacy_project, external_project):
    code, docs = external_project
    a = projects.build_project("Legacy", legacy_project, prefix="MN")
    b = projects.build_project("Story App", code, prefix="APP", docs_path=docs)
    projects.upsert_project(a)
    projects.upsert_project(b)
    data = projects.load_registry()
    assert data["active_project"] == "legacy"
    assert a["docs_mode"] == "in-repo" and b["docs_mode"] == "external"
    assert projects.find_project_for_path(code / "main.py")["slug"] == "story-app"
    assert projects.find_project_for_path(docs / "cards")["slug"] == "story-app"
    assert projects.select_project("story-app")["prefix"] == "APP"
    assert projects.get_active_project()["slug"] == "story-app"


def test_build_project_rejects_external_inside_code(legacy_project):
    import pytest

    with pytest.raises(ValueError):
        projects.build_project("X", legacy_project, docs_mode="external", docs_path=legacy_project / "pm")
    with pytest.raises(ValueError):
        projects.build_project("X", legacy_project, prefix="bad-prefix")


def test_filename_id_fallback_keeps_backlog_style_ids(legacy_project):
    """Review round 1 #5: MN-BACKLOG-003 style stems still yield their id without frontmatter."""
    cards = legacy_project / "docs" / "cards"
    (cards / "MN-BACKLOG-009 - No frontmatter.md").write_text("# Loose idea\n", encoding="utf-8")
    (cards / "APP-4.2 - New prefix.md").write_text("# Another\n", encoding="utf-8")
    ids = {c["mn_id"] for c in PMParser(legacy_project).get_all_cards()}
    assert {"MN-BACKLOG-009", "APP-4.2", "MN-1.2"} <= ids


def test_validate_reports_failed_docs_commit(cockpit_home, external_project, monkeypatch):
    """Review round 1 #7: a docs-repo commit failure is surfaced, not swallowed."""
    code, docs = external_project
    project = projects.build_project("Story App", code, prefix="APP", docs_path=docs)
    projects.ensure_docs_root(project)
    seed_external_docs(docs)
    p = PMParser(project=project)
    monkeypatch.setattr(p, "commit_docs", lambda *_a: False)
    ok, msg = p.validate_card("APP-3.1", do_commit=True)
    assert ok and "commit failed" in msg


def test_shared_docs_repo_holds_many_projects(cockpit_home, tmp_path):
    """pm-qa-docs layout: one private docs repo, one folder per project, no nested repos."""
    from conftest import init_repo

    docs_repo = init_repo(tmp_path / "pm-qa-docs")
    (docs_repo / "README.md").write_text("docs\n", encoding="utf-8")
    git(docs_repo, "add", "-A")
    git(docs_repo, "commit", "-q", "-m", "init")
    code_a = init_repo(tmp_path / "app-a")
    code_b = init_repo(tmp_path / "app-b")

    a = projects.build_project("App A", code_a, prefix="APP", docs_path=docs_repo / "app-a")
    b = projects.build_project("App B", code_b, prefix="BBB", docs_path=docs_repo / "app-b")
    for proj in (a, b):
        projects.ensure_docs_root(proj)
    assert not (docs_repo / "app-a" / ".git").exists() and not (docs_repo / "app-b" / ".git").exists()

    seed_external_docs(docs_repo / "app-a")
    ok, msg = PMParser(project=a).validate_card("APP-3.1", do_commit=True)
    assert ok and "commit failed" not in msg
    assert "validate APP-3.1" in git(docs_repo, "log", "--oneline", "-1")
    assert git(code_a, "status", "--porcelain") == "" and git(code_b, "status", "--porcelain") == ""


def test_card_timestamps_extraction_and_qa_entered_at(legacy_project):
    cards_dir = legacy_project / "docs" / "cards"
    c1_path = cards_dir / "MN-1.3 - Timestamp test.md"
    c1_path.write_text(
        "---\n"
        "id: MN-1.3\n"
        "sprint: '1.3'\n"
        "type: feature\n"
        "status: qa\n"
        "created: 2026-10-18\n"
        "created_time: '14:35'\n"
        "---\n\n"
        "# MN-1.3 - Timestamp test\n\n"
        "## Changelog\n\n"
        "| Date & Time | Agent | What |\n"
        "|---|---|---|\n"
        "| 2026-10-18 14:35 | Dev | Created card |\n"
        "| 2026-10-18 16:40 | Reviewer | Review pass, ready for qa |\n",
        encoding="utf-8",
    )
    p = PMParser(legacy_project)
    card = next(c for c in p.get_all_cards() if c["mn_id"] == "MN-1.3")
    assert card["created_date"] == "2026-10-18"
    assert card["created_time"] == "14:35"
    assert card["created_at"] == "2026-10-18 14:35"
    assert card["qa_entered_at"] == "2026-10-18 16:40"


def test_active_sprint_overview_fifo_ascending_sort(legacy_project):
    cards_dir = legacy_project / "docs" / "cards"
    for f in cards_dir.glob("*.md"):
        f.unlink()
    # Create two ready-to-build cards with different timestamps
    (cards_dir / "MN-2.1 - First Oldest.md").write_text(
        "---\n"
        "id: MN-2.1\n"
        "sprint: '2.1'\n"
        "type: feature\n"
        "status: sprint-backlog\n"
        "created: 2026-10-17\n"
        "created_time: '10:00'\n"
        "---\n# First\n",
        encoding="utf-8",
    )
    (cards_dir / "MN-2.2 - Second Newer.md").write_text(
        "---\n"
        "id: MN-2.2\n"
        "sprint: '2.2'\n"
        "type: feature\n"
        "status: sprint-backlog\n"
        "created: 2026-10-18\n"
        "created_time: '09:00'\n"
        "---\n# Second\n",
        encoding="utf-8",
    )

    p = PMParser(legacy_project)
    overview = p.get_active_sprint_overview()
    # The default active sprint when nothing is in-progress should pick the oldest ready-to-build story (2.1)
    assert overview["sprint_id"] == "2.1"

