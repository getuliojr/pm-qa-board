"""Extra card tags: registry declarations, card exposure, public API shape, CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_server import get, post, server  # noqa: F401  (server fixture)

import projects
from parser import PMParser, declared_extra_tags

CLI = Path(__file__).resolve().parent.parent / "board" / "cli.py"

TICKET = {"key": "ticket", "label": "Ticket", "link": "https://tracker.example.com/browse/{value}", "filter": True}
ITERATION = {"key": "iteration", "label": "Iteration", "filter": True}


def card_text(card_id: str, **extra) -> str:
    fm = "".join(f"{k}: {v}\n" for k, v in extra.items())
    return f'---\nid: {card_id}\nstory: "1"\ntype: feature\nstatus: story-backlog\ncreated: 2026-10-02\n{fm}---\n\n# {card_id} - Tagged\n\n## Definition of Done / Acceptance Criteria\n\n- [ ] item\n'


def make_project(external_project, tags, cards):
    code, docs = external_project
    project = projects.build_project("Story App", code, prefix="APP", docs_path=docs, extra_tags=tags)
    projects.ensure_docs_root(project)
    for card_id, fm in cards.items():
        (docs / "cards" / f"{card_id} - Tagged.md").write_text(card_text(card_id, **fm), encoding="utf-8")
    (docs / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    return project


def parse(project, card_id):
    return {c["mn_id"]: c for c in PMParser(project=project).get_all_cards()}[card_id]


def test_declared_key_exposes_label_value_link_filter(external_project):
    project = make_project(external_project, [TICKET, ITERATION], {"APP-1": {"ticket": "EXAMPLE-123", "iteration": 7}})
    assert parse(project, "APP-1")["extra_tags"] == [
        {"key": "ticket", "label": "Ticket", "value": "EXAMPLE-123", "link": "https://tracker.example.com/browse/EXAMPLE-123", "filter": True},
        {"key": "iteration", "label": "Iteration", "value": "7", "link": None, "filter": True},
    ]


def test_missing_empty_and_undeclared_values_are_skipped(external_project):
    project = make_project(external_project, [TICKET, ITERATION], {"APP-1": {"ticket": "", "other": "x"}, "APP-2": {}})
    assert parse(project, "APP-1")["extra_tags"] == []
    assert parse(project, "APP-2")["extra_tags"] == []


def test_no_declaration_gives_empty_list(external_project):
    project = make_project(external_project, None, {"APP-1": {"ticket": "EXAMPLE-1"}})
    assert "extra_tags" not in project
    assert parse(project, "APP-1")["extra_tags"] == []


def test_malformed_declarations_are_ignored():
    assert declared_extra_tags({"extra_tags": "ticket"}) == []
    assert declared_extra_tags({"extra_tags": {"key": "ticket"}}) == []
    bad = ["x", 3, None, {"key": 5}, {"key": "  "}, {"label": "no key"}, {"key": " k ", "label": 4, "link": 5, "filter": "yes"}]
    assert declared_extra_tags({"extra_tags": bad}) == [{"key": "k", "label": "k", "link": None, "filter": False}]


def test_non_http_template_has_no_link(external_project):
    tag = {"key": "ticket", "link": "javascript:alert({value})"}
    project = make_project(external_project, [tag], {"APP-1": {"ticket": "A1"}})
    assert parse(project, "APP-1")["extra_tags"][0]["link"] is None


def test_value_is_url_quoted_in_link(external_project):
    project = make_project(external_project, [TICKET], {"APP-1": {"ticket": "EX<b>-9&x"}})
    tag = parse(project, "APP-1")["extra_tags"][0]
    assert tag["value"] == "EX<b>-9&x"
    assert tag["link"] == "https://tracker.example.com/browse/EX%3Cb%3E-9%26x"


def test_bool_and_int_values_become_strings(external_project):
    project = make_project(external_project, [{"key": "flag"}, {"key": "n"}], {"APP-1": {"flag": "true", "n": 12}})
    assert [t["value"] for t in parse(project, "APP-1")["extra_tags"]] == ["True", "12"]


def test_first_duplicate_key_wins():
    tags = declared_extra_tags({"extra_tags": [{"key": "a", "label": "First"}, {"key": " a ", "label": "Second"}]})
    assert [(t["key"], t["label"]) for t in tags] == [("a", "First")]


def test_prototype_like_keys_work(external_project):
    project = make_project(
        external_project, [{"key": "__proto__"}, {"key": "constructor"}], {"APP-1": {"__proto__": "p", "constructor": "c"}}
    )
    assert [(t["key"], t["value"]) for t in parse(project, "APP-1")["extra_tags"]] == [("__proto__", "p"), ("constructor", "c")]


def test_server_cards_and_public_projects(server, external_project):  # noqa: F811
    base, srv = server
    code, docs = external_project
    project = make_project(external_project, [TICKET, ITERATION], {"APP-1": {"ticket": "EXAMPLE-1"}, "APP-2": {}})
    projects.upsert_project(project, activate=False)
    status, res = post(f"{base}/api/projects/select", {"slug": "story-app"})
    assert status == 200
    assert "tracker.example.com" not in json.dumps(res)

    cards = {c["mn_id"]: c for c in json.loads(get(f"{base}/api/cards")[1])["cards"]}
    assert cards["APP-1"]["extra_tags"][0]["link"] == "https://tracker.example.com/browse/EXAMPLE-1"
    assert cards["APP-2"]["extra_tags"] == []

    view = json.loads(get(f"{base}/api/projects")[1])
    expected = [{"key": "ticket", "label": "Ticket", "filter": True}, {"key": "iteration", "label": "Iteration", "filter": True}]
    assert view["current"]["extra_tags"] == expected
    listed = [p for p in view["projects"] if p["slug"] == "story-app"]
    assert listed and listed[0]["extra_tags"] == expected
    assert "tracker.example.com" not in json.dumps(view)
    assert "extra_tags" in projects.load_registry()["projects"]["story-app"]
    assert projects.load_registry()["projects"]["story-app"]["extra_tags"][0]["link"]


def test_configure_preserves_extra_tags(server, external_project):  # noqa: F811
    base, _ = server
    code, docs = external_project
    project = make_project(external_project, [TICKET], {})
    projects.upsert_project(project, activate=False)
    status, res = post(
        f"{base}/api/projects/configure",
        {"name": "Story App", "code_path": str(code), "docs_path": str(docs), "prefix": "APP"},
    )
    assert status == 200 and res["success"], res
    assert res["project"]["extra_tags"] == [{"key": "ticket", "label": "Ticket", "filter": True}]
    assert "tracker.example.com" not in json.dumps(res)
    assert projects.load_registry()["projects"]["story-app"]["extra_tags"] == [TICKET]


def test_declarations_apply_on_reselect_only(server, external_project):  # noqa: F811
    base, srv = server
    project = make_project(external_project, [TICKET], {"APP-1": {"ticket": "EXAMPLE-1", "iteration": 4}})
    projects.upsert_project(project, activate=False)
    post(f"{base}/api/projects/select", {"slug": "story-app"})

    reg = projects.load_registry()
    reg["projects"]["story-app"]["extra_tags"] = [ITERATION]
    projects.save_registry(reg)
    srv.STATE.parser.invalidate_cache()
    view = json.loads(get(f"{base}/api/projects")[1])
    assert [t["key"] for t in view["current"]["extra_tags"]] == ["ticket"]

    post(f"{base}/api/projects/select", {"slug": "story-app"})
    srv.STATE.parser.invalidate_cache()
    view = json.loads(get(f"{base}/api/projects")[1])
    assert [t["key"] for t in view["current"]["extra_tags"]] == ["iteration"]
    cards = {c["mn_id"]: c for c in json.loads(get(f"{base}/api/cards")[1])["cards"]}
    assert [t["key"] for t in cards["APP-1"]["extra_tags"]] == ["iteration"]

    reg = projects.load_registry()
    del reg["projects"]["story-app"]["extra_tags"]
    projects.save_registry(reg)
    post(f"{base}/api/projects/select", {"slug": "story-app"})
    srv.STATE.parser.invalidate_cache()
    cards = {c["mn_id"]: c for c in json.loads(get(f"{base}/api/cards")[1])["cards"]}
    assert cards["APP-1"]["extra_tags"] == []
    assert "extra_tags" not in json.loads(get(f"{base}/api/projects")[1])["current"]


def run_cli(*args):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, encoding="utf-8")


@pytest.mark.parametrize("tags", [[TICKET, ITERATION], None])
def test_cli_tags(external_project, tags):
    project = make_project(external_project, tags, {})
    projects.upsert_project(project, activate=True)
    out = run_cli("--project", "story-app", "tags")
    assert out.returncode == 0, out.stderr
    if tags:
        lines = out.stdout.splitlines()
        assert lines[0].split() == ["Key", "Label", "Filter", "Link", "template"]
        assert "ticket" in lines[1] and "yes" in lines[1] and "https://tracker.example.com/browse/{value}" in lines[1]
        assert lines[2].split() == ["iteration", "Iteration", "yes", "-"]
    else:
        assert out.stdout.strip() == "No extra tags declared for this project."
