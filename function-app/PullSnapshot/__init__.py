"""
Timer-triggered function: pulls live ConnectWise ticket data every 15 minutes,
builds the "Today's Snapshot" section of the report, and writes it (merged with
the existing weekly-trend / leaderboard / legacy sections) to blob storage as
`latest.json`. GetReportData (the HTTP-triggered function) serves that blob to
the frontend.

The weekly trend / tech leaderboard / per-board snapshot sections take longer
to compute (they scan more history) and don't need to be this fresh — this
scaffold recomputes everything every run for simplicity, but if ConnectWise API
rate limits become a concern, split those into their own timer (e.g. hourly)
and have this function only touch today's data.
"""
import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func
from azure.storage.blob import BlobServiceClient

from shared.cw_client import ConnectWiseClient
from shared.aggregate import build_today_snapshot, BOARD_LABELS

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


def main(myTimer: func.TimerRequest) -> None:
    now = datetime.now(timezone.utc)
    logging.info("PullSnapshot running at %s", now.isoformat())

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

    # Merge with whatever's already in the blob (weeklyTrend / techLeaderboard /
    # snapshot / legacy sections) rather than recomputing everything here.
    # A future iteration can move that logic into this same function or a
    # slower-cadence sibling timer — for now this scaffold keeps PullSnapshot
    # focused on the part the manager checks every few minutes.
    existing = _read_existing_blob()
    existing["todaySnapshot"] = today_snapshot
    existing["meta"] = existing.get("meta", {})
    existing["meta"]["lastUpdated"] = today_snapshot["asOf"]

    _write_blob(existing)
    logging.info("PullSnapshot complete — wrote %d bytes", len(json.dumps(existing)))


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
