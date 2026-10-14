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


def visual(page, key, vtype, pos, roles, title=None, sort=None, objects=None, filters=None):
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
    container = {
        "$schema": VC_SCHEMA,
        "name": vname(page, key),
        "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 0},
        "visual": v,
    }
    if filters:
        # Visual-level filters: scope only this visual (e.g. which options a
        # slicer lists), not the rest of the page.
        container["filterConfig"] = {"filters": filters}
    return container


def slicer(page, key, pos, ref, mode, title=None, single=False, filters=None):
    objects = {"data": [{"properties": {"mode": lit(s(mode))}}]}
    if single:
        objects["selection"] = [{"properties": {"singleSelect": lit("true"), "strictSingleSelect": lit("true")}}]
    v = visual(page, key, "slicer", pos, {"Values": [ref]}, title=title, objects=objects, filters=filters)
    # A slicer that CHOOSES the field parameter (lists Month/Week/Day) must
    # NOT carry fieldParameters metadata — that marks the slicer as one whose
    # field is SWITCHED by the parameter, and Desktop then turns it into a
    # Calendar[Month]/[Week]/[Date] value picker. With single-select that
    # pinned every visual to one month (2026-10-14). Only visuals that
    # display the parameter (matrix columns, chart axes) get fieldParameters.
    for state in v["visual"]["query"]["queryState"].values():
        state.pop("fieldParameters", None)
    return v


# Project boards never have tickets here (project tickets aren't synced), so
# they'd only ever show as empty options in the Board dropdown.
PROJECT_BOARDS = [
    "Backup Services Project", "Demo Project", "Managed Services Project",
    "Professional Services Project", "Technical Services Project",
]
# Retired board whose leftover open tickets management doesn't want counted.
EXCLUDED_BOARDS = ["z(inactive)Quartus - Managed Services"]


def period_value_slicer(page, pos):
    """The "Period" picker: lists the VALUES of whichever grain Group by
    selected (months, weeks or days) and filters the page to the ones
    picked; nothing picked = everything. This is the dynamic-slicer pattern
    — the slicer's field is switched by the Period field parameter — in the
    exact form Desktop saves it (all three Calendar fields + fieldParameters
    length 3). Multi-select, no default selection."""
    fields = [("Calendar[Month]", "Month", True), ("Calendar[Week]", "Week", False), ("Calendar[Date]", "Day", False)]
    projections = []
    for ref, display, active in fields:
        proj = projection(ref)
        proj.update(active=active, displayName=display)
        projections.append(proj)
    param, _, _ = field(PERIOD)
    v = visual(page, "period_value", "slicer", pos, {"Values": ["Calendar[Month]"]}, title="Period",
               objects={"data": [{"properties": {"mode": lit(s("Dropdown"))}}]})
    v["visual"]["query"]["queryState"] = {"Values": {
        "projections": projections,
        "fieldParameters": [{"parameterExpr": param, "index": 0, "length": 3}],
    }}
    v["filterConfig"] = {"filters": [
        {"name": vname(page, f"period_value_{c}"), "field": col("Calendar", c), "type": "Categorical"}
        for c in ("Month", "Week", "Date")
    ]}
    return v


def period_slicers(page, x, w):
    """Group by (grain) above Period (which month/week/day), sharing the top band."""
    half = TOP_H // 2 - 2
    return [
        slicer(page, "period", (x, TOP_Y, w, half), PERIOD, "Dropdown", "Group by", single=True),
        period_value_slicer(page, (x, TOP_Y + half + 4, w, half)),
    ]


def trend_interactions(page, visuals):
    """Trend charts (Group-by axis) ignore the Period picker, so they always
    show the whole timeline while everything else filters to the pick."""
    source = vname(page, "period_value")
    if not any(v["name"] == source for v in visuals):
        return []
    return [{"source": source, "target": v["name"], "type": "NoFilter"}
            for v in visuals
            if v["visual"]["visualType"] != "slicer"
            and "fieldParameters" in v["visual"]["query"]["queryState"].get("Category", {})]


def board_slicer(page, pos):
    """Board dropdown listing only active, non-project boards. Inactive
    boards — the "z(Inactive)…", "z(DO NOT USE)…" and "zzzz… (DO NOT USE)"
    ones, all marked inactive in ConnectWise — are hidden from the options."""
    active = categorical(f"activeBoards_{page}", "Board", "Is Active", ["true"])
    no_projects = categorical(f"noProjectBoards_{page}", "Board", "Board",
                              [s(b) for b in PROJECT_BOARDS], exclude=True)
    return slicer(page, "board", pos, "Board[Board]", "Dropdown", "Board", filters=[active, no_projects])


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
ACTIVE_BOARDS_ONLY = categorical("activeBoardsOnly", "Board", "Is Active", ["true"])
NO_EXCLUDED_BOARDS = categorical("excludedBoards", "Board", "Board", [s(b) for b in EXCLUDED_BOARDS], exclude=True)

# --------------------------------------------------------------- pages
# Canvas 1280 x 720. Positions are (x, y, width, height).
# Top band (slicers + KPI cards) is TOP_H tall — cards need ~100px or their
# value and label get clipped. Content starts at BODY_Y.
TOP_Y, TOP_H = 8, 104
BODY_Y = TOP_Y + TOP_H + 10


def kpi_row(p, x0, measures, width=1270):
    """Evenly spaced cards from x0 to the right edge of the canvas."""
    gap = 8
    w = (width - x0 - gap * (len(measures) - 1)) / len(measures)
    return [card(p, f"c{i}", (round(x0 + i * (w + gap)), TOP_Y, round(w), TOP_H), m)
            for i, m in enumerate(measures)]


def tickets_by_tech(p):
    return [
        *period_slicers(p, 10, 200),
        board_slicer(p, (220, TOP_Y, 280, TOP_H)),
        *kpi_row(p, 510, ["[Tickets Assigned]", "[Tickets Closed]", "[Open Now]", "[Waiting Now]"]),
        visual(p, "matrix", "pivotTable", (10, BODY_Y, 760, 720 - BODY_Y - 8),
               {"Rows": ["Tech[Tech]"], "Columns": [PERIOD],
                "Values": ["[Tickets Assigned]", "[Tickets Closed]"]},
               title="Assigned vs. closed per tech", sort=("[Tickets Closed]", "Descending")),
        visual(p, "backlog", "tableEx", (780, BODY_Y, 490, 330),
               {"Values": ["Tech[Tech]", "[Open Now]", "[Waiting Now]", "[Avg Open Age (Days)]",
                           "[Tickets Resolved]", "[Assigned - Closed]", "[Close Rate]"]},
               title="Current backlog & throughput", sort=("[Open Now]", "Descending")),
        visual(p, "trend", "clusteredColumnChart", (780, BODY_Y + 340, 490, 720 - BODY_Y - 348),
               {"Category": [PERIOD], "Y": ["[Tickets Assigned]", "[Tickets Closed]"]},
               title="Assigned vs. closed over time"),
        # Below the original 720px layout (page is taller, fits to width).
        visual(p, "backlog_trend", "lineChart", (10, 730, 1260, 300),
               {"Category": [PERIOD], "Y": ["[Open at End of Period]"]},
               title="Open backlog at end of each period (by current owner; Resolved counts as done)"),
    ]


def sla(p):
    return [
        *period_slicers(p, 10, 200),
        board_slicer(p, (220, TOP_Y, 280, TOP_H)),
        *kpi_row(p, 510, ["[Response SLA %]", "[Resolution SLA %]", "[Median Response Hours]",
                          "[Median Resolution Hours]", "[Out of SLA Now]"]),
        visual(p, "trend", "lineChart", (10, BODY_Y, 620, 240),
               {"Category": [PERIOD], "Y": ["[Response SLA %]", "[Resolution SLA %]"]},
               title="SLA attainment over time"),
        visual(p, "byboard", "clusteredBarChart", (640, BODY_Y, 630, 240),
               {"Category": ["Board[Board]"], "Y": ["[Response SLA %]", "[Resolution SLA %]"]},
               title="SLA by board", sort=("[Response SLA %]", "Ascending")),
        visual(p, "priority", "pivotTable", (10, BODY_Y + 250, 620, 720 - BODY_Y - 258),
               {"Rows": ["Tickets[Priority]"],
                "Values": ["[Responses With SLA Target]", "[Response SLA %]", "[Avg Response Hours]",
                           "[Resolutions With SLA Target]", "[Resolution SLA %]", "[Avg Resolution Hours]"]},
               title="SLA by priority"),
        visual(p, "tech", "tableEx", (640, BODY_Y + 250, 630, 720 - BODY_Y - 258),
               {"Values": ["Tech[Tech]", "[Responses With SLA Target]", "[Response SLA %]", "[Avg Response Hours]",
                           "[Resolution SLA %]", "[Avg Resolution Hours]"]},
               title="SLA by ticket owner", sort=("[Responses With SLA Target]", "Descending")),
    ]


def utilization(p):
    return [
        *period_slicers(p, 10, 200),
        slicer(p, "cwutil", (220, TOP_Y, 230, TOP_H), "Tech[In CW Utilization]", "Dropdown", "In CW utilization"),
        *kpi_row(p, 460, ["[Utilization %]", "[Billable %]", "[Hours Logged]"]),
        visual(p, "matrix", "pivotTable", (10, BODY_Y, 760, 360),
               {"Rows": ["Tech[Tech]"],
                "Values": ["[Available Hours]", "[Hours Logged]", "[Utilized Hours]", "[Billable Hours]",
                           "[PTO Hours]", "[Utilization %]", "[Billable %]"]},
               title="Utilization by tech", sort=("[Utilization %]", "Descending")),
        visual(p, "trend", "clusteredColumnChart", (780, BODY_Y, 490, 360),
               {"Category": [PERIOD], "Y": ["[Utilization %]", "[Billable %]"]},
               title="Utilization over time"),
        visual(p, "worktype", "clusteredBarChart", (10, BODY_Y + 370, 760, 720 - BODY_Y - 378),
               {"Category": ["TimeEntries[Work Type]"], "Y": ["[Hours Logged]"]},
               title="Hours by work type", sort=("[Hours Logged]", "Descending")),
        visual(p, "charge", "donutChart", (780, BODY_Y + 370, 490, 720 - BODY_Y - 378),
               {"Category": ["TimeEntries[Charge Type]"], "Y": ["[Hours Logged]"]},
               title="Ticket vs. project vs. admin time"),
    ]


def client_profitability(p):
    return [
        slicer(p, "year", (10, TOP_Y, 150, TOP_H), "Calendar[Year]", "Dropdown", "Year"),
        slicer(p, "month", (170, TOP_Y, 190, TOP_H), "Calendar[Month]", "Dropdown", "Month"),
        slicer(p, "ctype", (370, TOP_Y, 250, TOP_H), "Company[Company Types]", "Dropdown", "Company type"),
        *kpi_row(p, 630, ["[Normalized Revenue]", "[Client Total Cost]", "[Client Margin]", "[Client Margin %]"]),
        visual(p, "table", "tableEx", (10, BODY_Y, 1260, 320),
               {"Values": ["Company[Company]", "[Normalized Revenue]", "[Invoiced Revenue]", "[Client Labor Cost]",
                           "[Client Addition Cost]", "[Client Product Cost]", "[Client Margin]", "[Client Margin %]",
                           "[Client Hours]", "[Client Effective Hourly Rate]", "[Client Uncosted Hours]"]},
               title="Profitability by client", sort=("[Normalized Revenue]", "Descending")),
        visual(p, "scatter", "scatterChart", (10, BODY_Y + 330, 620, 720 - BODY_Y - 338),
               {"Category": ["Company[Company]"], "X": ["[Normalized Revenue]"], "Y": ["[Client Margin %]"],
                "Size": ["[Client Hours]"]},
               title="Revenue vs. margin % (bubble = hours)"),
        visual(p, "trend", "lineChart", (640, BODY_Y + 330, 630, 720 - BODY_Y - 338),
               {"Category": ["Calendar[Month]"], "Y": ["[Normalized Revenue]", "[Invoiced Revenue]", "[Client Margin]"]},
               title="Revenue and margin by month"),
    ]


def contract_profitability(p):
    return [
        slicer(p, "year", (10, TOP_Y, 140, TOP_H), "Calendar[Year]", "Dropdown", "Year"),
        slicer(p, "month", (160, TOP_Y, 170, TOP_H), "Calendar[Month]", "Dropdown", "Month"),
        slicer(p, "atype", (340, TOP_Y, 220, TOP_H), "Agreement[Agreement Type]", "Dropdown", "Agreement type"),
        slicer(p, "astatus", (570, TOP_Y, 170, TOP_H), "Agreement[Agreement Status]", "Dropdown", "Status"),
        *kpi_row(p, 750, ["[Agreement Revenue]", "[Agreement Margin]", "[Agreement Margin %]"]),
        visual(p, "table", "tableEx", (10, BODY_Y, 1260, 370),
               {"Values": ["Agreement[Agreement]", "Agreement[Agreement Type]", "Company[Company]",
                           "[Agreement Revenue]", "[Agreement Labor Cost]", "[Agreement Addition Cost]",
                           "[Agreement Margin]", "[Agreement Margin %]", "[Agreement Hours]",
                           "[Agreement Effective Hourly Rate]"]},
               title="Profitability by agreement", sort=("[Agreement Revenue]", "Descending")),
        visual(p, "bytype", "clusteredBarChart", (10, BODY_Y + 380, 620, 720 - BODY_Y - 388),
               {"Category": ["Agreement[Agreement Type]"], "Y": ["[Agreement Margin %]"]},
               title="Margin % by agreement type", sort=("[Agreement Margin %]", "Ascending")),
        visual(p, "trend", "lineChart", (640, BODY_Y + 380, 630, 720 - BODY_Y - 388),
               {"Category": ["Calendar[Month]"], "Y": ["[Agreement Revenue]", "[Agreement Margin]"]},
               title="Agreement revenue and margin by month"),
    ]


def tickets_by_client(p):
    """Client monitoring page (taller canvas: scrolls, fits to width).
    Clicking a client in the overview cross-filters everything else."""
    def recent(tag):   # filter names must be unique across the whole report
        return categorical(f"recentClients_{p}_{tag}", "Company", "Has Recent Tickets", ["true"])
    open_only = categorical(f"openOnly_{p}", "Tickets", "Is Open", ["true"])
    return [
        *period_slicers(p, 10, 200),
        slicer(p, "client", (220, TOP_Y, 300, TOP_H), "Company[Company]", "Dropdown", "Client",
               filters=[recent("slicer")]),
        board_slicer(p, (530, TOP_Y, 230, TOP_H)),
        *kpi_row(p, 770, ["[Tickets Opened]", "[Tickets Closed]", "[Open Now]", "[After-Hours %]"]),
        visual(p, "overview", "tableEx", (10, BODY_Y, 1260, 400),
               {"Values": ["Company[Company]", "[Tickets Opened]", "[Tickets Closed]", "[Open Now]", "[Waiting Now]",
                           "[Avg Open Age (Days)]", "[Max Open Age (Days)]", "[Response SLA %]", "[Resolution SLA %]",
                           "[Hours Logged]", "[Avg Hours per Closed Ticket]", "[After-Hours %]", "[Phone %]",
                           "[Requesting Contacts]", "[Revenue per Ticket]"]},
               title="Client overview (click a client to focus the page)",
               sort=("[Tickets Opened]", "Descending"), filters=[recent("overview")]),
        # Flat table, not a Company > Tech matrix: a matrix opens collapsed
        # at the client level with its expand buttons hidden by default, so
        # the tech rows were never visible.
        visual(p, "bytech", "tableEx", (10, BODY_Y + 410, 620, 340),
               {"Values": ["Company[Company]", "Tech[Tech]", "[Tickets Assigned]", "[Tickets Closed]",
                           "[Open Now]"]},
               title="Tickets per client by tech", sort=("[Tickets Assigned]", "Descending")),
        visual(p, "category", "barChart", (640, BODY_Y + 410, 630, 340),
               {"Category": ["Tickets[Subtype]"], "Series": ["Tickets[Type]"], "Y": ["[Tickets Opened]"]},
               title="Tickets by category (subtype, split by type)", sort=("[Tickets Opened]", "Descending")),
        visual(p, "trend", "clusteredColumnChart", (10, BODY_Y + 760, 410, 300),
               {"Category": [PERIOD], "Y": ["[Tickets Opened]", "[Tickets Closed]"]},
               title="Opened vs. closed over time"),
        visual(p, "contacts", "clusteredBarChart", (430, BODY_Y + 760, 410, 300),
               {"Category": ["Tickets[Contact]"], "Y": ["[Tickets Opened]"]},
               title="Top requesters", sort=("[Tickets Opened]", "Descending")),
        visual(p, "source", "donutChart", (850, BODY_Y + 760, 420, 300),
               {"Category": ["Tickets[Source]"], "Y": ["[Tickets Opened]"]},
               title="Intake channel"),
        visual(p, "openlist", "tableEx", (10, BODY_Y + 1070, 1260, 420),
               {"Values": ["Tickets[TicketId]", "Company[Company]", "Tickets[Summary]", "Tickets[Status]",
                           "Tickets[Priority]", "Tickets[Subtype]", "Tech[Tech]", "Tickets[Open Age Days]"]},
               title="Open tickets (oldest first)", sort=("Tickets[Open Age Days]", "Descending"),
               filters=[open_only]),
    ]


def phones(p):
    """Manager's review of techs on the phones (Dialpad), by Group-by period."""
    tech_filters = [categorical(f"activeTechs_{p}", "Tech", "Is Active", ["true"]),
                    categorical(f"noAutomationTechs_{p}", "Tech", "Is Automation", ["false"])]
    return [
        *period_slicers(p, 10, 200),
        slicer(p, "tech", (220, TOP_Y, 260, TOP_H), "Tech[Tech]", "Dropdown", "Tech", filters=tech_filters),
        *kpi_row(p, 490, ["[Calls Answered]", "[Calls Missed]", "[Answer Rate]", "[Talk Hours]",
                          "[Avg Talk Time (min)]", "[Outbound Calls]"]),
        visual(p, "scorecard", "tableEx", (10, BODY_Y, 1260, 420),
               {"Values": ["Tech[Tech]", "[Inbound Calls]", "[Calls Answered]", "[Calls Missed]", "[Answer Rate]",
                           "[Rejected Calls]", "[Ring No Answer]", "[Voicemails]", "[Outbound Calls]",
                           "[Outbound Connected]", "[Talk Hours]", "[Avg Talk Time (min)]", "[Avg Hold Time (min)]",
                           "[Avg Ring Time (sec)]", "[Transfers Out]", "[Calls per Day]",
                           "[Talk % of Available Hours]", "[Tickets Closed]"]},
               title="Phone scorecard by tech", sort=("[Calls Answered]", "Descending")),
        visual(p, "matrix", "pivotTable", (10, BODY_Y + 430, 760, 360),
               {"Rows": ["Tech[Tech]"], "Columns": [PERIOD],
                "Values": ["[Calls Answered]", "[Calls Missed]", "[Talk Hours]"]},
               title="Answered, missed and talk hours per tech", sort=("[Calls Answered]", "Descending")),
        visual(p, "trend", "clusteredColumnChart", (780, BODY_Y + 430, 490, 360),
               {"Category": [PERIOD], "Y": ["[Calls Answered]", "[Calls Missed]"]},
               title="Calls over time"),
        visual(p, "talk", "clusteredBarChart", (10, BODY_Y + 800, 620, 300),
               {"Category": ["Tech[Tech]"], "Y": ["[Talk Hours]"]},
               title="Talk hours by tech", sort=("[Talk Hours]", "Descending")),
        visual(p, "answerrate", "clusteredBarChart", (640, BODY_Y + 800, 630, 300),
               {"Category": ["Tech[Tech]"], "Y": ["[Answer Rate]"]},
               title="Answer rate by tech", sort=("[Answer Rate]", "Ascending")),
    ]


# (folder name Desktop created, display name, builder, page filters)
PAGES = [
    # Alerts included on every page since 2026-10-14 (per Morgan) — use the
    # Board picker to look at it, or at everything but it.
    ("ReportSection0", "Tickets by Tech", tickets_by_tech, [NO_AUTOMATION, NO_EXCLUDED_BOARDS]),
    ("ReportSection1", "SLA", sla, [NO_AUTOMATION, NO_EXCLUDED_BOARDS]),
    ("ReportSection2", "Utilization", utilization, [NO_AUTOMATION]),
    ("ReportSection3", "Client Profitability", client_profitability, []),
    ("ReportSection4", "Contract Profitability", contract_profitability, []),
    # Every client's tickets count here — including Alerts and tickets closed
    # by automation (per Morgan, 2026-10-14) — only inactive boards (the
    # "(DO NOT USE)" / "(Inactive)" ones, Quartus included) are excluded.
    ("ReportSection5", "Tickets by Client", tickets_by_client, [ACTIVE_BOARDS_ONLY]),
    ("ReportSection6", "Phones", phones, [NO_AUTOMATION]),
]

# Pages that don't fit 1280x720 (scroll vertically, fit to width).
PAGE_SIZES = {"ReportSection0": (1280, 730 + 300 + 10),
              "ReportSection5": (1280, BODY_Y + 1070 + 420 + 10),
              "ReportSection6": (1280, BODY_Y + 800 + 300 + 10)}
# Display order of pages in the report (by folder name).
PAGE_ORDER = ["ReportSection0", "ReportSection5", "ReportSection6", "ReportSection1", "ReportSection2",
              "ReportSection3", "ReportSection4"]
PAGES_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.1.0/schema.json"

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
    """Validates each doc against the schema it declares. Desktop can write a
    schema version newer than Microsoft has published yet (it saved
    visualContainer 2.13.0 in Oct 2026 while 2.12.0 was the latest online);
    those fall back to the newest published version of the same schema, which
    flags anything 2.13-specific rather than silently passing it."""
    import urllib.error
    validators, used = {}, {}
    errors = []
    fallbacks = {"visualContainer": VC_SCHEMA, "page": PAGE_SCHEMA, "report": REPORT_SCHEMA}
    for path, doc in docs:
        url = doc["$schema"]
        if url not in validators:
            try:
                validators[url], used[url] = validator_for(url), url
            except urllib.error.HTTPError:
                kind = url.rstrip("/").split("/")[-3]
                if kind not in fallbacks:
                    raise
                print(f"  note: {url.split('/definition/')[-1]} not published yet — validating against "
                      f"{fallbacks[kind].split('/definition/')[-1]}")
                validators[url], used[url] = validator_for(fallbacks[kind]), fallbacks[kind]
        # Each schema pins its own $schema URL; validate a copy declaring the
        # schema actually used (the file on disk keeps what Desktop wrote).
        checked = doc if used[url] == url else {**doc, "$schema": used[url]}
        for e in validators[url].iter_errors(checked):
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
    """py generate_report.py ReportSection5     -> build only the named pages;
       all other pages are left exactly as Desktop last saved them.
       py generate_report.py --all              -> rebuild EVERY page in PAGES
       (discards any edits made to those pages in Desktop)."""
    load_measure_tables()
    args = sys.argv[1:]
    if not args:
        raise SystemExit("Name the page folder(s) to build, e.g. `py generate_report.py ReportSection5`, "
                         "or pass --all to rebuild every page (discards Desktop edits to them). "
                         f"Pages: {[(p[0], p[1]) for p in PAGES]}")
    only = set() if args == ["--all"] else set(args)
    unknown = only - {p[0] for p in PAGES}
    if unknown:
        raise SystemExit(f"Unknown page folder(s): {sorted(unknown)} — choose from {[p[0] for p in PAGES]}")
    pages = [p for p in PAGES if not only or p[0] in only]
    docs = []   # (path, json)
    for folder, display, build, filters in pages:
        page_dir = DEF / "pages" / folder
        if (page_dir / "page.json").exists():
            page = json.loads((page_dir / "page.json").read_text(encoding="utf-8"))
        else:
            page = {"$schema": PAGE_SCHEMA, "name": folder, "displayOption": "FitToPage",
                    "height": 720, "width": 1280}
        page["displayName"] = display
        if folder in PAGE_SIZES:
            page["width"], page["height"] = PAGE_SIZES[folder]
            page["displayOption"] = "FitToWidth"
        if filters:
            page["filterConfig"] = {"filters": filters}
        else:
            page.pop("filterConfig", None)
        docs.append((page_dir / "page.json", page))
        built = build(folder)
        ours = {i["source"] for i in trend_interactions(folder, built)}
        keep = [i for i in page.get("visualInteractions", []) if i.get("source") not in ours]
        if trend_interactions(folder, built) or keep:
            page["visualInteractions"] = keep + trend_interactions(folder, built)
        for v in built:
            docs.append((page_dir / "visuals" / v["name"] / "visual.json", v))

    pages_path = DEF / "pages" / "pages.json"
    pages_meta = json.loads(pages_path.read_text(encoding="utf-8"))
    existing = [p for p in pages_meta.get("pageOrder", []) if p not in PAGE_ORDER]
    pages_meta["pageOrder"] = PAGE_ORDER + existing   # keep any pages added in Desktop, after ours
    pages_meta["$schema"] = PAGES_SCHEMA
    docs.append((pages_path, pages_meta))

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

    for folder, *_ in pages:
        shutil.rmtree(DEF / "pages" / folder / "visuals", ignore_errors=True)
    for path, doc in docs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {len(pages)} page(s) ({', '.join(p[1] for p in pages)}), {len(names)} visuals; "
          f"all files valid against the PBIR schemas.")


if __name__ == "__main__":
    main()
