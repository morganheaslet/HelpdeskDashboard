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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from azure.storage.blob import BlobServiceClient

from shared.cw_client import ConnectWiseClient
from shared.aggregate import (
    build_today_snapshot, build_dispatch_queue, is_truly_open, BOARD_LABELS,
    build_weekly_trend, build_backlog_snapshot, build_tech_leaderboard, week_starts,
    build_phone_history,
)
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
    "id", "owner", "status", "priority", "type", "company", "board", "summary",
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

# --- Executive Summary / Service Desk / Tech Leaderboard (added 2026-09-13) ---
# These three tabs used to be frozen at the report's original generation date
# — run_refresh() never touched weeklyTrend/snapshot/techLeaderboard. Making
# them live means three extra, wider pulls per throughput board (never
# Dispatch — see LEADERBOARD_BOARD_KEYS in aggregate.py):
#   1. every ticket opened in the last WEEKLY_TREND_WEEKS weeks (by dateEntered)
#   2. every ticket closed in that same window (by closedDate)
#   3. every ticket closed in the last LEADERBOARD_DAYS days, with owner/
#      priority/source, for the tech leaderboard
# The current backlog snapshot (Service Desk tab) needs no extra pull at all —
# it's built from the same open+closed-today tickets already fetched below
# for Today's Snapshot (see build_backlog_snapshot's docstring).
WEEKLY_TREND_WEEKS = 11
LEADERBOARD_DAYS = 14
MINIMAL_DATE_FIELDS = ["id", "_info/dateEntered", "closedDate"]
LEADERBOARD_FIELDS = ["id", "owner", "priority", "source"]

# --- Phones tab — Dialpad weekly history (added 2026-09-13) ---
# Same rolling-window idea as the ConnectWise weekly trend above, but a
# per-week Dialpad export takes ~20-30+ seconds (the same async job/poll
# pattern as the "today" pull), so re-fetching all PHONE_HISTORY_WEEKS weeks
# on every single PullSnapshot/RefreshNow run would make every refresh take
# several extra minutes for no reason — only the current week's numbers
# actually change between runs. Instead, `phoneHistoryByWeek` in the blob is
# the real source of truth (week-start ISO date -> that week's raw stats);
# each run only fetches weeks missing from it, plus the current
# (in-progress) week every time. A brand-new deployment backfills all
# PHONE_HISTORY_WEEKS on its first run; after that, steady-state runs make
# exactly one Dialpad call here.
#
# Confirmed live 2026-09-13: a single Dialpad export can itself take longer
# than expected (one daily pull timed out at the poller's old 120s cap,
# since bumped to 150s in dialpad_client.py). With up to PHONE_HISTORY_WEEKS
# calls queued back-to-back on a backfill run, several slow ones in a row
# could otherwise blow well past host.json's functionTimeout before this
# function ever gets to write the blob — losing the ConnectWise work that
# same run already did, since _write_blob() only happens once at the very
# end. DIALPAD_HISTORY_BUDGET_S bounds how much wall-clock time this block
# will spend fetching weeks: it stops starting new week pulls once the
# budget is used up, leaving any remaining weeks for the next run (they're
# just "still missing from phoneHistoryByWeek," which the logic above
# already retries every run) instead of risking the whole function.
PHONE_HISTORY_WEEKS = 8
DIALPAD_HISTORY_BUDGET_S = 300


def run_refresh(now=None):
    """Pulls ConnectWise + Dialpad, merges into the existing blob, writes it,
    and returns the full updated dict (so an HTTP caller can hand it straight
    back to the frontend without a second round-trip to GetReportData)."""
    now = now or datetime.now(timezone.utc)
    # Real wall-clock start, independent of `now` (which is the logical "as
    # of" timestamp this run represents — normally the same instant, but
    # callers such as tests can pass a fixed value). Used only to bound the
    # Dialpad weekly-history backfill's time budget below.
    run_started_at = datetime.now(timezone.utc)
    logging.info("run_refresh starting at %s", now.isoformat())

    # Read this up front (not just at the very end, as before) because the
    # Dialpad weekly-history block below needs to see what's already stored
    # in phoneHistoryByWeek to know which weeks it can skip re-fetching.
    existing = _read_existing_blob()

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

    # Executive Summary / Service Desk / Tech Leaderboard — see the constants'
    # comments above for why these are separate, wider pulls. Wrapped the same
    # defensive way as Dialpad: a failure here (a transient API error, a slow
    # response) must never take down the Today's Snapshot half of this run,
    # since that's the tab people watch live minute-to-minute. On failure the
    # blob simply keeps whatever these three sections were last time.
    weekly_trend = backlog_snapshot = tech_leaderboard = None
    try:
        weeks = week_starts(now, WEEKLY_TREND_WEEKS)
        window_start = weeks[0] + "T00:00:00Z"
        leaderboard_window_start = (now - timedelta(days=LEADERBOARD_DAYS)).strftime("%Y-%m-%dT00:00:00Z")

        opened_by_board, closed_by_board, leaderboard_closed_by_board = {}, {}, {}
        for key in BOARD_KEYS:
            board_id = os.environ[BOARD_ENV_VARS[key]]
            # Every ticket opened in the trend window, regardless of current
            # status — bucketed client-side by the week it was opened in.
            opened_by_board[key] = cw.list_tickets(
                board_id, fields=MINIMAL_DATE_FIELDS,
                conditions=f"_info/dateEntered>=[{window_start}]",
            )
            # Every ticket closed in that same window, bucketed by closedDate.
            closed_by_board[key] = cw.list_tickets(
                board_id, closed_flag=True, fields=MINIMAL_DATE_FIELDS,
                conditions=f"closedDate>=[{window_start}]",
            )
            # A separate, shorter window for the tech leaderboard, with the
            # extra owner/priority/source fields it needs.
            leaderboard_closed_by_board[key] = cw.list_tickets(
                board_id, closed_flag=True, fields=LEADERBOARD_FIELDS,
                conditions=f"closedDate>=[{leaderboard_window_start}]",
            )

        weekly_trend = build_weekly_trend(opened_by_board, closed_by_board, weeks)
        # Reuses the open+closed-today tickets already pulled above for
        # Today's Snapshot — no extra API call for the current backlog.
        backlog_snapshot = build_backlog_snapshot(
            {key: tickets_by_board[key] for key in BOARD_KEYS}, now=now,
        )
        tech_leaderboard = build_tech_leaderboard(
            leaderboard_closed_by_board, member_names, days=LEADERBOARD_DAYS, now=now,
        )
        logging.info(
            "Weekly trend / backlog snapshot / tech leaderboard rebuilt (%d weeks, %d-day leaderboard window)",
            WEEKLY_TREND_WEEKS, LEADERBOARD_DAYS,
        )
    except Exception:
        logging.exception(
            "Weekly trend / backlog snapshot / tech leaderboard pull failed — "
            "leaving those three blob sections as whatever was there before this run"
        )

    # Phones tab — Dialpad weekly history. See PHONE_HISTORY_WEEKS' comment
    # above for why this only fetches missing/current weeks instead of the
    # whole window every run. Wrapped the same defensive way as everything
    # else Dialpad-related: a failure here must never take down the
    # ConnectWise half of this run.
    phone_history = None
    try:
        dialpad_weekly = DialpadClient()
        if not dialpad_weekly._configured():
            raise NotImplementedError("Dialpad API key not configured")

        phone_weeks = week_starts(now, PHONE_HISTORY_WEEKS)
        by_week = dict(existing.get("phoneHistoryByWeek") or {})
        current_week = phone_weeks[-1]
        today_str = now.date().isoformat()

        # Completed (non-current) weeks are backfilled once and never
        # change again, so they're only fetched if missing entirely. The
        # CURRENT week is the one exception — it's still in progress, so its
        # numbers do change during the day — but re-pulling it on every
        # 15-minute PullSnapshot/RefreshNow call is unnecessary Dialpad load
        # for data that's a rolling multi-week trend, not a live-second-by-
        # second view (that's what Today's Snapshot's separate live phone
        # card is for). So it only refreshes once per calendar day: each
        # stored week carries a "_fetchedAt" timestamp, and the current week
        # is re-fetched only if it's missing or wasn't already fetched today.
        current_week_entry = by_week.get(current_week)
        current_week_fresh_today = bool(
            current_week_entry and current_week_entry.get("_fetchedAt", "")[:10] == today_str
        )
        weeks_to_fetch = [w for w in phone_weeks if w != current_week and w not in by_week]
        if not current_week_fresh_today:
            weeks_to_fetch.insert(0, current_week)  # do it first when it IS due

        deadline = run_started_at + timedelta(seconds=DIALPAD_HISTORY_BUDGET_S)
        fetched, skipped_out_of_budget = [], []
        for wk in weeks_to_fetch:
            if datetime.now(timezone.utc) >= deadline:
                skipped_out_of_budget.append(wk)
                continue
            try:
                week_data = dialpad_weekly.get_call_stats_for_week(wk, now=now)
                if week_data is not None:
                    week_data["_fetchedAt"] = now.isoformat()
                    by_week[wk] = week_data
                fetched.append(wk)
            except Exception:
                logging.exception(
                    "Dialpad weekly pull failed for week of %s — keeping whatever "
                    "was already stored for that week", wk,
                )
                fetched.append(wk)  # attempted, not skipped — counts against the budget either way

        # Drop weeks that have rolled out of the window so the blob doesn't
        # grow forever.
        by_week = {wk: v for wk, v in by_week.items() if wk in phone_weeks}
        existing["phoneHistoryByWeek"] = by_week
        phone_history = build_phone_history(by_week, phone_weeks)
        if fetched:
            logging.info(
                "Dialpad weekly history: attempted %d/%d week(s) this run (%s)%s",
                len(fetched), len(phone_weeks), ", ".join(fetched),
                f" — {len(skipped_out_of_budget)} week(s) deferred to a later run (out of the {DIALPAD_HISTORY_BUDGET_S}s budget): {', '.join(skipped_out_of_budget)}" if skipped_out_of_budget else "",
            )
        else:
            logging.info(
                "Dialpad weekly history: nothing to fetch this run — current week already "
                "refreshed today (%s) and no backfill weeks missing", today_str,
            )
    except NotImplementedError:
        logging.info("Dialpad not configured yet — skipping weekly phone history")
    except Exception:
        logging.exception(
            "Dialpad weekly history pull failed — leaving phoneHistory as "
            "whatever was in the blob before this run"
        )

    # Merge with whatever's already in the blob (legacy section, plus
    # weeklyTrend/snapshot/techLeaderboard/phoneHistory on any run where the
    # corresponding block above failed) rather than recomputing everything here.
    existing["todaySnapshot"] = today_snapshot
    if dispatch_queue is not None:
        existing["dispatchQueue"] = dispatch_queue
    if today_phone is not None:
        existing["todayPhone"] = today_phone
    if weekly_trend is not None:
        existing["weeklyTrend"] = weekly_trend
    if backlog_snapshot is not None:
        existing["snapshot"] = backlog_snapshot
    if tech_leaderboard is not None:
        existing["techLeaderboard"] = tech_leaderboard
    if phone_history is not None:
        existing["phoneHistory"] = phone_history
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
