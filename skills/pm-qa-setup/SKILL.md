---
name: pm-qa-setup
description: Use when the user asks to set up, install, onboard, register, upgrade or check PM & QA Cockpit in a project (or on this machine), or to clean up old cockpit skills. Inspects what the project and this machine already have, says in plain words where each thing belongs, asks a few plain questions, and changes only what the user approves; never overwrites a user's rules or skills. Ends with a read-only doctor check.
metadata:
  version: 0.6.0
  source: pm-qa-cockpit
---

# pm-qa-setup: guided install, upgrade and check of PM & QA Cockpit

You are setting up (or upgrading, or checking) the PM & QA Cockpit in **one project**, for a user who
owns that project's rules. **The user's orchestration is theirs: you inspect, explain, and apply only
what they approve.** Nothing is ever done silently, and a project's own rules always win.

The CLI does the analysis; you present it in plain words and talk to the user. Run it from the
**running copy**:

```
CLI = uv run python ~/.pm-qa-cockpit/app/cockpit/cli.py      (Windows: $HOME\.pm-qa-cockpit\app\cockpit\cli.py)
```

If the environment variable `PM_QA_USER_HOME` is set (a QA sandbox), read every `~` below as that
folder. If `~/.pm-qa-cockpit/app` does not exist, say so and offer to clone it (it appears as a
decision in the plan); don't run a dev clone instead.

## 1. Plan (read-only)

Run `CLI setup-plan <project path>`. Add `--name`, `--prefix`, `--docs-path <external folder>`,
`--stealth` only when the user told you; if the project isn't registered yet, ask for name, prefix and
where the PM data should live first (recommend in-repo docs for a team repo, an external folder, e.g.
in a private `pm-qa-docs` repo, when the user wants no PM files in the code repo).
Use `--json` to read the details. The plan carries:

- **`inventory`**: groups *This project*, *Global folder: <tool>* (one per tool the project uses),
  *Launchers*, *PM data*, *Hooks*. Each item: what it is, where it is now, where it belongs, why, and a
  status (`ok` stays as is, `change` an approval changes it, `review` the user's to look at).
- **`target`**: the recommended end state, in sentences.
- **`decisions`**: at most one per fixed theme, in this order: where the PM data lives, where skills
  live, launcher wiring, gates, who does each step (the roster; empty themes are skipped), plus one
  question per skill the user has edited. Each has a plain `question`, a `recommendation` and `options` whose `approve` lists hold
  the internal action ids.
- `actions` (the internal ids, files written, exact text added), `notes`, `mode`
  (`full`, or `register-only` when the project runs its **own loop**, e.g. `money-nest-orchestrate`:
  then no cockpit skills go into the project. Re-plan with `--full` only if the user insists).

## 2. The first reply

In this order, in plain words:
1. **What you have**: the inventory, grouped as above, one line per item ("grill-me: in this project
   only, missing for Claude Code; belongs in ~/.claude/skills too; the project copy stays").
   Mention the notes.
2. **Recommended target**: the `target` sentences.
3. **The decisions**: ask them with the interactive question tool if you have one (one question per
   decision, the recommended option first), otherwise as a short numbered list of questions. That is
   at most 5 questions, plus one per edited skill.

**Never show action ids (A1, A3 …) as the thing to decide.** They are for you: map each answer to its
option's `approve` list. Show ids only if the user asks for them.
For a launcher change, show the exact old and new line. For an edited skill, first show the diff
between their copy and the shipped one (`git diff --no-index <theirs> ~/.pm-qa-cockpit/app/skills/<name>`).

**The roster question** ("Who does each step: use the recommended roster?") appears only in full mode
and only when a role has no agent yet. Show `roster_table` as one table per tool (role, model, effort,
and "new" / "yours" / "your session pick"); the lead is the user's session choice, only recorded. Yes
approves the whole action. "Customise" asks the decision's `questions` one by one (a model from the
list, recommendation first or Other, and an effort), then passes the answers to apply as
`--roster '{"claude": {"advisor": {"model": "...", "effort": "high"}}}'`. In a register-only project
the roster is reported, never offered: the project's own loop wins. Existing agent files are never edited **by the plan**; step 2b may edit them, role by role, on approval.

## 2b. Review the team and the QA setup (every setup, every mode)

The plan above only fills gaps. A project that already has agents and gates still gets these two
reviews, because "nothing missing" is not the same as "still right". Ask them with the interactive
question tool, one role or one topic per question, recommended option first. Skipping them silently has
already failed once (money-nest, 2026-10-17: the user had to ask for both).

**Roster review.** Read the project's own agent files (`.claude/agents/`, `.cursor/agents/`,
`.agents/subagents/`) and its rules file's roster text. Show one table per tool: role, model, effort,
and any bare alias (`sonnet`, `opus`, `haiku`) that may resolve to an older model than intended. Then,
**role by role**, ask the model and the thinking level, offering "keep as is" first; a role the user
keeps is not touched. If the tool exposes no effort level (Cursor: only Fast on/off), ask that instead
and say so. Check the claims against the files: a rules file saying one model while an agent file pins
another is a finding. On approval, edit those agent files and the rules text that names the model
(minimal edits, the project's own words); in a register-only project this is still allowed here
because the user approved it role by role. Say plainly what you could not verify (a frontmatter key,
a model slug the tool may not accept).

**QA review.** Inspect, read-only: the gate scripts and what each gate runs; whether a fast/heavy split
exists and when the heavy gate last ran (age in commits); coverage thresholds in config against the
latest real coverage (a floor far below actual lets a large regression pass); whether any gate is
retired or has no owner; and whether per-sprint numbers are recorded anywhere. Present: what exists,
the gaps, and the metrics worth keeping per sprint: tests passed/skipped/failed, coverage %, static
gate error counts, review rounds and findings per round, and **escapes** (bugs found in QA or later
that gates and review missed, the one number that says whether the gates work). Then ask, one question
each: raise a stale coverage floor (recommend a floor a couple of points under actual), add a floor
where none exists (or measure first), flag a stale heavy run (never run a project's heavy gate for
them), and where a small metrics table should live (the project's quality doc, or a new file).
Apply only the approved ones; record the staleness note, don't fix it.

## 3. Apply only what was approved

`CLI setup-apply <project path> <ids…>`, with the ids from the approved options, in order. `A4=new`
or `A4=replace` carry an edited skill's choice; a plain id keeps the user's copy. It re-plans and
refuses if anything the user saw changed (title, files, text, a launcher line); then re-plan and ask
again. It also refuses, before writing anything, if a target file isn't UTF-8.
Some results come back as `run: …` (`register`, `profile-gates`, `clone-running-copy`,
`fetch-grill-me`). Run them and show their output:
- `register` → `CLI init … --apply` (registry entry, docs layout; external docs get their own git repo
  unless they sit inside one already).
- `profile-gates` → `CLI --project <slug> profile-gates --measure --apply`. Say first that it runs the
  full suite once to time it (≤ 6 min = **fast**, run inline before every close; longer = **heavy**).
- `fetch-grill-me` → show the source (<https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me>);
  after a yes, fetch the folder's files (GitHub contents API, or the raw files under
  `https://raw.githubusercontent.com/mattpocock/skills/main/skills/productivity/grill-me/`) into each
  folder the action lists.

If the user declines something because it conflicts with their own rules, record that (with their OK)
in the project's PM `WORKLOG.md`, e.g. "declined the AGENTS.md section: AGENTS.md is generated".
Don't commit or push in the user's project; leave the changes for them (commit locally only if asked).

## 4. End with doctor

Run `CLI doctor <project path or slug>` and show its checklist. Every line should be `pass`, or `warn`
with its reason and fix. A `fail` (exit 1) means setup isn't done: say what the fix line says. Tell
the user they can re-run doctor anytime, and how to open the dashboard:
`powershell -File ~/.pm-qa-cockpit/app/dashboard.ps1 -Project <slug>` → <http://localhost:3333>.
Design work starts with `grill-me`, then the `pm-orchestrate` loop (full mode) or the project's own
loop (register-only).

## Where things belong (what the plan recommends)

- **Generic skills** (`grill-me`, `grilling`): in the global folder of every tool the project uses.
  One found only in a project or another tool's folder is **copied**, never moved; the project copy
  stays (it is the project's, and teammates use it).
- **Cockpit skills** (`pm-qa-cockpit`, `pm-orchestrate`, `pm-sync`, `pm-review`): in the project's own
  skill folder (full mode) or nowhere (register-only). With external or stealth docs they are kept
  out of git via `.git/info/exclude` (exact paths only).
- **`pm-qa-setup`**: the one global cockpit skill, reachable for every tool used.
- **Old global copies** of the cockpit skills (installed before per-project skills): removed, but only
  copies provably ours and unedited (an exact past release counts); edited or uncertain ones are
  listed for the user and never deleted.
- **Launchers** (`scripts/*.ps1`, root scripts, `package.json` scripts) call the running copy
  `~/.pm-qa-cockpit/app` first; a fallback to the dev clone after it is fine. A launcher that
  prefers the dev clone gets an exact one-line fix, written only on approval.
- **The user's own loop, rules files, hooks and orchestration**: stay. A rules file only ever gets a
  short section appended between `<!-- pm-qa-cockpit:start/end -->` markers.

Tools: Claude Code reads `~/.claude/skills` and the project's `.claude/skills`; Antigravity
`~/.agents/skills` and `.agents/skills`; Cursor reads `~/.cursor/skills`, `~/.agents/skills` and
`~/.claude/skills` (and the project equivalents), so a Cursor project gets `.claude/skills` plus an
`AGENTS.md` note, never a third copy.

## Upgrades
`CLI skills-status <project path>` lists installed vs available versions. States: `current` ·
`outdated` (ours and provably unedited, by install record or an exact past release: safe to upgrade
on approval) · `edited` (ours but changed: keep / save the new one beside it / replace, see step 2) ·
`foreign` (a same-named skill that isn't ours: never touched) · `missing`. Before upgrading, the
running copy should be current: `git -C ~/.pm-qa-cockpit/app pull`.

## Rules
- Propose, then apply on approval: every write, every time. The project's rules win on conflict, and
  the appended section says so.
- Compare everything with line endings normalised (the CLI does): a CRLF checkout is not a change.
- Never push, never open PRs, never commit in the user's project unless asked.
