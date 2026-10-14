"""
One SyncMetrics run: every source, in order, each inside its own try/except
and time budget so one slow or failing source can't starve or break the
others. During the initial backfill each entity advances as far as its
budget allows and continues on the next run.

Budgets: host.json's functionTimeout is 9 minutes. RUN_BUDGET_S caps the
whole run below that; each entity gets min(its own share, what's left), and
is skipped entirely if less than its `min_start` remains — a budget is only
checked between windows/exports, so an entity can overrun its deadline by
one window (a Dialpad export alone can take ~150 s).
"""
import logging
from datetime import datetime, timedelta, timezone

from shared import db, sync_cw, sync_finance
from shared.aggregate import to_local
from shared.cw_client import ConnectWiseClient
from shared.sync_dialpad import sync_dialpad
from shared.sync_smileback import sync_smileback

RUN_BUDGET_S = 450

# (entity, own budget seconds, min seconds left to start, fn(cw, conn, deadline))
STEPS = [
    ("cw_tickets", 110, 30, sync_cw.sync_tickets),
    ("cw_time_entries", 80, 30, sync_cw.sync_time_entries),
    ("dp_agent_daily", 60, 200, lambda cw, conn, dl: sync_dialpad(conn, dl)),
    ("cw_invoices", 40, 30, sync_finance.sync_invoices),
    ("cw_products", 25, 30, sync_finance.sync_products),
    ("cw_additions", 25, 30, sync_finance.sync_additions),
    ("sb_reviews", 25, 30, lambda cw, conn, dl: sync_smileback(conn, dl)),
]


def _daily_agreements(cw, conn, now):
    local_date = to_local(now).date()
    if db.get_state(conn, "cw_agreements")["last_daily_job_date"] == local_date:
        return
    started = db.utcnow()
    try:
        n = sync_finance.sync_agreements(cw, conn)
        db.set_state(conn, "cw_agreements", last_daily_job_date=local_date)
        db.log_run(conn, "cw_agreements", started, "ok", n)
    except Exception as exc:
        conn.rollback()
        logging.exception("agreements sync failed")
        db.log_run(conn, "cw_agreements", started, "error", message=repr(exc))
    conn.commit()


def run_sync(now=None):
    now = now or datetime.now(timezone.utc)
    run_end = datetime.now(timezone.utc) + timedelta(seconds=RUN_BUDGET_S)
    cw = ConnectWiseClient()
    summary = {}
    with db.connection() as conn:
        try:
            summary["cw_daily"] = sync_cw.run_daily_cw(cw, conn, now)
            _daily_agreements(cw, conn, now)
        except Exception:
            logging.exception("daily ConnectWise jobs crashed")

        for entity, seconds, min_start, fn in STEPS:
            remaining = (run_end - datetime.now(timezone.utc)).total_seconds()
            if remaining < min_start:
                summary[entity] = "skipped (out of run budget)"
                continue
            started = db.utcnow()
            deadline = min(datetime.now(timezone.utc) + timedelta(seconds=seconds), run_end)
            try:
                n = fn(cw, conn, deadline)
                state = db.get_state(conn, entity)
                done = state["backfill_complete"] or entity == "cw_additions"
                db.log_run(conn, entity, started, "ok" if done else "partial", n,
                           None if done else f"backfilling — cursor at {state['cursor_utc']}")
                summary[entity] = n
            except Exception as exc:
                conn.rollback()
                logging.exception("%s sync failed", entity)
                db.log_run(conn, entity, started, "error", message=repr(exc))
                summary[entity] = "error"
            conn.commit()
    logging.info("run_sync complete: %s", summary)
    return summary
