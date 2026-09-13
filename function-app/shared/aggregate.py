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
from collections import Counter
from datetime import datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# Executive Summary / Service Desk / Tech Leaderboard — added 2026-09-13 to
# make these tabs live instead of frozen at the report's original generation
# time (2026-09-12). Each of these pulls a materially wider window than the
# "today" boards above (11 weeks of history, or the current full open
# backlog, or a 14-day closed window), so refresh.py wraps the calls into
# these functions in their own try/except — a failure here must never take
# down the Today's Snapshot half of a run, since that's the tab people watch
# live minute-to-minute.
# ---------------------------------------------------------------------------

# The four throughput boards that feed the Tech Leaderboard and Service Desk
# backlog snapshot. Dispatch is deliberately excluded from both — see the
# module docstring and BOARD_LABELS above: it's a pre-triage/routing queue,
# not a board techs "close tickets on."
LEADERBOARD_BOARD_KEYS = ["managedServices", "technicalServices", "alerts", "securityServices"]
LEADERBOARD_CLOSED_FIELD = {
    "managedServices": "closedMS",
    "technicalServices": "closedTS",
    "alerts": "closedAlerts",
    "securityServices": "closedSecurity",
}


def _week_start(dt):
    """Monday (as a date) of the week containing dt (a date or datetime)."""
    d = dt.date() if hasattr(dt, "date") else dt
    return d - timedelta(days=d.weekday())


def week_starts(now, count=11):
    """
    Returns `count` ISO week-start (Monday) date strings, oldest first,
    ending with the Monday of the CURRENT (possibly in-progress) week — this
    matches the shape of the original manually-built weeklyTrend.weeks (see
    report/data.json), where the last entry is the week containing the
    report's own generation date, not necessarily a completed week.
    """
    now = now or datetime.now(timezone.utc)
    current = _week_start(now)
    return [(current - timedelta(weeks=(count - 1 - i))).isoformat() for i in range(count)]


def _format_hms(total_seconds):
    """Formats a seconds count as H:MM:SS, matching the legacy report's
    manually-typed duration strings (e.g. "3:23:00") that phoneTechTable /
    phoneKpis in report.html already expect and render as-is."""
    total_seconds = int(round(total_seconds or 0))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def build_phone_history(by_week, weeks):
    """
    by_week: dict of week-start ISO date -> raw per-week Dialpad stats
    (DialpadClient.get_call_stats_for_week()'s return shape:
    {"totalInbound","answered","abandoned","answeredPct","avgWaitTimeSeconds",
    "techTalkTimeSeconds":{name:seconds},"techCallsAnswered":{name:count}}),
    or missing/None for a week that's never been fetched or whose fetch
    failed. `by_week` is the actual source of truth stored in the blob
    (`phoneHistoryByWeek`) — refresh.py only re-fetches the weeks that are
    missing from it plus the current in-progress week each run, so this
    function is a cheap, pure reshape and never itself calls Dialpad.

    weeks: the desired rolling window (oldest first) — see week_starts().

    Returns the exact shape the Phones tab's renderPhones() (report.html)
    expects — parallel arrays across `weeks`, plus per-tech dicts of the
    same shape — mirroring the field names the old manually-typed
    `legacy.phones` used, so the frontend rendering code barely had to
    change when this went live.
    """
    total_inbound, answered, abandoned, answered_pct, avg_wait = [], [], [], [], []
    tech_names = set()
    for wk in weeks:
        wdata = by_week.get(wk)
        if not wdata:
            total_inbound.append(0)
            answered.append(0)
            abandoned.append(0)
            answered_pct.append(None)
            avg_wait.append("0:00:00")
            continue
        total_inbound.append(wdata.get("totalInbound", 0))
        answered.append(wdata.get("answered", 0))
        abandoned.append(wdata.get("abandoned", 0))
        answered_pct.append(wdata.get("answeredPct"))
        avg_wait.append(_format_hms(wdata.get("avgWaitTimeSeconds", 0)))
        tech_names.update((wdata.get("techTalkTimeSeconds") or {}).keys())

    tech_talk_time, tech_calls_answered = {}, {}
    for name in sorted(tech_names):
        tech_talk_time[name] = []
        tech_calls_answered[name] = []
        for wk in weeks:
            wdata = by_week.get(wk) or {}
            secs = (wdata.get("techTalkTimeSeconds") or {}).get(name, 0.0)
            calls = (wdata.get("techCallsAnswered") or {}).get(name, 0)
            tech_talk_time[name].append(_format_hms(secs))
            tech_calls_answered[name].append(calls)

    return {
        "weeks": weeks,
        "totalInbound": total_inbound,
        "answered": answered,
        "abandoned": abandoned,
        "answeredPct": answered_pct,
        "avgWaitTime": avg_wait,
        "techTalkTime": tech_talk_time,
        "techCallsAnswered": tech_calls_answered,
    }


def _parse_date(iso_ts):
    if not iso_ts:
        return None
    try:
        return datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_weekly_trend(opened_by_board, closed_by_board, weeks):
    """
    opened_by_board / closed_by_board: dict of board_key -> list of tickets
    (minimal fields — id, _info/dateEntered, closedDate) covering the full
    `weeks` window: opened_by_board's tickets were pulled by dateEntered in
    range regardless of current status, closed_by_board's were pulled by
    closedDate in range with closedFlag=true. Buckets each ticket into the
    Monday-week it was opened/closed in and returns the exact
    {"weeks": [...], boardKey: {"opened": [...], "closed": [...]}} shape the
    frontend's Executive Summary trend chart already renders (see
    report/data.json's `weeklyTrend` for the reference shape) — one count
    per week, in the same order as `weeks`.
    """
    week_index = {w: i for i, w in enumerate(weeks)}
    result = {"weeks": weeks}
    for board_key in opened_by_board:
        opened_counts = [0] * len(weeks)
        closed_counts = [0] * len(weeks)
        for t in opened_by_board.get(board_key, []):
            entered = _parse_date(t.get("_info", {}).get("dateEntered") or t.get("dateEntered"))
            if entered is None:
                continue
            wk = _week_start(entered).isoformat()
            if wk in week_index:
                opened_counts[week_index[wk]] += 1
        for t in closed_by_board.get(board_key, []):
            closed = _parse_date(t.get("closedDate"))
            if closed is None:
                continue
            wk = _week_start(closed).isoformat()
            if wk in week_index:
                closed_counts[week_index[wk]] += 1
        result[board_key] = {"opened": opened_counts, "closed": closed_counts}
    return result


def build_backlog_snapshot(open_tickets_by_board, now=None):
    """
    open_tickets_by_board: dict of the four throughput board keys -> tickets
    for that board (a mix of currently-open and closed-today is fine — this
    re-filters with is_truly_open itself, so it can reuse the exact same
    pull run_refresh() already does for Today's Snapshot with no extra API
    call). Returns the Service Desk tab's {board_key: {...}} shape — see
    report/data.json's `snapshot` section for the reference shape.
    """
    now = now or datetime.now(timezone.utc)
    result = {}
    for board_key, tickets in open_tickets_by_board.items():
        open_tickets = [t for t in tickets if is_truly_open(t)]
        by_status, by_priority, by_type, by_company = Counter(), Counter(), Counter(), Counter()
        oldest = []

        for t in open_tickets:
            status = (t.get("status") or {}).get("name") or "(none)"
            priority = (t.get("priority") or {}).get("name") or "(none)"
            ttype = (t.get("type") or {}).get("name") or "(none)"
            company = (t.get("company") or {}).get("name") or "(none)"
            by_status[status] += 1
            by_priority[priority] += 1
            by_type[ttype] += 1
            by_company[company] += 1

            entered = t.get("_info", {}).get("dateEntered") or t.get("dateEntered")
            hrs = _hours_since(entered, now) if entered else None
            age_days = round(hrs / 24.0, 1) if hrs is not None else 0
            oldest.append({
                "id": t.get("id"),
                "summary": t.get("summary"),
                "company": company,
                "status": status,
                "priority": priority,
                "ageDays": age_days,
            })

        oldest.sort(key=lambda r: r["ageDays"], reverse=True)

        result[board_key] = {
            "openTotal": len(open_tickets),
            "byStatus": dict(by_status.most_common()),
            "byPriority": dict(by_priority.most_common()),
            "byType": dict(by_type.most_common()),
            "topCompanies": by_company.most_common(7),
            "oldestOpen": oldest[:10],
        }
    return result


def build_tech_leaderboard(closed_tickets_by_board, member_display_names, days=14, now=None):
    """
    closed_tickets_by_board: dict of the four throughput board keys (never
    dispatch) -> tickets closed in the last `days` days on that board
    (fields: id, owner, priority, source). Excludes unassigned tickets from
    every tech's counts, per the explicit rule carried over from the manual
    report — an unassigned ticket is never counted as a "closer." Returns the
    Tech Leaderboard tab's {"boards", "byTech", "sourceMix", "priorityMix",
    "totalClosed"} shape — see report/data.json's `techLeaderboard` for the
    reference shape.
    """
    now = now or datetime.now(timezone.utc)
    by_tech = {}
    source_mix = {k: Counter() for k in LEADERBOARD_BOARD_KEYS}
    priority_mix = {k: Counter() for k in LEADERBOARD_BOARD_KEYS}
    total_closed = {k: 0 for k in LEADERBOARD_BOARD_KEYS}

    for board_key in LEADERBOARD_BOARD_KEYS:
        field = LEADERBOARD_CLOSED_FIELD[board_key]
        for t in closed_tickets_by_board.get(board_key, []):
            owner_id = (t.get("owner") or {}).get("identifier")
            if not owner_id:
                continue  # unassigned tickets are never counted as a "closer"
            owner_name = member_display_names.get(owner_id, owner_id)
            entry = by_tech.setdefault(owner_id, {
                "id": owner_id, "name": owner_name,
                "closedMS": 0, "closedTS": 0, "closedAlerts": 0, "closedSecurity": 0, "closedTotal": 0,
            })
            entry[field] += 1
            entry["closedTotal"] += 1
            total_closed[board_key] += 1

            source = (t.get("source") or {}).get("name") or "(none)"
            priority = (t.get("priority") or {}).get("name") or "(none)"
            source_mix[board_key][source] += 1
            priority_mix[board_key][priority] += 1

    total_closed["all"] = sum(total_closed.values())

    all_source, all_priority = Counter(), Counter()
    for k in LEADERBOARD_BOARD_KEYS:
        all_source.update(source_mix[k])
        all_priority.update(priority_mix[k])

    return {
        "boards": ["Managed Services", "Technical Services", "Alerts", "Security Services"],
        "byTech": sorted(by_tech.values(), key=lambda e: e["closedTotal"], reverse=True),
        "sourceMix": {**{k: dict(v.most_common()) for k, v in source_mix.items()}, "all": dict(all_source.most_common())},
        "priorityMix": {**{k: dict(v.most_common()) for k, v in priority_mix.items()}, "all": dict(all_priority.most_common())},
        "totalClosed": total_closed,
    }
