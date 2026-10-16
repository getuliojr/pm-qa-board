# PM & QA Cockpit

A local, zero-dependency dashboard and CLI for running agent-built work as **stories**: markdown cards,
Definition of Done, cold-review verdicts, Human QA checks, heavy gates and interactive close-out
artifacts, across any number of projects. It reads markdown on disk; there is no database and no board
to keep in sync. It plus a small skill suite (`pm-qa-cockpit`, `pm-orchestrate`, `pm-sync`, `pm-review`)
gives Claude Code, Cursor and similar agents one shared working loop.

**It never pushes.** Agents commit locally; `git push` is always the user's call.

## Quick start

```powershell
# 1. Install the skills (preview first, then for real)
powershell -NoProfile -File install.ps1 -DryRun
powershell -NoProfile -File install.ps1            # add -Force to overwrite changed files

# 2. Register a project (report only; add --apply to write)
uv run python cockpit\cli.py init C:\path\to\app --name "My App" --prefix APP
uv run python cockpit\cli.py init C:\path\to\app --name "My App" --prefix APP --apply

# 3. Launch the dashboard (http://localhost:3333)
powershell -File cockpit.ps1 [-Project my-app] [-Port 3333]
```

`install.ps1` copies the skills into `~/.claude/skills`, `~/.agents/skills` and `~/.cursor/skills-cursor`,
and fetches `grill-me` from mattpocock/skills if neither `grill-me` nor `grilling` is installed
(`-RequireGrillMe` makes a failed download fatal).

## Registry

`~/.pm-qa-cockpit/projects.json` (override the folder with `PM_QA_COCKPIT_HOME`) lists projects with
`slug`, `name`, `prefix`, `code_path`, `docs_path` and a gate profile, plus the active project.
Resolution order: `--project`, then the project containing the current directory, then the active one.

## In-repo vs external docs

- **In-repo** (default): PM files live in `<code_path>/docs`.
- **External** (`--docs-path`): PM files live in a separate git repo: `PLAN.md`, `cards/`, `reviews/`,
  `artifacts/`, `WORKLOG.md`, `QUALITY.md`, `E2E_CANDIDATES.md`, `gate-heavy-results/`. Code git
  operations stay in the code repo and PM files never enter it. `--stealth` also hides local cockpit files
  through `.git/info/exclude`.
- **Shared private docs repo**: an external `docs_path` may be a folder inside one private repo that holds
  many projects (`pm-qa-docs/<slug>/`). The cockpit commits into that enclosing repo and never creates a
  nested one.

## Code, running copy and data — keep them apart

| Layer | Where | Syncs via |
|---|---|---|
| Tool code (you develop it) | your dev clone of this repo, on branches | this public repo |
| Running copy (projects use it) | `~/.pm-qa-cockpit/app`, a clone of this repo on `main` | `git -C ~/.pm-qa-cockpit/app pull` |
| PM data (cards, PLAN, reviews, close-outs) | each project's own repo, or a private docs repo such as `pm-qa-docs/<slug>/` | that repo |
| Registry | `~/.pm-qa-cockpit/projects.json` (absolute paths, per machine) | not synced: re-run `cli.py init` |
| Installed skills | `~/.claude/skills`, `~/.agents/skills`, `~/.cursor/skills-cursor` | `install.ps1` per machine |

**This repo never holds PM data.** Its own cards live in the private docs repo (`.gitignore` blocks
`docs/`, `PLAN.md` and friends here as a safety net).

**Release flow:** develop and test in the dev clone → merge to `main` and push → update the running copy
and skills:

```powershell
git -C $HOME\.pm-qa-cockpit\app pull
powershell -File $HOME\.pm-qa-cockpit\app\install.ps1 -Force
```

Projects find the cockpit through `$env:PM_QA_COCKPIT_REPO`, else `~/.pm-qa-cockpit/app`, else a sibling
`pm-qa-cockpit` folder — so a half-finished edit in the dev clone never breaks a project's dashboard.

**New machine:** clone this repo (dev), clone it again into `~/.pm-qa-cockpit/app` (running copy), clone
your projects and the private docs repo, run `install.ps1`, then `cli.py init ... --apply` once per project.

## Story schema

Cards are `<docs>/cards/<PREFIX>-<story> - <Title>.md` with frontmatter `id`, `story`, `type`, `status`
(`backlog | story-backlog | in-progress | qa | completed`), `created`, `validated`; sections
`## Description`, `## Definition of Done / Acceptance Criteria`, `## Human QA Test`
(`### Verification Check` checkboxes) and `## Changelog` (`| YYYY-MM-DD HH:MM | Role (Model) | What |`).
The legacy money-nest schema (`mn-id`, `sprint`, `sprint-backlog`, `## Sprint`) is still accepted.

## Gate modes

`cli.py profile-gates --measure --apply` times the full static + test suite against a 360 s threshold:

- `fast` (<= 360 s): the agent runs the full suite inline before closing a story.
- `heavy` (> 360 s): the agent runs only fast gates; the full suite is a generated `finalgates.ps1`
  (`scripts/` in-repo, `<docs_path>/gates/` for external docs) run by the user or the QA & Heavy Gates tab.
- `unprofiled`: profile first.

## Close-out artifacts

Every closed story writes `<docs>/artifacts/<PREFIX>-<story>-closeout.html`: a self-contained interactive
page (goal and scope, a demo of the work, gate and review badges, live Human QA checkboxes and a
"Validate & Complete Story" button wired to the Cockpit API). Opened outside the Cockpit it degrades to
read-only. See `skills/pm-orchestrate/SKILL.md` for the spec.

## Tests

```powershell
uv run --system-certs --group dev pytest
```
