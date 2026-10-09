"""Per-dashboard feature catalogue for tab/table/KPI-level access control.

A "feature" is one gate-able piece of a dashboard: a KPI card, a whole tab,
or a single table/chart inside a tab. Access is per-user and deny-by-default
(see ``swift_db.user_can_see``). Admins always see everything.

Only the Creditors/Debtors dashboard (key ``creditors``) uses feature-level
gating today. Every other dashboard stays dashboard-level only — it simply has
no entry here, and ``has_feature_gating()`` returns False for it.

The SAME file is copied into the Creditors app repo so the dashboard and the
Swift Hub admin UI agree on feature_keys. Keep both copies identical.
"""
from __future__ import annotations

# Each feature: key (stored in DB), label (shown to admin), group (UI heading).
CREDITORS_FEATURES: list[dict] = [
    # --- KPI cards (top metric row) ---------------------------------------
    {"key": "kpi_payables",            "group": "KPI cards",   "label": "Payables (we owe)"},
    {"key": "kpi_receivables",         "group": "KPI cards",   "label": "Receivables (owed to us)"},
    {"key": "kpi_net_balance",         "group": "KPI cards",   "label": "Net balance"},
    {"key": "kpi_accounts",            "group": "KPI cards",   "label": "Accounts (net ≠ 0)"},

    # --- Payables tab ------------------------------------------------------
    {"key": "tab_payables",            "group": "Payables tab", "label": "Tab: Payables only (we owe)"},
    {"key": "table_pump_vendors",      "group": "Payables tab", "label": "⛽ Pump Vendors table"},
    {"key": "table_vendor_groups",     "group": "Payables tab", "label": "🏢 Vendor groups + Unclassified tables"},

    # --- Receivables tab ---------------------------------------------------
    {"key": "tab_receivables",         "group": "Receivables tab", "label": "Tab: Receivables only (owed to us)"},
    {"key": "table_oem_accounts",      "group": "Receivables tab", "label": "📥 OEM Accounts table"},
    {"key": "table_market_load",       "group": "Receivables tab", "label": "📋 Market load Accounts table"},

    # --- All accounts tab --------------------------------------------------
    {"key": "tab_all",                 "group": "All accounts tab", "label": "Tab: All accounts"},
    {"key": "table_aging_summary",     "group": "All accounts tab", "label": "📊 Aging summary (Payables vs Receivables)"},
    {"key": "table_expected_collection","group": "All accounts tab", "label": "📅 Expected Collection — OEM (week-wise)"},
    {"key": "table_payment_vs_outstanding","group": "All accounts tab", "label": "💰 Payment Received vs Outstanding — OEM"},
    {"key": "chart_outstanding_by_office","group": "All accounts tab", "label": "Chart: Outstanding by office"},
    {"key": "chart_aging_outstanding", "group": "All accounts tab", "label": "Chart: Aging of outstanding"},
    {"key": "chart_split_category",    "group": "All accounts tab", "label": "Chart: Split by category & type"},
    {"key": "chart_monthly_trend",     "group": "All accounts tab", "label": "Chart: Monthly reference amount trend"},
    {"key": "chart_top_parties",       "group": "All accounts tab", "label": "Chart: Top parties by outstanding"},
]

FEATURE_CATALOGUE: dict[str, list[dict]] = {
    "creditors": CREDITORS_FEATURES,
}


def has_feature_gating(dashboard_key: str) -> bool:
    """True if this dashboard uses per-feature (tab/table) access control."""
    return dashboard_key in FEATURE_CATALOGUE


def features_for(dashboard_key: str) -> list[dict]:
    """Full feature dicts (key/group/label) for a dashboard, in display order."""
    return FEATURE_CATALOGUE.get(dashboard_key, [])


def feature_keys(dashboard_key: str) -> list[str]:
    """Just the feature_keys for a dashboard, in display order."""
    return [f["key"] for f in FEATURE_CATALOGUE.get(dashboard_key, [])]
