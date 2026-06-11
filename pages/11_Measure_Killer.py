"""
11_Measure_Killer.py — Safe Delete / Measure Killer
PBI Intelligence Platform

Find every truly unused measure and column, confirm it is safe to delete,
and generate the removal script — using the full dependency graph.
"""

import sys
import io
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

st.set_page_config(
    page_title="Measure Killer — PBI Intelligence Platform",
    page_icon="🗑️",
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
.safe-card   { background:#f0fdf4; border-left:5px solid #16a34a; border-radius:8px; padding:14px 18px; margin:6px 0; }
.unsafe-card { background:#fef2f2; border-left:5px solid #dc2626; border-radius:8px; padding:14px 18px; margin:6px 0; }
.warn-card   { background:#fffbeb; border-left:5px solid #d97706; border-radius:8px; padding:14px 18px; margin:6px 0; }
.dep-chip    { display:inline-block; background:#dbeafe; color:#1e40af; padding:2px 10px; border-radius:20px; font-size:.8rem; margin:2px; }
.section-hdr { font-size:1.1rem; font-weight:700; color:#1e293b; margin:24px 0 12px;
    border-bottom:2px solid #e2e8f0; padding-bottom:6px; }
.kill-btn    { background:#dc2626; color:#fff; padding:8px 16px; border-radius:8px; font-weight:700; font-size:.9rem; }
.code-block  { background:#0f172a; color:#e2e8f0; border-radius:8px; padding:16px 18px;
    font-family:monospace; font-size:.85rem; white-space:pre-wrap; line-height:1.6; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("<h1>PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")
    st.markdown("### 🛡️ Safety Settings")
    include_hidden = st.checkbox(
        "Include hidden measures", value=False,
        help="Hidden measures are often internal helpers — include to catch truly unused ones."
    )
    include_base_measures = st.checkbox(
        "Flag base measures (no callers)", value=True,
        help="Measures with no callers are candidates for deletion unless they are used in visuals."
    )
    st.markdown("---")
    st.markdown("""
    **How safe delete works:**
    1. Build the full dependency graph
    2. Find measures/columns with **zero callers**
    3. Mark each as Safe / Warn / Unsafe
    4. Generate the XMLA/Tabular Editor removal script
    """)

# ─────────────────────────────────────────────────────────────────────────────
# Project module imports
# ─────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from pbit_extractor import extract_pbit_metadata
from dependency_graph import build_graph

# ─────────────────────────────────────────────────────────────────────────────
# Page header
# ─────────────────────────────────────────────────────────────────────────────
st.title("🗑️ Measure Killer — Safe Delete")
st.markdown(
    "Find every **truly unused** measure and calculated column in your model, "
    "verify it's safe to delete, and get a ready-to-run removal script."
)

# ─────────────────────────────────────────────────────────────────────────────
# File input
# ─────────────────────────────────────────────────────────────────────────────
uploaded = st.file_uploader(
    "Upload a .pbix or .pbit file",
    type=["pbix", "pbit"],
    key="killer_upload",
)

raw_bytes: bytes | None = None
if uploaded:
    raw_bytes = uploaded.read()
    st.session_state["pbi_file_bytes"] = raw_bytes
    st.session_state["pbi_file_name"]  = uploaded.name
elif st.session_state.get("pbi_file_bytes"):
    raw_bytes = st.session_state["pbi_file_bytes"]
    fname = st.session_state.get("pbi_file_name", "Previously uploaded file")
    st.info(f"Using previously uploaded file: **{fname}**")

if not raw_bytes:
    st.warning("Upload a .pbix or .pbit file to analyse.")
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# Parse + build graph
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner="Building dependency graph…")
def _parse_and_graph(data: bytes):
    meta  = extract_pbit_metadata(io.BytesIO(data))
    graph = build_graph(meta)
    return meta, graph

with st.spinner("Analysing dependencies…"):
    meta, graph = _parse_and_graph(raw_bytes)

measures    = meta.get("measures", [])
columns     = meta.get("columns", [])
callers     = graph.get("callers", {})      # measure → list of measures that call it
broken      = graph.get("broken", [])
measure_map = graph.get("measures", {})     # name → MeasureNode
time_intel  = graph.get("time_intel", set()) # measures using time-intelligence functions
broken_set  = {b["measure"] for b in broken} # measures that have broken refs

# ── Report layout scan results ────────────────────────────────────────────────
vr           = meta.get("visual_refs", {})
layout_found = vr.get("found", False)
visual_refs  = vr.get("visual_refs", set())       # set of measure names used in visuals
page_refs    = vr.get("page_refs", {})             # {page: set[measure]}
page_names   = vr.get("page_names", [])
visual_count = vr.get("visual_count", 0)
report_filter_refs = vr.get("report_filter_refs", set())

# ─────────────────────────────────────────────────────────────────────────────
# Build calc-column → measure caller index
# (a measure with 0 measure callers may still be referenced by a calc column)
# ─────────────────────────────────────────────────────────────────────────────
from dax_parser import parse_dax

col_measure_callers: dict[str, list[str]] = {}   # measure name → calc cols that reference it
for c in columns:
    expr = c.get("expression") or ""
    if not expr:
        continue
    col_label = f"{c.get('table','')}[{c.get('name','')}]"
    refs = parse_dax(expr)
    for ref in refs.unique_measure_refs:
        col_measure_callers.setdefault(ref, []).append(col_label)

# ─────────────────────────────────────────────────────────────────────────────
# Classify every measure — full multi-criteria check
# ─────────────────────────────────────────────────────────────────────────────
# STATUS DEFINITIONS
# ─────────────────
# Unsafe  – called by 1+ other measures (dependency confirmed, do NOT delete)
# Warn    – 0 measure callers BUT any of:
#             • measure is hidden (may be used directly in a visual)
#             • measure uses time-intelligence functions (likely in visuals)
#             • measure is referenced by a calculated column
#             • measure itself has broken/unresolved references
#             • measure has a display folder (intentionally organised, likely used)
# Safe    – 0 callers, visible, no time-intel, not in any calc column,
#           no broken refs, no display folder → genuinely looks unused
# ─────────────────────────────────────────────────────────────────────────────
rows = []
for name, node in measure_map.items():
    caller_list     = callers.get(name, [])
    n_callers       = len(caller_list)
    is_hidden       = bool(getattr(node, "is_hidden", False))
    is_time_intel   = name in time_intel
    col_callers     = col_measure_callers.get(name, [])
    has_col_callers = len(col_callers) > 0
    has_broken      = name in broken_set
    display_folder  = (getattr(node, "display_folder", "") or "").strip()
    has_folder      = bool(display_folder)
    expression_full = (getattr(node, "expression", "") or "").strip()

    if is_hidden and not include_hidden:
        continue

    warn_flags: list[str] = []
    if n_callers > 0:
        status = "Unsafe"
        reason = f"Called by {n_callers} measure(s): {', '.join(caller_list[:3])}" + ("…" if n_callers > 3 else "")
    else:
        # Check all warn criteria
        if is_hidden:
            warn_flags.append("hidden measure")
        if is_time_intel:
            warn_flags.append("uses time-intelligence (likely in visuals)")
        if has_col_callers:
            warn_flags.append(f"used in calc column(s): {', '.join(col_callers[:2])}")
        if has_broken:
            warn_flags.append("has broken/unresolved DAX references")
        if has_folder:
            warn_flags.append(f"in display folder '{display_folder}'")

        if warn_flags:
            status = "Warn"
            reason = "; ".join(warn_flags)
        else:
            status = "Safe"
            reason = "0 callers, visible, no time-intel, not in any calc column"

    rows.append({
        "Table":          getattr(node, "table", ""),
        "Measure":        name,
        "Display Folder": display_folder,
        "Status":         status,
        "Callers":        n_callers,
        "Caller List":    ", ".join(caller_list) if caller_list else "—",
        "Calc Col Refs":  ", ".join(col_callers) if col_callers else "—",
        "Time Intel":     is_time_intel,
        "Hidden":         is_hidden,
        "Broken Refs":    has_broken,
        "Reason":         reason,
        "Expression":     expression_full,
    })

df = pd.DataFrame(rows) if rows else pd.DataFrame()

# Calculated columns — candidates for deletion
calc_col_rows = []
for c in columns:
    if not c.get("expression"):
        continue
    calc_col_rows.append({
        "Table":      c.get("table", ""),
        "Column":     c.get("name", ""),
        "Type":       c.get("dataType", "unknown"),
        "Hidden":     bool(c.get("isHidden", False)),
        "Expression": (c.get("expression") or "").strip(),
    })
df_calc = pd.DataFrame(calc_col_rows) if calc_col_rows else pd.DataFrame()

# ─────────────────────────────────────────────────────────────────────────────
# Advanced table helper — data_editor with search + copy-paste
# ─────────────────────────────────────────────────────────────────────────────
def _adv_table(data: pd.DataFrame, key: str, col_configs: dict | None = None, height: int = 400):
    """Render a searchable, copy-paste enabled, sortable data editor."""
    if data.empty:
        return
    search = st.text_input(
        "🔍 Search / filter rows",
        key=f"search_{key}",
        placeholder="Type to filter any column…",
    )
    filtered = data.copy()
    if search.strip():
        mask = filtered.apply(
            lambda col: col.astype(str).str.contains(search.strip(), case=False, na=False)
        ).any(axis=1)
        filtered = filtered[mask]
        st.caption(f"{len(filtered)} of {len(data)} rows match")

    # Build column config
    cfg: dict = {}
    if col_configs:
        cfg.update(col_configs)
    # Auto-configure bool columns as checkboxes
    for col in filtered.columns:
        if col not in cfg and filtered[col].dtype == bool:
            cfg[col] = st.column_config.CheckboxColumn(col, disabled=True)
    # Expression column gets text area so full DAX is readable
    if "Expression" in filtered.columns and "Expression" not in cfg:
        cfg["Expression"] = st.column_config.TextColumn(
            "Expression (DAX)", width="large"
        )

    st.data_editor(
        filtered.reset_index(drop=True),
        use_container_width=True,
        hide_index=True,
        disabled=True,
        height=height,
        column_config=cfg,
        key=f"editor_{key}",
    )

# ─────────────────────────────────────────────────────────────────────────────
# Summary KPIs
# ─────────────────────────────────────────────────────────────────────────────
n_safe   = int((df["Status"] == "Safe").sum())   if not df.empty else 0
n_warn   = int((df["Status"] == "Warn").sum())   if not df.empty else 0
n_unsafe = int((df["Status"] == "Unsafe").sum()) if not df.empty else 0
n_calc   = len(df_calc)

st.markdown('<p class="section-hdr">📊 Deletion Candidates Summary</p>', unsafe_allow_html=True)

# Criteria legend
with st.expander("ℹ️ How classification works — all criteria explained"):
    st.markdown("""
| Status | Criteria |
|--------|----------|
| 🔒 **Unsafe** | Referenced by 1+ other measures (DAX caller detected) |
| ⚠️ **Warn** | 0 measure callers BUT: hidden · uses time-intelligence · referenced by calculated column · has broken DAX refs · lives in a display folder |
| ✅ **Safe** | 0 callers + visible + no time-intel + not in any calc column + no broken refs + no display folder |

> **Note:** Power BI visuals reference measures directly — we cannot detect that from the model file alone. Always verify in Desktop before deleting.
    """)

c1, c2, c3, c4 = st.columns(4)
with c1:
    st.markdown(f"""
    <div class="safe-card">
      <div style="font-size:1.8rem;font-weight:800">{n_safe}</div>
      <div style="font-size:.85rem;color:#15803d">✅ Safe to Delete</div>
    </div>""", unsafe_allow_html=True)
with c2:
    st.markdown(f"""
    <div class="warn-card">
      <div style="font-size:1.8rem;font-weight:800">{n_warn}</div>
      <div style="font-size:.85rem;color:#854d0e">⚠️ Review First</div>
    </div>""", unsafe_allow_html=True)
with c3:
    st.markdown(f"""
    <div class="unsafe-card">
      <div style="font-size:1.8rem;font-weight:800">{n_unsafe}</div>
      <div style="font-size:.85rem;color:#b91c1c">🔒 In Use — Keep</div>
    </div>""", unsafe_allow_html=True)
with c4:
    st.metric("Calc Columns", n_calc, help="Calculated columns worth reviewing for measure conversion")

# ─────────────────────────────────────────────────────────────────────────────
# Pie chart
# ─────────────────────────────────────────────────────────────────────────────
if not df.empty:
    counts = df["Status"].value_counts().reset_index()
    counts.columns = ["Status", "Count"]
    color_map = {"Safe": "#22c55e", "Warn": "#f59e0b", "Unsafe": "#ef4444"}
    fig_pie = px.pie(
        counts, names="Status", values="Count",
        color="Status", color_discrete_map=color_map,
        title="Measure Deletion Safety",
        hole=0.45,
    )
    fig_pie.update_layout(paper_bgcolor="#f8fafc", margin=dict(t=40,b=10,l=10,r=10), title_font_size=14)
    col_pie, col_bar = st.columns(2)
    with col_pie:
        st.plotly_chart(fig_pie, use_container_width=True)
    with col_bar:
        df_callers_chart = df[df["Status"] == "Unsafe"].copy()
        if not df_callers_chart.empty:
            fig_bar = px.histogram(
                df_callers_chart, x="Callers", nbins=15,
                title="Number of Callers (In-Use Measures)",
                color_discrete_sequence=["#ef4444"],
                labels={"Callers": "# Callers", "count": "# Measures"},
            )
            fig_bar.update_layout(
                paper_bgcolor="#f8fafc", plot_bgcolor="#f8fafc",
                margin=dict(t=40,b=20,l=20,r=20), title_font_size=14,
            )
            st.plotly_chart(fig_bar, use_container_width=True)
        else:
            st.info("All measures are unused — the whole model might need a clean-up!")

# ─────────────────────────────────────────────────────────────────────────────
# Safe-to-delete — advanced editor
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">✅ Safe to Delete</p>', unsafe_allow_html=True)
df_safe = df[df["Status"] == "Safe"].copy() if not df.empty else pd.DataFrame()

if df_safe.empty:
    st.success("No unused measures found — nothing safe to delete.")
else:
    st.markdown(
        f"**{len(df_safe)} measures** passed all 5 safety checks and are safe to remove. "
        "Click any cell to copy. Select rows, then Ctrl+C to copy the full table."
    )
    _adv_table(
        df_safe[["Table", "Measure", "Display Folder", "Reason", "Expression"]],
        key="safe",
        height=350,
    )

    # Generate Tabular Editor / XMLA removal script
    st.markdown("#### 📜 Removal Script (Tabular Editor C#)")
    script_lines = [
        "// Auto-generated by PBI Intelligence Platform — Measure Killer",
        "// Paste into Tabular Editor 2/3 → Advanced Scripting",
        f"// Generated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    for _, row in df_safe.iterrows():
        table  = row["Table"].replace('"', '\\"')
        mname  = row["Measure"].replace('"', '\\"')
        script_lines.append(f'Model.Tables["{table}"].Measures["{mname}"].Delete();')
    script_lines.append('\nModel.SaveChanges();')
    script = "\n".join(script_lines)

    st.code(script, language="csharp")
    st.download_button(
        "📥 Download Removal Script (.csx)",
        data=script,
        file_name="remove_unused_measures.csx",
        mime="text/plain",
    )

# ─────────────────────────────────────────────────────────────────────────────
# Review-first list — advanced editor
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">⚠️ Review First</p>', unsafe_allow_html=True)
df_warn = df[df["Status"] == "Warn"].copy() if not df.empty else pd.DataFrame()
if df_warn.empty:
    st.info("No measures in the review queue.")
else:
    st.markdown(
        "These measures have **0 measure callers** but failed at least one safety check. "
        "Verify in Power BI Desktop before deleting."
    )
    _adv_table(
        df_warn[["Table", "Measure", "Display Folder", "Hidden", "Time Intel",
                 "Calc Col Refs", "Broken Refs", "Reason", "Expression"]],
        key="warn",
        height=380,
    )

# ─────────────────────────────────────────────────────────────────────────────
# In-use measures — advanced editor
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">🔒 In-Use Measures (keep)</p>', unsafe_allow_html=True)
df_unsafe = df[df["Status"] == "Unsafe"].copy() if not df.empty else pd.DataFrame()
if not df_unsafe.empty:
    _adv_table(
        df_unsafe[["Table", "Measure", "Display Folder", "Callers", "Caller List", "Expression"]],
        key="unsafe",
        height=380,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Calculated columns — advanced editor
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">🗂️ Calculated Columns — Consider Converting to Measures</p>', unsafe_allow_html=True)
if df_calc.empty:
    st.success("No calculated columns found.")
else:
    st.markdown(
        f"**{len(df_calc)} calculated columns** store values per row, increasing file size and "
        "slowing refresh. Evaluate whether each can be replaced with a DAX measure."
    )
    _adv_table(df_calc, key="calc_cols", height=380)

# ─────────────────────────────────────────────────────────────────────────────
# Full dependency view — advanced editor
# ─────────────────────────────────────────────────────────────────────────────
with st.expander("🔗 Full Dependency View — all measures, all criteria"):
    if not df.empty:
        st.markdown("All columns are visible. Use the search box to filter. Click any cell to copy.")
        _adv_table(
            df[["Table", "Measure", "Display Folder", "Status", "Callers",
                "Caller List", "Calc Col Refs", "Time Intel", "Hidden",
                "Broken Refs", "Reason", "Expression"]],
            key="full_dep",
            height=500,
        )

# ─────────────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">⬇️ Export Report</p>', unsafe_allow_html=True)
if not df.empty:
    import openpyxl
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="All Measures", index=False)
        df_safe.to_excel(writer, sheet_name="Safe to Delete", index=False)
        df_warn.to_excel(writer, sheet_name="Review First", index=False)
        if not df_calc.empty:
            df_calc.to_excel(writer, sheet_name="Calculated Columns", index=False)
    st.download_button(
        "📥 Download Measure Killer Report (.xlsx)",
        data=out.getvalue(),
        file_name="measure_killer_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
