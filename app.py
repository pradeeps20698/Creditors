"""
Swift Party Reference — Creditors / Receivables Dashboard
Streamlit dashboard built on the `swift_party_ref` table (AWS RDS Postgres).

Run:  streamlit run app.py   ->  http://localhost:8501/
"""
import os
from datetime import datetime

import pandas as pd
import streamlit as st
import plotly.express as px
from dotenv import load_dotenv
from sqlalchemy import create_engine
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode

load_dotenv()

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="Swift Party Reference Dashboard",
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

st.title("📊 Swift Party Reference Dashboard")
st.caption(
    f"Source: `swift_party_ref` · {len(data):,} reference rows · "
    f"data as of {TODAY:%d %b %Y}"
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
# KPI row  —  NET, account-level (matches the Account-wise Ledger below)
# --------------------------------------------------------------------------- #
# Net balance per account, then drop near-zero (-100..100), same rule as ledger
acct_net = df.groupby("account_name")["bal_amount"].sum()
acct_net = acct_net[(acct_net < -100) | (acct_net > 100)]
payables = acct_net[acct_net < 0].sum()      # negative net = we owe
receivables = acct_net[acct_net > 0].sum()   # positive net = owed to us
net = acct_net.sum()
overdue_amt = df[df["is_overdue"]]["outstanding"].sum()
n_parties = len(acct_net)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Payables (we owe)", inr(payables))
c2.metric("Receivables (owed to us)", inr(receivables))
c3.metric("Net balance", inr(net))
c4.metric("Overdue outstanding", inr(overdue_amt))
c5.metric("Accounts (net ≠ 0)", f"{n_parties:,}")

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

view = st.radio(
    "Show",
    ["All accounts", "Payables only (we owe)", "Receivables only (owed to us)"],
    horizontal=True, key="ledger_view",
)

acct = (
    led.groupby("account_name")
    .agg(bill_amount=("ref_amount", "sum"),
         amount_paid=("amount_paid", "sum"),
         net_outstanding=("bal_amount", "sum"),
         refs=("invoice_ref_id", "count"))
    .reset_index()
)
if view.startswith("Payables"):
    acct = acct[acct["net_outstanding"] < 0]
elif view.startswith("Receivables"):
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

# Interactive grid — per-column search lives INSIDE the header (floating filters)
gb = GridOptionsBuilder.from_dataframe(acct_display)
gb.configure_default_column(
    filter=True, floatingFilter=True, sortable=True, resizable=True,
    filterParams={"buttons": ["clear"]},
)
inr_fmt = JsCode(
    "function(p){return p.value==null?'':Number(p.value)"
    ".toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});}"
)
for col in ["Bill Amount", "Amount Paid / Received", "Net Outstanding"]:
    gb.configure_column(col, type=["numericColumn"],
                        filter="agNumberColumnFilter", valueFormatter=inr_fmt)
gb.configure_column("Account Name", minWidth=280)
gb.configure_column("Net Outstanding", sort="desc")  # default order
grid_options = gb.build()

# Pinned TOTAL row at the top of the grid
grid_options["pinnedTopRowData"] = [{
    "Account Name": f"TOTAL  ({len(acct_display):,} accounts)",
    "Bill Amount": float(acct_display["Bill Amount"].sum()),
    "Amount Paid / Received": float(acct_display["Amount Paid / Received"].sum()),
    "Net Outstanding": float(acct_display["Net Outstanding"].sum()),
    "Refs": int(acct_display["Refs"].sum()),
}]
grid_options["getRowStyle"] = JsCode(
    "function(p){ if(p.node.rowPinned){ return "
    "{'fontWeight':'700','background':'rgba(120,120,120,0.18)'}; } }"
)

metrics_box = st.container()
grid = AgGrid(
    acct_display,
    gridOptions=grid_options,
    height=460,
    theme="streamlit",
    allow_unsafe_jscode=True,
    fit_columns_on_grid_load=True,
    update_on=["filterChanged", "sortChanged"],
    data_return_mode="filtered_and_sorted",
    key="ledger_grid",
)
# Metric cards read from the SAME source as the pinned TOTAL row (acct_display),
# so the cards and the in-grid total row always agree.
with metrics_box:
    lk1, lk2, lk3 = st.columns(3)
    lk1.metric("Accounts", f"{len(acct_display):,}")
    lk2.metric("Total Net Outstanding", inr(acct_display["Net Outstanding"].sum()))
    lk3.metric("Total Amount Paid / Received", inr(acct_display["Amount Paid / Received"].sum()))

st.download_button(
    "⬇️ Download account-wise ledger (CSV)",
    acct_display.to_csv(index=False).encode("utf-8"),
    file_name="account_wise_ledger_apr2025_to_date.csv",
    mime="text/csv",
    key="dl_ledger",
)

st.divider()

# --------------------------------------------------------------------------- #
# Charts row 1
# --------------------------------------------------------------------------- #
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
    g = df.groupby("aging_bucket")["outstanding"].sum().reindex(AGING_ORDER).dropna().reset_index()
    fig = px.bar(g, x="aging_bucket", y="outstanding", text_auto=".2s",
                 color="aging_bucket", color_discrete_sequence=px.colors.sequential.Reds)
    fig.update_layout(showlegend=False, height=420, xaxis_title="", margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)

# --------------------------------------------------------------------------- #
# Charts row 2
# --------------------------------------------------------------------------- #
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

# --------------------------------------------------------------------------- #
# Top parties
# --------------------------------------------------------------------------- #
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

# --------------------------------------------------------------------------- #
# Detail table
# --------------------------------------------------------------------------- #
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
