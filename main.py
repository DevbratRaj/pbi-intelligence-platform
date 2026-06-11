import time
import streamlit as st
import zipfile
import io
import json
import re
import subprocess
import tempfile
import shutil
import urllib.request
import pandas as pd
from pathlib import Path

st.set_page_config(
    page_title="PBI Intelligence Platform",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Global styles ─────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
        /* Sidebar */
        [data-testid="stSidebar"] { background-color: #0d1b2a; }
        [data-testid="stSidebar"] * { color: #e0e8f0; }
        [data-testid="stSidebar"] h1 {
            color: #ffffff; font-size: 1.4rem; font-weight: 700;
            padding-bottom: 0.5rem; border-bottom: 1px solid #1e3a5f;
        }

        /* Landing hero */
        .hero-wrap {
            max-width: 680px;
            margin: 0 auto;
            text-align: center;
            padding: 3rem 0 1.5rem;
        }
        .hero-badge {
            display: inline-block;
            background: #eef2ff;
            color: #4338ca;
            border-radius: 999px;
            font-size: 0.78rem;
            font-weight: 600;
            letter-spacing: 0.05em;
            padding: 4px 14px;
            margin-bottom: 1.1rem;
        }
        .hero-title {
            font-size: 2.6rem;
            font-weight: 800;
            color: #0f172a;
            line-height: 1.15;
            margin: 0 0 0.65rem;
        }
        .hero-sub {
            font-size: 1.05rem;
            color: #475569;
            margin: 0 0 2rem;
            line-height: 1.6;
        }

        /* Upload card */
        .upload-card {
            background: #ffffff;
            border: 1.5px solid #e2e8f0;
            border-radius: 14px;
            padding: 2rem 2.2rem;
            box-shadow: 0 2px 12px rgba(0,0,0,0.05);
            max-width: 680px;
            margin: 0 auto 1.4rem;
        }
        .section-label {
            font-size: 0.78rem;
            font-weight: 700;
            color: #64748b;
            letter-spacing: 0.07em;
            text-transform: uppercase;
            margin-bottom: 0.35rem;
        }
        .divider-light {
            border: none;
            border-top: 1px solid #f1f5f9;
            margin: 1.4rem 0;
        }

        /* Feature pills */
        .feature-row {
            display: flex;
            justify-content: center;
            gap: 10px;
            flex-wrap: wrap;
            margin-top: 2rem;
        }
        .feature-pill {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 999px;
            padding: 5px 14px;
            font-size: 0.8rem;
            color: #475569;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("<h1>PBI Intelligence Platform</h1>", unsafe_allow_html=True)
    st.markdown("---")

# ── Hero ──────────────────────────────────────────────────────────────────────
_, hero_col, _ = st.columns([1, 2.4, 1])
with hero_col:
    st.markdown(
        """
        <div class="hero-wrap">
          <div class="hero-badge">⚡ Power BI Audit Tool</div>
          <div class="hero-title">PBI Intelligence Platform</div>
          <div class="hero-sub">
            Upload a Power BI report to detect performance anti-patterns,
            unused measures, data-quality issues, and governance gaps — instantly.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ── Upload card ───────────────────────────────────────────────────────────────
_, card_col, _ = st.columns([1, 2.4, 1])
with card_col:
    with st.container(border=True):
        st.markdown(
            '<p class="section-label">📂 Report File</p>',
            unsafe_allow_html=True,
        )
        uploaded_files = st.file_uploader(
            "Upload .pbix or .pbit reports",
            type=["pbix", "pbit"],
            accept_multiple_files=True,
            label_visibility="collapsed",
            help="Upload one or more Power BI Desktop files (.pbix) or templates (.pbit). "
                 "Uploading multiple reports from the same dataset gives more accurate unused-measure detection.",
        )

        if not uploaded_files:
            uploaded_files = []

        if uploaded_files:
            for f in uploaded_files:
                size_kb = round(f.size / 1024, 1)
                st.success(f"✅ **{f.name}** — {size_kb} KB ready to scan")

        st.markdown('<hr class="divider-light">', unsafe_allow_html=True)

        st.markdown(
            '<p class="section-label">🔗 Workspace Connection (optional)</p>',
            unsafe_allow_html=True,
        )
        workspace_url = st.text_input(
            "Power BI workspace URL",
            placeholder="https://app.powerbi.com/groups/your-workspace-id",
            label_visibility="collapsed",
        )

        has_input = bool(uploaded_files) or bool(workspace_url.strip())
        start_clicked = st.button(
            "🚀 Start Scan",
            use_container_width=True,
            disabled=not has_input,
            type="primary",
        )
        if not has_input:
            st.caption("Upload a file or enter a workspace URL to enable the scan.")

# ── Feature pills ─────────────────────────────────────────────────────────────
_, pills_col, _ = st.columns([1, 2.4, 1])
with pills_col:
    st.markdown(
        """
        <div class="feature-row">
          <span class="feature-pill">⚡ Performance Engine</span>
          <span class="feature-pill">🔍 Unused Measures</span>
          <span class="feature-pill">🧹 Data Quality</span>
          <span class="feature-pill">📐 DAX Analysis</span>
          <span class="feature-pill">⚠️ Governance Audit</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown("<br>", unsafe_allow_html=True)

# ── Initialise session-state flags ────────────────────────────────────────────
if "scan_complete" not in st.session_state:
    st.session_state["scan_complete"] = False
if "scan_files" not in st.session_state:
    st.session_state["scan_files"] = []

# Trigger: mark that a new scan should run when Start Scan is clicked
if start_clicked:
    st.session_state["scan_complete"] = False   # reset so progress re-runs
    st.session_state["scan_files"] = [f.name for f in uploaded_files]

# Gate: nothing below renders until user provides input
if not (start_clicked or uploaded_files or st.session_state["scan_complete"]):
    st.stop()

# ── Progress scanner ──────────────────────────────────────────────────────────
if start_clicked and not st.session_state["scan_complete"]:
    st.markdown("---")
    _, prog_col, _ = st.columns([1, 2.4, 1])
    with prog_col:
        st.markdown(
            """
            <div style="text-align:center;margin-bottom:0.8rem">
              <span style="font-size:1.25rem;font-weight:700;color:#0f172a">
                🔍 Scanning Report…
              </span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        progress_bar = st.progress(0)
        status_box   = st.empty()

        _STEPS = [
            ("🔗 Initialising scan…",          8),
            ("📐 Running Lineage Engine…",      20),
            ("⚡ Analysing Performance…",       38),
            ("🧹 Checking Data Quality…",       54),
            ("⚠️ Reviewing Governance…",        68),
            ("🔒 Scanning Security…",           82),
            ("📊 Finalising Scorecard…",       100),
        ]

        for _label, _pct in _STEPS:
            status_box.markdown(
                f"""
                <div style="
                    background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                    padding:10px 16px;font-size:0.92rem;color:#334155;margin-bottom:6px;
                    display:flex;align-items:center;gap:8px">
                  <span>{_label}</span>
                  <span style="margin-left:auto;color:#94a3b8;font-size:0.8rem">{_pct}%</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            progress_bar.progress(_pct)
            time.sleep(0.9)

        status_box.empty()
        st.session_state["scan_complete"] = True
        st.rerun()

# ── Post-scan success banner + health scorecard ───────────────────────────────
if st.session_state.get("scan_complete"):
    st.markdown("---")
    _, banner_col, _ = st.columns([1, 2.4, 1])
    with banner_col:
        scanned = st.session_state.get("scan_files", [])
        files_str = ", ".join(f"**{n}**" for n in scanned) if scanned else "your report"
        st.success(
            f"✅ **Scan complete!** Results for {files_str} are shown below. "
            "Use the sidebar to navigate to each engine."
        )
        st.markdown("<br>", unsafe_allow_html=True)

    # ── Mini Health Scorecard ─────────────────────────────────────────────────
    _score_raw = st.session_state.get("pbi_file_bytes")
    if _score_raw:
        import zipfile as _zf, json as _js, re as _re

        def _hs_decode(raw: bytes) -> dict | None:
            for enc in ("utf-8-sig", "utf-16-le", "utf-16"):
                try:
                    return _js.loads(raw.decode(enc))
                except Exception:
                    continue
            return None

        def _hs_extract(raw_bytes: bytes):
            """Return (measures, tables, relationships) for the scorecard fallback."""
            measures, tables, relationships = [], [], []
            try:
                with _zf.ZipFile(__import__("io").BytesIO(raw_bytes)) as z:
                    names_lower = {n.lower(): n for n in z.namelist()}
                    schema_file = next(
                        (real for lower, real in names_lower.items()
                         if "datamodelschema" in lower or lower.endswith(".bim")),
                        None,
                    )
                    if schema_file:
                        schema = _hs_decode(z.read(schema_file))
                        if schema:
                            model = schema.get("model", schema)
                            for tbl in model.get("tables", []):
                                t_name = tbl.get("name", "")
                                if not t_name or any(
                                    t_name.startswith(p) for p in
                                    ("$", "LocalDateTable", "DateTableTemplate")
                                ):
                                    continue
                                for m in tbl.get("measures", []):
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
                                tables.append(tbl)
                            relationships = model.get("relationships", [])
            except Exception:
                pass
            return measures, tables, relationships

        def _grade(s: int) -> tuple[str, str, str]:
            if s >= 90: return "Excellent", "#1b5e20", "#e8f5e9"
            if s >= 75: return "Good", "#1565c0", "#e3f2fd"
            if s >= 60: return "Needs Attention", "#e65100", "#fff3e0"
            return "Critical", "#b71c1c", "#fdecea"

        # ── Use real engine scores if the user has visited those pages ──────
        # Fallback to heuristics only when an engine hasn't been visited yet.
        _perf_score_real = st.session_state.get("perf_score")
        _dq_score_real   = st.session_state.get("dq_score")
        _gov_score_real  = st.session_state.get("gov_score")

        if _perf_score_real is None or _dq_score_real is None or _gov_score_real is None:
            # At least one engine not yet visited — compute heuristics for missing ones
            _sc_measures, _sc_tables, _sc_rels = _hs_extract(_score_raw)
            _total_m = len(_sc_measures)

            if _perf_score_real is None:
                _PERF_RE = _re.compile(
                    r"\b(SUMX|AVERAGEX|MAXX|MINX)\s*\(.*\b(SUMX|AVERAGEX|MAXX|MINX)\b"
                    r"|FILTER\s*\(\s*(?!ALL\b|VALUES\b|CALCULATETABLE\b)'?[A-Za-z]"
                    r"|IFERROR\s*\(.*\b(CALCULATE|FILTER|SUMX)\b",
                    _re.IGNORECASE | _re.DOTALL,
                )
                _perf_issues_n = sum(
                    1 for m in _sc_measures
                    if _PERF_RE.search(_re.sub(r"--[^\r\n]*", "", m["expression"]))
                )
                _perf_score_real = max(0, 100 - min(_perf_issues_n * 8, 60))

            if _dq_score_real is None:
                _no_desc_h = sum(1 for m in _sc_measures if not str(m.get("description", "")).strip())
                _m2m = sum(
                    1 for r in _sc_rels
                    if str(r.get("fromCardinality", "")).lower() in ("many", "0")
                    and str(r.get("toCardinality", "")).lower() in ("many", "0")
                )
                _dq_ded = min((_no_desc_h / _total_m * 20) if _total_m else 0, 15) + _m2m * 10
                _dq_score_real = max(0, int(100 - _dq_ded))

            if _gov_score_real is None:
                _no_desc_g  = sum(1 for m in _sc_measures if not str(m.get("description", "")).strip())
                _no_folder  = sum(1 for m in _sc_measures if not str(m.get("displayFolder", "")).strip())
                _gov_ded = min(((_no_desc_g + _no_folder) / max(_total_m * 2, 1)) * 40, 40)
                _gov_score_real = max(0, int(100 - _gov_ded))

        _perf_score = _perf_score_real
        _dq_score   = _dq_score_real
        _gov_score  = _gov_score_real

        # Caption: show which scores are exact vs estimated
        _perf_src = "exact" if st.session_state.get("perf_score") is not None else "estimated"
        _dq_src   = "exact" if st.session_state.get("dq_score")   is not None else "estimated"
        _gov_src  = "exact" if st.session_state.get("gov_score")   is not None else "estimated"

        st.markdown("### 🏥 Health Scorecard")
        _sc1, _sc2, _sc3 = st.columns(3)
        for _col, _label, _score, _src in [
            (_sc1, "⚡ Performance", _perf_score, _perf_src),
            (_sc2, "🧹 Data Quality", _dq_score,  _dq_src),
            (_sc3, "📋 Governance",  _gov_score,  _gov_src),
        ]:
            _g, _c, _bg = _grade(_score)
            _src_badge = (
                '<span style="font-size:0.65rem;color:#2e7d32;font-weight:600">✔ exact</span>'
                if _src == "exact" else
                '<span style="font-size:0.65rem;color:#888">~ estimated</span>'
            )
            with _col:
                st.markdown(
                    f'<div style="background:{_bg};border:1.5px solid {_c};border-radius:10px;'
                    f'padding:14px 10px;text-align:center">'
                    f'<div style="font-size:0.85rem;color:#555;font-weight:600">{_label}</div>'
                    f'<div style="font-size:2.2rem;font-weight:800;color:{_c};line-height:1.1">{_score}</div>'
                    f'<div style="font-size:0.75rem;color:{_c};font-weight:600">{_g}</div>'
                    f'<div style="margin-top:4px">{_src_badge}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        st.caption(
            "✔ exact = score was computed by visiting that engine page  ·  "
            "~ estimated = engine not yet visited, based on quick heuristic"
        )
        st.markdown("<br>", unsafe_allow_html=True)

st.markdown("---")


# ---------------------------------------------------------------------------
# Helpers — DataModelSchema  (used by both .pbix fallback and .pbit)
# ---------------------------------------------------------------------------

HIDDEN_TABLE_PREFIXES = ("$", "LocalDateTable", "DateTableTemplate")


def _is_hidden_table(name: str) -> bool:
    return any(name.startswith(p) for p in HIDDEN_TABLE_PREFIXES)


def _extract_col_expr(c: dict) -> str:
    """Return a column's DAX expression string (handles list or str)."""
    expr = c.get("expression", "")
    if isinstance(expr, list):
        expr = "\n".join(expr)
    return expr.strip()


def _is_calculated_column(c: dict) -> bool:
    """Return True if this column entry is a calculated column."""
    return (
        c.get("type") == "calculatedColumn"
        or bool(_extract_col_expr(c))
    )


def _parse_full_schema(schema: dict) -> tuple[list[dict], list[dict]]:
    """
    Parse a DataModelSchema dict and return:
      tables  — [{name, columns:[{Name, Data Type}], calc_columns:[{Name, Data Type, DAX Expression}]}]
      measures — [{Measure Name, Table, DAX Expression}]
    Hidden tables (system tables) are excluded.
    """
    tables_out: list[dict] = []
    measures_out: list[dict] = []
    model = schema.get("model", schema)
    for table in model.get("tables", []):
        t_name = table.get("name", "")
        if not t_name or _is_hidden_table(t_name):
            continue
        cols = []
        calc_cols = []
        for c in table.get("columns", []):
            name = c.get("name", "")
            if not name or name.startswith("RowNumber"):
                continue
            if _is_calculated_column(c):
                calc_cols.append({
                    "Name": name,
                    "Data Type": c.get("dataType", "unknown"),
                    "DAX Expression": _extract_col_expr(c),
                })
            else:
                cols.append({
                    "Name": name,
                    "Data Type": c.get("dataType", "unknown"),
                })
        tables_out.append({"name": t_name, "columns": cols, "calc_columns": calc_cols})
        for m in table.get("measures", []):
            expr = m.get("expression", "")
            if isinstance(expr, list):
                expr = "\n".join(expr)
            measures_out.append(
                {
                    "Measure Name": m.get("name", ""),
                    "Table": t_name,
                    "DAX Expression": expr.strip(),
                    "description": m.get("description", ""),
                    "displayFolder": m.get("displayFolder", ""),
                }
            )
    return tables_out, measures_out


def _tables_from_json(schema: dict) -> dict[str, list[str]]:
    """Lightweight extractor — returns {table_name: [column_names]} only."""
    tables, _ = _parse_full_schema(schema)
    return {t["name"]: [c["Name"] for c in t["columns"]] for t in tables}


def extract_schema(zf: zipfile.ZipFile) -> dict[str, list[str]]:
    """Extract table→columns from DataModelSchema (JSON) if present in a .pbix."""
    names_lower = {n.lower(): n for n in zf.namelist()}
    candidates = [
        real
        for lower, real in names_lower.items()
        if "datamodelschema" in lower or lower.endswith(".bim")
    ]
    _ENCODINGS = ("utf-8-sig", "utf-16-le", "utf-16", "utf-8", "utf-16-be", "latin-1")
    for candidate in candidates:
        try:
            raw = zf.read(candidate)
            schema = None
            for encoding in _ENCODINGS:
                try:
                    text = raw.decode(encoding)
                    stripped = text.lstrip("\ufeff").lstrip()
                    if stripped and stripped[0] not in ("{", "["):
                        continue
                    schema = json.loads(text)
                    break
                except Exception:
                    continue
            if schema is None:
                continue
            tables = _tables_from_json(schema)
            if tables:
                return tables
        except Exception:
            pass
    return {}


def parse_pbit_schema(zf: zipfile.ZipFile) -> tuple[list[dict], list[dict]]:
    """
    Parse a .pbit file's DataModelSchema entry.
    Returns (tables, measures) as produced by _parse_full_schema.
    DataModelSchema in .pbit files is UTF-16-LE encoded JSON.
    """
    names_lower = {n.lower(): n for n in zf.namelist()}
    candidates = [
        real
        for lower, real in names_lower.items()
        if "datamodelschema" in lower or lower.endswith(".bim")
    ]
    _ENCODINGS = ("utf-8-sig", "utf-16-le", "utf-16", "utf-8", "utf-16-be", "latin-1")
    for candidate in candidates:
        try:
            raw = zf.read(candidate)
            schema = None
            for encoding in _ENCODINGS:
                try:
                    text = raw.decode(encoding)
                    stripped = text.lstrip("\ufeff").lstrip()
                    if stripped and stripped[0] not in ("{", "["):
                        continue
                    schema = json.loads(text)
                    break
                except Exception:
                    continue
            if schema is None:
                continue
            tables, measures = _parse_full_schema(schema)
            if tables or measures:
                return tables, measures
        except Exception:
            pass
    return [], []


# ---------------------------------------------------------------------------
# Helpers — pbi-tools
# ---------------------------------------------------------------------------

PBITOOLS_DIR = Path(__file__).parent / ".pbi-tools"
_HIDDEN_PREFIXES = ("$", "LocalDateTable", "DateTableTemplate")

# The exe may be named pbi-tools.exe or pbi-tools.core.exe depending on build
_PBITOOLS_EXE_CANDIDATES = ["pbi-tools.exe", "pbi-tools.core.exe"]


def _find_pbitools() -> Path | None:
    """Return path to pbi-tools exe if available locally or on PATH."""
    for name in _PBITOOLS_EXE_CANDIDATES:
        candidate = PBITOOLS_DIR / name
        if candidate.exists():
            return candidate
    for name in _PBITOOLS_EXE_CANDIDATES:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _download_pbitools() -> tuple[bool, str]:
    """Download the latest pbi-tools Windows release from GitHub."""
    api = "https://api.github.com/repos/pbi-tools/pbi-tools/releases/latest"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "pbi-platform"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            release = json.loads(resp.read())

        asset = next(
            (
                a for a in release.get("assets", [])
                if a["name"].endswith(".zip")
                and ("win" in a["name"].lower() or "x64" in a["name"].lower())
            ),
            None,
        )
        if not asset:
            return False, "Could not find a Windows .zip asset in the latest release."

        PBITOOLS_DIR.mkdir(parents=True, exist_ok=True)
        zip_path = PBITOOLS_DIR / "pbi-tools.zip"
        urllib.request.urlretrieve(asset["browser_download_url"], zip_path)

        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                        fname = member.lower()
                        if fname.endswith(".exe") and "pbi-tools" in fname:
                            data = zf.read(member)
                            dest = PBITOOLS_DIR / Path(member).name
                            dest.write_bytes(data)
        if _find_pbitools() is not None:
            return True, f"pbi-tools downloaded from {asset['name']}."
        return False, "pbi-tools exe not found inside the downloaded archive."
    except Exception as exc:
        return False, f"Download failed: {exc}"


def _parse_table_file(data: dict) -> dict:
    """Parse a single table JSON (pbi-tools output) into a clean dict."""
    name = data.get("name", "Unknown")
    columns = []
    calc_columns = []
    for c in data.get("columns", []):
        col_name = c.get("name", "")
        if not col_name or col_name.startswith("RowNumber"):
            continue
        if _is_calculated_column(c):
            calc_columns.append({
                "Name": col_name,
                "Data Type": c.get("dataType", "unknown"),
                "DAX Expression": _extract_col_expr(c),
            })
        else:
            columns.append({"Name": col_name, "Data Type": c.get("dataType", "unknown")})
    measures = []
    for m in data.get("measures", []):
        expr = m.get("expression", "")
        if isinstance(expr, list):
            expr = "\n".join(expr)
        measures.append({"name": m.get("name", ""), "expression": expr.strip()})
    return {"name": name, "columns": columns, "calc_columns": calc_columns, "measures": measures}


def _scan_tables_dir(tables_dir: Path) -> list[dict]:
    """Recursively find and parse table JSON files in a pbi-tools output folder."""
    results = []
    if not tables_dir.exists():
        return results
    for item in sorted(tables_dir.iterdir()):
        try:
            if item.is_file() and item.suffix == ".json":
                data = json.loads(item.read_text(encoding="utf-8-sig"))
                t = _parse_table_file(data)
            elif item.is_dir():
                # pbi-tools may put table.json inside a per-table folder
                candidate = item / "table.json"
                if not candidate.exists():
                    candidate = item / f"{item.name}.json"
                if not candidate.exists():
                    continue
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
                t = _parse_table_file(data)
            else:
                continue
            if not any(t["name"].startswith(p) for p in _HIDDEN_PREFIXES):
                results.append(t)
        except Exception:
            pass
    return results


def _parse_pbitools_output(extract_root: Path) -> list[dict]:
    """Find Model/tables/ anywhere under extract_root and parse it."""
    # Try direct path first
    tables_dir = extract_root / "Model" / "tables"
    tables = _scan_tables_dir(tables_dir)
    if tables:
        return tables
    # pbi-tools sometimes creates a sub-folder named after the .pbix file
    for sub in extract_root.iterdir():
        if sub.is_dir():
            tables = _scan_tables_dir(sub / "Model" / "tables")
            if tables:
                return tables
    return []


def _run_pbitools_extract(exe: Path, pbix_path: Path, out_dir: Path) -> subprocess.CompletedProcess:
    """Try both known pbi-tools extract command signatures."""
    # Syntax 1 (1.x): pbi-tools extract file.pbix -extractFolder dir
    result = subprocess.run(
        [str(exe), "extract", str(pbix_path), "-extractFolder", str(out_dir)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode == 0:
        return result
    # Syntax 2 (older): pbi-tools extract -pbixPath file.pbix -extractFolder dir
    result2 = subprocess.run(
        [str(exe), "extract", "-pbixPath", str(pbix_path), "-extractFolder", str(out_dir)],
        capture_output=True, text=True, timeout=120,
    )
    return result2 if result2.returncode == 0 else result


# ---------------------------------------------------------------------------
# Helpers — Report/Layout
# ---------------------------------------------------------------------------

def _clean_query_ref(ref: str) -> str:
    """Strip surrounding quotes from table name: 'My Table'[Col] → My Table[Col]."""
    return re.sub(r"'([^']+)'\[", r"\1[", ref.strip())


def _fields_from_single_visual(sv: dict) -> set[str]:
    """
    Extract field names from a singleVisual dict using two strategies:

    1. projections  — dict of role → [{queryRef: "Table[Col]"}, ...]
    2. prototypeQuery → Select — list of items with:
         • NativeReferenceName  (plain string label)
         • expression → Column/Measure → {Expression→SourceRef→Entity, Property}
    """
    fields: set[str] = set()

    # ── Strategy 1: projections ──────────────────────────────────────────
    projections = sv.get("projections", {})
    if isinstance(projections, dict):
        for role_items in projections.values():
            if not isinstance(role_items, list):
                continue
            for item in role_items:
                qr = item.get("queryRef", "")
                if qr:
                    fields.add(_clean_query_ref(qr))

    # ── Strategy 2: prototypeQuery → Select ─────────────────────────────
    select_items = sv.get("prototypeQuery", {}).get("Select", [])
    for sel in select_items:
        # 2a. NativeReferenceName is often "Table[Column]" already
        native = sel.get("NativeReferenceName", "")
        if native and "[" in native:
            fields.add(_clean_query_ref(native))

        # 2b. Drill into expression → Column or Measure
        expr = sel.get("expression", sel.get("Expression", {}))
        for kind in ("Column", "Measure", "HierarchyLevel"):
            node = expr.get(kind, {})
            if not isinstance(node, dict):
                continue
            entity = node.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
            prop = node.get("Property", "")
            if entity and prop:
                fields.add(f"{entity}[{prop}]")

    return fields


def _extract_visual_title(config: dict, container: dict) -> str:
    """
    Extract the display title of a visual. Checks in order:
      1. singleVisual → vcObjects → title → [0] → properties → text → expr → Literal → Value
      2. singleVisual → vcObjects → title → [0] → properties → text → expr → ResourcePackageItem → name
      3. top-level 'name' field on the container dict
    """
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
    """
    Return [{"Field": "Table[Col]", "Role": "values"}, ...] from a singleVisual dict.

    Sources (in order):
      1. projections dict — role → [{queryRef}]
      2. prototypeQuery.Select[] top-level Measure/Column with alias→Entity via from_map
      3. "Name" dot-notation fallback per Select item
    """
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

        # 1. NativeReferenceName (legacy format)
        native = sel.get("NativeReferenceName", "")
        if native and "[" in native:
            field = _clean_query_ref(native)

        # 2. Top-level Measure/Column (confirmed real-file structure)
        if not field:
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

        # 3. Wrapped expression.Measure/Column (older PBIX format compatibility)
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

        # 4. "Name" dot-notation fallback  "TABLE.FieldName" → TABLE[FieldName]
        if not field:
            name_fb = sel.get("Name", "")
            if name_fb:
                nq = _clean_query_ref(name_fb)
                if "[" in nq:
                    field = nq

        if field and field not in seen:
            seen.add(field)
            rows.append({"Field": field, "Role": "query"})

    return rows


def _fields_from_single_visual(sv: dict) -> set[str]:
    """Return a flat set of field references (no role info)."""
    return {row["Field"] for row in _fields_with_roles(sv)}


def parse_layout(zf: zipfile.ZipFile) -> list[dict]:
    """
    Parse Report/Layout and return a list of pages with title, type, and
    role-tagged fields per visual.
    """
    try:
        raw = zf.read("Report/Layout")
    except KeyError:
        return []

    try:
        text = raw.decode("utf-16-le")
        layout = json.loads(text)
    except Exception:
        return []

    pages_out = []
    for section in layout.get("sections", []):
        page_name = section.get("displayName") or section.get("name", "Unknown Page")
        visuals_out = []

        for container in section.get("visualContainers", []):
            config_str = container.get("config", "{}")
            try:
                config = json.loads(config_str)
            except Exception:
                config = {}

            single_visual = config.get("singleVisual", {})

            visual_type = single_visual.get("visualType") or config.get("vcObjects", {})
            if isinstance(visual_type, dict):
                visual_type = None
            visual_type = visual_type or "unknown"

            title = _extract_visual_title(config, container)

            # Build alias→Entity map from prototypeQuery.From[] for alias resolution
            proto_q = single_visual.get("prototypeQuery", {})
            _from_items = proto_q.get("From", [])
            _from_map = {
                f.get("Name", ""): f.get("Entity", "")
                for f in _from_items if f.get("Name") and f.get("Entity")
            }
            field_rows: list[dict] = _fields_with_roles(single_visual, _from_map)

            # ── dataTransforms.selects[]: primary extraction source ──────────
            # Confirmed structure: sel.expr.Measure/Column.Expression.SourceRef.Entity
            dt_raw = container.get("dataTransforms", "")
            if dt_raw:
                try:
                    dt = json.loads(dt_raw) if isinstance(dt_raw, str) else dt_raw
                    existing = {r["Field"] for r in field_rows}
                    for sel in dt.get("selects", []):
                        field = ""
                        expr = sel.get("expr", {})
                        for kind in ("Measure", "Column", "HierarchyLevel"):
                            node = expr.get(kind, {})
                            if isinstance(node, dict) and node:
                                entity = node.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
                                prop = node.get("Property", "")
                                if prop:
                                    field = f"{entity}[{prop}]" if entity else f"[{prop}]"
                                    break
                        if not field:
                            qn = sel.get("queryName", "") or sel.get("queryRef", "")
                            if qn:
                                nf = _clean_query_ref(qn)
                                if "[" in nf:
                                    field = nf
                        if field and field not in existing:
                            field_rows.append({"Field": field, "Role": "dataTransforms"})
                            existing.add(field)
                except Exception:
                    pass

            # ── query key (SemanticQueryDataShapeCommand, etc.) ──────────────
            query_raw = container.get("query", "{}")
            try:
                obj = json.loads(query_raw)
                existing = {r["Field"] for r in field_rows}
                for row in _fields_with_roles(obj.get("singleVisual", {}), _from_map):
                    if row["Field"] not in existing:
                        field_rows.append(row)
                        existing.add(row["Field"])
                for sel in obj.get("Commands", [{}]):
                    for sel2 in sel.get("SemanticQueryDataShapeCommand", {}).get("Query", {}).get("Select", []):
                        native = sel2.get("NativeReferenceName", "")
                        field = _clean_query_ref(native) if native and "[" in native else ""
                        if not field:
                            for kind in ("Measure", "Column"):
                                node = sel2.get(kind, {})
                                if isinstance(node, dict) and node:
                                    src_ref = node.get("Expression", {}).get("SourceRef", {})
                                    entity = src_ref.get("Entity", "") or _from_map.get(src_ref.get("Source", ""), "")
                                    prop = node.get("Property", "")
                                    if prop:
                                        field = f"{entity}[{prop}]" if entity else f"[{prop}]"
                                        break
                        if field and field not in existing:
                            field_rows.append({"Field": field, "Role": "query"})
                            existing.add(field)
            except Exception:
                pass

            visuals_out.append({"title": title, "type": visual_type, "fields": field_rows})

        pages_out.append({"name": page_name, "visuals": visuals_out})

    return pages_out


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

_CALC_COL_STYLE = """
<div style="background:#fffbe6;border-left:4px solid #f5a623;
            padding:8px 12px;border-radius:4px;margin-bottom:4px">
  <b>🟡 Calculated Columns</b>
</div>
"""


def _render_table_expander(t: dict) -> None:
    """
    Render one expander for a table dict that has keys:
      name, columns, calc_columns, (optionally) measures
    Calculated columns appear with a yellow-highlighted header.
    """
    col_count = len(t.get("columns", []))
    calc_count = len(t.get("calc_columns", []))
    meas_count = len(t.get("measures", []))
    parts = [f"{col_count} col{'s' if col_count != 1 else ''}"]
    if calc_count:
        parts.append(f"{calc_count} calc")
    if meas_count:
        parts.append(f"{meas_count} measure{'s' if meas_count != 1 else ''}")
    label = f"📋 {t['name']}  ({', '.join(parts)})"
    with st.expander(label, expanded=False):
        if t.get("columns"):
            st.markdown("**Columns**")
            st.dataframe(
                pd.DataFrame(t["columns"]),
                use_container_width=True,
                hide_index=True,
            )
        if t.get("calc_columns"):
            st.markdown(_CALC_COL_STYLE, unsafe_allow_html=True)
            for cc in t["calc_columns"]:
                st.markdown(f"`{cc['Name']}`  _{cc['Data Type']}_")
                st.code(cc["DAX Expression"], language="dax")
        if t.get("measures"):
            st.markdown("**Measures**")
            for m in t["measures"]:
                st.markdown(f"`{m['name']}`")
                st.code(m["expression"], language="dax")
        if not t.get("columns") and not t.get("calc_columns") and not t.get("measures"):
            st.info("No columns or measures found for this table.")


def _render_measures_table(measures: list[dict], table_key_suffix: str = "") -> None:
    """
    Render the measures table with governance badges, truncated DAX, and search.

    Expects each row to have at minimum:
      'Measure Name', 'Table', 'DAX Expression', 'description', 'displayFolder'
    """
    if not measures:
        st.info("No measures found.")
        return

    # ── Governance summary ────────────────────────────────────────────────────
    no_desc  = sum(1 for m in measures if not str(m.get("description",  "")).strip())
    no_fold  = sum(1 for m in measures if not str(m.get("displayFolder","")).strip())
    total    = len(measures)

    # Persist exact governance score for the home scorecard
    if total > 0:
        _gov_ded = min(((no_desc + no_fold) / max(total * 2, 1)) * 40, 40)
        st.session_state["gov_score"] = max(0, int(100 - _gov_ded))
        st.session_state["gov_issues"] = {"no_desc": no_desc, "no_fold": no_fold, "total": total}

    if no_desc:
        st.warning(
            f"⚠️ **{no_desc} of {total}** measure{'s' if no_desc != 1 else ''} "
            "have no description — consider adding descriptions for governance and documentation."
        )
    else:
        st.success(f"✅ All {total} measures have descriptions.")

    # ── Search box ───────────────────────────────────────────────────────────
    search_val = st.text_input(
        "🔎 Filter by measure name or table",
        placeholder="Type to filter…",
        key=f"meas_search_{table_key_suffix}",
    )

    # ── Build display rows ────────────────────────────────────────────────────
    _TRUNC = 60
    rows = []
    for m in measures:
        desc   = str(m.get("description",   "")).strip()
        folder = str(m.get("displayFolder", "")).strip()
        expr   = str(m.get("DAX Expression", "")).strip()

        if desc and folder:
            badge = "✅ Complete"
        elif not desc and not folder:
            badge = "🔴 No description  ·  📁 No folder"
        elif not desc:
            badge = "🔴 No description"
        else:
            badge = "📁 No folder"

        dax_short = (expr[:_TRUNC] + "…") if len(expr) > _TRUNC else expr

        rows.append({
            "Table":           m.get("Table", ""),
            "Measure Name":    m.get("Measure Name", ""),
            "⚠️ Governance":   badge,
            "DAX (preview)":   dax_short,
            "_dax_full":       expr,       # hidden — used for tooltip column
        })

    # ── Apply search filter ───────────────────────────────────────────────────
    if search_val:
        q = search_val.lower()
        rows = [
            r for r in rows
            if q in r["Measure Name"].lower() or q in r["Table"].lower()
        ]

    if not rows:
        st.info("No measures match the filter.")
        return

    display_df = pd.DataFrame(rows).drop(columns=["_dax_full"])

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Table":         st.column_config.TextColumn("Table",         width="medium"),
            "Measure Name":  st.column_config.TextColumn("Measure",       width="medium"),
            "⚠️ Governance": st.column_config.TextColumn("⚠️ Governance", width="medium"),
            "DAX (preview)": st.column_config.TextColumn(
                "DAX Expression",
                width="large",
                help="Shows first 60 characters. Open the table expander above for the full DAX.",
            ),
        },
    )
    st.caption(
        f"Showing {len(rows)} of {total} measure(s). "
        "Open a table expander above to see the full DAX expression for any measure."
    )


def _render_pbit(raw_bytes: bytes, filename: str) -> None:
    """Render the full analysis UI for a .pbit template file."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            # ZIP listing
            entries = zf.infolist()
            file_df = pd.DataFrame(
                {
                    "File Name": [e.filename for e in entries],
                    "Size (bytes)": [e.file_size for e in entries],
                    "Compressed (bytes)": [e.compress_size for e in entries],
                }
            )
            with st.expander("📂 Raw ZIP contents", expanded=False):
                st.dataframe(file_df, use_container_width=True, hide_index=True)

            st.markdown("---")
            st.markdown("## 🗂️ Data Model Schema")

            tables, measures = parse_pbit_schema(zf)

            if not tables and not measures:
                st.warning(
                    "No `DataModelSchema` found or it contained no tables. "
                    "This may not be a standard .pbit file."
                )
                return

            # ── Tables, Columns & Calculated Columns ──────────────────────
            total_calc = sum(len(t.get("calc_columns", [])) for t in tables)
            st.markdown(
                f"### 📋 Tables  ({len(tables)})  "
                f"·  {total_calc} calculated column{'s' if total_calc != 1 else ''}"
            )
            for t in sorted(tables, key=lambda x: x["name"]):
                _render_table_expander(t)

            # ── Measures ───────────────────────────────────────────────────
            st.markdown("---")
            st.markdown(f"### ⚡ Measures  ({len(measures)})")
            _render_measures_table(measures, table_key_suffix="pbit")

            # ── Report pages (same layout parser works for .pbit too) ─────
            pages = parse_layout(zf)
            if pages:
                st.markdown("---")
                st.markdown("## 📑 Report Pages")
                m_dict, cc_dict = _build_schema_dicts_from_measures(tables, measures)
                usage_idx = _build_usage_index(pages)
                _render_pages(pages, m_dict, cc_dict, usage_index=usage_idx)
                # Unused-measure analysis is shown in the cross-report section
                # below the report tabs, where all uploaded files are combined.

    except zipfile.BadZipFile:
        st.error("This file does not appear to be a valid .pbit (ZIP) file.")


def _build_schema_dicts(schema_tables: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """
    Build two lookup dicts from parsed schema tables.

    measures_dict  : {'TableName[MeasureName]': expr, 'MeasureName': expr}
    calc_cols_dict : {'TableName[ColumnName]': expr}
    """
    measures_dict: dict[str, str] = {}
    calc_cols_dict: dict[str, str] = {}
    for t in schema_tables:
        t_name = t["name"]
        # Measures (stored in schema_measures list — re-derive from calc_columns structure)
        for m in t.get("measures", []):          # pbi-tools path
            name = m.get("name", "")
            expr = m.get("expression", "")
            if name:
                measures_dict[f"{t_name}[{name}]"] = expr
                measures_dict.setdefault(name, expr)   # plain-name fallback
        # Calculated columns
        for cc in t.get("calc_columns", []):
            name = cc.get("Name", "")
            expr = cc.get("DAX Expression", "")
            if name:
                key = f"{t_name}[{name}]"
                calc_cols_dict[key] = expr
                calc_cols_dict[key.lower()] = expr   # case-insensitive fallback
                calc_cols_dict[name.lower()] = expr  # bare-name fallback
    return measures_dict, calc_cols_dict


def _build_schema_dicts_from_measures(
    schema_tables: list[dict],
    schema_measures: list[dict],
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Build lookup dicts when measures come from _parse_full_schema
    (which returns them as a flat list with 'Table', 'Measure Name', 'DAX Expression').
    """
    measures_dict: dict[str, str] = {}
    calc_cols_dict: dict[str, str] = {}
    for m in schema_measures:
        t_name = m.get("Table", "")
        name = m.get("Measure Name", "")
        expr = m.get("DAX Expression", "")
        if name:
            measures_dict[f"{t_name}[{name}]"] = expr
            measures_dict.setdefault(name, expr)
    for t in schema_tables:
        t_name = t["name"]
        for cc in t.get("calc_columns", []):
            name = cc.get("Name", "")
            expr = cc.get("DAX Expression", "")
            if name:
                key = f"{t_name}[{name}]"
                calc_cols_dict[key] = expr
                calc_cols_dict[key.lower()] = expr  # case-insensitive fallback
                calc_cols_dict[name.lower()] = expr  # bare-name fallback
    return measures_dict, calc_cols_dict


def _enrich_fields(
    field_rows: list[dict],
    measures_dict: dict[str, str],
    calc_cols_dict: dict[str, str],
) -> list[dict]:
    """Add Type and DAX Expression columns to field rows."""
    enriched = []
    for row in field_rows:
        field = row["Field"]
        # Normalise dot-notation "Table.Field" → "Table[Field]" for lookups
        if "." in field and "[" not in field:
            bracket_field = re.sub(r"^([^.]+)\.(.+)$", r"\1[\2]", field)
        else:
            bracket_field = field
        # Bare name fallback (strips table prefix from either notation)
        if "[" in bracket_field:
            bare = bracket_field.split("[", 1)[-1].rstrip("]")
        elif "." in field:
            bare = field.split(".", 1)[-1]
        else:
            bare = field

        bracket_lower = bracket_field.lower()
        bare_lower = bare.lower()
        if field in measures_dict or bracket_field in measures_dict or bare in measures_dict:
            expr = measures_dict.get(field) or measures_dict.get(bracket_field) or measures_dict.get(bare, "")
            enriched.append({"Role": row["Role"], "Field Name": field,
                             "Type": "📐 Measure", "DAX Expression": expr})
        elif (field in calc_cols_dict or bracket_field in calc_cols_dict
              or bracket_lower in calc_cols_dict or bare_lower in calc_cols_dict):
            expr = (calc_cols_dict.get(field) or calc_cols_dict.get(bracket_field)
                    or calc_cols_dict.get(bracket_lower) or calc_cols_dict.get(bare_lower, ""))
            enriched.append({"Role": row["Role"], "Field Name": field,
                             "Type": "🧮 Calc Column", "DAX Expression": expr})
        else:
            enriched.append({"Role": row["Role"], "Field Name": field,
                             "Type": "📋 Column", "DAX Expression": "—"})
    return enriched


# Visual types that never bind to data — always excluded from the lineage view
_ALWAYS_HIDDEN_VISUAL_TYPES: frozenset[str] = frozenset({
    "shape", "basicShape", "image", "textbox", "button", "actionButton",
})

# Visual types that are considered standard data visuals (used to decide whether
# a 0-field visual should be shown or hidden)
_DATA_VISUAL_TYPES: frozenset[str] = frozenset({
    "barChart", "clusteredBarChart", "stackedBarChart", "hundredPercentStackedBarChart",
    "columnChart", "clusteredColumnChart", "stackedColumnChart", "hundredPercentStackedColumnChart",
    "lineChart", "areaChart", "stackedAreaChart", "hundredPercentStackedAreaChart",
    "lineClusteredColumnComboChart", "lineStackedColumnComboChart",
    "ribbonChart", "waterfallChart", "funnelChart", "scatterChart", "bubbleChart",
    "pieChart", "donutChart", "treemap", "sunburstChart",
    "tableEx", "pivotTable",  # table / matrix
    "card", "multiRowCard", "kpi", "gauge",
    "map", "filledMap", "azureMap", "shapeMap",
    "slicer",
    "decompositionTreeVisual", "keyInfluencers", "qnaVisual",
    "rdlVisual", "pythonVisual", "rVisual", "scriptVisual",
    "customVisual",
})


# ---------------------------------------------------------------------------
# Lineage helpers — usage index & unused measure detection
# ---------------------------------------------------------------------------

def _build_usage_index(pages: list[dict]) -> dict[str, int]:
    """Return {field_key: N} where N = number of data visuals that reference that field.
    All roles are counted (values, axis, legend, tooltips, filters, drill-through, etc.)."""
    index: dict[str, int] = {}
    for page in pages:
        for visual in page.get("visuals", []):
            if not _is_data_visual(visual["type"], len(visual["fields"])):
                continue
            for row in visual["fields"]:
                key = row["Field"]
                index[key] = index.get(key, 0) + 1
    return index


# Regex: matches [Name] NOT preceded by a word char, quote, or ] → standalone measure ref
_DAX_MREF_RE = re.compile(r"(?<!['\w\]])\[([^\]]+)\]")


def _dax_dep_graph(measures: list[dict]) -> dict[str, set[str]]:
    """
    Build {name_lower: {referenced_name_lower, ...}} for all measures.
    Accepts both 'name'/'expression' and 'Measure Name'/'DAX Expression' key formats.
    Only registers references that are themselves known measure names.
    """
    def _name(m: dict) -> str:
        return (m.get("name") or m.get("Measure Name", "")).lower()

    def _expr(m: dict) -> str:
        return m.get("expression") or m.get("DAX Expression", "")

    all_names = {_name(m) for m in measures}
    graph: dict[str, set[str]] = {}
    for m in measures:
        refs = {r.lower() for r in _DAX_MREF_RE.findall(_expr(m)) if r.lower() in all_names}
        graph[_name(m)] = refs
    return graph


def _transitive_used(seeds: set[str], graph: dict[str, set[str]]) -> set[str]:
    """Return all nodes reachable from seeds via graph (BFS)."""
    visited: set[str] = set()
    queue = list(seeds)
    while queue:
        node = queue.pop()
        if node in visited:
            continue
        visited.add(node)
        for dep in graph.get(node, ()):
            if dep not in visited:
                queue.append(dep)
    return visited


def _compute_unused_measures(
    measures: list[dict],
    usage_index: dict[str, int],
    reports_usage: dict[str, dict[str, int]] | None = None,
) -> list[dict]:
    """
    Return rows for measures not found in any of the uploaded reports.
    Columns: Status, Table, Measure, DAX Expression, Details, Risk to Delete, Used in Reports.

    reports_usage: {filename: {field_key: count}} — one usage_index per uploaded file.
    When provided, a measure is only flagged if not used in ANY of those reports.
    """
    def _norm(m: dict) -> dict:
        return {
            "name": (m.get("name") or m.get("Measure Name", "")),
            "table": (m.get("table") or m.get("Table", "")),
            "expression": (m.get("expression") or m.get("DAX Expression", "")),
            "description": m.get("description", ""),
            "displayFolder": m.get("displayFolder", ""),
        }

    normed = [_norm(m) for m in measures]
    all_names_lower = {m["name"].lower() for m in normed}

    # Build one combined usage index (union across all reports)
    if reports_usage:
        combined_index: dict[str, int] = {}
        for idx in reports_usage.values():
            for key, count in idx.items():
                combined_index[key] = combined_index.get(key, 0) + count
    else:
        combined_index = usage_index

    # Step 1 — visually bound names across ALL uploaded reports
    visually_used: set[str] = set()
    for key, count in combined_index.items():
        if count == 0:
            continue
        bare = key.split("[", 1)[-1].rstrip("]") if "[" in key else key
        visually_used.add(bare.lower())

    # Step 2 — DAX dependency graph restricted to known measure names
    graph = _dax_dep_graph(normed)

    # Step 3 — transitive closure: everything reachable from visually-used measures
    used_set = _transitive_used(visually_used, graph)

    # Step 4 — reverse ref map: which measures reference each measure
    reverse: dict[str, set[str]] = {}
    for m_lower, deps in graph.items():
        for dep in deps:
            if dep in all_names_lower:
                reverse.setdefault(dep, set()).add(m_lower)

    # Step 5 — per-report used-bare-names map (for "Used in Reports" column)
    per_report_bare: dict[str, set[str]] = {}
    if reports_usage:
        for fname, idx in reports_usage.items():
            s: set[str] = set()
            for key, count in idx.items():
                if count > 0:
                    bare = key.split("[", 1)[-1].rstrip("]") if "[" in key else key
                    s.add(bare.lower())
            per_report_bare[fname] = s

    result: list[dict] = []
    for m in normed:
        name_lower = m["name"].lower()
        if name_lower in used_set:
            continue
        referrers = reverse.get(name_lower, set())

        # Which uploaded reports use this measure (direct visual binding)
        if per_report_bare:
            found_in = [fn for fn, s in per_report_bare.items() if name_lower in s]
        else:
            found_in = []

        # If it IS found in at least one report, skip — it’s used
        if found_in:
            continue

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
        has_metadata = bool(m["description"].strip() or m["displayFolder"].strip())
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


def _render_unused_measures(
    schema_measures: list[dict],
    usage_index: dict[str, int],
    reports_usage: dict[str, dict[str, int]] | None = None,
) -> int:
    """
    Render a 'Measures Not Used in Any Uploaded Report' section.
    Returns the count of such measures.
    """
    not_used = _compute_unused_measures(schema_measures, usage_index, reports_usage)

    n_reports = len(reports_usage) if reports_usage else 1
    n_measures = len(schema_measures)
    report_names = list(reports_usage.keys()) if reports_usage else []

    st.markdown("### 🔍 Measures Not Used in Any Uploaded Report")
    st.caption(
        f"📂 **{n_reports}** report(s) analysed · "
        f"**{n_measures}** measure(s) found across all reports · "
        f"**{len(not_used)}** measure(s) not used in any uploaded report"
    )
    if report_names:
        st.caption("Reports included in this analysis: " + " · ".join(f"`{fn}`" for fn in report_names))
    st.info(
        "💡 For complete coverage, upload **all** reports that share this dataset at the same time."
    )
    if not not_used:
        st.success("✅ All model measures are referenced in at least one uploaded report (or transitively used by one).")
    else:
        st.warning(
            f"⚠️ **{len(not_used)} measure(s)** are not used in any of the {n_reports} uploaded report(s). "
            "They may still be used in other reports connected to the same dataset. "
            "**Do not delete without checking all connected reports.**"
        )
        st.dataframe(
            pd.DataFrame(not_used),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Status": st.column_config.TextColumn("Status", width="medium"),
                "DAX Expression": st.column_config.TextColumn("DAX Expression", width="large"),
                "Details": st.column_config.TextColumn("Details", width="large"),
                "Risk to Delete": st.column_config.TextColumn("Risk to Delete", width="small"),
                "Used in Reports": st.column_config.TextColumn("Used in Reports", width="large"),
            },
        )
    return len(not_used)


def _is_data_visual(visual_type: str, field_count: int) -> bool:
    """Return True if this visual should appear in the lineage view."""
    vt = visual_type.lower() if visual_type else ""
    # Always hide decorative / container visuals
    if visual_type in _ALWAYS_HIDDEN_VISUAL_TYPES or vt in {v.lower() for v in _ALWAYS_HIDDEN_VISUAL_TYPES}:
        return False
    # Slicer with no fields — nothing to show
    if vt == "slicer" and field_count == 0:
        return False
    # Any visual with 0 fields that is not a recognised data visual type — hide it
    if field_count == 0:
        known = {v.lower() for v in _DATA_VISUAL_TYPES}
        if vt not in known:
            return False
    return True


def _render_pages(
    pages: list[dict],
    measures_dict: dict[str, str] | None = None,
    calc_cols_dict: dict[str, str] | None = None,
    usage_index: dict[str, int] | None = None,
) -> None:
    """Render all report pages as Streamlit tabs with visuals and enriched fields."""
    measures_dict = measures_dict or {}
    calc_cols_dict = calc_cols_dict or {}

    # ── Summary scorecard ────────────────────────────────────────────────────
    data_visuals_all = [
        v
        for p in pages
        for v in p.get("visuals", [])
        if _is_data_visual(v["type"], len(v["fields"]))
    ]
    total_field_bindings = sum(len(v["fields"]) for v in data_visuals_all)
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Report Pages", len(pages))
    sc2.metric("Data Visuals", len(data_visuals_all))
    sc3.metric("Field Bindings", total_field_bindings)
    # unused measure count if caller provided usage_index
    if usage_index is not None:
        # count measures in measures_dict (full-key format only, i.e. contains "[")
        model_measures = [k for k in measures_dict if "[" in k]
        unused_ct = sum(
            1 for k in model_measures
            if usage_index.get(k, 0) == 0
            and not any(ui.endswith(k.split("[", 1)[-1]) for ui in usage_index)
        )
        sc4.metric("Unused Measures", unused_ct, delta=None)
    else:
        sc4.metric("Unique Fields", len({row["Field"] for v in data_visuals_all for row in v["fields"]}))
    st.markdown("---")

    tab_labels = [p["name"] for p in pages]
    tabs = st.tabs(tab_labels)
    for tab, page in zip(tabs, pages):
        with tab:
            visuals = page["visuals"]
            # Filter to only data-bearing visuals before rendering
            visuals = [v for v in visuals if _is_data_visual(v["type"], len(v["fields"]))]
            if not visuals:
                st.success("✅ No data visuals on this page.")
                continue
            st.markdown(f"**{len(visuals)} visual(s)** on this page")
            for i, visual in enumerate(visuals, 1):
                v_type = visual["type"]
                title = visual.get("title", "").strip()
                field_rows = visual["fields"]  # list of {Field, Role}

                if title:
                    display_title = title
                elif field_rows:
                    # Use the first field name as the visual's auto-title
                    first_field = field_rows[0]["Field"]
                    display_title = first_field
                else:
                    display_title = f"Untitled Visual — {v_type}"
                field_count = len(field_rows)
                label = f"{display_title}  ({field_count} field{'s' if field_count != 1 else ''})"

                with st.expander(label, expanded=False):
                    st.markdown(f"**{display_title}**")
                    st.caption(f"Type: `{v_type}`")
                    if field_rows:
                        enriched = _enrich_fields(field_rows, measures_dict, calc_cols_dict)
                        st.dataframe(
                            pd.DataFrame(enriched),
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "Role": st.column_config.TextColumn("Role", width="small"),
                                "Field Name": st.column_config.TextColumn("Field Name", width="medium"),
                                "Type": st.column_config.TextColumn("Type", width="small"),
                                "DAX Expression": st.column_config.TextColumn("DAX Expression", width="large"),
                            },
                        )
                    else:
                        st.info("No field references detected for this visual.")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

if uploaded_files:
    # ── Parse every uploaded file and store results in session state ──────────
    reports: dict[str, dict] = {}
    for uf in uploaded_files:
        raw = uf.read()
        fname = uf.name
        file_ext = Path(fname).suffix.lower()
        r: dict = {
            "raw_bytes": raw,
            "filename": fname,
            "ext": file_ext,
            "tables": [],
            "measures": [],
            "pages": [],
            "usage_index": {},
        }
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                if file_ext == ".pbit":
                    r["tables"], r["measures"] = parse_pbit_schema(zf)
                else:
                    names_lower = {n.lower(): n for n in zf.namelist()}
                    # Detect binary-only DataModel (newer enhanced PBIX format)
                    _has_binary = any(
                        k == "datamodel" or k.endswith("/datamodel")
                        for k in names_lower
                    )
                    _has_json = any(
                        "datamodelschema" in k or k.endswith(".bim")
                        for k in names_lower
                    )
                    r["has_binary_model"] = _has_binary
                    r["has_schema_json"]  = _has_json
                    _encodings = ("utf-8-sig", "utf-16-le", "utf-16", "utf-8", "utf-16-be", "latin-1")

                    def _try_full_schema_candidates(target_zf, target_names):
                        """Try DataModelSchema candidates in target_zf, return (tables, measures) or (None, None)."""
                        cands = [
                            real for lower, real in target_names.items()
                            if "datamodelschema" in lower or lower.endswith(".bim")
                        ]
                        for cand in cands:
                            try:
                                raw_s = target_zf.read(cand)
                                parsed = None
                                for enc in _encodings:
                                    try:
                                        text = raw_s.decode(enc)
                                        stripped = text.lstrip("\ufeff").lstrip()
                                        if stripped and stripped[0] not in ("{", "["):
                                            continue
                                        parsed = json.loads(text)
                                        break
                                    except Exception:
                                        continue
                                if parsed:
                                    tbls, meas = _parse_full_schema(parsed)
                                    if tbls or meas:
                                        return tbls, meas
                            except Exception:
                                continue
                        return None, None

                    # Path A: outer DataModelSchema JSON (standard PBIX / all PBIT)
                    _tbls, _meas = _try_full_schema_candidates(zf, names_lower)
                    if _tbls is not None:
                        r["tables"], r["measures"] = _tbls, _meas

                    # Path B: enhanced-model PBIX — DataModel entry is itself a nested ZIP
                    if not r["tables"] and not r["measures"] and _has_binary:
                        _dm_entry = next(
                            (real for lower, real in names_lower.items()
                             if lower == "datamodel" or lower.endswith("/datamodel")),
                            None,
                        )
                        if _dm_entry:
                            try:
                                _inner_bytes = zf.read(_dm_entry)
                                with zipfile.ZipFile(io.BytesIO(_inner_bytes)) as _inner_zf:
                                    _inner_names = {n.lower(): n for n in _inner_zf.namelist()}
                                    _tbls2, _meas2 = _try_full_schema_candidates(_inner_zf, _inner_names)
                                    if _tbls2 is not None:
                                        r["tables"], r["measures"] = _tbls2, _meas2
                            except (zipfile.BadZipFile, Exception):
                                pass
                r["pages"] = parse_layout(zf)
                r["usage_index"] = _build_usage_index(r["pages"])
        except zipfile.BadZipFile:
            st.error(f"❌ **{fname}** is not a valid .pbix/.pbit (ZIP) file.")
            continue
        reports[fname] = r

    # Persist to session state
    st.session_state["reports"] = reports
    if reports:
        primary_name = list(reports.keys())[0]
        primary = reports[primary_name]
        st.session_state["pbi_file_bytes"] = primary["raw_bytes"]
        st.session_state["pbi_file_name"]  = primary_name
    else:
        st.stop()

    # Combined usage index across all reports (for unused-measure detection)
    reports_usage: dict[str, dict[str, int]] = {
        fn: r["usage_index"] for fn, r in reports.items()
    }
    combined_usage: dict[str, int] = {}
    for idx in reports_usage.values():
        for key, count in idx.items():
            combined_usage[key] = combined_usage.get(key, 0) + count

    # Canonical measures list from the primary (first) report's model
    all_measures = primary["measures"]

    if len(reports) > 1:
        st.success(
            f"📂 **{len(reports)} report(s) uploaded:** " +
            ", ".join(f"`{fn}`" for fn in reports)
        )
    else:
        st.success(f"File **{primary_name}** uploaded successfully!")

    # ── Render one tab per report ─────────────────────────────────────────────
    if len(reports) > 1:
        report_tabs = st.tabs(list(reports.keys()))
    else:
        report_tabs = [st.container()]

    for tab_ctx, (fname, r) in zip(report_tabs, reports.items()):
        with tab_ctx:
            raw_bytes = r["raw_bytes"]
            file_ext  = r["ext"]
            pages     = r["pages"]
            schema_tables   = r["tables"]
            schema_measures = r["measures"]

            if file_ext == ".pbit":
                _render_pbit(raw_bytes, fname)
                continue

            # .pbix path
            try:
                with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                    entries = zf.infolist()
                    file_df = pd.DataFrame({
                        "File Name": [e.filename for e in entries],
                        "Size (bytes)": [e.file_size for e in entries],
                        "Compressed (bytes)": [e.compress_size for e in entries],
                    })
                    with st.expander("📂 Raw ZIP contents", expanded=False):
                        st.dataframe(file_df, use_container_width=True, hide_index=True)

                    st.markdown("---")
                    st.markdown("## 📑 Report Pages")

                    measures_dict, calc_cols_dict = _build_schema_dicts_from_measures(
                        schema_tables, schema_measures
                    )

                    if not pages:
                        st.warning("Could not find or parse `Report/Layout` in this .pbix file.")
                    else:
                        _render_pages(
                            pages, measures_dict, calc_cols_dict,
                            usage_index=r["usage_index"],
                        )

                    st.markdown("---")
                    st.markdown("## 🗂️ Data Model Schema (DataModelSchema)")

                    if not schema_tables:
                        if r.get("has_binary_model") and not r.get("has_schema_json"):
                            st.warning(
                                "⚠️ **Enhanced Model Format detected.**  "
                                "This PBIX stores its data model as a compiled binary (`DataModel`) — "
                                "there is no plain-text `DataModelSchema` JSON to read directly.  "
                                "Use **pbi-tools Deep Extract** below (or export as .pbit from Power BI Desktop) "
                                "to unlock full measure & relationship analysis."
                            )
                        else:
                            st.info(
                                "`DataModelSchema` not found in this .pbix. "
                                "Use **pbi-tools Deep Extract** below for full schema."
                            )
                    else:
                        total_calc = sum(len(t.get("calc_columns", [])) for t in schema_tables)
                        st.caption(
                            f"{len(schema_tables)} table(s)  ·  "
                            f"{total_calc} calculated column{'s' if total_calc != 1 else ''}  ·  "
                            f"{len(schema_measures)} measure{'s' if len(schema_measures) != 1 else ''}"
                        )
                        for t in sorted(schema_tables, key=lambda x: x["name"]):
                            _render_table_expander(t)
                        if schema_measures:
                            st.markdown("---")
                            st.markdown(f"### ⚡ Measures  ({len(schema_measures)})")
                            _render_measures_table(schema_measures, table_key_suffix="pbix")

            except zipfile.BadZipFile:
                st.error(f"**{fname}** does not appear to be a valid .pbix (ZIP) file.")

            # pbi-tools deep extract (only for primary report)
            if fname == primary_name:
                st.markdown("---")
                st.markdown("## 🔬 Deep Schema Analysis  (pbi-tools)")
                pbitools_exe = _find_pbitools()
                if pbitools_exe is None:
                    st.warning(
                        "**pbi-tools** is not installed. "
                        "Click the button below to download it automatically (~30 MB)."
                    )
                    if st.button("⬇️ Download pbi-tools"):
                        with st.spinner("Downloading pbi-tools from GitHub…"):
                            ok, msg = _download_pbitools()
                        if ok:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                else:
                    st.caption(f"pbi-tools found: `{pbitools_exe}`")
                    if st.button("🔬 Extract & Analyze with pbi-tools"):
                        with tempfile.TemporaryDirectory() as tmp:
                            tmp_path = Path(tmp)
                            pbix_path = tmp_path / fname
                            pbix_path.write_bytes(raw_bytes)
                            out_dir = tmp_path / "extracted"
                            out_dir.mkdir()
                            with st.spinner("Running pbi-tools extract…"):
                                proc = _run_pbitools_extract(pbitools_exe, pbix_path, out_dir)
                            pbi_tables = _parse_pbitools_output(out_dir)
                        if pbi_tables:
                            st.success(f"Extracted **{len(pbi_tables)}** table(s)")
                            for t in sorted(pbi_tables, key=lambda x: x["name"]):
                                _render_table_expander(t)
                        else:
                            st.warning("pbi-tools ran but no tables were found in the extracted output.")
                            if proc.stderr:
                                with st.expander("pbi-tools output", expanded=True):
                                    st.code(proc.stderr[:2000])

    # ── Cross-report unused measures section ──────────────────────────────────
    if all_measures:
        st.markdown("---")
        _render_unused_measures(all_measures, combined_usage, reports_usage)
