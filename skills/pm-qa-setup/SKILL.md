---
name: pm-qa-setup
description: Use when the user asks to set up, install, onboard, register, upgrade or check PM & QA Cockpit in a project (or on this machine), or to clean up old cockpit skills. Inspects the project's existing orchestration and hooks, proposes exactly what to add, and changes only what the user approves — never overwrites a user's rules or skills.
metadata:
  version: 0.2.0
  source: pm-qa-cockpit
---

# pm-qa-setup — guided install and upgrade of PM & QA Cockpit

You are setting up (or upgrading) the PM & QA Cockpit in **one project**, for a user who owns that
project's rules. **The user's orchestration is theirs: you inspect, propose, and apply only what they
approve.** Nothing here is ever done silently, and a project's own rules always win over the cockpit's.

All logic lives in the cockpit CLI; you run it, read its output, and talk to the user. Run it from the
**running copy**:

```
CLI = uv run python ~/.pm-qa-cockpit/app/cockpit/cli.py      (Windows: $HOME\.pm-qa-cockpit\app\cockpit\cli.py)
```

If `~/.pm-qa-cockpit/app` does not exist, say so and offer (step 1) — don't run the dev clone instead.

## The flow

### 1. Prerequisites (machine)
Check `uv` and `git` are on PATH. If the running copy is missing, offer:
`git clone https://github.com/getuliojr/pm-qa-cockpit ~/.pm-qa-cockpit/app`.
If **grill-me** is not installed (no `grill-me` or `grilling` skill in `~/.claude/skills` /
`~/.agents/skills`), offer to install it **once for this machine**, showing the source first:
<https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me>. Fetch the folder's files
(the GitHub contents API lists them; raw files are under
`https://raw.githubusercontent.com/mattpocock/skills/main/skills/productivity/grill-me/`) into
`~/.claude/skills/grill-me/` and `~/.agents/skills/grill-me/` only after the user says yes.

### 2. Plan (read-only)
Run `CLI setup-plan <project path>` (add `--name`, `--prefix`, `--docs-path <external folder>`,
`--stealth` when the user has told you; ask for name/prefix/docs location if the project isn't
registered yet — recommend in-repo docs for a team repo, external docs (e.g. a folder of a private
`pm-qa-docs` repo) when the user wants zero PM files in the code repo).
Use `--json` if you need to reason over details. The plan lists:
- **Project / docs mode / detected agents** (Claude Code, Cursor, Antigravity).
- **Mode** — `full`, or `register-only` when the project already runs its **own loop** (e.g.
  `money-nest-orchestrate`). Register-only installs **no** cockpit skills: the project's loop wins.
  Only if the user explicitly wants the cockpit skills anyway, re-plan with `--full`.
- **Notes** — things the plan will not touch but the user should know (a same-named skill that isn't
  ours, an edited old global copy, no git repo to exclude from).
- **Numbered actions (A1, A2 …)**, each with the exact files it would write and, for orchestration
  files, the exact text it would add (between `<!-- pm-qa-cockpit:start/end -->` markers, appended —
  existing text is never rewritten).

### 3. Show and ask
Present a short gap checklist and the actions in plain words (what each changes and why). Then ask
which ones to apply — use the interactive question tool if you have one, recommended set first.
Typical recommendation: all of them, except anything touching a file the user said is off-limits.
**Never apply an action the user did not approve. Never edit any file the plan didn't list.**

### 4. Apply only what was approved
`CLI setup-apply <project path> A1 A3 …` — it re-checks the plan and refuses if anything the user saw
changed since you showed it (title, files, paths, text), then re-plan and ask again. It also refuses
before writing anything if a target file isn't UTF-8. For a skill in state `edited`, pass the user's
choice per action: `A4=replace`, `A5=new`; a plain id keeps the user's copy.
If the user declines an action because it conflicts with their own rules, record that (with their OK)
in the project's PM `WORKLOG.md`, e.g. "declined A3: AGENTS.md is generated; cockpit note kept out". Actions that are commands of their own (`register`,
`profile-gates`, `clone-running-copy`, `fetch-grill-me`) come back as `run: …`; run them and show their
output:
- `register` → `CLI init … --apply` (registers in `~/.pm-qa-cockpit/projects.json`, creates the docs
  layout; external docs get their own git repo unless inside one already).
- `profile-gates` → `CLI --project <slug> profile-gates --measure --apply` (≤ 6 min suite = **fast**,
  run inline before every close; longer = **heavy**, split into fast gates + an out-of-band heavy gate).
  Tell the user it will run the full suite once to time it.

Do not commit or push in the user's project; leave changes for them to review (offer a local commit
only if they ask).

### 5. Confirm
Re-run `CLI setup-plan` — it should show nothing left (or only what the user declined). Tell the user
how to open the dashboard: `powershell -File ~/.pm-qa-cockpit/app/cockpit.ps1 -Project <slug>` →
<http://localhost:3333>, and that design work starts with `grill-me` then the `pm-orchestrate` loop
(full mode) or the project's own loop (register-only).

## Upgrades
Run `CLI skills-status <project path>` (and `setup-plan`). States:
`current` · `outdated` (ours and provably unedited since install → safe to upgrade on approval) ·
`edited` (ours but changed, or not provably unedited → show the user the diff between their copy and
the shipped one at `~/.pm-qa-cockpit/app/skills/<name>/`, e.g. `git diff --no-index <theirs> <shipped>`,
then ask: **keep theirs** (plain id, default), **replace** (`A4=replace`), or **save the new version
beside it** (`A4=new` → written to `<skills folder>/.pm-qa-cockpit-new/<name>/`, which agents don't
load; merge by hand, then delete it)) · `foreign` (a same-named skill that isn't ours — never touched) ·
`missing`. The global `pm-qa-setup` itself appears as an `upgrade-bootstrap` action when outdated or
edited, with the same choices.
Before upgrading, the running copy should be current: `git -C ~/.pm-qa-cockpit/app pull`.

## Cleanup of old global copies
Before Story 2 the four project skills (`pm-qa-cockpit`, `pm-orchestrate`, `pm-sync`, `pm-review`)
were installed globally. The plan offers a `cleanup-global` action listing only copies that are
provably ours and unedited (edited or uncertain ones appear as notes, never deleted). Its detail names
registered full-mode projects that still rely on the global copies — set those up first. Remove only
with approval, and never touch `grill-me`/`grilling` or any other skill.

## Rules
- Propose, then apply on approval — every write, every time. The project's rules win on conflict, and
  the pointer text says so.
- Skills go into the project's own agent folders (`.claude/skills/`, `.agents/skills/`), only for
  agents detected there; Cursor reads `.claude/skills` through `AGENTS.md`, so a Cursor project gets
  `.claude/skills` plus an `AGENTS.md` note, never a third copy.
  With external or stealth docs the installed skill folders are excluded via `.git/info/exclude`
  (exact paths only).
- Compare everything with line endings normalised (the CLI does) — a CRLF checkout is not a change.
- Never push, never open PRs, never commit in the user's project unless asked.
