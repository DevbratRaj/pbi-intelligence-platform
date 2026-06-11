import zipfile, json

HIDDEN = ("$", "LocalDateTable", "DateTableTemplate")
path = r"C:\Users\devbrat.raj\Downloads\Maza IPA.pbit"

with zipfile.ZipFile(path) as zf:
    print("Files in zip:", zf.namelist())
    names_lower = {n.lower(): n for n in zf.namelist()}
    candidates = [real for lower, real in names_lower.items() if "datamodelschema" in lower]
    print("Schema candidates:", candidates)
    if candidates:
        raw = zf.read(candidates[0])
        # DataModelSchema in .pbit is UTF-16-LE encoded
        for encoding in ("utf-8-sig", "utf-16-le", "utf-16"):
            try:
                schema = json.loads(raw.decode(encoding))
                print(f"Decoded with: {encoding}")
                break
            except Exception:
                continue
        else:
            print("Could not decode DataModelSchema")
            exit(1)
        model = schema.get("model", schema)
        tables = [t for t in model.get("tables", []) if not any(t.get("name", "").startswith(p) for p in HIDDEN)]
        print(f"\nTables found: {len(tables)}")
        total_measures = 0
        for t in tables:
            cols = [c["name"] for c in t.get("columns", []) if not c.get("name", "").startswith("RowNumber")]
            meas = [m["name"] for m in t.get("measures", [])]
            total_measures += len(meas)
            print(f"  {t['name']}: {len(cols)} cols, {len(meas)} measures")
        print(f"\nTotal measures: {total_measures}")
