"""
roster.py - Who does each step (Story 2.2): the agent/model roster per tool.

Six roles: ``lead`` (a session pick, recorded in the project's ORCHESTRATION.md Roster table) and five
delegated roles whose agent *files* are the truth: builder, advisor, reviewer, retrieval, pm-sync.
This module is pure (no writes): it reads existing agent files, maps them to roles, ships the
recommended defaults and the known-model lists, renders the thin ``board-<role>.md`` files and the
Roster table, and checks pins. ``setup_flow`` does the writing; ``doctor`` check 10 does the warning.

Verified 2026-10-16 (docs): Claude Code agents read ``effort`` (low|medium|high|xhigh|max) and
``tools``; Cursor reads ``.cursor/agents`` (and also ``.claude/agents``, which is not scanned here: a Claude file
carries Claude model IDs, and ``.cursor/`` wins on a name clash) with ``model`` / ``readonly``;
Antigravity documents ``.agents/agents`` (money-nest's ``.agents/subagents`` is read too, never
written) with ``model`` ``inherit|flash|pro``.

Standard library only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROLES = ("lead", "builder", "advisor", "reviewer", "retrieval", "pm-sync")
FILE_ROLES = ROLES[1:]
ROSTER_START = "<!-- pm-qa-board:roster:start -->"
ROSTER_END = "<!-- pm-qa-board:roster:end -->"
PREFIX = "board-"
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# A role is matched by file name first, then by description (heuristic, Q9).
_L, _R = r"(?<![a-z0-9])", r"(?![a-z0-9])"  # whole words, where "_" and "-" both separate
MATCH = {"builder": re.compile(_L + r"builders?" + _R), "advisor": re.compile(_L + r"advisors?" + _R),
         "reviewer": re.compile(_L + r"review(er)?s?" + _R), "retrieval": re.compile(_L + r"retriev(e|es|al|er|ers)" + _R),
         "pm-sync": re.compile(_L + r"pm[-_ ]?sync" + _R)}
READ_DIRS = {
    "claude": [".claude/agents"],
    "cursor": [".cursor/agents"],
    "antigravity": [".agents/agents", ".agents/subagents"],
    "codex": [".agents/agents", ".agents/subagents"],
}
WRITE_DIR = {"claude": ".claude/agents", "cursor": ".cursor/agents", "antigravity": ".agents/agents", "codex": ".agents/agents"}

# (model, effort) per role per tool. Cursor / Antigravity are seeded from money-nest's proven pins.
# Cursor slugs keep ``[fast=false]``: a plain slug may route to Fast.
DEFAULT_EFFORT = {"lead": "high", "advisor": "medium", "reviewer": "high", "builder": "medium",
                  "retrieval": "low", "pm-sync": "low"}
DEFAULT_MODELS = {
    "claude": {"lead": "claude-opus-5-5", "advisor": "claude-opus-5-5", "reviewer": "claude-opus-5-5",
               "builder": "claude-sonnet-5-5", "retrieval": "claude-haiku-5-5", "pm-sync": "claude-haiku-5-5"},
    "cursor": {"lead": "composer-2.5[fast=false]", "advisor": "grok-4.6[fast=false]", "reviewer": "grok-4.7[fast=false]",
               "builder": "grok-4.6[fast=false]", "retrieval": "composer-2.5[fast=false]", "pm-sync": "composer-2.5[fast=false]"},
    "antigravity": {"lead": "pro", "advisor": "pro", "reviewer": "pro", "builder": "pro", "retrieval": "flash", "pm-sync": "flash"},
    "codex": {"lead": "gpt-5.6-terra", "advisor": "gpt-6.1-sol", "reviewer": "gpt-6.1-sol",
              "builder": "gpt-5.6-terra", "retrieval": "gpt-6-luna", "pm-sync": "gpt-6-luna"},
}
# Pins this board version knows. A pin outside the list only warns (lists go stale).
KNOWN_MODELS = {
    "claude": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-sonnet-5"],
    "cursor": ["composer-2.5", "grok-4.6", "grok-4.7"],
    "antigravity": ["pro", "flash"],
    "codex": ["gpt-5.6-terra", "gpt-6.1-sol", "gpt-6-luna"],
}
ALIASES = {"claude": ["opus", "sonnet", "haiku", "fable"], "cursor": ["auto"], "antigravity": [], "codex": []}
# Claude Code is the only tool whose agent frontmatter is verified to read `effort`.
EFFORT_IN_FRONTMATTER = {"claude"}
CLAUDE_TOOLS = {"reviewer": "Read, Grep, Glob, Bash, Write", "retrieval": "Read, Grep, Glob, Bash, WebFetch, WebSearch"}

ROLE_TEXT = {
    "builder": ("Writes code against a brief from the lead.",
                "Touch only the files the brief names; stop and report on any contradiction. Read the `pm-orchestrate` skill, section *Hard rules*."),
    "advisor": ("Consulted for one hard call; returns a recommendation, never edits code.",
                "At most 3 calls per problem, then the lead escalates to the user. Read the `pm-orchestrate` skill, section *The loop*."),
    "reviewer": ("The cold review gate: reviews a story's diff with no inherited context.",
                 "Separate and cold: never inherit the lead's model or context. Diff-only from the start SHA. Follow the `pm-review` skill; write only the review file."),
    "retrieval": ("Fact-finding only: lookups, searches, running a gate and reporting pass/fail.",
                  "Hand back a fact with `file:line`, never a transcript, never a diagnosis. Read the `pm-orchestrate` skill, section *Hard rules*."),
    "pm-sync": ("Mechanical bookkeeping: ticks the card's and PLAN.md's DoD boxes and writes Changelog rows.",
                "Follow the `pm-sync` skill exactly; change nothing but checkboxes and Changelog rows. Never `git push`."),
}


def frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^﻿?---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    out: dict[str, str] = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and re.match(r"^[A-Za-z_][\w-]*$", k.strip()) and not line.startswith((" ", "\t")):
            out[k.strip()] = v.strip().strip("\"'")
    return out


def base_model(tool: str, model: str) -> str:
    """The model without Cursor's bracketed parameters (``grok-4.6[fast=false]`` -> ``grok-4.6``)."""
    return model.split("[", 1)[0].strip()


# A description is prose ("never reviews it"), so it only counts when it names the agent noun itself.
DESC_MATCH = {"builder": MATCH["builder"], "advisor": MATCH["advisor"], "reviewer": re.compile(_L + r"reviewers?" + _R),
              "retrieval": re.compile(_L + r"retriev(al|er|ers)" + _R), "pm-sync": MATCH["pm-sync"]}


def roles_for(name: str, description: str) -> tuple[list[str], bool]:
    """(roles a file could fill, decided by the name). Whole words only. Name first; the description only if
    the name says nothing, and a description that names several roles is prose, not an identity."""
    for text, by_name, table in ((name.lower(), True, MATCH), (description.lower(), False, DESC_MATCH)):
        hits = [r for r, rx in table.items() if rx.search(text)]
        if hits:
            return hits, by_name
    return [], False


def match_role(name: str, description: str) -> str | None:
    """The one role a file fills, or None when it fits none or several."""
    hits, _ = roles_for(name, description)
    return hits[0] if len(hits) == 1 else None


def detect_tool(code: Path, tool: str) -> dict[str, Any]:
    """Existing agent files of one tool mapped to roles. ``roles[role]`` is None when missing."""
    roles: dict[str, dict[str, Any] | None] = {r: None for r in FILE_ROLES}
    extra: list[str] = []
    possible: dict[str, list[str]] = {}  # files that could fill several roles: reported, never duplicated
    for rel in READ_DIRS[tool]:
        d = code / rel
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                fm = frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            name = fm.get("name") or f.stem
            hits, by_name = roles_for(f.stem if not fm.get("name") else f"{f.stem} {name}", fm.get("description", ""))
            if len(hits) > 1 and not by_name:
                hits = []
            if len(hits) > 1:
                for h in hits:
                    possible.setdefault(h, []).append(f"{rel}/{f.name}")
                continue
            role = hits[0] if hits else None
            if role is None or roles[role] is not None:
                extra.append(f"{rel}/{f.name}")
                continue
            roles[role] = {"file": f"{rel}/{f.name}", "name": name, "model": fm.get("model", ""),
                           "effort": fm.get("effort", ""), "ours": f.stem.startswith(PREFIX)}
    return {"roles": roles, "extra": extra, "possible": possible,
            "missing": [r for r, v in roles.items() if v is None and r not in possible]}


def parse_lead(text: str, labels: dict[str, str]) -> dict[str, dict[str, str]]:
    """The lead row per tool from an ORCHESTRATION.md Roster section."""
    a, b = text.find(ROSTER_START), text.find(ROSTER_END)
    if a < 0 or b < a:
        return {}
    out: dict[str, dict[str, str]] = {}
    tool = None
    for line in text[a:b].splitlines():
        if line.startswith("### "):
            tool = next((t for t, lab in labels.items() if lab == line[4:].strip()), None)
        elif tool and line.startswith("|"):
            cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
            if len(cells) >= 4 and cells[0] == "lead":
                out[tool] = {"model": cells[2], "effort": cells[3]}
    return out


def recommended(tool: str, role: str) -> dict[str, str]:
    return {"model": DEFAULT_MODELS[tool][role], "effort": DEFAULT_EFFORT[role]}


def valid_value(kind: str, value: str) -> bool:
    if kind == "effort":
        return value in EFFORTS
    return bool(re.fullmatch(r"[A-Za-z0-9][\w.\-]*(\[[\w=,.\-]+\])?", value))


def forbidden_pin(tool: str, role: str, model: str) -> str | None:
    """Pins setup refuses to write (an unknown model only warns later, but these defeat the design)."""
    if base_model(tool, model).lower() in ALIASES[tool]:
        return "an alias may resolve to an older model: use the full ID"
    if model == "inherit":
        return "inherit is not allowed: pin a model"
    return None


def render_agent(tool: str, role: str, model: str, effort: str) -> str:
    """A thin agent file: frontmatter + the role's one-line job and its hard rule (Q7)."""
    job, rule = ROLE_TEXT[role]
    name = PREFIX + role
    lines = ["---", f"name: {name}", f"description: {job} Dispatched by the PM & QA Board loop."]
    if tool == "claude":
        if role in CLAUDE_TOOLS:
            lines.append(f"tools: {CLAUDE_TOOLS[role]}")
        lines.append(f"model: {model}")
        lines.append(f"effort: {effort}")
    else:
        lines.append(f"model: {model}")
        if tool == "antigravity":
            lines.append("mainAgent: false")
    lines += ["---", "", f"You are the board's **{role}** agent. {job}", "", rule]
    if tool not in EFFORT_IN_FRONTMATTER:
        lines += ["", f"Effort: **{effort}**. This tool reads no effort field from an agent file, so the dispatcher passes it when it starts you."]
    lines.append("")
    return "\n".join(lines)


def render_section(tool_rows: dict[str, dict[str, Any]], labels: dict[str, str]) -> str:
    """The Roster section for ORCHESTRATION.md. ``tool_rows[tool]`` = {role: {agent, model, effort}}."""
    out = [ROSTER_START, "## Roster", "",
           "Who does each step. The agent files are the truth for the delegated roles; the lead is a session pick.",
           "The `pm-orchestrate`, `pm-review` and `pm-sync` skills dispatch the agent named here for each role,",
           "and fall back to their generic rule when a role has none.", ""]
    for tool, rows in tool_rows.items():
        out += [f"### {labels[tool]}", "", "| Role | Agent | Model | Effort |", "|---|---|---|---|"]
        for role in ROLES:
            r = rows.get(role)
            if r:
                out.append(f"| {role} | {r['agent']} | `{r['model']}` | {r['effort'] or '-'} |")
            else:
                out.append(f"| {role} | (none) | - | - |")
        out.append("")
    out.append(ROSTER_END)
    return "\n".join(out) + "\n"


def check_pin(tool: str, role: str, model: str) -> str | None:
    """A warn-level finding for one pin, or None (Q6): empty, alias, inherit on advisor/reviewer, unknown."""
    if not model:
        return "no model line (it follows the session)"
    if model == "inherit":
        return "inherit is not allowed for this role (it must be a separate model)" if role in ("advisor", "reviewer") else None
    if base_model(tool, model).lower() in ALIASES[tool]:
        return f"'{model}' is an alias that may resolve to an older model: pin the full ID"
    if base_model(tool, model) not in KNOWN_MODELS[tool]:
        return f"'{model}' is not in the list this board version knows"
    return None
