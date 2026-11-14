"""Role-Based Access Control (RBAC) policy engine.
Maps GitHub teams and GitLab groups to Continuum roles (lead, ml, backend, frontend, docs, infra, general).
"""
import os
import sqlite3
import time
from flask import Blueprint, jsonify, request, session
import requests
from token_store import get_connection, get_db

rbac_bp = Blueprint("rbac", __name__)

VALID_ROLES = {"lead", "ml", "backend", "frontend", "docs", "infra", "general"}


def init_rbac():
    with get_db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS role_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            org TEXT NOT NULL,
            team_slug TEXT NOT NULL,
            role TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_roles (
            user_key TEXT PRIMARY KEY,
            roles TEXT NOT NULL,
            updated TEXT NOT NULL
        );
        """)
        # Seed default demo policies if empty
        row = c.execute("SELECT count(*) as cnt FROM role_policies").fetchone()
        if row and row["cnt"] == 0:
            c.executescript("""
            INSERT INTO role_policies (provider, org, team_slug, role) VALUES
            ('github', 'default', 'leads', 'lead'),
            ('github', 'default', 'ml-team', 'ml'),
            ('github', 'default', 'backend-team', 'backend'),
            ('github', 'default', 'frontend-team', 'frontend'),
            ('github', 'default', 'infra-team', 'infra');
            """)


init_rbac()


def get_policies() -> list[dict]:
    with get_db() as c:
        rows = c.execute("SELECT id, provider, org, team_slug, role FROM role_policies ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


def add_policy(provider: str, org: str, team_slug: str, role: str) -> int:
    clean_role = role.lower().strip()
    if clean_role not in VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Allowed: {', '.join(sorted(VALID_ROLES))}")
    with get_db() as c:
        pid = c.execute("INSERT INTO role_policies (provider, org, team_slug, role) VALUES (?, ?, ?, ?)",
                        (provider.lower().strip(), org.strip(), team_slug.strip().lower(), clean_role)).lastrowid
    return pid


def sync_user_roles(user_key: str, provider: str, access_token: str) -> list[str]:
    """Fetch user's teams/groups from provider API and map to allowed roles using role_policies."""
    if not access_token:
        return ["general"]

    teams = set()
    auth_header = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if provider == "github":
        auth_header["Authorization"] = f"token {access_token}"
        try:
            r = requests.get("https://api.github.com/user/teams", headers=auth_header, timeout=10)
            if r.status_code == 200:
                for t in r.json():
                    slug = t.get("slug", "").lower()
                    org = (t.get("organization") or {}).get("login", "").lower()
                    teams.add(slug)
                    if org:
                        teams.add(f"{org}/{slug}")
        except Exception:
            pass
    elif provider == "gitlab":
        gitlab_base = os.environ.get("GITLAB_BASE", "https://gitlab.com").rstrip("/")
        try:
            r = requests.get(f"{gitlab_base}/api/v4/groups?min_access_level=10", headers=auth_header, timeout=10)
            if r.status_code == 200:
                for g in r.json():
                    path = g.get("path", "").lower()
                    full = g.get("full_path", "").lower()
                    teams.add(path)
                    if full:
                        teams.add(full)
        except Exception:
            pass

    policies = get_policies()
    allowed = {"general"}  # All signed-in members get general role
    for p in policies:
        if p["provider"].lower() == provider.lower():
            target_slug = p["team_slug"].lower()
            target_org = p["org"].lower()
            if target_slug in teams or (target_org != "default" and f"{target_org}/{target_slug}" in teams):
                allowed.add(p["role"])

    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    roles_str = ",".join(sorted(allowed))
    with get_db() as c:
        c.execute("""
            INSERT INTO user_roles (user_key, roles, updated) VALUES (?, ?, ?)
            ON CONFLICT(user_key) DO UPDATE SET roles=excluded.roles, updated=excluded.updated
        """, (user_key, roles_str, now_str))

    return sorted(list(allowed))


def get_user_allowed_roles(user_key: str) -> list[str] | None:
    """Return allowed roles for user_key. If anonymous (user_key is None), returns None (open access)."""
    if not user_key:
        return None
    with get_db() as c:
        row = c.execute("SELECT roles FROM user_roles WHERE user_key=?", (user_key,)).fetchone()
    if row and row["roles"]:
        return [r.strip() for r in row["roles"].split(",") if r.strip()]

    # If connection exists but user_roles row not yet populated, return default
    conn = get_connection(user_key)
    if conn and conn.get("status") == "active":
        return ["general"]
    return None


@rbac_bp.get("/api/rbac/policies")
def api_get_policies():
    return jsonify(get_policies())


@rbac_bp.post("/api/rbac/policies")
def api_add_policy():
    j = request.get_json(force=True)
    prov = j.get("provider", "github")
    org = j.get("org", "default")
    team = j.get("team_slug", "")
    role = j.get("role", "")
    if not team or not role:
        return jsonify(error="team_slug and role are required."), 400
    try:
        pid = add_policy(prov, org, team, role)
        return jsonify(id=pid, provider=prov, org=org, team_slug=team, role=role)
    except ValueError as e:
        return jsonify(error=str(e)), 400


@rbac_bp.post("/api/rbac/sync")
def api_rbac_sync():
    user_key = session.get("user_key")
    if not user_key:
        return jsonify(error="Not logged in."), 401
    conn = get_connection(user_key)
    if not conn or not conn.get("access_token"):
        return jsonify(error="No active provider connection found."), 400
    roles = sync_user_roles(user_key, conn["provider"], conn["access_token"])
    return jsonify(roles=roles)
