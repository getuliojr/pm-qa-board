#!/usr/bin/env python3
"""
setup_sandbox.py - Rebuild PQC-2.1's Human QA starting state (R3-Q12) in a throwaway folder.

    uv run python scripts/setup_sandbox.py [--dir FOLDER] [--dev-first]

Creates a fake HOME (old global cockpit copies, grill-me only for Antigravity, pm-qa-setup 0.2.0, a
registry with money-nest) and a money-nest-shaped project. The fake HOME's running copy is a local
clone of this repo's **committed HEAD**, so commit first. Nothing outside FOLDER is touched: the
cockpit reads ``PM_QA_USER_HOME`` instead of your real home, while your agent keeps its own login.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import sandbox_fixture  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the PQC-2.1 QA sandbox (fake HOME + money-nest-shaped project)")
    ap.add_argument("--dir", help="Empty or missing folder to build in (default: a new temp folder)")
    ap.add_argument("--dev-first", action="store_true", help="Make scripts/pm.ps1 prefer the dev clone (QA step 6)")
    args = ap.parse_args(argv)
    root = Path(args.dir).resolve() if args.dir else Path(tempfile.mkdtemp(prefix="pqc-2.1-sandbox-"))
    if root.exists() and any(root.iterdir()):
        print(f"Error: {root} is not empty; pick a new folder.")
        return 1
    paths = sandbox_fixture.build(root, dev_first=args.dev_first, running_copy=ROOT)
    home, project = paths["home"], paths["project"]
    skill = home / ".pm-qa-cockpit" / "app" / "skills" / "pm-qa-setup" / "SKILL.md"
    cli = home / ".pm-qa-cockpit" / "app" / "board" / "cli.py"
    print(f"""Sandbox ready.
  fake HOME: {home}
  project:   {project}

In a new PowerShell window:
  $env:PM_QA_USER_HOME = "{home}"
  Set-Location "{project}"
  claude            # or your agent of choice; it keeps its own login

Then say:
  Set up PM & QA Cockpit in this project. Follow the pm-qa-setup skill at
  {skill}
  (not the copy in your global skills folder), with CLI = uv run python {cli}

Doctor at any time (same window):
  uv run python "{cli}" doctor money-nest
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
