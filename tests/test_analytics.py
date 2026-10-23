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

    # Invalid window defaults to 6w (Story 29 cohort default)
    invalid_data = parser.get_analytics_summary(window="unknown_window")
    assert invalid_data["window"] == "6w"
    assert len(invalid_data["cohorts"]) == 6

    # Weekly Cohort presets (Story 29)
    w6_data = parser.get_analytics_summary(window="6w")
    assert w6_data["window"] == "6w"
    assert len(w6_data["cohorts"]) == 6

    w4_data = parser.get_analytics_summary(window="4w")
    assert w4_data["window"] == "4w"
    assert len(w4_data["cohorts"]) == 4

    qtd_data = parser.get_analytics_summary(window="qtd")
    assert qtd_data["window"] == "qtd"
    assert len(qtd_data["cohorts"]) >= 1

    # "all" window
    all_data = parser.get_analytics_summary(window="all")
    assert all_data["window"] == "all"
    assert len(all_data["charts"]["daily_throughput"]["dates"]) >= 1
    assert len(all_data["cohorts"]) >= 1


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




def test_analytics_frontend_wiring():
    html_path = Path("board/static/index.html")
    assert html_path.exists()
    html = html_path.read_text(encoding="utf-8")
    assert 'data-tab="analytics"' in html
    assert 'id="tab-analytics"' in html
    assert 'id="kpiThroughputValue"' in html
    assert 'id="kpiWorkVolumeValue"' in html
    assert 'id="kpiCycleTimeValue"' in html
    assert 'id="kpiQualityRateValue"' in html
    assert 'id="chartThroughputContainer"' in html
    assert 'id="chartBurnupContainer"' in html
    assert 'id="chartCompositionContainer"' in html
    assert 'id="chartQualityContainer"' in html
    # Story 30: Flat story list removed in favor of Weekly Cohorts accordion & slide-over ticketDrawer
    assert 'id="ticketDrawer"' in html
    # Story 29: Executive analytics additions
    assert 'id="analyticsReadinessBadge"' in html
    assert 'id="kpiVelocityValue"' in html
    assert 'id="kpiStagnationAlertBadge"' in html
    assert 'id="weeklyCohortsTable"' in html
    assert 'id="weeklyCohortsTableBody"' in html
    assert 'id="pipelineHealthTableBody"' in html
    assert 'id="stuckTicketsList"' in html
    assert 'id="ticketDrawer"' in html
    assert 'id="ticketDrawerSearch"' in html
    assert 'data-window="6w"' in html

    js_path = Path("board/static/app.js")
    assert js_path.exists()
    js = js_path.read_text(encoding="utf-8")
    assert "loadAnalytics" in js
    assert "setAnalyticsWindow" in js
    assert "setAnalyticsScope" in js
    assert "renderDailyThroughputChart" in js
    assert "renderBurnupChart" in js
    assert "renderCompositionChart" in js
    assert "renderQualityChart" in js
    # Story 29 JS wiring
    assert "toggleBurnupChartMode" in js
    assert "renderPipelineHealth" in js
    assert "renderWeeklyCohorts" in js
    assert "toggleCohortAccordion" in js
    assert "openTicketDrawer" in js
    assert "closeTicketDrawer" in js
    assert "filterTicketDrawer" in js


def test_story_29_executive_analytics_cohorts_and_stagnation(external_project):
    code, docs = external_project
    cards_dir = docs / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    # 1. Completed Feature with custom points: 5
    card1 = """---
id: APP-1
story: "1"
type: feature
points: 5
status: completed
created: 2026-10-18
validated: 2026-10-20
---
# APP-1 - Custom Points Feature
## Description
Custom points.
## Definition of Done
- [x] Item 1
## Changelog
| Date & Time | Agent | What |
|---|---|---|
| 2026-10-18 10:00 | Lead (Antigravity) | Build started |
| 2026-10-20 12:00 | Cold Reviewer (Pro) | Review round 1: PASS |
"""
    (cards_dir / "APP-1.md").write_text(card1, encoding="utf-8")

    # 2. Completed Improvement (default 2 points)
    card2 = """---
id: APP-2
story: "2"
type: improvement
status: completed
created: 2026-10-19
validated: 2026-10-21
---
# APP-2 - Default Points Improvement
## Description
Improvement description.
## Definition of Done
- [x] Item 1
## Changelog
| Date & Time | Agent | What |
|---|---|---|
| 2026-10-19 10:00 | Lead (Antigravity) | Build started |
| 2026-10-21 15:00 | Lead (Antigravity) | Validated |
"""
    (cards_dir / "APP-2.md").write_text(card2, encoding="utf-8")

    # 3. Completed Bug (default 1 point, net bug fixed)
    card3 = """---
id: APP-3
story: "3"
type: fix
status: completed
created: 2026-10-20
validated: 2026-10-22
---
# APP-3 - Resolved Bug
## Description
Bug description.
## Definition of Done
- [x] Item 1
## Changelog
| Date & Time | Agent | What |
|---|---|---|
| 2026-10-20 10:00 | Lead (Antigravity) | Build started |
| 2026-10-22 11:00 | Cold Reviewer (Pro) | Review round 1: PASS |
"""
    (cards_dir / "APP-3.md").write_text(card3, encoding="utf-8")

    # 4. Active In-Progress Bug aging > 7 days in Development stage
    card4 = """---
id: APP-4
story: "4"
type: fix
status: in-progress
created: 2026-09-30
---
# APP-4 - Aging Bug in Development
## Description
Old bug not touched.
## Definition of Done
- [ ] Item 1
## Changelog
| Date & Time | Agent | What |
|---|---|---|
| 2026-10-01 09:00 | Lead (Antigravity) | Build started |
"""
    (cards_dir / "APP-4.md").write_text(card4, encoding="utf-8")

    # 5. Active Critical Bug (keyword in title)
    card5 = """---
id: APP-5
story: "5"
type: bug
severity: critical
status: in-progress
created: 2026-10-22
---
# APP-5 - Critical Outage In Production
## Description
Critical issue.
## Definition of Done
- [ ] Item 1
## Changelog
| Date & Time | Agent | What |
|---|---|---|
| 2026-10-22 14:00 | Lead (Antigravity) | Build started |
"""
    (cards_dir / "APP-5.md").write_text(card5, encoding="utf-8")

    # 6. Card in QA with Review Round 2 (Reopened / Bounced ticket)
    card6 = """---
id: APP-6
story: "6"
type: feature
status: qa
created: 2026-10-21
---
# APP-6 - Feature with Rework
## Description
Feature in QA.
## Definition of Done
- [x] Item 1
## Changelog
| Date & Time | Agent | What |
|---|---|---|
| 2026-10-21 10:00 | Lead (Antigravity) | Build started |
| 2026-10-22 09:00 | Cold Reviewer (Pro) | Review round 2: findings fixed |
"""
    (cards_dir / "APP-6.md").write_text(card6, encoding="utf-8")

    # 7. Card in Ready to Build (story-backlog)
    card7 = """---
id: APP-7
story: "7"
type: chore
status: story-backlog
created: 2026-10-22
---
# APP-7 - Refactor Database Layer
## Description
Chore in backlog.
## Definition of Done
- [ ] Item 1
"""
    (cards_dir / "APP-7.md").write_text(card7, encoding="utf-8")

    # 8. Blocked card
    card8 = """---
id: APP-8
story: "8"
type: feature
status: in-progress
blocked: true
created: 2026-10-22
---
# APP-8 - Blocked by External API
## Description
Waiting on external team.
## Definition of Done
- [ ] Item 1
"""
    (cards_dir / "APP-8.md").write_text(card8, encoding="utf-8")

    parser = PMParser(code_path=code, docs_path=docs, prefix="APP")
    data = parser.get_analytics_summary(window="6w")

    # 1. Smart hybrid sizing & Velocity KPI
    # APP-1 (5 pts) + APP-2 (2 pts) + APP-3 (1 pt) = 8 points completed
    assert data["kpis"]["throughput"]["value"] == 3
    assert data["kpis"]["velocity"]["value"] == 8
    assert data["kpis"]["velocity"]["unit"] == "pts"
    # Feature capacity: APP-1 (5 pts feature) / 8 pts total = 62.5%
    assert data["kpis"]["velocity"]["feature_capacity_pct"] == 62.5

    # 2. Open Defect Severity Counter
    # APP-4 (normal) and APP-5 (critical)
    assert data["open_defects"]["critical"] == 1
    assert data["open_defects"]["normal"] == 1
    assert data["open_defects"]["total"] == 2

    # 3. Pipeline Health & WIP Stagnation Monitor
    ph = data["pipeline_health"]
    stages_dict = {s["id"]: s for s in ph["stages"]}
    assert "ready" in stages_dict
    assert "development" in stages_dict
    assert "qa" in stages_dict
    assert "blocked" in stages_dict

    assert stages_dict["ready"]["count"] == 1  # APP-7
    assert stages_dict["development"]["count"] == 2  # APP-4, APP-5
    assert stages_dict["qa"]["count"] == 1  # APP-6
    assert stages_dict["blocked"]["count"] == 1  # APP-8

    # APP-4 has been in stage since 2026-10-01 (>7 days)
    assert ph["total_aging"] >= 1
    stuck_ids = [t["id"] for t in ph["stuck_tickets"]]
    assert "APP-4" in stuck_ids
    assert ph["stuck_tickets"][0]["days_in_stage"] > 7

    # Reopened / Bounced ticket detection (APP-6 had round 2)
    assert ph["reopened_count"] >= 1

    # Stagnation Alert KPI
    assert data["kpis"]["stagnation"]["aging_count"] >= 1
    assert data["kpis"]["stagnation"]["critical_bugs"] == 1
    assert data["kpis"]["stagnation"]["alert_level"] == "critical"

    # 4. Weekly Cohorts Matrix
    cohorts = data["cohorts"]
    assert len(cohorts) == 6
    # Most recent cohort has stories delivered
    recent_cohort = cohorts[0]
    assert "velocity_points" in recent_cohort
    assert "feature_capacity_pct" in recent_cohort
    assert "work_mix" in recent_cohort
    assert "net_bug_flow" in recent_cohort
    assert "momentum" in recent_cohort
    assert recent_cohort["momentum"] in ("advancing", "stagnated", "bug_drag")

    # 5. Charts series
    charts = data["charts"]
    assert "scope_burnup" in charts
    assert len(charts["scope_burnup"]["series"]) == 6
    assert "total_scope" in charts["scope_burnup"]["series"][-1]
    assert "cumulative_completed" in charts["scope_burnup"]["series"][-1]

    assert "net_bug_flow" in charts
    assert len(charts["net_bug_flow"]["series"]) == 6
    assert ("Net Fixed" in charts["net_bug_flow"]["series"][-1]["net_label"] or "Bug Drag" in charts["net_bug_flow"]["series"][-1]["net_label"])

    assert "weekly_velocity" in charts
    assert len(charts["weekly_velocity"]["series"]) == 6
    assert "velocity_points" in charts["weekly_velocity"]["series"][-1]

    # 6. All delivered stories search list
    all_deliv_ids = [s["id"] for s in data["all_delivered_stories"]]
    assert "APP-1" in all_deliv_ids
    assert "APP-2" in all_deliv_ids
    assert "APP-3" in all_deliv_ids


def test_analytics_refinements_active_turnaround_and_qa_health(external_project):
    code, docs = external_project
    cards_dir = docs / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    # Card with distinct created date (backlog idle) and active in-progress build
    (cards_dir / "APP-10.md").write_text("""---
id: APP-10
story: "10"
type: feature
points: 3
status: completed
created: "2026-09-01"
---

# Feature 10

## Changelog
- 2026-10-22 14:00: Start build at `abcdef1`. Card in-progress. Agent: Claude Opus 5.5
- 2026-10-22 14:45: Build complete, card to qa. Agent: Claude Opus 5.5
""", encoding="utf-8")

    parser = PMParser(code_path=code, docs_path=docs, prefix="APP")
    card = parser.parse_card_file(cards_dir / "APP-10.md")
    turnaround_sec = parser.compute_card_active_turnaround(card)
    assert 2400 <= turnaround_sec <= 3000, f"Expected ~45m turnaround, got {turnaround_sec}s"

    data = parser.get_analytics_summary("6w")
    kpis = data["kpis"]

    # Velocity rate and points
    assert "rate_per_week" in kpis["velocity"]
    assert "target_points" not in kpis["velocity"]
    assert "active_turnaround" in kpis
    assert "mean_seconds" in kpis["active_turnaround"]

    # Test health
    assert "test_health" in kpis
    assert kpis["test_health"]["pass_rate_pct"] == 100.0

    # Agent work breakdown
    comp = data["charts"]["work_composition"]
    assert "agent_work_breakdown" in comp
    opus_entry = next((a for a in comp["agent_work_breakdown"] if "Opus" in a["agent"]), None)
    assert opus_entry is not None
    assert opus_entry["feature"] >= 1
    assert opus_entry["total_points"] >= 3
    assert len(opus_entry["stories"]) >= 1


def test_analytics_qa_test_health_and_momentum_status_fixes(external_project):
    code, docs = external_project
    cards_dir = docs / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    # 1. Test Gate runs parsing rejects git commit SHAs (like 3043581)
    heavy_dir = docs / "gate-heavy-results"
    heavy_dir.mkdir(parents=True, exist_ok=True)
    (heavy_dir / "INDEX.md").write_text("""# Heavy gate runs
| Started | Run id | Sprint | SHA | pytest | Wall time | Summary |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-29 20:04 | `20260929-200458-46b7-7ba9ce8` | -- | `7ba9ce8` | PASS (pytest 1039, full vitest) | 27:49 | consumed |
| 2026-10-04 10:28 | `20261004-102842-3473-3043581` | -- | `3043581` | FAIL (1222 passed, 91.23%) | 31:15 | consumed |
| 2026-10-15 16:56 | `20261015-165654-7a7e-f16c004` | -- | `f16c004` | PASS (pytest 1851 passed 89.99%) | 59:14 | consumed |
""", encoding="utf-8")

    parser = PMParser(code_path=code, docs_path=docs, prefix="APP")
    growth = parser._parse_gate_runs_from_quality()
    assert len(growth) == 3
    assert growth[0]["tests"] == 1039
    assert growth[1]["tests"] == 1222  # Must be 1222, NOT the commit sha 3043581!
    assert growth[2]["tests"] == 1851

    # 2. Test Momentum status: 13 stories (1 feat, 12 improvements, 0 bugs) is Advancing, not Bug Drag
    for i in range(1, 13):
        (cards_dir / f"APP-{i}.md").write_text(f"""---
id: APP-{i}
story: "{i}"
type: improvement
points: 2
status: completed
created: "2026-10-20"
---
# Story {i}
## Changelog
- 2026-10-21 10:00: Completed.
""", encoding="utf-8")

    (cards_dir / "APP-13.md").write_text("""---
id: APP-13
story: "13"
type: feature
points: 3
status: completed
created: "2026-10-20"
---
# Feature 13
## Changelog
- 2026-10-21 11:00: Completed.
""", encoding="utf-8")

    summary = parser.get_analytics_summary("6w")
    cohorts = summary["weekly_cohorts"]
    assert len(cohorts) > 0
    active_cohort = next((c for c in cohorts if c["total_stories"] >= 13), None)
    assert active_cohort is not None
    assert active_cohort["momentum"] == "advancing"
    assert "Advancing" in active_cohort["momentum_label"]
    assert "W" in active_cohort["label"]  # Has week number



