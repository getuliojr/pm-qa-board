"""
projects.py - Global multi-project registry for PM & QA Cockpit.

The registry lives at ``~/.pm-qa-cockpit/projects.json`` (override the folder with the
``PM_QA_COCKPIT_HOME`` env var — tests always do). Shape::

    {
      "active_project": "money-nest",
      "projects": {
        "money-nest": {
          "name": "Money-Nest", "slug": "money-nest", "prefix": "MN",
          "code_path": "C:/.../money-nest", "docs_path": "C:/.../money-nest/docs",
          "docs_mode": "in-repo",            # or "external"
          "gates": {"mode": "heavy", "fast": "...", "heavy": "...", "measured_seconds": null}
        }
      }
    }

Standard library only, like the rest of the cockpit.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

DOCS_MODES = ("in-repo", "external")
# The PM artifacts an external docs_path holds (design R2-Q2).
EXTERNAL_DOCS_LAYOUT = ("cards", "reviews", "artifacts", "gate-heavy-results")
EXTERNAL_DOCS_FILES = ("PLAN.md", "WORKLOG.md", "QUALITY.md", "E2E_CANDIDATES.md")


def user_home() -> Path:
    """The home folder holding the global skill folders and ``.pm-qa-cockpit``. ``PM_QA_USER_HOME``
    points it at a sandbox (QA sandbox, tests) while the agent itself keeps its real HOME and login.
    Read on every call, never cached at import."""
    env = os.environ.get("PM_QA_USER_HOME")
    return Path(env).expanduser() if env else Path.home()


def cockpit_home() -> Path:
    env = os.environ.get("PM_QA_COCKPIT_HOME")
    return Path(env).expanduser() if env else user_home() / ".pm-qa-cockpit"


def registry_path() -> Path:
    return cockpit_home() / "projects.json"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


def _norm(p: str | Path) -> str:
    return str(Path(p).expanduser().resolve()).replace("\\", "/")


def git_toplevel(path: str | Path) -> Path | None:
    """The git work tree containing ``path`` (or None). Lets one private docs repo hold many projects."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    res = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=str(p), capture_output=True, text=True, check=False
    )
    return Path(res.stdout.strip()).resolve() if res.returncode == 0 and res.stdout.strip() else None


def load_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.exists():
        return {"active_project": None, "projects": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active_project": None, "projects": {}}
    data.setdefault("active_project", None)
    data.setdefault("projects", {})
    return data


def save_registry(data: dict[str, Any]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def build_project(
    name: str,
    code_path: str | Path,
    *,
    slug: str | None = None,
    prefix: str | None = None,
    docs_path: str | Path | None = None,
    docs_mode: str | None = None,
    gates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and normalise one project entry. Raises ValueError on bad input."""
    if not name or not str(name).strip():
        raise ValueError("name is required")
    code = Path(code_path).expanduser()
    if not code.is_dir():
        raise ValueError(f"code_path does not exist: {code}")
    code_s = _norm(code)
    if docs_path:
        docs_s = _norm(docs_path)
    else:
        docs_s = _norm(code / "docs")
    inside = Path(docs_s).is_relative_to(Path(code_s))
    mode = docs_mode or ("in-repo" if inside else "external")
    if mode not in DOCS_MODES:
        raise ValueError(f"docs_mode must be one of {DOCS_MODES}")
    if mode == "external" and inside:
        raise ValueError("an external docs_path must live outside code_path")
    pfx = (prefix or "").strip().upper() or re.sub(r"[^A-Z]", "", name.upper())[:3] or "PRJ"
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,7}", pfx):
        raise ValueError("prefix must be 1-8 uppercase letters/digits, starting with a letter")
    return {
        "name": str(name).strip(),
        "slug": slug or slugify(name),
        "prefix": pfx,
        "code_path": code_s,
        "docs_path": docs_s,
        "docs_mode": mode,
        "gates": gates or {"mode": "unprofiled"},
    }


def ensure_docs_root(project: dict[str, Any]) -> list[str]:
    """Create the docs layout; for external docs also ``git init`` it. Returns created paths.

    Never writes into ``code_path`` for an external project — that is the whole promise.
    """
    docs = Path(project["docs_path"])
    created: list[str] = []
    if not docs.exists():
        docs.mkdir(parents=True)
        created.append(str(docs))
    for sub in ("cards", "reviews", "artifacts"):
        d = docs / sub
        if not d.exists():
            d.mkdir(parents=True)
            created.append(str(d))
    if project["docs_mode"] == "external":
        for fname in EXTERNAL_DOCS_FILES:
            f = docs / fname
            if not f.exists():
                title = fname[:-3].replace("_", " ")
                f.write_text(f"# {project['name']} — {title}\n", encoding="utf-8")
                created.append(str(f))
        # A folder inside an existing docs repo (e.g. pm-qa-docs/<slug>) uses that repo; only a
        # stand-alone folder gets its own `git init`. Never nest a repo inside another.
        if git_toplevel(docs) is None:
            subprocess.run(["git", "init", "-q"], cwd=str(docs), check=False, capture_output=True)
            created.append(str(docs / ".git"))
    return created


def upsert_project(project: dict[str, Any], *, activate: bool = False) -> dict[str, Any]:
    data = load_registry()
    data["projects"][project["slug"]] = project
    if activate or not data.get("active_project"):
        data["active_project"] = project["slug"]
    save_registry(data)
    return data


def select_project(slug: str) -> dict[str, Any]:
    data = load_registry()
    if slug not in data["projects"]:
        raise KeyError(slug)
    data["active_project"] = slug
    save_registry(data)
    return data["projects"][slug]


def get_active_project() -> dict[str, Any] | None:
    data = load_registry()
    slug = data.get("active_project")
    return data["projects"].get(slug) if slug else None


def find_project_for_path(path: str | Path) -> dict[str, Any] | None:
    """The registered project whose code_path or docs_path contains ``path`` (deepest wins)."""
    target = Path(_norm(path))
    best: tuple[int, dict[str, Any]] | None = None
    for proj in load_registry()["projects"].values():
        for key in ("code_path", "docs_path"):
            root = Path(proj.get(key) or "")
            if str(root) and target.is_relative_to(root):
                depth = len(root.parts)
                if best is None or depth > best[0]:
                    best = (depth, proj)
    return best[1] if best else None


def adhoc_project(code_path: str | Path) -> dict[str, Any]:
    """An unregistered in-repo project rooted at ``code_path`` (legacy single-root behaviour)."""
    code = Path(code_path).resolve()
    docs = code / "docs"
    return {
        "name": code.name,
        "slug": slugify(code.name),
        "prefix": "MN" if (docs / "cards").exists() and any((docs / "cards").glob("MN-*.md")) else "PRJ",
        "code_path": _norm(code),
        "docs_path": _norm(docs if docs.exists() else code),
        "docs_mode": "in-repo",
        "gates": {"mode": "unprofiled"},
    }


def public_view(data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or load_registry()
    return {
        "active_project": data.get("active_project"),
        "projects": sorted(data["projects"].values(), key=lambda p: p.get("name", "").lower()),
        "registry_path": str(registry_path()).replace("\\", "/"),
    }
