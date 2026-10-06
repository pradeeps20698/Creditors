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


@st.cache_data(ttl=300, show_spinner=False)
def last_data_update():
    """Last successful swift_party_ref sync (from api_execution_log), as IST.

    executed_at is stored in UTC, so shift +5:30 for India Standard Time.
    Returns None if the log has no successful run yet.
    """
    q = (
        "SELECT MAX(executed_at) AS ts FROM api_execution_log "
        "WHERE api_name = 'swift_party_ref' AND status = 'SUCCESS'"
    )
    try:
        ts = pd.to_datetime(pd.read_sql(q, get_engine())["ts"].iloc[0])
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts + pd.Timedelta(hours=5, minutes=30)


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


def _inr_int(v) -> str:
    """Indian-grouped integer string, e.g. 19581686 -> '1,95,81,686'."""
    n = int(round(float(v)))
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    head = re.sub(r"(?<=\d)(?=(\d\d)+$)", ",", head)
    return sign + head + "," + tail


def _coll_cell(v) -> str:
    """Collection-report cell text: Indian integer + ' (N L)' when N rounds != 0."""
    n = float(v)
    if not n:
        return "0"
    full = _inr_int(n)
    ln = int(round(n / 100000))
    return full if ln == 0 else f"{full} ({_inr_int(ln)} L)"


def _collection_xlsx_bytes(disp_df, num_cols, detail_df=None) -> bytes:
    """Excel export of the collection report, styled like the on-screen table:
    numbers right-aligned, the '(N L)' part coloured blue, TOTAL row bold.

    When `detail_df` is given it is written to a second 'Bill Detail' sheet —
    the bill-no / bill-date / due-date line items backing the summary."""
    import io
    from openpyxl import Workbook
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.styles import Alignment, Font

    blue = InlineFont(color="58A6FF")
    right = Alignment(horizontal="right")
    left = Alignment(horizontal="left")
    wb = Workbook()
    ws = wb.active
    ws.title = "Collection"
    headers = list(disp_df.columns)
    ws.append(headers)
    for ci in range(1, len(headers) + 1):
        ws.cell(1, ci).font = Font(bold=True)
        ws.cell(1, ci).alignment = Alignment(horizontal="center")
    for _, row in disp_df.iterrows():
        rr = ws.max_row + 1
        is_total = str(row[headers[0]]).strip().upper() == "TOTAL"
        for ci, col in enumerate(headers, start=1):
            cell = ws.cell(rr, ci)
            if col in num_cols:
                n = float(row[col])
                full = _inr_int(n) if n else "0"
                ln = int(round(n / 100000)) if n else 0
                if n and ln != 0:
                    cell.value = CellRichText(full + " ",
                                              TextBlock(blue, f"({_inr_int(ln)} L)"))
                else:
                    cell.value = full
                cell.alignment = right
            else:
                cell.value = str(row[col])
                cell.alignment = left
            if is_total:
                cell.font = Font(bold=True)
    for ci, col in enumerate(headers, start=1):
        ws.column_dimensions[ws.cell(1, ci).column_letter].width = \
            22 if col == headers[0] else 20

    if detail_df is not None and not detail_df.empty:
        ws2 = wb.create_sheet("Bill Detail")
        dheaders = list(detail_df.columns)
        ws2.append(dheaders)
        for ci in range(1, len(dheaders) + 1):
            ws2.cell(1, ci).font = Font(bold=True)
            ws2.cell(1, ci).alignment = Alignment(horizontal="center")
        for _, row in detail_df.iterrows():
            rr = ws2.max_row + 1
            for ci, col in enumerate(dheaders, start=1):
                cell = ws2.cell(rr, ci)
                val = row[col]
                if col in ("Amount", "Credit Days", "Days Overdue"):
                    cell.value = None if pd.isna(val) else float(val)
                    cell.alignment = right
                else:
                    cell.value = "" if pd.isna(val) else str(val)
                    cell.alignment = left
        for ci, col in enumerate(dheaders, start=1):
            ws2.column_dimensions[ws2.cell(1, ci).column_letter].width = \
                26 if col == "Account Name" else 14

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _bill_detail_frame(r, accounts, bucket_col, bucket_label, group_header="Group"):
    """Line-level bills backing an aging/collection table — one row per reference,
    limited to the `accounts` actually shown above. Columns: group, account, bill
    no (ref_no), bill date (ref_date), due date, credit days, days overdue, the
    per-row bucket/week, and the net amount. Sorted by group → account → due date."""
    d = r[r["account_name"].isin(list(accounts))].copy()
    d = d.sort_values(["_grp", "account_name", "due_date"])
    out = pd.DataFrame({
        group_header: d["_grp"].values,
        "Account Name": d["account_name"].values,
        "Bill No": (d["ref_no"].astype("string").fillna("").values
                    if "ref_no" in d.columns else ""),
        "Bill Date": d["ref_date"].dt.strftime("%d-%m-%Y").values,
        "Due Date": d["due_date"].dt.strftime("%d-%m-%Y").values,
        "Credit Days": pd.to_numeric(d["credit_days"], errors="coerce").values,
        "Days Overdue": (TODAY - d["due_date"]).dt.days.values,
        bucket_label: d[bucket_col].astype("string").fillna("").values,
        "Amount": d["bal_amount"].round(0).values,
    })
    return out


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
_updated = last_data_update()
_updated_txt = (
    f"data updated {_updated:%d %b %Y, %I:%M %p} IST" if _updated is not None
    else "update time unavailable"
)
st.markdown(
    "<p style='text-align:center; color:#8b949e; font-size:0.9rem; margin-top:-0.5rem;'>"
    f"Source: <code>swift_party_ref</code> · {len(data):,} reference rows · "
    f"{_updated_txt}</p>",
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


def _norm_name(s) -> str:
    """Case-/whitespace-insensitive key for matching account names."""
    return " ".join(str(s).strip().lower().split())


# Pump Vendors "Unbilled" column: each pump account (name carries an oil-company
# code) is paired with a separate "control" account (same name without the code).
# The Unbilled cell shows that control account's NET balance. Both accounts are
# Current Liabilities. Keys/values matched via _norm_name (case/space-insensitive).
PUMP_UNBILLED_CONTROL = {
    "SWAMI SAMARTH PETROL PUMP_IOCL": "Swami Samarth Petrol Pump",
    "TIWARI PETROLEUM_IOCL": "TIWARI PETROLEUM",
    "Tiwari Highway Fuels_IOCL": "Tiwari Highway Fuels",
    "Shyam Filling Station_IOCL": "Shyam Filling Station",
    "HUMSAFAR INDIAN OIL_IOCL": "Humsafar Indian Oil",
    "Sangeeta Filling Station_IOCL": "Sangeeta Filling Station",
    "MS SHRI VANDAN SERVICE STATION_IOCL": "MS SHRI VANDAN SERVICE STATION",
    "Shakti Hi-tech Filling Center_IOCL": "Shakti Hi-tech Filling Center",
    "Gill Petrolium_BPCL": "Gill Petroleum",
    "Eesh Kripa Filling Station_BPCL": "Eesh Kripa Filling Station",
    "Jagalur Petroliums_BPCL": "Jagalur Petroliums",
    "Sri Babu Raju Ram Fuel Station_IOCL": "Sri Babu Raju Ram Fuel Station",
    "Bombay And Central India Carriers_BPCL": "Bombay And Central India Carriers",
    "Yash Petroleum_BPCL": "Yash Petroleum",
    "PARVATHI SUPER FUELS_IOCL": "PARVATHI SUPER FUELS",
    "NAND PETROLEUM_IOCL": "NAND PETROLEUM",
    "Mohini Filling Station_IOCL": "MOHINI FILLING STATION",
    "MS Balaji Fuels_BPCL": "MS Balaji Fuels",
    "Elango Service Station_BPCL": "Elango Service Station",
    "Highway Services_BPCL": "Highway Services",
    "Kalyani Petroleum_BPCL": "Kalyani Petroleum",
    "Sai Sangam Petroleum_BPCL": "Sai Sangam Petroleum",
    "Narayani Fuel Point_IOCL": "Narayani Fuel Point",
    "KAJALE AND SONS _IOCL": "KAJALE & SONS",
    "Jainex Filling Station_IOCL": "Jainex Filling Station",
    "Ganesh Petrolium Pune_IOCL": "Ganesh Petrolium Pune",
    "AR Plaza Hpcl Pump_HPCL": "AR PLAZA - BHANDRA",
    "Pratima Filling Station_HPCL": "PRATIMA FILLING STATION",
    "Supreme Auto Station_IOCL": "Supreme Auto Station",
    "Sri Chennakesava Filling Station_IOCL": "Sri Chennakesava Filling Station",
    "TPRS FUELS_BPCL": "TPRS FUELS",
    "Poonam Service Station_IOCL": "Poonam Service Station",
    "Sainik Petrol Pump_HPCL": "Sainik Petrol Pump",
    "Pokar Petroleum_IOCL": "Pokar Petroleum",
    "Kashana Motors Noida_IOCL": "Kashana Motors Noida",
    "Anand Rekha E/W_HPCL": "Anand Rekha E/W Corridor Service",
    "Aditi ENTERPRISE_IOCL": "M/S Aditi ENTERPRISE",
    "Bhavani Petroleums_BPCL": "Bhavani Petroleums",
    "Dhruv Petrolium-IOCL": "Dhruv Petrolium",
    "D.S Fuel KSK _ IOCL": "D.S Fuel KSK",
    "Lakshya SCK Fuels_BPCL": "Lakshya SCK Fuels",
    "Bidadi Petroleums_IOCL": "Bidadi Petroleums",
    "Ashok Fuel Station_HPCL": "Ashok Fuel Station",
}
# Normalised lookup: normalised pump-name -> control account name.
PUMP_UNBILLED_CONTROL_NORM = {
    _norm_name(k): v for k, v in PUMP_UNBILLED_CONTROL.items()
}


def _pump_unbilled_map(rows: pd.DataFrame) -> dict:
    """normalised pump-account-name -> matched control account's net balance.

    Control balances are summed over Current Liabilities accounts in `rows`.
    """
    cl = rows[rows["account_type"] == "Current Liabilities"]
    control_net = cl.groupby(cl["account_name"].map(_norm_name))["bal_amount"].sum()
    out = {}
    for pump_norm, ctrl_name in PUMP_UNBILLED_CONTROL_NORM.items():
        v = control_net.get(_norm_name(ctrl_name))
        if v is not None:
            out[pump_norm] = float(v)
    return out


# Payables tab: non-pump vendors on this list are split out into their own
# "Associate Creditor" table. Matched via _norm_name (case/space-insensitive).
ASSOCIATE_CREDITOR_ACCOUNTS = [
    "DAX FLEETS AND LOGISTICS PRIVATE LIMITED",
    "Durga Logistics",
    "FLEETPARCEL LOGISTICS PRIVATE LIMITED",
    "krish Logistics - HVP",
    "Laxshita Car Transport - HVP",
    "Maruti Roadways",
    "MOHAN LOGISTICS PRIVATE LIMITED",
    "Nishant Saini Associates",
    "PICKALL LOGISTICS PRIVATE LIMITED-CR",
    "PRADEEP BANDHU",
    "RAJNISH KUMAR",
    "RAMAN ROADWAYS",
    "Ranjeet Singh Logistics - CR",
    "Road Express Technology Private Limited",
    "RRD ROADCARE PRIVATE LIMITED-HVP",
    "SHREE GANPATI TRILOR SERVISE",
    "Sukhbir Singh Batch",
    "Sun India Logistics HVP",
    "Swaraj Enterprises",
    "Thakur Transport Company",
]
ASSOCIATE_CREDITOR_NORM = {_norm_name(n) for n in ASSOCIATE_CREDITOR_ACCOUNTS}


def _slug(s) -> str:
    """URL/key-safe slug from an arbitrary label."""
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "grp"


def _has_displayable_accounts(sub: pd.DataFrame) -> bool:
    """True if any account in `sub` survives the ledger's near-zero filter
    (|net bal_amount| > 100) — i.e. the table would render at least one row."""
    if sub.empty:
        return False
    net = sub.groupby("account_name")["bal_amount"].sum()
    return bool((net.abs() > 100).any())


@st.cache_data(ttl=600)
def load_vendor_groups():
    """Load the Payables vendor grouping from Group.xlsx / vendor_groups.csv
    (two columns: Group, Vendor(s) Name). Returns (normalised-name -> group dict,
    ordered groups). Returns ({}, []) if no file is present (graceful fallback)."""
    here = os.path.dirname(os.path.abspath(__file__))
    xlsx = os.path.join(here, "Group.xlsx")
    csv = os.path.join(here, "vendor_groups.csv")
    try:
        if os.path.exists(xlsx):
            g = pd.read_excel(xlsx, sheet_name=0, dtype=str).fillna("")
        elif os.path.exists(csv):
            g = pd.read_csv(csv, dtype=str).fillna("")
        else:
            return {}, []
    except Exception:
        return {}, []
    low = {c.lower().strip(): c for c in g.columns}
    gcol = low.get("group", g.columns[0])
    ncol = (low.get("account_name") or low.get("vendors name")
            or low.get("vendor name") or low.get("name")
            or (g.columns[1] if len(g.columns) > 1 else g.columns[0]))
    name_to_group, order = {}, []
    for _, r in g.iterrows():
        grp = str(r[gcol]).strip()
        nm = _norm_name(r[ncol])
        if not grp or not nm:
            continue
        name_to_group[nm] = grp
        if grp not in order:
            order.append(grp)
    return name_to_group, order


# --------------------------------------------------------------------------- #
# KPI row  —  NET, account-level, by AccountType.
# --------------------------------------------------------------------------- #
# Payables = Current Liabilities, Receivables = Current Assets. Payables EXCLUDES
# the "Creditors Diesel Control A/c" group (shown as Unbilled in Pump Vendors) and
# the non-pump accounts not assigned to any group (the "Unclassified" table), to
# avoid double-counting / contra accounts.
_kpi_groups, _ = load_vendor_groups()
_acct = df.groupby(["account_name", "account_type"], as_index=False)["bal_amount"].sum()
_acct = _acct[_acct["bal_amount"].abs() > 100]  # drop near-zero, same rule as ledger
_coded = _acct["account_name"].str.contains(CODE_PATTERN, case=False, regex=True)
_grp = _acct["account_name"].map(lambda n: _kpi_groups.get(_norm_name(n)))
# NaN-safe: when no groups load (e.g. Group.xlsx/openpyxl missing) _grp is all-NaN.
_grp_l = _grp.astype("string").fillna("").str.strip().str.lower()
_is_cl = _acct["account_type"] == "Current Liabilities"
_is_ca = _acct["account_type"] == "Current Assets"
# Count a payable account if it's a pump (coded) OR grouped but not Diesel-Control.
_pay_incl = _is_cl & (_coded | (_grp.notna() & (_grp_l != "creditors diesel control a/c")))
_rec_incl = _is_ca
payables = _acct.loc[_pay_incl, "bal_amount"].sum()      # we owe (negative)
receivables = _acct.loc[_rec_incl, "bal_amount"].sum()   # owed to us (positive)
net = payables + receivables
n_parties = int((_pay_incl | _rec_incl).sum())

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
        "function(p){return p.value==null?'':Math.round(Number(p.value))"
        ".toLocaleString('en-IN',{minimumFractionDigits:0,maximumFractionDigits:0});}"
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
    grid_h = min(460, 112 + 30 * len(acct_display))  # shrink to fit; scroll if tall
    AgGrid(
        acct_display,
        gridOptions=grid_options,
        height=grid_h,
        theme="streamlit",
        allow_unsafe_jscode=True,
        fit_columns_on_grid_load=True,
        update_on=["filterChanged", "sortChanged"],
        data_return_mode="filtered_and_sorted",
        custom_css=AG_DARK_CSS,  # centred blue headers, same as other tables
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


# Aging layout: amounts already PAST DUE are split by days-overdue into buckets,
# and everything still to come is collapsed into one "Due in coming weeks" column.
# days_to_due = (due_date - today).days  (<0 = overdue, >=0 = upcoming).
DUE_SOON_COL = "Due in coming weeks"
# Payables only: references with a POSITIVE bal_amount are advances (we've paid
# ahead), pulled out of the overdue/due-soon buckets into their own column.
ADVANCE_COL = "Advance"
# Overdue-age buckets: (label, inclusive_upper days-overdue); last bin open-ended.
OVERDUE_BINS = [("Overdue by 0-3", 3), ("Overdue by 4-7", 7), ("Overdue by 8-10", 10),
                ("Overdue by 11-15", 15), ("Overdue by 16-30", 30), ("Overdue by 30+", None)]
OVERDUE_COLS = [lbl for lbl, _ in OVERDUE_BINS]

# Payables use the NEW overdue-age layout above (bins passed in are ignored for
# that side). Receivables keep the CLASSIC forward-looking layout below:
# "Overdue" (already past due) as one column, then amounts coming due in windows.
PAYABLE_BINS = OVERDUE_BINS  # placeholder; payables ignore this and use OVERDUE_BINS
RECEIVABLE_BINS = [("Next 0-7", 7), ("Next 8-14", 14), ("Next 15-30", 30),
                   ("Next 30-60", 60), ("Next 60+", None)]
FUTURE_COLS = OVERDUE_COLS


def _bucket_by_due(d):
    """NEW (payables) layout. Map days-to-due to a column: NaN -> 'No due date';
    >=0 -> due-soon; <0 -> an overdue-age bucket keyed by how many days past due."""
    if pd.isna(d):
        return "No due date"
    if d >= 0:
        return DUE_SOON_COL
    dov = -d  # days overdue (>= 1)
    for label, hi in OVERDUE_BINS:
        if hi is None or dov <= hi:
            return label
    return OVERDUE_BINS[-1][0]


def _make_bucketer(bins):
    """CLASSIC (receivables) layout: map days-until-due to a bucket label for the
    given bins. "Overdue" if already past due; else the matching upcoming window."""
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
                        default_credit_days: int = None,
                        select_by_type: bool = False) -> None:
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
    r["_dtd"] = (r["due_date"] - TODAY).dt.days
    r["_dov"] = (TODAY - r["due_date"]).dt.days
    r["_grp"] = r["account_name"]  # display grouping (group label when group_map set)
    if sign == "payable":
        # New layout: "Due in coming weeks" + "Advance" + overdue-age buckets.
        r["_bkt"] = r["_dtd"].apply(_bucket_by_due)
        bucket_order = [DUE_SOON_COL, ADVANCE_COL] + OVERDUE_COLS
        tips = _due_soon_tip_by_account(r)   # hover breakdown of upcoming windows
        tip_col = DUE_SOON_COL
    else:
        # Classic receivable layout: "Overdue" lump + upcoming windows.
        r["_bkt"] = r["_dtd"].apply(_make_bucketer(bins))
        bucket_order = ["Overdue"] + [lbl for lbl, _ in bins]
        tips = _overdue_tip_by_account(r)    # hover breakdown of overdue age
        tip_col = "Overdue"
    full_order = bucket_order + ["No due date"]
    piv = (
        r.pivot_table(index="account_name", columns="_bkt",
                      values="bal_amount", aggfunc="sum", fill_value=0.0)
        .reindex(columns=full_order, fill_value=0.0)
    )
    piv["Total"] = piv.sum(axis=1)
    # Payables: an account whose NET total is positive is a net advance — show the
    # whole balance in the Advance column and leave the aging buckets empty.
    if sign == "payable":
        adv = piv["Total"] > 0
        piv.loc[adv, full_order] = 0.0
        piv.loc[adv, ADVANCE_COL] = piv.loc[adv, "Total"]
    # Keep accounts + hide near-zero. When selecting by AccountType the rows are
    # already the correct side (filtered upstream), so keep BOTH net signs and
    # order by magnitude; otherwise keep the side matching `sign` (net direction).
    if select_by_type:
        piv = piv[piv["Total"].abs() > 100].sort_values(
            "Total", key=lambda s: s.abs(), ascending=False)
    elif sign == "receivable":
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
    piv["_ov_tip"] = piv[name_header].map(tips).fillna("")  # hover tooltip breakdown
    if sign == "payable":
        piv.loc[piv["Total"] > 0, "_ov_tip"] = ""  # advance accounts: no due-soon breakdown

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
        "function(p){return p.value==null?'':Math.round(Number(p.value))"
        ".toLocaleString('en-IN',{minimumFractionDigits:0,maximumFractionDigits:0});}"
    )
    for c in num_cols:
        gb.configure_column(c, type=["numericColumn"],
                            filter="agNumberColumnFilter", valueFormatter=inr_fmt)
    # Tint the Advance column so it stands out.
    if ADVANCE_COL in num_cols:
        gb.configure_column(
            ADVANCE_COL, type=["numericColumn"], filter="agNumberColumnFilter",
            valueFormatter=inr_fmt,
            cellStyle={"textAlign": "center", "backgroundColor": "rgba(210,153,34,0.20)"})
    # Tooltip cell (payables: "Due in coming weeks"; receivables: "Overdue"):
    # on hover show its day-range breakdown.
    if tip_col in num_cols:
        gb.configure_column(
            tip_col, type=["numericColumn"], filter="agNumberColumnFilter",
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
    grid_h = min(460, 112 + 30 * len(piv))  # shrink to fit; scroll if tall
    AgGrid(
        piv,
        gridOptions=grid_options,
        height=grid_h,
        theme="streamlit",
        allow_unsafe_jscode=True,
        fit_columns_on_grid_load=True,
        update_on=["filterChanged", "sortChanged"],
        data_return_mode="filtered_and_sorted",
        custom_css=AG_DARK_CSS,
        key=f"aging_grid_{key}",
    )
    with metrics_box:
        if sign == "payable":
            overdue_total = piv[[c for c in OVERDUE_COLS if c in piv.columns]].sum().sum()
            soon_total = piv[DUE_SOON_COL].sum() if DUE_SOON_COL in piv.columns else 0.0
            soon_label = "Due in coming weeks"
        else:
            overdue_total = piv["Overdue"].sum() if "Overdue" in piv.columns else 0.0
            soon_cols = [lbl for lbl, _ in bins if lbl in piv.columns]
            soon_total = piv[soon_cols].sum().sum() if soon_cols else 0.0
            soon_label = "Coming due (next)"
        n_no_credit = int(piv["_no_credit"].sum())
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(unit.capitalize(), f"{len(piv):,}")
        m2.metric("Total Net Outstanding", inr(piv["Total"].sum()))
        m3.metric("Overdue (past due)", inr(overdue_total))
        m4.metric(soon_label, inr(soon_total))
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

    acol1, acol2 = st.columns(2)
    with acol1:
        st.download_button(
            "⬇️ Download aging ledger (CSV)",
            piv.drop(columns=["_no_credit", "_ov_tip"]).to_csv(index=False).encode("utf-8"),
            file_name=f"aging_ledger_{key}.csv",
            mime="text/csv",
            key=f"dl_aging_{key}",
        )
    with acol2:
        _detail = _bill_detail_frame(
            r, piv[name_header], bucket_col="_bkt",
            bucket_label="Aging Bucket", group_header="Group")
        st.download_button(
            "⬇️ Download bill-wise detail (CSV)",
            _detail.to_csv(index=False).encode("utf-8"),
            file_name=f"aging_bills_{key}.csv",
            mime="text/csv",
            key=f"dl_aging_bills_{key}",
        )

    # Row-level references for the accounts shown above.
    render_reference_detail(rows, piv[name_header].tolist(), key)


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


# Day-range windows for the "Due in coming weeks" popup: (lower, upper, label) on
# days-to-due (>= 0). The last window is open-ended (upper = None).
DUE_SOON_POPUP_BINS = [
    (0, 3, "Next 0-3 days"), (4, 7, "Next 4-7 days"), (8, 15, "Next 8-15 days"),
    (16, 30, "Next 16-30 days"), (31, None, "Next 30+ days"),
]


def _due_soon_breakdown(sub: pd.DataFrame) -> pd.DataFrame:
    """Split an account's UPCOMING references (_dtd >= 0) into day-range windows."""
    out = []
    for lo, hi, lbl in DUE_SOON_POPUP_BINS:
        m = (sub["_dtd"] >= lo) if hi is None else ((sub["_dtd"] >= lo) & (sub["_dtd"] <= hi))
        out.append({"Window": lbl,
                    "Amount": float(sub.loc[m, "bal_amount"].sum()),
                    "Refs": int(m.sum())})
    return pd.DataFrame(out)


def _due_soon_tip_by_account(r: pd.DataFrame) -> dict:
    """Map account_name -> HTML tooltip with its upcoming-window breakdown.

    Requires columns _dtd (days to due), bal_amount, account_name on `r`.
    """
    tips = {}
    for acct_name, sub_r in r[r["_dtd"] >= 0].groupby("account_name"):
        bd = _due_soon_breakdown(sub_r)
        parts = [f"{b.Window}: {inr(b.Amount)} ({b.Refs})"
                 for b in bd.itertuples() if b.Amount]
        if parts:
            tips[acct_name] = ("<b>Due in coming weeks</b><br>" + "<br>".join(parts)
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


# Row-level "Reference detail" columns (matches the tab_all detail table).
_DETAIL_COLS = ["ref_no", "ref_type", "category", "account_name", "office",
                "division_name", "ref_date", "due_date", "days_overdue",
                "aging_bucket", "ref_amount", "amount_paid", "bal_amount", "is_active"]


def render_reference_detail(rows: pd.DataFrame, account_names, key: str) -> None:
    """Below an aging table: row-level references for exactly the accounts shown
    in that table (`account_names`). Collapsible, with search + CSV download."""
    accts = list(dict.fromkeys(account_names))  # de-dup, keep order
    base = rows[rows["account_name"].isin(accts)].copy()
    with st.expander(f"🔎 Reference detail — {len(accts):,} account(s) in the table above",
                     expanded=False):
        search = st.text_input("Search party / reference no / narration", "",
                               key=f"refdet_search_{key}")
        show = base
        if search:
            s = search.lower()
            show = show[
                show["account_name"].fillna("").str.lower().str.contains(s)
                | show["ref_no"].fillna("").str.lower().str.contains(s)
                | show["narration"].fillna("").str.lower().str.contains(s)
            ]
        cols = [c for c in _DETAIL_COLS if c in show.columns]
        _num = ["ref_amount", "amount_paid", "bal_amount", "days_overdue"]
        colcfg = {c: st.column_config.NumberColumn(format="%.0f")
                  for c in _num if c in cols}
        st.dataframe(
            show[cols].sort_values("bal_amount", key=lambda x: x.abs(), ascending=False),
            use_container_width=True, height=360, hide_index=True, column_config=colcfg,
        )
        st.caption(f"{len(show):,} rows shown")
        st.download_button(
            "⬇️ Download reference detail (CSV)",
            show[cols].to_csv(index=False).encode("utf-8"),
            file_name=f"reference_detail_{key}.csv",
            mime="text/csv",
            key=f"refdet_dl_{key}",
        )


def render_grouped_aging_ledger(rows: pd.DataFrame, key: str, group_map,
                                bins=RECEIVABLE_BINS, sign: str = "receivable",
                                name_header: str = "Account Name",
                                apply_defaults: bool = False,
                                default_credit_days: int = None,
                                group_label: str = "OEM",
                                overdue_popup: bool = False,
                                select_by_type: bool = False,
                                unbilled_map: dict = None) -> None:
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
    r["_dtd"] = (r["due_date"] - TODAY).dt.days
    if sign == "payable":
        r["_bkt"] = r["_dtd"].apply(_bucket_by_due)
        full_order = [DUE_SOON_COL, ADVANCE_COL] + OVERDUE_COLS + ["No due date"]
    else:
        r["_bkt"] = r["_dtd"].apply(_make_bucketer(bins))
        full_order = ["Overdue"] + [lbl for lbl, _ in bins] + ["No due date"]
    piv = (
        r.pivot_table(index=["_grp", "account_name"], columns="_bkt",
                      values="bal_amount", aggfunc="sum", fill_value=0.0)
        .reindex(columns=full_order, fill_value=0.0)
    )
    piv["Total"] = piv.sum(axis=1)
    # Payables: accounts with a positive NET total are net advances — the whole
    # balance goes to the Advance column, aging buckets left empty.
    if sign == "payable":
        adv = piv["Total"] > 0
        piv.loc[adv, full_order] = 0.0
        piv.loc[adv, ADVANCE_COL] = piv.loc[adv, "Total"]
    # Keep near-zero accounts hidden. Selecting by AccountType keeps both net
    # signs (rows are already the right side); otherwise keep the side by `sign`.
    if select_by_type:
        piv = piv[piv["Total"].abs() > 100]
    elif sign == "receivable":
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

    # Precompute the hover-tooltip per account (payables: upcoming-window
    # breakdown; receivables: overdue day-range breakdown).
    tip_by_acct = (_due_soon_tip_by_account(r) if sign == "payable"
                   else _overdue_tip_by_account(r))
    tip_col = DUE_SOON_COL if sign == "payable" else "Overdue"
    # Advance accounts (positive net) show 0 in due-soon, so no breakdown tooltip.
    if sign == "payable":
        adv_accts = set(piv.loc[piv["Total"] > 0, "account_name"])
        for a in adv_accts:
            tip_by_acct.pop(a, None)

    # Optional "Unbilled" column (Pump Vendors): net balance of each account's
    # matched control account, shown as the first numeric column.
    show_unbilled = unbilled_map is not None
    grand_unbilled = 0.0

    # Build the display frame: account rows per group, then a subtotal row.
    grp_order = piv.groupby("_grp")["Total"].sum().sort_values(ascending=asc).index
    display_rows = []
    for g in grp_order:
        sub = piv[piv["_grp"] == g].sort_values("Total", ascending=asc)
        grp_unbilled = 0.0
        for _, row in sub.iterrows():
            acct = row["account_name"]
            d = {name_header: acct, "_is_total": False,
                 "_no_credit": bool(no_credit.get(acct, False)),
                 "_ov_tip": tip_by_acct.get(acct, "")}
            if show_unbilled:
                unb = float(unbilled_map.get(_norm_name(acct), 0.0))
                d["Unbilled"] = unb
                grp_unbilled += unb
                grand_unbilled += unb
            d.update({c: float(row[c]) for c in num_cols})
            d[BASIS_COL] = basis_by_acct.get(acct, "Actual")
            display_rows.append(d)
        d = {name_header: f"{g} - Total", "_is_total": True, "_no_credit": False,
             "_ov_tip": ""}
        if show_unbilled:
            d["Unbilled"] = grp_unbilled
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
        "function(p){return p.value==null?'':Math.round(Number(p.value))"
        ".toLocaleString('en-IN',{minimumFractionDigits:0,maximumFractionDigits:0});}"
    )
    for c in num_cols:
        gb.configure_column(c, type=["numericColumn"], valueFormatter=inr_fmt)
    # Tint the Advance column so it stands out.
    if ADVANCE_COL in num_cols:
        gb.configure_column(
            ADVANCE_COL, type=["numericColumn"], valueFormatter=inr_fmt,
            cellStyle={"textAlign": "center", "backgroundColor": "rgba(210,153,34,0.20)"})
    # Tint the Unbilled column a distinct colour.
    if show_unbilled:
        gb.configure_column(
            "Unbilled", type=["numericColumn"], valueFormatter=inr_fmt,
            cellStyle={"textAlign": "center", "backgroundColor": "rgba(88,166,255,0.20)"})
    # Tooltip cell (payables: "Due in coming weeks"; receivables: "Overdue"):
    # on hover show its day-range breakdown.
    if overdue_popup and tip_col in num_cols:
        gb.configure_column(
            tip_col, type=["numericColumn"], valueFormatter=inr_fmt,
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
    if show_unbilled:
        total_row["Unbilled"] = grand_unbilled
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

    grid_h = min(560, 84 + 30 * len(disp))  # shrink to fit; scroll if tall
    AgGrid(
        disp,
        gridOptions=grid_options,
        height=grid_h,
        theme="streamlit",
        allow_unsafe_jscode=True,
        fit_columns_on_grid_load=True,
        custom_css=AG_DARK_CSS,
        key=f"grouped_aging_grid_{key}",
    )

    if overdue_popup:
        st.caption(f"💡 Hover a **{tip_col}** amount to see its day-range breakdown.")

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

    dcol1, dcol2 = st.columns(2)
    with dcol1:
        st.download_button(
            "⬇️ Download grouped aging ledger (CSV)",
            disp.drop(columns=["_is_total", "_no_credit"]).to_csv(index=False).encode("utf-8"),
            file_name=f"grouped_aging_ledger_{key}.csv",
            mime="text/csv",
            key=f"dl_grouped_aging_{key}",
        )
    with dcol2:
        _detail = _bill_detail_frame(
            r, piv["account_name"], bucket_col="_bkt",
            bucket_label="Aging Bucket", group_header=group_label)
        st.download_button(
            "⬇️ Download bill-wise detail (CSV)",
            _detail.to_csv(index=False).encode("utf-8"),
            file_name=f"grouped_aging_bills_{key}.csv",
            mime="text/csv",
            key=f"dl_grouped_aging_bills_{key}",
        )

    # Row-level references for the accounts shown above.
    render_reference_detail(rows, piv["account_name"].tolist(), key)


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
    # Fold undated amounts into Overdue so each row's buckets sum to its net.
    piv["Overdue"] = piv["Overdue"] + piv["No due date"]
    piv = piv.drop(columns="No due date")
    bucket_cols = ["Overdue"] + future_cols
    piv["Total"] = piv[bucket_cols].sum(axis=1)  # = net bal_amount per account

    # Classify by AccountType, matching the KPI row: Payables = Current Liabilities
    # except the Diesel-Control group and Unclassified (non-pump, ungrouped);
    # Receivables = Current Assets. Drop near-zero accounts (|net| <= 100).
    acct_type = rows.groupby("account_name")["account_type"].first()
    name_to_group, _ = load_vendor_groups()

    def _pay_ok(a):
        if acct_type.get(a) != "Current Liabilities":
            return False
        coded = bool(re.search(CODE_PATTERN, str(a), re.I))
        g = name_to_group.get(_norm_name(a))
        return coded or (g is not None and g.strip().lower() != "creditors diesel control a/c")

    idx = piv.index
    big = piv["Total"].abs() > 100
    pay_mask = pd.Series([_pay_ok(a) for a in idx], index=idx) & big
    rec_mask = pd.Series([acct_type.get(a) == "Current Assets" for a in idx], index=idx) & big
    pay, rec = piv[pay_mask], piv[rec_mask]

    def _side(sub, label):
        d = {"Type": label}
        d.update({c: float(sub[c].sum()) for c in bucket_cols})
        d["Total"] = float(sub["Total"].sum())
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


@st.cache_data(ttl=600, show_spinner=False)
def load_unbilled_income() -> dict:
    """Unbilled income per OEM group from cn_data: basic_freight of trips that are
    delivered (POD receipt present) but not yet invoiced (bill_no blank).

    Filters: active rows; drop TEST cn_no; drop the known bad Ranjeet Singh
    Logistics / 65000 record. Grouped by billing_party -> OEM keyword group."""
    d = pd.read_sql(
        "SELECT cn_no, billing_party, bill_no, pod_receipt_no, basic_freight, "
        "is_active FROM cn_data", get_engine())
    bf = pd.to_numeric(d["basic_freight"], errors="coerce")
    active = (d["is_active"] == True) | (d["is_active"].astype(str).str.lower() == "yes")  # noqa: E712
    not_test = d["cn_no"].isna() | (~d["cn_no"].astype(str).str.startswith("TEST"))
    bad = (d["billing_party"] == "Ranjeet Singh Logistics") & (bf == 65000)
    base = d[active & not_test & ~bad]
    bn = base["bill_no"].astype("string")
    pod = base["pod_receipt_no"].astype("string")
    unb = base[(bn.isna() | (bn.str.strip() == ""))            # not yet invoiced
               & (pod.notna() & (pod.str.strip() != ""))]       # POD received
    g = (unb.assign(_g=unb["billing_party"].map(_oem_group),
                    _bf=pd.to_numeric(unb["basic_freight"], errors="coerce").fillna(0.0))
         .groupby("_g")["_bf"].sum())
    return g.to_dict()


def render_collection_report(oem_rows: pd.DataFrame, key: str = "collection") -> None:
    """Week-wise expected-collection forecast for OEM receivables.

    Rows = OEM group; columns = calendar weeks (current week + next 3), then a
    'Later' bucket and a Total. Amounts are placed by effective due date (OEM
    default credit-days fill missing ones); anything overdue folds into the
    current week. Weeks run Monday–Sunday based on the calendar."""
    if oem_rows is None or oem_rows.empty:
        st.info("No OEM receivable accounts to forecast.")
        return
    r = oem_rows.copy()
    # Effective due date: apply OEM default credit-days where DB credit_days is 0/null.
    cd = pd.to_numeric(r["credit_days"], errors="coerce")
    missing = cd.isna() | (cd <= 0)
    dflt = r["account_name"].map(_oem_default_credit_days)
    use = missing & dflt.notna()
    if use.any():
        r.loc[use, "due_date"] = (r.loc[use, "ref_date"]
                                  + pd.to_timedelta(dflt[use].astype(float), unit="D"))
    r["_grp"] = r["account_name"].map(_oem_group)

    N = 4  # calendar weeks shown (current + next 3); rest -> "Later"
    monday = (TODAY - pd.Timedelta(days=int(TODAY.weekday()))).normalize()

    def _wk(due):
        if pd.isna(due):
            return None
        d = (due.normalize() - monday).days
        if d < 0:
            return -1           # Old Collection (overdue, before current week)
        return min(d // 7, N)   # N -> "Later"
    r["_w"] = r["due_date"].apply(_wk)
    r = r[r["_w"].notna()]
    r["_w"] = r["_w"].astype(int)

    col_keys = list(range(-1, N + 1))   # -1 = Old Collection, 0..N-1 weeks, N = Later
    label_of = {-1: "Old Collection"}
    for i in range(N):
        s = monday + pd.Timedelta(days=7 * i)
        e = s + pd.Timedelta(days=6)
        head = "This week" if i == 0 else f"Week {i + 1}"
        label_of[i] = f"{head} ({s:%d %b}–{e:%d %b})"
    label_of[N] = f"Later ({(monday + pd.Timedelta(days=7 * N)):%d %b}+)"
    labels = [label_of[k] for k in col_keys]

    piv = (r.pivot_table(index="_grp", columns="_w", values="bal_amount",
                         aggfunc="sum", fill_value=0.0)
           .reindex(columns=col_keys, fill_value=0.0))
    piv.columns = labels
    order = [k for k in RECEIVABLE_KEEP_KEYWORDS if k in piv.index]
    piv = piv.reindex(order)
    piv["Total"] = piv.sum(axis=1)
    piv = piv.reset_index().rename(columns={"_grp": "OEM Group"})
    _unb = load_unbilled_income()  # OEM group -> unbilled basic_freight (from cn_data)
    piv["Unbilled Income"] = piv["OEM Group"].map(lambda g: float(_unb.get(g, 0.0)))
    num = labels + ["Total", "Unbilled Income"]
    tot = {"OEM Group": "TOTAL"}
    for c in num:
        tot[c] = float(piv[c].sum())

    inr0 = JsCode(
        "class {"
        " init(p){"
        "  this.eGui=document.createElement('span');"
        "  var v=p.value;"
        "  if(v==null||v===''){ this.eGui.textContent=''; return; }"
        "  var n=Number(v);"
        "  if(!n){ this.eGui.textContent='0'; return; }"
        "  var full=Math.round(n).toLocaleString('en-IN',"
        "{minimumFractionDigits:0,maximumFractionDigits:0});"
        "  var Ln=Math.round(n/100000);"
        "  if(Ln===0){ this.eGui.textContent=full; return; }"
        "  var L=Ln.toLocaleString('en-IN',"
        "{minimumFractionDigits:0,maximumFractionDigits:0});"
        "  this.eGui.innerHTML=full+' <span style=\"color:#58a6ff\">('+L+' L)</span>';"
        " }"
        " getGui(){ return this.eGui; }"
        "}")
    gb = GridOptionsBuilder.from_dataframe(piv)
    gb.configure_default_column(sortable=False, filter=False, resizable=True, flex=1,
                                cellStyle={"textAlign": "center"})
    for c in num:
        gb.configure_column(c, type=["numericColumn"], cellRenderer=inr0,
                            cellStyle={"textAlign": "right"})
    gb.configure_column("OEM Group", pinned="left", minWidth=150,
                        cellStyle={"textAlign": "left", "fontWeight": "600"})
    gb.configure_column("Total", cellRenderer=inr0,
                        cellStyle={"textAlign": "right", "fontWeight": "700"})
    go = gb.build()
    go["pinnedTopRowData"] = [tot]
    go["enableCellTextSelection"] = True
    go["getRowStyle"] = JsCode(
        "function(p){ if(p.node.rowPinned){ return {'fontWeight':'700',"
        "'background':'rgba(120,120,120,0.18)'}; } }")
    AgGrid(piv, gridOptions=go, height=min(460, 95 + 34 * len(piv)),
           theme="streamlit", allow_unsafe_jscode=True, fit_columns_on_grid_load=True,
           custom_css=AG_DARK_CSS, key=f"collection_grid_{key}")
    st.caption(f"Expected collection by calendar week (Mon–Sun). Overdue amounts fold "
               f"into the current week. Today {TODAY:%d/%m/%Y}.")
    _disp = pd.concat([pd.DataFrame([tot]), piv], ignore_index=True)
    # Bill-wise detail (bill no / bill date / due date) backing the week forecast,
    # written to a second sheet of the Excel. "Collection Week" = the week column
    # each bill falls under above.
    r["_cwk"] = r["_w"].map(label_of)
    _shown_accts = r.loc[r["_grp"].isin(order), "account_name"].unique()
    _detail = _bill_detail_frame(
        r, _shown_accts, bucket_col="_cwk",
        bucket_label="Collection Week", group_header="OEM Group")
    st.download_button(
        "⬇️ Download collection report (Excel)",
        _collection_xlsx_bytes(_disp, num, detail_df=_detail),
        file_name=f"collection_report_{key}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"dl_collection_{key}",
    )


tab_all, tab_pay, tab_rec = st.tabs(
    ["All accounts", "Payables only (we owe)",
     "Receivables only (owed to us)"]
)
with tab_pay:
    st.caption(
        "Net Outstanding split into due-date aging buckets — days overdue "
        f"(today {TODAY:%d/%m/%Y} − due_date). **Pump Vendors** = accounts with an "
        "oil-company code (IOCL/BPCL/HPCL). Fuel/pump accounts **without** a code, "
        "and control/slip/PUC accounts, are in the **Control-AC & others** tab."
    )
    # Payables = accounts classified as "Current Liabilities" (AccountType).
    pay_led = led[led["account_type"] == "Current Liabilities"]
    names = pay_led["account_name"].fillna("")
    has_code = names.str.contains(CODE_PATTERN, case=False, regex=True)
    excl = _is_dedicated(pay_led)

    # Pump Vendors = carries an oil-company code (IOCL/BPCL/HPCL).
    pump_mask = has_code & ~excl

    st.markdown("### ⛽ Pump Vendors")
    st.caption("Grouped by oil company (BPCL / IOCL / HPCL) with a subtotal row per group. "
               "**Unbilled** = net balance of each pump's matched control account.")
    render_grouped_aging_ledger(pay_led[pump_mask], "payables_pump",
                                group_map=_pump_group, bins=PAYABLE_BINS,
                                sign="payable", name_header="Account Name",
                                default_credit_days=7, group_label="Oil company",
                                overdue_popup=True, select_by_type=True,
                                unbilled_map=_pump_unbilled_map(led))

    st.divider()

    # Remaining (non-pump) Current-Liabilities accounts, split into one table per
    # Group from vendor_groups.csv. ALL such accounts are grouped (see scope choice).
    name_to_group, group_order = load_vendor_groups()
    grp_of = pay_led["account_name"].map(lambda n: name_to_group.get(_norm_name(n)))

    if group_order:
        # Groups not shown as their own table in Payables (control accounts feed the
        # Pump Vendors "Unbilled" column instead).
        hidden_groups = {"creditors diesel control a/c"}
        for grp in group_order:
            if grp.strip().lower() in hidden_groups:
                continue
            sub = pay_led[(~has_code) & (grp_of == grp)]
            # Hide the whole group if nothing to show now; auto-appears when data comes.
            if not _has_displayable_accounts(sub):
                continue
            st.markdown(f"### 📂 {grp}")
            render_aging_ledger(sub, f"pay_grp_{_slug(grp)}",
                                default_credit_days=7, select_by_type=True)
            st.divider()
        # Any non-pump account not present in the CSV mapping.
        uncl = pay_led[(~has_code) & grp_of.isna()]
        if _has_displayable_accounts(uncl):
            st.markdown("### 🏢 Vendors — Unclassified")
            st.caption("Current-Liabilities accounts not assigned to any group in Group.xlsx.")
            render_aging_ledger(uncl, "pay_grp_unclassified",
                                default_credit_days=7, select_by_type=True)
    else:
        # Fallback until vendor_groups.csv is added: Vendors + Associate Creditor.
        st.info("Add **vendor_groups.csv** (columns: Group, Vendor Name) to the project "
                "folder to split these into per-group tables. Showing the default split.")
        is_assoc = pay_led["account_name"].map(_norm_name).isin(ASSOCIATE_CREDITOR_NORM)
        st.markdown("### 🏢 Vendors (non-pump)")
        render_aging_ledger(pay_led[(~has_code) & ~excl & ~is_assoc], "payables_vendor",
                            default_credit_days=7, select_by_type=True)
        st.divider()
        st.markdown("### 🤝 Associate Creditor")
        render_aging_ledger(pay_led[(~has_code) & ~excl & is_assoc], "payables_assoc",
                            default_credit_days=7, select_by_type=True)

with tab_rec:
    st.caption(
        "Net Outstanding split into due-date aging buckets — Overdue (past due) "
        f"plus amounts coming due in the next windows (today {TODAY:%d/%m/%Y})."
    )
    # Receivables = accounts classified as "Current Assets" (AccountType).
    rec_led = led[led["account_type"] == "Current Assets"]
    # Exclude Enroute vendors and Control-AC & others (they have their own tabs).
    excl = _is_dedicated(rec_led)
    # Main table = accounts whose name contains one of the keep-keywords.
    is_keep = rec_led["account_name"].fillna("").str.contains(
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
    render_grouped_aging_ledger(rec_led[is_keep & ~excl], "receivables",
                                group_map=_oem_group, bins=RECEIVABLE_BINS,
                                sign="receivable", name_header="Account Name",
                                apply_defaults=True, overdue_popup=True,
                                select_by_type=True)

    st.divider()

    st.markdown("### 📋 Market load Accounts")
    render_aging_ledger(rec_led[~is_keep & ~excl], "receivables_split",
                        bins=RECEIVABLE_BINS, sign="receivable",
                        default_credit_days=7, select_by_type=True)

with tab_all:
    st.markdown("### 📊 Aging summary — Payables vs Receivables")
    st.caption(
        "Net outstanding split into due-date buckets — Overdue (past due) and amounts "
        f"coming due next (today {TODAY:%d/%m/%Y}). Accounts with no DB `credit_days` "
        "use a 7-day default. Classified by AccountType — totals match the KPI row."
    )
    render_aging_summary(df, "all", bins=RECEIVABLE_BINS)

    st.divider()

    st.markdown("### 📅 Expected Collection — OEM Receivables (week-wise)")
    st.caption("Built from the OEM Accounts (Receivables) — expected collection per "
               "OEM group across the coming calendar weeks.")
    _rec_all = led[led["account_type"] == "Current Assets"]
    _oem_keep = _rec_all["account_name"].fillna("").str.contains(
        RECEIVABLE_KEEP_PATTERN, case=False, regex=True)
    render_collection_report(_rec_all[_oem_keep & ~_is_dedicated(_rec_all)], "all")

    st.divider()

    # Full (KPI) frame kept for the aging chart so it matches the summary table.
    _summary_src = df
    # Charts / detail below exclude accounts that live in a dedicated tab.
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
        st.caption("Same buckets and figures as the aging summary table above "
                   "(classified by AccountType, with the 7-day default).")
        summ, bucket_cols = aging_summary_frame(_summary_src,
                                                bins=RECEIVABLE_BINS)
        long = summ.melt(id_vars="Type", value_vars=bucket_cols,
                         var_name="Bucket", value_name="Amount")
        long["label"] = long["Amount"].map(inr)  # Indian Cr/L labels (match the table)
        fig = px.bar(
            long, x="Bucket", y="Amount", color="Type", barmode="group",
            text="label",
            category_orders={"Bucket": bucket_cols,
                             "Type": ["Payables (we owe)",
                                      "Receivables (owed to us)", "Net balance"]},
            color_discrete_map={"Payables (we owe)": "#f85149",
                                "Receivables (owed to us)": "#3fb950",
                                "Net balance": "#8b949e"},
        )
        fig.update_traces(textposition="outside", cliponaxis=False)
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
