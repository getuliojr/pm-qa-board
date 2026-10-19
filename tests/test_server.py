"""HTTP API: project listing/switching/configuring, artifacts, QA writes on the active project."""

from __future__ import annotations

import json
import re
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
    assert "building_count" in view["current"]
    assert isinstance(view["current"]["building_count"], int)
    for p in view["projects"]:
        assert "building_count" in p
        assert isinstance(p["building_count"], int)


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
    assert "PM &amp; QA Board" in html or "PM & QA Board" in html


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

    # Unauthenticated GET / returns 401 HTML with interactive token form
    req_html_unauth = urllib.request.Request(
        f"{base}/",
        headers={"CF-Connecting-IP": "203.0.113.1"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req_html_unauth)
    assert exc_info.value.code == 401
    err_html = exc_info.value.read().decode("utf-8")
    assert "PM &amp; QA Board" in err_html
    assert "Connect to Board" in err_html
    assert 'id="tok"' in err_html

    # Authenticated GET / over HTTPS sets Secure cookie
    req_html_auth_https = urllib.request.Request(
        f"{base}/?token=secret-token-123",
        headers={"CF-Connecting-IP": "203.0.113.1", "X-Forwarded-Proto": "https"},
    )
    with urllib.request.urlopen(req_html_auth_https) as r:
        assert r.status == 200
        set_cookie = r.headers.get("Set-Cookie", "")
        assert "cockpit_token=secret-token-123" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=Lax" in set_cookie


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


def test_notification_config_get_and_post(server, external_project):
    base, srv = server
    code, docs = external_project

    # Configure external project
    post(
        f"{base}/api/projects/configure",
        {"name": "NotifyPrj", "code_path": str(code), "docs_path": str(docs), "docs_mode": "external"},
    )

    # GET default config
    status, body = get(f"{base}/api/notifications/config?slug=notifyprj")
    assert status == 200
    res = json.loads(body)
    assert res["success"] is True
    cfg = res["config"]
    assert cfg["sound_enabled"] is True
    assert cfg["push_enabled"] is True
    assert "notifyprj" in cfg["ntfy_topic"]

    # POST update config
    status, updated = post(
        f"{base}/api/notifications/config",
        {"slug": "notifyprj", "sound_enabled": False, "push_enabled": True, "ntfy_topic": "custom-notify-topic"},
    )
    assert status == 200
    assert updated["success"] is True
    assert updated["config"]["sound_enabled"] is False
    assert updated["config"]["ntfy_topic"] == "custom-notify-topic"

    # Verify persisted GET
    status, body2 = get(f"{base}/api/notifications/config?slug=notifyprj")
    assert status == 200
    res2 = json.loads(body2)
    assert res2["config"]["sound_enabled"] is False
    assert res2["config"]["ntfy_topic"] == "custom-notify-topic"


def test_notification_test_endpoint(server, monkeypatch):
    base, srv = server
    dispatched = []

    def fake_send(topic, **kwargs):
        dispatched.append({"topic": topic, **kwargs})
        return True, "Notification sent"

    monkeypatch.setattr(srv, "send_ntfy_notification", fake_send)

    status, res = post(
        f"{base}/api/notifications/test",
        {"topic": "my-test-topic"},
    )
    assert status == 200
    assert res["success"] is True
    assert res["topic"] == "my-test-topic"
    assert len(dispatched) == 1
    assert dispatched[0]["topic"] == "my-test-topic"


def test_notification_watcher_transitions(cockpit_home, external_project, monkeypatch):
    import server as srv
    from conftest import seed_external_docs

    code, docs = external_project
    proj = projects.build_project(
        name="WatchPrj", code_path=code, docs_path=docs, docs_mode="external"
    )
    projects.ensure_docs_root(proj)
    seed_external_docs(docs)
    projects.upsert_project(proj, activate=True)
    srv.STATE.activate(proj)

    watcher = srv.NotificationWatcher()
    dispatched = []

    def fake_send(topic, **kwargs):
        dispatched.append({"topic": topic, **kwargs})
        return True, "Notification sent"

    monkeypatch.setattr(srv, "send_ntfy_notification", fake_send)

    # Initial check seeds the state - no alerts sent
    alerts = watcher.check_transitions()
    assert len(alerts) == 0

    # Modify card status from story-backlog to qa
    card_file = docs / "cards" / "APP-3.1 - New story card.md"
    assert card_file.exists()
    content = card_file.read_text(encoding="utf-8")
    card_file.write_text(content.replace("status: story-backlog", "status: qa"), encoding="utf-8")

    # Second check should detect transition to qa and trigger alert
    alerts = watcher.check_transitions()
    assert len(alerts) == 1
    assert alerts[0]["new_status"] == "qa"
    assert alerts[0]["card_id"] == "APP-3.1"
    assert len(dispatched) == 1
    assert "Ready for QA" in dispatched[0]["title"]

    # Transition to completed
    content = card_file.read_text(encoding="utf-8")
    card_file.write_text(content.replace("status: qa", "status: completed"), encoding="utf-8")

    alerts = watcher.check_transitions()
    assert len(alerts) == 1
    assert alerts[0]["new_status"] == "completed"
    assert len(dispatched) == 2
    assert "Story Completed" in dispatched[1]["title"]


def test_static_assets_contain_story8_sound_and_notifications():
    static_dir = Path(__file__).resolve().parent.parent / "cockpit" / "static"
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")

    # Toolbar sound button
    assert 'id="soundToggleBtn"' in index_html
    assert 'id="soundToggleIcon"' in index_html
    assert 'toggleSoundMute()' in index_html

    # Mobile connect notification panel
    assert 'id="browserNotificationStatusBadge"' in index_html
    assert 'id="enableNotificationsBtn"' in index_html
    assert 'id="ntfyTopicInput"' in index_html
    assert 'id="ntfySubscribeLink"' in index_html
    assert 'id="testAlertBtn"' in index_html
    assert 'sendTestAlert()' in index_html

    # app.js functions & engine
    assert "function playChime(" in app_js
    assert "function playNote(" in app_js
    assert "function toggleSoundMute(" in app_js
    assert "function detectCardTransitions(" in app_js
    assert "function handleUrlDeepLink(" in app_js
    assert "function requestNotificationPermission(" in app_js
    assert "function sendTestAlert(" in app_js
    assert "navigator.vibrate" in app_js


def test_system_update_status_endpoint(server):
    base, srv = server
    status, body = get(f"{base}/api/system/update-status")
    assert status == 200
    res = json.loads(body)
    assert res["success"] is True
    assert "current_version" in res
    assert "current_commit" in res
    assert "behind_count" in res
    assert "is_dirty" in res
    assert "incoming_commits" in res


def test_system_update_apply_endpoint_dirty_check(server, monkeypatch):
    base, srv = server
    import server as srv_mod

    monkeypatch.setattr(
        srv_mod,
        "apply_system_update",
        lambda target=None: (
            False,
            {
                "success": False,
                "error": "Working tree has uncommitted changes.",
                "dirty_files": ["foo.txt"],
            },
        ),
    )
    status, res = post(f"{base}/api/system/update-apply", {})
    assert status == 400
    assert res["success"] is False
    assert "Working tree" in res["error"]


def test_system_update_apply_endpoint_success(server, monkeypatch):
    base, srv = server
    import server as srv_mod

    monkeypatch.setattr(
        srv_mod,
        "apply_system_update",
        lambda target=None: (
            True,
            {
                "success": True,
                "old_commit": "aaa1111",
                "new_commit": "bbb2222",
                "changed_files": ["cockpit/static/app.js"],
                "skills_outdated": True,
            },
        ),
    )
    status, res = post(f"{base}/api/system/update-apply", {})
    assert status == 200
    assert res["success"] is True
    assert res["new_commit"] == "bbb2222"
    assert res["skills_outdated"] is True


def test_static_assets_contain_story9_switcher_and_updates():
    static_dir = Path(__file__).resolve().parent.parent / "cockpit" / "static"
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")

    # Connection path switcher
    assert 'id="connectionPathSwitcher"' in index_html
    assert 'id="pathTunnelBtn"' in index_html
    assert 'id="pathLanBtn"' in index_html
    assert "switchConnectionPath('tunnel')" in index_html
    assert "switchConnectionPath('lan')" in index_html

    # Sidebar version badge & Software update modal
    assert 'id="boardVersionBadge"' in index_html
    assert 'id="softwareUpdateModal"' in index_html
    assert 'id="checkForUpdatesBtn"' in index_html
    assert 'id="installUpdateBtn"' in index_html
    assert 'id="updateDirtyWarning"' in index_html
    assert 'id="skillsAdvisoryCard"' in index_html

    # app.js functions
    assert "function switchConnectionPath(" in app_js
    assert "function checkSystemUpdates(" in app_js
    assert "function applySystemUpdate(" in app_js
    assert "function openSoftwareUpdateModal(" in app_js
    assert "function closeSoftwareUpdateModal(" in app_js
    assert "function copySkillUpgradePrompt(" in app_js


def test_dashboard_ps1_story9_tunnel_default():
    dashboard = Path(__file__).resolve().parent.parent / "dashboard.ps1"
    content = dashboard.read_text(encoding="utf-8")
    assert "[1] Public Tunnel" in content
    assert 'PM_QA_COCKPIT_TUNNEL_URL = ""' in content


def test_no_browser_native_dialogs_in_frontend():
    """Story 11: Assert zero occurrences of alert(), confirm(), prompt() across frontend assets."""
    static_dir = Path(__file__).resolve().parent.parent / "cockpit" / "static"
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")

    dialog_pattern = re.compile(r"\b(alert|confirm|prompt)\s*\(")

    matches_js = dialog_pattern.findall(app_js)
    assert matches_js == [], f"Found forbidden browser-native dialogs in app.js: {matches_js}"

    matches_html = dialog_pattern.findall(index_html)
    assert matches_html == [], f"Found forbidden browser-native dialogs in index.html: {matches_html}"

    # Verify postMessage toast bridge exists
    assert "window.addEventListener('message'" in app_js
    assert "pmqa:toast" in app_js


def test_static_assets_story12_recessed_trays_and_elevated_cards():
    """Story 12: Assert recessed tray styling and elevated cards across secondary views."""
    static_dir = Path(__file__).resolve().parent.parent / "cockpit" / "static"
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    style_css = (static_dir / "style.css").read_text(encoding="utf-8")

    # 1. Trays in HTML
    assert "lg:col-span-4 bg-[var(--tray-bg)] p-4 rounded-2xl border border-[var(--tray-border)]" in index_html
    assert "lg:col-span-7 bg-[var(--tray-bg)] p-5 rounded-2xl border border-[var(--tray-border)]" in index_html
    assert "border-b border-[var(--tray-header-border)]" in index_html

    # 2. Elevated card hierarchy in app.js
    assert "shadow-[var(--card-shadow)]" in app_js
    assert "ring-2 ring-amber-500/25" in app_js
    assert "bg-[var(--card-bg)] border border-[var(--card-border)] rounded-xl shadow-[var(--card-shadow)]" in app_js

    # 3. CSS variables and hover lift in style.css
    assert "--tray-bg:" in style_css
    assert "--tray-border:" in style_css
    assert "--tray-header-border:" in style_css
    assert "[data-qa-card-id]:hover" in style_css
    assert "translateY(-1.5px)" in style_css


def test_server_sends_connection_close_header(server):
    """Story 13: Verify Connection: close header is sent to prevent browser connection pool exhaustion."""
    base, srv = server
    with urllib.request.urlopen(f"{base}/api/server-info") as r:
        assert r.headers.get("Connection") == "close"

    with urllib.request.urlopen(f"{base}/") as r:
        assert r.headers.get("Connection") == "close"


def test_static_assets_contain_story13_instant_qr_and_connection_hygiene():
    """Story 13: Verify instant QR helper, server-info caching, and Phosphor copy feedback."""
    static_dir = Path(__file__).resolve().parent.parent / "cockpit" / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")

    assert "function renderQrCodeGraphic(" in app_js
    assert "function fetchServerInfo(" in app_js
    assert "function updateMobileBadges(" in app_js
    assert "serverInfoTime" in app_js
    assert "generateQrSvg" in app_js
    assert "ph ph-check text-emerald-500" in app_js
    assert "Mobile URL copied to clipboard" in app_js


def test_building_cards_cache_avoids_redundant_disk_reads(tmp_path):
    """Story 14: Verify count_building_cards caches results and invalidation works properly."""
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir(parents=True)
    c1 = cards_dir / "TEST-1.md"
    c1.write_text("---\nstatus: in-progress\n---\n# Test", encoding="utf-8")

    import server as srv

    project = {"docs_path": str(tmp_path)}
    srv.invalidate_building_count_cache(tmp_path)
    count1 = srv.count_building_cards(project)
    assert count1 == 1

    # Add second in-progress card without invalidation
    c2 = cards_dir / "TEST-2.md"
    c2.write_text("---\nstatus: in-progress\n---\n# Test 2", encoding="utf-8")

    # Cached value should still be 1
    count_cached = srv.count_building_cards(project)
    assert count_cached == 1

    # Invalidate cache -> should now be 2
    srv.invalidate_building_count_cache(tmp_path)
    count_fresh = srv.count_building_cards(project)
    assert count_fresh == 2


def test_project_select_returns_complete_view(server, legacy_project):
    """Story 14: Verify POST /api/projects/select returns complete project payload for zero-lag switching."""
    base, srv = server
    projects.upsert_project(projects.build_project("Legacy", legacy_project, prefix="MN"))
    req = urllib.request.Request(
        f"{base}/api/projects/select",
        data=json.dumps({"slug": "legacy"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        assert r.status == 200
        payload = json.loads(r.read().decode("utf-8"))
        assert payload.get("success") is True
        assert "current" in payload
        assert "projects" in payload
        assert isinstance(payload["projects"], list)


def test_static_assets_contain_story14_lazy_loading_and_abort_hygiene():
    """Story 14: Verify view-scoped data fetchers, non-overlapping polling, and in-flight abort in app.js."""
    static_dir = Path(__file__).resolve().parent.parent / "cockpit" / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")

    # View-scoped fetchers
    assert "function fetchBoardData(" in app_js
    assert "function fetchQaData(" in app_js
    assert "function fetchRoadmapData(" in app_js
    assert "function fetchActivityData(" in app_js
    assert "function fetchActiveSprintData(" in app_js
    assert "function fetchCurrentViewData(" in app_js

    # Non-overlapping chained polling
    assert "function scheduleNextPoll(" in app_js
    assert "currentFetchAbortController" in app_js
    assert "renderLoadingSkeletons(" in app_js


def test_git_sync_status_endpoint(server):
    """Story 15: Verify GET /api/git/sync-status returns branch, ahead/behind, current SHA."""
    base, srv = server
    status, body = get(f"{base}/api/git/sync-status")
    assert status == 200
    res = json.loads(body)
    assert res.get("success") is True
    assert "branch" in res
    assert "ahead_count" in res
    assert "behind_count" in res
    assert "current_sha" in res


def test_git_commit_endpoint(server, monkeypatch):
    """Story 15: Verify POST /api/git/commit validates message and executes commit."""
    base, srv = server
    import server as srv_mod

    # 1. Empty message rejection
    status, res = post(f"{base}/api/git/commit", {"message": ""})
    assert status == 400
    assert "error" in res

    # 2. Successful commit
    monkeypatch.setattr(
        srv_mod,
        "git_commit_changes",
        lambda msg, files=None, code_dir=None: (
            True,
            {"success": True, "sha": "1234567890abcdef", "message": msg, "output": "[main 1234567] test commit"},
        ),
    )
    status, res = post(f"{base}/api/git/commit", {"message": "feat: test commit", "files": ["test.txt"]})
    assert status == 200
    assert res.get("success") is True
    assert res.get("sha") == "1234567890abcdef"


def test_git_push_and_pull_endpoints(server, monkeypatch):
    """Story 15: Verify POST /api/git/push and POST /api/git/pull."""
    base, srv = server
    import server as srv_mod

    # Push
    monkeypatch.setattr(
        srv_mod,
        "git_push_branch",
        lambda code_dir=None: (True, {"success": True, "output": "Everything up-to-date"}),
    )
    status, res = post(f"{base}/api/git/push", {})
    assert status == 200
    assert res.get("success") is True

    # Pull
    monkeypatch.setattr(
        srv_mod,
        "git_pull_branch",
        lambda code_dir=None: (True, {"success": True, "output": "Already up to date."}),
    )
    status, res = post(f"{base}/api/git/pull", {})
    assert status == 200
    assert res.get("success") is True


def test_static_assets_contain_story15_features():
    """Story 15: Verify status badge classes, QA station bugfix, card-driven roadmap and git UI."""
    repo_root = Path(__file__).resolve().parent.parent
    static_dir = repo_root / "cockpit" / "static"
    style_css = (static_dir / "style.css").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    dashboard_ps1 = (repo_root / "dashboard.ps1").read_text(encoding="utf-8")
    pm_orchestrate = (repo_root / "skills" / "pm-orchestrate" / "SKILL.md").read_text(encoding="utf-8")

    # 1. High contrast status badge classes in style.css
    assert ".badge-status-building" in style_css
    assert ".badge-status-qa" in style_css
    assert ".badge-status-complete" in style_css
    assert ".badge-status-in-progress" in style_css
    assert "#b45309" in style_css
    assert "#fcd34d" in style_css

    # 2. QA Station crash fix in app.js
    assert "renderSprintAuditTable()" in app_js
    assert "renderSprintQualityAudit" not in app_js

    # 3. Card-driven story status in app.js
    assert "function getStoryStatusInfo(" in app_js
    assert "badge-status-complete" in app_js
    assert "badge-status-qa" in app_js

    # 4. In-App Git UI in index.html & app.js
    assert 'id="activityCommitBtn"' in index_html
    assert 'id="gitSyncBar"' in index_html
    assert 'id="gitSyncBranch"' in index_html
    assert 'id="gitPushBtn"' in index_html
    assert 'id="gitPullBtn"' in index_html
    assert 'id="gitCommitModal"' in index_html
    assert 'id="gitConfirmModal"' in index_html
    assert "function openCommitModal(" in app_js
    assert "function submitGitCommit(" in app_js
    assert "function promptGitPush(" in app_js
    assert "function promptGitPull(" in app_js

    # 5. Launcher default option yellow & gray
    assert '[1] Public Tunnel (Cloudflare Quick Tunnel with secure session token) [default]" -ForegroundColor Yellow' in dashboard_ps1
    assert '[2] Mobile Wi-Fi (LAN access for phone on same Wi-Fi, keep-awake)" -ForegroundColor Gray' in dashboard_ps1

    # 6. Orchestration alignment
    assert "seed the card at `story-backlog` (the Ready to Build lane)" in pm_orchestrate









