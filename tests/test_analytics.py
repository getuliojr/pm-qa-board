import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from parser import PMParser
import projects


def test_analytics_summary_windows_and_defaults(external_project):
    code, docs = external_project
    (docs / "cards").mkdir(parents=True, exist_ok=True)
    parser = PMParser(code_path=code, docs_path=docs, prefix="APP")

    for win, expected_days in [("7d", 7), ("14d", 14), ("30d", 30)]:
        data = parser.get_analytics_summary(window=win)
        assert data["window"] == win
        assert len(data["charts"]["daily_throughput"]["dates"]) == expected_days
        assert len(data["charts"]["daily_throughput"]["series"]) == expected_days
        assert len(data["charts"]["burnup"]["dates"]) == expected_days
        assert "kpis" in data
        assert "throughput" in data["kpis"]
        assert "work_volume" in data["kpis"]
        assert "cycle_time" in data["kpis"]
        assert "quality_rate" in data["kpis"]

    # Invalid window defaults to 7d
    invalid_data = parser.get_analytics_summary(window="unknown_window")
    assert invalid_data["window"] == "7d"
    assert len(invalid_data["charts"]["daily_throughput"]["dates"]) == 7

    # "all" window
    all_data = parser.get_analytics_summary(window="all")
    assert all_data["window"] == "all"
    assert len(all_data["charts"]["daily_throughput"]["dates"]) >= 1


def test_analytics_card_metrics_and_churn(external_project):
    code, docs = external_project
    cards_dir = docs / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    # Completed card with changelog and duration
    card_content = """---
id: APP-1
story: "1"
type: feature
status: completed
created: 2026-10-20
---

# APP-1 - Test Feature

## Description
Test description.

## Definition of Done
- [x] Task 1

## Changelog
| Date & Time | Agent | What |
|---|---|---|
| 2026-10-20 10:00 | Lead (Antigravity) | Build started |
| 2026-10-20 11:30 | Cold Reviewer (Pro) | Review round 1: PASS |
"""
    (cards_dir / "APP-1 - Test Feature.md").write_text(card_content, encoding="utf-8")

    # In-progress card
    card2_content = """---
id: APP-2
story: "2"
type: fix
status: in-progress
created: 2026-10-21
---

# APP-2 - Fix Bug

## Description
Bug fix.

## Definition of Done
- [ ] Fix it
"""
    (cards_dir / "APP-2 - Fix Bug.md").write_text(card2_content, encoding="utf-8")

    # QUALITY.md with gate run
    quality_content = """# QUALITY
## Gate runs
| Story | Tree | Command | Result |
|---|---|---|---|
| 1 | `abc1234` | `pytest` | **green**: 42 passed |
"""
    (docs / "QUALITY.md").write_text(quality_content, encoding="utf-8")

    parser = PMParser(code_path=code, docs_path=docs, prefix="APP")
    data = parser.get_analytics_summary(window="30d")

    # KPIs check
    assert data["kpis"]["throughput"]["value"] >= 1
    assert data["kpis"]["work_volume"]["commits"] >= 1
    assert data["kpis"]["quality_rate"]["test_count"] == 42
    assert data["kpis"]["quality_rate"]["first_pass_rate"] == 100.0

    # Drill-down stories check
    story_ids = [s["id"] for s in data["stories"]]
    assert "APP-1" in story_ids
    assert "APP-2" in story_ids

    # Composition check
    types = {item["type"]: item["count"] for item in data["charts"]["work_composition"]["by_type"]}
    assert "feature" in types or "fix" in types


def test_analytics_caching_and_invalidation(external_project):
    code, docs = external_project
    (docs / "cards").mkdir(parents=True, exist_ok=True)
    parser = PMParser(code_path=code, docs_path=docs, prefix="APP")

    data1 = parser.get_analytics_summary(window="7d")
    data2 = parser.get_analytics_summary(window="7d")
    # Same cached object
    assert data1 is data2

    parser.invalidate_cache()
    data3 = parser.get_analytics_summary(window="7d")
    # Fresh copy after invalidation
    assert data3 is not data1
    assert data3["window"] == "7d"


def test_analytics_portfolio_and_all_projects(external_project, tmp_path):
    code, docs = external_project
    (docs / "cards").mkdir(parents=True, exist_ok=True)
    parser = PMParser(code_path=code, docs_path=docs, prefix="APP", project={"slug": "app-test", "name": "App Test"})

    data = parser.get_analytics_summary(window="7d", project_slug="all")
    assert data["project"] == "all"
    assert "projects_summary" in data
    assert isinstance(data["projects_summary"], list)



