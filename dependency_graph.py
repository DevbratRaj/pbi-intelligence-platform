"""
dependency_graph.py
===================
Build a measure/column dependency graph from extracted PBIT metadata.

Given the output of `pbit_extractor.extract_pbit_metadata`, produce:

    {
      "measures":  { "Total Sales": MeasureNode, ... },
      "callers":   { "Total Sales": ["YoY Sales", "Sales Growth %"], ... },
      "broken":    [ "<measure that references a missing thing>", ... ],
      "smells":    [ {"measure": ..., "severity": ..., "message": ...}, ... ],
      "time_intel":{ "Sales YoY", ... },          # measures using time-intel
      "hidden":    { "_Helper", ... },             # isHidden=true
      "orphans":   [ "Unused KPI", ... ],          # measures nobody references AND not hidden
    }

And answer the killer question:

    impact_of_rename(measure_name) ->
        { "direct": [...], "transitive": [...], "total": int }
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from dax_parser import parse_dax, detect_smells, is_time_intelligence, DaxRefs


@dataclass
class MeasureNode:
    name:         str
    table:        str
    expression:   str
    description:  str = ""
    display_folder: str = ""
    is_hidden:    bool = False
    refs:         DaxRefs | None = None
    smells:       list[dict] = field(default_factory=list)


def build_graph(meta: dict) -> dict:
    measures_in: list[dict] = meta.get("measures", []) or []
    columns_in:  list[dict] = meta.get("columns",  []) or []
    tables_in:   list[dict] = meta.get("tables",   []) or []

    # ── known names for resolution ────────────────────────────────────────
    measure_names = {m.get("name", ""): m for m in measures_in if m.get("name")}
    table_names   = {t.get("name", "") for t in tables_in       if t.get("name")}
    column_keys   = {(c.get("table", ""), c.get("name", ""))
                     for c in columns_in if c.get("name")}

    # DAX is case-insensitive — build lowercase lookup sets to avoid false positives
    measure_names_ci = {k.lower(): v for k, v in measure_names.items()}
    table_names_ci   = {n.lower() for n in table_names}
    column_keys_ci   = {(t.lower(), c.lower()) for t, c in column_keys}
    # DAX also allows Table[MeasureName] syntax for measures — track these to
    # avoid flagging them as broken column references (the most common false positive)
    measure_table_keys_ci = {
        (m.get("table", "").lower(), m.get("name", "").lower())
        for m in measures_in if m.get("name") and m.get("table")
    }

    # ── parse every measure ───────────────────────────────────────────────
    nodes: dict[str, MeasureNode] = {}
    callers: dict[str, list[str]] = defaultdict(list)
    smells_all: list[dict] = []
    broken: list[dict] = []
    time_intel: set[str] = set()
    hidden: set[str] = set()

    for m in measures_in:
        name  = m.get("name", "")
        if not name:
            continue
        expr  = m.get("expression", "") or ""
        refs  = parse_dax(expr)

        node = MeasureNode(
            name=name,
            table=m.get("table", ""),
            expression=expr,
            description=(m.get("description") or ""),
            display_folder=(m.get("displayFolder") or m.get("display_folder") or ""),
            is_hidden=bool(m.get("isHidden") or m.get("is_hidden")),
            refs=refs,
        )
        nodes[name] = node

        if node.is_hidden:
            hidden.add(name)
        if is_time_intelligence(refs):
            time_intel.add(name)

        # smells
        for s in detect_smells(expr, refs):
            s = {**s, "measure": name, "table": node.table}
            node.smells.append(s)
            smells_all.append(s)

        # build caller index + broken refs
        for ref_name in refs.unique_measure_refs:
            if ref_name.lower() in measure_names_ci:
                # Use the canonical name for the caller index
                canonical = measure_names_ci[ref_name.lower()].get("name", ref_name)
                callers[canonical].append(name)
            else:
                broken.append({
                    "measure":   name,
                    "kind":      "measure",
                    "missing":   ref_name,
                    "message":   f"References unknown measure [{ref_name}]",
                })

        for tbl, col in refs.unique_column_refs:
            tbl_l = tbl.lower()
            col_l = col.lower()
            if tbl_l not in table_names_ci:
                broken.append({
                    "measure":   name,
                    "kind":      "table",
                    "missing":   tbl,
                    "message":   f"References unknown table '{tbl}'",
                })
            elif (tbl_l, col_l) not in column_keys_ci:
                # DAX allows Table[Measure] syntax — not a broken ref if it resolves to a measure
                if (tbl_l, col_l) in measure_table_keys_ci:
                    canonical_m = measure_names_ci.get(col_l, {}).get("name", col)
                    callers[canonical_m].append(name)
                else:
                    broken.append({
                        "measure":   name,
                        "kind":      "column",
                        "missing":   f"{tbl}[{col}]",
                        "message":   f"References unknown column {tbl}[{col}]",
                    })

    # ── orphans (no one calls them and not hidden) ────────────────────────
    referenced = set(callers.keys())
    orphans = sorted(
        n for n, node in nodes.items()
        if n not in referenced and not node.is_hidden
    )

    return {
        "measures":   nodes,
        "callers":    dict(callers),
        "broken":     broken,
        "smells":     smells_all,
        "time_intel": time_intel,
        "hidden":     hidden,
        "orphans":    orphans,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Impact analysis  —  the killer feature
# ─────────────────────────────────────────────────────────────────────────────
def impact_of_rename(graph: dict, measure_name: str) -> dict:
    """
    Return everything that would break if `measure_name` were renamed.

      direct:     measures that reference it directly (1 hop)
      transitive: measures that depend on those (2+ hops)
      total:      direct + transitive count
    """
    callers = graph.get("callers", {})
    direct = list(callers.get(measure_name, []))

    # BFS to find transitive callers (measures that depend on something
    # that depends on us).
    visited = set(direct) | {measure_name}
    queue = deque(direct)
    transitive: list[str] = []

    while queue:
        cur = queue.popleft()
        for parent in callers.get(cur, []):
            if parent not in visited:
                visited.add(parent)
                transitive.append(parent)
                queue.append(parent)

    return {
        "direct":     direct,
        "transitive": transitive,
        "total":      len(direct) + len(transitive),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers used by the Governance UI
# ─────────────────────────────────────────────────────────────────────────────
def measures_needing_description(graph: dict) -> list[MeasureNode]:
    """Filter out hidden measures — they don't need descriptions."""
    return [
        node for node in graph["measures"].values()
        if not node.is_hidden and not (node.description or "").strip()
    ]


def misplaced_measures(graph: dict, fact_keywords=("fact","facts","sales","orders","transactions")) -> list[MeasureNode]:
    """
    A measure is misplaced if it lives in a fact-looking table AND it is
    NOT a time-intelligence measure (those legitimately live near the date
    dimension, not in a _Measures table).
    """
    out = []
    for node in graph["measures"].values():
        tlow = node.table.lower()
        looks_fact = any(k in tlow for k in fact_keywords)
        if looks_fact and node.name not in graph["time_intel"]:
            out.append(node)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, json
    from pbit_extractor import extract_pbit_metadata

    if len(sys.argv) < 2:
        print("usage: python dependency_graph.py <file.pbit>")
        sys.exit(1)

    meta  = extract_pbit_metadata(sys.argv[1])
    graph = build_graph(meta)

    print(f"measures:     {len(graph['measures'])}")
    print(f"hidden:       {len(graph['hidden'])}")
    print(f"time-intel:   {len(graph['time_intel'])}")
    print(f"orphans:      {len(graph['orphans'])}")
    print(f"broken refs:  {len(graph['broken'])}")
    print(f"smells:       {len(graph['smells'])}")

    # Show top-5 most-referenced measures
    by_callers = sorted(graph["callers"].items(), key=lambda kv: -len(kv[1]))[:5]
    print("\nTop-5 most-referenced measures (rename impact):")
    for name, refs in by_callers:
        imp = impact_of_rename(graph, name)
        print(f"  {name:<40} direct={len(imp['direct']):>2}  transitive={len(imp['transitive']):>2}  total={imp['total']}")

    if graph["broken"]:
        print("\nBroken references:")
        for b in graph["broken"][:5]:
            print(f"  {b['measure']:<30} → {b['message']}")

    if graph["smells"]:
        print("\nDAX smells:")
        for s in graph["smells"][:5]:
            print(f"  [{s['severity']}] {s['measure']}: {s['message']}")
