"""
server.py - PM & QA Cockpit local web server (multi-project).

Runs on port 3333 with zero external dependencies (Python standard library).
Provides REST APIs for story card inspection, interactive QA validation, git status,
and switching between the projects registered in ``~/.pm-qa-cockpit/projects.json``.
"""

from __future__ import annotations

import collections
import datetime
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import projects
from parser import PMParser

PORT = int(os.environ.get("PM_QA_COCKPIT_PORT", "3333"))
STATIC_DIR = Path(__file__).resolve().parent / "static"


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
            heavy = (self.parser.project.get("gates") or {}).get("heavy_script")
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

            p = subprocess.Popen(
                cmd,
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
                header = f"\n=== [Sprint Quality Auditor] Running finalgates.ps1"
                if sprint_target:
                    header += f" for Sprint {sprint_target}"
                header += " ===\n"
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
            return p.wait()

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


class CockpitState:
    """The active project's parser + heavy-gate worker; swapped atomically on project switch."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.workers: dict[str, HeavyGateWorker] = {}
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

    def activate(self, project: dict[str, Any]) -> None:
        new_parser = PMParser(project=project)
        with self.lock:
            self.project = project
            self.parser = new_parser
            # One worker per project so a running heavy gate keeps its own log across switches;
            # an idle worker is rebuilt so a reconfigured docs_path / gate script takes effect.
            current = self.workers.get(project["slug"])
            if current is None or current.status != "running":
                self.workers[project["slug"]] = HeavyGateWorker(new_parser)

    @property
    def worker(self) -> HeavyGateWorker:
        return self.workers[self.project["slug"]]

    def project_view(self) -> dict[str, Any]:
        view = projects.public_view()
        view["current"] = self.project
        if self.project.get("slug") and not any(p["slug"] == self.project["slug"] for p in view["projects"]):
            view["projects"] = [self.project, *view["projects"]]
        return view


STATE = CockpitState()


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
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self) -> None:
        # The UI is same-origin; no cross-origin access is granted (no Access-Control-Allow-* headers).
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

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
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        # ----------------------------------------------------------------------
        # API Routes
        # ----------------------------------------------------------------------
        if path == "/api/projects":
            self.send_json(STATE.project_view())
            return

        if path.startswith("/artifacts/"):
            self.serve_artifact(urllib.parse.unquote(path[len("/artifacts/"):]))
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

        if path == "/api/heavy-gate/status":
            gate_info = STATE.parser.get_heavy_gate_info()
            worker_status = STATE.worker.get_status()
            self.send_json({"worker": worker_status, "gate_info": gate_info})
            return

        if path == "/api/heavy-gate/sprints":
            sprint_statuses = STATE.parser.get_sprint_gate_status()
            worker_status = STATE.worker.get_status()
            self.send_json(
                {
                    "sprints": sprint_statuses,
                    "total": len(sprint_statuses),
                    "worker": worker_status,
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
            self.send_header("Content-Type", f"{mime_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
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
            self.send_json({"success": True, "project": project, **STATE.project_view()})
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
                )
            except ValueError as err:
                self.send_json({"success": False, "error": str(err)}, status=400)
                return
            created = projects.ensure_docs_root(project)
            activate = bool(body.get("activate", True))
            projects.upsert_project(project, activate=activate)
            if activate:
                STATE.activate(project)
            self.send_json({"success": True, "project": project, "created": created, **STATE.project_view()})
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
                    self.send_json({"success": True, "message": msg})
                else:
                    self.send_json({"success": False, "error": msg}, status=400)
                return

        # ----------------------------------------------------------------------
        # Heavy Gate start/stop
        # ----------------------------------------------------------------------
        if path == "/api/heavy-gate/start":
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


def main():
    print("=" * 60)
    print("  PM & QA Cockpit")
    print(f"  Project:    {STATE.project.get('name')} ({STATE.project.get('docs_mode')})")
    print(f"  Serving at: http://localhost:{PORT}")
    print("=" * 60)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", PORT), PMRequestHandler)
    # Pre-warm cards cache in background so first browser request is instantaneous
    threading.Thread(target=STATE.parser.get_all_cards, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
