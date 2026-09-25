"""
Swift Party Reference — Creditors / Receivables Dashboard
Streamlit dashboard built on the `swift_party_ref` table (AWS RDS Postgres).

Run:  streamlit run app.py   ->  http://localhost:8501/
"""
import os
import re
from datetime import datetime

import pandas as pd
import streamlit as st
import plotly.express as px
from dotenv import load_dotenv
from sqlalchemy import create_engine
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode

load_dotenv()

# --------------------------------------------------------------------------- #
# AgGrid dark-theme backstop
# --------------------------------------------------------------------------- #
# `theme="streamlit"` follows whatever theme the viewer resolves to, so a viewer
# whose browser is set to Light gets light grids even though config.toml pins a
# dark default. These overrides repaint the AG Grid base dark via its own CSS
# variables, so every table looks identical (dark) for all viewers. Variables
# only set defaults, so inline row styles from getRowStyle (amber "no credit"
# rows, grey/blue TOTAL rows) still win and keep their highlight colours.
AG_DARK_VARS = {
    ".ag-root-wrapper": {
        "--ag-background-color": "#0e1117",
        "--ag-foreground-color": "#fafafa",
        "--ag-data-color": "#fafafa",
        "--ag-header-background-color": "#161b22",
        "--ag-header-foreground-color": "#4aa3ff",
        # Uniform navy background — no odd/even row striping.
        "--ag-odd-row-background-color": "#0e1117",
        "--ag-row-hover-color": "rgba(74,163,255,0.12)",
        "--ag-border-color": "#30363d",
        "--ag-row-border-color": "#21262d",
        "--ag-secondary-border-color": "#21262d",
        # Vertical divider line between every column (light grey, clearly visible).
        "--ag-cell-horizontal-border": "solid 1px #484f58",
        "background-color": "#0e1117",
    },
    # Explicit vertical divider line between columns. The
    # --ag-cell-horizontal-border variable is ignored by this theme, so draw a
    # right border on every body + header cell directly (!important to win).
    ".ag-cell, .ag-header-cell": {
        "border-right": "1px solid #484f58 !important",
    },
    # Kill the odd/even row striping so every row is the same navy. The
    # streamlit AgGrid theme paints .ag-row-odd with its own rule that beats the
    # CSS variable, so target the row classes directly with higher specificity.
    # No !important, so getRowStyle inline highlights (amber, TOTAL) still win.
    ".ag-root-wrapper .ag-row.ag-row-odd, "
    ".ag-root-wrapper .ag-row.ag-row-even": {
        "background-color": "#0e1117",
    },
    # Pinned (Account Name) column: AG Grid tints the pinned region grey. Force
    # the pinned containers and their cells transparent so the row's own
    # background shows through — matches the navy body and preserves the amber
    # "no credit" / TOTAL row highlights that getRowStyle paints on the row.
    # !important is required to beat AG Grid's own cell/container background.
    ".ag-pinned-left-cols-container, .ag-pinned-right-cols-container, "
    ".ag-pinned-left-header, .ag-pinned-right-header": {
        "background-color": "transparent !important",
    },
    ".ag-pinned-left-cols-container .ag-cell, "
    ".ag-pinned-right-cols-container .ag-cell": {
        "background-color": "transparent !important",
    },
}
# Full backstop = dark base + the centred blue header styling the aging/OEM
# tables already used.
AG_DARK_CSS = {
    **AG_DARK_VARS,
    ".ag-header-cell-label": {"justify-content": "center"},
    ".ag-header-cell-text": {"color": "#4aa3ff", "font-weight": "700"},
}

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="Swift Creditors/Debtors Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

def _db_conf(key, env, default=None):
    """Read config from Streamlit secrets first, then env, then default."""
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(env, default)


DB = dict(
    host=_db_conf("PGHOST", "PGHOST"),
    user=_db_conf("PGUSER", "PGUSER"),
    password=_db_conf("PGPASSWORD", "PGPASSWORD"),
    port=_db_conf("PGPORT", "PGPORT", "5432"),
    dbname=_db_conf("PGDATABASE", "PGDATABASE", "postgres"),
)
TODAY = pd.Timestamp(datetime.now().date())


@st.cache_resource
def get_engine():
    url = f"postgresql+psycopg2://{DB['user']}:{DB['password']}@{DB['host']}:{DB['port']}/{DB['dbname']}"
    return create_engine(url, pool_pre_ping=True)


@st.cache_data(ttl=600, show_spinner="Loading data from RDS…")
def load_data() -> pd.DataFrame:
    q = "SELECT * FROM swift_party_ref"
    df = pd.read_sql(q, get_engine())
    for c in ["ref_date", "due_date", "ref_doe", "created_at", "synced_at", "active_updated_at"]:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    for c in ["ref_amount", "amount_paid", "bal_amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    # Business framing:
    #  - Current Liabilities  -> Creditors / Payables (we owe)
    #  - Current Assets        -> Debtors / Receivables (owed to us)
    df["category"] = df["account_type"].map(
        {"Current Liabilities": "Payables (Creditors)", "Current Assets": "Receivables (Debtors)"}
    ).fillna(df["account_type"].fillna("Unknown"))

    # Outstanding as positive magnitude for readability
    df["outstanding"] = df["bal_amount"].abs()

    # Aging on open items (due date driven)
    df["days_overdue"] = (TODAY - df["due_date"]).dt.days
    df["is_overdue"] = df["days_overdue"] > 0

    def bucket(d):
        if pd.isna(d):
            return "No due date"
        if d <= 0:
            return "Not due"
        if d <= 30:
            return "1-30 days"
        if d <= 60:
            return "31-60 days"
        if d <= 90:
            return "61-90 days"
        return "90+ days"

    df["aging_bucket"] = df["days_overdue"].apply(bucket)

    # Dashboard shows ACTIVE references only (inactive = soft-deleted in source sync)
    df = df[df["is_active"].fillna(False) == True]  # noqa: E712
    return df


AGING_ORDER = ["Not due", "1-30 days", "31-60 days", "61-90 days", "90+ days", "No due date"]
PERIOD_START = pd.Timestamp("2025-04-01")  # dashboard covers Apr 2025 -> today (no FY split)


def inr(v: float) -> str:
    """Indian-style short currency formatting."""
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e7:
        return f"{sign}₹{a/1e7:,.2f} Cr"
    if a >= 1e5:
        return f"{sign}₹{a/1e5:,.2f} L"
    return f"{sign}₹{a:,.0f}"


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
try:
    data = load_data()
except Exception as e:
    st.error(f"Could not connect to the database.\n\n{e}")
    st.stop()

# Per-account "no credit terms" flag (credit_days null or <= 0 on every ref).
# Without credit_days a due date can't be derived, so these accounts are flagged.
_cd_max = (data.assign(_cd=pd.to_numeric(data["credit_days"], errors="coerce"))
           .groupby("account_name")["_cd"].max())
NO_CREDIT = _cd_max.isna() | (_cd_max <= 0)

st.markdown(
    "<h1 style='text-align:center;'>📊 Swift Creditors/Debtors Dashboard</h1>",
    unsafe_allow_html=True,
)
st.markdown(
    "<p style='text-align:center; color:#8b949e; font-size:0.9rem; margin-top:-0.5rem;'>"
    f"Source: <code>swift_party_ref</code> · {len(data):,} reference rows · "
    f"data as of {TODAY:%d %b %Y}</p>",
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Sidebar filters
# --------------------------------------------------------------------------- #
st.sidebar.header("Filters")

if st.sidebar.button("🔄 Refresh data (clear cache)", use_container_width=True):
    st.cache_data.clear()
    st.rerun()


def ms(label, col):
    opts = sorted([x for x in data[col].dropna().unique()])
    return st.sidebar.multiselect(label, opts, default=[])


f_cat = ms("Category", "category")
f_office = ms("Office", "office")
f_div = ms("Division", "division_name")
f_reftype = ms("Reference type", "ref_type")

st.sidebar.caption("Showing **active references only**")
overdue_only = st.sidebar.checkbox("Overdue items only", value=False)

min_d, max_d = data["ref_date"].min(), data["ref_date"].max()
date_range = st.sidebar.date_input(
    "Reference date range",
    value=(min_d.date(), max_d.date()),
    min_value=min_d.date(),
    max_value=max_d.date(),
)

df = data.copy()
if f_cat:
    df = df[df["category"].isin(f_cat)]
if f_office:
    df = df[df["office"].isin(f_office)]
if f_div:
    df = df[df["division_name"].isin(f_div)]
if f_reftype:
    df = df[df["ref_type"].isin(f_reftype)]
if overdue_only:
    df = df[df["is_overdue"] == True]  # noqa: E712
if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
    s, e = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1]) + pd.Timedelta(days=1)
    df = df[(df["ref_date"] >= s) & (df["ref_date"] < e)]

if df.empty:
    st.warning("No rows match the selected filters.")
    st.stop()

# --------------------------------------------------------------------------- #
# Account classifiers — decide which tab an account belongs to.
# Accounts that belong to a dedicated tab (Enroute Vendors, Control-AC & others)
# are shown ONLY there and excluded from All accounts / Payables / Receivables.
# --------------------------------------------------------------------------- #
CODE_PATTERN = r"iocl|bpcl|hpcl"          # oil-marketing-company codes
FUEL_KW_PATTERN = r"pump|petrol|fuel|filling"
CONTROL_PATTERN = r"control|slip|puc"


@st.cache_data
def load_enroute_norm() -> set:
    """Normalized (strip+lower) set of Enroute-vendor account names.

    Read from enroute_vendors.txt (one account name per line) so the large list
    lives outside the code.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enroute_vendors.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return {ln.strip().lower() for ln in fh if ln.strip()}
    except FileNotFoundError:
        return set()


ENROUTE_NORM = load_enroute_norm()


def _is_enroute(frame: pd.DataFrame) -> pd.Series:
    """Rows whose account_name is in the Enroute-vendors list."""
    return frame["account_name"].fillna("").str.strip().str.lower().isin(ENROUTE_NORM)


def _is_control_others(frame: pd.DataFrame) -> pd.Series:
    """Rows for the Control-AC & others tab: control/slip/PUC accounts, plus
    fuel/pump accounts that do NOT carry an oil-company code (IOCL/BPCL/HPCL)."""
    names = frame["account_name"].fillna("")
    has_code = names.str.contains(CODE_PATTERN, case=False, regex=True)
    is_fuel_kw = names.str.contains(FUEL_KW_PATTERN, case=False, regex=True)
    is_control = names.str.contains(CONTROL_PATTERN, case=False, regex=True)
    return is_control | (is_fuel_kw & ~has_code)


def _is_dedicated(frame: pd.DataFrame) -> pd.Series:
    """Accounts that live in a dedicated tab (Enroute or Control-AC & others),
    so they are excluded from All accounts / Payables / Receivables."""
    return _is_enroute(frame) | _is_control_others(frame)


def _pump_group(name: str) -> str:
    """Map a pump-vendor account to its oil company (BPCL / IOCL / HPCL) by the
    code embedded in the name; anything without a code falls into 'Other'."""
    low = (name or "").lower()
    for code in ("bpcl", "iocl", "hpcl"):
        if code in low:
            return code.upper()
    return "Other"


# --------------------------------------------------------------------------- #
# KPI row  —  NET, account-level (matches the Account-wise Ledger below)
# --------------------------------------------------------------------------- #
# Only Payables + Receivables tab accounts count here — exclude accounts that
# live in a dedicated tab (Enroute Vendors, Control-AC & others).
df_kpi = df[~_is_dedicated(df)]
# Net balance per account, then drop near-zero (-100..100), same rule as ledger
acct_net = df_kpi.groupby("account_name")["bal_amount"].sum()
acct_net = acct_net[(acct_net < -100) | (acct_net > 100)]
payables = acct_net[acct_net < 0].sum()      # negative net = we owe
receivables = acct_net[acct_net > 0].sum()   # positive net = owed to us
net = acct_net.sum()
n_parties = len(acct_net)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Payables (we owe)", inr(payables))
c2.metric("Receivables (owed to us)", inr(receivables))
c3.metric("Net balance", inr(net))
c4.metric("Accounts (net ≠ 0)", f"{n_parties:,}")

st.divider()

# --------------------------------------------------------------------------- #
# Account-wise ledger — Apr 2025 → today (single period, NO financial-year split)
# --------------------------------------------------------------------------- #
st.subheader("📋 Account-wise Ledger (Apr 2025 → today)")
st.caption(
    f"All references {PERIOD_START:%d/%m/%Y} → {TODAY:%d/%m/%Y} · no financial-year split. "
    "**Bill Amount** = ref_amount · **Amount Paid / Received** = amount_paid · "
    "**Net Outstanding** = bal_amount (negative = payable / we owe)."
)

# Full period (ignores the sidebar date range) but respects the categorical /
# status filters selected in the sidebar.
led = data.copy()
if f_cat:
    led = led[led["category"].isin(f_cat)]
if f_office:
    led = led[led["office"].isin(f_office)]
if f_div:
    led = led[led["division_name"].isin(f_div)]
if f_reftype:
    led = led[led["ref_type"].isin(f_reftype)]

# Base aggregation — the All accounts ledger. Excludes accounts that belong to a
# dedicated tab (Enroute vendors, Control-AC & others).
acct_base = (
    led[~_is_dedicated(led)].groupby("account_name")
    .agg(bill_amount=("ref_amount", "sum"),
         amount_paid=("amount_paid", "sum"),
         net_outstanding=("bal_amount", "sum"),
         refs=("invoice_ref_id", "count"))
    .reset_index()
)


def render_ledger(acct_all: pd.DataFrame, sign: str, key: str) -> None:
    """Render the account-wise ledger grid.

    sign: "all" | "payables" (net<0) | "receivables" (net>0) — the balance filter.
    key : unique suffix for widget keys / CSV file name.
    """
    acct = acct_all.copy()
    if sign == "payables":
        acct = acct[acct["net_outstanding"] < 0]
    elif sign == "receivables":
        acct = acct[acct["net_outstanding"] > 0]
    # Hide near-zero accounts (net outstanding between -100 and 100)
    acct = acct[(acct["net_outstanding"] < -100) | (acct["net_outstanding"] > 100)]
    # Default order: Net Outstanding, highest → lowest
    acct = acct.sort_values("net_outstanding", ascending=False)

    acct_display = acct.rename(columns={
        "account_name": "Account Name",
        "bill_amount": "Bill Amount",
        "amount_paid": "Amount Paid / Received",
        "net_outstanding": "Net Outstanding",
        "refs": "Refs",
    })[["Account Name", "Bill Amount", "Amount Paid / Received", "Net Outstanding", "Refs"]]

    if acct_display.empty:
        st.info("No accounts in this group.")
        return

    # Flag accounts with NO credit terms (credit_days null or <= 0) for highlight
    acct_display["_no_credit"] = acct_display["Account Name"].map(NO_CREDIT).fillna(False)

    # Interactive grid — per-column search lives INSIDE the header (floating filters)
    gb = GridOptionsBuilder.from_dataframe(acct_display)
    gb.configure_default_column(
        filter=True, floatingFilter=True, sortable=True, resizable=True,
        flex=1,  # stretch columns to fill width (no empty gap on the right)
        filterParams={"buttons": ["clear"]},
    )
    inr_fmt = JsCode(
        "function(p){return p.value==null?'':Number(p.value)"
        ".toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});}"
    )
    for col in ["Bill Amount", "Amount Paid / Received", "Net Outstanding"]:
        gb.configure_column(col, type=["numericColumn"],
                            filter="agNumberColumnFilter", valueFormatter=inr_fmt)
    gb.configure_column(
        "Account Name", minWidth=280,
        filter="agTextColumnFilter",
        filterParams={"filterOptions": ["contains"], "defaultOption": "contains",
                      "maxNumConditions": 1, "buttons": ["clear"]},
    )
    gb.configure_column("Net Outstanding", sort="desc")  # default order
    gb.configure_column("_no_credit", hide=True)  # hidden flag drives row colour
    grid_options = gb.build()
    # Allow selecting & copying cell text (e.g. the account name)
    grid_options["enableCellTextSelection"] = True
    grid_options["ensureDomOrder"] = True

    # Pinned TOTAL row at the top of the grid
    grid_options["pinnedTopRowData"] = [{
        "Account Name": f"TOTAL  ({len(acct_display):,} accounts)",
        "Bill Amount": float(acct_display["Bill Amount"].sum()),
        "Amount Paid / Received": float(acct_display["Amount Paid / Received"].sum()),
        "Net Outstanding": float(acct_display["Net Outstanding"].sum()),
        "Refs": int(acct_display["Refs"].sum()),
    }]
    # Pinned TOTAL row = grey/bold; accounts with no credit terms = amber highlight
    grid_options["getRowStyle"] = JsCode(
        "function(p){"
        " if(p.node.rowPinned){ return {'fontWeight':'700','background':'rgba(120,120,120,0.18)'}; }"
        " if(p.data && p.data._no_credit){ return {'background':'rgba(255,193,7,0.22)'}; }"
        "}"
    )

    metrics_box = st.container()
    AgGrid(
        acct_display,
        gridOptions=grid_options,
        height=460,
        theme="streamlit",
        allow_unsafe_jscode=True,
        fit_columns_on_grid_load=True,
        update_on=["filterChanged", "sortChanged"],
        data_return_mode="filtered_and_sorted",
        custom_css=AG_DARK_VARS,
        key=f"ledger_grid_{key}",
    )
    # Metric cards read from the SAME source as the pinned TOTAL row (acct_display),
    # so the cards and the in-grid total row always agree.
    with metrics_box:
        n_no_credit = int(acct_display["_no_credit"].sum())
        lk1, lk2, lk3 = st.columns(3)
        lk1.metric("Accounts", f"{len(acct_display):,}")
        lk2.metric("Total Net Outstanding", inr(acct_display["Net Outstanding"].sum()))
        lk3.metric("Total Amount Paid / Received", inr(acct_display["Amount Paid / Received"].sum()))
        if n_no_credit:
            st.caption(
                f"🟡 **{n_no_credit}** account(s) highlighted amber have **no credit_days** "
                "set — their due date can't be derived. Set credit_days for these accounts."
            )

    st.download_button(
        "⬇️ Download account-wise ledger (CSV)",
        acct_display.drop(columns=["_no_credit"]).to_csv(index=False).encode("utf-8"),
        file_name=f"account_wise_ledger_{key}_apr2025_to_date.csv",
        mime="text/csv",
        key=f"dl_ledger_{key}",
    )


# Due-date buckets, forward-looking: "Overdue" (already crossed today) first,
# then amounts coming due in the next N days (days_to_due = due_date - today).
# Each bin is (label, inclusive_upper_days); the last bin uses None = open-ended.
PAYABLE_BINS = [("Next 0-3", 3), ("Next 3-5", 5), ("Next 6-7", 7), ("Next 8-14", 14),
                ("Next 14-21", 21), ("Next 21-30", 30), ("Next 30+", None)]
RECEIVABLE_BINS = [("Next 0-7", 7), ("Next 8-14", 14), ("Next 15-30", 30),
                   ("Next 30-60", 60), ("Next 60+", None)]
FUTURE_COLS = [lbl for lbl, _ in PAYABLE_BINS]


def _make_bucketer(bins):
    """Return a fn mapping days-until-due to a bucket label for the given bins."""
    def _bucket(d):
        if pd.isna(d):
            return "No due date"
        if d < 0:
            return "Overdue"
        for label, hi in bins:
            if hi is None or d <= hi:
                return label
        return bins[-1][0]
    return _bucket


def render_aging_ledger(rows: pd.DataFrame, key: str,
                        bins=PAYABLE_BINS, sign: str = "payable",
                        group_map=None, name_header: str = "Account Name",
                        unit: str = "accounts",
                        default_credit_days: int = None) -> None:
    """Account-wise Net Outstanding split by due date.

    First column "Overdue" = amounts already past due (due_date < today). The
    remaining columns are amounts coming due in the next windows (per `bins`).
    sign="payable" keeps net<0 accounts; sign="receivable" keeps net>0 accounts.

    group_map : optional callable(account_name) -> group label. When given, rows
                are aggregated by that label instead of by account_name (e.g.
                collapse every "Tata …" account into one "Tata" row). name_header
                / unit control the first-column header and the TOTAL/metric wording.
    default_credit_days : when set, rows whose DB credit_days is zero/null get a
                default due date (ref_date + this many days) so their amounts land
                in the right aging bucket instead of all falling into Overdue.
    """
    future_cols = [lbl for lbl, _ in bins]
    r = rows.copy()
    if default_credit_days is not None:
        cd = pd.to_numeric(r["credit_days"], errors="coerce")
        missing = cd.isna() | (cd <= 0)
        if missing.any():
            r.loc[missing, "due_date"] = (
                r.loc[missing, "ref_date"]
                + pd.to_timedelta(float(default_credit_days), unit="D")
            )
    if group_map is not None:
        r["account_name"] = r["account_name"].fillna("").map(group_map)
    r["_bkt"] = (r["due_date"] - TODAY).dt.days.apply(_make_bucketer(bins))
    r["_dov"] = (TODAY - r["due_date"]).dt.days
    ov_tips = _overdue_tip_by_account(r)  # account -> Overdue day-range tooltip HTML
    full_order = ["Overdue"] + future_cols + ["No due date"]
    piv = (
        r.pivot_table(index="account_name", columns="_bkt",
                      values="bal_amount", aggfunc="sum", fill_value=0.0)
        .reindex(columns=full_order, fill_value=0.0)
    )
    piv["Total"] = piv.sum(axis=1)
    # Keep the requested side + hide near-zero accounts; largest magnitude first
    if sign == "receivable":
        piv = piv[piv["Total"] > 100].sort_values("Total", ascending=False)
    else:
        piv = piv[piv["Total"] < -100].sort_values("Total")
    # Drop "No due date" if it carries no amount anywhere (Overdue is always kept)
    if piv["No due date"].abs().sum() == 0:
        piv = piv.drop(columns="No due date")
    piv = piv.reset_index().rename(columns={"account_name": name_header})

    if piv.empty:
        st.info("No accounts in this group.")
        return

    # Flag accounts with NO credit terms (credit_days null or <= 0 on all refs).
    # When a default is applied, accounts that received it are NOT flagged.
    cd_max = (r.assign(_cd=pd.to_numeric(r["credit_days"], errors="coerce"))
              .groupby("account_name")["_cd"].max())
    no_credit = cd_max.isna() | (cd_max <= 0)
    if default_credit_days is not None:
        no_credit = no_credit & False  # every missing account received the default
    piv["_no_credit"] = piv[name_header].map(no_credit).fillna(False)
    piv["_ov_tip"] = piv[name_header].map(ov_tips).fillna("")  # Overdue hover tooltip

    # Due-date basis per account: "Actual" (DB due date), "Default" (default
    # credit-days applied because DB credit_days was 0/null), or "Mixed" (both).
    BASIS_COL = "Due date basis"
    if default_credit_days is not None:
        basis_by_acct = {}
        for acct_name, sub_r in r.groupby("account_name"):
            cd = pd.to_numeric(sub_r["credit_days"], errors="coerce")
            missing = cd.isna() | (cd <= 0)
            if not missing.any():
                basis_by_acct[acct_name] = "Actual"
            elif missing.all():
                basis_by_acct[acct_name] = "Default"
            else:
                basis_by_acct[acct_name] = "Mixed"
        piv[BASIS_COL] = piv[name_header].map(basis_by_acct).fillna("Actual")

    num_cols = [c for c in piv.columns
                if c not in (name_header, "_no_credit", "_ov_tip", BASIS_COL)]
    gb = GridOptionsBuilder.from_dataframe(piv)
    gb.configure_default_column(
        filter=True, floatingFilter=True, sortable=True, resizable=True,
        flex=1,  # stretch columns to fill width (no empty gap on the right)
        filterParams={"buttons": ["clear"]},
        cellStyle={"textAlign": "center"},  # centre values (OEM-table format)
    )
    inr_fmt = JsCode(
        "function(p){return p.value==null?'':Number(p.value)"
        ".toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});}"
    )
    for c in num_cols:
        gb.configure_column(c, type=["numericColumn"],
                            filter="agNumberColumnFilter", valueFormatter=inr_fmt)
    # Overdue cell: on hover show a day-range breakdown tooltip.
    if "Overdue" in num_cols:
        gb.configure_column(
            "Overdue", type=["numericColumn"], filter="agNumberColumnFilter",
            valueFormatter=inr_fmt, tooltipField="_ov_tip",
            tooltipComponent=_overdue_tooltip_component(),
        )
    gb.configure_column(
        name_header, minWidth=240, pinned="left",
        filter="agTextColumnFilter",
        filterParams={"filterOptions": ["contains"], "defaultOption": "contains",
                      "maxNumConditions": 1, "buttons": ["clear"]},
    )
    gb.configure_column("Total", sort=("desc" if sign == "receivable" else "asc"))
    gb.configure_column("_no_credit", hide=True)  # hidden flag drives row colour
    gb.configure_column("_ov_tip", hide=True)
    # Due-date basis column — colour-coded text (Actual / Default / Mixed).
    if default_credit_days is not None:
        gb.configure_column(
            BASIS_COL, maxWidth=160, filter="agTextColumnFilter",
            cellStyle=JsCode(
                "function(p){ var s={'textAlign':'center'}; "
                " if(!p.value) return s; "
                " if(p.value=='Default'){ s.color='#4aa3ff'; s.fontWeight='600'; } "
                " else if(p.value=='Mixed'){ s.color='#d29922'; s.fontWeight='600'; } "
                " else { s.color='#3fb950'; } "
                " return s; }"),
        )
    grid_options = gb.build()
    # Allow selecting & copying cell text (e.g. the account name)
    grid_options["enableCellTextSelection"] = True
    grid_options["ensureDomOrder"] = True
    _enable_overdue_tooltip(grid_options)

    total_row = {name_header: f"TOTAL  ({len(piv):,} {unit})"}
    for c in num_cols:
        total_row[c] = float(piv[c].sum())
    if default_credit_days is not None:
        total_row[BASIS_COL] = ""
    grid_options["pinnedTopRowData"] = [total_row]
    # Pinned TOTAL row = grey/bold; accounts with no credit terms = amber highlight
    grid_options["getRowStyle"] = JsCode(
        "function(p){"
        " if(p.node.rowPinned){ return {'fontWeight':'700','background':'rgba(120,120,120,0.18)'}; }"
        " if(p.data && p.data._no_credit){ return {'background':'rgba(255,193,7,0.22)'}; }"
        "}"
    )

    metrics_box = st.container()
    AgGrid(
        piv,
        gridOptions=grid_options,
        height=460,
        theme="streamlit",
        allow_unsafe_jscode=True,
        fit_columns_on_grid_load=True,
        update_on=["filterChanged", "sortChanged"],
        data_return_mode="filtered_and_sorted",
        custom_css=AG_DARK_CSS,
        key=f"aging_grid_{key}",
    )
    with metrics_box:
        due_soon_cols = [c for c in future_cols if c in piv.columns]
        n_no_credit = int(piv["_no_credit"].sum())
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(unit.capitalize(), f"{len(piv):,}")
        m2.metric("Total Net Outstanding", inr(piv["Total"].sum()))
        m3.metric("Overdue (past due)", inr(piv["Overdue"].sum()))
        m4.metric("Coming due (next)", inr(piv[due_soon_cols].sum().sum()))
        if default_credit_days is not None:
            n_default = int((piv[BASIS_COL] != "Actual").sum())
            st.caption(
                f"🔵 **{n_default}** account(s) had no DB `credit_days`, so a default of "
                f"**{default_credit_days} day(s)** (due date = ref date + {default_credit_days}) "
                "was applied to place their amounts in the right aging bucket. "
                "The **Due date basis** column shows *Actual* / *Default* / *Mixed*."
            )
        elif n_no_credit:
            st.caption(
                f"🟡 **{n_no_credit}** account(s) highlighted amber have **no credit_days** "
                "set — their due date can't be derived, so amounts fall into *Overdue*. "
                "Set credit_days for these accounts."
            )

    st.download_button(
        "⬇️ Download aging ledger (CSV)",
        piv.drop(columns=["_no_credit", "_ov_tip"]).to_csv(index=False).encode("utf-8"),
        file_name=f"aging_ledger_{key}.csv",
        mime="text/csv",
        key=f"dl_aging_{key}",
    )


# Day-range bins for the Overdue popup breakdown: (lower-exclusive, upper-inclusive, label)
OVERDUE_POPUP_BINS = [
    (0, 5, "Last 5 days"), (5, 10, "5-10 days"), (10, 20, "10-20 days"),
    (20, 30, "20-30 days"), (30, 60, "30-60 days"), (60, None, "60+ days"),
]


def _overdue_breakdown(sub: pd.DataFrame) -> pd.DataFrame:
    """Split an account's OVERDUE references (_dov > 0) into day-range buckets."""
    out = []
    for lo, hi, lbl in OVERDUE_POPUP_BINS:
        m = sub["_dov"] > lo if hi is None else ((sub["_dov"] > lo) & (sub["_dov"] <= hi))
        out.append({"Aging": lbl,
                    "Amount": float(sub.loc[m, "bal_amount"].sum()),
                    "Refs": int(m.sum())})
    return pd.DataFrame(out)


def _overdue_tip_by_account(r: pd.DataFrame) -> dict:
    """Map account_name -> HTML tooltip with its overdue day-range breakdown.

    Requires columns _dov (days overdue), bal_amount, account_name on `r`.
    """
    tips = {}
    for acct_name, sub_r in r[r["_dov"] > 0].groupby("account_name"):
        bd = _overdue_breakdown(sub_r)
        parts = [f"{b.Aging}: {inr(b.Amount)} ({b.Refs})"
                 for b in bd.itertuples() if b.Amount]
        if parts:
            tips[acct_name] = ("<b>Overdue by age</b><br>" + "<br>".join(parts)
                               + f"<br><b>Total: {inr(bd['Amount'].sum())}</b>")
    return tips


def _overdue_tooltip_component() -> JsCode:
    """ag-grid tooltip component that renders the HTML breakdown string."""
    return JsCode(
        "class {"
        " init(p){"
        "  this.eGui=document.createElement('div');"
        "  this.eGui.innerHTML=p.value||'';"
        "  this.eGui.style.cssText='background:#1e2530;color:#fff;padding:8px 10px;"
        "border:1px solid #4aa3ff;border-radius:6px;font-size:12px;line-height:1.5;"
        "box-shadow:0 2px 8px rgba(0,0,0,0.4);';"
        " }"
        " getGui(){ return this.eGui; }"
        "}"
    )


def _enable_overdue_tooltip(grid_options: dict) -> None:
    """Apply the tooltip show/hide timing so the popup stays while hovering."""
    grid_options["tooltipShowDelay"] = 150
    grid_options["tooltipHideDelay"] = 600000  # stays visible while cursor is on the cell
    grid_options["tooltipInteraction"] = True


def render_grouped_aging_ledger(rows: pd.DataFrame, key: str, group_map,
                                bins=RECEIVABLE_BINS, sign: str = "receivable",
                                name_header: str = "Account Name",
                                apply_defaults: bool = False,
                                default_credit_days: int = None,
                                group_label: str = "OEM",
                                overdue_popup: bool = False) -> None:
    """Aging ledger that lists ACTUAL account names, ordered by their group, with
    a bold subtotal row ("<group> - Total") inserted after each group's accounts.

    group_map : callable(account_name) -> group label used only for ordering and
                the subtotal rows; individual account rows keep their real names.
    apply_defaults : when True, rows whose DB credit_days is zero/null get a
                default due date (ref_date + OEM default credit-days) so their
                amounts land in the right aging bucket instead of Overdue.
    default_credit_days : flat fallback (e.g. 7) applied to EVERY row whose DB
                credit_days is zero/null — used for non-OEM groups that have no
                per-group default. Takes effect only when apply_defaults is False.
    """
    future_cols = [lbl for lbl, _ in bins]
    r = rows.copy()
    r["_grp"] = r["account_name"].fillna("").map(group_map)
    if apply_defaults:
        cd = pd.to_numeric(r["credit_days"], errors="coerce")
        missing = cd.isna() | (cd <= 0)
        dflt = r["account_name"].map(_oem_default_credit_days)
        use = missing & dflt.notna()
        if use.any():
            r.loc[use, "due_date"] = (
                r.loc[use, "ref_date"]
                + pd.to_timedelta(dflt[use].astype(float), unit="D")
            )
    elif default_credit_days is not None:
        cd = pd.to_numeric(r["credit_days"], errors="coerce")
        missing = cd.isna() | (cd <= 0)
        if missing.any():
            r.loc[missing, "due_date"] = (
                r.loc[missing, "ref_date"]
                + pd.to_timedelta(float(default_credit_days), unit="D")
            )
    r["_bkt"] = (r["due_date"] - TODAY).dt.days.apply(_make_bucketer(bins))
    full_order = ["Overdue"] + future_cols + ["No due date"]
    piv = (
        r.pivot_table(index=["_grp", "account_name"], columns="_bkt",
                      values="bal_amount", aggfunc="sum", fill_value=0.0)
        .reindex(columns=full_order, fill_value=0.0)
    )
    piv["Total"] = piv.sum(axis=1)
    # Keep the requested side + hide near-zero accounts
    if sign == "receivable":
        piv = piv[piv["Total"] > 100]
    else:
        piv = piv[piv["Total"] < -100]
    if piv["No due date"].abs().sum() == 0:
        piv = piv.drop(columns="No due date")
    piv = piv.reset_index()

    if piv.empty:
        st.info("No accounts in this group.")
        return

    num_cols = [c for c in piv.columns if c not in ("_grp", "account_name")]
    asc = (sign != "receivable")  # receivables largest first; payables most-negative first

    # Flag accounts with NO credit terms (credit_days null or <= 0 on all refs).
    # When defaults apply, accounts that received a default are NOT flagged.
    cd_max = (r.assign(_cd=pd.to_numeric(r["credit_days"], errors="coerce"))
              .groupby("account_name")["_cd"].max())
    no_credit = cd_max.isna() | (cd_max <= 0)
    if apply_defaults:
        has_default = pd.Series(no_credit.index, index=no_credit.index).map(
            lambda n: _oem_default_credit_days(n) is not None)
        no_credit = no_credit & ~has_default
    elif default_credit_days is not None:
        no_credit = no_credit & False  # every missing account received the flat default

    # Days overdue per ref (effective due date, after any defaults) — for tooltip.
    r["_dov"] = (TODAY - r["due_date"]).dt.days

    # Due-date basis per account: "Actual" (DB due date), "Default" (default
    # credit-days applied because DB credit_days was 0/null), or "Mixed" (both).
    BASIS_COL = "Due date basis"
    basis_by_acct = {}
    for acct_name, sub_r in r.groupby("account_name"):
        cd = pd.to_numeric(sub_r["credit_days"], errors="coerce")
        missing = cd.isna() | (cd <= 0)
        if apply_defaults:
            has_def = _oem_default_credit_days(acct_name) is not None
        else:
            has_def = default_credit_days is not None
        used_default = missing & has_def
        if not used_default.any():
            basis_by_acct[acct_name] = "Actual"
        elif used_default.all():
            basis_by_acct[acct_name] = "Default"
        else:
            basis_by_acct[acct_name] = "Mixed"

    # Precompute the Overdue hover-tooltip (day-range breakdown) per account.
    tip_by_acct = _overdue_tip_by_account(r)

    # Build the display frame: account rows per group, then a subtotal row.
    grp_order = piv.groupby("_grp")["Total"].sum().sort_values(ascending=asc).index
    display_rows = []
    for g in grp_order:
        sub = piv[piv["_grp"] == g].sort_values("Total", ascending=asc)
        for _, row in sub.iterrows():
            acct = row["account_name"]
            d = {name_header: acct, "_is_total": False,
                 "_no_credit": bool(no_credit.get(acct, False)),
                 "_ov_tip": tip_by_acct.get(acct, "")}
            d.update({c: float(row[c]) for c in num_cols})
            d[BASIS_COL] = basis_by_acct.get(acct, "Actual")
            display_rows.append(d)
        d = {name_header: f"{g} - Total", "_is_total": True, "_no_credit": False,
             "_ov_tip": ""}
        d.update({c: float(sub[c].sum()) for c in num_cols})
        d[BASIS_COL] = ""
        display_rows.append(d)
    disp = pd.DataFrame(display_rows)

    gb = GridOptionsBuilder.from_dataframe(disp)
    # Sorting/filtering would scramble the group + subtotal layout, so disable them.
    # Centre every cell's value (headers are centred via custom_css below).
    gb.configure_default_column(filter=False, sortable=False, resizable=True,
                                flex=1,  # stretch columns to fill width (no gap)
                                cellStyle={"textAlign": "center"})
    inr_fmt = JsCode(
        "function(p){return p.value==null?'':Number(p.value)"
        ".toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});}"
    )
    for c in num_cols:
        gb.configure_column(c, type=["numericColumn"], valueFormatter=inr_fmt)
    # Overdue cell: on hover show a day-range breakdown tooltip (no click needed).
    if overdue_popup and "Overdue" in num_cols:
        gb.configure_column(
            "Overdue", type=["numericColumn"], valueFormatter=inr_fmt,
            tooltipField="_ov_tip", tooltipComponent=_overdue_tooltip_component(),
        )
    gb.configure_column("_ov_tip", hide=True)
    # Due-date basis column at the end — colour-coded text (Actual/Default/Mixed),
    # centred header + values.
    gb.configure_column(
        BASIS_COL, maxWidth=160,
        headerClass="ag-center-header",
        cellStyle=JsCode(
            "function(p){ var s={'textAlign':'center'}; "
            " if(!p.value) return s; "
            " if(p.value=='Default'){ s.color='#4aa3ff'; s.fontWeight='600'; } "
            " else if(p.value=='Mixed'){ s.color='#d29922'; s.fontWeight='600'; } "
            " else { s.color='#3fb950'; } "
            " return s; }"),
    )
    gb.configure_column(name_header, minWidth=280, pinned="left")
    gb.configure_column("_is_total", hide=True)
    gb.configure_column("_no_credit", hide=True)
    grid_options = gb.build()
    grid_options["enableCellTextSelection"] = True
    grid_options["ensureDomOrder"] = True

    # Grand total (account rows only, not the subtotal rows) pinned at the top.
    n_grp = int(piv["_grp"].nunique())
    n_acct = int(len(piv))
    total_row = {name_header: f"TOTAL  ({n_grp} {group_label} groups · {n_acct} accounts)"}
    total_row.update({c: float(piv[c].sum()) for c in num_cols})
    total_row[BASIS_COL] = ""
    grid_options["pinnedTopRowData"] = [total_row]
    # Pinned grand-total row = grey/bold; per-group subtotal rows = blue/bold;
    # accounts with no credit_days = amber (same as the other ledgers).
    grid_options["getRowStyle"] = JsCode(
        "function(p){"
        " if(p.node.rowPinned){ return {'fontWeight':'700','background':'rgba(120,120,120,0.18)'}; }"
        " if(p.data && p.data._is_total){ return {'fontWeight':'700','background':'rgba(56,139,253,0.30)'}; }"
        " if(p.data && p.data._no_credit){ return {'background':'rgba(255,193,7,0.22)'}; }"
        "}"
    )

    if overdue_popup:
        _enable_overdue_tooltip(grid_options)

    AgGrid(
        disp,
        gridOptions=grid_options,
        height=560,
        theme="streamlit",
        allow_unsafe_jscode=True,
        fit_columns_on_grid_load=True,
        custom_css=AG_DARK_CSS,
        key=f"grouped_aging_grid_{key}",
    )

    if overdue_popup:
        st.caption("💡 Hover an **Overdue** amount to see its day-range breakdown.")

    m1, m2, m3 = st.columns(3)
    m1.metric(f"{group_label} groups", f"{n_grp:,}")
    m2.metric("Accounts", f"{n_acct:,}")
    m3.metric("Total Net Outstanding", inr(piv["Total"].sum()))
    n_no_credit = int(disp.loc[~disp["_is_total"], "_no_credit"].sum())
    if default_credit_days is not None:
        n_default = int((disp.loc[~disp["_is_total"], BASIS_COL] != "Actual").sum())
        st.caption(
            f"🔵 **{n_default}** account(s) had no DB `credit_days`, so a default of "
            f"**{default_credit_days} day(s)** was applied. "
            "🔵 Blue rows are per-group subtotals."
        )
    elif n_no_credit:
        st.caption(
            f"🟡 **{n_no_credit}** account(s) highlighted amber have **no credit_days** "
            "set — their due date can't be derived, so amounts fall into *Overdue*. "
            "🔵 Blue rows are per-group subtotals."
        )

    st.download_button(
        "⬇️ Download grouped aging ledger (CSV)",
        disp.drop(columns=["_is_total", "_no_credit"]).to_csv(index=False).encode("utf-8"),
        file_name=f"grouped_aging_ledger_{key}.csv",
        mime="text/csv",
        key=f"dl_grouped_aging_{key}",
    )


def aging_summary_frame(rows: pd.DataFrame, bins=RECEIVABLE_BINS,
                        default_credit_days: int = 7):
    """Compute the aging summary used by both the summary table and the aging
    chart. Returns (frame, bucket_cols) where frame has one row per Type
    (Payables / Receivables / Net balance) and columns = bucket_cols + Total.
    A default credit-days fallback places accounts with no DB credit_days in the
    right bucket instead of Overdue."""
    future_cols = [lbl for lbl, _ in bins]
    r = rows.copy()
    if default_credit_days is not None:
        cd = pd.to_numeric(r["credit_days"], errors="coerce")
        missing = cd.isna() | (cd <= 0)
        if missing.any():
            r.loc[missing, "due_date"] = (
                r.loc[missing, "ref_date"]
                + pd.to_timedelta(float(default_credit_days), unit="D")
            )
    r["_bkt"] = (r["due_date"] - TODAY).dt.days.apply(_make_bucketer(bins))
    full_order = ["Overdue"] + future_cols + ["No due date"]
    piv = (
        r.pivot_table(index="account_name", columns="_bkt",
                      values="bal_amount", aggfunc="sum", fill_value=0.0)
        .reindex(columns=full_order, fill_value=0.0)
    )
    piv["Total"] = piv.sum(axis=1)  # includes No due date, for side classification
    bucket_cols = ["Overdue"] + future_cols
    pay = piv[piv["Total"] < -100]   # net payable accounts
    rec = piv[piv["Total"] > 100]    # net receivable accounts

    def _side(sub, label):
        d = {"Type": label}
        d.update({c: float(sub[c].sum()) for c in bucket_cols})
        d["Total"] = float(sub[bucket_cols].sum().sum())
        return d

    pay_row = _side(pay, "Payables (we owe)")
    rec_row = _side(rec, "Receivables (owed to us)")
    net_row = {"Type": "Net balance"}
    for c in bucket_cols + ["Total"]:
        net_row[c] = pay_row[c] + rec_row[c]
    return pd.DataFrame([pay_row, rec_row, net_row]), bucket_cols


def render_aging_summary(rows: pd.DataFrame, key: str, bins=RECEIVABLE_BINS,
                         default_credit_days: int = 7) -> None:
    """Compact aging summary: one row for Payables and one for Receivables, split
    into the same due-date buckets (Overdue, Next 0-7, … , Total). Amounts are the
    per-account net; a default credit-days fallback is applied so accounts with no
    DB credit_days land in the right bucket instead of Overdue."""
    disp, bucket_cols = aging_summary_frame(rows, bins, default_credit_days)
    num_cols = bucket_cols + ["Total"]

    gb = GridOptionsBuilder.from_dataframe(disp)
    gb.configure_default_column(filter=False, sortable=False, resizable=True,
                                flex=1,  # stretch columns to fill width (no gap)
                                cellStyle={"textAlign": "center"})
    inr_fmt = JsCode(
        "function(p){return p.value==null?'':Math.round(Number(p.value))"
        ".toLocaleString('en-IN',{minimumFractionDigits:0,maximumFractionDigits:0});}"
    )
    for c in num_cols:
        gb.configure_column(c, type=["numericColumn"], valueFormatter=inr_fmt)
    gb.configure_column("Type", minWidth=220, pinned="left",
                        cellStyle={"textAlign": "left", "fontWeight": "600"})
    gb.configure_column("Total", cellStyle={"textAlign": "center", "fontWeight": "700"})
    grid_options = gb.build()
    grid_options["enableCellTextSelection"] = True
    # Payables row tinted red, Receivables row tinted green.
    grid_options["getRowStyle"] = JsCode(
        "function(p){"
        " if(p.data && p.data.Type && p.data.Type.indexOf('Payable')===0){"
        "   return {'background':'rgba(248,81,73,0.12)'}; }"
        " if(p.data && p.data.Type && p.data.Type.indexOf('Receivable')===0){"
        "   return {'background':'rgba(63,185,80,0.12)'}; }"
        " if(p.data && p.data.Type==='Net balance'){"
        "   return {'fontWeight':'700','background':'rgba(120,120,120,0.22)'}; }"
        "}"
    )
    AgGrid(
        disp,
        gridOptions=grid_options,
        height=150,
        theme="streamlit",
        allow_unsafe_jscode=True,
        fit_columns_on_grid_load=True,
        custom_css=AG_DARK_CSS,
        key=f"aging_summary_{key}",
    )


# Receivable accounts to pull OUT of the main Receivables table into a separate
# table below it (explicit user-supplied list). Matched case-insensitively.
RECEIVABLE_SPLIT_ACCOUNTS = [
    "Ranjeet Singh Logistics", "LOHR INDIA AUTOMOTIVE PRIVATE LIMITED",
    "Trolltech Engineering Works Reg", "NORTHRUN TECHNOLOGY",
    "Ajay Kumar Chaudhary Guide", "Suman Prasad", "SHRI BALAKNATH TRADING CO.",
    "SRI HARSHA TRUCKING PRIVATE LIMITED",
    "AUTOBAHN TRUCKING CORPORATION PRIVATE LIMITED", "AVON BRIGHT STEEL BARS",
    "Mahindra Logistics Advance", "Swaraj-DEF",
    "Shree Gajanan Car Carrier Denting Welding workshop",
    "Truckinzy Infotech Private Limited", "SWAYAMBHU VAHAN SEWA PVT LTD.",
    "PPS Motors Private Limited 23AA", "Manoj Kumar Mechanic",
    "Parnitha Automobiles", "Sri Mookambika Towing Service",
    "GO DIGIT GENERAL INSURANCE LIMITED", "KAPOOR TRADING CO.",
    "KATARIA MOTORS PRIVATE LIMITED", "PRAKASH BATTERY AGENCY", "RBMLPump",
    "MAA NARMADA AUTO MOBILES", "Mynd Solutions Pvt. Ltd.",
    "BINAY MOTORS PRIVATE LIMITED", "RAJNISH KUMAR", "SHREYASH SAFETY SHOES",
    "Receivables Exchange of India Limited", "PRABAL MOTORS PRIVATE LIMITED 33",
    "VIVEK AUTOMOBILES", "PRABAL MOTORS PRIVATE LIMITED", "Mehatab Garage",
    "M S AUTO INDUSTRIES", "GAUTAM TRUCKING PRIVATE LIMITED", "STRONG WINGS LLP",
    "AML MOTORS PRIVATE LIMITED", "SHREE SAINATH AUTO ELECTRIC",
    "Geeta Automobiles", "NAVKAR MOTORS", "INDIAN ENTERPRISE", "SDL MOTORS",
    "SRI RAMADAS MOTOR TRANSPORT LTD", "PUNYA AUTOWHEELS PRIVATE LIMITED",
    "PREM MOTORS INDIA LLP",
    "INNOVATIVE INFRA AND MINING SOLUTIONS LIMITED",
    "Satya Trucking Private Limited", "MOTOR TRADE CENTRE", "SRI S.A.T. MOTORS",
    "SAHIMA MOTORS", "ANSARI AUTO CARE", "H.P. Motors", "Shri Shivshakti Motors",
    "SUMITRA MOTORS", "GOKUL AUTOMOBILES", "Two Wheeler Mechanic",
    "KAMAL COMMERCIAL VEHICLES PRIVATE LIMITED", "RNS EARTHMOVERS PRIVATE LIMITED",
    "SHRI DEVNARAYAN AUTO PARTS", "SINARE AUTOMOTIVE", "PRAGATI MOTORS",
    "PPS MOTORS PRIVATE LIMITED 09AAFCP8182N1ZM", "Friends Auto Garrage",
    "RAJESH AND SONS", "G R MOTORS", "MARBLE MOTORS", "ASHIRVAD MOTORS",
    "BALAJI TRADERS PAVANESH", "AAYUSH MOTORS", "MAARS EQUIPMENTS INDUSTRIES",
    "MAC VEHICLES PRIVATE LIMITED", "SWAGAT AUTOMOTIVE", "M.N. AGENCY",
    "SWASTIK AUTO MOBIL 10PODPS7359L1Z6",
    "KAMAL COMMERCIAL VEHICLES PRIVATE LIMITED 08ZX", "SANWARIYA AUTO SERVICE",
    "Shyam Enterprise", "HARSHIT MOTORS", "JAI SAI AUTO CENTRE",
    "URD MOTORS PRIVATE LIMITED", "KALAHANU AUTOMOBILES PRIVATE LIMITED",
    "Triumphant Enterprises", "JAIKA AUTOMOBILES AND FINANCE PVT LTD",
    "KEKA TECHNOLOGIES PRIVATE LIMITED", "DHINGRA MOTORS PVT. LTD.06AABCD897",
    "Jai shyam baba fabricator", "GANESH AUTO MOBILES",
    "PPS MOTORS PRIVATE LIMITED 21AAFCP8182N1Z0", "Imran Auto Electric Works",
    "SHREENATH PETROLEUM", "TAPUKARA MOTORS", "Super Motors",
    "Uttrakhand Motor Workshop", "SIDHHI BATTERY AND AUTO PARTS",
    "Sainik Petrol Pump_HPCL", "PREMIER POWER ELECTRONICS",
    "SANTOSH KUMAR SPRAY PAINTER", "Sarvendra Guarantor",
    "V.S.T. MOTORS PRIVATE LIMITED", "MAHAKALI AUTOMOBILES PRIVATE LIMITED",
    "ANAND MOTOR SPARE PARTS", "PAL SVAM POWER SOLUTIONS PRIVATE LIMITED",
]
RECEIVABLE_SPLIT_NORM = {n.strip().lower() for n in RECEIVABLE_SPLIT_ACCOUNTS}

# Receivables tab: the main table shows ONLY accounts whose name contains one of
# these keywords (OEMs / key parties). Every other receivable account drops into
# the "Other accounts" table below. Matched case-insensitively as a substring.
RECEIVABLE_KEEP_KEYWORDS = [
    "MAHINDRA", "Toyota", "Glovis", "Tata", "Honda", "SKODA", "MG Motor",
    "VALUEDRIVE", "PURERIDE", "R.sai", "John Deere", "ESCORTS KUBOTA", "SAERA",
]
RECEIVABLE_KEEP_PATTERN = "|".join(re.escape(k) for k in RECEIVABLE_KEEP_KEYWORDS)


def _oem_group(name: str) -> str:
    """Map an account name to its OEM keyword group (first keyword it contains)."""
    low = str(name).lower()
    for kw in RECEIVABLE_KEEP_KEYWORDS:
        if kw.lower() in low:
            return kw
    return "Other"


# Default credit_days for the OEM Accounts table ONLY, used when the DB
# credit_days is zero/null. Keyed by OEM group; a per-account entry (exact name,
# lower-cased) overrides the group default. Due date = ref_date + these days.
OEM_DEFAULT_CREDIT_DAYS = {           # by OEM group keyword
    "MAHINDRA": 18, "Tata": 7, "VALUEDRIVE": 15, "PURERIDE": 15, "Toyota": 10,
}
OEM_DEFAULT_CREDIT_DAYS_BY_ACCOUNT = {  # exact account name overrides group
    "glovis india pvt ltd - pune": 14,
}


def _oem_default_credit_days(name):
    """Default credit-days for an account, or None if none is configured."""
    low = str(name).strip().lower()
    if low in OEM_DEFAULT_CREDIT_DAYS_BY_ACCOUNT:
        return OEM_DEFAULT_CREDIT_DAYS_BY_ACCOUNT[low]
    return OEM_DEFAULT_CREDIT_DAYS.get(_oem_group(name))

tab_all, tab_pay, tab_rec, tab_ctrl, tab_enroute = st.tabs(
    ["All accounts", "Payables only (we owe)",
     "Receivables only (owed to us)", "Control-AC & others", "Enroute Vendors"]
)
with tab_pay:
    st.caption(
        "Net Outstanding split into due-date aging buckets — days overdue "
        f"(today {TODAY:%d/%m/%Y} − due_date). **Pump Vendors** = accounts with an "
        "oil-company code (IOCL/BPCL/HPCL). Fuel/pump accounts **without** a code, "
        "and control/slip/PUC accounts, are in the **Control-AC & others** tab."
    )
    names = led["account_name"].fillna("")
    has_code = names.str.contains(CODE_PATTERN, case=False, regex=True)
    # Exclude accounts that belong to a dedicated tab (Enroute, Control-AC & others).
    excl = _is_dedicated(led)

    # Pump Vendors = carries an oil-company code (IOCL/BPCL/HPCL).
    pump_mask = has_code & ~excl
    # Vendors = plain trade accounts (no code), not in a dedicated tab.
    vendor_mask = ~has_code & ~excl

    st.markdown("### ⛽ Pump Vendors")
    st.caption("Grouped by oil company (BPCL / IOCL / HPCL) with a subtotal row per group.")
    render_grouped_aging_ledger(led[pump_mask], "payables_pump",
                                group_map=_pump_group, bins=PAYABLE_BINS,
                                sign="payable", name_header="Account Name",
                                default_credit_days=7, group_label="Oil company",
                                overdue_popup=True)

    st.divider()

    st.markdown("### 🏢 Vendors (non-pump)")
    render_aging_ledger(led[vendor_mask], "payables_vendor", default_credit_days=7)

with tab_rec:
    st.caption(
        "Net Outstanding split into due-date aging buckets — Overdue (past due) "
        f"plus amounts coming due in the next windows (today {TODAY:%d/%m/%Y})."
    )
    # Exclude Enroute vendors and Control-AC & others (they have their own tabs).
    excl = _is_dedicated(led)
    # Main table = accounts whose name contains one of the keep-keywords.
    is_keep = led["account_name"].fillna("").str.contains(
        RECEIVABLE_KEEP_PATTERN, case=False, regex=True)

    st.markdown("### 📥 OEM Accounts")
    st.caption("Grouped by OEM: " + ", ".join(RECEIVABLE_KEEP_KEYWORDS))
    st.caption(
        "Default credit-days (used only where DB credit_days is 0/null) — "
        "MAHINDRA: 18, Glovis India Pvt Ltd - Pune: 14, Tata: 7, "
        "VALUEDRIVE: 15, PURERIDE: 15, Toyota: 10. Rows with a valid DB "
        "credit_days keep their actual due date. The **Due date basis** column "
        "shows 🟢 Actual (DB) · 🔵 Default · 🟠 Mixed."
    )
    render_grouped_aging_ledger(led[is_keep & ~excl], "receivables",
                                group_map=_oem_group, bins=RECEIVABLE_BINS,
                                sign="receivable", name_header="Account Name",
                                apply_defaults=True, overdue_popup=True)

    st.divider()

    st.markdown("### 📋 Other then OEM Accounts")
    render_aging_ledger(led[~is_keep & ~excl], "receivables_split",
                        bins=RECEIVABLE_BINS, sign="receivable",
                        default_credit_days=7)

with tab_ctrl:
    st.caption(
        "Control ledger / slip / PUC accounts, plus fuel/pump accounts that do "
        "NOT carry an oil-company code (IOCL/BPCL/HPCL). Split into due-date aging "
        f"buckets — days overdue (today {TODAY:%d/%m/%Y} − due_date)."
    )
    # Control/slip/PUC + fuel-named accounts lacking an oil-company code, minus
    # any that are in the Enroute-vendors list (those have their own tab).
    control_mask = _is_control_others(led) & ~_is_enroute(led)
    render_aging_ledger(led[control_mask], "control_others")

with tab_enroute:
    st.caption(
        "Enroute vendors (custom list from `enroute_vendors.txt`). These accounts "
        "appear ONLY here — they are excluded from the Payables, Receivables and "
        "Control-AC & others tabs. Net Outstanding split into due-date aging "
        f"buckets (today {TODAY:%d/%m/%Y})."
    )
    enroute_rows = led[_is_enroute(led)]

    st.markdown("### 💸 Payables (we owe)")
    render_aging_ledger(enroute_rows, "enroute_payables",
                        bins=PAYABLE_BINS, sign="payable")

    st.divider()

    st.markdown("### 📥 Receivables (owed to us)")
    render_aging_ledger(enroute_rows, "enroute_receivables",
                        bins=RECEIVABLE_BINS, sign="receivable")

with tab_all:
    st.markdown("### 📊 Aging summary — Payables vs Receivables")
    st.caption(
        "Net outstanding split into due-date buckets — Overdue (past due) and amounts "
        f"coming due next (today {TODAY:%d/%m/%Y}). Accounts with no DB `credit_days` "
        "use a 7-day default. Excludes Control-AC & Enroute accounts."
    )
    render_aging_summary(led[~_is_dedicated(led)], "all", bins=RECEIVABLE_BINS)

    st.divider()

    render_ledger(acct_base, "all", "all")

    # Charts / detail below also exclude accounts that live in a dedicated tab
    # (Enroute vendors, Control-AC & others), to match the ledger above.
    df = df[~_is_dedicated(df)]

    st.divider()

    # ----------------------------------------------------------------------- #
    # Charts row 1
    # ----------------------------------------------------------------------- #
    left, right = st.columns(2)

    with left:
        st.subheader("Outstanding by office")
        g = (
            df.groupby("office", dropna=False)["outstanding"].sum()
            .reset_index().sort_values("outstanding", ascending=False).head(15)
        )
        fig = px.bar(g, x="outstanding", y="office", orientation="h", text_auto=".2s")
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=420, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    with right:
        st.subheader("Aging of outstanding")
        st.caption("Same buckets as the aging summary — Payables (below 0), "
                   "Receivables (above 0) and Net balance, with the 7-day default.")
        summ, bucket_cols = aging_summary_frame(led[~_is_dedicated(led)],
                                                bins=RECEIVABLE_BINS)
        long = summ.melt(id_vars="Type", value_vars=bucket_cols,
                         var_name="Bucket", value_name="Amount")
        fig = px.bar(
            long, x="Bucket", y="Amount", color="Type", barmode="group",
            text_auto=".2s",
            category_orders={"Bucket": bucket_cols,
                             "Type": ["Payables (we owe)",
                                      "Receivables (owed to us)", "Net balance"]},
            color_discrete_map={"Payables (we owe)": "#f85149",
                                "Receivables (owed to us)": "#3fb950",
                                "Net balance": "#8b949e"},
        )
        fig.update_layout(height=420, xaxis_title="", legend_title="",
                          legend=dict(orientation="h", y=1.12),
                          margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    # ----------------------------------------------------------------------- #
    # Charts row 2
    # ----------------------------------------------------------------------- #
    left, right = st.columns([1, 1])

    with left:
        st.subheader("Split by category & type")
        g = df.groupby(["category", "ref_type"])["outstanding"].sum().reset_index()
        fig = px.sunburst(g, path=["category", "ref_type"], values="outstanding")
        fig.update_layout(height=420, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    with right:
        st.subheader("Monthly reference amount trend")
        t = df.dropna(subset=["ref_date"]).copy()
        t["month"] = t["ref_date"].dt.to_period("M").dt.to_timestamp()
        g = t.groupby(["month", "category"])["ref_amount"].sum().reset_index()
        fig = px.line(g, x="month", y="ref_amount", color="category", markers=True)
        fig.update_layout(height=420, xaxis_title="", legend_title="", margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ----------------------------------------------------------------------- #
    # Top parties
    # ----------------------------------------------------------------------- #
    st.subheader("Top parties by outstanding")
    tcol1, tcol2 = st.columns([1, 3])
    topn = tcol1.slider("Show top N", 5, 30, 10)
    top = (
        df.groupby("account_name")
        .agg(outstanding=("outstanding", "sum"),
             net=("bal_amount", "sum"),
             items=("invoice_ref_id", "count"),
             overdue=("is_overdue", "sum"))
        .reset_index().sort_values("outstanding", ascending=False).head(topn)
    )
    fig = px.bar(top.sort_values("outstanding"), x="outstanding", y="account_name",
                 orientation="h", text_auto=".2s", hover_data=["items", "overdue"])
    fig.update_layout(height=max(320, topn * 26), yaxis_title="", xaxis_title="Outstanding",
                      margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)

    # ----------------------------------------------------------------------- #
    # Detail table
    # ----------------------------------------------------------------------- #
    st.subheader("Reference detail")
    search = st.text_input("Search party / reference no / narration", "")
    show = df.copy()
    if search:
        s = search.lower()
        mask = (
            show["account_name"].fillna("").str.lower().str.contains(s)
            | show["ref_no"].fillna("").str.lower().str.contains(s)
            | show["narration"].fillna("").str.lower().str.contains(s)
        )
        show = show[mask]

    cols = ["ref_no", "ref_type", "category", "account_name", "office", "division_name",
            "ref_date", "due_date", "days_overdue", "aging_bucket",
            "ref_amount", "amount_paid", "bal_amount", "is_active"]
    st.dataframe(
        show[cols].sort_values("bal_amount", key=lambda x: x.abs(), ascending=False),
        use_container_width=True, height=430, hide_index=True,
    )
    st.caption(f"{len(show):,} rows shown")

    st.download_button(
        "⬇️ Download filtered data (CSV)",
        show[cols].to_csv(index=False).encode("utf-8"),
        file_name="swift_party_ref_filtered.csv",
        mime="text/csv",
    )
