"""
Dialpad client — pulls live call-center stats via Dialpad's Stats Export API.

## Auth
Dialpad's REST API accepts a static API key as a bearer token — no OAuth
authorization-code dance needed for a single-org, admin-generated key.
Generate one at Admin Settings > Integrations > API, then set it as
DIALPAD_API_KEY in Function App settings (see README step 6).

You also need a target to scope the stats to — either DIALPAD_OFFICE_ID
(pulls every eligible target under that office) or leave it unset to query
company-wide (Dialpad still requires at least one of office_id/target_id/
target_type on some accounts, so if calls fail with a 400, set the office id).
Find your office id with GET /api/v2/offices (see list_offices() below).

## How the Stats API actually works (this was the tricky part)
It's asynchronous, not a single request/response:
  1. POST /api/v2/stats to kick off processing -> returns a request_id
  2. Poll GET /api/v2/stats/{request_id} until status == "complete"
     (Dialpad's own docs recommend waiting ~15-20s before the first poll,
     then every 5-10s after that — hammering it with identical requests
     still counts against your rate limit even though no new work happens)
  3. download_url in the completed response points to a CSV file — fetch
     and parse that.

## CSV column names — READ THIS
Dialpad's docs describe the *request* parameters precisely (stat_type,
export_type, group_by, etc.) but do NOT publish an exact column list for the
resulting CSV in their API reference — they point to a help-center article
("Read Your Exported Analytics") instead. Rather than hardcode column names
that might not match your account's export format, `_find_col()` below matches
columns by case-insensitive substring (e.g. any header containing "answer"
counts as the answered-calls column). This is deliberately defensive, but it
means: **the first time you run this for real, check the Function App's logs**
— get_daily_call_stats() logs a warning listing the actual CSV headers if the
"answered" column pattern doesn't match anything, and you should sanity-check
the numbers against Dialpad's own analytics dashboard once. If Dialpad's export
format doesn't match these patterns, adjust the substrings passed to
_find_col() in get_daily_call_stats() rather than assuming the numbers are
right.
"""
import csv
import io
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

BASE_URL = "https://dialpad.com/api/v2"


class DialpadClient:
    def __init__(self):
        self.api_key = os.environ.get("DIALPAD_API_KEY", "")
        self.office_id = os.environ.get("DIALPAD_OFFICE_ID", "")

    def _configured(self):
        return bool(self.api_key) and self.api_key != "not-yet-configured"

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def list_offices(self):
        """GET /api/v2/offices — useful one-off call to find your office id."""
        resp = requests.get(f"{BASE_URL}/offices", headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json().get("items", [])

    def _create_stats_export(self, stat_type, export_type, **kwargs):
        body = {"stat_type": stat_type, "export_type": export_type, **kwargs}
        if self.office_id and "office_id" not in body:
            body["office_id"] = int(self.office_id)
        resp = requests.post(f"{BASE_URL}/stats", headers=self._headers(), json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()["request_id"]

    def _poll_stats_export(self, request_id, initial_wait_s=18, poll_interval_s=8, max_wait_s=120):
        time.sleep(initial_wait_s)
        waited = initial_wait_s
        while waited <= max_wait_s:
            resp = requests.get(f"{BASE_URL}/stats/{request_id}", headers=self._headers(), timeout=30)
            resp.raise_for_status()
            result = resp.json()
            status = result.get("status")
            if status == "complete":
                return result["download_url"]
            if status == "failed":
                raise RuntimeError(f"Dialpad stats export {request_id} failed")
            time.sleep(poll_interval_s)
            waited += poll_interval_s
        raise TimeoutError(f"Dialpad stats export {request_id} did not complete within {max_wait_s}s")

    def _download_csv(self, url):
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        return list(reader)

    def _run_export(self, stat_type, export_type, **kwargs):
        request_id = self._create_stats_export(stat_type, export_type, **kwargs)
        download_url = self._poll_stats_export(request_id)
        return self._download_csv(download_url)

    @staticmethod
    def _find_col(fieldnames, *substrings):
        """Case-insensitive substring match against CSV headers — see module
        docstring for why this isn't a hardcoded column list."""
        if not fieldnames:
            return None
        for field in fieldnames:
            low = field.lower()
            if all(s in low for s in substrings):
                return field
        return None

    @classmethod
    def _match_columns(cls, fieldnames):
        """
        Shared column-matching logic for both get_daily_call_stats() (today,
        via is_today=True) and get_call_stats_for_week() (a historical week,
        via days_ago_start/days_ago_end) — same export shape (group_by=user),
        so the same substring patterns apply to both.

        Confirmed against a real export on 2026-09-13 (one row per user per
        hour): columns are exactly 'name', 'answered', 'missed',
        'talk_duration' (cumulative seconds, NOT an average), 'abandoned',
        'inbound_calls', and 'ringing_duration' (also cumulative). Two of the
        original patterns matched the wrong column the first time this ran
        for real, both fixed here:
          - name: "user" matched 'user_id' before "name" ever got a chance
            (there IS a literal 'name' column) — reordered to prefer it.
          - duration: "talk"+"time" matched nothing (the real column is
            'talk_duration', no "time" in it), so it fell through to the
            generic "duration" pattern, which grabbed 'ringing_duration'
            instead — reordered to try "talk"+"duration" first.
        """
        return {
            "name": cls._find_col(fieldnames, "name") or cls._find_col(fieldnames, "user") or cls._find_col(fieldnames, "operator"),
            "answered": cls._find_col(fieldnames, "answer"),
            "missed": cls._find_col(fieldnames, "missed") or cls._find_col(fieldnames, "no", "answer"),
            "talk_duration": cls._find_col(fieldnames, "talk", "duration") or cls._find_col(fieldnames, "talk", "time") or cls._find_col(fieldnames, "duration") or cls._find_col(fieldnames, "avg", "time"),
            "abandoned": cls._find_col(fieldnames, "abandoned"),
            "inbound": cls._find_col(fieldnames, "inbound", "call"),
            "ring_duration": cls._find_col(fieldnames, "ringing", "duration") or cls._find_col(fieldnames, "avg", "ring"),
        }

    @staticmethod
    def _to_int(val):
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_seconds(val):
        """Handles either a raw seconds number or an H:MM:SS / MM:SS string."""
        if val in (None, ""):
            return 0.0
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
        parts = str(val).split(":")
        try:
            parts = [float(p) for p in parts]
        except ValueError:
            return 0.0
        seconds = 0.0
        for p in parts:
            seconds = seconds * 60 + p
        return seconds

    def get_daily_call_stats(self, date=None):
        """
        Returns:
        {
          "date": "2026-09-12",
          "answered": 0,
          "missed": 0,
          "avgTalkTimeSeconds": 0,
          "byAgent": {"Agent Name": {"answered": 0, "missed": 0}}
        }
        `date` is currently ignored beyond logging — is_today=True always
        pulls "today" per Dialpad's own real-time (30-min-refresh) tables,
        which is what a shift-check dashboard wants; historical single-day
        pulls would use days_ago_start/days_ago_end instead if ever needed.
        """
        if not self._configured():
            raise NotImplementedError(
                "Dialpad API key not configured — set DIALPAD_API_KEY in Function App settings "
                "once you've generated one (Admin Settings > Integrations > API)."
            )
        rows = self._run_export(
            stat_type="calls", export_type="stats", group_by="user", is_today=True, timezone="UTC",
        )
        if not rows:
            return {"date": date, "answered": 0, "missed": 0, "avgTalkTimeSeconds": 0, "byAgent": {}}

        fieldnames = list(rows[0].keys())
        cols = self._match_columns(fieldnames)

        # Always log the real CSV headers and which column each matched to,
        # not just as a warning when matching fails — 2026-09-13's first live
        # run came back all-zeros with a single "Unknown" agent row, but the
        # Function App log that was checked afterwards didn't actually contain
        # this diagnostic (either the match silently succeeded on a wrong
        # column, or the log view filtered an INFO/WARNING line out). Logging
        # unconditionally at INFO means every run's logs settle this either
        # way, without guessing at the substrings blind.
        logging.info(
            "Dialpad CSV columns: %s | matched %s | %d row(s)",
            fieldnames, cols, len(rows),
        )
        if not cols["answered"]:
            logging.warning(
                "Dialpad CSV columns didn't match expected patterns: %s — "
                "answered/missed/duration will read as 0 until the substring "
                "patterns in _match_columns() are adjusted to match this export.",
                fieldnames,
            )

        by_agent = {}
        total_answered = 0
        total_missed = 0
        talk_time_total = 0.0

        for row in rows:
            agent = (row.get(cols["name"]) or "Unknown").strip() if cols["name"] else "Unknown"
            answered = self._to_int(row.get(cols["answered"])) if cols["answered"] else 0
            missed = self._to_int(row.get(cols["missed"])) if cols["missed"] else 0
            duration = self._to_seconds(row.get(cols["talk_duration"])) if cols["talk_duration"] else 0.0

            entry = by_agent.setdefault(agent, {"answered": 0, "missed": 0})
            entry["answered"] += answered
            entry["missed"] += missed

            total_answered += answered
            total_missed += missed
            # cols["talk_duration"] is a CUMULATIVE seconds total per row (one
            # row per user per hour), not a per-call average — so summing it
            # across every row gives total talk seconds for the whole pull,
            # and dividing by total answered calls (not row count) below
            # gives a real average seconds-per-call figure.
            talk_time_total += duration

        return {
            "date": date,
            "answered": total_answered,
            "missed": total_missed,
            "avgTalkTimeSeconds": round(talk_time_total / total_answered, 1) if total_answered else 0,
            "byAgent": by_agent,
        }

    def get_call_stats_for_week(self, week_start_iso, now=None):
        """
        Pulls ONE week's aggregated call stats — Monday `week_start_iso`
        through that week's Sunday, or through today if the week is still in
        progress — via days_ago_start/days_ago_end instead of is_today.
        Used to build the Phones tab's rolling multi-week history
        (shared/refresh.py's phone-history block + aggregate.py's
        build_phone_history()) without needing to have stored anything in
        advance: Dialpad's Stats API can retrieve any past window on demand.

        Returns the per-week shape build_phone_history() expects:
        {"totalInbound","answered","abandoned","answeredPct",
         "avgWaitTimeSeconds","techTalkTimeSeconds":{name:seconds},
         "techCallsAnswered":{name:count}}
        — or None if the week is in the future, or the pull returned no
        rows at all (e.g. before Dialpad has any data that far back). A
        caller should keep whatever it already had stored for this week
        rather than treating None as "zero calls."
        """
        if not self._configured():
            raise NotImplementedError(
                "Dialpad API key not configured — set DIALPAD_API_KEY in Function App settings "
                "once you've generated one (Admin Settings > Integrations > API)."
            )
        now = now or datetime.now(timezone.utc)
        today = now.date()
        week_start = date.fromisoformat(week_start_iso)
        if week_start > today:
            return None
        week_end = min(week_start + timedelta(days=6), today)
        days_ago_start = (today - week_start).days
        days_ago_end = max((today - week_end).days, 0)

        rows = self._run_export(
            stat_type="calls", export_type="stats", group_by="user",
            days_ago_start=days_ago_start, days_ago_end=days_ago_end, timezone="UTC",
        )
        if not rows:
            return None

        fieldnames = list(rows[0].keys())
        cols = self._match_columns(fieldnames)
        logging.info(
            "Dialpad weekly pull (week of %s, %d-%d days ago): columns matched %s | %d row(s)",
            week_start_iso, days_ago_start, days_ago_end, cols, len(rows),
        )

        total_inbound = total_answered = total_abandoned = 0
        total_ring = 0.0
        tech_talk, tech_answered = {}, {}

        for row in rows:
            agent = (row.get(cols["name"]) or "Unknown").strip() if cols["name"] else "Unknown"
            answered = self._to_int(row.get(cols["answered"])) if cols["answered"] else 0
            inbound = self._to_int(row.get(cols["inbound"])) if cols["inbound"] else 0
            abandoned = self._to_int(row.get(cols["abandoned"])) if cols["abandoned"] else 0
            ring = self._to_seconds(row.get(cols["ring_duration"])) if cols["ring_duration"] else 0.0
            talk = self._to_seconds(row.get(cols["talk_duration"])) if cols["talk_duration"] else 0.0

            total_inbound += inbound
            total_answered += answered
            total_abandoned += abandoned
            total_ring += ring
            tech_talk[agent] = tech_talk.get(agent, 0.0) + talk
            tech_answered[agent] = tech_answered.get(agent, 0) + answered

        return {
            "totalInbound": total_inbound,
            "answered": total_answered,
            "abandoned": total_abandoned,
            "answeredPct": round(total_answered / total_inbound, 3) if total_inbound else None,
            # ringing_duration is cumulative across every row (like
            # talk_duration) — dividing by total inbound calls gives a real
            # average wait-before-answer-or-abandon per call, not per row.
            "avgWaitTimeSeconds": round(total_ring / total_inbound, 1) if total_inbound else 0.0,
            "techTalkTimeSeconds": tech_talk,
            "techCallsAnswered": tech_answered,
        }
