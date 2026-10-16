# CLAUDE.md — pm-qa-cockpit (the tool)

This repo is **public** and holds **only the tool**. Keep it short; the details are in the skills.

- **PM data for this project lives outside this repo**, in the private docs repo:
  `../pm-qa-docs/pm-qa-cockpit/` (PLAN.md, WORKLOG.md, QUALITY.md, cards/, reviews/, artifacts/).
  Card prefix `PQC`. Never create `docs/`, `PLAN.md`, `WORKLOG.md` or cards here (`.gitignore` blocks them).
- **Loop:** follow `skills/pm-orchestrate/SKILL.md` and `../pm-qa-docs/pm-qa-cockpit/ORCHESTRATION.md`
  (grill-me before features, card `in-progress` with start SHA before code, local commits per chunk,
  cold review, close-out artifact, user confirms). Ad-hoc chat fixes need no card.
- **Gates: fast mode.** Before close run the whole suite inline:
  `uv run --system-certs --group dev pytest -q` (~2 min). Record the result in the docs repo's `QUALITY.md`.
- **Dev clone vs running copy.** Projects run the cockpit from `~/.pm-qa-cockpit/app`, not from here.
  A change reaches them only after merge to `main`, push, then
  `git -C $HOME\.pm-qa-cockpit\app pull` and `install.ps1 -Force` (skills). Never edit the running copy.
- **Compatibility:** money-nest (legacy `mn-id`/`sprint` schema) must keep rendering identically;
  `tests/test_money_nest_legacy_*.py` run against a local money-nest checkout when present.
- **Never push or publish without the user asking.** Use PowerShell for git on this machine (Git Bash
  intermittently fails with `.git/objects … Permission denied`); commit messages via `git commit -F <file>`.
- Standard library only for `cockpit/` (no runtime deps); PowerShell scripts stay 5.1-compatible.
