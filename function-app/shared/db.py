"""
Azure SQL access for th2-metrics-db.

Auth: by default this authenticates with an Entra ID access token from
azure-identity's DefaultAzureCredential — the Function App's managed identity
when deployed, your `az login` session when run locally — so there is no SQL
password anywhere. SQL_CONNECTION_STRING then only needs the server/database:

    Driver={ODBC Driver 18 for SQL Server};Server=tcp:th2-metrics-sql.database.windows.net,1433;Database=th2-metrics-db;Encrypt=yes;

If the connection string already carries its own credentials (UID= or
Authentication=), it's used as-is instead.

Writes go through upsert(): rows are bulk-inserted into a temp table with
pyodbc's fast_executemany, then MERGEd into the target by primary key — so
re-syncing any window is always safe, and a 1000-row page is one round trip
rather than 1000.
"""
import logging
import os
import struct
from contextlib import contextmanager
from datetime import datetime, timezone

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


def upsert(conn, table, key_cols, rows, chunk_size=1000):
    """
    MERGE `rows` (list of dicts, all with the same keys) into `table` by
    `key_cols`. Columns not present in the dicts are left untouched on update.
    Returns the number of rows sent.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    col_list = ", ".join(f"[{c}]" for c in cols)
    on = " AND ".join(f"t.[{k}] = s.[{k}]" for k in key_cols)
    updates = ", ".join(f"t.[{c}] = s.[{c}]" for c in cols if c not in key_cols)
    cur = conn.cursor()
    cur.fast_executemany = True
    # SELECT TOP 0 ... INTO copies the target's column types, so values bind
    # exactly as they will in the MERGE (no implicit-conversion surprises).
    cur.execute(f"DROP TABLE IF EXISTS #stage; SELECT TOP 0 {col_list} INTO #stage FROM {table};")
    placeholders = ", ".join("?" for _ in cols)
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]
        cur.executemany(
            f"INSERT INTO #stage ({col_list}) VALUES ({placeholders})",
            [tuple(r.get(c) for c in cols) for r in chunk],
        )
    matched = f"WHEN MATCHED THEN UPDATE SET {updates}" if updates else ""
    cur.execute(f"""
        MERGE {table} AS t
        USING #stage AS s ON {on}
        {matched}
        WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({", ".join(f"s.[{c}]" for c in cols)});
        DROP TABLE #stage;
    """)
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
        set_state(conn, entity, last_success_at=utcnow())
    logging.info("sync %s: %s (%s rows) %s", entity, status, rows_upserted, message or "")
