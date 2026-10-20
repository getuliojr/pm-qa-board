#!/usr/bin/env python3
"""
skill_ledger.py - Dev tool for ``skills/RELEASES.json`` (Story 2.1, R2-Q8).

The ledger lists every content hash each shipped skill has had under each released version, so a copy
installed by any past release (even one with no ``metadata:`` block or install manifest) is recognised
as ours and unedited. Runtime never needs git: this script is the only thing that reads history.

    uv run python scripts/skill_ledger.py record     # last step of a release: current hashes under VERSION
    uv run python scripts/skill_ledger.py backfill   # rebuild the past entries from git history

``record`` refuses to add a second hash to a version that already has one: bump VERSION instead.
Commits before the VERSION file existed are recorded as 0.1.0 (the pre-metadata copies).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "board"))
import setup_flow  # noqa: E402

PRE_VERSION = "0.1.0"


def _git(*args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, check=True)
    return res.stdout.decode("utf-8", errors="replace")


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, check=True).stdout


def backfill() -> dict[str, dict[str, list[str]]]:
    """{version: {skill: [hashes]}} for every commit that touched skills/ or VERSION."""
    ledger = setup_flow.load_ledger()
    for sha in reversed(_git("log", "--format=%H", "--", "skills", "VERSION").split()):
        try:
            version = _git("show", f"{sha}:VERSION").strip() or PRE_VERSION
        except subprocess.CalledProcessError:
            version = PRE_VERSION
        files: dict[str, dict[str, bytes]] = {}
        for path in _git("ls-tree", "-r", "--name-only", sha, "skills/").splitlines():
            parts = path.split("/")
            if len(parts) < 3 or parts[-1] == setup_flow.MANIFEST:
                continue  # skills/RELEASES.json and the like are not inside a skill
            files.setdefault(parts[1], {})["/".join(parts[2:])] = _git_bytes("show", f"{sha}:{path}")
        for skill, tree in files.items():
            if "SKILL.md" not in tree:
                continue
            known = ledger.setdefault(version, {}).setdefault(skill, [])
            digest = setup_flow.files_hash(tree)
            if digest not in known:
                known.append(digest)
    return ledger


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("record", "backfill"))
    args = ap.parse_args(argv)
    if args.command == "backfill":
        ledger = backfill()
        setup_flow.LEDGER.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for version in sorted(ledger, key=setup_flow._vtuple):
            print(f"{version}: " + ", ".join(f"{s} x{len(h)}" for s, h in sorted(ledger[version].items())))
        return 0
    version = setup_flow.tool_version()
    try:
        out = setup_flow.record_release(setup_flow.TOOL_ROOT / "skills", version)
    except ValueError as err:
        print(f"Error: {err}")
        return 1
    for skill, what in out.items():
        print(f"{version} {skill}: {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
