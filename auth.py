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
* Set ``DATABASE_URL`` (a Postgres connection string, e.g. Neon) for durable
  accounts + tokens. Tables are auto-created on first run.
* Without ``DATABASE_URL`` the app falls back to a local SQLite file
  (``.streamlit/users.db``, override with ``USER_DB_PATH``) -- fine for local
  development, but Streamlit Community Cloud's *ephemeral* filesystem wipes it
  on container restart.
* Always set ``FERNET_KEY`` in secrets for production so the same encryption
  key is reused across restarts; otherwise stored tokens become undecryptable.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Optional, Tuple

import streamlit as st
import streamlit_authenticator as stauth
from streamlit_authenticator.utilities.validator import Validator
from cryptography.fernet import Fernet, InvalidToken

# ------------------------------------------------------------------
# Relax password rules globally.
#
# streamlit-authenticator's default Validator enforces an 8-20 char
# password with upper/lower/digit/symbol. We want short numeric
# passcodes (e.g. a 4-6 digit PIN). The library also has a bug where
# AuthenticationController ignores any validator passed to
# Authenticate() and hardcodes its own Validator(), so patching the
# class method itself is the only reliable override.
# ------------------------------------------------------------------
Validator.validate_password = lambda self, password: len(password or "") >= 4


# ============================================================
# Paths / config
# ============================================================
_BASE_DIR = os.path.dirname(__file__)
_DB_PATH = os.environ.get("USER_DB_PATH", os.path.join(_BASE_DIR, ".streamlit", "users.db"))
_KEY_PATH = os.path.join(_BASE_DIR, ".streamlit", "fernet.key")


def _secret(key: str, default: str = "") -> str:
    """Read a config value from Streamlit secrets, falling back to env vars.

    Works at module level (no get_secret callable needed) so the DB layer can
    decide its backend without threading secrets through every call.
    """
    try:
        val = st.secrets.get(key, None)
        if val:
            return str(val)
    except Exception:
        pass
    return os.environ.get(key, default)


def _database_url() -> str:
    """Postgres connection string, if configured. Empty string => use SQLite."""
    return _secret("DATABASE_URL", "")


def _is_postgres() -> bool:
    return bool(_database_url())


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
# Store -- Postgres (durable, set DATABASE_URL) or SQLite (local dev)
# ============================================================
#
# Streamlit Community Cloud's filesystem is ephemeral, so a SQLite file is
# wiped on container restart. Set DATABASE_URL to a Postgres connection string
# (e.g. Neon) for accounts + tokens that survive restarts. Without it, the app
# falls back to a local SQLite file -- fine for development.
#
# Both backends accept the same SQL: parameters use "?" placeholders (translated
# to "%s" for Postgres) and the "INSERT ... ON CONFLICT ... DO UPDATE" upsert
# syntax is supported by SQLite 3.24+ and Postgres 9.5+.


class _ConnWrapper:
    """Uniform execute() over sqlite3 / psycopg2 with "?" placeholders and
    dict-like rows (row["col"]) on both backends."""

    def __init__(self, raw, is_pg: bool):
        self._raw = raw
        self._is_pg = is_pg

    def execute(self, sql: str, params: tuple = ()):
        if self._is_pg:
            cur = self._raw.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self._raw.execute(sql, params)


@contextmanager
def _connect():
    """Yield a connection wrapper, committing on success and always closing."""
    if _is_postgres():
        import psycopg2
        from psycopg2.extras import RealDictCursor

        # connect_timeout caps how long a misconfigured / unreachable DATABASE_URL
        # can block the page load (otherwise psycopg2 hangs on the TCP connect).
        conn = psycopg2.connect(
            _database_url(), cursor_factory=RealDictCursor, connect_timeout=10
        )
        try:
            yield _ConnWrapper(conn, is_pg=True)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        conn = sqlite3.connect(_DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            # WAL + busy timeout reduce "database is locked" under concurrency.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield _ConnWrapper(conn, is_pg=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# Tables only need creating once per process; reruns re-import nothing, so this
# module-level flag skips the round trip on every subsequent page load.
_db_ready = False


def _init_db() -> None:
    global _db_ready
    if _db_ready:
        return
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
    _db_ready = True


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


@st.cache_resource(show_spinner=False)
def _cached_credentials() -> dict:
    """Process-wide cache of the user table so login does not re-query Postgres
    on every rerun. Cleared by ``_persist_new_registrations`` when a user signs
    up so the new account is immediately loadable."""
    return _load_credentials()


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

    credentials = _cached_credentials()
    authenticator = stauth.Authenticate(
        credentials,
        "checklist_sync_auth",
        cookie_key,
        cookie_expiry_days=7,
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
            # register_user mutated `credentials` in place on success; persist it
            # and drop the cache so the new user is reloaded from the DB next run.
            _persist_new_registrations(credentials)
            _cached_credentials.clear()
        except Exception as e:  # registration validation errors surface here
            st.error(str(e))

    st.stop()
