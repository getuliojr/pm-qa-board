"""
backfill_dates.py - One-time and recurring backfill of created date in Obsidian card frontmatter.

Hierarchy used to determine card creation date:
1. Earliest entry in the card's ## Changelog table.
2. Git commit addition date (git log --diff-filter=A --format=%as -1 -- <file>).
3. "designed YYYY-MM-DD" match in card body.
4. File creation timestamp on disk (stat().st_ctime).
"""

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def get_card_creation_date(file_path: Path, root_dir: Path) -> str:
    content = file_path.read_text(encoding="utf-8", errors="replace")

    # 1. Earliest Changelog date
    cl_match = re.search(
        r"##\s+Changelog\s*\r?\n.*?\r?\n\|[-|\s]+\r?\n(.*?)(?=\r?\n##|\Z)",
        content,
        re.DOTALL,
    )
    if cl_match:
        cl_dates = []
        for row in cl_match.group(1).splitlines():
            parts = [p.strip() for p in row.strip().strip("|").split("|")]
            if parts and re.match(r"^\d{4}-\d{2}-\d{2}", parts[0]):
                cl_dates.append(parts[0][:10])
        if cl_dates:
            return sorted(cl_dates)[0]

    # 2. Git commit addition date
    try:
        res = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%as", "-1", "--", str(file_path)],
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        git_date = res.stdout.strip()
        if git_date and re.match(r"^\d{4}-\d{2}-\d{2}", git_date):
            return git_date[:10]
    except Exception:
        pass

    # 3. Designed date mention in body
    des_match = re.search(r"designed\s+(\d{4}-\d{2}-\d{2})", content, re.IGNORECASE)
    if des_match:
        return des_match.group(1)

    # 4. File creation timestamp
    try:
        return datetime.date.fromtimestamp(file_path.stat().st_ctime).isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def insert_created_date_frontmatter(
    file_path: Path, date_str: str, dry_run: bool = False
) -> bool:
    content = file_path.read_text(encoding="utf-8", errors="replace")

    fm_match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", content, re.DOTALL)
    if not fm_match:
        return False

    fm_text, body = fm_match.groups()

    # Check if already has created or created_date
    for line in fm_text.splitlines():
        line_clean = line.strip()
        if line_clean.startswith("created:") or line_clean.startswith("created_date:"):
            return False  # Already present

    nl = "\r\n" if "\r\n" in content else "\n"
    lines = fm_text.splitlines()
    new_fm_lines = []
    inserted = False

    # Prefer inserting right after sprint: "..."
    for line in lines:
        new_fm_lines.append(line)
        if line.strip().startswith("sprint:") and not inserted:
            new_fm_lines.append(f'created: "{date_str}"')
            inserted = True

    if not inserted:
        # If no sprint key, insert at the beginning of frontmatter
        new_fm_lines.insert(0, f'created: "{date_str}"')

    new_content = f"---{nl}{nl.join(new_fm_lines)}{nl}---{nl}{body}"

    if not dry_run:
        file_path.write_text(new_content, encoding="utf-8")

    return True


def run_backfill(
    root_dir: Path, dry_run: bool = False, verbose: bool = False, cards_dir: Path | None = None
) -> dict[str, Any]:
    cards_dir = cards_dir or root_dir / "docs" / "cards"
    card_dirs = [cards_dir, cards_dir / "backlog"]

    all_files: list[Path] = []
    for cd in card_dirs:
        if cd.exists():
            all_files.extend(sorted(cd.glob("*.md")))

    updated_files: list[dict[str, str]] = []
    skipped_files: list[str] = []

    for f in all_files:
        content = f.read_text(encoding="utf-8", errors="replace")
        has_date = bool(re.search(r"^created(?:_date)?:\s*", content, re.MULTILINE))
        if has_date:
            skipped_files.append(f.name)
            continue

        date_val = get_card_creation_date(f, cards_dir)
        ok = insert_created_date_frontmatter(f, date_val, dry_run=dry_run)
        if ok:
            updated_files.append({"file": f.name, "date": date_val})
            if verbose:
                prefix = "[DRY-RUN] Would add" if dry_run else "Added"
                print(f"{prefix} created: '{date_val}' -> {f.name}")
        else:
            skipped_files.append(f.name)

    return {
        "total_scanned": len(all_files),
        "updated_count": len(updated_files),
        "skipped_count": len(skipped_files),
        "updated_files": updated_files,
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Backfill created date into Obsidian card frontmatter."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying any files",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Print each updated file"
    )
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent.parent
    res = run_backfill(root_dir, dry_run=args.dry_run, verbose=args.verbose)

    action = "Would update" if res["dry_run"] else "Updated"
    print(f"\nScan complete: {res['total_scanned']} total cards scanned.")
    print(f"{action}: {res['updated_count']} files.")
    print(f"Already had date / skipped: {res['skipped_count']} files.")


if __name__ == "__main__":
    main()
