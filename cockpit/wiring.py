"""
wiring.py - Which cockpit a project's launchers and hooks actually call (Story 2.1, R2-Q6 / R3-Q11).

Read-only scanning. Every cockpit path reference in a launcher is classified as

    running  - the released running copy ``~/.pm-qa-cockpit/app`` (what projects should call)
    dev      - a pm-qa-cockpit checkout (the dev clone: unreleased, half-finished edits break it)
    frozen   - a frozen in-repo copy (``tools/pm-dashboard``, from before the cockpit was extracted)

and a launcher's verdict comes from its **first** reference in code order: running copy first is ok
even when it falls back to the dev clone or a frozen copy (money-nest ``scripts/pm.ps1``). A stale
launcher whose dev-clone reference is a full path literal gets an exact one-line edit to propose;
anything else is reported with a fix hint, never rewritten.

Standard library only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import projects

SCRIPT_EXTS = (".ps1", ".sh", ".cmd", ".bat")
ORCHESTRATION_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
OVERRIDE_VARS = ("PM_QA_COCKPIT_REPO", "PM_QA_COCKPIT_HOME", "PM_QA_USER_HOME")
_TOKEN = re.compile(r"[^\s\"'`(){}\[\],;=|<>]+")
_QUOTED_NAME = re.compile(r"([\"'])pm-qa-cockpit\1")
# What the running copy is called in each launcher language (inside double quotes or unquoted).
RUNNING_EXPR = {".ps1": "$HOME{sep}.pm-qa-cockpit{sep}app", ".sh": "$HOME/.pm-qa-cockpit/app",
                ".cmd": "%USERPROFILE%\\.pm-qa-cockpit\\app", ".bat": "%USERPROFILE%\\.pm-qa-cockpit\\app"}


def running_copy() -> Path:
    return projects.cockpit_home() / "app"


def dev_clones() -> list[Path]:
    """Registered projects that are a cockpit checkout other than the running copy (local only)."""
    run = running_copy().resolve()
    out = []
    for proj in projects.load_registry()["projects"].values():
        p = Path(proj.get("code_path") or "")
        if (p / "cockpit" / "setup_flow.py").is_file() and p.resolve() != run:
            out.append(p)
    return out


def _code_lines(text: str, ext: str) -> list[tuple[int, str]]:
    """(1-based line number, line) for lines that are code, not comments (PowerShell block help too)."""
    out, in_block = [], False
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if ext == ".ps1":
            if in_block:
                in_block = "#>" not in s
                continue
            if s.startswith("<#"):
                in_block = "#>" not in s[2:]
                continue
            if s.startswith("#"):
                continue
        elif ext == ".sh" and s.startswith("#"):
            continue
        elif ext in (".cmd", ".bat") and (s.lower().startswith("rem ") or s.startswith("::")):
            continue
        out.append((n, line))
    return out


def classify_line(line: str) -> list[dict[str, Any]]:
    """Cockpit references in one line: [{kind, token, start, end, segment}] in order. ``segment`` is
    the index of the token's ``pm-qa-cockpit`` path segment when it is a full dev-clone path literal
    (so it can be rewritten), else None."""
    if any(v in line for v in OVERRIDE_VARS) or "://" in line:
        return []  # an explicit override, or a clone URL - not a launch path
    refs: list[dict[str, Any]] = []
    for m in _TOKEN.finditer(line):
        tok = m.group(0)
        parts = re.split(r"[\\/]", tok)
        kind, seg = None, None
        if ".pm-qa-cockpit" in parts:
            kind = "running"
        elif "pm-dashboard" in parts and len(parts) > 1:
            kind = "frozen"
        elif "pm-qa-cockpit" in parts and len(parts) > 1:
            kind = "dev"
            i = parts.index("pm-qa-cockpit")
            anchored = parts[0] in ("..", "~", "$HOME", "%USERPROFILE%") or re.fullmatch(r"[A-Za-z]:", parts[0])
            seg = i if i > 0 and anchored else None
        if kind:
            refs.append({"kind": kind, "token": tok, "start": m.start(), "end": m.end(), "segment": seg})
    for m in _QUOTED_NAME.finditer(line):
        # the bare folder name handed to Join-Path / path.join: a dev clone, but not safely rewritable
        refs.append({"kind": "dev", "token": m.group(0), "start": m.start(), "end": m.end(), "segment": None})
    return sorted(refs, key=lambda r: r["start"])


def proposed_line(line: str, ref: dict[str, Any], ext: str) -> str | None:
    """The line with a full dev-clone path literal pointed at the running copy, or None when no
    exact, safe rewrite exists (single-quoted strings don't expand $HOME / %USERPROFILE%)."""
    if ref["segment"] is None or ext not in RUNNING_EXPR:
        return None
    if ref["start"] and line[ref["start"] - 1] == "'":
        return None
    tok = ref["token"]
    seps = re.findall(r"[\\/]", tok)
    sep = seps[0] if seps else "\\"
    rest = re.split(r"[\\/]", tok)[ref["segment"] + 1:]
    expr = RUNNING_EXPR[ext].format(sep=sep)
    new_tok = sep.join([expr, *rest]) if rest else expr
    return line[: ref["start"]] + new_tok + line[ref["end"]:]


def _verdict(refs: list[dict[str, Any]]) -> str:
    first = refs[0]["kind"]
    if first == "running":
        return "ok" if running_copy().joinpath("cockpit", "server.py").is_file() else "missing"
    return {"dev": "dev-clone-first", "frozen": "frozen"}[first]


def launcher_files(code: Path) -> list[Path]:
    found = []
    if (code / "scripts").is_dir():
        found += [p for p in sorted((code / "scripts").iterdir()) if p.is_file() and p.suffix.lower() in SCRIPT_EXTS]
    found += [p for p in sorted(code.iterdir()) if p.is_file() and p.suffix.lower() in SCRIPT_EXTS]
    return found


def scan_launchers(code: Path) -> list[dict[str, Any]]:
    """Every launcher / orchestration file that references a cockpit, with its verdict.

    verdict: ok | missing (running copy first but not installed) | dev-clone-first | frozen.
    ``edit`` ({line, old, new}) is set only for a stale script with an exact safe rewrite."""
    out: list[dict[str, Any]] = []
    for f in launcher_files(code):
        try:
            text = f.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        ext = f.suffix.lower()
        refs = [(n, line, r) for n, line in _code_lines(text, ext) for r in classify_line(line)]
        if not refs:
            continue
        verdict = _verdict([r for _, _, r in refs])
        item = {"file": f.relative_to(code).as_posix(), "kind": "script", "verdict": verdict,
                "refs": [{"line": n, "kind": r["kind"], "text": line.strip()} for n, line, r in refs], "edit": None}
        if verdict == "dev-clone-first":
            n, line, r = refs[0]
            new = proposed_line(line, r, ext)
            if new is not None:
                item["edit"] = {"line": n, "old": line, "new": new}
        out.append(item)
    pkg = code / "package.json"
    if pkg.is_file():
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8-sig")).get("scripts") or {})
        except (OSError, json.JSONDecodeError, AttributeError):
            scripts = {}
        refs = [(name, r) for name, cmd in scripts.items() if isinstance(cmd, str) for r in classify_line(cmd)]
        if refs:
            out.append({"file": "package.json", "kind": "package-scripts", "verdict": _verdict([r for _, r in refs]),
                        "refs": [{"line": None, "script": name, "kind": r["kind"], "text": scripts[name]} for name, r in refs],
                        "edit": None})
    for name in ORCHESTRATION_FILES:
        f = code / name
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        refs = [(n, line, r) for n, line in enumerate(text.splitlines(), 1) for r in classify_line(line)]
        if refs:
            # prose: only a dev-clone path is worth a warning; mentions of frozen copies are history
            dev = [x for x in refs if x[2]["kind"] == "dev"]
            out.append({"file": name, "kind": "orchestration", "verdict": "dev-clone-mentioned" if dev else "ok",
                        "refs": [{"line": n, "kind": r["kind"], "text": line.strip()} for n, line, r in refs], "edit": None})
    return out


def scan_hooks(code: Path) -> list[dict[str, Any]]:
    """Hooks per tool with the cockpit paths they reference (Claude Code settings, Cursor hooks.json).
    problem: None | 'dev-clone' | 'missing-path'."""
    sources = [("claude", code / ".claude" / "settings.json"), ("claude", code / ".claude" / "settings.local.json"),
               ("cursor", code / ".cursor" / "hooks.json")]
    out: list[dict[str, Any]] = []
    for tool, path in sources:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        for event, entries in ((data.get("hooks") or {}) if isinstance(data, dict) else {}).items():
            for cmd in _hook_commands(entries):
                refs = classify_line(cmd)
                problem = None
                if any(r["kind"] == "dev" for r in refs):
                    problem = "dev-clone"
                elif any(r["kind"] == "running" for r in refs) and not running_copy().is_dir():
                    problem = "missing-path"
                elif any(r["kind"] == "frozen" and not _frozen_exists(code, r["token"]) for r in refs):
                    problem = "missing-path"
                out.append({"tool": tool, "file": path.relative_to(code).as_posix(), "event": event,
                            "command": cmd, "cockpit_refs": [r["kind"] for r in refs], "problem": problem})
    return out


def _hook_commands(entries: Any) -> list[str]:
    cmds: list[str] = []
    if isinstance(entries, dict):
        entries = [entries]
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        if isinstance(e.get("command"), str):
            cmds.append(e["command"])
        cmds += _hook_commands(e.get("hooks"))
    return cmds


def _frozen_exists(code: Path, token: str) -> bool:
    parts = re.split(r"[\\/]", token)
    i = parts.index("pm-dashboard")
    rel = [p for p in parts[: i + 1] if p and not p.startswith("$")]
    return (code.joinpath(*rel)).exists()


def apply_launcher_edit(code: Path, rel_file: str, edit: dict[str, Any]) -> None:
    """Replace exactly one line, re-checking it still reads as shown. Every other byte (line endings,
    BOM, trailing newline) is kept. Raises ValueError if the line changed."""
    f = code / rel_file
    data = f.read_bytes()
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    lines = data[len(bom):].decode("utf-8").splitlines(keepends=True)
    i = edit["line"] - 1
    if i >= len(lines) or lines[i].rstrip("\r\n") != edit["old"]:
        raise ValueError(f"{rel_file}:{edit['line']} changed since the plan was shown; nothing was written")
    eol = lines[i][len(lines[i].rstrip("\r\n")):]
    lines[i] = edit["new"] + eol
    f.write_bytes(bom + "".join(lines).encode("utf-8"))
