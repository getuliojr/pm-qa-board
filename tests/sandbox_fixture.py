"""
sandbox_fixture.py - The 2026-10-16 starting state of PQC-2.1's Human QA, rebuilt on demand (R3-Q12).

A fake HOME and a money-nest-shaped project, as the machine looked before it was fixed by hand:

* fake HOME
  - ``~/.claude/skills``, ``~/.agents/skills``, ``~/.cursor/skills-cursor``: the four cockpit skills as
    0.1.0 shipped them (no metadata block, no manifest) and ``pm-qa-setup`` 0.2.0;
  - grill-me / grilling **only** in ``~/.agents/skills`` (Antigravity), so Claude Code has none;
  - ``~/.pm-qa-cockpit``: the registry (money-nest registered, heavy gates) and a running copy.
* project ``money-nest``: its own loop skills, grill-me / grilling in ``.claude/skills``, CLAUDE.md /
  AGENTS.md / GEMINI.md, ``.cursor`` and ``.agents``, a hook, cards in ``docs/cards`` and the
  ``scripts/pm.ps1`` launcher (running copy first; ``dev_first=True`` makes it prefer the dev clone).

The automated tests and ``scripts/setup_sandbox.py`` (the Human QA reproduction) both build it here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COCKPIT_SKILLS = ("pm-qa-cockpit", "pm-orchestrate", "pm-sync", "pm-review")
GLOBAL_DIRS = (".claude/skills", ".agents/skills", ".cursor/skills-cursor")

GRILL_ME = "---\nname: grill-me\ndescription: Interview the user relentlessly about a plan or design.\n---\n\nAsk one question at a time.\n"
GRILLING = "---\nname: grilling\ndescription: Grill the user about a plan.\n---\n\nStress-test the design.\n"
OWN_LOOP = "---\nname: {name}\ndescription: money-nest's own story loop step.\n---\n\nmoney-nest rules.\n"

PM_PS1_RUNNING_FIRST = r"""#Requires -Version 5.1
<#
.SYNOPSIS
    Launches the PM & QA Cockpit on port 3333, opened on money-nest.

.DESCRIPTION
    Looks in order at $env:PM_QA_COCKPIT_REPO, the released running copy ~/.pm-qa-cockpit/app,
    then the dev clone ..\pm-qa-cockpit, then the frozen tools\pm-dashboard copy.
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
# Prefer the stable running copy over the dev clone ..\pm-qa-cockpit
$AppCopy = Join-Path $HOME ".pm-qa-cockpit\app"
$CockpitRepo = if ($env:PM_QA_COCKPIT_REPO) { $env:PM_QA_COCKPIT_REPO }
    elseif (Test-Path (Join-Path $AppCopy "cockpit\server.py")) { $AppCopy }
    else { Join-Path (Split-Path -Parent $RootDir) "pm-qa-cockpit" }
$CockpitServer = Join-Path $CockpitRepo "cockpit\server.py"
if (Test-Path $CockpitServer) {
    $ServerPath = $CockpitServer
    $env:PM_QA_COCKPIT_PROJECT_ROOT = $RootDir
} else {
    $ServerPath = Join-Path $RootDir "tools\pm-dashboard\server.py"
}
Set-Location $RootDir
& uv run python $ServerPath
"""

# The QA "broken launcher": the dev clone is tried before the running copy.
DEV_FIRST_LINE = '$CockpitRepo = if (Test-Path "..\\pm-qa-cockpit\\cockpit\\server.py") { "..\\pm-qa-cockpit" }'
PM_PS1_DEV_FIRST = PM_PS1_RUNNING_FIRST.replace(
    "$CockpitRepo = if ($env:PM_QA_COCKPIT_REPO) { $env:PM_QA_COCKPIT_REPO }",
    DEV_FIRST_LINE,
)

CARD = """---
id: MN-1.1
story: "1.1"
type: feature
status: story-backlog
created: 2026-10-01
validated: false
---

# MN-1.1 - A money-nest card

## Definition of Done / Acceptance Criteria

- [ ] Something

## Human QA Test

- [ ] Check it

## Changelog

| Date & Time | Agent | What |
|---|---|---|
| 2026-10-01 10:00 | Lead | Created |
"""

PLAN = """# Plan

| Story | Goal | Status |
|---|---|---|
| Story 1.1 | Something | Story backlog |

## Story 1.1 — Something

### Definition of Done / Acceptance Criteria — Story 1.1

- [ ] Something
"""


def _git(cwd: Path, *args: str) -> None:
    # Windows intermittently fails object writes (AV file locks -> exit 128); retry before failing.
    for attempt in range(4):
        res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
        if res.returncode == 0:
            return
        time.sleep(0.4 * (attempt + 1))
    raise subprocess.CalledProcessError(res.returncode, res.args, res.stdout, res.stderr)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def stub_running_copy(app: Path, version: str) -> None:
    """Just enough of a running copy for the checks (tests). The QA script clones the real one."""
    _write(app / "cockpit" / "server.py", "# running copy stub\n")
    _write(app / "dashboard.ps1", "# running copy stub\n")
    _write(app / "VERSION", version + "\n")


def build(root: Path, *, dev_first: bool = False, running_copy: Path | None = None,
          version: str | None = None) -> dict[str, Path]:
    """Create ``root/home`` and ``root/money-nest``. ``running_copy``: a cockpit checkout to clone
    into the fake HOME (the QA script passes the dev clone); default a stub at ``version``."""
    home, code = root / "home", root / "money-nest"
    for rel in GLOBAL_DIRS:
        d = home / rel
        for s in COCKPIT_SKILLS:
            shutil.copytree(FIXTURES / "skills-0.1.0" / s, d / s)
        shutil.copytree(FIXTURES / "skills-0.2.0" / "pm-qa-setup", d / "pm-qa-setup")
    _write(home / ".agents" / "skills" / "grill-me" / "SKILL.md", GRILL_ME)
    _write(home / ".agents" / "skills" / "grilling" / "SKILL.md", GRILLING)

    code.mkdir(parents=True)
    _git(code, "init", "-q")
    _git(code, "config", "user.email", "sandbox@example.com")
    _git(code, "config", "user.name", "Sandbox")
    _write(code / ".claude" / "skills" / "grill-me" / "SKILL.md", GRILL_ME)
    _write(code / ".claude" / "skills" / "grilling" / "SKILL.md", GRILLING)
    for name in ("money-nest-orchestrate", "money-nest-pm-sync", "money-nest-opus-review"):
        _write(code / ".claude" / "skills" / name / "SKILL.md", OWN_LOOP.format(name=name))
    _write(code / ".claude" / "settings.json", json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": 'node "${CLAUDE_PROJECT_DIR}/.claude/skills/money-nest-pm-sync/scripts/start-guard.mjs"'}]}]}},
        indent=2) + "\n")
    _write(code / ".agents" / "skills" / "money-nest-builder" / "SKILL.md", OWN_LOOP.format(name="money-nest-builder"))
    _write(code / ".cursor" / "rules" / "money-nest.mdc", "Follow AGENTS.md.\n")
    _write(code / "CLAUDE.md", "# money-nest\n\nRun the money-nest-orchestrate loop. Never push.\n")
    _write(code / "AGENTS.md", "# money-nest agents\n\nCards live in docs/cards. Never push.\n")
    _write(code / "GEMINI.md", "# money-nest (Antigravity)\n\nSee AGENTS.md.\n")
    # money-nest's own rosters (read and reported, never written): Claude 3 roles (alias pins), Cursor 5, Antigravity 4.
    for tool_dir, role, name, model in (
        (".claude/agents", "advisor", "money-nest-advisor", "opus"), (".claude/agents", "builder", "money-nest-builder", "sonnet"),
        (".claude/agents", "retrieval", "money-nest-retrieval", "claude-haiku-5-5"),
        (".cursor/agents", "advisor", "advisor", "grok-4.6[fast=false]"), (".cursor/agents", "builder", "builder", "grok-4.6[fast=false]"),
        (".cursor/agents", "reviewer", "reviewer", "grok-4.7[fast=false]"), (".cursor/agents", "retrieval", "retrieve", "composer-2.5[fast=false]"),
        (".cursor/agents", "pm-sync", "pm-sync", "composer-2.5[fast=false]"),
        (".agents/subagents", "advisor", "advisor", "pro"), (".agents/subagents", "builder", "builder", "pro"),
        (".agents/subagents", "reviewer", "reviewer", "pro"), (".agents/subagents", "retrieval", "retrieve", "flash"),
    ):
        _write(code / tool_dir / f"{name}.md",
               f"---\nname: {name}\ndescription: money-nest {role} agent.\nmodel: {model}\n---\n\nmoney-nest {role} rules.\n")
    _write(code / "scripts" / "pm.ps1", PM_PS1_DEV_FIRST if dev_first else PM_PS1_RUNNING_FIRST)
    _write(code / "docs" / "cards" / "MN-1.1 - A money-nest card.md", CARD)
    _write(code / "PLAN.md", PLAN)
    _git(code, "add", "-A")
    _git(code, "commit", "-q", "-m", "money-nest-shaped sandbox")

    cockpit = home / ".pm-qa-cockpit"
    if running_copy is not None:
        subprocess.run(["git", "clone", "-q", str(running_copy), str(cockpit / "app")], capture_output=True, check=True)
    else:
        stub_running_copy(cockpit / "app", version or "0.0.0")
    code_s = str(code.resolve()).replace("\\", "/")
    _write(cockpit / "projects.json", json.dumps({"active_project": "money-nest", "projects": {"money-nest": {
        "name": "Money-Nest", "slug": "money-nest", "prefix": "MN", "code_path": code_s,
        "docs_path": code_s + "/docs", "docs_mode": "in-repo",
        "gates": {"mode": "heavy", "reason": "sandbox", "threshold_seconds": 360}}}}, indent=2) + "\n")
    return {"home": home, "project": code, "cockpit_home": cockpit}
