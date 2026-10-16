"""PQC-2.1 (R2-Q8): the shipped hash ledger recognises past releases and forces a VERSION bump."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import setup_flow

SKILLS = setup_flow.TOOL_ROOT / "skills"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_every_shipped_skill_is_recorded_under_the_current_version():
    """Fails when a skill changed without a VERSION bump + `scripts/skill_ledger.py record`."""
    ledger = setup_flow.load_ledger()
    version = setup_flow.tool_version()
    assert version in ledger, f"run `uv run python scripts/skill_ledger.py record` for {version}"
    for d in (p for p in SKILLS.iterdir() if p.is_dir()):
        assert setup_flow.content_hash(d) in ledger[version].get(d.name, []), (
            f"{d.name} changed: bump VERSION and every skill's metadata.version, then run "
            "`uv run python scripts/skill_ledger.py record`"
        )


def test_a_hash_belongs_to_one_version_only():
    seen: dict[tuple[str, str], str] = {}
    for version, skills in setup_flow.load_ledger().items():
        for skill, hashes in skills.items():
            for h in hashes:
                assert (skill, h) not in seen, f"{skill} {h[:8]} under {seen.get((skill, h))} and {version}"
                seen[(skill, h)] = version


def test_record_refuses_a_new_hash_under_an_existing_version(tmp_path):
    skills = tmp_path / "skills"
    shutil.copytree(SKILLS / "pm-sync", skills / "pm-sync")
    ledger = tmp_path / "RELEASES.json"
    assert setup_flow.record_release(skills, "1.0.0", ledger) == {"pm-sync": "recorded"}
    assert setup_flow.record_release(skills, "1.0.0", ledger) == {"pm-sync": "already recorded"}
    md = skills / "pm-sync" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    before = ledger.read_bytes()
    with pytest.raises(ValueError, match="bump VERSION"):
        setup_flow.record_release(skills, "1.0.0", ledger)
    assert ledger.read_bytes() == before, "a refused record writes nothing"
    assert setup_flow.record_release(skills, "1.0.1", ledger) == {"pm-sync": "recorded"}
    assert set(json.loads(ledger.read_text(encoding="utf-8"))) == {"1.0.0", "1.0.1"}


def test_a_pre_metadata_copy_is_ours_and_outdated(tmp_path):
    """The 2026-10-16 global copies: no metadata block, no manifest. Only the ledger proves them ours."""
    old = tmp_path / "pm-orchestrate"
    shutil.copytree(FIXTURES / "skills-0.1.0" / "pm-orchestrate", old)
    assert "metadata:" not in (old / "SKILL.md").read_text(encoding="utf-8")
    st = setup_flow.skill_state(old, SKILLS / "pm-orchestrate")
    assert st["state"] == "outdated" and st["installed_version"] == "0.1.0"
    assert setup_flow.global_copy_status(old, "pm-orchestrate") == "ours"
    # one changed byte and it is no longer provably ours
    (old / "SKILL.md").write_text((old / "SKILL.md").read_text(encoding="utf-8") + "x", encoding="utf-8")
    assert setup_flow.skill_state(old, SKILLS / "pm-orchestrate")["state"] != "outdated"
    assert setup_flow.global_copy_status(old, "pm-orchestrate") != "ours"


def test_a_crlf_copy_of_an_old_release_still_matches(tmp_path):
    old = tmp_path / "pm-qa-setup"
    shutil.copytree(FIXTURES / "skills-0.2.0" / "pm-qa-setup", old)
    md = old / "SKILL.md"
    md.write_bytes(md.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert setup_flow.skill_state(old, SKILLS / "pm-qa-setup")["installed_version"] == "0.2.0"
