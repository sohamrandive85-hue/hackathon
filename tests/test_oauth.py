"""Unit tests for Tier 2 OAuth 2.0, token encryption, and authenticated ingestion."""
import base64
import os
import time
from unittest.mock import patch, MagicMock
import pytest
from app import app
from token_store import (
    encrypt_token,
    decrypt_token,
    save_connection,
    get_connection,
    get_token_for_repo,
    set_connection_status,
)
from analyzer import load_commits, _git


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"
    with app.test_client() as c:
        yield c


def test_token_encryption_roundtrip():
    raw_secret = "gho_test_token_1234567890abcdef"
    enc = encrypt_token(raw_secret)
    assert enc != raw_secret
    assert "gho_" not in enc
    dec = decrypt_token(enc)
    assert dec == raw_secret


def test_state_mismatch_rejected(client, monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "dummy_client_id")
    with client.session_transaction() as sess:
        sess["user_key"] = "test_user_key"
        sess["oauth_state_github"] = "legitimate_state_123"

    # Send mismatched state
    res = client.get("/auth/github/callback?state=attacker_state_456&code=test_code")
    assert res.status_code == 400
    data = res.get_json()
    assert "State mismatch" in data.get("error", "")


def test_api_me_leaks_no_tokens(client):
    # Setup test connection
    user_key = "user_safe_test"
    secret_token = "gho_super_secret_token_never_leak"
    save_connection(user_key, "github", "octocat", "repo read:org", secret_token)

    with client.session_transaction() as sess:
        sess["user_key"] = user_key

    res = client.get("/api/me")
    assert res.status_code == 200
    data = res.get_json()
    assert data["logged_in"] is True
    assert data["account_login"] == "octocat"
    assert data["provider"] == "github"

    # Assert no token fields exist anywhere in JSON
    json_str = str(data)
    assert "secret_token" not in json_str
    assert "access_token" not in json_str
    assert "gho_" not in json_str


@patch("requests.get")
def test_api_repos_leaks_no_tokens(mock_get, client):
    user_key = "repos_test_user"
    secret_token = "gho_repos_secret_token"
    save_connection(user_key, "github", "octocat", "repo read:org", secret_token)

    with client.session_transaction() as sess:
        sess["user_key"] = user_key

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"full_name": "octocat/Hello-World", "html_url": "https://github.com/octocat/Hello-World", "private": True}
    ]
    mock_get.return_value = mock_resp

    res = client.get("/api/repos")
    assert res.status_code == 200
    data = res.get_json()
    assert len(data) == 1
    assert data[0]["full_name"] == "octocat/Hello-World"
    assert data[0]["private"] is True

    # Confirm token is never present in response
    assert "token" not in str(data)
    assert "gho_" not in str(data)


@patch("auth.refresh_oauth_token")
def test_refresh_near_expiry(mock_refresh):
    user_key = "exp_user"
    old_acc = "gho_old_token"
    ref_tok = "ghr_refresh_token"
    new_acc = "gho_new_refreshed_token"

    mock_refresh.return_value = (new_acc, ref_tok, 3600)

    # Save connection expiring in 30 seconds (<= 60s trigger)
    save_connection(user_key, "github", "octocat", "repo read:org", old_acc, refresh_token=ref_tok, expires_in=30)

    token, prov = get_token_for_repo("https://github.com/octocat/repo", user_key=user_key)
    assert token == new_acc
    assert prov == "github"
    mock_refresh.assert_called_once()


@patch("requests.get")
def test_401_sets_needs_reconnect(mock_get, client):
    user_key = "user_401_test"
    save_connection(user_key, "github", "octocat", "repo", "gho_invalid_token")

    with client.session_transaction() as sess:
        sess["user_key"] = user_key

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_get.return_value = mock_resp

    res = client.get("/api/repos")
    assert res.status_code == 200
    conn = get_connection(user_key, "github")
    assert conn["status"] == "needs_reconnect"


@patch("subprocess.run")
def test_git_env_has_header_and_argv_has_no_token(mock_run):
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "\x1dsha1\x1fauthor\x1f2026-11-14\x1fSubject\x1fBody\x1efile.py"
    mock_run.return_value = mock_res

    fake_token = "ghp_secret_access_token_12345"
    load_commits("https://github.com/owner/private-repo", token=fake_token, provider="github")

    assert mock_run.called
    clone_calls = [c for c in mock_run.call_args_list if "clone" in c[0][0]]
    assert len(clone_calls) == 1
    args, kwargs = clone_calls[0]
    cmd = args[0]
    # Verify argv contains NO token
    assert not any(fake_token in str(arg) for arg in cmd)

    # Verify git config extraHeader is present in subprocess env
    env = kwargs.get("env", {})
    assert env.get("GIT_CONFIG_COUNT") == "1"
    assert env.get("GIT_CONFIG_KEY_0") == "http.extraHeader"
    header_val = env.get("GIT_CONFIG_VALUE_0", "")
    assert "Authorization: Basic" in header_val
    # Decode and verify header content
    b64_part = header_val.split("Basic ")[-1]
    decoded = base64.b64decode(b64_part.encode()).decode()
    assert decoded == f"x-access-token:{fake_token}"


def test_git_error_scrubs_tokens():
    with patch("subprocess.run") as mock_run:
        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_res.stderr = "fatal: Authentication failed for Authorization: Basic secret_token_xyz_12345678901234567890"
        mock_run.return_value = mock_res

        with pytest.raises(ValueError) as exc:
            _git(["clone", "dummy"])
        err_msg = str(exc.value)
        assert "secret_token" not in err_msg
        assert "[REDACTED]" in err_msg
