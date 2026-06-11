import streamlit as st
import zipfile
import io
import json
import re
from collections import defaultdict

# ─────────────────────────────────────────────────────────────────────────────
# Page config + styles
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Data Quality Engine — PBI Intelligence Platform",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
        [data-testid="stSidebar"] { background-color: #0d1b2a; }
        [data-testid="stSidebar"] * { color: #e0e8f0; }
        [data-testid="stSidebar"] h1 {
            color: #ffffff; font-size: 1.4rem; font-weight: 700;
            padding-bottom: 0.5rem; border-bottom: 1px solid #1e3a5f;
        }
        .risk-high   { background:#fdecea; border-left:4px solid #d32f2f; padding:12px 16px; border-radius:6px; margin-bottom:10px; }
        .risk-medium { background:#fff3e0; border-left:4px solid #e65100; padding:12px 16px; border-radius:6px; margin-bottom:10px; }
        .risk-low    { background:#fffde7; border-left:4px solid #f9a825; padding:12px 16px; border-radius:6px; margin-bottom:10px; }
        .score-box   { padding:20px 28px; border-radius:10px; display:inline-block; margin-bottom:24px; }
        .cat-direct    { background:#c8e6c9; color:#1b5e20; border-radius:4px; padding:2px 7px; font-size:0.78rem; font-weight:600; }
        .cat-ind-meas  { background:#bbdefb; color:#0d47a1; border-radius:4px; padding:2px 7px; font-size:0.78rem; font-weight:600; }
        .cat-ind-chain { background:#d1c4e9; color:#4a148c; border-radius:4px; padding:2px 7px; font-size:0.78rem; font-weight:600; }
        .cat-filter    { background:#b2ebf2; color:#006064; border-radius:4px; padding:2px 7px; font-size:0.78rem; font-weight:600; }
        .cat-rls       { background:#ffe0b2; color:#e65100; border-radius:4px; padding:2px 7px; font-size:0.78rem; font-weight:600; }
        .cat-model     { background:#ede7f6; color:#4a148c; border-radius:4px; padding:2px 7px; font-size:0.78rem; font-weight:600; }
        .cat-orphan    { background:#fce4ec; color:#880e4f; border-radius:4px; padding:2px 7px; font-size:0.78rem; font-weight:600; }
        .col-row       { padding:8px 14px; border-radius:5px; margin-bottom:6px; border:1px solid #e0e0e0; font-size:0.85rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("<h1>PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")

st.title("🔎 Data Quality Engine")
st.markdown(
    "Two tools in one page: **Data Quality Audit** (10 structural checks) "
    "and **Column Usage Analyser** (full inventory of every column with where it is used)."
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_ENCODING_ORDER = ("utf-8-sig", "utf-16-le", "utf-16", "utf-8", "utf-16-be", "latin-1")
_BRACKET_COL_RE = re.compile(r"'?([^'\[\r\n,()]+)'?\[([^\]]+)\]")

# ─────────────────────────────────────────────────────────────────────────────
# Shared parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _try_decode(raw: bytes) -> dict | None:
    for enc in _ENCODING_ORDER:
        try:
            text = raw.decode(enc)
            stripped = text.lstrip("\ufeff").lstrip()
            if stripped and stripped[0] not in ("{", "["):
                continue
            return json.loads(text)
        except Exception:
            continue
    return None


def _extract_schema(raw_bytes: bytes):
    """
    Returns (tables, relationships).
      tables: list of {name, columns:[{name,dataType,type,isHidden}],
                       measures:[{name,expression,description}], isHidden}
      relationships: list of {fromTable,fromColumn,toTable,toColumn,
                              fromCardinality,toCardinality,crossFilter}

    Supports both standard PBIX/PBIT (outer DataModelSchema JSON) and the newer
    enhanced-model PBIX format where DataModel is itself a nested ZIP archive.
    """
    tables: list[dict] = []
    relationships: list[dict] = []

    def _parse_model(model: dict) -> None:
        """Fill tables/relationships in-place from a model dict."""
        for tbl in model.get("tables", []):
            t_name = tbl.get("name", "")
            cols = []
            for c in tbl.get("columns", []):
                c_name = c.get("name", "")
                if not c_name or c_name.startswith("RowNumber"):
                    continue
                cols.append({
                    "name": c_name,
                    "dataType": c.get("dataType", ""),
                    "type": c.get("type", ""),
                    "isHidden": c.get("isHidden", False),
                })
            measures_list = []
            for m in tbl.get("measures", []):
                expr = m.get("expression", "")
                if isinstance(expr, list):
                    expr = "\n".join(expr)
                measures_list.append({
                    "name": m.get("name", ""),
                    "expression": expr.strip(),
                    "description": m.get("description", ""),
                })
            tables.append({
                "name": t_name,
                "columns": cols,
                "measures": measures_list,
                "isHidden": tbl.get("isHidden", False),
            })
        for rel in model.get("relationships", []):
            relationships.append({
                "fromTable":       rel.get("fromTable", ""),
                "fromColumn":      rel.get("fromColumn", ""),
                "toTable":         rel.get("toTable", ""),
                "toColumn":        rel.get("toColumn", ""),
                "fromCardinality": str(rel.get("fromCardinality", "")).lower(),
                "toCardinality":   str(rel.get("toCardinality", "")).lower(),
                "crossFilter":     rel.get("crossFilteringBehavior", ""),
            })

    def _try_candidates(target_zf: zipfile.ZipFile, target_names: dict) -> bool:
        """Try every DataModelSchema candidate in target_zf. Returns True if data found."""
        cands = [
            real for lower, real in target_names.items()
            if "datamodelschema" in lower or lower.endswith(".bim")
        ]
        for cand in cands:
            try:
                schema = _try_decode(target_zf.read(cand))
                if not schema:
                    continue
                _parse_model(schema.get("model", schema))
                if tables:
                    return True
            except Exception:
                continue
        return False

    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            names_lower = {n.lower(): n for n in zf.namelist()}

            # Path A: outer DataModelSchema JSON (standard PBIX / all PBIT)
            _try_candidates(zf, names_lower)

            # Path B: enhanced-model PBIX — DataModel entry is itself a ZIP archive
            if not tables:
                dm_entry = next(
                    (real for lower, real in names_lower.items()
                     if lower == "datamodel" or lower.endswith("/datamodel")),
                    None,
                )
                if dm_entry:
                    try:
                        inner_bytes = zf.read(dm_entry)
                        with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner_zf:
                            inner_names = {n.lower(): n for n in inner_zf.namelist()}
                            _try_candidates(inner_zf, inner_names)
                    except (zipfile.BadZipFile, Exception):
                        pass

    except zipfile.BadZipFile:
        st.error("The stored file is not a valid .pbix / .pbit archive.")
    return tables, relationships


def _build_field_usage_context(raw_bytes: bytes) -> dict:
    """
    Returns {  "Table[Column]": [{"page","visual_name","visual_type","role"}] }
    Normalises 'Table'[Col] and Table.Col notation.
    """
    ctx: dict[str, list[dict]] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            try:
                raw = zf.read("Report/Layout")
            except KeyError:
                return ctx
            layout = None
            for enc in _ENCODING_ORDER:
                try:
                    layout = json.loads(raw.decode(enc))
                    break
                except Exception:
                    continue
            if not layout:
                return ctx
            for section in layout.get("sections", []):
                page_name = section.get("displayName") or section.get("name", "Unknown Page")
                for container in section.get("visualContainers", []):
                    try:
                        config = json.loads(container.get("config", "{}"))
                    except Exception:
                        continue
                    sv = config.get("singleVisual", {})
                    v_type = sv.get("visualType", "unknown")
                    title_items = sv.get("vcObjects", {}).get("title", [])
                    v_title = ""
                    if isinstance(title_items, list) and title_items:
                        lit = (title_items[0].get("properties", {})
                               .get("text", {}).get("expr", {})
                               .get("Literal", {}).get("Value", ""))
                        v_title = lit.strip("'") if lit else ""
                    if not v_title:
                        v_title = container.get("name", "")
                    projections = sv.get("projections", {})
                    if not isinstance(projections, dict):
                        continue
                    for role, role_items in projections.items():
                        if not isinstance(role_items, list):
                            continue
                        for item in role_items:
                            qr = item.get("queryRef", "").strip()
                            if not qr:
                                continue
                            qr = re.sub(r"'([^']+)'\[", r"\1[", qr)
                            if "." in qr and "[" not in qr:
                                qr = re.sub(r"^([^.]+)\.(.+)$", r"\1[\2]", qr)
                            if "[" not in qr:
                                continue
                            ctx.setdefault(qr, []).append({
                                "page": page_name,
                                "visual_name": v_title,
                                "visual_type": v_type,
                                "role": role,
                            })
    except zipfile.BadZipFile:
        pass
    return ctx

# ─────────────────────────────────────────────────────────────────────────────
# System table filter (Fix 1)
# ─────────────────────────────────────────────────────────────────────────────

def _is_system_table(t: dict) -> bool:
    """Return True for Power BI auto-generated / internal tables."""
    name   = t.get("name", "")
    hidden = t.get("isHidden", False)
    if name.startswith("DateTableTemplate"):
        return True
    if name.startswith("LocalDateTable"):
        return True
    if "LocalDate" in name:
        return True
    if hidden and name.lower().startswith("date"):
        return True
    if name.startswith("$"):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════  SECTION 1 — DATA QUALITY AUDIT  ═══════════════════
# ─────────────────────────────────────────────────────────────────────────────

# Check 1 — Empty Tables
def _check_empty_tables(tables: list[dict]) -> list[dict]:
    out = []
    for t in tables:
        if len(t["columns"]) == 0 and len(t["measures"]) == 0:
            out.append({
                "table": t["name"], "item_name": t["name"], "item_type": "Table",
                "issue": "Table has no columns — likely a disconnected query or staging table left behind",
                "recommendation": "Remove this table from the model or fix the Power Query step so it returns columns",
                "risk": "HIGH",
            })
    return out


# Check 2 — Columns with no data type
def _check_no_datatype(tables: list[dict]) -> list[dict]:
    out = []
    for t in tables:
        for c in t["columns"]:
            if not str(c.get("dataType", "")).strip():
                out.append({
                    "table": t["name"], "item_name": c["name"], "item_type": "Column",
                    "issue": "No data type defined — can cause implicit conversion errors in DAX and slow queries",
                    "recommendation": f"Set an explicit data type for {t['name']}[{c['name']}] in Power Query or model view",
                    "risk": "MEDIUM",
                })
    return out


# Check 3 — Orphan columns (summary card — Fix 2)
def _check_orphan_columns(
    tables: list[dict],
    relationships: list[dict],
    field_usage_ctx: dict,
) -> list[dict]:
    measure_refs: set[str] = set()
    for t in tables:
        for m in t["measures"]:
            for match in _BRACKET_COL_RE.finditer(m["expression"]):
                measure_refs.add(f"{match.group(1).strip()}[{match.group(2).strip()}]")

    rel_refs: set[str] = set()
    for rel in relationships:
        if rel["fromColumn"]:
            rel_refs.add(f"{rel['fromTable']}[{rel['fromColumn']}]")
        if rel["toColumn"]:
            rel_refs.add(f"{rel['toTable']}[{rel['toColumn']}]")

    visual_refs_lower = {k.lower() for k in field_usage_ctx}
    all_refs_lower = {r.lower() for r in measure_refs | rel_refs} | visual_refs_lower

    orphan_count = 0
    for t in tables:
        for c in t["columns"]:
            if c.get("type") == "calculatedColumn":
                continue
            key_lower = f"{t['name']}[{c['name']}]".lower()
            if key_lower not in all_refs_lower:
                orphan_count += 1

    if orphan_count == 0:
        return []
    return [{
        "table": "Model",
        "item_name": f"{orphan_count} orphan column(s)",
        "item_type": "Summary",
        "issue": (
            f"{orphan_count} column(s) are not referenced anywhere — "
            "safe to remove, reduces model file size"
        ),
        "recommendation": (
            "Switch to the Column Usage Analyser tab → filter by category 'Orphan' "
            "to see the full list. Verify none are used in row-level security before deleting."
        ),
        "risk": "LOW",
    }]


# Check 4 — Broken relationships
def _check_broken_relationships(tables: list[dict], relationships: list[dict]) -> list[dict]:
    t_names = {t["name"] for t in tables}
    out = []
    for rel in relationships:
        missing = [x for x in (rel["fromTable"], rel["toTable"]) if x and x not in t_names]
        if missing:
            out.append({
                "table": rel["fromTable"] or "Unknown",
                "item_name": f"{rel['fromTable']} → {rel['toTable']}",
                "item_type": "Relationship",
                "issue": "Relationship references a table that no longer exists — will cause errors in visuals",
                "recommendation": f"Delete this relationship or restore missing table(s): {', '.join(missing)}",
                "risk": "HIGH",
            })
    return out


# Check 5 — Many-to-many relationships
def _check_many_to_many(relationships: list[dict]) -> list[dict]:
    out = []
    for rel in relationships:
        if "many" in rel["fromCardinality"] and "many" in rel["toCardinality"]:
            out.append({
                "table": rel["fromTable"],
                "item_name": f"{rel['fromTable']} ↔ {rel['toTable']}",
                "item_type": "Relationship",
                "issue": "Many to many relationship — can produce incorrect totals and ambiguous filter paths",
                "recommendation": "Introduce a bridge table or redesign the schema to use one-to-many relationships",
                "risk": "HIGH",
            })
    return out


# Check 6 — No date table
def _check_no_date_table(tables: list[dict]) -> list[dict]:
    date_kws = {"date", "calendar", "dim_date", "time"}
    if not any(any(kw in t["name"].lower() for kw in date_kws) for t in tables):
        return [{
            "table": "Model", "item_name": "Date Table", "item_type": "Model",
            "issue": "No dedicated date table — time intelligence functions like TOTALYTD and SAMEPERIODLASTYEAR may not work correctly",
            "recommendation": "Create a date table, mark it as Date Table in Power BI, and connect it to fact tables",
            "risk": "MEDIUM",
        }]
    return []


# Check 7 — Duplicate measure names
def _check_duplicate_measures(tables: list[dict]) -> list[dict]:
    name_map: dict[str, list[tuple]] = defaultdict(list)
    for t in tables:
        for m in t["measures"]:
            name_map[m["name"].lower()].append((t["name"], m["name"]))
    out = []
    for occurrences in name_map.values():
        if len(occurrences) > 1:
            tbl_list = ", ".join(f"'{tbl}'" for tbl, _ in occurrences)
            out.append({
                "table": occurrences[0][0],
                "item_name": occurrences[0][1],
                "item_type": "Measure",
                "issue": f"Same measure name in two tables ({tbl_list}) — can cause wrong values if wrong measure is selected",
                "recommendation": f"Rename one duplicate or consolidate all measures into a single measures table",
                "risk": "MEDIUM",
            })
    return out


# Check 7b — Duplicate DAX expressions (copy-paste duplicates)
def _check_duplicate_dax(tables: list[dict]) -> list[dict]:
    """Flag measures that share identical DAX expressions (copy-paste duplicates).
    Normalises whitespace and case before comparison to catch trivial variations."""
    expr_map: dict[str, list[tuple]] = defaultdict(list)
    for t in tables:
        for m in t["measures"]:
            expr = m.get("expression", "")
            if isinstance(expr, list):
                expr = "\n".join(expr)
            # Normalise: collapse whitespace, lowercase
            norm = re.sub(r"\s+", " ", expr.strip().lower())
            if norm:  # skip measures with no expression
                expr_map[norm].append((t["name"], m["name"]))
    out = []
    for norm_expr, occurrences in expr_map.items():
        if len(occurrences) > 1:
            names_str = ", ".join(f"'{tbl}'[{name}]" for tbl, name in occurrences)
            # Use first 60 chars of original expression as a preview
            preview = norm_expr[:60] + "…" if len(norm_expr) > 60 else norm_expr
            out.append({
                "table": occurrences[0][0],
                "item_name": occurrences[0][1],
                "item_type": "Measure",
                "issue": (
                    f"Identical DAX expression shared by {len(occurrences)} measures: {names_str}. "
                    f"Preview: `{preview}`"
                ),
                "recommendation": (
                    "Consolidate into a single base measure and reference it from the others, "
                    "or confirm the duplication is intentional (e.g. table-scoped copies)."
                ),
                "risk": "LOW",
            })
    return out


# Check 8 — Hidden tables used in visuals
def _check_hidden_tables_in_visuals(tables: list[dict], field_usage_ctx: dict) -> list[dict]:
    hidden = {t["name"] for t in tables if t.get("isHidden", False)}
    if not hidden:
        return []
    out = []
    seen: set[str] = set()
    for key in field_usage_ctx:
        bp = key.find("[")
        if bp == -1:
            continue
        t_name = key[:bp]
        if t_name not in hidden or key in seen:
            continue
        seen.add(key)
        c_name = key[bp + 1:key.find("]")] if "]" in key else key
        out.append({
            "table": t_name, "item_name": c_name, "item_type": "Column",
            "issue": "Hidden table fields appearing in visuals — governance risk",
            "recommendation": f"Unhide '{t_name}' or move its fields to a visible table",
            "risk": "MEDIUM",
            "_used_in_key": key,
        })
    return out


# Check 9 — Isolated tables (no relationships)
def _check_isolated_tables(tables: list[dict], relationships: list[dict]) -> list[dict]:
    in_rels: set[str] = set()
    for rel in relationships:
        in_rels.add(rel["fromTable"])
        in_rels.add(rel["toTable"])
    out = []
    for t in tables:
        if t["name"] in in_rels:
            continue
        real_cols = [c for c in t["columns"] if c.get("type") != "calculatedColumn"]
        if len(real_cols) == 0 and t["measures"]:
            continue  # measures-only table
        if len(t["columns"]) == 0 and not t["measures"]:
            continue  # already caught by empty table check
        out.append({
            "table": t["name"], "item_name": t["name"], "item_type": "Table",
            "issue": "Isolated table — data cannot be filtered across other tables correctly",
            "recommendation": f"Connect '{t['name']}' to the rest of the model or remove it if not needed",
            "risk": "MEDIUM",
        })
    return out


# Check 10 — Measures with no description (summary card — Fix 2)
def _check_no_description(tables: list[dict]) -> list[dict]:
    total = 0
    undocumented = 0
    for t in tables:
        for m in t["measures"]:
            total += 1
            if not str(m.get("description", "")).strip():
                undocumented += 1
    if total == 0:
        return []
    # Only flag if fewer than 50 % of measures are documented
    documented = total - undocumented
    if documented / total >= 0.5:
        return []
    return [{
        "table": "Model",
        "item_name": f"{undocumented} of {total} measures have no description",
        "item_type": "Summary",
        "issue": (
            f"{undocumented} of {total} measures have no description — "
            "consider adding documentation"
        ),
        "recommendation": (
            "Select each measure in Power BI Desktop → Properties pane → Description field. "
            "Prioritise measures used in published reports and shared datasets."
        ),
        "risk": "LOW",
    }]


# ── Scoring (Fix 3 — capped deductions, max total deduction = 17) ────────────

def _dq_score(findings: list[dict]) -> tuple:
    """
    Returns (score, grade_label, score_color, score_bg, h_ded, m_ded, lo_ded).
    HIGH  → capped at 10 pts total deduction
    MEDIUM → capped at  5 pts total deduction
    LOW   → capped at  2 pts total deduction
    Max total deduction = 17 pts.
    """
    has_h  = any(f["risk"] == "HIGH"   for f in findings)
    has_m  = any(f["risk"] == "MEDIUM" for f in findings)
    has_lo = any(f["risk"] == "LOW"    for f in findings)
    h_ded  = 10 if has_h  else 0
    m_ded  =  5 if has_m  else 0
    lo_ded =  2 if has_lo else 0
    score  = max(0, 100 - h_ded - m_ded - lo_ded)
    if score >= 90:
        grade, color, bg = "Excellent", "#1b5e20", "#e8f5e9"
    elif score >= 75:
        grade, color, bg = "Good",      "#1565c0", "#e3f2fd"
    elif score >= 60:
        grade, color, bg = "Needs Attention", "#e65100", "#fff3e0"
    else:
        grade, color, bg = "Critical",  "#b71c1c", "#fdecea"
    return score, grade, color, bg, h_ded, m_ded, lo_ded


# ── Render helpers ────────────────────────────────────────────────────────────

def _get_used_in(f: dict, field_usage_ctx: dict) -> list[dict]:
    if prekey := f.get("_used_in_key"):
        return field_usage_ctx.get(prekey, [])
    item_type = f.get("item_type", "")
    t_name = f.get("table", "")
    i_name = f.get("item_name", "")
    if item_type == "Column":
        key = f"{t_name}[{i_name}]"
        if key in field_usage_ctx:
            return field_usage_ctx[key]
        lower = key.lower()
        for k, v in field_usage_ctx.items():
            if k.lower() == lower:
                return v
        return []
    if item_type == "Measure":
        suffix = f"[{i_name}]".lower()
        matches = []
        for k, v in field_usage_ctx.items():
            if k.lower().endswith(suffix):
                matches.extend(v)
        return matches
    if item_type in ("Table", "Model"):
        pref = f"{t_name}[".lower()
        matches = []
        for k, v in field_usage_ctx.items():
            if k.lower().startswith(pref):
                matches.extend(v)
        return matches
    if item_type == "Relationship":
        parts = re.split(r"[→↔]", i_name)
        matches = []
        for part in parts:
            pref = f"{part.strip()}[".lower()
            for k, v in field_usage_ctx.items():
                if k.lower().startswith(pref):
                    matches.extend(v)
        return matches
    return []


def _render_dq_finding(f: dict, field_usage_ctx: dict) -> None:
    import pandas as _pd
    risk = f.get("risk", "LOW")
    cls  = {"HIGH": "risk-high", "MEDIUM": "risk-medium", "LOW": "risk-low"}.get(risk, "risk-low")
    emoji = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}.get(risk, "🟡")
    t_name = f.get("table", "")
    i_name = f.get("item_name", "")
    i_type = f.get("item_type", "")
    hdr_parts = []
    if t_name and t_name != i_name:
        hdr_parts.append(f"<b>{t_name}</b>")
    hdr_parts.append(f"<b>{i_name}</b>")
    if i_type:
        hdr_parts.append(f'<span style="font-size:0.78rem;color:#666;font-weight:400">[{i_type}]</span>')
    hdr = " &nbsp;·&nbsp; ".join(hdr_parts)
    st.markdown(
        f'<div class="{cls}">'
        f'{emoji} {hdr}<br>'
        f'<span style="font-size:0.85rem;margin-top:4px;display:block">{f["issue"]}</span>'
        f'<span style="font-size:0.82rem;color:#555;margin-top:2px;display:block">💡 {f["recommendation"]}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    used_in = _get_used_in(f, field_usage_ctx)
    label = f"  ·  {len(used_in)} visual(s)" if used_in else "  ·  not used"
    with st.expander(f"📍 Used In Report{label}", expanded=False):
        if used_in:
            rows = [{"Page": u["page"], "Visual Title": u["visual_name"],
                     "Visual Type": u["visual_type"], "Role": u["role"]} for u in used_in]
            st.dataframe(_pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.markdown(
                '<div style="background:#fffde7;border-left:4px solid #f9a825;'
                'padding:8px 12px;border-radius:4px">'
                '⚠️ <b>Not used in any visual</b> — review before keeping</div>',
                unsafe_allow_html=True,
            )


def _render_dq_section(title: str, findings: list[dict], field_usage_ctx: dict) -> None:
    st.markdown(f"### {title}  ({len(findings)})")
    if not findings:
        st.success("None found ✅")
    else:
        for f in findings:
            _render_dq_finding(f, field_usage_ctx)


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════  SECTION 2 — COLUMN USAGE ANALYSER  ════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

_CAT_DIRECT     = "Direct Visual Use"       # directly in a visual projection
_CAT_IND_MEAS   = "Indirect via Measure"    # col → measure in visual
_CAT_IND_CHAIN  = "Indirect via Calc Chain" # col → calc col → measure in visual
_CAT_FILTER     = "Filter Pane Only"        # in visual/page/report filter only
_CAT_RLS        = "RLS Rule Only"           # in an RLS row filter only
_CAT_MODEL_ONLY = "Model Only"              # relationship key or measure ref, not in visuals
_CAT_ORPHAN     = "Orphan"                  # passes none of the 5 checks


def _build_filter_usage_context(raw_bytes: bytes) -> dict:
    """
    Parse Report/Layout filters at report, page, and visual scope.
    Returns {"Table[Column]": [{"page": str, "filter_type": str}]}
    """
    ctx: dict[str, list[dict]] = {}

    def _parse_filter_val(val, scope: str, page: str) -> None:
        if not val:
            return
        try:
            data = json.loads(val) if isinstance(val, str) else val
        except Exception:
            return
        items = data if isinstance(data, list) else [data]
        for item in items:
            _walk(item, scope, page)

    def _walk(obj, scope: str, page: str) -> None:
        if isinstance(obj, list):
            for x in obj:
                _walk(x, scope, page)
            return
        if not isinstance(obj, dict):
            return
        if "filter" in obj:
            _walk(obj["filter"], scope, page)
        if "From" in obj:
            sources: dict[str, str] = {
                src.get("Name", ""): src.get("Entity", "")
                for src in obj.get("From", [])
                if src.get("Name") and src.get("Entity")
            }
            _find_cols(obj, sources, scope, page)
        else:
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    _walk(v, scope, page)

    def _find_cols(obj, sources: dict, scope: str, page: str) -> None:
        if isinstance(obj, list):
            for x in obj:
                _find_cols(x, sources, scope, page)
            return
        if not isinstance(obj, dict):
            return
        if "Column" in obj:
            col    = obj["Column"]
            prop   = col.get("Property", "")
            src    = col.get("Expression", {}).get("SourceRef", {}).get("Source", "")
            entity = sources.get(src, "")
            if entity and prop:
                key = f"{entity}[{prop}]"
                ctx.setdefault(key, []).append({"page": page, "filter_type": scope})
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _find_cols(v, sources, scope, page)

    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            try:
                raw = zf.read("Report/Layout")
            except KeyError:
                return ctx
            layout = None
            for enc in _ENCODING_ORDER:
                try:
                    layout = json.loads(raw.decode(enc))
                    break
                except Exception:
                    continue
            if not layout:
                return ctx
            for fk in ("filters", "filterConfig"):
                _parse_filter_val(layout.get(fk), "Report Filter", "Report Level")
            for section in layout.get("sections", []):
                pg = section.get("displayName") or section.get("name", "Unknown Page")
                for fk in ("filters", "filterConfig"):
                    _parse_filter_val(section.get(fk), "Page Filter", pg)
                for container in section.get("visualContainers", []):
                    for fk in ("filters", "filterConfig"):
                        _parse_filter_val(container.get(fk), "Visual Filter", pg)
                    try:
                        cfg = json.loads(container.get("config", "{}"))
                        sv  = cfg.get("singleVisual", {})
                        for fk in ("filters", "filterConfig"):
                            _parse_filter_val(sv.get(fk), "Visual Filter", pg)
                    except Exception:
                        pass
    except zipfile.BadZipFile:
        pass
    return ctx


def _build_rls_context(raw_bytes: bytes) -> dict:
    """
    Returns {"Table[Column]": [{"role_name": str}]}
    Reads roles[].tablePermissions[].filterExpression from DataModelSchema.
    """
    ctx: dict[str, list[dict]] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            names_lower = {n.lower(): n for n in zf.namelist()}
            candidates = [
                real for lower, real in names_lower.items()
                if "datamodelschema" in lower or lower.endswith(".bim")
            ]
            for candidate in candidates:
                try:
                    schema = _try_decode(zf.read(candidate))
                    if not schema:
                        continue
                    model = schema.get("model", schema)
                    for role in model.get("roles", []):
                        role_name = role.get("name", "Unknown Role")
                        for perm in role.get("tablePermissions", []):
                            t_name = perm.get("name", "") or perm.get("table", "")
                            expr   = perm.get("filterExpression", "") or perm.get("condition", "")
                            if isinstance(expr, list):
                                expr = " ".join(str(x) for x in expr)
                            if not expr or not t_name:
                                continue
                            for match in re.finditer(r'\[([^\]]+)\]', str(expr)):
                                col_name = match.group(1).strip()
                                if col_name:
                                    key   = f"{t_name}[{col_name}]"
                                    entry = {"role_name": role_name}
                                    if entry not in ctx.get(key, []):
                                        ctx.setdefault(key, []).append(entry)
                    break
                except Exception:
                    continue
    except zipfile.BadZipFile:
        pass
    return ctx


def _build_column_inventory(
    tables: list[dict],
    relationships: list[dict],
    field_usage_ctx: dict,
    filter_ctx: dict,
    rls_ctx: dict,
) -> list[dict]:
    """
    Build a 5-level usage chain for every column in the model.
    Only columns that pass NONE of the 5 checks are classified as Orphan.
    """
    # ── All measure names ────────────────────────────────────────────────
    all_measures_lower: dict[str, tuple[str, str]] = {}  # name_lower → (tbl, name)
    for t in tables:
        for m in t["measures"]:
            all_measures_lower[m["name"].lower()] = (t["name"], m["name"])

    # Which measures appear directly in visual queryRefs?
    visual_measure_set: set[str] = set()
    visual_measure_usages: dict[str, list] = {}
    for qr, usages in field_usage_ctx.items():
        bp = qr.find("[")
        ep = qr.find("]", bp + 1) if bp != -1 else -1
        if bp != -1 and ep != -1:
            inside = qr[bp + 1:ep].lower()
            if inside in all_measures_lower:
                visual_measure_set.add(inside)
                visual_measure_usages.setdefault(inside, []).extend(usages)

    # col_key_lower → list of (tbl, measure_name) for measures referencing this col
    col_to_measures: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for t in tables:
        for m in t["measures"]:
            for match in _BRACKET_COL_RE.finditer(m["expression"]):
                ck = f"{match.group(1).strip()}[{match.group(2).strip()}]".lower()
                col_to_measures[ck].append((t["name"], m["name"]))

    # col_key_lower → list of (tbl, calc_col_name) for calc cols referencing this col
    col_to_calc_cols: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for t in tables:
        for c in t["columns"]:
            expr = c.get("expression", "")
            if isinstance(expr, list):
                expr = " ".join(expr)
            if not expr.strip():
                continue
            for match in _BRACKET_COL_RE.finditer(expr):
                ck = f"{match.group(1).strip()}[{match.group(2).strip()}]".lower()
                col_to_calc_cols[ck].append((t["name"], c["name"]))

    # calc_col_key_lower → list of (tbl, measure_name) for measures referencing that calc col
    calc_col_to_measures: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for t in tables:
        for m in t["measures"]:
            for match in _BRACKET_COL_RE.finditer(m["expression"]):
                ck = f"{match.group(1).strip()}[{match.group(2).strip()}]".lower()
                calc_col_to_measures[ck].append((t["name"], m["name"]))

    # Relationship keys
    rel_col_keys: set[str] = set()
    for rel in relationships:
        if rel["fromColumn"]:
            rel_col_keys.add(f"{rel['fromTable']}[{rel['fromColumn']}]".lower())
        if rel["toColumn"]:
            rel_col_keys.add(f"{rel['toTable']}[{rel['toColumn']}]".lower())

    # Lowercase lookups
    visual_lower = {k.lower(): v for k, v in field_usage_ctx.items()}
    filter_lower = {k.lower(): v for k, v in filter_ctx.items()}
    rls_lower    = {k.lower(): v for k, v in rls_ctx.items()}

    # ── Build inventory ───────────────────────────────────────────────────
    inventory: list[dict] = []
    for t in tables:
        t_name = t["name"]
        for c in t["columns"]:
            c_name    = c["name"]
            key_lower = f"{t_name}[{c_name}]".lower()
            is_calc   = c.get("type") == "calculatedColumn"
            data_type = str(c.get("dataType", "—")).strip() or "—"

            # Check 1 — direct visual queryRef
            visual_usages = visual_lower.get(key_lower, [])
            check1 = bool(visual_usages)

            # Check 2 — indirect via measure → visual
            ind_measure_details: list[dict] = []
            for (m_tbl, m_name) in col_to_measures.get(key_lower, []):
                if m_name.lower() in visual_measure_set:
                    ind_measure_details.append({
                        "measure": f"{m_tbl}[{m_name}]",
                        "visual_usages": visual_measure_usages.get(m_name.lower(), []),
                    })
            check2 = bool(ind_measure_details)

            # Check 3 — indirect via calc col chain → measure → visual
            chain_details: list[str] = []
            for (cc_tbl, cc_name) in col_to_calc_cols.get(key_lower, []):
                cc_key = f"{cc_tbl}[{cc_name}]".lower()
                for (m_tbl, m_name) in calc_col_to_measures.get(cc_key, []):
                    if m_name.lower() in visual_measure_set:
                        visuals = visual_measure_usages.get(m_name.lower(), [])
                        vis_str = visuals[0]["visual_name"] if visuals else "a visual"
                        chain_details.append(
                            f"{t_name}[{c_name}] → {cc_tbl}[{cc_name}] → [{m_name}] → {vis_str}"
                        )
            check3 = bool(chain_details)

            # Check 4 — filter pane (report/page/visual filters)
            filter_usages = filter_lower.get(key_lower, [])
            check4 = bool(filter_usages)

            # Check 5 — RLS filter expression
            rls_usages = rls_lower.get(key_lower, [])
            check5 = bool(rls_usages)

            # Legacy model refs (for Model Only fallback and verify display)
            ref_measures   = col_to_measures.get(key_lower, [])
            ref_calc_cols  = col_to_calc_cols.get(key_lower, [])
            in_relationships = key_lower in rel_col_keys
            in_model = bool(ref_measures) or bool(ref_calc_cols) or in_relationships

            # Category (priority order)
            if check1:
                category = _CAT_DIRECT
            elif check2:
                category = _CAT_IND_MEAS
            elif check3:
                category = _CAT_IND_CHAIN
            elif check4:
                category = _CAT_FILTER
            elif check5:
                category = _CAT_RLS
            elif in_model:
                category = _CAT_MODEL_ONLY
            else:
                category = _CAT_ORPHAN

            inventory.append({
                "table":        t_name,
                "column":       c_name,
                "data_type":    data_type,
                "is_hidden":    c.get("isHidden", False),
                "is_calculated": is_calc,
                "category":     category,
                # 5 check booleans
                "check1": check1, "check2": check2, "check3": check3,
                "check4": check4, "check5": check5,
                # Usage details
                "visual_usages":       visual_usages,
                "ind_measure_details": ind_measure_details,
                "chain_details":       chain_details,
                "filter_usages":       filter_usages,
                "rls_usages":          rls_usages,
                # Legacy columns (table view + verify)
                "in_visuals":      check1,
                "in_measures":     bool(ref_measures),
                "in_calc_cols":    bool(ref_calc_cols),
                "in_relationships": in_relationships,
                "measure_refs":    [f"{mt}[{mn}]" for mt, mn in ref_measures],
                "calc_refs":       [f"{ct}[{cn}]" for ct, cn in ref_calc_cols],
            })
    return inventory


def _cat_badge(cat: str) -> str:
    css = {
        _CAT_DIRECT:     "cat-direct",
        _CAT_IND_MEAS:   "cat-ind-meas",
        _CAT_IND_CHAIN:  "cat-ind-chain",
        _CAT_FILTER:     "cat-filter",
        _CAT_RLS:        "cat-rls",
        _CAT_MODEL_ONLY: "cat-model",
        _CAT_ORPHAN:     "cat-orphan",
    }.get(cat, "cat-orphan")
    return f'<span class="{css}">{cat}</span>'


def _bool_icon(val: bool) -> str:
    return "✅" if val else "—"


def _render_verify_breakdown(r: dict) -> None:
    """Show the 5-check breakdown for a column — used in card and table Orphan verify."""
    checks = [
        (
            "Check 1 — Direct Visual",
            r["check1"],
            "Column appears directly in a visual projection (queryRef)",
            (f"Found in {len(r['visual_usages'])} visual(s)" if r["check1"]
             else "Not found in any visual queryRef"),
        ),
        (
            "Check 2 — Indirect via Measure",
            r["check2"],
            "Column referenced in a measure that appears in a visual",
            (f"Bridge measure(s): {', '.join(d['measure'] for d in r['ind_measure_details'][:3])}"
             if r["check2"]
             else (f"Referencing measures exist but none reach a visual: {', '.join(r['measure_refs'][:3])}"
                   if r["measure_refs"]
                   else "No measures reference this column")),
        ),
        (
            "Check 3 — Indirect via Calc Chain",
            r["check3"],
            "Column → calculated column → measure → visual chain exists",
            (f"Chain: {r['chain_details'][0]}" if r["check3"]
             else (f"Referenced in calc col(s) {', '.join(r['calc_refs'][:2])} but chain does not reach a visual"
                   if r["calc_refs"]
                   else "No calculated columns reference this column")),
        ),
        (
            "Check 4 — Filter Pane",
            r["check4"],
            "Column appears in a report, page, or visual filter",
            (f"Found in {len(r['filter_usages'])} filter(s)" if r["check4"]
             else "Not found in any filter"),
        ),
        (
            "Check 5 — RLS Rules",
            r["check5"],
            "Column appears in a row-level security filter expression",
            (f"Used in role(s): {', '.join(e['role_name'] for e in r['rls_usages'])}" if r["check5"]
             else "Not found in any RLS filter expression"),
        ),
    ]
    for label, passed, desc, detail in checks:
        bg    = "#e8f5e9" if passed else "#fdecea"
        color = "#1b5e20" if passed else "#c62828"
        icon  = "✅" if passed else "❌"
        st.markdown(
            f'<div style="background:{bg};border-radius:5px;padding:7px 12px;margin-bottom:5px">'
            f'<span style="color:{color};font-weight:700">{icon} {label}</span><br>'
            f'<span style="font-size:0.78rem;color:#555">{desc}</span><br>'
            f'<span style="font-size:0.78rem;color:{color}">{detail}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )


def _render_column_analyser(inventory: list[dict]) -> None:
    import pandas as _pd

    if not inventory:
        st.info("No columns found in the model.")
        return

    all_tables = sorted({row["table"] for row in inventory})
    all_types  = sorted({row["data_type"] for row in inventory})
    all_cats   = [
        _CAT_DIRECT, _CAT_IND_MEAS, _CAT_IND_CHAIN,
        _CAT_FILTER, _CAT_RLS, _CAT_MODEL_ONLY, _CAT_ORPHAN,
    ]

    # ── Summary metrics ───────────────────────────────────────────────────
    cat_counts = defaultdict(int)
    for row in inventory:
        cat_counts[row["category"]] += 1

    r1c1, r1c2, r1c3, r1c4 = st.columns(4)
    r2c1, r2c2, r2c3, _    = st.columns(4)
    r1c1.metric("Direct Visual Use",       cat_counts[_CAT_DIRECT],    help="Column directly in a visual projection")
    r1c2.metric("Indirect via Measure",    cat_counts[_CAT_IND_MEAS],  help="Col → measure → visual")
    r1c3.metric("Indirect via Calc Chain", cat_counts[_CAT_IND_CHAIN], help="Col → calc col → measure → visual")
    r1c4.metric("Filter Pane Only",        cat_counts[_CAT_FILTER],    help="In a filter but not in any projection")
    r2c1.metric("RLS Rule Only",           cat_counts[_CAT_RLS],       help="Used in row-level security only")
    r2c2.metric("Model Only",              cat_counts[_CAT_MODEL_ONLY],help="Relationship key or unreached measure ref")
    r2c3.metric("Orphan",                  cat_counts[_CAT_ORPHAN],    help="Passes none of the 5 usage checks")

    st.markdown("---")

    # ── Filter controls ───────────────────────────────────────────────────
    fc1, fc2, fc3, fc4 = st.columns([2, 2, 2, 1])
    sel_tables  = fc1.multiselect("Filter by table",     all_tables, placeholder="All tables")
    sel_cats    = fc2.multiselect("Filter by category",  all_cats,   placeholder="All categories")
    sel_types   = fc3.multiselect("Filter by data type", all_types,  placeholder="All types")
    show_hidden = fc4.checkbox("Hidden only", value=False)

    filtered = inventory
    if sel_tables:
        filtered = [r for r in filtered if r["table"] in sel_tables]
    if sel_cats:
        filtered = [r for r in filtered if r["category"] in sel_cats]
    if sel_types:
        filtered = [r for r in filtered if r["data_type"] in sel_types]
    if show_hidden:
        filtered = [r for r in filtered if r["is_hidden"]]

    st.markdown(f"**{len(filtered)}** columns shown")
    st.markdown("---")

    if not filtered:
        st.info("No columns match the selected filters.")
        return

    # ── View mode ─────────────────────────────────────────────────────────
    view_mode = st.radio(
        "View mode",
        ["📊 Table view", "🃏 Card view"],
        horizontal=True,
        label_visibility="collapsed",
    )

    _cat_colors = {
        _CAT_DIRECT:     "background-color:#c8e6c9;color:#1b5e20",
        _CAT_IND_MEAS:   "background-color:#bbdefb;color:#0d47a1",
        _CAT_IND_CHAIN:  "background-color:#d1c4e9;color:#4a148c",
        _CAT_FILTER:     "background-color:#b2ebf2;color:#006064",
        _CAT_RLS:        "background-color:#ffe0b2;color:#e65100",
        _CAT_MODEL_ONLY: "background-color:#ede7f6;color:#4a148c",
        _CAT_ORPHAN:     "background-color:#fce4ec;color:#880e4f",
    }
    _cat_bg = {
        _CAT_DIRECT:     "#e8f5e9",   _CAT_IND_MEAS:   "#e3f2fd",
        _CAT_IND_CHAIN:  "#ede7f6",   _CAT_FILTER:     "#e0f7fa",
        _CAT_RLS:        "#fff3e0",   _CAT_MODEL_ONLY: "#f3e5f5",
        _CAT_ORPHAN:     "#fce4ec",
    }
    _cat_border = {
        _CAT_DIRECT:     "#2e7d32",   _CAT_IND_MEAS:   "#1565c0",
        _CAT_IND_CHAIN:  "#6a1b9a",   _CAT_FILTER:     "#006064",
        _CAT_RLS:        "#e65100",   _CAT_MODEL_ONLY: "#7b1fa2",
        _CAT_ORPHAN:     "#ad1457",
    }

    if view_mode == "📊 Table view":
        df_rows = []
        for r in filtered:
            visual_pages = ", ".join(
                sorted({u["page"] for u in r["visual_usages"]})
            ) if r["visual_usages"] else "—"
            df_rows.append({
                "Table":            r["table"],
                "Column":           r["column"],
                "Data Type":        r["data_type"],
                "Hidden":           "Yes" if r["is_hidden"] else "No",
                "Calculated":       "Yes" if r["is_calculated"] else "No",
                "Category":         r["category"],
                "Ch1 Direct":       "✅" if r["check1"] else "—",
                "Ch2 Via Measure":  "✅" if r["check2"] else "—",
                "Ch3 Via Chain":    "✅" if r["check3"] else "—",
                "Ch4 Filter":       "✅" if r["check4"] else "—",
                "Ch5 RLS":          "✅" if r["check5"] else "—",
                "In Relationships": "✅" if r["in_relationships"] else "—",
                "Visual Pages":     visual_pages,
            })
        df = _pd.DataFrame(df_rows)

        def _highlight_category(val: str) -> str:
            return _cat_colors.get(val, "")

        styled = df.style.map(_highlight_category, subset=["Category"])
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # Verify orphan rows below the table
        orphan_rows = [r for r in filtered if r["category"] == _CAT_ORPHAN]
        if orphan_rows:
            st.markdown("---")
            st.markdown(f"#### 🔍 Orphan Column Verification ({len(orphan_rows)} columns)")
            st.caption("Expand each row to see exactly why it was classified as Orphan.")
            for r in orphan_rows:
                with st.expander(f"{r['table']}[{r['column']}]", expanded=False):
                    _render_verify_breakdown(r)

    else:  # Card view
        for r in filtered:
            cat    = r["category"]
            bg     = _cat_bg.get(cat, "#f5f5f5")
            border = _cat_border.get(cat, "#888")
            hidden_tag = " · <i>hidden</i>" if r["is_hidden"] else ""
            calc_tag   = " · <i>calculated</i>" if r["is_calculated"] else ""

            st.markdown(
                f'<div style="background:{bg};border-left:4px solid {border};'
                f'padding:10px 14px;border-radius:6px;margin-bottom:6px">'
                f'<b>{r["table"]}[{r["column"]}]</b>{hidden_tag}{calc_tag} &nbsp;{_cat_badge(cat)}<br>'
                f'<span style="font-size:0.8rem;color:#555">'
                f'Type: <b>{r["data_type"]}</b> &nbsp;|&nbsp; '
                f'Ch1:{_bool_icon(r["check1"])} '
                f'Ch2:{_bool_icon(r["check2"])} '
                f'Ch3:{_bool_icon(r["check3"])} '
                f'Ch4:{_bool_icon(r["check4"])} '
                f'Ch5:{_bool_icon(r["check5"])} '
                f'Rel:{_bool_icon(r["in_relationships"])}'
                f'</span></div>',
                unsafe_allow_html=True,
            )

            # Drill-down per category
            if r["check1"] and r["visual_usages"]:
                with st.expander(f"📊 Direct visual usage ({len(r['visual_usages'])} references)", expanded=False):
                    rows = [{"Page": u["page"], "Visual Title": u["visual_name"],
                             "Visual Type": u["visual_type"], "Role": u["role"]}
                            for u in r["visual_usages"]]
                    st.dataframe(_pd.DataFrame(rows), use_container_width=True, hide_index=True)

            if r["check2"] and r["ind_measure_details"]:
                with st.expander(f"📐 Indirect via {len(r['ind_measure_details'])} measure(s)", expanded=False):
                    for detail in r["ind_measure_details"]:
                        st.markdown(f"**Bridge measure:** `{detail['measure']}`")
                        if detail["visual_usages"]:
                            rows = [{"Page": u["page"], "Visual Title": u["visual_name"],
                                     "Visual Type": u["visual_type"], "Role": u["role"]}
                                    for u in detail["visual_usages"]]
                            st.dataframe(_pd.DataFrame(rows), use_container_width=True, hide_index=True)

            if r["check3"] and r["chain_details"]:
                with st.expander(f"🔗 Calc column chain ({len(r['chain_details'])} path(s))", expanded=False):
                    for chain in r["chain_details"]:
                        st.markdown(f"`{chain}`")

            if r["check4"] and r["filter_usages"]:
                with st.expander(f"🔎 Filter pane ({len(r['filter_usages'])} filter(s))", expanded=False):
                    rows = [{"Page": u["page"], "Filter Type": u["filter_type"]}
                            for u in r["filter_usages"]]
                    st.dataframe(_pd.DataFrame(rows), use_container_width=True, hide_index=True)

            if r["check5"] and r["rls_usages"]:
                with st.expander(f"🔐 RLS rule ({len(r['rls_usages'])} role(s))", expanded=False):
                    for entry in r["rls_usages"]:
                        st.markdown(f"- Role: `{entry['role_name']}`")

            if cat == _CAT_MODEL_ONLY:
                if r["in_relationships"]:
                    st.markdown(
                        '<span style="font-size:0.78rem;color:#555">🔗 Used as relationship key</span>',
                        unsafe_allow_html=True,
                    )
                if r["measure_refs"]:
                    with st.expander(f"📐 Measure references ({len(r['measure_refs'])})", expanded=False):
                        for ref in r["measure_refs"]:
                            st.markdown(f"- `{ref}`")

            if cat == _CAT_ORPHAN:
                with st.expander("🔍 Verify — why is this classified as Orphan?", expanded=False):
                    _render_verify_breakdown(r)

# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════  SECTION 3 — RELATIONSHIP MAP  ═════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

def _get_rel_cardinality(rel: dict) -> tuple[str, str]:
    """Extract from/to cardinality as lower-case strings, trying all known PBI field names."""
    from_val = (
        rel.get("fromCardinality")
        or rel.get("fromCardinalityType")
        or ""
    )
    to_val = (
        rel.get("toCardinality")
        or rel.get("toCardinalityType")
        or ""
    )
    # Older PBI versions store cardinality as integers: 0 = many, 1 = one
    if isinstance(from_val, int):
        from_val = "many" if from_val == 0 else "one"
    if isinstance(to_val, int):
        to_val = "many" if to_val == 0 else "one"
    return str(from_val).lower(), str(to_val).lower()


def _get_rel_cross_filter(rel: dict) -> str:
    """Extract cross-filter direction as lower-case string, trying all known PBI field names."""
    val = (
        rel.get("crossFilteringBehavior")
        or rel.get("crossFilter")
        or rel.get("crossFilterDirection")
        or ""
    )
    return str(val).lower()


def _rel_cardinality_label(rel: dict) -> str:
    """Return a human-readable cardinality string."""
    fc, tc = _get_rel_cardinality(rel)
    if "many" in fc and "many" in tc:
        return "Many : Many"
    if "many" in fc:
        return "Many : One"
    if "many" in tc:
        return "One : Many"
    return "One : One"


def _rel_health(rel: dict) -> str:
    """
    Health tiers:
      Critical — Many:Many + bothDirections  (incorrect totals + perf hit)
      Warning  — Many:Many + oneDirection    (M:M alone is risky)
      Review   — One:Many or Many:One + bothDirections  (may be intentional; verify)
      Healthy  — everything else
    """
    fc, tc = _get_rel_cardinality(rel)
    cf = _get_rel_cross_filter(rel)
    is_m2m   = "many" in fc and "many" in tc
    is_bidir = "both" in cf
    if is_m2m and is_bidir:
        return "Critical"
    if is_m2m:
        return "Warning"
    if is_bidir:
        return "Review"
    return "Healthy"


def _build_table_rel_summary(tables: list[dict], relationships: list[dict]) -> list[dict]:
    """Build one summary row per table for the relationship matrix."""
    t_names = {t["name"] for t in tables}
    # Count connections and flags per table
    conn_map: dict[str, set] = defaultdict(set)   # table → set of connected table names
    rel_count: dict[str, int] = defaultdict(int)
    has_m2m: dict[str, bool] = defaultdict(bool)
    worst_health: dict[str, str] = {}

    for rel in relationships:
        ft, tt = rel["fromTable"], rel["toTable"]
        if ft not in t_names or tt not in t_names:
            continue
        conn_map[ft].add(tt)
        conn_map[tt].add(ft)
        rel_count[ft] += 1
        rel_count[tt] += 1
        fc = rel.get("fromCardinality", "").lower()
        tc = rel.get("toCardinality", "").lower()
        if "many" in fc and "many" in tc:
            has_m2m[ft] = True
            has_m2m[tt] = True
        h = _rel_health(rel)
        for tbl in (ft, tt):
            prev = worst_health.get(tbl, "Healthy")
            order = {"Healthy": 0, "Review": 1, "Warning": 2, "Critical": 3}
            if order.get(h, 0) > order.get(prev, 0):
                worst_health[tbl] = h

    rows = []
    for t in tables:
        name = t["name"]
        conn = len(conn_map.get(name, set()))
        total_r = rel_count.get(name, 0)
        m2m = has_m2m.get(name, False)
        health = worst_health.get(name, "Healthy") if total_r > 0 else "Healthy"
        if total_r == 0:
            role = "Isolated"
        elif total_r >= 5:
            role = "Fact Table"
        else:
            role = "Dimension"
        rows.append({
            "Table Name": name,
            "Connected To": conn,
            "Total Relationships": total_r,
            "Many-to-Many": "⚠️ Yes" if m2m else "✅ No",
            "Health": health,
            "Role": role,
        })
    return rows


def _build_model_overview(tables: list[dict], relationships: list[dict]) -> str:
    """Generate a plain-English paragraph describing the model structure."""
    n_tables = len(tables)
    n_rels   = len(relationships)
    t_names  = {t["name"] for t in tables}

    # Count connections per table
    conn_count: dict[str, int] = defaultdict(int)
    m2m_count = 0
    bidir_count = 0
    for rel in relationships:
        ft, tt = rel["fromTable"], rel["toTable"]
        if ft in t_names:
            conn_count[ft] += 1
        if tt in t_names:
            conn_count[tt] += 1
        fc, tc = _get_rel_cardinality(rel)
        if "many" in fc and "many" in tc:
            m2m_count += 1
        cf = _get_rel_cross_filter(rel)
        if "both" in cf:
            bidir_count += 1

    isolated = [t["name"] for t in tables if conn_count.get(t["name"], 0) == 0
                and not (len([c for c in t["columns"] if c.get("type") != "calculatedColumn"]) == 0 and t["measures"])]
    fact_candidates = sorted(conn_count, key=lambda x: conn_count[x], reverse=True)
    central_table = fact_candidates[0] if fact_candidates else None
    central_conn = conn_count.get(central_table, 0) if central_table else 0

    # Infer schema shape
    if n_rels == 0:
        schema_type = "No relationships defined"
    elif m2m_count == 0 and bidir_count == 0 and len(isolated) == 0:
        schema_type = "Clean Star Schema"
    elif m2m_count == 0 and bidir_count == 0:
        schema_type = "Star Schema with isolated tables"
    elif m2m_count > 0:
        schema_type = "Star Schema with exceptions (many-to-many present)"
    else:
        schema_type = "Star Schema with bidirectional filter exceptions"

    parts = [
        f"This model has **{n_tables} tables** and **{n_rels} relationships**.",
    ]
    if central_table:
        parts.append(
            f"The central table appears to be **{central_table}**, "
            f"which connects to {central_conn} other table(s)."
        )
    if m2m_count:
        parts.append(
            f"There {'is' if m2m_count == 1 else 'are'} "
            f"**{m2m_count} many-to-many relationship(s)** that should be reviewed."
        )
    if bidir_count:
        parts.append(
            f"**{bidir_count} bidirectional filter(s)** detected — these can create ambiguous filter paths."
        )
    if isolated:
        parts.append(
            f"**{len(isolated)} isolated table(s)** have no connections: "
            + ", ".join(f"*{t}*" for t in isolated[:5])
            + (" and others." if len(isolated) > 5 else ".")
        )
    parts.append(f"Overall model structure: **{schema_type}**.")
    return "  \n".join(parts)


def _render_relationship_diagram(selected_table: str, relationships: list[dict], t_names: set) -> None:
    """Render an HTML/CSS visual diagram of the selected table and its neighbours."""
    import streamlit.components.v1 as components

    # Gather connected tables
    connected: list[dict] = []
    for rel in relationships:
        ft, tt = rel["fromTable"], rel["toTable"]
        if ft not in t_names or tt not in t_names:
            continue
        if ft == selected_table:
            connected.append({"neighbour": tt, "rel": rel, "direction": "out"})
        elif tt == selected_table:
            connected.append({"neighbour": ft, "rel": rel, "direction": "in"})

    if not connected:
        st.info(f"'{selected_table}' has no relationships to display.")
        return

    def _line_color(rel: dict) -> str:
        fc = rel.get("fromCardinality", "").lower()
        tc = rel.get("toCardinality", "").lower()
        cf = rel.get("crossFilter", "").lower()
        if "many" in fc and "many" in tc:
            return "#d32f2f"  # red
        if cf in ("bothdirections", "both"):
            return "#e65100"  # orange
        return "#2e7d32"     # green

    def _card_label(rel: dict, direction: str) -> str:
        fc = rel.get("fromCardinality", "").lower()
        tc = rel.get("toCardinality", "").lower()
        from_sym = "*" if "many" in fc else "1"
        to_sym   = "*" if "many" in tc else "1"
        if direction == "out":
            return f"{from_sym} : {to_sym}"
        return f"{to_sym} : {from_sym}"

    # Build HTML neighbour cards
    cards_html = ""
    for item in connected:
        nb  = item["neighbour"]
        rel = item["rel"]
        direction = item["direction"]
        color = _line_color(rel)
        label = _card_label(rel, direction)
        arrow = "→" if direction == "out" else "←"
        cf = rel.get("crossFilter", "").lower()
        if cf in ("bothdirections", "both"):
            arrow = "↔"
        cards_html += f"""
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
          <div style="background:#e3f2fd;border:2px solid #1565c0;border-radius:8px;
                      padding:10px 16px;min-width:140px;text-align:center;font-weight:600;font-size:0.85rem">
            {nb}
          </div>
          <div style="display:flex;flex-direction:column;align-items:center;gap:2px">
            <span style="font-size:0.7rem;color:{color};font-weight:700">{label}</span>
            <span style="font-size:1.2rem;color:{color}">{arrow}</span>
          </div>
        </div>"""

    html = f"""
    <div style="font-family:sans-serif;padding:16px;background:#f9f9f9;border-radius:10px;border:1px solid #ddd">
      <div style="display:flex;align-items:flex-start;gap:32px;flex-wrap:wrap">
        <div style="background:#1565c0;color:#fff;border-radius:10px;padding:14px 20px;
                    font-weight:700;font-size:1rem;min-width:150px;text-align:center;
                    align-self:center;box-shadow:0 2px 8px rgba(21,101,192,0.3)">
          {selected_table}<br>
          <span style="font-size:0.72rem;font-weight:400;opacity:0.85">Selected Table</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:0">
          {cards_html}
        </div>
      </div>
      <div style="margin-top:14px;font-size:0.75rem;color:#888">
        <span style="color:#2e7d32;font-weight:600">&#9644; Green</span> = One-to-many &nbsp;
        <span style="color:#e65100;font-weight:600">&#9644; Orange</span> = Bidirectional &nbsp;
        <span style="color:#d32f2f;font-weight:600">&#9644; Red</span> = Many-to-many
      </div>
    </div>
    """
    components.html(html, height=max(120, len(connected) * 56 + 80), scrolling=False)


def _render_relationship_map(tables: list[dict], relationships: list[dict]) -> None:
    import pandas as _pd
    import json as _json

    t_names = {t["name"] for t in tables}

    # ── DEBUG: raw relationship JSON (first 3) ────────────────────────────
    if relationships:
        with st.expander("🔬 Debug — raw JSON for first 3 relationships (helps diagnose field names)", expanded=False):
            for i, rel in enumerate(relationships[:3]):
                st.markdown(f"**Relationship {i + 1}**")
                st.json(rel)

    # ── Layer 4: Model Overview paragraph ────────────────────────────────
    overview = _build_model_overview(tables, relationships)
    st.markdown(
        f'<div style="background:#e8eaf6;border-left:4px solid #3949ab;'
        f'padding:14px 18px;border-radius:7px;margin-bottom:20px;font-size:0.9rem;line-height:1.7">'
        f'{overview}</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    # ── Layer 1: Summary matrix ───────────────────────────────────────────
    st.markdown("### 📊 Relationship Summary Matrix")

    summary_rows = _build_table_rel_summary(tables, relationships)
    if not summary_rows:
        st.info("No relationships found in the model.")
        return

    role_filter = st.selectbox(
        "Filter by Role",
        ["All", "Fact Tables", "Dimension", "Isolated"],
        index=0,
        label_visibility="visible",
    )
    role_map = {"Fact Tables": "Fact Table", "Dimension": "Dimension", "Isolated": "Isolated"}
    filtered_rows = summary_rows if role_filter == "All" else [
        r for r in summary_rows if r["Role"] == role_map.get(role_filter, "")
    ]

    df = _pd.DataFrame(filtered_rows)

    def _style_health(val: str) -> str:
        return {
            "Critical": "background-color:#fdecea;color:#b71c1c;font-weight:700",
            "Warning":  "background-color:#fff3e0;color:#e65100;font-weight:700",
            "Review":   "background-color:#fff8e1;color:#f57f17;font-weight:700",
            "Healthy":  "background-color:#e8f5e9;color:#1b5e20;font-weight:700",
        }.get(val, "")

    def _style_role(val: str) -> str:
        return {
            "Fact Table": "background-color:#e3f2fd;color:#0d47a1;font-weight:600",
            "Isolated":   "background-color:#fce4ec;color:#880e4f;font-weight:600",
        }.get(val, "")

    styled = (
        df.style
        .map(_style_health, subset=["Health"])
        .map(_style_role,   subset=["Role"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Layer 2: Relationship detail ──────────────────────────────────────
    st.markdown("### 🔍 Relationship Detail")
    all_table_names = sorted(t_names)
    selected = st.selectbox("Select a table to see its relationships", all_table_names)

    if selected:
        detail_rows = []
        for rel in relationships:
            ft, tt = rel["fromTable"], rel["toTable"]
            if ft not in t_names or tt not in t_names:
                continue
            if ft != selected and tt != selected:
                continue

            from_col = rel.get("fromColumn", "—")
            to_col   = rel.get("toColumn",   "—")
            arrow    = "→" if ft == selected else "←"
            direction = f"{ft}[{from_col}]  {arrow}  {tt}[{to_col}]"

            card = _rel_cardinality_label(rel)
            cf   = _get_rel_cross_filter(rel)
            filter_dir = "Both directions" if "both" in cf else "One direction"
            health = _rel_health(rel)
            health_icon = {
                "Critical": "🔴 Critical",
                "Warning":  "🟠 Warning",
                "Review":   "🟡 Review",
                "Healthy":  "✅ Healthy",
            }.get(health, health)

            detail_rows.append({
                "Relationship":     direction,
                "Cardinality":      card,
                "Filter Direction": filter_dir,
                "Health":           health_icon,
            })

        if detail_rows:
            detail_df = _pd.DataFrame(detail_rows)

            def _style_detail_health(val: str) -> str:
                return {
                    "🔴 Critical": "background-color:#fdecea;color:#b71c1c;font-weight:700",
                    "🟠 Warning":  "background-color:#fff3e0;color:#e65100;font-weight:700",
                    "🟡 Review":   "background-color:#fff8e1;color:#f57f17;font-weight:700",
                    "✅ Healthy":  "background-color:#e8f5e9;color:#1b5e20;font-weight:700",
                }.get(val, "")

            styled_detail = detail_df.style.map(_style_detail_health, subset=["Health"])
            st.dataframe(styled_detail, use_container_width=True, hide_index=True)
        else:
            st.info(f"'{selected}' has no relationships.")

        st.markdown("---")

        # ── Layer 3: Visual diagram ───────────────────────────────────────
        st.markdown(f"### 🗺️ Visual Diagram — {selected}")
        _render_relationship_diagram(selected, relationships, t_names)


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════  MAIN  ═════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

raw_bytes: bytes | None = st.session_state.get("pbi_file_bytes")
file_name: str          = st.session_state.get("pbi_file_name", "")

if not raw_bytes:
    st.info("👈 Upload a `.pbix` or `.pbit` file on the **Home** page first, then come back here.")
    st.stop()

st.caption(f"Analysing: **{file_name}**")

with st.spinner("Parsing model…"):
    _all_tables, relationships = _extract_schema(raw_bytes)
    field_usage_ctx            = _build_field_usage_context(raw_bytes)

if not _all_tables:
    st.warning("No tables found. The file may not contain a DataModelSchema.")
    st.stop()

# Fix 1 — exclude system / auto-generated tables from all counts and checks
tables         = [t for t in _all_tables if not _is_system_table(t)]
excluded_count = len(_all_tables) - len(tables)

# Also filter relationships — drop any that reference a system table on either end
_system_names  = {t["name"] for t in _all_tables if _is_system_table(t)}
relationships  = [
    r for r in relationships
    if r["fromTable"] not in _system_names and r["toTable"] not in _system_names
]

# ─────────────────────────────────────────────────────────────────────────────
# Page-level tab navigation
# ─────────────────────────────────────────────────────────────────────────────

tab1, tab2, tab3 = st.tabs(["🏥 Data Quality Audit", "🗺️ Relationship Map", "🔬 Column Usage Analyser"])

# ═══════════════════════  TAB 1 — DATA QUALITY AUDIT  ════════════════════════

with tab1:
    with st.spinner("Running 10 quality checks…"):
        high_f:   list[dict] = []
        medium_f: list[dict] = []
        low_f:    list[dict] = []

        def _bucket(findings: list[dict]) -> None:
            for f in findings:
                r = f.get("risk", "LOW")
                if r == "HIGH":
                    high_f.append(f)
                elif r == "MEDIUM":
                    medium_f.append(f)
                else:
                    low_f.append(f)

        _bucket(_check_empty_tables(tables))
        _bucket(_check_no_datatype(tables))
        _bucket(_check_orphan_columns(tables, relationships, field_usage_ctx))
        _bucket(_check_broken_relationships(tables, relationships))
        _bucket(_check_many_to_many(relationships))
        _bucket(_check_no_date_table(tables))
        _bucket(_check_duplicate_measures(tables))
        _bucket(_check_duplicate_dax(tables))
        _bucket(_check_hidden_tables_in_visuals(tables, field_usage_ctx))
        _bucket(_check_isolated_tables(tables, relationships))
        _bucket(_check_no_description(tables))

    all_findings = high_f + medium_f + low_f

    # Metric boxes
    total_cols = sum(len(t["columns"]) for t in tables)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Tables",        len(tables))
    c1.caption(f"System tables excluded: {excluded_count}")
    c2.metric("Total Columns",       total_cols)
    c3.metric("Total Relationships", len(relationships))
    c4.metric("Total Issues Found",  len(all_findings))
    st.markdown("---")

    # Score (Fix 3 — capped deductions + grade)
    total_score, grade, sc, sb, h_ded, m_ded, lo_ded = _dq_score(all_findings)
    # Persist exact score so the home scorecard reads the real number
    st.session_state["dq_score"] = total_score
    st.markdown(
        f'<div class="score-box" style="background:{sb};border:2px solid {sc}">'
        f'<span style="font-size:2.4rem;font-weight:800;color:{sc}">{total_score}</span>'
        f'<span style="font-size:1.1rem;color:{sc}"> / 100 &nbsp;·&nbsp; {grade}</span><br>'
        f'<span style="font-size:0.85rem;color:#555">'
        f'-{h_ded} HIGH (max 10) &nbsp;·&nbsp;'
        f'-{m_ded} MEDIUM (max 5) &nbsp;·&nbsp;'
        f'-{lo_ded} LOW (max 2)</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown("---")

    st.markdown("## 🔎 Data Quality Findings")
    _render_dq_section("🔴 HIGH RISK", high_f, field_usage_ctx)
    st.markdown("---")
    _render_dq_section("🟠 MEDIUM RISK", medium_f, field_usage_ctx)
    st.markdown("---")
    _render_dq_section("🟡 LOW RISK", low_f, field_usage_ctx)

    # ── CSV Export ────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("## ⬇️ Export Findings")
    if all_findings:
        import pandas as _pd_exp
        _dq_export_rows = [
            {
                "Risk":           f["risk"],
                "Table":          f.get("table", ""),
                "Item":           f.get("item_name", ""),
                "Type":           f.get("item_type", ""),
                "Issue":          f.get("issue", ""),
                "Recommendation": f.get("recommendation", ""),
            }
            for f in all_findings
        ]
        _dq_csv = _pd_exp.DataFrame(_dq_export_rows).to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Download all findings as CSV",
            data=_dq_csv,
            file_name=f"data_quality_findings_{file_name.replace('.', '_')}.csv",
            mime="text/csv",
            help="Download a spreadsheet of all data quality findings for this report.",
        )
    else:
        st.success("No findings to export — your model is clean! ✅")

# ════════════════════  TAB 2 — RELATIONSHIP MAP  ════════════════════════════

with tab2:
    _render_relationship_map(tables, relationships)

# ════════════════════  TAB 3 — COLUMN USAGE ANALYSER  ═══════════════════════

with tab3:
    with st.spinner("Building column inventory (5-level chain check)…"):
        filter_ctx = _build_filter_usage_context(raw_bytes)
        rls_ctx    = _build_rls_context(raw_bytes)
        inventory  = _build_column_inventory(tables, relationships, field_usage_ctx, filter_ctx, rls_ctx)

    st.markdown(
        "Every column is classified using a **5-level usage chain**: "
        "direct visual use, indirect via measure, indirect via calc column chain, "
        "filter pane, and RLS rules. Only columns that pass **none** of these checks are marked Orphan."
    )
    st.markdown("---")
    _render_column_analyser(inventory)
