"""
auth.py -- User accounts + per-user encrypted Smartsheet API keys.

Provides a username/password login layer (via streamlit-authenticator) and a
small SQLite store that holds:

  * user accounts (username, name, email, bcrypt password hash)
  * each user's *own* Smartsheet API token + default workspace, encrypted at
    rest with Fernet so the token is never stored in plaintext.

This replaces the old single shared-token model. Every user signs in and
supplies their own Smartsheet API key; one user can never see or use another
user's key.

Storage notes
-------------
* DB path defaults to ``.streamlit/users.db`` (override with ``USER_DB_PATH``).
* Streamlit Community Cloud has an *ephemeral* filesystem -- the DB and the
  encryption key survive within a running container but are wiped on restart.
  For durable production use, point ``USER_DB_PATH`` at a mounted volume (or
  swap the SQLite calls for an external DB) and set ``FERNET_KEY`` in secrets
  so the same key is reused across restarts.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Callable, Optional, Tuple

import streamlit as st
import streamlit_authenticator as stauth
from streamlit_authenticator.utilities.validator import Validator
from cryptography.fernet import Fernet, InvalidToken

# ============================================================
# Paths
# ============================================================
_BASE_DIR = os.path.dirname(__file__)
_DB_PATH = os.environ.get("USER_DB_PATH", os.path.join(_BASE_DIR, ".streamlit", "users.db"))
_KEY_PATH = os.path.join(_BASE_DIR, ".streamlit", "fernet.key")


# ============================================================
# Encryption
# ============================================================
def _get_fernet(get_secret: Callable[[str, str], str]) -> Fernet:
    """Return a Fernet cipher.

    Key resolution order:
      1. ``FERNET_KEY`` secret / env var (recommended for production -- keep it
         stable so persisted tokens stay decryptable across restarts).
      2. A locally generated key cached in ``.streamlit/fernet.key`` (gitignored).
    """
    key = get_secret("FERNET_KEY", "") or os.environ.get("FERNET_KEY", "")
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)

    # Fall back to a locally cached key so tokens persist within this container.
    try:
        with open(_KEY_PATH, "rb") as f:
            return Fernet(f.read().strip())
    except Exception:
        new_key = Fernet.generate_key()
        try:
            os.makedirs(os.path.dirname(_KEY_PATH), exist_ok=True)
            with open(_KEY_PATH, "wb") as f:
                f.write(new_key)
        except Exception:
            pass
        return Fernet(new_key)


def _encrypt(fernet: Fernet, plaintext: str) -> str:
    if not plaintext:
        return ""
    return fernet.encrypt(plaintext.encode()).decode()


def _decrypt(fernet: Fernet, token: str) -> str:
    if not token:
        return ""
    try:
        return fernet.decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        # Key rotated or corrupt value -- treat as "no stored token".
        return ""


# ============================================================
# SQLite store
# ============================================================
def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username      TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                email         TEXT,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_secrets (
                username   TEXT PRIMARY KEY,
                api_token  TEXT,
                workspace_id TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (username) REFERENCES users (username)
            )
            """
        )


def _load_credentials() -> dict:
    """Build the credentials dict that streamlit-authenticator expects."""
    creds: dict = {"usernames": {}}
    with _connect() as conn:
        for row in conn.execute("SELECT username, name, email, password_hash FROM users"):
            creds["usernames"][row["username"]] = {
                "name": row["name"],
                "email": row["email"] or "",
                "password": row["password_hash"],
            }
    return creds


def _upsert_user(username: str, name: str, email: str, password_hash: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (username, name, email, password_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                name=excluded.name,
                email=excluded.email,
                password_hash=excluded.password_hash
            """,
            (username, name, email, password_hash, datetime.utcnow().isoformat()),
        )


def _persist_new_registrations(credentials: dict) -> None:
    """After register_user mutates the in-memory dict, write any new users to DB."""
    with _connect() as conn:
        existing = {r["username"] for r in conn.execute("SELECT username FROM users")}
    for username, data in credentials.get("usernames", {}).items():
        if username not in existing:
            _upsert_user(
                username,
                data.get("name", username),
                data.get("email", ""),
                data.get("password", ""),
            )


# ============================================================
# Per-user secret accessors (public)
# ============================================================
def load_user_secrets(username: str, get_secret: Callable[[str, str], str]) -> Tuple[str, str]:
    """Return (api_token, workspace_id) for a user, decrypting the token."""
    fernet = _get_fernet(get_secret)
    with _connect() as conn:
        row = conn.execute(
            "SELECT api_token, workspace_id FROM user_secrets WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        return "", ""
    return _decrypt(fernet, row["api_token"] or ""), (row["workspace_id"] or "")


def save_user_secrets(
    username: str,
    api_token: str,
    workspace_id: str,
    get_secret: Callable[[str, str], str],
) -> None:
    """Encrypt and persist a user's own API token + workspace."""
    fernet = _get_fernet(get_secret)
    encrypted = _encrypt(fernet, api_token or "")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_secrets (username, api_token, workspace_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                api_token=excluded.api_token,
                workspace_id=excluded.workspace_id,
                updated_at=excluded.updated_at
            """,
            (username, encrypted, workspace_id or "", datetime.utcnow().isoformat()),
        )


# ============================================================
# Permissive validator for passcodes
# ============================================================
class PermissiveValidator(Validator):
    """Allow any password 4+ characters with no complexity requirements."""

    def validate_password(self, password: str) -> bool:
        return len(password) >= 4


# ============================================================
# Login gate (public)
# ============================================================
def require_login(get_secret: Callable[[str, str], str]):
    """Render the login / registration UI and gate the app.

    Returns the authenticator object and the logged-in username once the user
    is authenticated; otherwise calls ``st.stop()``.
    """
    _init_db()

    cookie_key = (
        get_secret("AUTH_COOKIE_KEY", "")
        or os.environ.get("AUTH_COOKIE_KEY", "")
        or "checklist-sync-dev-cookie-key-change-me"
    )

    credentials = _load_credentials()
    validator = PermissiveValidator()
    authenticator = stauth.Authenticate(
        credentials,
        "checklist_sync_auth",
        cookie_key,
        cookie_expiry_days=7,
        validator=validator,
    )

    authenticator.login(location="main")
    status = st.session_state.get("authentication_status")

    if status:
        return authenticator, st.session_state.get("username")

    # Not logged in -- show login error (if any) and a registration option.
    if status is False:
        st.error("Username or password is incorrect.")
    else:
        st.info("Please sign in, or create an account, to continue.")

    with st.expander("Create a new account"):
        try:
            authenticator.register_user(
                location="main", pre_authorization=False, captcha=False
            )
            # register_user mutated `credentials` in place on success; persist it.
            _persist_new_registrations(credentials)
        except Exception as e:  # registration validation errors surface here
            st.error(str(e))

    st.stop()
