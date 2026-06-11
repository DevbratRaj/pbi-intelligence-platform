"""
12_Power_Query_Editor.py — Power Query M Editor
PBI Intelligence Platform

Pick any table, inspect every M step, edit the Source (connection/path)
or any individual step, and get the reconstructed full M query ready to
paste back into Power BI Desktop → Power Query.
"""

import sys
import io
import re
from pathlib import Path
import streamlit as st

st.set_page_config(
    page_title="Power Query Editor — PBI Intelligence Platform",
    page_icon="⚙️",
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
.step-card {
    background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px;
    padding:14px 18px; margin:8px 0;
}
.step-card.source-step {
    background:#eff6ff; border:2px solid #3b82f6;
}
.step-name {
    font-weight:700; color:#1e293b; font-size:.95rem; margin-bottom:6px;
}
.step-source-badge {
    display:inline-block; background:#3b82f6; color:#fff;
    padding:2px 10px; border-radius:20px; font-size:.75rem;
    margin-left:8px; vertical-align:middle;
}
.step-dep {
    font-size:.78rem; color:#64748b; margin-top:4px;
}
.copy-tip {
    background:#f0fdf4; border-left:4px solid #22c55e; border-radius:6px;
    padding:10px 14px; font-size:.88rem; margin:10px 0;
}
.warn-box {
    background:#fffbeb; border-left:4px solid #f59e0b; border-radius:6px;
    padding:10px 14px; font-size:.88rem; margin:10px 0;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Project module imports
# ─────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from pbit_extractor import extract_pbit_metadata

# ─────────────────────────────────────────────────────────────────────────────
# M query parser — splits `let ... in ...` into named steps
# ─────────────────────────────────────────────────────────────────────────────

def _parse_m_steps(expr: str) -> list[dict]:
    """
    Parse a Power Query M expression into a list of step dicts:
        { "name": str, "body": str, "is_source": bool, "depends_on": list[str] }

    Handles:
      • Standard  let Step1 = ..., Step2 = ..., ... in LastStep
      • Single-line and multi-line expressions
      • Quoted identifiers  #"Step Name With Spaces"
    """
    if not expr or not expr.strip():
        return []

    text = expr.strip()

    # Locate the outermost let…in boundary
    let_match = re.match(r'(?is)^let\b', text)
    if not let_match:
        # Not a standard let-in — return as a single "Query" step
        return [{"name": "Query", "body": text, "is_source": False, "depends_on": []}]

    # Find the final `in` keyword (not inside nested let/in blocks)
    in_pos = _find_final_in(text)
    if in_pos is None:
        return [{"name": "Query", "body": text, "is_source": False, "depends_on": []}]

    body_block = text[len("let"):in_pos].strip()
    final_step = text[in_pos + len("in"):].strip()

    # Split assignments by commas that are at depth-0 (not inside brackets/parens/quotes)
    raw_assignments = _split_top_level_commas(body_block)

    steps: list[dict] = []
    all_step_names: list[str] = []

    for raw in raw_assignments:
        raw = raw.strip()
        if not raw:
            continue
        # Match:  StepName = <body>   or   #"Step Name" = <body>
        m = re.match(r'^(#"[^"]*"|[A-Za-z_][A-Za-z0-9_]*)\s*=\s*', raw)
        if not m:
            # Malformed — keep as-is
            steps.append({"name": f"Step{len(steps)+1}", "body": raw,
                          "is_source": False, "depends_on": []})
            continue
        name = m.group(1).strip('"').lstrip('#').strip('"')
        body = raw[m.end():].strip()
        all_step_names.append(name)
        steps.append({"name": name, "body": body, "is_source": False, "depends_on": []})

    # Mark source step (first step whose body looks like a data-connector call)
    _SOURCE_PATTERNS = re.compile(
        r'(?i)^\s*('
        r'Sql\.Database|Sql\.Databases|'
        r'Oracle\.Database|'
        r'MySQL\.Database|'
        r'PostgreSQL\.Database|'
        r'Odbc\.DataSource|'
        r'OleDb\.DataSource|'
        r'Excel\.Workbook|'
        r'Csv\.Document|'
        r'Json\.Document|'
        r'Xml\.Document|'
        r'SharePoint\.|'
        r'AzureStorage\.|'
        r'Snowflake\.Databases|'
        r'Databricks\.|'
        r'AzureSynapse\.|'
        r'OData\.Feed|'
        r'Web\.Contents|'
        r'File\.Contents|'
        r'Folder\.Files|'
        r'GoogleAnalytics\.|'
        r'SalesforceV2?\.|'
        r'DynamicsCrm\.'
        r')'
    )
    for step in steps:
        if _SOURCE_PATTERNS.match(step["body"]):
            step["is_source"] = True
            break
    # If none matched pattern, mark the very first step as source
    if steps and not any(s["is_source"] for s in steps):
        steps[0]["is_source"] = True

    # Build simple dependency list (which prior step names appear in each body)
    name_set = set(all_step_names)
    for i, step in enumerate(steps):
        deps = [n for n in all_step_names[:i] if re.search(r'\b' + re.escape(n) + r'\b', step["body"])]
        step["depends_on"] = deps

    # Append a virtual "Result" entry showing the final `in` expression
    if final_step and final_step not in (s["name"] for s in steps):
        steps.append({"name": "▶ Result", "body": final_step,
                      "is_source": False, "depends_on": [final_step] if final_step in name_set else []})

    return steps


def _find_final_in(text: str) -> int | None:
    """Find the position of the top-level `in` keyword in a let…in M expression."""
    depth = 0
    i = 0
    in_string = False
    string_char = None

    while i < len(text):
        ch = text[i]

        # String handling
        if in_string:
            if ch == string_char:
                in_string = False
            elif ch == '#' and string_char == '"':
                pass  # #"..." — already handled by entering on '"'
            i += 1
            continue

        if ch == '"':
            in_string = True
            string_char = '"'
            i += 1
            continue

        # Bracket depth
        if ch in ('(', '[', '{'):
            depth += 1
        elif ch in (')', ']', '}'):
            depth -= 1

        # Top-level `in` keyword
        if depth == 0 and text[i:i+2].lower() == 'in' and i + 2 <= len(text):
            after = text[i+2] if i + 2 < len(text) else ' '
            before = text[i-1] if i > 0 else ' '
            if not (before.isalnum() or before in ('_', '"')) and \
               not (after.isalnum() or after in ('_', '"')):
                return i
        i += 1
    return None


def _split_top_level_commas(text: str) -> list[str]:
    """Split text on commas that are at depth-0 (not inside parens/brackets/strings)."""
    parts = []
    depth = 0
    in_string = False
    buf = []

    i = 0
    while i < len(text):
        ch = text[i]

        if in_string:
            buf.append(ch)
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            buf.append(ch)
            i += 1
            continue

        if ch in ('(', '[', '{'):
            depth += 1
            buf.append(ch)
        elif ch in (')', ']', '}'):
            depth -= 1
            buf.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1

    if buf:
        parts.append(''.join(buf))
    return parts


def _reconstruct_m(steps: list[dict]) -> str:
    """Rebuild the full M let…in query from the edited steps list."""
    result_step = None
    body_steps = []
    for s in steps:
        if s["name"].startswith("▶"):
            result_step = s["body"].strip()
        else:
            body_steps.append(s)

    if not body_steps:
        return ""

    lines = ["let"]
    for i, s in enumerate(body_steps):
        name = s["name"]
        # Quote name if it contains spaces or special chars
        if re.search(r'[^A-Za-z0-9_]', name):
            name = f'#"{name}"'
        comma = "," if i < len(body_steps) - 1 else ""
        # Indent multi-line body
        body_lines = s["body"].strip().split("\n")
        if len(body_lines) == 1:
            lines.append(f"    {name} = {body_lines[0]}{comma}")
        else:
            lines.append(f"    {name} = {body_lines[0]}")
            for bl in body_lines[1:-1]:
                lines.append(f"        {bl}")
            lines.append(f"        {body_lines[-1]}{comma}")

    last_step_name = body_steps[-1]["name"]
    result_expr = result_step or last_step_name
    if re.search(r'[^A-Za-z0-9_]', result_expr) and not result_expr.startswith('#"'):
        result_expr = f'#"{result_expr}"'
    lines.append(f"in")
    lines.append(f"    {result_expr}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("<h1>PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")
    st.markdown("### ⚙️ Editor Options")
    show_deps = st.checkbox("Show step dependencies", value=True)
    show_raw  = st.checkbox("Show raw M (read-only)", value=False)
    st.markdown("---")
    st.markdown("""
**How to use:**
1. Upload your `.pbix` or `.pbit`
2. Pick the table to edit
3. Modify the **Source** step (server, path, URL…)
4. Optionally edit any other step
5. Copy the reconstructed M query
6. In Power BI Desktop → Power Query → Advanced Editor → paste
    """)

# ─────────────────────────────────────────────────────────────────────────────
# Page header
# ─────────────────────────────────────────────────────────────────────────────
st.title("⚙️ Power Query M Editor")
st.markdown(
    "Edit any table's Power Query M steps — change the **Source** connection "
    "(server name, file path, URL) while all other transformation steps stay intact. "
    "Copy the result directly into Power BI Desktop → Advanced Editor."
)

# ─────────────────────────────────────────────────────────────────────────────
# File input
# ─────────────────────────────────────────────────────────────────────────────
uploaded = st.file_uploader(
    "Upload a .pbix or .pbit file",
    type=["pbix", "pbit"],
    key="pq_editor_upload",
)

raw_bytes: bytes | None = None
if uploaded:
    raw_bytes = uploaded.read()
    st.session_state["pbi_file_bytes"] = raw_bytes
    st.session_state["pbi_file_name"]  = uploaded.name
elif st.session_state.get("pbi_file_bytes"):
    raw_bytes = st.session_state["pbi_file_bytes"]
    st.info(f"Using previously uploaded file: **{st.session_state.get('pbi_file_name', 'file')}**")

if not raw_bytes:
    st.warning("Upload a .pbix or .pbit file to start editing.")
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# Parse
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner="Reading model…")
def _parse(data: bytes):
    return extract_pbit_metadata(io.BytesIO(data))

meta   = _parse(raw_bytes)
tables = meta.get("tables", [])

# Only tables that have at least one partition with M expression
pq_tables = [
    t for t in tables
    if any(p.get("expression") and p.get("source_type") in ("m", "", None)
           for p in t.get("partitions", []))
]

if not pq_tables:
    st.warning(
        "No Power Query (M) tables found. This file may use DirectQuery, "
        "live connection, or imported data with no stored M expressions."
    )
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# Table selector
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("---")
col_sel, col_info = st.columns([2, 3])

with col_sel:
    table_names = [t["name"] for t in pq_tables]
    selected_name = st.selectbox(
        "Select a table to edit",
        options=table_names,
        key="pq_table_select",
    )

selected_table = next((t for t in pq_tables if t["name"] == selected_name), None)
if not selected_table:
    st.stop()

# Get the M expression (first M-type partition)
m_partition = next(
    (p for p in selected_table.get("partitions", [])
     if p.get("expression") and p.get("source_type") in ("m", "", None)),
    None,
)
original_m = (m_partition or {}).get("expression", "") if m_partition else ""

with col_info:
    n_steps_raw = original_m.count("=") if original_m else 0
    st.markdown(f"""
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px;margin-top:24px">
      <b>Table:</b> {selected_name}<br>
      <b>Hidden:</b> {'Yes' if selected_table.get('is_hidden') else 'No'} &nbsp;|&nbsp;
      <b>Internal:</b> {'Yes' if selected_table.get('is_internal') else 'No'}<br>
      <b>Columns:</b> {len(selected_table.get('columns', []))} &nbsp;|&nbsp;
      <b>Measures:</b> {len(selected_table.get('measures', []))}
    </div>
    """, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Raw M view (optional)
# ─────────────────────────────────────────────────────────────────────────────
if show_raw:
    with st.expander("📄 Raw M Query (original, read-only)"):
        st.code(original_m or "(empty)", language="plaintext")

# ─────────────────────────────────────────────────────────────────────────────
# Parse into steps
# ─────────────────────────────────────────────────────────────────────────────
steps = _parse_m_steps(original_m)

if not steps:
    st.warning("Could not parse M expression for this table.")
    st.stop()

st.markdown("---")
st.subheader("📝 Step-by-Step Editor")
st.markdown(
    "Each step below is editable. The **Source** step (highlighted in blue) is where "
    "you change the server name, file path, database name, or URL. "
    "All other steps are preserved exactly as-is unless you choose to edit them."
)

# ─────────────────────────────────────────────────────────────────────────────
# Step editor — one text_area per step
# ─────────────────────────────────────────────────────────────────────────────
edited_steps: list[dict] = []

for idx, step in enumerate(steps):
    is_src    = step["is_source"]
    is_result = step["name"].startswith("▶")
    card_cls  = "step-card source-step" if is_src else "step-card"

    badge = '<span class="step-source-badge">SOURCE</span>' if is_src else ""
    dep_html = ""
    if show_deps and step["depends_on"] and not is_result:
        dep_html = f'<div class="step-dep">Depends on: {", ".join(step["depends_on"])}</div>'

    st.markdown(
        f'<div class="{card_cls}">'
        f'<div class="step-name">{step["name"]}{badge}</div>'
        f'{dep_html}'
        f'</div>',
        unsafe_allow_html=True,
    )

    if is_result:
        # Result step — show as non-editable info
        st.info(f"**Result:** `{step['body']}`")
        edited_steps.append({**step})
    else:
        new_body = st.text_area(
            label=f"Step body — **{step['name']}**" + (" ← edit source here" if is_src else ""),
            value=step["body"],
            height=120 if "\n" not in step["body"] else max(120, step["body"].count("\n") * 22 + 60),
            key=f"step_body_{selected_name}_{idx}",
            help="Edit this step's M expression. For the Source step, change server/path/URL here.",
        )
        edited_steps.append({**step, "body": new_body})

# ─────────────────────────────────────────────────────────────────────────────
# Reconstruct and show final M query
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("✅ Reconstructed M Query — Copy into Advanced Editor")

final_m = _reconstruct_m(edited_steps)

# Detect if anything was actually changed
changed = final_m.strip() != original_m.strip()
if changed:
    st.markdown(
        '<div class="copy-tip">✏️ <b>Changes detected.</b> Copy the query below and paste it into '
        'Power BI Desktop → Power Query → select the table → Home → Advanced Editor → replace all → Done.</div>',
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        '<div class="warn-box">ℹ️ No changes made yet. Edit the Source step above to modify the connection.</div>',
        unsafe_allow_html=True,
    )

st.code(final_m, language="plaintext")

# Diff summary
if changed:
    st.markdown("#### What changed")
    orig_lines = original_m.strip().splitlines()
    new_lines  = final_m.strip().splitlines()
    diffs = []
    max_len = max(len(orig_lines), len(new_lines))
    for i in range(max_len):
        o = orig_lines[i].strip() if i < len(orig_lines) else "(removed)"
        n = new_lines[i].strip()  if i < len(new_lines)  else "(added)"
        if o != n:
            diffs.append({"Line": i + 1, "Original": o, "New": n})
    if diffs:
        import pandas as pd
        st.data_editor(
            pd.DataFrame(diffs),
            use_container_width=True,
            hide_index=True,
            disabled=True,
            key="diff_table",
        )

# ─────────────────────────────────────────────────────────────────────────────
# Quick source pattern helper
# ─────────────────────────────────────────────────────────────────────────────
with st.expander("💡 Common Source patterns — reference"):
    st.code("""
-- SQL Server
Source = Sql.Database("server-name", "database-name")

-- SQL Server with options
Source = Sql.Database("server-name", "database-name", [Query="SELECT ..."])

-- Azure SQL
Source = Sql.Database("yourserver.database.windows.net", "database-name")

-- Excel file
Source = Excel.Workbook(File.Contents("C:\\path\\to\\file.xlsx"), null, true)

-- CSV file
Source = Csv.Document(File.Contents("C:\\path\\to\\file.csv"), [Delimiter=","])

-- SharePoint folder
Source = SharePoint.Files("https://company.sharepoint.com/sites/SiteName", [ApiVersion=15])

-- OData feed
Source = OData.Feed("https://services.odata.org/V4/Northwind/", null, [Implementation="2.0"])

-- Web
Source = Web.Contents("https://api.example.com/data")

-- Snowflake
Source = Snowflake.Databases("account.snowflakecomputing.com")

-- Azure Synapse
Source = AzureSynapse.WorkspaceArtifacts("https://workspace.azuresynapse.net")
""", language="plaintext")

# ─────────────────────────="───────────────────────────────────────────────────
# Bulk source replacement
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("🔄 Bulk Source Replace — All Tables at Once")
st.markdown(
    "Replace a string across **every table's Source step** in one go. "
    "Useful when migrating from DEV → PROD (e.g. change server name everywhere)."
)

c1, c2 = st.columns(2)
with c1:
    find_str  = st.text_input("Find (exact string in Source step)", key="bulk_find",
                               placeholder='e.g. dev-sql-server')
with c2:
    repl_str  = st.text_input("Replace with", key="bulk_replace",
                               placeholder='e.g. prod-sql-server')

if st.button("🔍 Preview bulk replace", key="bulk_preview_btn"):
    if not find_str.strip():
        st.warning("Enter a string to find.")
    else:
        results = []
        for t in pq_tables:
            part = next(
                (p for p in t.get("partitions", [])
                 if p.get("expression") and p.get("source_type") in ("m", "", None)),
                None,
            )
            if not part:
                continue
            m_expr = part.get("expression", "")
            parsed = _parse_m_steps(m_expr)
            src_step = next((s for s in parsed if s["is_source"]), None)
            if src_step and find_str in src_step["body"]:
                new_body = src_step["body"].replace(find_str, repl_str)
                results.append({
                    "Table": t["name"],
                    "Original Source": src_step["body"][:120],
                    "New Source": new_body[:120],
                })

        if not results:
            st.info(f"No Source steps contain **{find_str}**.")
        else:
            import pandas as pd
            st.success(f"Found in **{len(results)}** table(s):")
            st.data_editor(
                pd.DataFrame(results),
                use_container_width=True,
                hide_index=True,
                disabled=True,
                key="bulk_preview_table",
                height=300,
            )

            # Generate full replacement scripts
            scripts: list[str] = []
            for t in pq_tables:
                part = next(
                    (p for p in t.get("partitions", [])
                     if p.get("expression") and p.get("source_type") in ("m", "", None)),
                    None,
                )
                if not part:
                    continue
                m_expr = part.get("expression", "")
                if find_str not in m_expr:
                    continue
                new_m = m_expr.replace(find_str, repl_str)
                scripts.append(
                    f"// ── Table: {t['name']} ──────────────────────────────────────\n{new_m}"
                )

            if scripts:
                full_script = "\n\n".join(scripts)
                st.markdown("#### 📋 All updated M queries (copy table-by-table into Advanced Editor)")
                st.code(full_script, language="plaintext")
                st.download_button(
                    "📥 Download all updated M queries (.txt)",
                    data=full_script,
                    file_name="updated_m_queries.txt",
                    mime="text/plain",
                )
