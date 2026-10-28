from __future__ import annotations

import subprocess
import sys
import time
import textwrap
from pathlib import Path

import pytest

BOARD = Path(__file__).resolve().parent.parent / "board"
BOARD = BOARD
sys.path.insert(0, str(BOARD))


def git(cwd: Path, *args: str) -> str:
    # Windows intermittently fails object writes (AV file locks -> exit 128); retry before failing.
    for attempt in range(3):
        res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
        if res.returncode == 0 or attempt == 2:
            break
        time.sleep(0.3)
    if res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, res.args, res.stdout, res.stderr)
    return res.stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    return path


LEGACY_CARD = textwrap.dedent(
    """\
    ---
    mn-id: MN-1.2
    sprint: "1.2"
    type: feature
    status: sprint-backlog
    created: 2026-10-01
    validated: false
    ---

    # MN-1.2 - Legacy sprint card

    ## Definition of Done / Acceptance Criteria

    - [ ] Legacy item

    ## Human QA Test

    - [ ] Legacy QA check

    ## Changelog

    | Date & Time | Agent | What |
    |---|---|---|
    | 2026-10-01 10:00 | Lead | Created |
    """
)

STORY_CARD = textwrap.dedent(
    """\
    ---
    id: APP-3.1
    story: "3.1"
    type: feature
    status: story-backlog
    created: 2026-10-02
    validated: false
    ---

    # APP-3.1 - New story card

    ## Definition of Done / Acceptance Criteria

    - [ ] Story item one
    - [x] Story item two

    ## Human QA Test

    1. Open the page.

    - [ ] Story QA check

    ## Changelog

    | Date & Time | Agent | What |
    |---|---|---|
    | 2026-10-02 10:00 | Lead | Created |
    """
)

LEGACY_PLAN = textwrap.dedent(
    """\
    # Plan

    | Sprint | Goal | Status |
    |---|---|---|
    | Sprint 1.2 | Legacy | Sprint backlog |

    ## Sprint 1.2 — Legacy

    ### Definition of Done / Acceptance Criteria — Sprint 1.2

    - [ ] Legacy plan item
    """
)

STORY_PLAN = textwrap.dedent(
    """\
    # Plan

    | Story | Goal | Status |
    |---|---|---|
    | Story 3.1 | New | Story backlog |

    ## Story 3.1 — New story

    ### Definition of Done / Acceptance Criteria — Story 3.1

    - [ ] Story plan item
    """
)


@pytest.fixture(autouse=True)
def board_home(tmp_path, monkeypatch):
    home = tmp_path / "board-home"
    monkeypatch.setenv("PM_QA_BOARD_HOME", str(home))
    return home


@pytest.fixture
def legacy_project(tmp_path):
    """money-nest shaped: PLAN.md at the code root, cards under <code>/docs/cards."""
    code = init_repo(tmp_path / "legacy-app")
    (code / "docs" / "cards").mkdir(parents=True)
    (code / "docs" / "cards" / "MN-1.2 - Legacy sprint card.md").write_text(LEGACY_CARD, encoding="utf-8")
    (code / "PLAN.md").write_text(LEGACY_PLAN, encoding="utf-8")
    (code / "README.md").write_text("legacy\n", encoding="utf-8")
    git(code, "add", "-A")
    git(code, "commit", "-q", "-m", "init")
    return code


@pytest.fixture
def external_project(tmp_path):
    """Code repo with no PM files; all PM docs live in an external folder."""
    code = init_repo(tmp_path / "story-app")
    (code / "main.py").write_text("print('hi')\n", encoding="utf-8")
    git(code, "add", "-A")
    git(code, "commit", "-q", "-m", "code init")
    docs = tmp_path / "story-app-docs"
    return code, docs


def seed_external_docs(docs: Path) -> None:
    (docs / "cards" / "APP-3.1 - New story card.md").write_text(STORY_CARD, encoding="utf-8")
    (docs / "PLAN.md").write_text(STORY_PLAN, encoding="utf-8")
