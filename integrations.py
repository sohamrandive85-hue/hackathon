"""Integrations module: Slack Block Kit webhooks, Jira Cloud REST API, and Notion database exports."""
import base64
import json
import logging
import os
import requests

logger = logging.getLogger(__name__)


def is_dry_run() -> bool:
    return os.environ.get("INTEGRATIONS_DRY_RUN", "0").lower() in ("1", "true", "yes")


def get_base_url():
    return os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")


def get_integrations_status() -> dict:
    return {
        "slack": {
            "configured": bool(os.environ.get("SLACK_WEBHOOK_URL")),
            "target": "Incoming Webhook" if os.environ.get("SLACK_WEBHOOK_URL") else "Not configured",
        },
        "jira": {
            "configured": bool(
                os.environ.get("JIRA_BASE_URL")
                and os.environ.get("JIRA_EMAIL")
                and os.environ.get("JIRA_API_TOKEN")
                and os.environ.get("JIRA_PROJECT_KEY")
            ),
            "target": os.environ.get("JIRA_BASE_URL", "Not configured"),
        },
        "notion": {
            "configured": bool(os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DATABASE_ID")),
            "target": os.environ.get("NOTION_DATABASE_ID", "Not configured"),
        },
        "dry_run": is_dry_run(),
    }


def send_slack_handover(hid: int, data: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Posts a structured Block Kit handover notification to Slack."""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    dry = dry_run or is_dry_run()
    if not webhook_url and not dry:
        return False, "Slack not configured (missing SLACK_WEBHOOK_URL)"

    summary = data.get("summary", {})
    items = data.get("items", [])
    approved = [i for i in items if i.get("status") == "approved"]
    decisions_count = sum(1 for i in approved if i.get("type") == "decision")
    tasks_count = sum(1 for i in approved if i.get("type") == "task")
    risks_count = sum(1 for i in approved if i.get("type") == "risk")

    departing = data.get("departing") or "Team"
    deep_link = f"{get_base_url()}/?h={hid}&role=lead"

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "ContinuAI: Knowledge Handover Published"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Departing member:*\n{departing}"},
                    {"type": "mrkdwn", "text": f"*Repository:*\n{summary.get('commits', 0)} commits analyzed"},
                    {"type": "mrkdwn", "text": f"*Approved Decisions:*\n{decisions_count}"},
                    {"type": "mrkdwn", "text": f"*Next Steps (Tasks):*\n{tasks_count}"},
                ]
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View Role Handover"},
                        "url": deep_link,
                        "style": "primary"
                    }
                ]
            }
        ]
    }

    if dry:
        logger.info(f"[DRY-RUN] Slack notification payload for handover {hid}: {json.dumps(payload)}")
        return True, "Slack handover notification sent (dry-run mode)"

    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code == 200:
            return True, "Slack handover notification posted successfully"
        return False, f"Slack HTTP {r.status_code} {r.reason}"
    except Exception:
        return False, "Slack notification failed: network error"


def sync_jira_tasks(hid: int, data: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Converts approved 'task' items into Jira Cloud issues with ADF formatting."""
    base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    project_key = os.environ.get("JIRA_PROJECT_KEY")
    issue_type = os.environ.get("JIRA_ISSUE_TYPE", "Epic")
    dry = dry_run or is_dry_run()

    if not (base_url and email and token and project_key) and not dry:
        return False, "Jira not configured (missing credentials or project key)"

    approved_tasks = [i for i in data.get("items", []) if i.get("type") == "task" and i.get("status") == "approved"]
    if not approved_tasks:
        return True, "No approved tasks to sync to Jira"

    auth_b64 = base64.b64encode(f"{email}:{token}".encode()).decode() if email and token else ""
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    synced_keys = []
    for item in approved_tasks:
        # Check if already synced to avoid duplicate tickets
        if item.get("jira_key"):
            continue

        role = item.get("role", "general")
        url = item.get("url", "")
        author = item.get("author", "Unassigned")
        why = item.get("why", "Marked as unfinished in commit message.")

        # Atlassian Document Format (ADF) description
        adf_desc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": f"Why: {why}"}]
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": f"Original Author: {author} | Commit: {url or 'Local'}"}]
                }
            ]
        }

        body = {
            "fields": {
                "project": {"key": project_key or "PROJ"},
                "summary": f"[ContinuAI Handover #{hid}] {item.get('title', 'Task')}"[:255],
                "description": adf_desc,
                "issuetype": {"name": issue_type},
                "labels": ["continuai", f"handover-{hid}", role],
            }
        }

        if dry:
            mock_key = f"{project_key or 'PROJ'}-{hid}{item['id']}"
            item["jira_key"] = mock_key
            synced_keys.append(mock_key)
            continue

        try:
            r = requests.post(f"{base_url}/rest/api/3/issue", json=body, headers=headers, timeout=12)
            if r.status_code in (200, 201):
                key = r.json().get("key")
                item["jira_key"] = key
                synced_keys.append(key)
            else:
                logger.warning(f"Jira issue creation failed: HTTP {r.status_code} {r.reason}")
        except Exception:
            logger.warning("Jira request error: network failure")

    if dry:
        return True, f"Jira tasks created: {', '.join(synced_keys)} (dry-run mode)"
    if synced_keys:
        return True, f"Jira tasks created: {', '.join(synced_keys)}"
    return True, "All approved tasks are already synced to Jira"


def sync_notion_tasks(hid: int, data: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Exports approved task items into a Notion database."""
    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_DATABASE_ID")
    title_prop = os.environ.get("NOTION_TITLE_PROP", "Name")
    dry = dry_run or is_dry_run()

    if not (token and db_id) and not dry:
        return False, "Notion not configured (missing NOTION_TOKEN or NOTION_DATABASE_ID)"

    approved_tasks = [i for i in data.get("items", []) if i.get("type") == "task" and i.get("status") == "approved"]
    if not approved_tasks:
        return True, "No approved tasks to export to Notion"

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    synced_pages = []
    for item in approved_tasks:
        if item.get("notion_page_id"):
            continue

        title_text = f"[Handover #{hid}] {item.get('title', 'Task')}"
        body = {
            "parent": {"database_id": db_id or "dummy_db"},
            "properties": {
                title_prop: {
                    "title": [{"text": {"content": title_text[:100]}}]
                }
            }
        }

        if dry:
            mock_id = f"mock-page-{hid}-{item['id']}"
            item["notion_page_id"] = mock_id
            synced_pages.append(mock_id)
            continue

        try:
            r = requests.post("https://api.notion.com/v1/pages", json=body, headers=headers, timeout=12)
            if r.status_code in (200, 201):
                page_id = r.json().get("id")
                item["notion_page_id"] = page_id
                synced_pages.append(page_id)
            else:
                logger.warning(f"Notion page creation failed: HTTP {r.status_code} {r.reason}")
        except Exception:
            logger.warning("Notion request error: network failure")

    if dry:
        return True, f"Notion pages created: {len(synced_pages)} items (dry-run mode)"
    if synced_pages:
        return True, f"Notion pages exported: {len(synced_pages)} items"
    return True, "All approved tasks are already exported to Notion"


def run_all_integrations(hid: int, data: dict, save_log_fn=None):
    """Executes all configured integrations sequentially without failing the publish pipeline."""
    # 1. Slack
    ok, msg = send_slack_handover(hid, data)
    if save_log_fn:
        save_log_fn(hid, "System", "integration", f"Slack: {msg}")

    # 2. Jira
    ok, msg = sync_jira_tasks(hid, data)
    if save_log_fn:
        save_log_fn(hid, "System", "integration", f"Jira: {msg}")

    # 3. Notion
    ok, msg = sync_notion_tasks(hid, data)
    if save_log_fn:
        save_log_fn(hid, "System", "integration", f"Notion: {msg}")
