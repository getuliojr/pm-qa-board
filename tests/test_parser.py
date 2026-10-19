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


def test_roadmap_qa_debt_not_marked_complete(tmp_path):
    """Story 15: Status with QA debt ('owed: Human QA', 'Card in qa') must not be marked is_complete."""
    plan_file = tmp_path / "PLAN.md"
    plan_content = """# Project PLAN

| Story | Goal | Status |
|---|---|---|
| Story 1 | Completed story | ✅ Done 2026-10-16 |
| Story 2 | Built awaiting QA | ✅ **Built 2026-10-16**: Card in `qa`; owed: Human QA |
| Story 3 | Another QA debt | 🔨 Built; awaiting qa |
| Story 4 | In progress story | 🔨 Building (start SHA 1234567) |
"""
    plan_file.write_text(plan_content, encoding="utf-8")
    p = PMParser(tmp_path)
    plan_data = p.parse_plan()
    roadmap = plan_data["roadmap"]

    s1 = next(r for r in roadmap if "Story 1" in r["sprint"])
    assert s1["is_complete"] is True
    assert s1["is_built"] is True

    s2 = next(r for r in roadmap if "Story 2" in r["sprint"])
    assert s2["is_complete"] is False
    assert s2["is_built"] is True

    s3 = next(r for r in roadmap if "Story 3" in r["sprint"])
    assert s3["is_complete"] is False
    assert s3["is_built"] is True

    s4 = next(r for r in roadmap if "Story 4" in r["sprint"])
    assert s4["is_complete"] is False
    assert s4["is_built"] is False


def test_pqc_17_quality_md_gate_runs_parsing_and_metrics(tmp_path):
    """PQC-17: Parse 4-column ## Gate runs table in QUALITY.md for fast-mode projects."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    cards_dir = docs_dir / "cards"
    cards_dir.mkdir()

    # Seed card for Story 15
    (cards_dir / "PQC-15 - Sample Story.md").write_text(
        "---\nid: PQC-15\nsprint: '15'\ntype: feature\nstatus: completed\ncreated: 2026-10-18\n---\n"
        "Closed at `ea020fe`\n",
        encoding="utf-8",
    )

    # Seed PLAN.md
    (tmp_path / "PLAN.md").write_text(
        "# Plan\n\n| Story | Goal | Status |\n|---|---|---|\n| Story 15 | Testing | ✅ Done 2026-10-18 `@ea020fe` |\n",
        encoding="utf-8",
    )

    # Seed QUALITY.md with 4-column Gate runs table
    (docs_dir / "QUALITY.md").write_text(
        "# Quality\n\n## Gate runs\n\n"
        "| Story | Tree | Command | Result |\n"
        "|---|---|---|---|\n"
        "| Story 15 | `ea020fe` | `uv run pytest` | ✅ **139 passed, 1 xfailed** in 1.45s (tree clean) |\n"
        "| Story 16 | `1234567` | `uv run pytest` | ✅ **140 passed** in 2.10s |\n",
        encoding="utf-8",
    )

    proj = {
        "slug": "sample-project",
        "prefix": "PQC",
        "code_path": str(tmp_path),
        "docs_path": str(docs_dir),
        "gates": {"mode": "fast", "full": "uv run pytest"},
    }

    p = PMParser(project=proj)
    sprints = p.get_sprint_gate_status()

    s15 = next((s for s in sprints if s["sprint"] == "15"), None)
    assert s15 is not None
    assert s15["verified"] is True
    assert s15["status"] == "Passed"
    assert s15["pass_count"] == 139
    assert s15["xfail_count"] == 1
    assert s15["duration_sec"] == "1.45"
    assert s15["tested_sha"] == "ea020fe"


def test_pqc_17_non_legacy_project_never_grandfathered(tmp_path):
    """PQC-17: Non-money-nest projects must never grandfather stories 1-15 as passed."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    cards_dir = docs_dir / "cards"
    cards_dir.mkdir()

    (cards_dir / "PQC-2 - Story Two.md").write_text(
        "---\nid: PQC-2\nsprint: '2'\ntype: feature\nstatus: completed\ncreated: 2026-10-18\n---\n"
        "Closed at `abcd123`\n",
        encoding="utf-8",
    )
    (tmp_path / "PLAN.md").write_text(
        "# Plan\n\n| Story | Goal | Status |\n|---|---|---|\n| Story 2 | Goal | ✅ Done `@abcd123` |\n",
        encoding="utf-8",
    )

    proj = {
        "slug": "custom-fast-tool",
        "prefix": "PQC",
        "code_path": str(tmp_path),
        "docs_path": str(docs_dir),
        "gates": {"mode": "fast"},
    }

    p = PMParser(project=proj)
    sprints = p.get_sprint_gate_status()
    s2 = next((s for s in sprints if s["sprint"] == "2"), None)
    assert s2 is not None
    # No gate runs or logs recorded, so must be Owed (NOT grandfathered as Passed)
    assert s2["verified"] is False
    assert s2["status"] == "Owed"


def test_story18_git_creation_timestamp_priority_over_changelog(monkeypatch, tmp_path):
    """Story 18: Git creation commit timestamp takes precedence over skewed/hallucinated changelog timestamps."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    cards_dir = docs_dir / "cards"
    cards_dir.mkdir()

    card_file = cards_dir / "MN-18.6.1 - First Story.md"
    card_file.write_text(
        "---\n"
        "id: MN-18.6.1\n"
        "sprint: '18.6.1'\n"
        "type: feature\n"
        "status: sprint-backlog\n"
        "created: 2026-10-19\n"
        "---\n\n"
        "## Changelog\n\n"
        "| Date & Time | Agent | What |\n"
        "|---|---|---|\n"
        "| 2026-10-19 14:40 | Agent | Seeded card at future time |\n",
        encoding="utf-8",
    )

    proj = {
        "slug": "test-skew",
        "prefix": "MN",
        "code_path": str(tmp_path),
        "docs_path": str(docs_dir),
        "gates": {"mode": "fast"},
    }

    p = PMParser(project=proj)
    # Mock get_card_git_commit_map to return an authoritative git commit with time 14:24
    monkeypatch.setattr(
        p,
        "get_card_git_commit_map",
        lambda: {
            card_file.name: [
                {"date": "2026-10-19", "time": "14:24", "datetime": "2026-10-19 14:24", "subject": "Initial commit"}
            ]
        },
    )

    cards = p.get_all_cards()
    c = next(card for card in cards if card["mn_id"] == "MN-18.6.1")
    # Must use git commit 14:24, NOT changelog 14:40
    assert c["created_time"] == "14:24"
    assert c["created_at"] == "2026-10-19 14:24"



