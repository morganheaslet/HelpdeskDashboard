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
        name_col = self._find_col(fieldnames, "user") or self._find_col(fieldnames, "name") or self._find_col(fieldnames, "operator")
        answered_col = self._find_col(fieldnames, "answer")
        missed_col = self._find_col(fieldnames, "missed") or self._find_col(fieldnames, "no", "answer")
        duration_col = self._find_col(fieldnames, "talk", "time") or self._find_col(fieldnames, "duration") or self._find_col(fieldnames, "avg", "time")

        # Always log the real CSV headers and which column each matched to,
        # not just as a warning when matching fails — 2026-09-13's first live
        # run came back all-zeros with a single "Unknown" agent row, but the
        # Function App log that was checked afterwards didn't actually contain
        # this diagnostic (either the match silently succeeded on a wrong
        # column, or the log view filtered an INFO/WARNING line out). Logging
        # unconditionally at INFO means the *next* run's logs settle this
        # either way, without guessing at the substrings blind.
        logging.info(
            "Dialpad CSV columns: %s | matched name=%r answered=%r missed=%r duration=%r | %d row(s)",
            fieldnames, name_col, answered_col, missed_col, duration_col, len(rows),
        )
        if not answered_col:
            logging.warning(
                "Dialpad CSV columns didn't match expected patterns: %s — "
                "answered/missed/duration will read as 0 until _extract() column "
                "patterns in dialpad_client.py are adjusted to match this export.",
                fieldnames,
            )

        by_agent = {}
        total_answered = 0
        total_missed = 0
        talk_time_total = 0.0
        talk_time_count = 0

        for row in rows:
            agent = (row.get(name_col) or "Unknown").strip() if name_col else "Unknown"
            answered = self._to_int(row.get(answered_col)) if answered_col else 0
            missed = self._to_int(row.get(missed_col)) if missed_col else 0
            duration = self._to_seconds(row.get(duration_col)) if duration_col else 0.0

            entry = by_agent.setdefault(agent, {"answered": 0, "missed": 0})
            entry["answered"] += answered
            entry["missed"] += missed

            total_answered += answered
            total_missed += missed
            if duration:
                talk_time_total += duration
                talk_time_count += 1

        return {
            "date": date,
            "answered": total_answered,
            "missed": total_missed,
            "avgTalkTimeSeconds": round(talk_time_total / talk_time_count, 1) if talk_time_count else 0,
            "byAgent": by_agent,
        }
