"""
Generates the HelpdeskMetrics Power BI project (PBIP, TMDL semantic model)
from the pbi.* views in th2-metrics-db.

    py powerbi/generate_model.py

Columns are read from the live views, so re-run this after changing
sql/006_powerbi_views.sql. Relationships and measures are defined below.
Requires: az login (Entra admin or reporting_reader), pyodbc, azure-identity.

Output (overwritten): powerbi/HelpdeskMetrics.pbip plus the .SemanticModel
and .Report folders. Report pages and visuals are built in Power BI Desktop
(see powerbi/README.md) and saved back into the .Report folder — the
generator only creates the report skeleton the first time, and never
overwrites it afterwards.
"""
import json
import os
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "function-app"))
os.environ.setdefault(
    "SQL_CONNECTION_STRING",
    "Driver={ODBC Driver 18 for SQL Server};Server=tcp:th2-metrics-sql.database.windows.net,1433;"
    "Database=th2-metrics-db;Encrypt=yes;",
)
from shared import db  # noqa: E402

NAME = "HelpdeskMetrics"
SERVER = "th2-metrics-sql.database.windows.net"
DATABASE = "th2-metrics-db"

TABLES = ["Calendar", "Tech", "Board", "Company", "Agreement", "Tickets", "TicketAssignments",
          "BacklogDaily", "TimeEntries", "Capacity", "ClientMonth", "AgreementMonth", "PhoneDaily"]

# Columns hidden from report authors (keys used only by relationships).
HIDDEN = {"OwnerKey", "CloserKey", "ResolverKey", "ResponderKey", "TechKey", "BoardId", "CompanyId",
          "AgreementId", "Month Sort", "Weekday Sort"}
HIDDEN_ON_DIMS_TOO = {"Month Sort", "Weekday Sort"}
SORT_BY = {("Calendar", "Month"): "Month Sort", ("Calendar", "Weekday"): "Weekday Sort",
           ("Calendar", "Week"): "Week Start"}
MONEY_WORDS = ("Revenue", "Cost", "Margin", "Amount")

# (from table, from column, to table, to column, active)
RELATIONSHIPS = [
    ("Tickets", "Entered Date", "Calendar", "Date", True),
    ("Tickets", "Closed Date", "Calendar", "Date", False),
    ("Tickets", "Resolved Date", "Calendar", "Date", False),
    ("Tickets", "OwnerKey", "Tech", "TechKey", True),
    ("Tickets", "CloserKey", "Tech", "TechKey", False),
    ("Tickets", "ResolverKey", "Tech", "TechKey", False),
    ("Tickets", "BoardId", "Board", "BoardId", True),
    ("Tickets", "CompanyId", "Company", "CompanyId", True),
    ("TicketAssignments", "Assigned Date", "Calendar", "Date", True),
    ("TicketAssignments", "TechKey", "Tech", "TechKey", True),
    ("TicketAssignments", "BoardId", "Board", "BoardId", True),
    ("TicketAssignments", "CompanyId", "Company", "CompanyId", True),
    ("BacklogDaily", "Date", "Calendar", "Date", True),
    ("BacklogDaily", "TechKey", "Tech", "TechKey", True),
    ("BacklogDaily", "BoardId", "Board", "BoardId", True),
    ("TimeEntries", "Date", "Calendar", "Date", True),
    ("TimeEntries", "TechKey", "Tech", "TechKey", True),
    ("TimeEntries", "CompanyId", "Company", "CompanyId", True),
    ("TimeEntries", "AgreementId", "Agreement", "AgreementId", True),
    ("Capacity", "Date", "Calendar", "Date", True),
    ("Capacity", "TechKey", "Tech", "TechKey", True),
    ("ClientMonth", "Month Start", "Calendar", "Date", True),
    ("ClientMonth", "CompanyId", "Company", "CompanyId", True),
    ("AgreementMonth", "Month Start", "Calendar", "Date", True),
    ("AgreementMonth", "AgreementId", "Agreement", "AgreementId", True),
    ("AgreementMonth", "CompanyId", "Company", "CompanyId", True),
    ("PhoneDaily", "Date", "Calendar", "Date", True),
    ("PhoneDaily", "TechKey", "Tech", "TechKey", True),
]

COUNT, PCT, HOURS, MONEY, RATE = "#,0", "0.0%", "#,0.0", "\\$#,0;(\\$#,0);\\$#,0", "\\$#,0.00"

# table -> [(measure name, DAX, format, display folder)]
MEASURES = {
    "Tickets": [
        ("Tickets Opened", "COUNTROWS ( Tickets )", COUNT, "Volume"),
        ("Tickets Closed",
         "CALCULATE ( COUNTROWS ( Tickets ), Tickets[Is Closed] = TRUE (), "
         "USERELATIONSHIP ( Tickets[Closed Date], 'Calendar'[Date] ), "
         "USERELATIONSHIP ( Tickets[CloserKey], Tech[TechKey] ) )", COUNT, "Volume"),
        ("Tickets Resolved",
         "CALCULATE ( COUNTROWS ( Tickets ), NOT ISBLANK ( Tickets[Resolved Date] ), "
         "USERELATIONSHIP ( Tickets[Resolved Date], 'Calendar'[Date] ), "
         "USERELATIONSHIP ( Tickets[ResolverKey], Tech[TechKey] ) )", COUNT, "Volume"),
        ("Open Now",
         "CALCULATE ( COUNTROWS ( Tickets ), Tickets[Is Open] = TRUE (), REMOVEFILTERS ( 'Calendar' ) )",
         COUNT, "Backlog"),
        ("Waiting Now",
         "CALCULATE ( COUNTROWS ( Tickets ), Tickets[Is Open] = TRUE (), Tickets[Is Waiting] = TRUE (), "
         "REMOVEFILTERS ( 'Calendar' ) )", COUNT, "Backlog"),
        ("Avg Open Age (Days)",
         "CALCULATE ( AVERAGE ( Tickets[Open Age Days] ), Tickets[Is Open] = TRUE (), REMOVEFILTERS ( 'Calendar' ) )",
         HOURS, "Backlog"),
        ("Out of SLA Now",
         "CALCULATE ( COUNTROWS ( Tickets ), Tickets[Is Open] = TRUE (), Tickets[Is In SLA Now] = FALSE (), "
         "REMOVEFILTERS ( 'Calendar' ) )", COUNT, "SLA"),
        ("Responses With SLA Target",
         "CALCULATE ( COUNTROWS ( Tickets ), NOT ISBLANK ( Tickets[Met Response SLA] ) )", COUNT, "SLA"),
        ("Response SLA %",
         "DIVIDE ( CALCULATE ( COUNTROWS ( Tickets ), Tickets[Met Response SLA] = 1 ), [Responses With SLA Target] )",
         PCT, "SLA"),
        ("Resolutions With SLA Target",
         "CALCULATE ( COUNTROWS ( Tickets ), NOT ISBLANK ( Tickets[Met Resolution SLA] ), Tickets[Is Closed] = TRUE (), "
         "USERELATIONSHIP ( Tickets[Closed Date], 'Calendar'[Date] ) )", COUNT, "SLA"),
        ("Resolution SLA %",
         "DIVIDE ( CALCULATE ( COUNTROWS ( Tickets ), Tickets[Met Resolution SLA] = 1, Tickets[Is Closed] = TRUE (), "
         "USERELATIONSHIP ( Tickets[Closed Date], 'Calendar'[Date] ) ), [Resolutions With SLA Target] )", PCT, "SLA"),
        ("Avg Response Hours", "AVERAGE ( Tickets[Response Hours (SLA clock)] )", HOURS, "SLA"),
        ("Median Response Hours", "MEDIAN ( Tickets[Response Hours (SLA clock)] )", HOURS, "SLA"),
        ("Avg Resolution Hours",
         "CALCULATE ( AVERAGE ( Tickets[Resolution Hours (SLA clock)] ), Tickets[Is Closed] = TRUE (), "
         "USERELATIONSHIP ( Tickets[Closed Date], 'Calendar'[Date] ) )", HOURS, "SLA"),
        ("Median Resolution Hours",
         "CALCULATE ( MEDIAN ( Tickets[Resolution Hours (SLA clock)] ), Tickets[Is Closed] = TRUE (), "
         "USERELATIONSHIP ( Tickets[Closed Date], 'Calendar'[Date] ) )", HOURS, "SLA"),
    ],
    "TicketAssignments": [
        ("Tickets Assigned", "COUNTROWS ( TicketAssignments )", COUNT, "Volume"),
        ("Assigned - Closed", "[Tickets Assigned] - [Tickets Closed]", "+#,0;-#,0;0", "Volume"),
        ("Close Rate", "DIVIDE ( [Tickets Closed], [Tickets Assigned] )", PCT, "Volume"),
    ],
    "BacklogDaily": [
        ("Open at Start of Day",
         "VAR d = CALCULATE ( MAX ( BacklogDaily[Date] ) ) "
         "RETURN CALCULATE ( SUM ( BacklogDaily[Open Tickets] ), BacklogDaily[Date] = d )", COUNT, "Backlog"),
    ],
    "TimeEntries": [
        ("Hours Logged", "SUM ( TimeEntries[Hours] )", HOURS, "Time"),
        ("Utilized Hours", "SUM ( TimeEntries[Utilized Hours] )", HOURS, "Time"),
        ("Billable Hours", "SUM ( TimeEntries[Billable Hours] )", HOURS, "Time"),
        ("PTO Hours", "SUM ( TimeEntries[PTO Hours] )", HOURS, "Time"),
        ("Ticket Hours", "CALCULATE ( [Hours Logged], TimeEntries[Charge Type] = \"ServiceTicket\" )", HOURS, "Time"),
        ("Project Hours", "CALCULATE ( [Hours Logged], TimeEntries[Charge Type] = \"ProjectTicket\" )", HOURS, "Time"),
        ("Labor Cost", "SUM ( TimeEntries[Labor Cost] )", MONEY, "Time"),
        ("Uncosted Hours", "SUM ( TimeEntries[Uncosted Hours] )", HOURS, "Time"),
    ],
    "Capacity": [
        ("Capacity Hours", "SUM ( Capacity[Capacity Hours] )", HOURS, "Utilization"),
        ("Available Hours", "[Capacity Hours] - [PTO Hours]", HOURS, "Utilization"),
        ("Utilization %", "DIVIDE ( [Utilized Hours], [Available Hours] )", PCT, "Utilization"),
        ("Billable %", "DIVIDE ( [Billable Hours], [Available Hours] )", PCT, "Utilization"),
    ],
    "ClientMonth": [
        ("Normalized Revenue", "SUM ( ClientMonth[Normalized Revenue] )", MONEY, "Client"),
        ("Invoiced Revenue", "SUM ( ClientMonth[Invoiced Revenue] )", MONEY, "Client"),
        ("Agreement Revenue (Client)", "SUM ( ClientMonth[Agreement Revenue] )", MONEY, "Client"),
        ("Client Labor Cost", "SUM ( ClientMonth[Labor Cost] )", MONEY, "Client"),
        ("Client Addition Cost", "SUM ( ClientMonth[Addition Cost] )", MONEY, "Client"),
        ("Client Product Cost", "SUM ( ClientMonth[Product Cost] )", MONEY, "Client"),
        ("Client Total Cost", "[Client Labor Cost] + [Client Addition Cost] + [Client Product Cost]", MONEY, "Client"),
        ("Client Margin", "SUM ( ClientMonth[Margin] )", MONEY, "Client"),
        ("Client Margin %", "DIVIDE ( [Client Margin], [Normalized Revenue] )", PCT, "Client"),
        ("Client Invoiced Margin", "SUM ( ClientMonth[Invoiced Margin] )", MONEY, "Client"),
        ("Client Hours", "SUM ( ClientMonth[Hours] )", HOURS, "Client"),
        ("Client Uncosted Hours", "SUM ( ClientMonth[Uncosted Hours] )", HOURS, "Client"),
        ("Client Effective Hourly Rate", "DIVIDE ( [Normalized Revenue], [Client Hours] )", RATE, "Client"),
    ],
    "AgreementMonth": [
        ("Agreement Revenue", "SUM ( AgreementMonth[Revenue] )", MONEY, "Agreement"),
        ("Agreement Labor Cost", "SUM ( AgreementMonth[Labor Cost] )", MONEY, "Agreement"),
        ("Agreement Addition Cost", "SUM ( AgreementMonth[Addition Cost] )", MONEY, "Agreement"),
        ("Agreement Margin", "SUM ( AgreementMonth[Margin] )", MONEY, "Agreement"),
        ("Agreement Margin %", "DIVIDE ( [Agreement Margin], [Agreement Revenue] )", PCT, "Agreement"),
        ("Agreement Hours", "SUM ( AgreementMonth[Hours] )", HOURS, "Agreement"),
        ("Agreement Effective Hourly Rate", "DIVIDE ( [Agreement Revenue], [Agreement Hours] )", RATE, "Agreement"),
    ],
    "PhoneDaily": [
        ("Inbound Calls", "SUM ( PhoneDaily[Inbound Calls] )", COUNT, "Phones"),
        ("Calls Answered", "SUM ( PhoneDaily[Answered] )", COUNT, "Phones"),
        ("Calls Missed", "SUM ( PhoneDaily[Missed] )", COUNT, "Phones"),
        ("Answer Rate", "DIVIDE ( [Calls Answered], [Inbound Calls] )", PCT, "Phones"),
        ("Talk Hours", "SUM ( PhoneDaily[Talk Hours] )", HOURS, "Phones"),
    ],
}


def q(name):
    """TMDL object name, quoted when it isn't a plain identifier."""
    return name if name.replace("_", "").isalnum() and not name[0].isdigit() else "'" + name.replace("'", "''") + "'"


def tmdl_type(sql_type):
    if sql_type in ("int", "bigint", "smallint", "tinyint"):
        return "int64"
    if sql_type == "bit":
        return "boolean"
    if sql_type in ("decimal", "numeric", "float", "real", "money"):
        return "double"
    if sql_type in ("date", "datetime", "datetime2", "smalldatetime"):
        return "dateTime"
    return "string"


def column_format(table, col, dtype):
    if dtype == "dateTime":
        return "yyyy-mm-dd"
    if dtype == "double":
        if any(w in col for w in MONEY_WORDS):
            return MONEY
        return HOURS
    if dtype == "int64" and (col.endswith("Id") or col.endswith("Sort") or col == "Year"):
        return "0"
    return None


def lineage():
    return str(uuid.uuid4())


def read_columns():
    cols = {}
    with db.connection() as conn:
        cur = conn.cursor()
        for t in TABLES:
            cur.execute(
                """SELECT c.name, ty.name FROM sys.columns c JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                   WHERE c.object_id = OBJECT_ID(?) ORDER BY c.column_id""", f"pbi.[{t}]")
            cols[t] = [(r[0], r[1]) for r in cur.fetchall()]
            if not cols[t]:
                raise SystemExit(f"pbi.{t} not found — run sql/006_powerbi_views.sql first")
    return cols


def table_tmdl(table, columns):
    is_dim = table in ("Calendar", "Tech", "Board", "Company", "Agreement")
    out = [f"table {q(table)}", f"\tlineageTag: {lineage()}"]
    if table == "Calendar":
        out.append("\tdataCategory: Time")
    out.append("")
    for name, dax, fmt, folder in MEASURES.get(table, []):
        out += [f"\tmeasure {q(name)} = {dax}",
                f"\t\tformatString: {fmt}",
                f"\t\tdisplayFolder: {folder}",
                f"\t\tlineageTag: {lineage()}", ""]
    for col, sql_type in columns:
        dtype = tmdl_type(sql_type)
        out.append(f"\tcolumn {q(col)}")
        out.append(f"\t\tdataType: {dtype}")
        if table == "Calendar" and col == "Date":
            out.append("\t\tisKey")
        fmt = column_format(table, col, dtype)
        if fmt:
            out.append(f"\t\tformatString: {fmt}")
        hidden = (col in HIDDEN and not is_dim) or col in HIDDEN_ON_DIMS_TOO
        if hidden:
            out.append("\t\tisHidden")
        out.append(f"\t\tlineageTag: {lineage()}")
        out.append("\t\tsummarizeBy: none")
        out.append(f"\t\tsourceColumn: {col}")
        if (table, col) in SORT_BY:
            out.append(f"\t\tsortByColumn: {q(SORT_BY[(table, col)])}")
        out += ["", "\t\tannotation SummarizationSetBy = Automatic", ""]
    out += [
        f"\tpartition {q(table)} = m",
        "\t\tmode: import",
        "\t\tsource =",
        "\t\t\t\tlet",
        "\t\t\t\t    Source = Sql.Database(#\"SQL Server\", #\"SQL Database\"),",
        f"\t\t\t\t    Data = Source{{[Schema=\"pbi\",Item=\"{table}\"]}}[Data]",
        "\t\t\t\tin",
        "\t\t\t\t    Data",
        "",
        "\tannotation PBI_ResultType = Table",
        "",
    ]
    return "\n".join(out)


PERIOD_TABLE = """table Period
\tlineageTag: {l1}

\tcolumn Period
\t\tdataType: string
\t\tlineageTag: {l2}
\t\tsummarizeBy: none
\t\tsourceColumn: [Value1]
\t\tsortByColumn: 'Period Order'

\t\trelatedColumnDetails
\t\t\tgroupByColumn: 'Period Fields'

\t\tannotation SummarizationSetBy = Automatic

\tcolumn 'Period Fields'
\t\tdataType: string
\t\tisHidden
\t\tlineageTag: {l3}
\t\tsummarizeBy: none
\t\tsourceColumn: [Value2]
\t\tsortByColumn: 'Period Order'

\t\textendedProperty ParameterMetadata =
\t\t\t\t{{
\t\t\t\t  "version": 3,
\t\t\t\t  "kind": 2
\t\t\t\t}}

\t\tannotation SummarizationSetBy = Automatic

\tcolumn 'Period Order'
\t\tdataType: int64
\t\tisHidden
\t\tformatString: 0
\t\tlineageTag: {l4}
\t\tsummarizeBy: sum
\t\tsourceColumn: [Value3]

\t\tannotation SummarizationSetBy = Automatic

\tpartition Period = calculated
\t\tmode: import
\t\tsource =
\t\t\t\t{{
\t\t\t\t    ("Day", NAMEOF('Calendar'[Date]), 0),
\t\t\t\t    ("Week", NAMEOF('Calendar'[Week]), 1),
\t\t\t\t    ("Month", NAMEOF('Calendar'[Month]), 2)
\t\t\t\t}}

\tannotation PBI_Id = {pid}
"""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def main():
    cols = read_columns()
    model_dir = ROOT / f"{NAME}.SemanticModel"
    defn = model_dir / "definition"
    if (defn / "tables").exists():
        for f in (defn / "tables").glob("*.tmdl"):
            f.unlink()

    write(model_dir / "definition.pbism", json.dumps({"version": "4.0", "settings": {}}, indent=2) + "\n")
    write(defn / "database.tmdl", "database\n\tcompatibilityLevel: 1567\n")
    all_tables = TABLES + ["Period"]
    write(defn / "model.tmdl", "\n".join([
        "model Model",
        "\tculture: en-US",
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "\tsourceQueryCulture: en-US",
        "\tdataAccessOptions",
        "\t\tlegacyRedirects",
        "\t\treturnErrorValuesAsNull",
        "",
        f"annotation PBI_QueryOrder = {json.dumps(['SQL Server', 'SQL Database'] + TABLES)}",
        "",
        "annotation __PBI_TimeIntelligenceEnabled = 0",
        "",
        *[f"ref table {q(t)}" for t in all_tables],
        "",
    ]))
    write(defn / "expressions.tmdl", "\n".join([
        f'expression \'SQL Server\' = "{SERVER}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
        f"\tlineageTag: {lineage()}",
        "",
        "\tannotation PBI_ResultType = Text",
        "",
        f'expression \'SQL Database\' = "{DATABASE}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
        f"\tlineageTag: {lineage()}",
        "",
        "\tannotation PBI_ResultType = Text",
        "",
    ]))
    rel = []
    for frm_t, frm_c, to_t, to_c, active in RELATIONSHIPS:
        rel.append(f"relationship {uuid.uuid5(uuid.NAMESPACE_URL, f'{frm_t}.{frm_c}->{to_t}.{to_c}')}")
        if not active:
            rel.append("\tisActive: false")
        if to_t == "Calendar":
            rel.append("\tjoinOnDateBehavior: datePartOnly")
        rel.append(f"\tfromColumn: {q(frm_t)}.{q(frm_c)}")
        rel.append(f"\ttoColumn: {q(to_t)}.{q(to_c)}")
        rel.append("")
    write(defn / "relationships.tmdl", "\n".join(rel))
    for t in TABLES:
        write(defn / "tables" / f"{t}.tmdl", table_tmdl(t, cols[t]))
    write(defn / "tables" / "Period.tmdl", PERIOD_TABLE.format(
        l1=lineage(), l2=lineage(), l3=lineage(), l4=lineage(), pid=uuid.uuid4().hex))

    write(ROOT / f"{NAME}.pbip", json.dumps({
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{NAME}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    }, indent=2) + "\n")

    report_dir = ROOT / f"{NAME}.Report"
    if not (report_dir / "report.json").exists() and not (report_dir / "definition").exists():
        write(report_dir / "definition.pbir", json.dumps({
            "version": "4.0",
            "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}, "byConnection": None},
        }, indent=2) + "\n")
        pages = ["Tickets by Tech", "SLA", "Utilization", "Client Profitability", "Contract Profitability"]
        write(report_dir / "report.json", json.dumps({
            "config": json.dumps({"version": "5.53", "themeCollection": {}, "activeSectionIndex": 0}),
            "layoutOptimization": 0,
            "resourcePackages": [],
            "sections": [{
                "config": "{}", "displayName": p, "displayOption": 1, "filters": "[]",
                "height": 720.0, "width": 1280.0, "name": f"ReportSection{i}", "ordinal": i,
                "visualContainers": [],
            } for i, p in enumerate(pages)],
        }, indent=2) + "\n")

    n_measures = sum(len(v) for v in MEASURES.values())
    print(f"Wrote {len(TABLES) + 1} tables, {len(RELATIONSHIPS)} relationships, {n_measures} measures -> {ROOT}")


if __name__ == "__main__":
    main()
