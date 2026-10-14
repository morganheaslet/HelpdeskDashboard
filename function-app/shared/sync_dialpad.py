"""
Dialpad -> dp.agent_daily (one row per agent per local day, through
yesterday — today's live numbers still come from the blob pipeline's
is_today pull).

Backfill: from BACKFILL_START in CHUNK_DAYS-day exports (Dialpad's stats
POST is limited to ~200/hour and each export takes 20-30+ s to process, so
fewer, bigger requests matter). If the export turns out not to carry a
per-day column, it drops to one day per request (remembered in
sync_state.cursor_id) — slower, but still correct.

Steady state: once per local day, re-pull the last REFRESH_DAYS days —
Dialpad's historical tables refresh every 4-5 hours, so yesterday's numbers
can still move the next morning.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from shared import db
from shared.aggregate import to_local
from shared.dialpad_client import DialpadClient, DialpadNoDateColumn

BACKFILL_START = date(2025, 1, 1)
CHUNK_DAYS = 30
REFRESH_DAYS = 3
ENTITY = "dp_agent_daily"


def _store(conn, stats):
    now = db.utcnow()
    return db.upsert(conn, "dp.agent_daily", ["stat_date", "agent_name"], [{
        "stat_date": date.fromisoformat(day), "agent_name": agent[:200],
        "inbound_calls": v["inbound"], "answered": v["answered"], "missed": v["missed"],
        "abandoned": v["abandoned"], "talk_seconds": round(v["talk_seconds"], 1),
        "ring_seconds": round(v["ring_seconds"], 1), "synced_at": now,
    } for (day, agent), v in stats.items()])


def sync_dialpad(conn, deadline, now_utc=None):
    client = DialpadClient()
    if not client._configured():
        logging.info("Dialpad not configured — skipping dp.agent_daily")
        return 0
    now_utc = now_utc or datetime.now(timezone.utc)
    today = to_local(now_utc).date()
    yesterday = today - timedelta(days=1)
    state = db.get_state(conn, ENTITY)
    chunk = state["cursor_id"] or CHUNK_DAYS
    total = 0

    if not state["backfill_complete"]:
        cursor = state["cursor_utc"].date() if state["cursor_utc"] else BACKFILL_START
        while cursor <= yesterday and datetime.now(timezone.utc) < deadline:
            end = min(cursor + timedelta(days=chunk - 1), yesterday)
            try:
                stats = client.get_agent_daily_stats(cursor, end, now=now_utc)
            except DialpadNoDateColumn:
                logging.warning("Dialpad export has no per-day column — switching to one day per request")
                chunk = 1
                db.set_state(conn, ENTITY, cursor_id=chunk)
                conn.commit()
                continue
            total += _store(conn, stats)
            cursor = end + timedelta(days=1)
            db.set_state(
                conn, ENTITY, cursor_utc=datetime.combine(cursor, datetime.min.time()),
                cursor_id=chunk, backfill_complete=cursor > yesterday,
                last_daily_job_date=today if cursor > yesterday else None,
            )
            conn.commit()
        return total

    if state["last_daily_job_date"] == today:
        return 0
    start = yesterday - timedelta(days=REFRESH_DAYS - 1)
    day = start
    while day <= yesterday:
        end = yesterday if chunk > 1 else day
        total += _store(conn, client.get_agent_daily_stats(day, end, now=now_utc))
        day = end + timedelta(days=1)
    db.set_state(conn, ENTITY, last_daily_job_date=today)
    conn.commit()
    return total
