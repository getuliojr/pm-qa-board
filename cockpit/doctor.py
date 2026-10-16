"""
doctor.py - `cli.py doctor <project>`: is this project's cockpit setup correct? (Story 2.1, R2-Q7)

Strictly read-only: no registry writes, no plan file, no exclude edits, no server, no network. Each of
the 9 checks is pass / warn / fail with a one-line fix for anything that isn't a pass; any fail makes
the command exit 1. `pm-qa-setup` ends with it, and the user can re-run it anytime.

Standard library only.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import Any

import projects
import setup_flow
import wiring
from setup_flow import BOOTSTRAP_SKILL, GENERIC_SKILLS, PROJECT_SKILLS, TOOL_LABELS, TOOL_ORDER


def _check(n: int, name: str, status: str, detail: str, fix: str = "") -> dict[str, Any]:
    return {"n": n, "name": name, "status": status, "detail": detail, "fix": fix if status != "pass" else ""}


def resolve(target: str) -> tuple[dict[str, Any], bool]:
    """(project, registered) from a registered slug or a path inside a project."""
    data = projects.load_registry()
    if target in data["projects"]:
        return data["projects"][target], True
    path = Path(target).expanduser()
    if path.exists():
        found = projects.find_project_for_path(path)
        if found:
            return found, True
        return projects.adhoc_project(path), False
    raise SystemExit(f"Error: '{target}' is neither a registered project nor a folder. "
                     f"Registered: {', '.join(data['projects']) or 'none'}")


def run(target: str) -> dict[str, Any]:
    project, registered = resolve(target)
    code = Path(project["code_path"])
    docs = Path(project["docs_path"])
    checks: list[dict[str, Any]] = []
    run_copy = wiring.running_copy()
    run_ok = (run_copy / "cockpit" / "server.py").is_file()
    setup_cmd = "ask your agent to run pm-qa-setup in this project"

    # 1. registration + PM data layout
    if not registered:
        checks.append(_check(1, "Registered, PM data in place", "fail", f"{code} is not registered",
                             f"{setup_cmd} (it registers the project with `cli.py init`)"))
    elif not docs.is_dir():
        checks.append(_check(1, "Registered, PM data in place", "fail", f"PM data folder {docs.as_posix()} does not exist",
                             f"create it, or re-register with the right docs path (`cli.py init {code} ... --apply`)"))
    else:
        missing = [f for f in projects.EXTERNAL_DOCS_FILES if not (docs / f).is_file()] if project["docs_mode"] == "external" else []
        plan_ok = (docs / "PLAN.md").is_file() or (code / "PLAN.md").is_file()
        if missing or not plan_ok:
            gaps = missing or ["PLAN.md"]
            checks.append(_check(1, "Registered, PM data in place", "warn",
                                 f"'{project['slug']}' at {docs.as_posix()}: missing {', '.join(gaps)}",
                                 f"create {', '.join(gaps)} in {docs.as_posix()}"))
        else:
            checks.append(_check(1, "Registered, PM data in place", "pass",
                                 f"'{project['slug']}' (prefix {project['prefix']}), PM data at {docs.as_posix()} ({project['docs_mode']})"))

    # 2. the dashboard can load it: the same parser, in process, no server
    from parser import PMParser

    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser = PMParser(project=project)
            files = [f for d in (parser.cards_dir, parser.backlog_dir) if d.is_dir()
                     for f in sorted(d.glob("*.md")) if f.name != "template.md"]
            cards = parser.get_all_cards(refresh=True)
            err = None
        except Exception as exc:  # noqa: BLE001 - any parser crash is the finding
            files, cards, err = [], [], exc
    parsed = {Path(c.get("file_path") or "").name for c in cards}
    bad = [f.name for f in files if f.name not in parsed]
    unparsed = len(bad)
    if err is not None:
        checks.append(_check(2, "Dashboard can load it", "fail", f"the parser failed: {err}",
                             "fix the file named in the error, then re-run doctor"))
    elif unparsed > 0:
        names = ", ".join(bad[:3]) + (" ..." if len(bad) > 3 else "")
        checks.append(_check(2, "Dashboard can load it", "fail", f"{len(cards)} card(s) loaded, {unparsed} could not be parsed ({names})",
                             "check the frontmatter of those cards (no inline # comments; an id and a status)"))
    elif run_ok and not (run_copy / "dashboard.ps1").is_file():
        checks.append(_check(2, "Dashboard can load it", "fail", f"{len(cards)} card(s) load, but the running copy has no dashboard.ps1",
                             f"update the running copy: git -C {run_copy} pull"))
    else:
        checks.append(_check(2, "Dashboard can load it", "pass", f"{len(cards)} card(s) load with no parse errors"
                             + ("; dashboard.ps1 is in the running copy" if run_ok else "")))

    # 3. the running copy
    if not run_ok:
        checks.append(_check(3, "Running copy", "fail", f"{run_copy} is missing",
                             f"git clone https://github.com/getuliojr/pm-qa-cockpit {run_copy}"))
    else:
        version = setup_flow.tool_version(run_copy)
        newer = [(p, setup_flow.tool_version(p)) for p in wiring.dev_clones()
                 if setup_flow._vtuple(setup_flow.tool_version(p)) > setup_flow._vtuple(version)]
        if newer:
            p, v = newer[0]
            checks.append(_check(3, "Running copy", "warn", f"{run_copy} is {version}; the dev clone {p} is {v} (unreleased)",
                                 f"release: merge and push, then git -C {run_copy} pull"))
        else:
            checks.append(_check(3, "Running copy", "pass", f"{run_copy} ({version})"))

    # 4. launchers call the running copy
    launchers = [lw for lw in wiring.scan_launchers(code) if not (lw["kind"] == "orchestration" and lw["verdict"] == "ok")]
    bad_l = [lw for lw in launchers if lw["verdict"] != "ok"]
    if not launchers:
        checks.append(_check(4, "Launchers call the running copy", "pass", "no launcher in this project calls the cockpit"))
    elif bad_l:
        how = {"dev-clone-first": "calls the dev clone first", "frozen": "calls a frozen in-repo copy",
               "missing": "calls the running copy, which isn't installed", "dev-clone-mentioned": "mentions a dev-clone path"}
        detail = "; ".join(f"{lw['file']} {how[lw['verdict']]}" for lw in bad_l)
        fix = (f"{setup_cmd} (it offers the exact line change)" if any(lw["edit"] for lw in bad_l)
               else "make it look in $HOME/.pm-qa-cockpit/app first, with the others only as a fallback")
        checks.append(_check(4, "Launchers call the running copy", "warn", detail, fix))
    else:
        checks.append(_check(4, "Launchers call the running copy", "pass",
                             "; ".join(f"{lw['file']} calls the running copy first" for lw in launchers)))

    tools = [t for t in TOOL_ORDER if t in setup_flow.detect_agents(code)] or ["claude"]
    labels = {t: TOOL_LABELS[t] for t in tools}

    # 5. skills current per tool used
    loop = setup_flow.own_loop_skills(code)
    rows = []
    for t in tools:
        for rel in {"claude": [".claude/skills"], "antigravity": [".agents/skills"], "cursor": [".claude/skills"]}[t]:
            for s in PROJECT_SKILLS:
                st = setup_flow.skill_state(code / rel / s, setup_flow.TOOL_ROOT / "skills" / s)
                rows.append((t, rel, s, st))
    installed = sorted({f"{rel}/{s}" for _, rel, s, st in rows if st["state"] != "missing"})
    if loop:
        if installed:
            checks.append(_check(5, "Skills current", "warn",
                                 f"own loop ({', '.join(Path(p).name for p in loop)}) and cockpit skills ({', '.join(installed)}) side by side",
                                 "two loops confuse agents: keep one (remove the cockpit skills, or the own loop)"))
        else:
            checks.append(_check(5, "Skills current", "pass",
                                 f"register-only: the project's own loop runs ({', '.join(Path(p).name for p in loop)})"))
    else:
        by = {}
        for t, rel, s, st in rows:
            by.setdefault(st["state"], set()).add(f"{rel}/{s}")
        if by.get("missing"):
            checks.append(_check(5, "Skills current", "fail", f"missing: {', '.join(sorted(by['missing']))}", setup_cmd))
        elif set(by) - {"current"}:
            parts = [f"{k}: {', '.join(sorted(v))}" for k, v in sorted(by.items()) if k != "current"]
            checks.append(_check(5, "Skills current", "warn", "; ".join(parts),
                                 f"{setup_cmd} (it upgrades unedited copies and asks about edited ones)"))
        else:
            checks.append(_check(5, "Skills current", "pass",
                                 f"cockpit skills current ({setup_flow.tool_version()}) for {', '.join(labels.values())}"))

    # 6. grill-me reachable for every tool used
    reach = {t: setup_flow.generic_reach(code, t) for t in tools}
    none = [labels[t] for t in tools if not (reach[t]["global"] or reach[t]["project"])]
    local_only = [labels[t] for t in tools if not reach[t]["global"] and reach[t]["project"]]
    if none:
        checks.append(_check(6, "grill-me reachable", "fail", f"no {' or '.join(GENERIC_SKILLS)} for {', '.join(none)}",
                             f"{setup_cmd} (it copies or installs grill-me globally)"))
    else:
        checks.append(_check(6, "grill-me reachable", "pass", f"for {', '.join(labels.values())}"
                             + (f" ({', '.join(local_only)}: from this project only; setup offers a global copy)" if local_only else "")))

    # 7. pm-qa-setup reachable and current for every tool used
    shipped = setup_flow.TOOL_ROOT / "skills" / BOOTSTRAP_SKILL
    missing_b, stale_b = [], []
    for t in tools:
        found = setup_flow.bootstrap_reach(code, t)
        if not found:
            missing_b.append(labels[t])
            continue
        st = setup_flow.skill_state(found[0], shipped)
        if st["state"] != "current":
            stale_b.append(f"{labels[t]}: {st['state']} ({st['installed_version'] or '-'}; {st['available_version']} available)")
    if missing_b:
        checks.append(_check(7, f"{BOOTSTRAP_SKILL} reachable", "fail", f"missing for {', '.join(missing_b)}",
                             "run install.ps1 from the running copy, or let setup install it"))
    elif stale_b:
        checks.append(_check(7, f"{BOOTSTRAP_SKILL} reachable", "warn", "; ".join(stale_b),
                             f"{setup_cmd} (it upgrades the global copy)"))
    else:
        checks.append(_check(7, f"{BOOTSTRAP_SKILL} reachable", "pass", f"current for {', '.join(labels.values())}"))

    # 8. gate mode profiled
    mode = (project.get("gates") or {}).get("mode", "unprofiled")
    if mode in ("unprofiled", None):
        checks.append(_check(8, "Gate mode profiled", "warn", "not measured yet",
                             f"cli.py --project {project['slug']} profile-gates --measure --apply"))
    else:
        checks.append(_check(8, "Gate mode profiled", "pass", mode))

    # 9. hooks reference only existing paths, none the dev clone
    hooks = wiring.scan_hooks(code)
    bad_h = [h for h in hooks if h["problem"]]
    if bad_h:
        why = {"dev-clone": "calls the dev clone", "missing-path": "calls a cockpit path that doesn't exist"}
        checks.append(_check(9, "Hooks", "warn", "; ".join(f"{h['file']} {h['event']} {why[h['problem']]}" for h in bad_h),
                             "point the hook at ~/.pm-qa-cockpit/app (or the path that exists)"))
    else:
        checks.append(_check(9, "Hooks", "pass", f"{len(hooks)} hook command(s), none calls a missing cockpit path or the dev clone"
                             if hooks else "no hooks"))

    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail")}
    return {"project": project["slug"], "name": project["name"], "code_path": str(code), "checks": checks,
            "counts": counts, "ok": counts["fail"] == 0}


def cmd_doctor(args: Any) -> int:
    import json

    report = run(args.target)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    print(f"PM & QA Cockpit doctor: {report['name']} ({report['code_path']})\n")
    for c in report["checks"]:
        print(f"  [{c['status']}] {c['n']}. {c['name']}: {c['detail']}")
        if c["fix"]:
            print(f"         fix: {c['fix']}")
    k = report["counts"]
    print(f"\n{k['pass']} pass, {k['warn']} warn, {k['fail']} fail. Read-only: nothing was changed.")
    return 0 if report["ok"] else 1
