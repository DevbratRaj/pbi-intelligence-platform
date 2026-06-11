"""
10_Master_Dashboard.py — Master Dashboard
PBI Intelligence Platform

Plug any report in → 60-second full scorecard across all engines.
Aggregates Performance, Data Quality, Governance, Business Impact,
and Documentation scores into one executive view.
"""

import sys
import io
import re
import math
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

st.set_page_config(
    page_title="Master Dashboard — PBI Intelligence Platform",
    page_icon="🏆",
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
.master-header {
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
    border-radius:14px; padding:28px 32px; margin-bottom:24px; color:#fff;
}
.master-header h2 { margin:0; font-size:1.8rem; font-weight:800; }
.master-header p  { margin:6px 0 0; opacity:.75; font-size:1rem; }
.score-pill {
    display:inline-block; border-radius:50%; width:90px; height:90px;
    line-height:90px; text-align:center; font-size:1.6rem; font-weight:800;
    margin:0 auto; box-shadow:0 4px 14px rgba(0,0,0,.15);
}
.score-a { background:#dcfce7; color:#15803d; }
.score-b { background:#d1fae5; color:#065f46; }
.score-c { background:#fef9c3; color:#854d0e; }
.score-d { background:#fee2e2; color:#b91c1c; }
.score-f { background:#fca5a5; color:#7f1d1d; }
.engine-card {
    background:#fff; border-radius:12px; padding:20px 22px;
    box-shadow:0 2px 8px rgba(0,0,0,.07); margin-bottom:14px;
    border-top:4px solid #3b82f6;
}
.engine-card.green  { border-top-color:#16a34a; }
.engine-card.amber  { border-top-color:#d97706; }
.engine-card.red    { border-top-color:#dc2626; }
.engine-card.blue   { border-top-color:#2563eb; }
.engine-card.purple { border-top-color:#7c3aed; }
.finding-row { padding:8px 0; border-bottom:1px solid #f1f5f9; font-size:.9rem; }
.finding-row:last-child { border-bottom:none; }
.badge-red    { background:#fee2e2; color:#b91c1c; padding:2px 8px; border-radius:20px; font-size:.75rem; font-weight:700; }
.badge-amber  { background:#fef9c3; color:#854d0e; padding:2px 8px; border-radius:20px; font-size:.75rem; font-weight:700; }
.badge-green  { background:#dcfce7; color:#15803d; padding:2px 8px; border-radius:20px; font-size:.75rem; font-weight:700; }
.section-hdr  { font-size:1.1rem; font-weight:700; color:#1e293b; margin:24px 0 12px;
    border-bottom:2px solid #e2e8f0; padding-bottom:6px; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("<h1>PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")
    st.markdown("### ⚙️ Score Weights")
    st.caption("Adjust how much each dimension counts toward the overall score.")
    w_perf  = st.slider("Performance",    0, 100, 25, 5)
    w_qual  = st.slider("Data Quality",   0, 100, 20, 5)
    w_gov   = st.slider("Governance",     0, 100, 20, 5)
    w_doc   = st.slider("Documentation",  0, 100, 15, 5)
    w_biz   = st.slider("Business Impact",0, 100, 20, 5)
    total_w = w_perf + w_qual + w_gov + w_doc + w_biz
    if total_w == 0:
        total_w = 1
    st.caption(f"Total weight: {total_w} (auto-normalised)")

# ─────────────────────────────────────────────────────────────────────────────
# Project module
# ─────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from pbit_extractor import extract_pbit_metadata
try:
    from ai_helper import ai_call, ai_available
    _AI_ON = ai_available()
except Exception:
    _AI_ON = False
    def ai_call(*a, **kw): return ""
    def ai_available(): return False

# ─────────────────────────────────────────────────────────────────────────────
# Page header
# ─────────────────────────────────────────────────────────────────────────────
st.title("🏆 Master Dashboard")
st.markdown(
    "Upload any report and get a **60-second full health scorecard** — "
    "performance, data quality, governance, documentation, and business impact in one view."
)

# ─────────────────────────────────────────────────────────────────────────────
# File input
# ─────────────────────────────────────────────────────────────────────────────
uploaded = st.file_uploader(
    "Upload a .pbix or .pbit file",
    type=["pbix", "pbit"],
    key="master_upload",
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
    st.warning("Upload a .pbix or .pbit file to generate the scorecard.")
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# Parse
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner="Running all engines…")
def _parse(data: bytes):
    return extract_pbit_metadata(io.BytesIO(data))

meta     = _parse(raw_bytes)
measures = meta.get("measures", [])
columns  = meta.get("columns", [])
tables   = meta.get("tables", [])
rels     = meta.get("relationships", [])
roles    = meta.get("roles", [])

fname    = st.session_state.get("pbi_file_name", "Report")

# ─────────────────────────────────────────────────────────────────────────────
# ── ENGINE 1: PERFORMANCE SCORE ──────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
ANTI_PATTERNS = [
    "FILTER(ALL", "CALCULATE(FILTER", "SUMX(FILTER", "CROSSJOIN(",
    "GENERATE(", "EARLIER(", "RANKX(ALL",
]
ITERATOR_FNS  = ["SUMX","COUNTX","AVERAGEX","MINX","MAXX","RANKX"]

perf_issues = []
for m in measures:
    expr = (m.get("expression") or "").upper()
    hits = [p for p in ANTI_PATTERNS if p.upper() in expr]
    iters = sum(1 for fn in ITERATOR_FNS if fn in expr)
    if hits:
        perf_issues.append({"Measure": m.get("name",""), "Issue": ", ".join(hits)})
    elif iters >= 2:
        perf_issues.append({"Measure": m.get("name",""), "Issue": f"{iters} nested iterators"})

total_m  = max(len(measures), 1)
perf_pct = max(0, 100 - round(len(perf_issues) / total_m * 100))
perf_score = perf_pct

# ─────────────────────────────────────────────────────────────────────────────
# ── ENGINE 2: DATA QUALITY SCORE ─────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
dq_issues = []

# Duplicate measure names
measure_names = [m.get("name","") for m in measures]
seen_names = set(); dupe_names = set()
for n in measure_names:
    if n in seen_names: dupe_names.add(n)
    seen_names.add(n)
if dupe_names:
    dq_issues.append(f"{len(dupe_names)} duplicate measure name(s)")

# Blank measure expressions
blank_expr = [m for m in measures if not (m.get("expression") or "").strip()]
if blank_expr:
    dq_issues.append(f"{len(blank_expr)} measure(s) with blank expression")

# Tables with no columns
empty_tables = [t for t in tables if not any(c.get("table","") == t.get("name","") for c in columns)]
if empty_tables:
    dq_issues.append(f"{len(empty_tables)} table(s) with no columns")

# Orphan relationships (ref non-existent tables)
table_names = {t.get("name","") for t in tables}
orphan_rels = [r for r in rels if r.get("fromTable","") not in table_names or r.get("toTable","") not in table_names]
if orphan_rels:
    dq_issues.append(f"{len(orphan_rels)} orphaned relationship(s)")

# Calculated columns (model bloat)
calc_cols = [c for c in columns if c.get("expression")]

dq_penalty = min(50, len(dq_issues) * 10 + len(calc_cols) * 2)
dq_score   = max(0, 100 - dq_penalty)

# ─────────────────────────────────────────────────────────────────────────────
# ── ENGINE 3: GOVERNANCE SCORE ───────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
gov_issues = []

# Naming convention: measures should be Title Case or contain spaces/verbs
_bad_name_pat = re.compile(r"^[a-z]|_[a-z]|Column\d+|Measure\d+|^\d")
bad_names = [m for m in measures if _bad_name_pat.search(m.get("name",""))]
if bad_names:
    gov_issues.append(f"{len(bad_names)} measure(s) with poor naming")

# No RLS roles defined
if not roles:
    gov_issues.append("No Row-Level Security roles defined")

# Missing descriptions
undoc = [m for m in measures if not (m.get("description") or "").strip()]
if undoc:
    gov_issues.append(f"{len(undoc)}/{len(measures)} measures undocumented")

gov_penalty = min(60, len(gov_issues) * 15 + len(bad_names))
gov_score   = max(0, 100 - gov_penalty)

# ─────────────────────────────────────────────────────────────────────────────
# ── ENGINE 4: DOCUMENTATION SCORE ────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
documented = sum(1 for m in measures if (m.get("description") or "").strip())
doc_score  = round(documented / max(len(measures), 1) * 100) if measures else 100

doc_issues = []
if len(measures) > 0 and doc_score < 100:
    doc_issues.append(f"{len(measures)-documented} of {len(measures)} measures have no description")
    
# Check table descriptions (if available)
tables_with_desc = sum(1 for t in tables if (t.get("description") or "").strip())
if tables and tables_with_desc < len(tables):
    doc_issues.append(f"{len(tables)-tables_with_desc} of {len(tables)} tables have no description")

# ─────────────────────────────────────────────────────────────────────────────
# ── ENGINE 5: BUSINESS IMPACT / COMPLEXITY ───────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
def _complexity(expr: str) -> int:
    if not expr: return 0
    depth    = min(expr.count("("), 20)
    iters    = sum(1 for fn in ITERATOR_FNS if fn in expr.upper())
    anti     = sum(1 for p in ANTI_PATTERNS if p.upper() in expr.upper())
    return min(10, round(depth * 0.12 + iters * 1.5 + anti * 2))

complexities = [_complexity(m.get("expression","") or "") for m in measures]
avg_complexity = sum(complexities) / max(len(complexities), 1)
high_complex  = sum(1 for c in complexities if c >= 7)

biz_issues = []
if high_complex > 0:
    biz_issues.append(f"{high_complex} high-complexity measure(s) (score ≥ 7/10)")
if avg_complexity > 4:
    biz_issues.append(f"Average complexity {avg_complexity:.1f}/10 is above target (4.0)")
if len(calc_cols) > 5:
    biz_issues.append(f"{len(calc_cols)} calculated columns add significant model size")

biz_penalty = min(70, high_complex * 5 + round(avg_complexity * 4) + len(calc_cols))
biz_score   = max(0, 100 - biz_penalty)

# ─────────────────────────────────────────────────────────────────────────────
# Overall score
# ─────────────────────────────────────────────────────────────────────────────
overall = round(
    (perf_score * w_perf + dq_score * w_qual + gov_score * w_gov
     + doc_score * w_doc + biz_score * w_biz) / total_w
)

def _grade(s: int) -> tuple[str, str]:
    if s >= 90: return "A", "score-a"
    if s >= 75: return "B", "score-b"
    if s >= 60: return "C", "score-c"
    if s >= 40: return "D", "score-d"
    return "F", "score-f"

def _colour(s: int) -> str:
    if s >= 75: return "green"
    if s >= 50: return "amber"
    return "red"

overall_grade, overall_cls = _grade(overall)

# ─────────────────────────────────────────────────────────────────────────────
# Hero scorecard header
# ─────────────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="master-header">
  <div style="display:flex;align-items:center;gap:32px">
    <div>
      <div class="score-pill {overall_cls}">{overall}</div>
    </div>
    <div>
      <h2>{fname}</h2>
      <p>Overall Health Score: <strong>{overall}/100</strong> &nbsp;·&nbsp; Grade: <strong>{overall_grade}</strong>
         &nbsp;·&nbsp; {len(measures)} measures &nbsp;·&nbsp; {len(columns)} columns
         &nbsp;·&nbsp; {len(tables)} tables &nbsp;·&nbsp; {len(rels)} relationships</p>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Radar chart
# ─────────────────────────────────────────────────────────────────────────────
radar_categories = ["Performance", "Data Quality", "Governance", "Documentation", "Business Impact"]
radar_scores     = [perf_score, dq_score, gov_score, doc_score, biz_score]

fig_radar = go.Figure()
fig_radar.add_trace(go.Scatterpolar(
    r=radar_scores + [radar_scores[0]],
    theta=radar_categories + [radar_categories[0]],
    fill="toself",
    fillcolor="rgba(59,130,246,0.15)",
    line=dict(color="#3b82f6", width=2),
    name="Score",
    mode="lines+markers",
    marker=dict(size=8, color="#3b82f6"),
))
fig_radar.update_layout(
    polar=dict(
        radialaxis=dict(visible=True, range=[0, 100], tickfont_size=10),
        angularaxis=dict(tickfont_size=12),
    ),
    showlegend=False,
    title="Health Scorecard Radar",
    title_font_size=15,
    paper_bgcolor="#f8fafc",
    margin=dict(t=60, b=20, l=40, r=40),
)

col_radar, col_scores = st.columns([1, 1])
with col_radar:
    st.plotly_chart(fig_radar, use_container_width=True)

with col_scores:
    st.markdown("")
    for name, score, issues in [
        ("⚡ Performance",    perf_score, perf_issues),
        ("🔬 Data Quality",   dq_score,   dq_issues),
        ("🛡️ Governance",     gov_score,   gov_issues),
        ("📖 Documentation",  doc_score,   doc_issues),
        ("💷 Business Impact",biz_score,  biz_issues),
    ]:
        grade, gcls = _grade(score)
        colour = _colour(score)
        badge_cls = f"badge-{'green' if colour=='green' else 'amber' if colour=='amber' else 'red'}"
        st.markdown(f"""
        <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #f1f5f9">
          <div style="min-width:44px;text-align:center">
            <span class="score-pill {gcls}" style="width:44px;height:44px;line-height:44px;font-size:1rem">{score}</span>
          </div>
          <div style="flex:1">
            <div style="font-weight:700;font-size:.95rem">{name}</div>
            <div style="font-size:.8rem;color:#64748b">
              {issues[0] if issues else "✅ No issues detected"}
            </div>
          </div>
          <span class="{badge_cls}">{grade}</span>
        </div>
        """, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Gauge chart
# ─────────────────────────────────────────────────────────────────────────────
fig_gauge = go.Figure(go.Indicator(
    mode="gauge+number+delta",
    value=overall,
    delta={"reference": 75, "valueformat": ".0f"},
    gauge={
        "axis": {"range": [0, 100], "tickwidth": 1},
        "bar":  {"color": "#3b82f6", "thickness": 0.35},
        "steps": [
            {"range": [0,  40], "color": "#fee2e2"},
            {"range": [40, 60], "color": "#fef9c3"},
            {"range": [60, 75], "color": "#d1fae5"},
            {"range": [75,100], "color": "#bbf7d0"},
        ],
        "threshold": {"line": {"color": "#15803d", "width": 3}, "thickness": 0.8, "value": 75},
    },
    title={"text": f"Overall Score — Grade {overall_grade}", "font": {"size": 16}},
))
fig_gauge.update_layout(
    paper_bgcolor="#f8fafc",
    margin=dict(t=40, b=20, l=20, r=20),
    height=260,
)
st.plotly_chart(fig_gauge, use_container_width=True)

# ─────────────────────────────────────────────────────────────────────────────
# AI Executive Summary
# ─────────────────────────────────────────────────────────────────────────────
if _AI_ON:
    st.markdown('<p class="section-hdr">🧠 AI Executive Summary</p>', unsafe_allow_html=True)
    _summ_key = f"ai_exec_summary_{len(raw_bytes)}"
    if _summ_key not in st.session_state:
        if st.button("✨ Generate AI Summary", type="primary", use_container_width=True,
                     help="GPT-4o reads all scores and top issues to write a plain-English executive brief"):
            with st.spinner("GPT-4o writing executive summary…"):
                try:
                    _issue_lines = "\n".join(
                        f"- [{e}] {i if isinstance(i, str) else i.get('Issue', str(i))}"
                        for e, issues, _ in [
                            ("Performance",    perf_issues,   perf_score),
                            ("Data Quality",   dq_issues,     dq_score),
                            ("Governance",     gov_issues,    gov_score),
                            ("Documentation",  doc_issues,    doc_score),
                            ("Business Impact",biz_issues,    biz_score),
                        ]
                        for i in issues[:3]
                    ) or "No significant issues found."
                    _summ = ai_call(
                        "You are a Power BI expert writing a concise executive summary for a stakeholder. "
                        "Be direct, professional, and action-oriented. Use bullet points. Max 200 words.",
                        f"Report: {fname}\n"
                        f"Overall Health Score: {overall_score}/100\n"
                        f"Scores — Performance: {perf_score}, Data Quality: {dq_score}, "
                        f"Governance: {gov_score}, Documentation: {doc_score}, Business Impact: {biz_score}\n"
                        f"Model: {len(measures)} measures, {len(tables)} tables, {len(columns)} columns\n"
                        f"Top issues:\n{_issue_lines}\n\n"
                        "Write a 3-section executive summary: "
                        "1) Overall Health (1 sentence), "
                        "2) Key Risks (top 3 bullets), "
                        "3) Recommended Actions (top 3 priority fixes)"
                    )
                    st.session_state[_summ_key] = _summ
                except Exception as _e:
                    st.session_state[_summ_key] = f"❌ AI error: {_e}"
    if _summ_key in st.session_state:
        st.markdown(
            f"<div style='background:#f0f4ff;border:1px solid #c7d2fe;border-radius:10px;"
            f"padding:18px 20px;line-height:1.8;font-size:0.95rem'>"
            f"{st.session_state[_summ_key].replace(chr(10), '<br>')}</div>",
            unsafe_allow_html=True,
        )

# ─────────────────────────────────────────────────────────────────────────────
# Top issues & quick wins
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">🚨 Top Issues & Quick Wins</p>', unsafe_allow_html=True)

all_issues = []
for engine, issues, engine_score in [
    ("Performance Engine",    perf_issues,    perf_score),
    ("Data Quality Engine",   dq_issues,      dq_score),
    ("Governance Engine",     gov_issues,     gov_score),
    ("Documentation",         doc_issues,     doc_score),
    ("Business Impact",       biz_issues,     biz_score),
]:
    for issue in issues:
        severity = "🔴 High" if engine_score < 50 else "🟡 Medium" if engine_score < 75 else "🟢 Low"
        msg = issue if isinstance(issue, str) else issue.get("Issue", str(issue))
        all_issues.append({
            "Engine":   engine,
            "Severity": severity,
            "Issue":    msg,
        })

if all_issues:
    df_issues = pd.DataFrame(all_issues)
    st.dataframe(df_issues, use_container_width=True, hide_index=True)
else:
    st.success("🎉 No significant issues found — this report is in great shape!")

# ─────────────────────────────────────────────────────────────────────────────
# Model overview
# ─────────────────────────────────────────────────────────────────────────────
with st.expander("📋 Model Overview"):
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1: st.metric("Tables",        len(tables))
    with c2: st.metric("Columns",       len(columns))
    with c3: st.metric("Measures",      len(measures))
    with c4: st.metric("Relationships", len(rels))
    with c5: st.metric("RLS Roles",     len(roles))

    col_left, col_right = st.columns(2)
    with col_left:
        st.markdown("**Tables**")
        st.dataframe(
            pd.DataFrame([{"Table": t.get("name",""), "Columns": sum(1 for c in columns if c.get("table")==t.get("name","")), "Measures": sum(1 for m in measures if m.get("table")==t.get("name",""))} for t in tables]),
            use_container_width=True, hide_index=True,
        )
    with col_right:
        st.markdown("**Relationships**")
        if rels:
            st.dataframe(
                pd.DataFrame([{"From": f"{r.get('fromTable','')}[{r.get('fromColumn','')}]",
                               "To":   f"{r.get('toTable','')}[{r.get('toColumn','')}]",
                               "Filter": r.get("crossFilteringBehavior","single")} for r in rels]),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No relationships defined.")

# ─────────────────────────────────────────────────────────────────────────────
# Score history (session-based)
# ─────────────────────────────────────────────────────────────────────────────
if "score_history" not in st.session_state:
    st.session_state["score_history"] = []

# Append current score if new file
history = st.session_state["score_history"]
last_entry = history[-1] if history else None
if not last_entry or last_entry["File"] != fname or last_entry["Overall"] != overall:
    import datetime
    history.append({
        "File": fname, "Overall": overall, "Performance": perf_score,
        "Data Quality": dq_score, "Governance": gov_score,
        "Documentation": doc_score, "Business Impact": biz_score,
        "Time": datetime.datetime.now().strftime("%H:%M:%S"),
    })
    if len(history) > 20:
        history = history[-20:]
    st.session_state["score_history"] = history

if len(history) > 1:
    with st.expander(f"📈 Score History (this session — {len(history)} scans)"):
        df_hist = pd.DataFrame(history)
        fig_hist = px.line(
            df_hist, x="Time", y=["Overall", "Performance", "Data Quality", "Governance", "Documentation", "Business Impact"],
            title="Score Trend (Session)",
            markers=True,
            color_discrete_sequence=px.colors.qualitative.Safe,
        )
        fig_hist.update_layout(paper_bgcolor="#f8fafc", plot_bgcolor="#f8fafc",
                                margin=dict(t=40,b=20,l=20,r=20), yaxis_range=[0,100])
        st.plotly_chart(fig_hist, use_container_width=True)
        st.dataframe(df_hist, use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="section-hdr">⬇️ Export Master Report</p>', unsafe_allow_html=True)

import openpyxl
out = io.BytesIO()
with pd.ExcelWriter(out, engine="openpyxl") as writer:
    pd.DataFrame({
        "Engine": ["Performance", "Data Quality", "Governance", "Documentation", "Business Impact", "OVERALL"],
        "Score":  [perf_score, dq_score, gov_score, doc_score, biz_score, overall],
        "Grade":  [_grade(s)[0] for s in [perf_score, dq_score, gov_score, doc_score, biz_score, overall]],
    }).to_excel(writer, sheet_name="Scorecard", index=False)

    if all_issues:
        pd.DataFrame(all_issues).to_excel(writer, sheet_name="Issues", index=False)

    pd.DataFrame([{"Table": t.get("name",""), "Columns": sum(1 for c in columns if c.get("table")==t.get("name","")), "Measures": sum(1 for m in measures if m.get("table")==t.get("name",""))} for t in tables]).to_excel(writer, sheet_name="Model Overview", index=False)

st.download_button(
    "📥 Download Master Report (.xlsx)",
    data=out.getvalue(),
    file_name=f"master_report_{fname.replace(' ','_').replace('.pbix','').replace('.pbit','')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
