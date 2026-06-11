"""
pbit_extractor.py
=================
Standalone utility to extract structured metadata from a Power BI Template
(.pbit) or Report (.pbix) file.

Usage
-----
    from pbit_extractor import extract_pbit_metadata

    result = extract_pbit_metadata("MyReport.pbit")
    print(result["summary"])
    for m in result["measures"]:
        print(m["table"], "->", m["name"])

Public API
----------
    extract_pbit_metadata(file_path: str | Path) -> dict

Return shape
------------
    {
        "file":          str,                 # absolute path to the input file
        "zip_contents":  list[str],           # every entry in the zip archive
        "model_format":  str,                 # "standard" | "enhanced" | "live_connect" | "unknown"
        "tables":        list[TableDict],
        "columns":       list[ColumnDict],    # flat list, includes "table" key
        "measures":      list[MeasureDict],   # flat list, includes "table" key
        "relationships": list[RelDict],
        "roles":         list[RoleDict],      # row-level security roles
        "data_sources":  list[DataSourceDict],
        "summary":       SummaryDict,
        "errors":        list[str],           # non-fatal warnings / parse issues
    }
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Encoding order observed in the wild for DataModelSchema blobs.
# utf-16-le is most common in PBIT; utf-8-sig in newer formats.
# ─────────────────────────────────────────────────────────────────────────────
_ENCODINGS = ("utf-8-sig", "utf-16-le", "utf-16", "utf-8", "utf-16-be", "latin-1")

# Table name prefixes that represent Power BI internal / auto-generated tables.
_INTERNAL_PREFIXES = ("$", "LocalDateTable_", "DateTableTemplate_")


# ─────────────────────────────────────────────────────────────────────────────
# Low-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _decode_json(raw: bytes) -> dict | None:
    """Try every known encoding to decode a raw byte blob into a JSON dict."""
    for enc in _ENCODINGS:
        try:
            text = raw.decode(enc)
            stripped = text.lstrip("\ufeff").lstrip()
            # Guard against accidentally decoding a binary blob as text
            if not stripped or stripped[0] not in ('{', '['):
                continue
            return json.loads(stripped)
        except Exception:
            continue
    return None


def _normalise_expr(expr: Any) -> str:
    """DAX expressions are sometimes stored as a list of lines; join them."""
    if isinstance(expr, list):
        return "\n".join(str(line) for line in expr).strip()
    return str(expr).strip() if expr else ""


def _is_internal(table_name: str) -> bool:
    return any(table_name.startswith(p) for p in _INTERNAL_PREFIXES)


# ─────────────────────────────────────────────────────────────────────────────
# Schema candidates inside a ZipFile
# ─────────────────────────────────────────────────────────────────────────────

def _find_schema_candidates(names_lower: dict[str, str]) -> list[str]:
    """
    Return the real-cased zip entry names that look like model schema files.
    Covers: DataModelSchema, *.bim (Tabular Editor), model.json (Azure AS).
    """
    return [
        real
        for lower, real in names_lower.items()
        if "datamodelschema" in lower
        or lower.endswith(".bim")
        or lower.endswith("model.json")
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Parsers: turn a raw model dict into typed lists
# ─────────────────────────────────────────────────────────────────────────────

def _parse_columns(tbl: dict, table_name: str) -> list[dict]:
    """Extract every non-internal column from a table dict."""
    cols = []
    for c in tbl.get("columns", []):
        name = c.get("name", "").strip()
        # Skip Power BI's internal RowNumber columns
        if not name or name.startswith("RowNumber"):
            continue
        cols.append({
            "name":           name,
            "table":          table_name,
            "data_type":      c.get("dataType", ""),
            "type":           c.get("type", "data"),       # "data" | "calculated" | "rowNumber"
            "expression":     _normalise_expr(c.get("expression", "")),
            "is_hidden":      bool(c.get("isHidden", False)),
            "format_string":  c.get("formatString", ""),
            "description":    c.get("description", ""),
            "display_folder": c.get("displayFolder", ""),
            "sort_by_column": c.get("sortByColumn", ""),
            "summarize_by":   c.get("summarizeBy", ""),
        })
    return cols


def _parse_measures(tbl: dict, table_name: str) -> list[dict]:
    """Extract every measure from a table dict."""
    measures = []
    for m in tbl.get("measures", []):
        name = m.get("name", "").strip()
        if not name:
            continue
        measures.append({
            "name":           name,
            "table":          table_name,
            "expression":     _normalise_expr(m.get("expression", "")),
            "description":    m.get("description", ""),
            "format_string":  m.get("formatString", ""),
            "display_folder": m.get("displayFolder", ""),
            "is_hidden":      bool(m.get("isHidden", False)),
        })
    return measures


def _parse_partitions(tbl: dict) -> list[dict]:
    """Extract partition / source query info from a table dict."""
    parts = []
    for p in tbl.get("partitions", []):
        source = p.get("source", {})
        parts.append({
            "name":        p.get("name", ""),
            "source_type": source.get("type", ""),   # "m" | "calculated" | "entity"
            "expression":  _normalise_expr(source.get("expression", "")),
        })
    return parts


def _parse_tables(model: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Walk model["tables"] and return three parallel flat lists:
        tables_out   – one entry per table (includes nested columns/measures/partitions)
        columns_out  – flat list with "table" key injected
        measures_out – flat list with "table" key injected
    Internal Power BI auto-tables are still parsed but flagged is_internal=True.
    """
    tables_out:   list[dict] = []
    columns_out:  list[dict] = []
    measures_out: list[dict] = []

    for tbl in model.get("tables", []):
        t_name = tbl.get("name", "").strip()
        if not t_name:
            continue

        cols  = _parse_columns(tbl, t_name)
        meas  = _parse_measures(tbl, t_name)
        parts = _parse_partitions(tbl)

        tables_out.append({
            "name":         t_name,
            "is_hidden":    bool(tbl.get("isHidden", False)),
            "is_internal":  _is_internal(t_name),
            "description":  tbl.get("description", ""),
            "columns":      cols,
            "measures":     meas,
            "partitions":   parts,
        })
        columns_out.extend(cols)
        measures_out.extend(meas)

    return tables_out, columns_out, measures_out


def _parse_relationships(model: dict) -> list[dict]:
    rels = []
    for r in model.get("relationships", []):
        rels.append({
            "from_table":       r.get("fromTable", ""),
            "from_column":      r.get("fromColumn", ""),
            "to_table":         r.get("toTable", ""),
            "to_column":        r.get("toColumn", ""),
            "from_cardinality": str(r.get("fromCardinality", "")).lower(),
            "to_cardinality":   str(r.get("toCardinality", "")).lower(),
            "cross_filter":     r.get("crossFilteringBehavior", "oneDirection"),
            "is_active":        not bool(r.get("isActive") is False),  # default True
        })
    return rels


def _parse_roles(model: dict) -> list[dict]:
    """Extract Row-Level Security role definitions."""
    roles = []
    for role in model.get("roles", []):
        table_permissions = []
        for tp in role.get("tablePermissions", []):
            table_permissions.append({
                "table":      tp.get("name", ""),
                "filter_dax": _normalise_expr(tp.get("filterExpression", "")),
            })
        roles.append({
            "name":              role.get("name", ""),
            "model_permission":  role.get("modelPermission", "read"),
            "table_permissions": table_permissions,
        })
    return roles


def _parse_data_sources(model: dict) -> list[dict]:
    """Extract data-source connection descriptors."""
    sources = []
    for ds in model.get("dataSources", []):
        sources.append({
            "name":            ds.get("name", ""),
            "type":            ds.get("type", ""),
            "connection_info": ds.get("connectionString", ds.get("connectionDetails", {})),
            "impersonation":   ds.get("impersonationMode", ""),
        })
    return sources


# ─────────────────────────────────────────────────────────────────────────────
# ZIP traversal — handles standard, enhanced (nested ZIP), and live-connect
# ─────────────────────────────────────────────────────────────────────────────

def _parse_model_from_zip(
    zf: zipfile.ZipFile,
    names_lower: dict[str, str],
    errors: list[str],
) -> tuple[dict | None, str]:
    """
    Attempt to locate and parse the model schema inside an open ZipFile.

    Returns (model_dict, format_tag) where format_tag is one of:
        "standard"    – DataModelSchema found directly in the outer ZIP
        "enhanced"    – DataModel entry was itself a nested ZIP
        "live_connect"– No embedded schema; report uses a live connection
        "unknown"     – Could not locate a parsable schema
    """
    # ── Path A: standard PBIX / all PBIT ─────────────────────────────────────
    candidates = _find_schema_candidates(names_lower)
    for cand in candidates:
        try:
            raw = zf.read(cand)
            schema = _decode_json(raw)
            if schema is None:
                errors.append(f"Could not decode schema entry '{cand}' with any known encoding.")
                continue
            model = schema.get("model", schema)
            if model.get("tables"):
                return model, "standard"
        except Exception as exc:
            errors.append(f"Error reading '{cand}': {exc}")

    # ── Path B: enhanced model — DataModel entry is a nested ZIP ─────────────
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
                inner_cands = _find_schema_candidates(inner_names)
                for cand in inner_cands:
                    try:
                        raw = inner_zf.read(cand)
                        schema = _decode_json(raw)
                        if schema is None:
                            continue
                        model = schema.get("model", schema)
                        if model.get("tables"):
                            return model, "enhanced"
                    except Exception as exc:
                        errors.append(f"Error reading inner '{cand}': {exc}")
        except zipfile.BadZipFile:
            errors.append("DataModel entry exists but is not a valid ZIP archive.")
        except Exception as exc:
            errors.append(f"Error opening nested DataModel archive: {exc}")

    # ── Path C: live connection? ──────────────────────────────────────────────
    if "connections" in names_lower:
        try:
            conn_raw = zf.read(names_lower["connections"])
            conn_data = _decode_json(conn_raw) or {}
            conn_list = conn_data.get("Connections", [])
            is_live = any(
                "live" in str(c.get("ConnectionType", "")).lower()
                or "xmla" in str(c.get("ConnectionType", "")).lower()
                for c in conn_list
            )
            if is_live:
                return None, "live_connect"
        except Exception as exc:
            errors.append(f"Could not inspect Connections entry: {exc}")

    return None, "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Report layout scanner — finds which measures are used in visuals / filters
# ─────────────────────────────────────────────────────────────────────────────

def _walk_measure_props(obj: Any) -> set[str]:
    """
    Recursively walk any parsed JSON structure (dict, list, nested JSON strings)
    and collect every Measure.Property value — i.e. every measure name referenced
    inside a SemanticQuery visual binding or filter expression.
    """
    found: set[str] = set()

    if isinstance(obj, dict):
        # Core pattern: {"Measure": {"Expression": {...}, "Property": "MeasureName"}}
        if "Measure" in obj and isinstance(obj.get("Measure"), dict):
            prop = obj["Measure"].get("Property")
            if prop and isinstance(prop, str):
                found.add(prop)
        # Also handle: {"Aggregation": {"Expression": {"Measure": {..., "Property": "X"}}}}
        for v in obj.values():
            found |= _walk_measure_props(v)

    elif isinstance(obj, list):
        for item in obj:
            found |= _walk_measure_props(item)

    elif isinstance(obj, str) and len(obj) > 1 and obj[0] in ('{', '['):
        # Nested JSON string — common in visualContainers (config, filters, query)
        try:
            found |= _walk_measure_props(json.loads(obj))
        except Exception:
            pass

    return found


def _extract_visual_refs(
    zf: zipfile.ZipFile,
    names_lower: dict[str, str],
    all_measure_names: set[str],
    errors: list[str],
) -> dict:
    """
    Parse Report/Layout to find which measures are used in visuals, filters,
    slicers, and bookmarks across every report page.

    Returns
    -------
    dict with keys:
        found          – bool, True if Report/Layout was present in the archive
        page_names     – ordered list of page display names
        page_count     – number of report pages
        visual_count   – total visual containers scanned
        visual_refs    – set[str] of canonical measure names used anywhere
        page_refs      – {page_display_name: set[measure_name]} breakdown
        report_filter_refs – set[str] of measures in report-level filters
    """
    empty = {
        "found":              False,
        "page_names":         [],
        "page_count":         0,
        "visual_count":       0,
        "visual_refs":        set(),
        "page_refs":          {},
        "report_filter_refs": set(),
    }

    layout_key = next(
        (real for lower, real in names_lower.items() if lower == "report/layout"),
        None,
    )
    if not layout_key:
        return empty  # .pbit template or live-connect — no layout blob

    try:
        raw = zf.read(layout_key)
        layout = _decode_json(raw)
        if not layout:
            errors.append("Report/Layout found but could not be decoded.")
            return empty
    except Exception as exc:
        errors.append(f"Error reading Report/Layout: {exc}")
        return empty

    # Case-insensitive lookup so we return canonical model measure names
    measure_ci: dict[str, str] = {n.lower(): n for n in all_measure_names}

    def _resolve(raw_props: set[str]) -> set[str]:
        resolved = set()
        for p in raw_props:
            canonical = measure_ci.get(p.lower())
            if canonical:
                resolved.add(canonical)
        return resolved

    all_refs: set[str] = set()
    page_refs: dict[str, set[str]] = {}
    page_names: list[str] = []
    visual_count = 0

    # Report-level filters
    report_filter_refs = _resolve(_walk_measure_props(layout.get("filters", "")))
    all_refs |= report_filter_refs

    for section in layout.get("sections", []):
        pname = (section.get("displayName") or section.get("name") or "Unknown Page").strip()
        page_names.append(pname)
        page_raw: set[str] = set()

        # Page-level filters
        page_raw |= _walk_measure_props(section.get("filters", ""))

        for vc in section.get("visualContainers", []):
            visual_count += 1
            # Every field that may contain SemanticQuery bindings
            for field in ("config", "filters", "query", "dataTransforms"):
                val = vc.get(field)
                if val:
                    page_raw |= _walk_measure_props(val)

        resolved_page = _resolve(page_raw)
        page_refs[pname] = resolved_page
        all_refs |= resolved_page

    return {
        "found":              True,
        "page_names":         page_names,
        "page_count":         len(page_names),
        "visual_count":       visual_count,
        "visual_refs":        all_refs,
        "page_refs":          page_refs,
        "report_filter_refs": report_filter_refs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Summary builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_summary(
    tables:        list[dict],
    columns:       list[dict],
    measures:      list[dict],
    relationships: list[dict],
    roles:         list[dict],
) -> dict:
    user_tables   = [t for t in tables   if not t["is_internal"]]
    hidden_tables = [t for t in user_tables if t["is_hidden"]]

    calc_columns = [c for c in columns if c.get("type") == "calculatedColumn" or c.get("expression")]
    hidden_meas  = [m for m in measures if m["is_hidden"]]
    no_desc_meas = [m for m in measures if not m["description"]]
    no_folder_meas = [m for m in measures if not m["display_folder"]]

    # Identify measures living inside fact/dimension tables (not in a dedicated measures table)
    dedicated_measure_tables = {t["name"] for t in user_tables if not t["columns"]}
    misplaced_meas = [
        m for m in measures
        if m["table"] not in dedicated_measure_tables
        and any(
            m["table"] == t["name"]
            for t in user_tables
            if t["columns"]  # table has data columns → it is a fact/dim table
        )
    ]

    return {
        "table_count":                    len(user_tables),
        "internal_table_count":           len(tables) - len(user_tables),
        "hidden_table_count":             len(hidden_tables),
        "column_count":                   len(columns),
        "calculated_column_count":        len(calc_columns),
        "measure_count":                  len(measures),
        "hidden_measure_count":           len(hidden_meas),
        "measures_without_description":   len(no_desc_meas),
        "measures_without_display_folder":len(no_folder_meas),
        "measures_in_fact_dim_tables":    len(misplaced_meas),
        "relationship_count":             len(relationships),
        "rls_role_count":                 len(roles),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def extract_pbit_metadata(file_path: "str | Path | bytes | io.BytesIO") -> dict:
    """
    Extract structured metadata from a Power BI Template (.pbit) or Report
    (.pbix) file.

    Parameters
    ----------
    file_path : str | Path | bytes | io.BytesIO
        Absolute or relative path to the .pbit / .pbix file,
        or raw bytes / BytesIO object of the file contents.

    Returns
    -------
    dict with keys:
        file          – resolved absolute path (str) or "<in-memory>"
        zip_contents  – all entry names in the archive
        model_format  – "standard" | "enhanced" | "live_connect" | "unknown"
        tables        – list of table dicts (with nested columns/measures/partitions)
        columns       – flat list of all column dicts (table name injected)
        measures      – flat list of all measure dicts (table name injected)
        relationships – list of relationship dicts
        roles         – list of RLS role dicts
        data_sources  – list of data-source dicts
        summary       – aggregate counts dict
        errors        – list of non-fatal warning strings

    Raises
    ------
    FileNotFoundError  – if the file does not exist
    ValueError         – if the file is not a valid zip archive
    """
    errors: list[str] = []

    # ── Resolve raw bytes from whatever input type was provided ───────────────
    if isinstance(file_path, (bytes, bytearray)):
        raw_bytes = bytes(file_path)
        resolved_name = "<in-memory>"
    elif isinstance(file_path, io.IOBase):
        raw_bytes = file_path.read()
        resolved_name = "<in-memory>"
    else:
        file_path = Path(file_path).resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        resolved_name = str(file_path)
        try:
            raw_bytes = file_path.read_bytes()
        except OSError as exc:
            raise OSError(f"Cannot read file '{file_path}': {exc}") from exc

    try:
        outer_zf_ctx = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"'{resolved_name}' is not a valid ZIP / PBIT archive.") from exc

    with outer_zf_ctx as zf:
        zip_contents   = zf.namelist()
        names_lower    = {n.lower(): n for n in zip_contents}

        # ── Locate and parse the model ────────────────────────────────────────
        model, model_format = _parse_model_from_zip(zf, names_lower, errors)

        if model is None:
            # Return an empty but well-shaped result
            return {
                "file":          resolved_name,
                "zip_contents":  zip_contents,
                "model_format":  model_format,
                "tables":        [],
                "columns":       [],
                "measures":      [],
                "relationships": [],
                "roles":         [],
                "data_sources":  [],
                "summary":       _build_summary([], [], [], [], []),
                "errors":        errors + [
                    f"No parsable model schema found (format detected: '{model_format}'). "
                    "The file may use a live DirectQuery / XMLA connection."
                ],
                "visual_refs":   _extract_visual_refs(zf, names_lower, set(), errors),
            }

        # ── Parse every section ───────────────────────────────────────────────
        tables, columns, measures = _parse_tables(model)
        relationships              = _parse_relationships(model)
        roles                      = _parse_roles(model)
        data_sources               = _parse_data_sources(model)
        summary                    = _build_summary(tables, columns, measures, relationships, roles)

        # ── Scan Report/Layout for visual measure references ──────────────────
        all_measure_names = {m["name"] for m in measures if m.get("name")}
        visual_refs_data  = _extract_visual_refs(zf, names_lower, all_measure_names, errors)

    return {
        "file":          resolved_name,
        "zip_contents":  zip_contents,
        "model_format":  model_format,
        "tables":        tables,
        "columns":       columns,
        "measures":      measures,
        "relationships": relationships,
        "roles":         roles,
        "data_sources":  data_sources,
        "summary":       summary,
        "errors":        errors,
        "visual_refs":   visual_refs_data,   # NEW: report layout scan results
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI convenience — run as `python pbit_extractor.py path/to/file.pbit`
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python pbit_extractor.py <path-to-pbit-or-pbix>")
        sys.exit(1)

    result = extract_pbit_metadata(sys.argv[1])

    print(f"\nFile      : {result['file']}")
    print(f"Format    : {result['model_format']}")
    print(f"\nSummary")
    print("-" * 40)
    for k, v in result["summary"].items():
        print(f"  {k:<40} {v}")

    print(f"\nTables ({result['summary']['table_count']} user-facing)")
    print("-" * 40)
    for t in result["tables"]:
        if t["is_internal"]:
            continue
        flag = " [hidden]" if t["is_hidden"] else ""
        print(f"  {t['name']}{flag}  —  {len(t['columns'])} cols, {len(t['measures'])} measures")

    print(f"\nMeasures ({result['summary']['measure_count']})")
    print("-" * 40)
    for m in result["measures"]:
        desc = f"  # {m['description']}" if m["description"] else ""
        folder = f"  [{m['display_folder']}]" if m["display_folder"] else ""
        print(f"  [{m['table']}] {m['name']}{folder}{desc}")

    if result["errors"]:
        print(f"\nWarnings ({len(result['errors'])})")
        print("-" * 40)
        for e in result["errors"]:
            print(f"  ⚠  {e}")
