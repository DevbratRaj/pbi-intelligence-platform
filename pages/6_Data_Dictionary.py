"""
6_Data_Dictionary.py — Data Dictionary
PBI Intelligence Platform

Standalone page: interactive data dictionary built from your Power BI file's
report visuals — every field, measure and column used across all pages.
"""

import re
import sys
import io
import json
import zipfile
from pathlib import Path
import streamlit as st
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Page config + CSS
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Data Dictionary — PBI Intelligence Platform",
    page_icon="📖",
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
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("<h1>PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# Import pbit_extractor from project root
# ─────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from pbit_extractor import extract_pbit_metadata

# ─────────────────────────────────────────────────────────────────────────────
# Helper constants
# ─────────────────────────────────────────────────────────────────────────────
_ALWAYS_HIDDEN_VISUAL_TYPES: frozenset[str] = frozenset({
    "shape", "basicShape", "image", "textbox", "button", "actionButton",
})

_DATA_VISUAL_TYPES: frozenset[str] = frozenset({
    "barChart", "clusteredBarChart", "stackedBarChart", "hundredPercentStackedBarChart",
    "columnChart", "clusteredColumnChart", "stackedColumnChart", "hundredPercentStackedColumnChart",
    "lineChart", "areaChart", "stackedAreaChart", "hundredPercentStackedAreaChart",
    "lineClusteredColumnComboChart", "lineStackedColumnComboChart",
    "ribbonChart", "waterfallChart", "funnelChart", "scatterChart", "bubbleChart",
    "pieChart", "donutChart", "treemap", "sunburstChart",
    "tableEx", "pivotTable", "card", "multiRowCard", "kpi", "gauge",
    "map", "filledMap", "azureMap", "shapeMap", "slicer",
    "decompositionTreeVisual", "keyInfluencers", "qnaVisual",
    "rdlVisual", "pythonVisual", "rVisual", "scriptVisual", "customVisual",
})


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────
def _is_data_visual(visual_type: str, field_count: int) -> bool:
    vt = visual_type.lower() if visual_type else ""
    if visual_type in _ALWAYS_HIDDEN_VISUAL_TYPES or vt in {v.lower() for v in _ALWAYS_HIDDEN_VISUAL_TYPES}:
        return False
    if vt == "slicer" and field_count == 0:
        return False
    if field_count == 0 and vt not in {v.lower() for v in _DATA_VISUAL_TYPES}:
        return False
    return True


def _clean_query_ref(ref: str) -> str:
    return re.sub(r"'([^']+)'\[", r"\1[", ref.strip())


def _extract_visual_title(config: dict, container: dict) -> str:
    sv = config.get("singleVisual", {})
    title_items = sv.get("vcObjects", {}).get("title", [])
    if isinstance(title_items, list) and title_items:
        props = title_items[0].get("properties", {})
        text_node = props.get("text", {}).get("expr", {})
        literal = text_node.get("Literal", {}).get("Value", "")
        if literal:
            return literal.strip("'")
        rpi = text_node.get("ResourcePackageItem", {}).get("name", "")
        if rpi:
            return rpi
    return container.get("name", "")


def _fields_with_roles(sv: dict, from_map: dict | None = None) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    from_map = from_map or {}

    projections = sv.get("projections", {})
    if isinstance(projections, dict):
        for role, items in projections.items():
            if not isinstance(items, list):
                continue
            for item in items:
                qr = _clean_query_ref(item.get("queryRef", ""))
                if qr and qr not in seen:
                    seen.add(qr)
                    rows.append({"Field": qr, "Role": role})

    proto_q = sv.get("prototypeQuery", {})
    for sel in proto_q.get("Select", []):
        field = ""
        native = sel.get("NativeReferenceName", "")
        if native and "[" in native:
            field = _clean_query_ref(native)

        if not field:
            for kind in ("Measure", "Column", "HierarchyLevel"):
                node = sel.get(kind, {})
                if not isinstance(node, dict) or not node:
                    continue
                src_ref = node.get("Expression", {}).get("SourceRef", {})
                entity = src_ref.get("Entity", "") or from_map.get(src_ref.get("Source", ""), "")
                prop = node.get("Property", "")
                if prop:
                    field = f"{entity}[{prop}]" if entity else f"[{prop}]"
                    break

        if not field:
            expr = sel.get("expression", sel.get("Expression", {}))
            if isinstance(expr, dict):
                for kind in ("Measure", "Column", "HierarchyLevel"):
                    node = expr.get(kind, {})
                    if not isinstance(node, dict) or not node:
                        continue
                    src_ref = node.get("Expression", {}).get("SourceRef", {})
                    entity = src_ref.get("Entity", "") or from_map.get(src_ref.get("Source", ""), "")
                    prop = node.get("Property", "")
                    if prop:
                        field = f"{entity}[{prop}]" if entity else f"[{prop}]"
                        break

        if field and field not in seen:
            seen.add(field)
            rows.append({"Field": field, "Role": "query"})

    return rows


def _parse_report_pages(raw_bytes: bytes) -> list[dict]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            raw = zf.read("Report/Layout")
    except Exception:
        return []
    try:
        layout = json.loads(raw.decode("utf-16-le"))
    except Exception:
        return []

    pages_out = []
    for section in layout.get("sections", []):
        page_name = section.get("displayName") or section.get("name", "Unknown Page")
        visuals_out = []
        for container in section.get("visualContainers", []):
            try:
                config = json.loads(container.get("config", "{}"))
            except Exception:
                config = {}
            single_visual = config.get("singleVisual", {})
            visual_type = single_visual.get("visualType") or "unknown"
            title = _extract_visual_title(config, container)
            proto_q = single_visual.get("prototypeQuery", {})
            from_map = {
                f.get("Name", ""): f.get("Entity", "")
                for f in proto_q.get("From", []) if f.get("Name") and f.get("Entity")
            }
            field_rows = _fields_with_roles(single_visual, from_map)
            visuals_out.append({"title": title or visual_type, "type": visual_type, "fields": field_rows})
        pages_out.append({"name": page_name, "visuals": visuals_out})
    return pages_out


def _humanise_name(name: str) -> str:
    """Convert a technical name like 'TotalCollected' or 'FINANCIAL' to readable text."""
    name = name.replace(".", " ")
    name = re.sub(r"\b([A-Z]{2,})\b", lambda m: m.group(1).capitalize(), name)
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    name = re.sub(r"[_\-]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _make_plain_desc(name: str, table: str, meta: dict, expression: str = "") -> str:
    """Generate a plain-English description from a measure/field name + expression."""
    measure = next(
        (m for m in meta.get("measures", []) if m["name"].lower() == name.lower()), None
    )
    if measure and measure.get("description"):
        return measure["description"]

    human = _humanise_name(name)
    n = name.lower().replace(".", " ").replace("_", " ")
    expr_lower = (expression or "").lower()

    if expression:
        if "totalmtd" in expr_lower or "totalytd" in expr_lower or "totalqtd" in expr_lower:
            period = "month" if "mtd" in expr_lower else ("year" if "ytd" in expr_lower else "quarter")
            return f"Running total of {human} from the start of the {period} up to the selected date."
        if "datesbetween" in expr_lower or "datesytd" in expr_lower:
            return f"Calculates {human} over a specific date range."
        if "divide(" in expr_lower:
            return f"Calculates {human} as a ratio or percentage — divides two values safely."
        if "calculate(" in expr_lower and "filter(" in expr_lower:
            return f"Calculates {human} by applying specific filter conditions."
        if "distinctcount(" in expr_lower:
            return f"Counts the number of unique values for {human}."
        if "countrows(" in expr_lower:
            return f"Counts the number of records for {human}."
        if "average(" in expr_lower or "averagex(" in expr_lower:
            return f"The average value of {human}."
        if "sum(" in expr_lower or "sumx(" in expr_lower:
            return f"The total amount of {human} added together."
        if "max(" in expr_lower or "maxx(" in expr_lower:
            return f"The highest recorded value of {human}."
        if "min(" in expr_lower or "minx(" in expr_lower:
            return f"The lowest recorded value of {human}."

    if re.search(r"\byoy\b|year.?on.?year", n):
        return f"Shows how {human} compares to the same period last year."
    if re.search(r"\bytd\b|year.?to.?date", n):
        return f"Running total of {human} from the start of the year to today."
    if re.search(r"\bmtd\b|month.?to.?date|monthlyrunning|runningcoll", n):
        return f"Running total of {human} from the start of the month to today."
    if re.search(r"\bbudget\b|\bforecast\b|\btarget\b", n):
        return f"The planned or budgeted value for {human}."
    if re.search(r"\bvariance\b", n):
        return f"The difference between the actual and expected value of {human}."
    if re.search(r"\bgrowth\b|\bchange\b", n):
        return f"How much {human} has grown or changed over the selected period."
    if re.search(r"\bavg\b|\baverage\b", n):
        return f"The average value of {human}."
    if re.search(r"\bpct\b|\bpercent\b|\bratio\b|\brate\b|\bmargin\b", n):
        return f"The percentage or rate for {human}."
    if re.search(r"\bcount\b|\bcnt\b|\brows\b", n):
        return f"The total count of {human}."
    if re.search(r"\bmax\b|\bmaximum\b|\bhighest\b", n):
        return f"The highest value of {human}."
    if re.search(r"\bmin\b|\bminimum\b|\blowest\b", n):
        return f"The lowest value of {human}."
    if re.search(r"\btotal\b|\bsum\b|\bamount\b|\bcollected\b|\bcollection\b", n):
        return f"The total amount of {human} added together."
    if re.search(r"\bworkingday\b|\bworkday\b|\bworking.?day\b", n):
        return f"The working day number within the current month."
    if re.search(r"\bcommission\b", n):
        return f"The commission amount for {human}."
    if re.search(r"\bactive\b|\bcurrent\b", n):
        return f"The current active count or value of {human}."
    if re.search(r"\bdate\b|\bmonth\b|\bquarter\b|\bweek\b", n):
        return f"The date / time value used to filter or group data — {human}."

    t = _humanise_name(table) if table else ""
    return f"Shows {human} from the {t} data." if t else f"Shows {human}."


# ─────────────────────────────────────────────────────────────────────────────
# Page title
# ─────────────────────────────────────────────────────────────────────────────
st.title("📖 Data Dictionary")
st.markdown(
    "A plain-English reference of every measure and field used across all report visuals — "
    "formatted to match your standard documentation template. "
    "Share with business users, stakeholders, or new team members, no DAX knowledge required."
)
st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# File source — shared session bytes (uploaded on another page) or local upload
# ─────────────────────────────────────────────────────────────────────────────
raw_bytes: bytes | None = st.session_state.get("pbi_file_bytes")

if not raw_bytes:
    st.info("📂 No file loaded yet. Upload a **.pbit / .pbix** file to build the dictionary.")
    up = st.file_uploader("Upload Power BI file", type=["pbit", "pbix"], label_visibility="collapsed")
    if up:
        raw_bytes = up.read()
        st.session_state["pbi_file_bytes"] = raw_bytes
        st.success(f"✅ {up.name} loaded ({round(len(raw_bytes)/1024, 1)} KB)")
        st.rerun()
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# Parse metadata + report pages
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner="Parsing model…")
def _get_meta_cached(b: bytes) -> dict:
    import tempfile, os
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pbit") as tmp:
        tmp.write(b)
        tmp_path = tmp.name
    try:
        return extract_pbit_metadata(tmp_path)
    finally:
        os.unlink(tmp_path)


@st.cache_data(show_spinner="Reading report pages…")
def _get_pages_cached(b: bytes) -> list[dict]:
    return _parse_report_pages(b)


try:
    meta = _get_meta_cached(raw_bytes)
    for e in meta.get("errors", []):
        st.warning(f"⚠️ {e}")
except Exception as exc:
    st.error(f"Could not parse file: {exc}")
    st.stop()

report_pages = _get_pages_cached(raw_bytes)

b1, b2 = st.columns([4, 1])
with b1:
    file_label = Path(meta.get("file", "unknown")).name
    st.markdown(
        f"<span style='background:#eff6ff;color:#1d4ed8;border-radius:6px;"
        f"padding:4px 12px;font-size:.85rem;font-weight:600;'>📁 {file_label}</span>",
        unsafe_allow_html=True,
    )
with b2:
    if st.button("🔄 Clear file", use_container_width=True):
        st.session_state.pop("pbi_file_bytes", None)
        st.rerun()

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# Build Data Dictionary
# ─────────────────────────────────────────────────────────────────────────────
# measure name/key → {expression, description}
measure_detail: dict[str, dict] = {}
for m in meta.get("measures", []):
    expr = m.get("expression", "") or ""
    if isinstance(expr, list):
        expr = "\n".join(expr)
    key_full = f"{m['table']}[{m['name']}]"
    measure_detail[key_full] = {"expression": expr.strip(), "description": m.get("description", "")}
    measure_detail[m["name"]] = {"expression": expr.strip(), "description": m.get("description", "")}

measure_key_set = {f"{m['table']}[{m['name']}]" for m in meta.get("measures", [])}

_DECORATOR_TYPES = {t.lower() for t in _ALWAYS_HIDDEN_VISUAL_TYPES}

dict_rows: list[dict] = []
seen_combos: set[tuple] = set()

for page in report_pages:
    page_name = page.get("name", "Unknown Page")
    for visual in page.get("visuals", []):
        vt = (visual.get("type", "") or "").lower()
        if vt in _DECORATOR_TYPES:
            continue
        fields = visual.get("fields", [])
        if not fields:
            continue
        visual_title = visual.get("title") or visual.get("type", "Unknown")
        for field_row in fields:
            field_key = _clean_query_ref(field_row.get("Field", ""))
            if not field_key:
                continue
            if "[" not in field_key and field_key not in measure_detail:
                continue
            combo = (page_name, visual_title, field_key)
            if combo in seen_combos:
                continue
            seen_combos.add(combo)

            field_name = field_key.split("[", 1)[1].rstrip("]") if "[" in field_key else field_key
            table_name = field_key.split("[", 1)[0] if "[" in field_key else ""
            is_measure = field_key in measure_key_set
            obj_type = "Measure" if is_measure else "Column / Field"

            m_detail = measure_detail.get(field_key) or measure_detail.get(field_name, {})
            expression = m_detail.get("expression", "")
            if is_measure and expression:
                criteria = f"{field_name} = {expression}"
            elif is_measure:
                criteria = field_name
            else:
                criteria = field_key

            comments = (
                m_detail.get("description")
                or _make_plain_desc(field_name, table_name, meta, expression)
            )

            dict_rows.append({
                "Segment":               page_name,
                "Report":                page_name,
                "Visual":                visual_title,
                "Required Data Pointer": field_name,
                "Type":                  obj_type,
                "Criteria Defined":      criteria,
                "Comments":              comments,
            })

if not dict_rows:
    st.info(
        "📂 No report page data found. The dictionary is built from the visuals inside "
        "your actual report pages. Make sure the uploaded file contains report pages."
    )
    st.stop()

df_dict = pd.DataFrame(dict_rows)

# ── Segment override ─────────────────────────────────────────────────────────
with st.expander("🗂 Edit Segment labels (optional)", expanded=False):
    st.caption(
        "By default the Segment is set to the report page name. "
        "Type a different business label (e.g. 'Revenue', 'Operations') for each page below."
    )
    seg_map: dict[str, str] = {}
    for pg in sorted(df_dict["Report"].unique()):
        seg_map[pg] = st.text_input(f"Segment for page '{pg}'", value=pg, key=f"seg_{pg}")
    df_dict["Segment"] = df_dict["Report"].map(seg_map)

# ── Filters ──────────────────────────────────────────────────────────────────
fc1, fc2, fc3 = st.columns(3)
with fc1:
    page_filter = st.multiselect(
        "Filter by Report Page", sorted(df_dict["Report"].unique()), key="dd_page"
    )
with fc2:
    type_filter = st.multiselect(
        "Filter by Type", ["Measure", "Column / Field"], key="dd_type"
    )
with fc3:
    search_term = st.text_input(
        "Search", placeholder="measure or visual name…", key="dd_search"
    )

filtered = df_dict.copy()
if page_filter:
    filtered = filtered[filtered["Report"].isin(page_filter)]
if type_filter:
    filtered = filtered[filtered["Type"].isin(type_filter)]
if search_term:
    mask = (
        filtered["Required Data Pointer"].str.contains(search_term, case=False, na=False)
        | filtered["Visual"].str.contains(search_term, case=False, na=False)
    )
    filtered = filtered[mask]

st.caption(f"Showing **{len(filtered)}** of {len(df_dict)} entries")

display_cols = ["Segment", "Report", "Visual", "Required Data Pointer",
                "Criteria Defined", "Comments"]
st.dataframe(filtered[display_cols], use_container_width=True, hide_index=True)

# ── Excel export ──────────────────────────────────────────────────────────────
st.markdown("---")
_buf = io.BytesIO()
with pd.ExcelWriter(_buf, engine="openpyxl") as _writer:
    filtered[display_cols].to_excel(
        _writer, index=False, sheet_name="Data Dictionary"
    )
    _ws = _writer.sheets["Data Dictionary"]
    from openpyxl.styles import Font as _Font, PatternFill as _Fill, Alignment as _Align
    _hf = _Font(bold=True, color="FFFFFF")
    _hb = _Fill(fill_type="solid", fgColor="1E3A5F")
    for _cell in _ws[1]:
        _cell.font  = _hf
        _cell.fill  = _hb
        _cell.alignment = _Align(horizontal="center", wrap_text=True)
    _col_widths = [18, 20, 25, 30, 60, 55]
    for _i, _col in enumerate(_ws.columns):
        _letter = _col[0].column_letter
        _ws.column_dimensions[_letter].width = _col_widths[_i] if _i < len(_col_widths) else 20
    for _row in _ws.iter_rows(min_row=2):
        for _cell in _row:
            _cell.alignment = _Align(wrap_text=True, vertical="top")

st.download_button(
    label="⬇ Export Data Dictionary to Excel",
    data=_buf.getvalue(),
    file_name="data_dictionary.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
    type="primary",
)
st.caption(
    "Columns exported: **Segment · Report · Visual · Required Data Pointer · "
    "Criteria Defined · Comments** — matching your standard documentation template."
)
