"""Unit tests for Tier 3 RBAC policy engine and Slack/Jira/Notion integrations."""
import os
from unittest.mock import patch, MagicMock
import pytest
from app import app, db
from rbac import add_policy, sync_user_roles, get_user_allowed_roles
from integrations import (
    send_slack_handover,
    sync_jira_tasks,
    sync_notion_tasks,
    run_all_integrations,
    get_integrations_status,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"
    with app.test_client() as c:
        yield c


@patch("requests.get")
def test_team_to_role_mapping(mock_get):
    user_key = "test_rbac_user_1"
    # User is in ml-team and backend-team
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"slug": "ml-team", "organization": {"login": "default"}},
        {"slug": "backend-team", "organization": {"login": "default"}},
    ]
    mock_get.return_value = mock_resp

    roles = sync_user_roles(user_key, "github", "gho_fake_token")
    assert "ml" in roles
    assert "backend" in roles
    assert "general" in roles

    # Check persistence
    allowed = get_user_allowed_roles(user_key)
    assert "ml" in allowed
    assert "backend" in allowed


def test_role_enforcement(client):
    # Setup published handover
    with db() as c:
        hid = c.execute(
            "insert into h(source,departing,status,created,data) values(?,?,?,?,?)",
            ("demo", "Aarav", "published", "2026-11-14",
             '{"summary":{"commits":10},"items":[{"id":"i1","type":"decision","status":"approved","roles":["ml"],"title":"ML Model"}],"graph":{"nodes":[{"id":"m:ml","label":"ml","group":"ml"}],"edges":[]}}')
        ).lastrowid

    # User with only backend & general roles
    user_key = "rbac_limited_user"
    with db() as c:
        c.execute("insert or replace into user_roles(user_key, roles, updated) values(?,?,?)",
                  (user_key, "backend,general", "2026-11-14 12:00:00"))

    with client.session_transaction() as sess:
        sess["user_key"] = user_key

    # Denied access to ml view
    res = client.get(f"/api/h/{hid}/view?role=ml")
    assert res.status_code == 403
    assert "Access denied" in res.get_json()["error"]

    # Allowed access to backend view
    res = client.get(f"/api/h/{hid}/view?role=backend")
    assert res.status_code == 200


def test_anonymous_compatibility(client):
    # Anonymous user (no session) can view role views without restriction
    with db() as c:
        hid = c.execute(
            "insert into h(source,departing,status,created,data) values(?,?,?,?,?)",
            ("demo", "", "published", "2026-11-14",
             '{"summary":{"commits":5},"items":[{"id":"i1","type":"decision","status":"approved","roles":["ml"],"title":"ML Model"}],"graph":{"nodes":[{"id":"m:ml","label":"ml","group":"ml"}],"edges":[]}}')
        ).lastrowid

    res = client.get(f"/api/h/{hid}/view?role=ml")
    assert res.status_code == 200
    res = client.get(f"/api/h/{hid}/view?role=backend")
    assert res.status_code == 200


@patch("requests.post")
def test_slack_payload_shape(mock_post, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T00/B00/X00")
    monkeypatch.setenv("BASE_URL", "https://continuai.example.com")
    mock_post.return_value = MagicMock(status_code=200)

    data = {
        "departing": "Priya Nair",
        "summary": {"commits": 15},
        "items": [
            {"id": "i1", "type": "decision", "status": "approved"},
            {"id": "i2", "type": "task", "status": "approved"},
            {"id": "i3", "type": "risk", "status": "approved"},
        ]
    }
    ok, msg = send_slack_handover(42, data)
    assert ok is True
    assert mock_post.called

    sent_payload = mock_post.call_args[1]["json"]
    blocks = sent_payload["blocks"]
    assert any("Priya Nair" in str(b) for b in blocks)
    assert any("https://continuai.example.com/?h=42&role=lead" in str(b) for b in blocks)


@patch("requests.post")
def test_jira_payload_shape_and_no_duplicates(mock_post, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://testorg.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "lead@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake_jira_token")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "PROJ")

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"key": "PROJ-101"}
    mock_post.return_value = mock_resp

    data = {
        "items": [
            {"id": "i1", "type": "task", "status": "approved", "title": "Cache features in parquet",
             "why": "Speed up retraining", "author": "Aarav", "role": "ml"}
        ]
    }

    # 1. First run creates issue
    ok, msg = sync_jira_tasks(10, data)
    assert ok is True
    assert data["items"][0]["jira_key"] == "PROJ-101"
    assert mock_post.call_count == 1

    payload = mock_post.call_args[1]["json"]
    fields = payload["fields"]
    assert fields["project"]["key"] == "PROJ"
    assert "Cache features in parquet" in fields["summary"]
    assert "continuai" in fields["labels"]
    assert "handover-10" in fields["labels"]

    # 2. Rerun should not post duplicate ticket
    mock_post.reset_mock()
    ok2, msg2 = sync_jira_tasks(10, data)
    assert ok2 is True
    assert mock_post.call_count == 0  # No duplicate request made


@patch("requests.post")
def test_notion_payload_shape(mock_post, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "secret_notion_token")
    monkeypatch.setenv("NOTION_DATABASE_ID", "fake_notion_db_id")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "page_notion_999"}
    mock_post.return_value = mock_resp

    data = {
        "items": [
            {"id": "i1", "type": "task", "status": "approved", "title": "Setup CI pipeline"}
        ]
    }
    ok, msg = sync_notion_tasks(5, data)
    assert ok is True
    assert data["items"][0]["notion_page_id"] == "page_notion_999"
    assert mock_post.called

    sent_body = mock_post.call_args[1]["json"]
    assert sent_body["parent"]["database_id"] == "fake_notion_db_id"


def test_dry_run_mode(monkeypatch):
    monkeypatch.setenv("INTEGRATIONS_DRY_RUN", "1")

    data = {
        "departing": "Rohan",
        "items": [
            {"id": "i1", "type": "task", "status": "approved", "title": "WIP mobile layout"}
        ]
    }
    # Slack in dry run
    ok_s, msg_s = send_slack_handover(99, data)
    assert ok_s is True
    assert "dry-run" in msg_s

    # Jira in dry run
    ok_j, msg_j = sync_jira_tasks(99, data)
    assert ok_j is True
    assert "dry-run" in msg_j
    assert data["items"][0]["jira_key"].startswith("PROJ-99")

    # Notion in dry run
    ok_n, msg_n = sync_notion_tasks(99, data)
    assert ok_n is True
    assert "dry-run" in msg_n
    assert "mock-page" in data["items"][0]["notion_page_id"]


@patch("requests.post")
def test_integration_failure_does_not_break_publish(mock_post, client, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/fail")
    mock_post.side_effect = Exception("Slack API connection timeout")

    with db() as c:
        hid = c.execute(
            "insert into h(source,departing,status,created,data) values(?,?,?,?,?)",
            ("demo", "Priya", "draft", "2026-11-14",
             '{"summary":{"commits":5},"items":[{"id":"i1","type":"decision","status":"approved","title":"Decision 1","roles":["general"]}],"graph":{"nodes":[],"edges":[]}}')
        ).lastrowid

    # Publish should succeed even if integration fails
    res = client.post(f"/api/h/{hid}/publish", json={"actor": "Lead"})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    # Check status changed to published
    with db() as c:
        h = c.execute("select status from h where id=?", (hid,)).fetchone()
        assert h["status"] == "published"
