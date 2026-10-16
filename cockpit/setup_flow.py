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

    # 1. Prerequisites (machine level)
    prereq = {
        "uv": _which("uv"),
        "git": _which("git"),
        "running_copy": (projects.cockpit_home() / "app" / "cockpit" / "server.py").is_file(),
        "grill_me": any((r / n / "SKILL.md").is_file() for r in global_roots().values() for n in ("grill-me", "grilling")),
    }
    if not prereq["running_copy"]:
        add("clone-running-copy", "Clone the cockpit's running copy",
            writes=[str(projects.cockpit_home() / "app")],
            detail="git clone https://github.com/getuliojr/pm-qa-cockpit ~/.pm-qa-cockpit/app")
    if not prereq["grill_me"]:
        add("fetch-grill-me", "Install grill-me once for this machine (design-before-code skill)",
            writes=[str(global_roots()["claude"] / "grill-me"), str(global_roots()["antigravity"] / "grill-me")],
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

    # 5. Keep installed skills out of git when docs are external / stealth (exact paths only)
    if exclude_skills and not register_only:
        paths = sorted([f"{rel}/{s}/" for rel in skill_dirs for s in PROJECT_SKILLS]
                       + [f"{rel}/{NEW_DIR}/" for rel in skill_dirs])
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
        "notes": notes,
        "actions": actions,
    }


# ---------------------------------------------------------------------------
# Applying approved actions
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


def apply_actions(plan: dict[str, Any], approved: list[str], on_conflict: str | dict[str, str] = "abort") -> list[dict[str, str]]:
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
        elif kind == "upgrade-bootstrap":
            dest = global_roots()[a["agent"]] / BOOTSTRAP_SKILL
            results.append({"id": aid, "result": install_skill(TOOL_ROOT / "skills" / BOOTSTRAP_SKILL, dest, choice(aid))})
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
