---
name: pm-review
description: Use when a story's build is committed and gates are green, to run the cold review gate before the card moves to qa. Defines the separate cold reviewer, diff-only rounds, the review file format and the qa transition.
metadata:
  version: 0.6.2
  source: pm-qa-cockpit
---

# PM review - the cold gate

Every agent-built story card gets this gate before `qa`, one item or many. The user's later hands-on QA is
a different party and never replaces it. Only an ad-hoc chat fix with no card is exempt.

## Rules
- **Cold and separate:** dispatch a fresh agent on a stronger model than the builder, with no inherited
  context. Use the **reviewer** agent the project's `ORCHESTRATION.md` Roster names for your tool; with no
  Roster or no reviewer there, spawn a fresh agent on a stronger model yourself. Never `inherit`. The lead reviewing its own work never counts - it holds the assumptions that produced any bug.
- **Everything committed first:** a worktree or diff only sees committed history. Verify a clean tree.
- **Diff-only:** the reviewer reads `git diff <start_sha>..HEAD` (in `code_path`) plus the PM files and takes
  the builder's reported gate numbers. It does not re-run gates unless the brief flags the change as
  high-risk (auth, migrations, money, security).
- **Checks:** correctness, reuse/simplification, DoD vs the diff, card and PLAN.md boxes honest,
  `WORKLOG.md` entry, `QUALITY.md` gate numbers, `E2E_CANDIDATES.md` business-rule row, close-out
  artifact present (spec in `pm-orchestrate`).
- **Rounds, cap 3:** round 1 always. Run round N+1 only if round N found a correctness bug or a fix big
  enough to be wrong; later rounds read only the previous fix diff. Stop at PASS with zero findings, or
  with only docs/mechanical findings (fix them, scope-verify, no new cold agent). A would-be round 4 goes
  to the user.
- Findings are fixed in their own commit, never an amend.

## Review file
`<docs>/reviews/<PREFIX>-<story>-round-<n>.md`

```markdown
---
story: "<story>"
round: 1
verdict: PASS            # PASS | PASS with findings | CHANGES REQUESTED
reviewer: Reviewer (Model)
date: YYYY-MM-DD
start_sha: <sha>
head_sha: <sha>
---
## Findings
- [severity] file:line - what and why
## Checklist
- DoD vs diff / PLAN / WORKLOG / QUALITY / E2E_CANDIDATES / close-out artifact
```

(In the real file keep the frontmatter values bare, with no trailing comments.)

## On PASS
In the **same turn**: tick the review DoD line (`pm-sync`), set `status: qa`, add the Changelog row, and
make sure the close-out artifact exists. The card goes to `qa` immediately; waiting for user testing or
heavy gates is *why* it is in `qa`, not a blocker to reaching it.
For external projects review files and card edits commit to the docs repo. Never `git push`.
