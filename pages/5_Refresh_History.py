"""
5_Refresh_History.py -- Dataset Refresh History (Last 30 Days)
PBI Intelligence Platform
"""
from __future__ import annotations
import datetime, json, subprocess, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Refresh History", page_icon="🔄", layout="wide")
st.markdown("""
<style>
[data-testid="stSidebar"]{background:#0d1b2a;}
[data-testid="stSidebar"] *{color:#e0e8f0;}
</style>""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### PBI Intelligence Platform")
    st.markdown("---")

# ── Constants ─────────────────────────────────────────────────────────────────
PBI         = "https://api.powerbi.com/v1.0/myorg"
STORE_DIR   = Path(__file__).parent.parent / "refresh_store"
STORE_DIR.mkdir(exist_ok=True)

# ── Token acquisition — MSAL username/password (ROPC) ────────────────────────
_PBI_SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
_CLIENT_ID = "04b07795-8542-4c4c-8b8b-6c5c1f2b9543"  # Azure CLI public client

def _msal_app(tenant_id: str):
    try:
        import msal
    except ImportError:
        raise RuntimeError("`msal` package not installed. Run: pip install msal")
    authority = f"https://login.microsoftonline.com/{tenant_id.strip()}"
    return msal.PublicClientApplication(_CLIENT_ID, authority=authority)

def acquire_token_by_password(tenant_id: str, username: str, password: str) -> str:
    """Sign in with Power BI email + password (ROPC flow). No browser needed."""
    app = _msal_app(tenant_id)
    # Try cached token first
    accounts = app.get_accounts(username=username)
    if accounts:
        result = app.acquire_token_silent(_PBI_SCOPE, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]
    result = app.acquire_token_by_username_password(
        username=username.strip(),
        password=password,
        scopes=_PBI_SCOPE,
    )
    if "access_token" in result:
        return result["access_token"]
    err = result.get("error_description") or result.get("error") or str(result)
    raise RuntimeError(err)

# ── API helpers ───────────────────────────────────────────────────────────────
def fetch_dataset_name(token, wid, did):
    r = requests.get(f"{PBI}/groups/{wid}/datasets/{did}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=15)
    return r.json().get("name", did) if r.ok else did

def fetch_refreshes(token, wid, did) -> list[dict]:
    r = requests.get(
        f"{PBI}/groups/{wid}/datasets/{did}/refreshes?$top=200",
        headers={"Authorization": f"Bearer {token}"}, timeout=15,
    )
    if r.status_code == 401: raise RuntimeError("401 – token expired, run az login")
    if r.status_code == 403: raise RuntimeError("403 – no access to this workspace/dataset")
    if r.status_code == 404: raise RuntimeError("404 – workspace or dataset ID not found")
    r.raise_for_status()
    return r.json().get("value", [])

# ── Activity Log API (admin) ─────────────────────────────────────────────────
def fetch_activity_log_day(token: str, date: datetime.date) -> list[dict]:
    """Fetch all Power BI activity events for one UTC day — requires tenant admin."""
    # API requires ISO 8601 without milliseconds or Z suffix, wrapped in single quotes
    start = f"{date}T00:00:00"
    end   = f"{date}T23:59:59"
    base  = "https://api.powerbi.com/v1.0/myorg/admin/activityevents"
    params = {"startDateTime": f"'{start}'", "endDateTime": f"'{end}'"}
    events = []
    next_url = None
    while True:
        if next_url:
            r = requests.get(next_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        else:
            r = requests.get(base, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if r.status_code == 403:
            raise RuntimeError("Activity Log API requires Power BI tenant admin access (403)")
        r.raise_for_status()
        data = r.json()
        events.extend(data.get("activityEventEntities", []))
        next_url = data.get("continuationUri")
        if not next_url:
            break
    return events

def get_fetched_al_dates(did: str) -> set:
    p = STORE_DIR / f"{did}_al_dates.json"
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def mark_al_dates_fetched(did: str, dates: list) -> None:
    existing = get_fetched_al_dates(did)
    existing.update(dates)
    p = STORE_DIR / f"{did}_al_dates.json"
    p.write_text(json.dumps(sorted(existing)), encoding="utf-8")

def fetch_and_store_activity_log(token: str, did: str, days: int, progress_cb=None) -> tuple[int, list[str]]:
    """
    Fetch Activity Log for any days not already stored (always re-fetches today + yesterday).
    Saves results into the main refresh store.
    Returns (new_record_count, list_of_errors).
    """
    today = datetime.date.today()
    fetched_dates = get_fetched_al_dates(did)
    force_refetch = {str(today), str(today - datetime.timedelta(days=1))}

    days_to_fetch = [
        today - datetime.timedelta(days=offset)
        for offset in range(days)
        if str(today - datetime.timedelta(days=offset)) not in fetched_dates
        or str(today - datetime.timedelta(days=offset)) in force_refetch
    ]

    if not days_to_fetch:
        return 0, []

    new_records   = []
    newly_fetched = []
    errors        = []

    def _fetch_one(day):
        """Fetch and filter one day; returns (day, records, error_or_None)."""
        try:
            events = fetch_activity_log_day(token, day)
        except Exception as e:
            return day, [], str(e)
        recs = []
        for ev in events:
            ds_id = (ev.get("DatasetId") or ev.get("ArtifactId") or "").lower()
            if ds_id != did.lower():
                continue
            op = (ev.get("Operation") or ev.get("Activity") or "").lower()
            if "refresh" not in op:
                continue
            ts     = ev.get("CreationTime", "")
            status = "Completed" if ev.get("IsSuccess", True) else "Failed"
            recs.append({
                "requestId": ev.get("RequestId") or ev.get("Id", ""),
                "startTime": ts,
                "endTime":   None,
                "status":    status,
                "_source":   "activity_log",
            })
        return day, recs, None

    total = len(days_to_fetch)
    done  = 0
    # 8 parallel workers — stays well within Power BI rate limits
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_one, d): d for d in days_to_fetch}
        for fut in as_completed(futures):
            day, recs, err = fut.result()
            done += 1
            if progress_cb:
                progress_cb(done / total, f"Activity Log: {done}/{total} days fetched…")
            if err:
                errors.append(f"{day}: {err}")
            else:
                new_records.extend(recs)
                newly_fetched.append(str(day))

    if progress_cb:
        progress_cb(1.0, "Activity Log: done")
    if new_records:
        save_merged(did, new_records)
    if newly_fetched:
        mark_al_dates_fetched(did, newly_fetched)
    return len(new_records), errors

# ── Local history store ───────────────────────────────────────────────────────
def load_stored(did: str) -> list[dict]:
    """Load previously persisted refresh records for this dataset."""
    p = STORE_DIR / f"{did}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

def save_merged(did: str, records: list[dict]) -> None:
    """Merge new records with stored ones (deduplicate by requestId) and save."""
    stored = load_stored(did)
    # Index stored by requestId; fall back to startTime for entries without requestId
    def key(r):
        return r.get("requestId") or r.get("startTime", "")
    index = {key(r): r for r in stored}
    for r in records:
        k = key(r)
        existing = index.get(k)
        # Prefer refresh-history records (have endTime/duration) over activity-log records
        if existing and existing.get("_source") != "activity_log" and r.get("_source") == "activity_log":
            continue
        index[k] = r
    merged = sorted(index.values(), key=lambda r: r.get("startTime", ""), reverse=True)
    p = STORE_DIR / f"{did}.json"
    p.write_text(json.dumps(merged, indent=2), encoding="utf-8")

# ── Build table ───────────────────────────────────────────────────────────────
def build_table(records: list[dict], dataset_name: str, days: int = 30) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["startTime"] = pd.to_datetime(df.get("startTime"), utc=True, errors="coerce")
    df["endTime"]   = pd.to_datetime(df.get("endTime"),   utc=True, errors="coerce")

    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    df = df[df["startTime"] >= cutoff].copy()
    if df.empty:
        return df

    df["date"]     = df["startTime"].dt.date
    df["status"]   = df["status"].fillna("Unknown").str.capitalize()
    df["duration"] = ((df["endTime"] - df["startTime"]).dt.total_seconds() / 60).round(1)
    # Activity log records have no endTime → duration=0 is meaningless, clear it
    df.loc[df["duration"] == 0, "duration"] = None
    # Use startTime for time display when endTime is missing (activity log records)
    time_ref = df["endTime"].fillna(df["startTime"])
    df["IST"] = time_ref.dt.tz_convert("Asia/Kolkata").dt.strftime("%I:%M %p")
    df["CST"] = time_ref.dt.tz_convert("America/Chicago").dt.strftime("%I:%M %p")

    today       = datetime.date.today()
    cutoff_date = today - datetime.timedelta(days=29)

    rows = []
    for d, grp in df.groupby("date"):
        last    = grp.sort_values("startTime").iloc[-1]
        avg_dur = grp["duration"].dropna().mean()
        status  = "Failed" if (grp["status"] == "Failed").any() else last["status"]
        rows.append({
            "Date":               d,
            "Dataset":            dataset_name,
            "Last Completed (IST)": last["IST"],
            "Last Completed (CST)": last["CST"],
            "Refreshes":          len(grp),
            "Avg Duration (min)": round(avg_dur, 1) if pd.notna(avg_dur) else None,
            "Status":             status,
        })

    done_dates = {r["Date"] for r in rows}
    today = datetime.date.today()
    # Only fill "No Refresh" gaps from the EARLIEST actual API record to today.
    # Days before the earliest API record are unknown — the API caps at ~60 entries
    # so older history simply isn't available, not necessarily "No Refresh".
    if done_dates:
        window_start = min(done_dates)
        d = window_start
        while d <= today:
            if d not in done_dates:
                rows.append({"Date": d, "Dataset": dataset_name,
                             "Last Completed (IST)": "—", "Last Completed (CST)": "—",
                             "Refreshes": 0, "Avg Duration (min)": None, "Status": "No Refresh"})
            d += datetime.timedelta(days=1)

    out = pd.DataFrame(rows)
    out = out.sort_values("Date", ascending=False).reset_index(drop=True)
    out["Avg Duration (min)"] = pd.to_numeric(out["Avg Duration (min)"], errors="coerce")
    # Keep only display columns in a fixed order
    cols = ["Date", "Dataset", "Last Completed (IST)", "Last Completed (CST)",
            "Refreshes", "Avg Duration (min)", "Status"]
    return out[[c for c in cols if c in out.columns]]

# ── Sidebar ───────────────────────────────────────────────────────────────────
DEFAULT_WS = "466083da-5ff6-432b-8c54-8d39393bc81c"
DEFAULT_DS = "38767ebb-5030-41b3-a15b-23a00bb73cfa"

with st.sidebar:
    st.markdown("### 🔐 Power BI Sign-In")
    tenant_id = st.text_input(
        "Azure Tenant ID",
        value=st.session_state.get("pbi_tenant_id", ""),
        placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        help="portal.azure.com → Azure Active Directory → Overview → Tenant ID",
    )
    pbi_username = st.text_input(
        "Work Email",
        value=st.session_state.get("pbi_username", ""),
        placeholder="you@company.com",
    )
    pbi_password = st.text_input(
        "Password",
        type="password",
        placeholder="Your Power BI password",
    )
    sign_in_btn = st.button("🔐 Sign In", use_container_width=True, type="primary",
                            disabled=not (tenant_id and pbi_username and pbi_password))
    if tenant_id:
        st.session_state["pbi_tenant_id"] = tenant_id
    if pbi_username:
        st.session_state["pbi_username"] = pbi_username
    st.markdown("---")
    st.markdown("### �📋 Datasets")
    workspace_id = st.text_input("Workspace ID", value=DEFAULT_WS)
    st.markdown("**Dataset IDs** (one per line):")
    datasets_raw = st.text_area("", value=DEFAULT_DS, height=130,
                                placeholder="paste dataset ID here")
    st.markdown("---")
    days_to_show = st.slider("History window (days)", min_value=7, max_value=180,
                             value=30, step=7,
                             help="Fetches from API + local store. More days = deeper history as store grows.")
    use_activity_log = st.checkbox(
        "📋 Include Activity Log (admin)",
        value=True,
        help="Uses the Power BI Admin Activity Events API (30-day retention) to fill history gaps beyond the Refresh History API cap.",
    )
    fetch_btn = st.button("🔄 Fetch", use_container_width=True, type="primary")

# ── Main ──────────────────────────────────────────────────────────────────────
st.title(f"🔄 Refresh History — Last {days_to_show} Days")

# ── Authentication ────────────────────────────────────────────────────────────
if "pbi_token" not in st.session_state:
    st.session_state["pbi_token"] = None

# Handle sign-in button click
if sign_in_btn and pbi_username and pbi_password and tenant_id:
    with st.spinner("Signing in…"):
        try:
            tok = acquire_token_by_password(tenant_id, pbi_username, pbi_password)
            st.session_state["pbi_token"] = tok
            st.rerun()
        except Exception as e:
            st.error(f"Sign-in failed: {e}")
            st.stop()

if not st.session_state["pbi_token"]:
    st.info(
        "Enter your **Azure Tenant ID**, **Work Email** and **Password** "
        "in the sidebar, then click **Sign In**.",
        icon="🔐",
    )
    st.stop()

token = st.session_state["pbi_token"]

# Sign-out button in sidebar
with st.sidebar:
    if st.button("🚪 Sign Out", use_container_width=True):
        st.session_state["pbi_token"] = None
        st.rerun()

if not fetch_btn:
    st.info("Enter Dataset IDs in the sidebar and click Fetch.", icon="ℹ️")
    st.stop()

dataset_ids = [d.strip() for d in datasets_raw.splitlines() if d.strip()]
if not workspace_id.strip() or not dataset_ids:
    st.error("Need Workspace ID and at least one Dataset ID.")
    st.stop()

for did in dataset_ids:
    try:
        with st.spinner(f"Fetching refresh history for {did}…"):
            dname    = fetch_dataset_name(token, workspace_id.strip(), did)
            api_recs = fetch_refreshes(token, workspace_id.strip(), did)
            save_merged(did, api_recs)   # persist to local store

        # ── Activity Log (admin) ──────────────────────────────────────────────
        al_new, al_errors = 0, []
        if use_activity_log:
            prog = st.progress(0.0, text="Activity Log: starting…")
            try:
                al_new, al_errors = fetch_and_store_activity_log(
                    token, did, days=min(days_to_show, 30),
                    progress_cb=lambda v, t: prog.progress(min(v, 1.0), text=t),
                )
            except Exception as al_err:
                al_errors = [str(al_err)]
            finally:
                prog.empty()

        all_recs     = load_stored(did)
        table        = build_table(all_recs, dname, days=days_to_show)
        stored_count = len(all_recs)

        st.subheader(f"📊 {dname}")
        al_note = f" · Activity Log added {al_new} new records" if al_new else ""
        st.caption(f"Refresh API: {len(api_recs)} records · Store total: {stored_count}{al_note}")
        if al_errors:
            with st.expander(f"⚠️ Activity Log issues ({len(al_errors)} days failed)", expanded=True):
                for e in al_errors[:10]:
                    st.code(e)

        if table.empty:
            st.warning("No refresh records found in the selected window. The local store will grow as you fetch regularly.")
            continue

        # Warn if coverage is shorter than requested window
        earliest_date = table["Date"].min()
        cutoff_date   = (pd.Timestamp.now() - pd.Timedelta(days=days_to_show)).date()
        if earliest_date > cutoff_date:
            days_covered = (pd.Timestamp.now().date() - earliest_date).days + 1
            al_hint = " Enable 'Include Activity Log' for deeper coverage." if not use_activity_log else ""
            st.info(
                f"Earliest record: **{earliest_date}** ({days_covered} days of data).{al_hint}",
                icon="ℹ️",
            )

        total  = int(table["Refreshes"].sum())
        days_w = int((table["Status"] == "Completed").sum())
        days_f = int((table["Status"] == "Failed").sum())
        days_n = int((table["Status"] == "No Refresh").sum())

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total Refreshes",   total)
        k2.metric("Days Completed",    days_w)
        k3.metric("Days Failed",       days_f)
        k4.metric("Days No Refresh",   days_n)

        st.dataframe(table, use_container_width=True, height=500)

        csv = table.to_csv(index=False).encode()
        st.download_button(f"⬇️ Download CSV", csv,
                           file_name=f"refresh_{dname[:30]}.csv", mime="text/csv")
        st.markdown("---")

    except Exception as e:
        st.error(f"**{did}**: {e}")
