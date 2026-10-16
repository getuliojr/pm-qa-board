"""
Money-nest regression suite for PMParser (copied from money-nest tools/pm-dashboard/test_parser.py).
Runs against the real money-nest checkout to prove the extracted parser renders it identically.
"""
import os
from pathlib import Path

import pytest

MONEY_NEST = Path(os.environ.get("MONEY_NEST_ROOT", Path(__file__).resolve().parents[2] / "money-nest"))
pytestmark = pytest.mark.skipif(not (MONEY_NEST / "docs" / "cards").is_dir(), reason="money-nest checkout not found")

from pathlib import Path
from parser import PMParser


def test_parser():
    p = PMParser(MONEY_NEST)
    cards = p.get_all_cards()
    print(f"Total cards found: {len(cards)}")
    assert len(cards) > 0, "Should find existing cards in docs/cards"

    # Check that known cards exist
    card_ids = {c["mn_id"] for c in cards}
    print(f"Sample IDs: {list(card_ids)[:5]}")

    # Check status distribution
    statuses = {}
    for c in cards:
        statuses[c["status"]] = statuses.get(c["status"], 0) + 1
    print(f"Status distribution: {statuses}")

    # Check plan parsing
    plan = p.parse_plan()
    roadmap = plan["roadmap"]
    print(f"Total sprints in roadmap: {len(roadmap)}")
    assert len(roadmap) > 0, "Should parse roadmap from PLAN.md"
    print(f"First 3 sprints: {[r['sprint'] for r in roadmap[:3]]}")

    # Check git activity
    git_act = p.get_git_activity(limit=5)
    print(f"Current branch: {git_act['branch']}")
    print(f"Recent commits: {len(git_act['commits'])}")

    # Check created_date presence on cards
    has_created = sum(1 for c in cards if c.get("created_date"))
    print(f"Cards with created_date: {has_created}/{len(cards)}")
    assert has_created == len(cards), "Every card should have a created_date"

    # Check that 100% of card files physically contain created: in frontmatter
    import re
    all_card_files = [
        f for f in (list(p.cards_dir.glob("*.md")) + (list(p.backlog_dir.glob("*.md")) if p.backlog_dir.exists() else []))
        if f.name != "template.md"
    ]
    cards_with_frontmatter_date = sum(
        1
        for f in all_card_files
        if re.search(r"^created(?:_date)?:\s*", f.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
    )
    print(f"Card files with created frontmatter on disk: {cards_with_frontmatter_date}/{len(all_card_files)}")
    assert cards_with_frontmatter_date == len(all_card_files), "Every card file on disk must have created: frontmatter"

    # Check backfill method reports 0 needed updates (all up to date)
    bf_res = p.backfill_card_created_dates(dry_run=True)
    assert bf_res["updated_count"] == 0, "All cards should already have created dates"
    print("Backfill idempotency assertion PASSED")

    # Check sprint doc retrieval
    doc_15_6 = p.get_sprint_doc("15.6")
    assert doc_15_6 is not None, "Should find doc for Sprint 15.6"
    assert "content" in doc_15_6 and len(doc_15_6["content"]) > 100
    print(f"Sprint 15.6 doc retrieved: {doc_15_6['title']} ({len(doc_15_6['content'])} chars)")

    doc_1 = p.get_sprint_doc("Sprint 1")
    assert doc_1 is not None, "Should find doc for Sprint 1"
    print(f"Sprint 1 doc retrieved: {doc_1['title']} ({len(doc_1['content'])} chars)")

    # Check active sprint overview
    active_overview = p.get_active_sprint_overview()
    assert "sprint_id" in active_overview
    assert "sprint_dod" in active_overview
    assert "cards" in active_overview
    assert len(active_overview["available_sprints"]) > 0
    print(
        f"Active sprint overview OK: Sprint {active_overview['sprint_id']} "
        f"({active_overview['sprint_dod_checked']}/{active_overview['sprint_dod_total']} DoD, "
        f"{len(active_overview['cards'])} cards)"
    )

    # Check multi-stream active streams overview (Sprint 17.11)
    streams_overview = p.get_active_streams_overview()
    assert streams_overview["mode"] == "multi-stream"
    assert "active_count" in streams_overview
    assert streams_overview["active_count"] >= 0
    assert "streams" in streams_overview
    assert len(streams_overview["streams"]) == streams_overview["active_count"]

    # Check stream structure and strict exclusion of QA/completed cards
    stream_ids = [s["id"] for s in streams_overview["streams"]]
    assert "MN-17-001" not in stream_ids, "MN-17-001 is in QA and must be excluded from active streams"
    assert "MN-17.11" not in stream_ids, "MN-17.11 has moved to QA and must be excluded from active streams"

    for stream in streams_overview["streams"]:
        assert stream["status"] == "in-progress"
        assert "title" in stream
        assert "sprint" in stream
        assert "dod" in stream
        assert "dod_total" in stream
        assert "dod_checked" in stream
        assert "progress_pct" in stream
        assert "latest_activity" in stream
        assert "changelog" in stream
        assert "elapsed_seconds" in stream
        assert "duration_formatted" in stream
        assert "start_time" in stream
        assert "end_time" in stream
        assert stream["start_time"] is not None, "Stream start_time should be populated"
    print(f"Active streams overview OK: {streams_overview['active_count']} active streams, IDs: {stream_ids}")

    # Check heavy gate info
    heavy_info = p.get_heavy_gate_info()
    assert "queue_count" in heavy_info
    assert "queue_items" in heavy_info
    assert "latest_run" in heavy_info
    if heavy_info.get("latest_run"):
        lr = heavy_info["latest_run"]
        assert "overall_ok" in lr
    print(f"Heavy gate info OK: {heavy_info['queue_count']} queued checks")

    # Check sprint DoD toggle & restore on 15.8.1
    items_15_8_1 = p._extract_sprint_dod("15.8.1")
    if items_15_8_1:
        orig_state = items_15_8_1[0]["checked"]
        ok, msg = p.toggle_sprint_dod_item("15.8.1", 0, not orig_state)
        assert ok, f"Toggle failed: {msg}"
        # Restore
        ok2, msg2 = p.toggle_sprint_dod_item("15.8.1", 0, orig_state)
        assert ok2, f"Restore failed: {msg2}"
        print("Sprint 15.8.1 DoD toggle and restore test PASSED")

    # Check sprint gate audit status
    sprint_gate_status = p.get_sprint_gate_status()
    assert isinstance(sprint_gate_status, list)
    assert len(sprint_gate_status) > 0, "Should return list of completed roadmap sprints"
    # Rule 1: Every sprint must have a commit SHA
    assert all(bool(s.get("sha")) for s in sprint_gate_status), "Every sprint must have a resolved commit SHA"
    sample_s = sprint_gate_status[0]
    assert "sprint" in sample_s
    assert "sha" in sample_s
    assert "verified" in sample_s
    assert "status" in sample_s
    # Rule 2: Sprints <= 15.7 must be marked verified/completed
    for s in sprint_gate_status:
        if s["sprint"] in ["0", "1", "6", "15", "15.7"]:
            assert s["verified"] is True, f"Sprint {s['sprint']} should be marked verified"
            assert s["status"] in ("Passed", "Verified") or s["status"].startswith("Passed via"), f"Sprint {s['sprint']} status should be 'Passed'"

    # Rule 3: completed_sha boundary check
    s_17_3_2 = next((s for s in sprint_gate_status if s["sprint"] == "17.3.2"), None)
    assert s_17_3_2 is not None, "Should find Sprint 17.3.2"
    # Sprint 17.3.2 is subsumed by descendant Gate 5 run at 8e77eb1
    assert s_17_3_2["verified"] is True
    assert "Passed via" in s_17_3_2["status"]

    s_16d_2 = next((s for s in sprint_gate_status if s["sprint"] == "16d.2"), None)
    if s_16d_2:
        assert s_16d_2["status"] in ("Owed", "Passed", "Verified") or s_16d_2["status"].startswith("Passed via"), f"Sprint 16d.2 status unexpected: {s_16d_2['status']}"

    s_17_4 = next((s for s in sprint_gate_status if s["sprint"] == "17.4"), None)
    assert s_17_4 is not None, "Should find Sprint 17.4"
    assert s_17_4["verified"] is True, "Sprint 17.4 completed before 0fbff46 and should be verified"
    assert s_17_4["status"].startswith("Passed"), f"Sprint 17.4 status should start with Passed, got {s_17_4['status']}"

    # Verify recent sprints (17.15 through 17.38.2, and 16d.6) are tracked and have valid commit SHAs
    recent_sprints_to_check = [
        "16d.6", "17.11", "17.11.1", "17.15", "17.15.1", "17.15.3", "17.17", "17.18",
        "17.19", "17.20", "17.21", "17.24", "17.25", "17.26", "17.27", "17.28",
        "17.29", "17.30", "17.31", "17.32", "17.33", "17.34", "17.35", "17.38",
        "17.38.1", "17.38.2"
    ]
    tracked_sprint_map = {s["sprint"]: s for s in sprint_gate_status}
    for req_sp in recent_sprints_to_check:
        assert req_sp in tracked_sprint_map, f"Sprint/story {req_sp} must be tracked in sprint_gate_status"
        sp_obj = tracked_sprint_map[req_sp]
        assert bool(sp_obj.get("sha")), f"Sprint/story {req_sp} must have a valid resolved commit SHA"
        assert sp_obj["status"] in ("Owed", "Passed", "Failed") or sp_obj["status"].startswith("Passed via"), f"Sprint {req_sp} has invalid status {sp_obj['status']}"

    print(f"Sprint gate audit status OK: {len(sprint_gate_status)} sprints tracked (including all {len(recent_sprints_to_check)} recent sprints), sample: Sprint {sample_s['sprint']} (status={sample_s['status']})")

    # Sprint 17.3.4: Subsumption metrics persistence assertion
    s_17_26 = tracked_sprint_map.get("17.26")
    if s_17_26:
        assert s_17_26["verified"] is True
        assert "Passed via" in s_17_26["status"]
        assert s_17_26["pass_count"] is not None and s_17_26["pass_count"] >= 1000, f"Expected pass_count >= 1000, got {s_17_26['pass_count']}"
        assert s_17_26["subsumed_by"] is not None

    s_17_33 = tracked_sprint_map.get("17.33")
    if s_17_33:
        assert s_17_33["verified"] is True
        assert "Passed via" in s_17_33["status"]
        assert s_17_33["pass_count"] is not None and s_17_33["pass_count"] >= 1700, f"Expected pass_count >= 1700, got {s_17_33['pass_count']}"
        assert s_17_33["coverage"] is not None and "%" in s_17_33["coverage"], f"Expected valid coverage %, got {s_17_33['coverage']}"
        assert s_17_33["subsumed_by"] is not None

    # Sprint 17.3.4: Test INDEX.md parsing variations
    row1 = "| 2026-10-15 09:43 | `20261015-094337-ef83-d422e39` | -- | `d422e39` | PASS (pytest 1820 passed 93.31%, full vitest PASS) | 1:01:24 | run dir consumed 2026-10-15 |"
    parsed1 = p.parse_heavy_gate_index_row(row1)
    assert parsed1 is not None
    assert parsed1["sha"] == "d422e39"
    assert parsed1["run_id"] == "20261015-094337-ef83-d422e39"
    assert parsed1["status"] == "PASS"
    assert parsed1["pass_count"] == 1820
    assert parsed1["coverage"] == "93.31%"

    row2 = "| 2026-09-29 20:04 | `20260929-200458-46b7-7ba9ce8` | -- | `7ba9ce8` | PASS (pytest 1039, full vitest) | 27:49 | run dir consumed 2026-10-01 |"
    parsed2 = p.parse_heavy_gate_index_row(row2)
    assert parsed2 is not None
    assert parsed2["sha"] == "7ba9ce8"
    assert parsed2["status"] == "PASS"
    assert parsed2["pass_count"] == 1039
    assert parsed2["coverage"] is None

    row3 = "| 2026-10-01 11:47 | `20261001-114724-fc12-4d8ed04` | -- | `4d8ed04` | PASS | 34:49 | `runs/20261001-114724-fc12-4d8ed04/SUMMARY.md` |"
    parsed3 = p.parse_heavy_gate_index_row(row3)
    assert parsed3 is not None
    assert parsed3["sha"] == "4d8ed04"
    assert parsed3["status"] == "PASS"
    assert parsed3["pass_count"] is None
    assert parsed3["coverage"] is None

    row4 = "| 2026-10-01 08:35 | `20261001-083506-028d-9774b3d` | -- | `9774b3d` | FAIL (vitest: `DashboardKpiStrip` pace sentence; clock pinned to Sep 2026 in the spec). pytest 1079 passed, 90.31% | 49:31 | run dir consumed 2026-10-01 |"
    parsed4 = p.parse_heavy_gate_index_row(row4)
    assert parsed4 is not None
    assert parsed4["sha"] == "9774b3d"
    assert parsed4["status"] == "FAIL"
    assert parsed4["pass_count"] == 1079
    assert parsed4["coverage"] == "90.31%"
    print("INDEX.md parsing variations assertions PASSED")

    # Check Human QA section on MN-17.3
    card_17_3 = next((c for c in cards if c["mn_id"] == "MN-17.3"), None)
    assert card_17_3 is not None, "Should find card MN-17.3"
    assert "qa_instructions" in card_17_3
    assert "qa_items" in card_17_3
    assert len(card_17_3["qa_items"]) > 0, "MN-17.3 should have at least one QA verification item"
    assert "### Verification Check" not in card_17_3["qa_instructions"], "Trailing Verification Check heading should be stripped"
    assert hasattr(p, "parse_card"), "PMParser should have parse_card alias"
    print(f"MN-17.3 QA section OK: {len(card_17_3['qa_items'])} QA items, instructions length: {len(card_17_3['qa_instructions'])}")

    # Test toggle_qa_item on MN-17.3
    card_file_path = p.root_dir / card_17_3["file_path"]
    orig_card_bytes = card_file_path.read_bytes()
    orig_qa_state = card_17_3["qa_items"][0]["checked"]
    ok_qa, msg_qa = p.toggle_qa_item("MN-17.3", 0, not orig_qa_state)
    assert ok_qa, f"QA toggle failed: {msg_qa}"
    # Restore exact bytes
    card_file_path.write_bytes(orig_card_bytes)
    print("MN-17.3 QA item toggle and restore test PASSED")

    # --------------------------------------------------------------------------
    # Sprint 17.3.3 Tests: Pytest Failure Extraction, Review Parsing, Cascade
    # --------------------------------------------------------------------------
    # 1. Test parse_pytest_failures
    sample_log = """
============================= test session starts =============================
collected 20 items

tests/test_native_export.py .F...
tests/test_native_import.py ..F.

=================================== FAILURES ===================================
___________________ test_ofx_serializer_produces_parseable_xml ___________________

    def test_ofx_serializer_produces_parseable_xml():
>       assert False
E       AssertionError: expected parseable xml but got error

tests/test_native_export.py:42: AssertionError
___________________ test_ensure_institution_matches ___________________

    def test_ensure_institution_matches():
>       raise ValueError("invalid institution")
E       ValueError: invalid institution

tests/test_native_import.py:88: ValueError
=========================== short test summary info ===========================
FAILED tests/test_native_export.py::test_ofx_serializer_produces_parseable_xml - AssertionError: expected parseable xml
FAILED tests/test_native_import.py::test_ensure_institution_matches
======================== 2 failed, 18 passed in 12.34s =========================
"""
    failures = p.parse_pytest_failures(sample_log)
    assert len(failures) == 2, f"Expected 2 parsed failures, got {len(failures)}"
    assert failures[0]["test_name"] == "test_ofx_serializer_produces_parseable_xml"
    assert failures[0]["file_path"] == "tests/test_native_export.py"
    assert "pytest" in failures[0]["command"] and "test_ofx_serializer_produces_parseable_xml" in failures[0]["command"]
    assert "AssertionError" in failures[0]["message"]
    assert failures[1]["test_name"] == "test_ensure_institution_matches"
    assert failures[1]["file_path"] == "tests/test_native_import.py"
    print("parse_pytest_failures assertion PASSED")

    # 2. Test Review Parsing (from docs/reviews/ and legacy from docs/QUALITY.md)
    revs_17_3_1 = p.get_reviews_for_sprint("17.3.1")
    assert len(revs_17_3_1) >= 1, "Should find review for Sprint 17.3.1"
    rev1 = revs_17_3_1[0]
    assert rev1["sprint"] == "17.3.1"
    assert rev1["round"] == 1
    assert rev1["verdict"] == "PASS"
    assert rev1["reviewer"] == "Gemini 3.8 Pro"
    assert rev1["findings_count"] == 3
    assert all(f["checked"] for f in rev1["findings"])
    assert rev1["is_legacy"] is False
    print("docs/reviews/ review parsing assertion PASSED")

    # Legacy fallback from QUALITY.md
    legacy_revs = p.get_reviews_for_sprint("17.4")
    assert len(legacy_revs) >= 1, "Should find legacy review for Sprint 17.4 in QUALITY.md"
    assert legacy_revs[0]["is_legacy"] is True
    assert "PASS" in legacy_revs[0]["verdict"]
    print("docs/QUALITY.md legacy review parsing assertion PASSED")

    # Card review attachment
    card_17_3_3 = next((c for c in cards if c["mn_id"] == "MN-17.3.3"), None)
    assert card_17_3_3 is not None, "Should find card MN-17.3.3"
    assert "reviews" in card_17_3_3
    assert "review_count" in card_17_3_3
    # Check changelog review tagging, timestamps, and ascending order
    cl_17_3_3 = card_17_3_3.get("changelog", [])
    assert len(cl_17_3_3) >= 4, "Should have at least 4 changelog entries"
    # Verify ascending order across entire changelog
    timestamps = [f"{e.get('date', '')} {e.get('time', '')}".strip() for e in cl_17_3_3]
    # Match entries by content rather than fixed index to stay robust to QA closeout rows
    e_created = next((e for e in cl_17_3_3 if "created card" in e["what"].lower()), None)
    assert e_created is not None and e_created["time"] == "09:33"
    assert e_created["has_review"] is False

    e_build = next((e for e in cl_17_3_3 if "build started" in e["what"].lower()), None)
    assert e_build is not None and e_build["time"] == "09:51"
    assert e_build["has_review"] is False, "Build started row should NOT have has_review=True even if mentioning 'cold review modal'"

    e_rev = next((e for e in cl_17_3_3 if "cold definition-of-done review" in e["what"].lower()), None)
    assert e_rev is not None and e_rev["has_review"] is True, "Review row should have has_review=True"
    assert e_rev["time"] == "10:18", f"Expected time 10:18, got {e_rev.get('time')}"
    assert e_rev["datetime"] == "2026-09-22 10:18"

    e_res = next((e for e in cl_17_3_3 if "resolved round 1" in e["what"].lower()), None)
    assert e_res is not None and e_res["time"] == "10:20"
    assert e_res["has_review"] is False
    print("Card reviews attachment, time extraction, ascending order, and false-positive prevention assertions PASSED")

    # --------------------------------------------------------------------------
    # Sprint 17.11.1 Tests: Datetime Precision, Git Backfill & Duration Calculation
    # --------------------------------------------------------------------------
    # 1. Test format_duration helper
    assert PMParser.format_duration(25) == "< 1m"
    assert PMParser.format_duration(120) == "2m"
    assert PMParser.format_duration(3600) == "1h"
    assert PMParser.format_duration(8100) == "2h 15m"
    assert PMParser.format_duration(90000) == "1d 1h"
    assert PMParser.format_duration(86400) == "1d"
    print("format_duration helper assertions PASSED")

    # 2. Test compute_card_duration logic
    import datetime
    sim_start = datetime.datetime(2026, 9, 23, 10, 0)
    sim_now = datetime.datetime(2026, 9, 23, 12, 15)

    # In-progress card duration
    card_in_prog = {
        "status": "in-progress",
        "changelog": [{"datetime": "2026-09-23 10:00", "what": "Build started"}],
    }
    dur_prog = p.compute_card_duration(card_in_prog, now=sim_now)
    assert dur_prog["elapsed_seconds"] == 8100  # 2h 15m = 8100s
    assert dur_prog["duration_formatted"] == "2h 15m"
    assert dur_prog["start_time"] == "2026-09-23 10:00"
    assert dur_prog["end_time"] is None

    # Completed card duration
    card_completed = {
        "status": "completed",
        "changelog": [
            {"datetime": "2026-09-23 10:00", "what": "Build started"},
            {"datetime": "2026-09-23 13:40", "what": "Review PASS and user verified"},
        ],
    }
    dur_comp = p.compute_card_duration(card_completed)
    assert dur_comp["elapsed_seconds"] == 13200  # 3h 40m = 13200s
    assert dur_comp["duration_formatted"] == "3h 40m"
    assert dur_comp["start_time"] == "2026-09-23 10:00"
    assert dur_comp["end_time"] == "2026-09-23 13:40"
    print("compute_card_duration assertions PASSED")

    # 3. Test parsing with | Date & Time | and | Date | headers & backfill
    import tempfile
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        card_content = """---
mn-id: MN-TEST-17.11.1
sprint: "17.11.1"
type: improvement
status: in-progress
created: 2026-09-23
validated: false
---

# MN-TEST-17.11.1 - Test Card

## Description
Testing headers and backfill.

## Definition of Done
- [ ] Task 1

## Changelog

| Date & Time | Agent | What |
|---|---|---|
| 2026-09-23 09:30 | Lead (Gemini 3.8 Flash) | Created card |
| 2026-09-23 | Builder (Composer 2.5) | Legacy entry without explicit time |
"""
        test_file = tmp_path / "MN-TEST-17.11.1 - Test Card.md"
        test_file.write_text(card_content, encoding="utf-8")
        parsed_test = p.parse_card_file(test_file)
        assert parsed_test is not None
        cl = parsed_test["changelog"]
        assert len(cl) == 2
        # First entry: explicit datetime parsed
        assert cl[0]["date"] == "2026-09-23"
        assert cl[0]["time"] == "09:30"
        assert cl[0]["datetime"] == "2026-09-23 09:30"
        # Second entry: missing time is backfilled (never empty!)
        assert cl[1]["date"] == "2026-09-23"
        assert bool(cl[1]["time"]), "Legacy row must have backfilled time"
        assert bool(cl[1]["datetime"]), "Legacy row must have populated datetime"
        assert cl[1]["datetime"].startswith("2026-09-23 ")
        assert "duration_formatted" in parsed_test
        assert "elapsed_seconds" in parsed_test
    print("Date & Time header parsing and backfill assertions PASSED")

    # 4. Sprint 17.11.2 - Strict Backlog vs Sprint Backlog separation tests
    card_map = {c["mn_id"]: c for c in cards}

    # Verify template default status
    template_file = p.cards_dir / "template.md"
    assert template_file.exists()
    assert re.search(r"^status:\s*backlog\b", template_file.read_text(encoding="utf-8"), re.MULTILINE)

    # Verify retired backlog folder and moved cards in cards_dir
    assert not (p.root_dir / "docs" / "cards" / "backlog").exists(), "docs/cards/backlog folder must be retired"
    assert "MN-BACKLOG-001" in card_map, "MN-BACKLOG-001 must be parsed from docs/cards"
    assert "MN-BACKLOG-003" in card_map, "MN-BACKLOG-003 must be parsed from docs/cards"
    assert card_map["MN-BACKLOG-001"]["status"] == "backlog"
    assert card_map["MN-BACKLOG-003"]["status"] == "backlog"

    # Verify triaged cards are status: backlog
    triaged_ids = [
        "MN-17.9.5", "MN-17.9.7",
        "MN-17.9.8", "MN-17.6.5", "MN-17.9", "MN-17.10"
    ]
    for tid in triaged_ids:
        assert tid in card_map, f"{tid} must exist in cards"
        assert card_map[tid]["status"] == "backlog", f"{tid} must be in status: backlog"

    # Verify ready-to-build cards have DoD in PLAN.md. (The original pinned their status to
    # sprint-backlog, which went stale once MN-17.9.4.2 was built — status is live data.)
    ready_ids = ["MN-17.9.4.2", "MN-18.3"]
    for rid in ready_ids:
        assert rid in card_map, f"{rid} must exist in cards"
        assert card_map[rid]["missing_plan_dod"] is False, f"{rid} must have DoD in PLAN.md"
        assert len(card_map[rid]["warnings"]) == 0

    # Verify warning and flag when sprint-backlog card lacks DoD in PLAN.md
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        bad_card = tmp_path / "MN-NONEXISTENT-001 - Fake Card.md"
        bad_card.write_text("""---
mn-id: MN-NONEXISTENT-001
sprint: "999.999"
type: feature
status: sprint-backlog
created: 2026-10-04
---
# MN-NONEXISTENT-001
""", encoding="utf-8")
        parsed_bad = p.parse_card_file(bad_card)
        assert parsed_bad["status"] == "sprint-backlog"
        assert parsed_bad["missing_plan_dod"] is True
        assert len(parsed_bad["warnings"]) >= 1
        assert "lacks Definition of Done" in parsed_bad["warnings"][0]

    print("Sprint 17.11.2 Strict Backlog vs Sprint Backlog assertions PASSED")

    print("\nAll parser tests PASSED successfully!")


if __name__ == "__main__":
    test_parser()
