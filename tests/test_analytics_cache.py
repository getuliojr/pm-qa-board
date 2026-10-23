"""Story 32: per-commit git cache, analytics payload cache, Story 31 cache hardening."""
from __future__ import annotations

import datetime
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

import parser as parser_mod
import projects
from conftest import git, init_repo
from index import SCHEMA_VERSION, CardIndex
from parser import PMParser


def commit(repo: Path, name: str, msg: str | None = None) -> None:
    (repo / name).write_text(f"{name}\nline2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg or f"add {name}")


def bulk_commits(repo: Path, count: int, prefix: str = "f") -> None:
    """`count` one-file commits through a single `git fast-import` (a commit per process is too slow)."""
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    parent = git(repo, "rev-parse", "HEAD").strip()
    stamp = int(time.time()) - count
    out: list[str] = []
    for i in range(count):
        msg = f"bulk {prefix}{i}"
        body = f"{prefix}{i}\nline2\n"
        out += [
            f"commit refs/heads/{branch}",
            f"committer Test <test@example.com> {stamp + i} +0000",
            f"data {len(msg)}",
            msg,
        ]
        if i == 0:
            out.append(f"from {parent}")  # later commits continue the branch tip implicitly
        out += [f"M 100644 inline {prefix}{i}.txt", f"data {len(body)}", body]
    data = "\n".join(out) + "\n"
    subprocess.run(["git", "fast-import", "--quiet"], cwd=str(repo), input=data.encode("utf-8"), check=True)


@pytest.fixture
def proj(tmp_path):
    code = init_repo(tmp_path / "app")
    docs = tmp_path / "app-docs"
    (docs / "cards").mkdir(parents=True)
    (docs / "reviews").mkdir()
    (docs / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (docs / "QUALITY.md").write_text("# Quality\n", encoding="utf-8")
    commit(code, "a.txt")
    return code, docs


def make_parser(proj, **kw) -> PMParser:
    code, docs = proj
    return PMParser(project={"slug": "app", "code_path": str(code), "docs_path": str(docs), "prefix": "APP"}, **kw)


@pytest.fixture
def no_thread(monkeypatch):
    """Keep backfill deterministic: the tests drive it inline."""
    monkeypatch.setattr(PMParser, "_start_backfill", lambda self: None)


# ---------------------------------------------------------------- Phase 0


def test_schema_version_mismatch_wipes_every_cache_table(tmp_path):
    db = tmp_path / "x.db"
    idx = CardIndex(db)
    idx.upsert_cards_batch([("c.md", 1.0, 1, {"id": "A-1", "status": "backlog"})])
    idx.put_analytics("6w", "s", "k", 1.0, "{}")
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()

    reopened = CardIndex(db)
    assert reopened.get_all_cards() == {}
    assert reopened.get_analytics("6w", "s", "k", 600, 2.0) is None
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()
    # same version again: nothing is wiped
    reopened.upsert_cards_batch([("c.md", 1.0, 1, {"id": "A-1", "status": "backlog"})])
    assert len(CardIndex(db).get_all_cards()) == 1


def test_adhoc_projects_with_same_docs_folder_name_get_distinct_dbs(tmp_path):
    dbs = []
    for name in ("one", "two"):
        code = init_repo(tmp_path / name)
        (code / "docs").mkdir()
        dbs.append(PMParser(code_path=code, docs_path=code / "docs")._index.db_path)
    assert dbs[0] != dbs[1]
    assert all(d.name.startswith("docs-") for d in dbs)


def test_registered_project_db_is_named_by_slug(proj):
    assert make_parser(proj)._index.db_path.name == "app.db"


def test_suite_never_uses_the_real_cockpit_home(tmp_path):
    assert projects.cockpit_home() != Path.home() / ".pm-qa-cockpit"
    assert tmp_path in projects.cockpit_home().parents


# ---------------------------------------------------------------- Phase 1: git cache


def test_more_than_fifty_commits_in_window_are_all_counted(proj, no_thread):
    code, _ = proj
    bulk_commits(code, 60)
    p = make_parser(proj)
    p._INLINE_BUDGET = 1000
    commits = p._get_git_churn(since_date=(datetime.date.today() - datetime.timedelta(days=5)).isoformat())
    assert len(commits) == 61
    assert sum(c["added"] for c in commits) == 61 * 2
    assert p._git_backfill == {"done": True, "counted": 61, "total": 61}


def test_incremental_sync_reads_numstat_only_for_new_commits(proj, no_thread, monkeypatch):
    code, _ = proj
    p = make_parser(proj)
    since = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    p._get_git_churn(since_date=since)

    seen: list[str] = []
    real = PMParser._git_log

    def spy(self, extra, timeout, stdin=None, numstat=False):
        if stdin:
            seen.extend(stdin.split())
        return real(self, extra, timeout, stdin, numstat)

    monkeypatch.setattr(PMParser, "_git_log", spy)
    p._get_git_churn(since_date=since)
    assert seen == []  # nothing new: no numstat at all

    commit(code, "b.txt")
    new_sha = git(code, "rev-parse", "HEAD").strip()
    commits = make_parser(proj)._get_git_churn(since_date=since)
    assert seen == [new_sha]
    assert len(commits) == 2


def test_backfill_progress_is_reported_and_never_stored_partial(proj, no_thread):
    code, _ = proj
    for i in range(5):
        commit(code, f"f{i}.txt")
    p = make_parser(proj)
    p._INLINE_BUDGET = 2
    p._get_git_churn(since_date=(datetime.date.today() - datetime.timedelta(days=5)).isoformat())
    assert p._git_backfill == {"done": False, "counted": 2, "total": 6}

    calls = []
    real = PMParser._compute_project_analytics
    PMParser._compute_project_analytics = lambda self, w: calls.append(w) or real(self, w)
    try:
        for _ in range(2):
            p._cached_project_analytics("6w")
    finally:
        PMParser._compute_project_analytics = real
    assert len(calls) == 2  # partial payload was not cached


def test_background_thread_finishes_the_backfill(proj):
    code, _ = proj
    for i in range(5):
        commit(code, f"f{i}.txt")
    p = make_parser(proj)
    p._INLINE_BUDGET = 1
    since = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    p._get_git_churn(since_date=since)
    deadline = time.time() + 20
    while time.time() < deadline:
        p._get_git_churn(since_date=since)
        if p._git_backfill["done"]:
            break
        time.sleep(0.1)
    assert p._git_backfill == {"done": True, "counted": 6, "total": 6}


def test_branch_switch_counts_only_reachable_commits(proj, no_thread):
    code, _ = proj
    since = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    base = git(code, "rev-parse", "--abbrev-ref", "HEAD").strip()
    git(code, "checkout", "-q", "-b", "feature")
    commit(code, "x.txt")
    commit(code, "y.txt")
    p = make_parser(proj)
    p._INLINE_BUDGET = 100
    assert len(p._get_git_churn(since_date=since)) == 3
    git(code, "checkout", "-q", base)
    assert len(p._get_git_churn(since_date=since)) == 1
    git(code, "checkout", "-q", "feature")
    assert len(p._get_git_churn(since_date=since)) == 3


def test_read_only_computes_live_and_creates_no_files(proj):
    code, _ = proj
    commit(code, "b.txt")
    p = make_parser(proj, read_only=True)
    commits = p._get_git_churn(since_date=(datetime.date.today() - datetime.timedelta(days=5)).isoformat())
    assert len(commits) == 2 and sum(c["added"] for c in commits) == 4
    p.get_analytics_summary("6w")
    assert not (projects.cockpit_home() / "cache").exists()


@pytest.mark.parametrize("mode", ["loose", "packed", "detached", "worktree"])
def test_head_reader_matches_rev_parse(proj, tmp_path, mode):
    code, _ = proj
    commit(code, "b.txt")
    target = code
    if mode == "packed":
        git(code, "pack-refs", "--all")
    elif mode == "detached":
        git(code, "checkout", "-q", "--detach")
    elif mode == "worktree":
        target = tmp_path / "wt"
        git(code, "worktree", "add", "-q", "-b", "wtb", str(target))
        assert (target / ".git").is_file()
    p = PMParser(code_path=target, docs_path=proj[1])
    assert p._read_git_head() == git(target, "rev-parse", "HEAD").strip()


# ---------------------------------------------------------------- Phase 2: payload cache


@pytest.fixture
def counted(monkeypatch):
    calls: list[str] = []
    real = PMParser._compute_project_analytics

    def wrap(self, window):
        calls.append(window)
        return real(self, window)

    monkeypatch.setattr(PMParser, "_compute_project_analytics", wrap)
    return calls


def test_payload_cache_hit_and_each_invalidation(proj, counted, monkeypatch):
    code, docs = proj
    p = make_parser(proj)
    first = p._cached_project_analytics("6w")
    assert p._cached_project_analytics("6w") == first
    assert len(counted) == 1  # second call was a hit

    def expect_miss(label, mutate):
        before = len(counted)
        mutate()
        p._cached_project_analytics("6w")
        assert len(counted) == before + 1, label
        p._cached_project_analytics("6w")
        assert len(counted) == before + 1, f"{label}: re-cached"

    expect_miss("new commit", lambda: commit(code, "n.txt"))
    expect_miss(
        "card edit",
        lambda: (docs / "cards" / "APP-1 - X.md").write_text("---\nid: APP-1\nstory: \"1\"\nstatus: story-backlog\n---\n# APP-1 - X\n", encoding="utf-8"),
    )
    expect_miss("new review", lambda: (docs / "reviews" / "APP-1-review.md").write_text("# r\n", encoding="utf-8"))
    expect_miss("QUALITY.md", lambda: (docs / "QUALITY.md").write_text("# Quality\nmore\n", encoding="utf-8"))
    expect_miss("PLAN.md", lambda: (docs / "PLAN.md").write_text("# Plan\nmore\n", encoding="utf-8"))
    expect_miss("VERSION", lambda: monkeypatch.setattr(PMParser, "_app_version", staticmethod(lambda: "9.9.9")))
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    expect_miss("date change", lambda: monkeypatch.setattr(PMParser, "_today", staticmethod(lambda: tomorrow)))
    p.project["extra_tags"] = [{"key": "jira", "label": "Jira"}]
    expect_miss("tag config", lambda: p.project.update(extra_tags=[{"key": "ticket", "label": "T"}]))


def test_payload_older_than_ten_minutes_recomputes(proj, counted, monkeypatch):
    p = make_parser(proj)
    p._cached_project_analytics("6w")
    real = time.time()
    monkeypatch.setattr(parser_mod.time, "time", lambda: real + 601)
    p._cached_project_analytics("6w")
    assert len(counted) == 2


def test_only_latest_row_per_window_and_scope_is_kept(proj):
    p = make_parser(proj)
    p._cached_project_analytics("6w")
    commit(proj[0], "n.txt")
    p._cached_project_analytics("6w")
    conn = sqlite3.connect(p._index.db_path)
    assert conn.execute("SELECT COUNT(*) FROM analytics_cache WHERE window = '6w'").fetchone()[0] == 1
    conn.close()


def test_warm_switch_back_is_fast(proj):
    code, _ = proj
    bulk_commits(code, 30)
    p = make_parser(proj)
    p._INLINE_BUDGET = 1000
    p._cached_project_analytics("6w")  # cold
    started = time.perf_counter()
    again = make_parser(proj)  # a fresh parser, as before the pool
    again._cached_project_analytics("6w")
    assert time.perf_counter() - started < 0.5


# ---------------------------------------------------------------- Phase 3: parser pool


def test_server_pool_reuses_parser_until_config_changes(proj):
    import server

    code, docs = proj
    project = {"slug": "pool-app", "code_path": str(code), "docs_path": str(docs), "prefix": "APP"}
    first = server.STATE.parser_for(project)
    assert server.STATE.parser_for(dict(project)) is first
    assert first.parser_provider == server.STATE.parser_for
    rebuilt = server.STATE.parser_for({**project, "prefix": "ZZ"})
    assert rebuilt is not first and rebuilt.prefix == "ZZ"
    tagged = server.STATE.parser_for({**project, "prefix": "ZZ", "extra_tags": [{"key": "jira", "label": "J"}]})
    assert tagged is not rebuilt
