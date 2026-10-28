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
    code, docs = proj
    entry = {"slug": "app", "code_path": str(code), "docs_path": str(docs), "prefix": "APP"}
    projects.save_registry({"active_project": "app", "projects": {"app": entry}})
    assert make_parser(proj)._index.db_path.name == "app.db"


def test_unregistered_projects_sharing_a_slug_get_distinct_dbs(tmp_path):
    dbs = []
    for parent in ("a", "b"):
        code = init_repo(tmp_path / parent / "app")
        project = projects.adhoc_project(code)
        dbs.append(PMParser(project=project)._index.db_path)
    assert dbs[0] != dbs[1]


def test_suite_never_uses_the_real_board_home(tmp_path):
    assert projects.board_home() != Path.home() / ".pm-qa-board"
    assert tmp_path in projects.board_home().parents


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


def test_history_rewrite_drops_replaced_commits(proj, no_thread):
    code, _ = proj
    since = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    commit(code, "b.txt")
    p = make_parser(proj)
    p._INLINE_BUDGET = 100
    assert len(p._get_git_churn(since_date=since)) == 2
    git(code, "commit", "-q", "--amend", "-m", "reworded")  # same count, new SHA
    commits = p._get_git_churn(since_date=since)
    assert len(commits) == 2 and "reworded" in [c["subject"] for c in commits]
    git(code, "reset", "-q", "--hard", "HEAD~1")
    assert len(p._get_git_churn(since_date=since)) == 1


def test_git_failure_is_flagged_and_its_payload_not_cached(proj, counted, monkeypatch):
    p = make_parser(proj)
    monkeypatch.setattr(PMParser, "_sync_git_commits", lambda self, bound: (_ for _ in ()).throw(RuntimeError("boom")))
    first = p._cached_project_analytics("6w")
    assert first["git_backfill"].get("error") is True
    p._cached_project_analytics("6w")
    assert len(counted) == 2


def test_inline_counting_is_skipped_while_the_background_thread_runs(proj, monkeypatch):
    code, _ = proj
    bulk_commits(code, 5)
    p = make_parser(proj)
    monkeypatch.setattr(PMParser, "_backfill_running", lambda self: True)
    monkeypatch.setattr(PMParser, "_start_backfill", lambda self: None)
    p._get_git_churn(since_date=(datetime.date.today() - datetime.timedelta(days=5)).isoformat())
    assert p._git_backfill["counted"] == 0


def test_read_only_computes_live_and_creates_no_files(proj):
    code, _ = proj
    commit(code, "b.txt")
    p = make_parser(proj, read_only=True)
    commits = p._get_git_churn(since_date=(datetime.date.today() - datetime.timedelta(days=5)).isoformat())
    assert len(commits) == 2 and sum(c["added"] for c in commits) == 4
    p.get_analytics_summary("6w")
    assert not (projects.board_home() / "cache").exists()


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


# ---------------------------------------------------------------- Phase 4: frontend contract

STATIC = Path(__file__).resolve().parent.parent / "board" / "static"


def test_frontend_wires_skeletons_stale_guard_and_backfill_badge():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "activeTab === 'analytics'" in app_js and "renderAnalyticsSkeleton()" in app_js
    assert "Stale guard" in app_js and "analyticsRequestKey(state.analyticsWindow" in app_js
    assert "Counting history… ${backfill.counted} of ${backfill.total} commits" in app_js
    assert "state.analyticsCache = {};\n  state.analyticsFilter" not in app_js  # cache survives a project switch
    for element_id in ("analyticsKpiStrip", "analyticsBackfillBadge", "chartThroughputContainer", "weeklyCohortsTableBody"):
        assert f'id="{element_id}"' in index_html


def test_frontend_switch_restores_cached_analytics_without_a_skeleton():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "restoreCachedAnalytics()" in app_js
    assert "state.pendingProjectSlug" in app_js
    assert "const data = changed ? fresh : cached;" in app_js


# ---------------------------------------------------------------- Phase 5: index optimization & repo defense


def test_git_commits_optimized_indexes_exist_and_eliminate_temp_sorts(tmp_path):
    from index import CardIndex

    db_path = tmp_path / "index_test.db"
    idx = CardIndex(db_path)
    conn = idx._get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'git_commits'")
        index_names = {row[0] for row in cur.fetchall()}
        assert "idx_git_date" in index_names
        assert "idx_git_reachable_date" in index_names
        assert "idx_git_uncounted" in index_names

        # Verify query plan for get_uncounted_shas uses idx_git_uncounted covering index
        # and creates NO temporary B-tree
        cur.execute(
            "EXPLAIN QUERY PLAN SELECT sha FROM git_commits "
            "WHERE reachable = 1 AND counted = 0 ORDER BY date DESC, sha LIMIT 10"
        )
        uncounted_plan = cur.fetchall()
        uncounted_text = " ".join(r[3] for r in uncounted_plan)
        assert "idx_git_uncounted" in uncounted_text
        assert "TEMP B-TREE" not in uncounted_text

        # Verify query plan for get_git_commits uses idx_git_reachable_date
        # and creates NO temporary B-tree
        cur.execute(
            "EXPLAIN QUERY PLAN SELECT sha, date, subject, author, added, deleted FROM git_commits "
            "WHERE reachable = 1 AND date >= '2026-01-01' ORDER BY date DESC, sha"
        )
        commits_plan = cur.fetchall()
        commits_text = " ".join(r[3] for r in commits_plan)
        assert "idx_git_reachable_date" in commits_text
        assert "TEMP B-TREE" not in commits_text
    finally:
        conn.close()


def test_gitignore_ignores_sqlite_databases_and_cache_directories():
    repo_root = Path(__file__).resolve().parent.parent
    gitignore_text = (repo_root / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("*.db", "*.sqlite", "*.sqlite3", "cache/"):
        assert pattern in gitignore_text, f"{pattern} missing from .gitignore"

