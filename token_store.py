"""Token Store: Fernet encryption and SQLite persistence for OAuth tokens and repository connections."""
import base64
import os
import sqlite3
import time
from cryptography.fernet import Fernet

# Load .env if present
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "continuai.db")

_fernet_instance = None


def get_fernet():
    global _fernet_instance
    if _fernet_instance is None:
        key = os.environ.get("TOKEN_ENCRYPTION_KEY")
        if not key:
            # Fallback for dev/test environments without explicit key
            key = Fernet.generate_key().decode()
            os.environ["TOKEN_ENCRYPTION_KEY"] = key
        if isinstance(key, str):
            key = key.encode()
        _fernet_instance = Fernet(key)
    return _fernet_instance


def encrypt_token(raw_token: str) -> str:
    if not raw_token:
        return ""
    return get_fernet().encrypt(raw_token.encode()).decode()


def decrypt_token(enc_token: str) -> str:
    if not enc_token:
        return ""
    try:
        return get_fernet().decrypt(enc_token.encode()).decode()
    except Exception:
        return ""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_token_store():
    with get_db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            account_login TEXT NOT NULL,
            scopes TEXT,
            access_token_enc TEXT NOT NULL,
            refresh_token_enc TEXT,
            expires_at INTEGER,
            created TEXT NOT NULL,
            last_used TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS repo_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            repo TEXT NOT NULL,
            connection_id INTEGER NOT NULL,
            created TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conn_user ON connections(user_key, provider);
        CREATE INDEX IF NOT EXISTS idx_repo_user ON repo_connections(user_key, repo);
        """)


init_token_store()


def save_connection(user_key: str, provider: str, account_login: str, scopes: str,
                    access_token: str, refresh_token: str = None, expires_in: int = None) -> int:
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = int(time.time() + expires_in) if expires_in else None
    acc_enc = encrypt_token(access_token)
    ref_enc = encrypt_token(refresh_token) if refresh_token else None

    with get_db() as c:
        row = c.execute("SELECT id FROM connections WHERE user_key=? AND provider=?", (user_key, provider)).fetchone()
        if row:
            cid = row["id"]
            c.execute("""
                UPDATE connections
                SET account_login=?, scopes=?, access_token_enc=?, refresh_token_enc=?,
                    expires_at=?, last_used=?, status='active'
                WHERE id=?
            """, (account_login, scopes, acc_enc, ref_enc, expires_at, now_str, cid))
        else:
            cid = c.execute("""
                INSERT INTO connections (user_key, provider, account_login, scopes, access_token_enc,
                                         refresh_token_enc, expires_at, created, last_used, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """, (user_key, provider, account_login, scopes, acc_enc, ref_enc, expires_at, now_str, now_str)).lastrowid
    return cid


def get_connection(user_key: str, provider: str = None) -> dict:
    with get_db() as c:
        if provider:
            row = c.execute("SELECT * FROM connections WHERE user_key=? AND provider=? AND status!='revoked'",
                            (user_key, provider)).fetchone()
        else:
            row = c.execute("SELECT * FROM connections WHERE user_key=? AND status!='revoked' ORDER BY id DESC LIMIT 1",
                            (user_key,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["access_token"] = decrypt_token(d["access_token_enc"])
    d["refresh_token"] = decrypt_token(d["refresh_token_enc"])
    return d


def disconnect_connection(user_key: str, provider: str):
    with get_db() as c:
        c.execute("UPDATE connections SET status='revoked' WHERE user_key=? AND provider=?", (user_key, provider))


def save_repo_connection(user_key: str, provider: str, repo: str, connection_id: int):
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    clean_repo = repo.strip().lower()
    with get_db() as c:
        row = c.execute("SELECT id FROM repo_connections WHERE user_key=? AND repo=?", (user_key, clean_repo)).fetchone()
        if not row:
            c.execute("INSERT INTO repo_connections (user_key, provider, repo, connection_id, created) VALUES (?, ?, ?, ?, ?)",
                      (user_key, provider, clean_repo, connection_id, now_str))


def set_connection_status(connection_id: int, status: str):
    with get_db() as c:
        c.execute("UPDATE connections SET status=? WHERE id=?", (status, connection_id))


def get_token_for_repo(source: str, user_key: str = None) -> tuple[str, str]:
    """Retrieve decrypted (token, provider) for a repository source if authorized."""
    src = source.strip()
    provider = "github"
    if "gitlab" in src.lower():
        provider = "gitlab"

    # Normalize repo format: e.g. "owner/repo" or URL
    norm_repo = src
    if "github.com/" in src:
        norm_repo = src.split("github.com/")[-1].replace(".git", "").strip("/")
    elif "gitlab.com/" in src:
        norm_repo = src.split("gitlab.com/")[-1].replace(".git", "").strip("/")
    norm_repo = norm_repo.lower()

    with get_db() as c:
        # Check explicit repo link
        conn_row = None
        if user_key:
            rlink = c.execute("SELECT connection_id FROM repo_connections WHERE user_key=? AND repo=?",
                              (user_key, norm_repo)).fetchone()
            if rlink:
                conn_row = c.execute("SELECT * FROM connections WHERE id=? AND status='active'",
                                     (rlink["connection_id"],)).fetchone()

        if not conn_row and user_key:
            conn_row = c.execute("SELECT * FROM connections WHERE user_key=? AND provider=? AND status='active'",
                                 (user_key, provider)).fetchone()

        if not conn_row:
            # Fallback to any active connection for that provider
            conn_row = c.execute("SELECT * FROM connections WHERE provider=? AND status='active' ORDER BY id DESC LIMIT 1",
                                 (provider,)).fetchone()

    if not conn_row:
        return None, None

    conn_dict = dict(conn_row)
    now_ts = time.time()
    # Check expiry within 60s
    if conn_dict.get("expires_at") and (conn_dict["expires_at"] - now_ts <= 60):
        # Refresh if possible
        ref_token = decrypt_token(conn_dict.get("refresh_token_enc"))
        if ref_token:
            from auth import refresh_oauth_token
            new_acc, new_ref, exp = refresh_oauth_token(conn_dict["provider"], ref_token)
            if new_acc:
                save_connection(conn_dict["user_key"], conn_dict["provider"], conn_dict["account_login"],
                                conn_dict["scopes"], new_acc, new_ref or ref_token, exp)
                return new_acc, conn_dict["provider"]
            else:
                set_connection_status(conn_dict["id"], "needs_reconnect")
                return None, conn_dict["provider"]

    token = decrypt_token(conn_dict.get("access_token_enc"))
    return token, conn_dict.get("provider", "github")
