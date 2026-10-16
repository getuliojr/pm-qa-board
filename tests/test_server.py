"""HTTP API: project listing/switching/configuring, artifacts, QA writes on the active project."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from conftest import git, seed_external_docs

import projects


@pytest.fixture
def server(cockpit_home, legacy_project, monkeypatch):
    monkeypatch.setenv("PM_QA_COCKPIT_PROJECT_ROOT", str(legacy_project))
    import server as srv

    srv.STATE.workers.clear()
    srv.STATE.activate(projects.adhoc_project(legacy_project))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.PMRequestHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", srv
    httpd.shutdown()
    httpd.server_close()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read().decode("utf-8")


def post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_configure_switch_and_isolate_external_project(server, external_project):
    base, srv = server
    code, docs = external_project

    status, body = get(f"{base}/api/cards")
    assert status == 200 and [c["mn_id"] for c in json.loads(body)["cards"]] == ["MN-1.2"]

    status, res = post(
        f"{base}/api/projects/configure",
        {"name": "Story App", "code_path": str(code), "docs_path": str(docs), "prefix": "APP"},
    )
    assert status == 200 and res["success"], res
    assert res["project"]["docs_mode"] == "external"
    assert (docs / ".git").is_dir(), "external docs root is git-initialised"
    seed_external_docs(docs)
    srv.STATE.parser.invalidate_cache()

    status, body = get(f"{base}/api/cards")
    cards = json.loads(body)["cards"]
    assert [c["mn_id"] for c in cards] == ["APP-3.1"]
    assert cards[0]["status"] == "sprint-backlog"  # story-backlog groups with the legacy column

    git(docs, "config", "user.email", "t@example.com")
    git(docs, "config", "user.name", "T")
    status, res = post(f"{base}/api/cards/APP-3.1/qa-item", {"index": 0, "checked": True})
    assert status == 200 and res["success"]
    assert git(code, "status", "--porcelain") == "", "PM writes never land in the code repo"

    status, body = get(f"{base}/api/projects")
    view = json.loads(body)
    assert view["active_project"] == "story-app"
    assert {p["slug"] for p in view["projects"]} >= {"story-app"}


def test_select_switches_back_and_unknown_is_404(server, legacy_project, external_project):
    base, srv = server
    code, docs = external_project
    projects.upsert_project(projects.build_project("Legacy", legacy_project, prefix="MN"))
    projects.upsert_project(projects.build_project("Story App", code, prefix="APP", docs_path=docs))
    projects.ensure_docs_root(projects.load_registry()["projects"]["story-app"])

    status, res = post(f"{base}/api/projects/select", {"slug": "story-app"})
    assert status == 200 and res["project"]["slug"] == "story-app"
    assert srv.STATE.parser.docs_path == docs.resolve()

    status, res = post(f"{base}/api/projects/select", {"slug": "legacy"})
    assert status == 200
    _, body = get(f"{base}/api/cards")
    assert [c["mn_id"] for c in json.loads(body)["cards"]] == ["MN-1.2"]

    status, res = post(f"{base}/api/projects/select", {"slug": "nope"})
    assert status == 404 and not res["success"]


def test_configure_rejects_bad_input(server, tmp_path):
    base, _ = server
    status, res = post(f"{base}/api/projects/configure", {"name": "X", "code_path": str(tmp_path / "missing")})
    assert status == 400 and "code_path" in res["error"]


def test_artifact_served_and_traversal_blocked(server, legacy_project):
    base, srv = server
    art = legacy_project / "docs" / "artifacts"
    art.mkdir()
    (art / "MN-1.2-closeout.html").write_text("<h1>close-out</h1>", encoding="utf-8")
    srv.STATE.activate(projects.adhoc_project(legacy_project))

    status, body = get(f"{base}/artifacts/MN-1.2-closeout.html")
    assert status == 200 and "close-out" in body
    with pytest.raises(urllib.error.HTTPError) as err:
        get(f"{base}/artifacts/..%2F..%2FPLAN.md")
    assert err.value.code == 404


def test_index_is_rebranded(server):
    base, _ = server
    _, html = get(f"{base}/")
    assert "PM &amp; QA Cockpit" in html or "PM & QA Cockpit" in html


def raw_post(url, data: bytes, headers: dict):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers


def test_csrf_guard_blocks_cross_site_posts(server, external_project):
    """Review round 1 #1: a page the user visits must not drive configure / heavy-gate."""
    base, srv = server
    code, docs = external_project
    port = base.rsplit(":", 1)[1]
    body = json.dumps({"name": "Evil", "code_path": str(code), "docs_path": str(docs)}).encode()

    # "simple" cross-site request: text/plain skips the CORS preflight
    status, _ = raw_post(f"{base}/api/projects/configure", body, {"Content-Type": "text/plain"})
    assert status == 403
    # JSON but from a foreign origin
    status, _ = raw_post(
        f"{base}/api/projects/configure", body, {"Content-Type": "application/json", "Origin": "http://evil.example"}
    )
    assert status == 403
    status, _ = raw_post(
        f"{base}/api/heavy-gate/start", b"{}", {"Content-Type": "text/plain", "Origin": "http://evil.example"}
    )
    assert status == 403
    assert not docs.exists(), "nothing was created by the rejected requests"
    # the cockpit's own origin is fine, and no CORS grant is ever sent
    status, headers = raw_post(
        f"{base}/api/projects/configure", body, {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
    )
    assert status == 200 and headers.get("Access-Control-Allow-Origin") is None


def test_configure_ignores_gate_commands_from_body(server, external_project):
    base, srv = server
    code, docs = external_project
    status, res = post(
        f"{base}/api/projects/configure",
        {"name": "Story App", "code_path": str(code), "docs_path": str(docs),
         "gates": {"mode": "heavy", "heavy_script": "C:/evil.ps1"}},
    )
    assert status == 200
    assert "heavy_script" not in (res["project"].get("gates") or {})
    assert "heavy_script" not in (projects.load_registry()["projects"]["story-app"].get("gates") or {})


def test_reactivating_rebuilds_idle_worker(server, legacy_project, tmp_path):
    """Review round 1 #3: a reconfigured docs_path reaches the heavy-gate worker without a restart."""
    _, srv = server
    first = srv.STATE.worker
    other_docs = tmp_path / "moved-docs"
    project = projects.build_project("Legacy", legacy_project, slug=srv.STATE.project["slug"], prefix="MN",
                                     docs_path=other_docs)
    projects.ensure_docs_root(project)
    srv.STATE.activate(project)
    assert srv.STATE.worker is not first
    assert srv.STATE.worker.log_file_path.parent == other_docs.resolve() / "gate-heavy-results"
