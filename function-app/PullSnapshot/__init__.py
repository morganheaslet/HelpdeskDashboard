"""
Timer-triggered function: every 15 minutes, pulls live ConnectWise + Dialpad
data and writes it to blob storage as `latest.json`. GetReportData (the
HTTP-triggered function) serves that blob to the frontend; RefreshNow (also
HTTP-triggered) does the exact same pull on demand, for the report's
"Refresh Now" button. The actual pull-and-merge logic lives in shared/refresh.py
so both triggers stay identical instead of drifting apart.

The weekly trend / tech leaderboard / per-board snapshot sections take longer
to compute (they scan more history) and don't need to be this fresh — this
scaffold recomputes everything every run for simplicity, but if ConnectWise API
rate limits become a concern, split those into their own timer (e.g. hourly)
and have this function only touch today's data.
"""
import logging
from datetime import datetime, timezone

import azure.functions as func

from shared.refresh import run_refresh


def main(myTimer: func.TimerRequest) -> None:
    now = datetime.now(timezone.utc)
    logging.info("PullSnapshot running at %s", now.isoformat())
    run_refresh(now=now)
