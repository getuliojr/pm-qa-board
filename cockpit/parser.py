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
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class PMParser:
    def __init__(
        self,
        root_dir: Path | str | None = None,
        *,
        code_path: Path | str | None = None,
        docs_path: Path | str | None = None,
        prefix: str = "MN",
        project: dict[str, Any] | None = None,
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
                text=True,
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

    def compute_card_duration(
        self,
        card: dict[str, Any],
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        """
        Calculates elapsed duration for a card or stream:
        - For 'in-progress' cards / active grills: duration from start to current local time.
        - For 'qa' / 'completed' cards: duration from start to final changelog / review timestamp.
        - For 'backlog' / 'sprint-backlog': elapsed_seconds = 0.
        Returns dict with: elapsed_seconds, duration_formatted, start_time, end_time.
        """
        changelog = card.get("changelog") or []
        status = (card.get("status") or "backlog").lower().strip()
        is_grill = bool(card.get("is_grill", False))

        dt_start: datetime.datetime | None = None

        # 1. Determine start timestamp from earliest changelog entry with valid datetime
        for entry in changelog:
            dt_val = entry.get("datetime") or f"{entry.get('date', '')} {entry.get('time', '')}".strip()
            if dt_val:
                try:
                    dt_start = datetime.datetime.strptime(dt_val[:16], "%Y-%m-%d %H:%M")
                    break
                except Exception:
                    continue

        # Fallback to created_date or file mtime if no valid changelog datetime
        if not dt_start:
            c_date = card.get("created_date")
            if c_date:
                try:
                    dt_start = datetime.datetime.strptime(f"{str(c_date)[:10]} 09:00", "%Y-%m-%d %H:%M")
                except Exception:
                    pass

        if not dt_start:
            ref_now = now or datetime.datetime.now()
            dt_start = ref_now

        # 2. Determine end timestamp and elapsed seconds based on lifecycle state
        dt_end: datetime.datetime | None = None
        elapsed_seconds: int = 0
        end_time_str: str | None = None

        if status == "in-progress" or is_grill:
            ref_now = now or datetime.datetime.now()
            elapsed_seconds = max(0, int((ref_now - dt_start).total_seconds()))
            end_time_str = None
        elif status in ("qa", "completed"):
            if changelog:
                last_entry = changelog[-1]
                dt_val = last_entry.get("datetime") or f"{last_entry.get('date', '')} {last_entry.get('time', '')}".strip()
                if dt_val:
                    try:
                        dt_end = datetime.datetime.strptime(dt_val[:16], "%Y-%m-%d %H:%M")
                    except Exception:
                        pass
            if not dt_end:
                dt_end = dt_start
            if dt_end < dt_start:
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
    def parse_card_file(self, file_path: Path) -> dict[str, Any] | None:
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

        # Backfill missing times from Git commit logs or file mtime so entry.datetime is always populated
        file_commits = None
        for entry in changelog:
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

        if not created_date and changelog:
            valid_dates = [c["date"] for c in changelog if re.match(r"^\d{4}-\d{2}-\d{2}", c["date"])]
            if valid_dates:
                created_date = sorted(valid_dates)[0]

        if not created_date:
            card_map = self.get_card_git_commit_map()
            commits = card_map.get(file_path.name)
            if commits:
                # Oldest commit in reverse-chronological log is creation commit
                created_date = commits[-1]["date"]

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

        reviews = self.get_reviews_for_card(mn_id)
        if not reviews:
            s_val = str(frontmatter.get("sprint", "")).strip()
            if s_val:
                reviews = self.get_reviews_for_sprint(s_val)

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

        card_dict = {
            "mn_id": mn_id,
            "id": mn_id,
            "title": title,
            "sprint": str(frontmatter.get("sprint", "")),
            "story": str(frontmatter.get("sprint", "")),
            "schema": frontmatter.get("_schema", "sprint"),
            "type": frontmatter.get("type", "feature"),
            "status": status,
            "warnings": warnings_list,
            "missing_plan_dod": missing_plan_dod,
            "warning_missing_plan_dod": missing_plan_dod,
            "validated": frontmatter.get("validated"),
            "created_date": created_date,
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

    def get_all_cards(self, refresh: bool = False) -> list[dict[str, Any]]:
        now = time.time()
        if not refresh and getattr(self, "_cards_cache", None) is not None and (now - getattr(self, "_cards_cache_time", 0)) < 10.0:
            return self._cards_cache

        cards: list[dict[str, Any]] = []
        if self.cards_dir.exists():
            for f in sorted(self.cards_dir.glob("*.md")):
                if f.name == "template.md":
                    continue
                card = self.parse_card_file(f)
                if card:
                    cards.append(card)

        if self.backlog_dir.exists():
            for f in sorted(self.backlog_dir.glob("*.md")):
                if f.name == "template.md":
                    continue
                card = self.parse_card_file(f)
                if card:
                    cards.append(card)

        # Surface active design grills from PLAN.md in the In-Progress column on the board
        if self.plan_path.exists():
            plan_content = ""
            try:
                plan_content = self.plan_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

            if plan_content:
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

                    cards.append({
                        "mn_id": f"GRILL-{clean_g_sprint}",
                        "id": f"GRILL-{clean_g_sprint}",
                        "title": title,
                        "sprint": clean_g_sprint,
                        "story": clean_g_sprint,
                        "schema": "story",
                        "type": "grill",
                        "is_grill": True,
                        "status": "in-progress",
                        "warnings": [],
                        "missing_plan_dod": False,
                        "warning_missing_plan_dod": False,
                        "validated": False,
                        "created_date": datetime.date.today().isoformat(),
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
                                "date": datetime.date.today().isoformat(),
                                "time": "",
                                "datetime": datetime.date.today().isoformat(),
                            }
                        ],
                        "reviews": [],
                        "review_count": 0,
                        "file_name": "PLAN.md",
                        "file_path": self.rel_path(self.plan_path),
                        "closeout": None,
                        "updated_at": datetime.datetime.now().isoformat(),
                    })

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
                is_complete = "✅" in status_col or "complete" in st_lower or "completed" in st_lower
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

        return {"roadmap": roadmap, "sprints": sprint_sections}

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
                text=True,
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
                "start_time": c.get("start_time"),
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
                        sprint_backlog_cards.sort(
                            key=lambda x: str(x.get("created_date") or ""),
                            reverse=True,
                        )
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

        cards_by_status = {
            "sprint-backlog": [c for c in cards if c["status"] == "sprint-backlog"],
            "in-progress": [c for c in cards if c["status"] == "in-progress"],
            "qa": [c for c in cards if c["status"] == "qa"],
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
                for line in quality_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("|") and "**" in line:
                        m_row = re.match(r"^\|\s*\*\*([0-9a-zA-Z._-]+)\*\*", line)
                        if m_row:
                            q_sprint = m_row.group(1).strip()
                            parts = [p.strip() for p in line.split("|")]
                            if len(parts) >= 4:
                                q_test_cell = parts[2]
                                q_cov_cell = parts[3]
                                m_p = re.search(r"(\d+)\s+passed", q_test_cell)
                                m_c = re.search(r"([\d.]+%)", q_cov_cell) or re.search(r"([\d.]+%)", q_test_cell)
                                m_r = re.search(r"(?:run\s+|`runs/)(2026\d{4}-\d{6}-[0-9a-zA-Z_-]+)", q_test_cell)
                                m_sha = re.search(r"@\s*`?([0-9a-fA-F]{7,40})`?", q_test_cell)
                                is_owed = "owed" in q_test_cell.lower()
                                if m_p:
                                    quality_sprints[q_sprint.lower()] = {
                                        "pass_count": int(m_p.group(1)),
                                        "coverage": m_c.group(1) if m_c else None,
                                        "run_id": m_r.group(1) if m_r else None,
                                        "tested_sha": m_sha.group(1)[:7].lower() if m_sha else None,
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
                    if not q_tested_sha or not sha or is_ancestor(sha, q_tested_sha):
                        verified = True
                        status_text = "Passed"
                        pass_count = q_info["pass_count"]
                        coverage = q_info["coverage"]
                        latest_run_id = q_info["run_id"]
                        tested_sha = q_tested_sha
                    else:
                        status_text = "Owed"
                else:
                    status_text = "Owed"

            # Rule 2: Anything sprint 15.7 or earlier should be marked as done and complete
            if is_sprint_15_7_or_earlier(s_num):
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
                text=True,
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
                text=True,
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
                text=True,
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
