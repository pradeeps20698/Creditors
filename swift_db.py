"""Postgres helpers for Swift Hub: users, permissions, access logs."""
from __future__ import annotations

import psycopg2
import psycopg2.extras
import streamlit as st


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def _new_conn():
    cfg = st.secrets["database"]
    conn = psycopg2.connect(
        host=cfg["host"],
        port=int(cfg.get("port", 5432)),
        user=cfg["user"],
        password=cfg["password"],
        dbname=cfg["dbname"],
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    conn.autocommit = True
    return conn


@st.cache_resource(show_spinner=False)
def _cached_conn():
    return _new_conn()


def get_conn():
    """Return a live Postgres connection, reconnecting if the cached one is dead."""
    conn = _cached_conn()
    try:
        conn.cursor().execute("SELECT 1")
    except Exception:
        # Connection is dead — clear the cache and create a fresh one
        try:
            conn.close()
        except Exception:
            pass
        _cached_conn.clear()
        conn = _cached_conn()
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def init_schema() -> None:
    with get_conn().cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS swift_hub_users (
                email       TEXT PRIMARY KEY,
                name        TEXT,
                role        TEXT NOT NULL DEFAULT 'user',
                is_blocked  BOOLEAN NOT NULL DEFAULT FALSE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS swift_hub_dashboard_permissions (
                role           TEXT NOT NULL,
                dashboard_key  TEXT NOT NULL,
                PRIMARY KEY (role, dashboard_key)
            );

            -- Per-USER, per-feature (tab/table/KPI) permissions. Deny-by-default:
            -- a row grants one feature of one dashboard to one user. Currently
            -- only the Creditors/Debtors dashboard uses feature-level gating.
            CREATE TABLE IF NOT EXISTS swift_hub_feature_permissions (
                email          TEXT NOT NULL,
                dashboard_key  TEXT NOT NULL,
                feature_key    TEXT NOT NULL,
                PRIMARY KEY (email, dashboard_key, feature_key)
            );
            CREATE INDEX IF NOT EXISTS idx_swift_hub_feature_perms_user
              ON swift_hub_feature_permissions (email, dashboard_key);

            CREATE TABLE IF NOT EXISTS swift_hub_access_logs (
                id             BIGSERIAL PRIMARY KEY,
                email          TEXT NOT NULL,
                dashboard_key  TEXT,
                action         TEXT NOT NULL,
                ts             TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_swift_hub_access_logs_ts
              ON swift_hub_access_logs (ts DESC);
            CREATE INDEX IF NOT EXISTS idx_swift_hub_access_logs_email
              ON swift_hub_access_logs (email);

            CREATE TABLE IF NOT EXISTS swift_hub_sessions (
                id          BIGSERIAL PRIMARY KEY,
                token_hash  TEXT NOT NULL UNIQUE,
                email       TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL,
                revoked     BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE INDEX IF NOT EXISTS idx_swift_hub_sessions_email
              ON swift_hub_sessions (email);
            CREATE INDEX IF NOT EXISTS idx_swift_hub_sessions_token
              ON swift_hub_sessions (token_hash);

            CREATE TABLE IF NOT EXISTS swift_hub_login_codes (
                email       TEXT NOT NULL,
                code_hash   TEXT NOT NULL,
                expires_at  TIMESTAMPTZ NOT NULL,
                used        BOOLEAN NOT NULL DEFAULT FALSE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_swift_hub_login_codes_email
              ON swift_hub_login_codes (email, created_at DESC);
            """
        )


# Backwards-compat alias
def init_users_table() -> None:
    init_schema()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def list_users() -> list[dict]:
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT email, name, role, is_blocked, created_at FROM swift_hub_users ORDER BY email"
        )
        return [dict(r) for r in cur.fetchall()]


def get_user(email: str) -> dict | None:
    email = email.lower().strip()
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT email, name, role, is_blocked FROM swift_hub_users WHERE email = %s",
            (email,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def upsert_user(email: str, name: str = "", role: str = "user", is_blocked: bool = False) -> None:
    email = email.lower().strip()
    with get_conn().cursor() as cur:
        cur.execute(
            """
            INSERT INTO swift_hub_users (email, name, role, is_blocked)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE
              SET name = EXCLUDED.name,
                  role = EXCLUDED.role,
                  is_blocked = EXCLUDED.is_blocked,
                  updated_at = NOW();
            """,
            (email, name, role, is_blocked),
        )


def delete_user(email: str) -> None:
    email = email.lower().strip()
    with get_conn().cursor() as cur:
        cur.execute("DELETE FROM swift_hub_users WHERE email = %s", (email,))


def set_blocked(email: str, blocked: bool) -> None:
    email = email.lower().strip()
    with get_conn().cursor() as cur:
        cur.execute(
            "UPDATE swift_hub_users SET is_blocked = %s, updated_at = NOW() WHERE email = %s",
            (blocked, email),
        )


def count_users() -> int:
    with get_conn().cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM swift_hub_users")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Dashboard permissions
# ---------------------------------------------------------------------------
def list_permissions() -> list[dict]:
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT role, dashboard_key FROM swift_hub_dashboard_permissions ORDER BY role, dashboard_key"
        )
        return [dict(r) for r in cur.fetchall()]


def get_permitted_dashboards(role: str) -> set[str]:
    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT dashboard_key FROM swift_hub_dashboard_permissions WHERE role = %s",
            (role,),
        )
        return {r[0] for r in cur.fetchall()}


def grant_permission(role: str, dashboard_key: str) -> None:
    with get_conn().cursor() as cur:
        cur.execute(
            """
            INSERT INTO swift_hub_dashboard_permissions (role, dashboard_key)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING;
            """,
            (role, dashboard_key),
        )


def revoke_permission(role: str, dashboard_key: str) -> None:
    with get_conn().cursor() as cur:
        cur.execute(
            "DELETE FROM swift_hub_dashboard_permissions WHERE role = %s AND dashboard_key = %s",
            (role, dashboard_key),
        )


def set_role_permissions(role: str, dashboard_keys: list[str]) -> None:
    with get_conn().cursor() as cur:
        cur.execute(
            "DELETE FROM swift_hub_dashboard_permissions WHERE role = %s", (role,)
        )
        for k in dashboard_keys:
            cur.execute(
                "INSERT INTO swift_hub_dashboard_permissions (role, dashboard_key) VALUES (%s, %s)",
                (role, k),
            )


def user_can_access(email: str, dashboard_key: str) -> bool:
    u = get_user(email)
    if not u or u["is_blocked"]:
        return False
    if u["role"] == "admin":
        return True
    return dashboard_key in get_permitted_dashboards(u["role"])


# ---------------------------------------------------------------------------
# Per-user feature (tab/table/KPI) permissions  — deny-by-default
# ---------------------------------------------------------------------------
def get_user_features(email: str, dashboard_key: str) -> set[str]:
    """Return the set of feature_keys this user is explicitly granted for the
    given dashboard. Empty set means (deny-by-default) they see nothing."""
    email = email.lower().strip()
    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT feature_key FROM swift_hub_feature_permissions "
            "WHERE email = %s AND dashboard_key = %s",
            (email, dashboard_key),
        )
        return {r[0] for r in cur.fetchall()}


def set_user_features(email: str, dashboard_key: str, feature_keys: list[str]) -> None:
    """Replace ALL feature grants for this user+dashboard with feature_keys."""
    email = email.lower().strip()
    with get_conn().cursor() as cur:
        cur.execute(
            "DELETE FROM swift_hub_feature_permissions "
            "WHERE email = %s AND dashboard_key = %s",
            (email, dashboard_key),
        )
        for k in feature_keys:
            cur.execute(
                "INSERT INTO swift_hub_feature_permissions "
                "(email, dashboard_key, feature_key) VALUES (%s, %s, %s)",
                (email, dashboard_key, k),
            )


def grant_feature(email: str, dashboard_key: str, feature_key: str) -> None:
    email = email.lower().strip()
    with get_conn().cursor() as cur:
        cur.execute(
            "INSERT INTO swift_hub_feature_permissions "
            "(email, dashboard_key, feature_key) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (email, dashboard_key, feature_key),
        )


def revoke_feature(email: str, dashboard_key: str, feature_key: str) -> None:
    email = email.lower().strip()
    with get_conn().cursor() as cur:
        cur.execute(
            "DELETE FROM swift_hub_feature_permissions "
            "WHERE email = %s AND dashboard_key = %s AND feature_key = %s",
            (email, dashboard_key, feature_key),
        )


def list_feature_permissions(dashboard_key: str) -> list[dict]:
    """All (email, feature_key) grants for a dashboard — for admin overview."""
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT email, feature_key FROM swift_hub_feature_permissions "
            "WHERE dashboard_key = %s ORDER BY email, feature_key",
            (dashboard_key,),
        )
        return [dict(r) for r in cur.fetchall()]


def user_can_see(email: str, dashboard_key: str, feature_key: str) -> bool:
    """True if the user may see one feature (tab/table/KPI) of a dashboard.

    Admins always see everything. Everyone else must (a) have the dashboard
    via their role AND (b) be explicitly granted the feature. Deny-by-default.
    """
    u = get_user(email)
    if not u or u["is_blocked"]:
        return False
    if u["role"] == "admin":
        return True
    if dashboard_key not in get_permitted_dashboards(u["role"]):
        return False
    return feature_key in get_user_features(email, dashboard_key)


# ---------------------------------------------------------------------------
# Access logs
# ---------------------------------------------------------------------------
def log_access(email: str, action: str, dashboard_key: str | None = None) -> None:
    try:
        with get_conn().cursor() as cur:
            cur.execute(
                "INSERT INTO swift_hub_access_logs (email, dashboard_key, action) VALUES (%s, %s, %s)",
                (email.lower().strip(), dashboard_key, action),
            )
    except Exception:
        # Logging must never break the app.
        pass


def store_login_code(email: str, code_hash: str, ttl_seconds: int = 600) -> None:
    email = email.lower().strip()
    with get_conn().cursor() as cur:
        cur.execute(
            """
            INSERT INTO swift_hub_login_codes (email, code_hash, expires_at)
            VALUES (%s, %s, NOW() + (%s || ' seconds')::interval)
            """,
            (email, code_hash, str(ttl_seconds)),
        )


def consume_login_code(email: str, code_hash: str) -> bool:
    """Mark the most recent matching, unused, unexpired code as used. Returns True on success."""
    email = email.lower().strip()
    with get_conn().cursor() as cur:
        cur.execute(
            """
            UPDATE swift_hub_login_codes
               SET used = TRUE
             WHERE ctid IN (
               SELECT ctid FROM swift_hub_login_codes
                WHERE email = %s
                  AND code_hash = %s
                  AND used = FALSE
                  AND expires_at > NOW()
                ORDER BY created_at DESC
                LIMIT 1
             )
            """,
            (email, code_hash),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Sessions (DB-backed, server-revocable)
# ---------------------------------------------------------------------------
import hashlib as _hashlib
import secrets as _secrets


def _hash_token(raw: str) -> str:
    return _hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_session(email: str, ttl_seconds: int = 7 * 24 * 60 * 60) -> str:
    """Create a new session and return the raw token to give the client.
    Only the SHA-256 hash is stored server-side."""
    email = email.lower().strip()
    raw = _secrets.token_urlsafe(32)
    with get_conn().cursor() as cur:
        cur.execute(
            """
            INSERT INTO swift_hub_sessions (token_hash, email, expires_at)
            VALUES (%s, %s, NOW() + (%s || ' seconds')::interval)
            """,
            (_hash_token(raw), email, str(ttl_seconds)),
        )
    return raw


def lookup_session(raw_token: str) -> str | None:
    """Return the email for a valid (not expired, not revoked) session, else None.
    Touches last_seen on success."""
    if not raw_token:
        return None
    with get_conn().cursor() as cur:
        cur.execute(
            """
            UPDATE swift_hub_sessions
               SET last_seen = NOW()
             WHERE token_hash = %s
               AND revoked = FALSE
               AND expires_at > NOW()
         RETURNING email
            """,
            (_hash_token(raw_token),),
        )
        row = cur.fetchone()
        return row[0] if row else None


def revoke_session(raw_token: str) -> None:
    if not raw_token:
        return
    with get_conn().cursor() as cur:
        cur.execute(
            "UPDATE swift_hub_sessions SET revoked = TRUE WHERE token_hash = %s",
            (_hash_token(raw_token),),
        )


def revoke_all_sessions_for(email: str) -> int:
    email = email.lower().strip()
    with get_conn().cursor() as cur:
        cur.execute(
            "UPDATE swift_hub_sessions SET revoked = TRUE WHERE email = %s AND revoked = FALSE",
            (email,),
        )
        return cur.rowcount


def list_active_sessions() -> list[dict]:
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, email, created_at, last_seen, expires_at
              FROM swift_hub_sessions
             WHERE revoked = FALSE AND expires_at > NOW()
             ORDER BY last_seen DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]


def revoke_session_by_id(session_id: int) -> None:
    with get_conn().cursor() as cur:
        cur.execute("UPDATE swift_hub_sessions SET revoked = TRUE WHERE id = %s", (session_id,))


def recent_logs(limit: int = 200) -> list[dict]:
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT email, dashboard_key, action, ts FROM swift_hub_access_logs ORDER BY ts DESC LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def logs_in_date_range(start_date: str, end_date: str) -> list[dict]:
    """Return access logs between start_date and end_date (inclusive, IST)."""
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT email, dashboard_key, action, ts
              FROM swift_hub_access_logs
             WHERE ts >= (%s::date) AT TIME ZONE 'Asia/Kolkata'
               AND ts <  ((%s::date) + INTERVAL '1 day') AT TIME ZONE 'Asia/Kolkata'
               AND action IN ('open', 'heartbeat')
             ORDER BY ts
            """,
            (start_date, end_date),
        )
        return [dict(r) for r in cur.fetchall()]
