#!/usr/bin/env python3
"""
cli.py - PM & QA Cockpit command line (multi-project story cards).

Lets agents and developers query, update, check off DoD items and transition story cards
from the terminal without the web server. The project is resolved, in order, from
``--project <slug>``, the registered project containing the current directory, the
enclosing git repo as an ad-hoc in-repo project, then the registry's active project.

Usage:
    uv run python cli.py [--project SLUG] list [--status STATUS] [--story STORY]
    uv run python cli.py show <CARD_ID>
    uv run python cli.py status <CARD_ID> <STATUS> [--agent AGENT]
    uv run python cli.py check <CARD_ID> <INDEX> [--agent AGENT]
    uv run python cli.py uncheck <CARD_ID> <INDEX>
    uv run python cli.py check-qa / uncheck-qa <CARD_ID> <INDEX>
    uv run python cli.py validate <CARD_ID> [--notes NOTES]
    uv run python cli.py projects [--select SLUG]
    uv run python cli.py init <CODE_PATH> --name NAME --prefix PFX [--docs-path DIR] [--stealth] [--apply]
    uv run python cli.py profile-gates [--measure] [--apply] [--threshold-seconds 360]
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

# Add script directory to sys.path to import PMParser
CURRENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CURRENT_DIR))
import projects  # noqa: E402
from parser import PMParser  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CHANGELOG_HEADER_RE = r"(##\s+Changelog\s*\r?\n.*?\r?\n\|[-|\s]+\r?\n)"


def now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def resolve_project(slug: str | None) -> dict:
    if slug:
        data = projects.load_registry()
        if slug not in data["projects"]:
            raise SystemExit(f"Error: unknown project '{slug}'. Registered: {', '.join(data['projects']) or 'none'}")
        return data["projects"][slug]
    cwd = Path.cwd()
    found = projects.find_project_for_path(cwd)
    if found:
        return found
    root = cwd
    while root.parent != root and not (root / ".git").exists():
        root = root.parent
    if (root / ".git").exists():
        return projects.adhoc_project(root)
    return projects.get_active_project() or projects.adhoc_project(cwd)


def get_card_file(parser: PMParser, mn_id: str) -> tuple[Path, dict] | tuple[None, None]:
    cards = parser.get_all_cards()
    card = next((c for c in cards if c["mn_id"] == mn_id), None)
    if not card:
        return None, None
    return parser.resolve_path(card["file_path"]), card


def append_changelog(file_path: Path, agent: str, what: str) -> None:
    content = file_path.read_text(encoding="utf-8")
    if "## Changelog" not in content:
        return
    nl = "\r\n" if "\r\n" in content else "\n"
    entry = f"| {now_stamp()} | {agent} | {what} |"
    content = re.sub(CHANGELOG_HEADER_RE, lambda m: f"{m.group(1)}{entry}{nl}", content, count=1)
    file_path.write_text(content, encoding="utf-8")


def cmd_list(parser: PMParser, args: argparse.Namespace) -> int:
    cards = parser.get_all_cards()
    story = args.story or args.sprint
    if args.status:
        wanted = args.status.lower().strip()
        wanted = "sprint-backlog" if wanted == "story-backlog" else wanted
        cards = [c for c in cards if c["status"] == wanted]
    if story:
        cards = [c for c in cards if str(c.get("sprint", "")).lower() == story.lower().strip()]

    if not cards:
        print("No matching cards found.")
        return 0

    print(f"{'ID':<15} {'Status':<16} {'Story':<8} {'DoD':<8} {'Title'}")
    print("-" * 75)
    for c in sorted(cards, key=lambda x: (x.get("sprint", ""), x.get("mn_id", ""))):
        dod_str = f"{c['dod_checked']}/{c['dod_total']}"
        print(f"{c['mn_id']:<15} {c['status']:<16} {c['sprint']:<8} {dod_str:<8} {c['title'][:35]}")
    print(f"\nTotal: {len(cards)} card(s)")
    return 0


def cmd_show(parser: PMParser, args: argparse.Namespace) -> int:
    file_path, card = get_card_file(parser, args.card_id)
    if not card or not file_path:
        print(f"Error: Card '{args.card_id}' not found.", file=sys.stderr)
        return 1

    print(f"Card:    {card['mn_id']} - {card['title']}")
    print(f"File:    {card['file_path']}")
    print(f"Story:   {card['sprint']}")
    print(f"Type:    {card['type']}")
    print(f"Status:  {card['status']}")
    print(f"Date:    {card.get('created_date', 'N/A')}")
    print(f"Valid:   {card.get('validated', 'N/A')}")
    if card.get("closeout"):
        print(f"Close-Out: {card['closeout']['file_path']}")
    print("\nDefinition of Done:")
    for idx, item in enumerate(card.get("dod", [])):
        mark = "[x]" if item["checked"] else "[ ]"
        print(f"  {idx}: {mark} {item['text']}")

    if card.get("qa_instructions") or card.get("qa_items"):
        print("\nHuman QA Test:")
        if card.get("qa_instructions"):
            indented = card["qa_instructions"].replace("\n", "\n    ")
            print(f"  Instructions:\n    {indented}")
        if card.get("qa_items"):
            print("  Verification Checks:")
            for idx, item in enumerate(card["qa_items"]):
                mark = "[x]" if item["checked"] else "[ ]"
                print(f"    {idx}: {mark} {item['text']}")

    if card.get("changelog"):
        print("\nChangelog:")
        for cl in card["changelog"]:
            print(f"  {cl['date']} | {cl['agent']} | {cl['what']}")

    return 0


def cmd_status(parser: PMParser, args: argparse.Namespace) -> int:
    file_path, card = get_card_file(parser, args.card_id)
    if not card or not file_path:
        print(f"Error: Card '{args.card_id}' not found.", file=sys.stderr)
        return 1

    new_status = args.new_status.lower().strip()
    valid_statuses = ("backlog", "story-backlog", "sprint-backlog", "in-progress", "qa", "completed")
    if new_status not in valid_statuses:
        print(f"Error: Invalid status '{new_status}'. Allowed: {', '.join(valid_statuses)}", file=sys.stderr)
        return 1
    # Each card stays in its own schema: legacy cards keep `sprint-backlog`, story cards `story-backlog`.
    if new_status in ("story-backlog", "sprint-backlog"):
        new_status = "story-backlog" if card.get("schema") == "story" else "sprint-backlog"

    content = file_path.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in content else "\n"
    if re.search(r"^status:", content, flags=re.MULTILINE):
        content = re.sub(r"^status:\s*.*$", f"status: {new_status}", content, count=1, flags=re.MULTILINE)
    else:
        content = re.sub(r"^(---\r?\n)", lambda m: f"{m.group(1)}status: {new_status}{nl}", content, count=1)
    file_path.write_text(content, encoding="utf-8")
    append_changelog(file_path, args.agent or "Agent", f"Status moved to {new_status}")
    print(f"Updated {args.card_id} status to '{new_status}'.")
    return 0


def cmd_check(parser: PMParser, args: argparse.Namespace) -> int:
    ok, msg = parser.toggle_dod_item(args.card_id, args.index, checked=True)
    if not ok:
        print(f"Error: {msg}", file=sys.stderr)
        return 1
    file_path, _ = get_card_file(parser, args.card_id)
    if file_path and file_path.exists():
        append_changelog(file_path, args.agent or "Agent", f"Completed DoD item #{args.index}")
    print(f"Checked item #{args.index} on {args.card_id}.")
    return 0


def cmd_uncheck(parser: PMParser, args: argparse.Namespace) -> int:
    ok, msg = parser.toggle_dod_item(args.card_id, args.index, checked=False)
    if not ok:
        print(f"Error: {msg}", file=sys.stderr)
        return 1
    print(f"Unchecked item #{args.index} on {args.card_id}.")
    return 0


def cmd_validate(parser: PMParser, args: argparse.Namespace) -> int:
    ok, msg = parser.validate_card(args.card_id, notes=args.notes or "Validated via CLI", do_commit=False)
    if not ok:
        print(f"Error: {msg}", file=sys.stderr)
        return 1
    print(msg)
    return 0


def cmd_check_qa(parser: PMParser, args: argparse.Namespace) -> int:
    ok, msg = parser.toggle_qa_item(args.card_id, args.index, checked=True)
    if not ok:
        print(f"Error: {msg}", file=sys.stderr)
        return 1
    print(f"Checked QA item #{args.index} on {args.card_id}.")
    return 0


def cmd_uncheck_qa(parser: PMParser, args: argparse.Namespace) -> int:
    ok, msg = parser.toggle_qa_item(args.card_id, args.index, checked=False)
    if not ok:
        print(f"Error: {msg}", file=sys.stderr)
        return 1
    print(f"Unchecked QA item #{args.index} on {args.card_id}.")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    if args.select:
        try:
            projects.select_project(args.select)
        except KeyError:
            print(f"Error: unknown project '{args.select}'", file=sys.stderr)
            return 1
    view = projects.public_view()
    print(f"Registry: {view['registry_path']}")
    if not view["projects"]:
        print("  (no projects registered — run `cli.py init`)")
    for p in view["projects"]:
        mark = "*" if p["slug"] == view["active_project"] else " "
        print(f" {mark} {p['slug']:<16} {p['prefix']:<5} {p['docs_mode']:<8} {p['code_path']}")
        if p["docs_mode"] == "external":
            print(f"   {'':<16} docs: {p['docs_path']}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PM & QA Cockpit — story card CLI")
    parser.add_argument("--project", help="Registered project slug (default: resolved from the current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List cards")
    p_list.add_argument("--status", help="Filter by status")
    p_list.add_argument("--story", help="Filter by story id")
    p_list.add_argument("--sprint", help="Legacy alias of --story")

    p_show = sub.add_parser("show", help="Show card details, DoD, and Human QA items")
    p_show.add_argument("card_id", help="Card ID (e.g. MN-15.8.3)")

    p_status = sub.add_parser("status", help="Update card status")
    p_status.add_argument("card_id", help="Card ID")
    p_status.add_argument(
        "new_status", help="New status (backlog|story-backlog|in-progress|qa|completed; sprint-backlog accepted)"
    )
    p_status.add_argument("--agent", help="Agent identity for changelog, e.g. 'Lead (Sonnet 5.5)'", default="Agent")

    p_check = sub.add_parser("check", help="Check off a DoD item by index")
    p_check.add_argument("card_id", help="Card ID")
    p_check.add_argument("index", type=int, help="DoD item index (0-based)")
    p_check.add_argument("--agent", help="Agent identity for changelog", default="Agent")

    p_uncheck = sub.add_parser("uncheck", help="Uncheck a DoD item by index")
    p_uncheck.add_argument("card_id", help="Card ID")
    p_uncheck.add_argument("index", type=int, help="DoD item index (0-based)")

    p_check_qa = sub.add_parser("check-qa", help="Check off a Human QA verification item by index")
    p_check_qa.add_argument("card_id", help="Card ID")
    p_check_qa.add_argument("index", type=int, help="QA item index (0-based)")

    p_uncheck_qa = sub.add_parser("uncheck-qa", help="Uncheck a Human QA verification item by index")
    p_uncheck_qa.add_argument("card_id", help="Card ID")
    p_uncheck_qa.add_argument("index", type=int, help="QA item index (0-based)")

    p_validate = sub.add_parser("validate", help="Mark card as completed/validated")
    p_validate.add_argument("card_id", help="Card ID")
    p_validate.add_argument("--notes", help="Optional validation notes", default="")

    p_projects = sub.add_parser("projects", help="List registered projects (or switch the active one)")
    p_projects.add_argument("--select", help="Make this project slug active")

    p_init = sub.add_parser("init", help="Onboard a project: register it, inspect orchestration + hooks")
    p_init.add_argument("code_path", help="Project code repo path")
    p_init.add_argument("--name", required=True)
    p_init.add_argument("--prefix", required=True, help="Card prefix, e.g. APP")
    p_init.add_argument("--slug")
    p_init.add_argument("--docs-path", help="External docs folder (default: <code_path>/docs, in-repo)")
    p_init.add_argument("--stealth", action="store_true", help="Keep cockpit files out of git via .git/info/exclude")
    p_init.add_argument("--apply", action="store_true", help="Write changes (default: report only)")

    p_prof = sub.add_parser("profile-gates", help="Detect the stack, time the suite, pick Fast vs Heavy gate mode")
    p_prof.add_argument("--fast-cmd", help="Override the detected fast/static gate command")
    p_prof.add_argument("--full-cmd", help="Override the detected full test command")
    p_prof.add_argument("--threshold-seconds", type=int, default=360)
    p_prof.add_argument("--measure", action="store_true", help="Actually run the full command and time it")
    p_prof.add_argument("--apply", action="store_true", help="Save the result into the project's registry entry")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.command == "projects":
        return cmd_projects(args)
    if args.command == "init":
        from onboarding import cmd_init

        return cmd_init(args)
    if args.command == "profile-gates":
        from gate_profiler import cmd_profile_gates

        return cmd_profile_gates(resolve_project(args.project), args)

    commands = {
        "list": cmd_list,
        "show": cmd_show,
        "status": cmd_status,
        "check": cmd_check,
        "uncheck": cmd_uncheck,
        "check-qa": cmd_check_qa,
        "uncheck-qa": cmd_uncheck_qa,
        "validate": cmd_validate,
    }
    return commands[args.command](PMParser(project=resolve_project(args.project)), args)


if __name__ == "__main__":
    sys.exit(main())
