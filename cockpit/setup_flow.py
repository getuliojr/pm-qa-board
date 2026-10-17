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
import roster
import wiring
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
LEDGER = TOOL_ROOT / "skills" / "RELEASES.json"


def global_roots() -> dict[str, Path]:
    """Global skill folders the cockpit installs into, per tool. Evaluated per call (never at import)
    so a sandbox ``PM_QA_USER_HOME`` set after import still applies."""
    home = projects.user_home()
    return {
        "claude": home / ".claude" / "skills",
        "antigravity": home / ".agents" / "skills",
        "cursor": home / ".cursor" / "skills-cursor",
    }


def global_read_roots(tool: str) -> list[Path]:
    """Every global folder a tool loads skills from. Cursor (verified in its docs, 2026-10-16) reads
    ~/.cursor/skills, ~/.agents/skills and ~/.claude/skills; skills-cursor holds its built-ins and
    the copies install.ps1 put there."""
    home = projects.user_home()
    return {
        "claude": [home / ".claude" / "skills"],
        "antigravity": [home / ".agents" / "skills"],
        "cursor": [home / ".cursor" / "skills", home / ".cursor" / "skills-cursor",
                   home / ".agents" / "skills", home / ".claude" / "skills"],
    }[tool]


def project_read_dirs(tool: str) -> list[str]:
    """Project folders a tool loads skills from (Cursor also reads the Claude and Agents folders)."""
    return {
        "claude": [".claude/skills"],
        "antigravity": [".agents/skills"],
        "cursor": [".cursor/skills", ".agents/skills", ".claude/skills"],
    }[tool]


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


def files_hash(files: dict[str, bytes]) -> str:
    """SHA-256 over (relative posix path, NUL, LF-normalised bytes, NUL), ordered by path."""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(normalized(files[rel]))
        h.update(b"\0")
    return h.hexdigest()


def content_hash(skill_dir: Path) -> str:
    """``files_hash`` of every file in a skill folder except the manifest. install.ps1 computes the
    identical hash."""
    return files_hash({p.relative_to(skill_dir).as_posix(): p.read_bytes()
                       for p in skill_dir.rglob("*") if p.is_file() and p.name != MANIFEST})


def load_ledger(path: Path | None = None) -> dict[str, dict[str, list[str]]]:
    """``skills/RELEASES.json``: {version: {skill: [content hashes shipped under that version]}}."""
    try:
        return json.loads((path or LEDGER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def ledger_version(skill: str, digest: str, ledger: dict | None = None) -> str | None:
    """The newest released version whose ``skill`` had exactly this content, else None."""
    ledger = load_ledger() if ledger is None else ledger
    hits = [v for v, skills in ledger.items() if digest in skills.get(skill, [])]
    return max(hits, key=_vtuple) if hits else None


def record_release(skills_root: Path, version: str, path: Path | None = None) -> dict[str, str]:
    """Dev command: record every shipped skill's current hash under ``version``. Refuses (ValueError,
    nothing written) to add a new hash to a version that already lists one for that skill — a changed
    skill needs a VERSION bump. Returns {skill: "recorded" | "already recorded"}."""
    path = path or LEDGER
    ledger = load_ledger(path)
    entry = ledger.setdefault(version, {})
    out: dict[str, str] = {}
    stale = []
    for d in sorted(p for p in skills_root.iterdir() if (p / "SKILL.md").is_file()):
        digest = content_hash(d)
        known = entry.get(d.name, [])
        if digest in known:
            out[d.name] = "already recorded"
        elif known:
            stale.append(d.name)
        else:
            out[d.name] = "recorded"
            entry[d.name] = [digest]
    if stale:
        raise ValueError(f"{', '.join(stale)} changed but {version} is already recorded with another hash: bump VERSION")
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


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
        # utf-8-sig: Windows PowerShell 5.1 writes a BOM that json.loads rejects
        return json.loads((skill_dir / MANIFEST).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def skill_state(installed: Path, source: Path) -> dict[str, Any]:
    """State of one installed skill vs the shipped one.

    missing  - not installed
    current  - same content as shipped (line endings ignored)
    outdated - ours and provably unedited (the install manifest's hash still matches, or the content
               is exactly a released version in the ledger), differs from shipped
               -> safe to upgrade on approval
    edited   - ours, but changed since install OR not provably unedited (no recorded hash)
               -> ask: keep / replace / save a .new copy
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
    released = ledger_version(source.name, here)
    if released:
        # exactly what some release shipped (pre-metadata copies included): ours, never edited
        return {"state": "outdated", "installed_version": released, "available_version": src_version}
    ours = manifest.get("source") == SOURCE or meta.get("source") == SOURCE
    if not ours:
        return {"state": "foreign", **base}
    # Only a recorded hash that still matches proves the user never touched it.
    if manifest.get("sha256") and manifest["sha256"] == here:
        return {"state": "outdated", **base}
    return {"state": "edited", **base}


NEW_DIR = ".pm-qa-cockpit-new"


def new_copy_dir(dest: Path) -> Path:
    """Where `--on-conflict new` puts the shipped version: beside the skills root, in a dot-folder two
    levels deep so agents never load it as a second skill with the same name. An existing copy (maybe
    half-merged by the user) is never overwritten: a dated sibling is used instead."""
    base = dest.parent / NEW_DIR / dest.name
    if not base.exists():
        return base
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = base.with_name(f"{dest.name}-{stamp}")
    n = 1
    while candidate.exists():
        n += 1
        candidate = base.with_name(f"{dest.name}-{stamp}-{n}")
    return candidate


def install_skill(source: Path, dest: Path, on_conflict: str = "abort") -> str:
    """Copy a shipped skill into ``dest`` and write its manifest. Returns what happened.

    on_conflict applies to 'edited': abort (default, keep the user's) | replace | new (shipped copy
    written to ``new_copy_dir``). 'foreign' can never be replaced.
    """
    state = skill_state(dest, source)["state"]
    if state == "current":
        # content is identical to shipped, so (re)recording its hash can't launder an edit
        if read_manifest(dest).get("sha256") != content_hash(dest):
            _write_manifest(dest, source)
        return "current"
    if state == "foreign":
        return "skipped-foreign"
    if state == "edited" and on_conflict == "abort":
        return "skipped-edited"
    if state == "edited" and on_conflict == "new":
        target = new_copy_dir(dest)
        shutil.copytree(source, target)
        return f"wrote-new:{target}"
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


TOOL_LABELS = {"claude": "Claude Code", "cursor": "Cursor", "antigravity": "Antigravity"}
TOOL_ORDER = ("claude", "antigravity", "cursor")  # Cursor last: it also reads the other two folders
GENERIC_SKILLS = ("grill-me", "grilling")


def generic_global_target(tool: str) -> Path:
    """Where a generic skill is copied for one tool (the tool's own global folder)."""
    home = projects.user_home()
    return {"claude": home / ".claude" / "skills", "antigravity": home / ".agents" / "skills",
            "cursor": home / ".cursor" / "skills"}[tool]


def generic_reach(code: Path, tool: str) -> dict[str, list[str]]:
    """Where a tool can load grill-me / grilling from: its global folders and the project folders it reads."""
    return {
        "global": [str(r / n) for r in global_read_roots(tool) for n in GENERIC_SKILLS if (r / n / "SKILL.md").is_file()],
        "project": [f"{rel}/{n}" for rel in project_read_dirs(tool) for n in GENERIC_SKILLS
                    if (code / rel / n / "SKILL.md").is_file()],
    }


def bootstrap_reach(code: Path, tool: str) -> list[Path]:
    """Every pm-qa-setup copy a tool can load (global first, then the project's folders)."""
    found = [r / BOOTSTRAP_SKILL for r in global_read_roots(tool)]
    found += [code / rel / BOOTSTRAP_SKILL for rel in project_read_dirs(tool)]
    return [p for p in found if (p / "SKILL.md").is_file()]


def machine_tools() -> list[str]:
    home = projects.user_home()
    return [t for t, d in (("claude", ".claude"), ("antigravity", ".agents"), ("cursor", ".cursor")) if (home / d).is_dir()]


def _home_short(p: str | Path) -> str:
    """A path shown to the user with the home folder as ``~``."""
    s, h = str(p).replace("\\", "/"), str(projects.user_home()).replace("\\", "/")
    return "~" + s[len(h):] if s.lower().startswith(h.lower()) else s


def _strip_metadata(b: bytes) -> bytes:
    return re.sub(rb"\nmetadata:\n(?:[ \t]+[^\n]*\n)+", b"\n", normalized(b))


def global_copy_status(d: Path, name: str) -> str | None:
    """Classify a global copy of one of the project skill names (cleanup is a deletion, so be strict).

    'ours'        - provably ours and unedited -> may be offered for deletion
    'ours-edited' - ours but changed by the user -> never offered; reported for review
    'maybe'       - same name and mentions the cockpit, but not provably ours -> reported only
    None          - absent or not ours
    """
    md = d / "SKILL.md"
    if not md.is_file():
        return None
    if ledger_version(name, content_hash(d)):
        return "ours"  # exactly a released version (any release, pre-metadata copies included)
    files = [p for p in d.rglob("*") if p.is_file() and p.name != MANIFEST]
    shipped = TOOL_ROOT / "skills" / name / "SKILL.md"
    same_as_shipped = (
        len(files) == 1 and shipped.is_file() and _strip_metadata(md.read_bytes()) == _strip_metadata(shipped.read_bytes())
    )
    manifest = read_manifest(d)
    if manifest.get("source") == SOURCE or skill_metadata(md).get("source") == SOURCE:
        if manifest.get("sha256"):
            return "ours" if manifest["sha256"] == content_hash(d) else "ours-edited"
        return "ours" if same_as_shipped else "ours-edited"
    if same_as_shipped:
        return "ours"  # a pre-versioning copy, byte-for-byte the shipped skill apart from metadata
    text = md.read_text(encoding="utf-8", errors="replace")
    if re.search(rf"^name:\s*{re.escape(name)}\s*$", text, re.MULTILINE) and "cockpit" in text.lower():
        return "maybe"
    return None


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _git(code: Path, *args: str) -> subprocess.CompletedProcess | None:
    if not _which("git"):
        return None
    return subprocess.run(["git", *args], cwd=str(code), capture_output=True, text=True, check=False)


def _is_ignored(code: Path, rel_dir: str) -> bool:
    # Probe a file inside the folder: a `dir/` pattern can't match a folder that doesn't exist yet.
    res = _git(code, "check-ignore", "-q", f"{rel_dir.rstrip('/')}/SKILL.md")
    return bool(res and res.returncode == 0)


def exclude_file(code: Path) -> Path | None:
    """The repo's info/exclude, also when .git is a file (worktree / submodule)."""
    res = _git(code, "rev-parse", "--git-path", "info/exclude")
    if not res or res.returncode != 0 or not res.stdout.strip():
        return None
    p = Path(res.stdout.strip())
    return p if p.is_absolute() else (code / p).resolve()


def projects_relying_on_globals() -> list[str]:
    """Registered full-mode projects with no local cockpit skills: removing the global copies would
    leave them without the skills until setup is run there."""
    out = []
    for proj in projects.load_registry()["projects"].values():
        code = Path(proj.get("code_path", ""))
        if not code.is_dir() or own_loop_skills(code):
            continue
        local = any((code / rel / "pm-orchestrate" / "SKILL.md").is_file() for rel in AGENT_SKILL_DIRS.values())
        if not local:
            out.append(proj["slug"])
    return sorted(out)


def pointer_text(project: dict[str, Any], register_only: bool) -> str:
    docs = project["docs_path"]
    lines = [
        POINTER_START,
        "## PM & QA Cockpit",
        "",
        f"Stories are tracked with PM & QA Cockpit (card prefix `{project['prefix']}`, docs in `{docs}`).",
        f"Dashboard: `powershell -File ~/.pm-qa-cockpit/app/dashboard.ps1 -Project {project['slug']}`"
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


def read_roster(code: Path, project: dict[str, Any], tools: list[str]) -> dict[str, Any]:
    """Read-only: each used tool's agent files mapped to roles, plus the lead row from ORCHESTRATION.md."""
    orch = Path(project["docs_path"]) / "ORCHESTRATION.md"
    try:
        text = orch.read_text(encoding="utf-8", errors="replace") if orch.is_file() else ""
    except OSError:
        text = ""
    starts, ends = text.count(roster.ROSTER_START), text.count(roster.ROSTER_END)
    markers = "none" if not (starts or ends) else (
        "ok" if starts == 1 and ends == 1 and text.find(roster.ROSTER_START) < text.find(roster.ROSTER_END) else "damaged")
    block = text[text.find(roster.ROSTER_START):text.find(roster.ROSTER_END)] if markers == "ok" else ""
    return {"orchestration": str(orch), "orchestration_exists": orch.is_file(),
            "readable": _readable_utf8(orch), "markers": markers,
            "has_section": "| Role | Agent |" in block,
            "lead": roster.parse_lead(text, TOOL_LABELS),
            "tools": {t: roster.detect_tool(code, t) for t in tools}}


def build_plan(code_path: str | Path, *, name: str | None = None, prefix: str | None = None,
               docs_path: str | Path | None = None, stealth: bool | None = None,
               force_full: bool = False) -> dict[str, Any]:
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
    # A project's own loop wins (register-only) unless the user explicitly asks for the full install.
    register_only = bool(loop) and not force_full
    exclude_skills = project["docs_mode"] == "external" or stealth
    notes: list[str] = []
    if loop and force_full:
        notes.append("Full install forced although the project has its own loop skills: " + ", ".join(loop))
    # Skill folders to install into: Claude Code and Antigravity read their own; Cursor reads
    # .claude/skills (through AGENTS.md), so a Cursor-only project still gets .claude/skills.
    skill_dirs = sorted({AGENT_SKILL_DIRS[a] for a in agents if a in AGENT_SKILL_DIRS}
                        | ({AGENT_SKILL_DIRS["claude"]} if "cursor" in agents else set()))

    actions: list[dict[str, Any]] = []

    def add(kind: str, title: str, **kw: Any) -> None:
        actions.append({"id": f"A{len(actions) + 1}", "kind": kind, "title": title, **kw})

    # 1. Prerequisites (machine level). grill-me must be reachable for every tool this project uses
    #    (R2-Q5); a generic skill found only in a project or another tool's folder is copied to the
    #    tool's global folder, never moved (R1-Q4).
    tools = [t for t in TOOL_ORDER if t in agents] or ["claude"]
    reach = {t: generic_reach(code, t) for t in tools}
    prereq = {
        "uv": _which("uv"),
        "git": _which("git"),
        "running_copy": (projects.cockpit_home() / "app" / "cockpit" / "server.py").is_file(),
        "grill_me": all(reach[t]["global"] or reach[t]["project"] for t in tools),
        "grill_me_global": {t: bool(reach[t]["global"]) for t in tools},
    }
    if not prereq["running_copy"]:
        add("clone-running-copy", "Clone the cockpit's running copy",
            writes=[str(projects.cockpit_home() / "app")],
            detail="git clone https://github.com/getuliojr/pm-qa-cockpit ~/.pm-qa-cockpit/app")
    sources: dict[str, Path] = {}  # generic skill name -> a folder to copy it from
    for rel in sorted({d for t in tools for d in project_read_dirs(t)}):
        for n in GENERIC_SKILLS:
            if (code / rel / n / "SKILL.md").is_file():
                sources.setdefault(n, code / rel / n)
    for r in [*global_roots().values(), *(generic_global_target(t) for t in TOOL_ORDER)]:
        for n in GENERIC_SKILLS:
            if (r / n / "SKILL.md").is_file():
                sources.setdefault(n, r / n)
    planned: set[Path] = set()  # global folders that will hold a generic skill after the copies
    fetch_for: list[str] = []
    for t in tools:
        if reach[t]["global"] or any(r in planned for r in global_read_roots(t)):
            continue
        target = generic_global_target(t)
        copies = [{"skill": n, "from": str(src), "to": str(target / n)} for n, src in sorted(sources.items())]
        if copies:
            planned.add(target)
            add("copy-generic", f"Copy {' and '.join(c['skill'] for c in copies)} to the {TOOL_LABELS[t]} global folder",
                tool=t, copies=copies, writes=[c["to"] for c in copies],
                detail="copy only: " + "; ".join(f"{c['from']} -> {c['to']}" for c in copies))
        else:
            fetch_for.append(t)
    if fetch_for:
        add("fetch-grill-me", "Install grill-me once for this machine (design-before-code skill)",
            tools=fetch_for, writes=[str(generic_global_target(t) / "grill-me") for t in fetch_for],
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
            writes=[str(projects.registry_path())],
            detail=f"cli.py --project {project['slug']} profile-gates --measure --apply")

    # 3. Skills per detected agent (none when the project runs its own loop)
    skills_status: list[dict[str, Any]] = []
    dir_agents = {rel: [a for a in agents if AGENT_SKILL_DIRS.get(a) == rel or (a == "cursor" and rel == AGENT_SKILL_DIRS["claude"])]
                  for rel in skill_dirs}
    for rel in skill_dirs:
        label = "+".join(dir_agents[rel])
        for skill in PROJECT_SKILLS:
            dest = code / rel / skill
            st = skill_state(dest, TOOL_ROOT / "skills" / skill)
            skills_status.append({"agent": label, "skill": skill, "path": f"{rel}/{skill}", **st})
            if register_only or st["state"] in ("current", "foreign"):
                if st["state"] == "foreign":
                    notes.append(f"{rel}/{skill} is someone else's skill with the same name — never touched.")
                continue
            verb = {"missing": "Install", "outdated": "Upgrade", "edited": "Resolve local edits in"}[st["state"]]
            add("install-skill", f"{verb} {skill} for {label} ({rel}/{skill})", agent=label, dir=rel, skill=skill,
                state=st["state"],
                writes=[str(dest)] + ([str(code / rel / NEW_DIR)] if st["state"] == "edited" else []),
                detail=(f"edited: keep (default) / replace (removes the whole folder, including files you added)"
                        f" / new (shipped copy saved under {rel}/{NEW_DIR}/; compare with {TOOL_ROOT / 'skills' / skill})")
                if st["state"] == "edited" else f"{st['installed_version'] or '-'} -> {st['available_version']}")

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

    # 4b. Roster (Story 2.2): who does each step. Read for every project; written only in full mode,
    #     only for missing roles, never over an existing agent file.
    roster_state = read_roster(code, project, tools)
    roster_files: list[str] = []
    table_only = (roster_state["orchestration_exists"] and roster_state["readable"]
                  and roster_state["markers"] != "damaged" and not roster_state["has_section"])
    if not register_only and (table_only or any(r["missing"] for r in roster_state["tools"].values())):
        files = [{"tool": t, "role": role, "path": f"{roster.WRITE_DIR[t]}/{roster.PREFIX}{role}.md", **roster.recommended(t, role)}
                 for t in tools for role in roster_state["tools"][t]["missing"]]
        lead = {t: roster_state["lead"].get(t) or roster.recommended(t, "lead") for t in tools}
        roster_files = [f["path"] for f in files]
        add("roster", "Add agent files for the missing roles and record the roster", files=files, lead=lead,
            writes=[str(code / f) for f in roster_files] + [roster_state["orchestration"]],
            detail="new files only, each pinned by full model ID; an existing agent file is never edited")

    # 5. Keep installed skills out of git when docs are external / stealth (exact paths only)
    if exclude_skills and not register_only:
        paths = sorted([f"{rel}/{s}/" for rel in skill_dirs for s in PROJECT_SKILLS]
                       + [f"{rel}/{NEW_DIR}/" for rel in skill_dirs] + roster_files)
        missing = [p for p in paths if not _is_ignored(code, p)]
        exclude = exclude_file(code)
        if missing and exclude:
            add("exclude", "Keep the installed cockpit skills out of git (info/exclude)",
                paths=missing, writes=[str(exclude)])
        elif missing:
            notes.append("Not a git repository (or git missing): installed skills can't be excluded from git.")

    # 6. Legacy global copies of the project skills (installed before Story 2). Only provably ours and
    #    unedited copies are offered for deletion; anything else is listed for the user to review.
    legacy: list[str] = []
    for r in global_roots().values():
        for s in PROJECT_SKILLS:
            status = global_copy_status(r / s, s)
            if status == "ours":
                legacy.append(str(r / s))
            elif status in ("ours-edited", "maybe"):
                notes.append(f"Global {r / s} looks like a cockpit skill but is {'edited' if status == 'ours-edited' else 'not provably ours'}"
                             " — left for you to review, never removed automatically.")
    if legacy:
        relying = projects_relying_on_globals()
        here_needs = not register_only and any(
            a["kind"] == "install-skill" and a["state"] == "missing" for a in actions
        )
        if here_needs and project.get("slug") not in relying:
            relying.append(f"{project['slug']} (this project, unless you also approve its skill installs)")
        add("cleanup-global", "Remove the old global copies of the project skills (now installed per project)",
            paths=legacy, writes=legacy,
            detail=("Still relying on them (set those up first): " + ", ".join(relying)) if relying
            else "No registered full-mode project still relies on them.")

    # 7. The global bootstrap skill itself (upgrade only where it is installed; never create here)
    for agent, root in global_roots().items():
        st = skill_state(root / BOOTSTRAP_SKILL, TOOL_ROOT / "skills" / BOOTSTRAP_SKILL)
        if st["state"] in ("outdated", "edited"):
            add("upgrade-bootstrap", f"{'Upgrade' if st['state'] == 'outdated' else 'Resolve local edits in'} the global {BOOTSTRAP_SKILL} for {agent}",
                agent=agent, skill=BOOTSTRAP_SKILL, state=st["state"], writes=[str(root / BOOTSTRAP_SKILL)],
                detail=f"{st['installed_version'] or '-'} -> {st['available_version']}")
    # ... and installed for a used tool that can't reach any copy (Cursor also reads ~/.claude/skills)
    for t in tools:
        if not bootstrap_reach(code, t) and not any(
            a["kind"] == "install-bootstrap" and global_roots()[a["agent"]] in global_read_roots(t) for a in actions
        ):
            dest = global_roots()[t] / BOOTSTRAP_SKILL
            add("install-bootstrap", f"Install {BOOTSTRAP_SKILL} in the {TOOL_LABELS[t]} global folder",
                agent=t, skill=BOOTSTRAP_SKILL, state="missing", writes=[str(dest)],
                detail=f"- -> {tool_version()}")

    # 8. Launcher wiring (R2-Q6): a launcher that prefers the dev clone gets the exact line change.
    launchers = wiring.scan_launchers(code)
    for lw in launchers:
        if lw["edit"]:
            e = lw["edit"]
            add("launcher-edit", f"Point {lw['file']} at the running copy (line {e['line']})", file=lw["file"],
                edit=e, writes=[str(code / lw["file"])], preview=f"- {e['old'].strip()}\n+ {e['new'].strip()}")
    hooks = wiring.scan_hooks(code)

    plan = {
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
        "notes": notes,
        "actions": actions,
        "tools": tools,
        "launchers": launchers,
        "hooks": hooks,
        "roster": roster_state,
    }
    for t in machine_tools():
        if t not in tools:
            notes.append(f"{TOOL_LABELS[t]} is installed on this machine but this project doesn't use it: nothing is planned for it.")
    plan["inventory"] = build_inventory(plan, code, reach, sources)
    plan["roster_table"] = build_roster_table(plan)
    plan["decisions"] = build_decisions(plan)
    plan["target"] = build_target(plan)
    return plan


# ---------------------------------------------------------------------------
# Inventory, target state and decisions (Story 2.1): the first reply, in plain words
# ---------------------------------------------------------------------------

THEMES = (
    ("pm-data", "Where the PM data lives"),
    ("skills", "Where skills live"),
    ("wiring", "Launcher wiring"),
    ("gates", "Gates"),
    ("roster", "Who does each step"),
)
THEME_OF = {
    "register": "pm-data", "pointer": "pm-data",
    "clone-running-copy": "skills", "fetch-grill-me": "skills", "copy-generic": "skills",
    "install-skill": "skills", "exclude": "skills", "cleanup-global": "skills",
    "upgrade-bootstrap": "skills", "install-bootstrap": "skills",
    "launcher-edit": "wiring", "profile-gates": "gates", "roster": "roster",
}


def _item(what: str, now: str, belongs: str, why: str, status: str = "ok", actions: list[str] | None = None) -> dict[str, Any]:
    """status: ok (stays as is) | change (an approval would change it) | review (yours to look at) | info"""
    return {"what": what, "now": now, "belongs": belongs, "why": why, "status": status, "actions": actions or []}


def _ids(plan: dict[str, Any], kind: str, **match: Any) -> list[str]:
    return [a["id"] for a in plan["actions"] if a["kind"] == kind and all(a.get(k) == v for k, v in match.items())]


def build_inventory(plan: dict[str, Any], code: Path, reach: dict[str, dict[str, list[str]]],
                    sources: dict[str, Path]) -> list[dict[str, Any]]:
    """Groups: this project / each used tool's global folder / launchers / PM data / hooks."""
    project, full = plan["project"], plan["mode"] == "full"
    groups: list[dict[str, Any]] = []

    # -- this project
    items = []
    for s in plan["own_loop_skills"]:
        items.append(_item(f"Your own loop skill {Path(s).name}", s, f"stays in {s}",
                           "this project already runs its own story loop, and its rules win"))
    for rel in sorted({d for t in plan["tools"] for d in project_read_dirs(t)}):
        for n in GENERIC_SKILLS:
            if (code / rel / n / "SKILL.md").is_file():
                items.append(_item(f"{n} (generic design skill)", f"{rel}/{n}", f"stays in {rel}/{n}; also in each tool's global folder",
                                   "the project copy is yours (committed, teammates use it); a global copy makes it work in every project"))
    by_dir: dict[str, list[dict[str, Any]]] = {}
    for row in plan["skills"]:
        by_dir.setdefault(row["path"].rsplit("/", 1)[0], []).append(row)
    for rel, rows in sorted(by_dir.items()):
        states = {r["state"] for r in rows}
        if not full:
            installed = [r["skill"] for r in rows if r["state"] != "missing"]
            if installed:
                items.append(_item("Cockpit skills", f"{rel}: {', '.join(installed)}", "not needed here",
                                   "register-only: the project's own loop wins; they were left as they are", "review"))
            continue
        ids = [i for r in rows for i in _ids(plan, "install-skill", dir=rel, skill=r["skill"])]
        now = "; ".join(f"{r['skill']} {r['state']}" + (f" ({r['installed_version']})" if r["installed_version"] else "") for r in rows)
        status = "ok" if states <= {"current"} else ("review" if states & {"edited", "foreign"} else "change")
        items.append(_item(f"Cockpit skills for {rows[0]['agent']}", now, f"{rel}, current version {plan['tool_version']}",
                           "full mode: the cockpit loop runs from the project's own skill folder", status, ids))
    existing = [f for f, ok in plan["inspection"]["orchestration_files"].items() if ok]
    for f in existing:
        ids = _ids(plan, "pointer", file=f)
        items.append(_item(f"Your rules file {f}", f, f"stays; {'a short cockpit section is appended' if ids else 'unchanged'}",
                           "your rules are yours: text is only ever appended between markers, never rewritten",
                           "change" if ids else "ok", ids))
    for a in plan["actions"]:
        if a["kind"] == "pointer" and a["file"] not in existing:
            items.append(_item(f"{a['file']} (new)", "missing", f"created with a short cockpit section",
                               f"{a['file']} is where this project's tools read their rules", "change", [a["id"]]))
    groups.append({"group": "This project", "items": items})

    # -- global folders, one group per used tool (plus any folder with something to clean up)
    tool_groups: dict[str, list[dict[str, Any]]] = {t: [] for t in plan["tools"]}
    for t in plan["tools"]:
        r = reach[t]
        ids = _ids(plan, "copy-generic", tool=t) + [a["id"] for a in plan["actions"]
                                                   if a["kind"] == "fetch-grill-me" and t in a.get("tools", [])]
        if r["global"]:
            tool_groups[t].append(_item("grill-me (design before code)", ", ".join(_home_short(p) for p in r["global"]),
                                        "here", "a generic skill: global, so every project has it"))
        else:
            copied = [a for a in plan["actions"] if a["id"] in ids and a["kind"] == "copy-generic"]
            def shown(p: str) -> str:
                return Path(p).relative_to(code).as_posix() if Path(p).is_relative_to(code) else _home_short(p)

            src = f"; found in {', '.join(shown(c['from']) for c in copied[0]['copies'])}" if copied else ""
            covered = not ids and any(r2 in {Path(c["to"]).parent for a in plan["actions"] if a["kind"] == "copy-generic"
                                             for c in a["copies"]} for r2 in global_read_roots(t))
            tool_groups[t].append(_item(
                "grill-me (design before code)",
                f"missing for {TOOL_LABELS[t]}" + (" (only in this project)" if r["project"] else "") + src,
                f"{_home_short(generic_global_target(t))}" + (" (covered by another tool's folder)" if covered else "")
                + ("; the project copy stays" if r["project"] else ""),
                "a generic skill: global, so every project has it", "change" if ids or covered else "review", ids))
        boot = bootstrap_reach(code, t)
        boot_ids = [a["id"] for a in plan["actions"] if a["kind"] in ("upgrade-bootstrap", "install-bootstrap")
                    and global_roots()[a["agent"]] in global_read_roots(t)]
        if boot:
            st = skill_state(boot[0], TOOL_ROOT / "skills" / BOOTSTRAP_SKILL)
            tool_groups[t].append(_item(f"{BOOTSTRAP_SKILL} (this setup)", f"{_home_short(boot[0])} ({st['state']}, {st['installed_version'] or '-'})",
                                        f"current version {plan['tool_version']}", "the one global cockpit skill; it runs setup and upgrades",
                                        "ok" if st["state"] == "current" else ("review" if st["state"] in ("edited", "foreign") else "change"),
                                        boot_ids))
        else:
            tool_groups[t].append(_item(f"{BOOTSTRAP_SKILL} (this setup)", f"missing for {TOOL_LABELS[t]}",
                                        _home_short(global_roots()[t]), "the one global cockpit skill; it runs setup and upgrades",
                                        "change", boot_ids))
    cleanup = next((a for a in plan["actions"] if a["kind"] == "cleanup-global"), None)
    for t, root in global_roots().items():
        for s in PROJECT_SKILLS:
            status = global_copy_status(root / s, s)
            if not status:
                continue
            rel = ledger_version(s, content_hash(root / s))
            if status == "ours":
                it = _item(f"Old cockpit skill {s}", _home_short(root / s) + (f" (release {rel}, unedited)" if rel else " (unedited)"),
                           "removed: cockpit skills now live per project", "an old cockpit version, safe to remove",
                           "change", [cleanup["id"]] if cleanup else [])
                if cleanup and "Still relying" in cleanup.get("detail", ""):
                    it["why"] += f" once the projects using it are set up ({cleanup['detail'].split(': ', 1)[1]})"
            else:
                it = _item(f"Cockpit-like skill {s}", _home_short(root / s), "yours to review",
                           "edited by you" if status == "ours-edited" else "not provably a cockpit copy", "review")
            tool_groups.setdefault(t, []).append(it)
    for t, items in tool_groups.items():
        if items:
            groups.append({"group": f"Global folder: {TOOL_LABELS[t]}" + ("" if t in plan["tools"] else " (not used by this project)"),
                           "items": items})

    # -- roster (Story 2.2)
    items = []
    action = next((a for a in plan["actions"] if a["kind"] == "roster"), None)
    for t in plan["tools"]:
        rs = plan["roster"]["tools"][t]

        def say_role(r: str) -> str:
            have = rs["roles"][r]
            if not have:
                return f"{r}: possibly {', '.join(rs['possible'][r])}" if r in rs["possible"] else f"{r}: missing"
            return f"{r}: {have['model'] or 'no model'}" + ("" if have["ours"] else " (yours)")

        now = "; ".join(say_role(r) for r in roster.FILE_ROLES)
        if rs["possible"]:
            now += " (a file named for several roles is never duplicated)"
        if rs["extra"]:
            now += f"; other agents left alone: {', '.join(rs['extra'])}"
        if not rs["missing"]:
            status, belongs = "ok", "stays as it is"
            if full and action and not action["files"]:
                status, belongs = "change", "the roster table is recorded in ORCHESTRATION.md"
        elif not full:
            status, belongs = "review", "stays as your project has it (reported only: its own loop decides)"
        else:
            status, belongs = "change", f"the missing roles ({', '.join(rs['missing'])}) get a cockpit agent; yours stay"
        items.append(_item(f"Agents for {TOOL_LABELS[t]}", now, belongs,
                           "each loop step is done by an agent pinned to a full model ID",
                           status, [action["id"]] if action and status == "change" else []))
        lead = plan["roster"]["lead"].get(t)
        rec = roster.recommended(t, "lead")
        items.append(_item(f"Lead for {TOOL_LABELS[t]}", f"{lead['model']} ({lead['effort']})" if lead else "not recorded",
                           f"recommended: {rec['model']}, effort {rec['effort']} (you pick it per session; setup records it)",
                           "the lead is your session's model, so it has no agent file", "ok" if lead or not full else "info"))
    rs0 = plan["roster"]
    if rs0["orchestration_exists"] and (rs0["markers"] == "damaged" or not rs0["readable"]):
        items.append(_item("Roster table in ORCHESTRATION.md", "its roster markers are damaged" if rs0["readable"] else "the file is not UTF-8",
                           "yours to fix: exactly one start and one end marker, in order", "setup never writes a damaged file", "review"))
    groups.append({"group": "Roster", "items": items})

    # -- launchers
    items = []
    for lw in plan["launchers"]:
        if lw["kind"] == "orchestration" and lw["verdict"] == "ok":
            continue  # prose that only mentions an old copy: history, not a launch path
        ids = _ids(plan, "launcher-edit", file=lw["file"])
        first = lw["refs"][0]
        verdicts = {
            "ok": ("calls the running copy first" + (" (falls back to others when it is missing)" if len(lw["refs"]) > 1 else ""), "ok"),
            "missing": ("calls the running copy, which isn't installed", "review"),
            "dev-clone-first": ("calls the dev clone first", "change" if ids else "review"),
            "frozen": ("calls a frozen in-repo copy", "review"),
            "dev-clone-mentioned": ("mentions a dev-clone path", "review"),
        }
        how, status = verdicts[lw["verdict"]]
        now = f"{lw['file']}" + (f" line {first['line']}" if first.get("line") else "") + f": {how}"
        fix = "" if status == "ok" or ids else " (no exact automatic edit: change it by hand to $HOME/.pm-qa-cockpit/app)"
        items.append(_item(f"Launcher {lw['file']}", now, "calls ~/.pm-qa-cockpit/app first",
                           "the running copy only changes on release, so a half-finished edit in the dev clone can't break it" + fix,
                           status, ids))
    if items:
        groups.append({"group": "Launchers", "items": items})

    # -- PM data
    docs = Path(project["docs_path"])
    reg_ids = _ids(plan, "register")
    gates = (project.get("gates") or {}).get("mode", "unprofiled")
    items = [
        _item("Registration", "registered" if plan["registered"] else "not registered",
              f"registered as '{project['slug']}' (prefix {project['prefix']})",
              "the dashboard and the CLI find a project through the registry", "ok" if plan["registered"] else "change", reg_ids),
        _item("PM data folder", f"{docs.as_posix()} ({project['docs_mode']})" + ("" if docs.is_dir() else ", not created yet"),
              "stays there" if docs.is_dir() else f"{docs.as_posix()}",
              "in-repo docs are committed with the code; external docs keep PM files out of the code repo",
              "ok" if docs.is_dir() else "change", reg_ids),
        _item("Gate mode", gates, "fast or heavy, measured once",
              "a suite under 6 minutes runs inline before every close; a longer one is split out",
              "ok" if gates not in ("unprofiled", None) else "change", _ids(plan, "profile-gates")),
    ]
    groups.append({"group": "PM data", "items": items})

    # -- hooks
    items = []
    for h in plan["hooks"]:
        if not h["cockpit_refs"] and not h["problem"]:
            continue
        items.append(_item(f"{TOOL_LABELS.get(h['tool'], h['tool'])} hook ({h['event']})", f"{h['file']}: {h['command'][:80]}",
                           "stays, never touched", {None: "calls the cockpit correctly", "dev-clone": "calls the dev clone: point it at ~/.pm-qa-cockpit/app",
                                                   "missing-path": "calls a cockpit path that doesn't exist"}[h["problem"]],
                           "ok" if not h["problem"] else "review"))
    if plan["hooks"] and not items:
        items.append(_item("Your hooks", f"{len(plan['hooks'])} hook command(s), none calls the cockpit", "stay, never touched",
                           "the cockpit ships no hooks; it only checks the paths yours call"))
    if items:
        groups.append({"group": "Hooks", "items": items})
    return groups


def _say(a: dict[str, Any], plan: dict[str, Any]) -> str:
    """One action in plain words (no ids)."""
    p = plan["project"]
    k = a["kind"]
    if k == "register":
        where = "inside the repo" if p["docs_mode"] == "in-repo" else "outside the code repo"
        return f"register {p['name']} (card prefix {p['prefix']}) with its PM data in {p['docs_path']} ({where})"
    if k == "pointer":
        return f"append a short PM & QA Cockpit section to {a['file']} (your text stays as it is)"
    if k == "clone-running-copy":
        return "download the cockpit's running copy to ~/.pm-qa-cockpit/app"
    if k == "fetch-grill-me":
        return f"install grill-me from {GRILL_SOURCE} for {', '.join(TOOL_LABELS[t] for t in a['tools'])}"
    if k == "copy-generic":
        return (f"copy {' and '.join(c['skill'] for c in a['copies'])} to the {TOOL_LABELS[a['tool']]} global folder "
                f"({_home_short(Path(a['copies'][0]['to']).parent)}); the copy it comes from stays")
    if k == "install-skill":
        return {"missing": f"install {a['skill']} into {a['dir']}",
                "outdated": f"upgrade {a['skill']} in {a['dir']} ({a['detail']})"}.get(a["state"], f"resolve {a['skill']}")
    if k == "exclude":
        return "keep the installed cockpit skill folders out of git (.git/info/exclude, exact paths)"
    if k == "cleanup-global":
        return f"remove {len(a['paths'])} old, unedited cockpit skill copies from the global folders"
    if k == "upgrade-bootstrap":
        return f"upgrade {BOOTSTRAP_SKILL} in the {TOOL_LABELS[a['agent']]} global folder ({a['detail']})"
    if k == "install-bootstrap":
        return f"install {BOOTSTRAP_SKILL} in the {TOOL_LABELS[a['agent']]} global folder"
    if k == "launcher-edit":
        return f"change {a['file']} line {a['edit']['line']} to call the running copy"
    if k == "roster":
        roles = sorted({f"{TOOL_LABELS[f['tool']]} {f['role']}" for f in a["files"]})
        if not roles:
            return "record the roster (who does each step) in ORCHESTRATION.md"
        return f"add cockpit agents for the missing roles ({_join(roles)}) and record the roster in ORCHESTRATION.md"
    if k == "profile-gates":
        return "time the test suite once (it runs the whole suite) to choose fast or heavy gates"
    return a["title"]


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _merge_skill_installs(acts: list[dict[str, Any]], plan: dict[str, Any]) -> list[str]:
    """Several per-skill installs into one folder read as one sentence."""
    out, grouped, boot = [], {}, {}
    for a in acts:
        if a["kind"] == "install-skill":
            grouped.setdefault((a["dir"], a["state"]), []).append(a["skill"])
        elif a["kind"] in ("upgrade-bootstrap", "install-bootstrap"):
            boot.setdefault((a["kind"], a["detail"]), []).append(TOOL_LABELS[a["agent"]])
        else:
            out.append(_say(a, plan))
    for (rel, state), skills in grouped.items():
        verb = "install" if state == "missing" else "upgrade"
        out.insert(0, f"{verb} the cockpit skills {_join(skills)} in {rel}")
    for (kind, detail), tools in boot.items():
        verb = "upgrade" if kind == "upgrade-bootstrap" else "install"
        where = "folder" if len(tools) == 1 else "folders"
        out.append(f"{verb} {BOOTSTRAP_SKILL} in the {_join(tools)} global {where}"
                   + (f" ({detail})" if kind == "upgrade-bootstrap" else ""))
    return out


def build_decisions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Fixed themes in order (R3-Q9), empty ones skipped; plus one question per edited skill. The
    text never contains action ids: those live in ``actions`` / each option's ``approve`` list."""
    edited = [a for a in plan["actions"] if a.get("state") == "edited"]
    out: list[dict[str, Any]] = []
    for theme, title in THEMES:
        acts = [a for a in plan["actions"] if THEME_OF.get(a["kind"]) == theme and a not in edited]
        if not acts:
            continue
        if theme == "roster":
            out.append(roster_decision(plan, acts[0]))
            continue
        said = _merge_skill_installs(acts, plan)
        ids = [a["id"] for a in acts]
        extra = ""
        if theme == "skills" and plan["mode"] == "register-only":
            extra = " No cockpit skills go into this project: its own loop wins."
        out.append({
            "theme": theme,
            "title": title,
            "question": f"{title}: OK to {_join(said)}?",
            "recommendation": "Yes, all of it." + extra,
            "options": [
                {"label": "Yes, all of it (recommended)", "description": "; ".join(said), "approve": ids},
                {"label": "Not now", "description": "Nothing in this group is changed; setup can offer it again later.", "approve": []},
            ],
            "actions": ids,
        })
    for a in edited:
        where = a.get("dir") or TOOL_LABELS.get(a.get("agent", ""), "") + " global folder"
        out.append({
            "theme": "edited",
            "title": f"Your edits in {a['skill']}",
            "question": f"{a['skill']} in {where} has your own changes and a newer version is available. What should happen?",
            "recommendation": "Keep yours; save the new version beside it to merge by hand.",
            "options": [
                {"label": "Keep mine, save the new one beside it (recommended)",
                 "description": f"the new version goes to {NEW_DIR}/, which agents don't load", "approve": [f"{a['id']}=new"]},
                {"label": "Keep mine only", "description": "nothing changes", "approve": [a["id"]]},
                {"label": "Replace with the new version", "description": "removes the whole skill folder, including files you added",
                 "approve": [f"{a['id']}=replace"]},
            ],
            "actions": [a["id"]],
        })
    return out


def build_roster_table(plan: dict[str, Any]) -> list[dict[str, str]]:
    """Rows for the "use the recommended roster?" question: role -> model, effort, per tool."""
    action = next((a for a in plan["actions"] if a["kind"] == "roster"), None)
    new = {(f["tool"], f["role"]): f for f in (action["files"] if action else [])}
    rows = []
    for t in plan["tools"]:
        lead = (action["lead"].get(t) if action else None) or plan["roster"]["lead"].get(t) or roster.recommended(t, "lead")
        rows.append({"tool": TOOL_LABELS[t], "role": "lead", "model": lead["model"], "effort": lead["effort"], "who": "your session pick"})
        for r in roster.FILE_ROLES:
            if (t, r) in new:
                rows.append({"tool": TOOL_LABELS[t], "role": r, "model": new[(t, r)]["model"], "effort": new[(t, r)]["effort"], "who": "new"})
            else:
                have = plan["roster"]["tools"][t]["roles"][r]
                if have:
                    rows.append({"tool": TOOL_LABELS[t], "role": r, "model": have["model"] or "-", "effort": have["effort"] or "-", "who": "yours"})
    return rows


def roster_questions(plan: dict[str, Any], action: dict[str, Any]) -> list[dict[str, Any]]:
    """The per-role questions behind "customise": the recommendation first, then the known models."""
    out = []
    rows = [("lead", t, action["lead"][t]) for t in plan["tools"]] + [(f["role"], f["tool"], f) for f in action["files"]]
    for role, t, cur in sorted(rows, key=lambda x: (plan["tools"].index(x[1]), roster.ROLES.index(x[0]))):
        suffix = "[fast=false]" if t == "cursor" else ""
        models = [cur["model"]] + [m + suffix for m in roster.KNOWN_MODELS[t] if m != roster.base_model(t, cur["model"])]
        out.append({"tool": t, "role": role, "question": f"{TOOL_LABELS[t]}: which model for the {role}?",
                    "recommendation": f"{cur['model']}, effort {cur['effort']}",
                    "models": models, "efforts": list(roster.EFFORTS), "default_effort": cur["effort"]})
    return out


def roster_decision(plan: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    lines = [f"{TOOL_LABELS[t]}: " + ", ".join(
        f"{r['role']} {r['model']} ({r['effort']})" + ("" if r["who"] == "new" else f" - {r['who']}")
        for r in plan["roster_table"] if r["tool"] == TOOL_LABELS[t]) for t in plan["tools"]]
    return {
        "theme": "roster",
        "title": "Who does each step",
        "question": "Who does each step: use the recommended roster?",
        "recommendation": "Yes: the missing roles get a cockpit agent on the recommended model; your own agents stay as they are.",
        "options": [
            {"label": "Yes, use the recommended roster (recommended)", "description": " | ".join(lines), "approve": [action["id"]]},
            {"label": "Customise", "description": "pick the model and effort per role (one question each), then apply",
             "approve": [action["id"]], "customise": True},
            {"label": "Not now", "description": "No agent files are written; setup can offer it again later.", "approve": []},
        ],
        "actions": [action["id"]],
        "questions": roster_questions(plan, action),
    }


def build_target(plan: dict[str, Any]) -> list[str]:
    """The recommended end state in sentences."""
    p = plan["project"]
    tools = _join([TOOL_LABELS[t] for t in plan["tools"]])
    out = [f"{p['name']} is registered (prefix {p['prefix']}) with its PM data in {p['docs_path']}."]
    if plan["mode"] == "full":
        out.append(f"The cockpit skills live in this project's own skill folder and are current ({plan['tool_version']}).")
    else:
        out.append("No cockpit skills in this project: its own loop and rules stay in charge; the dashboard only shows its cards.")
    if plan["mode"] == "full":
        out.append("Every loop role has an agent per tool (yours are kept, missing ones added), recorded in the Roster of ORCHESTRATION.md.")
    out.append(f"grill-me and {BOOTSTRAP_SKILL} are in the global folder of every tool this project uses ({tools}).")
    if any(a["kind"] == "cleanup-global" for a in plan["actions"]):
        out.append("The old global copies of the cockpit skills are gone.")
    if plan["launchers"]:
        out.append("Every launcher calls the running copy ~/.pm-qa-cockpit/app first.")
    out.append("Your own rules, hooks and orchestration files are untouched.")
    out.append("`cli.py doctor` then reports every check as pass.")
    return out
# ---------------------------------------------------------------------------


def append_bytes(path: Path, block: str) -> None:
    """Append ``block`` to a text file without touching a single existing byte: the block is written in
    the file's own line ending (CRLF if it already uses CRLF) and encoded as UTF-8."""
    data = path.read_bytes() if path.exists() else b""
    eol = b"\r\n" if b"\r\n" in data else b"\n"
    body = block.replace("\r\n", "\n").encode("utf-8").replace(b"\n", eol)
    if not data or data.endswith(eol + eol):
        sep = b""
    elif data.endswith(eol) or data.endswith(b"\n"):
        sep = eol
    else:
        sep = eol + eol
    with path.open("ab") as fh:
        fh.write(sep + body)


def _readable_utf8(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        path.read_bytes().decode("utf-8-sig")
        return True
    except UnicodeDecodeError:
        return False


def _roster_overrides(plan: dict[str, Any], approved_roster: list[dict[str, Any]], overrides: dict[str, Any]) -> None:
    """Validate the "customise" answers before anything is written: only a tool this project uses, only a
    role the roster action would add (or the lead), a well-formed model and a known effort."""
    allowed: dict[tuple[str, str], bool] = {}
    for a in approved_roster:
        for f in a["files"]:
            allowed[(f["tool"], f["role"])] = True
        for t in a["lead"]:
            allowed[(t, "lead")] = True
    if not isinstance(overrides, dict) or not all(isinstance(r, dict) and all(isinstance(v, dict) for v in r.values())
                                                  for r in overrides.values()):
        raise ValueError('roster choices must look like {"claude": {"advisor": {"model": "...", "effort": "high"}}}; nothing was changed.')
    for tool, roles in overrides.items():
        for role, val in roles.items():
            if (tool, role) not in allowed:
                raise ValueError(f"roster choice for {tool}/{role} is not part of the approved roster; nothing was changed.")
            for key in ("model", "effort"):
                if key in val and not roster.valid_value(key, str(val[key])):
                    raise ValueError(f"roster choice {tool}/{role} {key}={val[key]!r} is not valid; nothing was changed.")
            bad = roster.forbidden_pin(tool, role, str(val["model"])) if "model" in val else None
            if bad:
                raise ValueError(f"roster choice {tool}/{role} model {val['model']!r}: {bad}; nothing was changed.")
            if set(val) - {"model", "effort"}:
                raise ValueError(f"roster choice for {tool}/{role} may only set model and effort; nothing was changed.")


def apply_roster(code: Path, plan: dict[str, Any], a: dict[str, Any], overrides: dict[str, Any]) -> str:
    """Write the missing roles' thin agent files (never over an existing file), then the Roster section."""
    done = []
    for f in a["files"]:
        pick = {**f, **(overrides.get(f["tool"], {}).get(f["role"], {}))}
        dest = code / f["path"]
        if dest.exists():
            done.append(f"{f['path']}: already there, kept")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(roster.render_agent(f["tool"], f["role"], pick["model"], pick["effort"]).encode("utf-8"))
        done.append(f"{f['path']}: written")
    rows: dict[str, dict[str, Any]] = {}
    for t in plan["tools"]:
        lead = {**a["lead"][t], **overrides.get(t, {}).get("lead", {})}
        rows[t] = {"lead": {"agent": "(session pick)", **lead}}
        for role, have in roster.detect_tool(code, t)["roles"].items():
            if have:
                rows[t][role] = {"agent": have["name"], "model": have["model"] or "-", "effort": have["effort"] or "-"}
    orch = Path(plan["roster"]["orchestration"])
    if not orch.is_file():
        done.append("ORCHESTRATION.md is not there yet: re-run setup after registering to record the roster table")
    elif not _readable_utf8(orch):
        done.append("ORCHESTRATION.md is not UTF-8: roster table not written")
    else:
        section = roster.render_section(rows, TOOL_LABELS)
        data = orch.read_bytes()
        eol = b"\r\n" if b"\r\n" in data else b"\n"
        start, end = roster.ROSTER_START.encode(), roster.ROSTER_END.encode()
        i, j = data.find(start), data.find(end)
        if data.count(start) == 1 and data.count(end) == 1 and i < j:  # our own markers: refresh only what is between them
            block = section.rstrip("\n").replace("\n", "\n" if eol == b"\n" else "\r\n").encode("utf-8")
            orch.write_bytes(data[:i] + block + data[j + len(end):])
            done.append("ORCHESTRATION.md: roster table written")
        elif data.count(start) or data.count(end):
            done.append("ORCHESTRATION.md: the roster markers are damaged (need exactly one start then one end): table not written")
        else:
            append_bytes(orch, section)
            done.append("ORCHESTRATION.md: roster table written")
    return "; ".join(done)


def apply_actions(plan: dict[str, Any], approved: list[str], on_conflict: str | dict[str, str] = "abort",
                  roster_overrides: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Run only the approved action ids. Registration / gates / clone / grill-me fetch are left to
    their own commands (returned as `run` instructions) so the agent shows their output.

    ``on_conflict`` is one choice for every edited skill, or a per-id mapping ({"A4": "replace"});
    ids not in the mapping default to "abort" (keep the user's copy).
    """
    code = Path(plan["code_path"])
    by_id = {a["id"]: a for a in plan["actions"]}
    unknown = [a for a in approved if a not in by_id]
    if unknown:
        raise ValueError(f"unknown action id(s): {', '.join(unknown)}")
    # Check every target before writing anything, so a bad file can't leave a half-applied setup.
    for aid in approved:
        a = by_id[aid]
        if a["kind"] == "pointer" and not _readable_utf8(code / a["file"]):
            raise ValueError(f"{a['file']} is not UTF-8; nothing was changed. Convert it or decline {aid}.")
        if a["kind"] == "exclude" and exclude_file(code) is None:
            raise ValueError(f"cannot locate this repo's info/exclude; nothing was changed. Decline {aid}.")
        if a["kind"] == "launcher-edit":
            f = code / a["file"]
            lines = f.read_bytes().decode("utf-8-sig", errors="replace").splitlines() if f.is_file() else []
            n = a["edit"]["line"]
            if n > len(lines) or lines[n - 1] != a["edit"]["old"]:
                raise ValueError(f"{a['file']}:{n} changed since the plan was shown; nothing was changed.")

    overrides = roster_overrides or {}
    _roster_overrides(plan, [by_id[i] for i in approved if by_id[i]["kind"] == "roster"], overrides)

    def choice(aid: str) -> str:
        return on_conflict.get(aid, "abort") if isinstance(on_conflict, dict) else on_conflict

    results: list[dict[str, str]] = []
    for aid in approved:
        a = by_id[aid]
        kind = a["kind"]
        if kind == "install-skill":
            dest = code / a["dir"] / a["skill"]
            outcome = install_skill(TOOL_ROOT / "skills" / a["skill"], dest, choice(aid))
            results.append({"id": aid, "result": outcome})
        elif kind == "pointer":
            f = code / a["file"]
            text = f.read_bytes().decode("utf-8-sig") if f.exists() else ""
            if POINTER_START not in text:
                append_bytes(f, a["preview"])
            results.append({"id": aid, "result": "written"})
        elif kind == "exclude":
            exclude = exclude_file(code)
            existing = exclude.read_text(encoding="utf-8", errors="replace").splitlines() if exclude.exists() else []
            add = [p for p in a["paths"] if p not in existing]
            if add:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                append_bytes(exclude, "# PM & QA Cockpit skills (installed per project)\n" + "\n".join(add) + "\n")
            results.append({"id": aid, "result": f"excluded {len(add)} path(s)"})
        elif kind in ("upgrade-bootstrap", "install-bootstrap"):
            dest = global_roots()[a["agent"]] / BOOTSTRAP_SKILL
            results.append({"id": aid, "result": install_skill(TOOL_ROOT / "skills" / BOOTSTRAP_SKILL, dest, choice(aid))})
        elif kind == "copy-generic":
            done = []
            for c in a["copies"]:
                src, dest = Path(c["from"]), Path(c["to"])
                if dest.exists():
                    done.append(f"{c['skill']}: already there")  # never overwrite another copy
                elif (src / "SKILL.md").is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(src, dest)
                    done.append(f"{c['skill']}: copied")
                else:
                    done.append(f"{c['skill']}: source gone, skipped")
            results.append({"id": aid, "result": "; ".join(done)})
        elif kind == "roster":
            results.append({"id": aid, "result": apply_roster(code, plan, a, overrides)})
        elif kind == "launcher-edit":
            wiring.apply_launcher_edit(code, a["file"], a["edit"])
            results.append({"id": aid, "result": "written"})
        elif kind == "cleanup-global":
            removed, kept = 0, []
            for p in a["paths"]:
                d = Path(p)
                # re-check at deletion time: only provably ours and unedited
                if d.is_dir() and global_copy_status(d, d.name) == "ours":
                    shutil.rmtree(d)
                    removed += 1
                elif d.is_dir():
                    kept.append(str(d))
            msg = f"removed {removed} folder(s)" + (f"; kept (changed since the plan): {', '.join(kept)}" if kept else "")
            results.append({"id": aid, "result": msg})
        else:
            results.append({"id": aid, "result": "run", "command": a.get("detail", "")})
    return results


def skills_status(code_path: str | Path | None = None) -> dict[str, Any]:
    """Read-only: the global bootstrap skill and, for a project, its installed cockpit skills."""
    out: dict[str, Any] = {"tool_version": tool_version(), "global": [], "project": []}
    for agent, root in global_roots().items():
        st = skill_state(root / BOOTSTRAP_SKILL, TOOL_ROOT / "skills" / BOOTSTRAP_SKILL)
        out["global"].append({"agent": agent, "skill": BOOTSTRAP_SKILL, "path": str(root / BOOTSTRAP_SKILL), **st})
    if code_path:
        code = Path(code_path).resolve()
        agents = detect_agents(code)
        dirs = sorted({AGENT_SKILL_DIRS[a] for a in agents if a in AGENT_SKILL_DIRS}
                      | ({AGENT_SKILL_DIRS["claude"]} if "cursor" in agents else set()))
        for rel in dirs:
            for skill in PROJECT_SKILLS:
                st = skill_state(code / rel / skill, TOOL_ROOT / "skills" / skill)
                out["project"].append({"agent": rel.split("/")[0].lstrip("."), "skill": skill, "path": f"{rel}/{skill}", **st})
    return out
