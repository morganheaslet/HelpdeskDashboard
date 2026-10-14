"""
Azure SQL access for th2-metrics-db.

Auth: by default this authenticates with an Entra ID access token from
azure-identity's DefaultAzureCredential — the Function App's managed identity
when deployed, your `az login` session when run locally — so there is no SQL
password anywhere. SQL_CONNECTION_STRING then only needs the server/database:

    Driver={ODBC Driver 18 for SQL Server};Server=tcp:th2-metrics-sql.database.windows.net,1433;Database=th2-metrics-db;Encrypt=yes;

If the connection string already carries its own credentials (UID= or
Authentication=), it's used as-is instead.

Writes go through upsert(): each batch is sent as one JSON parameter and
MERGEd into the target by primary key — so re-syncing any window is always
safe, and a 1000-row page is one round trip rather than 1000.
"""
import json
import logging
import os
import struct
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

import pyodbc

SQL_COPT_SS_ACCESS_TOKEN = 1256  # msodbcsql pre-connect attribute for an Entra token
_SQL_SCOPE = "https://database.windows.net/.default"
_credential = None


def _token_attrs():
    global _credential
    from azure.identity import DefaultAzureCredential
    if _credential is None:
        _credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    token = _credential.get_token(_SQL_SCOPE).token.encode("utf-16-le")
    return {SQL_COPT_SS_ACCESS_TOKEN: struct.pack(f"<I{len(token)}s", len(token), token)}


def connect():
    conn_str = os.environ["SQL_CONNECTION_STRING"]
    lowered = conn_str.lower()
    if "uid=" in lowered or "authentication=" in lowered:
        conn = pyodbc.connect(conn_str, autocommit=False)
    else:
        conn = pyodbc.connect(conn_str, attrs_before=_token_attrs(), autocommit=False)
    return conn


@contextmanager
def connection():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def utcnow():
    """Naive UTC 'now' — every DATETIME2 column in this database is UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def parse_ts(value):
    """ConnectWise/Dialpad ISO timestamp -> naive UTC datetime (or None)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


_column_types = {}


def _sql_types(conn, table):
    """{column: T-SQL type} for `table`, cached per process."""
    if table not in _column_types:
        schema, name = table.split(".")
        cur = conn.cursor()
        cur.execute("""
            SELECT c.name, t.name, c.max_length, c.precision, c.scale
            FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id
            WHERE c.object_id = OBJECT_ID(?)""", f"[{schema}].[{name}]")
        types = {}
        for col, typ, max_len, prec, scale in cur.fetchall():
            if typ in ("nvarchar", "nchar"):
                types[col] = f"{typ}({'max' if max_len == -1 else max_len // 2})"
            elif typ in ("varchar", "char"):
                types[col] = f"{typ}({'max' if max_len == -1 else max_len})"
            elif typ in ("decimal", "numeric"):
                types[col] = f"decimal({prec},{scale})"
            elif typ in ("datetime2", "time", "datetimeoffset"):
                types[col] = f"{typ}({scale})"
            else:
                types[col] = typ
        _column_types[table] = types
    return _column_types[table]


def _json_value(v):
    # Numbers go over as fixed-point strings: SQL Server can't convert
    # exponent notation ('1e-05') to decimal, and a string converts to the
    # target decimal(p,s) with normal rounding.
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (float, Decimal)):
        return format(v, "f")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def upsert(conn, table, key_cols, rows, chunk_size=1000):
    """
    MERGE `rows` (list of dicts, all with the same keys) into `table` by
    `key_cols`. Columns not present in the dicts are left untouched on update.
    Returns the number of distinct rows sent.

    Each chunk goes over as ONE JSON parameter, shredded server-side with
    OPENJSON ... WITH (the target's own column types). This replaced a
    fast_executemany + #temp-table approach that failed in production:
    pyodbc can't describe #temp parameters, so it inferred types and buffer
    sizes from the first row — a leading None or short string then broke
    every later row ("varchar to numeric", "right truncation").
    """
    if not rows:
        return 0
    # ConnectWise's page-number paging can return a record twice when data
    # shifts mid-pull, and MERGE rejects duplicate source keys — keep the last.
    deduped = {tuple(r[k] for k in key_cols): r for r in rows}
    rows = list(deduped.values())

    types = _sql_types(conn, table)
    cols = list(rows[0].keys())
    missing = [c for c in cols if c not in types]
    if missing:
        raise KeyError(f"{table} has no column(s) {missing}")
    col_list = ", ".join(f"[{c}]" for c in cols)
    with_clause = ", ".join(f"[{c}] {types[c]} '$.\"{c}\"'" for c in cols)
    on = " AND ".join(f"t.[{k}] = s.[{k}]" for k in key_cols)
    updates = ", ".join(f"t.[{c}] = s.[{c}]" for c in cols if c not in key_cols)
    matched = f"WHEN MATCHED THEN UPDATE SET {updates}" if updates else ""
    sql = f"""
        MERGE {table} AS t
        USING (SELECT * FROM OPENJSON(?) WITH ({with_clause})) AS s ON {on}
        {matched}
        WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({", ".join(f"s.[{c}]" for c in cols)});
    """
    cur = conn.cursor()
    for i in range(0, len(rows), chunk_size):
        payload = json.dumps([{c: _json_value(r.get(c)) for c in cols} for r in rows[i:i + chunk_size]])
        cur.execute(sql, payload)
    return len(rows)


# ------------------------------------------------------------ sync state

def get_state(conn, entity):
    cur = conn.cursor()
    cur.execute(
        "SELECT cursor_utc, backfill_complete, last_daily_job_date, cursor_id FROM meta.sync_state WHERE entity = ?",
        entity,
    )
    row = cur.fetchone()
    if row is None:
        return {"cursor_utc": None, "backfill_complete": False, "last_daily_job_date": None, "cursor_id": None}
    return {"cursor_utc": row[0], "backfill_complete": bool(row[1]), "last_daily_job_date": row[2], "cursor_id": row[3]}


def set_state(conn, entity, **fields):
    fields = dict(fields)
    row = {"entity": entity, **fields}
    upsert(conn, "meta.sync_state", ["entity"], [row])


def log_run(conn, entity, started_at, status, rows_upserted=None, message=None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO meta.sync_log (entity, started_at, finished_at, rows_upserted, status, message) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        entity, started_at, utcnow(), rows_upserted, status, (message or "")[:2000] or None,
    )
    if status == "error":
        set_state(conn, entity, last_error_at=utcnow(), last_error=(message or "")[:2000])
    else:
        # Clear the previous error so sync health reflects the current state;
        # the error itself stays in meta.sync_log.
        set_state(conn, entity, last_success_at=utcnow(), last_error=None, last_error_at=None)
    logging.info("sync %s: %s (%s rows) %s", entity, status, rows_upserted, message or "")
