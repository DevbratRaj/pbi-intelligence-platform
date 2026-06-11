"""
dax_parser.py
=============
Lightweight DAX tokenizer + reference extractor.

We do NOT need a full grammar — we just need:
  • Measure references           [MeasureName]
  • Column / table references    Table[Column]   or   'Quoted Table'[Column]
  • Function calls               FUNCNAME(
  • Variable definitions         VAR x = ...
  • Numeric literal usage hints  (for performance smells)

Strategy: strip strings + comments first (so brackets/quotes inside them
don't confuse the regex), then run a handful of carefully-anchored regexes
against the cleaned expression.

This handles ~99% of real-world DAX. It is *intentionally* lenient — better
to under-report than to crash on weird formatting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# ─────────────────────────────────────────────────────────────────────────────
# 1. Strip strings + comments
# ─────────────────────────────────────────────────────────────────────────────
_STRING_RE       = re.compile(r'"(?:[^"\\]|\\.|"")*"')          # "..." with escapes / "" inside
_LINE_COMMENT_RE = re.compile(r'//[^\n]*')
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)


def _clean(expr: str) -> str:
    if not expr:
        return ""
    s = _BLOCK_COMMENT_RE.sub(" ", expr)
    s = _LINE_COMMENT_RE.sub(" ",  s)
    s = _STRING_RE.sub('""',       s)        # collapse string literals to ""
    return s


# ─────────────────────────────────────────────────────────────────────────────
# 2. Reference regexes
# ─────────────────────────────────────────────────────────────────────────────
# Measure ref:  a `[Name]` NOT preceded by a table-name token.
# We capture all [Name] then filter out the ones that have a table prefix.
_BRACKET_REF_RE = re.compile(
    r"""
    (?:                              # optional table prefix  (captured into group 'tbl')
        (?P<tbl>
              '[^']+'                #   'Quoted Table'
            | [A-Za-z_][\w]*         #   UnquotedTable
        )
    )?
    \[                               # opening [
        (?P<name>[^\[\]]+)           #   item name (no nested brackets)
    \]
    """,
    re.VERBOSE,
)

# Function call:  IDENT followed by '('  (case-insensitive, kw-aware downstream)
_FUNC_CALL_RE = re.compile(r"\b([A-Z][A-Z0-9\.]*)\s*\(", re.IGNORECASE)

# VAR declarations
_VAR_RE = re.compile(r"\bVAR\s+([A-Za-z_]\w*)\b", re.IGNORECASE)

# DAX keywords that look like function calls but aren't user-callable functions.
_KEYWORDS = {
    "VAR", "RETURN", "IF", "ELSE", "TRUE", "FALSE",
    "AND", "OR", "NOT", "IN",
}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Public dataclasses
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class DaxRefs:
    """Everything we extracted from a single DAX expression."""
    measure_refs: list[str]              = field(default_factory=list)   # ["Sales", "Profit"]
    column_refs:  list[tuple[str, str]]  = field(default_factory=list)   # [("FactSales","Amount")]
    table_refs:   list[str]              = field(default_factory=list)   # ["FactSales"]   (bare table mentions)
    functions:    list[str]              = field(default_factory=list)   # uppercase, in order, with dupes
    variables:    list[str]              = field(default_factory=list)   # VAR-defined names

    @property
    def function_set(self) -> set[str]:
        return {f.upper() for f in self.functions}

    @property
    def unique_measure_refs(self) -> set[str]:
        return set(self.measure_refs)

    @property
    def unique_column_refs(self) -> set[tuple[str, str]]:
        return set(self.column_refs)


# ─────────────────────────────────────────────────────────────────────────────
# 4. The parser
# ─────────────────────────────────────────────────────────────────────────────
def parse_dax(expression: str, *, local_vars: Iterable[str] = ()) -> DaxRefs:
    """
    Extract references from a DAX expression.

    `local_vars` lets the caller pass in additional names (e.g. measure names
    in scope) so we never confuse a VAR reference with a measure ref.
    """
    refs = DaxRefs()
    if not expression or not expression.strip():
        return refs

    cleaned = _clean(expression)

    # variables first — needed to filter bracket refs that point to VARs (rare but valid)
    refs.variables = _VAR_RE.findall(cleaned)
    var_set = {v.lower() for v in refs.variables} | {v.lower() for v in local_vars}

    # bracket references
    for m in _BRACKET_REF_RE.finditer(cleaned):
        tbl  = (m.group("tbl") or "").strip()
        name = m.group("name").strip()

        if tbl:
            # strip surrounding quotes from quoted table names
            if tbl.startswith("'") and tbl.endswith("'"):
                tbl = tbl[1:-1]
            refs.column_refs.append((tbl, name))
            refs.table_refs.append(tbl)
        else:
            # bare [Name] — could be measure ref OR var ref
            if name.lower() in var_set:
                continue
            refs.measure_refs.append(name)

    # function calls
    for m in _FUNC_CALL_RE.finditer(cleaned):
        fn = m.group(1).upper()
        if fn in _KEYWORDS:
            continue
        refs.functions.append(fn)

    return refs


# ─────────────────────────────────────────────────────────────────────────────
# 5. Performance / style smells
# ─────────────────────────────────────────────────────────────────────────────
# Each rule returns a (severity, message) tuple if triggered, else None.
# Severity: "high" | "medium" | "low"

def _smell_calculate_filter_all(expr: str, refs: DaxRefs) -> tuple[str, str] | None:
    """CALCULATE(..., FILTER(ALL(...), ...))  →  recommend KEEPFILTERS or remove ALL."""
    fns = refs.function_set
    if "CALCULATE" not in fns or "FILTER" not in fns or "ALL" not in fns:
        return None
    # heuristic: nested pattern actually present?
    if re.search(r"FILTER\s*\(\s*ALL\s*\(", _clean(expr), re.IGNORECASE):
        return ("medium",
                "CALCULATE(…, FILTER(ALL(…), …)) pattern — usually faster as "
                "CALCULATE(…, KEEPFILTERS(…)) or by removing FILTER entirely.")
    return None


def _smell_earlier(expr: str, refs: DaxRefs) -> tuple[str, str] | None:
    if "EARLIER" in refs.function_set:
        return ("medium",
                "Uses EARLIER — modern DAX prefers VAR / RETURN, which is clearer "
                "and typically faster.")
    return None


def _smell_iferror_divide(expr: str, refs: DaxRefs) -> tuple[str, str] | None:
    fns = refs.function_set
    if "IFERROR" in fns and "DIVIDE" in fns:
        return ("low",
                "IFERROR + DIVIDE detected — DIVIDE already handles divide-by-zero "
                "via its 3rd argument; the IFERROR wrapper is redundant.")
    return None


def _smell_nested_if(expr: str, refs: DaxRefs) -> tuple[str, str] | None:
    """3+ levels of nested IF → SWITCH(TRUE()) is more readable."""
    cleaned = _clean(expr)
    # very rough: count `IF(` occurrences; if 3 or more inside one expression, smell.
    if cleaned.upper().count("IF(") >= 3:
        return ("low",
                "3+ nested IFs — consider SWITCH(TRUE(), …) for readability.")
    return None


def _smell_deprecated(expr: str, refs: DaxRefs) -> tuple[str, str] | None:
    bad = {"FIRSTNONBLANK", "LASTNONBLANK"} & refs.function_set
    if bad:
        return ("low",
                f"Uses {', '.join(sorted(bad))} — modern equivalents "
                "(MINX/MAXX with filtering) are usually preferred.")
    return None


_SMELLS = (
    _smell_calculate_filter_all,
    _smell_earlier,
    _smell_iferror_divide,
    _smell_nested_if,
    _smell_deprecated,
)


def detect_smells(expression: str, refs: DaxRefs | None = None) -> list[dict]:
    """Return a list of {'severity', 'message'} dicts for the given DAX."""
    if refs is None:
        refs = parse_dax(expression)
    out: list[dict] = []
    for smell in _SMELLS:
        result = smell(expression, refs)
        if result:
            sev, msg = result
            out.append({"severity": sev, "message": msg})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 6. Time-intelligence detection (used to silence "misplaced measure" false-positives)
# ─────────────────────────────────────────────────────────────────────────────
_TIME_INTEL_FUNCS = {
    "DATEADD", "DATESYTD", "DATESQTD", "DATESMTD",
    "TOTALYTD", "TOTALQTD", "TOTALMTD",
    "SAMEPERIODLASTYEAR", "PARALLELPERIOD", "PREVIOUSYEAR",
    "PREVIOUSQUARTER", "PREVIOUSMONTH", "PREVIOUSDAY",
    "NEXTYEAR", "NEXTQUARTER", "NEXTMONTH", "NEXTDAY",
    "DATESBETWEEN", "DATESINPERIOD", "FIRSTDATE", "LASTDATE",
    "STARTOFYEAR", "STARTOFQUARTER", "STARTOFMONTH",
    "ENDOFYEAR", "ENDOFQUARTER", "ENDOFMONTH",
    "OPENINGBALANCEYEAR", "OPENINGBALANCEQUARTER", "OPENINGBALANCEMONTH",
    "CLOSINGBALANCEYEAR", "CLOSINGBALANCEQUARTER", "CLOSINGBALANCEMONTH",
}


def is_time_intelligence(refs: DaxRefs) -> bool:
    """True if the measure uses any time-intelligence function — these
    measures legitimately live near their date dimension and should NOT
    be flagged as 'misplaced'."""
    return bool(refs.function_set & _TIME_INTEL_FUNCS)


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    sample = """
        VAR _curr = [Total Sales]
        VAR _prev = CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[Date]))
        RETURN
            DIVIDE(_curr - _prev, _prev)
    """
    r = parse_dax(sample)
    print(json.dumps({
        "measure_refs": r.measure_refs,
        "column_refs":  r.column_refs,
        "functions":    sorted(r.function_set),
        "variables":    r.variables,
        "is_ti":        is_time_intelligence(r),
        "smells":       detect_smells(sample, r),
    }, indent=2, default=str))
