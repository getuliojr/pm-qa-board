"""
setup_flow.py - The engine behind the `pm-qa-setup` skill (Story 2 / PQC-2).

An agent never edits a user's orchestration by itself. It asks this module for a **plan** (read-only:
what exists, what is missing, numbered proposed actions with the exact files each would write), shows
it to the user, and then applies **only the action ids the user approved**.

Skill versions: every shipped skill carries ``metadata: {version, source: pm-qa-cockpit}`` in its
frontmatter, and an installed copy gets a ``.pm-qa-cockpit.json`` manifest recording the version and a
line-ending-insensitive content hash. That lets an upgrade tell "outdated" from "edited by the user",
and a same-named skill that isn't ours ("foreign") is never touched.

Standard library only.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import projects
from onboarding import inspect_project

TOOL_ROOT = Path(__file__).resolve().parent.parent
SOURCE = "pm-qa-cockpit"
MANIFEST = ".pm-qa-cockpit.json"
BOOTSTRAP_SKILL = "pm-qa-setup"
PROJECT_SKILLS = ("pm-qa-cockpit", "pm-orchestrate", "pm-sync", "pm-review")
POINTER_START = "<!-- pm-qa-cockpit:start -->"
POINTER_END = "<!-- pm-qa-cockpit:end -->"
GRILL_SOURCE = "https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me"

# Where each agent reads project skills. Cursor reads AGENTS.md (+ .cursor/rules) and is pointed at
# the Claude skills folder instead of receiving a third copy.
AGENT_SKILL_DIRS = {"claude": ".claude/skills", "antigravity": ".agents/skills"}
GLOBAL_ROOTS = {
    "claude": Path.home() / ".claude" / "skills",
    "antigravity": Path.home() / ".agents" / "skills",
    "cursor": Path.home() / ".cursor" / "skills-cursor",
}


# ---------------------------------------------------------------------------
# Versions and skill state
# ---------------------------------------------------------------------------


def tool_version(root: Path = TOOL_ROOT) -> str:
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")) or (0,)


def normalized(data: bytes) -> bytes:
    """Content with CRLF/CR folded to LF, so Windows checkouts never look 'changed'."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def content_hash(skill_dir: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in skill_dir.rglob("*") if p.is_file() and p.name != MANIFEST):
        h.update(f.relative_to(skill_dir).as_posix().encode())
        h.update(b"\0")
        h.update(normalized(f.read_bytes()))
        h.update(b"\0")
    return h.hexdigest()


def skill_metadata(skill_md: Path) -> dict[str, str]:
    """``metadata.version`` / ``metadata.source`` from a SKILL.md frontmatter (tiny YAML subset)."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    if not m:
        return {}
    meta: dict[str, str] = {}
    in_meta = False
    for line in m.group(1).splitlines():
        if re.match(r"^metadata:\s*$", line):
            in_meta = True
            continue
        if in_meta and re.match(r"^\s+\w+:", line):
            k, v = line.strip().split(":", 1)
            meta[k.strip()] = v.strip().strip("\"'")
        elif in_meta:
            in_meta = False
    return meta


def read_manifest(skill_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((skill_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def skill_state(installed: Path, source: Path) -> dict[str, Any]:
    """State of one installed skill vs the shipped one.

    missing  - not installed
    current  - same content as shipped (line endings ignored)
    outdated - ours, unedited since install, older version than shipped -> safe to upgrade on approval
    edited   - ours, but changed since install -> ask: keep / replace / save .new
    foreign  - a same-named skill that is not ours -> never touched
    """
    src_version = skill_metadata(source / "SKILL.md").get("version", "")
    if not (installed / "SKILL.md").is_file():
        return {"state": "missing", "installed_version": None, "available_version": src_version}
    manifest = read_manifest(installed)
    meta = skill_metadata(installed / "SKILL.md")
    inst_version = manifest.get("version") or meta.get("version")
    here = content_hash(installed)
    base = {"installed_version": inst_version, "available_version": src_version}
    if here == content_hash(source):
        return {"state": "current", **base}
    ours = manifest.get("source") == SOURCE or meta.get("source") == SOURCE
    if not ours:
        return {"state": "foreign", **base}
    if manifest.get("sha256") and manifest["sha256"] != here:
        return {"state": "edited", **base}
    if not manifest and meta.get("version") == src_version:
        return {"state": "edited", **base}  # same version, different content, no manifest: hand-edited
    if _vtuple(inst_version or "0") < _vtuple(src_version):
        return {"state": "outdated", **base}
    return {"state": "edited", **base}


def install_skill(source: Path, dest: Path, on_conflict: str = "abort") -> str:
    """Copy a shipped skill into ``dest`` and write its manifest. Returns what happened.

    on_conflict applies to 'edited'/'foreign': abort (default) | replace | new (write SKILL.md.new etc.).
    'foreign' can never be replaced.
    """
    state = skill_state(dest, source)["state"]
    if state == "current":
        _write_manifest(dest, source)
        return "current"
    if state == "foreign":
        return "skipped-foreign"
    if state == "edited" and on_conflict == "abort":
        return "skipped-edited"
    if state == "edited" and on_conflict == "new":
        for f in source.rglob("*"):
            if f.is_file():
                target = dest / f.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, target.with_name(target.name + ".new"))
        return "wrote-new"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    _write_manifest(dest, source)
    return "installed" if state == "missing" else "upgraded"


def _write_manifest(dest: Path, source: Path) -> None:
    meta = skill_metadata(source / "SKILL.md")
    (dest / MANIFEST).write_text(
        json.dumps(
            {
                "source": SOURCE,
                "version": meta.get("version", tool_version()),
                "sha256": content_hash(dest),
                "installed": datetime.datetime.now().isoformat(timespec="seconds"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Project inspection
# ---------------------------------------------------------------------------


def detect_agents(code: Path) -> list[str]:
    agents = []
    if (code / ".claude").is_dir() or (code / "CLAUDE.md").is_file():
        agents.append("claude")
    if (code / ".cursor").is_dir() or (code / ".cursorrules").is_file():
        agents.append("cursor")
    if (code / ".agents").is_dir() or (code / "GEMINI.md").is_file():
        agents.append("antigravity")
    return agents


def own_loop_skills(code: Path) -> list[str]:
    """A project's own orchestration skills (e.g. money-nest-orchestrate) — their presence means the
    project already runs a loop, so the cockpit registers only and installs no parallel skills."""
    found = []
    for rel in AGENT_SKILL_DIRS.values():
        d = code / rel
        if not d.is_dir():
            continue
        for s in d.iterdir():
            if s.is_dir() and s.name not in PROJECT_SKILLS and s.name != BOOTSTRAP_SKILL and re.search(
                r"orchestrat|opus-review|pm-sync", s.name
            ):
                found.append(f"{rel}/{s.name}")
    return sorted(set(found))


def _is_cockpit_skill(d: Path, name: str) -> bool:
    """A global copy of one of our skills: by manifest/metadata, or (pre-versioning copies) by its
    frontmatter name plus a mention of the cockpit."""
    if not (d / "SKILL.md").is_file():
        return False
    if read_manifest(d).get("source") == SOURCE or skill_metadata(d / "SKILL.md").get("source") == SOURCE:
        return True
    # Pre-versioning copy: same content as shipped once the metadata block is ignored.
    shipped = TOOL_ROOT / "skills" / name / "SKILL.md"
    if shipped.is_file():
        def strip(b: bytes) -> bytes:
            return re.sub(rb"\nmetadata:\n(?:[ \t]+[^\n]*\n)+", b"\n", normalized(b))

        if strip((d / "SKILL.md").read_bytes()) == strip(shipped.read_bytes()):
            return True
    text = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    return bool(re.search(rf"^name:\s*{re.escape(name)}\s*$", text, re.MULTILINE)) and "cockpit" in text.lower()


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _is_ignored(code: Path, rel: str) -> bool:
    res = subprocess.run(["git", "check-ignore", "-q", rel], cwd=str(code), capture_output=True, check=False)
    return res.returncode == 0


def pointer_text(project: dict[str, Any], register_only: bool) -> str:
    docs = project["docs_path"]
    lines = [
        POINTER_START,
        "## PM & QA Cockpit",
        "",
        f"Stories are tracked with PM & QA Cockpit (card prefix `{project['prefix']}`, docs in `{docs}`).",
        f"Dashboard: `powershell -File ~/.pm-qa-cockpit/app/cockpit.ps1 -Project {project['slug']}`"
        " → http://localhost:3333.",
    ]
    if register_only:
        lines.append(
            "This project keeps its own loop and skills; the cockpit only displays its cards. "
            "**This file's rules always win.**"
        )
    else:
        lines.append(
            "Follow the `pm-orchestrate` loop and the `pm-qa-cockpit`, `pm-sync` and `pm-review` skills. "
            "**If they conflict with anything else in this file, this file's rules win.**"
        )
    lines.append(POINTER_END)
    return "\n".join(lines) + "\n"


def build_plan(code_path: str | Path, *, name: str | None = None, prefix: str | None = None,
               docs_path: str | Path | None = None, stealth: bool | None = None) -> dict[str, Any]:
    """Read-only. Everything the agent shows the user before asking for approval."""
    code = Path(code_path).expanduser().resolve()
    registered = projects.find_project_for_path(code)
    if registered and Path(registered["code_path"]) != code:
        registered = None
    project = registered or projects.build_project(
        name or code.name, code, prefix=prefix or None, docs_path=docs_path or None
    )
    if stealth is None:
        stealth = bool(project.get("stealth"))
    report = inspect_project(code)
    agents = detect_agents(code)
    loop = own_loop_skills(code)
    register_only = bool(loop)
    exclude_skills = project["docs_mode"] == "external" or stealth

    actions: list[dict[str, Any]] = []

    def add(kind: str, title: str, **kw: Any) -> None:
        actions.append({"id": f"A{len(actions) + 1}", "kind": kind, "title": title, **kw})

    # 1. Prerequisites (machine level)
    prereq = {
        "uv": _which("uv"),
        "git": _which("git"),
        "running_copy": (projects.cockpit_home() / "app" / "cockpit" / "server.py").is_file(),
        "grill_me": any((r / n / "SKILL.md").is_file() for r in GLOBAL_ROOTS.values() for n in ("grill-me", "grilling")),
    }
    if not prereq["running_copy"]:
        add("clone-running-copy", "Clone the cockpit's running copy",
            writes=[str(projects.cockpit_home() / "app")],
            detail="git clone https://github.com/getuliojr/pm-qa-cockpit ~/.pm-qa-cockpit/app")
    if not prereq["grill_me"]:
        add("fetch-grill-me", "Install grill-me once for this machine (design-before-code skill)",
            writes=[str(GLOBAL_ROOTS["claude"] / "grill-me"), str(GLOBAL_ROOTS["antigravity"] / "grill-me")],
            detail=f"source: {GRILL_SOURCE}")

    # 2. Register + gates
    if not registered:
        add("register", f"Register '{project['name']}' (prefix {project['prefix']}, docs {project['docs_mode']})",
            writes=[str(projects.registry_path()), project["docs_path"]],
            detail=f"cli.py init {code} --name \"{project['name']}\" --prefix {project['prefix']}"
            + (f" --docs-path \"{project['docs_path']}\"" if project["docs_mode"] == "external" else "")
            + (" --stealth" if stealth else "") + " --apply")
    if (project.get("gates") or {}).get("mode") in (None, "unprofiled"):
        add("profile-gates", "Profile the test suite (fast <= 6 min runs inline; heavy splits out)",
            writes=[str(projects.registry_path())], detail="cli.py profile-gates --measure --apply")

    # 3. Skills per detected agent (none when the project runs its own loop)
    skills_status: list[dict[str, Any]] = []
    for agent in agents:
        rel = AGENT_SKILL_DIRS.get(agent)
        if not rel:
            continue
        for skill in PROJECT_SKILLS:
            dest = code / rel / skill
            st = skill_state(dest, TOOL_ROOT / "skills" / skill)
            skills_status.append({"agent": agent, "skill": skill, "path": f"{rel}/{skill}", **st})
            if register_only or st["state"] in ("current", "foreign"):
                continue
            verb = {"missing": "Install", "outdated": "Upgrade", "edited": "Resolve local edits in"}[st["state"]]
            add("install-skill", f"{verb} {skill} for {agent} ({rel}/{skill})", agent=agent, skill=skill,
                state=st["state"], writes=[str(dest)],
                detail="edited: choose keep / replace / new" if st["state"] == "edited" else
                f"{st['installed_version'] or '-'} -> {st['available_version']}")

    # 4. Pointer section in the orchestration file(s) — appended between markers, never rewriting text
    existing = [f for f, ok in report["orchestration_files"].items() if ok]
    if register_only:
        # The project's own loop already governs its agents; one note in its main file is enough.
        targets = existing[:1] or ["CLAUDE.md"]
    else:
        # Each detected agent's entry file: Claude Code -> CLAUDE.md, Cursor -> AGENTS.md,
        # Antigravity -> GEMINI.md (if the project has one) else AGENTS.md.
        wanted = {"claude": "CLAUDE.md", "cursor": "AGENTS.md",
                  "antigravity": "GEMINI.md" if "GEMINI.md" in existing else "AGENTS.md"}
        targets = sorted({wanted[a] for a in agents}) or existing[:1] or ["AGENTS.md"]
    for fname in targets:
        text = (code / fname).read_text(encoding="utf-8", errors="replace") if (code / fname).exists() else ""
        if POINTER_START in text:
            continue
        add("pointer", f"Add a PM & QA Cockpit section to {fname}", file=fname, writes=[str(code / fname)],
            preview=pointer_text(project, register_only))

    # 5. Keep installed skills out of git when docs are external / stealth (exact paths only)
    if exclude_skills and not register_only:
        paths = sorted({f"{AGENT_SKILL_DIRS[a]}/{s}/" for a in agents if a in AGENT_SKILL_DIRS for s in PROJECT_SKILLS})
        missing = [p for p in paths if not _is_ignored(code, p.rstrip("/"))]
        if missing:
            add("exclude", "Keep the installed cockpit skills out of git (.git/info/exclude)",
                paths=missing, writes=[str(code / ".git" / "info" / "exclude")])

    # 6. Legacy global copies of the project skills (installed before Story 2)
    legacy = [str(r / s) for r in GLOBAL_ROOTS.values() for s in PROJECT_SKILLS if _is_cockpit_skill(r / s, s)]
    if legacy:
        add("cleanup-global", "Remove the old global copies of the project skills (now installed per project)",
            paths=legacy, writes=legacy)

    # 7. The global bootstrap skill itself (upgrade only where it is installed; never create here)
    for agent, root in GLOBAL_ROOTS.items():
        st = skill_state(root / BOOTSTRAP_SKILL, TOOL_ROOT / "skills" / BOOTSTRAP_SKILL)
        if st["state"] in ("outdated", "edited"):
            add("upgrade-bootstrap", f"{'Upgrade' if st['state'] == 'outdated' else 'Resolve local edits in'} the global {BOOTSTRAP_SKILL} for {agent}",
                agent=agent, skill=BOOTSTRAP_SKILL, state=st["state"], writes=[str(root / BOOTSTRAP_SKILL)],
                detail=f"{st['installed_version'] or '-'} -> {st['available_version']}")

    return {
        "tool_version": tool_version(),
        "code_path": str(code),
        "project": project,
        "registered": bool(registered),
        "agents": agents,
        "own_loop_skills": loop,
        "mode": "register-only" if register_only else "full",
        "stealth": stealth,
        "prerequisites": prereq,
        "inspection": report,
        "skills": skills_status,
        "actions": actions,
    }


# ---------------------------------------------------------------------------
# Applying approved actions
# ---------------------------------------------------------------------------


def apply_actions(plan: dict[str, Any], approved: list[str], on_conflict: str = "abort") -> list[dict[str, str]]:
    """Run only the approved action ids. Registration / gates / clone / grill-me fetch are left to
    their own commands (returned as `run` instructions) so the agent shows their output."""
    code = Path(plan["code_path"])
    by_id = {a["id"]: a for a in plan["actions"]}
    unknown = [a for a in approved if a not in by_id]
    if unknown:
        raise ValueError(f"unknown action id(s): {', '.join(unknown)}")
    results: list[dict[str, str]] = []
    for aid in approved:
        a = by_id[aid]
        kind = a["kind"]
        if kind == "install-skill":
            dest = code / AGENT_SKILL_DIRS[a["agent"]] / a["skill"]
            outcome = install_skill(TOOL_ROOT / "skills" / a["skill"], dest, on_conflict)
            results.append({"id": aid, "result": outcome})
        elif kind == "pointer":
            f = code / a["file"]
            text = f.read_text(encoding="utf-8") if f.exists() else ""
            if POINTER_START not in text:
                sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
                f.write_text(text + sep + a["preview"], encoding="utf-8")
            results.append({"id": aid, "result": "written"})
        elif kind == "exclude":
            exclude = code / ".git" / "info" / "exclude"
            existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
            add = [p for p in a["paths"] if p not in existing]
            if add:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                with exclude.open("a", encoding="utf-8") as fh:
                    if existing and existing[-1].strip():
                        fh.write("\n")
                    fh.write("# PM & QA Cockpit skills (installed per project)\n" + "\n".join(add) + "\n")
            results.append({"id": aid, "result": f"excluded {len(add)} path(s)"})
        elif kind == "upgrade-bootstrap":
            dest = GLOBAL_ROOTS[a["agent"]] / BOOTSTRAP_SKILL
            results.append({"id": aid, "result": install_skill(TOOL_ROOT / "skills" / BOOTSTRAP_SKILL, dest, on_conflict)})
        elif kind == "cleanup-global":
            removed = 0
            for p in a["paths"]:
                d = Path(p)
                if d.is_dir():
                    shutil.rmtree(d)
                    removed += 1
            results.append({"id": aid, "result": f"removed {removed} folder(s)"})
        else:
            results.append({"id": aid, "result": "run", "command": a.get("detail", "")})
    return results


def skills_status(code_path: str | Path | None = None) -> dict[str, Any]:
    """Read-only: the global bootstrap skill and, for a project, its installed cockpit skills."""
    out: dict[str, Any] = {"tool_version": tool_version(), "global": [], "project": []}
    for agent, root in GLOBAL_ROOTS.items():
        st = skill_state(root / BOOTSTRAP_SKILL, TOOL_ROOT / "skills" / BOOTSTRAP_SKILL)
        out["global"].append({"agent": agent, "skill": BOOTSTRAP_SKILL, "path": str(root / BOOTSTRAP_SKILL), **st})
    if code_path:
        code = Path(code_path).resolve()
        for agent in detect_agents(code):
            rel = AGENT_SKILL_DIRS.get(agent)
            if not rel:
                continue
            for skill in PROJECT_SKILLS:
                st = skill_state(code / rel / skill, TOOL_ROOT / "skills" / skill)
                out["project"].append({"agent": agent, "skill": skill, "path": f"{rel}/{skill}", **st})
    return out
