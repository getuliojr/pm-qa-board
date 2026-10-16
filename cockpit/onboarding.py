"""
onboarding.py - `cli.py init`: bring a project under PM & QA Cockpit (design R2-Q3).

Report-first: without ``--apply`` nothing is written. With ``--apply`` it
  * registers the project in ``~/.pm-qa-cockpit/projects.json`` (and makes it active),
  * creates the docs layout (external docs are ``git init``-ed in their own folder),
  * writes ``ORCHESTRATION.md`` (the 7-step story loop) into the docs root if missing,
  * in stealth mode appends the in-repo PM paths to ``.git/info/exclude`` so nothing is committed.
It never edits an existing CLAUDE.md / AGENTS.md / GEMINI.md / settings.json — it prints an
alignment checklist instead, so a project's own rules are never clobbered.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import projects

ORCHESTRATION_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
LOOP_SIGNALS = {
    "design before code (grill-me)": ("grill",),
    "cold review gate": ("cold review", "review gate", "cold reviewer"),
    "commit locally, never push": ("never push", "do not push", "don't push"),
    "story/sprint cards": ("docs/cards", "cards/", "story card", "mn-"),
    "worklog per session": ("worklog",),
}
STEALTH_PATTERNS = ("docs/cards/", "docs/reviews/", "docs/artifacts/", "PLAN.md", "gate-heavy-results/")

ORCHESTRATION_TEMPLATE = """# {name} — Story loop (PM & QA Cockpit)

Cards: `{cards}` with prefix `{prefix}-<story>`. Dashboard: `pm-qa-cockpit` on http://localhost:3333.

1. **Design before code** — run `grill-me`; persist each settled round under
   `### Grill in progress — Story <id>` in `PLAN.md`; close with a `## Story <id>` section
   (decisions + WHY, rejected alternatives, non-goals, unchecked DoD) and a card in `story-backlog`.
2. **Start** — card to `in-progress` with a Changelog row `Build started (start SHA <sha>)`
   before the first line of code.
3. **Build** — commit locally after each coherent chunk; tick card + `PLAN.md` DoD as it lands
   (`pm-sync`).
4. **Gates** — mode `{gate_mode}`: {gate_note}
5. **Commit everything** before review.
6. **Cold review** — a separate reviewer reads the diff from the start SHA (`pm-review`); up to 3
   rounds, fewer when nothing is worth another. Persist to `reviews/<PREFIX>-<story>-round-<n>.md`.
7. **Close** — card to `qa`, write the interactive Story Close-Out artifact
   `artifacts/<PREFIX>-<story>-closeout.html`, hand the user the Human QA steps. Only the user's
   confirmation sets `completed`. **Never push** — pushing is always the user's call.
"""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def inspect_project(code: Path) -> dict[str, Any]:
    """What orchestration and hooks the project already has."""
    docs_found = {name: (code / name).exists() for name in ORCHESTRATION_FILES}
    corpus = "\n".join(_read(code / n).lower() for n, ok in docs_found.items() if ok)
    signals = {label: any(k in corpus for k in keys) for label, keys in LOOP_SIGNALS.items()}

    hooks: dict[str, int] = {}
    for settings in (code / ".claude" / "settings.json", code / ".claude" / "settings.local.json"):
        try:
            data = json.loads(_read(settings) or "{}")
        except json.JSONDecodeError:
            continue
        for event, entries in (data.get("hooks") or {}).items():
            hooks[event] = hooks.get(event, 0) + (len(entries) if isinstance(entries, list) else 1)
    return {
        "orchestration_files": docs_found,
        "loop_signals": signals,
        "hooks": hooks,
        "cursor": (code / ".cursor").exists(),
        "agents_dir": (code / ".agents").exists(),
        "is_git": (code / ".git").exists(),
    }


def skill_installed(name: str) -> list[str]:
    home = projects.user_home()
    roots = [home / ".claude" / "skills", home / ".agents" / "skills", home / ".cursor" / "skills-cursor"]
    return [str(r / name) for r in roots if (r / name / "SKILL.md").exists()]


def apply_stealth(code: Path, project: dict[str, Any]) -> list[str]:
    """Append in-repo PM paths to .git/info/exclude (idempotent). External docs need none."""
    exclude = code / ".git" / "info" / "exclude"
    if not (code / ".git").is_dir():
        return []
    docs_rel = None
    docs = Path(project["docs_path"])
    if docs.is_relative_to(code):
        docs_rel = docs.relative_to(code).as_posix()
    # docs_path == code_path (bare root layout) leaves docs_rel "." — fall back to the known paths.
    wanted = [f"{docs_rel}/"] if docs_rel and docs_rel != "." else list(STEALTH_PATTERNS)
    wanted.append(".pm-qa-cockpit/")
    existing = _read(exclude).splitlines()
    added = [w for w in wanted if w not in existing]
    if added:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as f:
            if existing and existing[-1].strip():
                f.write("\n")
            f.write("# PM & QA Cockpit (stealth mode)\n" + "\n".join(added) + "\n")
    return added


def cmd_init(args: argparse.Namespace) -> int:
    code = Path(args.code_path).expanduser().resolve()
    try:
        project = projects.build_project(
            args.name, code, slug=args.slug, prefix=args.prefix, docs_path=args.docs_path
        )
    except ValueError as err:
        print(f"Error: {err}")
        return 1

    report = inspect_project(code)
    print(f"Project:  {project['name']} [{project['prefix']}] slug={project['slug']}")
    print(f"Code:     {project['code_path']}")
    print(f"Docs:     {project['docs_path']} ({project['docs_mode']})")
    print(f"Stealth:  {'yes (.git/info/exclude)' if args.stealth else 'no'}")
    print("\nOrchestration files:")
    for name, ok in report["orchestration_files"].items():
        print(f"  [{'x' if ok else ' '}] {name}")
    print("\nLoop alignment checklist (found in those files):")
    for label, ok in report["loop_signals"].items():
        print(f"  [{'x' if ok else ' '}] {label}")
    print("\nHooks (.claude/settings*.json):")
    for event in HOOK_EVENTS:
        n = report["hooks"].get(event, 0)
        print(f"  [{'x' if n else ' '}] {event}" + (f" ({n})" if n else ""))
    print(f"\nCursor rules: {'yes' if report['cursor'] else 'no'} · .agents: {'yes' if report['agents_dir'] else 'no'}")
    grill = skill_installed("grill-me") or skill_installed("grilling")
    print(f"grill-me skill: {'installed' if grill else 'MISSING — ask your agent to run pm-qa-setup (it installs it)'}")

    missing = [label for label, ok in report["loop_signals"].items() if not ok]
    if missing:
        print("\nTo align, add a pointer to ORCHESTRATION.md in CLAUDE.md / AGENTS.md covering:")
        for label in missing:
            print(f"  - {label}")

    if not args.apply:
        print("\nReport only. Re-run with --apply to register the project and create its docs layout.")
        return 0

    created = projects.ensure_docs_root(project)
    orch = Path(project["docs_path"]) / "ORCHESTRATION.md"
    if not orch.exists():
        gates = project.get("gates") or {}
        mode = gates.get("mode", "unprofiled")
        note = {
            "fast": "run the full suite inline before close and record it in QUALITY.md.",
            "heavy": "agent runs fast gates only; the user runs the Heavy Gate out of band.",
        }.get(mode, "run `cli.py profile-gates --measure --apply` to choose Fast vs Heavy.")
        orch.write_text(
            ORCHESTRATION_TEMPLATE.format(
                name=project["name"],
                cards="cards/ (next to this file)",
                prefix=project["prefix"],
                gate_mode=mode,
                gate_note=note,
            ),
            encoding="utf-8",
        )
        created.append(str(orch))
    if args.stealth:
        project["stealth"] = True
        added = apply_stealth(code, project)
        if added:
            print(f"Stealth: excluded {', '.join(added)}")
    projects.upsert_project(project, activate=True)
    for c in created:
        print(f"Created: {c}")
    print(f"Registered '{project['slug']}' as the active project in {projects.registry_path()}")
    if os.name == "nt":
        print("Next: cli.py profile-gates --measure --apply  ·  powershell -File dashboard.ps1")
    return 0
