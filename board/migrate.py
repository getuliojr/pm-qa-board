"""
migrate.py - move an existing install from the old product name to PM & QA Board.

This is the ONLY module that knows the old names (a test enforces it). Everything here is a plan
first: ``plan()`` reads and never writes; ``apply()`` runs only what the user approved, and never
renames the folder that holds running code (Windows cannot), so the new running copy is a fresh clone
and the old folder is left for the user to delete.

What moves, and how (Story 34):
  moves      projects.json, cache/, plans/, bin/ are COPIED to ~/.pm-qa-board (never overwriting)
  clone      a fresh running copy is cloned into ~/.pm-qa-board/app
  launchers  project launcher scripts and hook commands that call the old path are repointed, one
             line at a time, after a backup (wiring.apply_launcher_edit re-checks each line)
  files      the managed blocks the old setup wrote into CLAUDE.md / AGENTS.md / ORCHESTRATION.md
             (pointer and roster markers, paths, skill names) are rewritten, only inside the markers
  agents     the old thin agent files (<old prefix>-<role>.md) are renamed and rewritten
  skills     old install manifests are converted so edited old skills stay recognised; the replaced
             old skill folder is removed only when provably unedited AND its replacement is installed
  shortcuts  desktop shortcuts (Windows) are repointed

Standard library only.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import projects
import setup_flow
import wiring

OLD_DIR = ".pm-qa-cockpit"
OLD_SKILL = "pm-qa-cockpit"
OLD_MANIFEST = ".pm-qa-cockpit.json"
OLD_ENV_PREFIX = "PM_QA_COCKPIT_"
OLD_AGENT_PREFIX = "cockpit-"
DOCS_FOLDER = "pm-qa-docs/pm-qa-cockpit"  # the private PM-data folder keeps its name (Story 34, decision 4)
REPO_URL = "https://github.com/getuliojr/pm-qa-board"
MOVE_ITEMS = ("projects.json", "cache", "plans", "bin")
PARTS = ("moves", "clone", "launchers", "files", "agents", "skills", "shortcuts")
SKILL_NAMES = ("pm-orchestrate", "pm-review", "pm-sync", "pm-qa-setup", OLD_SKILL)
HOOK_FILES = (".claude/settings.json", ".claude/settings.local.json", ".cursor/hooks.json")
AGENT_DIRS = (".claude/agents", ".agents/agents", ".cursor/agents")
_REGION = re.compile(r"<!-- pm-qa-cockpit:(roster:)?start -->.*?<!-- pm-qa-cockpit:(roster:)?end -->", re.DOTALL)
_PROTECT = "\u0001DOCS\u0001"


def old_home() -> Path:
    return projects.user_home() / OLD_DIR


# ---------------------------------------------------------------------------
# Reading the old install
# ---------------------------------------------------------------------------


def _registry_projects() -> list[tuple[Path, Path | None]]:
    """(code folder, docs folder) of registered projects, from the new registry if it exists, else the old."""
    for reg in (projects.board_home() / "projects.json", old_home() / "projects.json"):
        try:
            data = json.loads(reg.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        out = []
        for proj in (data.get("projects") or {}).values():
            p = Path(proj.get("code_path") or "")
            if p.is_dir():
                d = Path(proj["docs_path"]) if proj.get("docs_path") else None
                out.append((p, d if d and d.is_dir() else None))
        return out
    return []


def repoint_line(line: str) -> str | None:
    """A launcher or hook line with the old install path, the old package folder under it and old env
    var names replaced, or None. The package folder is only rewritten right after the old app path, so
    an unrelated ``webapp/cockpit/`` in a project is never touched."""
    new = re.sub(r"(" + re.escape(OLD_DIR) + r")([\\/]+app[\\/]+)cockpit([\\/]+)", r"\1\2board\3", line, flags=re.IGNORECASE)
    new = re.sub(re.escape(OLD_DIR), ".pm-qa-board", new, flags=re.IGNORECASE)
    new = new.replace(OLD_ENV_PREFIX, "PM_QA_BOARD_")
    return new if new != line else None


def broad_line(line: str) -> str | None:
    """A line of a managed block (text this tool wrote itself) with every old name replaced."""
    t = line.replace(DOCS_FOLDER, _PROTECT)
    t = t.replace("COCKPIT", "BOARD").replace("Cockpit", "Board").replace("cockpit", "board").replace(_PROTECT, DOCS_FOLDER)
    return t if t != line else None


def _lines(f: Path) -> list[str] | None:
    """The file's lines, or None when it cannot be edited safely (UTF-16 or not text)."""
    try:
        data = f.read_bytes()
    except OSError:
        return None
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:400]:
        return None
    try:
        return data.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        return None


def scan_launchers(code: Path, extra_hook_only: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """(per-line edits, files that need a manual edit) for the launcher scripts and hook files of one
    project that call the old path. ``extra_hook_only``: only the hook files (used for the user folder)."""
    files = [] if extra_hook_only else list(wiring.launcher_files(code))
    files += [code / h for h in HOOK_FILES if (code / h).is_file()]
    edits: list[dict[str, Any]] = []
    manual: list[str] = []
    for f in files:
        lines = _lines(f)
        if lines is None:
            try:
                raw = f.read_bytes()
            except OSError:
                continue
            if OLD_DIR.encode().lower() in raw.lower() or OLD_DIR.encode("utf-16-le").lower() in raw.lower():
                manual.append(str(f))
            continue
        for n, line in enumerate(lines, 1):
            new = repoint_line(line)
            if new is not None:
                edits.append({"code": str(code), "file": f.relative_to(code).as_posix(), "line": n, "old": line, "new": new})
    return edits, manual


def scan_managed(root: Path, rels: list[str]) -> list[dict[str, Any]]:
    """Per-line edits inside the markers this tool's old setup wrote (never outside them)."""
    edits: list[dict[str, Any]] = []
    for rel in rels:
        f = root / rel
        if not f.is_file():
            continue
        lines = _lines(f)
        if lines is None:
            continue
        text = "\n".join(lines)
        inside: set[int] = set()
        for m in _REGION.finditer(text):
            first = text.count("\n", 0, m.start()) + 1
            last = text.count("\n", 0, m.end()) + 1
            inside.update(range(first, last + 1))
        for n in sorted(inside):
            new = broad_line(lines[n - 1])
            if new is not None:
                edits.append({"code": str(root), "file": rel, "line": n, "old": lines[n - 1], "new": new})
    return edits


def scan_agents(code: Path) -> list[dict[str, Any]]:
    out = []
    for rel in AGENT_DIRS:
        d = code / rel
        if not d.is_dir():
            continue
        for f in sorted(d.glob(OLD_AGENT_PREFIX + "*.md")):
            target = d / ("board-" + f.name[len(OLD_AGENT_PREFIX):])
            out.append({"code": str(code), "old": str(f), "new": str(target), "target_exists": target.exists()})
    return out


def _skill_dirs(codes: list[Path]) -> list[Path]:
    roots: list[Path] = list(setup_flow.global_roots().values())
    for tool in setup_flow.TOOL_ORDER:
        roots += setup_flow.global_read_roots(tool)
        for code in codes:
            roots += [code / rel for rel in setup_flow.project_read_dirs(tool)]
    seen, out = set(), []
    for r in roots:
        key = str(r.resolve()) if r.exists() else str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _hash_without_manifests(d: Path) -> str:
    return setup_flow.files_hash({p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*")
                                  if p.is_file() and p.name not in (OLD_MANIFEST, setup_flow.MANIFEST)})


def _old_manifest(d: Path) -> dict[str, Any]:
    try:
        return json.loads((d / OLD_MANIFEST).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def scan_skills(codes: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for root in _skill_dirs(codes):
        for name in SKILL_NAMES:
            d = root / name
            if not (d / "SKILL.md").is_file():
                continue
            if name == OLD_SKILL:
                digest = _hash_without_manifests(d)
                unedited = digest == _old_manifest(d).get("sha256") or bool(setup_flow.ledger_version(OLD_SKILL, digest))
                replaced = (root / "pm-qa-board" / "SKILL.md").is_file()
                if not unedited:
                    out.append({"path": str(d), "action": "leave-edited",
                                "why": "edited since install, so it is left alone (remove it by hand if you do not need it)"})
                elif not replaced:
                    out.append({"path": str(d), "action": "keep-until-replaced",
                                "why": "unedited, but pm-qa-board is not installed here yet; run pm-qa-setup, then run migrate-home again to remove it"})
                else:
                    out.append({"path": str(d), "action": "remove-old-skill", "why": "replaced by the pm-qa-board skill installed next to it; unedited"})
            elif (d / OLD_MANIFEST).is_file():
                out.append({"path": str(d), "action": "convert-manifest",
                            "why": "keep this install recognised under the new name (content is not touched)"})
    return out


def skills_actionable(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in skills if s["action"] in ("remove-old-skill", "convert-manifest")]


def _desktop_dirs() -> list[Path]:
    home = projects.user_home()
    dirs = [home / "Desktop"] + sorted(home.glob("OneDrive*/Desktop"))
    pub = os.environ.get("PUBLIC")
    if pub:
        dirs.append(Path(pub) / "Desktop")
    return dirs


def scan_shortcuts() -> list[str]:
    if sys.platform != "win32":
        return []
    needle = [OLD_DIR.encode("ascii"), OLD_DIR.encode("utf-16-le")]
    out = []
    for d in _desktop_dirs():
        for lnk in sorted(d.glob("*.lnk")) if d.is_dir() else []:
            try:
                data = lnk.read_bytes()
            except OSError:
                continue
            if any(n.lower() in data.lower() for n in needle):
                out.append(str(lnk))
    return out


def dashboards_running() -> list[int]:
    """Process ids of a dashboard server started from the old folder (best effort, empty on failure).
    The process list is filtered here, not in the query, so the query never matches itself."""
    me = os.getpid()
    needle = str(old_home()).lower().replace("/", "\\")  # this install's exact folder, not the folder name anywhere
    try:
        if sys.platform == "win32":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                               capture_output=True, text=True, timeout=60)
            data = json.loads(r.stdout or "[]")
            data = [data] if isinstance(data, dict) else data
            return [int(p["ProcessId"]) for p in data
                    if p.get("CommandLine") and int(p["ProcessId"]) != me and "server.py" in p["CommandLine"]
                    and needle in p["CommandLine"].lower().replace("/", "\\") and "Get-CimInstance" not in p["CommandLine"]]
        r = subprocess.run(["pgrep", "-f", f"{re.escape(str(old_home()))}.*server.py"], capture_output=True, text=True, timeout=10)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit() and int(x) != me]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Plan (read-only)
# ---------------------------------------------------------------------------


def _pending_moves(old: Path, new: Path) -> list[dict[str, str]]:
    return [{"src": str(old / i), "dst": str(new / i)} for i in MOVE_ITEMS if (old / i).exists() and not (new / i).exists()]


def plan() -> dict[str, Any]:
    old, new = old_home(), projects.board_home()
    proj = _registry_projects()
    codes = [c for c, _ in proj]
    moves = _pending_moves(old, new)
    clone = None
    if not (new / "app" / "board" / "server.py").is_file() and (old.exists() or moves):
        clone = {"url": REPO_URL, "dest": str(new / "app")}
    launchers: list[dict[str, Any]] = []
    manual: list[str] = []
    files: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    for code, docs in proj:
        e, m = scan_launchers(code)
        launchers += e
        manual += m
        files += scan_managed(code, list(wiring.ORCHESTRATION_FILES))
        if docs is not None:
            files += scan_managed(docs, ["ORCHESTRATION.md"])
        agents += scan_agents(code)
    e, m = scan_launchers(projects.user_home(), extra_hook_only=True)  # user-level hooks
    launchers += e
    manual += m
    skills = scan_skills(codes)
    shortcuts = scan_shortcuts()
    blockers = []
    if old.exists():
        pids = dashboards_running()
        if pids:
            blockers.append(f"a dashboard started from {old} is running (process {', '.join(map(str, pids))}); close it first")
        if clone and not shutil.which("git"):
            blockers.append("git is not installed, so the new running copy cannot be cloned")
    todo = bool(moves or clone or launchers or files or agents or skills_actionable(skills) or shortcuts)
    if not old.exists() and not todo:
        state = "nothing-to-do"
    elif not todo:
        state = "already-migrated" if new.exists() else "nothing-to-do"
    else:
        state = "blocked" if blockers else "ready"
    return {"state": state, "old_home": str(old), "new_home": str(new), "moves": moves, "clone": clone,
            "launchers": launchers, "files": files, "agents": agents, "skills": skills, "shortcuts": shortcuts,
            "manual": manual, "blockers": blockers, "old_folder_remains": old.exists()}


def legacy_state() -> dict[str, Any]:
    """Cheap check for doctor and setup: is there an old install that has not been moved over? Also true
    when the new running copy was cloned first but the old registry and tools are still waiting."""
    old, new = old_home(), projects.board_home()
    app_ok = (new / "app" / "board" / "server.py").is_file()
    pending = bool(_pending_moves(old, new)) or not app_ok
    return {"old_home_exists": old.exists(), "new_home_exists": new.exists(), "needs_migration": old.exists() and pending}


# ---------------------------------------------------------------------------
# Apply (only what the user approved)
# ---------------------------------------------------------------------------


def _backup(backup_root: Path, code: Path, rel: str) -> None:
    tag = re.sub(r"[:\\/]+", "_", code.name or "root") + "-" + hashlib.sha1(str(code).encode("utf-8")).hexdigest()[:8]
    dst = backup_root / tag / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.copy2(code / rel, dst)


def _clone(url: str, dest: str) -> None:
    subprocess.run(["git", "clone", "--quiet", url, dest], check=True, capture_output=True, text=True, timeout=600)


def _repoint_shortcut(lnk: str) -> None:
    """The shortcut path travels in an environment variable: PowerShell does not pass trailing
    arguments to a -Command string, and a file name must never be parsed as code."""
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:PMQA_LNK);"
          "foreach($p in 'TargetPath','Arguments','WorkingDirectory','IconLocation'){$v=$s.$p;"
          f"if($v){{$s.$p=[regex]::Replace($v,[regex]::Escape('{OLD_DIR}'),'.pm-qa-board','IgnoreCase')}}}};$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True, text=True, timeout=60,
                   env={**os.environ, "PMQA_LNK": lnk})


def _convert_manifest(d: Path) -> None:
    data = _old_manifest(d)
    data["source"] = setup_flow.SOURCE
    (d / setup_flow.MANIFEST).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (d / OLD_MANIFEST).unlink()


def _rename_agent(a: dict[str, Any], backup_root: Path) -> None:
    old, new = Path(a["old"]), Path(a["new"])
    if new.exists():
        raise FileExistsError(f"{new.name} already exists; both files were left as they are")
    code = Path(a["code"])
    _backup(backup_root, code, old.relative_to(code).as_posix())
    text = old.read_text(encoding="utf-8")
    new.write_text("\n".join(broad_line(l) or l for l in text.split("\n")), encoding="utf-8", newline="")
    old.unlink()


def apply(skip: tuple[str, ...] = ()) -> dict[str, Any]:
    """Re-plan, then run every part not in ``skip``. Nothing runs when a blocker exists."""
    p = plan()
    result: dict[str, Any] = {"state_before": p["state"], "done": [], "failed": [], "skipped": list(skip)}
    if p["state"] in ("nothing-to-do", "already-migrated"):
        return result
    if p["blockers"]:
        result["failed"].append({"part": "blockers", "error": "; ".join(p["blockers"])})
        return result
    new = Path(p["new_home"])
    backup_root = new / "migration-backup" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    new.mkdir(parents=True, exist_ok=True)

    def attempt(part: str, what: str, fn) -> bool:
        try:
            fn()
            result["done"].append({"part": part, "what": what})
            return True
        except Exception as e:  # report, keep going: nothing here is destructive to the old folder
            result["failed"].append({"part": part, "what": what, "error": f"{type(e).__name__}: {e}"})
            return False

    if "moves" not in skip:
        for m in p["moves"]:
            src, dst = Path(m["src"]), Path(m["dst"])
            attempt("moves", f"copy {src.name}", lambda s=src, d=dst: shutil.copytree(s, d) if s.is_dir() else shutil.copy2(s, d))
    new_app_ok = (new / "app" / "board" / "server.py").is_file()
    if p["clone"] and "clone" not in skip:
        new_app_ok = attempt("clone", f"clone {p['clone']['url']}", lambda: _clone(p["clone"]["url"], p["clone"]["dest"])) \
            and (new / "app" / "board" / "server.py").is_file()

    def edit_all(part: str, edits: list[dict[str, Any]]) -> None:
        if part in skip or not edits:
            return
        if not new_app_ok:  # never repoint anything at a running copy that does not exist
            result["failed"].append({"part": part, "error": "the new running copy is missing, so nothing was changed"})
            return
        for e in edits:
            root = Path(e["code"])

            def _edit(e=e, root=root) -> None:
                _backup(backup_root, root, e["file"])
                wiring.apply_launcher_edit(root, e["file"], e)
            attempt(part, f"{e['file']}:{e['line']}", _edit)

    edit_all("launchers", p["launchers"])
    edit_all("files", p["files"])
    if "agents" not in skip and p["agents"]:
        if not new_app_ok:
            result["failed"].append({"part": "agents", "error": "the new running copy is missing, so nothing was changed"})
        else:
            for a in p["agents"]:
                attempt("agents", f"{Path(a['old']).name} -> {Path(a['new']).name}", lambda a=a: _rename_agent(a, backup_root))
    if "skills" not in skip:
        for s in p["skills"]:
            d = Path(s["path"])
            if s["action"] == "convert-manifest":
                attempt("skills", f"convert manifest in {d}", lambda d=d: _convert_manifest(d))
            elif s["action"] == "remove-old-skill":
                attempt("skills", f"remove old skill {d}", lambda d=d: shutil.rmtree(d))
    if "shortcuts" not in skip and new_app_ok:
        for lnk in p["shortcuts"]:
            attempt("shortcuts", f"repoint {lnk}", lambda lnk=lnk: _repoint_shortcut(lnk))
    result["old_folder_remains"] = Path(p["old_home"]).exists()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _edit_block(title: str, edits: list[dict[str, Any]]) -> list[str]:
    out = [title]
    for e in edits:
        out += [f"  {e['code']}  {e['file']}:{e['line']}", f"    - {e['old'].strip()}", f"    + {e['new'].strip()}"]
    return out + [""]


def format_plan(p: dict[str, Any]) -> str:
    out = [f"State: {p['state']}", f"Old home: {p['old_home']}", f"New home: {p['new_home']}", ""]
    if p["blockers"]:
        out += ["BLOCKED:"] + [f"  - {b}" for b in p["blockers"]] + [""]
    if p["moves"]:
        out += ["Copy (the old files stay where they are):"] + [f"  {m['src']} -> {m['dst']}" for m in p["moves"]] + [""]
    if p["clone"]:
        out += [f"Clone the new running copy: git clone {p['clone']['url']} {p['clone']['dest']}", ""]
    if p["launchers"]:
        out += _edit_block("Repoint launchers and hooks (a backup is kept):", p["launchers"])
    if p["files"]:
        out += _edit_block("Rewrite the managed blocks in CLAUDE.md / AGENTS.md / ORCHESTRATION.md (only between the markers):", p["files"])
    if p["agents"]:
        out += ["Rename the old agent files (contents rewritten, a backup is kept):"]
        out += [f"  {a['old']} -> {a['new']}" + ("   (target exists: will be reported, nothing overwritten)" if a["target_exists"] else "") for a in p["agents"]] + [""]
    if p["skills"]:
        out += ["Installed skills:"] + [f"  [{s['action']}] {s['path']} - {s['why']}" for s in p["skills"]] + [""]
    if p["shortcuts"]:
        out += ["Desktop shortcuts to repoint:"] + [f"  {s}" for s in p["shortcuts"]] + [""]
    if p["manual"]:
        out += ["Edit by hand (UTF-16 or unreadable files that mention the old path; the tool does not touch them):"] + [f"  {m}" for m in p["manual"]] + [""]
    if p["state"] == "ready":
        out.append("Nothing was changed. To apply everything above: migrate-home --apply   (add --skip launchers,skills,... to leave parts out)")
    elif p["state"] == "already-migrated":
        out.append("Already migrated; there is nothing to do." + (f" The old folder {p['old_home']} can be deleted once you have checked everything works." if p["old_folder_remains"] else ""))
    elif p["state"] == "nothing-to-do":
        out.append("No old install found; nothing to do.")
    return "\n".join(out)


def cmd_migrate_home(args: Any) -> int:
    skip = tuple(x.strip() for x in (args.skip or "").split(",") if x.strip())
    bad = [s for s in skip if s not in PARTS]
    if bad:
        print(f"Unknown part(s) in --skip: {', '.join(bad)}. Choose from: {', '.join(PARTS)}", file=sys.stderr)
        return 2
    if not args.apply:
        p = plan()
        print(json.dumps(p, indent=2) if args.json else format_plan(p))
        return 2 if p["state"] == "blocked" else 0
    res = apply(skip)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        for d in res["done"]:
            print(f"done    [{d['part']}] {d['what']}")
        for f in res["failed"]:
            print(f"FAILED  [{f['part']}] {f.get('what', '')} {f['error']}")
        if not res["done"] and not res["failed"]:
            print("Nothing to do.")
        if res.get("old_folder_remains") and res["done"] and not res["failed"]:
            print(f"\nFinished. Check that the dashboard starts from the new folder, then delete the old one: {old_home()}")
    return 1 if res["failed"] else 0
