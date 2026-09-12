"""
The actual "pull everything and write the blob" logic, shared by two triggers:

- PullSnapshot (timer, every 15 min) — the regular background refresh.
- RefreshNow (HTTP, POST /api/RefreshNow) — the "Refresh Now" button in the
  report's header, for when someone doesn't want to wait for the next timer
  tick. Both call run_refresh() below and do nothing else — this way there is
  exactly one place that knows how to pull ConnectWise/Dialpad and merge the
  result into the blob, instead of two copies that could drift apart.

run_refresh() is synchronous and can take a while — Dialpad's stats export in
particular is asynchronous on their end (submit a job, poll for ~20-30+
seconds). That's fine for a timer, and fine for an HTTP call too as long as
the caller (the frontend's Refresh Now button) shows a "refreshing…" state
and doesn't assume this returns instantly.
"""
import json
import logging
import os
from datetime import datetime, timezone

from azure.storage.blob import BlobServiceClient

from shared.cw_client import ConnectWiseClient
from shared.aggregate import build_today_snapshot, BOARD_LABELS
from shared.dialpad_client import DialpadClient

BOARD_KEYS = ["managedServices", "technicalServices", "alerts", "securityServices"]
BOARD_ENV_VARS = {
    "managedServices": "CW_BOARD_MANAGED_SERVICES",
    "technicalServices": "CW_BOARD_TECHNICAL_SERVICES",
    "alerts": "CW_BOARD_ALERTS",
    "securityServices": "CW_BOARD_SECURITY_SERVICES",
}

TICKET_FIELDS = [
    "id", "owner", "status", "priority", "company", "board",
    "dateEntered", "closedFlag", "closedDate", "_info/lastUpdated",
]


def run_refresh(now=None):
    """Pulls ConnectWise + Dialpad, merges into the existing blob, writes it,
    and returns the full updated dict (so an HTTP caller can hand it straight
    back to the frontend without a second round-trip to GetReportData)."""
    now = now or datetime.now(timezone.utc)
    logging.info("run_refresh starting at %s", now.isoformat())

    cw = ConnectWiseClient()
    member_names = {
        m["identifier"]: f"{m.get('firstName', '')} {m.get('lastName', '')}".strip() or m["identifier"]
        for m in cw.list_members()
    }

    today_iso = now.strftime("%Y-%m-%dT00:00:00Z")
    tickets_by_board = {}
    for key in BOARD_KEYS:
        board_id = os.environ[BOARD_ENV_VARS[key]]
        # Pull anything open right now, OR touched (opened/closed) today —
        # that covers everything build_today_snapshot needs in one call per board.
        open_tickets = cw.list_tickets(board_id, closed_flag=False, fields=TICKET_FIELDS)
        closed_today = cw.list_tickets(
            board_id, closed_flag=True, fields=TICKET_FIELDS,
            conditions=f"closedDate>=[{today_iso}]",
        )
        # de-dupe by id in case a ticket was both opened and closed today
        by_id = {t["id"]: t for t in open_tickets}
        for t in closed_today:
            by_id.setdefault(t["id"], t)
        tickets_by_board[key] = list(by_id.values())
        logging.info("%s: %d tickets pulled", BOARD_LABELS[key], len(tickets_by_board[key]))

    today_snapshot = build_today_snapshot(tickets_by_board, member_names, now=now)

    # Live phone stats (Today's Snapshot tab) — optional until DIALPAD_API_KEY
    # is configured. Failures here (not-yet-configured, a transient API error,
    # a slow/timed-out export) must never take down the ConnectWise half of
    # this run, so they're caught and logged rather than raised; the frontend
    # falls back to its "not live yet" note whenever todayPhone is absent.
    today_phone = None
    try:
        dialpad = DialpadClient()
        today_phone = dialpad.get_daily_call_stats(date=now.strftime("%Y-%m-%d"))
        logging.info(
            "Dialpad: %d answered, %d missed today across %d agents",
            today_phone["answered"], today_phone["missed"], len(today_phone["byAgent"]),
        )
    except NotImplementedError:
        logging.info("Dialpad not configured yet (DIALPAD_API_KEY unset) — skipping live phone stats")
    except Exception:
        logging.exception("Dialpad pull failed — leaving todayPhone out of this run's blob")

    # Merge with whatever's already in the blob (weeklyTrend / techLeaderboard /
    # snapshot / legacy sections) rather than recomputing everything here.
    existing = _read_existing_blob()
    existing["todaySnapshot"] = today_snapshot
    if today_phone is not None:
        existing["todayPhone"] = today_phone
    existing["meta"] = existing.get("meta", {})
    existing["meta"]["lastUpdated"] = today_snapshot["asOf"]

    _write_blob(existing)
    logging.info("run_refresh complete — wrote %d bytes", len(json.dumps(existing)))
    return existing


def _blob_client():
    conn_str = os.environ["STORAGE_CONNECTION_STRING"]
    container = os.environ.get("DATA_CONTAINER", "helpdesk-report-data")
    service = BlobServiceClient.from_connection_string(conn_str)
    try:
        service.create_container(container)
    except Exception:
        pass  # already exists
    return service.get_blob_client(container=container, blob="latest.json")


def _read_existing_blob():
    client = _blob_client()
    try:
        return json.loads(client.download_blob().readall())
    except Exception:
        logging.warning("No existing latest.json blob yet — starting from empty shell")
        return {"meta": {}, "weeklyTrend": {}, "snapshot": {}, "techLeaderboard": {}, "legacy": {}}


def _write_blob(data):
    client = _blob_client()
    client.upload_blob(json.dumps(data), overwrite=True, content_settings=None)
