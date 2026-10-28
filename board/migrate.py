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
  skills     old install manifests are converted so edited old skills stay recognised; the replaced
             old skill folder is removed only when provably unedited
  shortcuts  desktop shortcuts (Windows) are repointed

Standard library only.
"""

from __future__ import annotations

import datetime
import json
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
REPO_URL = "https://github.com/getuliojr/pm-qa-board"
MOVE_ITEMS = ("projects.json", "cache", "plans", "bin")
PARTS = ("moves", "clone", "launchers", "skills", "shortcuts")
SKILL_NAMES = ("pm-orchestrate", "pm-review", "pm-sync", "pm-qa-setup", OLD_SKILL)
HOOK_FILES = (".claude/settings.json", ".claude/settings.local.json", ".cursor/hooks.json")


def old_home() -> Path:
    return projects.user_home() / OLD_DIR


# ---------------------------------------------------------------------------
# Reading the old install
# ---------------------------------------------------------------------------


def _registry_codes() -> list[Path]:
    """Code folders of registered projects, from the new registry if it exists, else the old one."""
    for reg in (projects.board_home() / "projects.json", old_home() / "projects.json"):
        try:
            data = json.loads(reg.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        out = []
        for proj in (data.get("projects") or {}).values():
            p = Path(proj.get("code_path") or "")
            if p.is_dir():
                out.append(p)
        return out
    return []


def repoint_line(line: str) -> str | None:
    """The line with old paths, the old package folder and old env var names replaced, or None."""
    new = re.sub(re.escape(OLD_DIR), ".pm-qa-board", line, flags=re.IGNORECASE)
    new = re.sub(r"(app[\\/]+)cockpit([\\/]+)", r"\1board\2", new)
    new = new.replace(OLD_ENV_PREFIX, "PM_QA_BOARD_")
    return new if new != line else None


def scan_launchers(code: Path) -> list[dict[str, Any]]:
    """Per-line edits for every launcher script and hook file of one project that calls the old path."""
    files = list(wiring.launcher_files(code)) + [code / h for h in HOOK_FILES if (code / h).is_file()]
    edits: list[dict[str, Any]] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            new = repoint_line(line)
            if new is not None:
                edits.append({"code": str(code), "file": f.relative_to(code).as_posix(), "line": n, "old": line, "new": new})
    return edits


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
            has_old = (d / OLD_MANIFEST).is_file()
            if name == OLD_SKILL:
                digest = _hash_without_manifests(d)
                manifest = _old_manifest(d)
                unedited = digest == manifest.get("sha256") or bool(setup_flow.ledger_version(OLD_SKILL, digest))
                out.append({"path": str(d), "action": "remove-old-skill" if unedited else "leave-edited",
                            "why": "replaced by the pm-qa-board skill; unedited" if unedited
                            else "edited since install, so it is left alone (remove it by hand if you do not need it)"})
            elif has_old:
                out.append({"path": str(d), "action": "convert-manifest",
                            "why": "keep this install recognised under the new name (content is not touched)"})
    return out


def _desktop_dirs() -> list[Path]:
    return [projects.user_home() / "Desktop"]


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
    """Process ids of a dashboard server started from the old folder (best effort, empty on failure)."""
    old = str(old_home())
    try:
        if sys.platform == "win32":
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*server.py*' -and "
                  f"$_.CommandLine -like '*{OLD_DIR}*' }} | Select-Object -ExpandProperty ProcessId")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20)
        else:
            r = subprocess.run(["pgrep", "-f", f"{old}.*server.py"], capture_output=True, text=True, timeout=10)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


# ---------------------------------------------------------------------------
# Plan (read-only)
# ---------------------------------------------------------------------------


def plan() -> dict[str, Any]:
    old, new = old_home(), projects.board_home()
    codes = _registry_codes()
    moves = [{"src": str(old / i), "dst": str(new / i)} for i in MOVE_ITEMS
             if (old / i).exists() and not (new / i).exists()]
    clone = None
    if not (new / "app" / "board" / "server.py").is_file() and (old.exists() or moves):
        clone = {"url": REPO_URL, "dest": str(new / "app")}
    launchers = [e for c in codes for e in scan_launchers(c)]
    skills = scan_skills(codes)
    shortcuts = scan_shortcuts()
    blockers = []
    if old.exists():
        pids = dashboards_running()
        if pids:
            blockers.append(f"a dashboard started from {old} is running (process {', '.join(map(str, pids))}); close it first")
        if clone and not shutil.which("git"):
            blockers.append("git is not installed, so the new running copy cannot be cloned")
    todo = bool(moves or clone or launchers or skills_actionable(skills) or shortcuts)
    if not old.exists() and not todo:
        state = "nothing-to-do"
    elif not todo:
        state = "already-migrated" if new.exists() else "nothing-to-do"
    else:
        state = "blocked" if blockers else "ready"
    return {"state": state, "old_home": str(old), "new_home": str(new), "moves": moves, "clone": clone,
            "launchers": launchers, "skills": skills, "shortcuts": shortcuts, "blockers": blockers,
            "old_folder_remains": old.exists()}


def skills_actionable(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in skills if s["action"] != "leave-edited"]


def legacy_state() -> dict[str, Any]:
    """Cheap check for doctor and setup: is there an old install that has not been moved over?"""
    old, new = old_home(), projects.board_home()
    return {"old_home_exists": old.exists(), "new_home_exists": new.exists(),
            "needs_migration": old.exists() and not (new / "app" / "board" / "server.py").is_file()}


# ---------------------------------------------------------------------------
# Apply (only what the user approved)
# ---------------------------------------------------------------------------


def _backup(backup_root: Path, code: Path, rel: str) -> None:
    dst = backup_root / re.sub(r"[:\\/]+", "_", str(code)).strip("_") / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.copy2(code / rel, dst)


def _clone(url: str, dest: str) -> None:
    subprocess.run(["git", "clone", "--quiet", url, dest], check=True, capture_output=True, text=True, timeout=600)


def _repoint_shortcut(lnk: str) -> None:
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($args[0]);"
          f"foreach($p in 'TargetPath','Arguments','WorkingDirectory','IconLocation'){{$v=$s.$p;"
          f"if($v){{$s.$p=[regex]::Replace($v,[regex]::Escape('{OLD_DIR}'),'.pm-qa-board','IgnoreCase')}}}};$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps, lnk], check=True, capture_output=True, text=True, timeout=60)


def _convert_manifest(d: Path) -> None:
    data = _old_manifest(d)
    data["source"] = setup_flow.SOURCE
    (d / setup_flow.MANIFEST).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (d / OLD_MANIFEST).unlink()


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
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = new / "migration-backup" / stamp
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
            attempt("moves", f"copy {src.name}",
                    lambda s=src, d=dst: shutil.copytree(s, d) if s.is_dir() else shutil.copy2(s, d))
    new_app_ok = (new / "app" / "board" / "server.py").is_file()
    if p["clone"] and "clone" not in skip:
        new_app_ok = attempt("clone", f"clone {p['clone']['url']}", lambda: _clone(p["clone"]["url"], p["clone"]["dest"])) \
            and (new / "app" / "board" / "server.py").is_file()
    # never repoint anything at a running copy that does not exist
    if "launchers" not in skip:
        if not new_app_ok:
            result["failed"].append({"part": "launchers", "error": "the new running copy is missing, so no launcher was changed"})
        else:
            for e in p["launchers"]:
                code = Path(e["code"])

                def _edit(e=e, code=code) -> None:
                    _backup(backup_root, code, e["file"])
                    wiring.apply_launcher_edit(code, e["file"], e)
                attempt("launchers", f"{e['file']}:{e['line']}", _edit)
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


def format_plan(p: dict[str, Any]) -> str:
    out = [f"State: {p['state']}", f"Old home: {p['old_home']}", f"New home: {p['new_home']}", ""]
    if p["blockers"]:
        out += ["BLOCKED:"] + [f"  - {b}" for b in p["blockers"]] + [""]
    if p["moves"]:
        out += ["Copy (the old files stay where they are):"] + [f"  {m['src']} -> {m['dst']}" for m in p["moves"]] + [""]
    if p["clone"]:
        out += [f"Clone the new running copy: git clone {p['clone']['url']} {p['clone']['dest']}", ""]
    if p["launchers"]:
        out += ["Repoint launchers and hooks (a backup is kept):"]
        for e in p["launchers"]:
            out += [f"  {e['code']}  {e['file']}:{e['line']}", f"    - {e['old'].strip()}", f"    + {e['new'].strip()}"]
        out += [""]
    if p["skills"]:
        out += ["Installed skills:"] + [f"  [{s['action']}] {s['path']} - {s['why']}" for s in p["skills"]] + [""]
    if p["shortcuts"]:
        out += ["Desktop shortcuts to repoint:"] + [f"  {s}" for s in p["shortcuts"]] + [""]
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
