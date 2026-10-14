"""
Builds the HelpdeskMetrics report pages (PBIR format) from the spec below:
Tickets by Tech, SLA, Utilization, Client Profitability, Contract
Profitability.

    py powerbi/generate_report.py

- Close Power BI Desktop first: Desktop rewrites these files when it saves.
- Re-running REPLACES every visual on the five pages it owns (pages not in
  PAGES are left alone). Hand-made tweaks in Desktop to those pages are lost,
  so once the layout is settled, stop regenerating and edit in Desktop.
- Every file is validated against Microsoft's published PBIR JSON schemas
  (the same ones Desktop enforces) before anything is written. Schemas are
  cached in %LOCALAPPDATA%\\Temp\\th2schemas\\mirror (downloaded on first run).
  Requires: pip install jsonschema referencing
"""
import hashlib
import json
import os
import pathlib
import shutil
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
DEF = ROOT / "HelpdeskMetrics.Report" / "definition"

VC_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.12.0/schema.json"
PAGE_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
REPORT_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.3.0/schema.json"

# --------------------------------------------------------------- helpers

def col(table, name):
    return {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}}


def meas(table, name):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}}


def lit(value):
    return {"expr": {"Literal": {"Value": value}}}


def s(text):
    """String literal in Power BI query syntax: 'text'."""
    return "'" + text.replace("'", "''") + "'"


# Where each measure lives (the model's home table — needed in SourceRef).
MEASURE_TABLE = {}


def load_measure_tables():
    sys.path.insert(0, str(ROOT))
    import generate_model  # noqa: E402  (pure module-level constants, no DB access on import)
    for table, ms in generate_model.MEASURES.items():
        for m in ms:
            MEASURE_TABLE[m[0]] = table


def field(ref):
    """'Table[Column]' -> column; '[Measure]' -> measure (home table looked up)."""
    if ref.startswith("["):
        name = ref[1:-1]
        return meas(MEASURE_TABLE[name], name), f"{MEASURE_TABLE[name]}.{name}", name
    table, name = ref[:-1].split("[", 1)
    return col(table, name), f"{table}.{name}", name


def projection(ref):
    f, qref, native = field(ref)
    return {"field": f, "queryRef": qref, "nativeQueryRef": native}


PERIOD = "Period[Period]"   # the Day/Week/Month field parameter


def role(refs):
    state = {"projections": [projection(r) for r in refs]}
    if PERIOD in refs:
        f, _, _ = field(PERIOD)
        state["fieldParameters"] = [{"parameterExpr": f, "index": refs.index(PERIOD), "length": 1}]
    return state


def vname(page, key):
    return hashlib.sha1(f"{page}/{key}".encode()).hexdigest()[:20]


def title_objects(title):
    return {"title": [{"properties": {"show": lit("true"), "text": lit(s(title))}}]}


def visual(page, key, vtype, pos, roles, title=None, sort=None, objects=None):
    x, y, w, h = pos
    v = {
        "visualType": vtype,
        "query": {"queryState": {r: role(refs) for r, refs in roles.items()}},
        "drillFilterOtherVisuals": True,
    }
    if sort:
        ref, direction = sort
        f, _, _ = field(ref)
        v["query"]["sortDefinition"] = {"sort": [{"field": f, "direction": direction}], "isDefaultSort": False}
    if objects:
        v["objects"] = objects
    if title:
        v["visualContainerObjects"] = title_objects(title)
    return {
        "$schema": VC_SCHEMA,
        "name": vname(page, key),
        "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 0},
        "visual": v,
    }


def slicer(page, key, pos, ref, mode, title=None, single=False):
    objects = {"data": [{"properties": {"mode": lit(s(mode))}}]}
    if single:
        objects["selection"] = [{"properties": {"singleSelect": lit("true"), "strictSingleSelect": lit("true")}}]
    return visual(page, key, "slicer", pos, {"Values": [ref]}, title=title, objects=objects)


def card(page, key, pos, measure):
    return visual(page, key, "card", pos, {"Values": [measure]})


# ------------------------------------------------------------- filters

def categorical(name, table, column, values, exclude=False):
    """Page/report filter: table[column] IN values (or NOT IN)."""
    alias = table[0].lower()
    cond = {"In": {
        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": alias}}, "Property": column}}],
        "Values": [[{"Literal": {"Value": v}}] for v in values],
    }}
    if exclude:
        cond = {"Not": {"Expression": cond}}
    return {
        "name": name,
        "field": col(table, column),
        "type": "Categorical",
        "filter": {"Version": 2, "From": [{"Name": alias, "Entity": table, "Type": 0}], "Where": [{"Condition": cond}]},
        "howCreated": "User",
    }


NO_AUTOMATION = categorical("noAutomation", "Tech", "Is Automation", ["false"])
NO_ALERTS = categorical("noAlertsBoard", "Board", "Is Alerts", ["false"])

# --------------------------------------------------------------- pages
# Canvas 1280 x 720. Positions are (x, y, width, height).

def tickets_by_tech(p):
    return [
        slicer(p, "date", (10, 8, 300, 72), "Calendar[Date]", "Between", "Date range"),
        slicer(p, "period", (320, 8, 150, 72), PERIOD, "Dropdown", "Group by", single=True),
        slicer(p, "board", (480, 8, 230, 72), "Board[Board]", "Dropdown", "Board"),
        card(p, "c_assigned", (720, 8, 135, 72), "[Tickets Assigned]"),
        card(p, "c_closed", (860, 8, 135, 72), "[Tickets Closed]"),
        card(p, "c_open", (1000, 8, 135, 72), "[Open Now]"),
        card(p, "c_waiting", (1140, 8, 130, 72), "[Waiting Now]"),
        visual(p, "matrix", "pivotTable", (10, 90, 760, 620),
               {"Rows": ["Tech[Tech]"], "Columns": [PERIOD],
                "Values": ["[Tickets Assigned]", "[Tickets Closed]"]},
               title="Assigned vs. closed per tech", sort=("[Tickets Closed]", "Descending")),
        visual(p, "backlog", "tableEx", (780, 90, 490, 360),
               {"Values": ["Tech[Tech]", "[Open Now]", "[Waiting Now]", "[Avg Open Age (Days)]",
                           "[Tickets Resolved]", "[Assigned - Closed]", "[Close Rate]"]},
               title="Current backlog & throughput", sort=("[Open Now]", "Descending")),
        visual(p, "trend", "clusteredColumnChart", (780, 460, 490, 250),
               {"Category": [PERIOD], "Y": ["[Tickets Assigned]", "[Tickets Closed]"]},
               title="Assigned vs. closed over time"),
    ]


def sla(p):
    return [
        slicer(p, "date", (10, 8, 300, 72), "Calendar[Date]", "Between", "Date range"),
        slicer(p, "period", (320, 8, 150, 72), PERIOD, "Dropdown", "Group by", single=True),
        slicer(p, "board", (480, 8, 230, 72), "Board[Board]", "Dropdown", "Board"),
        card(p, "c_resp", (10, 90, 245, 90), "[Response SLA %]"),
        card(p, "c_res", (265, 90, 245, 90), "[Resolution SLA %]"),
        card(p, "c_mresp", (520, 90, 245, 90), "[Median Response Hours]"),
        card(p, "c_mres", (775, 90, 245, 90), "[Median Resolution Hours]"),
        card(p, "c_out", (1030, 90, 240, 90), "[Out of SLA Now]"),
        visual(p, "trend", "lineChart", (10, 190, 620, 250),
               {"Category": [PERIOD], "Y": ["[Response SLA %]", "[Resolution SLA %]"]},
               title="SLA attainment over time"),
        visual(p, "byboard", "clusteredBarChart", (640, 190, 630, 250),
               {"Category": ["Board[Board]"], "Y": ["[Response SLA %]", "[Resolution SLA %]"]},
               title="SLA by board", sort=("[Response SLA %]", "Ascending")),
        visual(p, "priority", "pivotTable", (10, 450, 620, 260),
               {"Rows": ["Tickets[Priority]"],
                "Values": ["[Responses With SLA Target]", "[Response SLA %]", "[Avg Response Hours]",
                           "[Resolutions With SLA Target]", "[Resolution SLA %]", "[Avg Resolution Hours]"]},
               title="SLA by priority"),
        visual(p, "tech", "tableEx", (640, 450, 630, 260),
               {"Values": ["Tech[Tech]", "[Responses With SLA Target]", "[Response SLA %]", "[Avg Response Hours]",
                           "[Resolution SLA %]", "[Avg Resolution Hours]"]},
               title="SLA by ticket owner", sort=("[Responses With SLA Target]", "Descending")),
    ]


def utilization(p):
    return [
        slicer(p, "date", (10, 8, 300, 72), "Calendar[Date]", "Between", "Date range"),
        slicer(p, "period", (320, 8, 150, 72), PERIOD, "Dropdown", "Group by", single=True),
        slicer(p, "cwutil", (480, 8, 230, 72), "Tech[In CW Utilization]", "Dropdown", "In CW utilization"),
        card(p, "c_util", (720, 8, 180, 72), "[Utilization %]"),
        card(p, "c_bill", (905, 8, 180, 72), "[Billable %]"),
        card(p, "c_hours", (1090, 8, 180, 72), "[Hours Logged]"),
        visual(p, "matrix", "pivotTable", (10, 90, 760, 380),
               {"Rows": ["Tech[Tech]"],
                "Values": ["[Available Hours]", "[Hours Logged]", "[Utilized Hours]", "[Billable Hours]",
                           "[PTO Hours]", "[Utilization %]", "[Billable %]"]},
               title="Utilization by tech", sort=("[Utilization %]", "Descending")),
        visual(p, "trend", "clusteredColumnChart", (780, 90, 490, 380),
               {"Category": [PERIOD], "Y": ["[Utilization %]", "[Billable %]"]},
               title="Utilization over time"),
        visual(p, "worktype", "clusteredBarChart", (10, 480, 760, 230),
               {"Category": ["TimeEntries[Work Type]"], "Y": ["[Hours Logged]"]},
               title="Hours by work type", sort=("[Hours Logged]", "Descending")),
        visual(p, "charge", "donutChart", (780, 480, 490, 230),
               {"Category": ["TimeEntries[Charge Type]"], "Y": ["[Hours Logged]"]},
               title="Ticket vs. project vs. admin time"),
    ]


def client_profitability(p):
    return [
        slicer(p, "year", (10, 8, 160, 72), "Calendar[Year]", "Dropdown", "Year"),
        slicer(p, "month", (180, 8, 200, 72), "Calendar[Month]", "Dropdown", "Month"),
        slicer(p, "ctype", (390, 8, 250, 72), "Company[Company Types]", "Dropdown", "Company type"),
        card(p, "c_rev", (650, 8, 150, 72), "[Normalized Revenue]"),
        card(p, "c_cost", (805, 8, 150, 72), "[Client Total Cost]"),
        card(p, "c_margin", (960, 8, 150, 72), "[Client Margin]"),
        card(p, "c_pct", (1115, 8, 155, 72), "[Client Margin %]"),
        visual(p, "table", "tableEx", (10, 90, 1260, 340),
               {"Values": ["Company[Company]", "[Normalized Revenue]", "[Invoiced Revenue]", "[Client Labor Cost]",
                           "[Client Addition Cost]", "[Client Product Cost]", "[Client Margin]", "[Client Margin %]",
                           "[Client Hours]", "[Client Effective Hourly Rate]", "[Client Uncosted Hours]"]},
               title="Profitability by client", sort=("[Normalized Revenue]", "Descending")),
        visual(p, "scatter", "scatterChart", (10, 440, 620, 270),
               {"Category": ["Company[Company]"], "X": ["[Normalized Revenue]"], "Y": ["[Client Margin %]"],
                "Size": ["[Client Hours]"]},
               title="Revenue vs. margin % (bubble = hours)"),
        visual(p, "trend", "lineChart", (640, 440, 630, 270),
               {"Category": ["Calendar[Month]"], "Y": ["[Normalized Revenue]", "[Invoiced Revenue]", "[Client Margin]"]},
               title="Revenue and margin by month"),
    ]


def contract_profitability(p):
    return [
        slicer(p, "year", (10, 8, 160, 72), "Calendar[Year]", "Dropdown", "Year"),
        slicer(p, "month", (180, 8, 200, 72), "Calendar[Month]", "Dropdown", "Month"),
        slicer(p, "atype", (390, 8, 250, 72), "Agreement[Agreement Type]", "Dropdown", "Agreement type"),
        slicer(p, "astatus", (650, 8, 180, 72), "Agreement[Agreement Status]", "Dropdown", "Status"),
        card(p, "c_rev", (840, 8, 140, 72), "[Agreement Revenue]"),
        card(p, "c_margin", (985, 8, 140, 72), "[Agreement Margin]"),
        card(p, "c_pct", (1130, 8, 140, 72), "[Agreement Margin %]"),
        visual(p, "table", "tableEx", (10, 90, 1260, 400),
               {"Values": ["Agreement[Agreement]", "Agreement[Agreement Type]", "Company[Company]",
                           "[Agreement Revenue]", "[Agreement Labor Cost]", "[Agreement Addition Cost]",
                           "[Agreement Margin]", "[Agreement Margin %]", "[Agreement Hours]",
                           "[Agreement Effective Hourly Rate]"]},
               title="Profitability by agreement", sort=("[Agreement Revenue]", "Descending")),
        visual(p, "bytype", "clusteredBarChart", (10, 500, 620, 210),
               {"Category": ["Agreement[Agreement Type]"], "Y": ["[Agreement Margin %]"]},
               title="Margin % by agreement type", sort=("[Agreement Margin %]", "Ascending")),
        visual(p, "trend", "lineChart", (640, 500, 630, 210),
               {"Category": ["Calendar[Month]"], "Y": ["[Agreement Revenue]", "[Agreement Margin]"]},
               title="Agreement revenue and margin by month"),
    ]


# (folder name Desktop created, display name, builder, page filters)
PAGES = [
    ("ReportSection0", "Tickets by Tech", tickets_by_tech, [NO_AUTOMATION, NO_ALERTS]),
    ("ReportSection1", "SLA", sla, [NO_AUTOMATION, NO_ALERTS]),
    ("ReportSection2", "Utilization", utilization, [NO_AUTOMATION]),
    ("ReportSection3", "Client Profitability", client_profitability, []),
    ("ReportSection4", "Contract Profitability", contract_profitability, []),
]

REPORT_FILTERS = [categorical("fullHistory", "Calendar", "Is Full History", ["true"])]

# ----------------------------------------------------------- validation

def validator_for(schema_url):
    from jsonschema import Draft7Validator
    from referencing import Registry, Resource
    mirror = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "Temp" / "th2schemas" / "mirror"
    web = "https://developer.microsoft.com/json-schemas/"
    raw = "https://raw.githubusercontent.com/microsoft/json-schemas/main/"

    def retrieve(uri):
        rel = uri.split(web, 1)[-1] if uri.startswith(web) else uri
        path = mirror / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(raw + rel, timeout=60) as r:
                path.write_bytes(r.read())
        return Resource.from_contents(json.loads(path.read_text(encoding="utf-8")))

    registry = Registry(retrieve=retrieve)
    schema = retrieve(schema_url).contents
    return Draft7Validator({**schema, "$id": schema_url}, registry=registry)


def validate(docs):
    validators = {}
    errors = []
    for path, doc in docs:
        url = doc["$schema"]
        if url not in validators:
            validators[url] = validator_for(url)
        for e in validators[url].iter_errors(doc):
            errors.append(f"{path}: {'/'.join(map(str, e.absolute_path))}: {e.message[:300]}")
    return errors


def model_objects():
    """{table: {column/measure names}} parsed from the TMDL model files."""
    import re
    objs = {}
    for f in (ROOT / "HelpdeskMetrics.SemanticModel" / "definition" / "tables").glob("*.tmdl"):
        table = None
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^table\s+'?(.*?)'?\s*$", line)
            if m:
                table = m.group(1)
                objs[table] = set()
            m = re.match(r"^\t(column|measure)\s+('(?:[^']|'')*'|\S+)", line)
            if m and table:
                objs[table].add(m.group(2).strip("'").replace("''", "'"))
    return objs


def check_model_references(docs):
    """Every Column/Measure a visual or filter uses must exist in the model —
    the schema can't know that, and Desktop would show a broken visual."""
    objs = model_objects()
    missing = set()

    def walk(o):
        if isinstance(o, dict):
            for kind in ("Column", "Measure"):
                if kind in o and isinstance(o[kind], dict):
                    src = o[kind]["Expression"]["SourceRef"]
                    table = src.get("Entity")
                    if table and o[kind]["Property"] not in objs.get(table, set()):
                        missing.add(f"{kind} {table}[{o[kind]['Property']}]")
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for _, doc in docs:
        walk(doc)
    if missing:
        raise SystemExit("Fields not in the model — nothing written:\n  " + "\n  ".join(sorted(missing)))


# --------------------------------------------------------------- main

def main():
    load_measure_tables()
    docs = []   # (path, json)
    for folder, display, build, filters in PAGES:
        page_dir = DEF / "pages" / folder
        page = json.loads((page_dir / "page.json").read_text(encoding="utf-8"))
        page["displayName"] = display
        if filters:
            page["filterConfig"] = {"filters": filters}
        else:
            page.pop("filterConfig", None)
        docs.append((page_dir / "page.json", page))
        for v in build(folder):
            docs.append((page_dir / "visuals" / v["name"] / "visual.json", v))

    report_path = DEF / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["filterConfig"] = {"filters": REPORT_FILTERS}
    docs.append((report_path, report))

    check_model_references(docs)
    names = [d["name"] for p, d in docs if p.name == "visual.json"]
    if len(names) != len(set(names)):
        raise SystemExit("Duplicate visual names")

    errors = validate(docs)
    if errors:
        raise SystemExit("Schema validation failed — nothing written:\n  " + "\n  ".join(errors[:40]))

    for folder, *_ in PAGES:
        shutil.rmtree(DEF / "pages" / folder / "visuals", ignore_errors=True)
    for path, doc in docs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {len(PAGES)} pages, {len(names)} visuals; all files valid against the PBIR schemas.")


if __name__ == "__main__":
    main()
