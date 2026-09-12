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


def build_today_snapshot(tickets_by_board, member_display_names, now=None):
    """
    tickets_by_board: dict of board_key -> list of ticket dicts (as returned by
    ConnectWiseClient.list_tickets), containing ALL tickets touched today
    (opened today OR closed today OR currently open) for that board.
    member_display_names: dict mapping CW member identifier -> "First Last",
    used so the report shows human names instead of login IDs.
    """
    now = now or datetime.now(timezone.utc)
    today_str = now.date().isoformat()

    opened_total = closed_total = 0
    opened_by_board, closed_by_board = {}, {}
    opened_by_tech, closed_by_tech = {}, {}
    opened_unassigned = closed_unassigned = 0

    workload = {}  # owner identifier -> {"name":..., "open":0, "waiting":0}
    unassigned_open = unassigned_waiting = 0
    total_active_open = total_waiting = total_urgent_open = 0
    oldest_waiting = []

    for board_key, tickets in tickets_by_board.items():
        board_label = BOARD_LABELS.get(board_key, board_key)
        for t in tickets:
            owner_id = (t.get("owner") or {}).get("identifier")
            owner_display = member_display_names.get(owner_id, owner_id) if owner_id else None
            entered_date = (t.get("_info", {}).get("dateEntered") or t.get("dateEntered") or "")[:10]
            closed_date = (t.get("closedDate") or "")[:10]

            if entered_date == today_str:
                opened_total += 1
                opened_by_board[board_label] = opened_by_board.get(board_label, 0) + 1
                if owner_display:
                    opened_by_tech[owner_display] = opened_by_tech.get(owner_display, 0) + 1
                else:
                    opened_unassigned += 1

            if t.get("closedFlag") and closed_date == today_str:
                closed_total += 1
                closed_by_board[board_label] = closed_by_board.get(board_label, 0) + 1
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

                if owner_id:
                    w = workload.setdefault(owner_id, {"id": owner_id, "name": owner_display, "open": 0, "waiting": 0})
                    w["open"] += 1
                    if waiting:
                        w["waiting"] += 1
                else:
                    unassigned_open += 1
                    if waiting:
                        unassigned_waiting += 1

                last_touch = t.get("_info", {}).get("lastUpdated") or t.get("lastUpdated")
                hrs = _hours_since(last_touch, now) if last_touch else None
                if waiting and hrs is not None:
                    oldest_waiting.append({
                        "id": t.get("id"),
                        "board": board_label,
                        "owner": owner_display or "(Unassigned)",
                        "status": (t.get("status") or {}).get("name"),
                        "company": (t.get("company") or {}).get("name"),
                        "priority": (t.get("priority") or {}).get("name"),
                        "hoursSinceTouch": round(hrs, 1),
                    })

    oldest_waiting.sort(key=lambda x: x["hoursSinceTouch"], reverse=True)

    return {
        "asOf": now.strftime("%Y-%m-%dT%H:%M:00Z"),
        "opened": {
            "total": opened_total, "byBoard": opened_by_board,
            "byTech": opened_by_tech, "unassignedCount": opened_unassigned,
        },
        "closed": {
            "total": closed_total, "byBoard": closed_by_board,
            "byTech": closed_by_tech, "unassignedCount": closed_unassigned,
        },
        "workload": sorted(workload.values(), key=lambda w: w["open"], reverse=True),
        "unassignedBacklog": {
            "id": "(unassigned)", "name": "(Unassigned)",
            "open": unassigned_open, "waiting": unassigned_waiting,
        },
        "totalActiveOpen": total_active_open,
        "totalWaiting": total_waiting,
        "totalUrgentOpen": total_urgent_open,
        "oldestWaiting": oldest_waiting[:10],
    }
