import streamlit as st
import zipfile
import io
import json
import re
import pandas as pd
from collections import deque, defaultdict

st.set_page_config(
    page_title="Performance Engine — PBI Intelligence Platform",
    page_icon="⚡",
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
        .quick-win   { background:#e8f5e9; border-left:4px solid #2e7d32; padding:14px 16px; border-radius:6px; margin-bottom:14px; }
        .quick-win pre { background:#c8e6c9; border-radius:4px; padding:10px 12px; font-family:monospace; font-size:0.85rem; white-space:pre-wrap; word-break:break-word; margin-top:8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("<h1>PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")

st.title("⚡ Performance Engine")
st.markdown("Scan every DAX measure and calculated column for performance anti-patterns and get an optimisation score.")

# ---------------------------------------------------------------------------
# Helpers — extract measures + relationships from raw bytes
# ---------------------------------------------------------------------------

# All encodings observed in the wild for PBIX/PBIT DataModelSchema files.
# Order matters: most common first to minimise failed attempts.
_ENCODING_ORDER = ("utf-8-sig", "utf-16-le", "utf-16", "utf-8", "utf-16-be", "latin-1")


def _try_decode(raw: bytes) -> dict | None:
    for enc in _ENCODING_ORDER:
        try:
            text = raw.decode(enc)
            # Skip if the decoded text starts with binary-looking content
            # (means we decoded the wrong binary DataModel blob)
            stripped = text.lstrip("\ufeff").lstrip()
            if stripped and stripped[0] not in ('{', '['):
                continue
            return json.loads(text)
        except Exception:
            continue
    return None


def _extract_all(raw_bytes: bytes, filename: str):
    """
    Returns (measures, calc_columns, relationships) where
      measures      = [{"name", "table", "expression"}]
      calc_columns  = [{"name", "table", "expression", "table_measure_count", "is_fact_name"}]
      relationships = [{"fromTable", "toTable", "crossFilter"}]

    Supports three formats:
      Path A — outer DataModelSchema JSON (standard PBIX / all PBIT)
      Path B — enhanced-model PBIX where DataModel is itself a nested ZIP archive
      Path C — truly opaque binary: show actionable guidance
    """
    measures: list[dict] = []
    calc_columns: list[dict] = []
    relationships: list[dict] = []

    def _parse_model(model: dict) -> None:
        """Fill measures/calc_columns/relationships in-place from a model dict."""
        table_measure_counts: dict[str, int] = {}
        for _t in model.get("tables", []):
            table_measure_counts[_t.get("name", "")] = len(_t.get("measures", []))

        for table in model.get("tables", []):
            t_name = table.get("name", "")
            is_fact_name = "fact" in t_name.lower()
            t_mcount = table_measure_counts.get(t_name, 0)

            for m in table.get("measures", []):
                expr = m.get("expression", "")
                if isinstance(expr, list):
                    expr = "\n".join(expr)
                measures.append({
                    "name": m.get("name", ""),
                    "table": t_name,
                    "expression": expr.strip(),
                    "description": m.get("description", ""),
                    "displayFolder": m.get("displayFolder", ""),
                })

            for c in table.get("columns", []):
                expr = c.get("expression", "")
                if isinstance(expr, list):
                    expr = "\n".join(expr)
                expr = expr.strip()
                if not (c.get("type") == "calculatedColumn" or expr):
                    continue
                col_name = c.get("name", "")
                if not col_name or col_name.startswith("RowNumber"):
                    continue
                calc_columns.append({
                    "name": col_name,
                    "table": t_name,
                    "expression": expr,
                    "table_measure_count": t_mcount,
                    "is_fact_name": is_fact_name,
                })

        for rel in model.get("relationships", []):
            relationships.append({
                "fromTable": rel.get("fromTable", "?"),
                "toTable": rel.get("toTable", "?"),
                "crossFilter": rel.get("crossFilteringBehavior", ""),
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
                if measures or calc_columns:
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
            if not measures and not calc_columns:
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

            # Path C: still nothing — show actionable guidance
            if not measures and not calc_columns:
                has_binary = any(
                    k == "datamodel" or k.endswith("/datamodel")
                    for k in names_lower
                )
                if has_binary:
                    st.warning(
                        "⚠️ **Enhanced Model Format — schema could not be extracted.**  \n\n"
                        "This PBIX stores its data model as a compiled binary. "
                        "The nested `DataModel` archive contains no readable schema file.  \n\n"
                        "**To unlock full DAX analysis, do one of:**\n"
                        "- Export the report as **.pbit** from Power BI Desktop "
                        "(File → Export → Power BI Template), then upload the .pbit here.\n"
                        "- Use **pbi-tools extract** on the Lineage Engine page to unpack the schema."
                    )

    except zipfile.BadZipFile:
        st.error(f"❌ **{filename}** is not a valid .pbix / .pbit archive.")

    return measures, calc_columns, relationships


# ---------------------------------------------------------------------------
# Helpers — visual usage context from Report/Layout
# ---------------------------------------------------------------------------

def _normalise_qr(qr: str) -> str:
    """Normalise a queryRef to TableName[FieldName] or bare [FieldName] form."""
    qr = qr.strip()
    # 'Table'[Col] → Table[Col]
    qr = re.sub(r"'([^']+)'\[", r"\1[", qr)
    # Table.Col → Table[Col]
    if "." in qr and "[" not in qr:
        qr = re.sub(r"^([^.]+)\.(.+)$", r"\1[\2]", qr)
    return qr


def _bare_name(field_key: str) -> str:
    """Extract the bare measure/column name from Table[Name] or just return as-is."""
    if "[" in field_key:
        return field_key.split("[", 1)[-1].rstrip("]")
    return field_key


def _extract_fields_from_select(select_items: list, from_map: dict | None = None) -> list[str]:
    """
    Pull field names from prototypeQuery.Select[] items.

    Confirmed real-file structure per item:
      { "Measure": { "Expression": {"SourceRef": {"Source": "alias"}}, "Property": "Name" },
        "Name": "TABLE.FieldName" }

    Extraction order (stops at first hit per item):
      1. NativeReferenceName (legacy format, still present in some files)
      2. Top-level Measure/Column.Property  +  Entity or alias→from_map lookup
      3. Wrapped expression.Measure/Column (older PBIX format, kept for compatibility)
      4. "Name" dot-notation fallback  "TABLE.FieldName" → TABLE[FieldName]
    """
    from_map = from_map or {}
    out: list[str] = []
    for sel in select_items:
        field = ""

        # 1. NativeReferenceName
        native = sel.get("NativeReferenceName", "")
        if native:
            nq = _normalise_qr(native)
            if "[" in nq:
                out.append(nq)
                continue

        # 2. Top-level Measure / Column / HierarchyLevel (confirmed real-file path)
        for kind in ("Measure", "Column", "HierarchyLevel"):
            node = sel.get(kind, {})
            if not isinstance(node, dict) or not node:
                continue
            src_ref = node.get("Expression", {}).get("SourceRef", {})
            entity = src_ref.get("Entity", "")
            if not entity:
                entity = from_map.get(src_ref.get("Source", ""), "")
            prop = node.get("Property", "")
            if prop:
                field = f"{entity}[{prop}]" if entity else f"[{prop}]"
                break

        # 3. Wrapped expression.Measure/Column (older format compatibility)
        if not field:
            expr = sel.get("expression", sel.get("Expression", {}))
            if isinstance(expr, dict):
                for kind in ("Measure", "Column", "HierarchyLevel"):
                    node = expr.get(kind, {})
                    if not isinstance(node, dict) or not node:
                        continue
                    src_ref = node.get("Expression", {}).get("SourceRef", {})
                    entity = src_ref.get("Entity", "")
                    if not entity:
                        entity = from_map.get(src_ref.get("Source", ""), "")
                    prop = node.get("Property", "")
                    if prop:
                        field = f"{entity}[{prop}]" if entity else f"[{prop}]"
                        break

        # 4. "Name" dot-notation fallback  e.g. "FINANCIAL.TotalCollected"
        if not field:
            name_fb = sel.get("Name", "")
            if name_fb:
                nq = _normalise_qr(name_fb)
                if "[" in nq:
                    field = nq

        if field:
            out.append(field)
    return out


def _extract_fields_from_vcobjects(vcobjects: dict) -> list[str]:
    """
    Scan vcObjects (tooltips, conditional formatting, reference lines, etc.)
    for any queryRef / NativeReferenceName / Property values.
    """
    out: list[str] = []
    for _obj_list in vcobjects.values():
        if not isinstance(_obj_list, list):
            continue
        for _obj in _obj_list:
            props = _obj.get("properties", {})
            for _prop_val in props.values():
                if not isinstance(_prop_val, dict):
                    continue
                # expr.Measure.Property / expr.Column.Property
                inner_expr = _prop_val.get("expr", {})
                for kind in ("Measure", "Column", "HierarchyLevel"):
                    node = inner_expr.get(kind, {})
                    if isinstance(node, dict):
                        entity = node.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
                        prop = node.get("Property", "")
                        if prop:
                            out.append(f"{entity}[{prop}]" if entity else f"[{prop}]")
                # direct queryRef string values
                qr = _prop_val.get("queryRef", "")
                if qr:
                    nq = _normalise_qr(qr)
                    if "[" in nq:
                        out.append(nq)
    return out


def _extract_fields_from_filter_config(fc_obj: dict | list) -> list[str]:
    """
    Extract field refs from filterConfig (page-level or visual-level).
    filterConfig contains a list of filter objects; each has an 'expression' tree.
    """
    out: list[str] = []
    items = fc_obj if isinstance(fc_obj, list) else fc_obj.get("filters", [])
    for f in items:
        expr = f.get("expression", {})
        for kind in ("Measure", "Column", "HierarchyLevel"):
            node = expr.get(kind, {})
            if isinstance(node, dict):
                entity = node.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
                prop = node.get("Property", "")
                if prop:
                    out.append(f"{entity}[{prop}]" if entity else f"[{prop}]")
        # Also handle nested Column/Measure inside "Left"/"Right" comparison nodes
        for side in ("Left", "Right"):
            side_node = expr.get(side, {})
            for kind in ("Measure", "Column"):
                node = side_node.get(kind, {})
                if isinstance(node, dict):
                    entity = node.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
                    prop = node.get("Property", "")
                    if prop:
                        out.append(f"{entity}[{prop}]" if entity else f"[{prop}]")
    return out


def _extract_all_fields_from_visual(
    container: dict,
    page_name: str,
) -> tuple[str, str, str, list[dict]]:
    """
    Extract every field reference from a visual container using all known locations.
    Returns (v_type, v_title, page_name, [{Field, Role, Source}]).
    """
    try:
        config = json.loads(container.get("config", "{}"))
    except Exception:
        config = {}

    sv = config.get("singleVisual", {})
    v_type = sv.get("visualType", "unknown")

    # Title
    title_items = sv.get("vcObjects", {}).get("title", [])
    v_title = ""
    if isinstance(title_items, list) and title_items:
        lit = (title_items[0].get("properties", {})
               .get("text", {}).get("expr", {})
               .get("Literal", {}).get("Value", ""))
        v_title = lit.strip("'") if lit else ""
    if not v_title:
        v_title = container.get("name", "")

    seen: set[str] = set()
    rows: list[dict] = []

    def _add(field: str, role: str, source: str) -> None:
        if not field or "[" not in field:
            return
        nf = _normalise_qr(field)
        key = nf + "|" + role
        if key not in seen:
            seen.add(key)
            rows.append({"Field": nf, "Role": role, "Source": source})

    # ── Source 1: projections (most visual types) ────────────────────────
    for role, role_items in sv.get("projections", {}).items():
        if not isinstance(role_items, list):
            continue
        for item in role_items:
            qr = item.get("queryRef", "")
            if qr:
                _add(_normalise_qr(qr), role, "projections")

    # ── Source 2 & 3 & 4: prototypeQuery.Select ──────────────────────────
    # Build alias→Entity map from prototypeQuery.From[] for Source alias resolution
    proto_q = sv.get("prototypeQuery", {})
    _from_items = proto_q.get("From", [])
    _from_map = {
        f.get("Name", ""): f.get("Entity", "")
        for f in _from_items if f.get("Name") and f.get("Entity")
    }
    for f in _extract_fields_from_select(proto_q.get("Select", []), _from_map):
        _add(f, "query", "prototypeQuery.Select")

    # ── Source 5: vcObjects (tooltips, conditional formatting, etc.) ─────
    for f in _extract_fields_from_vcobjects(sv.get("vcObjects", {})):
        _add(f, "vcObject", "vcObjects")

    # ── Source 6: visual-level filterConfig ─────────────────────────────
    for raw_key in ("filterConfig", "filters"):
        raw_str = container.get(raw_key, "")
        if raw_str:
            try:
                fc = json.loads(raw_str) if isinstance(raw_str, str) else raw_str
                for f in _extract_fields_from_filter_config(fc):
                    _add(f, "filter", f"visual.{raw_key}")
            except Exception:
                pass
    # filterConfig may also be inside config JSON
    raw_fc = config.get("filterConfig", "")
    if raw_fc:
        try:
            fc = json.loads(raw_fc) if isinstance(raw_fc, str) else raw_fc
            for f in _extract_fields_from_filter_config(fc):
                _add(f, "filter", "config.filterConfig")
        except Exception:
            pass

    # ── Source 7: drillFilterOtherVisuals ────────────────────────────────
    drill_str = container.get("drillFilterOtherVisuals", "")
    if drill_str:
        try:
            drill = json.loads(drill_str) if isinstance(drill_str, str) else drill_str
            if isinstance(drill, dict):
                for f in _extract_fields_from_select(drill.get("Select", [])):
                    _add(f, "drillthrough", "drillFilterOtherVisuals")
        except Exception:
            pass

    # ── Source 1b: dataTransforms.selects[] ────────────────────────────────
    # Confirmed structure: { "expr": { "Measure": { "Expression": {"SourceRef":
    #   {"Entity": "TABLE"}}, "Property": "Name" } }, "queryRef": "TABLE.Name" }
    dt_str = container.get("dataTransforms", "")
    if dt_str:
        try:
            dt = json.loads(dt_str) if isinstance(dt_str, str) else dt_str
            for sel in dt.get("selects", []):
                field = ""
                # Primary: expr.Measure/Column.Expression.SourceRef.Entity + Property
                expr = sel.get("expr", {})
                for kind in ("Measure", "Column", "HierarchyLevel"):
                    node = expr.get(kind, {})
                    if isinstance(node, dict) and node:
                        entity = node.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
                        prop = node.get("Property", "")
                        if prop:
                            field = f"{entity}[{prop}]" if entity else f"[{prop}]"
                            break
                # Fallback: queryName or queryRef dot notation
                if not field:
                    qn = sel.get("queryName", "") or sel.get("queryRef", "")
                    if qn:
                        nf = _normalise_qr(qn)
                        if "[" in nf:
                            field = nf
                if field:
                    _add(field, "dataTransforms", "dataTransforms.selects")
        except Exception:
            pass

    return v_type, v_title, page_name, rows


def _build_field_usage_context(raw_bytes: bytes) -> dict[str, list[dict]]:
    """
    Parse Report/Layout and return:
      { "TableName[FieldName]": [{"page", "visual_name", "visual_type", "role", "source"}, ...] }

    Extracts from all 7 locations:
      1. dataTransforms.selects[].queryRef
      2. prototypeQuery.Select[].Measure.Property
      3. prototypeQuery.Select[].Column.Property
      4. prototypeQuery.Select[].NativeReferenceName
      5. vcObjects (tooltips, conditional formatting)
      6. filterConfig (visual + page level)
      7. drillFilterOtherVisuals
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

                # Page-level filters
                page_fc_str = section.get("filters", "")
                page_fc_fields: list[str] = []
                if page_fc_str:
                    try:
                        page_fc = json.loads(page_fc_str) if isinstance(page_fc_str, str) else page_fc_str
                        page_fc_fields = _extract_fields_from_filter_config(
                            page_fc if isinstance(page_fc, list) else page_fc
                        )
                    except Exception:
                        pass
                for f in page_fc_fields:
                    nf = _normalise_qr(f)
                    if "[" in nf:
                        ctx.setdefault(nf, []).append({
                            "page": page_name,
                            "visual_name": "(page filter)",
                            "visual_type": "pageFilter",
                            "role": "filter",
                            "source": "page.filters",
                        })

                for container in section.get("visualContainers", []):
                    v_type, v_title, _, rows = _extract_all_fields_from_visual(container, page_name)
                    for row in rows:
                        nf = row["Field"]
                        ctx.setdefault(nf, []).append({
                            "page": page_name,
                            "visual_name": v_title,
                            "visual_type": v_type,
                            "role": row["Role"],
                            "source": row["Source"],
                        })
    except zipfile.BadZipFile:
        pass
    return ctx


def _build_debug_field_map(raw_bytes: bytes) -> list[dict]:
    """
    Returns a list of {Page, Visual, Type, Field, Role, Source} rows
    suitable for displaying in a debug expander.
    """
    rows: list[dict] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            try:
                raw = zf.read("Report/Layout")
            except KeyError:
                return rows
            layout = None
            for enc in _ENCODING_ORDER:
                try:
                    layout = json.loads(raw.decode(enc))
                    break
                except Exception:
                    continue
            if not layout:
                return rows
            for section in layout.get("sections", []):
                page_name = section.get("displayName") or section.get("name", "Unknown Page")
                for container in section.get("visualContainers", []):
                    v_type, v_title, _, fields = _extract_all_fields_from_visual(container, page_name)
                    for row in fields:
                        rows.append({
                            "Page": page_name,
                            "Visual": v_title or f"({v_type})",
                            "Type": v_type,
                            "Field": row["Field"],
                            "Role": row["Role"],
                            "Source": row["Source"],
                        })
    except zipfile.BadZipFile:
        pass
    return rows


# ---------------------------------------------------------------------------
# Unused measure detection helpers (dependency graph + transitive closure)
# ---------------------------------------------------------------------------

# Matches standalone [MeasureName] NOT preceded by a word char, quote, or ] (not a column ref)
_DAX_MREF_RE = re.compile(r"(?<!['\w\]])\[([^\]]+)\]")


def _dax_dep_graph(measures: list[dict]) -> dict[str, set[str]]:
    """
    Build {name_lower: {referenced_name_lower, ...}} restricted to known measure names.
    Measures use keys 'name' and 'expression'.
    """
    all_names = {m["name"].lower() for m in measures}
    graph: dict[str, set[str]] = {}
    for m in measures:
        refs = {r.lower() for r in _DAX_MREF_RE.findall(m.get("expression", ""))
                if r.lower() in all_names}
        graph[m["name"].lower()] = refs
    return graph


def _transitive_used(seeds: set[str], graph: dict[str, set[str]]) -> set[str]:
    """BFS — return all names reachable from seeds."""
    visited: set[str] = set()
    queue: deque[str] = deque(seeds)
    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        for dep in graph.get(node, ()):
            if dep not in visited:
                queue.append(dep)
    return visited


def _compute_unused_measures_perf(
    measures: list[dict],
    field_usage_ctx: dict[str, list[dict]],
    per_report_bare: dict[str, set[str]] | None = None,
) -> list[dict]:
    """
    Returns rows for measures not found in ANY of the uploaded reports.
    Columns: Status, Table, Measure, DAX Expression, Details, Risk to Delete, Used in Reports.

    per_report_bare: {filename: {bare_name_lower, ...}} — one set per uploaded file.
    When provided, a measure is only flagged if its bare name is absent from every set.
    field_usage_ctx should already be the union across all uploaded reports.
    """
    all_names_lower = {m["name"].lower() for m in measures}
    # Table-qualified lookup for accurate matching: TableName[MeasureName] -> name_lower
    _tq_lookup: dict[str, str] = {
        f"{m['table'].lower()}[{m['name'].lower()}]": m["name"].lower()
        for m in measures
    }

    # Step 1 — visually-bound names (union across all reports)
    # Prefer table-qualified key match; fall back to bare name only when ambiguous
    visually_used: set[str] = set()
    if per_report_bare:
        for s in per_report_bare.values():
            visually_used.update(s)
    else:
        for key in field_usage_ctx:
            key_lower = key.lower()
            # Attempt table-qualified match first
            if key_lower in _tq_lookup:
                visually_used.add(_tq_lookup[key_lower])
            else:
                # Fall back to bare name
                bare = _bare_name(key).lower()
                if bare in all_names_lower:
                    visually_used.add(bare)

    # Step 2 — dependency graph
    graph = _dax_dep_graph(measures)

    # Step 3 — transitive closure from all visually-used seeds
    used_set = _transitive_used(visually_used, graph)

    # Step 4 — reverse ref map: which measures reference each measure
    reverse: dict[str, set[str]] = {}
    for m_lower, deps in graph.items():
        for dep in deps:
            if dep in all_names_lower:
                reverse.setdefault(dep, set()).add(m_lower)

    result: list[dict] = []
    for m in measures:
        name_lower = m["name"].lower()
        if name_lower in used_set:
            continue

        # Which uploaded reports use this measure via direct visual binding
        if per_report_bare:
            found_in = [fn for fn, s in per_report_bare.items() if name_lower in s]
        else:
            found_in = []

        if found_in:
            continue  # used in at least one report — not an orphan

        referrers = reverse.get(name_lower, set())
        used_in_label = ", ".join(found_in) if found_in else "Not found in any uploaded report"

        # Details
        if not referrers:
            details = "Not in any visual · Not referenced by any other measure"
        elif all(r not in used_set for r in referrers):
            details = "Not in any visual · Only referenced by other measures not in any uploaded report"
        else:
            details = "Not in any visual"

        # Status
        status = "⚠️ Possibly unused" if not referrers else "🔴 Not used in any uploaded report"

        # Risk to Delete
        has_metadata = bool(m.get("description", "").strip() or m.get("displayFolder", "").strip())
        if has_metadata:
            risk = "LOW"
        elif referrers:
            risk = "MEDIUM"
        else:
            risk = "HIGH"

        result.append({
            "Status": status,
            "Table": m["table"],
            "Measure": m["name"],
            "DAX Expression": m["expression"],
            "Details": details,
            "Risk to Delete": risk,
            "Used in Reports": used_in_label,
        })
    return result


# ---------------------------------------------------------------------------

# Iterators — RANKX excluded: it is an intentional pattern, not a performance anti-pattern
_ITER_FUNCS = re.compile(r"\b(SUMX|AVERAGEX|MAXX|MINX)\b", re.IGNORECASE)
_VAR_KW = re.compile(r"\bVAR\b", re.IGNORECASE)
# FILTER(ALL/VALUES — existing anti-pattern
_FILTER_ALL = re.compile(r"FILTER\s*\(\s*(ALL|VALUES)\s*\(", re.IGNORECASE)
# CALCULATETABLE wrapper — if this wraps a FILTER(ALL) it is already the correct fix
_CALCULATETABLE_RE = re.compile(r"\bCALCULATETABLE\s*\(", re.IGNORECASE)
# FILTER(PlainTable, condition) — iterates every row without columnar engine help
_FILTER_PLAIN_TABLE = re.compile(
    r"FILTER\s*\(\s*(?!ALL\b|VALUES\b|CALCULATETABLE\b)'?[A-Za-z_][A-Za-z0-9_ ]*'?\s*,",
    re.IGNORECASE,
)
# High-cardinality column-name signals for DISTINCTCOUNT
_HIGH_CARD_SIGNALS = re.compile(
    r"\b(ID|Key|Code|Hash|GUID|Guid|Number|Num|Index|Idx|SKU|UUID|Token)\b",
    re.IGNORECASE,
)


def _strip_dax_comments(expr: str) -> str:
    """Remove single-line DAX comments (-- ...) so regex patterns don't
    match tokens inside comments and inflate false-positive counts."""
    return re.sub(r"--[^\r\n]*", "", expr)
# IFERROR and the functions that make an inner expression "complex"
_IFERROR_RE = re.compile(r"\bIFERROR\s*\(", re.IGNORECASE)
_COMPLEX_INNER_RE = re.compile(
    r"\b(CALCULATE|FILTER|SUMX|AVERAGEX|MAXX|MINX|SUMMARIZE|ADDCOLUMNS)\b",
    re.IGNORECASE,
)
# SUMMARIZE / SELECTCOLUMNS used as filter arguments inside CALCULATE
_SUMMARIZE_IN_CALC_RE = re.compile(
    r"\bCALCULATE\b.*\bSUMMARIZE\b", re.IGNORECASE | re.DOTALL
)
_SELECTCOLS_IN_CALC_RE = re.compile(
    r"\bCALCULATE\b.*\bSELECTCOLUMNS\b", re.IGNORECASE | re.DOTALL
)

# ── Stage-2 constants ─────────────────────────────────────────────────────────
_ALLSELECTED_RE = re.compile(r"\bALLSELECTED\s*\(", re.IGNORECASE)
_ALLEXCEPT_RE   = re.compile(r"\bALLEXCEPT\s*\(", re.IGNORECASE)
_CROSSJOIN_RE   = re.compile(r"\bCROSSJOIN\s*\(", re.IGNORECASE)
_CALCULATE_RE   = re.compile(r"\bCALCULATE\s*\(", re.IGNORECASE)
_TIME_INTEL_RE  = re.compile(
    r"\b(TOTALYTD|TOTALQTD|TOTALMTD|SAMEPERIODLASTYEAR|DATEADD"
    r"|PREVIOUSYEAR|PREVIOUSQUARTER|PREVIOUSMONTH"
    r"|DATESYTD|DATESQTD|DATESMTD)\b",
    re.IGNORECASE,
)


# ── Stage-2 helper: DAX nesting depth ────────────────────────────────────────
def _dax_max_depth(expr: str) -> int:
    """Return the maximum parenthesis nesting depth of a DAX expression."""
    max_d = cur_d = 0
    for ch in expr:
        if ch == "(":
            cur_d += 1
            if cur_d > max_d:
                max_d = cur_d
        elif ch == ")":
            cur_d = max(0, cur_d - 1)
    return max_d


# ── Stage-2 helper: fix impact badge ─────────────────────────────────────────
def _impact_label(f: dict) -> str:
    """Return a human-readable fix-impact badge based on finding detail."""
    detail = f.get("detail", "").upper()
    if any(k in detail for k in ("CROSSJOIN", "CIRCULAR", "CARTESIAN")):
        return "🔥 Very High"
    if any(k in detail for k in ("NESTED ITERATOR", "QUADRATIC")):
        return "🔥 Very High"
    if any(k in detail for k in ("FILTER(", "SUMMARIZE", "SELECTCOLUMNS",
                                  "TIME INTELLIGENCE", "SAMEPERIODLASTYEAR",
                                  "TOTALYTD", "MARKED DATE TABLE")):
        return "🔴 High"
    if any(k in detail for k in ("IFERROR", "ALLSELECTED", "DISTINCTCOUNT")):
        return "🟡 Medium"
    return "🟢 Low"


# ── Stage-2 checker: ALLSELECTED misuse ──────────────────────────────────────
def _check_allselected(m: dict) -> dict | None:
    """Flag ALLSELECTED used as a CALCULATE modifier — silently removes filters
    the user expects to apply, causing wrong grand totals."""
    expr = _strip_dax_comments(m["expression"])
    if not (_ALLSELECTED_RE.search(expr) and _CALCULATE_RE.search(expr)):
        return None
    return {
        "measure": m["name"],
        "table": m["table"],
        "expression": m["expression"],
        "risk": "MEDIUM",
        "detail": (
            "ALLSELECTED inside CALCULATE — restores outer slicer context "
            "but silently ignores all inner visual / row filters"
        ),
        "recommendation": (
            "Use ALL() to remove all filters, or remove ALLSELECTED and rely on "
            "natural filter context. ALLSELECTED is only safe as an iterator table "
            "argument (e.g. SUMX(ALLSELECTED(…))), not as a CALCULATE modifier."
        ),
    }


# ── Stage-2 checker: ALLEXCEPT misuse ───────────────────────────────────────
def _check_allexcept(m: dict) -> dict | None:
    """Flag ALLEXCEPT inside CALCULATE.
    ALLEXCEPT(Table, Col1, Col2) removes ALL filters from the table except the
    listed columns — when used as a CALCULATE modifier it silently drops visual
    filters on every column not explicitly listed, causing wrong subtotals."""
    expr = _strip_dax_comments(m["expression"])
    if not (_ALLEXCEPT_RE.search(expr) and _CALCULATE_RE.search(expr)):
        return None
    return {
        "measure": m["name"],
        "table": m["table"],
        "expression": m["expression"],
        "risk": "MEDIUM",
        "detail": (
            "ALLEXCEPT inside CALCULATE — removes ALL table filters except the listed "
            "columns, silently dropping visual/row filters you did not explicitly exclude"
        ),
        "recommendation": (
            "Replace ALLEXCEPT(Table, Col) with individual ALL(Table[Col]) modifiers so "
            "the filter removal is explicit and reviewable. "
            "ALLEXCEPT is safest as an iterator table argument, not a CALCULATE modifier."
        ),
    }


# ── Stage-2 checker: CROSSJOIN ───────────────────────────────────────────────
def _check_crossjoin(m: dict) -> dict | None:
    """Flag CROSSJOIN — Cartesian product multiplies row counts exponentially."""
    expr = _strip_dax_comments(m["expression"])
    if not _CROSSJOIN_RE.search(expr):
        return None
    return {
        "measure": m["name"],
        "table": m["table"],
        "expression": m["expression"],
        "risk": "HIGH",
        "detail": (
            "CROSSJOIN creates a Cartesian product — row count = rows(A) × rows(B), "
            "query time explodes on any table larger than a few hundred rows"
        ),
        "recommendation": (
            "Replace CROSSJOIN with a model relationship, or use GENERATE / GENERATEALL "
            "if row-by-row expansion is intentional. Verify the result row count before deploying."
        ),
    }


# ── Stage-2 checker: Time intelligence without a date table ──────────────────
def _check_time_intelligence(m: dict, date_table_names: set) -> dict | None:
    """Flag time intelligence functions when no dedicated date table is detected."""
    expr = _strip_dax_comments(m["expression"])
    match = _TIME_INTEL_RE.search(expr)
    if not match:
        return None
    if date_table_names:
        return None  # a marked date table exists — likely fine
    fn = match.group(1).upper()
    return {
        "measure": m["name"],
        "table": m["table"],
        "expression": m["expression"],
        "risk": "HIGH",
        "detail": (
            f"Uses {fn} but no dedicated date table detected in this model — "
            "time intelligence requires a table marked as 'Date Table' to return correct results"
        ),
        "recommendation": (
            "Create a date table, mark it as 'Mark as Date Table' in Power BI Desktop, "
            "and connect it to your fact tables via the date key. Without this, "
            f"{fn} will return blank or incorrect totals."
        ),
    }


# ── Stage-2 checker: Circular measure references ─────────────────────────────
def _detect_circular_refs(measures: list[dict]) -> list[dict]:
    """Detect A → B → A circular DAX measure dependencies using iterative DFS."""
    name_set      = {m["name"].lower() for m in measures}
    canonical     = {m["name"].lower(): m["name"]       for m in measures}
    table_of      = {m["name"].lower(): m["table"]      for m in measures}
    expr_of       = {m["name"].lower(): m["expression"] for m in measures}
    deps: dict[str, set[str]] = {}
    for m in measures:
        refs: set[str] = set()
        for match in re.finditer(r"(?<!['\w\]])\[([^\]]+)\]", m["expression"]):
            ref = match.group(1).lower()
            if ref in name_set and ref != m["name"].lower():
                refs.add(ref)
        deps[m["name"].lower()] = refs

    findings: list[dict] = []
    seen_cycles: set = set()
    color: dict[str, int] = {}  # 0=white, 1=grey(in-stack), 2=black(done)

    def dfs(node: str, path: list[str]) -> None:
        color[node] = 1
        path.append(node)
        for nbr in deps.get(node, set()):
            c = color.get(nbr, 0)
            if c == 1:
                try:
                    start = path.index(nbr)
                except ValueError:
                    start = 0
                cycle = path[start:]
                key = frozenset(cycle)
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    chain = " → ".join(canonical.get(n, n) for n in cycle) + f" → {canonical.get(nbr, nbr)}"
                    findings.append({
                        "measure":        canonical.get(node, node),
                        "table":          table_of.get(node, ""),
                        "expression":     expr_of.get(node, ""),
                        "risk":           "HIGH",
                        "detail":         f"Circular reference: {chain}",
                        "recommendation": (
                            "Break the cycle by extracting the shared logic into "
                            "a non-self-referencing base measure."
                        ),
                    })
            elif c == 0:
                dfs(nbr, path)
        path.pop()
        color[node] = 2

    for m in measures:
        n = m["name"].lower()
        if color.get(n, 0) == 0:
            dfs(n, [])

    return findings


# ── Stage-2: Measure complexity tier ─────────────────────────────────────────
def _complexity_record(m: dict) -> dict:
    """Return a complexity record dict for one measure."""
    expr  = _strip_dax_comments(m["expression"])
    depth = _dax_max_depth(expr)
    iters = len(_ITER_FUNCS.findall(expr))
    refs  = expr.count("[")
    raw   = depth * 2 + iters * 5 + refs
    if raw >= 25 or depth >= 10 or iters >= 3:
        tier = "🔴 Slow"
    elif raw >= 10 or depth >= 5 or iters >= 1:
        tier = "🟡 Medium"
    else:
        tier = "🟢 Fast"
    return {
        "Table":     m["table"],
        "Measure":   m["name"],
        "Tier":      tier,
        "Depth":     depth,
        "Iterators": iters,
        "Refs":      refs,
        "Score":     raw,
    }


# ── Stage-2: Power Query M audit ─────────────────────────────────────────────
def _scan_power_query(raw_bytes: bytes) -> list[dict]:
    """Scan Power Query M source for: hardcoded credentials, native SQL queries,
    hardcoded URLs, and missing error handling.

    Handles both storage locations:
      • PBIX — M code is inside a nested Mashup ZIP (DataMashup / Mashup entry)
      • PBIT — M code is in DataModelSchema JSON at
                model.tables[*].partitions[*].source.expression
    """
    findings: list[dict] = []

    # ── Pattern helpers ──────────────────────────────────────────────────────
    def _scan_content(content: str, label: str) -> None:
        """Run all rule checks on one M source string."""
        # Rule 1 — Hardcoded credential / password
        if re.search(r'(?i)(password|pwd|secret|credential)\s*=\s*"[^"]+"', content):
            findings.append({
                "file": label, "risk": "HIGH",
                "issue": "Hardcoded credential / password in Power Query M",
                "recommendation": (
                    "Use parameterised data sources or Windows / OAuth auth — "
                    "never embed secrets in M code (they are visible inside the file ZIP)."
                ),
            })
        # Rule 2 — Value.NativeQuery (raw SQL, bypasses query folding)
        if re.search(r"\bValue\.NativeQuery\b", content, re.IGNORECASE):
            findings.append({
                "file": label, "risk": "MEDIUM",
                "issue": "Value.NativeQuery — sends raw SQL, bypasses Power Query folding",
                "recommendation": (
                    "Verify the query is fully parameterised (prevents SQL injection) "
                    "and confirm no sensitive data is returned unnecessarily."
                ),
            })
        # Rule 3 — Hardcoded Web.Contents URLs
        urls = re.findall(
            r'Web\.Contents\s*\(\s*"(https?://[^"]+)"', content, re.IGNORECASE
        )
        for url in urls[:3]:
            preview = url[:70] + ("…" if len(url) > 70 else "")
            findings.append({
                "file": label, "risk": "LOW",
                "issue": f"Hardcoded URL in Web.Contents: {preview}",
                "recommendation": (
                    "Move the URL to a query parameter so it can be changed "
                    "without editing the report file."
                ),
            })
        # Rule 4 — try/otherwise missing around external sources
        has_web  = bool(re.search(r'\bWeb\.Contents\b',   content, re.IGNORECASE))
        has_sql  = bool(re.search(r'\bSql\.Database\b',   content, re.IGNORECASE))
        has_file = bool(re.search(r'\bFile\.Contents\b',  content, re.IGNORECASE))
        has_err  = bool(re.search(r'\btry\b.*\botherwise\b', content, re.IGNORECASE | re.DOTALL))
        if (has_web or has_sql or has_file) and not has_err:
            findings.append({
                "file": label, "risk": "LOW",
                "issue": "External data source with no try/otherwise error handling",
                "recommendation": (
                    "Wrap external calls in try … otherwise to prevent the entire "
                    "report from failing when a source is temporarily unavailable."
                ),
            })

    found_any_source = False

    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            names_lower = {n.lower(): n for n in zf.namelist()}

            # ── Path A: PBIX Mashup ZIP ──────────────────────────────────────
            mashup_key = next(
                (k for k in names_lower
                 if k in ("datamashup", "mashup") or k.endswith("/datamashup")),
                None,
            )
            if mashup_key:
                mashup_bytes = zf.read(names_lower[mashup_key])
                try:
                    inner = zipfile.ZipFile(io.BytesIO(mashup_bytes))
                    with inner:
                        m_files = [
                            n for n in inner.namelist()
                            if n.lower().endswith((".m", ".pq"))
                            or "section1" in n.lower()
                            or "formula" in n.lower()
                        ]
                        for mf_name in m_files[:8]:
                            try:
                                content = inner.read(mf_name).decode("utf-8", errors="replace")
                                label = mf_name.split("/")[-1]
                                _scan_content(content, label)
                                found_any_source = True
                            except Exception:
                                continue
                except zipfile.BadZipFile:
                    pass

            # ── Path B: PBIT / PBIX fallback — M in DataModelSchema JSON ────
            if not found_any_source:
                schema_key = next(
                    (real for lower, real in names_lower.items()
                     if "datamodelschema" in lower or lower.endswith(".bim")),
                    None,
                )
                if schema_key:
                    schema = _try_decode(zf.read(schema_key))
                    if schema:
                        model = schema.get("model", schema)
                        for tbl in model.get("tables", []):
                            t_name = tbl.get("name", "Unknown")
                            for part in tbl.get("partitions", []):
                                src = part.get("source", {})
                                expr = src.get("expression", "")
                                if isinstance(expr, list):
                                    expr = "\n".join(expr)
                                expr = expr.strip()
                                if not expr:
                                    continue
                                # Only scan M partitions (type=m or no type specified)
                                src_type = src.get("type", "m").lower()
                                if src_type not in ("m", ""):
                                    continue
                                label = f"{t_name} (partition)"
                                _scan_content(expr, label)
                                found_any_source = True

    except Exception:
        pass

    return findings, found_any_source


# ── Stage-2: Cross-report DAX drift ──────────────────────────────────────────
def _detect_drift(
    current_measures: list[dict],
    reports_store: dict,
    current_file: str,
) -> list[dict]:
    """Compare measure DAX expressions across uploaded reports.
    Returns records for measures that share a name but have different DAX."""
    measure_versions: dict[str, list] = defaultdict(list)
    for m in current_measures:
        norm = re.sub(r"\s+", " ", _strip_dax_comments(m["expression"]).strip().lower())
        measure_versions[m["name"].lower()].append(
            (current_file, norm, m["name"], m["table"])
        )
    for fname, rdata in reports_store.items():
        if fname == current_file:
            continue
        raw = rdata.get("raw_bytes", b"")
        if not raw:
            continue
        try:
            other_ms, _, _ = _extract_all(raw, fname)
            for m in other_ms:
                norm = re.sub(r"\s+", " ", _strip_dax_comments(m["expression"]).strip().lower())
                measure_versions[m["name"].lower()].append(
                    (fname, norm, m["name"], m["table"])
                )
        except Exception:
            pass
    drift: list[dict] = []
    for name_lower, versions in measure_versions.items():
        if len(versions) < 2:
            continue
        exprs = {v[1] for v in versions}
        if len(exprs) == 1:
            continue  # all identical — no drift
        drift.append({
            "Measure":  versions[0][2],
            "Table":    versions[0][3],
            "Variants": len(exprs),
            "Reports":  ", ".join(v[0] for v in versions),
            "Issue":    "DAX differs across reports — version drift detected",
        })
    return drift

# Per-function recommendation messages
_ITER_RECOMMENDATIONS: dict[str, str] = {
    "RANKX": "Ensure ALL() is the table argument and the ranked measure is simple.",
    "SUMX": "Replace with CALCULATE(SUM()) to avoid row-by-row iteration.",
    "AVERAGEX": "Replace with DIVIDE(SUM(), COUNTROWS()) for faster aggregation.",
    "MAXX": "Replace with CALCULATE(MAX()) to avoid row-by-row iteration.",
    "MINX": "Replace with CALCULATE(MIN()) to avoid row-by-row iteration.",
}


def _build_iter_rewrite(func: str, name: str, table: str) -> str:
    """Return a suggested rewrite template for a specific iterator function."""
    if func == "RANKX":
        return (
            f"-- [{name}]: each RANKX argument explained\n"
            f"RANKX(\n"
            f"    ALL( {table} ),    -- Table: ALL() removes current filter — ranks across all rows\n"
            f"    [{name}],          -- Expression: the measure to rank (keep it simple)\n"
            f"    ,                  -- Value: blank = reuse the expression above\n"
            f"    DESC,              -- Order: DESC means highest value = Rank 1\n"
            f"    Dense              -- Ties: Dense assigns consecutive ranks (no gaps)\n"
            f")"
        )
    if func == "SUMX":
        return (
            f"-- [{name}]: rewrite SUMX as CALCULATE + SUM\n"
            f"{name} =\n"
            f"CALCULATE(\n"
            f"    SUM( {table}[<YourColumn>] ),\n"
            f"    <filter condition>   -- move the row-level predicate here\n"
            f")"
        )
    if func == "AVERAGEX":
        return (
            f"-- [{name}]: rewrite AVERAGEX as DIVIDE(SUM, COUNTROWS)\n"
            f"{name} =\n"
            f"DIVIDE(\n"
            f"    SUM( {table}[<YourColumn>] ),\n"
            f"    COUNTROWS( {table} )\n"
            f")"
        )
    if func == "MAXX":
        return (
            f"-- [{name}]: rewrite MAXX as CALCULATE(MAX)\n"
            f"{name} =\n"
            f"CALCULATE(\n"
            f"    MAX( {table}[<YourColumn>] ),\n"
            f"    <filter condition>\n"
            f")"
        )
    if func == "MINX":
        return (
            f"-- [{name}]: rewrite MINX as CALCULATE(MIN)\n"
            f"{name} =\n"
            f"CALCULATE(\n"
            f"    MIN( {table}[<YourColumn>] ),\n"
            f"    <filter condition>\n"
            f")"
        )
    return ""


def _check_nested_iterators(m: dict) -> list[dict]:
    """
    Flag only genuinely nested iterator patterns — the proven O(n²) anti-pattern:
      • SUMX/AVERAGEX/MAXX/MINX whose table argument itself contains another X-function
      • Any X-function whose table argument is a FILTER(plain-table, ...) call

    Simple top-level SUMX(Table, expr) is the *intended* DAX pattern — not flagged.
    RANKX is an intentional ranking pattern — not flagged at all.
    Comments are stripped before matching to prevent false positives.
    """
    expr = _strip_dax_comments(m["expression"])
    iter_matches = list(_ITER_FUNCS.finditer(expr))
    if not iter_matches:
        return []

    results: list[dict] = []
    seen_combos: set[str] = set()

    for outer_match in iter_matches:
        outer_func = outer_match.group(0).upper()
        outer_start = outer_match.start()

        # Find the opening paren for this iterator call
        open_pos = expr.find("(", outer_start + len(outer_func))
        if open_pos == -1:
            continue

        # Depth-aware scan to find the matching closing paren
        depth = 0
        close_pos = open_pos
        for i, ch in enumerate(expr[open_pos:], start=open_pos):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close_pos = i
                    break

        inner = expr[open_pos + 1 : close_pos]

        # Check 1: another iterator lives inside this call → nested O(n²)
        inner_iter = _ITER_FUNCS.search(inner)
        if inner_iter:
            inner_func = inner_iter.group(0).upper()
            combo = f"{outer_func}+{inner_func}"
            if combo not in seen_combos:
                seen_combos.add(combo)
                results.append({
                    "measure": m["name"],
                    "table": m["table"],
                    "expression": expr,
                    "risk": "HIGH",
                    "detail": (
                        f"Nested iterator: {outer_func}(…{inner_func}(…)…) — "
                        "quadratic row-by-row evaluation"
                    ),
                    "recommendation": (
                        f"Extract the inner {inner_func} into a VAR or base measure, "
                        f"then reference it inside {outer_func} to avoid O(n²) scanning."
                    ),
                    "rewrite": _build_iter_rewrite(inner_func, m["name"], m["table"]),
                })
            continue

        # Check 2: table argument is FILTER(plain-table, ...) — unbounded scan inside iterator
        if _FILTER_PLAIN_TABLE.search(inner) and not _FILTER_ALL.search(inner):
            combo = f"{outer_func}+FILTER_TABLE"
            if combo not in seen_combos:
                seen_combos.add(combo)
                results.append({
                    "measure": m["name"],
                    "table": m["table"],
                    "expression": expr,
                    "risk": "HIGH",
                    "detail": (
                        f"{outer_func} iterates over FILTER(table, condition) — "
                        "scans the full table row-by-row before aggregating"
                    ),
                    "recommendation": (
                        f"Replace the inner FILTER with CALCULATETABLE and move the "
                        f"{outer_func} aggregation outside, or use CALCULATE(SUM(…), condition)."
                    ),
                    "rewrite": _build_iter_rewrite(outer_func, m["name"], m["table"]),
                })

    return results


def _check_bidir(relationships: list[dict]) -> list[dict]:
    """Flag bidirectional cross-filter relationships.
    Covers all known Power BI crossFilteringBehavior string variants."""
    _BIDIR = {"bothdirections", "both", "twowaymany", "onewayrightfiltersleft"}
    flags = []
    for rel in relationships:
        cf = str(rel.get("crossFilter", "")).lower().replace(" ", "")
        if cf in _BIDIR:
            flags.append({
                "measure": f"{rel['fromTable']} ↔ {rel['toTable']}",
                "table": "Relationship",
                "expression": f"crossFilteringBehavior = {rel['crossFilter']}",
                "risk": "MEDIUM",
                "detail": f"Bidirectional relationship between '{rel['fromTable']}' and '{rel['toTable']}'",
                "recommendation": "Switch to single-direction to avoid ambiguous filter paths and slow queries.",
            })
    return flags


_STAT_FUNCTIONS = ["MEDIAN", "AVERAGE", "AVERAGEA", "PERCENTILE", "SUM",
                   "COUNT", "MAX", "MIN", "DISTINCTCOUNT"]


def _parse_dax_parts(expr: str) -> dict:
    """
    Extract real names from a DAX expression using string methods only.
    Returns dict with keys: stat_function, column_ref, table_name, filters, all_refs.
    Never raises — returns safe defaults on any error.
    """
    _default = {"stat_function": "", "column_ref": "", "table_name": "",
                "filters": [], "all_refs": [],
                # legacy aliases used by _nested_measure_scenario
                "stat_func": "", "table": "", "column": ""}
    try:
        expr_upper = expr.upper()

        # ── Step 1: find statistical function ────────────────────────────
        stat_func = ""
        for fn in _STAT_FUNCTIONS:
            if fn in expr_upper:
                stat_func = fn
                break

        # ── Step 2: find column ref inside the stat function call ─────────
        column_ref = ""
        if stat_func:
            fn_pos = expr_upper.find(stat_func)
            open_pos = expr.find("(", fn_pos)
            if open_pos != -1:
                # find the matching close paren, depth-aware
                depth = 0
                inner_start = open_pos + 1
                inner_end = inner_start
                for i, ch in enumerate(expr[open_pos:], start=open_pos):
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            inner_end = i
                            break
                inner = expr[inner_start:inner_end].strip()
                # inner is e.g. "ClaimMaster[1-PlxToDmnd]" or "'Table'[Col]"
                bracket_open = inner.find("[")
                bracket_close = inner.find("]")
                if bracket_open != -1 and bracket_close != -1:
                    column_ref = inner[bracket_open + 1:bracket_close].strip()

        # ── Step 3: collect filter conditions from CALCULATE args ─────────
        filters: list[str] = []
        calc_upper_pos = expr_upper.find("CALCULATE(")
        if calc_upper_pos == -1:
            calc_upper_pos = expr_upper.find("CALCULATE (")
        if calc_upper_pos != -1:
            paren_start = expr.find("(", calc_upper_pos)
            if paren_start != -1:
                # depth-aware split of CALCULATE arguments
                depth = 0
                args: list[str] = []
                current: list[str] = []
                for ch in expr[paren_start + 1:]:
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        if depth == 0:
                            break
                        depth -= 1
                    if ch == "," and depth == 0:
                        args.append("".join(current).strip())
                        current = []
                    else:
                        current.append(ch)
                if current:
                    args.append("".join(current).strip())
                # args[0] is the aggregation; args[1:] are filters
                for arg in args[1:]:
                    if arg and ("=" in arg or ">" in arg or "<" in arg or "IN" in arg.upper()):
                        filters.append(arg)

        # ── Step 4: find table name from single-quoted text or Table[Col] ─
        table_name = ""
        # look for 'TableName'
        sq_start = expr.find("'")
        if sq_start != -1:
            sq_end = expr.find("'", sq_start + 1)
            if sq_end != -1:
                table_name = expr[sq_start + 1:sq_end].strip()
        # fallback: text before the first [ that isn't a space
        if not table_name:
            first_bracket = expr.find("[")
            if first_bracket > 0:
                candidate = expr[:first_bracket].strip()
                # strip function names and parens
                for ch in "(),'\"":
                    candidate = candidate.replace(ch, "")
                # take the last word
                words = candidate.split()
                if words:
                    last = words[-1].strip()
                    # skip if it looks like a DAX function
                    if last.upper() not in ("CALCULATE", "CALCULATETABLE", "FILTER",
                                            "ALL", "VALUES", "SUMX", "AVERAGEX",
                                            "RANKX", "MAXX", "MINX", "IF", "SWITCH",
                                            "RETURN", "VAR"):
                        table_name = last

        # ── Step 5: collect all [BracketRef] tokens ───────────────────────
        all_refs: list[str] = []
        pos = 0
        while True:
            ob = expr.find("[", pos)
            if ob == -1:
                break
            cb = expr.find("]", ob)
            if cb == -1:
                break
            all_refs.append("[" + expr[ob + 1:cb] + "]")
            pos = cb + 1

        result = {
            "stat_function": stat_func,
            "column_ref": column_ref,
            "table_name": table_name,
            "filters": filters,
            "all_refs": all_refs,
            # legacy aliases
            "stat_func": stat_func,
            "table": table_name,
            "column": column_ref,
        }
        return result

    except Exception:
        return _default


def _nested_measure_scenario(expr: str, measure_name: str = "") -> tuple:
    """
    Returns (scenario_label, recommendation, clean_rewrite_dax, rewrite_explanation).
    clean_rewrite_dax has zero inline comments — ready to copy-paste.
    """
    p = _parse_dax_parts(expr)
    bracket_refs = p["all_refs"]
    count = len(bracket_refs)
    tbl = p["table"] or "<Table>"
    col = p["column"] or "<Column>"
    mname = measure_name or "<Measure>"

    expr_upper = expr.upper()

    # Scenario D — IF/SWITCH with measure calls
    has_conditional = ("IF(" in expr_upper or "IF (" in expr_upper
                       or "SWITCH(" in expr_upper or "SWITCH (" in expr_upper)
    if has_conditional and count > 1:
        b1 = bracket_refs[0] if len(bracket_refs) > 0 else "[BranchA]"
        b2 = bracket_refs[1] if len(bracket_refs) > 1 else "[BranchB]"
        clean_dax = (
            f"VAR _BranchA =\n"
            f"    {b1}\n"
            f"VAR _BranchB =\n"
            f"    {b2}\n"
            f"RETURN\n"
            f"    IF( <condition>, _BranchA, _BranchB )"
        )
        explanation = (
            f"Each conditional branch of [{mname}] extracted into a named VAR. "
            f"Replace <condition> with the original IF condition. "
            f"Branch names taken directly from the original DAX."
        )
        return (
            "Scenario D — Conditional logic calling other measures",
            "Extract each conditional branch into a named VAR for clarity.",
            clean_dax, explanation,
        )

    # Scenario C — more than 5 measure references
    if count > 5:
        var_lines = "".join(
            f"VAR _Ref{i+1} = {ref}\n"
            for i, ref in enumerate(bracket_refs[:6])
        )
        clean_dax = var_lines + "RETURN\n    _Ref1"
        explanation = (
            f"[{mname}] references {count} measures. Each captured as a VAR in dependency order. "
            f"Replace _Ref1 in RETURN with your final aggregation. "
            f"Draw a dependency diagram to confirm the correct order before applying."
        )
        return (
            f"Scenario C — Deep dependency chain ({count} references)",
            "Map the dependency chain and check for circular references before deploying.",
            clean_dax, explanation,
        )

    # Scenario B — statistical functions
    if p["stat_func"]:
        func = p["stat_func"]
        filters = p["filters"]
        if filters:
            filter_lines = ",\n        ".join(filters)
            clean_dax = (
                f"VAR _FilteredContext =\n"
                f"    CALCULATE(\n"
                f"        {func}( {tbl}[{col}] ),\n"
                f"        {filter_lines}\n"
                f"    )\n"
                f"RETURN\n"
                f"    _FilteredContext"
            )
            explanation = (
                f"Rewritten using VAR to separate filter context. "
                f"{func} applied to {tbl}[{col}]. "
                f"Original filters preserved: {', '.join(filters)}. "
                f"Ready to paste into Power BI Desktop measure editor."
            )
        else:
            clean_dax = (
                f"VAR _FilteredContext =\n"
                f"    CALCULATE(\n"
                f"        {func}( {tbl}[{col}] )\n"
                f"    )\n"
                f"RETURN\n"
                f"    _FilteredContext"
            )
            explanation = (
                f"Rewritten using VAR to isolate the {func} calculation on {tbl}[{col}]. "
                f"Ready to paste into Power BI Desktop measure editor."
            )
        return (
            f"Scenario B — Statistical function ({func}) with nested references",
            "Valid pattern; ensure filter arguments don't conflict with row context in a matrix.",
            clean_dax, explanation,
        )

    # Scenario A — CALCULATE with filter conditions
    has_calculate = "CALCULATE(" in expr_upper or "CALCULATE (" in expr_upper
    if has_calculate:
        filters = p["filters"]
        filter_str = ("\n    " + ",\n    ".join(filters)) if filters else ""
        clean_dax = (
            f"[{mname} Base] =\n"
            f"    SUM( {tbl}[{col}] )\n"
            f"\n"
            f"[{mname}] =\n"
            f"CALCULATE(\n"
            f"    [{mname} Base]{filter_str}\n"
            f")"
        )
        filter_desc = f" Filters applied: {', '.join(filters)}." if filters else ""
        explanation = (
            f"[{mname}] split into a reusable base measure on {tbl}[{col}] and a "
            f"CALCULATE wrapper that applies the original filter conditions."
            f"{filter_desc} "
            f"Define [{mname} Base] first, then update [{mname}] in Power BI Desktop."
        )
        return (
            "Scenario A — CALCULATE with filter conditions",
            "Consider extracting repeated filter conditions into a reusable base measure.",
            clean_dax, explanation,
        )

    # Generic fallback
    var_lines = "".join(
        f"VAR _Ref{i+1} = {ref}\n"
        for i, ref in enumerate(bracket_refs[:4])
    )
    clean_dax = var_lines + "RETURN\n    _Ref1"
    explanation = (
        f"[{mname}] has {count} bracket reference(s) captured as VARs. "
        f"Replace _Ref1 in RETURN with your final calculation logic."
    )
    return (
        f"Complex measure ({count} references)",
        "Add VAR blocks for each referenced measure to improve readability.",
        clean_dax, explanation,
    )


def _check_filter_all(m: dict) -> dict | None:
    """Flag FILTER(ALL/VALUES) and FILTER(plain-table, condition) anti-patterns.
    Skips expressions where the FILTER is already inside a CALCULATETABLE wrapper
    (that is the correct rewrite — flagging it would be a false positive).
    Comments are stripped before matching.
    """
    expr = _strip_dax_comments(m["expression"])
    # If the whole expression is wrapped in CALCULATETABLE the pattern is correct usage
    if _CALCULATETABLE_RE.search(expr):
        return None
    if _FILTER_ALL.search(expr):
        return {
            "measure": m["name"],
            "table": m["table"],
            "expression": m["expression"],
            "risk": "HIGH",
            "detail": "Contains FILTER(ALL(…)) or FILTER(VALUES(…)) — materialises a full table in memory",
            "recommendation": "Replace FILTER(ALL(…), condition) with CALCULATETABLE(ALL(…), condition) to use the storage engine's columnar aggregation.",
        }
    if _FILTER_PLAIN_TABLE.search(expr):
        return {
            "measure": m["name"],
            "table": m["table"],
            "expression": m["expression"],
            "risk": "HIGH",
            "detail": "Contains FILTER(table, condition) — scans every row of the table at query time",
            "recommendation": "Replace FILTER(tableName, condition) with CALCULATETABLE(tableName, condition) to let the storage engine filter columnar data directly.",
        }
    return None


_DISTINCTCOUNT_RE = re.compile(r'\bDISTINCTCOUNT\b(?:\s*\([^)]*\))?', re.IGNORECASE)
_DISTINCTCOUNT_COL_RE = re.compile(r'\bDISTINCTCOUNT\s*\(\s*[^[]*\[([^\]]+)\]', re.IGNORECASE)


def _check_distinctcount(m: dict) -> dict | None:
    """Flag DISTINCTCOUNT only when the column name contains high-cardinality signals
    (ID, Key, Code, Hash, GUID, etc.) — prevents false-positives on low-cardinality
    columns like Year, Status, Category."""
    expr = _strip_dax_comments(m["expression"])
    if not re.search(r'\bDISTINCTCOUNT\b', expr, re.IGNORECASE):
        return None
    # Extract the column name from DISTINCTCOUNT(Table[Column]) and check its name
    col_match = _DISTINCTCOUNT_COL_RE.search(expr)
    col_name = col_match.group(1) if col_match else ""
    if col_name and not _HIGH_CARD_SIGNALS.search(col_name):
        # Column name has no high-cardinality signals — skip to avoid false positive
        return None
    detail = (
        f"DISTINCTCOUNT({col_name}) — high-cardinality column detected"
        if col_name else
        "Uses DISTINCTCOUNT — expensive on high-cardinality columns"
    )
    return {
        "measure": m["name"],
        "table": m["table"],
        "expression": m["expression"],
        "risk": "HIGH",
        "detail": detail,
        "recommendation": (
            "Verify cardinality in Power BI Desktop (column profiling). "
            "If high-cardinality, pre-aggregate in Power Query or use an integer surrogate key with COUNT."
        ),
    }


def _check_iferror_complex(m: dict) -> dict | None:
    """Flag IFERROR wrapping a complex inner expression.
    IFERROR forces full evaluation of the inner expression before the error handler
    runs — it prevents the engine from short-circuiting on simple cases."""
    expr = _strip_dax_comments(m["expression"])
    iferror_match = _IFERROR_RE.search(expr)
    if not iferror_match:
        return None
    # Find the argument span of the IFERROR call
    open_pos = expr.find("(", iferror_match.start())
    if open_pos == -1:
        return None
    depth = 0
    close_pos = open_pos
    for i, ch in enumerate(expr[open_pos:], start=open_pos):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close_pos = i
                break
    inner = expr[open_pos + 1 : close_pos]
    if _COMPLEX_INNER_RE.search(inner):
        return {
            "measure": m["name"],
            "table": m["table"],
            "expression": expr,
            "risk": "HIGH",
            "detail": "IFERROR wraps a complex expression — forces full evaluation and masks upstream errors",
            "recommendation": (
                "Use DIVIDE(numerator, denominator, 0) for division errors, "
                "or IF(ISERROR(simpleExpr), fallback, simpleExpr) to expose the root cause. "
                "IFERROR prevents the engine from short-circuiting."
            ),
        }
    return None


def _check_summarize_filter(m: dict) -> dict | None:
    """Flag SUMMARIZE or SELECTCOLUMNS used as a filter argument inside CALCULATE.
    Both force the engine to materialise an in-memory virtual table."""
    expr = _strip_dax_comments(m["expression"])
    if _SUMMARIZE_IN_CALC_RE.search(expr):
        return {
            "measure": m["name"],
            "table": m["table"],
            "expression": expr,
            "risk": "HIGH",
            "detail": "SUMMARIZE used inside CALCULATE — forces materialisation of a virtual table as a filter",
            "recommendation": (
                "Replace CALCULATE(expr, SUMMARIZE(…)) with CALCULATETABLE or a "
                "pre-built dimension table. SUMMARIZE as a CALCULATE filter argument "
                "triggers an in-memory virtualisation step."
            ),
        }
    if _SELECTCOLS_IN_CALC_RE.search(expr):
        return {
            "measure": m["name"],
            "table": m["table"],
            "expression": expr,
            "risk": "HIGH",
            "detail": "SELECTCOLUMNS used inside CALCULATE — forces materialisation of a virtual table as a filter",
            "recommendation": (
                "Replace CALCULATE(expr, SELECTCOLUMNS(…)) with CALCULATETABLE or a "
                "simple column filter. SELECTCOLUMNS as a filter argument forces materialisation."
            ),
        }
    return None


# ---------------------------------------------------------------------------
# Calculated column checkers
# ---------------------------------------------------------------------------

_RELATED_KW  = re.compile(r"\bRELATED(TABLE)?\b", re.IGNORECASE)
_IF_KW       = re.compile(r"\bIF\b", re.IGNORECASE)

_SLICER_TYPES = {"slicer", "basicfilter", "advancedfilter"}
_MATRIX_TYPES = {"matrix", "tableex", "pivottable"}
_CARD_TYPES   = {"card", "kpi", "multirowcard"}


def _cc_check_iterators(c: dict) -> list[dict]:
    """Return one finding per unique iterator function found in a calculated column."""
    results: list[dict] = []
    seen: set[str] = set()
    for match in _ITER_FUNCS.finditer(c["expression"]):
        func = match.group(0).upper()
        if func in seen:
            continue
        seen.add(func)
        results.append({
            "measure": c["name"],
            "table": c["table"],
            "expression": c["expression"],
            "risk": "HIGH",
            "detail": f"Iterator function in calculated column: {func}",
            "recommendation": "Move iterator logic to a measure — calculated columns evaluate row by row at refresh.",
            "rewrite": _build_iter_rewrite(func, c["name"], c["table"]),
        })
    return results


def _cc_check_related(c: dict) -> dict | None:
    if _RELATED_KW.search(c["expression"]):
        return {
            "measure": c["name"],
            "table": c["table"],
            "expression": c["expression"],
            "risk": "MEDIUM",
            "detail": "Uses RELATED() or RELATEDTABLE() in a calculated column",
            "recommendation": "RELATED() in a calculated column slows refresh on large fact tables.",
        }
    return None


def _cc_check_fact_table(c: dict, usage_ctx: dict) -> dict | None:
    if not (c["is_fact_name"] or c["table_measure_count"] > 10):
        return None

    field_key = f"{c['table']}[{c['name']}]"
    usages = usage_ctx.get(field_key, [])
    visual_types = {u["visual_type"].lower() for u in usages}

    base = {
        "measure": c["name"],
        "table": c["table"],
        "expression": c["expression"],
        "used_in": usages,
    }

    # Not used in any visual — low priority
    if not usages:
        return {**base, "risk": "LOW",
                "detail": "Calculated column on likely fact table — not found in any visual",
                "recommendation": "Not used in any visual — candidate for removal"}

    # Used only in slicers / filters — acceptable
    if visual_types.issubset(_SLICER_TYPES):
        return {**base, "risk": "LOW",
                "detail": f"Used only in slicer/filter visual(s): {', '.join(visual_types)}",
                "recommendation": "Used only as a slicer or filter — acceptable use of calculated column"}

    # Used in card / KPI
    card_hits = visual_types & _CARD_TYPES
    if card_hits and not (visual_types & _MATRIX_TYPES):
        return {**base, "risk": "MEDIUM",
                "detail": f"Used in card/KPI visual(s): {', '.join(card_hits)}",
                "recommendation": "Used in card visual — consider rewriting as measure"}

    # Used in matrix/table with many measures on fact table
    if visual_types & _MATRIX_TYPES and c["table_measure_count"] > 3:
        return {**base, "risk": "HIGH",
                "detail": f"Used in matrix/table visual; table has {c['table_measure_count']} measures",
                "recommendation": "Used in matrix with multiple measures — move to measure for better performance"}

    # Generic fact-table flag
    reason = []
    if c["is_fact_name"]:
        reason.append(f"table name '{c['table']}' contains 'fact'")
    if c["table_measure_count"] > 10:
        reason.append(f"table has {c['table_measure_count']} measures (likely a fact table)")
    return {**base, "risk": "HIGH",
            "detail": "Calculated column on likely fact table: " + "; ".join(reason),
            "recommendation": "Calculated column on fact table increases model size and slows refresh — use a measure instead"}


def _cc_check_nested_if(c: dict) -> dict | None:
    count = len(_IF_KW.findall(c["expression"]))
    if count > 2:
        return {
            "measure": c["name"],
            "table": c["table"],
            "expression": c["expression"],
            "risk": "MEDIUM",
            "detail": f"IF keyword appears {count} times — likely nested IFs",
            "recommendation": "Replace nested IFs with SWITCH(TRUE, ...) for readability.",
        }
    return None


def _cc_check_missing_var(c: dict) -> dict | None:
    expr = c["expression"]
    if len(expr) > 200 and not _VAR_KW.search(expr):
        return {
            "measure": c["name"],
            "table": c["table"],
            "expression": expr,
            "risk": "LOW",
            "detail": f"Expression is {len(expr)} characters long and contains no VAR",
            "recommendation": "Add VAR to avoid repeated sub-expression evaluation.",
        }
    return None


# ---------------------------------------------------------------------------
# Quick-win rewrite suggestions
# ---------------------------------------------------------------------------

_FILTER_ALL_RE   = re.compile(r"FILTER\s*\(\s*ALL\s*\(", re.IGNORECASE)
_FILTER_VALS_RE  = re.compile(r"FILTER\s*\(\s*VALUES\s*\(", re.IGNORECASE)


def _suggest_rewrite(finding: dict) -> str | None:
    """
    Return a suggested rewritten DAX string for auto-fixable HIGH RISK findings,
    or None if no automatic rewrite is possible.
    """
    expr   = finding["expression"]
    detail = finding["detail"]
    name   = finding["measure"]
    table  = finding["table"]

    # ── FILTER(ALL/VALUES) → CALCULATETABLE ────────────────────────────────
    # FILTER(ALL(X), cond) and CALCULATETABLE(ALL(X), cond) share identical
    # parenthesis structure, so a direct prefix replacement is safe.
    if _FILTER_ALL_RE.search(expr) or _FILTER_VALS_RE.search(expr):
        rewritten = _FILTER_ALL_RE.sub("CALCULATETABLE(ALL(", expr)
        rewritten = _FILTER_VALS_RE.sub("CALCULATETABLE(VALUES(", rewritten)
        return rewritten

    # ── Iterator function in a measure → CALCULATE(SUM) template ──────────
    if "Iterator function(s):" in detail and table != "Relationship":
        funcs = detail.split(": ", 1)[-1]
        return (
            f"-- [{name}] uses {funcs} — suggested set-based pattern:\n"
            f"{name} =\n"
            f"CALCULATE(\n"
            f"    SUM( {table}[<YourColumn>] ),\n"
            f"    <filter condition>  -- replace the row-level predicate here\n"
            f")"
        )

    # ── Iterator function in a calculated column → convert to measure ──────
    if "Iterator function" in detail and "calculated column" in detail:
        return (
            f"-- Move [{name}] out of the table and define as a measure:\n"
            f"{name} :=\n"
            f"CALCULATE(\n"
            f"    SUM( {table}[<YourColumn>] ),\n"
            f"    <filter>\n"
            f")"
        )

    # ── Calculated column on fact table → convert to measure template ───────
    if "fact table" in detail.lower():
        return (
            f"-- Remove the calculated column and define as a measure instead:\n"
            f"{name} :=\n"
            f"-- Paste your expression below and adjust context filters:\n"
            f"{expr}"
        )

    return None


# ---------------------------------------------------------------------------
# Impact assessment
# ---------------------------------------------------------------------------

def _get_impact(f: dict) -> str:
    """Return a one-line impact summary for a finding."""
    detail = f.get("detail", "").lower()
    risk = f.get("risk", "LOW")
    table = f.get("table", "?")

    if "uses iterator function:" in detail:
        func = f["detail"].split(": ", 1)[-1].upper()
        return f"High cost operation · {func} re-scans the table on every filter context change"
    if "iterator function in calculated column:" in detail:
        func = f["detail"].split(": ", 1)[-1].upper()
        return f"High refresh cost · {func} iterates all rows at model refresh time"
    if "filter(all" in detail or "filter(values" in detail:
        return "High memory cost · Materialises a full filtered table into the storage engine"
    if "bidirectional" in detail:
        tables = f.get("measure", "").replace(" \u2194 ", " and ")
        return f"Medium query cost · Filters propagate in both directions across {tables}"
    if "dependency chain" in detail or "bracket reference" in detail:
        ref_count = next((int(w) for w in detail.split() if w.isdigit()), 0)
        return f"Medium query cost · {ref_count or 'Multiple'} chained measure calls add storage engine round-trips"
    if "contains no var" in detail:
        expr_len = len(f.get("expression", ""))
        return f"Low\u2013Medium cost · {expr_len}-char expression may be evaluated multiple times"
    if "likely fact table" in detail or ("fact table" in detail and table != "Relationship"):
        return f"High model size + refresh cost · Column stored per-row across all rows in {table}"
    if "related()" in detail or "relatedtable()" in detail:
        return f"Medium refresh cost · Follows the relationship for every row in {table}"
    if "nested if" in detail or "if keyword" in detail:
        return "Low\u2013Medium refresh cost · Each IF nesting level adds a branch evaluation per row"
    if risk == "HIGH":
        return "High cost operation · Review before deploying to production"
    if risk == "MEDIUM":
        return "Medium cost operation · Monitor query performance with large datasets"
    return "Low cost operation · Minimal performance impact expected"


# ---------------------------------------------------------------------------
# Rewrite identity check
# ---------------------------------------------------------------------------

def _is_rewrite_trivial(original: str, rewrite: str) -> bool:
    """
    Return True if the rewrite does not offer a genuinely different DAX expression.
    Catches the case where the rewrite is the original wrapped in comment lines only.
    """
    if not rewrite:
        return True
    _ws = lambda s: re.sub(r'\s+', ' ', s.strip().lower())
    if _ws(original) == _ws(rewrite):
        return True
    # Strip comment-only lines then strip a leading "Name :=" declaration
    no_comments = '\n'.join(
        line for line in rewrite.split('\n')
        if not line.strip().startswith('--')
    )
    no_comments = re.sub(r'^[^\n]+:=\s*\n?', '', no_comments.strip(), count=1)
    if _ws(original) == _ws(no_comments):
        return True
    return False


# ---------------------------------------------------------------------------
# Score calculation
# ---------------------------------------------------------------------------

def _score(findings: list[dict]) -> int:
    s = 100
    for f in findings:
        if f["risk"] == "HIGH":
            s -= 15
        elif f["risk"] == "MEDIUM":
            s -= 8
        else:
            s -= 3
    return max(s, 0)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_RISK_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}
_RISK_CLASS = {"HIGH": "risk-high", "MEDIUM": "risk-medium", "LOW": "risk-low"}
_RISK_LABEL = {"HIGH": "HIGH RISK", "MEDIUM": "MEDIUM RISK", "LOW": "LOW RISK"}


def _render_finding(f: dict, usage_ctx: dict | None = None) -> None:
    usage_ctx = usage_ctx or {}
    emoji = _RISK_EMOJI[f["risk"]]
    css = _RISK_CLASS[f["risk"]]
    label = _RISK_LABEL[f["risk"]]
    table_info = f"  \u00b7  Table: **{f['table']}**" if f["table"] != "Relationship" else ""
    impact = _get_impact(f)
    st.markdown(
        f"""<div class="{css}">
        <b>{emoji} {label}</b>{table_info}<br>
        <b>Measure / Item:</b> {f['measure']}<br>
        <b>Issue:</b> {f['detail']}<br>
        <b>Recommendation:</b> {f['recommendation']}<br>
        <b>Impact:</b> <span style="font-size:0.88rem;color:#555">{impact}</span>
        </div>""",
        unsafe_allow_html=True,
    )

    # \u2500\u2500 Used In Report \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    used_in = f.get("used_in")
    if used_in is None:
        m_name = f["measure"]
        t_name = f["table"]
        field_key = f"{t_name}[{m_name}]"
        used_in = usage_ctx.get(field_key, [])
        if not used_in:
            suffix = f"[{m_name}]"
            for key, val in usage_ctx.items():
                if key.endswith(suffix):
                    used_in = val
                    break
        if not used_in:
            used_in = []

    if used_in:
        expander_usage_label = f"✅ Used in {len(used_in)} visual(s)"
    else:
        expander_usage_label = "⚠️ Defined but not bound to any visual"
    with st.expander(expander_usage_label, expanded=False):
        if used_in:
            import pandas as _pd
            rows = [
                {
                    "Page": u.get("page", ""),
                    "Visual Title": u.get("visual_name", ""),
                    "Visual Type": u.get("visual_type", ""),
                    "Role": u.get("role", ""),
                }
                for u in used_in
            ]
            v_types_lower = {u.get("visual_type", "").lower() for u in used_in}
            v_roles_lower = {u.get("role", "").lower() for u in used_in}
            # Context-aware banners
            if v_types_lower & {"matrix", "tableex"}:
                st.markdown(
                    '<div style="background:#fdecea;border-left:4px solid #d32f2f;'
                    'padding:8px 12px;border-radius:4px;margin-bottom:8px">'
                    '\U0001f534 <b>Used in matrix</b> \u2014 performance impact is high here</div>',
                    unsafe_allow_html=True,
                )
            elif v_roles_lower & {"tooltips", "tooltip"}:
                st.markdown(
                    '<div style="background:#fff3e0;border-left:4px solid #e65100;'
                    'padding:8px 12px;border-radius:4px;margin-bottom:8px">'
                    '\U0001f7e0 <b>Used only in tooltip</b> \u2014 low priority measure</div>',
                    unsafe_allow_html=True,
                )
            st.dataframe(_pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.markdown(
                '<div style="background:#fffde7;border-left:4px solid #f9a825;'
                'padding:8px 12px;border-radius:4px">'
                '\u26a0\ufe0f <b>Not used in any visual</b> \u2014 review before keeping</div>',
                unsafe_allow_html=True,
            )

    # ── Original DAX + Suggested Rewrite / Why This Works ──────────────────
    rewrite = f.get("rewrite") or _suggest_rewrite(f)
    rewrite_explanation = f.get("rewrite_explanation", "")
    # Only surface a rewrite block if the code is genuinely different from the original
    has_real_rewrite = bool(rewrite) and not _is_rewrite_trivial(f["expression"], rewrite)
    expander_label = "🔍 Original DAX" + ("  ·  💡 Suggested Rewrite available" if has_real_rewrite else "")
    with st.expander(expander_label, expanded=False):
        st.markdown("**Original DAX**")
        # text_area: no line numbers, full expression, auto-sized height
        line_count = f["expression"].count("\n") + 1
        height = max(80, min(line_count * 22, 500))
        st.text_area(
            "",
            value=f["expression"],
            height=height,
            key=f"dax_{hash(f['measure'] + f['table'] + f['expression']) & 0xFFFFFF}",
            disabled=True,
            label_visibility="collapsed",
        )
        if has_real_rewrite:
            st.markdown("---")
            st.markdown("**Suggested Rewrite**")
            if rewrite_explanation:
                st.markdown(
                    f'<p style="color:#2e7d32;font-size:0.88rem;margin-bottom:6px">'
                    f'{rewrite_explanation}</p>',
                    unsafe_allow_html=True,
                )
            st.code(rewrite, language="dax")
        elif rewrite_explanation:
            st.markdown("---")
            st.markdown(
                '<div style="background:#e8f5e9;border-left:4px solid #2e7d32;'
                'padding:10px 14px;border-radius:6px">'
                '<b>✅ Why This Works</b><br>'
                f'<span style="font-size:0.88rem">{rewrite_explanation}</span></div>',
                unsafe_allow_html=True,
            )


def _render_section(title: str, findings: list[dict], usage_ctx: dict | None = None) -> None:
    st.markdown(f"### {title}  ({len(findings)})")
    if not findings:
        st.success("None found ✅")
    else:
        for f in findings:
            _render_finding(f, usage_ctx)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

raw_bytes: bytes | None = st.session_state.get("pbi_file_bytes")
file_name: str = st.session_state.get("pbi_file_name", "")

if not raw_bytes:
    st.info("👈 Upload a `.pbix` or `.pbit` file on the **Home** page first, then come back here.")
    st.stop()

st.caption(f"Analysing: **{file_name}**")

with st.spinner("Scanning measures and calculated columns…"):
    measures, calc_columns, relationships = _extract_all(raw_bytes, file_name)
    field_usage_ctx = _build_field_usage_context(raw_bytes)
    debug_field_rows = _build_debug_field_map(raw_bytes)

# ── Merge field usage from all other uploaded reports ─────────────────────────
_reports_store: dict = st.session_state.get("reports", {})
_per_report_bare: dict[str, set[str]] = {
    file_name: {_bare_name(k).lower() for k in field_usage_ctx}
}
for _fname, _rdata in _reports_store.items():
    if _fname == file_name:
        continue
    _r_raw = _rdata.get("raw_bytes", b"")
    if not _r_raw:
        continue
    _r_ctx = _build_field_usage_context(_r_raw)
    for _key, _usages in _r_ctx.items():
        field_usage_ctx.setdefault(_key, []).extend(_usages)
    _per_report_bare[_fname] = {_bare_name(k).lower() for k in _r_ctx}

_n_reports = len(_per_report_bare)

if not measures and not relationships and not calc_columns:
    st.warning("No measures, calculated columns or relationships found. The file may not contain a DataModelSchema.")
    st.stop()

# ── Run all checks ────────────────────────────────────────────────────────────
findings_high: list[dict] = []
findings_medium: list[dict] = []
findings_low: list[dict] = []

# Stage-2: detect date table names for time-intelligence validation
_date_table_names: set[str] = set()
try:
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as _zf_dt:
        _names_dt = {n.lower(): n for n in _zf_dt.namelist()}
        _cand_dt = next((r for l, r in _names_dt.items()
                         if "datamodelschema" in l or l.endswith(".bim")), None)
        if _cand_dt:
            _schema_dt = _try_decode(_zf_dt.read(_cand_dt))
            if _schema_dt:
                _model_dt = _schema_dt.get("model", _schema_dt)
                for _tbl in _model_dt.get("tables", []):
                    if _tbl.get("isDateTable") or _tbl.get("showAsVariationsOnly"):
                        _date_table_names.add(_tbl.get("name", ""))
                    # heuristic: table named "Date", "Calendar", "Dates" etc.
                    _tn = _tbl.get("name", "").lower()
                    if any(_tn == x or _tn.startswith(x) for x in ("date", "calendar", "dim_date", "dimdate")):
                        _date_table_names.add(_tbl.get("name", ""))
except Exception:
    pass

for m in measures:
    # Stage-1 proven anti-patterns
    for result in _check_nested_iterators(m):
        findings_high.append(result)
    for checker in (_check_filter_all, _check_iferror_complex,
                    _check_summarize_filter, _check_distinctcount):
        result = checker(m)
        if result:
            if result["risk"] == "HIGH":
                findings_high.append(result)
            elif result["risk"] == "MEDIUM":
                findings_medium.append(result)
            else:
                findings_low.append(result)
    # Stage-2 new checkers
    for checker2 in (_check_allselected, _check_allexcept, _check_crossjoin):
        result = checker2(m)
        if result:
            if result["risk"] == "HIGH":
                findings_high.append(result)
            elif result["risk"] == "MEDIUM":
                findings_medium.append(result)
            else:
                findings_low.append(result)
    ti_result = _check_time_intelligence(m, _date_table_names)
    if ti_result:
        findings_high.append(ti_result)

for f in _check_bidir(relationships):
    findings_medium.append(f)

# Stage-2: circular reference detection
for f in _detect_circular_refs(measures):
    findings_high.append(f)

# Stage-2: Power Query audit (run once, cached in session_state)
_pq_key        = f"pq_findings_{hash(file_name) & 0xFFFF}"
_pq_source_key = f"pq_found_{hash(file_name) & 0xFFFF}"
if _pq_key not in st.session_state:
    _pq_results, _pq_found = _scan_power_query(raw_bytes)
    st.session_state[_pq_key]        = _pq_results
    st.session_state[_pq_source_key] = _pq_found
pq_findings: list[dict] = st.session_state[_pq_key]
pq_source_found: bool   = st.session_state.get(_pq_source_key, False)

# Stage-2: Cross-report DAX drift (only when multiple files uploaded)
_drift_key = f"drift_{hash(file_name) & 0xFFFF}"
if _drift_key not in st.session_state:
    st.session_state[_drift_key] = _detect_drift(measures, _reports_store, file_name)
drift_findings: list[dict] = st.session_state[_drift_key]

# Stage-2: Measure complexity tier for every measure
complexity_records: list[dict] = [_complexity_record(m) for m in measures]

# Calculated columns are evaluated at model refresh, not at query time.
# Runtime query performance is driven by DAX measures only — no column findings raised.
cc_findings_high: list[dict] = []
cc_findings_medium: list[dict] = []
cc_findings_low: list[dict] = []

all_findings = (findings_high + findings_medium + findings_low
                + cc_findings_high + cc_findings_medium + cc_findings_low)
total_score = _score(all_findings)
total_issues = len(all_findings)
n_high   = len(findings_high)   + len(cc_findings_high)
n_medium = len(findings_medium) + len(cc_findings_medium)
n_low    = len(findings_low)    + len(cc_findings_low)

# Persist exact score so the home scorecard reads the real number
st.session_state["perf_score"] = total_score
st.session_state["perf_issues"] = {"high": n_high, "medium": n_medium, "low": n_low}

# Stage-2: Score trend — persist last 10 scores in session_state
_trend_key = f"score_hist_{hash(file_name) & 0xFFFF}"
_score_hist = st.session_state.get(_trend_key, [])
if not _score_hist or _score_hist[-1] != total_score:
    _score_hist = (_score_hist + [total_score])[-10:]
    st.session_state[_trend_key] = _score_hist

# ── Compute unused measures ───────────────────────────────────────────────────
unused_measures = _compute_unused_measures_perf(measures, field_usage_ctx, _per_report_bare)

# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR — Filters + Debug toggle
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("---")
    st.markdown("### 🔍 Filters")

    _filter_risk: str = st.radio(
        "Risk Level",
        options=["All", "High", "Medium", "Low"],
        index=0,
        key="perf_filter_risk",
    )

    _all_tables_list = sorted({
        f.get("table", "") for f in all_findings
        if f.get("table") and f["table"] != "Relationship"
    })
    _filter_table: str = st.selectbox(
        "Table",
        options=["All Tables"] + _all_tables_list,
        index=0,
        key="perf_filter_table",
    )

    _filters_active = _filter_risk != "All" or _filter_table != "All Tables"
    if _filters_active:
        st.caption("🔵 Filters active — results are narrowed")

    st.markdown("---")
    st.markdown("### 🔎 Global Search")
    _global_search: str = st.text_input(
        "Search all findings",
        placeholder="e.g. SUMX, FILTER, table name…",
        key="perf_global_search",
        label_visibility="collapsed",
    )
    if _global_search:
        st.caption(f"Showing only findings containing **{_global_search}**")

    st.markdown("---")
    _show_debug: bool = st.toggle("🐛 Show Debug Panel", value=False, key="perf_show_debug")
    if _show_debug:
        st.markdown("### 🐛 Field Bindings")
        if debug_field_rows:
            import pandas as _pd_dbg
            st.caption(
                f"{len(debug_field_rows)} binding(s) · "
                f"{len({r['Page'] for r in debug_field_rows})} page(s) · "
                f"{len({(r['Page'], r['Visual']) for r in debug_field_rows})} visual(s)"
            )
            st.dataframe(
                _pd_dbg.DataFrame(debug_field_rows),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Field":  st.column_config.TextColumn("Field",  width="medium"),
                    "Source": st.column_config.TextColumn("Source", width="medium"),
                },
            )
        else:
            st.info("No field bindings extracted.")

# ── Helper: apply table-level + global-search filters ────────────────────────
def _apply_table_filter(findings: list[dict]) -> list[dict]:
    out = findings
    if _filter_table != "All Tables":
        out = [f for f in out if f.get("table", "") == _filter_table]
    if _global_search:
        q = _global_search.lower()
        out = [
            f for f in out
            if q in f.get("measure", "").lower()
            or q in f.get("table", "").lower()
            or q in f.get("detail", "").lower()
            or q in f.get("expression", "").lower()
        ]
    return out

# ─────────────────────────────────────────────────────────────────────────────
# SCORE BOX
# ─────────────────────────────────────────────────────────────────────────────
if total_score >= 80:
    score_color = "#1b5e20"; score_bg = "#e8f5e9"; score_label = "Great shape"
elif total_score >= 60:
    score_color = "#e65100"; score_bg = "#fff3e0"; score_label = "Needs attention"
else:
    score_color = "#b71c1c"; score_bg = "#fdecea"; score_label = "Critical issues"

st.markdown(
    f"""<div class="score-box" style="background:{score_bg};border:2px solid {score_color}">
    <span style="font-size:2.4rem;font-weight:800;color:{score_color}">{total_score}</span>
    <span style="font-size:1.1rem;color:{score_color}"> / 100 &nbsp;·&nbsp; {score_label}</span><br>
    <span style="font-size:0.85rem;color:#555">
      {total_issues} issue(s) &nbsp;·&nbsp;
      {n_high} HIGH &nbsp;·&nbsp;
      {n_medium} MEDIUM &nbsp;·&nbsp;
      {n_low} LOW &nbsp;·&nbsp;
      {len(unused_measures)} not used in report
    </span>
    </div>""",
    unsafe_allow_html=True,
)

# ── Score trend chart ─────────────────────────────────────────────────────────
if len(_score_hist) > 1:
    _trend_df = pd.DataFrame({"Scan #": range(1, len(_score_hist) + 1), "Score": _score_hist})
    st.caption(f"Score history ({len(_score_hist)} scans) — trend over time:")
    st.line_chart(_trend_df.set_index("Scan #"), height=90, use_container_width=True)
st.markdown("")

# ── Clickable Summary Scorecard ───────────────────────────────────────────────
st.markdown(
    """<style>
    a.metric-card {
        background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px;
        padding:16px 20px; text-align:center; cursor:pointer; text-decoration:none;
        display:block; color:inherit; transition:box-shadow .2s, background .2s;
    }
    a.metric-card:hover { box-shadow:0 4px 14px rgba(0,0,0,.10); background:#f1f5f9; }
    a.metric-card:visited { color:inherit; }
    .mc-value { font-size:2rem; font-weight:800; color:#1e293b; line-height:1.1; }
    .mc-label { font-size:0.82rem; color:#64748b; margin-top:5px; }
    </style>""",
    unsafe_allow_html=True,
)

_high_color = "#d32f2f" if n_high > 0 else "#2e7d32"
mc1, mc2, mc3, mc4 = st.columns(4)
with mc1:
    st.markdown(
        f'<a class="metric-card" href="#measures-analysis">'
        f'<div class="mc-value">{len(measures)}</div>'
        f'<div class="mc-label">⚡ Measures Scanned</div>'
        f'</a>',
        unsafe_allow_html=True,
    )
with mc2:
    st.markdown(
        f'<a class="metric-card" href="#high-risk">'
        f'<div class="mc-value" style="color:{_high_color}">🔴 {n_high}</div>'
        f'<div class="mc-label">High Risk Issues</div>'
        f'</a>',
        unsafe_allow_html=True,
    )
with mc3:
    st.markdown(
        f'<a class="metric-card" href="#unused-measures">'
        f'<div class="mc-value">{len(unused_measures)}</div>'
        f'<div class="mc-label">🔍 Not Used in Report</div>'
        f'</a>',
        unsafe_allow_html=True,
    )
with mc4:
    st.markdown(
        f'<a class="metric-card" href="#calculated-columns">'
        f'<div class="mc-value">{len(calc_columns)}</div>'
        f'<div class="mc-label">🧮 Calculated Columns</div>'
        f'</a>',
        unsafe_allow_html=True,
    )

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# UNUSED MEASURES — 4-column table + search box
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div id="unused-measures"></div>', unsafe_allow_html=True)
_um_title = "Measures Not Used in Any Uploaded Report" if _n_reports > 1 else "Measures Not Used in This Report"
st.markdown(f"## 🔍 {_um_title}")
st.caption(
    f"📂 **{_n_reports}** report(s) analysed · "
    f"**{len(measures)}** measure(s) · "
    f"**{len(unused_measures)}** not used in any uploaded report"
)
if _n_reports > 1:
    st.caption("Reports included: " + " · ".join(f"`{fn}`" for fn in _per_report_bare))
st.info("💡 For complete coverage, upload all reports that share this dataset at the same time.")

if not unused_measures:
    st.success("✅ All measures are referenced in at least one uploaded report (or transitively used by one).")
else:
    st.warning(
        f"⚠️ **{len(unused_measures)} measure(s)** are not used in any of the {_n_reports} uploaded report(s). "
        "They may still be used in other reports connected to the same dataset. "
        "**Do not delete without checking all connected reports.**"
    )
    import pandas as _pd_um

    _um_search = st.text_input(
        "🔎 Filter by table name",
        placeholder="Type a table name…",
        key="um_search",
    )
    _um_rows = [
        {
            "Table": r["Table"],
            "Measure": r["Measure"],
            "Risk to Delete": r["Risk to Delete"],
            "Used In Reports": r["Used in Reports"],
        }
        for r in unused_measures
    ]
    if _um_search:
        _um_rows = [r for r in _um_rows if _um_search.lower() in r["Table"].lower()]

    if _um_rows:
        st.dataframe(
            _pd_um.DataFrame(_um_rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Table":          st.column_config.TextColumn("Table",          width="medium"),
                "Measure":        st.column_config.TextColumn("Measure",        width="large"),
                "Risk to Delete": st.column_config.TextColumn("Risk to Delete", width="small"),
                "Used In Reports":st.column_config.TextColumn("Used In Reports",width="large"),
            },
        )
    else:
        st.info("No results match the table name filter.")

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# COMPACT FINDINGS TABLE — helpers
# ─────────────────────────────────────────────────────────────────────────────
_RISK_BADGE_HTML = {
    "HIGH":   ('<span style="background:#fdecea;color:#c62828;border:1px solid #ef9a9a;'
               'border-radius:4px;padding:2px 8px;font-size:0.76rem;font-weight:700">🔴 HIGH</span>'),
    "MEDIUM": ('<span style="background:#fff3e0;color:#e65100;border:1px solid #ffcc80;'
               'border-radius:4px;padding:2px 8px;font-size:0.76rem;font-weight:700">🟡 MED</span>'),
    "LOW":    ('<span style="background:#f1f8e9;color:#33691e;border:1px solid #aed581;'
               'border-radius:4px;padding:2px 8px;font-size:0.76rem;font-weight:700">🟢 LOW</span>'),
}


def _get_used_in_summary(f: dict, usage_ctx: dict) -> tuple[str, list]:
    """Return (short display label, full used_in list)."""
    used_in = f.get("used_in")
    if used_in is None:
        m_name, t_name = f["measure"], f["table"]
        field_key = f"{t_name}[{m_name}]"
        used_in = usage_ctx.get(field_key, [])
        if not used_in:
            suffix = f"[{m_name}]"
            for k, v in usage_ctx.items():
                if k.endswith(suffix):
                    used_in = v
                    break
        used_in = used_in or []
    label = f"✅ {len(used_in)} visual(s)" if used_in else "⚠️ Not used"
    return label, used_in


def _render_compact_findings(findings: list[dict], usage_ctx: dict, prefix: str) -> None:
    """Compact table: one row per finding, View Details expands inline."""
    if not findings:
        st.success("None found ✅")
        return

    # Header row — Risk | Table | Measure | Issue | Impact | Used In | Action
    hdr = st.columns([1.2, 2, 3, 3.5, 1.8, 1.8, 1.6])
    for col, lbl in zip(hdr, ["Risk", "Table", "Measure", "Issue", "Impact", "Used In", "Action"]):
        col.markdown(f"<small><b>{lbl}</b></small>", unsafe_allow_html=True)
    st.markdown(
        '<hr style="margin:2px 0 6px;border:none;border-top:2px solid #cbd5e1">',
        unsafe_allow_html=True,
    )

    for i, f in enumerate(findings):
        _key = abs(hash(f["measure"] + f["table"] + f.get("expression", "")[:40])) % 999983
        row_key = f"{prefix}_{i}_{_key}"

        used_label, _ = _get_used_in_summary(f, usage_ctx)
        badge = _RISK_BADGE_HTML.get(f["risk"], f["risk"])
        issue_short = f["detail"][:80] + ("…" if len(f["detail"]) > 80 else "")
        impact = _impact_label(f)

        cols = st.columns([1.2, 2, 3, 3.5, 1.8, 1.8, 1.6])
        cols[0].markdown(badge, unsafe_allow_html=True)
        cols[1].markdown(f"<small>{f['table']}</small>", unsafe_allow_html=True)
        cols[2].markdown(f"<small>**{f['measure']}**</small>", unsafe_allow_html=True)
        cols[3].markdown(f"<small>{issue_short}</small>", unsafe_allow_html=True)
        cols[4].markdown(f"<small>{impact}</small>", unsafe_allow_html=True)
        cols[5].markdown(f"<small>{used_label}</small>", unsafe_allow_html=True)

        _open_key = f"open_{row_key}"
        is_open = st.session_state.get(_open_key, False)
        if cols[6].button(
            "▲ Hide" if is_open else "▼ Details",
            key=f"btn_{row_key}",
            use_container_width=True,
        ):
            st.session_state[_open_key] = not is_open

        if st.session_state.get(_open_key, False):
            _render_finding(f, usage_ctx)

        st.markdown(
            '<hr style="margin:4px 0;border:none;border-top:1px solid #f1f5f9">',
            unsafe_allow_html=True,
        )


def _render_risk_group(
    risk_label: str,
    emoji: str,
    findings: list[dict],
    usage_ctx: dict,
    prefix: str,
    expanded_by_default: bool = False,
) -> None:
    """Collapsible section header + compact table for one risk tier."""
    if not findings:
        return
    display = _apply_table_filter(findings)
    if not display:
        return  # all filtered out by table filter — skip expander entirely
    with st.expander(f"{emoji} {risk_label} ({len(display)})", expanded=expanded_by_default):
        _render_compact_findings(display, usage_ctx, prefix)


# ─────────────────────────────────────────────────────────────────────────────
# MEASURES ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div id="measures-analysis"></div>', unsafe_allow_html=True)
st.markdown("## 📐 Measures Analysis")
st.markdown('<div id="high-risk"></div>', unsafe_allow_html=True)

if not (findings_high or findings_medium or findings_low):
    st.success("✅ No performance issues found in measures.")
else:
    _show_high   = _filter_risk in ("All", "High")
    _show_medium = _filter_risk in ("All", "Medium")
    _show_low    = _filter_risk in ("All", "Low")

    if _show_high:
        _render_risk_group("HIGH RISK",   "🔴", findings_high,   field_usage_ctx, "mh", expanded_by_default=True)
    if _show_medium:
        _render_risk_group("MEDIUM RISK", "🟡", findings_medium, field_usage_ctx, "mm", expanded_by_default=False)
    if _show_low:
        _render_risk_group("LOW RISK",    "🟢", findings_low,    field_usage_ctx, "ml", expanded_by_default=False)

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# CALCULATED COLUMNS — informational only
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div id="calculated-columns"></div>', unsafe_allow_html=True)
st.markdown("## 🧮 Calculated Columns")
st.info(
    "Calculated columns are evaluated once at model refresh — they have no runtime query cost. "
    "Runtime query performance is determined entirely by DAX **measures**. "
    "No performance findings are raised for columns."
)

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# MEASURE COMPLEXITY TIER  (Stage-2)
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("## 🧠 Measure Complexity Tier")
st.markdown(
    "Every measure is scored by **nesting depth + iterator count + bracket references**. "
    "Use this to prioritise optimisation — fix 🔴 Slow measures first."
)
if complexity_records:
    _cx_search = st.text_input(
        "🔎 Filter measures",
        placeholder="Type a measure or table name…",
        key="cx_search",
        label_visibility="collapsed",
    )
    _cx_tier_filter = st.radio(
        "Tier",
        options=["All", "🔴 Slow", "🟡 Medium", "🟢 Fast"],
        index=0,
        horizontal=True,
        key="cx_tier_filter",
    )
    _cx_rows = complexity_records
    if _cx_tier_filter != "All":
        _cx_rows = [r for r in _cx_rows if r["Tier"] == _cx_tier_filter]
    if _cx_search:
        _qs = _cx_search.lower()
        _cx_rows = [r for r in _cx_rows if _qs in r["Measure"].lower() or _qs in r["Table"].lower()]
    # Sort: Slow first
    _tier_order = {"🔴 Slow": 0, "🟡 Medium": 1, "🟢 Fast": 2}
    _cx_rows_sorted = sorted(_cx_rows, key=lambda r: (_tier_order.get(r["Tier"], 3), -r["Score"]))

    _slow_n   = sum(1 for r in complexity_records if r["Tier"] == "🔴 Slow")
    _medium_n = sum(1 for r in complexity_records if r["Tier"] == "🟡 Medium")
    _fast_n   = sum(1 for r in complexity_records if r["Tier"] == "🟢 Fast")
    _c1, _c2, _c3 = st.columns(3)
    _c1.metric("🔴 Slow",   _slow_n,   help="Nesting depth ≥ 10, score ≥ 25, or ≥ 3 iterators")
    _c2.metric("🟡 Medium", _medium_n, help="Score ≥ 10 or depth ≥ 5 or any iterator")
    _c3.metric("🟢 Fast",   _fast_n,   help="Simple measures — no iterators, shallow nesting")

    if _cx_rows_sorted:
        st.dataframe(
            pd.DataFrame(_cx_rows_sorted),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Table":     st.column_config.TextColumn("Table",     width="medium"),
                "Measure":   st.column_config.TextColumn("Measure",   width="large"),
                "Tier":      st.column_config.TextColumn("Tier",      width="small"),
                "Depth":     st.column_config.NumberColumn("Depth",   width="small",
                             help="Max parenthesis nesting depth"),
                "Iterators": st.column_config.NumberColumn("Iters",   width="small",
                             help="SUMX / AVERAGEX / MAXX / MINX count"),
                "Refs":      st.column_config.NumberColumn("Refs",    width="small",
                             help="Number of [bracket] references"),
                "Score":     st.column_config.NumberColumn("Score",   width="small",
                             help="Composite: Depth×2 + Iters×5 + Refs"),
            },
        )
    else:
        st.info("No measures match the current filter.")
else:
    st.info("No measures found to analyse.")

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# POWER QUERY AUDIT  (Stage-2)
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("## 🔧 Power Query (M) Audit")
st.markdown(
    "Scans Power Query M source for "
    "**hardcoded credentials**, **native SQL queries**, **hardcoded URLs**, "
    "and **missing error handling** on external sources. "
    "Reads the Mashup ZIP (PBIX) or `DataModelSchema` partition expressions (PBIT) — "
    "both file types are fully supported."
)
if pq_findings:
    _pq_risk_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    _pq_sorted = sorted(pq_findings, key=lambda r: _pq_risk_order.get(r["risk"], 3))
    for _pf in _pq_sorted:
        _pq_risk = _pf["risk"]
        _pq_cls = {"HIGH": "risk-high", "MEDIUM": "risk-medium", "LOW": "risk-low"}.get(_pq_risk, "risk-low")
        _pq_em  = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(_pq_risk, "🟢")
        st.markdown(
            f'<div class="{_pq_cls}">'
            f'{_pq_em} <b>[{_pf["file"]}]</b> {_pf["issue"]}<br>'
            f'<span style="font-size:0.82rem;color:#555">💡 {_pf["recommendation"]}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
elif pq_source_found:
    st.success("✅ No Power Query red flags found (credentials, native SQL, hardcoded URLs, or missing error handling).")
else:
    st.warning(
        "⚠️ No Power Query M source could be extracted from this file. "
        "The model may use import mode with no M partitions, "
        "or the Mashup section is absent / compressed differently."
    )

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# CROSS-REPORT DAX DRIFT  (Stage-2 — only when >1 report uploaded)
# ─────────────────────────────────────────────────────────────────────────────
if _n_reports > 1:
    st.markdown("## 🔄 Cross-Report DAX Drift")
    st.markdown(
        f"Comparing **{_n_reports}** uploaded reports. "
        "Measures below share the same name but have **different DAX** — version drift."
    )
    if drift_findings:
        st.warning(f"⚠️ {len(drift_findings)} measure(s) have diverged across reports.")
        st.dataframe(
            pd.DataFrame(drift_findings),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Measure":  st.column_config.TextColumn("Measure",  width="large"),
                "Table":    st.column_config.TextColumn("Table",    width="medium"),
                "Variants": st.column_config.NumberColumn("Variants", width="small",
                            help="Number of distinct DAX expressions across reports"),
                "Reports":  st.column_config.TextColumn("In Reports", width="large"),
                "Issue":    st.column_config.TextColumn("Issue",    width="large"),
            },
        )
    else:
        st.success("✅ All shared measures have identical DAX across uploaded reports.")
    st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# QUICK WINS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("## ✅ Quick Wins")
st.markdown(
    "HIGH RISK items where a fix can be generated automatically. "
    "Review each rewrite, adjust column/table names as needed, then apply in Power BI Desktop."
)

_all_high   = findings_high + cc_findings_high
quick_wins  = [(f, _suggest_rewrite(f)) for f in _all_high]
quick_wins  = [(f, rw) for f, rw in quick_wins if rw is not None]

if not quick_wins:
    st.info("No automatically rewritable HIGH RISK findings found.")
else:
    st.caption(f"{len(quick_wins)} auto-fixable finding(s)")
    for f, rewrite in quick_wins:
        table_badge = f" · Table: **{f['table']}**" if f["table"] not in ("", "Relationship") else ""
        st.markdown(
            f"""<div class="quick-win">
            <b>🔴 {f['measure']}</b>{table_badge}<br>
            <span style="color:#555;font-size:0.9rem">{f['detail']}</span>
            <pre>{rewrite}</pre>
            </div>""",
            unsafe_allow_html=True,
        )

# ─────────────────────────────────────────────────────────────────────────────
# CSV EXPORT
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown("## ⬇️ Export Findings")
if all_findings:
    _export_rows = [
        {
            "Risk":           f["risk"],
            "Impact":         _impact_label(f),
            "Table":          f.get("table", ""),
            "Measure":        f.get("measure", ""),
            "Issue":          f.get("detail", ""),
            "Recommendation": f.get("recommendation", ""),
        }
        for f in all_findings
    ]
    _csv_bytes = pd.DataFrame(_export_rows).to_csv(index=False).encode("utf-8")
    st.download_button(
        label="📥 Download all findings as CSV",
        data=_csv_bytes,
        file_name=f"performance_findings_{file_name.replace('.', '_')}.csv",
        mime="text/csv",
        help="Download a spreadsheet of all performance findings for this report.",
    )
else:
    st.success("No findings to export — your DAX is clean! ✅")
