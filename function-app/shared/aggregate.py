"""
Port of the aggregation logic used to build the report's data.json, so the
scheduled Function can produce the same shape the frontend already renders.

Two data-quality rules carried over from the manual build of this report:

1. CLOSED_STATUSES — this ConnectWise instance has some legacy tickets where
   `closedFlag` is False but the status name is actually a closed-type status
   (e.g. "Closed", "Resolved*", "Canceled", "No Communication", "QC Hold",
   "QC/No Communication"). Anything with `closedFlag=False` still needs this
   filter applied client-side before it's counted as genuinely open/active.

2. Unassigned-ticket exclusion (per explicit instruction) — tickets with no
   owner are NEVER counted as a tech's opened/closed totals and never appear
   as a "closer" in the tech leaderboard. They still show up in a separate
   unassigned-backlog bucket for triage visibility, since that's operational
   awareness, not a throughput metric.

3. "Waiting on first touch" — status in {New, Assigned*, Assigned, Client
   Responded}, per the definition the helpdesk manager gave for what counts
   as untouched-since-last-client-contact.
"""
from datetime import datetime, timezone

CLOSED_STATUSES = {
    "closed", "resolved*", "resolved", "canceled", "cancelled",
    "no communication", "qc hold", "qc/no communication",
}
WAITING_STATUSES = {"new", "assigned*", "assigned", "client responded"}

BOARD_LABELS = {
    "managedServices": "Managed Services",
    "technicalServices": "Technical Services",
    "alerts": "Alerts",
    "securityServices": "Security Services",
    "dispatch": "Dispatch",
}


def _hours_since(iso_ts, now):
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (now - dt).total_seconds() / 3600.0


def _owner_name(ticket):
    owner = (ticket.get("owner") or {}).get("name") or (ticket.get("owner") or {}).get("identifier")
    return owner or None  # None => unassigned


def is_truly_open(ticket):
    """closedFlag is authoritative when True; when False, still exclude stale
    tickets whose status name is actually a closed-type status."""
    if ticket.get("closedFlag"):
        return False
    status_name = ((ticket.get("status") or {}).get("name") or "").strip().lower()
    return status_name not in CLOSED_STATUSES


def is_waiting(ticket):
    status_name = ((ticket.get("status") or {}).get("name") or "").strip().lower()
    return status_name in WAITING_STATUSES


def _build_board_snapshot(tickets, board_label, member_display_names, now):
    """
    Aggregates ONE board's tickets into the exact shape the frontend's
    mergeTodaySnapshot() expects at D.todaySnapshot.perBoard[boardKey] — see
    that function in report.html for the merge logic that combines these
    across whichever boards are currently active (the Alerts/Security
    Services toggles, plus Dispatch, which is always included).
    """
    today_str = now.date().isoformat()

    opened_total = closed_total = 0
    opened_by_tech, closed_by_tech = {}, {}
    opened_unassigned = closed_unassigned = 0

    workload = {}  # owner identifier -> {"id":...,"name":...,"open":0,"waiting":0}
    unassigned_open = unassigned_waiting = 0
    total_active_open = total_waiting = total_urgent_open = 0
    waiting_candidates = []
    unassigned_tickets = []

    for t in tickets:
        owner_id = (t.get("owner") or {}).get("identifier")
        owner_display = member_display_names.get(owner_id, owner_id) if owner_id else None
        entered_date = (t.get("_info", {}).get("dateEntered") or t.get("dateEntered") or "")[:10]
        closed_date = (t.get("closedDate") or "")[:10]

        if entered_date == today_str:
            opened_total += 1
            if owner_display:
                opened_by_tech[owner_display] = opened_by_tech.get(owner_display, 0) + 1
            else:
                opened_unassigned += 1

        if t.get("closedFlag") and closed_date == today_str:
            closed_total += 1
            if owner_display:
                closed_by_tech[owner_display] = closed_by_tech.get(owner_display, 0) + 1
            else:
                closed_unassigned += 1

        if is_truly_open(t):
            total_active_open += 1
            waiting = is_waiting(t)
            if waiting:
                total_waiting += 1
            priority_name = ((t.get("priority") or {}).get("name") or "").lower()
            if "critical" in priority_name or "high" in priority_name:
                total_urgent_open += 1

            entered = t.get("_info", {}).get("dateEntered") or t.get("dateEntered")
            last_touch = t.get("_info", {}).get("lastUpdated") or t.get("lastUpdated")
            hrs_touch = _hours_since(last_touch, now) if last_touch else None
            hrs_queue = _hours_since(entered, now) if entered else None

            if owner_id:
                w = workload.setdefault(owner_id, {"id": owner_id, "name": owner_display, "open": 0, "waiting": 0})
                w["open"] += 1
                if waiting:
                    w["waiting"] += 1
            else:
                unassigned_open += 1
                if waiting:
                    unassigned_waiting += 1
                unassigned_tickets.append({
                    "id": t.get("id"),
                    "board": board_label,
                    "company": (t.get("company") or {}).get("name"),
                    "summary": t.get("summary"),
                    "hoursInQueue": round(hrs_queue, 1) if hrs_queue is not None else None,
                })

            if waiting and hrs_touch is not None:
                waiting_candidates.append({
                    "id": t.get("id"),
                    "board": board_label,
                    "owner": owner_display or "(Unassigned)",
                    "status": (t.get("status") or {}).get("name"),
                    "company": (t.get("company") or {}).get("name"),
                    "summary": t.get("summary"),
                    "priority": (t.get("priority") or {}).get("name"),
                    "hoursSinceTouch": round(hrs_touch, 1),
                })

    return {
        "opened": {"total": opened_total, "byTech": opened_by_tech, "unassignedCount": opened_unassigned},
        "closed": {"total": closed_total, "byTech": closed_by_tech, "unassignedCount": closed_unassigned},
        "workload": list(workload.values()),
        "unassignedBacklog": {"open": unassigned_open, "waiting": unassigned_waiting},
        "totalActiveOpen": total_active_open,
        "totalWaiting": total_waiting,
        "totalUrgentOpen": total_urgent_open,
        "waitingCandidates": waiting_candidates,
        "unassignedTickets": unassigned_tickets,
    }


def build_today_snapshot(tickets_by_board, member_display_names, now=None):
    """
    tickets_by_board: dict of board_key -> list of ticket dicts (as returned by
    ConnectWiseClient.list_tickets), containing ALL tickets touched today
    (opened today OR closed today OR currently open) for that board. Include
    a "dispatch" key here too — the frontend's activeBoardKeys() always
    includes Dispatch in Today's Snapshot (it has no on/off toggle), so
    without a "dispatch" entry here the frontend crashes trying to read
    D.todaySnapshot.perBoard.dispatch.
    member_display_names: dict mapping CW member identifier -> "First Last",
    used so the report shows human names instead of login IDs.

    Returns {"asOf": ..., "perBoard": {board_key: {...}, ...}} — the frontend's
    mergeTodaySnapshot() does the cross-board combining itself (respecting the
    Include Alerts / Include Security Services toggles), so this function
    deliberately does NOT pre-merge across boards the way it used to.
    """
    now = now or datetime.now(timezone.utc)
    per_board = {}
    for board_key, tickets in tickets_by_board.items():
        board_label = BOARD_LABELS.get(board_key, board_key)
        per_board[board_key] = _build_board_snapshot(tickets, board_label, member_display_names, now)

    return {
        "asOf": now.strftime("%Y-%m-%dT%H:%M:00Z"),
        "perBoard": per_board,
    }


def build_dispatch_queue(tickets, now=None):
    """
    Builds the Dispatch (Live) tab's data from the raw list of currently-open
    tickets on the Dispatch board (board id 1 on this instance).

    Unlike the four boards fed into build_today_snapshot, Dispatch is a
    pre-triage/routing queue — most tickets sit here with no tech assigned
    until they're routed elsewhere, so a high unassigned count is normal and
    expected, not a problem. Per the report's own explanation text, Dispatch
    is deliberately excluded from build_today_snapshot / the Tech Leaderboard
    entirely — it isn't a board a tech "closes tickets on" — so this stays a
    separate, simpler aggregation: every open ticket on the board, with how
    long it's been sitting there (hoursInQueue, since dateEntered) and how
    long since anyone touched it (hoursSinceTouch, since lastUpdated).
    """
    now = now or datetime.now(timezone.utc)
    rows = []
    unassigned_total = 0

    for t in tickets:
        owner_id = (t.get("owner") or {}).get("identifier")
        owner_name = (t.get("owner") or {}).get("name") or owner_id
        if not owner_name:
            owner_name = "(Unassigned)"
            unassigned_total += 1

        entered = t.get("_info", {}).get("dateEntered") or t.get("dateEntered")
        last_touch = t.get("_info", {}).get("lastUpdated") or t.get("lastUpdated")
        hours_in_queue = _hours_since(entered, now) if entered else None
        hours_since_touch = _hours_since(last_touch, now) if last_touch else None

        rows.append({
            "id": t.get("id"),
            "company": (t.get("company") or {}).get("name"),
            "contact": (t.get("contact") or {}).get("name"),
            "summary": t.get("summary"),
            "status": (t.get("status") or {}).get("name"),
            "priority": (t.get("priority") or {}).get("name"),
            "owner": owner_name,
            "hoursInQueue": round(hours_in_queue, 1) if hours_in_queue is not None else None,
            "hoursSinceTouch": round(hours_since_touch, 1) if hours_since_touch is not None else None,
        })

    rows.sort(key=lambda r: (r["hoursInQueue"] if r["hoursInQueue"] is not None else -1), reverse=True)

    return {
        "asOf": now.strftime("%Y-%m-%dT%H:%M:00Z"),
        "total": len(rows),
        "unassignedTotal": unassigned_total,
        "tickets": rows,
    }
