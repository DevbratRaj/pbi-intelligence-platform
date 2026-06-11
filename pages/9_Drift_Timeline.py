"""
9_Drift_Timeline.py — Drift & Change Detection Engine
PBI Intelligence Platform

Upload two versions of the same report → see exactly what changed:
new/removed/modified measures, columns, relationships, and tables.
"""

import sys
import io
import json
from pathlib import Path
from datetime import datetime
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

st.set_page_config(
    page_title="Drift & Timeline — PBI Intelligence Platform",
    page_icon="🕐",
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
.diff-added   { background:#f0fdf4; border-left:4px solid #16a34a; padding:10px 14px; border-radius:6px; margin:4px 0; }
.diff-removed { background:#fef2f2; border-left:4px solid #dc2626; padding:10px 14px; border-radius:6px; margin:4px 0; }
.diff-changed { background:#fffbeb; border-left:4px solid #d97706; padding:10px 14px; border-radius:6px; margin:4px 0; }
.diff-same    { background:#f8fafc; border-left:4px solid #94a3b8; padding:10px 14px; border-radius:6px; margin:4px 0; }
.change-badge-add { background:#dcfce7; color:#15803d; padding:2px 8px; border-radius:20px; font-size:.78rem; font-weight:700; }
.change-badge-rem { background:#fee2e2; color:#b91c1c; padding:2px 8px; border-radius:20px; font-size:.78rem; font-weight:700; }
.change-badge-mod { background:#fef9c3; color:#854d0e; padding:2px 8px; border-radius:20px; font-size:.78rem; font-weight:700; }
.section-hdr { font-size:1.1rem; font-weight:700; color:#1e293b; margin:24px 0 12px;
    border-bottom:2px solid #e2e8f0; padding-bottom:6px; }
.score-badge { display:inline-block; padding:6px 16px; border-radius:20px; font-weight:700; font-size:1rem; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("<h1>PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")
    st.markdown("### ℹ️ How it works")
    st.markdown("""
    1. Upload **Version A** (older)  
    2. Upload **Version B** (newer)  
    3. See a full diff — added, removed & modified items  
    
    Works with **.pbix** and **.pbit** files.
    """)
    st.markdown("---")
    show_unchanged = st.checkbox("Show unchanged items", value=False)

# ─────────────────────────────────────────────────────────────────────────────
# Project module
# ─────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from pbit_extractor import extract_pbit_metadata

# ─────────────────────────────────────────────────────────────────────────────
# Page header
# ─────────────────────────────────────────────────────────────────────────────
st.title("🕐 Drift & Change Detection Engine")
st.markdown(
    "Upload two versions of the same report and get a **git-style diff** — "
    "every measure, column, relationship, and table that was added, removed, or modified."
)

# ─────────────────────────────────────────────────────────────────────────────
# File upload (two files side-by-side)
# ─────────────────────────────────────────────────────────────────────────────
col_a, col_b = st.columns(2)
with col_a:
    st.markdown("#### 📁 Version A (older / baseline)")
    file_a = st.file_uploader("Upload baseline .pbix / .pbit", type=["pbix", "pbit"], key="drift_a")
with col_b:
    st.markdown("#### 📁 Version B (newer / current)")
    file_b = st.file_uploader("Upload current .pbix / .pbit", type=["pbix", "pbit"], key="drift_b")

if not file_a or not file_b:
    st.info("Upload both versions to generate the change report.")
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# Parse both files
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner="Parsing files…")
def _parse(data: bytes):
    return extract_pbit_metadata(io.BytesIO(data))

with st.spinner("Parsing Version A…"):
    meta_a = _parse(file_a.read())
with st.spinner("Parsing Version B…"):
    meta_b = _parse(file_b.read())

# ─────────────────────────────────────────────────────────────────────────────
# Diff helpers
# ─────────────────────────────────────────────────────────────────────────────
def _key(item: dict, kind: str) -> str:
    if kind == "measure":
        return f"{item.get('table','')}::{item.get('name','')}"
    if kind == "column":
        return f"{item.get('table','')}::{item.get('name','')}"
    if kind == "table":
        return item.get("name", "")
    if kind == "relationship":
        return (f"{item.get('fromTable','')}[{item.get('fromColumn','')}]"
                f"→{item.get('toTable','')}[{item.get('toColumn','')}]")
    return str(item)

def _diff(items_a: list, items_b: list, kind: str, compare_fields: list[str]) -> list[dict]:
    """Return list of change rows: status in {added, removed, changed, unchanged}."""
    map_a = {_key(i, kind): i for i in items_a}
    map_b = {_key(i, kind): i for i in items_b}
    all_keys = sorted(set(map_a) | set(map_b))
    rows = []
    for k in all_keys:
        if k in map_a and k not in map_b:
            rows.append({"Key": k, "Status": "removed", **{f: map_a[k].get(f, "") for f in compare_fields}})
        elif k not in map_a and k in map_b:
            rows.append({"Key": k, "Status": "added",   **{f: map_b[k].get(f, "") for f in compare_fields}})
        else:
            a_vals = {f: map_a[k].get(f, "") for f in compare_fields}
            b_vals = {f: map_b[k].get(f, "") for f in compare_fields}
            if a_vals != b_vals:
                changed_fields = [f for f in compare_fields if a_vals[f] != b_vals[f]]
                row = {"Key": k, "Status": "changed", "Changed Fields": ", ".join(changed_fields)}
                for f in compare_fields:
                    row[f"A: {f}"] = a_vals[f]
                    row[f"B: {f}"] = b_vals[f]
                rows.append(row)
            elif show_unchanged:
                rows.append({"Key": k, "Status": "unchanged", **a_vals})
    return rows

# ─────────────────────────────────────────────────────────────────────────────
# Run diffs
# ─────────────────────────────────────────────────────────────────────────────
measure_diff  = _diff(meta_a["measures"],     meta_b["measures"],     "measure",      ["expression", "description", "formatString"])
column_diff   = _diff(meta_a["columns"],      meta_b["columns"],      "column",       ["dataType", "isHidden", "expression"])
table_diff    = _diff(meta_a["tables"],       meta_b["tables"],       "table",        ["name"])
rel_diff      = _diff(meta_a["relationships"],meta_b["relationships"],"relationship", ["crossFilteringBehavior"])

# ─────────────────────────────────────────────────────────────────────────────
# Summary headline
# ─────────────────────────────────────────────────────────────────────────────
def _count(diff_list, status):
    return sum(1 for r in diff_list if r["Status"] == status)

total_changes = sum(
    _count(d, s)
    for d in [measure_diff, column_diff, table_diff, rel_diff]
    for s in ("added", "removed", "changed")
)

st.markdown("---")
st.markdown('<p class="section-hdr">📋 Change Summary</p>', unsafe_allow_html=True)

c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric("Total Changes", total_changes)
with c2:
    adds = sum(_count(d,"added")   for d in [measure_diff, column_diff, table_diff, rel_diff])
    st.metric("➕ Added",   adds)
with c3:
    rems = sum(_count(d,"removed") for d in [measure_diff, column_diff, table_diff, rel_diff])
    st.metric("➖ Removed", rems)
with c4:
    mods = sum(_count(d,"changed") for d in [measure_diff, column_diff, table_diff, rel_diff])
    st.metric("✏️ Modified", mods)

# Sankey-style category breakdown
cat_labels = ["Measures", "Columns", "Tables", "Relationships"]
cat_adds   = [_count(measure_diff,"added"),   _count(column_diff,"added"),   _count(table_diff,"added"),   _count(rel_diff,"added")]
cat_rems   = [_count(measure_diff,"removed"), _count(column_diff,"removed"), _count(table_diff,"removed"), _count(rel_diff,"removed")]
cat_mods   = [_count(measure_diff,"changed"), _count(column_diff,"changed"), _count(table_diff,"changed"), _count(rel_diff,"changed")]

fig_breakdown = go.Figure()
fig_breakdown.add_trace(go.Bar(name="Added",    x=cat_labels, y=cat_adds, marker_color="#22c55e"))
fig_breakdown.add_trace(go.Bar(name="Removed",  x=cat_labels, y=cat_rems, marker_color="#ef4444"))
fig_breakdown.add_trace(go.Bar(name="Modified", x=cat_labels, y=cat_mods, marker_color="#f59e0b"))
fig_breakdown.update_layout(
    barmode="group",
    title="Changes by Category",
    plot_bgcolor="#f8fafc", paper_bgcolor="#f8fafc",
    title_font_size=14,
    margin=dict(t=40, b=20, l=20, r=20),
    legend=dict(orientation="h", yanchor="bottom", y=1.02),
)
st.plotly_chart(fig_breakdown, use_container_width=True)

# ─────────────────────────────────────────────────────────────────────────────
# Detailed diff tables
# ─────────────────────────────────────────────────────────────────────────────
STATUS_ICONS = {"added": "➕", "removed": "➖", "changed": "✏️", "unchanged": "—"}
STATUS_COLORS = {
    "added":     ("change-badge-add", "Added"),
    "removed":   ("change-badge-rem", "Removed"),
    "changed":   ("change-badge-mod", "Modified"),
    "unchanged": ("diff-same", "Unchanged"),
}

def _render_diff_section(title: str, diff_rows: list[dict], icon: str):
    n_add = _count(diff_rows, "added")
    n_rem = _count(diff_rows, "removed")
    n_mod = _count(diff_rows, "changed")
    n_total = n_add + n_rem + n_mod
    label = f"{icon} {title} — {n_total} change{'s' if n_total != 1 else ''}"
    with st.expander(label, expanded=(n_total > 0)):
        if not diff_rows:
            st.success("No changes.")
            return
        df = pd.DataFrame(diff_rows)
        # Colour-code status column
        def _badge(s):
            cls, txt = STATUS_COLORS.get(s, ("diff-same", s))
            return f'<span class="{cls}">{txt}</span>'
        df["Status"] = df["Status"].apply(_badge)
        st.write(df.to_html(escape=False, index=False), unsafe_allow_html=True)

st.markdown('<p class="section-hdr">🔍 Detailed Changes</p>', unsafe_allow_html=True)

_render_diff_section("Measures",      measure_diff, "📐")
_render_diff_section("Columns",       column_diff,  "📊")
_render_diff_section("Tables",        table_diff,   "🗂️")
_render_diff_section("Relationships", rel_diff,     "🔗")

# ─────────────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">⬇️ Export Change Report</p>', unsafe_allow_html=True)

import openpyxl
out = io.BytesIO()
with pd.ExcelWriter(out, engine="openpyxl") as writer:
    summary_data = {
        "Category":   ["Measures", "Columns", "Tables", "Relationships"],
        "Added":      cat_adds,
        "Removed":    cat_rems,
        "Modified":   cat_mods,
    }
    pd.DataFrame(summary_data).to_excel(writer, sheet_name="Summary", index=False)
    for sheet_name, diff_rows in [
        ("Measures",      measure_diff),
        ("Columns",       column_diff),
        ("Tables",        table_diff),
        ("Relationships", rel_diff),
    ]:
        if diff_rows:
            pd.DataFrame(diff_rows).to_excel(writer, sheet_name=sheet_name, index=False)

st.download_button(
    "📥 Download Change Report (.xlsx)",
    data=out.getvalue(),
    file_name=f"drift_report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
