"""OAuth 2.0 authentication blueprint for GitHub and GitLab."""
import os
import secrets
import urllib.parse
from flask import Blueprint, jsonify, redirect, request, session
import requests
from token_store import (
    save_connection,
    get_connection,
    disconnect_connection,
    save_repo_connection,
    set_connection_status,
)

auth_bp = Blueprint("auth", __name__)


def get_base_url():
    return os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")


def get_providers():
    gitlab_base = os.environ.get("GITLAB_BASE", "https://gitlab.com").rstrip("/")
    return {
        "github": {
            "name": "GitHub",
            "client_id": os.environ.get("GITHUB_CLIENT_ID", ""),
            "client_secret": os.environ.get("GITHUB_CLIENT_SECRET", ""),
            "auth_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "user_url": "https://api.github.com/user",
            "repos_url": "https://api.github.com/user/repos?per_page=100&sort=updated",
            "teams_url": "https://api.github.com/user/teams",
            "scope": "repo read:org",
        },
        "gitlab": {
            "name": "GitLab",
            "client_id": os.environ.get("GITLAB_CLIENT_ID", ""),
            "client_secret": os.environ.get("GITLAB_CLIENT_SECRET", ""),
            "auth_url": f"{gitlab_base}/oauth/authorize",
            "token_url": f"{gitlab_base}/oauth/token",
            "user_url": f"{gitlab_base}/api/v4/user",
            "repos_url": f"{gitlab_base}/api/v4/projects?membership=true&per_page=100&order_by=updated_at",
            "groups_url": f"{gitlab_base}/api/v4/groups?min_access_level=10",
            "scope": "read_repository read_api read_user",
        },
    }


def ensure_user_key():
    if "user_key" not in session:
        session["user_key"] = secrets.token_hex(16)
    return session["user_key"]


def refresh_oauth_token(provider: str, refresh_token: str):
    """Server-side token refresh for providers that support it."""
    providers = get_providers()
    cfg = providers.get(provider)
    if not cfg or not cfg["client_id"] or not cfg["client_secret"]:
        return None, None, None

    payload = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {"Accept": "application/json"}
    try:
        r = requests.post(cfg["token_url"], data=payload, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data.get("access_token"), data.get("refresh_token"), data.get("expires_in")
    except Exception:
        pass
    return None, None, None


@auth_bp.get("/auth/<p>/login")
def oauth_login(p):
    p = p.lower()
    providers = get_providers()
    cfg = providers.get(p)
    if not cfg or not cfg["client_id"]:
        return jsonify(error=f"{p.capitalize()} OAuth is not configured."), 400

    ensure_user_key()
    state = secrets.token_hex(16)
    session[f"oauth_state_{p}"] = state

    redirect_uri = f"{get_base_url()}/auth/{p}/callback"
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "scope": cfg["scope"],
        "state": state,
        "response_type": "code",
    }
    return redirect(f"{cfg['auth_url']}?{urllib.parse.urlencode(params)}")


@auth_bp.get("/auth/<p>/callback")
def oauth_callback(p):
    p = p.lower()
    providers = get_providers()
    cfg = providers.get(p)
    if not cfg or not cfg["client_id"]:
        return jsonify(error="Provider not configured."), 400

    user_key = ensure_user_key()
    expected_state = session.pop(f"oauth_state_{p}", None)
    state = request.args.get("state")
    if not expected_state or state != expected_state:
        return jsonify(error="State mismatch / CSRF detected."), 400

    code = request.args.get("code")
    if not code:
        return jsonify(error="Missing authorization code."), 400

    redirect_uri = f"{get_base_url()}/auth/{p}/callback"
    token_payload = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    headers = {"Accept": "application/json"}
    try:
        r = requests.post(cfg["token_url"], data=token_payload, headers=headers, timeout=20)
        if r.status_code != 200:
            return jsonify(error=f"Token exchange failed: HTTP {r.status_code} {r.reason}"), 400
        token_data = r.json()
    except Exception:
        return jsonify(error="Token exchange failed: connection error"), 400

    access_token = token_data.get("access_token")
    if not access_token:
        return jsonify(error="No access token received from provider."), 400

    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")

    # Fetch user profile
    auth_header = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if p == "github":
        auth_header["Authorization"] = f"token {access_token}"
    try:
        ur = requests.get(cfg["user_url"], headers=auth_header, timeout=15)
        if ur.status_code != 200:
            return jsonify(error=f"Failed to fetch user profile: HTTP {ur.status_code} {ur.reason}"), 400
        user_info = ur.json()
    except Exception:
        return jsonify(error="Failed to fetch user profile: connection error"), 400

    account_login = user_info.get("login") or user_info.get("username") or str(user_info.get("id"))
    conn_id = save_connection(
        user_key=user_key,
        provider=p,
        account_login=account_login,
        scopes=cfg["scope"],
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )

    # Populate initial repositories for fast selection
    try:
        rr = requests.get(cfg["repos_url"], headers=auth_header, timeout=15)
        if rr.status_code == 200:
            repos_data = rr.json()
            if isinstance(repos_data, list):
                for repo_item in repos_data:
                    full_name = repo_item.get("full_name") or repo_item.get("path_with_namespace")
                    if full_name:
                        save_repo_connection(user_key, p, full_name, conn_id)
    except Exception:
        pass

    # Synchronize RBAC policies if rbac module is active
    try:
        from rbac import sync_user_roles
        sync_user_roles(user_key, p, access_token)
    except Exception:
        pass

    return redirect("/")


@auth_bp.post("/auth/<p>/disconnect")
def oauth_disconnect(p):
    p = p.lower()
    user_key = session.get("user_key")
    if user_key:
        disconnect_connection(user_key, p)
    return jsonify(ok=True)


@auth_bp.get("/api/me")
def api_me():
    """Returns safe connection state for the current session. NEVER leaks tokens."""
    user_key = session.get("user_key")
    providers = get_providers()
    configured = [k for k, v in providers.items() if v["client_id"]]

    conn = get_connection(user_key) if user_key else None
    if not conn or conn.get("status") == "revoked":
        return jsonify({
            "logged_in": False,
            "provider": None,
            "account_login": None,
            "status": None,
            "configured_providers": configured,
        })

    return jsonify({
        "logged_in": True,
        "provider": conn.get("provider"),
        "account_login": conn.get("account_login"),
        "status": conn.get("status"),
        "configured_providers": configured,
    })


@auth_bp.get("/api/repos")
def api_repos():
    """Returns user's accessible repositories for dropdown selection without exposing tokens."""
    user_key = session.get("user_key")
    if not user_key:
        return jsonify([])

    conn = get_connection(user_key)
    if not conn or conn.get("status") != "active":
        return jsonify([])

    provider = conn.get("provider", "github")
    providers = get_providers()
    cfg = providers.get(provider)
    if not cfg:
        return jsonify([])

    token = conn.get("access_token")
    if not token:
        return jsonify([])

    auth_header = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if provider == "github":
        auth_header["Authorization"] = f"token {token}"

    try:
        r = requests.get(cfg["repos_url"], headers=auth_header, timeout=15)
        if r.status_code == 401:
            set_connection_status(conn["id"], "needs_reconnect")
            return jsonify([])
        if r.status_code != 200:
            return jsonify([])
        raw_list = r.json()
        if not isinstance(raw_list, list):
            return jsonify([])

        repos = []
        for item in raw_list:
            name = item.get("full_name") or item.get("path_with_namespace")
            if name:
                repos.append({
                    "full_name": name,
                    "url": item.get("html_url") or item.get("web_url") or f"https://{provider}.com/{name}",
                    "private": bool(item.get("private", False)),
                })
        return jsonify(repos)
    except Exception:
        return jsonify([])
