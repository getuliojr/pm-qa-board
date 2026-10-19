---
name: pm-qa-cockpit
description: Use when creating, reading or updating story cards, PLAN.md, reviews or close-out artifacts for a project tracked by the PM & QA Cockpit (local dashboard on :3333), or when asked to launch/register the Cockpit. Defines card schema, project resolution, docs layout and the CLI.
metadata:
  version: 0.6.1
  source: pm-qa-cockpit
---

# PM & QA Cockpit

A local, zero-dependency dashboard + CLI that reads a project's markdown cards (stories) and shows
status, Definition of Done (DoD), Human QA checks, cold reviews, heavy gates and close-out artifacts.
Markdown on disk is the only source of truth: no board mirroring, no double entry.

## Resolving the project
1. `--project <slug>` on the CLI, else
2. the project whose `code_path` contains the current directory, else
3. the active project in `~/.pm-qa-cockpit/projects.json` (`cli.py projects --select <slug>`).

`PM_QA_COCKPIT_HOME` relocates the registry. Each entry holds `slug, name, prefix, code_path, docs_path`
plus the gate profile.

## Docs layout
- **In-repo** (default `docs_path = <code_path>/docs`): `docs/cards/`, `docs/reviews/`, `docs/artifacts/`, ...
- **External** (`--docs-path`): `docs_path` is its own git repo holding `PLAN.md`, `cards/`, `reviews/`,
  `artifacts/`, `WORKLOG.md`, `QUALITY.md`, `E2E_CANDIDATES.md`, `gate-heavy-results/`, `gates/`.
  Code git operations (commits, SHAs, diffs) stay in `code_path`; PM files commit to the docs repo.
  **PM files never go into the code repo for external projects.** `init --stealth` additionally writes
  `.git/info/exclude` for any local leftovers.

## Card format (Story schema)
File: `<docs>/cards/<PREFIX>-<story> - <Short Title>.md`.

```markdown
---
id: <PREFIX>-<story>
story: "<story>"
type: feature
status: backlog
created: YYYY-MM-DD
validated: false
---
# <PREFIX>-<story> - <Short Title>
## Description
## Definition of Done / Acceptance Criteria
- [ ] ...
## Human QA Test
### Verification Instructions
1. concrete steps a non-author can follow
### Verification Check
- [ ] Human QA test completed and verified
## Changelog
| Date & Time | Agent | What |
|---|---|---|
| YYYY-MM-DD HH:MM | Lead (Model) | Created card |
```

- `status`: `backlog | story-backlog | in-progress | qa | completed`. `story-backlog` = designed, ready to build.
- Frontmatter values are bare: **no inline `# comment`** (the parser keeps it in the value and the card
  silently lands in Backlog).
- Changelog rows: exact 24h datetime and `<Role (Model)>`, or `User`. Never bare dates or a bare "Agent".
- `completed` + `validated: YYYY-MM-DD` only on explicit user confirmation; all-`[x]` DoD means ready for
  QA, not done.
- **Legacy money-nest schema still parses**: `mn-id` / `sprint` / `status: sprint-backlog` / `## Sprint`.
- Story ids: `X.Y` for a new or unrelated story, `X.Y.Z` for a follow-up of `X.Y`. One card per story.

## Artifacts and reviews
- `<docs>/reviews/<PREFIX>-<story>-round-<n>.md` - cold review verdicts (see `pm-review`).
- `<docs>/artifacts/<PREFIX>-<story>-closeout.html` - interactive close-out page, served at
  `/artifacts/<name>` (spec in `pm-orchestrate`). **One per card that reaches `qa`**: the card's
  **Story Close-Out** button appears only when this exact file exists.

## CLI cheat-sheet (`uv run python <cockpit>/cockpit/cli.py [--project slug] ...`)
| Command | Does |
|---|---|
| `projects [--select slug]` | list / switch project |
| `init <code_path> --name N --prefix P [--docs-path D] [--stealth] [--apply]` | onboard (report-only without `--apply`) |
| `list [--status s] [--story id]` / `show <id>` | inspect cards, DoD, QA items |
| `status <id> <status> --agent "Lead (Model)"` | transition + changelog row |
| `check <id> <i>` / `uncheck <id> <i>` | DoD item by 0-based index |
| `check-qa <id> <i>` / `uncheck-qa <id> <i>` | Human QA item |
| `validate <id> --notes ...` | completed + validated (user confirmation only) |
| `profile-gates [--measure] [--apply] [--threshold-seconds 360]` | pick fast/heavy gate mode |

## Launching
`powershell -File <cockpit>\dashboard.ps1 [-Project slug] [-Port 3333]` opens http://localhost:3333.
Agents do not launch it unattended (long-running server) unless asked.

## Related skills
`pm-orchestrate` (the loop), `pm-sync` (incremental DoD), `pm-review` (cold gate). Never `git push`.
