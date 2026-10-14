"""
ConnectWise -> th2-metrics-db sync.

Incremental by lastUpdated, in bounded time windows:
- meta.sync_state.cursor_utc is the high-water mark per entity. It starts at
  BACKFILL_START, so the very first runs ARE the backfill — each run walks
  forward window by window until it hits its time budget, commits, and the
  next run picks up where it stopped. No separate backfill job, and a run
  that dies mid-way loses at most one window (MERGE makes re-syncing safe).
- Steady state, each run re-reads from (cursor - OVERLAP) to now, which
  catches records whose lastUpdated landed just before a run but committed
  after it.

Once per local day (run_daily_cw) it also: refreshes reference tables,
re-pulls every currently-open ticket (an old open ticket untouched since
before BACKFILL_START would never show up by lastUpdated), snapshots the
start-of-day backlog, refreshes timesheets, and reconciles deletions —
ConnectWise reports deleted records to a lastUpdated query by simply not
returning them.

Project tickets are excluded per management's definition of ticket metrics;
project TIME is still synced (it's real work for utilization/profitability)
and is distinguishable by charge_to_type.
"""
import logging
from datetime import datetime, timedelta, timezone

from shared import db
from shared.aggregate import CLOSED_STATUSES, to_local

BACKFILL_START = datetime(2025, 1, 1)          # naive UTC, like every DB timestamp
BACKFILL_WINDOW = timedelta(days=7)
OVERLAP = timedelta(minutes=5)
TIME_ENTRY_RECONCILE_DAYS = 45                  # entries get edited/deleted until approved

# ConnectWise condition field for "last modified". Unconfirmed against this
# instance's build — if the first sync logs a 400 mentioning it, this is the
# one place to change (e.g. to "_info/lastUpdated").
UPDATED_FIELD = "lastUpdated"

TICKET_FIELDS = [
    "id", "recordType", "board", "status", "priority", "type", "subType", "item",
    "team", "source", "severity", "impact", "company", "contact", "owner",
    "summary", "parentTicketId", "agreement", "sla", "slaStatus", "isInSla",
    "_info/dateEntered", "_info/enteredBy", "_info/lastUpdated",
    "closedFlag", "closedDate", "closedBy",
    "dateResponded", "respondedBy", "dateResplan", "resplanBy", "dateResolved", "resolvedBy",
    "respondMinutes", "resPlanMinutes", "resolveMinutes", "minutesWaiting",
    "actualHours", "budgetHours", "customerUpdatedFlag",
]

TIME_ENTRY_FIELDS = [
    "id", "member", "chargeToId", "chargeToType", "ticket", "project", "company",
    "agreement", "workType", "workRole", "billableOption", "timeStart", "timeEnd",
    "actualHours", "hoursDeduct", "hoursBilled", "invoiceHours", "agreementHours",
    "agreementAmount", "hourlyRate", "hourlyCost", "status", "timeSheet", "invoice",
    "ticketBoard", "dateEntered", "_info/lastUpdated",
]


# ------------------------------------------------------------ helpers

def _id(ref):
    return (ref or {}).get("id")


def _name(ref, n=100):
    return _s((ref or {}).get("name"), n)


def _ident(ref):
    return _s((ref or {}).get("identifier"), 50)


def _s(value, n):
    if value is None:
        return None
    value = str(value).strip()
    return value[:n] if value else None


def _info(rec, key):
    return (rec.get("_info") or {}).get(key)


def _cw_ts(dt):
    """naive-UTC datetime -> ConnectWise condition literal."""
    return f"[{dt.strftime('%Y-%m-%dT%H:%M:%SZ')}]"


def _local(utc_naive):
    """Naive-UTC datetime -> naive local (REPORT_TIMEZONE) datetime.
    Computed here, once per row at sync time, because doing it in SQL
    (AT TIME ZONE) per row per query was ~85% of the reporting views' cost."""
    if utc_naive is None:
        return None
    return to_local(utc_naive.replace(tzinfo=timezone.utc)).replace(tzinfo=None)


def _local_date(utc_naive):
    local = _local(utc_naive)
    return local.date() if local else None


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def ticket_row(t, now):
    row = _ticket_row(t, now)
    row.update({
        "entered_local": _local(row["date_entered"]),
        "entered_local_date": _local_date(row["date_entered"]),
        "closed_local_date": _local_date(row["closed_date"]),
        "resolved_local_date": _local_date(row["date_resolved"]),
        "responded_local_date": _local_date(row["date_responded"]),
    })
    return row


def _ticket_row(t, now):
    return {
        "ticket_id": t["id"],
        "record_type": _s(t.get("recordType"), 30),
        "board_id": _id(t.get("board")), "board_name": _name(t.get("board")),
        "status_id": _id(t.get("status")), "status_name": _name(t.get("status")),
        "priority_id": _id(t.get("priority")), "priority_name": _name(t.get("priority")),
        "type_name": _name(t.get("type")), "subtype_name": _name(t.get("subType")),
        "item_name": _name(t.get("item")),
        "team_id": _id(t.get("team")), "team_name": _name(t.get("team")),
        "source_name": _name(t.get("source")),
        "severity": _s(t.get("severity"), 20), "impact": _s(t.get("impact"), 20),
        "company_id": _id(t.get("company")), "company_name": _name(t.get("company"), 200),
        "contact_name": _name(t.get("contact"), 200),
        "owner_identifier": _ident(t.get("owner")),
        "summary": _s(t.get("summary"), 500),
        "parent_ticket_id": t.get("parentTicketId"),
        "agreement_id": _id(t.get("agreement")), "agreement_name": _name(t.get("agreement"), 200),
        "sla_id": _id(t.get("sla")), "sla_name": _name(t.get("sla")),
        "sla_status": _s(t.get("slaStatus"), 100), "is_in_sla": t.get("isInSla"),
        "date_entered": db.parse_ts(_info(t, "dateEntered")),
        "entered_by": _s(_info(t, "enteredBy"), 50),
        "last_updated": db.parse_ts(_info(t, "lastUpdated")),
        "closed_flag": bool(t.get("closedFlag")),
        "closed_date": db.parse_ts(t.get("closedDate")),
        "closed_by": _s(t.get("closedBy"), 50),
        "date_responded": db.parse_ts(t.get("dateResponded")), "responded_by": _s(t.get("respondedBy"), 50),
        "date_resplan": db.parse_ts(t.get("dateResplan")), "resplan_by": _s(t.get("resplanBy"), 50),
        "date_resolved": db.parse_ts(t.get("dateResolved")), "resolved_by": _s(t.get("resolvedBy"), 50),
        "respond_minutes": t.get("respondMinutes"),
        "resplan_minutes": t.get("resPlanMinutes"),
        "resolve_minutes": t.get("resolveMinutes"),
        "minutes_waiting": t.get("minutesWaiting"),
        "actual_hours": t.get("actualHours"), "budget_hours": t.get("budgetHours"),
        "customer_updated": t.get("customerUpdatedFlag"),
        "deleted": False,
        "synced_at": now,
    }


def time_entry_row(e, now):
    charge_type = e.get("chargeToType")
    return {
        "time_entry_id": e["id"],
        "member_identifier": _ident(e.get("member")),
        "charge_to_type": _s(charge_type, 30),
        "charge_to_id": e.get("chargeToId"),
        "ticket_id": _id(e.get("ticket")) or (e.get("chargeToId") if charge_type == "ServiceTicket" else None),
        "project_id": _id(e.get("project")),
        "company_id": _id(e.get("company")), "company_name": _name(e.get("company"), 200),
        "agreement_id": _id(e.get("agreement")), "agreement_name": _name(e.get("agreement"), 200),
        "work_type_id": _id(e.get("workType")), "work_type_name": _name(e.get("workType")),
        "work_role_id": _id(e.get("workRole")), "work_role_name": _name(e.get("workRole")),
        "billable_option": _s(e.get("billableOption"), 20),
        "time_start": db.parse_ts(e.get("timeStart")), "time_end": db.parse_ts(e.get("timeEnd")),
        "local_date": _local_date(db.parse_ts(e.get("timeStart"))),
        "actual_hours": e.get("actualHours"), "hours_deduct": e.get("hoursDeduct"),
        "hours_billed": e.get("hoursBilled"), "invoice_hours": e.get("invoiceHours"),
        "agreement_hours": e.get("agreementHours"), "agreement_amount": e.get("agreementAmount"),
        "hourly_rate": e.get("hourlyRate"),
        "hourly_cost": _num(e.get("hourlyCost")),   # a string in the spec
        "status": _s(e.get("status"), 30),
        "time_sheet_id": _id(e.get("timeSheet")),
        "invoice_id": _id(e.get("invoice")),
        "ticket_board": _s(e.get("ticketBoard"), 100),
        "date_entered": db.parse_ts(e.get("dateEntered")),
        "last_updated": db.parse_ts(_info(e, "lastUpdated")),
        "deleted": False,
        "synced_at": now,
    }


def _date(v):
    """Date-only CW fields (e.g. timesheet dateStart) come back as midnight
    'Z' timestamps; the calendar date is what matters, not a UTC instant."""
    dt = db.parse_ts(v)
    return dt.date() if dt else None


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------ incremental sync

def _windowed_sync(cw, conn, entity, path, fields, to_rows, deadline, on_batch=None):
    """
    Walks meta.sync_state[entity].cursor_utc forward to now in
    BACKFILL_WINDOW-sized lastUpdated windows until done or `deadline`
    (an aware UTC datetime) passes. Commits after every window. Returns rows
    upserted.
    """
    state = db.get_state(conn, entity)
    cursor = state["cursor_utc"] or BACKFILL_START
    total = 0
    while datetime.now(timezone.utc) < deadline:
        now = db.utcnow()
        start = cursor - OVERLAP if state["backfill_complete"] else cursor
        end = min(cursor + BACKFILL_WINDOW, now)
        records = cw.get_all(
            path, fields=fields,
            conditions=f"{UPDATED_FIELD}>={_cw_ts(start)} AND {UPDATED_FIELD}<{_cw_ts(end)}",
        )
        rows = to_rows(records, now)
        if on_batch:
            on_batch(conn, rows, state["backfill_complete"])
        total += db.upsert(conn, *_TABLES[entity], rows)
        caught_up = end >= now
        cursor = end
        state["backfill_complete"] = state["backfill_complete"] or caught_up
        db.set_state(conn, entity, cursor_utc=cursor, backfill_complete=state["backfill_complete"])
        conn.commit()
        logging.info("%s: window %s -> %s, %d rows", entity, start, end, len(rows))
        if caught_up:
            break
    return total


_TABLES = {
    "cw_tickets": ("cw.tickets", ["ticket_id"]),
    "cw_time_entries": ("cw.time_entries", ["time_entry_id"]),
    "cw_invoices": ("cw.invoices", ["invoice_id"]),
    "cw_products": ("cw.products", ["product_item_id"]),
}


def _ticket_rows(records, now):
    # Project tickets/issues can come back from /service/tickets too — not
    # counted in ticket metrics, so not stored.
    return [ticket_row(t, now) for t in records if (t.get("recordType") or "ServiceTicket") == "ServiceTicket"]


def _time_entry_rows(records, now):
    return [time_entry_row(e, now) for e in records]


def _record_ticket_events(conn, rows, backfill_complete):
    """Diffs incoming tickets against stored rows and logs status/owner/board
    changes to cw.ticket_events. Skipped during the initial backfill — a diff
    between two backfill windows months apart isn't a real "event"."""
    if not backfill_complete or not rows:
        return
    incoming = {r["ticket_id"]: r for r in rows}
    cur = conn.cursor()
    events = []
    for ids in _chunks(list(incoming), 1000):
        cur.execute(
            f"SELECT ticket_id, status_name, owner_identifier, board_name FROM cw.tickets "
            f"WHERE ticket_id IN ({','.join('?' * len(ids))})", *ids,
        )
        for ticket_id, status, owner, board in cur.fetchall():
            new = incoming[ticket_id]
            at = new["last_updated"] or new["synced_at"]
            for field, old_val, new_val in (
                ("status", status, new["status_name"]),
                ("owner", owner, new["owner_identifier"]),
                ("board", board, new["board_name"]),
            ):
                if (old_val or None) != (new_val or None):
                    events.append((ticket_id, at, field, old_val, new_val))
    if events:
        # Plain executemany: a handful of rows per run, and fast_executemany
        # mis-binds a leading NULL old_value (see db.upsert's docstring).
        cur.executemany(
            "INSERT INTO cw.ticket_events (ticket_id, event_at, field, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
            events,
        )


def sync_tickets(cw, conn, deadline):
    return _windowed_sync(
        cw, conn, "cw_tickets", "/service/tickets", TICKET_FIELDS, _ticket_rows, deadline,
        on_batch=_record_ticket_events,
    )


def sync_time_entries(cw, conn, deadline):
    return _windowed_sync(
        cw, conn, "cw_time_entries", "/time/entries", TIME_ENTRY_FIELDS, _time_entry_rows, deadline,
    )


# ---------------------------------------------------- reference tables

def sync_reference(cw, conn):
    now = db.utcnow()
    n = 0

    members = cw.get_all("/system/members")
    n += db.upsert(conn, "cw.members", ["member_id"], [{
        "member_id": m["id"], "identifier": _s(m.get("identifier"), 50),
        "first_name": _s(m.get("firstName"), 100), "last_name": _s(m.get("lastName"), 100),
        "title": _s(m.get("title"), 100), "member_type": _name(m.get("type")),
        "license_class": _s(m.get("licenseClass"), 5),
        "inactive": bool(m.get("inactiveFlag")), "inactive_date": db.parse_ts(m.get("inactiveDate")),
        "hire_date": db.parse_ts(m.get("hireDate")),
        "daily_capacity": m.get("dailyCapacity"),
        "include_in_utilization": m.get("includeInUtilizationReportingFlag"),
        "hourly_cost": _num(m.get("hourlyCost")), "hourly_rate": _num(m.get("hourlyRate")),
        "work_role_name": _name(m.get("workRole")),
        "default_department": _name(m.get("defaultDepartment")),
        "default_location": _name(m.get("defaultLocation")),
        "reports_to": _ident(m.get("reportsTo")),
        "synced_at": now,
    } for m in members if m.get("identifier")])

    n += db.upsert(conn, "cw.work_types", ["work_type_id"], [{
        "work_type_id": w["id"], "name": _s(w.get("name"), 100),
        "bill_time": _s(w.get("billTime"), 30), "utilization_flag": w.get("utilizationFlag"),
        "accrual_type": _s(w.get("accrualType"), 30), "inactive": bool(w.get("inactiveFlag")),
        "synced_at": now,
    } for w in cw.get_all("/time/workTypes")])

    n += db.upsert(conn, "cw.work_roles", ["work_role_id"], [{
        "work_role_id": w["id"], "name": _s(w.get("name"), 100),
        "hourly_rate": _num(w.get("hourlyRate")), "inactive": bool(w.get("inactiveFlag")),
        "synced_at": now,
    } for w in cw.get_all("/time/workRoles")])

    boards = cw.get_all("/service/boards")
    n += db.upsert(conn, "cw.boards", ["board_id"], [{
        "board_id": b["id"], "name": _s(b.get("name"), 100),
        "project_flag": b.get("projectFlag"), "inactive": bool(b.get("inactiveFlag")),
        "synced_at": now,
    } for b in boards])

    statuses, teams = [], []
    for b in boards:
        if b.get("inactiveFlag") or b.get("projectFlag"):
            continue
        for s in cw.get_all(f"/service/boards/{b['id']}/statuses"):
            statuses.append({
                "status_id": s["id"], "board_id": b["id"], "name": _s(s.get("name"), 100),
                "closed_status": s.get("closedStatus"),
                "escalation_status": _s(s.get("escalationStatus"), 30),
                "time_entry_not_allowed": s.get("timeEntryNotAllowed"),
                "inactive": bool(s.get("inactive")), "sort_order": s.get("sortOrder"),
                "synced_at": now,
            })
        for tm in cw.get_all(f"/service/boards/{b['id']}/teams"):
            teams.append({
                "team_id": tm["id"], "board_id": b["id"], "name": _s(tm.get("name"), 100),
                "team_leader": _ident(tm.get("teamLeader")), "synced_at": now,
            })
    n += db.upsert(conn, "cw.board_statuses", ["status_id"], statuses)
    n += db.upsert(conn, "cw.teams", ["team_id"], teams)

    n += db.upsert(conn, "cw.priorities", ["priority_id"], [{
        "priority_id": p["id"], "name": _s(p.get("name"), 100),
        "sort_order": p.get("sortOrder"), "synced_at": now,
    } for p in cw.get_all("/service/priorities")])

    slas = cw.get_all("/service/SLAs")
    n += db.upsert(conn, "cw.slas", ["sla_id"], [{
        "sla_id": s["id"], "name": _s(s.get("name"), 100), "based_on": _s(s.get("basedOn"), 30),
        "respond_hours": s.get("respondHours"), "plan_within": s.get("planWithin"),
        "resolution_hours": s.get("resolutionHours"), "synced_at": now,
    } for s in slas])
    targets = []
    for s in slas:
        for p in cw.get_all(f"/service/SLAs/{s['id']}/priorities"):
            if _id(p.get("priority")) is None:
                continue
            targets.append({
                "sla_id": s["id"], "priority_id": _id(p.get("priority")),
                "respond_hours": p.get("respondHours"), "plan_within": p.get("planWithin"),
                "resolution_hours": p.get("resolutionHours"), "synced_at": now,
            })
    n += db.upsert(conn, "cw.sla_priority_targets", ["sla_id", "priority_id"], targets)

    n += db.upsert(conn, "cw.companies", ["company_id"], [{
        "company_id": c["id"], "identifier": _s(c.get("identifier"), 50),
        "name": _s(c.get("name"), 200) or f"(company {c['id']})",
        "status_name": _name(c.get("status")),
        "company_types": _s(", ".join(t.get("name", "") for t in (c.get("types") or [])), 400),
        "territory": _name(c.get("territory")), "market": _name(c.get("market")),
        "parent_company_id": _id(c.get("parentCompany")),
        "deleted": bool(c.get("deletedFlag")),
        "date_acquired": db.parse_ts(c.get("dateAcquired")),
        "employee_count": c.get("numberOfEmployees"),
        "last_updated": db.parse_ts(_info(c, "lastUpdated")),
        "synced_at": now,
    } for c in cw.get_all("/company/companies", fields=[
        "id", "identifier", "name", "status", "types", "territory", "market", "parentCompany",
        "deletedFlag", "dateAcquired", "numberOfEmployees", "_info/lastUpdated",
    ])])
    return n


def sync_time_sheets(cw, conn):
    now = db.utcnow()
    sheets = cw.get_all("/time/sheets", conditions=f"dateStart>={_cw_ts(BACKFILL_START)}")
    return db.upsert(conn, "cw.time_sheets", ["time_sheet_id"], [{
        "time_sheet_id": s["id"], "member_identifier": _ident(s.get("member")),
        "year": s.get("year"), "period": s.get("period"),
        "date_start": _date(s.get("dateStart")),
        "date_end": _date(s.get("dateEnd")),
        "status": _s(s.get("status"), 30), "hours": s.get("hours"),
        "deadline": db.parse_ts(s.get("deadline")),
        "last_updated": db.parse_ts(_info(s, "lastUpdated")),
        "synced_at": now,
    } for s in sheets])


# ------------------------------------------------------ daily maintenance

def refresh_open_tickets(cw, conn, backfill_complete):
    """Re-pulls every open service ticket regardless of lastUpdated, then
    flags stored-as-open tickets ConnectWise no longer has at all."""
    now = db.utcnow()
    open_tickets = cw.get_all("/service/tickets", fields=TICKET_FIELDS, conditions="closedFlag=false")
    rows = _ticket_rows(open_tickets, now)
    n = db.upsert(conn, "cw.tickets", ["ticket_id"], rows)
    if not backfill_complete:
        # Mid-backfill, plenty of stored-as-open tickets were simply closed
        # in a window the backfill hasn't reached yet — not deleted.
        return n

    open_ids = {r["ticket_id"] for r in rows}
    cur = conn.cursor()
    cur.execute("SELECT ticket_id FROM cw.tickets WHERE closed_flag = 0 AND deleted = 0")
    stale = [r[0] for r in cur.fetchall() if r[0] not in open_ids]
    # Normally these were closed and the lastUpdated sync already caught it;
    # anything left stored-as-open but absent from CW's open list is checked
    # individually (a handful at most) — 404 means deleted/merged away.
    for ticket_id in stale[:200]:
        found = cw.get_all("/service/tickets", fields=TICKET_FIELDS, conditions=f"id={ticket_id}")
        if found:
            db.upsert(conn, "cw.tickets", ["ticket_id"], _ticket_rows(found, now))
        else:
            cur.execute("UPDATE cw.tickets SET deleted = 1, synced_at = ? WHERE ticket_id = ?", now, ticket_id)
    return n


def reconcile_time_entries(cw, conn):
    """Marks entries deleted in ConnectWise within the editable window."""
    since = db.utcnow() - timedelta(days=TIME_ENTRY_RECONCILE_DAYS)
    live_ids = {e["id"] for e in cw.get_all("/time/entries", fields=["id"], conditions=f"timeStart>={_cw_ts(since)}")}
    cur = conn.cursor()
    cur.execute("SELECT time_entry_id FROM cw.time_entries WHERE time_start >= ? AND deleted = 0", since)
    gone = [r[0] for r in cur.fetchall() if r[0] not in live_ids]
    for ids in _chunks(gone, 1000):
        cur.execute(
            f"UPDATE cw.time_entries SET deleted = 1, synced_at = ? WHERE time_entry_id IN ({','.join('?' * len(ids))})",
            db.utcnow(), *ids,
        )
    return len(gone)


def snapshot_backlog(conn, local_date):
    """Start-of-day open backlog. "Open" uses the same rule as the dashboard:
    closedFlag false AND status isn't a closed-type status (some legacy
    tickets have closedFlag=false with a closed status name)."""
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM cw.backlog_daily WHERE snapshot_date = ?", local_date)
    if cur.fetchone():
        return 0
    closed = sorted(CLOSED_STATUSES)
    cur.execute(f"""
        INSERT INTO cw.backlog_daily (snapshot_date, board_id, status_name, priority_name, owner_identifier, open_count, oldest_age_days)
        SELECT ?, t.board_id, ISNULL(t.status_name, ''), ISNULL(t.priority_name, ''), ISNULL(t.owner_identifier, ''),
               COUNT(*), MAX(DATEDIFF(MINUTE, t.date_entered, SYSUTCDATETIME())) / 1440.0
        FROM cw.tickets t
        LEFT JOIN cw.board_statuses bs ON bs.status_id = t.status_id
        WHERE t.closed_flag = 0 AND t.deleted = 0 AND t.board_id IS NOT NULL
          AND ISNULL(bs.closed_status, 0) = 0
          AND LOWER(ISNULL(t.status_name, '')) NOT IN ({','.join('?' * len(closed))})
        GROUP BY t.board_id, ISNULL(t.status_name, ''), ISNULL(t.priority_name, ''), ISNULL(t.owner_identifier, '')
    """, local_date, *closed)
    return cur.rowcount


def run_daily_cw(cw, conn, now_utc):
    """Once per local calendar day. Returns True if it ran."""
    local_date = to_local(now_utc).date()
    state = db.get_state(conn, "cw_daily")
    if state["last_daily_job_date"] == local_date:
        return False
    started = db.utcnow()
    tickets_ready = db.get_state(conn, "cw_tickets")["backfill_complete"]
    try:
        n = sync_reference(cw, conn)
        conn.commit()
        n += refresh_open_tickets(cw, conn, tickets_ready)
        conn.commit()
        n += sync_time_sheets(cw, conn)
        conn.commit()
        removed = reconcile_time_entries(cw, conn)
        # A backlog snapshot of a half-loaded ticket table would be wrong,
        # and it can't be redone later — skip days until the backfill is done.
        snap = snapshot_backlog(conn, local_date) if tickets_ready else 0
        db.set_state(conn, "cw_daily", last_daily_job_date=local_date)
        db.log_run(conn, "cw_daily", started, "ok", n, f"{removed} deleted time entries; {snap} backlog rows")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logging.exception("ConnectWise daily job failed")
        db.log_run(conn, "cw_daily", started, "error", message=repr(exc))
        conn.commit()
    return True
