---
name: pm-sync
description: Use during a story build, after every phase commit, lane merge, gate result or review milestone, to keep the card's DoD checkboxes and PLAN.md in step. Never batch the ticking at the end.
metadata:
  version: 0.8.0
  source: pm-qa-cockpit
---

# PM sync - incremental DoD during build

**Why:** batching `[x]` at review or session end makes the dashboard and PLAN.md lie for hours.
**Rule:** the moment a DoD criterion becomes true, mark it `[x]` on **both** the card
(`<docs>/cards/<PREFIX>-<story> - *.md`) **and** the matching line in that story's section of
`<docs>/PLAN.md`. Use `cli.py check <id> <index>` or edit the markdown directly.

| Trigger | Minimum action |
|---|---|
| Build start (before first code) | `status: in-progress`, Changelog `Build started at <start SHA>`; tick nothing yet |
| Phase / concern commit | `[x]` every DoD line that commit actually satisfies; mirror in PLAN.md |
| Lane / branch merge | tick the lines the builder reported as completed |
| Gates green | `[x]` gate line on card + PLAN.md; Changelog with the real numbers |
| Review reaches a terminal state (clean PASS, or findings fixed and no round owed) | `[x]` review line; `status: qa` in the same turn; Changelog `Reviewer (Model) PASS` |
| Review fix round, more rounds owed | stay `in-progress` |
| User confirms Human QA | tick the Human QA check; `completed` + `validated:` via `cli.py validate` |
| Session end | reconcile: any built criterion still `[ ]` on card or PLAN.md gets fixed first |

**Granularity:** "phase commit" means each DoD line, not each `git commit`. If one commit satisfies three
lines, tick three; on a long heads-down streak, tick each line as it lands.

**Build-start rule:** a story at `backlog` / `story-backlog` must not receive code. Flip it to `in-progress`
with the start SHA first. If code was already written against an unstarted card, flip it and say so.

**Changelog row:** `| YYYY-MM-DD HH:MM | <Role (Model)> | <What> |`; `User` for human actions.

**Anti-pattern:** "I'll update the card when we're done."

**Delegation:** the ticking is mechanical, so hand it to the **pm-sync** agent the project's
`ORCHESTRATION.md` Roster names for your tool (a cheap model); with no Roster or no such agent, do it
yourself. The agent changes only checkboxes and Changelog rows.

For external docs, card and PLAN.md edits commit to the docs repo, not the code repo (see `pm-qa-cockpit`).
Never `git push`.
