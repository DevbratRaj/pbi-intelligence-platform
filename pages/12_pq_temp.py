"""
12_Power_Query_Editor.py — Power Query Optimizer & Editor
PBI Intelligence Platform
"""

import sys
import io
import re
from pathlib import Path
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Power Query Optimizer — PBI Intelligence Platform",
    page_icon="⚡",
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
.score-card {
    border-radius:12px; padding:18px 24px; margin-bottom:16px;
    display:flex; align-items:center; gap:20px;
}
.score-circle {
    font-size:2.4rem; font-weight:800; min-width:64px; text-align:center;
    border-radius:50%; width:70px; height:70px;
    display:flex; align-items:center; justify-content:center;
}
.finding-high   { background:#fff1f2; border-left:5px solid #ef4444; border-radius:8px; padding:14px 18px; margin:8px 0; }
.finding-medium { background:#fffbeb; border-left:5px solid #f59e0b; border-radius:8px; padding:14px 18px; margin:8px 0; }
.finding-low    { background:#eff6ff; border-left:5px solid #3b82f6; border-radius:8px; padding:14px 18px; margin:8px 0; }
.finding-ok     { background:#f0fdf4; border-left:5px solid #22c55e; border-radius:8px; padding:14px 18px; margin:8px 0; }
.finding-title  { font-weight:700; font-size:1rem; margin-bottom:4px; }
.finding-detail { font-size:.88rem; color:#374151; white-space:pre-wrap; }
.badge-high   { background:#ef4444; color:#fff; border-radius:20px; padding:2px 10px; font-size:.75rem; margin-right:6px; }
.badge-medium { background:#f59e0b; color:#fff; border-radius:20px; padding:2px 10px; font-size:.75rem; margin-right:6px; }
.badge-low    { background:#3b82f6; color:#fff; border-radius:20px; padding:2px 10px; font-size:.75rem; margin-right:6px; }
.step-affected { background:#fef3c7; border:1px solid #fcd34d; border-radius:4px; padding:1px 8px; font-size:.78rem; margin:2px 3px 2px 0; display:inline-block; font-family:monospace; }
.step-source-badge { background:#3b82f6; color:#fff; border-radius:20px; padding:2px 10px; font-size:.75rem; margin-left:6px; }
</style>
""", unsafe_allow_html=True)

_HERE = Path(__file__).parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from pbit_extractor import extract_pbit_metadata


# =============================================================================
# M Query Parser
# =============================================================================

def _parse_m_steps(expr: str) -> list:
    if not expr or not expr.strip():
        return []
    text = expr.strip()
    if not re.match(r"(?is)^let\b", text):
        return [{"name": "Query", "body": text, "is_source": False, "depends_on": []}]
    in_pos = _find_final_in(text)
    if in_pos is None:
        return [{"name": "Query", "body": text, "is_source": False, "depends_on": []}]
    body_block = text[len("let"):in_pos].strip()
    final_step = text[in_pos + len("in"):].strip()
    raw_assignments = _split_top_level_commas(body_block)
    steps, all_step_names = [], []
    for raw in raw_assignments:
        raw = raw.strip()
        if not raw:
            continue
        m = re.match(r'^(#"[^"]*"|[A-Za-z_][A-Za-z0-9_]*)\s*=\s*', raw)
        if not m:
            steps.append({"name": f"Step{len(steps)+1}", "body": raw,
                          "is_source": False, "depends_on": []})
            continue
        name = m.group(1).strip('"').lstrip("#").strip('"')
        body = raw[m.end():].strip()
        all_step_names.append(name)
        steps.append({"name": name, "body": body, "is_source": False, "depends_on": []})
    _SOURCE_RE = re.compile(
        r"(?i)^\s*("
        r"Sql\.Database|Sql\.Databases|Oracle\.Database|MySQL\.Database|"
        r"PostgreSQL\.Database|Odbc\.DataSource|OleDb\.DataSource|"
        r"Excel\.Workbook|Csv\.Document|Json\.Document|Xml\.Document|"
        r"SharePoint\.|AzureStorage\.|Snowflake\.Databases|Databricks\.|"
        r"AzureSynapse\.|OData\.Feed|Web\.Contents|File\.Contents|"
        r"Folder\.Files|GoogleAnalytics\.|SalesforceV2?\.|DynamicsCrm\."
        r")"
    )
    for step in steps:
        if _SOURCE_RE.match(step["body"]):
            step["is_source"] = True
            break
    if steps and not any(s["is_source"] for s in steps):
        steps[0]["is_source"] = True
    name_set = set(all_step_names)
    for i, step in enumerate(steps):
        deps = [n for n in all_step_names[:i]
                if re.search(r"\b" + re.escape(n) + r"\b", step["body"])]
        step["depends_on"] = deps
    if final_step and final_step not in (s["name"] for s in steps):
        steps.append({"name": "▶ Result", "body": final_step, "is_source": False,
                      "depends_on": [final_step] if final_step in name_set else []})
    return steps


def _find_final_in(text: str):
    depth, i, in_string, string_char = 0, 0, False, None
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == string_char:
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string, string_char = True, '"'
            i += 1
            continue
        if ch in ("(", "[", "{"):
            depth += 1
        elif ch in (")", "]", "}"):
            depth -= 1
        if depth == 0 and text[i:i+2].lower() == "in" and i + 2 <= len(text):
            after  = text[i+2] if i + 2 < len(text) else " "
            before = text[i-1] if i > 0 else " "
            if not (before.isalnum() or before in ("_", '"')) and \
               not (after.isalnum()  or after  in ("_", '"')):
                return i
        i += 1
    return None


def _split_top_level_commas(text: str) -> list:
    parts, depth, in_string, buf = [], 0, False, []
    for ch in text:
        if in_string:
            buf.append(ch)
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            buf.append(ch)
            continue
        if ch in ("(", "[", "{"):
            depth += 1; buf.append(ch)
        elif ch in (")", "]", "}"):
            depth -= 1; buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _reconstruct_m(steps: list) -> str:
    result_step = next((s for s in steps if s["name"].startswith("▶")), None)
    body_steps  = [s for s in steps if not s["name"].startswith("▶")]
    if not body_steps:
        return ""
    lines = ["let"]
    for i, s in enumerate(body_steps):
        name = s["name"]
        if re.search(r"[^A-Za-z0-9_]", name):
            name = f'#"{name}"'
        comma = "," if i < len(body_steps) - 1 else ""
        body_lines = s["body"].strip().split("\n")
        if len(body_lines) == 1:
            lines.append(f"    {name} = {body_lines[0]}{comma}")
        else:
            lines.append(f"    {name} = {body_lines[0]}")
            for bl in body_lines[1:-1]:
                lines.append(f"        {bl}")
            lines.append(f"        {body_lines[-1]}{comma}")
    result_expr = result_step["body"] if result_step else body_steps[-1]["name"]
    if re.search(r"[^A-Za-z0-9_]", result_expr) and not result_expr.startswith('#"'):
        result_expr = f'#"{result_expr}"'
    lines += ["in", f"    {result_expr}"]
    return "\n".join(lines)


# =============================================================================
# Analysis Engine — 10 rules
# =============================================================================

def _body_steps(steps):
    return [s for s in steps if not s["name"].startswith("▶")]

def _result_name(steps):
    r = next((s for s in steps if s["name"].startswith("▶")), None)
    return r["body"].strip() if r else (_body_steps(steps)[-1]["name"] if _body_steps(steps) else "")

def _is_ttc(body: str) -> bool:
    return bool(re.match(r"\s*Table\.TransformColumnTypes\s*\(", body))

def _is_remove_cols(body: str) -> bool:
    return bool(re.match(r"\s*Table\.RemoveColumns\s*\(", body))

def _ttc_prev_ref(body: str) -> str:
    m = re.match(r"\s*Table\.TransformColumnTypes\s*\(\s*([A-Za-z_#][^,]*?)\s*,", body)
    return m.group(1).strip().strip('"').lstrip("#").strip('"') if m else ""

def _extract_ttc_columns(body: str):
    m = re.search(r"Table\.TransformColumnTypes\s*\([^,]+,\s*(\{.*)", body, re.DOTALL)
    if not m:
        return None
    list_str = m.group(1)
    depth, buf = 0, []
    for ch in list_str:
        buf.append(ch)
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
    inner = "".join(buf)
    pairs = re.findall(r'\{\s*"([^"]+)"\s*,\s*([^{}]+?)\s*\}', inner)
    return [(col.strip(), typ.strip()) for col, typ in pairs] if pairs else None


def _analyze_steps(steps: list) -> list:
    findings = []
    bs        = _body_steps(steps)
    fin_ref   = _result_name(steps)
    all_names = [s["name"] for s in bs]

    # Build reference map
    referenced = set()
    for s in bs:
        for n in all_names:
            if n != s["name"] and re.search(r"\b" + re.escape(n) + r"\b", s["body"]):
                referenced.add(n)
    for n in all_names:
        if re.search(r"\b" + re.escape(n) + r"\b", fin_ref):
            referenced.add(n)

    # --- Rule 1: Dead steps ---------------------------------------------------
    dead = [s["name"] for s in bs
            if s["name"] not in referenced and not s["is_source"]]
    if dead:
        findings.append({
            "id": "dead_steps",
            "severity": "HIGH",
            "title": f"Dead Steps ({len(dead)}) — wasted work at every refresh",
            "detail": (
                "These steps run on EVERY refresh but their output is NEVER used by any "
                "later step or the final result. They are completely safe to delete.\n\n"
                "Affected: " + ", ".join(dead)
            ),
            "affected_steps": dead,
            "can_auto_fix": True,
            "fix_label": f"✂️ Remove {len(dead)} dead step(s)",
        })

    # --- Rule 2: Sort before Filter ------------------------------------------
    for i, s in enumerate(bs[:-1]):
        if re.match(r"\s*Table\.Sort\s*\(", s["body"]):
            for j in range(i + 1, len(bs)):
                if re.match(r"\s*Table\.SelectRows\s*\(", bs[j]["body"]):
                    findings.append({
                        "id": f"sort_before_filter_{i}",
                        "severity": "HIGH",
                        "title": "Sort Before Filter — sorts all rows, then throws most away",
                        "detail": (
                            f"Step \"{s['name']}\" sorts the full table FIRST, "
                            f"then step \"{bs[j]['name']}\" filters rows away.\n\n"
                            "Fix: Move the filter BEFORE the sort.\n"
                            "Example: sorting 1M rows then filtering to 10K is "
                            "100x slower than filtering to 10K first, then sorting those 10K."
                        ),
                        "affected_steps": [s["name"], bs[j]["name"]],
                        "can_auto_fix": False,
                    })
                    break

    # --- Rule 3: Multiple consecutive TransformColumnTypes -------------------
    ttc_chains = []
    i = 0
    while i < len(bs):
        if _is_ttc(bs[i]["body"]):
            chain = [bs[i]]
            j = i + 1
            while j < len(bs) and _is_ttc(bs[j]["body"]) and \
                  re.search(r"\b" + re.escape(chain[-1]["name"]) + r"\b", bs[j]["body"]):
                chain.append(bs[j])
                j += 1
            if len(chain) > 1:
                ttc_chains.append(chain)
            i = j
        else:
            i += 1
    for chain in ttc_chains:
        names = [s["name"] for s in chain]
        findings.append({
            "id": f"dup_ttc_{'_'.join(names[:2])}",
            "severity": "MEDIUM",
            "title": f"Multiple Type-Change Steps ({len(chain)}) — merge into one",
            "detail": (
                f"Power BI scans the ENTIRE table once per TransformColumnTypes call.\n"
                f"{len(chain)} consecutive calls = {len(chain)}x full-table scans for no reason.\n\n"
                "Fix: Combine all column-type pairs into a single TransformColumnTypes step.\n"
                "Affected: " + " → ".join(names)
            ),
            "affected_steps": names,
            "can_auto_fix": True,
            "fix_label": f"⚡ Merge {len(chain)} type-change steps into 1",
            "_chain": chain,
        })

    # --- Rule 4: Multiple RemoveColumns -------------------------------------
    rc_steps = [s for s in bs if _is_remove_cols(s["body"])]
    if len(rc_steps) > 1:
        findings.append({
            "id": "dup_remove_cols",
            "severity": "MEDIUM",
            "title": f"Multiple Remove-Columns Steps ({len(rc_steps)}) — merge into one",
            "detail": (
                "Each Table.RemoveColumns call scans the column list separately.\n"
                "Combine all removed columns into a single call.\n\n"
                "Affected: " + ", ".join(s["name"] for s in rc_steps) + "\n\n"
                "Manual fix: Merge all column lists into one RemoveColumns step."
            ),
            "affected_steps": [s["name"] for s in rc_steps],
            "can_auto_fix": False,
        })

    # --- Rule 5: Type change before Promoted Headers -------------------------
    promote_idxs = [i for i, s in enumerate(bs)
                    if re.match(r"\s*Table\.PromoteHeaders\s*\(", s["body"])]
    for pi in promote_idxs:
        ttc_before = [bs[k]["name"] for k in range(pi) if _is_ttc(bs[k]["body"])]
        if ttc_before:
            findings.append({
                "id": f"ttc_before_promote_{pi}",
                "severity": "HIGH",
                "title": "Type Change Before Promoted Headers — targeting wrong column names",
                "detail": (
                    f"Steps {ttc_before} set column types BEFORE \"{bs[pi]['name']}\" promotes headers.\n\n"
                    "After PromoteHeaders, column names change from Column1/Column2 to your "
                    "actual header values. Any type-change before this targets the WRONG names "
                    "and silently fails or throws an error.\n\n"
                    "Fix: Move ALL TransformColumnTypes steps to AFTER the PromoteHeaders step."
                ),
                "affected_steps": ttc_before + [bs[pi]["name"]],
                "can_auto_fix": False,
            })

    # --- Rule 6: Table.Buffer misuse -----------------------------------------
    buf_steps = [s for s in bs if re.search(r"Table\.Buffer\s*\(", s["body"])]
    if buf_steps:
        findings.append({
            "id": "table_buffer",
            "severity": "MEDIUM",
            "title": f"Table.Buffer Used ({len(buf_steps)} step(s)) — blocks query folding",
            "detail": (
                "Table.Buffer forces the ENTIRE table into RAM and prevents query folding "
                "back to the source database.\n\n"
                "Only keep it inside recursive/loop operations to prevent repeated evaluation. "
                "In a straight transformation chain it just wastes memory.\n\n"
                "Affected: " + ", ".join(s["name"] for s in buf_steps) + "\n\n"
                "Fix: Remove Table.Buffer() and pass the inner expression directly."
            ),
            "affected_steps": [s["name"] for s in buf_steps],
            "can_auto_fix": False,
        })

    # --- Rule 7: Expand without column list ----------------------------------
    expand_all = [
        s for s in bs
        if re.search(r"Table\.ExpandTableColumn\s*\([^,]+,\s*\"[^\"]+\"\s*\)", s["body"])
        or re.search(r"Table\.ExpandRecordColumn\s*\([^,]+,\s*\"[^\"]+\"\s*\)", s["body"])
    ]
    if expand_all:
        findings.append({
            "id": "expand_all_cols",
            "severity": "MEDIUM",
            "title": f"Expand Without Column List ({len(expand_all)}) — loads ALL nested fields",
            "detail": (
                "Expanding without specifying which fields to keep loads EVERY nested field "
                "across all rows. This prevents query folding and transfers far more data than needed.\n\n"
                "Fix: Add an explicit column list as the 3rd argument:\n"
                "  Table.ExpandTableColumn(prev, \"Tbl\", {\"Col1\", \"Col2\"}, {\"Col1\", \"Col2\"})\n\n"
                "Affected: " + ", ".join(s["name"] for s in expand_all)
            ),
            "affected_steps": [s["name"] for s in expand_all],
            "can_auto_fix": False,
        })

    # --- Rule 8: Duplicate Table.Distinct -----------------------------------
    distinct_steps = [s for s in bs if re.match(r"\s*Table\.Distinct\s*\(", s["body"])]
    if len(distinct_steps) > 1:
        findings.append({
            "id": "dup_distinct",
            "severity": "LOW",
            "title": f"Table.Distinct Called {len(distinct_steps)} Times — only one is needed",
            "detail": (
                "Running Table.Distinct multiple times is redundant — once de-duplicated "
                "a second call does nothing extra.\n\n"
                "Affected: " + ", ".join(s["name"] for s in distinct_steps) + "\n\n"
                "Fix: Keep only the last Distinct call."
            ),
            "affected_steps": [s["name"] for s in distinct_steps],
            "can_auto_fix": False,
        })

    # --- Rule 9: High step count --------------------------------------------
    if len(bs) > 18:
        findings.append({
            "id": "high_step_count",
            "severity": "LOW",
            "title": f"High Step Count ({len(bs)} steps) — consider a staging query",
            "detail": (
                f"This query has {len(bs)} transformation steps. Very long queries:\n"
                "  - Are harder to debug and maintain\n"
                "  - Can hit Power Query internal evaluation limits\n"
                "  - May slow query folding analysis\n\n"
                "Fix: Split into staging tables (Enable Load = False)."
            ),
            "affected_steps": [],
            "can_auto_fix": False,
        })

    # --- Rule 10: Large TransformColumnTypes --------------------------------
    for s in bs:
        if _is_ttc(s["body"]):
            pairs = _extract_ttc_columns(s["body"]) or []
            if len(pairs) > 20:
                findings.append({
                    "id": f"large_ttc_{s['name']}",
                    "severity": "LOW",
                    "title": f"'{s['name']}' types {len(pairs)} columns — type only what you use",
                    "detail": (
                        f"This step changes types for {len(pairs)} columns. "
                        "If you later remove many of them, you waste time typing columns "
                        "that are then discarded.\n\n"
                        "Fix: Move RemoveColumns BEFORE TransformColumnTypes, "
                        "then only type the columns you actually keep."
                    ),
                    "affected_steps": [s["name"]],
                    "can_auto_fix": False,
                })

    return findings


# =============================================================================
# Auto-fix functions
# =============================================================================

def _fix_remove_dead(steps: list, dead_names: list) -> list:
    return [s for s in steps if s["name"] not in dead_names]


def _fix_merge_ttc_chain(steps: list, chain_names: list) -> list:
    bs          = _body_steps(steps)
    result_step = next((s for s in steps if s["name"].startswith("▶")), None)
    merged, order, first_ttc_ref, last_name = {}, [], "", chain_names[-1]
    for name in chain_names:
        s = next((x for x in bs if x["name"] == name), None)
        if not s:
            continue
        if not first_ttc_ref:
            first_ttc_ref = _ttc_prev_ref(s["body"])
        pairs = _extract_ttc_columns(s["body"]) or []
        for col, typ in pairs:
            if col not in merged:
                order.append(col)
            merged[col] = typ
    pairs_str   = ", ".join(f'{{"{col}", {merged[col]}}}' for col in order)
    merged_body = f"Table.TransformColumnTypes({first_ttc_ref}, {{{pairs_str}}})"
    skip_names  = set(chain_names[:-1])
    new_steps   = []
    for s in bs:
        if s["name"] in skip_names:
            continue
        if s["name"] == last_name:
            new_steps.append({**s, "body": merged_body})
        else:
            new_steps.append(s)
    if result_step:
        new_steps.append(result_step)
    return new_steps


def _apply_all_fixes(steps: list, fix_ids: set, findings: list) -> list:
    current = list(steps)
    for f in findings:
        if f["id"] not in fix_ids or not f.get("can_auto_fix"):
            continue
        if f["id"] == "dead_steps":
            current = _fix_remove_dead(current, f["affected_steps"])
        elif f["id"].startswith("dup_ttc_"):
            chain_names = [s["name"] for s in f.get("_chain", [])]
            if chain_names:
                current = _fix_merge_ttc_chain(current, chain_names)
    return current


def _performance_score(findings: list) -> int:
    score = 100
    for f in findings:
        if f["severity"] == "HIGH":   score -= 25
        elif f["severity"] == "MEDIUM": score -= 12
        elif f["severity"] == "LOW":    score -= 5
    return max(0, score)


# =============================================================================
# Sidebar
# =============================================================================
with st.sidebar:
    st.markdown("<h1>⚡ PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")
    st.markdown("### Power Query Optimizer")
    st.markdown("""
**Workflow:**
1. Upload `.pbix` / `.pbit`
2. Select a table
3. Review issues in **Optimizer** tab
4. Click **Auto-Fix** on fixable issues
5. Copy the clean M query
6. Paste into Power BI Desktop → Advanced Editor
    """)
    st.markdown("---")
    show_raw  = st.checkbox("Show original raw M (read-only)", value=False)
    show_deps = st.checkbox("Show step dependencies in editor", value=True)

# =============================================================================
# Page header
# =============================================================================
st.title("⚡ Power Query Optimizer")
st.markdown(
    "Finds **real** performance problems in M queries — dead steps, wrong step order, "
    "redundant type-change scans, Table.Buffer misuse, and more. "
    "Auto-fixes where safe. Copy the result straight into Advanced Editor."
)

# =============================================================================
# File input
# =============================================================================
uploaded = st.file_uploader("Upload a .pbix or .pbit file", type=["pbix", "pbit"],
                              key="pq_editor_upload")
raw_bytes = None
if uploaded:
    raw_bytes = uploaded.read()
    st.session_state["pbi_file_bytes"] = raw_bytes
    st.session_state["pbi_file_name"]  = uploaded.name
elif st.session_state.get("pbi_file_bytes"):
    raw_bytes = st.session_state["pbi_file_bytes"]
    st.info(f"Using previously uploaded file: **{st.session_state.get('pbi_file_name', 'file')}**")

if not raw_bytes:
    st.warning("Upload a .pbix or .pbit file to start.")
    st.stop()

# =============================================================================
# Parse
# =============================================================================
@st.cache_data(show_spinner="Reading model...")
def _parse(data: bytes):
    return extract_pbit_metadata(io.BytesIO(data))

meta      = _parse(raw_bytes)
tables    = meta.get("tables", [])
pq_tables = [
    t for t in tables
    if any(p.get("expression") and p.get("source_type") in ("m", "", None)
           for p in t.get("partitions", []))
]

if not pq_tables:
    st.warning("No Power Query (M) tables found in this file.")
    st.stop()

# =============================================================================
# Table selector
# =============================================================================
st.markdown("---")
col_sel, col_info = st.columns([2, 3])
with col_sel:
    table_names   = [t["name"] for t in pq_tables]
    selected_name = st.selectbox("Select a table to analyze", options=table_names,
                                  key="pq_table_select")

selected_table = next((t for t in pq_tables if t["name"] == selected_name), None)
if not selected_table:
    st.stop()

m_partition = next(
    (p for p in selected_table.get("partitions", [])
     if p.get("expression") and p.get("source_type") in ("m", "", None)), None
)
original_m = (m_partition or {}).get("expression", "") or ""

with col_info:
    raw_steps = _parse_m_steps(original_m)
    n_body    = len([s for s in raw_steps if not s["name"].startswith("▶")])
    st.markdown(f"""
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
                padding:14px 18px;margin-top:24px">
      <b>Table:</b> {selected_name}&nbsp;&nbsp;|&nbsp;&nbsp;
      <b>M Steps:</b> {n_body}&nbsp;&nbsp;|&nbsp;&nbsp;
      <b>Columns:</b> {len(selected_table.get("columns", []))}&nbsp;&nbsp;|&nbsp;&nbsp;
      <b>Measures:</b> {len(selected_table.get("measures", []))}<br>
      <b>Hidden:</b> {"Yes" if selected_table.get("is_hidden") else "No"}
    </div>
    """, unsafe_allow_html=True)

if show_raw:
    with st.expander("Original raw M (read-only)"):
        st.code(original_m or "(empty)", language="plaintext")

# =============================================================================
# Session state: applied fixes per table
# =============================================================================
applied_key = f"pq_applied_fixes_{selected_name}"
if applied_key not in st.session_state:
    st.session_state[applied_key] = set()

_base_steps      = _parse_m_steps(original_m)
findings_for_fix = _analyze_steps(_base_steps)
current_steps    = _apply_all_fixes(_base_steps, st.session_state[applied_key], findings_for_fix)
findings         = _analyze_steps(current_steps)
score            = _performance_score(findings)

# =============================================================================
# Tabs
# =============================================================================
st.markdown("---")
tab_opt, tab_edit, tab_bulk = st.tabs(["🔍 Optimizer", "📝 Step Editor", "🔄 Bulk Replace"])


# ─────────────────────────────────────────── TAB 1 — Optimizer ─────────────
with tab_opt:

    if score >= 80:
        score_color, score_bg, grade = "#16a34a", "#f0fdf4", "Good"
    elif score >= 55:
        score_color, score_bg, grade = "#d97706", "#fffbeb", "Needs Work"
    else:
        score_color, score_bg, grade = "#dc2626", "#fff1f2", "Poor"

    high   = sum(1 for f in findings if f["severity"] == "HIGH")
    medium = sum(1 for f in findings if f["severity"] == "MEDIUM")
    low    = sum(1 for f in findings if f["severity"] == "LOW")
    n_fix  = sum(1 for f in findings if f.get("can_auto_fix"))
    saved  = len(_body_steps(_base_steps)) - len(_body_steps(current_steps))

    st.markdown(f"""
    <div class="score-card" style="background:{score_bg}; border:2px solid {score_color}">
      <div class="score-circle" style="background:{score_color}; color:#fff">{score}</div>
      <div>
        <b style="font-size:1.25rem;color:{score_color}">{grade}</b><br>
        <span style="font-size:.9rem">
          🔴 {high} critical &nbsp;|&nbsp;
          🟡 {medium} medium &nbsp;|&nbsp;
          🔵 {low} low &nbsp;|&nbsp;
          <b>{n_fix} auto-fixable</b>
          {f" &nbsp;|&nbsp; ✅ {saved} steps already removed" if saved > 0 else ""}
        </span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    if not findings:
        st.markdown(
            '<div class="finding-ok"><div class="finding-title">✅ No issues found</div>'
            '<div class="finding-detail">This query follows M best practices. '
            'Nothing to optimize.</div></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(f"### {len(findings)} issue(s) found in **{selected_name}**")

        for f in sorted(findings, key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["severity"]]):
            sev   = f["severity"]
            cls   = {"HIGH": "finding-high", "MEDIUM": "finding-medium", "LOW": "finding-low"}[sev]
            badge = {"HIGH": "badge-high", "MEDIUM": "badge-medium", "LOW": "badge-low"}[sev]
            icon  = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}[sev]
            affected_html = " ".join(
                f'<span class="step-affected">{n}</span>' for n in f["affected_steps"]
            )
            already = f["id"] in st.session_state[applied_key]

            st.markdown(
                f'<div class="{cls}">'
                f'<div class="finding-title"><span class="{badge}">{sev}</span>'
                f'{icon} {f["title"]}'
                f'{"  ✅ <em>Fixed</em>" if already else ""}'
                f'</div>'
                f'<div class="finding-detail">{f["detail"]}</div>'
                f'{"<br>" + affected_html if f["affected_steps"] else ""}'
                f'</div>',
                unsafe_allow_html=True,
            )

            if f.get("can_auto_fix") and not already:
                if st.button(f.get("fix_label", "🔧 Auto-Fix"),
                             key=f"fix_{f['id']}_{selected_name}"):
                    st.session_state[applied_key].add(f["id"])
                    st.rerun()
            elif f.get("can_auto_fix") and already:
                if st.button("↩️ Undo fix", key=f"undo_{f['id']}_{selected_name}"):
                    st.session_state[applied_key].discard(f["id"])
                    st.rerun()

    st.markdown("---")
    optimized_m = _reconstruct_m(current_steps)
    changed     = optimized_m.strip() != original_m.strip()

    if changed:
        bs_orig = _body_steps(_base_steps)
        bs_now  = _body_steps(current_steps)
        st.success(
            f"✅ Optimized: {len(bs_orig)} → {len(bs_now)} steps "
            f"({len(bs_orig) - len(bs_now)} removed)"
        )
        if st.button("↩️ Reset all fixes", key=f"reset_{selected_name}"):
            st.session_state[applied_key] = set()
            st.rerun()
        st.markdown("#### 📋 Optimized M Query — paste into Advanced Editor")
        st.code(optimized_m, language="plaintext")
        st.download_button("📥 Download optimized M (.txt)", data=optimized_m,
                           file_name=f"{selected_name}_optimized.txt", mime="text/plain")
    else:
        if findings:
            st.info("Apply fixes above — the clean query will appear here.")
        else:
            st.markdown("#### 📋 Current M Query")
            st.code(optimized_m or original_m, language="plaintext")

    with st.expander("🗺️ Step dependency map"):
        bs2      = _body_steps(current_steps)
        dead_set = {n for f in findings if f["id"] == "dead_steps"
                    for n in f["affected_steps"]}
        rows = []
        for i, s in enumerate(bs2):
            tags = []
            if s["is_source"]: tags.append("SOURCE")
            if s["name"] in dead_set: tags.append("DEAD ⚠️")
            for ff in findings:
                if "dup_ttc" in ff["id"] and s["name"] in ff["affected_steps"]:
                    tags.append("MERGE ⚡"); break
            rows.append({
                "#":           i + 1,
                "Step":        s["name"],
                "Tags":        " | ".join(tags) if tags else "",
                "Depends On":  ", ".join(s["depends_on"]) or "—",
                "Body":        s["body"][:90].replace("\n", " "),
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ─────────────────────────────────────────── TAB 2 — Step Editor ───────────
with tab_edit:
    st.subheader("📝 Step Editor")
    st.markdown("Edit steps individually. The **Source** step (blue) is the connection — "
                "change server name, file path, or URL here.")

    edited_steps = []
    for idx, step in enumerate(current_steps):
        is_src    = step["is_source"]
        is_result = step["name"].startswith("▶")

        if is_src:
            st.markdown(
                f'<div style="background:#eff6ff;border:2px solid #3b82f6;border-radius:8px;'
                f'padding:10px 16px;margin:6px 0">'
                f'<b>{step["name"]}</b>'
                f'<span class="step-source-badge">SOURCE — edit connection here</span>'
                f'</div>', unsafe_allow_html=True)
        else:
            dep_txt = ""
            if show_deps and step.get("depends_on") and not is_result:
                dep_txt = f"  ← depends on: {', '.join(step['depends_on'])}"
            st.markdown(
                f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;'
                f'padding:8px 16px;margin:4px 0">'
                f'<b>{step["name"]}</b>'
                f'<span style="font-size:.78rem;color:#64748b">{dep_txt}</span>'
                f'</div>', unsafe_allow_html=True)

        if is_result:
            st.info(f"**Result (in):** `{step['body']}`")
            edited_steps.append({**step})
        else:
            new_body = st.text_area(
                label=f"**{step['name']}**" + (" ← SOURCE" if is_src else ""),
                value=step["body"],
                height=max(80, min(step["body"].count("\n") * 20 + 60, 300)),
                key=f"edit_{selected_name}_{idx}",
            )
            edited_steps.append({**step, "body": new_body})

    st.markdown("---")
    final_m  = _reconstruct_m(edited_steps)
    changed2 = final_m.strip() != original_m.strip()

    if changed2:
        st.markdown(
            '<div style="background:#f0fdf4;border-left:4px solid #22c55e;border-radius:6px;'
            'padding:10px 14px;font-size:.88rem;margin:10px 0">'
            '✏️ <b>Changes detected.</b> Copy below and paste into Advanced Editor.</div>',
            unsafe_allow_html=True)

    st.markdown("#### ✅ Reconstructed M Query")
    st.code(final_m, language="plaintext")
    if changed2:
        st.download_button("📥 Download edited M (.txt)", data=final_m,
                           file_name=f"{selected_name}_edited.txt", mime="text/plain",
                           key="dl_edited")

    with st.expander("💡 Common Source patterns — reference"):
        st.code("""-- SQL Server
Source = Sql.Database("server-name", "database-name")
-- Azure SQL
Source = Sql.Database("yourserver.database.windows.net", "database-name")
-- SQL with native query (enables query folding)
Source = Sql.Database("server", "db", [Query="SELECT id, name FROM dbo.Orders WHERE year=2024"])
-- Excel
Source = Excel.Workbook(File.Contents("C:\\path\\file.xlsx"), null, true)
-- CSV
Source = Csv.Document(File.Contents("C:\\path\\file.csv"), [Delimiter=",", Encoding=1252])
-- SharePoint
Source = SharePoint.Files("https://company.sharepoint.com/sites/Site", [ApiVersion=15])
-- Web API (JSON)
Source = Json.Document(Web.Contents("https://api.example.com/data"))
-- Snowflake
Source = Snowflake.Databases("account.snowflakecomputing.com")""", language="plaintext")


# ─────────────────────────────────────────── TAB 3 — Bulk Replace ──────────
with tab_bulk:
    st.subheader("🔄 Bulk Source Replace — All Tables at Once")
    st.markdown("Replace a string across every table's Source step simultaneously. "
                "Useful for DEV → PROD migrations.")

    c1, c2 = st.columns(2)
    with c1:
        find_str = st.text_input("Find (exact string in Source step)", key="bulk_find",
                                  placeholder="e.g.  dev-sql-server")
    with c2:
        repl_str = st.text_input("Replace with", key="bulk_replace",
                                  placeholder="e.g.  prod-sql-server")

    if st.button("🔍 Preview bulk replace", key="bulk_preview_btn"):
        if not find_str.strip():
            st.warning("Enter a string to find.")
        else:
            preview_rows = []
            for t in pq_tables:
                part = next(
                    (p for p in t.get("partitions", [])
                     if p.get("expression") and p.get("source_type") in ("m", "", None)), None)
                if not part:
                    continue
                m_expr  = part.get("expression", "")
                parsed  = _parse_m_steps(m_expr)
                src_stp = next((s for s in parsed if s["is_source"]), None)
                if src_stp and find_str in src_stp["body"]:
                    preview_rows.append({
                        "Table":           t["name"],
                        "Original Source": src_stp["body"][:120],
                        "New Source":      src_stp["body"].replace(find_str, repl_str)[:120],
                    })
            if not preview_rows:
                st.info(f'No Source steps contain "{find_str}".')
            else:
                st.success(f"Found in **{len(preview_rows)}** table(s):")
                st.dataframe(pd.DataFrame(preview_rows), use_container_width=True,
                             hide_index=True, height=300)
                scripts = []
                for t in pq_tables:
                    part = next(
                        (p for p in t.get("partitions", [])
                         if p.get("expression") and p.get("source_type") in ("m", "", None)), None)
                    if not part:
                        continue
                    m_expr = part.get("expression", "")
                    if find_str not in m_expr:
                        continue
                    scripts.append(
                        f"// -- Table: {t['name']} ----------------------------------------\n"
                        + m_expr.replace(find_str, repl_str)
                    )
                if scripts:
                    full_script = "\n\n".join(scripts)
                    st.markdown("#### All updated M queries")
                    st.code(full_script, language="plaintext")
                    st.download_button("📥 Download all updated M queries (.txt)",
                                       data=full_script, file_name="updated_m_queries.txt",
                                       mime="text/plain")
