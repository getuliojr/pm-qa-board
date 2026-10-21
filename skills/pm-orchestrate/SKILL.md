---
name: pm-orchestrate
description: Use at the start of any new feature, story or nontrivial change in a project tracked by the PM & QA Cockpit, before writing code or spawning agents; also when onboarding a project (cli.py init), choosing gate mode (profile-gates), or closing a story (close-out artifact).
metadata:
  version: 0.9.1
  source: pm-qa-cockpit
---

# PM orchestrate - the 7-step story loop

Roles are generic: **lead** (process, contract, merges, answers the user), **builder** (cheaper model,
writes code against a brief), **advisor** (stronger model, consulted for a hard call, max 3 calls per
problem then escalate to the user), **retrieval** (cheap, fact-finding only). Cold reviewer = a stronger
model (see `pm-review`), and **pm-sync** (cheap, ticks DoD boxes; see `pm-sync`). Never switch the
whole session's model; delegate instead.

**Roster.** Who fills each role is in the `## Roster` section of the project's `ORCHESTRATION.md`
(docs folder), one table per tool, with an agent, model and effort per role. When you delegate a role,
dispatch **the agent that table names** for the tool you run in (pass the table's effort if the agent
file has none). If the project has no Roster or no agent for that role, use the generic rule above.
`cli.py doctor` checks the roster; `pm-qa-setup` fills it.

## The loop
1. **Design first (`grill-me`).** No code until open questions are closed. Ask each decision interactively
   (recommended option first). **Persist after every round**: append settled Qs (id, choice, one-line WHY)
   under `### Grill in progress - Story <id>` in `<docs>/PLAN.md` immediately. Close in five parts:
   (a) write the settled design into PLAN.md (decisions + WHY, rejected alternatives, non-goals, an
   *unchecked* DoD), promoting the in-progress block; (b) seed the card at `story-backlog` (the Ready to Build lane); if the project registry entry declares
   `extra_tags` (`cli.py tags` lists them), first ask the user for each declared tag's value and write it as a
   bare frontmatter line `<key>: <value>` with no inline comment; omit the key if the user has no value;
   (c) emit a self-contained handoff prompt (decisions + rationale, file:line touchpoints, traps, DoD);
   (d) commit the grill locally, separate from build work; (e) delete any scratch plan file.
   The grilling session designs; a **new** session builds.
2. **Start.** Record the start SHA (`git rev-parse HEAD` in `code_path`). Card to `in-progress` with a
   Changelog row naming that SHA, **before the first line of code** (`pm-sync`).
3. **Build** with a local commit per phase/chunk, only once that chunk's own check is green; tick DoD as
   each line lands (`pm-sync`). Never a known-broken commit, never one giant commit at the end.
4. **Gates** per the project's gate mode (below). Fast gates run once per state, on the tree that ships.
5. **Commit everything** (code repo, and docs repo if external) before review: a diff/worktree sees only commits.
6. **Cold review** via `pm-review` (separate stronger agent, diff-only, max 3 rounds).
7. **Close:** card to `qa` in the same turn as PASS, write the close-out **file** (every card, one or many:
   see *Story Close-Out Artifact*), hand the user the Human QA steps. The user's explicit chat confirmation moves it to `completed` (`cli.py validate`).

Also: `docs/WORKLOG.md` entry at the end of every session; PLAN.md boxes ticked as work completes;
add a row to `E2E_CANDIDATES.md` naming the *business rule* the story promises, in plain words.
Write down every dead end (environment gotcha, stale process) in the relevant doc, not only the worklog.

## Hard rules
- **Never `git push`, open a PR, or publish anything outside the machine.** The user pushes, every time.
- **Subagents never run anything over ~60s, never background a command, never end a turn "waiting".**
  Long commands belong to the lead; stop a subagent once its report is in. Why: a backgrounded command dies
  with the agent's turn and each idle wake replays full context at full price.
- A brief is a contract: builders touch only the files it names and stop-and-report on contradictions.
- Validation has a budget: judgement-free checks belong in a script, not an agent. Re-running to see if it
  still says the same thing costs zero-budget.
- Live browser checks are the last resort (after gates, then unit tests), scoped to what the diff changed.
- UI work: refine the incumbent design system, never redesign as a side effect.

## Adaptive 6-minute gates
Run once per project: `cli.py profile-gates --measure --apply` (detects the stack, times the full static +
test suite, stores the mode in the registry). Threshold default 360 s.

| Mode | Meaning | What the agent does |
|---|---|---|
| `fast` | full suite <= 360 s | agent runs the full suite inline before story close and records the numbers in `QUALITY.md` / the QA tab itself |
| `heavy` | full suite > 360 s | agent runs only Fast gates (static + tests for changed files); the full suite is the out-of-band **Heavy Gate** script (generated `scripts/finalgates.ps1`, or `<docs_path>/gates/finalgates.ps1` for external docs) run by the user or the Cockpit's QA & Heavy Gates tab; the agent picks results up from `gate-heavy-results/` next session |
| `unprofiled` | not measured yet | profile first, then proceed per the mode |

Never run a whole slow suite inline in heavy mode, even "to be sure".

## Onboarding a project
Use the **`pm-qa-setup`** skill (`cli.py setup-plan` → user approves → `cli.py setup-apply`). It wraps
the steps below and never changes the project's rules without approval.

1. `cli.py init <code_path> --name N --prefix P [--docs-path D]` prints a **report only**: registry entry,
   docs layout, existing orchestration files, hooks. Review it, then re-run with `--apply`.
   `--stealth` keeps cockpit files out of git via `.git/info/exclude`.
2. Alignment checklist: card schema valid (no inline `#` comments in frontmatter); PLAN.md exists; WORKLOG,
   QUALITY, E2E_CANDIDATES present; agent instruction file (CLAUDE.md / AGENTS.md) points to these skills
   and states "never push"; `profile-gates --measure --apply` done.
3. Inspect existing hooks (`.claude/settings.json` etc.) for anything that conflicts with the loop
   (auto-push, blocking edit guards). Report; never silently change them.

## Story Close-Out Artifact (required at every close)
**The file in the repo is the deliverable.** The Cockpit draws a card's **Story Close-Out** button only
when `<docs_path>/artifacts/<PREFIX>-<story>-closeout.html` exists: no file, no button. A page published
to claude.ai (or shown in chat) leaves nothing the Cockpit can see, so it never replaces the file. Every
card that reaches `qa` gets one, big or small, and an agent checks `ls <docs_path>/artifacts/` shows the
id before calling the close done. If the agent can also publish the page elsewhere, it publishes this
same file. Commit it (`docs(<story>): close-out page`) before the card moves to `qa`; with an external
docs repo, commit there. Agents without any publish tool still write the file.

Write `<docs_path>/artifacts/<PREFIX>-<story>-closeout.html`: **self-contained** (inline CSS/JS, no CDN),
light + dark via `prefers-color-scheme`, usable at phone width. If the project declares `extra_tags`, show each
tag present on the card in the page header next to the story/type info. Sections:
1. **Goal & Scope explorer** - what was asked, what shipped, non-goals; expandable per DoD line.
2. **Interactive demo** - story-specific controls or a small simulation that shows the work (a toggle,
   slider, before/after, sample data). This is the part that proves it, so make it real, not decorative.
3. **Badges** - gate results and cold-review verdict/rounds with the real numbers (start/head SHA, test counts).
4. **Live Human QA** - the card's Verification Check items as checkboxes plus a **Validate & Complete Story**
    button, wired to the Cockpit API:
    - `POST /api/cards/<ID>/qa-item` body `{index, checked}`
    - `POST /api/cards/<ID>/validate` body `{notes}`
    - after each success: `window.parent.postMessage({type:'pmqa:refresh'}, location.origin)` and `window.parent.postMessage({type:'pmqa:toast', message: 'Story validated successfully!', level: 'success'}, location.origin)`.
    - Non-blocking feedback: never use `alert()`, `confirm()`, or `prompt()`. Show an inline animated glass toast banner and bridge notifications to the host window via `parent.postMessage({type: 'pmqa:toast', message, level})`.
    - Degrade gracefully: if `fetch` fails (opened as a plain file, or Cockpit not running), show a
      read-only notice and disable the controls. Never throw.
Validate is only for the user's click; an agent never presses it. Link the artifact in the card Changelog.

Related: `pm-qa-cockpit` (schema, CLI), `pm-sync`, `pm-review`.
