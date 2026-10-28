"""PQC-34: the old product name must not come back. Only the migration code and frozen history may mention it."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", ".claude", "__pycache__", ".pytest_cache", "node_modules"}
SKIP_FILES = {"uv.lock"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".db", ".sqlite", ".sqlite3", ".lnk", ".pyc", ".exe", ".woff", ".woff2", ".zip"}
# migrate.py must know the old names to move an old install; RELEASES.json keeps released history keyed by
# the old skill name; fixtures/skills-* are frozen copies of released old skills used by the upgrade tests.
ALLOWED_FILES = {"board/migrate.py", "tests/test_migrate_home.py", "tests/test_rename_guard.py", "skills/RELEASES.json"}
ALLOWED_PREFIXES = ("tests/fixtures/skills-",)
# the private PM-data folder keeps its name (Story 34 decision 4): only this exact path is exempt
PRIVATE_DOCS = "pm-qa-docs/pm-qa-cockpit"


def _offending_lines(text: str) -> list[tuple[int, str]]:
    return [(n, line) for n, line in enumerate(text.splitlines(), 1)
            if re.search("cockpit", line.replace(PRIVATE_DOCS, ""), re.IGNORECASE)]


def test_old_product_name_only_where_allowed():
    offenders = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT).as_posix()
        if not p.is_file() or p.name in SKIP_FILES or p.suffix.lower() in BINARY_EXT:
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if rel in ALLOWED_FILES or rel.startswith(ALLOWED_PREFIXES):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary file with an unlisted extension
        offenders += [f"{rel}:{n}: {line.strip()[:100]}" for n, line in _offending_lines(text)]
    assert not offenders, "the old name is back:\n" + "\n".join(offenders[:20])


def test_old_package_folder_is_gone():
    assert not (ROOT / "cockpit").exists()
    assert not (ROOT / "skills" / "pm-qa-cockpit").exists()
    assert (ROOT / "skills" / "pm-qa-board" / "SKILL.md").is_file()
