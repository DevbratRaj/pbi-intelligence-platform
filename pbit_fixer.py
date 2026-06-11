"""
pbit_fixer.py
=============
Safe in-place rewrite of a Power BI Template (.pbit) file.

Two output modes:

    1. apply_safe_fixes(raw_bytes, fixes) -> bytes
       Patches DataModelSchema with:
         * descriptions          — adds/updates measure description text
         * display_folders       — adds/updates measure displayFolder
         * placement             — moves measures into a `_Measures` home table
                                   (creates `_Measures` if it does not exist)
       Returns a new .pbit byte stream the user can download and open in
       Power BI Desktop. NO renames are performed (those break visual bindings).

    2. generate_rename_script(renames) -> str
       Returns a Tabular Editor 2/3 C# script (.csx) that performs each rename
       and lets Tabular Editor handle dependent reference updates safely.

Why this design
---------------
* Renames are dangerous: every measure, visual binding, RLS rule and bookmark
  that references the old name must update too. Tabular Editor does this
  correctly via its dependency tree. We do NOT attempt it ourselves.
* Descriptions / displayFolder / table placement are purely metadata changes
  that do not affect DAX references — so they are safe to rewrite directly.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Iterable

# Same encoding order as pbit_extractor — utf-16-le is the most common .pbit format
_ENCODINGS = ("utf-8-sig", "utf-16-le", "utf-16", "utf-8", "utf-16-be", "latin-1")


# ─────────────────────────────────────────────────────────────────────────────
# Encoding helpers
# ─────────────────────────────────────────────────────────────────────────────

def _decode_with_detection(raw: bytes) -> tuple[dict, str]:
    """
    Decode raw schema bytes and remember which encoding succeeded — we re-use
    the same encoding when writing back so Power BI Desktop accepts the file.
    """
    for enc in _ENCODINGS:
        try:
            text = raw.decode(enc)
            stripped = text.lstrip("\ufeff").lstrip()
            if not stripped or stripped[0] not in ("{", "["):
                continue
            return json.loads(stripped), enc
        except Exception:
            continue
    raise ValueError("Could not decode DataModelSchema with any known encoding.")


def _encode_back(obj: dict, encoding: str) -> bytes:
    """
    Re-encode the patched schema using the SAME encoding that was used in the
    original file. Power BI Desktop will refuse the file if the encoding or
    BOM differs.
    """
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if encoding == "utf-8-sig":
        return ("\ufeff" + text).encode("utf-8")
    if encoding in ("utf-16-le", "utf-16"):
        # Power BI expects UTF-16-LE WITH a BOM
        return "\ufeff".encode("utf-16-le") + text.encode("utf-16-le")
    if encoding == "utf-16-be":
        return "\ufeff".encode("utf-16-be") + text.encode("utf-16-be")
    return text.encode(encoding)


# ─────────────────────────────────────────────────────────────────────────────
# Schema location helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_schema_entry(zf: zipfile.ZipFile) -> str | None:
    """Locate the DataModelSchema (or .bim) entry inside the outer .pbit ZIP."""
    for name in zf.namelist():
        lower = name.lower()
        if "datamodelschema" in lower or lower.endswith(".bim"):
            return name
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Core schema mutators
# ─────────────────────────────────────────────────────────────────────────────

def _find_table(model: dict, table_name: str) -> dict | None:
    for t in model.get("tables", []):
        if t.get("name") == table_name:
            return t
    return None


def _ensure_measures_table(model: dict, name: str = "_Measures") -> dict:
    """
    Create a `_Measures` home table if missing. Power BI expects a table with
    at least one column, so we add a hidden placeholder column.
    """
    existing = _find_table(model, name)
    if existing is not None:
        return existing

    new_table = {
        "name": name,
        "isHidden": False,
        "columns": [
            {
                "name":           "_placeholder",
                "dataType":       "string",
                "isHidden":       True,
                "type":           "calculated",
                "expression":     '""',
                "summarizeBy":    "none",
            }
        ],
        "partitions": [
            {
                "name":   f"{name}-partition",
                "mode":   "import",
                "source": {"type": "calculated", "expression": 'ROW("x", BLANK())'},
            }
        ],
        "measures": [],
    }
    model.setdefault("tables", []).append(new_table)
    return new_table


def _patch_model(
    model: dict,
    descriptions:    list[dict] | None = None,
    display_folders: list[dict] | None = None,
    placements:      list[dict] | None = None,
) -> dict:
    """
    Apply each category of safe fix to the model in place.

    Each list contains dicts with these shapes:
        descriptions:    [{"table", "measure", "description"}]
        display_folders: [{"table", "measure", "folder"}]
        placements:      [{"measure", "current_table", "target_table"}]

    Returns a counters dict so the UI can report what actually got applied.
    """
    counters = {
        "descriptions_applied":    0,
        "descriptions_skipped":    0,
        "folders_applied":         0,
        "folders_skipped":         0,
        "measures_moved":          0,
        "moves_skipped":           0,
        "measures_table_created":  False,
    }

    # ── 1. Descriptions ──────────────────────────────────────────────────────
    for d in (descriptions or []):
        tbl = _find_table(model, d["table"])
        if not tbl:
            counters["descriptions_skipped"] += 1
            continue
        for m in tbl.get("measures", []):
            if m.get("name") == d["measure"]:
                m["description"] = d["description"]
                counters["descriptions_applied"] += 1
                break
        else:
            counters["descriptions_skipped"] += 1

    # ── 2. Display folders ───────────────────────────────────────────────────
    for f in (display_folders or []):
        tbl = _find_table(model, f["table"])
        if not tbl:
            counters["folders_skipped"] += 1
            continue
        for m in tbl.get("measures", []):
            if m.get("name") == f["measure"]:
                m["displayFolder"] = f["folder"]
                counters["folders_applied"] += 1
                break
        else:
            counters["folders_skipped"] += 1

    # ── 3. Placement (move measure to target table) ──────────────────────────
    if placements:
        # Make sure the destination exists ONCE
        target_names = {p["target_table"] for p in placements}
        for tgt in target_names:
            if _find_table(model, tgt) is None:
                _ensure_measures_table(model, tgt)
                counters["measures_table_created"] = True

        for p in placements:
            src = _find_table(model, p["current_table"])
            tgt = _find_table(model, p["target_table"])
            if not src or not tgt:
                counters["moves_skipped"] += 1
                continue
            measure_obj = None
            for i, m in enumerate(src.get("measures", [])):
                if m.get("name") == p["measure"]:
                    measure_obj = src["measures"].pop(i)
                    break
            if measure_obj is None:
                counters["moves_skipped"] += 1
                continue
            tgt.setdefault("measures", []).append(measure_obj)
            counters["measures_moved"] += 1

    return counters


# ─────────────────────────────────────────────────────────────────────────────
# OPTION B — Public API: rewrite a .pbit with safe fixes
# ─────────────────────────────────────────────────────────────────────────────

def apply_safe_fixes(
    raw_bytes:       bytes,
    descriptions:    list[dict] | None = None,
    display_folders: list[dict] | None = None,
    placements:      list[dict] | None = None,
) -> tuple[bytes, dict]:
    """
    Returns (new_pbit_bytes, counters).

    Safe to apply:
        * descriptions   — append / overwrite measure description text
        * display_folders— set displayFolder string on a measure
        * placements     — move measure into another (or new) home table

    NOT supported (would break references):
        * renames        — use generate_rename_script() instead

    Raises:
        ValueError      — if the schema cannot be located or decoded
    """
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as in_zf:
        schema_entry = _find_schema_entry(in_zf)
        if schema_entry is None:
            raise ValueError(
                "No DataModelSchema entry found in this archive. "
                "Enhanced-format .pbix or live-connect templates are not supported "
                "by the safe fixer — use a standard .pbit / .pbix instead."
            )

        # Decode schema, remember encoding for round-trip
        raw_schema = in_zf.read(schema_entry)
        schema, encoding = _decode_with_detection(raw_schema)
        model = schema.get("model", schema)

        # Apply patches in place
        counters = _patch_model(
            model,
            descriptions=descriptions,
            display_folders=display_folders,
            placements=placements,
        )

        # Re-encode using the original encoding so Power BI accepts it
        new_schema_bytes = _encode_back(schema, encoding)

        # Write a NEW zip — copy every entry verbatim except the schema
        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out_zf:
            for item in in_zf.infolist():
                if item.filename == schema_entry:
                    out_zf.writestr(item, new_schema_bytes)
                else:
                    out_zf.writestr(item, in_zf.read(item.filename))

    return out_buf.getvalue(), counters


# ─────────────────────────────────────────────────────────────────────────────
# OPTION C — Tabular Editor rename script generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_rename_script(renames: Iterable[dict]) -> str:
    """
    Build a Tabular Editor 2/3 C# script that renames measures safely.

    Each rename dict is:  {"table", "current_name", "new_name"}

    Tabular Editor automatically updates every dependent measure reference,
    visual binding, calculation group reference, and bookmark when you rename
    via `.Name = "..."`, so this is the safe way to do bulk renames.

    Usage shown to the end-user:
        1. Open your .pbix in Power BI Desktop
        2. External Tools → Tabular Editor (free at tabulareditor.com)
        3. File → Open Script... → select the downloaded .csx
        4. Press F5 (Run) — preview the changes in the script log
        5. File → Save (Ctrl+S) — writes back to Power BI Desktop
        6. Save the .pbix in Power BI Desktop
    """
    lines = [
        "// ─────────────────────────────────────────────────────────────",
        "// Generated by PBI Intelligence Platform — Governance Engine",
        "// Tabular Editor C# Script — Bulk measure rename",
        "// ─────────────────────────────────────────────────────────────",
        "// HOW TO RUN:",
        "//   1. Open your .pbix in Power BI Desktop",
        "//   2. External Tools → Tabular Editor",
        "//   3. File → Open Script… → select this .csx",
        "//   4. Press F5 to run",
        "//   5. File → Save (Ctrl+S) writes changes back to Power BI",
        "// ─────────────────────────────────────────────────────────────",
        "",
        "int renamed = 0;",
        "int skipped = 0;",
        "",
    ]

    for r in renames:
        table = r["table"].replace('"', '\\"')
        old   = r["current_name"].replace('"', '\\"')
        new   = r["new_name"].replace('"', '\\"')
        lines.append(
            f'// {old}  →  {new}  (in table: {table})'
        )
        lines.append('try {')
        lines.append(f'    var m = Model.Tables["{table}"].Measures["{old}"];')
        lines.append(f'    m.Name = "{new}";')
        lines.append('    renamed++;')
        lines.append('} catch (System.Exception ex) {')
        lines.append(
            f'    Output("Skipped {old}: " + ex.Message);'
        )
        lines.append('    skipped++;')
        lines.append('}')
        lines.append('')

    lines.append('Output(string.Format("Done — {0} renamed, {1} skipped.", renamed, skipped));')
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI smoke-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python pbit_fixer.py <path-to-pbit>")
        sys.exit(1)

    src = sys.argv[1]
    with open(src, "rb") as f:
        raw = f.read()

    # Demo: add a description to the first measure we find
    from pbit_extractor import extract_pbit_metadata
    meta = extract_pbit_metadata(src)
    if not meta["measures"]:
        print("No measures in file."); sys.exit(0)

    first = meta["measures"][0]
    out_bytes, counters = apply_safe_fixes(
        raw,
        descriptions=[{
            "table":   first["table"],
            "measure": first["name"],
            "description": "Patched by pbit_fixer smoke-test.",
        }],
        display_folders=[{
            "table":   first["table"],
            "measure": first["name"],
            "folder":  "Patched",
        }],
    )
    out_path = src.replace(".pbit", ".patched.pbit").replace(".pbix", ".patched.pbix")
    with open(out_path, "wb") as f:
        f.write(out_bytes)

    print(f"Wrote {out_path}")
    print("Counters:", counters)

    # Round-trip check
    rt = extract_pbit_metadata(out_path)
    for m in rt["measures"]:
        if m["name"] == first["name"] and m["table"] == first["table"]:
            print(f"  description: {m['description']!r}")
            print(f"  displayFolder: {m['display_folder']!r}")
            break

    # Rename script demo
    script = generate_rename_script([
        {"table": first["table"], "current_name": first["name"], "new_name": first["name"] + "Renamed"},
    ])
    print("\n--- Generated rename script ---")
    print(script)
