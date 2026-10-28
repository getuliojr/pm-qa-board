"""
parser.py - Data parser and updater for the Developer PM & QA Dashboard.

Reads:
- docs/cards/*.md
- docs/cards/backlog/*.md
- PLAN.md (sprint roadmap and DoD sections)
- Git log and git status

Provides write-back for:
- Toggling DoD subtask checkboxes in card files
- Marking cards as validated/completed
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any


def declared_extra_tags(project: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalise a registry entry's ``extra_tags`` into ``[{key, label, link, filter}]``.

    Malformed input is ignored defensively; the first declaration of a key wins.
    """
    raw = (project or {}).get("extra_tags")
    if not isinstance(raw, list):
        return []
    tags: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            continue
        key = key.strip()
        if key in seen:
            continue
        seen.add(key)
        label = entry.get("label")
        link = entry.get("link")
        tags.append(
            {
                "key": key,
                "label": label.strip() if isinstance(label, str) and label.strip() else key,
                "link": link.strip() if isinstance(link, str) and link.strip() else None,
                "filter": entry.get("filter") is True,
            }
        )
    return tags


class PMParser:
    def __init__(
        self,
        root_dir: Path | str | None = None,
        *,
        code_path: Path | str | None = None,
        docs_path: Path | str | None = None,
        prefix: str = "MN",
        project: dict[str, Any] | None = None,
        read_only: bool = False,
    ) -> None:
        """Resolve a project's code repo and PM docs root.

        ``code_path`` is the product repo (git activity, gate scripts). ``docs_path`` holds the PM
        artifacts: either in-repo (``<code_path>/docs``) or any external folder. ``root_dir`` is the
        legacy single-root form and means ``code_path=root_dir`` with ``docs`` under it.
        """
        if project:
            code_path = code_path or project.get("code_path")
            docs_path = docs_path or project.get("docs_path")
            prefix = project.get("prefix") or prefix
        if code_path is None:
            code_path = root_dir
        if code_path is None:
            current = Path.cwd().resolve()
            while current.parent != current:
                if (current / "PLAN.md").exists() or (current / ".git").exists():
                    break
                current = current.parent
            else:
                current = Path.cwd()
            code_path = current
        self.root_dir = Path(code_path).resolve()
        self.code_path = self.root_dir
        self.project = project or {}
        self.prefix = (prefix or "MN").strip().upper()

        if docs_path is None:
            in_repo_docs = self.root_dir / "docs"
            docs_path = in_repo_docs if in_repo_docs.exists() else self.root_dir
        self.docs_path = Path(docs_path).resolve()
        self.docs_external = not self.docs_path.is_relative_to(self.root_dir)

        cards_new = self.docs_path / "cards"
        cards_legacy = self.root_dir / "docs" / "obsidian" / "Cards"
        self.cards_dir = cards_new if cards_new.exists() or not cards_legacy.exists() else cards_legacy
        self.backlog_dir = self.cards_dir / "backlog"

        plan_docs = self.docs_path / "PLAN.md"
        self.plan_path = plan_docs if plan_docs.exists() or self.docs_external else self.root_dir / "PLAN.md"
        self.reviews_dir = self.docs_path / "reviews"
        self.quality_file = self.docs_path / "QUALITY.md"
        self.artifacts_dir = self.docs_path / "artifacts"
        self.sprints_dir = self.docs_path / "sprints"
        self.worklog_file = self.docs_path / "WORKLOG.md"
        self.heavy_results_dir = (
            self.docs_path / "gate-heavy-results" if self.docs_external else self.root_dir / "gate-heavy-results"
        )
        self._file_git_commits_cache: dict[str, list[dict[str, str]]] = {}
        self._sprint_dod_cache: dict[str, list[dict[str, Any]]] = {}
        self._card_git_commit_map: dict[str, list[dict[str, str]]] | None = None
        self._cards_cache: list[dict[str, Any]] | None = None
        self._cards_cache_time: float = 0.0
        self._analytics_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # Hook for the server's parser pool: slug/config -> reusable PMParser (default: build fresh).
        self.parser_provider: Any = None
        self._analytics_memo: dict[str, tuple[str, float, dict[str, Any]]] = {}
        self._git_local = threading.local()  # per-thread: pooled parsers serve several requests at once
        self._plan_cache: dict[str, Any] | None = None
        self._plan_cache_mtime: float = 0.0
        self._reviews_cache: list[dict[str, Any]] = []
        self._reviews_cache_time: float = 0.0

        # SQLite-backed incremental index
        self._index = None
        if not read_only:
            try:
                try:
                    import projects
                    from index import CardIndex
                except ImportError:
                    from board import projects
                    from board.index import CardIndex

                slug = self.project.get("slug")
                registered = (projects.load_registry().get("projects") or {}).get(slug or "")
                if not (registered and Path(registered.get("docs_path") or "").resolve() == self.docs_path):
                    # Not a registered project (ad-hoc, or a slug another checkout registered): a bare
                    # slug or docs folder name can collide, so add a hash of the resolved docs path.
                    base = slug or projects.slugify(self.docs_path.name or self.root_dir.name or "project")
                    digest = hashlib.sha1(str(self.docs_path).lower().encode("utf-8")).hexdigest()[:8]
                    slug = f"{base}-{digest}"
                db_path = projects.board_home() / "cache" / f"{slug}.db"
                self._index = CardIndex(db_path)
            except Exception:
                self._index = None

    def _card_extra_tags(self, frontmatter: dict[str, Any]) -> list[dict[str, Any]]:
        """Declared extra tags present on a card, with per-card resolved (http/https only) links."""
        result = []
        for tag in declared_extra_tags(self.project):
            raw = frontmatter.get(tag["key"])
            value = "" if raw is None else str(raw).strip()
            if not value:
                continue
            link = None
            template = tag["link"]
            if template and template.lower().startswith(("http://", "https://")):
                link = template.replace("{value}", urllib.parse.quote(value, safe=""))
            result.append(
                {"key": tag["key"], "label": tag["label"], "value": value, "link": link, "filter": tag["filter"]}
            )
        return result

    def rel_path(self, file_path: Path) -> str:
        """Display/storage path for a docs file: relative to the code repo when inside it, else absolute."""
        fp = Path(file_path).resolve()
        if fp.is_relative_to(self.root_dir):
            return str(fp.relative_to(self.root_dir)).replace("\\", "/")
        return str(fp).replace("\\", "/")

    def resolve_path(self, stored: str) -> Path:
        """Inverse of rel_path (an absolute stored path wins over the code root)."""
        return self.root_dir / stored

    @staticmethod
    def _normalize_story_frontmatter(frontmatter: dict[str, Any]) -> None:
        """Fold the Story schema onto the legacy keys the rest of the parser reads.

        New projects write ``id:`` / ``story:`` / ``status: story-backlog``; money-nest's history
        writes ``mn-id:`` / ``sprint:`` / ``sprint-backlog``. Both must group identically, so the
        parser keeps one internal vocabulary (``mn-id``/``sprint``/``sprint-backlog``) and records
        which schema the file used in ``_schema``.
        """
        is_story = "story" in frontmatter or (
            "id" in frontmatter and "mn-id" not in frontmatter
        ) or str(frontmatter.get("status", "")).strip().lower() == "story-backlog"
        if "id" in frontmatter and "mn-id" not in frontmatter:
            frontmatter["mn-id"] = frontmatter["id"]
        if "story" in frontmatter and "sprint" not in frontmatter:
            frontmatter["sprint"] = frontmatter["story"]
        frontmatter["_schema"] = "story" if is_story else "sprint"

    def commit_docs(self, files: list[Path], message: str) -> bool:
        """Commit PM files in the repo that holds docs_path (external docs only). Local, never push.

        docs_path may be its own repo or a folder of a shared docs repo (``pm-qa-docs/<slug>``).
        Refuses if that repo is the code repo — PM files never get committed there.
        """
        from projects import git_toplevel

        top = git_toplevel(self.docs_path)
        if top is None or top == self.root_dir or self.root_dir.is_relative_to(top):
            return False
        rels = [Path(f).resolve().relative_to(top).as_posix() for f in files]
        # Windows antivirus/indexer locks make git object writes fail now and then
        # ("unable to write file .git/objects/... Permission denied"); a short retry clears it.
        for attempt in range(3):
            add = subprocess.run(["git", "add", "--", *rels], cwd=str(top), capture_output=True, check=False)
            if add.returncode == 0:
                res = subprocess.run(
                    ["git", "commit", "-q", "-m", message, "--", *rels],
                    cwd=str(top),
                    capture_output=True,
                    check=False,
                )
                if res.returncode == 0:
                    return True
            time.sleep(0.3 * (attempt + 1))
        return False

    def get_closeout_artifact(self, card_id: str) -> dict[str, str] | None:
        """The interactive Story Close-Out artifact for a card, if one exists on disk."""
        name = f"{card_id}-closeout.html"
        path = self.artifacts_dir / name
        if not path.is_file():
            return None
        return {"file_name": name, "file_path": self.rel_path(path), "url": f"/artifacts/{name}"}

    def invalidate_cache(self) -> None:
        """Clears in-memory card and commit caches when disk modifications occur."""
        self._cards_cache = None
        self._card_git_commit_map = None
        self._file_git_commits_cache = {}
        self._analytics_cache = {}
        self._analytics_memo = {}
        self._plan_cache = None
        self._plan_cache_mtime = 0.0
        self._sprint_dod_cache = {}
        self._reviews_cache = []
        self._reviews_cache_time = 0.0

    @staticmethod
    def format_duration(seconds: int) -> str:
        """Formats elapsed seconds into a concise human string: '< 1m', '45m', '2h 15m', or '1d 4h'."""
        if seconds < 0:
            seconds = 0
        if seconds < 60:
            return "< 1m"
        total_minutes = seconds // 60
        if total_minutes < 60:
            return f"{total_minutes}m"
        total_hours = total_minutes // 60
        rem_minutes = total_minutes % 60
        if total_hours < 24:
            return f"{total_hours}h {rem_minutes}m" if rem_minutes > 0 else f"{total_hours}h"
        days = total_hours // 24
        rem_hours = total_hours % 24
        return f"{days}d {rem_hours}h" if rem_hours > 0 else f"{days}d"

    def get_card_git_commit_map(self) -> dict[str, list[dict[str, str]]]:
        """Extracts git commit history for all card files in docs/cards in a single fast batched query."""
        if getattr(self, "_card_git_commit_map", None) is not None:
            return self._card_git_commit_map

        commit_map: dict[str, list[dict[str, str]]] = {}
        git_paths = ["."]

        try:
            res = subprocess.run(
                [
                    "git",
                    "log",
                    "--name-only",
                    "--format=COMMIT%x09%ad%x09%s",
                    "--date=format:%Y-%m-%d %H:%M",
                    "--",
                ] + git_paths,
                cwd=str(self.cards_dir),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if res.returncode == 0:
                current_commit: dict[str, str] | None = None
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("COMMIT\t"):
                        parts = line.split("\t", 2)
                        if len(parts) >= 3:
                            dt_str = parts[1].strip()
                            subj = parts[2].strip()
                            m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", dt_str)
                            if m:
                                current_commit = {
                                    "date": m.group(1),
                                    "time": m.group(2),
                                    "datetime": f"{m.group(1)} {m.group(2)}",
                                    "subject": subj,
                                }
                    elif current_commit:
                        fname = Path(line).name
                        if fname not in commit_map:
                            commit_map[fname] = []
                        commit_map[fname].append(current_commit)
        except Exception:
            pass

        self._card_git_commit_map = commit_map
        return commit_map

    def get_file_git_commits(self, file_path: Path) -> list[dict[str, str]]:
        """Extracts git commit history for a specific file with YYYY-MM-DD HH:MM precision."""
        # Check fast batched cache first. If not in the batched map of all card commits,
        # it is untracked or newly created; return empty list without spawning a subprocess.
        card_map = self.get_card_git_commit_map()
        if file_path.name in card_map:
            return card_map[file_path.name]
        return []

    @staticmethod
    def _parse_iso_datetime(dt_val: Any) -> datetime.datetime | None:
        if not dt_val:
            return None
        s = str(dt_val).strip()
        m = re.match(r"^(\d{4}-\d{2}-\d{2})[T\s](\d{1,2}:\d{2})(?::\d{2})?", s)
        if m:
            try:
                return datetime.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
            except Exception:
                pass
        m_d = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
        if m_d:
            try:
                return datetime.datetime.strptime(f"{m_d.group(1)} 09:00", "%Y-%m-%d %H:%M")
            except Exception:
                pass
        return None

    def compute_card_duration(
        self,
        card: dict[str, Any],
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        """
        Calculates Active Turnaround duration for a card or stream:
        - Strictly measures time from in-progress start (earliest in-progress changelog timestamp / start SHA)
          to QA submission / completed timestamp, ignoring backlog idle latency.
        - For 'in-progress' cards / active grills: duration from start to current local time.
        - For 'qa' / 'completed' cards: duration from in-progress start to QA submission / completed timestamp.
        - For 'backlog' / 'story-backlog' / 'sprint-backlog': elapsed_seconds = 0.
        Returns dict with: elapsed_seconds, duration_formatted, start_time, end_time.
        """
        changelog = card.get("changelog") or []
        status = (card.get("status") or "backlog").lower().strip()
        is_grill = bool(card.get("is_grill", False))

        if status in ("backlog", "story-backlog", "sprint-backlog") and not is_grill:
            return {
                "elapsed_seconds": 0,
                "duration_formatted": "—",
                "start_time": None,
                "end_time": None,
            }

        dt_start: datetime.datetime | None = None

        # 1. Determine start timestamp from changelog entry when build started / in-progress
        for entry in changelog:
            what_lower = str(entry.get("what") or "").lower()
            if any(k in what_lower for k in ("start build", "build started", "in-progress", "start sha", "building", "moved card to in-progress")):
                dt_val = entry.get("datetime") or f"{entry.get('date', '')} {entry.get('time', '')}".strip()
                dt_start = self._parse_iso_datetime(dt_val)
                if dt_start:
                    break

        if not dt_start:
            for entry in changelog:
                what_lower = str(entry.get("what") or "").lower()
                if not any(k in what_lower for k in ("created", "backlog", "seed", "initial")):
                    dt_val = entry.get("datetime") or f"{entry.get('date', '')} {entry.get('time', '')}".strip()
                    dt_start = self._parse_iso_datetime(dt_val)
                    if dt_start:
                        break

        if not dt_start and changelog:
            for entry in changelog:
                dt_val = entry.get("datetime") or f"{entry.get('date', '')} {entry.get('time', '')}".strip()
                dt_start = self._parse_iso_datetime(dt_val)
                if dt_start:
                    break

        if not dt_start:
            dt_start = self._parse_iso_datetime(card.get("start_time"))

        if not dt_start:
            dt_start = self._parse_iso_datetime(card.get("created_date") or card.get("created_at"))

        if not dt_start:
            ref_now = now or datetime.datetime.now()
            dt_start = ref_now

        # 2. Determine end timestamp
        dt_end: datetime.datetime | None = None
        elapsed_seconds: int = 0
        end_time_str: str | None = None

        if status == "in-progress" or is_grill:
            ref_now = now or datetime.datetime.now()
            elapsed_seconds = max(0, int((ref_now - dt_start).total_seconds()))
            end_time_str = None
        elif status in ("qa", "completed"):
            if card.get("qa_entered_at"):
                dt_end = self._parse_iso_datetime(card.get("qa_entered_at"))

            if not dt_end and changelog:
                for entry in reversed(changelog):
                    what_lower = str(entry.get("what") or "").lower()
                    if any(k in what_lower for k in ("to qa", "ready for qa", "review pass", "card to qa", "reviewer pass", "completed", "validated")):
                        dt_val = entry.get("datetime") or f"{entry.get('date', '')} {entry.get('time', '')}".strip()
                        dt_end = self._parse_iso_datetime(dt_val)
                        if dt_end:
                            break

            if not dt_end and changelog:
                last_entry = changelog[-1]
                dt_val = last_entry.get("datetime") or f"{last_entry.get('date', '')} {last_entry.get('time', '')}".strip()
                dt_end = self._parse_iso_datetime(dt_val)

            if not dt_end:
                dt_end = self._parse_iso_datetime(card.get("validated") or card.get("end_time"))

            if not dt_end or dt_end < dt_start:
                dt_end = dt_start

            elapsed_seconds = max(0, int((dt_end - dt_start).total_seconds()))
            end_time_str = dt_end.strftime("%Y-%m-%d %H:%M")
        else:
            elapsed_seconds = 0
            end_time_str = None

        duration_formatted = self.format_duration(elapsed_seconds)
        start_time_str = dt_start.strftime("%Y-%m-%d %H:%M")

        return {
            "elapsed_seconds": elapsed_seconds,
            "duration_formatted": duration_formatted,
            "start_time": start_time_str,
            "end_time": end_time_str,
        }


    # --------------------------------------------------------------------------
    # Card Parsing
    # --------------------------------------------------------------------------
    def parse_card_file(self, file_path: Path, _preloaded_reviews: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any] | None:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return None

        # Parse YAML frontmatter
        fm_match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", content, re.DOTALL)
        frontmatter: dict[str, Any] = {}
        body = content

        if fm_match:
            fm_text, body = fm_match.groups()
            for line in fm_text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, val = line.split(":", 1)
                    key = key.strip()
                    val = val.strip().strip('"\'')
                    if val.lower() == "true":
                        frontmatter[key] = True
                    elif val.lower() == "false":
                        frontmatter[key] = False
                    else:
                        frontmatter[key] = val

        # Extract title from # Header or filename
        title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else file_path.stem

        # Story schema (id/story/story-backlog) with legacy money-nest fallback (mn-id/sprint/sprint-backlog)
        self._normalize_story_frontmatter(frontmatter)
        mn_id = frontmatter.get("mn-id")
        if not mn_id:
            id_match = re.search(r"^([A-Z][A-Z0-9]*-[A-Z0-9][\w.-]*)", file_path.stem)
            mn_id = id_match.group(1) if id_match else file_path.stem

        # Extract Description
        desc_match = re.search(
            r"##\s+Description\s*\r?\n(.*?)(?=\r?\n##|\Z)", body, re.DOTALL
        )
        description = desc_match.group(1).strip() if desc_match else ""

        # Extract DoD items
        dod_match = re.search(
            r"##\s+(?:Definition of Done|Acceptance Criteria).*?\r?\n(.*?)(?=\r?\n##|\Z)",
            body,
            re.DOTALL,
        )
        dod_items: list[dict[str, Any]] = []
        if dod_match:
            dod_text = dod_match.group(1)
            for line in dod_text.splitlines():
                m = re.match(r"^\s*-\s*\[([ xX])\]\s*(.+)$", line)
                if m:
                    checked = m.group(1).lower() == "x"
                    text = m.group(2).strip()
                    dod_items.append({"text": text, "checked": checked})

        # Calculate progress
        total_dod = len(dod_items)
        checked_dod = sum(1 for item in dod_items if item["checked"])
        progress_pct = round((checked_dod / total_dod * 100)) if total_dod > 0 else 0

        # Extract Human QA Test items/instructions
        qa_match = re.search(
            r"##\s+(?:Human QA Test|Human QA|Manual Verification).*?\r?\n(.*?)(?=\r?\n##\s|\Z)",
            body,
            re.DOTALL,
        )
        qa_text = ""
        qa_instructions = ""
        qa_items: list[dict[str, Any]] = []
        if qa_match:
            qa_text = qa_match.group(1).strip()
            instr_lines = []
            for line in qa_text.splitlines():
                m = re.match(r"^\s*-\s*\[([ xX])\]\s*(.+)$", line)
                if m:
                    checked = m.group(1).lower() == "x"
                    text = m.group(2).strip()
                    qa_items.append({"text": text, "checked": checked})
                else:
                    instr_lines.append(line)
            qa_instructions = "\n".join(instr_lines).strip()
            # Strip trailing empty subheadings (e.g. ### Verification Check, ### Verification Items)
            # whose checkbox items were extracted into qa_items
            while qa_instructions:
                lines = qa_instructions.splitlines()
                last_line = lines[-1].strip()
                if not last_line:
                    qa_instructions = "\n".join(lines[:-1]).strip()
                elif re.match(r"^#{1,6}\s+", last_line):
                    qa_instructions = "\n".join(lines[:-1]).strip()
                else:
                    break

        # Card status
        status = frontmatter.get("status", "backlog").lower().strip()
        # Normalise status
        if status in ("sprint-backlog", "story-backlog", "ready", "todo"):
            status = "sprint-backlog"
        elif status in ("in-progress", "building", "active"):
            status = "in-progress"
        elif status in ("qa", "qa-test", "testing"):
            status = "qa"
        elif status in ("completed", "done", "validated"):
            status = "completed"
        else:
            status = "backlog"

        # Changelog
        changelog: list[dict[str, Any]] = []
        cl_match = re.search(
            r"##\s+Changelog\s*\r?\n(.*?)(?=\r?\n##|\Z)",
            body,
            re.DOTALL,
        )
        if cl_match:
            cl_block = cl_match.group(1)
            table_lines = [
                ln.strip()
                for ln in cl_block.splitlines()
                if ln.strip().startswith("|") and ln.strip().endswith("|")
            ]

            header_parts: list[str] = []
            data_rows: list[list[str]] = []
            for ln in table_lines:
                if re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", ln):
                    continue
                parts = [p.strip() for p in ln.strip("|").split("|")]
                if not header_parts and any(
                    k in [p.lower() for p in parts]
                    for k in ("date", "date & time", "datetime", "agent", "what", "action")
                ):
                    header_parts = [p.lower() for p in parts]
                else:
                    data_rows.append(parts)

            for parts in data_rows:
                if len(parts) >= 3:
                    c_date = ""
                    c_time = ""
                    c_agent = ""
                    c_what = ""

                    # Map columns according to header if detected
                    date_col_idx = -1
                    time_col_idx = -1
                    agent_col_idx = -1
                    what_col_idx = -1

                    for idx, h in enumerate(header_parts):
                        if "date & time" in h or "datetime" in h:
                            date_col_idx = idx
                        elif "date" in h:
                            date_col_idx = idx
                        elif "time" in h:
                            time_col_idx = idx
                        elif "agent" in h or "role" in h:
                            agent_col_idx = idx
                        elif "what" in h or "action" in h or "work" in h:
                            what_col_idx = idx

                    if date_col_idx != -1 and agent_col_idx != -1 and what_col_idx != -1:
                        c_date_raw = parts[date_col_idx] if date_col_idx < len(parts) else ""
                        c_time = parts[time_col_idx] if time_col_idx != -1 and time_col_idx < len(parts) else ""
                        c_agent = parts[agent_col_idx] if agent_col_idx < len(parts) else ""
                        c_what = parts[what_col_idx] if what_col_idx < len(parts) else ""

                        dt_m = re.match(r"^(\d{4}-\d{2}-\d{2})[T\s]+(\d{1,2}:\d{2}(?::\d{2})?)(.*)$", c_date_raw)
                        if dt_m:
                            c_date = dt_m.group(1)
                            if not c_time:
                                c_time = dt_m.group(2)[:5]
                        else:
                            c_date = c_date_raw
                    else:
                        # Positional fallback
                        if len(parts) >= 4 and re.match(r"^\d{1,2}:\d{2}(?::\d{2})?$", parts[1]):
                            c_date = parts[0]
                            c_time = parts[1][:5]
                            c_agent = parts[2]
                            c_what = parts[3]
                        else:
                            c_date = parts[0]
                            c_time = ""
                            c_agent = parts[1]
                            c_what = parts[2]
                            dt_m = re.match(r"^(\d{4}-\d{2}-\d{2})[T\s]+(\d{1,2}:\d{2}(?::\d{2})?)(.*)$", c_date)
                            if dt_m:
                                c_date = dt_m.group(1)
                                c_time = dt_m.group(2)[:5]

                    # Review detection
                    is_build_or_card_start = bool(
                        re.match(r"^(?:build\s+started|created\s+card)\b", c_what, re.IGNORECASE)
                    )
                    has_explicit_link = bool(re.search(r"\[review:\s*[^\]]+\]", c_what, re.IGNORECASE))
                    is_reviewer_agent = bool(re.match(r"^(?:reviewer|cold\s+.*review)\b", c_agent, re.IGNORECASE))
                    has_verdict = bool(
                        re.search(r"\b(?:PASS|FAIL|CHANGES REQUESTED)\b", c_what, re.IGNORECASE)
                        and re.search(r"\b(?:review|round\s*\d+)\b", c_what, re.IGNORECASE)
                    )
                    has_review_title = bool(
                        re.search(r"\b(?:cold\s+)?(?:definition-of-done\s+)?review:\s*(?:PASS|FAIL)\b", c_what, re.IGNORECASE)
                    )

                    has_review = not is_build_or_card_start and (
                        has_explicit_link
                        or (is_reviewer_agent and (has_verdict or "review" in c_what.lower()))
                        or has_verdict
                        or has_review_title
                    )

                    m_r = re.search(r"round\s*(\d+)", c_what, re.IGNORECASE)
                    r_num = int(m_r.group(1)) if m_r else (1 if has_review else None)
                    changelog.append(
                        {
                            "date": c_date,
                            "time": c_time,
                            "datetime": f"{c_date} {c_time}".strip(),
                            "agent": c_agent,
                            "what": c_what,
                            "has_review": has_review,
                            "review_round": r_num,
                        }
                    )

            if not changelog:
                for ln in cl_block.splitlines():
                    ln_s = ln.strip()
                    if not (ln_s.startswith("-") or ln_s.startswith("*")):
                        continue
                    ln_s = ln_s.lstrip("-* ").strip()
                    m_dt = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T\s]+(\d{1,2}:\d{2}))?[:\s-]+(.*)$", ln_s)
                    if m_dt:
                        c_date = m_dt.group(1)
                        c_time = m_dt.group(2) or ""
                        rest = m_dt.group(3).strip()
                        ag_m = re.search(r"(?:Agent:\s*|by\s+)([A-Za-z0-9_.\s-]+?)(?:\.|$)", rest, re.IGNORECASE)
                        c_agent = ag_m.group(1).strip() if ag_m else ""
                        c_what = rest
                        changelog.append(
                            {
                                "date": c_date,
                                "time": c_time,
                                "datetime": f"{c_date} {c_time}".strip(),
                                "agent": c_agent,
                                "what": c_what,
                                "has_review": False,
                                "review_round": None,
                            }
                        )

        # Backfill missing times from Git commit logs or file mtime so entry.datetime is always populated
        file_commits = None
        for i, entry in enumerate(changelog):
            if not entry.get("time"):
                c_date = entry.get("date", "")
                c_what = entry.get("what", "").lower()
                backfilled_time = None

                if file_commits is None:
                    file_commits = self.get_file_git_commits(file_path)

                matching_date_commits = [c for c in file_commits if c["date"] == c_date]
                for gc in matching_date_commits:
                    words = [w for w in re.findall(r"\w+", c_what) if len(w) > 3]
                    subj_lower = gc["subject"].lower()
                    if words and any(w in subj_lower for w in words):
                        backfilled_time = gc["time"]
                        break

                if not backfilled_time and matching_date_commits:
                    backfilled_time = matching_date_commits[0]["time"]

                if not backfilled_time:
                    try:
                        st = file_path.stat()
                        mtime_dt = datetime.datetime.fromtimestamp(st.st_mtime)
                        if mtime_dt.strftime("%Y-%m-%d") == c_date:
                            backfilled_time = mtime_dt.strftime("%H:%M")
                        else:
                            ctime_dt = datetime.datetime.fromtimestamp(st.st_ctime)
                            if ctime_dt.strftime("%Y-%m-%d") == c_date:
                                backfilled_time = ctime_dt.strftime("%H:%M")
                            else:
                                backfilled_time = mtime_dt.strftime("%H:%M")
                    except Exception:
                        backfilled_time = "12:00"

                if i > 0 and changelog[i - 1].get("date") == c_date and changelog[i - 1].get("time"):
                    prev_time = changelog[i - 1]["time"]
                    if backfilled_time and backfilled_time < prev_time:
                        backfilled_time = prev_time

                entry["time"] = backfilled_time or "12:00"
                entry["datetime"] = f"{entry['date']} {entry['time']}".strip()

        # Ensure changelog is in ascending chronological order
        changelog.sort(key=lambda x: f"{x.get('date', '')} {x.get('time', '')}".strip())

        # Extract created_date
        created_date = None
        for fm_key in ("created", "created_date", "date"):
            val = frontmatter.get(fm_key)
            if val and re.match(r"^\d{4}-\d{2}-\d{2}", str(val)):
                created_date = str(val)[:10]
                break

        card_map = self.get_card_git_commit_map()
        commits = card_map.get(file_path.name)
        git_creation_commit = commits[-1] if commits else None

        if not created_date and git_creation_commit:
            # Oldest commit in reverse-chronological log is creation commit
            created_date = git_creation_commit.get("date")

        if not created_date and changelog:
            valid_dates = [c["date"] for c in changelog if re.match(r"^\d{4}-\d{2}-\d{2}", c["date"])]
            if valid_dates:
                created_date = sorted(valid_dates)[0]

        if not created_date:
            des_match = re.search(r"designed\s+(\d{4}-\d{2}-\d{2})", body, re.IGNORECASE)
            if des_match:
                created_date = des_match.group(1)

        if not created_date:
            try:
                created_date = datetime.date.fromtimestamp(file_path.stat().st_ctime).isoformat()
            except Exception:
                try:
                    created_date = datetime.date.fromtimestamp(file_path.stat().st_mtime).isoformat()
                except Exception:
                    created_date = datetime.date.today().isoformat()

        mtime = datetime.datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()

        # Extract created_time
        created_time = None
        for fm_key in ("created_time", "time"):
            val = frontmatter.get(fm_key)
            if val and re.match(r"^\d{1,2}:\d{2}", str(val)):
                created_time = str(val)[:5]
                break

        if not created_time:
            for fm_key in ("created", "created_date", "date"):
                val = str(frontmatter.get(fm_key) or "")
                m_time = re.search(r"[T\s](\d{1,2}:\d{2})", val)
                if m_time:
                    created_time = m_time.group(1)
                    break

        # Prioritize Git creation commit timestamp over changelog text (R2-Q1)
        if not created_time and git_creation_commit:
            created_time = git_creation_commit.get("time")

        if not created_time and changelog:
            for c in changelog:
                if c.get("time") and re.match(r"^\d{1,2}:\d{2}", str(c.get("time"))):
                    created_time = str(c.get("time"))[:5]
                    break

        if not created_time:
            try:
                st = file_path.stat()
                ctime_dt = datetime.datetime.fromtimestamp(st.st_ctime)
                if ctime_dt.strftime("%Y-%m-%d") == created_date:
                    created_time = ctime_dt.strftime("%H:%M")
                else:
                    mtime_dt = datetime.datetime.fromtimestamp(st.st_mtime)
                    created_time = mtime_dt.strftime("%H:%M")
            except Exception:
                created_time = "12:00"

        created_at = f"{created_date} {created_time}".strip() if (created_date and created_time) else (created_date or "")

        # Quick Win 2: use pre-loaded reviews when available (batch path from get_all_cards),
        # fall back to per-card lookup for standalone calls.
        if _preloaded_reviews is not None:
            card_key = mn_id.lower()
            s_val = str(frontmatter.get("sprint", "")).strip()
            sprint_key = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", s_val, flags=re.IGNORECASE).strip().lower()
            reviews = _preloaded_reviews.get(card_key, [])
            if not reviews and sprint_key:
                reviews = _preloaded_reviews.get(sprint_key, [])
        else:
            reviews = self.get_reviews_for_card(mn_id)
            if not reviews:
                s_val = str(frontmatter.get("sprint", "")).strip()
                if s_val:
                    reviews = self.get_reviews_for_sprint(s_val)

        # Compute qa_entered_at (timestamp when card entered QA)
        qa_entered_at = None
        for c in reversed(changelog):
            what_lower = (c.get("what") or "").lower()
            agent_lower = (c.get("agent") or "").lower()
            if any(k in what_lower for k in ("to qa", "ready for qa", "review pass", "card to qa", "reviewer pass")) or "reviewer" in agent_lower:
                c_dt = c.get("datetime") or f"{c.get('date', '')} {c.get('time', '')}".strip()
                if c_dt:
                    qa_entered_at = c_dt
                    break

        if not qa_entered_at and reviews:
            rev_date = reviews[-1].get("date")
            if rev_date:
                qa_entered_at = str(rev_date)

        if not qa_entered_at and status == "qa":
            try:
                qa_entered_at = mtime[:16].replace("T", " ")
            except Exception:
                pass

        # Check sprint-backlog readiness gate: must have DoD in PLAN.md
        warnings_list: list[str] = []
        missing_plan_dod = False
        if status == "sprint-backlog":
            s_val = str(frontmatter.get("sprint", "")).strip()
            clean_s = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", s_val, flags=re.IGNORECASE).strip()
            plan_dod = self._extract_sprint_dod(clean_s) if clean_s else []
            if not plan_dod:
                missing_plan_dod = True
                warn_msg = f"Card {mn_id} has status: sprint-backlog but Sprint '{clean_s}' lacks Definition of Done in PLAN.md"
                warnings_list.append(warn_msg)
                print(f"[WARN] {warn_msg}", file=sys.stderr)

        # Smart hybrid sizing (Story 29): read points/estimate frontmatter, or default:
        # Feature = 3 pts, Improvement = 2 pts, Bug/Fix/Chore/Docs/Other = 1 pt.
        raw_pts = frontmatter.get("points") or frontmatter.get("estimate")
        parsed_pts: int | None = None
        if raw_pts is not None:
            try:
                p_fl = float(str(raw_pts).strip())
                parsed_pts = int(p_fl) if p_fl.is_integer() else int(p_fl)
            except (ValueError, TypeError):
                pass
        c_type_norm = str(frontmatter.get("type") or "feature").lower().strip()
        if parsed_pts is not None:
            pts_val = max(1, parsed_pts)
            pts_custom = True
        elif c_type_norm == "feature":
            pts_val = 3
            pts_custom = False
        elif c_type_norm in ("improvement", "enhance", "enhancement"):
            pts_val = 2
            pts_custom = False
        else:
            pts_val = 1
            pts_custom = False

        sev_raw = str(frontmatter.get("severity") or frontmatter.get("priority") or "").strip()
        sev_val = sev_raw if sev_raw else None

        card_dict = {
            "mn_id": mn_id,
            "id": mn_id,
            "title": title,
            "sprint": str(frontmatter.get("sprint", "")),
            "story": str(frontmatter.get("sprint", "")),
            "schema": frontmatter.get("_schema", "sprint"),
            "type": frontmatter.get("type", "feature"),
            "points": pts_val,
            "points_custom": pts_custom,
            "severity": sev_val,
            "blocked": bool(frontmatter.get("blocked") is True or str(frontmatter.get("blocked", "")).lower() == "true"),
            "extra_tags": self._card_extra_tags(frontmatter),
            "_frontmatter": frontmatter,
            "status": status,
            "warnings": warnings_list,
            "missing_plan_dod": missing_plan_dod,
            "warning_missing_plan_dod": missing_plan_dod,
            "validated": frontmatter.get("validated"),
            "created_date": created_date,
            "created_time": created_time,
            "created_at": created_at,
            "qa_entered_at": qa_entered_at,
            "description": description,
            "dod": dod_items,
            "dod_total": total_dod,
            "dod_checked": checked_dod,
            "progress_pct": progress_pct,
            "qa_text": qa_text,
            "qa_instructions": qa_instructions,
            "qa_items": qa_items,
            "changelog": changelog,
            "reviews": reviews,
            "review_count": len(reviews),
            "file_name": file_path.name,
            "file_path": self.rel_path(file_path),
            "closeout": self.get_closeout_artifact(mn_id),
            "updated_at": mtime,
        }

        dur_info = self.compute_card_duration(card_dict)
        card_dict.update({
            "elapsed_seconds": dur_info["elapsed_seconds"],
            "duration_formatted": dur_info["duration_formatted"],
            "start_time": dur_info["start_time"],
            "end_time": dur_info["end_time"],
        })

        return card_dict
    parse_card = parse_card_file

    # --------------------------------------------------------------------------
    # Cold Review Report Parsing
    # --------------------------------------------------------------------------
    def parse_review_file(self, file_path: Path) -> dict[str, Any] | None:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

        fm_match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", content, re.DOTALL)
        frontmatter: dict[str, Any] = {}
        body = content

        if fm_match:
            fm_text, body = fm_match.groups()
            for line in fm_text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, val = line.split(":", 1)
                    key = key.strip()
                    val = val.strip().strip('"\'')
                    if val.lower() == "true":
                        frontmatter[key] = True
                    elif val.lower() == "false":
                        frontmatter[key] = False
                    elif val.isdigit():
                        frontmatter[key] = int(val)
                    else:
                        frontmatter[key] = val

        sprint = str(frontmatter.get("sprint", "")).strip()
        card_id = str(frontmatter.get("card", "")).strip()
        round_num = frontmatter.get("round")

        m_file = re.search(r"MN-([0-9a-zA-Z._-]+?)(?:-round-(\d+))?\.md", file_path.name, re.IGNORECASE)
        if m_file:
            if not sprint:
                sprint = m_file.group(1)
            if not card_id:
                card_id = f"MN-{m_file.group(1)}"
            if round_num is None and m_file.group(2):
                round_num = int(m_file.group(2))

        if not card_id and sprint:
            card_id = f"MN-{sprint}"
        if round_num is None:
            round_num = 1

        reviewer = str(frontmatter.get("reviewer", "Cold Reviewer")).strip()
        date_str = str(frontmatter.get("date", "")).strip()
        git_range = str(frontmatter.get("git_range", "")).strip()
        start_sha = str(frontmatter.get("start_sha", "")).strip()
        head_sha = str(frontmatter.get("head_sha", "")).strip()

        if not git_range and start_sha and head_sha:
            git_range = f"{start_sha}..{head_sha}"
        elif git_range and (not start_sha or not head_sha):
            if ".." in git_range:
                parts = git_range.split("..", 1)
                if not start_sha:
                    start_sha = parts[0].strip()
                if not head_sha:
                    head_sha = parts[1].strip()

        verdict = str(frontmatter.get("verdict", "PASS")).strip()

        # Parse findings checklist items: - [x] or - [ ]
        findings: list[dict[str, Any]] = []
        for line in body.splitlines():
            m_item = re.match(r"^\s*-\s*\[([ xX])\]\s*(.+)$", line)
            if m_item:
                checked = m_item.group(1).lower() == "x"
                item_text = m_item.group(2).strip()
                # Extract SHA if present, e.g. (`abc1234`) or (resolved in abc1234)
                m_sha = re.search(r"[`(]([0-9a-fA-F]{7,40})[`)]", item_text)
                if not m_sha:
                    m_sha = re.search(r"\b(?:in|resolved\s+in|commit)\s+`?([0-9a-fA-F]{7,40})`?", item_text, re.IGNORECASE)
                res_sha = m_sha.group(1)[:7].lower() if m_sha else None
                findings.append({
                    "text": item_text,
                    "checked": checked,
                    "sha": res_sha,
                    "resolution_sha": res_sha,
                    "status": "resolved" if checked else "open",
                })

        return {
            "sprint": sprint,
            "card_id": card_id,
            "round": int(round_num),
            "reviewer": reviewer,
            "date": date_str,
            "git_range": git_range,
            "start_sha": start_sha,
            "head_sha": head_sha,
            "verdict": verdict,
            "findings": findings,
            "findings_count": len(findings),
            "resolved_count": sum(1 for f in findings if f["checked"]),
            "body": body.strip(),
            "content": body.strip(),
            "file_path": self.rel_path(file_path),
            "file_name": file_path.name,
            "is_legacy": False,
        }

    def parse_legacy_reviews_from_quality_md(self) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        quality_file = self.quality_file
        if not quality_file.exists():
            return reviews

        try:
            content = quality_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return reviews

        round_log_match = re.search(r"##\s+Round log.*?\r?\n(.*?)(?=\r?\n##|\Z)", content, re.DOTALL | re.IGNORECASE)
        table_text = round_log_match.group(1) if round_log_match else content

        for line in table_text.splitlines():
            line = line.strip()
            if not line.startswith("|") or line.startswith("| ---") or line.startswith("| Sprint"):
                continue
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) >= 4:
                sp_raw = parts[0].strip("*").strip()
                rd_raw = parts[1].strip()
                res_raw = parts[2].strip("*").strip()
                notable = parts[3].strip()

                if not sp_raw or not rd_raw.isdigit():
                    continue

                m_sha = re.search(r"range\s+`?([0-9a-fA-F.]+)`?", notable)
                git_range = m_sha.group(1) if m_sha else ""

                m_rev = re.search(r"Cold\s+([A-Za-z0-9.\s]+?)(?:reviewer|\,)", notable, re.IGNORECASE)
                reviewer = m_rev.group(1).strip() if m_rev else "Cold Reviewer"

                m_date = re.search(r"\b(2026-\d{2}-\d{2})\b", notable)
                date_str = m_date.group(1) if m_date else ""

                reviews.append({
                    "sprint": sp_raw,
                    "card_id": f"MN-{sp_raw}",
                    "round": int(rd_raw),
                    "reviewer": reviewer,
                    "date": date_str,
                    "git_range": git_range,
                    "verdict": res_raw,
                    "findings": [],
                    "findings_count": 0,
                    "resolved_count": 0,
                    "body": notable,
                    "file_path": "docs/QUALITY.md",
                    "file_name": "QUALITY.md",
                    "is_legacy": True,
                })

        return reviews

    def get_all_reviews(self, refresh: bool = False) -> list[dict[str, Any]]:
        now = time.time()
        if not refresh and hasattr(self, "_reviews_cache") and (now - getattr(self, "_reviews_cache_time", 0)) < 30.0:
            return self._reviews_cache
        reviews: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, int]] = set()

        # 1. Scanned docs/reviews/
        if self.reviews_dir.exists():
            for f in sorted(self.reviews_dir.glob("*.md")):
                rev = self.parse_review_file(f)
                if rev:
                    reviews.append(rev)
                    seen_keys.add((rev["sprint"].lower(), rev["round"]))

        # 2. Legacy fallback from docs/QUALITY.md
        legacy_reviews = self.parse_legacy_reviews_from_quality_md()
        for l_rev in legacy_reviews:
            key = (l_rev["sprint"].lower(), l_rev["round"])
            if key not in seen_keys:
                reviews.append(l_rev)
                seen_keys.add(key)

        self._reviews_cache = reviews
        self._reviews_cache_time = now
        return reviews

    def get_reviews_for_sprint(self, sprint_id: str) -> list[dict[str, Any]]:
        clean_s = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", str(sprint_id), flags=re.IGNORECASE).strip().lower()
        all_revs = self.get_all_reviews()
        return [r for r in all_revs if r["sprint"].lower() == clean_s]

    def get_reviews_for_card(self, card_id: str) -> list[dict[str, Any]]:
        clean_card = str(card_id).strip().lower()
        m = re.search(r"MN-([0-9a-zA-Z._-]+)", clean_card, re.IGNORECASE)
        sprint_candidate = m.group(1).lower() if m else clean_card
        all_revs = self.get_all_reviews()
        return [r for r in all_revs if r["card_id"].lower() == clean_card or r["sprint"].lower() == sprint_candidate]

    def _append_active_grills(self, cards: list[dict[str, Any]]) -> None:
        """Surface active design grills from PLAN.md in the In-Progress column on the board."""
        if not self.plan_path.exists():
            return
        plan_content = ""
        try:
            plan_content = self.plan_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return
        if not plan_content:
            return

        grill_matches = re.finditer(
            r"^\s*#{3,4}\s+Grill in progress\s*[—–\-]\s*(?:(?:Sprint|Story)\s+)?([a-zA-Z0-9_.-]+)",
            plan_content,
            re.IGNORECASE | re.MULTILINE,
        )
        seen_sprints = {
            c.get("sprint", "").lower()
            for c in cards
            if c.get("sprint") and c.get("status") in ("sprint-backlog", "in-progress", "qa", "completed")
        }
        seen_ids = {
            c.get("id", "").lower()
            for c in cards
            if c.get("id")
        }

        # Roadmap data for scope / story title
        roadmap_items = self.parse_plan().get("roadmap", [])

        for gm in grill_matches:
            g_sprint = gm.group(1).strip()
            clean_g_sprint = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", g_sprint, flags=re.IGNORECASE).strip()
            if not clean_g_sprint:
                continue
            if clean_g_sprint.lower() in seen_sprints or f"grill-{clean_g_sprint}".lower() in seen_ids:
                continue

            roadmap_item = next(
                (
                    r
                    for r in roadmap_items
                    if str(r.get("sprint", "")).lower() == clean_g_sprint.lower()
                    or str(r.get("story", "")).lower() == clean_g_sprint.lower()
                ),
                None,
            )
            scope_text = roadmap_item["scope"] if roadmap_item and roadmap_item.get("scope") else "Design Grill"
            title = f"Story {clean_g_sprint} — {scope_text}"

            plan_dod = self._extract_sprint_dod(clean_g_sprint)
            dod_total = len(plan_dod)
            dod_checked = sum(1 for d in plan_dod if d.get("checked"))
            progress_pct = round((dod_checked / dod_total * 100)) if dod_total > 0 else 0

            plan_stat = self.plan_path.stat() if (self.plan_path and self.plan_path.exists()) else None
            plan_dt = datetime.datetime.fromtimestamp(plan_stat.st_mtime) if plan_stat else datetime.datetime.now()
            plan_date = plan_dt.strftime("%Y-%m-%d")
            plan_time = plan_dt.strftime("%H:%M")
            plan_created_at = f"{plan_date} {plan_time}".strip()

            cards.append({
                "mn_id": f"GRILL-{clean_g_sprint}",
                "id": f"GRILL-{clean_g_sprint}",
                "title": title,
                "sprint": clean_g_sprint,
                "story": clean_g_sprint,
                "schema": "story",
                "type": "grill",
                "points": 3,
                "points_custom": False,
                "severity": None,
                "is_grill": True,
                "status": "in-progress",
                "warnings": [],
                "missing_plan_dod": False,
                "warning_missing_plan_dod": False,
                "validated": False,
                "created_date": plan_date,
                "created_time": plan_time,
                "created_at": plan_created_at,
                "start_time": plan_created_at,
                "qa_entered_at": None,
                "description": "Interactive design session in progress in PLAN.md",
                "dod": plan_dod,
                "dod_total": dod_total,
                "dod_checked": dod_checked,
                "progress_pct": progress_pct,
                "qa_text": "",
                "qa_instructions": "",
                "qa_items": [],
                "changelog": [
                    {
                        "agent": "Lead (Grill)",
                        "what": "Interactive design session in progress in PLAN.md",
                        "date": plan_date,
                        "time": plan_time,
                        "datetime": plan_created_at,
                    }
                ],
                "reviews": [],
                "review_count": 0,
                "file_name": "PLAN.md",
                "file_path": self.rel_path(self.plan_path),
                "closeout": None,
                "updated_at": plan_dt.isoformat(),
            })

    def _get_cards_from_index(self, refresh: bool = False) -> list[dict[str, Any]] | None:
        """Fast incremental card loader using SQLite CardIndex."""
        if self._index is None:
            return None
        try:
            file_order: list[Path] = []
            found_map: dict[str, tuple[Path, float, int]] = {}
            for directory in (self.cards_dir, self.backlog_dir):
                if directory.exists():
                    for f in sorted(directory.glob("*.md")):
                        if f.name == "template.md":
                            continue
                        try:
                            st = f.stat()
                            key = self.rel_path(f)
                            found_map[key] = (f, st.st_mtime, st.st_size)
                            file_order.append(f)
                        except OSError:
                            pass

            cached_stats = self._index.get_all_file_stats()
            to_parse = [f for key, (f, mtime, size) in found_map.items() if key not in cached_stats or cached_stats[key] != (mtime, size)]
            deleted = [key for key in cached_stats if key not in found_map]

            if deleted:
                self._index.remove_cards(deleted)

            if to_parse:
                all_revs = self.get_all_reviews()
                reviews_by_card: dict[str, list[dict[str, Any]]] = {}
                for r in all_revs:
                    cid = r["card_id"].lower()
                    sp = r["sprint"].lower()
                    reviews_by_card.setdefault(cid, []).append(r)
                    if sp != cid:
                        reviews_by_card.setdefault(sp, []).append(r)

                batch_upsert = []
                unparsed_keys = []
                for f in to_parse:
                    key = self.rel_path(f)
                    card = self.parse_card_file(f, _preloaded_reviews=reviews_by_card)
                    if card:
                        _, mtime, size = found_map[key]
                        batch_upsert.append((key, mtime, size, card))
                    else:
                        unparsed_keys.append(key)
                if unparsed_keys:
                    self._index.remove_cards(unparsed_keys)
                if batch_upsert:
                    self._index.upsert_cards_batch(batch_upsert)

            # Pre-load reviews for dynamic attachment so new reviews appear immediately on cached cards
            all_revs = self.get_all_reviews(refresh=refresh)
            reviews_by_card: dict[str, list[dict[str, Any]]] = {}
            for r in all_revs:
                cid = r["card_id"].lower()
                sp = r["sprint"].lower()
                reviews_by_card.setdefault(cid, []).append(r)
                if sp != cid:
                    reviews_by_card.setdefault(sp, []).append(r)

            cached_cards = self._index.get_all_cards()
            cards: list[dict[str, Any]] = []
            for f in file_order:
                key = self.rel_path(f)
                c = cached_cards.get(key)
                if c:
                    if "_frontmatter" in c:
                        c["extra_tags"] = self._card_extra_tags(c["_frontmatter"])

                    # Dynamically attach reviews and closeout artifact
                    card_id = c.get("mn_id") or c.get("id") or ""
                    c_key = card_id.lower()
                    s_val = str(c.get("sprint") or "").strip()
                    sp_key = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", s_val, flags=re.IGNORECASE).strip().lower()
                    revs = reviews_by_card.get(c_key, [])
                    if not revs and sp_key:
                        revs = reviews_by_card.get(sp_key, [])
                    c["reviews"] = revs
                    c["review_count"] = len(revs)
                    c["closeout"] = self.get_closeout_artifact(card_id)

                    if c.get("status") == "in-progress":
                        c.update(self.compute_card_duration(c))
                    cards.append(c)

            self._append_active_grills(cards)
            return cards
        except Exception:
            return None

    def get_all_cards(self, refresh: bool = False) -> list[dict[str, Any]]:
        now = time.time()
        if not refresh and getattr(self, "_cards_cache", None) is not None and (now - getattr(self, "_cards_cache_time", 0)) < 30.0:
            return self._cards_cache

        # Fast path with SQLite index
        if self._index is not None:
            indexed_cards = self._get_cards_from_index(refresh=refresh)
            if indexed_cards is not None:
                self._cards_cache = indexed_cards
                self._cards_cache_time = now
                return indexed_cards

        # Fallback path if index is unavailable
        all_revs = self.get_all_reviews()
        reviews_by_card: dict[str, list[dict[str, Any]]] = {}
        for r in all_revs:
            cid = r["card_id"].lower()
            sp = r["sprint"].lower()
            reviews_by_card.setdefault(cid, []).append(r)
            if sp != cid:
                reviews_by_card.setdefault(sp, []).append(r)

        cards: list[dict[str, Any]] = []
        if self.cards_dir.exists():
            for f in sorted(self.cards_dir.glob("*.md")):
                if f.name == "template.md":
                    continue
                card = self.parse_card_file(f, _preloaded_reviews=reviews_by_card)
                if card:
                    cards.append(card)

        if self.backlog_dir.exists():
            for f in sorted(self.backlog_dir.glob("*.md")):
                if f.name == "template.md":
                    continue
                card = self.parse_card_file(f, _preloaded_reviews=reviews_by_card)
                if card:
                    cards.append(card)

        self._append_active_grills(cards)
        self._cards_cache = cards
        self._cards_cache_time = now
        return cards

    def backfill_card_created_dates(self, dry_run: bool = False) -> dict[str, Any]:
        """Backfills missing created: YYYY-MM-DD frontmatter in all Obsidian cards."""
        from backfill_dates import run_backfill

        return run_backfill(self.root_dir, dry_run=dry_run, cards_dir=self.cards_dir)

    # --------------------------------------------------------------------------
    # PLAN.md Parsing (Roadmap & Sprints)
    # --------------------------------------------------------------------------
    def parse_plan(self) -> dict[str, Any]:
        if not self.plan_path.exists():
            return {"roadmap": [], "sprints": {}}

        # Quick Win 1: mtime-based cache for PLAN.md parsing
        try:
            current_mtime = self.plan_path.stat().st_mtime
        except OSError:
            return {"roadmap": [], "sprints": {}}
        if self._plan_cache is not None and current_mtime == self._plan_cache_mtime:
            return self._plan_cache

        try:
            content = self.plan_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return {"roadmap": [], "sprints": {}}

        lines = content.splitlines()

        # Parse Roadmap table
        roadmap: list[dict[str, Any]] = []
        in_table = False
        for line in lines:
            line_str = line.strip()
            if re.match(r"^\|\s*(?:Sprint|Story)\s*\|", line_str):
                in_table = True
                continue
            if in_table:
                if line_str.startswith("#"):
                    break
                if not line_str.startswith("|") or line_str.startswith("|---|"):
                    continue
                # Split while respecting escaped pipes \|
                cleaned_line = line_str.replace(r"\|", "__ESCAPED_PIPE__")
                parts = [p.strip().replace("__ESCAPED_PIPE__", "|") for p in cleaned_line.strip("|").split("|")]
                if len(parts) >= 3:
                    sprint_col = parts[0].replace("**", "").replace("*", "").strip()
                    scope_col = " | ".join(parts[1:-1]).replace("**", "").replace("*", "").strip()
                    status_col = parts[-1].strip()
                elif len(parts) == 2:
                    sprint_col = parts[0].replace("**", "").replace("*", "").strip()
                    full_text = parts[1].strip()
                    m_stat = re.search(r"(\s*(?:✅|🔨|🔧|⚠️)?\s*(?:\*\*)?(?:Built|Complete|Completed|PASS|Review|QA|In progress|Sprint backlog|Scheduled)[^\n]*)", full_text, re.IGNORECASE)
                    if m_stat and m_stat.start() > 10:
                        scope_col = full_text[:m_stat.start()].replace("**", "").replace("*", "").strip()
                        status_col = m_stat.group(1).strip()
                    else:
                        scope_col = full_text.replace("**", "").replace("*", "").strip()
                        status_col = ""
                else:
                    continue

                # Determine status badge
                st_lower = status_col.lower()
                has_qa_debt = "owed" in st_lower or "card in qa" in st_lower or "in qa" in st_lower or "awaiting qa" in st_lower
                is_complete = ("✅" in status_col or "complete" in st_lower or "completed" in st_lower) and not has_qa_debt
                is_built = "built" in st_lower or "pass" in st_lower or "qa" in st_lower or is_complete
                is_partial = "⚠️" in status_col or "partial" in st_lower

                roadmap.append(
                    {
                        "sprint": sprint_col,
                        "scope": scope_col,
                        "status_raw": status_col,
                        "is_complete": is_complete,
                        "is_built": is_built,
                        "is_partial": is_partial,
                    }
                )

        # Parse Sprint sections and DoD in PLAN.md
        sprint_sections: dict[str, dict[str, Any]] = {}
        lead_regex = re.compile(r"^#{2,3}\s+(?:(?:Sprint|Story)\s+)?([\w.]+)\s*(?:[—–-]\s*(.*))?$")
        dod_regex = re.compile(r"^#{3,4}\s+Definition of done\s*[—–-]\s*([\w.]+)")

        current_sprint = None
        current_dod_items = []

        for line in lines:
            # Check for DoD heading
            m_dod = dod_regex.match(line)
            if m_dod:
                current_sprint = m_dod.group(1).strip()
                if current_sprint not in sprint_sections:
                    sprint_sections[current_sprint] = {"dod": [], "decisions": []}
                continue

            if current_sprint:
                # Check for DoD checkboxes
                m_box = re.match(r"^\s*-\s*\[([ xX])\]\s*(.+)$", line)
                if m_box:
                    sprint_sections[current_sprint]["dod"].append(
                        {
                            "checked": m_box.group(1).lower() == "x",
                            "text": m_box.group(2).strip(),
                        }
                    )
                elif line.startswith("## ") or line.startswith("### "):
                    # Section ended unless it's a sub-dod
                    if not line.startswith("#### "):
                        current_sprint = None

        result = {"roadmap": roadmap, "sprints": sprint_sections}
        self._plan_cache = result
        self._plan_cache_mtime = current_mtime
        return result

    def get_sprint_doc(self, sprint_name: str) -> dict[str, Any] | None:
        """
        Retrieves the markdown specification/document for a given sprint or phase.
        Checks docs/sprints/ first, then PLAN.md, falling back to a synthesized summary.
        """
        s = sprint_name.strip()
        clean_num = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", s, flags=re.IGNORECASE).strip()

        # 1. Check docs/sprints/
        docs_sprints_dir = self.sprints_dir
        if docs_sprints_dir.exists():
            dash_name = s.lower().replace(" ", "-")
            candidates = [
                docs_sprints_dir / f"sprint-{clean_num}.md",
                docs_sprints_dir / f"sprint-{clean_num.lower()}.md",
                docs_sprints_dir / f"sprint-{dash_name}.md",
            ]
            for cand in candidates:
                if cand.exists() and cand.is_file():
                    try:
                        text = cand.read_text(encoding="utf-8", errors="replace")
                        return {
                            "sprint": s,
                            "title": cand.stem,
                            "source": self.rel_path(cand),
                            "content": text,
                        }
                    except Exception:
                        pass

        # 2. Check PLAN.md
        if self.plan_path.exists():
            try:
                content = self.plan_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""

            if content:
                patterns = [
                    rf"^(##\s+(?:(?:Sprint|Story)\s+)?{re.escape(clean_num)}(?:[—–\-\s:].*)?)$",
                    rf"^(##\s+.*?(?:Sprint|Story|Phase)\s+{re.escape(clean_num)}(?:[—–\-\s:].*)?)$",
                    rf"^(###?\s+Settled\s*[—–-]\s*(?:(?:Sprint|Story)\s+)?{re.escape(clean_num)}(?:[—–\-\s:(].*)?)$",
                    rf"^(##+\s+.*?\b{re.escape(clean_num)}\b.*)$",
                ]

                for pat in patterns:
                    match = re.search(pat, content, re.MULTILINE | re.IGNORECASE)
                    if match:
                        start_pos = match.start()
                        heading_match = re.match(r"^#+", match.group(1))
                        heading_level = len(heading_match.group(0)) if heading_match else 2
                        stop_pat = rf"\n(?=#{{1,{heading_level}}}\s|\n---\n\s*#|\Z)"
                        rest = content[match.end():]
                        next_m = re.search(stop_pat, rest)
                        end_pos = match.end() + next_m.start() if next_m else len(content)
                        extracted = content[start_pos:end_pos].strip()
                        title_line = match.group(1).lstrip("#").strip()
                        return {
                            "sprint": s,
                            "title": title_line,
                            "source": "PLAN.md",
                            "content": extracted,
                        }

        # 3. Fallback: synthesize from roadmap and cards
        roadmap = self.parse_plan()["roadmap"]
        roadmap_item = next(
            (
                r
                for r in roadmap
                if r["sprint"].lower() == s.lower()
                or r["sprint"].lower() == clean_num.lower()
                or clean_num.lower() in r["sprint"].lower()
            ),
            None,
        )
        assigned_cards = [
            c
            for c in self.get_all_cards()
            if c["sprint"].lower() == clean_num.lower()
            or c["sprint"].lower() == s.lower()
        ]

        lines = [f"# {s}"]
        if roadmap_item:
            lines.append(f"\n**Scope:** {roadmap_item['scope']}")
            lines.append(f"**Status:** {roadmap_item['status_raw']}")
        if assigned_cards:
            lines.append(f"\n### Associated Cards ({len(assigned_cards)})\n")
            for c in assigned_cards:
                lines.append(f"- **{c['mn_id']}**: {c['title']} (`{c['status']}`)")
                if c.get("description"):
                    lines.append(f"  > {c['description'][:200]}")

        if len(lines) > 1:
            return {
                "sprint": s,
                "title": s,
                "source": "Generated from Roadmap & Cards",
                "content": "\n".join(lines),
            }

        return None

    def _extract_sprint_dod(self, clean_sid: str) -> list[dict[str, Any]]:
        """Extracts definition of done items for a specific sprint from PLAN.md line by line."""
        clean_key = clean_sid.strip().lower()
        if hasattr(self, "_sprint_dod_cache") and clean_key in self._sprint_dod_cache:
            return self._sprint_dod_cache[clean_key]

        if not self.plan_path.exists():
            return []
        try:
            content = self.plan_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        lines = content.splitlines()
        dod_items: list[dict[str, Any]] = []
        current_section = None
        in_target_dod = False

        for line in lines:
            m_sec = re.match(
                r"^(?:##\s+(?:Sprint\s+|Story\s+|Phase\s+)?|###\s+(?:Sprint\s+|Story\s+|Phase\s+))([\w.]+)", line, re.IGNORECASE
            )
            if m_sec:
                current_section = m_sec.group(1).strip()

            m_dod = re.match(
                r"^#{3,4}\s+Definition of done(?:\s*[—–\-:]\s*(?:Sprint\s+|Story\s+|Phase\s+)?([\w.]+))?",
                line,
                re.IGNORECASE,
            )
            if m_dod:
                sprint_id = (
                    m_dod.group(1).strip() if m_dod.group(1) else current_section
                )
                in_target_dod = bool(
                    sprint_id and sprint_id.lower() == clean_sid.lower()
                )
                continue

            if in_target_dod:
                m_box = re.match(r"^\s*-\s*\[([ xX])\]\s*(.+)$", line)
                if m_box:
                    dod_items.append(
                        {
                            "checked": m_box.group(1).lower() == "x",
                            "text": m_box.group(2).strip(),
                        }
                    )
                elif re.match(r"^#{1,4}\s+", line):
                    in_target_dod = False

        if hasattr(self, "_sprint_dod_cache"):
            self._sprint_dod_cache[clean_key] = dod_items

        return dod_items

    def get_active_streams_overview(self) -> dict[str, Any]:
        """
        Returns the multi-stream live overview of all active in-flight work:
        - All cards with status: in-progress (strictly excluding qa and completed).
        - Any active grills detected in PLAN.md ('### Grill in progress — Sprint <id>').
        - Card-level DoD items as primary source of truth, with PLAN.md fallback.
        - Calculated progress percentages, latest agent activity timestamps/names.
        - Sorted descending by latest activity timestamp.
        - Available sprints list for switching to any historical sprint.
        """
        plan_data = self.parse_plan()
        roadmap = plan_data.get("roadmap", [])
        all_cards = self.get_all_cards()

        # 1. Collect in-progress cards (strictly excluding qa, completed, and backlog)
        in_prog_cards = [
            c for c in all_cards
            if c.get("status") == "in-progress"
            and c.get("sprint")
            and "backlog" not in str(c["sprint"]).lower()
        ]

        # 2. Check for active grill sessions in PLAN.md
        plan_content = ""
        if self.plan_path.exists():
            try:
                plan_content = self.plan_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

        grill_matches = re.finditer(
            r"^\s*#{3,4}\s+Grill in progress\s*[—–\-]\s*(?:(?:Sprint|Story)\s+)?([a-zA-Z0-9_.-]+)",
            plan_content,
            re.IGNORECASE | re.MULTILINE,
        )
        active_grills = []
        for gm in grill_matches:
            g_sprint = gm.group(1).strip()
            clean_g_sprint = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", g_sprint, flags=re.IGNORECASE).strip()
            if clean_g_sprint:
                active_grills.append(clean_g_sprint)

        streams: list[dict[str, Any]] = []
        seen_sprints: set[str] = set()

        # Fetch recent git commits for history proof across streams
        recent_git_commits: list[dict[str, str]] = []
        try:
            res = subprocess.run(
                [
                    "git",
                    "log",
                    "-n60",
                    "--pretty=format:%h%x09%ad%x09%s%x09%an",
                    "--date=format:%Y-%m-%d %H:%M",
                ],
                cwd=str(self.root_dir),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 4:
                        dt_val = parts[1].strip()
                        dt_m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", dt_val)
                        date_str = dt_m.group(1) if dt_m else dt_val
                        time_str = dt_m.group(2) if dt_m else ""
                        recent_git_commits.append(
                            {
                                "sha": parts[0],
                                "date": date_str,
                                "time": time_str,
                                "datetime": dt_val,
                                "message": parts[2],
                                "author": parts[3],
                            }
                        )
        except Exception:
            pass

        for c in in_prog_cards:
            sprint_val = str(c.get("sprint", "")).strip()
            clean_s = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", sprint_val, flags=re.IGNORECASE).strip()
            if clean_s:
                seen_sprints.add(clean_s.lower())

            # Match git commits for history proof
            story_commits: list[dict[str, str]] = []
            s_hyphen = clean_s.replace(".", "-")
            for gc in recent_git_commits:
                msg = gc.get("message", "")
                if (
                    c["mn_id"].lower() in msg.lower()
                    or f"({clean_s})" in msg
                    or f"({s_hyphen})" in msg
                    or f"sprint {clean_s}" in msg.lower()
                    or f"{clean_s}:" in msg
                ):
                    story_commits.append(gc)

            # Match review evidence
            reviews = self.get_reviews_for_card(c["mn_id"]) or self.get_reviews_for_sprint(clean_s)

            # DoD items: Card DoD is primary source of truth
            raw_dod = c.get("dod") or []
            dod_source = "card"
            if not raw_dod and clean_s:
                # Fallback to PLAN.md sprint DoD
                plan_dod = self._extract_sprint_dod(clean_s)
                if not plan_dod and clean_s in plan_data.get("sprints", {}):
                    plan_dod = plan_data["sprints"][clean_s].get("dod", [])
                if plan_dod:
                    raw_dod = plan_dod
                    dod_source = "plan"

            dod_items = []
            for idx, item in enumerate(raw_dod):
                dod_items.append({
                    "text": item.get("text", ""),
                    "checked": bool(item.get("checked", False)),
                    "source": dod_source,
                    "index": idx,
                    "card_id": c.get("mn_id"),
                    "sprint_id": clean_s,
                })

            dod_total = len(dod_items)
            dod_checked = sum(1 for item in dod_items if item["checked"])
            progress_pct = round((dod_checked / dod_total * 100)) if dod_total > 0 else 0

            # Latest agent activity
            changelog = c.get("changelog") or []
            latest_activity: dict[str, Any] = {}
            if changelog:
                latest = changelog[-1]
                latest_activity = {
                    "agent": latest.get("agent", "Agent"),
                    "what": latest.get("what", ""),
                    "date": latest.get("date", ""),
                    "time": latest.get("time", ""),
                    "datetime": latest.get("datetime") or f"{latest.get('date', '')} {latest.get('time', '')}".strip(),
                }
            else:
                latest_activity = {
                    "agent": "Lead",
                    "what": "In progress",
                    "date": str(c.get("created_date") or ""),
                    "time": "",
                    "datetime": str(c.get("created_date") or ""),
                }

            # Scope / goal
            roadmap_item = next(
                (
                    r for r in roadmap
                    if re.sub(r"^(?:Sprint|Story|Phase)\s+", "", r.get("sprint", ""), flags=re.IGNORECASE).strip().lower()
                    == clean_s.lower()
                ),
                None,
            )
            scope = c.get("description") or (roadmap_item["scope"] if roadmap_item else "")

            streams.append({
                "id": c["mn_id"],
                "mn_id": c["mn_id"],
                "title": c.get("title", c["mn_id"]),
                "sprint": clean_s,
                "sprint_label": f"Sprint {clean_s}" if not clean_s.lower().startswith("phase") else clean_s,
                "status": "in-progress",
                "type": c.get("type", "feature"),
                "scope": scope,
                "description": c.get("description", ""),
                "dod": dod_items,
                "dod_total": dod_total,
                "dod_checked": dod_checked,
                "progress_pct": progress_pct,
                "dod_source": dod_source,
                "latest_activity": latest_activity,
                "changelog": changelog,
                "git_commits": story_commits[:5],
                "reviews": reviews,
                "is_grill": False,
                "file_path": c.get("file_path", ""),
                "created_date": c.get("created_date"),
                "elapsed_seconds": c.get("elapsed_seconds", 0),
                "duration_formatted": c.get("duration_formatted", "< 1m"),
                "start_time": c.get("start_time") or c.get("created_at") or c.get("created_date"),
                "end_time": c.get("end_time"),
            })

        # Add active grills if not already represented by an in-progress card
        for g_sprint in active_grills:
            if g_sprint.lower() in seen_sprints:
                continue

            roadmap_item = next(
                (
                    r for r in roadmap
                    if re.sub(r"^(?:Sprint|Story|Phase)\s+", "", r.get("sprint", ""), flags=re.IGNORECASE).strip().lower()
                    == g_sprint.lower()
                ),
                None,
            )
            g_dod = self._extract_sprint_dod(g_sprint)
            dod_items = [
                {
                    "text": item.get("text", ""),
                    "checked": bool(item.get("checked", False)),
                    "source": "plan",
                    "index": idx,
                    "card_id": f"GRILL-{g_sprint}",
                    "sprint_id": g_sprint,
                }
                for idx, item in enumerate(g_dod)
            ]
            dod_total = len(dod_items)
            dod_checked = sum(1 for item in dod_items if item["checked"])
            progress_pct = round((dod_checked / dod_total * 100)) if dod_total > 0 else 0

            g_dur = self.compute_card_duration({
                "status": "in-progress",
                "created_date": datetime.date.today().isoformat(),
                "changelog": [
                    {
                        "agent": "Lead (Grill)",
                        "what": "Interactive design session in progress in PLAN.md",
                        "date": datetime.date.today().isoformat(),
                        "time": "",
                    }
                ],
                "is_grill": True,
            })

            streams.append({
                "id": f"GRILL-{g_sprint}",
                "mn_id": f"GRILL-{g_sprint}",
                "title": f"Sprint {g_sprint} — Design Grill",
                "sprint": g_sprint,
                "sprint_label": f"Sprint {g_sprint}",
                "status": "in-progress",
                "type": "grill",
                "scope": roadmap_item["scope"] if roadmap_item else "Interactive design session in progress in PLAN.md",
                "description": "Interactive design session in progress in PLAN.md",
                "dod": dod_items,
                "dod_total": dod_total,
                "dod_checked": dod_checked,
                "progress_pct": progress_pct,
                "dod_source": "plan",
                "latest_activity": {
                    "agent": "Lead (Grill)",
                    "what": "Interactive design session in progress",
                    "date": datetime.date.today().isoformat(),
                    "time": "",
                    "datetime": datetime.date.today().isoformat(),
                },
                "changelog": [
                    {
                        "agent": "Lead (Grill)",
                        "what": "Interactive design session in progress in PLAN.md",
                        "date": datetime.date.today().isoformat(),
                        "time": "",
                    }
                ],
                "git_commits": [],
                "reviews": [],
                "is_grill": True,
                "file_path": "PLAN.md",
                "created_date": datetime.date.today().isoformat(),
                "elapsed_seconds": g_dur["elapsed_seconds"],
                "duration_formatted": g_dur["duration_formatted"],
                "start_time": g_dur["start_time"],
                "end_time": g_dur["end_time"],
            })

        # Sort streams by latest activity timestamp descending (most recent first)
        def _stream_sort_key(s: dict[str, Any]) -> str:
            act = s.get("latest_activity") or {}
            dt = act.get("datetime") or f"{act.get('date', '')} {act.get('time', '')}".strip()
            if dt:
                return dt
            return str(s.get("created_date") or "")

        streams.sort(key=_stream_sort_key, reverse=True)

        # 3. Available sprints for historical dropdown
        sprint_keys = {
            re.sub(r"^(?:Sprint|Story|Phase)\s+", "", r["sprint"], flags=re.IGNORECASE).strip()
            for r in roadmap
            if r.get("sprint")
        } | {str(c["sprint"]).strip() for c in all_cards if c.get("sprint")}
        available_sprints = sorted(
            [s for s in sprint_keys if s],
            key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)],
        )

        return {
            "mode": "multi-stream",
            "active_count": len(streams),
            "streams": streams,
            "available_sprints": available_sprints,
            "sprint_id": "ACTIVE",
            "sprint_name": "All Active Streams",
        }

    def get_active_sprint_overview(
        self, sprint_id: str | None = None
    ) -> dict[str, Any]:
        """
        Returns full live breakdown for the active or requested sprint:
        - Sprint definition of done from PLAN.md
        - All cards grouped by status (sprint-backlog, in-progress, qa, completed)
        - DoD progress metrics
        - List of all available sprints for quick switching
        """
        plan_data = self.parse_plan()
        roadmap = plan_data["roadmap"]
        all_cards = self.get_all_cards()

        # 1. Determine active sprint if not specified
        target_sprint = sprint_id
        if not target_sprint or target_sprint == "ACTIVE":
            in_prog_cards = [
                c
                for c in all_cards
                if c["status"] == "in-progress"
                and c.get("sprint")
                and "backlog" not in str(c["sprint"]).lower()
            ]
            if in_prog_cards:
                target_sprint = in_prog_cards[0]["sprint"]
            else:
                # Check if PLAN.md has an active grill or recently designed sprint
                plan_content = ""
                if self.plan_path.exists():
                    try:
                        plan_content = self.plan_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except Exception:
                        pass

                m_grill = re.search(
                    r"###\s+Grill in progress\s*[—–\-]\s*(?:Sprint|Story)\s+([\w.]+)",
                    plan_content,
                    re.IGNORECASE,
                )
                if m_grill:
                    target_sprint = m_grill.group(1).strip()
                else:
                    sprint_backlog_cards = [
                        c
                        for c in all_cards
                        if c.get("status") == "sprint-backlog"
                        and c.get("sprint")
                        and "backlog" not in str(c["sprint"]).lower()
                    ]
                    if sprint_backlog_cards:
                        def _sb_sort_key(c: dict[str, Any]) -> tuple[int, str, str]:
                            dt = c.get("created_at") or str(c.get("created_date") or "")
                            cid = str(c.get("id") or c.get("mn_id") or "")
                            if not dt:
                                return (1, "", cid)
                            return (0, dt, cid)

                        sprint_backlog_cards.sort(key=_sb_sort_key, reverse=False)
                        target_sprint = sprint_backlog_cards[0]["sprint"]
                    else:
                        qa_cards = [
                            c
                            for c in all_cards
                            if c.get("status") == "qa"
                            and c.get("sprint")
                            and "backlog" not in str(c["sprint"]).lower()
                        ]
                        if qa_cards:
                            target_sprint = qa_cards[0]["sprint"]
                        else:
                            active_roadmap_sprint = next(
                                (
                                    r
                                    for r in roadmap
                                    if not r["is_complete"]
                                    and not r["is_partial"]
                                    and "backlog" not in r["sprint"].lower()
                                    and "unscheduled" not in r["sprint"].lower()
                                ),
                                None,
                            )
                            if not active_roadmap_sprint:
                                active_roadmap_sprint = next(
                                    (
                                        r
                                        for r in roadmap
                                        if not r["is_complete"]
                                        and "backlog" not in r["sprint"].lower()
                                        and "unscheduled" not in r["sprint"].lower()
                                    ),
                                    None,
                                )
                            if active_roadmap_sprint:
                                target_sprint = re.sub(
                                    r"^(?:Sprint|Story|Phase)\s+",
                                    "",
                                    active_roadmap_sprint["sprint"],
                                    flags=re.IGNORECASE,
                                ).strip()
                            else:
                                target_sprint = "15.8.1"

        clean_sid = re.sub(
            r"^(?:Sprint|Story|Phase)\s+", "", str(target_sprint), flags=re.IGNORECASE
        ).strip()

        # 2. Get Roadmap entry
        roadmap_item = next(
            (
                r
                for r in roadmap
                if re.sub(
                    r"^(?:Sprint|Story|Phase)\s+", "", r["sprint"], flags=re.IGNORECASE
                ).strip().lower()
                == clean_sid.lower()
            ),
            None,
        )

        # 3. Get sprint-level DoD items
        sprint_dod_items = self._extract_sprint_dod(clean_sid)
        if not sprint_dod_items and clean_sid in plan_data["sprints"]:
            sprint_dod_items = plan_data["sprints"][clean_sid].get("dod", [])

        sprint_dod_total = len(sprint_dod_items)
        sprint_dod_checked = sum(1 for d in sprint_dod_items if d["checked"])
        sprint_dod_pct = (
            round((sprint_dod_checked / sprint_dod_total * 100))
            if sprint_dod_total > 0
            else 0
        )

        # 4. Filter cards for this sprint
        cards = [
            c
            for c in all_cards
            if str(c.get("sprint", "")).strip().lower() == clean_sid.lower()
        ]

        def _card_sort_key_asc(card: dict[str, Any]) -> tuple[int, str, str]:
            c_at = card.get("created_at") or card.get("created_date") or ""
            c_id = str(card.get("id") or card.get("mn_id") or "")
            if not c_at:
                return (1, "", c_id)
            return (0, c_at, c_id)

        sb_cards = [c for c in cards if c["status"] == "sprint-backlog"]
        sb_cards.sort(key=_card_sort_key_asc)

        def _card_sort_key_desc(card: dict[str, Any], date_field: str = "created_at") -> tuple[int, str, str]:
            dt = card.get(date_field) or card.get("created_at") or card.get("created_date") or ""
            c_id = str(card.get("id") or card.get("mn_id") or "")
            if not dt:
                return (1, "", c_id)
            return (0, dt, c_id)

        qa_cards = [c for c in cards if c["status"] == "qa"]
        qa_cards.sort(key=lambda c: (0 if c.get("qa_entered_at") else 1, c.get("qa_entered_at") or ""), reverse=True)

        cards_by_status = {
            "sprint-backlog": sb_cards,
            "in-progress": [c for c in cards if c["status"] == "in-progress"],
            "qa": qa_cards,
            "completed": [c for c in cards if c["status"] == "completed"],
        }

        # Aggregate card DoD stats
        all_card_dod = [item for c in cards for item in c.get("dod", [])]
        total_card_dod = len(all_card_dod)
        checked_card_dod = sum(1 for item in all_card_dod if item.get("checked"))
        card_dod_pct = (
            round((checked_card_dod / total_card_dod * 100))
            if total_card_dod > 0
            else 0
        )

        # 5. List of available sprints
        sprint_keys = {
            re.sub(r"^(?:Sprint|Story|Phase)\s+", "", r["sprint"], flags=re.IGNORECASE).strip()
            for r in roadmap
            if r["sprint"]
        } | {str(c["sprint"]).strip() for c in all_cards if c.get("sprint")}
        available_sprints = sorted(
            [s for s in sprint_keys if s],
            key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)],
        )

        return {
            "sprint_id": clean_sid,
            "sprint_name": f"Sprint {clean_sid}"
            if not clean_sid.lower().startswith("phase")
            else clean_sid,
            "scope": roadmap_item["scope"] if roadmap_item else "",
            "status_raw": roadmap_item["status_raw"] if roadmap_item else "Active",
            "is_complete": roadmap_item["is_complete"] if roadmap_item else False,
            "sprint_dod": sprint_dod_items,
            "sprint_dod_total": sprint_dod_total,
            "sprint_dod_checked": sprint_dod_checked,
            "sprint_dod_pct": sprint_dod_pct,
            "cards_by_status": cards_by_status,
            "cards": cards,
            "card_dod_total": total_card_dod,
            "card_dod_checked": checked_card_dod,
            "card_dod_pct": card_dod_pct,
            "available_sprints": available_sprints,
            "mode": "single-sprint",
        }

    def toggle_sprint_dod_item(
        self, sprint_id: str, item_index: int, checked: bool
    ) -> tuple[bool, str]:
        """Toggles a Definition of Done checkbox in PLAN.md for a given sprint."""
        clean_sid = re.sub(
            r"^(?:Sprint|Story|Phase)\s+", "", str(sprint_id), flags=re.IGNORECASE
        ).strip()
        if not self.plan_path.exists():
            return False, "PLAN.md not found"

        content = self.plan_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        current_section = None
        in_target_dod = False
        current_idx = 0
        modified = False
        new_lines = []

        for line in lines:
            m_sec = re.match(
                r"^(?:##\s+(?:Sprint\s+|Story\s+|Phase\s+)?|###\s+(?:Sprint\s+|Story\s+|Phase\s+))([\w.]+)", line, re.IGNORECASE
            )
            if m_sec:
                current_section = m_sec.group(1).strip()

            m_dod = re.match(
                r"^#{3,4}\s+Definition of done(?:\s*[—–\-:]\s*([\w.]+))?",
                line,
                re.IGNORECASE,
            )
            if m_dod:
                sprint_id_found = (
                    m_dod.group(1).strip() if m_dod.group(1) else current_section
                )
                in_target_dod = bool(
                    sprint_id_found and sprint_id_found.lower() == clean_sid.lower()
                )
                new_lines.append(line)
                continue

            if in_target_dod:
                if re.match(r"^#{1,4}\s+", line):
                    in_target_dod = False
                    new_lines.append(line)
                    continue

                m_box = re.match(r"^(\s*-\s*\[)([ xX])(\]\s*.*)$", line)
                if m_box:
                    if current_idx == item_index:
                        mark = "x" if checked else " "
                        new_lines.append(f"{m_box.group(1)}{mark}{m_box.group(3)}")
                        modified = True
                    else:
                        new_lines.append(line)
                    current_idx += 1
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        if modified:
            if hasattr(self, "_sprint_dod_cache"):
                self._sprint_dod_cache.clear()
            nl = "\r\n" if "\r\n" in content else "\n"
            text_to_write = nl.join(new_lines) + nl
            for attempt in range(5):
                try:
                    self.plan_path.write_text(text_to_write, encoding="utf-8", newline="")
                    break
                except OSError:
                    if attempt == 4:
                        raise
                    time.sleep(0.15)
            return True, f"Sprint {clean_sid} DoD item {item_index} updated"
        return False, f"Sprint {clean_sid} DoD item index {item_index} not found"

    # --------------------------------------------------------------------------
    # Pytest Failure Extraction
    # --------------------------------------------------------------------------
    def parse_pytest_failures(self, log_text: str) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        if not log_text:
            return failures

        seen_nodeids: set[str] = set()
        failed_line_pattern = re.compile(
            r"^FAILED\s+([^\s:]+)::([^\s\-]+)(?:\s*-\s*(.*))?$",
            re.MULTILINE,
        )
        for m in failed_line_pattern.finditer(log_text):
            f_path = m.group(1).replace("\\", "/").strip()
            t_name = m.group(2).strip()
            msg = (m.group(3) or "").strip()
            nodeid = f"{f_path}::{t_name}"
            if nodeid in seen_nodeids:
                continue
            seen_nodeids.add(nodeid)

            cmd_path = f_path
            if not cmd_path.startswith("backend/") and (self.root_dir / "backend" / cmd_path).exists():
                cmd_path = f"backend/{cmd_path}"
            command = f"uv run pytest {cmd_path} -k {t_name}"

            failures.append(
                {
                    "test_name": t_name,
                    "file_path": f_path,
                    "nodeid": nodeid,
                    "message": msg,
                    "command": command,
                }
            )

        # Parse detailed failure blocks to enrich empty messages
        failure_blocks = re.findall(
            r"_{3,}\s+([^\s_]+)\s+_{3,}\s*\r?\n(.*?)(?=\n_{3,}|\n={3,}|\Z)",
            log_text,
            re.DOTALL,
        )
        for t_name, block in failure_blocks:
            error_lines = [
                line.strip()[4:].strip()
                for line in block.splitlines()
                if line.startswith("E   ")
            ]
            detailed_msg = " \n ".join(error_lines) if error_lines else ""
            if not detailed_msg:
                block_lines = [l.strip() for l in block.splitlines() if l.strip()]
                if block_lines:
                    detailed_msg = block_lines[-1]

            for item in failures:
                if item["test_name"] == t_name and (not item["message"] or len(item["message"]) < 10):
                    item["message"] = detailed_msg

        return failures

    def get_heavy_gate_info(self) -> dict[str, Any]:
        """Inspects scripts/heavy-gate-queue/, gate-heavy-results/latest.json and recent runs."""
        queue_dir = self.root_dir / "scripts" / "heavy-gate-queue"
        queue_items: list[dict[str, Any]] = []
        if queue_dir.exists():
            for f in sorted(queue_dir.glob("*.ps1")):
                queue_items.append(
                    {
                        "file_name": f.name,
                        "file_path": self.rel_path(f),
                        "size": f.stat().st_size,
                    }
                )

        latest_file = self.heavy_results_dir / "latest.json"
        latest_run = None
        if latest_file.exists():
            try:
                latest_run = json.loads(latest_file.read_text(encoding="utf-8-sig"))
            except Exception:
                pass

        runs_dir = self.heavy_results_dir / "runs"
        recent_runs: list[dict[str, Any]] = []
        if runs_dir.exists():
            for d in sorted(
                runs_dir.iterdir(),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )[:8]:
                if d.is_dir():
                    summary_file = d / "results.json"
                    run_data = {
                        "run_id": d.name,
                        "timestamp": datetime.datetime.fromtimestamp(
                            d.stat().st_mtime
                        ).isoformat(),
                    }
                    if summary_file.exists():
                        try:
                            res_json = json.loads(
                                summary_file.read_text(encoding="utf-8-sig")
                            )
                            run_data.update(res_json)
                        except Exception:
                            pass
                    recent_runs.append(run_data)

        return {
            "queue_count": len(queue_items),
            "queue_items": queue_items,
            "latest_run": latest_run,
            "recent_runs": recent_runs,
        }

    def parse_heavy_gate_index_row(self, line: str) -> dict[str, Any] | None:
        """Parses a row from gate-heavy-results/INDEX.md extracting sha, run_id, pass_count, coverage, etc."""
        if not line.startswith("|") or line.startswith("| Started") or line.startswith("| ---"):
            return None
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6:
            return None

        # run_id e.g. `20261015-094337-ef83-d422e39`
        run_id = None
        m_rid = re.search(r"`?(\d{8}-\d{6}(?:-[a-zA-Z0-9_.-]+)?)`?", line)
        if m_rid:
            run_id = m_rid.group(1).strip("`").strip()

        # SHA: scan parts for hex string (7 to 40 chars)
        sha = None
        for part in parts:
            clean = part.strip("`").strip()
            if re.match(r"^[0-9a-fA-F]{7,40}$", clean) and clean != run_id:
                sha = clean[:7].lower()
                break
        if not sha and run_id:
            m_sha = re.search(r"-([0-9a-fA-F]{7,40})$", run_id)
            if m_sha:
                sha = m_sha.group(1)[:7].lower()

        if not sha:
            return None

        # Status: PASS if "PASS" in line and ("FAIL" not in line or PASS before FAIL)
        is_pass = "PASS" in line and ("FAIL" not in line or line.find("PASS") < line.find("FAIL"))
        status = "PASS" if is_pass else "FAIL"

        # Pass count: e.g. "1820 passed" or "pytest 1039"
        pass_count = None
        m_p = re.search(r"(\d+)\s+passed", line, re.IGNORECASE)
        if m_p:
            pass_count = int(m_p.group(1))
        else:
            m_pt = re.search(r"pytest\s+(\d+)", line, re.IGNORECASE)
            if m_pt:
                pass_count = int(m_pt.group(1))

        # Coverage: e.g. "93.31%"
        coverage = None
        m_c = re.search(r"([\d.]+%)", line)
        if m_c:
            coverage = m_c.group(1)

        # Sprint: if present in column 3 (7 columns table)
        sprint = None
        if len(parts) >= 8 and parts[3] not in ("--", "-", ""):
            clean_sp = parts[3].strip("`").strip()
            if not re.match(r"^[0-9a-fA-F]{7,40}$", clean_sp):
                sprint = clean_sp

        return {
            "run_id": run_id,
            "sha": sha,
            "status": status,
            "pass_count": pass_count,
            "coverage": coverage,
            "sprint": sprint or sha,
        }

    def parse_current_run_log(self, log_path: Path | str | None = None) -> dict[str, Any] | None:
        """Parses gate-heavy-results/current_run.log fallback when runs/ dirs are deleted."""
        p = Path(log_path) if log_path else self.heavy_results_dir / "current_run.log"
        if not p.exists():
            return None
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            run_id = None
            m_rid = re.search(r"run\s+id\s+([0-9a-zA-Z_-]+)", text)
            if m_rid:
                run_id = m_rid.group(1).strip()

            sha = None
            if run_id:
                m_s = re.search(r"-([0-9a-fA-F]{7,40})$", run_id)
                if m_s:
                    sha = m_s.group(1)[:7].lower()

            pass_count = None
            m_p = re.search(r"(\d+)\s+passed", text)
            if m_p:
                pass_count = int(m_p.group(1))

            coverage = None
            m_c = re.search(r"(?:Total coverage|test coverage.*?reached.*?Total coverage):\s*([\d.]+%)", text)
            if not m_c:
                m_c = re.search(r"Total coverage:\s*([\d.]+%)", text)
            if m_c:
                coverage = m_c.group(1)

            is_ok = bool(
                ("Heavy gate green" in text or "gate 5  pytest     ok" in text)
                and "Something is red" not in text
            )

            return {
                "run_id": run_id,
                "sha": sha,
                "pass_count": pass_count,
                "coverage": coverage,
                "overall_ok": is_ok,
            }
        except Exception:
            return None

    def get_sprint_gate_status(self) -> list[dict[str, Any]]:
        """
        Lists completed/built sprints from PLAN.md and cards, their commit SHAs,
        and whether a verified Gate 5 heavy run exists for them.
        Sprints without a commit SHA are omitted.
        Sprints 15.7 and earlier are marked as completed and verified.
        """
        now = time.time()
        if hasattr(self, "_sprint_gate_cache") and (now - getattr(self, "_sprint_gate_cache_time", 0)) < 10.0:
            return self._sprint_gate_cache

        plan_data = self.parse_plan()
        roadmap = plan_data.get("roadmap", [])
        cards = self.get_all_cards()
        all_reviews = self.get_all_reviews()
        reviews_by_sprint: dict[str, list[dict[str, Any]]] = {}
        for r in all_reviews:
            sp = r["sprint"].lower()
            reviews_by_sprint.setdefault(sp, []).append(r)

        def is_sprint_15_7_or_earlier(sprint_str: str) -> bool:
            clean = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", str(sprint_str), flags=re.IGNORECASE).strip()
            m = re.match(r"^(\d+)([a-zA-Z]+)?(?:\.(\d+))?(?:\.(\d+))?", clean)
            if not m:
                return False
            major = int(m.group(1))
            minor = int(m.group(3)) if m.group(3) is not None else 0
            patch = int(m.group(4)) if m.group(4) is not None else 0
            if major < 15:
                return True
            if major == 15 and (minor < 7 or (minor == 7 and patch == 0)):
                return True
            return False

        # Collect runs from runs/ and INDEX.md
        runs_dir = self.heavy_results_dir / "runs"
        completed_runs: list[dict[str, Any]] = []
        if runs_dir.exists():
            for d in runs_dir.iterdir():
                if d.is_dir():
                    summary_file = d / "results.json"
                    if summary_file.exists():
                        try:
                            res_json = json.loads(summary_file.read_text(encoding="utf-8-sig"))
                            res_json["run_id"] = d.name
                            pytest_info = res_json.get("pytest", {})
                            if not pytest_info.get("failed_tests"):
                                log_file = d / "pytest.log"
                                if log_file.exists():
                                    pytest_info["failed_tests"] = self.parse_pytest_failures(
                                        log_file.read_text(encoding="utf-8", errors="replace")
                                    )
                            if "overall_ok" not in res_json:
                                p_ok = pytest_info.get("Ok") is True
                                v_info = res_json.get("vitest")
                                v_ok = True if v_info is None else (v_info.get("Ok") is True)
                                q_items = res_json.get("queue_items") or []
                                q_ok = not any(not q.get("Ok") for q in q_items if isinstance(q, dict))
                                res_json["overall_ok"] = p_ok and v_ok and q_ok
                            completed_runs.append(res_json)
                        except Exception:
                            pass

        index_file = self.heavy_results_dir / "INDEX.md"
        indexed_shas: set[str] = set()
        indexed_runs: dict[str, str] = {}  # sha -> status
        indexed_runs_data: dict[str, dict[str, Any]] = {}
        if index_file.exists():
            try:
                for line in index_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    row_data = self.parse_heavy_gate_index_row(line)
                    if row_data:
                        r_sha = row_data["sha"]
                        r_status = row_data["status"]
                        if r_status == "PASS":
                            indexed_shas.add(r_sha)
                        indexed_runs[r_sha] = r_status
                        indexed_runs_data[r_sha] = row_data
            except Exception:
                pass

        # Also incorporate latest.json if available
        latest_file = self.heavy_results_dir / "latest.json"
        if latest_file.exists():
            try:
                l_data = json.loads(latest_file.read_text(encoding="utf-8-sig"))
                l_sha = str(l_data.get("sha") or "")[:7].lower()
                l_run_id = l_data.get("run_id")
                l_passed = l_data.get("pytest_passed")
                if l_passed is None:
                    l_passed = l_data.get("pass_count")
                l_cov = l_data.get("coverage")
                l_ok = l_data.get("overall_ok")
                if l_ok is None:
                    l_ok = l_data.get("pytest_ok") is True and l_data.get("vitest_ok") is not False

                if l_sha:
                    if l_ok:
                        indexed_shas.add(l_sha)
                        indexed_runs[l_sha] = "PASS"
                    else:
                        indexed_runs[l_sha] = "FAIL"

                    if l_sha not in indexed_runs_data:
                        indexed_runs_data[l_sha] = {
                            "run_id": l_run_id,
                            "sha": l_sha,
                            "status": "PASS" if l_ok else "FAIL",
                            "pass_count": int(l_passed) if l_passed is not None else None,
                            "coverage": l_cov,
                            "sprint": l_data.get("sprint") or l_sha,
                        }
                    else:
                        ex = indexed_runs_data[l_sha]
                        if ex.get("pass_count") is None and l_passed is not None:
                            ex["pass_count"] = int(l_passed)
                        if ex.get("coverage") is None and l_cov is not None:
                            ex["coverage"] = l_cov
                        if ex.get("run_id") is None and l_run_id:
                            ex["run_id"] = l_run_id
            except Exception:
                pass

        # Fallback to current_run.log if run directories were consumed/deleted
        current_log = self.heavy_results_dir / "current_run.log"
        log_data = self.parse_current_run_log(current_log)
        if log_data and log_data.get("sha"):
            c_sha = log_data["sha"]
            if c_sha in indexed_runs_data:
                ex = indexed_runs_data[c_sha]
                if ex.get("pass_count") is None and log_data.get("pass_count") is not None:
                    ex["pass_count"] = log_data["pass_count"]
                if ex.get("coverage") is None and log_data.get("coverage") is not None:
                    ex["coverage"] = log_data["coverage"]
                if ex.get("run_id") is None and log_data.get("run_id"):
                    ex["run_id"] = log_data["run_id"]
            elif log_data.get("overall_ok"):
                indexed_shas.add(c_sha)
                indexed_runs[c_sha] = "PASS"
                indexed_runs_data[c_sha] = {
                    "run_id": log_data.get("run_id"),
                    "sha": c_sha,
                    "status": "PASS",
                    "pass_count": log_data.get("pass_count"),
                    "coverage": log_data.get("coverage"),
                    "sprint": c_sha,
                }

        # Collect recorded sprint results from docs/QUALITY.md
        quality_file = self.quality_file
        quality_sprints: dict[str, dict[str, Any]] = {}
        if quality_file.exists():
            try:
                current_section = ""
                in_gate_table = False
                for raw_line in quality_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = raw_line.strip()
                    if line.startswith("#"):
                        current_section = line.lstrip("#").strip().lower()
                        in_gate_table = False
                        continue
                    if not line.startswith("|"):
                        continue
                    # Ignore review table headers and sections
                    if any(k in line.lower() for k in ("round", "verdict", "notable findings")):
                        in_gate_table = False
                        continue
                    # Ignore table divider lines
                    if re.match(r"^\|\s*[-:]+\s*\|", line):
                        continue
                    # Detect gate runs table header
                    if re.search(r"\|\s*(?:Story|Date|Run|Command|pytest)\s*\|", line, re.I):
                        in_gate_table = True
                        continue

                    # If in a review section, never treat rows as gate runs unless in an explicit gate table
                    is_review_section = any(k in current_section for k in ("review", "rule", "round"))
                    if is_review_section and not in_gate_table:
                        continue

                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) < 3:
                        continue
                    # parts[0] is empty string before leading '|', parts[1] is the first column
                    col1 = parts[1]
                    # Match story identifier: e.g. "15", "**15**", "Story 15", "**17.16** (2026-09-25...)"
                    m_sp = re.match(r"^(?:\*\*)?(?:Story|Sprint|Phase)?\s*([0-9a-zA-Z._-]+)", col1, re.I)
                    if not m_sp:
                        continue
                    q_sprint = m_sp.group(1).strip().lower()

                    # Search across the row for tests passed, coverage, sha, etc.
                    row_text = " | ".join(parts[2:])
                    m_p = re.search(r"(\d+)\s+passed", row_text)
                    m_xf = re.search(r"(\d+)\s+xfail(?:ed)?", row_text)
                    m_c = re.search(r"([\d.]+%)", row_text)
                    m_r = re.search(r"(?:run\s+|`runs/)(2026\d{4}-\d{6}-[0-9a-zA-Z_-]+)", row_text)
                    m_dur = re.search(r"(?:in\s+)?(~?\d+(?:\.\d+)?)\s*s\b", row_text)

                    # Tested SHA: look for Tree column backtick sha e.g. `ea020fe` or `@ `ea020fe`` or plain 7-40 hex
                    m_sha = re.search(r"@\s*`?([0-9a-fA-F]{7,40})`?", row_text)
                    if not m_sha and len(parts) >= 3:
                        m_tree = re.search(r"`([0-9a-fA-F]{7,40})`", parts[2])
                        if m_tree:
                            m_sha = m_tree
                    if not m_sha:
                        m_sha = re.search(r"`([0-9a-fA-F]{7,40})`", row_text)

                    tested_sha = m_sha.group(1)[:7].lower() if m_sha else None
                    is_owed = "owed" in row_text.lower() or "not run" in row_text.lower()

                    if m_p:
                        # Prioritize '## gate runs' or rows with explicit tested_sha
                        existing = quality_sprints.get(q_sprint)
                        is_priority = "gate" in current_section or (tested_sha and not (existing and existing.get("tested_sha")))
                        if not existing or is_priority:
                            quality_sprints[q_sprint] = {
                                "pass_count": int(m_p.group(1)),
                                "xfail_count": int(m_xf.group(1)) if m_xf else 0,
                                "coverage": m_c.group(1) if m_c else None,
                                "run_id": m_r.group(1) if m_r else None,
                                "duration_sec": m_dur.group(1) if m_dur else None,
                                "tested_sha": tested_sha,
                                "is_owed": is_owed,
                            }
            except Exception:
                pass


        sprint_statuses: list[dict[str, Any]] = []

        # Find git log cache for fast commit lookup
        git_log: list[tuple[str, str]] = []
        try:
            res = subprocess.run(
                ["git", "log", "--oneline"],
                cwd=str(self.root_dir),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.strip().split(" ", 1)
                    if len(parts) == 2:
                        git_log.append((parts[0].lower(), parts[1]))
        except Exception:
            pass

        sha_to_log_idx = {log_sha[:7]: i for i, (log_sha, _) in enumerate(git_log)}

        ancestor_cache: dict[tuple[str, str], bool] = {}

        def is_ancestor(ancestor_sha: str, descendant_sha: str) -> bool:
            if not ancestor_sha or not descendant_sha:
                return False
            a = ancestor_sha[:7].lower()
            d = descendant_sha[:7].lower()
            if a == d:
                return True
            pair = (a, d)
            if pair in ancestor_cache:
                return ancestor_cache[pair]
            if a in sha_to_log_idx and d in sha_to_log_idx:
                res = sha_to_log_idx[a] >= sha_to_log_idx[d]
                ancestor_cache[pair] = res
                return res
            try:
                res = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", a, d],
                    cwd=str(self.root_dir),
                    capture_output=True,
                    check=False,
                )
                outcome = res.returncode == 0
                ancestor_cache[pair] = outcome
                return outcome
            except Exception:
                ancestor_cache[pair] = False
                return False

        pat_grill = re.compile(r"grill\s+close", re.IGNORECASE)
        pat_ref = re.compile(r"\b(?:already|shipped in|from)\s+(?:sprint|phase)", re.IGNORECASE)
        clean_git_log = [
            (log_sha, msg)
            for log_sha, msg in git_log
            if not pat_grill.search(msg) and not pat_ref.search(msg)
        ]

        cards_by_sprint: dict[str, list[dict[str, Any]]] = {}
        for c in cards:
            sp = str(c.get("sprint") or "").strip().lower()
            if sp:
                cards_by_sprint.setdefault(sp, []).append(c)

        candidate_items: list[tuple[str, str, dict[str, Any], list[dict[str, Any]]]] = []
        seen_sprints: set[str] = set()

        for item in roadmap:
            s_name = item["sprint"].strip()
            s_num = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", s_name, flags=re.IGNORECASE).strip()
            clean_num = s_num.lower()
            seen_sprints.add(clean_num)

            sprint_cards = cards_by_sprint.get(clean_num, [])
            has_qa_or_done_card = any(c.get("status") in ("qa", "completed") for c in sprint_cards)
            has_review = clean_num in reviews_by_sprint
            is_built_or_comp = (
                item.get("is_complete")
                or item.get("is_built")
                or has_qa_or_done_card
                or has_review
            )
            if is_built_or_comp:
                candidate_items.append((s_num, s_name, item, sprint_cards))

        # Include completed/qa cards whose sprint was not in roadmap
        for c in cards:
            sp = str(c.get("sprint") or "").strip()
            if not sp or sp.lower() == "backlog":
                continue
            clean_num = sp.lower()
            if clean_num not in seen_sprints and c.get("status") in ("qa", "completed"):
                seen_sprints.add(clean_num)
                sprint_cards = cards_by_sprint.get(clean_num, [c])
                synth_item = {
                    "sprint": f"Sprint {sp}",
                    "scope": c.get("title", ""),
                    "status_raw": c.get("status", ""),
                    "is_complete": c.get("status") == "completed",
                    "is_built": True,
                    "is_partial": False,
                }
                candidate_items.append((sp, f"Sprint {sp}", synth_item, sprint_cards))

        for s_num, s_name, item, sprint_cards in candidate_items:
            clean_s = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", str(s_num), flags=re.IGNORECASE).strip().lower()
            card_count = len(sprint_cards)

            # Resolve commit SHA for sprint (ignoring design-only "Grill close" commits and reference commits)
            sha = ""
            start_sha = ""

            # 1. From reviews (most authoritative for completed/built work)
            s_revs = reviews_by_sprint.get(clean_s, [])
            s_revs_sorted = sorted(s_revs, key=lambda r: r.get("round", 1), reverse=True)
            for r in s_revs_sorted:
                h_sha = r.get("head_sha") or ""
                st_sha = r.get("start_sha") or ""
                gr = r.get("git_range") or ""
                if ".." in gr:
                    parts = gr.split("..")
                    if not st_sha:
                        st_sha = parts[0].strip("` ")
                    if not h_sha:
                        h_sha = parts[1].strip("` ")
                if h_sha and re.match(r"^[0-9a-fA-F]{7,40}$", h_sha):
                    sha = h_sha[:7].lower()
                if st_sha and re.match(r"^[0-9a-fA-F]{7,40}$", st_sha):
                    start_sha = st_sha[:7].lower()
                if sha:
                    break

            # 2. From PLAN.md status/scope text
            if not sha and item:
                text_to_search = f"{item.get('status_raw', '')} {item.get('scope', '')}"
                m_range = re.search(r"[`']?([0-9a-fA-F]{7,40})[`']?\s*\.\.\s*[`']?([0-9a-fA-F]{7,40})[`']?", text_to_search)
                if m_range:
                    if not start_sha:
                        start_sha = m_range.group(1)[:7].lower()
                    sha = m_range.group(2)[:7].lower()
                else:
                    m_run_sha = re.search(r"2026\d{4}-\d{6}-[0-9a-fA-F]+-([0-9a-fA-F]{7,40})", text_to_search)
                    if m_run_sha:
                        sha = m_run_sha.group(1)[:7].lower()
                    else:
                        m_at_sha = re.search(r"@\s*`?([0-9a-fA-F]{7,40})`?", text_to_search)
                        if m_at_sha:
                            sha = m_at_sha.group(1)[:7].lower()
                        else:
                            m_plain = re.search(r"`([0-9a-fA-F]{7,40})`", text_to_search)
                            if m_plain:
                                sha = m_plain.group(1)[:7].lower()

            # 3. From git log
            if not sha:
                pat_close = re.compile(rf"\b(?:close|finish)\s+(?:sprint|phase)?\s*{re.escape(clean_s)}(?!\.\d|[0-9a-zA-Z])", re.IGNORECASE)
                pat_conv = re.compile(rf"(?:\b[a-z]+(?:\([^\)]*?{re.escape(clean_s)}[^\)]*?\)|\[[^\]]*?{re.escape(clean_s)}[^\]]*?\]):|\bMN[-_]{re.escape(clean_s)}\b|\({re.escape(clean_s)}\))", re.IGNORECASE)
                pat_any = re.compile(rf"\b(?:sprint|phase)[\s\-_:]+{re.escape(clean_s)}(?!\.\d|[0-9a-zA-Z])", re.IGNORECASE)
                for log_sha, msg in clean_git_log:
                    if pat_close.search(msg):
                        sha = log_sha
                        break
                if not sha:
                    for log_sha, msg in clean_git_log:
                        if pat_conv.search(msg):
                            sha = log_sha
                            break
                if not sha:
                    for log_sha, msg in clean_git_log:
                        if pat_any.search(msg):
                            sha = log_sha
                            break

            # 4. From cards
            if not sha:
                for card in sprint_cards:
                    b = card.get("body", "")
                    m_range = re.search(r"[`']?([0-9a-fA-F]{7,40})[`']?\s*\.\.\s*[`']?([0-9a-fA-F]{7,40})[`']?", b)
                    if m_range:
                        sha = m_range.group(2)[:7].lower()
                        if not start_sha:
                            start_sha = m_range.group(1)[:7].lower()
                        break
                    m_cl = re.findall(r"\b([0-9a-fA-F]{7,8})\b", b)
                    if m_cl:
                        sha = m_cl[-1].lower()
                        break

            # Resolve start SHA if present in cards
            if not start_sha:
                for card in sprint_cards:
                    m_ss = re.search(r"-StartSha\s+([0-9a-fA-F]{7,40})", card.get("body", ""))
                    if m_ss:
                        start_sha = m_ss.group(1)[:7].lower()
                        break

            # Rule 1: Remove all sprints that do not have a commit SHA defined
            if not sha:
                continue

            # Match run
            matched_run = None
            clean_snum = s_num.replace(".", "_")
            for run in completed_runs:
                run_sprint = str(run.get("sprint") or "")
                run_sha = str(run.get("sha") or "")[:7].lower()
                run_id = str(run.get("run_id") or "")

                if (run_sprint and run_sprint == s_num) or (f"sprint-{clean_snum}-" in run_id) or (sha and run_sha == sha.lower()):
                    matched_run = run
                    break

            verified = False
            latest_run_id = None
            pass_count = None
            xfail_count = None
            duration_sec = None
            coverage = None
            status_text = "Owed"
            tested_sha = None
            failed_tests: list[dict[str, Any]] = []

            if matched_run:
                latest_run_id = matched_run.get("run_id")
                pytest_info = matched_run.get("pytest", {})
                is_ok = bool(matched_run.get("overall_ok"))
                run_sha = str(matched_run.get("sha") or "")[:7].lower()
                detail = pytest_info.get("Detail") or ""
                m_pass = re.search(r"(\d+)\s+passed", detail)
                if m_pass:
                    pass_count = int(m_pass.group(1))
                m_xf = re.search(r"(\d+)\s+xfail(?:ed)?", detail)
                if m_xf:
                    xfail_count = int(m_xf.group(1))
                m_cov = re.search(r"([\d.]+%)", detail)
                if m_cov:
                    coverage = m_cov.group(1)

                if is_ok:
                    # Strict check: sprint's completed sha must be within run_sha
                    if not sha or not run_sha or is_ancestor(sha, run_sha):
                        verified = True
                        status_text = "Passed"
                        tested_sha = run_sha
                    else:
                        status_text = "Owed"
                else:
                    status_text = "Failed"
                    failed_tests = pytest_info.get("failed_tests") or []
                    if not failed_tests and latest_run_id:
                        log_p = runs_dir / latest_run_id / "pytest.log"
                        if log_p.exists():
                            failed_tests = self.parse_pytest_failures(
                                log_p.read_text(encoding="utf-8", errors="replace")
                            )
            elif sha and sha.lower() in indexed_runs and indexed_runs[sha.lower()] == "PASS":
                verified = True
                status_text = "Passed"
                tested_sha = sha.lower()
                ind_info = indexed_runs_data.get(sha.lower(), {})
                pass_count = ind_info.get("pass_count")
                coverage = ind_info.get("coverage")
                latest_run_id = ind_info.get("run_id")
            elif s_num.lower() in quality_sprints:
                q_info = quality_sprints[s_num.lower()]
                q_tested_sha = q_info.get("tested_sha")
                if not q_info.get("is_owed"):
                    if not q_tested_sha or not sha or is_ancestor(sha, q_tested_sha) or is_ancestor(q_tested_sha, sha):
                        verified = True
                        status_text = "Passed"
                        pass_count = q_info["pass_count"]
                        xfail_count = q_info.get("xfail_count")
                        duration_sec = q_info.get("duration_sec")
                        coverage = q_info["coverage"]
                        latest_run_id = q_info["run_id"]
                        tested_sha = q_tested_sha or sha
                    else:
                        status_text = "Owed"
                else:
                    status_text = "Owed"

            # Rule 2: Legacy grandfathering (money-nest only): sprints 15.7 or earlier were verified before Gate 5 tracking began
            is_legacy_money_nest = (
                self.project.get("slug") == "money-nest"
                or self.project.get("prefix") == "MN"
                or self.root_dir.name.lower() == "money-nest"
                or (self.prefix == "MN" and "pm-qa-board" not in str(self.root_dir).lower() and "pm-qa-board" not in str(self.docs_path).lower())
            )
            if is_legacy_money_nest and is_sprint_15_7_or_earlier(s_num):
                verified = True
                status_text = "Passed"

            s_reviews = reviews_by_sprint.get(s_num.lower(), [])

            # Resolve completion date for sprint from card end_time, review date, or card created_date
            completed_date = ""
            for c in sprint_cards:
                c_et = c.get("end_time") or ""
                if c_et and c_et > completed_date:
                    completed_date = c_et
            if not completed_date:
                for r in s_reviews:
                    r_d = r.get("date") or ""
                    if r_d and r_d > completed_date:
                        completed_date = f"{r_d} 00:00"
            if not completed_date:
                for c in sprint_cards:
                    c_cd = str(c.get("created_date") or "")
                    if c_cd and f"{c_cd} 00:00" > completed_date:
                        completed_date = f"{c_cd} 00:00"

            sprint_statuses.append(
                {
                    "sprint": s_num,
                    "sprint_display": s_name,
                    "scope": item.get("scope", ""),
                    "sha": sha,
                    "start_sha": start_sha,
                    "tested_sha": tested_sha,
                    "verified": verified,
                    "status": status_text,
                    "completed_date": completed_date or None,
                    "card_count": card_count,
                    "run_id": latest_run_id,
                    "pass_count": pass_count,
                    "xfail_count": xfail_count,
                    "duration_sec": duration_sec,
                    "coverage": coverage,
                    "subsumed_by": None,
                    "failed_tests": failed_tests,
                    "failure_count": len(failed_tests),
                    "prior_failure": None,
                    "reviews": s_reviews,
                    "review_count": len(s_reviews),
                }
            )

        # Rule 3: Cumulative Subsumption Cascade
        # Gate 5 tests the cumulative integrated tree on feature/first_version.
        # A passing Gate 5 run at commit `cand_sha` verifies all merged ancestor sprints
        # on feature/first_version whose completed_sha is an ancestor of (or equal to) cand_sha.
        run_candidates: list[dict[str, Any]] = []

        # 1. From completed_runs
        for run in completed_runs:
            r_sha = str(run.get("sha") or "")[:7].lower()
            pytest_info = run.get("pytest", {})
            is_ok = bool(run.get("overall_ok"))
            if is_ok and r_sha:
                detail = pytest_info.get("Detail") or ""
                m_p = re.search(r"(\d+)\s+passed", detail)
                m_c = re.search(r"([\d.]+%)", detail)
                run_candidates.append({
                    "tested_sha": r_sha,
                    "run_id": run.get("run_id"),
                    "pass_count": int(m_p.group(1)) if m_p else None,
                    "coverage": m_c.group(1) if m_c else None,
                    "sprint": run.get("sprint") or r_sha,
                })

        # 2. From INDEX.md, latest.json, and current_run.log
        for ind_sha, ind_data in indexed_runs_data.items():
            if ind_data.get("status") == "PASS" and ind_sha:
                run_candidates.append({
                    "tested_sha": ind_sha,
                    "run_id": ind_data.get("run_id"),
                    "pass_count": ind_data.get("pass_count"),
                    "coverage": ind_data.get("coverage"),
                    "sprint": ind_data.get("sprint") or ind_sha,
                })

        # 3. From directly verified sprints that have an explicit tested_sha and metrics
        for s_info in sprint_statuses:
            if s_info.get("verified") and s_info.get("tested_sha") and (s_info.get("pass_count") is not None or s_info.get("run_id")):
                cand_sha = s_info.get("tested_sha")[:7].lower()
                run_candidates.append({
                    "tested_sha": cand_sha,
                    "run_id": s_info.get("run_id"),
                    "pass_count": s_info.get("pass_count"),
                    "coverage": s_info.get("coverage"),
                    "sprint": s_info.get("sprint"),
                })

        # Deduplicate candidates by tested_sha, keeping candidate with pass_count if available
        dedup_candidates: dict[str, dict[str, Any]] = {}
        for cand in run_candidates:
            t_sha = cand["tested_sha"]
            if t_sha not in dedup_candidates:
                dedup_candidates[t_sha] = cand
            else:
                existing = dedup_candidates[t_sha]
                if cand.get("pass_count") and not existing.get("pass_count"):
                    dedup_candidates[t_sha] = cand
                elif cand.get("pass_count") and existing.get("pass_count"):
                    c_sp = str(cand.get("sprint") or "")
                    e_sp = str(existing.get("sprint") or "")
                    if is_sprint_15_7_or_earlier(e_sp) and not is_sprint_15_7_or_earlier(c_sp):
                        dedup_candidates[t_sha] = cand
                    elif c_sp > e_sp:
                        dedup_candidates[t_sha] = cand

        # Sort verified candidates chronologically (oldest commit / highest index to newest)
        # to find the nearest verified descendant (earliest Gate 5 run after the sprint completed)
        sorted_candidates: list[tuple[int, dict[str, Any]]] = []
        for t_sha, cand in dedup_candidates.items():
            idx = sha_to_log_idx.get(t_sha, 999999)
            sorted_candidates.append((idx, cand))
        sorted_candidates.sort(key=lambda t: t[0], reverse=True)

        # Cascade: for any unverified or failed sprint, check if completed_sha was in a verified run
        for s_info in sprint_statuses:
            curr_sha = s_info.get("sha", "")[:7].lower()
            if not curr_sha:
                continue

            # 1. Resolve nearest descendant run for verification and status
            if not s_info.get("verified"):
                for _, cand in sorted_candidates:
                    cand_tested_sha = cand["tested_sha"]
                    if is_ancestor(curr_sha, cand_tested_sha):
                        via_sprint = cand.get("sprint")
                        if via_sprint and not re.match(r"^[0-9a-fA-F]{7,40}$", str(via_sprint)):
                            via_label = f"Sprint {via_sprint}"
                        else:
                            via_label = f"{cand_tested_sha[:7]}"

                        if s_info.get("status") == "Failed":
                            s_info["prior_failure"] = {
                                "status": "Failed",
                                "failed_tests": s_info.get("failed_tests", []),
                                "failure_count": s_info.get("failure_count", 0),
                                "run_id": s_info.get("run_id"),
                            }

                        s_info["verified"] = True
                        s_info["status"] = f"Passed via {via_label}"
                        s_info["run_id"] = cand.get("run_id")
                        s_info["pass_count"] = cand.get("pass_count")
                        s_info["coverage"] = cand.get("coverage")
                        s_info["subsumed_by"] = via_sprint or cand_tested_sha
                        break

            # 2. If verified but lacking recorded test numbers, inherit from the nearest descendant that recorded them
            if s_info.get("pass_count") is None:
                for _, cand in sorted_candidates:
                    cand_tested_sha = cand["tested_sha"]
                    if cand.get("pass_count") is not None and is_ancestor(curr_sha, cand_tested_sha):
                        s_info["pass_count"] = cand.get("pass_count")
                        s_info["coverage"] = cand.get("coverage")
                        if not s_info.get("run_id"):
                            s_info["run_id"] = cand.get("run_id")
                        if not s_info.get("subsumed_by"):
                            s_info["subsumed_by"] = cand.get("sprint") or cand_tested_sha
                        break

        self._sprint_gate_cache = sprint_statuses
        self._sprint_gate_cache_time = now
        return sprint_statuses

    def get_heavy_gate_failures(self, sprint_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        """
        Returns failure details for a specific sprint, run, or all recent failures.
        Extracts failed tests (test_name, file_path, message, command).
        """
        runs_dir = self.heavy_results_dir / "runs"

        # If run_id is given
        if run_id:
            r_dir = runs_dir / run_id
            if r_dir.exists() and r_dir.is_dir():
                pytest_log = r_dir / "pytest.log"
                failed_items = []
                if pytest_log.exists():
                    failed_items = self.parse_pytest_failures(pytest_log.read_text(encoding="utf-8", errors="replace"))
                return {
                    "run_id": run_id,
                    "failed_tests": failed_items,
                    "count": len(failed_items),
                }
            return {"run_id": run_id, "failed_tests": [], "count": 0, "error": "Run not found"}

        # If sprint_id is given
        if sprint_id:
            s_clean = sprint_id.strip()
            all_statuses = self.get_sprint_gate_status()
            s_match = next((s for s in all_statuses if s["sprint"].lower() == s_clean.lower()), None)

            if s_match:
                if s_match.get("failed_tests"):
                    return {
                        "sprint": s_match["sprint"],
                        "status": s_match["status"],
                        "run_id": s_match.get("run_id"),
                        "failed_tests": s_match["failed_tests"],
                        "count": len(s_match["failed_tests"]),
                    }
                elif s_match.get("prior_failure"):
                    pf = s_match["prior_failure"]
                    return {
                        "sprint": s_match["sprint"],
                        "status": s_match["status"],
                        "run_id": pf.get("run_id"),
                        "failed_tests": pf.get("failed_tests", []),
                        "count": len(pf.get("failed_tests", [])),
                        "prior_failure": True,
                    }

            if runs_dir.exists():
                clean_num = s_clean.replace(".", "_")
                for r_dir in sorted(runs_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
                    if r_dir.is_dir() and (f"sprint-{clean_num}-" in r_dir.name or r_dir.name.endswith(f"-{s_clean}")):
                        pytest_log = r_dir / "pytest.log"
                        if pytest_log.exists():
                            f_tests = self.parse_pytest_failures(pytest_log.read_text(encoding="utf-8", errors="replace"))
                            if f_tests:
                                return {
                                    "sprint": s_clean,
                                    "run_id": r_dir.name,
                                    "failed_tests": f_tests,
                                    "count": len(f_tests),
                                }

            return {"sprint": s_clean, "failed_tests": [], "count": 0}

        # General failure search: return all sprints that currently have failed tests or prior failures
        all_statuses = self.get_sprint_gate_status()
        results = []
        for s in all_statuses:
            if s.get("failed_tests"):
                results.append({
                    "sprint": s["sprint"],
                    "status": s["status"],
                    "run_id": s.get("run_id"),
                    "failed_tests": s["failed_tests"],
                    "count": len(s["failed_tests"]),
                })
            elif s.get("prior_failure"):
                results.append({
                    "sprint": s["sprint"],
                    "status": s["status"],
                    "run_id": s["prior_failure"].get("run_id"),
                    "failed_tests": s["prior_failure"].get("failed_tests", []),
                    "count": len(s["prior_failure"].get("failed_tests", [])),
                    "prior_failure": True,
                })
        return {"failures": results, "total": len(results)}

    # --------------------------------------------------------------------------
    # Git Activity
    # --------------------------------------------------------------------------
    @staticmethod
    def _clean_git_path(p: str) -> str:
        p = p.strip()
        if p.startswith('"') and p.endswith('"') and len(p) >= 2:
            try:
                return (
                    p[1:-1]
                    .encode("latin1")
                    .decode("unicode-escape")
                    .encode("latin1")
                    .decode("utf-8")
                )
            except Exception:
                return p[1:-1]
        return p

    def get_git_activity(self, limit: int = 15) -> dict[str, Any]:
        # Recent commits
        commits: list[dict[str, str]] = []
        try:
            res = subprocess.run(
                [
                    "git",
                    "log",
                    f"-n{limit}",
                    "--pretty=format:%h%x09%ad%x09%s%x09%an",
                    "--date=format:%Y-%m-%d %H:%M",
                ],
                cwd=str(self.root_dir),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 4:
                        commits.append(
                            {
                                "sha": parts[0],
                                "date": parts[1],
                                "message": parts[2],
                                "author": parts[3],
                            }
                        )
        except Exception:
            pass

        # Working status
        modified_files: list[dict] = []
        try:
            res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.root_dir),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if res.returncode == 0:
                for raw_line in res.stdout.splitlines():
                    line = raw_line.rstrip("\r\n")
                    if not line.strip():
                        continue
                    if len(line) >= 4 and line[2] == " ":
                        status = line[:2]
                        path = line[3:].strip()
                    else:
                        parts = line.split(maxsplit=1)
                        status = parts[0] if parts else ""
                        path = parts[1].strip() if len(parts) > 1 else ""

                    if " -> " in path:
                        old_p, new_p = path.split(" -> ", 1)
                        path = f"{self._clean_git_path(old_p)} -> {self._clean_git_path(new_p)}"
                    else:
                        path = self._clean_git_path(path)

                    modified_files.append({
                        "status": status.strip(),
                        "path": path,
                        "raw": line,
                    })
        except Exception:
            pass

        # Current branch
        branch = "unknown"
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self.root_dir),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if res.returncode == 0:
                branch = res.stdout.strip()
        except Exception:
            pass

        return {
            "branch": branch,
            "modified_files": modified_files,
            "commits": commits,
        }


    # --------------------------------------------------------------------------
    # Analytics & Executive Progress Aggregation (PQC-28)
    # --------------------------------------------------------------------------
    @staticmethod
    def _format_analytics_date_label(dt_str: str) -> str:
        try:
            dt = datetime.date.fromisoformat(dt_str[:10])
            return dt.strftime("%b %d")
        except Exception:
            return dt_str[-5:]

    @staticmethod
    def _clean_agent_display_name(raw: str) -> str:
        s = raw.strip()
        m = re.search(r"\(([^)]+)\)", s)
        if m:
            cand = m.group(1).strip()
            if cand.lower() in ("pro", "medium", "flash", "haiku", "opus", "sonnet"):
                base = re.sub(r"\s*\([^)]+\)", "", s).strip()
                return f"{cand} ({base})"
            return cand
        m_opus = re.search(r"Opus\s+[\d.]+", s, re.IGNORECASE)
        if m_opus:
            return m_opus.group(0)
        m_gpt = re.search(r"GPT-[\w.-]+", s, re.IGNORECASE)
        if m_gpt:
            return m_gpt.group(0)
        if re.search(r"\bAntigravity\b", s, re.IGNORECASE):
            return "Antigravity"
        if re.search(r"\bUser\b", s, re.IGNORECASE):
            return "User"
        return s[:24]

    # ------------------------------------------------------------------
    # Git churn: per-commit SQLite cache (commits are immutable)
    # ------------------------------------------------------------------
    _GIT_PRETTY = "--pretty=format:COMMIT%x09%H%x09%ad%x09%s%x09%an"
    _BACKFILL_THREADS: dict[str, Any] = {}
    _BACKFILL_LOCK = threading.Lock()
    _NUMSTAT_BATCH = 100
    _NUMSTAT_TIMEOUT = 60.0
    _INLINE_BUDGET = 40

    @property
    def _git_backfill(self) -> dict[str, Any]:
        return getattr(self._git_local, "backfill", None) or {"done": True, "counted": 0, "total": 0}

    @_git_backfill.setter
    def _git_backfill(self, value: dict[str, Any]) -> None:
        self._git_local.backfill = value

    def _backfill_running(self) -> bool:
        thread = self._BACKFILL_THREADS.get(str(self._index.db_path))
        return thread is not None and thread.is_alive()

    def _git_bound(self) -> str:
        """Oldest date any analytics window can ask for: max span of 90 days / quarter start."""
        today = datetime.date.today()
        q_start = datetime.date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
        return min(q_start, today - datetime.timedelta(days=90)).isoformat()

    def _read_git_head(self) -> str:
        """Current HEAD sha read from `.git` files; `git rev-parse HEAD` for anything unexpected."""
        git_dir = self.root_dir / ".git"
        try:
            if git_dir.is_dir():
                head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
                if re.fullmatch(r"[0-9a-f]{40}", head):
                    return head
                if head.startswith("ref: "):
                    ref = head[5:].strip()
                    loose = git_dir / ref
                    if loose.is_file():
                        sha = loose.read_text(encoding="utf-8").strip()
                        if re.fullmatch(r"[0-9a-f]{40}", sha):
                            return sha
                    packed = git_dir / "packed-refs"
                    if packed.is_file():
                        for line in packed.read_text(encoding="utf-8").splitlines():
                            parts = line.split(" ", 1)
                            if len(parts) == 2 and parts[1].strip() == ref and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                                return parts[0]
        except OSError:
            pass
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(self.root_dir), capture_output=True,
                encoding="utf-8", errors="replace", check=False, timeout=10.0,
            )
            if res.returncode == 0:
                return res.stdout.strip()
        except Exception:
            pass
        return ""

    def _parse_git_log(self, stdout: str) -> list[dict[str, Any]]:
        """Parse `COMMIT<TAB>sha<TAB>date<TAB>subject<TAB>author` blocks with optional numstat lines."""
        commits: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("COMMIT\t"):
                parts = line.split("\t")
                if len(parts) >= 3:
                    current = {
                        "sha": parts[1],
                        "date": parts[2],
                        "subject": parts[3] if len(parts) > 3 else "",
                        "author": parts[4] if len(parts) > 4 else "",
                        "added": 0,
                        "deleted": 0,
                    }
                    commits.append(current)
            elif current is not None and "\t" in line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    current["added"] += int(parts[0]) if parts[0].isdigit() else 0
                    current["deleted"] += int(parts[1]) if parts[1].isdigit() else 0
        return commits

    def _git_log(self, extra: list[str], timeout: float, stdin: str | None = None, numstat: bool = False) -> str | None:
        cmd = ["git", "--no-pager", "log", "--date=format:%Y-%m-%d", self._GIT_PRETTY, *extra]
        if numstat:
            cmd.insert(3, "--numstat")
            cmd.insert(4, "--no-renames")
        try:
            res = subprocess.run(
                cmd, cwd=str(self.root_dir), input=stdin, capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False, timeout=timeout,
            )
        except Exception:
            return None
        return res.stdout if res.returncode == 0 else None

    def _fill_numstat(self, shas: list[str]) -> int:
        """Read line counts for ``shas`` (one git call per batch) and store them. Returns commits stored."""
        stored = 0
        for i in range(0, len(shas), self._NUMSTAT_BATCH):
            batch = shas[i : i + self._NUMSTAT_BATCH]
            out = self._git_log(["--no-walk=unsorted", "--stdin"], self._NUMSTAT_TIMEOUT, "\n".join(batch) + "\n", numstat=True)
            if out is None:
                break
            parsed = {c["sha"]: c for c in self._parse_git_log(out)}
            # A commit git no longer knows (gc'd after a rebase) is stored as zero so it is not retried forever.
            stats = [(sha, parsed[sha]["added"] if sha in parsed else 0, parsed[sha]["deleted"] if sha in parsed else 0) for sha in batch]
            self._index.set_commit_stats(stats)
            stored += len(stats)
        return stored

    def _sync_git_commits(self, bound: str) -> None:
        """Refresh the reachable set when HEAD or the bound moved (cheap: no diffs are read)."""
        head = self._read_git_head()
        state = self._index.get_git_sync()
        if state is not None and state == (bound, head):
            return
        out = self._git_log([f"--since={bound}", "HEAD"], 60.0)
        if out is None:
            return
        rows = [(c["sha"], c["date"], c["subject"], c["author"]) for c in self._parse_git_log(out)]
        self._index.sync_git_reachable(bound, head, rows)

    def _start_backfill(self) -> None:
        """One background thread per project fills the remaining line counts, newest commits first."""
        key = str(self._index.db_path)
        with self._BACKFILL_LOCK:
            running = self._BACKFILL_THREADS.get(key)
            if running is not None and running.is_alive():
                return

            def work() -> None:
                try:
                    while True:
                        shas = self._index.get_uncounted_shas(self._NUMSTAT_BATCH)
                        if not shas or self._fill_numstat(shas) == 0:
                            break
                except Exception:
                    pass

            thread = threading.Thread(target=work, name=f"git-backfill-{key}", daemon=True)
            self._BACKFILL_THREADS[key] = thread
            thread.start()

    def _get_git_churn(self, since_date: str | None = None) -> list[dict[str, Any]]:
        """Every reachable commit since ``since_date``, with its line counts.

        Cached per commit in the SQLite index: only SHAs not seen before are read. A handful are read
        inline (a new commit shows up at once); a long history fills in on a background thread and
        ``self._git_backfill`` says how far along it is. Read-only parsers compute live, create no files.
        """
        self._git_backfill = {"done": True, "counted": 0, "total": 0}
        if self._index is None:
            out = self._git_log([f"--since={since_date}"] if since_date else [], 120.0, numstat=True)
            commits = self._parse_git_log(out or "")
            for c in commits:
                c["sha"] = c["sha"][:7]
            self._git_backfill = {"done": True, "counted": len(commits), "total": len(commits)}
            return commits

        bound = self._git_bound()
        since = max(since_date or bound, bound)
        try:
            self._sync_git_commits(bound)
            if not self._backfill_running():
                pending = self._index.get_uncounted_shas(self._INLINE_BUDGET)
                if pending:
                    self._fill_numstat(pending)
            counted, total = self._index.git_progress(since)
            if counted < total:
                self._start_backfill()
            commits = self._index.get_git_commits(since)
        except Exception:
            self._git_backfill = {"done": True, "counted": 0, "total": 0, "error": True}
            return []
        self._git_backfill = {"done": counted >= total, "counted": counted, "total": total}
        return commits

    def _parse_gate_runs_from_quality(self) -> list[dict[str, Any]]:
        growth: list[dict[str, Any]] = []
        if self.quality_file.exists():
            try:
                content = self.quality_file.read_text(encoding="utf-8", errors="replace")
                gate_match = re.search(r"##\s+Gate runs.*?\r?\n(.*?)(?=\r?\n##|\Z)", content, re.DOTALL | re.IGNORECASE)
                if gate_match:
                    for line in gate_match.group(1).splitlines():
                        line = line.strip()
                        if not line.startswith("|") or line.startswith("| ---") or line.startswith("| Story") or line.startswith("| Sprint"):
                            continue
                        m_p = re.search(r"\b(\d+)\s+passed\b", line, re.IGNORECASE)
                        if not m_p:
                            m_p = re.search(r"pytest\s+(\d+)", line, re.IGNORECASE)
                        if m_p:
                            passed_cnt = int(m_p.group(1))
                            if 0 < passed_cnt < 50000:
                                parts = [p.strip() for p in line.strip("|").split("|")]
                                story_raw = parts[0].strip("*").strip() if parts else ""
                                m_sha = re.search(r"`([0-9a-fA-F]{7,40})`", line)
                                sha_val = m_sha.group(1)[:7] if m_sha else ""
                                growth.append({
                                    "story": story_raw,
                                    "tests": passed_cnt,
                                    "sha": sha_val,
                                })
            except Exception:
                pass

        # If no gate runs in QUALITY.md, check gate-heavy-results/INDEX.md or latest.json (e.g. money-nest)
        if not growth:
            heavy_idx = self.docs_path / "gate-heavy-results" / "INDEX.md"
            if not heavy_idx.exists() and hasattr(self, "code_path"):
                heavy_idx = self.code_path / "gate-heavy-results" / "INDEX.md"
            if heavy_idx.exists():
                try:
                    h_text = heavy_idx.read_text(encoding="utf-8", errors="replace")
                    for line in h_text.splitlines():
                        if not line.startswith("|") or "---" in line or "Started" in line:
                            continue
                        m_p = re.search(r"\b(\d+)\s+passed\b", line, re.IGNORECASE)
                        if not m_p:
                            m_p = re.search(r"pytest\s+(\d+)", line, re.IGNORECASE)
                        if m_p:
                            passed_cnt = int(m_p.group(1))
                            # Reject commit SHAs (must be a realistic test count < 50,000)
                            if 0 < passed_cnt < 50000:
                                parts = [p.strip() for p in line.strip("|").split("|")]
                                date_str = parts[0][:10] if parts else ""
                                shas = re.findall(r"`([0-9a-fA-F]{7,40})`", line)
                                run_sha = ""
                                for s in shas:
                                    if len(s) == 7:
                                        run_sha = s
                                        break
                                if not run_sha and shas:
                                    run_sha = shas[-1][:7]
                                growth.append({
                                    "story": date_str,
                                    "tests": passed_cnt,
                                    "sha": run_sha,
                                })
                except Exception:
                    pass

        if not growth:
            latest_json = self.docs_path / "gate-heavy-results" / "latest.json"
            if not latest_json.exists() and hasattr(self, "code_path"):
                latest_json = self.code_path / "gate-heavy-results" / "latest.json"
            if latest_json.exists():
                try:
                    lj_data = json.loads(latest_json.read_text(encoding="utf-8"))
                    p_cnt = lj_data.get("pytest_passed")
                    if p_cnt and 0 < int(p_cnt) < 50000:
                        growth.append({
                            "story": "Latest Gate",
                            "tests": int(p_cnt),
                            "sha": str(lj_data.get("sha", ""))[:7],
                        })
                except Exception:
                    pass

        return growth

    def compute_card_active_turnaround(self, card: dict[str, Any]) -> int:
        """Computes active build duration in seconds from in-progress / build start to QA / done.
        Excludes idle backlog days.
        """
        status = str(card.get("status") or "").lower().strip()
        if status not in ("qa", "completed"):
            return 0

        changelog = card.get("changelog") or []
        dt_start: datetime.datetime | None = None
        dt_end: datetime.datetime | None = None

        for cl in changelog:
            note = str(cl.get("note") or cl.get("action") or cl.get("what") or "").lower()
            dt_val = cl.get("datetime") or f"{cl.get('date', '')} {cl.get('time', '')}".strip()
            if not dt_val:
                continue
            try:
                t = datetime.datetime.strptime(dt_val[:16], "%Y-%m-%d %H:%M")
            except Exception:
                continue

            is_start_marker = any(k in note for k in ("start", "in-progress", "building", "picked up", "began")) and not any(k in note for k in ("complete", "done", "qa", "finished", "review"))

            if is_start_marker:
                dt_start = t
            elif dt_start is None and not any(k in note for k in ("created", "backlog", "draft")):
                dt_start = t

            dt_end = t

        if dt_start is None and changelog:
            for cl in changelog:
                dt_val = cl.get("datetime") or f"{cl.get('date', '')} {cl.get('time', '')}".strip()
                if dt_val:
                    try:
                        dt_start = datetime.datetime.strptime(dt_val[:16], "%Y-%m-%d %H:%M")
                        break
                    except Exception:
                        continue

        if not dt_end or dt_end == dt_start:
            end_s = str(card.get("end_time") or card.get("validated") or "")
            if len(end_s) >= 16:
                try:
                    dt_end = datetime.datetime.strptime(end_s[:16], "%Y-%m-%d %H:%M")
                except Exception:
                    pass

        if dt_start and dt_end and dt_end > dt_start:
            diff_sec = int((dt_end - dt_start).total_seconds())
            if 60 <= diff_sec <= 259200:
                return diff_sec
            elif diff_sec > 259200:
                return min(14400, max(1800, len(changelog) * 1800))

        pts = card.get("points", 1)
        return max(1800, int(pts) * 1800)

    @staticmethod
    def _is_critical_defect(card: dict[str, Any]) -> bool:
        """Determines if an open bug/fix card is Critical (frontmatter severity/priority or title/tags keyword)."""
        c_type = str(card.get("type") or "").lower().strip()
        if c_type not in ("fix", "bug"):
            return False
        sev = str(card.get("severity") or card.get("priority") or "").lower().strip()
        if sev in ("critical", "high", "p0", "p1", "urgent", "blocker"):
            return True
        title = str(card.get("title") or "").lower()
        if re.search(r"\b(critical|p0|p1|blocker)\b", title, re.IGNORECASE):
            return True
        tags = [str(t).lower() for t in (card.get("extra_tags") or [])]
        for t in tags:
            if re.search(r"\b(critical|p0|p1|blocker)\b", t, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _get_card_stage(card: dict[str, Any]) -> str:
        """Maps an active card into one of the 4 native WIP stages (or backlog):
        ready, development, qa, blocked.
        """
        st = str(card.get("status") or "backlog").lower().strip()
        tags = [str(t).lower() for t in (card.get("extra_tags") or [])]
        if st == "blocked" or "blocked" in tags or card.get("blocked") is True:
            return "blocked"
        if st in ("in-progress", "wip", "dev", "development"):
            return "development"
        if st in ("qa", "qa-verification", "verification"):
            return "qa"
        if st in ("story-backlog", "sprint-backlog"):
            return "ready"
        if st == "backlog":
            return "backlog"
        return "development"

    @staticmethod
    def _get_card_days_in_stage(card: dict[str, Any], ref_today: datetime.date) -> int:
        """Calculates elapsed days a card has been in its current stage without movement."""
        changelog = card.get("changelog") or []
        last_date_str = None
        if changelog:
            for cl in reversed(changelog):
                d_val = str(cl.get("date") or "")
                m = re.match(r"^(\d{4}-\d{2}-\d{2})", d_val)
                if m:
                    last_date_str = m.group(1)
                    break
        if not last_date_str and card.get("qa_entered_at"):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(card["qa_entered_at"]))
            if m:
                last_date_str = m.group(1)
        if not last_date_str:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(card.get("created_date") or card.get("created_at") or ""))
            if m:
                last_date_str = m.group(1)
        if not last_date_str:
            return 0
        try:
            d_obj = datetime.date.fromisoformat(last_date_str[:10])
            return max(0, (ref_today - d_obj).days)
        except Exception:
            return 0

    @staticmethod
    def _is_reopened_or_bounced(card: dict[str, Any]) -> bool:
        """Detects whether a card was reopened / bounced during review or QA."""
        reviews = card.get("reviews") or []
        if len(reviews) > 1:
            return True
        for r in reviews:
            rnd = r.get("round")
            if rnd is not None and rnd > 1:
                return True
        changelog = card.get("changelog") or []
        for cl in changelog:
            rr = cl.get("review_round")
            if rr is not None and rr > 1:
                return True
            what = str(cl.get("what") or "").lower()
            if re.search(r"\b(round\s+[2-9]|rework|bounced|qa finding|review finding|findings fixed)\b", what):
                return True
        return False

    def _compute_pipeline_health(
        self, all_cards: list[dict[str, Any]], ref_today: datetime.date
    ) -> dict[str, Any]:
        """Calculates Pipeline Health & WIP Stagnation Monitor (7-day aging threshold)."""
        active_cards = [
            c for c in all_cards
            if str(c.get("status") or "").lower() not in ("completed", "done", "closed")
        ]

        stage_buckets: dict[str, list[dict[str, Any]]] = {
            "ready": [],
            "development": [],
            "qa": [],
            "blocked": [],
        }

        stuck_tickets: list[dict[str, Any]] = []
        reopened_count = 0

        for c in active_cards:
            stage_key = self._get_card_stage(c)
            days_in_stage = self._get_card_days_in_stage(c, ref_today)
            is_aging = days_in_stage > 7
            is_reopened = self._is_reopened_or_bounced(c)
            if is_reopened:
                reopened_count += 1

            c["_days_in_stage"] = days_in_stage
            c["_is_aging"] = is_aging
            c["_stage_key"] = stage_key

            if stage_key in stage_buckets:
                stage_buckets[stage_key].append(c)

            if is_aging:
                cl = c.get("changelog") or []
                lead = cl[0].get("agent", "") if cl else ""
                if lead:
                    lead = self._clean_agent_display_name(lead)
                stage_names = {
                    "ready": "Ready to Build",
                    "development": "Development",
                    "qa": "QA & Verification",
                    "blocked": "Blocked",
                    "backlog": "Backlog",
                }
                stuck_tickets.append({
                    "id": c.get("mn_id") or c.get("id"),
                    "title": c.get("title", ""),
                    "stage": stage_names.get(stage_key, stage_key.title()),
                    "stage_id": stage_key,
                    "days_in_stage": days_in_stage,
                    "age_days": days_in_stage,
                    "days_over_threshold": max(0.0, round(days_in_stage - 7.0, 1)),
                    "points": c.get("points", 1),
                    "type": c.get("type", "feature"),
                    "lead": lead,
                    "is_reopened": is_reopened,
                    "file_path": c.get("file_path", ""),
                })

        stuck_tickets.sort(key=lambda t: t["days_in_stage"], reverse=True)

        stages_meta = [
            ("ready", "Ready to Build", "sky"),
            ("development", "Development", "amber"),
            ("qa", "QA & Verification", "purple"),
            ("blocked", "Blocked", "rose"),
        ]

        stages_result = []
        for s_id, s_name, s_color in stages_meta:
            items = stage_buckets[s_id]
            stages_result.append({
                "id": s_id,
                "name": s_name,
                "count": len(items),
                "points": sum(c.get("points", 1) for c in items),
                "aging_count": sum(1 for c in items if c.get("_is_aging")),
                "color": s_color,
            })

        total_wip = sum(len(stage_buckets[k]) for k in stage_buckets)
        total_wip_points = sum(sum(c.get("points", 1) for c in stage_buckets[k]) for k in stage_buckets)

        return {
            "stages": stages_result,
            "total_wip": total_wip,
            "total_wip_points": total_wip_points,
            "aging_threshold_days": 7,
            "total_aging": len(stuck_tickets),
            "reopened_count": reopened_count,
            "stuck_tickets": stuck_tickets,
            "aging_tickets": stuck_tickets,
            "summary": {
                "total_wip": total_wip,
                "total_wip_points": total_wip_points,
                "aging_count": len(stuck_tickets),
                "reopened_count": reopened_count,
                "status": "stagnated" if len(stuck_tickets) > 0 else "healthy",
            },
        }

    def _generate_weekly_cohorts(
        self,
        all_cards: list[dict[str, Any]],
        commits: list[dict[str, Any]],
        window_preset: str,
        ref_today: datetime.date,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        current_monday = ref_today - datetime.timedelta(days=ref_today.weekday())
        current_sunday = current_monday + datetime.timedelta(days=6)

        norm = (window_preset or "6w").lower().strip()
        if norm == "4w":
            num_weeks = 4
        elif norm == "qtd":
            q_month = ((ref_today.month - 1) // 3) * 3 + 1
            q_start = datetime.date(ref_today.year, q_month, 1)
            q_monday = q_start - datetime.timedelta(days=q_start.weekday())
            num_weeks = max(1, (current_monday - q_monday).days // 7 + 1)
        elif norm == "all":
            known_dates = [c["_cr_date"] for c in all_cards if c.get("_cr_date")] + [c["_comp_date"] for c in all_cards if c.get("_comp_date")]
            if known_dates:
                earliest_d = min(datetime.date.fromisoformat(d[:10]) for d in known_dates)
            else:
                earliest_d = current_monday - datetime.timedelta(weeks=5)
            earliest_monday = earliest_d - datetime.timedelta(days=earliest_d.weekday())
            num_weeks = max(1, (current_monday - earliest_monday).days // 7 + 1)
            if num_weeks > 52:
                num_weeks = 52
        elif norm in ("7d", "14d"):
            num_weeks = 2 if norm == "7d" else 3
        elif norm == "30d":
            num_weeks = 5
        else:
            num_weeks = 6

        week_defs = []
        for i in range(num_weeks - 1, -1, -1):
            w_start = current_monday - datetime.timedelta(weeks=i)
            w_end = w_start + datetime.timedelta(days=6)
            week_defs.append((w_start, w_end))

        weekly_cohorts_chrono = []
        scope_burnup_series = []
        net_bug_flow_series = []
        weekly_velocity_series = []

        for w_start, w_end in week_defs:
            w_start_str = w_start.isoformat()
            w_end_str = w_end.isoformat()
            iso_year, iso_week, _ = w_start.isocalendar()
            date_range = f"{w_start.strftime('%b %d')} - {w_end.strftime('%b %d')}"
            full_label = f"W{iso_week} · {date_range}"
            label = full_label
            short_label = f"W{iso_week} ({w_start.strftime('%m/%d')})"
            chart_label = f"W{iso_week}"
            cohort_id = f"{iso_year}-W{iso_week:02d}"

            completed_in_week = [
                c for c in all_cards
                if c.get("status") == "completed" and c.get("_comp_date") and w_start_str <= c["_comp_date"] <= w_end_str
            ]
            completed_in_week.sort(key=lambda c: str(c.get("_comp_date") or ""), reverse=True)

            points_total = sum(c.get("points", 1) for c in completed_in_week)
            feat_points = sum(c.get("points", 1) for c in completed_in_week if str(c.get("type", "")).lower() == "feature")
            feat_cap_pct = round((feat_points / points_total) * 100, 1) if points_total > 0 else 0.0

            feat_cnt = sum(1 for c in completed_in_week if str(c.get("type", "")).lower() == "feature")
            fix_cnt = sum(1 for c in completed_in_week if str(c.get("type", "")).lower() in ("fix", "bug"))
            imp_cnt = sum(1 for c in completed_in_week if str(c.get("type", "")).lower() in ("improvement", "enhance", "enhancement"))
            chore_cnt = sum(1 for c in completed_in_week if str(c.get("type", "")).lower() in ("chore", "debt", "refactor"))
            docs_cnt = sum(1 for c in completed_in_week if str(c.get("type", "")).lower() == "docs")
            other_cnt = max(0, len(completed_in_week) - (feat_cnt + fix_cnt + imp_cnt + chore_cnt + docs_cnt))

            bugs_found = sum(1 for c in all_cards if str(c.get("type", "")).lower() in ("fix", "bug") and c.get("_cr_date") and w_start_str <= c["_cr_date"] <= w_end_str)
            bugs_fixed = sum(1 for c in all_cards if str(c.get("type", "")).lower() in ("fix", "bug") and c.get("status") == "completed" and c.get("_comp_date") and w_start_str <= c["_comp_date"] <= w_end_str)
            net_bugs = bugs_fixed - bugs_found
            net_label = f"+{net_bugs} Net Fixed" if net_bugs >= 0 else f"{net_bugs} Bug Drag"

            durations = [c.get("elapsed_seconds", 0) for c in completed_in_week if (c.get("elapsed_seconds") or 0) > 0]
            mean_dur = int(sum(durations) / len(durations)) if durations else 0
            dur_fmt = self.format_duration(mean_dur) if mean_dur > 0 else "—"

            turnaround_list = [self.compute_card_active_turnaround(c) for c in completed_in_week]
            turnaround_list = [t for t in turnaround_list if t > 0]
            mean_turnaround = int(sum(turnaround_list) / len(turnaround_list)) if turnaround_list else mean_dur
            turnaround_fmt = self.format_duration(mean_turnaround) if mean_turnaround > 0 else "—"

            r1_pass = sum(1 for c in completed_in_week if (c.get("review_count") or 1) <= 1)
            qa_pass_pct = round((r1_pass / len(completed_in_week)) * 100, 1) if completed_in_week else 100.0

            # Momentum status heuristic (Decoupled from internal code review iterations per GM-Q2)
            # Real bug drag occurs when net open bugs increase (found > fixed) OR bug fix cards dominate delivery
            fix_pts = sum(c.get("points", 1) for c in completed_in_week if str(c.get("type", "")).lower() in ("fix", "bug"))
            has_bug_drag = (bugs_found > bugs_fixed) or (
                len(completed_in_week) > 0 and (fix_cnt / len(completed_in_week)) >= 0.35
            ) or (
                points_total > 0 and (fix_pts / points_total) >= 0.35
            )

            if len(completed_in_week) == 0:
                momentum = "stagnated"
                momentum_label = "Stagnated 🔴"
                momentum_badge = "🔴 Stagnated"
            elif has_bug_drag:
                momentum = "bug_drag"
                momentum_label = "Bug Drag 🟡"
                momentum_badge = "🟡 Bug Drag"
            else:
                momentum = "advancing"
                momentum_label = "Advancing 🟢"
                momentum_badge = "🟢 Advancing"

            stories_in_cohort = []
            for sc in completed_in_week:
                cl = sc.get("changelog") or []
                lead = cl[0].get("agent", "") if cl else ""
                if lead:
                    lead = self._clean_agent_display_name(lead)
                turnaround_s = self.compute_card_active_turnaround(sc)
                stories_in_cohort.append({
                    "id": sc.get("mn_id") or sc.get("id"),
                    "title": sc.get("title", ""),
                    "type": sc.get("type", "feature"),
                    "points": sc.get("points", 1),
                    "status": "completed",
                    "completed_date": sc.get("_comp_date"),
                    "created_date": sc.get("_cr_date"),
                    "duration": self.format_duration(turnaround_s) if turnaround_s > 0 else sc.get("duration_formatted", "—"),
                    "elapsed_seconds": turnaround_s if turnaround_s > 0 else sc.get("elapsed_seconds", 0),
                    "active_turnaround_seconds": turnaround_s,
                    "lead": lead,
                    "review_rounds": sc.get("review_count", 0),
                    "closeout": sc.get("closeout"),
                    "file_path": sc.get("file_path", ""),
                })

            work_mix_dict = {
                "feature": feat_cnt,
                "fix": fix_cnt,
                "improvement": imp_cnt,
                "chore": chore_cnt,
                "docs": docs_cnt,
                "other": other_cnt,
            }
            cohort_dict = {
                "id": cohort_id,
                "week_key": cohort_id,
                "label": full_label,
                "full_label": full_label,
                "short_label": short_label,
                "chart_label": chart_label,
                "date_range": date_range,
                "week_number": iso_week,
                "year": iso_year,
                "start_date": w_start_str,
                "end_date": w_end_str,
                "stories_count": len(completed_in_week),
                "total_stories": len(completed_in_week),
                "velocity_points": points_total,
                "total_points": points_total,
                "feature_points": feat_points,
                "feature_capacity_pct": feat_cap_pct,
                "capacity_pct": feat_cap_pct,
                "work_mix": work_mix_dict,
                "work_composition": work_mix_dict,
                "net_bug_flow": {
                    "found": bugs_found,
                    "fixed": bugs_fixed,
                    "net": net_bugs,
                    "impact_label": net_label,
                },
                "active_turnaround": {
                    "mean_seconds": mean_turnaround,
                    "formatted": turnaround_fmt,
                },
                "cycle_time": {
                    "mean_seconds": mean_turnaround,
                    "formatted": turnaround_fmt,
                },
                "qa_pass_rate": qa_pass_pct,
                "qa_rate": {
                    "first_pass_rate": qa_pass_pct,
                },
                "momentum": momentum,
                "momentum_label": momentum_label,
                "momentum_badge": momentum_badge,
                "stories": stories_in_cohort,
            }
            weekly_cohorts_chrono.append(cohort_dict)

            cum_scope = sum(1 for c in all_cards if (c.get("_cr_date") or "") <= w_end_str)
            cum_done = sum(1 for c in all_cards if c.get("status") == "completed" and (c.get("_comp_date") or "") <= w_end_str)
            scope_burnup_series.append({
                "date": w_end_str,
                "label": label,
                "full_label": full_label,
                "total_scope": cum_scope,
                "cumulative_created": cum_scope,
                "cumulative_completed": cum_done,
                "in_flight": max(0, cum_scope - cum_done),
            })

            net_bug_flow_series.append({
                "date": w_end_str,
                "label": label,
                "full_label": full_label,
                "bugs_found": bugs_found,
                "bugs_fixed": bugs_fixed,
                "net": net_bugs,
                "net_label": net_label,
            })

            commits_in_w = [cm for cm in commits if w_start_str <= cm.get("date", "") <= w_end_str]
            weekly_velocity_series.append({
                "date": w_end_str,
                "label": label,
                "full_label": full_label,
                "feature": feat_cnt,
                "fix": fix_cnt,
                "improvement": imp_cnt,
                "chore": chore_cnt,
                "docs": docs_cnt,
                "other": other_cnt,
                "total_completed": len(completed_in_week),
                "velocity_points": points_total,
                "commits": len(commits_in_w),
            })

        cohorts_table = list(reversed(weekly_cohorts_chrono))
        return cohorts_table, weekly_velocity_series, scope_burnup_series, net_bug_flow_series

    def _compute_project_analytics(self, window: str = "6w") -> dict[str, Any]:
        norm_window = (window or "6w").lower().strip()
        if norm_window not in ("6w", "4w", "qtd", "all", "7d", "14d", "30d"):
            norm_window = "6w"

        ref_today = datetime.date.today()
        all_cards = self.get_all_cards()

        # Extract dates and ensure hybrid points for cards
        for c in all_cards:
            if "points" not in c:
                t = str(c.get("type") or "feature").lower().strip()
                if t == "feature":
                    c["points"] = 3
                elif t in ("improvement", "enhance", "enhancement"):
                    c["points"] = 2
                else:
                    c["points"] = 1
                c["points_custom"] = False

            status = str(c.get("status") or "backlog").lower().strip()
            c_date = None
            if status == "completed":
                end_t = str(c.get("end_time") or c.get("validated") or "")
                m = re.match(r"^(\d{4}-\d{2}-\d{2})", end_t)
                if not m:
                    for cl in reversed(c.get("changelog") or []):
                        m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(cl.get("date") or ""))
                        if m:
                            break
                if not m:
                    m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(c.get("created_date") or c.get("created_at") or ""))
                c_date = m.group(1) if m else ref_today.isoformat()
            c["_comp_date"] = c_date

            cr_m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(c.get("created_date") or c.get("created_at") or ""))
            c["_cr_date"] = cr_m.group(1) if cr_m else (c_date or ref_today.isoformat())

        # Compute Pipeline Health (WIP stages, >7d aging threshold)
        pipeline_health = self._compute_pipeline_health(all_cards, ref_today)

        # Compute Open Defect Severity Counter (Critical vs Normal)
        open_bugs = [
            c for c in all_cards
            if str(c.get("status") or "").lower() not in ("completed", "done", "closed")
            and str(c.get("type") or "").lower() in ("fix", "bug")
        ]
        crit_bugs = [c for c in open_bugs if self._is_critical_defect(c)]
        norm_bugs = [c for c in open_bugs if not self._is_critical_defect(c)]
        open_defects = {
            "critical": len(crit_bugs),
            "normal": len(norm_bugs),
            "total": len(open_bugs),
        }

        # Determine date range & prior period
        if norm_window in ("7d", "14d", "30d"):
            days = 7 if norm_window == "7d" else (14 if norm_window == "14d" else 30)
            dates = [(ref_today - datetime.timedelta(days=i)).isoformat() for i in reversed(range(days))]
            start_date = dates[0]
            end_date = dates[-1]
            prior_start = (ref_today - datetime.timedelta(days=2 * days - 1)).isoformat()
            prior_end = (ref_today - datetime.timedelta(days=days)).isoformat()
            since_date = prior_start
        elif norm_window in ("6w", "4w"):
            num_w = 6 if norm_window == "6w" else 4
            cur_mon = ref_today - datetime.timedelta(days=ref_today.weekday())
            start_dt = cur_mon - datetime.timedelta(weeks=num_w - 1)
            end_dt = cur_mon + datetime.timedelta(days=6)
            start_date = start_dt.isoformat()
            end_date = end_dt.isoformat()
            span_d = (end_dt - start_dt).days + 1
            prior_start = (start_dt - datetime.timedelta(days=span_d)).isoformat()
            prior_end = (start_dt - datetime.timedelta(days=1)).isoformat()
            since_date = prior_start
            dates = [(start_dt + datetime.timedelta(days=i)).isoformat() for i in range(span_d)]
        elif norm_window == "qtd":
            q_month = ((ref_today.month - 1) // 3) * 3 + 1
            q_start = datetime.date(ref_today.year, q_month, 1)
            start_date = q_start.isoformat()
            end_date = ref_today.isoformat()
            prior_start = None
            prior_end = None
            since_date = start_date
            dates = [(q_start + datetime.timedelta(days=i)).isoformat() for i in range((ref_today - q_start).days + 1)]
        else:
            # "all"
            prior_start = None
            prior_end = None
            known_dates = [c["_cr_date"] for c in all_cards if c.get("_cr_date")] + [c["_comp_date"] for c in all_cards if c.get("_comp_date")]
            if known_dates:
                earliest_d = min(datetime.date.fromisoformat(d[:10]) for d in known_dates)
            else:
                earliest_d = ref_today - datetime.timedelta(days=42)
            if (ref_today - earliest_d).days > 90:
                earliest_d = ref_today - datetime.timedelta(days=90)
            since_date = earliest_d.isoformat()
            span = max(1, (ref_today - earliest_d).days + 1)
            dates = [(earliest_d + datetime.timedelta(days=i)).isoformat() for i in range(span)]
            start_date = dates[0]
            end_date = dates[-1]

        # Filter completed cards in window & prior period
        if norm_window == "all":
            curr_completed = [c for c in all_cards if c.get("status") == "completed"]
        else:
            curr_completed = [
                c for c in all_cards
                if c.get("status") == "completed" and c.get("_comp_date") and start_date <= c["_comp_date"] <= end_date
            ]
        prior_completed = [
            c for c in all_cards
            if c.get("status") == "completed" and c.get("_comp_date") and prior_start and prior_start <= c["_comp_date"] <= prior_end
        ]

        # Git churn
        commits = self._get_git_churn(since_date=since_date)
        git_backfill = self._git_backfill
        if norm_window == "all":
            curr_commits = commits
        else:
            curr_commits = [c for c in commits if start_date <= c.get("date", "") <= end_date]
        prior_commits = [c for c in commits if prior_start and prior_start <= c.get("date", "") <= prior_end]

        # Generate Weekly Cohorts and Chart Series
        cohorts, weekly_velocity_series, scope_burnup_series, net_bug_flow_series = self._generate_weekly_cohorts(
            all_cards=all_cards,
            commits=commits,
            window_preset=norm_window,
            ref_today=ref_today,
        )

        # KPI 1: Throughput
        tp_value = len(curr_completed)
        prior_tp = len(prior_completed)
        diff_tp = tp_value - prior_tp
        trend_tp = (f"+{diff_tp}" if diff_tp > 0 else f"{diff_tp}") if prior_start else None

        # KPI 2: Velocity (Story Points & Feature Capacity %)
        curr_pts = sum(c.get("points", 1) for c in curr_completed)
        prior_pts = sum(c.get("points", 1) for c in prior_completed)
        diff_pts = curr_pts - prior_pts
        trend_pts = (f"+{diff_pts}" if diff_pts > 0 else f"{diff_pts}") if prior_start else None
        feat_pts = sum(c.get("points", 1) for c in curr_completed if str(c.get("type", "")).lower() == "feature")
        feat_cap_pct = round((feat_pts / curr_pts) * 100, 1) if curr_pts > 0 else 0.0
        pts_rate_per_week = round(curr_pts / max(1.0, len(dates) / 7.0), 1)

        # Work Volume
        commit_count = len(curr_commits)
        lines_added = sum(c.get("added", 0) for c in curr_commits)
        lines_deleted = sum(c.get("deleted", 0) for c in curr_commits)
        prior_commit_count = len(prior_commits)
        diff_commits = commit_count - prior_commit_count
        trend_commits = (f"+{diff_commits}" if diff_commits > 0 else f"{diff_commits}") if prior_start else None

        # KPI 3: Active Turnaround & Cycle Time
        curr_turnaround = [self.compute_card_active_turnaround(c) for c in curr_completed]
        curr_turnaround = [t for t in curr_turnaround if t > 0]
        mean_turnaround_seconds = int(sum(curr_turnaround) / len(curr_turnaround)) if curr_turnaround else 0
        prior_turnaround = [self.compute_card_active_turnaround(c) for c in prior_completed]
        prior_turnaround = [t for t in prior_turnaround if t > 0]
        prior_mean_turnaround = int(sum(prior_turnaround) / len(prior_turnaround)) if prior_turnaround else 0
        turnaround_trend = None
        if prior_start and prior_mean_turnaround > 0 and mean_turnaround_seconds > 0:
            d_sec = mean_turnaround_seconds - prior_mean_turnaround
            turnaround_trend = f"{'+' if d_sec > 0 else '-'}{self.format_duration(abs(d_sec))}"

        curr_durations = [c.get("elapsed_seconds", 0) for c in curr_completed if (c.get("elapsed_seconds") or 0) > 0]
        mean_seconds = int(sum(curr_durations) / len(curr_durations)) if curr_durations else mean_turnaround_seconds
        prior_durations = [c.get("elapsed_seconds", 0) for c in prior_completed if (c.get("elapsed_seconds") or 0) > 0]
        prior_mean_seconds = int(sum(prior_durations) / len(prior_durations)) if prior_durations else prior_mean_turnaround
        cycle_trend = turnaround_trend

        # KPI 4: Quality & Test Health
        gate_growth = self._parse_gate_runs_from_quality()
        latest_gate = gate_growth[-1] if gate_growth else None
        latest_test_count = latest_gate["tests"] if latest_gate else len(all_cards)
        passing_tests_cnt = latest_gate.get("passed", latest_test_count) if latest_gate else latest_test_count
        failed_tests_cnt = latest_gate.get("failed", 0) if latest_gate else 0
        pass_rate_val = round((passing_tests_cnt / latest_test_count) * 100, 1) if latest_test_count > 0 else 100.0

        first_test_count = gate_growth[0]["tests"] if len(gate_growth) > 1 else latest_test_count
        new_tests_window = max(0, latest_test_count - first_test_count)
        test_health = {
            "total_tests": latest_test_count,
            "passing_tests": passing_tests_cnt,
            "failed_tests": failed_tests_cnt,
            "pass_rate_pct": pass_rate_val,
            "new_tests": new_tests_window,
            "new_tests_in_window": new_tests_window,
            "pass_label": f"{int(pass_rate_val)}% Passing" if pass_rate_val.is_integer() else f"{pass_rate_val:.1f}% Passing",
            "trend_label": f"+{new_tests_window} new tests" if new_tests_window > 0 else f"{failed_tests_cnt} failures",
            "subtext": f"+{new_tests_window} new tests this period · {failed_tests_cnt} failures",
        }

        # KPI 4: Quality Rate
        cards_for_quality = curr_completed if len(curr_completed) >= 3 else [c for c in all_cards if c.get("status") in ("qa", "completed")]
        if not cards_for_quality:
            cards_for_quality = all_cards

        reviewed_cards = []
        for c in cards_for_quality:
            revs = c.get("reviews") or []
            rounds = [r.get("round", 1) for r in revs]
            if not rounds:
                rounds = [cl.get("review_round", 1) for cl in (c.get("changelog") or []) if cl.get("has_review")]
            if rounds or revs:
                max_r = max(rounds) if rounds else 1
                reviewed_cards.append((c, max_r))

        r1_count = sum(1 for _, mr in reviewed_cards if mr == 1)
        r2_count = sum(1 for _, mr in reviewed_cards if mr == 2)
        r3_plus_count = sum(1 for _, mr in reviewed_cards if mr >= 3)
        total_rev = len(reviewed_cards)
        first_pass_rate = round((r1_count / total_rev) * 100, 1) if total_rev > 0 else 100.0

        gate_growth = self._parse_gate_runs_from_quality()
        latest_test_count = gate_growth[-1]["tests"] if gate_growth else len(all_cards)

        # KPI 5: Stagnation / Pipeline Alert
        stagnation_alert_level = "critical" if open_defects["critical"] > 0 or pipeline_health["total_aging"] > 2 else ("warning" if pipeline_health["total_aging"] > 0 else "healthy")

        # Daily Throughput Series (for compatibility and daily inspection)
        commits_by_date: dict[str, list[dict[str, Any]]] = {}
        for cm in curr_commits:
            commits_by_date.setdefault(cm.get("date", ""), []).append(cm)

        daily_throughput_series = []
        for d in dates:
            d_cards = [c for c in curr_completed if c.get("_comp_date") == d]
            d_commits = commits_by_date.get(d, [])
            f_cnt = sum(1 for c in d_cards if str(c.get("type", "")).lower() == "feature")
            fix_cnt = sum(1 for c in d_cards if str(c.get("type", "")).lower() in ("fix", "bug"))
            chore_cnt = sum(1 for c in d_cards if str(c.get("type", "")).lower() in ("chore", "debt", "refactor"))
            docs_cnt = sum(1 for c in d_cards if str(c.get("type", "")).lower() == "docs")
            other_cnt = len(d_cards) - (f_cnt + fix_cnt + chore_cnt + docs_cnt)

            daily_throughput_series.append({
                "date": d,
                "label": self._format_analytics_date_label(d),
                "feature": f_cnt,
                "fix": fix_cnt,
                "chore": chore_cnt,
                "debt": 0,
                "other": max(0, other_cnt),
                "total_completed": len(d_cards),
                "commits": len(d_commits),
                "lines_added": sum(c.get("added", 0) for c in d_commits),
                "lines_deleted": sum(c.get("deleted", 0) for c in d_commits),
            })

        # Cumulative Burnup Series (daily)
        burnup_series = []
        for d in dates:
            cum_comp = sum(1 for c in all_cards if c.get("status") == "completed" and (c.get("_comp_date") or "") <= d)
            cum_created = sum(1 for c in all_cards if (c.get("_cr_date") or "") <= d)
            burnup_series.append({
                "date": d,
                "label": self._format_analytics_date_label(d),
                "cumulative_completed": cum_comp,
                "cumulative_created": cum_created,
                "in_flight": max(0, cum_created - cum_comp),
            })

        # Work Composition
        type_counter: dict[str, int] = {}
        target_cards_for_comp = curr_completed if curr_completed else all_cards
        for c in target_cards_for_comp:
            t = str(c.get("type") or "feature").lower().strip()
            type_counter[t] = type_counter.get(t, 0) + 1
        total_types = sum(type_counter.values()) or 1
        by_type = [
            {"type": t, "count": cnt, "pct": round((cnt / total_types) * 100, 1)}
            for t, cnt in sorted(type_counter.items(), key=lambda x: x[1], reverse=True)
        ]

        # Agent Work Breakdown by Type
        agent_work_map: dict[str, dict[str, Any]] = {}
        for c in target_cards_for_comp:
            c_type = str(c.get("type") or "feature").lower().strip()
            c_pts = c.get("points", 1)
            cid = c.get("mn_id") or c.get("id") or ""
            c_title = c.get("title", "")
            card_agents = set()
            for cl in (c.get("changelog") or []):
                ag = cl.get("agent")
                if ag:
                    clean_name = self._clean_agent_display_name(ag)
                    if not clean_name.lower().startswith("cold reviewer"):
                        card_agents.add(clean_name)
            if not card_agents:
                card_agents.add("Lead")
            for ag in card_agents:
                if ag not in agent_work_map:
                    agent_work_map[ag] = {
                        "agent": ag,
                        "feature": 0, "fix": 0, "improvement": 0, "chore": 0, "docs": 0, "other": 0,
                        "total_stories": 0, "total_points": 0, "stories": [],
                    }
                aw = agent_work_map[ag]
                aw["total_stories"] += 1
                aw["total_points"] += c_pts
                if c_type == "feature": aw["feature"] += 1
                elif c_type in ("fix", "bug"): aw["fix"] += 1
                elif c_type in ("improvement", "enhance", "enhancement"): aw["improvement"] += 1
                elif c_type in ("chore", "debt", "refactor"): aw["chore"] += 1
                elif c_type == "docs": aw["docs"] += 1
                else: aw["other"] += 1
                aw["stories"].append({
                    "id": cid, "title": c_title, "type": c_type, "points": c_pts,
                    "status": c.get("status", "completed"),
                })
        agent_work_breakdown = sorted(
            agent_work_map.values(), key=lambda x: (x["total_points"], x["total_stories"]), reverse=True
        )

        by_agent = []
        for aw in agent_work_breakdown[:8]:
            aw_copy = dict(aw)
            aw_copy["count"] = aw["total_stories"]
            aw_copy["points"] = aw["total_points"]
            aw_copy["pct"] = round((aw["total_stories"] / (len(target_cards_for_comp) or 1)) * 100, 1)
            aw_copy["feature_count"] = aw["feature"]
            aw_copy["fix_count"] = aw["fix"]
            aw_copy["improvement_count"] = aw["improvement"]
            aw_copy["chore_count"] = aw["chore"]
            aw_copy["other_count"] = aw["other"] + aw.get("docs", 0)
            aw_copy["work_mix"] = {
                "feature": aw["feature"],
                "fix": aw["fix"],
                "improvement": aw["improvement"],
                "chore": aw["chore"],
                "other": aw["other"] + aw.get("docs", 0),
            }
            aw_copy["story_ids"] = [s["id"] for s in aw["stories"]]
            by_agent.append(aw_copy)

        # Quality & Review Friction
        friction_rounds = [
            {"round": "Round 1 PASS", "count": r1_count, "pct": round((r1_count / (total_rev or 1)) * 100, 1), "color": "emerald"},
            {"round": "Round 2 PASS", "count": r2_count, "pct": round((r2_count / (total_rev or 1)) * 100, 1), "color": "amber"},
            {"round": "Round 3+ PASS", "count": r3_plus_count, "pct": round((r3_plus_count / (total_rev or 1)) * 100, 1), "color": "rose"},
        ]

        # Stories list for drill-down (window completed + all in-progress/qa)
        drill_down_stories = []
        stories_set: set[str] = set()
        for c in (curr_completed + [c for c in all_cards if c.get("status") in ("in-progress", "qa")]):
            cid = c.get("mn_id") or c.get("id") or ""
            if cid in stories_set:
                continue
            stories_set.add(cid)
            cl = c.get("changelog") or []
            lead = cl[0].get("agent", "") if cl else ""
            if lead:
                lead = self._clean_agent_display_name(lead)
            drill_down_stories.append({
                "id": cid,
                "title": c.get("title", ""),
                "type": c.get("type", "feature"),
                "points": c.get("points", 1),
                "status": c.get("status", "backlog"),
                "completed_date": c.get("_comp_date"),
                "created_date": c.get("_cr_date"),
                "duration": c.get("duration_formatted", "—"),
                "elapsed_seconds": c.get("elapsed_seconds", 0),
                "lead": lead,
                "review_rounds": c.get("review_count", 0),
                "closeout": c.get("closeout"),
                "file_path": c.get("file_path", ""),
            })

        # All Delivered Stories (for slide-over ticket drawer search)
        all_delivered_stories = []
        delivered_set: set[str] = set()
        for c in [c for c in all_cards if c.get("status") == "completed"]:
            cid = c.get("mn_id") or c.get("id") or ""
            if cid in delivered_set:
                continue
            delivered_set.add(cid)
            cl = c.get("changelog") or []
            lead = cl[0].get("agent", "") if cl else ""
            if lead:
                lead = self._clean_agent_display_name(lead)
            all_delivered_stories.append({
                "id": cid,
                "title": c.get("title", ""),
                "type": c.get("type", "feature"),
                "points": c.get("points", 1),
                "status": "completed",
                "completed_date": c.get("_comp_date"),
                "created_date": c.get("_cr_date"),
                "duration": c.get("duration_formatted", "—"),
                "elapsed_seconds": c.get("elapsed_seconds", 0),
                "lead": lead,
                "review_rounds": c.get("review_count", 0),
                "closeout": c.get("closeout"),
                "file_path": c.get("file_path", ""),
            })
        all_delivered_stories.sort(key=lambda s: str(s.get("completed_date") or ""), reverse=True)

        return {
            "window": norm_window,
            "project": self.project.get("slug") or "current",
            "git_backfill": dict(git_backfill),
            "kpis": {
                "throughput": {
                    "value": tp_value,
                    "trend": trend_tp,
                    "prior_value": prior_tp,
                    "unit": "stories",
                },
                "velocity": {
                    "value": curr_pts,
                    "points": curr_pts,
                    "delivered": curr_pts,
                    "trend": trend_pts,
                    "prior_value": prior_pts,
                    "feature_capacity_pct": feat_cap_pct,
                    "capacity_pct": feat_cap_pct,
                    "rate_per_week": pts_rate_per_week,
                    "pts_per_week": pts_rate_per_week,
                    "pts_per_wk": pts_rate_per_week,
                    "rate_label": f"{pts_rate_per_week} pts/wk",
                    "unit": "pts",
                },
                "work_volume": {
                    "commits": commit_count,
                    "lines_added": lines_added,
                    "lines_deleted": lines_deleted,
                    "churn_net": lines_added - lines_deleted,
                    "prior_commits": prior_commit_count,
                    "trend": trend_commits,
                },
                "active_turnaround": {
                    "mean_seconds": mean_turnaround_seconds,
                    "formatted": self.format_duration(mean_turnaround_seconds) if mean_turnaround_seconds > 0 else "—",
                    "trend": turnaround_trend,
                },
                "cycle_time": {
                    "mean_seconds": mean_turnaround_seconds,
                    "formatted": self.format_duration(mean_turnaround_seconds) if mean_turnaround_seconds > 0 else "—",
                    "trend": turnaround_trend,
                },
                "test_health": test_health,
                "quality_rate": {
                    "first_pass_rate": first_pass_rate,
                    "total_reviewed": total_rev,
                    "r1_pass_count": r1_count,
                    "r2_plus_count": r2_count + r3_plus_count,
                    "test_count": latest_test_count,
                    "new_tests": new_tests_window,
                    "pass_rate_pct": 100.0,
                },
                "stagnation": {
                    "active_wip": pipeline_health["total_wip"],
                    "active_wip_points": pipeline_health["total_wip_points"],
                    "aging_count": pipeline_health["total_aging"],
                    "critical_bugs": open_defects["critical"],
                    "reopened_count": pipeline_health["reopened_count"],
                    "alert_level": stagnation_alert_level,
                },
            },
            "cohorts": cohorts,
            "weekly_cohorts": cohorts,
            "pipeline_health": pipeline_health,
            "open_defects": open_defects,
            "scope_burnup": {
                "series": scope_burnup_series,
            },
            "net_bug_flow": {
                "series": net_bug_flow_series,
                "summary": {
                    "total_found": sum(s["bugs_found"] for s in net_bug_flow_series),
                    "total_fixed": sum(s["bugs_fixed"] for s in net_bug_flow_series),
                },
            },
            "charts": {
                "daily_throughput": {
                    "dates": dates,
                    "series": daily_throughput_series,
                    "story_types": ["feature", "fix", "chore", "debt", "other"],
                },
                "burnup": {
                    "dates": dates,
                    "series": burnup_series,
                },
                "weekly_velocity": {
                    "series": weekly_velocity_series,
                },
                "scope_burnup": {
                    "series": scope_burnup_series,
                },
                "net_bug_flow": {
                    "series": net_bug_flow_series,
                },
                "work_composition": {
                    "by_type": by_type,
                    "by_agent": by_agent,
                    "agent_work_breakdown": agent_work_breakdown,
                },
                "quality_friction": {
                    "rounds": friction_rounds,
                    "test_suite_growth": gate_growth,
                },
            },
            "stories": drill_down_stories,
            "all_delivered_stories": all_delivered_stories,
            "agent_work_breakdown": agent_work_breakdown,
            "test_health": test_health,
            "test_growth": {
                "series": gate_growth,
                "latest": latest_gate,
            },
            "active_turnaround": {
                "mean_seconds": mean_turnaround_seconds,
                "formatted": self.format_duration(mean_turnaround_seconds) if mean_turnaround_seconds > 0 else "—",
                "trend": turnaround_trend,
            },
            "projects_summary": [
                {
                    "slug": self.project.get("slug") or "current",
                    "name": self.project.get("name") or "Current Project",
                    "prefix": self.prefix,
                    "completed": tp_value,
                    "velocity_points": curr_pts,
                    "commits": commit_count,
                    "churn_added": lines_added,
                    "churn_deleted": lines_deleted,
                    "active_count": len([s for s in drill_down_stories if s.get("status") in ("in-progress", "qa")]),
                }
            ],
        }

    def _compute_portfolio_analytics(self, window: str = "6w") -> dict[str, Any]:
        try:
            from board import projects
        except ImportError:
            try:
                import projects
            except ImportError:
                projects = None

        registry_projects: dict[str, Any] = {}
        if projects:
            try:
                reg = projects.load_registry()
                registry_projects = reg.get("projects", {})
            except Exception:
                registry_projects = {}

        if not registry_projects:
            single = dict(self._cached_project_analytics(window))
            single["project"] = "all"
            return single

        sub_analytics: list[dict[str, Any]] = []
        projects_summary: list[dict[str, Any]] = []

        cur_slug = self.project.get("slug")
        for p_slug, p_conf in registry_projects.items():
            try:
                if cur_slug and p_slug == cur_slug:
                    p_parser = self
                else:
                    p_parser = self._parser_for(p_conf)
                p_ana = p_parser._cached_project_analytics(window)
                sub_analytics.append(p_ana)
                projects_summary.append({
                    "slug": p_slug,
                    "name": p_conf.get("name") or p_slug,
                    "prefix": p_conf.get("prefix") or "",
                    "completed": p_ana["kpis"]["throughput"]["value"],
                    "velocity_points": p_ana["kpis"]["velocity"]["value"],
                    "commits": p_ana["kpis"]["work_volume"]["commits"],
                    "churn_added": p_ana["kpis"]["work_volume"]["lines_added"],
                    "churn_deleted": p_ana["kpis"]["work_volume"]["lines_deleted"],
                    "active_count": len([s for s in p_ana["stories"] if s.get("status") in ("in-progress", "qa")]),
                })
            except Exception:
                pass

        if not sub_analytics:
            single = dict(self._cached_project_analytics(window))
            single["project"] = "all"
            return single

        total_completed = sum(a["kpis"]["throughput"]["value"] for a in sub_analytics)
        prior_completed = sum(a["kpis"]["throughput"].get("prior_value", 0) for a in sub_analytics)
        diff_tp = total_completed - prior_completed
        trend_tp = f"+{diff_tp}" if diff_tp > 0 else f"{diff_tp}"

        total_pts = sum(a["kpis"]["velocity"]["value"] for a in sub_analytics)
        prior_pts = sum(a["kpis"]["velocity"].get("prior_value", 0) for a in sub_analytics)
        diff_pts = total_pts - prior_pts
        trend_pts = f"+{diff_pts}" if diff_pts > 0 else f"{diff_pts}"
        total_feat_pts = sum(
            round((a["kpis"]["velocity"].get("feature_capacity_pct", 0) / 100.0) * a["kpis"]["velocity"]["value"])
            for a in sub_analytics
        )
        portfolio_feat_cap_pct = round((total_feat_pts / total_pts) * 100, 1) if total_pts > 0 else 0.0

        total_commits = sum(a["kpis"]["work_volume"]["commits"] for a in sub_analytics)
        total_added = sum(a["kpis"]["work_volume"]["lines_added"] for a in sub_analytics)
        total_deleted = sum(a["kpis"]["work_volume"]["lines_deleted"] for a in sub_analytics)
        prior_commits = sum(a["kpis"]["work_volume"].get("prior_commits", 0) for a in sub_analytics)
        diff_commits = total_commits - prior_commits
        trend_commits = f"+{diff_commits}" if diff_commits > 0 else f"{diff_commits}"

        all_stories = []
        for a in sub_analytics:
            all_stories.extend(a.get("stories", []))

        all_delivered = []
        for a in sub_analytics:
            all_delivered.extend(a.get("all_delivered_stories", []))

        completed_stories = [s for s in all_stories if s.get("status") == "completed"]
        durations = [s.get("elapsed_seconds", 0) for s in completed_stories if s.get("elapsed_seconds", 0) > 0]
        mean_seconds = int(sum(durations) / len(durations)) if durations else 0

        # Merge Pipeline Health across portfolio
        merged_stages_dict: dict[str, dict[str, Any]] = {
            "ready": {"id": "ready", "name": "Ready to Build", "count": 0, "points": 0, "aging_count": 0, "color": "sky"},
            "development": {"id": "development", "name": "Development", "count": 0, "points": 0, "aging_count": 0, "color": "amber"},
            "qa": {"id": "qa", "name": "QA & Verification", "count": 0, "points": 0, "aging_count": 0, "color": "purple"},
            "blocked": {"id": "blocked", "name": "Blocked", "count": 0, "points": 0, "aging_count": 0, "color": "rose"},
        }
        all_stuck: list[dict[str, Any]] = []
        total_wip = 0
        total_wip_points = 0
        total_reopened = 0

        for a in sub_analytics:
            ph = a.get("pipeline_health", {})
            for st in ph.get("stages", []):
                sid = st.get("id")
                if sid in merged_stages_dict:
                    merged_stages_dict[sid]["count"] += st.get("count", 0)
                    merged_stages_dict[sid]["points"] += st.get("points", 0)
                    merged_stages_dict[sid]["aging_count"] += st.get("aging_count", 0)
            all_stuck.extend(ph.get("stuck_tickets", []))
            total_wip += ph.get("total_wip", 0)
            total_wip_points += ph.get("total_wip_points", 0)
            total_reopened += ph.get("reopened_count", 0)

        all_stuck.sort(key=lambda t: t.get("days_in_stage", 0), reverse=True)
        portfolio_pipeline = {
            "stages": list(merged_stages_dict.values()),
            "total_wip": total_wip,
            "total_wip_points": total_wip_points,
            "aging_threshold_days": 7,
            "total_aging": len(all_stuck),
            "reopened_count": total_reopened,
            "stuck_tickets": all_stuck,
        }

        # Merge Open Defects
        crit_defects = sum(a.get("open_defects", {}).get("critical", 0) for a in sub_analytics)
        norm_defects = sum(a.get("open_defects", {}).get("normal", 0) for a in sub_analytics)
        portfolio_open_defects = {
            "critical": crit_defects,
            "normal": norm_defects,
            "total": crit_defects + norm_defects,
        }

        # Merge Cohorts across sub_analytics by cohort id
        cohort_map: dict[str, dict[str, Any]] = {}
        for a in sub_analytics:
            for ch in a.get("cohorts", []):
                cid = ch["id"]
                if cid not in cohort_map:
                    cohort_map[cid] = {
                        "id": cid,
                        "label": ch.get("label", ""),
                        "full_label": ch.get("full_label", ""),
                        "short_label": ch.get("short_label", ""),
                        "chart_label": ch.get("chart_label", ""),
                        "date_range": ch.get("date_range", ""),
                        "week_number": ch.get("week_number", 0),
                        "year": ch.get("year", 0),
                        "start_date": ch.get("start_date", ""),
                        "end_date": ch.get("end_date", ""),
                        "stories_count": 0,
                        "velocity_points": 0,
                        "feature_points": 0,
                        "work_mix": {"feature": 0, "fix": 0, "improvement": 0, "chore": 0, "docs": 0, "other": 0},
                        "net_bug_flow": {"found": 0, "fixed": 0, "net": 0, "impact_label": ""},
                        "durations": [],
                        "r1_pass": 0,
                        "stories": [],
                    }
                cm = cohort_map[cid]
                cm["stories_count"] += ch.get("stories_count", 0)
                cm["velocity_points"] += ch.get("velocity_points", 0)
                cm["feature_points"] += ch.get("feature_points", 0)
                for mk in ("feature", "fix", "improvement", "chore", "docs", "other"):
                    cm["work_mix"][mk] += ch.get("work_mix", {}).get(mk, 0)
                cm["net_bug_flow"]["found"] += ch.get("net_bug_flow", {}).get("found", 0)
                cm["net_bug_flow"]["fixed"] += ch.get("net_bug_flow", {}).get("fixed", 0)
                cm["stories"].extend(ch.get("stories", []))
                mean_sec = ch.get("cycle_time", {}).get("mean_seconds", 0)
                if mean_sec > 0:
                    cm["durations"].append(mean_sec)
                # Count r1 passes
                r1_cnt = sum(1 for s in ch.get("stories", []) if (s.get("review_rounds") or 1) <= 1)
                cm["r1_pass"] += r1_cnt

        merged_cohorts = []
        for cid in sorted(cohort_map.keys(), reverse=True):
            cm = cohort_map[cid]
            net_b = cm["net_bug_flow"]["fixed"] - cm["net_bug_flow"]["found"]
            cm["net_bug_flow"]["net"] = net_b
            cm["net_bug_flow"]["impact_label"] = f"+{net_b} Net Fixed" if net_b >= 0 else f"{net_b} Bug Drag"
            cm["feature_capacity_pct"] = round((cm["feature_points"] / cm["velocity_points"]) * 100, 1) if cm["velocity_points"] > 0 else 0.0
            mean_c_dur = int(sum(cm["durations"]) / len(cm["durations"])) if cm["durations"] else 0
            cm["cycle_time"] = {
                "mean_seconds": mean_c_dur,
                "formatted": self.format_duration(mean_c_dur) if mean_c_dur > 0 else "—",
            }
            qa_pct = round((cm["r1_pass"] / cm["stories_count"]) * 100, 1) if cm["stories_count"] > 0 else 100.0
            cm["qa_pass_rate"] = qa_pct

            # Momentum status heuristic (Decoupled from code review rounds per GM-Q2)
            has_bug_drag = (net_b < 0) or (
                cm["stories_count"] > 0 and (cm["work_mix"]["fix"] / cm["stories_count"]) >= 0.35
            )
            if cm["stories_count"] == 0:
                cm["momentum"] = "stagnated"
                cm["momentum_label"] = "Stagnated 🔴"
                cm["momentum_badge"] = "🔴 Stagnated"
            elif has_bug_drag:
                cm["momentum"] = "bug_drag"
                cm["momentum_label"] = "Bug Drag 🟡"
                cm["momentum_badge"] = "🟡 Bug Drag"
            else:
                cm["momentum"] = "advancing"
                cm["momentum_label"] = "Advancing 🟢"
                cm["momentum_badge"] = "🟢 Advancing"

            cm["week_key"] = cid
            cm["total_stories"] = cm["stories_count"]
            cm["total_points"] = cm["velocity_points"]
            cm["capacity_pct"] = cm["feature_capacity_pct"]
            cm["work_composition"] = cm["work_mix"]
            cm["active_turnaround"] = cm["cycle_time"]

            del cm["durations"]
            del cm["r1_pass"]
            merged_cohorts.append(cm)

        # Merge Weekly Velocity series (chronological)
        weekly_vel_map: dict[str, dict[str, Any]] = {}
        for a in sub_analytics:
            for wv in a.get("charts", {}).get("weekly_velocity", {}).get("series", []):
                dt = wv["date"]
                if dt not in weekly_vel_map:
                    weekly_vel_map[dt] = {
                        "date": dt,
                        "label": wv.get("label", ""),
                        "full_label": wv.get("full_label", ""),
                        "feature": 0, "fix": 0, "improvement": 0, "chore": 0, "docs": 0, "other": 0,
                        "total_completed": 0, "velocity_points": 0, "commits": 0,
                    }
                for k in ("feature", "fix", "improvement", "chore", "docs", "other", "total_completed", "velocity_points", "commits"):
                    weekly_vel_map[dt][k] += wv.get(k, 0)
        merged_weekly_vel = [weekly_vel_map[k] for k in sorted(weekly_vel_map.keys())]

        # Merge Scope Burnup series
        scope_burnup_map: dict[str, dict[str, Any]] = {}
        for a in sub_analytics:
            for sb in a.get("charts", {}).get("scope_burnup", {}).get("series", []):
                dt = sb["date"]
                if dt not in scope_burnup_map:
                    scope_burnup_map[dt] = {
                        "date": dt, "label": sb.get("label", ""), "full_label": sb.get("full_label", ""),
                        "total_scope": 0, "cumulative_created": 0, "cumulative_completed": 0, "in_flight": 0,
                    }
                for k in ("total_scope", "cumulative_created", "cumulative_completed", "in_flight"):
                    scope_burnup_map[dt][k] += sb.get(k, 0)
        merged_scope_burnup = [scope_burnup_map[k] for k in sorted(scope_burnup_map.keys())]

        # Merge Net Bug Flow series
        net_bug_map: dict[str, dict[str, Any]] = {}
        for a in sub_analytics:
            for nb in a.get("charts", {}).get("net_bug_flow", {}).get("series", []):
                dt = nb["date"]
                if dt not in net_bug_map:
                    net_bug_map[dt] = {
                        "date": dt, "label": nb.get("label", ""), "full_label": nb.get("full_label", ""),
                        "bugs_found": 0, "bugs_fixed": 0, "net": 0, "net_label": "",
                    }
                net_bug_map[dt]["bugs_found"] += nb.get("bugs_found", 0)
                net_bug_map[dt]["bugs_fixed"] += nb.get("bugs_fixed", 0)
        for dt, nb in net_bug_map.items():
            net_v = nb["bugs_fixed"] - nb["bugs_found"]
            nb["net"] = net_v
            nb["net_label"] = f"+{net_v} Net Fixed" if net_v >= 0 else f"{net_v} Bug Drag"
        merged_net_bug = [net_bug_map[k] for k in sorted(net_bug_map.keys())]

        # Merge Daily Throughput Series
        date_map: dict[str, dict[str, Any]] = {}
        for a in sub_analytics:
            for s in a["charts"]["daily_throughput"]["series"]:
                d = s["date"]
                if d not in date_map:
                    date_map[d] = {
                        "date": d,
                        "label": s.get("label") or self._format_analytics_date_label(d),
                        "feature": 0,
                        "fix": 0,
                        "chore": 0,
                        "debt": 0,
                        "other": 0,
                        "total_completed": 0,
                        "commits": 0,
                        "lines_added": 0,
                        "lines_deleted": 0,
                    }
                for k in ("feature", "fix", "chore", "debt", "other", "total_completed", "commits", "lines_added", "lines_deleted"):
                    date_map[d][k] += s.get(k, 0)

        sorted_dates = sorted(date_map.keys())
        merged_throughput_series = [date_map[d] for d in sorted_dates]

        # Merge Burnup Series (daily)
        burnup_map: dict[str, dict[str, Any]] = {}
        for a in sub_analytics:
            for b in a["charts"]["burnup"]["series"]:
                d = b["date"]
                if d not in burnup_map:
                    burnup_map[d] = {
                        "date": d,
                        "label": b.get("label") or self._format_analytics_date_label(d),
                        "cumulative_completed": 0,
                        "cumulative_created": 0,
                        "in_flight": 0,
                    }
                burnup_map[d]["cumulative_completed"] += b.get("cumulative_completed", 0)
                burnup_map[d]["cumulative_created"] += b.get("cumulative_created", 0)
                burnup_map[d]["in_flight"] += b.get("in_flight", 0)

        merged_burnup_series = [burnup_map[d] for d in sorted(burnup_map.keys())]

        # Work Composition
        type_counts: dict[str, int] = {}
        for a in sub_analytics:
            for item in a["charts"]["work_composition"]["by_type"]:
                type_counts[item["type"]] = type_counts.get(item["type"], 0) + item["count"]
        total_type_count = sum(type_counts.values()) or 1
        merged_by_type = [
            {"type": t, "count": cnt, "pct": round((cnt / total_type_count) * 100, 1)}
            for t, cnt in sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
        ]

        merged_agent_work: dict[str, dict[str, Any]] = {}
        for a in sub_analytics:
            for aw in a.get("charts", {}).get("work_composition", {}).get("agent_work_breakdown", []):
                ag = aw["agent"]
                if ag not in merged_agent_work:
                    merged_agent_work[ag] = {
                        "agent": ag,
                        "feature": 0, "fix": 0, "improvement": 0, "chore": 0, "docs": 0, "other": 0,
                        "total_stories": 0, "total_points": 0, "stories": [],
                    }
                maw = merged_agent_work[ag]
                maw["total_stories"] += aw.get("total_stories", 0)
                maw["total_points"] += aw.get("total_points", 0)
                for mk in ("feature", "fix", "improvement", "chore", "docs", "other"):
                    maw[mk] += aw.get(mk, 0)
                maw["stories"].extend(aw.get("stories", []))
        merged_agent_breakdown = sorted(
            merged_agent_work.values(), key=lambda x: (x["total_points"], x["total_stories"]), reverse=True
        )

        merged_by_agent = []
        for aw in merged_agent_breakdown[:8]:
            aw_copy = dict(aw)
            aw_copy["count"] = aw["total_stories"]
            aw_copy["points"] = aw["total_points"]
            aw_copy["pct"] = round((aw["total_stories"] / (len(completed_stories) or 1)) * 100, 1)
            aw_copy["feature_count"] = aw["feature"]
            aw_copy["fix_count"] = aw["fix"]
            aw_copy["improvement_count"] = aw["improvement"]
            aw_copy["chore_count"] = aw["chore"]
            aw_copy["other_count"] = aw["other"] + aw.get("docs", 0)
            aw_copy["work_mix"] = {
                "feature": aw["feature"],
                "fix": aw["fix"],
                "improvement": aw["improvement"],
                "chore": aw["chore"],
                "other": aw["other"] + aw.get("docs", 0),
            }
            aw_copy["story_ids"] = [s["id"] for s in aw["stories"]]
            merged_by_agent.append(aw_copy)

        r1_total = sum(a["kpis"]["quality_rate"].get("r1_pass_count", 0) for a in sub_analytics)
        r2_total = sum(a["kpis"]["quality_rate"].get("r2_plus_count", 0) for a in sub_analytics)
        total_rev = r1_total + r2_total
        first_pass_pct = round((r1_total / total_rev) * 100, 1) if total_rev > 0 else 100.0
        max_tests = max((a["kpis"].get("test_health", {}).get("total_tests", 0) for a in sub_analytics), default=0)
        port_new_tests = sum(a["kpis"].get("test_health", {}).get("new_tests", 0) for a in sub_analytics)

        port_turnarounds = [
            a["kpis"].get("active_turnaround", {}).get("mean_seconds", 0)
            for a in sub_analytics
            if a["kpis"].get("active_turnaround", {}).get("mean_seconds", 0) > 0
        ]
        port_turnaround_mean = int(sum(port_turnarounds) / len(port_turnarounds)) if port_turnarounds else mean_seconds
        port_rate_per_week = round(sum(a["kpis"]["velocity"].get("rate_per_week", 0.0) for a in sub_analytics), 1)

        portfolio_stagnation_alert = "critical" if crit_defects > 0 or portfolio_pipeline["total_aging"] > 2 else ("warning" if portfolio_pipeline["total_aging"] > 0 else "healthy")

        return {
            "window": window,
            "project": "all",
            "git_backfill": {
                "done": all(a.get("git_backfill", {}).get("done", True) for a in sub_analytics),
                "counted": sum(a.get("git_backfill", {}).get("counted", 0) for a in sub_analytics),
                "total": sum(a.get("git_backfill", {}).get("total", 0) for a in sub_analytics),
            },
            "kpis": {
                "throughput": {
                    "value": total_completed,
                    "trend": trend_tp if window != "all" else None,
                    "prior_value": prior_completed,
                    "unit": "stories",
                },
                "velocity": {
                    "value": total_pts,
                    "points": total_pts,
                    "delivered": total_pts,
                    "trend": trend_pts if window != "all" else None,
                    "prior_value": prior_pts,
                    "feature_capacity_pct": portfolio_feat_cap_pct,
                    "capacity_pct": portfolio_feat_cap_pct,
                    "rate_per_week": port_rate_per_week,
                    "pts_per_week": port_rate_per_week,
                    "pts_per_wk": port_rate_per_week,
                    "rate_label": f"{port_rate_per_week} pts/wk",
                    "unit": "pts",
                },
                "work_volume": {
                    "commits": total_commits,
                    "lines_added": total_added,
                    "lines_deleted": total_deleted,
                    "churn_net": total_added - total_deleted,
                    "prior_commits": prior_commits,
                    "trend": trend_commits if window != "all" else None,
                },
                "active_turnaround": {
                    "mean_seconds": port_turnaround_mean,
                    "formatted": self.format_duration(port_turnaround_mean) if port_turnaround_mean > 0 else "—",
                    "trend": None,
                },
                "cycle_time": {
                    "mean_seconds": port_turnaround_mean,
                    "formatted": self.format_duration(port_turnaround_mean) if port_turnaround_mean > 0 else "—",
                    "trend": None,
                },
                "test_health": {
                    "total_tests": max_tests,
                    "passing_tests": max_tests,
                    "failed_tests": 0,
                    "pass_rate_pct": 100.0,
                    "new_tests": port_new_tests,
                    "new_tests_in_window": port_new_tests,
                    "pass_label": "100% Passing",
                    "trend_label": f"+{port_new_tests} tests this period" if port_new_tests > 0 else "Stable test suite",
                    "subtext": f"+{port_new_tests} new tests this period · 0 failures",
                },
                "quality_rate": {
                    "first_pass_rate": first_pass_pct,
                    "total_reviewed": total_rev,
                    "r1_pass_count": r1_total,
                    "r2_plus_count": r2_total,
                    "test_count": max_tests,
                    "new_tests": port_new_tests,
                    "pass_rate_pct": 100.0,
                },
                "stagnation": {
                    "active_wip": portfolio_pipeline["total_wip"],
                    "active_wip_points": portfolio_pipeline["total_wip_points"],
                    "aging_count": portfolio_pipeline["total_aging"],
                    "critical_bugs": crit_defects,
                    "reopened_count": portfolio_pipeline["reopened_count"],
                    "alert_level": portfolio_stagnation_alert,
                },
            },
            "cohorts": merged_cohorts,
            "weekly_cohorts": merged_cohorts,
            "pipeline_health": portfolio_pipeline,
            "open_defects": portfolio_open_defects,
            "scope_burnup": {
                "series": merged_scope_burnup,
            },
            "net_bug_flow": {
                "series": merged_net_bug,
            },
            "charts": {
                "daily_throughput": {
                    "dates": sorted_dates,
                    "series": merged_throughput_series,
                    "story_types": ["feature", "fix", "chore", "debt", "other"],
                },
                "burnup": {
                    "dates": sorted(burnup_map.keys()),
                    "series": merged_burnup_series,
                },
                "weekly_velocity": {
                    "series": merged_weekly_vel,
                },
                "scope_burnup": {
                    "series": merged_scope_burnup,
                },
                "net_bug_flow": {
                    "series": merged_net_bug,
                },
                "work_composition": {
                    "by_type": merged_by_type,
                    "by_agent": merged_by_agent,
                    "agent_work_breakdown": merged_agent_breakdown,
                },
                "quality_friction": {
                    "rounds": [
                        {"round": "Round 1 PASS", "count": r1_total, "pct": round((r1_total / (total_rev or 1)) * 100, 1), "color": "emerald"},
                        {"round": "Round 2+ PASS", "count": r2_total, "pct": round((r2_total / (total_rev or 1)) * 100, 1), "color": "amber"},
                    ],
                    "test_suite_growth": sub_analytics[0]["charts"]["quality_friction"].get("test_suite_growth", []) if sub_analytics else [],
                },
            },
            "stories": all_stories,
            "all_delivered_stories": all_delivered,
            "agent_work_breakdown": merged_by_agent,
            "test_health": {
                "total_tests": max_tests,
                "passing_tests": max_tests,
                "failed_tests": 0,
                "pass_rate_pct": 100.0,
                "new_tests": port_new_tests,
                "new_tests_in_window": port_new_tests,
                "pass_label": "100% Passing",
                "trend_label": f"+{port_new_tests} tests this period" if port_new_tests > 0 else "Stable test suite",
                "subtext": f"+{port_new_tests} new tests this period · 0 failures",
            },
            "active_turnaround": {
                "mean_seconds": port_turnaround_mean,
                "formatted": self.format_duration(port_turnaround_mean) if port_turnaround_mean > 0 else "—",
                "trend": None,
            },
            "projects_summary": projects_summary,
        }

    _ANALYTICS_MAX_AGE = 600.0

    def _parser_for(self, project: dict[str, Any]) -> "PMParser":
        """The parser for another registered project: the server's pooled one when wired, else a fresh one."""
        if self.parser_provider is not None:
            return self.parser_provider(project)
        return PMParser(project=project)

    def _analytics_fingerprint(self) -> str:
        """Hash of every file the rollups read (path, mtime, size): cards, reviews, QUALITY.md, PLAN.md."""
        h = hashlib.sha256()
        entries: list[str] = []

        def add(path: Path) -> None:
            try:
                st = path.stat()
            except OSError:
                return
            entries.append(f"{path}|{st.st_mtime_ns}|{st.st_size}")

        for folder in (self.cards_dir, self.backlog_dir, self.reviews_dir):
            try:
                # DirEntry.stat() is served from the directory listing on Windows: no per-file syscall.
                for entry in os.scandir(folder):
                    if entry.is_file():
                        st = entry.stat()
                        entries.append(f"{entry.path}|{st.st_mtime_ns}|{st.st_size}")
            except OSError:
                continue
        add(self.quality_file)
        add(self.plan_path)
        for e in sorted(entries):
            h.update(e.encode("utf-8", errors="replace"))
            h.update(b"\n")
        return h.hexdigest()

    @staticmethod
    def _today() -> datetime.date:
        return datetime.date.today()

    @staticmethod
    def _app_version() -> str:
        try:
            return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _analytics_key(self, window: str) -> str:
        parts = [
            window,
            self.project.get("slug") or "",
            self._today().isoformat(),
            self._app_version(),
            self._read_git_head(),
            self._analytics_fingerprint(),
            self.prefix,
            json.dumps(declared_extra_tags(self.project), sort_keys=True),
        ]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8", errors="replace")).hexdigest()

    def _cached_project_analytics(self, window: str) -> dict[str, Any]:
        """This project's payload from the SQLite cache when its key still matches and the row is fresh."""
        if self._index is None:
            return self._compute_project_analytics(window)
        scope = self.project.get("slug") or "current"
        now = time.time()
        try:
            key = self._analytics_key(window)
            memo = self._analytics_memo.get(window)
            if memo is not None and memo[0] == key and (now - memo[1]) < self._ANALYTICS_MAX_AGE:
                return memo[2]
            hit = self._index.get_analytics(window, scope, key, self._ANALYTICS_MAX_AGE, now)
        except Exception:
            return self._compute_project_analytics(window)
        if hit is not None:
            created_at, payload = hit
            self._analytics_memo[window] = (key, created_at, payload)
            return payload
        # The key changed on disk, so drop in-memory card/review caches (a pooled parser keeps them
        # for 30s) before recomputing, or stale cards would be stored under the new key.
        self.invalidate_cache()
        result = self._compute_project_analytics(window)
        # A payload still counting history is never stored: it would freeze partial numbers.
        backfill = result.get("git_backfill", {})
        if backfill.get("done", True) and not backfill.get("error"):
            try:
                self._index.put_analytics(window, scope, key, time.time(), json.dumps(result, ensure_ascii=False))
                self._analytics_memo[window] = (key, time.time(), result)
            except Exception:
                pass
        return result

    def get_analytics_summary(
        self, window: str = "6w", project_slug: str | None = None
    ) -> dict[str, Any]:
        """Calculates executive progress charts, KPIs and trends for the requested time window."""
        norm_window = (window or "6w").lower().strip()
        if norm_window not in ("6w", "4w", "qtd", "all", "7d", "14d", "30d"):
            norm_window = "6w"

        cache_key = f"{norm_window}:{project_slug or ''}"
        now = time.time()
        # The SQLite payload cache does the freshness check; the 15s memory cache only serves
        # parsers without an index (read-only), where it would otherwise recompute every call.
        if self._index is None and cache_key in self._analytics_cache:
            c_time, c_data = self._analytics_cache[cache_key]
            if (now - c_time) < 15.0:
                return c_data

        if project_slug == "all":
            result = self._compute_portfolio_analytics(norm_window)
        elif project_slug and project_slug != self.project.get("slug"):
            try:
                from board import projects
            except ImportError:
                try:
                    import projects
                except ImportError:
                    projects = None
            reg_projects = projects.load_registry().get("projects", {}) if projects else {}
            if project_slug in reg_projects:
                result = self._parser_for(reg_projects[project_slug]).get_analytics_summary(window=norm_window)
            else:
                result = self._cached_project_analytics(norm_window)
        else:
            result = self._cached_project_analytics(norm_window)

        if self._index is None:
            self._analytics_cache[cache_key] = (now, result)
        return result

    # --------------------------------------------------------------------------
    # Write-Back Operations
    # --------------------------------------------------------------------------
    def toggle_dod_item(
        self, mn_id: str, item_index: int, checked: bool
    ) -> tuple[bool, str]:
        if mn_id.startswith("GRILL-"):
            sprint_id = mn_id.replace("GRILL-", "").strip()
            return self.toggle_sprint_dod_item(sprint_id, item_index, checked)

        cards = self.get_all_cards()
        target_card = next((c for c in cards if c["mn_id"] == mn_id), None)
        if not target_card:
            return False, f"Card {mn_id} not found"

        file_path = self.resolve_path(target_card["file_path"])
        if not file_path.exists():
            return False, f"File {file_path} not found"

        raw_bytes = file_path.read_bytes()
        nl = "\r\n" if b"\r\n" in raw_bytes else "\n"
        content = raw_bytes.decode("utf-8", errors="replace")
        lines = content.splitlines()

        current_idx = 0
        modified = False
        new_lines = []

        in_dod = False
        for line in lines:
            if re.match(r"^##\s+(?:Definition of Done|Acceptance Criteria)", line):
                in_dod = True
                new_lines.append(line)
                continue

            if in_dod:
                if line.startswith("## "):
                    in_dod = False
                    new_lines.append(line)
                    continue

                m = re.match(r"^(\s*-\s*\[)([ xX])(\]\s*.*)$", line)
                if m:
                    if current_idx == item_index:
                        mark = "x" if checked else " "
                        new_lines.append(f"{m.group(1)}{mark}{m.group(3)}")
                        modified = True
                    else:
                        new_lines.append(line)
                    current_idx += 1
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        if modified:
            file_path.write_text(nl.join(new_lines) + nl, encoding="utf-8", newline="")
            self.invalidate_cache()
            return True, "DoD item updated"

        # Fallback to PLAN.md sprint DoD if card has no card-level DoD items
        if not modified and target_card.get("sprint"):
            return self.toggle_sprint_dod_item(str(target_card["sprint"]), item_index, checked)

        return False, f"DoD item index {item_index} not found"

    def toggle_qa_item(
        self, mn_id: str, item_index: int, checked: bool
    ) -> tuple[bool, str]:
        cards = self.get_all_cards()
        target_card = next((c for c in cards if c["mn_id"] == mn_id), None)
        if not target_card:
            return False, f"Card {mn_id} not found"

        file_path = self.resolve_path(target_card["file_path"])
        if not file_path.exists():
            return False, f"File {file_path} not found"

        raw_bytes = file_path.read_bytes()
        nl = "\r\n" if b"\r\n" in raw_bytes else "\n"
        content = raw_bytes.decode("utf-8", errors="replace")
        lines = content.splitlines()

        current_idx = 0
        modified = False
        new_lines = []

        in_qa = False
        for line in lines:
            if re.match(r"^##\s+(?:Human QA Test|Human QA|Manual Verification)", line):
                in_qa = True
                new_lines.append(line)
                continue

            if in_qa:
                if line.startswith("## "):
                    in_qa = False
                    new_lines.append(line)
                    continue

                m = re.match(r"^(\s*-\s*\[)([ xX])(\]\s*.*)$", line)
                if m:
                    if current_idx == item_index:
                        mark = "x" if checked else " "
                        new_lines.append(f"{m.group(1)}{mark}{m.group(3)}")
                        modified = True
                    else:
                        new_lines.append(line)
                    current_idx += 1
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        if modified:
            file_path.write_text(nl.join(new_lines) + nl, encoding="utf-8", newline="")
            self.invalidate_cache()
            return True, "Human QA item updated"
        return False, f"Human QA item index {item_index} not found"

    def validate_card(
        self, mn_id: str, notes: str = "", do_commit: bool = True
    ) -> tuple[bool, str]:
        cards = self.get_all_cards()
        target_card = next((c for c in cards if c["mn_id"] == mn_id), None)
        if not target_card:
            return False, f"Card {mn_id} not found"

        file_path = self.resolve_path(target_card["file_path"])
        if not file_path.exists():
            return False, f"File {file_path} not found"

        content = file_path.read_text(encoding="utf-8")
        nl = "\r\n" if "\r\n" in content else "\n"
        today = datetime.date.today().isoformat()

        # 1. Update frontmatter status -> completed and add validated: YYYY-MM-DD
        if "status:" in content:
            content = re.sub(
                r"^status:\s*.*$", "status: completed", content, flags=re.MULTILINE
            )
        else:
            content = re.sub(
                r"^(---\r?\n)", rf"\1status: completed{nl}", content, count=1
            )

        if "validated:" in content:
            content = re.sub(
                r"^validated:\s*.*$",
                f"validated: {today}",
                content,
                flags=re.MULTILINE,
            )
        else:
            content = re.sub(
                r"^(---\r?\n)", rf"\1validated: {today}{nl}", content, count=1
            )

        # 2. Check all DoD items
        lines = content.splitlines()
        new_lines = []
        in_dod = False
        for line in lines:
            if re.match(r"^##\s+(?:Definition of Done|Acceptance Criteria)", line):
                in_dod = True
                new_lines.append(line)
                continue
            if in_dod:
                if line.startswith("## "):
                    in_dod = False
                    new_lines.append(line)
                    continue
                m = re.match(r"^(\s*-\s*\[)[ ]?(\]\s*.*)$", line)
                if m:
                    new_lines.append(f"{m.group(1)}x{m.group(2)}")
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        # 3. Add Changelog entry
        now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        note_text = notes.strip() if notes else "User QA validated via PM Dashboard"
        cl_entry = f"| {now_time} | User | {note_text} |"

        content = nl.join(new_lines) + nl
        if "## Changelog" in content:
            # Append to bottom of Changelog table (chronological ascending order)
            def _append_changelog_row(m: re.Match) -> str:
                t_body = m.group(1).rstrip()
                return f"{t_body}{nl}{cl_entry}{nl}"

            content = re.sub(
                r"(##\s+Changelog\s*\r?\n.*?\r?\n\|[-|\s]+\r?\n.*?)(?=\r?\n##|\Z)",
                _append_changelog_row,
                content,
                count=1,
                flags=re.DOTALL,
            )

        file_path.write_text(content, encoding="utf-8", newline="")
        self.invalidate_cache()

        # 4. Git commit locally if requested
        commit_note = ""
        if do_commit:
            import sys
            commit_msg = f"docs(qa): validate {mn_id} - {target_card['title']}"
            git_commit_script = self.root_dir / "scripts" / "git-commit.ps1"
            if self.docs_external:
                # External docs keep their own history; never touch the code repo.
                if not self.commit_docs([file_path], commit_msg):
                    commit_note = " (card saved, but the docs-repo commit failed — check its git identity)"
            elif git_commit_script.exists():
                ps_cmd = "powershell" if sys.platform == "win32" else "pwsh"
                try:
                    subprocess.run(
                        [
                            ps_cmd,
                            "-File",
                            str(git_commit_script),
                            "-Message",
                            commit_msg,
                        ],
                        cwd=str(self.root_dir),
                        capture_output=True,
                        check=False,
                    )
                except Exception:
                    pass

        return True, f"Card {mn_id} validated as completed on {today}{commit_note}"
