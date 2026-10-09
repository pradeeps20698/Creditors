"""Drop-in OTP auth + access control for child dashboards.

Usage in a child dashboard's main script:

    from swift_auth_child import require_dashboard_access
    user = require_dashboard_access("trip_log")

Each child app runs its own OTP login (Streamlit Cloud apps don't share
session state across subdomains), but they all share the same Postgres
user table, so a user added in Swift Hub can immediately log in to any
child dashboard their role permits.
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import streamlit as st

# Session-state keys — kept identical to swift_auth so hub and child agree.
# Defined locally (not imported from swift_auth) so child dashboards don't need
# the hub's heavy login stack (streamlit-local-storage, login CSS/assets).
SESSION_KEY = "sh_user_email"
RAW_TOKEN_KEY = "sh_raw_token"

from swift_db import (
    get_user,
    init_schema,
    log_access,
    lookup_session,
    user_can_access,
)

SWIFT_HUB_URL = "https://swiftapp-838rpjkwfx8t2uprdmffsd.streamlit.app/"


def _is_localhost() -> bool:
    """Return True when the app is being served from localhost (local dev)."""
    # Streamlit exposes the browser URL via get_script_run_ctx or we can
    # check the STREAMLIT_SERVER_ADDRESS env-var.  The most reliable way
    # is to check whether Streamlit is running on its default local port
    # and NOT on Streamlit Cloud (which sets the STREAMLIT_SHARING_MODE
    # env-var or HOSTNAME that starts with 'streamlit').
    hostname = os.environ.get("HOSTNAME", "")
    if hostname.startswith("streamlit"):
        return False  # running on Streamlit Cloud
    if os.environ.get("STREAMLIT_SHARING_MODE"):
        return False  # Streamlit Cloud sharing mode

    # Check the server address — defaults to localhost for local runs
    server_addr = os.environ.get("STREAMLIT_SERVER_ADDRESS", "localhost")
    return server_addr in ("localhost", "127.0.0.1", "0.0.0.0")


def _block_with_hub_redirect() -> None:
    """Show an 'Access via Swift Hub' page with a working link."""
    st.markdown(
        """
        <div style="text-align:center;margin-top:80px">
          <h1 style="font-size:38px">🔒 Access via Swift Hub</h1>
          <p style="color:#888;font-size:18px">
            This dashboard can only be opened from Swift Hub.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    _l, c, _r = st.columns([1, 1, 1])
    with c:
        st.link_button("Go to Swift Hub →", SWIFT_HUB_URL, use_container_width=True)
    st.markdown(
        f"<p style='text-align:center;margin-top:16px;font-size:13px;color:#666'>"
        f"<a href='{SWIFT_HUB_URL}' target='_blank' rel='noopener' "
        f"style='color:#888;text-decoration:underline'>{SWIFT_HUB_URL}</a></p>",
        unsafe_allow_html=True,
    )
    st.stop()


def require_dashboard_access(dashboard_key: str) -> dict:
    """Allow the page only if the user arrived via a valid Swift Hub ?s= token.
    Otherwise show a 'Go to Swift Hub' page — never an OTP login screen.

    When running on localhost (local development), the Swift Hub token
    check is bypassed so developers can test dashboards directly.
    """
    # --- Localhost bypass for local development ---
    if _is_localhost():
        return {
            "email": "dev@localhost",
            "name": "Local Developer",
            "role": "admin",
        }

    try:
        init_schema()
    except Exception as e:
        st.error(f"Database connection failed: {e}")
        st.stop()

    # Already authenticated in this Streamlit session? (refresh / interaction)
    email = st.session_state.get(SESSION_KEY)

    if not email:
        # Look for a session token passed from Swift Hub via ?s=<token>
        try:
            raw = st.query_params.get("s")
        except Exception:
            raw = None
        if not raw:
            _block_with_hub_redirect()

        session_email = lookup_session(raw)
        if not session_email:
            _block_with_hub_redirect()

        row = get_user(session_email)
        if not row or row["is_blocked"]:
            _block_with_hub_redirect()

        st.session_state[SESSION_KEY] = session_email
        st.session_state[RAW_TOKEN_KEY] = raw
        email = session_email

    # Build the user dict locally (no OTP gate involved)
    row = get_user(email)
    if not row or row["is_blocked"]:
        _block_with_hub_redirect()

    user = {
        "email": email,
        "name": row.get("name") or "",
        "role": row["role"],
    }

    if not user_can_access(user["email"], dashboard_key):
        log_access(user["email"], action="denied", dashboard_key=dashboard_key)
        st.error(
            f"Your role (`{user['role']}`) does not have access to this dashboard. "
            "Contact an administrator."
        )
        st.stop()

    if not st.session_state.get(f"_logged_open_{dashboard_key}"):
        log_access(user["email"], action="open", dashboard_key=dashboard_key)
        st.session_state[f"_logged_open_{dashboard_key}"] = True

    # Heartbeat: log activity every 10 minutes while the user is interacting.
    # Streamlit reruns this code on every widget interaction, so as long as
    # the user keeps clicking, heartbeats keep flowing.
    HEARTBEAT_INTERVAL = 10 * 60  # seconds
    beat_key = f"_last_heartbeat_{dashboard_key}"
    last_beat = st.session_state.get(beat_key, 0)
    now = time.time()
    if now - last_beat >= HEARTBEAT_INTERVAL:
        log_access(user["email"], action="heartbeat", dashboard_key=dashboard_key)
        st.session_state[beat_key] = now

    return user
