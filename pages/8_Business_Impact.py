"""
8_Business_Impact.py — Business Impact Engine
PBI Intelligence Platform

Translates technical debt into £ cost, annual compute waste, and ROI of cleanup.
Works entirely from the uploaded .pbix file — no API key needed.
"""

import sys
import io
import json
import math
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

st.set_page_config(
    page_title="Business Impact — PBI Intelligence Platform",
    page_icon="💷",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
[data-testid="stSidebar"] { background-color: #0d1b2a; }
[data-testid="stSidebar"] * { color: #e0e8f0; }
[data-testid="stSidebar"] h1 {
    color:#fff; font-size:1.4rem; font-weight:700;
    padding-bottom:.5rem; border-bottom:1px solid #1e3a5f;
}
.impact-card {
    background:#fff; border-radius:12px; padding:20px 24px;
    box-shadow:0 2px 8px rgba(0,0,0,.08); margin-bottom:16px;
}
.kpi-red    { background:#fef2f2; border-left:5px solid #dc2626; border-radius:8px; padding:16px 20px; }
.kpi-amber  { background:#fffbeb; border-left:5px solid #d97706; border-radius:8px; padding:16px 20px; }
.kpi-green  { background:#f0fdf4; border-left:5px solid #16a34a; border-radius:8px; padding:16px 20px; }
.kpi-blue   { background:#eff6ff; border-left:5px solid #2563eb; border-radius:8px; padding:16px 20px; }
.kpi-val    { font-size:2rem; font-weight:800; margin:4px 0; }
.kpi-lbl    { font-size:.82rem; color:#64748b; text-transform:uppercase; letter-spacing:.05em; }
.kpi-sub    { font-size:.9rem; color:#475569; margin-top:2px; }
.section-hdr { font-size:1.1rem; font-weight:700; color:#1e293b; margin:24px 0 12px; border-bottom:2px solid #e2e8f0; padding-bottom:6px; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("<h1>PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")
    st.markdown("### ⚙️ Cost Parameters")
    st.caption("Adjust to match your organisation's costs.")

    hourly_rate = st.number_input(
        "Developer hourly rate (£)",
        min_value=10, max_value=500, value=60, step=5,
        help="Used to estimate time cost of maintaining complex measures."
    )
    refresh_cost_per_hour = st.number_input(
        "Compute cost per refresh-hour (£)",
        min_value=0.01, max_value=100.0, value=0.50, step=0.05, format="%.2f",
        help="Azure / Premium capacity cost per hour of dataset refresh."
    )
    refreshes_per_day = st.number_input(
        "Refreshes per day",
        min_value=1, max_value=96, value=4,
        help="Scheduled refresh count (for annualised compute cost)."
    )
    working_days = st.number_input(
        "Working days per year",
        min_value=100, max_value=365, value=250,
    )
    st.markdown("---")
    currency = st.selectbox("Currency symbol", ["£", "$", "€", "₹"], index=0)

# ─────────────────────────────────────────────────────────────────────────────
# Project module import
# ─────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from pbit_extractor import extract_pbit_metadata

# ─────────────────────────────────────────────────────────────────────────────
# File input
# ─────────────────────────────────────────────────────────────────────────────
st.title("💷 Business Impact Engine")
st.markdown(
    "Translates every byte of technical debt into **real money** — "
    "developer time wasted, compute overspend, and the ROI of fixing it."
)

uploaded = st.file_uploader(
    "Upload a .pbix or .pbit file",
    type=["pbix", "pbit"],
    key="biz_impact_upload",
)

# Prefer session-state file (shared from landing page)
raw_bytes: bytes | None = None
if uploaded:
    raw_bytes = uploaded.read()
    st.session_state["pbi_file_bytes"] = raw_bytes
    st.session_state["pbi_file_name"] = uploaded.name
elif st.session_state.get("pbi_file_bytes"):
    raw_bytes = st.session_state["pbi_file_bytes"]
    fname = st.session_state.get("pbi_file_name", "Previously uploaded file")
    st.info(f"Using previously uploaded file: **{fname}**")

if not raw_bytes:
    st.warning("Upload a .pbix or .pbit file to get started.")
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# Parse
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner="Analysing report…")
def _parse(data: bytes):
    return extract_pbit_metadata(io.BytesIO(data))

meta = _parse(raw_bytes)
measures    = meta.get("measures", [])
columns     = meta.get("columns", [])
tables      = meta.get("tables", [])
rels        = meta.get("relationships", [])

# ─────────────────────────────────────────────────────────────────────────────
# Scoring helpers
# ─────────────────────────────────────────────────────────────────────────────
ANTI_PATTERNS = [
    "FILTER(ALL", "FILTER(ALL(", "CALCULATE(FILTER", "SUMX(FILTER",
    "COUNTX(FILTER", "AVERAGEX(FILTER", "RANKX(ALL", "EARLIER(",
    "CROSSJOIN(", "GENERATE(", "TOPN(", "IF(ISBLANK(CALCULATE(",
]

def _complexity_score(expr: str) -> int:
    """0-10 score based on nesting depth, iterator usage, and row context."""
    if not expr:
        return 0
    depth       = max(expr.count("(") - expr.count(")"), expr.count("("))
    iterators   = sum(1 for fn in ("SUMX","COUNTX","AVERAGEX","MINX","MAXX","RANKX","GENERATE","CROSSJOIN") if fn in expr.upper())
    anti        = sum(1 for p in ANTI_PATTERNS if p.upper() in expr.upper())
    score = min(10, round(depth * 0.15 + iterators * 1.5 + anti * 2))
    return score

def _maintenance_hours(complexity: int) -> float:
    """Estimated hours/year to maintain a measure at given complexity."""
    # Simple: complexity 0-3 → 0.5h, 4-6 → 2h, 7-10 → 5h
    if complexity <= 3:  return 0.5
    if complexity <= 6:  return 2.0
    return 5.0

# ─────────────────────────────────────────────────────────────────────────────
# Build measure analysis dataframe
# ─────────────────────────────────────────────────────────────────────────────
rows = []
for m in measures:
    expr    = m.get("expression", "") or ""
    cplx    = _complexity_score(expr)
    maint_h = _maintenance_hours(cplx)
    anti    = [p for p in ANTI_PATTERNS if p.upper() in expr.upper()]
    undoc   = not (m.get("description") or "").strip()
    rows.append({
        "Table":            m.get("table", ""),
        "Measure":          m.get("name", ""),
        "Complexity Score": cplx,
        "Anti-Patterns":    len(anti),
        "Anti-Pattern List": ", ".join(anti) if anti else "—",
        "Undocumented":     undoc,
        "Maint Hours/yr":   maint_h,
        "Maint Cost/yr":    round(maint_h * hourly_rate, 2),
        "Expression":       expr[:120] + ("…" if len(expr) > 120 else ""),
    })

df_measures = pd.DataFrame(rows) if rows else pd.DataFrame()

# ─────────────────────────────────────────────────────────────────────────────
# Column analysis
# ─────────────────────────────────────────────────────────────────────────────
col_rows = []
for c in columns:
    col_rows.append({
        "Table":       c.get("table", ""),
        "Column":      c.get("name", ""),
        "Type":        c.get("dataType", "unknown"),
        "Hidden":      c.get("isHidden", False),
        "Calculated":  bool(c.get("expression")),
    })
df_cols = pd.DataFrame(col_rows) if col_rows else pd.DataFrame()

hidden_cols  = len(df_cols[df_cols["Hidden"]]) if not df_cols.empty else 0
calc_cols    = len(df_cols[df_cols["Calculated"]]) if not df_cols.empty else 0

# ─────────────────────────────────────────────────────────────────────────────
# KPI calculations
# ─────────────────────────────────────────────────────────────────────────────
total_measures   = len(measures)
undoc_measures   = int(df_measures["Undocumented"].sum()) if not df_measures.empty else 0
high_complexity  = int((df_measures["Complexity Score"] >= 7).sum()) if not df_measures.empty else 0
total_anti       = int(df_measures["Anti-Patterns"].sum()) if not df_measures.empty else 0
annual_maint_cost= round(df_measures["Maint Cost/yr"].sum(), 2) if not df_measures.empty else 0

# Compute cost model: each high-complexity measure adds ~5% to refresh time
# Base assumption: one refresh = 10 minutes. High-complexity penalty accumulates.
base_refresh_min = 10
complexity_penalty_min = high_complexity * 0.5
total_refresh_min = base_refresh_min + complexity_penalty_min
annual_refresh_hours = (total_refresh_min / 60) * refreshes_per_day * working_days
annual_compute_cost = round(annual_refresh_hours * refresh_cost_per_hour, 2)

# ROI: fix all high-complexity measures → assume 40% complexity reduction
roi_maint_saving = round(annual_maint_cost * 0.4, 2)
roi_compute_saving = round(annual_compute_cost * 0.3, 2)
total_roi = roi_maint_saving + roi_compute_saving

# Fix effort: 2h per high-complexity measure, 0.5h per undoc measure
fix_hours = high_complexity * 2 + undoc_measures * 0.5
fix_cost  = round(fix_hours * hourly_rate, 2)
payback_months = round((fix_cost / (total_roi / 12)), 1) if total_roi > 0 else 999

# ─────────────────────────────────────────────────────────────────────────────
# Top KPI cards
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">📊 Executive Summary</p>', unsafe_allow_html=True)

c1, c2, c3, c4 = st.columns(4)
with c1:
    st.markdown(f"""
    <div class="kpi-red">
      <div class="kpi-lbl">Annual Maintenance Cost</div>
      <div class="kpi-val">{currency}{annual_maint_cost:,.0f}</div>
      <div class="kpi-sub">{total_measures} measures × avg complexity</div>
    </div>""", unsafe_allow_html=True)

with c2:
    st.markdown(f"""
    <div class="kpi-amber">
      <div class="kpi-lbl">Annual Compute Waste</div>
      <div class="kpi-val">{currency}{annual_compute_cost:,.0f}</div>
      <div class="kpi-sub">{refreshes_per_day} refreshes/day × {working_days} days</div>
    </div>""", unsafe_allow_html=True)

with c3:
    st.markdown(f"""
    <div class="kpi-green">
      <div class="kpi-lbl">ROI of Full Cleanup</div>
      <div class="kpi-val">{currency}{total_roi:,.0f}/yr</div>
      <div class="kpi-sub">Payback in ~{payback_months} months</div>
    </div>""", unsafe_allow_html=True)

with c4:
    st.markdown(f"""
    <div class="kpi-blue">
      <div class="kpi-lbl">Fix Investment</div>
      <div class="kpi-val">{currency}{fix_cost:,.0f}</div>
      <div class="kpi-sub">{fix_hours:.0f}h estimated effort</div>
    </div>""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Secondary metrics
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("")
m1, m2, m3, m4, m5 = st.columns(5)
with m1:
    st.metric("Total Measures",       total_measures)
with m2:
    st.metric("High Complexity (7+)", high_complexity,
              delta=f"{round(high_complexity/max(total_measures,1)*100)}% of total",
              delta_color="inverse")
with m3:
    st.metric("Anti-Pattern Hits",    total_anti, delta_color="inverse")
with m4:
    st.metric("Undocumented",         undoc_measures,
              delta=f"{round(undoc_measures/max(total_measures,1)*100)}%",
              delta_color="inverse")
with m5:
    st.metric("Hidden Columns",       hidden_cols)

# ─────────────────────────────────────────────────────────────────────────────
# Charts
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">📈 Cost Breakdown</p>', unsafe_allow_html=True)

if not df_measures.empty:
    col_chart1, col_chart2 = st.columns(2)

    with col_chart1:
        # Complexity distribution
        fig_hist = px.histogram(
            df_measures,
            x="Complexity Score",
            nbins=11,
            color_discrete_sequence=["#3b82f6"],
            title="Measure Complexity Distribution",
            labels={"Complexity Score": "Complexity (0–10)", "count": "# Measures"},
        )
        fig_hist.update_layout(
            plot_bgcolor="#f8fafc", paper_bgcolor="#f8fafc",
            title_font_size=14, margin=dict(t=40, b=20, l=20, r=20),
        )
        st.plotly_chart(fig_hist, use_container_width=True)

    with col_chart2:
        # Cost by table
        tbl_cost = df_measures.groupby("Table")["Maint Cost/yr"].sum().reset_index()
        tbl_cost = tbl_cost.sort_values("Maint Cost/yr", ascending=False).head(12)
        fig_bar = px.bar(
            tbl_cost,
            x="Maint Cost/yr",
            y="Table",
            orientation="h",
            color="Maint Cost/yr",
            color_continuous_scale="Reds",
            title=f"Maintenance Cost by Table ({currency}/yr)",
            labels={"Maint Cost/yr": f"Cost ({currency}/yr)", "Table": ""},
        )
        fig_bar.update_layout(
            plot_bgcolor="#f8fafc", paper_bgcolor="#f8fafc",
            title_font_size=14, margin=dict(t=40, b=20, l=20, r=20),
            showlegend=False, coloraxis_showscale=False,
            yaxis=dict(autorange="reversed"),
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    # Waterfall: cost components vs savings
    st.markdown('<p class="section-hdr">💰 ROI Waterfall</p>', unsafe_allow_html=True)
    fig_waterfall = go.Figure(go.Waterfall(
        name="ROI",
        orientation="v",
        measure=["absolute", "absolute", "relative", "relative", "total"],
        x=["Fix Investment", "Annual Maint Cost", "Maint Saving (40%)", "Compute Saving (30%)", "Net Benefit Year 1"],
        y=[-fix_cost, -annual_maint_cost, roi_maint_saving, roi_compute_saving, 0],
        connector={"line": {"color": "#94a3b8"}},
        decreasing={"marker": {"color": "#ef4444"}},
        increasing={"marker": {"color": "#22c55e"}},
        totals={"marker": {"color": "#3b82f6"}},
        text=[f"{currency}{abs(x):,.0f}" for x in [-fix_cost, -annual_maint_cost, roi_maint_saving, roi_compute_saving, roi_maint_saving + roi_compute_saving - fix_cost]],
        textposition="outside",
    ))
    fig_waterfall.update_layout(
        title="First-Year ROI Waterfall",
        title_font_size=15,
        plot_bgcolor="#f8fafc",
        paper_bgcolor="#f8fafc",
        margin=dict(t=50, b=30, l=20, r=20),
        yaxis_title=f"Cost / Saving ({currency})",
        showlegend=False,
    )
    st.plotly_chart(fig_waterfall, use_container_width=True)

# ─────────────────────────────────────────────────────────────────────────────
# Priority fix list
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">🎯 Priority Fix List — Biggest Bang for Buck</p>', unsafe_allow_html=True)

if not df_measures.empty:
    df_priority = df_measures[df_measures["Complexity Score"] >= 5].copy()
    df_priority = df_priority.sort_values(["Maint Cost/yr", "Complexity Score"], ascending=False)
    df_priority = df_priority[["Table", "Measure", "Complexity Score", "Anti-Patterns",
                                 "Maint Cost/yr", "Undocumented", "Expression"]].reset_index(drop=True)
    df_priority["Maint Cost/yr"] = df_priority["Maint Cost/yr"].apply(lambda x: f"{currency}{x:,.0f}")

    if df_priority.empty:
        st.success("No high-complexity measures found — great shape!")
    else:
        st.caption(f"Showing {len(df_priority)} measures with complexity ≥ 5. "
                   f"Fix these first for maximum ROI.")
        st.dataframe(df_priority, use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────────────────────────────────────
# Full measure cost table
# ─────────────────────────────────────────────────────────────────────────────
with st.expander("📋 All Measures — Full Cost Table"):
    if not df_measures.empty:
        df_show = df_measures[["Table", "Measure", "Complexity Score",
                                "Anti-Patterns", "Undocumented",
                                "Maint Hours/yr", "Maint Cost/yr", "Expression"]].copy()
        df_show["Maint Cost/yr"] = df_show["Maint Cost/yr"].apply(lambda x: f"{currency}{x:,.2f}")
        st.dataframe(df_show, use_container_width=True, hide_index=True)
    else:
        st.info("No measures found in this file.")

# ─────────────────────────────────────────────────────────────────────────────
# Column health
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">🗂️ Column Health</p>', unsafe_allow_html=True)
c1, c2, c3 = st.columns(3)
with c1:
    st.metric("Total Columns",      len(columns))
with c2:
    st.metric("Calculated Columns", calc_cols,
              help="Calculated columns expand model size and slow refresh.")
with c3:
    st.metric("Hidden Columns",     hidden_cols,
              help="Hidden columns may be safe to remove — check dependencies first.")

if not df_cols.empty:
    fig_col_types = px.pie(
        df_cols.groupby("Type").size().reset_index(name="Count"),
        names="Type", values="Count",
        title="Column Data Types",
        color_discrete_sequence=px.colors.qualitative.Set3,
    )
    fig_col_types.update_layout(
        paper_bgcolor="#f8fafc", margin=dict(t=50, b=10, l=10, r=10),
        title_font_size=14,
    )
    col_pie, col_note = st.columns([1, 1])
    with col_pie:
        st.plotly_chart(fig_col_types, use_container_width=True)
    with col_note:
        st.markdown(f"""
        **Column cost notes:**
        - **{calc_cols} calculated columns** each bloat the model because values are
          pre-calculated and stored per row. Replace with measures where possible.
        - **{hidden_cols} hidden columns** may exist purely for relationships — confirm
          before deleting using the Lineage Engine.
        - Each unnecessary column increases model size, slows refresh, and increases
          Premium capacity cost.
        """)

# ─────────────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">⬇️ Export Report</p>', unsafe_allow_html=True)

if not df_measures.empty:
    import openpyxl
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        summary_df = pd.DataFrame({
            "Metric": [
                "Annual Maintenance Cost",
                "Annual Compute Waste",
                "ROI of Full Cleanup (annual)",
                "Fix Investment",
                "Payback Period (months)",
                "Total Measures",
                "High Complexity Measures",
                "Undocumented Measures",
                "Anti-Pattern Hits",
            ],
            "Value": [
                f"{currency}{annual_maint_cost:,.0f}",
                f"{currency}{annual_compute_cost:,.0f}",
                f"{currency}{total_roi:,.0f}",
                f"{currency}{fix_cost:,.0f}",
                payback_months,
                total_measures,
                high_complexity,
                undoc_measures,
                total_anti,
            ],
        })
        summary_df.to_excel(writer, sheet_name="Executive Summary", index=False)
        df_measures.to_excel(writer, sheet_name="All Measures", index=False)
        if not df_cols.empty:
            df_cols.to_excel(writer, sheet_name="All Columns", index=False)

    st.download_button(
        "📥 Download Business Impact Report (.xlsx)",
        data=out.getvalue(),
        file_name="business_impact_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
