"""
server.py - PM & QA Board local web server (multi-project).

Runs on port 3333 with zero external dependencies (Python standard library).
Provides REST APIs for story card inspection, interactive QA validation, git status,
and switching between the projects registered in ``~/.pm-qa-cockpit/projects.json``.
"""

from __future__ import annotations

import atexit
import collections
import datetime
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import projects
from parser import PMParser, declared_extra_tags

HOST = os.environ.get("PM_QA_COCKPIT_HOST", "127.0.0.1")
PORT = int(os.environ.get("PM_QA_COCKPIT_PORT", "3333"))
TOKEN = os.environ.get("PM_QA_COCKPIT_TOKEN", "")
TUNNEL_URL = os.environ.get("PM_QA_COCKPIT_TUNNEL_URL", "")
KEEP_ALIVE = os.environ.get("PM_QA_COCKPIT_KEEP_ALIVE", "0").lower() in ("1", "true", "yes")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def set_keep_alive(enable: bool = True) -> bool:
    """Prevent Windows host from sleeping while server is running (Windows API)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        if enable:
            res = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            return res != 0
        else:
            res = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            return res != 0
    except Exception:
        return False


try:
    from wiring import get_lan_ip
except ImportError:
    from board.wiring import get_lan_ip


atexit.register(lambda: set_keep_alive(False))


class HeavyGateWorker:
    """Manages background execution of finalgates.ps1."""

    def __init__(self, parser: PMParser):
        self.parser = parser
        self.root_dir = parser.root_dir
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.status = "idle"  # idle, running, completed, failed
        self.current_sprint: str | None = None
        self.pending_sprints: list[str] = []
        self.start_time: float | None = None
        self.end_time: float | None = None
        self.exit_code: int | None = None
        self.was_stopped = False
        self.log_lines: collections.deque = collections.deque(maxlen=2000)
        self.log_file_path = parser.heavy_results_dir / "current_run.log"

    def get_status(self) -> dict[str, Any]:
        with self.lock:
            elapsed = 0
            if self.start_time:
                end = self.end_time if self.end_time else time.time()
                elapsed = round(end - self.start_time)
            return {
                "status": self.status,
                "is_running": self.status == "running",
                "current_sprint": self.current_sprint,
                "pending_sprints": list(self.pending_sprints),
                "start_time": (
                    datetime.datetime.fromtimestamp(self.start_time).isoformat()
                    if self.start_time
                    else None
                ),
                "elapsed_seconds": elapsed,
                "exit_code": self.exit_code,
            }

    def _generate_triage_card(self, sprint_id: str, sha_target: str | None) -> None:
        try:
            runs_dir = self.parser.heavy_results_dir / "runs"
            clean_snum = sprint_id.replace(".", "_")
            matched_run = None
            if runs_dir.exists():
                candidate_runs = []
                for d in runs_dir.iterdir():
                    if d.is_dir() and (f"sprint-{clean_snum}-" in d.name or d.name.endswith(f"-{sprint_id}")):
                        candidate_runs.append(d)
                if candidate_runs:
                    candidate_runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    matched_run = candidate_runs[0]

            run_id = matched_run.name if matched_run else "unknown"
            run_rel = f"gate-heavy-results/runs/{run_id}" if matched_run else "gate-heavy-results/runs/"
            pytest_log_rel = f"{run_rel}/pytest.log"
            summary_rel = f"{run_rel}/SUMMARY.md"

            failure_detail = "Gate 5 (pytest full suite + coverage) failed."
            if matched_run:
                summary_path = matched_run / "results.json"
                if summary_path.exists():
                    try:
                        res = json.loads(summary_path.read_text(encoding="utf-8-sig"))
                        detail = res.get("pytest", {}).get("Detail")
                        if detail:
                            failure_detail = f"Pytest result: {detail}"
                    except Exception:
                        pass

            cards_dir = self.parser.cards_dir
            cards_dir.mkdir(parents=True, exist_ok=True)
            pfx = self.parser.prefix
            card_filename = f"{pfx}-{sprint_id}-TRIAGE - Heavy Gate Failure Triage.md"
            card_path = cards_dir / card_filename

            today_str = datetime.date.today().isoformat()
            sha_display = f"`{sha_target}`" if sha_target else "commit at sprint close"

            card_content = f"""---
mn-id: {pfx}-{sprint_id}-TRIAGE
sprint: "{sprint_id}"
type: bug
status: sprint-backlog
created: {today_str}
validated: false
---

# {pfx}-{sprint_id}-TRIAGE - Heavy Gate Failure Triage

## Description

Automated heavy gate failure audit for Story {sprint_id}.
Gate 5 (pytest full suite + coverage) failed when run against {sha_display}.

- **Run ID**: `{run_id}`
- **Results Directory**: `{run_rel}`
- **Summary**: `{summary_rel}`
- **Log**: `{pytest_log_rel}`

### Failure Details
{failure_detail}

## Definition of Done / Acceptance Criteria

- [ ] Investigate failure log at `{pytest_log_rel}`.
- [ ] Determine if failure is a stale test or genuine regression, and apply the fix.
- [ ] Verify targeted pytest passes: `uv run pytest <failing_test>`.
- [ ] Fast gates 1–4 + 6 green (`powershell .\\scripts\\gates.ps1`).
- [ ] Mark card completed and delete processed run directory per orchestrate step 0.

## Changelog

| Date | Agent | What |
|---|---|---|
| {today_str} | HeavyGateWorker | Auto-generated failure triage card |
"""
            card_path.write_text(card_content, encoding="utf-8")
        except Exception as err:
            with self.lock:
                self.log_lines.append(f"Failed to generate triage card for Sprint {sprint_id}: {err}")

    def start(
        self,
        args: list[str] | None = None,
        sprint: str | None = None,
        sha: str | None = None,
        sprint_queue: list[Any] | None = None,
    ) -> tuple[bool, str]:
        with self.lock:
            if self.status == "running" and self.process and self.process.poll() is None:
                return False, "Heavy gate is already running"

            self.status = "running"
            self.start_time = time.time()
            self.end_time = None
            self.exit_code = None
            self.was_stopped = False
            self.current_sprint = sprint
            self.pending_sprints = [
                item["sprint"] if isinstance(item, dict) else (item[0] if isinstance(item, (list, tuple)) else str(item))
                for item in sprint_queue
            ] if sprint_queue else []
            self.log_lines.clear()

            self.log_file_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.log_file_path.write_text("", encoding="utf-8")
            except Exception:
                pass

        def _execute_single(sprint_target: str | None, sha_target: str | None, extra_args: list[str] | None) -> int:
            gates = self.parser.project.get("gates") or {}
            mode = gates.get("mode") or "heavy"
            heavy = gates.get("heavy_script")

            is_fast_runner = mode == "fast" or (not heavy and gates.get("full"))
            if is_fast_runner:
                full_cmd = gates.get("full") or "uv run --system-certs --group dev pytest -q"
                cmd = full_cmd
                header = f"\n=== [Test Suite Runner] Running test suite: {full_cmd} ===\n"
                shell_mode = True
            else:
                script = Path(heavy) if heavy else self.root_dir / "scripts" / "finalgates.ps1"
                if not script.is_absolute():
                    script = self.root_dir / script
                cmd = ["powershell", "-File", str(script)]
                if sprint_target:
                    cmd.extend(["-Sprint", str(sprint_target)])
                if sha_target:
                    cmd.extend(["-Sha", str(sha_target)])
                if extra_args and isinstance(extra_args, list):
                    for a in extra_args:
                        if isinstance(a, str) and re.match(r"^-[A-Za-z0-9_]+$", a.strip()):
                            cmd.append(a.strip())
                header = f"\n=== [Sprint Quality Auditor] Running finalgates.ps1"
                if sprint_target:
                    header += f" for Sprint {sprint_target}"
                header += " ===\n"
                shell_mode = False

            p = subprocess.Popen(
                cmd,
                shell=shell_mode,
                cwd=str(self.root_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
            with self.lock:
                self.process = p
                self.current_sprint = sprint_target

            with open(self.log_file_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(header)
                f.flush()
                with self.lock:
                    self.log_lines.append(header.strip())

                for line in iter(p.stdout.readline, ""):
                    line_str = line.rstrip()
                    with self.lock:
                        self.log_lines.append(line_str)
                    f.write(line)
                    f.flush()

            p.stdout.close()
            code = p.wait()

            # Record latest.json upon completion
            try:
                full_log = "\n".join(self.log_lines)
                m_pass = re.search(r"(\d+)\s+passed", full_log)
                m_fail = re.search(r"(\d+)\s+failed", full_log)
                m_xfail = re.search(r"(\d+)\s+xfailed", full_log)
                m_dur = re.search(r"in\s+([\d.]+)s", full_log)

                head_sha = ""
                try:
                    c_proc = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=str(self.root_dir),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    head_sha = c_proc.stdout.strip()[:7].lower()
                except Exception:
                    pass

                latest_data = {
                    "timestamp": datetime.datetime.now().isoformat(),
                    "sha": head_sha or sha_target or "",
                    "sprint": sprint_target or (self.parser.project.get("prefix") or "QA"),
                    "pytest_passed": int(m_pass.group(1)) if m_pass else None,
                    "pytest_failed": int(m_fail.group(1)) if m_fail else 0,
                    "pytest_xfailed": int(m_xfail.group(1)) if m_xfail else 0,
                    "elapsed_sec": float(m_dur.group(1)) if m_dur else (time.time() - self.start_time),
                    "overall_ok": code == 0,
                    "command": full_cmd if is_fast_runner else str(cmd),
                }
                self.parser.heavy_results_dir.mkdir(parents=True, exist_ok=True)
                (self.parser.heavy_results_dir / "latest.json").write_text(
                    json.dumps(latest_data, indent=2), encoding="utf-8"
                )
                self.parser._sprint_gate_cache_time = 0
            except Exception:
                pass

            return code


        def _run():
            try:
                overall_exit = 0
                if sprint_queue:
                    queue_copy = list(sprint_queue)
                    for item in queue_copy:
                        if isinstance(item, dict):
                            item_sprint = item.get("sprint")
                            item_sha = item.get("sha")
                        elif isinstance(item, (list, tuple)):
                            item_sprint = item[0]
                            item_sha = item[1] if len(item) > 1 else None
                        else:
                            item_sprint = str(item)
                            item_sha = None

                        with self.lock:
                            if self.was_stopped:
                                break
                            self.current_sprint = item_sprint
                            if self.pending_sprints:
                                self.pending_sprints.pop(0)

                        code = _execute_single(item_sprint, item_sha, args)
                        if code != 0:
                            overall_exit = code
                            self._generate_triage_card(str(item_sprint), item_sha)
                else:
                    overall_exit = _execute_single(sprint, sha, args)
                    if overall_exit != 0 and sprint:
                        self._generate_triage_card(str(sprint), sha)

                with self.lock:
                    if not self.was_stopped:
                        self.exit_code = overall_exit
                        self.end_time = time.time()
                        self.status = "completed" if overall_exit == 0 else "failed"
                        self.current_sprint = None
                        self.pending_sprints.clear()
            except Exception as e:
                with self.lock:
                    if not self.was_stopped:
                        self.exit_code = -1
                        self.end_time = time.time()
                        self.status = "failed"
                        self.current_sprint = None
                        self.pending_sprints.clear()
                    self.log_lines.append(f"Execution error: {e}")

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return True, "Heavy gate started in background"

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            if self.status != "running" or not self.process:
                return False, "No heavy gate currently running"
            try:
                self.was_stopped = True
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                        check=False,
                    )
                else:
                    self.process.terminate()
                self.status = "failed"
                self.exit_code = -99
                self.end_time = time.time()
                self.log_lines.append("[User stopped heavy gate execution]")
                return True, "Heavy gate stopped"
            except Exception as e:
                return False, f"Failed to stop process: {e}"

    def get_log(self, limit: int = 100) -> list[str]:
        with self.lock:
            return list(self.log_lines)[-limit:]


def _worker_config(parser: PMParser) -> tuple[str, str, str | None, str | None, str | None]:
    """What a HeavyGateWorker depends on; a change means the worker must be rebuilt."""
    gates = parser.project.get("gates") or {}
    return (
        str(parser.root_dir),
        str(parser.heavy_results_dir),
        gates.get("heavy_script"),
        gates.get("mode"),
        gates.get("full"),
    )



_BUILDING_COUNT_CACHE: dict[str, tuple[float, int]] = {}


def invalidate_building_count_cache(docs_path: str | None = None) -> None:
    """Invalidate building count cache for a specific project docs_path or entirely."""
    global _BUILDING_COUNT_CACHE
    if docs_path:
        _BUILDING_COUNT_CACHE.pop(str(docs_path), None)
    else:
        _BUILDING_COUNT_CACHE.clear()


def count_building_cards(project: dict[str, Any], *, max_age_seconds: float = 30.0) -> int:
    """Count how many stories are currently in progress ('building') for a project, with in-memory TTL caching."""
    docs_path = project.get("docs_path")
    if not docs_path:
        return 0

    norm_path = str(docs_path)
    now = time.time()
    if norm_path in _BUILDING_COUNT_CACHE:
        cached_time, cached_count = _BUILDING_COUNT_CACHE[norm_path]
        if (now - cached_time) < max_age_seconds:
            return cached_count

    cards_dir = Path(docs_path) / "cards"
    count = 0
    building_sprints = set()
    if cards_dir.is_dir():
        for card_file in cards_dir.glob("*.md"):
            try:
                with open(card_file, "r", encoding="utf-8", errors="ignore") as f:
                    header = f.read(1024)
                m = re.search(r"^status:\s*(.+)$", header, re.MULTILINE)
                if m:
                    st = m.group(1).strip().lower().strip("\"'")
                    if st in ("in-progress", "building", "active"):
                        count += 1
                        m_sp = re.search(r"^sprint:\s*(.+)$", header, re.MULTILINE)
                        if m_sp:
                            building_sprints.add(m_sp.group(1).strip().lower().strip("\"'"))
            except Exception:
                pass
    plan_path = Path(docs_path) / "PLAN.md"
    if plan_path.is_file():
        try:
            with open(plan_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            grills = re.findall(
                r"^\s*#{3,4}\s+Grill in progress\s*[—–\-]\s*(?:(?:Sprint|Story)\s+)?([a-zA-Z0-9_.-]+)",
                content,
                re.IGNORECASE | re.MULTILINE,
            )
            for g in grills:
                clean_g = re.sub(r"^(?:Sprint|Story|Phase)\s+", "", g, flags=re.IGNORECASE).strip().lower()
                if clean_g and clean_g not in building_sprints:
                    count += 1
        except Exception:
            pass

    _BUILDING_COUNT_CACHE[norm_path] = (now, count)
    return count


def _public_extra_tags(project: dict[str, Any]) -> dict[str, Any]:
    """Replace raw ``extra_tags`` on a project copy with key/label/filter (link templates stay server-side)."""
    if "extra_tags" in project:
        project["extra_tags"] = [
            {"key": t["key"], "label": t["label"], "filter": t["filter"]}
            for t in declared_extra_tags(project)
        ]
    return project


class CockpitState:
    """The active project's parser + heavy-gate worker; swapped atomically on project switch."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.workers: dict[str, HeavyGateWorker] = {}
        # One parser per project slug, reused by switches, the watcher and portfolio analytics so
        # its in-memory caches survive; rebuilt when the project's configuration changes.
        self._pool: dict[str, tuple[str, PMParser]] = {}
        self._pool_lock = threading.Lock()
        self.project: dict[str, Any] = {}
        self.parser: PMParser
        self.activate(self._initial_project())

    @staticmethod
    def _initial_project() -> dict[str, Any]:
        env_root = os.environ.get("PM_QA_COCKPIT_PROJECT_ROOT")
        if env_root:
            return projects.find_project_for_path(env_root) or projects.adhoc_project(env_root)
        active = projects.get_active_project()
        if active and Path(active["code_path"]).is_dir():
            return active
        return projects.find_project_for_path(Path.cwd()) or projects.adhoc_project(Path.cwd())

    @staticmethod
    def _pool_signature(project: dict[str, Any]) -> str:
        return json.dumps(
            [project.get(k) for k in ("code_path", "docs_path", "prefix", "extra_tags")],
            sort_keys=True,
            default=str,
        )

    def parser_for(self, project: dict[str, Any]) -> PMParser:
        """The pooled parser for ``project``, rebuilt when its code/docs path, prefix or tags changed."""
        slug = project.get("slug")
        if not slug:
            return PMParser(project=project)
        signature = self._pool_signature(project)
        with self._pool_lock:
            entry = self._pool.get(slug)
            if entry is None or entry[0] != signature:
                parser = PMParser(project=project)
                parser.parser_provider = self.parser_for
                entry = (signature, parser)
                self._pool[slug] = entry
            return entry[1]

    def activate(self, project: dict[str, Any]) -> None:
        new_parser = self.parser_for(project)
        new_parser._cards_cache = None  # a reused parser must not serve a stale card list after a switch
        with self.lock:
            self.project = project
            self.parser = new_parser
            # One worker per project, kept across switches so its last run's status and log survive.
            # It is rebuilt only when what it runs against changed (reconfigured docs_path or gate
            # script) and it is not mid-run.
            current = self.workers.get(project["slug"])
            if current is None:
                self.workers[project["slug"]] = HeavyGateWorker(new_parser)
            elif _worker_config(current.parser) != _worker_config(new_parser):
                with current.lock:
                    running = current.status == "running"
                if not running:
                    self.workers[project["slug"]] = HeavyGateWorker(new_parser)

    @property
    def worker(self) -> HeavyGateWorker:
        return self.workers[self.project["slug"]]

    def project_view(self) -> dict[str, Any]:
        view = projects.public_view()
        view["current"] = _public_extra_tags(dict(self.project))
        if self.project.get("slug") and not any(p["slug"] == self.project["slug"] for p in view["projects"]):
            view["projects"] = [_public_extra_tags(dict(self.project)), *view["projects"]]
        cur_slug = self.project.get("slug")
        try:
            cur_active_count = sum(1 for c in self.parser.get_all_cards() if c.get("status") == "in-progress")
        except Exception:
            cur_active_count = 0
        enriched_projects = []
        for p in view["projects"]:
            p_copy = _public_extra_tags(dict(p))
            if cur_slug and p_copy.get("slug") == cur_slug:
                p_copy["building_count"] = cur_active_count
            else:
                p_copy["building_count"] = count_building_cards(p_copy)
            enriched_projects.append(p_copy)
        view["projects"] = enriched_projects
        if view.get("current"):
            view["current"]["building_count"] = cur_active_count
        return view


STATE = CockpitState()


class _WorkerProxy:
    """Legacy proxy so `from server import HEAVY_WORKER` works for money-nest compatibility."""

    def __getattr__(self, name: str) -> Any:
        return getattr(STATE.worker, name)


HEAVY_WORKER = _WorkerProxy()


def send_ntfy_notification(
    topic: str,
    *,
    title: str = "PM & QA Board",
    message: str = "",
    priority: int | str = 3,
    tags: list[str] | None = None,
    click_url: str | None = None,
    timeout: float = 6.0,
) -> tuple[bool, str]:
    """Dispatch push notification via ntfy.sh (standard library urllib.request)."""
    import urllib.request

    clean_topic = topic.strip()
    if clean_topic.startswith("http://") or clean_topic.startswith("https://"):
        endpoint = clean_topic.rstrip("/")
        slug_topic = endpoint.split("/")[-1]
    else:
        slug_topic = clean_topic.lstrip("/")
        endpoint = "https://ntfy.sh"

    if not slug_topic:
        return False, "Empty ntfy topic"

    payload: dict[str, Any] = {
        "topic": slug_topic,
        "title": title,
        "message": message,
        "priority": priority,
    }
    if tags:
        payload["tags"] = tags
    if click_url:
        payload["click"] = click_url

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "PM-QA-Cockpit/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status in (200, 201, 202, 204):
                return True, f"Notification sent to {slug_topic}"
            return False, f"ntfy.sh returned HTTP {resp.status}"
    except Exception as e:
        return False, str(e)


class NotificationWatcher(threading.Thread):
    """Background watcher that polls card transitions across registered projects and sends ntfy alerts."""

    def __init__(self, interval: float = 10.0):
        super().__init__(daemon=True, name="NotificationWatcher")
        self.interval = interval
        self.stop_event = threading.Event()
        self.last_card_statuses: dict[str, dict[str, str]] = {}
        self.initialized_projects: set[str] = set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.check_transitions()
            except Exception:
                pass
            self.stop_event.wait(self.interval)

    def stop(self) -> None:
        self.stop_event.set()

    def check_transitions(self) -> list[dict[str, Any]]:
        """Poll card transitions and dispatch ntfy alerts. Returns list of triggered alerts (for testing)."""
        dispatched: list[dict[str, Any]] = []
        registry = projects.load_registry()
        all_projects = list(registry.get("projects", {}).values())
        if not all_projects and STATE.project:
            all_projects = [STATE.project]

        for proj in all_projects:
            slug = proj.get("slug")
            if not slug:
                continue
            docs_path = proj.get("docs_path")
            if not docs_path or not Path(docs_path).is_dir():
                continue

            try:
                if STATE.project.get("slug") == slug:
                    parser = STATE.parser
                else:
                    parser = STATE.parser_for(proj)
                cards = parser.get_all_cards(refresh=True)
            except Exception:
                continue

            curr_map = {
                (c.get("mn_id") or c.get("id")): c.get("status")
                for c in cards
                if (c.get("mn_id") or c.get("id"))
            }

            if slug not in self.initialized_projects:
                self.last_card_statuses[slug] = curr_map
                self.initialized_projects.add(slug)
                continue

            prev_map = self.last_card_statuses.get(slug, {})

            for card in cards:
                card_id = card.get("mn_id") or card.get("id")
                if not card_id:
                    continue
                old_status = prev_map.get(card_id)
                new_status = card.get("status")

                if old_status and old_status != new_status:
                    if new_status in ("qa", "completed"):
                        alert_info = self._handle_transition(proj, card, old_status, new_status)
                        if alert_info:
                            dispatched.append(alert_info)

            self.last_card_statuses[slug] = curr_map

        return dispatched

    def _handle_transition(
        self, proj: dict[str, Any], card: dict[str, Any], old_status: str, new_status: str
    ) -> dict[str, Any] | None:
        slug = proj.get("slug", "project")
        notif_cfg = projects.get_notification_config(slug)
        if not notif_cfg.get("push_enabled", True):
            return None

        topic = notif_cfg.get("ntfy_topic") or f"pmqa-{slug}"
        card_id = card.get("mn_id") or card.get("id")
        card_name = card.get("name") or card.get("title") or card_id

        token_q = f"&token={TOKEN}" if TOKEN else ""
        if TUNNEL_URL:
            base = TUNNEL_URL.rstrip("/")
        else:
            base = f"http://{get_lan_ip()}:{PORT}"

        if new_status == "qa":
            title = f"🔔 Ready for QA: {card_id}"
            message = f"{card_name} has finished building and is awaiting QA verification."
            click_url = f"{base}/?card={card_id}{token_q}#qa"
            priority = 4
            tags = ["bell", "inbox_tray"]
        elif new_status == "completed":
            title = f"Story Completed: {card_id}"
            message = f"{card_name} has been validated and completed."
            click_url = f"{base}/?card={card_id}{token_q}#pipeline"
            priority = 3
            tags = ["white_check_mark"]
        else:
            return None

        ok, msg = send_ntfy_notification(
            topic=topic,
            title=title,
            message=message,
            priority=priority,
            tags=tags,
            click_url=click_url,
        )
        return {
            "slug": slug,
            "card_id": card_id,
            "old_status": old_status,
            "new_status": new_status,
            "topic": topic,
            "ok": ok,
            "message": msg,
        }


NOTIFICATION_WATCHER = NotificationWatcher()

TOOL_ROOT = Path(__file__).resolve().parent.parent


def get_system_update_status() -> dict[str, Any]:
    """Inspect local git repository status vs remote origin/main."""
    tool_root = TOOL_ROOT
    curr_ver = "0.0.0"
    ver_file = tool_root / "VERSION"
    if ver_file.exists():
        try:
            curr_ver = ver_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    git_cmd = "git"
    fetch_err = None
    try:
        f_res = subprocess.run(
            [git_cmd, "fetch", "origin", "main"],
            cwd=str(tool_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if f_res.returncode != 0 and f_res.stderr:
            fetch_err = f_res.stderr.strip()
    except Exception as e:
        fetch_err = str(e)

    curr_sha = ""
    try:
        res = subprocess.run(
            [git_cmd, "rev-parse", "HEAD"],
            cwd=str(tool_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        curr_sha = res.stdout.strip()
    except Exception:
        pass

    remote_sha = ""
    try:
        res = subprocess.run(
            [git_cmd, "rev-parse", "origin/main"],
            cwd=str(tool_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        remote_sha = res.stdout.strip()
    except Exception:
        pass

    behind_count = 0
    ahead_count = 0
    if curr_sha and remote_sha:
        try:
            res = subprocess.run(
                [git_cmd, "rev-list", "--count", "HEAD..origin/main"],
                cwd=str(tool_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            behind_count = int(res.stdout.strip() or "0")
        except Exception:
            pass

        try:
            res = subprocess.run(
                [git_cmd, "rev-list", "--count", "origin/main..HEAD"],
                cwd=str(tool_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            ahead_count = int(res.stdout.strip() or "0")
        except Exception:
            pass

    is_dirty = False
    dirty_files: list[str] = []
    try:
        res = subprocess.run(
            [git_cmd, "status", "--porcelain"],
            cwd=str(tool_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        if lines:
            is_dirty = True
            dirty_files = lines
    except Exception:
        pass

    incoming_commits: list[dict[str, str]] = []
    if behind_count > 0:
        try:
            res = subprocess.run(
                [git_cmd, "log", "-n", "5", "--oneline", "HEAD..origin/main"],
                cwd=str(tool_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for line in res.stdout.splitlines():
                parts = line.strip().split(" ", 1)
                if len(parts) == 2:
                    incoming_commits.append({"sha": parts[0], "message": parts[1]})
                elif line.strip():
                    incoming_commits.append({"sha": parts[0], "message": ""})
        except Exception:
            pass

    return {
        "success": True,
        "current_version": curr_ver,
        "current_commit": curr_sha,
        "remote_commit": remote_sha,
        "behind_count": behind_count,
        "ahead_count": ahead_count,
        "is_dirty": is_dirty,
        "dirty_files": dirty_files,
        "incoming_commits": incoming_commits,
        "fetch_error": fetch_err,
    }


def apply_system_update(target_code_path: str | None = None) -> tuple[bool, dict[str, Any]]:
    """Pull origin/main fast-forward if worktree is clean, and check project skill status."""
    tool_root = TOOL_ROOT
    git_cmd = "git"

    # 1. Verify clean worktree
    try:
        res = subprocess.run(
            [git_cmd, "status", "--porcelain"],
            cwd=str(tool_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.stdout.strip():
            return False, {
                "success": False,
                "error": "Working tree has uncommitted changes. Please stash or commit before updating.",
                "dirty_files": [ln.strip() for ln in res.stdout.splitlines() if ln.strip()],
            }
    except Exception as e:
        return False, {"success": False, "error": f"Failed to check git status: {e}"}

    # 2. Record old SHA
    old_sha = ""
    try:
        res = subprocess.run(
            [git_cmd, "rev-parse", "HEAD"],
            cwd=str(tool_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        old_sha = res.stdout.strip()
    except Exception:
        pass

    # 3. Pull fast-forward
    try:
        pull_res = subprocess.run(
            [git_cmd, "pull", "--ff-only", "origin", "main"],
            cwd=str(tool_root),
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        if pull_res.returncode != 0:
            err_msg = pull_res.stderr.strip() or pull_res.stdout.strip() or "git pull failed"
            return False, {"success": False, "error": f"Update failed: {err_msg}"}
    except Exception as e:
        return False, {"success": False, "error": f"Update execution failed: {e}"}

    # 4. New SHA
    new_sha = ""
    try:
        res = subprocess.run(
            [git_cmd, "rev-parse", "HEAD"],
            cwd=str(tool_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        new_sha = res.stdout.strip()
    except Exception:
        pass

    # 5. List changed files
    changed_files: list[str] = []
    if old_sha and new_sha and old_sha != new_sha:
        try:
            res = subprocess.run(
                [git_cmd, "diff", "--name-only", f"{old_sha}..{new_sha}"],
                cwd=str(tool_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            changed_files = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        except Exception:
            pass

    # 6. Check skills state
    skills_outdated = False
    skills_status_data = None
    try:
        if str(TOOL_ROOT / "board") not in sys.path:
            sys.path.insert(0, str(TOOL_ROOT / "board"))
        import setup_flow

        target = target_code_path or (STATE.project.get("code_path") if STATE.project else None)
        skills_status_data = setup_flow.skills_status(target)
        for p_skill in skills_status_data.get("project", []):
            if p_skill.get("state") in ("outdated", "missing"):
                skills_outdated = True
                break
    except Exception:
        pass

    return True, {
        "success": True,
        "old_commit": old_sha,
        "new_commit": new_sha,
        "changed_files": changed_files,
        "skills_outdated": skills_outdated,
        "skills_status": skills_status_data,
    }


def get_active_code_dir() -> Path:
    """Return Path to active project's code_path, or current directory if not set."""
    if STATE.project and STATE.project.get("code_path"):
        p = Path(STATE.project["code_path"])
        if p.is_dir():
            return p
    return Path.cwd()


def get_git_sync_status(code_dir: Path | None = None) -> dict[str, Any]:
    """Inspect branch, upstream tracking, ahead/behind counts, current commit and porcelain status."""
    if code_dir is None:
        code_dir = get_active_code_dir()
    git_cmd = "git"

    branch = "main"
    try:
        res = subprocess.run(
            [git_cmd, "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(code_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            branch = res.stdout.strip()
    except Exception:
        pass

    ahead_count = 0
    behind_count = 0
    upstream = ""
    try:
        res = subprocess.run(
            [git_cmd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=str(code_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            upstream = res.stdout.strip()
            res2 = subprocess.run(
                [git_cmd, "rev-list", "--left-right", "--count", "HEAD...@{u}"],
                cwd=str(code_dir),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res2.returncode == 0:
                parts = res2.stdout.strip().split()
                if len(parts) >= 2:
                    ahead_count = int(parts[0])
                    behind_count = int(parts[1])
    except Exception:
        pass

    current_sha = ""
    try:
        res = subprocess.run(
            [git_cmd, "rev-parse", "HEAD"],
            cwd=str(code_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            current_sha = res.stdout.strip()
    except Exception:
        pass

    is_dirty = False
    dirty_files: list[dict[str, str]] = []
    try:
        res = subprocess.run(
            [git_cmd, "status", "--porcelain"],
            cwd=str(code_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            for raw_line in res.stdout.splitlines():
                line = raw_line.rstrip("\r\n")
                if not line.strip():
                    continue
                is_dirty = True
                if len(line) >= 4 and line[2] == " ":
                    status = line[:2]
                    path = line[3:].strip()
                else:
                    parts = line.split(maxsplit=1)
                    status = parts[0] if parts else ""
                    path = parts[1].strip() if len(parts) > 1 else ""
                dirty_files.append({"status": status.strip(), "path": path, "raw": line})
    except Exception:
        pass

    return {
        "success": True,
        "branch": branch,
        "upstream": upstream,
        "ahead_count": ahead_count,
        "behind_count": behind_count,
        "current_sha": current_sha,
        "is_dirty": is_dirty,
        "dirty_files": dirty_files,
    }


def git_commit_changes(
    message: str,
    files: list[str] | None = None,
    code_dir: Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Stage files (or all changes) and create a git commit with the given message."""
    if code_dir is None:
        code_dir = get_active_code_dir()
    git_cmd = "git"
    msg = (message or "").strip()
    if not msg:
        return False, {"success": False, "error": "Commit message cannot be empty."}

    # 1. Stage files
    try:
        if files and len(files) > 0:
            stage_cmd = [git_cmd, "add", "-A", "--"] + files
            res_add = subprocess.run(
                stage_cmd,
                cwd=str(code_dir),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if res_add.returncode != 0:
                return False, {"success": False, "error": f"Failed to stage files: {res_add.stderr.strip()}"}
        else:
            res_add = subprocess.run(
                [git_cmd, "add", "-A"],
                cwd=str(code_dir),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if res_add.returncode != 0:
                return False, {"success": False, "error": f"Failed to stage files: {res_add.stderr.strip()}"}
    except Exception as e:
        return False, {"success": False, "error": f"Staging error: {e}"}

    # 2. Commit using temporary file
    tf_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
            tf.write(msg)
            tf_path = tf.name

        res_commit = subprocess.run(
            [git_cmd, "commit", "-F", tf_path],
            cwd=str(code_dir),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if res_commit.returncode != 0:
            err = res_commit.stderr.strip() or res_commit.stdout.strip()
            return False, {"success": False, "error": f"Commit failed: {err}"}

        res_sha = subprocess.run(
            [git_cmd, "rev-parse", "HEAD"],
            cwd=str(code_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        new_sha = res_sha.stdout.strip() if res_sha.returncode == 0 else ""

        return True, {
            "success": True,
            "sha": new_sha,
            "message": msg,
            "output": res_commit.stdout.strip(),
        }
    except Exception as e:
        return False, {"success": False, "error": f"Commit execution error: {e}"}
    finally:
        if tf_path:
            try:
                Path(tf_path).unlink(missing_ok=True)
            except Exception:
                pass


def git_push_branch(code_dir: Path | None = None) -> tuple[bool, dict[str, Any]]:
    """Push local branch commits to remote."""
    if code_dir is None:
        code_dir = get_active_code_dir()
    git_cmd = "git"
    try:
        res = subprocess.run(
            [git_cmd, "push"],
            cwd=str(code_dir),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if res.returncode == 0:
            out = res.stdout.strip() or res.stderr.strip()
            return True, {"success": True, "output": out}
        else:
            err = res.stderr.strip() or res.stdout.strip()
            return False, {"success": False, "error": f"Push failed: {err}"}
    except Exception as e:
        return False, {"success": False, "error": f"Push execution error: {e}"}


def git_pull_branch(code_dir: Path | None = None) -> tuple[bool, dict[str, Any]]:
    """Pull upstream commits to local branch."""
    if code_dir is None:
        code_dir = get_active_code_dir()
    git_cmd = "git"
    try:
        res = subprocess.run(
            [git_cmd, "pull"],
            cwd=str(code_dir),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if res.returncode == 0:
            out = res.stdout.strip() or res.stderr.strip()
            return True, {"success": True, "output": out}
        else:
            err = res.stderr.strip() or res.stdout.strip()
            return False, {"success": False, "error": f"Pull failed: {err}"}
    except Exception as e:
        return False, {"success": False, "error": f"Pull execution error: {e}"}



class PMRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        # Custom concise logging
        sys.stderr.write(
            f"[{self.log_date_time_string()}] {self.command} {self.path} - {args[1]}\n"
        )

    def send_json(self, data: any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self) -> None:
        # The UI is same-origin; no cross-origin access is granted (no Access-Control-Allow-* headers).
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        super().send_error(code, message, explain)

    def is_authenticated(self) -> bool:
        """Check if request is authorized when session token protection is active."""
        if not TOKEN:
            return True
        # Local loopback client without tunnel/proxy headers is always allowed
        client_ip = self.client_address[0] if self.client_address else ""
        is_loopback = client_ip in ("127.0.0.1", "::1", "localhost")
        has_forwarded = any(
            self.headers.get(h)
            for h in ("CF-Ray", "CF-Connecting-IP", "X-Forwarded-For", "X-Forwarded-Host")
        )
        if is_loopback and not has_forwarded:
            return True

        # Extract token from query params, headers, or cookie
        parsed_url = urllib.parse.urlparse(self.path)
        query_token = urllib.parse.parse_qs(parsed_url.query).get("token", [None])[0]
        if query_token and query_token == TOKEN:
            return True

        header_token = self.headers.get("X-Cockpit-Token")
        if header_token and header_token == TOKEN:
            return True

        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and auth_header[7:].strip() == TOKEN:
            return True

        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            cookies = [c.strip() for c in cookie_header.split(";")]
            for c in cookies:
                if c.startswith("cockpit_token=") and c[len("cockpit_token=") :] == TOKEN:
                    return True

        return False

    def post_allowed(self) -> bool:
        """CSRF guard for state-changing calls.

        A web page the user visits can POST to localhost with a "simple" text/plain body that skips
        the CORS preflight. Requiring a JSON content type forces a preflight (which we never grant),
        and a present Origin must be this server's own.
        """
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # non-browser clients (CLI, tests) send no Origin
        port = self.server.server_address[1]
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        host = self.headers.get("Host")
        if host:
            allowed.add(f"http://{host}")
            allowed.add(f"https://{host}")
        fwd_host = self.headers.get("X-Forwarded-Host")
        if fwd_host:
            allowed.add(f"http://{fwd_host}")
            allowed.add(f"https://{fwd_host}")
        if TUNNEL_URL:
            t_clean = TUNNEL_URL.rstrip("/")
            allowed.add(t_clean)
            parsed_t = urllib.parse.urlparse(t_clean)
            if parsed_t.netloc:
                allowed.add(f"http://{parsed_t.netloc}")
                allowed.add(f"https://{parsed_t.netloc}")
        lan_ip = get_lan_ip()
        if lan_ip:
            allowed.add(f"http://{lan_ip}:{port}")
        return origin in allowed

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if not self.is_authenticated():
            if path.startswith("/api/"):
                self.send_json(
                    {"error": "Unauthorized: invalid or missing session token"},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            body = (
                b"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
                b"<title>Unauthorized - PM &amp; QA Board</title>"
                b"<style>body{background:#09090b;color:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;"
                b"display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:1.5rem;box-sizing:border-box;}"
                b".box{background:#18181b;border:1px solid #27272a;border-radius:1rem;padding:2rem;max-width:440px;width:100%;text-align:center;box-shadow:0 10px 25px -5px rgba(0,0,0,0.5);}"
                b"h1{font-size:1.25rem;color:#f43f5e;margin-top:0;font-weight:700;}p{font-size:0.875rem;color:#a1a1aa;line-height:1.5;margin:0.75rem 0;}"
                b"code{background:#09090b;padding:0.2rem 0.4rem;border-radius:0.375rem;color:#10b981;font-size:0.8rem;border:1px solid #27272a;}"
                b".form{margin-top:1.25rem;display:flex;flex-direction:column;gap:0.75rem;}"
                b"input{background:#09090b;border:1px solid #3f3f46;border-radius:0.5rem;padding:0.6rem 0.8rem;color:#f4f4f5;font-size:0.875rem;outline:none;text-align:center;box-sizing:border-box;width:100%;}"
                b"input:focus{border-color:#10b981;box-shadow:0 0 0 2px rgba(16,185,129,0.2);}"
                b"button{background:#10b981;color:#ffffff;border:none;border-radius:0.5rem;padding:0.65rem 1rem;font-size:0.875rem;font-weight:600;cursor:pointer;transition:background 0.2s;}"
                b"button:hover{background:#059669;}"
                b"</style></head>"
                b"<body><div class=\"box\">"
                b"<h1>401 - Session Token Required</h1>"
                b"<p>This PM &amp; QA Board instance is running with token authentication enabled.</p>"
                b"<p>Scan the QR code from the desktop dashboard or enter your session token below to connect:</p>"
                b"<form class=\"form\" onsubmit=\"event.preventDefault();submitToken();\">"
                b"<input type=\"text\" id=\"tok\" placeholder=\"Paste session token here...\" autocomplete=\"off\" required>"
                b"<button type=\"submit\">Connect to Board</button>"
                b"</form>"
                b"<script>"
                b"function submitToken(){"
                b"var t=document.getElementById('tok').value.trim();"
                b"if(!t)return;"
                b"try{localStorage.setItem('cockpit_token',t);sessionStorage.setItem('cockpit_token',t);}catch(e){}"
                b"document.cookie='cockpit_token='+encodeURIComponent(t)+'; Path=/; SameSite=Lax; Secure';"
                b"window.location.href=window.location.pathname+'?token='+encodeURIComponent(t);"
                b"}"
                b"</script>"
                b"</div></body></html>"
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self.wfile.write(body)
            return

        # ----------------------------------------------------------------------
        # API Routes
        # ----------------------------------------------------------------------
        if path == "/api/server-info":
            lan_ip = get_lan_ip()
            port = self.server.server_address[1]
            self.send_json(
                {
                    "host": HOST,
                    "port": port,
                    "lan_ip": lan_ip,
                    "lan_url": f"http://{lan_ip}:{port}",
                    "tunnel_url": TUNNEL_URL,
                    "token": TOKEN if TOKEN else None,
                    "token_required": bool(TOKEN),
                    "keep_alive": KEEP_ALIVE,
                }
            )
            return

        if path == "/api/projects":
            self.send_json(STATE.project_view())
            return

        if path.startswith("/artifacts/"):
            self.serve_artifact(urllib.parse.unquote(path[len("/artifacts/"):]))
            return

        if path == "/api/analytics":
            query_params = urllib.parse.parse_qs(parsed_url.query)
            window = query_params.get("window", ["6w"])[0]
            project_slug = query_params.get("project", [None])[0]
            data = STATE.parser.get_analytics_summary(window=window, project_slug=project_slug)
            self.send_json(data)
            return

        if path == "/api/cards":
            cards = STATE.parser.get_all_cards()
            self.send_json({"cards": cards, "total": len(cards)})
            return

        if path == "/api/qa":
            cards = STATE.parser.get_all_cards()
            qa_cards = [c for c in cards if c["status"] == "qa"]

            def _qa_sort_key(c: dict[str, Any]) -> tuple[str, str]:
                end_t = c.get("end_time") or ""
                if not end_t:
                    cl = c.get("changelog") or []
                    if cl:
                        last = cl[-1]
                        end_t = last.get("datetime") or f"{last.get('date', '')} {last.get('time', '')}".strip()
                if not end_t:
                    end_t = str(c.get("updated_at") or c.get("created_date") or "")
                return (end_t, str(c.get("mn_id") or ""))

            qa_cards.sort(key=_qa_sort_key, reverse=True)
            self.send_json({"qa_cards": qa_cards, "total": len(qa_cards)})
            return

        if path == "/api/sprints":
            plan_data = STATE.parser.parse_plan()
            cards = STATE.parser.get_all_cards()

            # Group cards by sprint
            cards_by_sprint: dict[str, list[dict]] = {}
            for c in cards:
                s = c["sprint"] or "Unscheduled"
                cards_by_sprint.setdefault(s, []).append(c)

            self.send_json(
                {
                    "roadmap": plan_data["roadmap"],
                    "sprint_details": plan_data["sprints"],
                    "cards_by_sprint": cards_by_sprint,
                }
            )
            return

        if path == "/api/activity":
            activity = STATE.parser.get_git_activity(limit=15)
            # Add summary counts
            cards = STATE.parser.get_all_cards()
            commits = activity.get("commits") or []
            first_commit_date = str(commits[0].get("date", ""))[:10] if commits else ""
            completed_today = [
                c
                for c in cards
                if c.get("validated")
                and first_commit_date
                and str(c["validated"]).startswith(first_commit_date)
            ]
            activity["completed_today_count"] = len(completed_today)
            self.send_json(activity)
            return

        if path.startswith("/api/sprints/") and path.endswith("/doc"):
            prefix = "/api/sprints/"
            suffix = "/doc"
            raw_sprint = path[len(prefix) : -len(suffix)]
            sprint_id = urllib.parse.unquote(raw_sprint).strip()
            doc = STATE.parser.get_sprint_doc(sprint_id)
            if doc:
                self.send_json(doc)
            else:
                self.send_json(
                    {"error": f"Document for sprint '{sprint_id}' not found", "sprint": sprint_id},
                    status=HTTPStatus.NOT_FOUND,
                )
            return

        if path == "/api/active-sprint":
            query_params = urllib.parse.parse_qs(parsed_url.query)
            target_sprint = query_params.get("sprint", [None])[0]
            if not target_sprint or target_sprint.upper() in ("ACTIVE", "ALL"):
                overview = STATE.parser.get_active_streams_overview()
            else:
                overview = STATE.parser.get_active_sprint_overview(target_sprint)
            self.send_json(overview)
            return

        if path == "/api/active-streams":
            overview = STATE.parser.get_active_streams_overview()
            self.send_json(overview)
            return

        if path == "/api/notifications/config":
            query_params = urllib.parse.parse_qs(parsed_url.query)
            target_slug = query_params.get("slug", [None])[0] or STATE.project.get("slug")
            cfg = projects.get_notification_config(target_slug)
            self.send_json(
                {
                    "success": True,
                    "slug": target_slug,
                    "config": cfg,
                    "tunnel_url": TUNNEL_URL,
                    "lan_ip": get_lan_ip(),
                    "port": PORT,
                    "token": TOKEN,
                }
            )
            return

        if path == "/api/system/update-status":
            info = get_system_update_status()
            self.send_json(info)
            return

        if path == "/api/git/sync-status":
            info = get_git_sync_status()
            self.send_json(info)
            return

        if path == "/api/heavy-gate/status":
            gate_info = STATE.parser.get_heavy_gate_info()
            worker_status = STATE.worker.get_status()
            gates = STATE.parser.project.get("gates") or {}
            self.send_json({"worker": worker_status, "gate_info": gate_info, "gates": gates})
            return

        if path == "/api/heavy-gate/sprints":
            sprint_statuses = STATE.parser.get_sprint_gate_status()
            worker_status = STATE.worker.get_status()
            gates = STATE.parser.project.get("gates") or {}
            self.send_json(
                {
                    "sprints": sprint_statuses,
                    "total": len(sprint_statuses),
                    "worker": worker_status,
                    "gates": gates,
                }
            )
            return


        if path == "/api/heavy-gate/log":
            query_params = urllib.parse.parse_qs(parsed_url.query)
            limit_str = query_params.get("limit", ["100"])[0]
            try:
                limit = max(1, min(1000, int(limit_str)))
            except ValueError:
                limit = 100
            lines = STATE.worker.get_log(limit=limit)
            self.send_json({"lines": lines, "total": len(lines)})
            return

        if path.startswith("/api/reviews/card/"):
            prefix = "/api/reviews/card/"
            raw_card = path[len(prefix):]
            card_id = urllib.parse.unquote(raw_card).strip()
            reviews = STATE.parser.get_reviews_for_card(card_id)
            self.send_json({"card_id": card_id, "reviews": reviews, "total": len(reviews)})
            return

        if path.startswith("/api/reviews/"):
            prefix = "/api/reviews/"
            raw_sprint = path[len(prefix):]
            sprint_id = urllib.parse.unquote(raw_sprint).strip()
            reviews = STATE.parser.get_reviews_for_sprint(sprint_id)
            self.send_json({"sprint": sprint_id, "reviews": reviews, "total": len(reviews)})
            return

        if path == "/api/heavy-gate/failures":
            query_params = urllib.parse.parse_qs(parsed_url.query)
            target_sprint = query_params.get("sprint", [None])[0]
            target_run = query_params.get("run", [None])[0]
            failures_info = STATE.parser.get_heavy_gate_failures(sprint_id=target_sprint, run_id=target_run)
            self.send_json(failures_info)
            return

        # ----------------------------------------------------------------------
        # Static File Serving
        # ----------------------------------------------------------------------
        if path == "/" or path == "/index.html":
            target_file = (STATIC_DIR / "index.html").resolve()
        else:
            rel_path = path.lstrip("/").replace("static/", "")
            target_file = (STATIC_DIR / rel_path).resolve()

        # Path traversal guard: ensure resolved path is strictly within STATIC_DIR
        try:
            target_file.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "Access denied")
            return

        if target_file.is_file() and target_file.exists():
            mime_type, _ = mimetypes.guess_type(str(target_file))
            if not mime_type:
                mime_type = "application/octet-stream"

            content = target_file.read_bytes()
            self.send_response(HTTPStatus.OK)
            if mime_type.startswith("text/") or mime_type in (
                "application/javascript",
                "application/json",
                "image/svg+xml",
            ):
                self.send_header("Content-Type", f"{mime_type}; charset=utf-8")
            else:
                self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Connection", "close")
            self.close_connection = True
            if TOKEN and (path == "/" or path == "/index.html"):
                is_secure = (
                    self.headers.get("X-Forwarded-Proto") == "https"
                    or (TUNNEL_URL and TUNNEL_URL.startswith("https"))
                )
                secure_flag = "; Secure" if is_secure else ""
                self.send_header("Set-Cookie", f"cockpit_token={TOKEN}; Path=/; SameSite=Lax{secure_flag}")
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, f"File {path} not found")

    def serve_artifact(self, name: str) -> None:
        """Serve a Story Close-Out artifact from the active project's ``<docs_path>/artifacts``."""
        base = STATE.parser.artifacts_dir.resolve()
        target = (base / name).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, f"Artifact {name} not found")
            return
        mime_type, _ = mimetypes.guess_type(str(target))
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime_type or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        # Read JSON body
        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len > 0:
            raw_body = self.rfile.read(content_len).decode("utf-8")
            try:
                body = json.loads(raw_body)
            except Exception:
                pass
        if not isinstance(body, dict):
            body = {}

        if not self.is_authenticated():
            self.send_json(
                {"success": False, "error": "Unauthorized: invalid or missing session token"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return

        if not self.post_allowed():
            self.send_json({"success": False, "error": "Rejected: JSON body from this origin required"}, status=403)
            return

        # ----------------------------------------------------------------------
        # Project registry: switch / configure
        # ----------------------------------------------------------------------
        if path == "/api/projects/select":
            slug = str(body.get("slug", "")).strip()
            try:
                project = projects.select_project(slug)
            except KeyError:
                self.send_json({"success": False, "error": f"Unknown project '{slug}'"}, status=404)
                return
            if not Path(project["code_path"]).is_dir():
                self.send_json({"success": False, "error": f"code_path missing: {project['code_path']}"}, status=400)
                return
            STATE.activate(project)
            self.send_json({"success": True, "project": _public_extra_tags(dict(project)), **STATE.project_view()})
            return

        if path == "/api/projects/configure":
            # Gate commands are never taken from a request body (they choose what the heavy-gate
            # runner executes); only `cli.py profile-gates --apply` sets them. Reconfiguring an
            # existing project keeps its saved profile.
            slug = body.get("slug") or projects.slugify(str(body.get("name", "")))
            existing = projects.load_registry()["projects"].get(slug) or {}
            try:
                project = projects.build_project(
                    body.get("name", ""),
                    body.get("code_path", ""),
                    slug=body.get("slug") or None,
                    prefix=body.get("prefix") or None,
                    docs_path=body.get("docs_path") or None,
                    docs_mode=body.get("docs_mode") or None,
                    gates=existing.get("gates"),
                    notifications=existing.get("notifications"),
                    extra_tags=existing.get("extra_tags"),
                )
            except ValueError as err:
                self.send_json({"success": False, "error": str(err)}, status=400)
                return
            created = projects.ensure_docs_root(project)
            activate = bool(body.get("activate", True))
            projects.upsert_project(project, activate=activate)
            if activate:
                STATE.activate(project)
            self.send_json({"success": True, "project": _public_extra_tags(dict(project)), "created": created, **STATE.project_view()})
            return

        # ----------------------------------------------------------------------
        # Notification Config & Test Alert
        # ----------------------------------------------------------------------
        if path == "/api/notifications/config":
            target_slug = body.get("slug") or STATE.project.get("slug")
            try:
                cfg = projects.update_notification_config(target_slug, body)
                self.send_json({"success": True, "slug": target_slug, "config": cfg})
            except Exception as err:
                self.send_json({"success": False, "error": str(err)}, status=400)
            return

        if path == "/api/notifications/test":
            target_slug = body.get("slug") or STATE.project.get("slug")
            cfg = projects.get_notification_config(target_slug)
            topic = body.get("topic") or cfg.get("ntfy_topic") or f"pmqa-{target_slug or 'test'}"
            token_q = f"?token={TOKEN}" if TOKEN else ""
            click_url = f"{TUNNEL_URL.rstrip('/')}/{token_q}" if TUNNEL_URL else f"http://{get_lan_ip()}:{PORT}/{token_q}"
            ok, msg = send_ntfy_notification(
                topic=topic,
                title="PM & QA Board — Test Alert",
                message="🔔 Test alert from PM & QA Board! Audio, haptic, and mobile push alerts are operational.",
                priority=3,
                tags=["bell"],
                click_url=click_url,
            )
            if ok:
                self.send_json({"success": True, "message": msg, "topic": topic})
            else:
                self.send_json({"success": False, "error": msg, "topic": topic}, status=502)
            return

        if path == "/api/system/update-apply":
            target_code = body.get("code_path") if isinstance(body, dict) else None
            ok, result = apply_system_update(target_code)
            if ok:
                self.send_json(result)
            else:
                self.send_json(result, status=400)
            return

        if path == "/api/git/commit":
            message = body.get("message", "") if isinstance(body, dict) else ""
            files = body.get("files") if isinstance(body, dict) else None
            ok, result = git_commit_changes(message, files)
            if ok:
                self.send_json(result)
            else:
                self.send_json(result, status=400)
            return

        if path == "/api/git/push":
            ok, result = git_push_branch()
            if ok:
                self.send_json(result)
            else:
                self.send_json(result, status=400)
            return

        if path == "/api/git/pull":
            ok, result = git_pull_branch()
            if ok:
                self.send_json(result)
            else:
                self.send_json(result, status=400)
            return


        # ----------------------------------------------------------------------
        # Toggle DoD checkbox
        # ----------------------------------------------------------------------
        if path.startswith("/api/cards/") and (path.endswith("/dod") or path.endswith("/toggle-dod")):
            # Format: /api/cards/{mn_id}/dod or /api/cards/{mn_id}/toggle-dod
            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                mn_id = parts[2]
                index = body.get("index", 0)
                checked = body.get("checked", False)
                ok, msg = STATE.parser.toggle_dod_item(mn_id, index, checked)
                if ok:
                    invalidate_building_count_cache(STATE.project.get("docs_path"))
                    self.send_json({"success": True, "message": msg})
                else:
                    self.send_json({"success": False, "error": msg}, status=400)
                return

        # ----------------------------------------------------------------------
        # Toggle Human QA checkbox
        # ----------------------------------------------------------------------
        if path.startswith("/api/cards/") and path.endswith("/qa-item"):
            # Format: /api/cards/{mn_id}/qa-item
            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                mn_id = parts[2]
                index = body.get("index", 0)
                checked = body.get("checked", False)
                ok, msg = STATE.parser.toggle_qa_item(mn_id, index, checked)
                if ok:
                    invalidate_building_count_cache(STATE.project.get("docs_path"))
                    self.send_json({"success": True, "message": msg})
                else:
                    self.send_json({"success": False, "error": msg}, status=400)
                return

        # ----------------------------------------------------------------------
        # Validate Card (Complete QA)
        # ----------------------------------------------------------------------
        if path.startswith("/api/cards/") and path.endswith("/validate"):
            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                mn_id = parts[2]
                notes = body.get("notes", "")
                commit = body.get("commit", True)
                ok, msg = STATE.parser.validate_card(mn_id, notes=notes, do_commit=commit)
                if ok:
                    invalidate_building_count_cache(STATE.project.get("docs_path"))
                    self.send_json({"success": True, "message": msg})
                else:
                    self.send_json({"success": False, "error": msg}, status=400)
                return

        # ----------------------------------------------------------------------
        # Toggle Sprint DoD checkbox in PLAN.md
        # ----------------------------------------------------------------------
        if path.startswith("/api/sprints/") and (path.endswith("/dod") or path.endswith("/toggle-dod")):
            # Format: /api/sprints/{sprint_id}/dod or /api/sprints/{sprint_id}/toggle-dod
            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                raw_sprint = parts[2]
                sprint_id = urllib.parse.unquote(raw_sprint).strip()
                index = body.get("index", 0)
                checked = body.get("checked", False)
                ok, msg = STATE.parser.toggle_sprint_dod_item(sprint_id, index, checked)
                if ok:
                    invalidate_building_count_cache(STATE.project.get("docs_path"))
                    self.send_json({"success": True, "message": msg})
                else:
                    self.send_json({"success": False, "error": msg}, status=400)
                return

        # ----------------------------------------------------------------------
        # Heavy Gate start/stop
        # ----------------------------------------------------------------------
        if path in ("/api/heavy-gate/start", "/api/fast-gate/run", "/api/suite/run"):
            args = body.get("args")
            target_sprint = body.get("sprint")
            target_sha = body.get("sha")
            all_pending = body.get("all_pending", False)

            sprint_queue = None
            if all_pending:
                # Find all unverified sprints from oldest to newest
                all_sprints = STATE.parser.get_sprint_gate_status()
                # Order oldest to newest: reverse since roadmap lists chronological or sort
                unverified = [{"sprint": s["sprint"], "sha": s.get("sha")} for s in all_sprints if not s.get("verified")]
                if not unverified:
                    self.send_json(
                        {"success": False, "error": "No pending or unverified sprints found"},
                        status=400,
                    )
                    return
                sprint_queue = unverified

            ok, msg = STATE.worker.start(
                args=args,
                sprint=target_sprint,
                sha=target_sha,
                sprint_queue=sprint_queue,
            )
            if ok:
                self.send_json(
                    {
                        "success": True,
                        "message": msg,
                        "worker": STATE.worker.get_status(),
                    }
                )
            else:
                self.send_json(
                    {
                        "success": False,
                        "error": msg,
                        "worker": STATE.worker.get_status(),
                    },
                    status=400,
                )
            return

        if path == "/api/heavy-gate/stop":
            ok, msg = STATE.worker.stop()
            if ok:
                self.send_json(
                    {
                        "success": True,
                        "message": msg,
                        "worker": STATE.worker.get_status(),
                    }
                )
            else:
                self.send_json(
                    {
                        "success": False,
                        "error": msg,
                        "worker": STATE.worker.get_status(),
                    },
                    status=400,
                )
            return

        self.send_error(HTTPStatus.NOT_FOUND, f"POST route {path} not found")


def main(argv: list[str] | None = None) -> None:
    global HOST, PORT, TOKEN, TUNNEL_URL, KEEP_ALIVE
    import argparse

    parser = argparse.ArgumentParser(description="PM & QA Board Server")
    parser.add_argument("--host", default=HOST, help="Host address to bind to (e.g. 127.0.0.1 or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=PORT, help="Port to listen on (default 3333)")
    parser.add_argument("--token", default=TOKEN, help="Session token for remote authentication")
    parser.add_argument("--tunnel-url", default=TUNNEL_URL, help="Public tunnel URL (e.g. from Cloudflare)")
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        default=KEEP_ALIVE,
        help="Prevent Windows sleep while running",
    )
    args = parser.parse_args(argv)

    HOST = args.host
    PORT = args.port
    TOKEN = args.token
    TUNNEL_URL = args.tunnel_url
    if args.keep_alive or (HOST not in ("127.0.0.1", "localhost")):
        KEEP_ALIVE = True

    lan_ip = get_lan_ip()
    print("=" * 60)
    print("  PM & QA Board")
    print(f"  Project:    {STATE.project.get('name')} ({STATE.project.get('docs_mode')})")
    print(f"  Local:      http://localhost:{PORT}")
    if HOST not in ("127.0.0.1", "localhost"):
        print(f"  LAN (Wi-Fi):http://{lan_ip}:{PORT}")
    if TUNNEL_URL:
        tunnel_display = TUNNEL_URL + (f"?token={TOKEN}" if TOKEN else "")
        print(f"  Tunnel:     {tunnel_display}")
    if KEEP_ALIVE:
        ka_active = set_keep_alive(True)
        print(f"  Keep-Alive: {'Active (sleep prevented)' if ka_active else 'Enabled'}")
    if TOKEN:
        print("  Security:   Session token active for remote clients")
    print("=" * 60)

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), PMRequestHandler)
    # Pre-warm cards cache in background so first browser request is instantaneous
    threading.Thread(target=STATE.parser.get_all_cards, daemon=True).start()
    # Start background notification watcher for card transitions (Awaiting QA / Completed)
    NOTIFICATION_WATCHER.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        NOTIFICATION_WATCHER.stop()
        if KEEP_ALIVE:
            set_keep_alive(False)
        server.server_close()


if __name__ == "__main__":
    main()
