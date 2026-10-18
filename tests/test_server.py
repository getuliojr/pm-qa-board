"""HTTP API: project listing/switching/configuring, artifacts, QA writes on the active project."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

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


def test_switching_back_keeps_finished_run_state(server, legacy_project, external_project):
    """Review round 2 #1: A -> B -> A must not wipe A's last heavy-gate status/log."""
    _, srv = server
    code, docs = external_project
    a = srv.STATE.project
    worker_a = srv.STATE.worker
    worker_a.status, worker_a.exit_code = "completed", 0
    worker_a.log_lines.append("run finished")
    b = projects.build_project("Story App", code, prefix="APP", docs_path=docs)
    projects.ensure_docs_root(b)
    srv.STATE.activate(b)
    srv.STATE.activate(a)
    assert srv.STATE.worker is worker_a
    assert srv.STATE.worker.get_status()["status"] == "completed"
    assert srv.STATE.worker.get_log() == ["run finished"]


def test_server_info_and_lan_ip(server):
    base, srv = server
    status, body = get(f"{base}/api/server-info")
    assert status == 200
    info = json.loads(body)
    assert "host" in info
    assert "port" in info
    assert "lan_ip" in info
    assert "token_required" in info
    assert srv.get_lan_ip() is not None


def test_token_auth_remote_guard(server, monkeypatch):
    base, srv = server
    monkeypatch.setattr(srv, "TOKEN", "secret-token-123")

    # Localhost requests bypass token by default
    status, body = get(f"{base}/api/server-info")
    assert status == 200

    # Remote or tunnel request (simulated via CF-Connecting-IP header) requires token
    req_unauth = urllib.request.Request(
        f"{base}/api/cards",
        headers={"CF-Connecting-IP": "203.0.113.1"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req_unauth)
    assert exc_info.value.code == 401

    # Authorized with X-Cockpit-Token header
    req_header = urllib.request.Request(
        f"{base}/api/cards",
        headers={"CF-Connecting-IP": "203.0.113.1", "X-Cockpit-Token": "secret-token-123"},
    )
    with urllib.request.urlopen(req_header) as r:
        assert r.status == 200

    # Authorized with ?token=... query parameter
    req_query = urllib.request.Request(
        f"{base}/api/cards?token=secret-token-123",
        headers={"CF-Connecting-IP": "203.0.113.1"},
    )
    with urllib.request.urlopen(req_query) as r:
        assert r.status == 200

    # Authorized with Cookie
    req_cookie = urllib.request.Request(
        f"{base}/api/cards",
        headers={"CF-Connecting-IP": "203.0.113.1", "Cookie": "cockpit_token=secret-token-123"},
    )
    with urllib.request.urlopen(req_cookie) as r:
        assert r.status == 200

    # POST unauthorized
    post_unauth = urllib.request.Request(
        f"{base}/api/projects/select",
        data=json.dumps({"slug": "legacy"}).encode(),
        headers={"Content-Type": "application/json", "CF-Connecting-IP": "203.0.113.1"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(post_unauth)
    assert exc_info.value.code == 401


def test_post_allowed_with_host_header(server, legacy_project):
    base, srv = server
    projects.upsert_project(projects.build_project("Legacy", legacy_project, prefix="MN"))
    # Origin matching request Host header is permitted (for LAN / tunnel)
    req = urllib.request.Request(
        f"{base}/api/projects/select",
        data=json.dumps({"slug": "legacy"}).encode(),
        headers={
            "Content-Type": "application/json",
            "Host": "my-mobile-host:3333",
            "Origin": "http://my-mobile-host:3333",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        assert r.status == 200

    # Origin differing from Host header and localhost is rejected
    req_bad = urllib.request.Request(
        f"{base}/api/projects/select",
        data=json.dumps({"slug": "legacy"}).encode(),
        headers={
            "Content-Type": "application/json",
            "Host": "my-mobile-host:3333",
            "Origin": "http://evil-site.com",
        },
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req_bad)
    assert exc_info.value.code == 403


def test_heavy_worker_proxy_compatibility(server):
    _, srv = server
    assert hasattr(srv, "HEAVY_WORKER")
    assert srv.HEAVY_WORKER.status == srv.STATE.worker.status


def test_doctor_check_11_mobile_readiness(server, legacy_project, monkeypatch):
    import doctor
    _, srv = server

    # With LAN IP detected
    monkeypatch.setattr(srv, "get_lan_ip", lambda: "192.168.1.100")
    report = doctor.run(str(legacy_project))
    check11 = [c for c in report["checks"] if c["n"] == 11][0]
    assert check11["status"] == "pass"
    assert "192.168.1.100" in check11["detail"]

    # With loopback only
    monkeypatch.setattr(srv, "get_lan_ip", lambda: "127.0.0.1")
    report_loopback = doctor.run(str(legacy_project))
    check11_lb = [c for c in report_loopback["checks"] if c["n"] == 11][0]
    assert check11_lb["status"] == "warn"
    assert "no LAN IP detected" in check11_lb["detail"]


def test_server_keep_alive(server):
    _, srv = server
    # Execution should not raise exception
    res_on = srv.set_keep_alive(True)
    res_off = srv.set_keep_alive(False)
    assert isinstance(res_on, bool)
    assert isinstance(res_off, bool)


def test_post_allowed_with_tunnel_and_forwarded_host(server, legacy_project, monkeypatch):
    base, srv = server
    projects.upsert_project(projects.build_project("Legacy", legacy_project, prefix="MN"))

    # Origin matching X-Forwarded-Host header is permitted (Cloudflare tunnel proxy)
    req_fwd = urllib.request.Request(
        f"{base}/api/projects/select",
        data=json.dumps({"slug": "legacy"}).encode(),
        headers={
            "Content-Type": "application/json",
            "Host": "127.0.0.1:3333",
            "X-Forwarded-Host": "my-quick-tunnel.trycloudflare.com",
            "Origin": "https://my-quick-tunnel.trycloudflare.com",
        },
        method="POST",
    )
    with urllib.request.urlopen(req_fwd) as r:
        assert r.status == 200

    # Origin matching configured TUNNEL_URL is permitted
    monkeypatch.setattr(srv, "TUNNEL_URL", "https://custom-tunnel.trycloudflare.com")
    req_tunnel = urllib.request.Request(
        f"{base}/api/projects/select",
        data=json.dumps({"slug": "legacy"}).encode(),
        headers={
            "Content-Type": "application/json",
            "Host": "127.0.0.1:3333",
            "Origin": "https://custom-tunnel.trycloudflare.com",
        },
        method="POST",
    )
    with urllib.request.urlopen(req_tunnel) as r:
        assert r.status == 200


def test_doctor_check_11_custom_cloudflared_in_cockpit_home(server, legacy_project, monkeypatch, tmp_path):
    import doctor
    _, srv = server
    fake_home = tmp_path / "cockpit-home"
    monkeypatch.setenv("PM_QA_COCKPIT_HOME", str(fake_home))
    custom_bin = fake_home / "bin"
    custom_bin.mkdir(parents=True, exist_ok=True)
    bin_name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    fake_cf = custom_bin / bin_name
    fake_cf.write_text("fake binary")

    monkeypatch.setattr(srv, "get_lan_ip", lambda: "192.168.1.150")
    report = doctor.run(str(legacy_project))
    check11 = [c for c in report["checks"] if c["n"] == 11][0]
    assert check11["status"] == "pass"
    assert "cloudflared installed" in check11["detail"]
    assert str(fake_cf) in check11["detail"]


def test_dashboard_ps1_parameter_syntax():
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        pytest.skip("powershell not available")
    dashboard = Path(__file__).resolve().parent.parent / "dashboard.ps1"
    res = subprocess.run(
        [ps, "-NoProfile", "-Command", f"Get-Command '{dashboard}' -Syntax; (Get-Command '{dashboard}').Parameters['Tunnel'].Aliases"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "-Mobile" in res.stdout
    assert "-Local" in res.stdout
    assert "-Tunnel" in res.stdout
    assert "-Mode" in res.stdout
    assert "Public" in res.stdout


