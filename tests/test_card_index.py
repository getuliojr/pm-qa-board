import json
import pytest
from pathlib import Path
from index import CardIndex


def test_card_index_crud(tmp_path: Path):
    db_file = tmp_path / "test.db"
    index = CardIndex(db_file)

    # Initial state
    assert index.get_all_file_stats() == {}
    assert index.get_all_cards() == {}
    assert index.count_by_status("in-progress") == 0

    # Insert cards
    card1 = {"mn_id": "MN-1", "title": "First", "status": "in-progress", "sprint": "1"}
    card2 = {"mn_id": "MN-2", "title": "Second", "status": "completed", "sprint": "1"}

    index.upsert_cards_batch([
        ("cards/MN-1.md", 100.0, 500, card1),
        ("cards/MN-2.md", 101.0, 600, card2),
    ])

    stats = index.get_all_file_stats()
    assert len(stats) == 2
    assert stats["cards/MN-1.md"] == (100.0, 500)
    assert stats["cards/MN-2.md"] == (101.0, 600)

    cards = index.get_all_cards()
    assert len(cards) == 2
    assert cards["cards/MN-1.md"]["title"] == "First"
    assert cards["cards/MN-2.md"]["title"] == "Second"

    assert index.count_by_status("in-progress") == 1
    assert index.count_by_status("completed") == 1
    assert index.count_by_status("backlog") == 0

    # Update card
    card1_updated = {"mn_id": "MN-1", "title": "First Updated", "status": "completed", "sprint": "1"}
    index.upsert_cards_batch([
        ("cards/MN-1.md", 102.0, 550, card1_updated),
    ])

    stats = index.get_all_file_stats()
    assert stats["cards/MN-1.md"] == (102.0, 550)
    assert index.count_by_status("in-progress") == 0
    assert index.count_by_status("completed") == 2

    # Remove card
    index.remove_cards(["cards/MN-2.md"])
    stats = index.get_all_file_stats()
    assert len(stats) == 1
    assert "cards/MN-2.md" not in stats

    # Clear
    index.clear()
    assert index.get_all_file_stats() == {}


def test_parser_with_card_index(tmp_path: Path, monkeypatch):
    from parser import PMParser
    monkeypatch.setenv("PM_QA_COCKPIT_HOME", str(tmp_path / ".pm-qa-cockpit"))

    docs = tmp_path / "docs"
    cards_dir = docs / "cards"
    cards_dir.mkdir(parents=True)

    card_file = cards_dir / "MN-101 - Sample Story.md"
    card_file.write_text(
        "---\n"
        "id: MN-101\n"
        "status: in-progress\n"
        "story: 1\n"
        "---\n"
        "# MN-101 - Sample Story\n"
        "## Description\n"
        "Test description\n"
        "## Definition of Done\n"
        "- [x] DoD 1\n"
        "- [ ] DoD 2\n",
        encoding="utf-8",
    )

    parser = PMParser(root_dir=tmp_path, docs_path=docs, project={"slug": "test-proj"})
    assert parser._index is not None

    # First call indexes the card
    cards = parser.get_all_cards()
    assert len(cards) == 1
    assert cards[0]["mn_id"] == "MN-101"
    assert cards[0]["status"] == "in-progress"

    # Verify card is stored in sqlite index
    stats = parser._index.get_all_file_stats()
    assert len(stats) == 1

    # Invalidate in-memory cache and call get_all_cards again (should load from index)
    parser.invalidate_cache()
    cards_second = parser.get_all_cards()
    assert len(cards_second) == 1
    assert cards_second[0]["mn_id"] == "MN-101"

    # Modify card on disk
    card_file.write_text(
        "---\n"
        "id: MN-101\n"
        "status: completed\n"
        "story: 1\n"
        "---\n"
        "# MN-101 - Sample Story Updated\n"
        "## Description\n"
        "Updated\n"
        "## Definition of Done\n"
        "- [x] DoD 1\n"
        "- [x] DoD 2\n",
        encoding="utf-8",
    )

    # Parser picks up change
    cards_updated = parser.get_all_cards(refresh=True)
    assert len(cards_updated) == 1
    assert cards_updated[0]["status"] == "completed"
    assert cards_updated[0]["title"] == "MN-101 - Sample Story Updated"

    # Delete card from disk
    card_file.unlink()
    cards_after_delete = parser.get_all_cards(refresh=True)
    assert len(cards_after_delete) == 0
    assert len(parser._index.get_all_file_stats()) == 0


def test_cached_cards_receive_new_reviews_and_artifacts(tmp_path: Path, monkeypatch):
    from parser import PMParser
    monkeypatch.setenv("PM_QA_COCKPIT_HOME", str(tmp_path / ".pm-qa-cockpit"))

    docs = tmp_path / "docs"
    cards_dir = docs / "cards"
    cards_dir.mkdir(parents=True)
    reviews_dir = docs / "reviews"
    reviews_dir.mkdir(parents=True)
    artifacts_dir = docs / "artifacts"
    artifacts_dir.mkdir(parents=True)

    card_file = cards_dir / "MN-200 - Review Test Story.md"
    card_file.write_text(
        "---\n"
        "id: MN-200\n"
        "status: qa\n"
        "story: 200\n"
        "---\n"
        "# MN-200 - Review Test Story\n",
        encoding="utf-8",
    )

    parser = PMParser(root_dir=tmp_path, docs_path=docs, project={"slug": "rev-test"})
    cards = parser.get_all_cards()
    assert len(cards) == 1
    assert cards[0]["reviews"] == []
    assert cards[0]["closeout"] is None

    # Add a review file without touching the card file
    rev_file = reviews_dir / "MN-200-round-1.md"
    rev_file.write_text(
        "---\n"
        "story: 200\n"
        "card: MN-200\n"
        "round: 1\n"
        "verdict: PASS\n"
        "reviewer: Cold Reviewer\n"
        "---\n"
        "Review details\n",
        encoding="utf-8",
    )

    # Add closeout artifact file
    closeout_file = artifacts_dir / "MN-200-closeout.html"
    closeout_file.write_text("<html>Closeout</html>", encoding="utf-8")

    # Invalidate parser memory cache so it reads from SQLite index
    parser.invalidate_cache()
    cards_with_review = parser.get_all_cards(refresh=True)
    assert len(cards_with_review) == 1
    assert cards_with_review[0]["review_count"] == 1
    assert cards_with_review[0]["reviews"][0]["verdict"] == "PASS"
    assert cards_with_review[0]["closeout"] is not None
    assert cards_with_review[0]["closeout"]["file_name"] == "MN-200-closeout.html"


