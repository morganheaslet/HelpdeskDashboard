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
from pathlib import Path

from azure.storage.blob import BlobServiceClient

from shared.cw_client import ConnectWiseClient
from shared.aggregate import build_today_snapshot, build_dispatch_queue, is_truly_open, BOARD_LABELS
from shared.dialpad_client import DialpadClient

# The four throughput boards that feed the Tech Leaderboard (Dispatch never
# does — it's a pre-triage/routing queue, not a board techs "close tickets
# on"). Today's Snapshot is different: the frontend's activeBoardKeys() always
# includes Dispatch there (it has no on/off toggle), so Dispatch tickets are
# pulled the same way as these four and folded into tickets_by_board below —
# see DISPATCH_KEY.
BOARD_KEYS = ["managedServices", "technicalServices", "alerts", "securityServices"]
BOARD_ENV_VARS = {
    "managedServices": "CW_BOARD_MANAGED_SERVICES",
    "technicalServices": "CW_BOARD_TECHNICAL_SERVICES",
    "alerts": "CW_BOARD_ALERTS",
    "securityServices": "CW_BOARD_SECURITY_SERVICES",
}
DISPATCH_KEY = "dispatch"
DISPATCH_BOARD_ENV_VAR = "CW_BOARD_DISPATCH"

TICKET_FIELDS = [
    "id", "owner", "status", "priority", "company", "board", "summary",
    # ConnectWise nests audit-trail fields (dateEntered, lastUpdated) under
    # the ticket's `_info` sub-object rather than exposing them as top-level
    # attributes — requesting bare "dateEntered" returns nothing, which
    # silently zeroed out every opened-today count and every Dispatch
    # hoursInQueue value until this was caught against a live run.
    "_info/dateEntered", "closedFlag", "closedDate", "_info/lastUpdated",
]

# Dispatch tickets also need `contact` for the Dispatch (Live) tab's queue
# table — the other four boards don't render that column, so it stays off
# the base TICKET_FIELDS to keep those payloads smaller.
DISPATCH_TICKET_FIELDS = TICKET_FIELDS + ["contact"]


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

    # Dispatch — pulled with the same open+closed-today pattern as the four
    # throughput boards above (so it can contribute to Today's Snapshot's
    # totals the way the frontend expects), just with the extra `contact`
    # field the Dispatch (Live) tab's ticket table also needs.
    dispatch_board_id = os.environ.get(DISPATCH_BOARD_ENV_VAR)
    if dispatch_board_id:
        open_tickets = cw.list_tickets(dispatch_board_id, closed_flag=False, fields=DISPATCH_TICKET_FIELDS)
        closed_today = cw.list_tickets(
            dispatch_board_id, closed_flag=True, fields=DISPATCH_TICKET_FIELDS,
            conditions=f"closedDate>=[{today_iso}]",
        )
        by_id = {t["id"]: t for t in open_tickets}
        for t in closed_today:
            by_id.setdefault(t["id"], t)
        tickets_by_board[DISPATCH_KEY] = list(by_id.values())
        logging.info("Dispatch: %d tickets pulled", len(tickets_by_board[DISPATCH_KEY]))
    else:
        logging.info("CW_BOARD_DISPATCH not set — skipping Dispatch entirely (Today's Snapshot and Dispatch (Live) will both be missing it)")

    today_snapshot = build_today_snapshot(tickets_by_board, member_names, now=now)

    # Dispatch (Live) tab's queue view only cares about currently-open
    # tickets — reuse the pull above instead of a second API call.
    if dispatch_board_id:
        open_dispatch = [t for t in tickets_by_board[DISPATCH_KEY] if is_truly_open(t)]
        dispatch_queue = build_dispatch_queue(open_dispatch, now=now)
        logging.info(
            "Dispatch (Live): %d open tickets (%d unassigned)",
            dispatch_queue["total"], dispatch_queue["unassignedTotal"],
        )
    else:
        dispatch_queue = None

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
    if dispatch_queue is not None:
        existing["dispatchQueue"] = dispatch_queue
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


def _seed_data():
    """
    Fallback content for the very first run against a brand-new (empty) blob
    container. `shared/seed_data.json` is a copy of the report's original
    data.json — it carries the real weeklyTrend/snapshot/techLeaderboard/legacy
    sections (the manually-compiled historical data the frontend's Executive
    Summary, Service Desk, and Tech Leaderboard tabs render), which nothing
    else in this scaffold ever populates. Without this, a fresh deployment's
    first PullSnapshot/RefreshNow run would write a blob containing only
    empty {} placeholders for those sections — which run_refresh() then
    happily preserves forever afterwards (it only ever touches todaySnapshot/
    dispatchQueue/todayPhone) — and the frontend crashes trying to `.forEach`
    over data that was never actually there. This surfaced for real on
    2026-09-13: the live deployment's blob had exactly this problem, and had
    to be manually re-seeded (see README's "Re-seeding the blob" note).
    """
    seed_path = Path(__file__).parent / "seed_data.json"
    try:
        return json.loads(seed_path.read_text())
    except Exception:
        logging.exception("Bundled shared/seed_data.json missing or unreadable — falling back to an empty shell")
        return {"meta": {}, "weeklyTrend": {}, "snapshot": {}, "techLeaderboard": {}, "legacy": {}}


def _read_existing_blob():
    client = _blob_client()
    try:
        return json.loads(client.download_blob().readall())
    except Exception:
        logging.warning("No existing latest.json blob yet — seeding from shared/seed_data.json")
        return _seed_data()


def _write_blob(data):
    client = _blob_client()
    client.upload_blob(json.dumps(data), overwrite=True, content_settings=None)
