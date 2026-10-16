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
    uv run python cli.py setup-plan [CODE_PATH] [--json] / setup-apply CODE_PATH IDS... / skills-status
    uv run python cli.py doctor [SLUG_OR_PATH] [--json]
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


def _plan_file(code_path: str) -> Path:
    import hashlib

    key = hashlib.sha1(str(Path(code_path).resolve()).lower().encode()).hexdigest()[:12]
    return projects.cockpit_home() / "plans" / f"{key}.json"


def cmd_setup(args: argparse.Namespace) -> int:
    """setup-plan / setup-apply / skills-status — the commands the pm-qa-setup skill drives."""
    import json

    import setup_flow

    if args.command == "skills-status":
        status = setup_flow.skills_status(args.code_path)
        if args.json:
            print(json.dumps(status, indent=2))
            return 0
        print(f"Tool version: {status['tool_version']}")
        for group in ("global", "project"):
            for row in status[group]:
                print(f"  [{row['state']:<8}] {group:<7} {row['agent']:<11} {row['path']} "
                      f"({row['installed_version'] or '-'} / {row['available_version']})")
        return 0

    if args.command == "setup-plan":
        plan = setup_flow.build_plan(
            args.code_path, name=args.name, prefix=args.prefix, docs_path=args.docs_path, stealth=args.stealth,
            force_full=args.full,
        )
        pf = _plan_file(args.code_path)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        if args.json:
            print(json.dumps(plan, indent=2))
            return 0
        p = plan["project"]
        print(f"Project: {p['name']} [{p['prefix']}] {'(registered)' if plan['registered'] else '(not registered)'}")
        print(f"Code:    {plan['code_path']}")
        print(f"Docs:    {p['docs_path']} ({p['docs_mode']}{', stealth' if plan['stealth'] else ''})")
        print(f"Tools:   {', '.join(setup_flow.TOOL_LABELS[t] for t in plan['agents']) or 'none detected'}")
        print(f"Mode:    {plan['mode']}" + (f" (own loop: {', '.join(plan['own_loop_skills'])})" if plan["own_loop_skills"] else ""))
        mark = {"ok": "ok    ", "change": "change", "review": "review", "info": "info  "}
        print("\n== What you have, and where it belongs")
        for g in plan["inventory"]:
            print(f"\n  {g['group']}")
            for it in g["items"]:
                print(f"   [{mark[it['status']]}] {it['what']}")
                print(f"            now:     {it['now']}")
                print(f"            belongs: {it['belongs']}")
                print(f"            why:     {it['why']}")
        for note in plan.get("notes", []):
            print(f"  Note: {note}")
        print("\n== Recommended target")
        for line in plan["target"]:
            print(f"  - {line}")
        if not plan["decisions"]:
            print("\n== Nothing to decide: this project is set up and current.")
        else:
            print("\n== Decisions (ask these, recommendation first)")
            for n, d in enumerate(plan["decisions"], 1):
                print(f"\n  {n}. {d['question']}")
                print(f"     Recommended: {d['recommendation']}")
                for o in d["options"]:
                    print(f"       - {o['label']}: {o['description']}")
            print("\n== For the agent only (never show these ids as the thing to decide)")
            for n, d in enumerate(plan["decisions"], 1):
                print(f"  {n}. " + " | ".join(f"{o['label']} -> {' '.join(o['approve']) or '(nothing)'}" for o in d["options"]))
            for a in plan["actions"]:
                print(f"  {a['id']} {a['kind']}: {a['title']}")
                for w in a.get("writes", []):
                    print(f"       writes: {w}")
                if a.get("detail"):
                    print(f"       {a['detail']}")
                if a.get("preview"):
                    print("       adds:\n" + "\n".join("         | " + line for line in a["preview"].splitlines()))
        print("\nNothing has been changed. Apply the approved options with: cli.py setup-apply <code_path> <ids>")
        return 0

    # setup-apply: re-plan and refuse if the approved actions no longer match what the user saw
    pf = _plan_file(args.code_path)
    if not pf.exists():
        print("Error: no saved plan for this project — run `cli.py setup-plan` first and show it to the user.")
        return 1
    seen = json.loads(pf.read_text(encoding="utf-8"))
    fresh = setup_flow.build_plan(
        args.code_path,
        name=seen["project"]["name"],
        prefix=seen["project"]["prefix"],
        docs_path=seen["project"]["docs_path"] if seen["project"]["docs_mode"] == "external" else None,
        stealth=seen["stealth"],
        force_full=seen["mode"] == "full" and bool(seen["own_loop_skills"]),
    )
    ids, conflicts = [], {}
    for token in args.ids:
        aid, _, how = token.partition("=")
        if how and how not in ("abort", "replace", "new"):
            print(f"Error: {token}: the choice must be abort, replace or new.")
            return 1
        ids.append(aid)
        if how:
            conflicts[aid] = how

    def canon(a: dict | None) -> str:
        return json.dumps(a, sort_keys=True)

    # Every detail the user saw (title, writes, paths, preview text, state) must be unchanged.
    seen_by_id = {a["id"]: canon(a) for a in seen["actions"]}
    fresh_by_id = {a["id"]: canon(a) for a in fresh["actions"]}
    changed = [i for i in ids if seen_by_id.get(i) != fresh_by_id.get(i)]
    if changed:
        print(f"Error: the plan changed since it was shown ({', '.join(changed)}). Re-run setup-plan and ask again.")
        return 1
    try:
        results = setup_flow.apply_actions(fresh, ids, on_conflict=conflicts)
    except ValueError as err:
        print(f"Error: {err}")
        return 1
    for r in results:
        line = f"  {r['id']}: {r['result']}"
        if r.get("command"):
            line += f"  -> run: {r['command']}"
        print(line)
    pf.unlink(missing_ok=True)
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

    p_plan = sub.add_parser("setup-plan", help="Read-only: what pm-qa-setup would do in a project (numbered actions)")
    p_plan.add_argument("code_path", nargs="?", default=".", help="Project code repo (default: current dir)")
    p_plan.add_argument("--name")
    p_plan.add_argument("--prefix")
    p_plan.add_argument("--docs-path", help="External docs folder for a new project")
    p_plan.add_argument("--stealth", action="store_true", default=None)
    p_plan.add_argument("--full", action="store_true",
                        help="Install the cockpit skills even if the project has its own loop (default: register only)")
    p_plan.add_argument("--json", action="store_true", help="Machine-readable output")

    p_apply = sub.add_parser("setup-apply", help="Apply ONLY the approved action ids from the last setup-plan")
    p_apply.add_argument("code_path")
    p_apply.add_argument(
        "ids", nargs="+",
        help="Approved action ids, e.g. A2 A3. For an edited skill add its choice: A4=replace or A5=new "
             "(plain id = keep the user's copy)",
    )

    p_status = sub.add_parser("skills-status", help="Read-only: installed vs available skill versions")
    p_status.add_argument("code_path", nargs="?", help="Also report a project's installed skills")
    p_status.add_argument("--json", action="store_true")

    p_doc = sub.add_parser("doctor", help="Read-only: is this project's cockpit setup correct? (pass / warn / fail)")
    p_doc.add_argument("target", nargs="?", default=".", help="Registered slug or a path in the project (default: here)")
    p_doc.add_argument("--json", action="store_true")

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
    if args.command in ("setup-plan", "setup-apply", "skills-status"):
        return cmd_setup(args)
    if args.command == "doctor":
        from doctor import cmd_doctor

        return cmd_doctor(args)
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
