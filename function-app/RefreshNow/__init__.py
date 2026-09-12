"""
HTTP-triggered function behind the report's "Refresh Now" button: runs the
exact same ConnectWise + Dialpad pull as the 15-minute timer (PullSnapshot),
on demand, and hands the freshly-written data straight back so the frontend
can re-render immediately without a second round-trip to GetReportData.

authLevel is "anonymous" at the Function level for the same reason as
GetReportData: access control happens one layer up, at the Static Web App's
route rules (static-web-app/staticwebapp.config.json requires an authenticated
Entra ID session for /api/*) — this function is never meant to be reachable
directly, only through the linked Static Web App.

This can take a while to respond — Dialpad's stats export is asynchronous on
their end (roughly 20-30+ seconds per pull) — which is expected; the frontend's
Refresh Now button shows a "refreshing…" state and simply waits.
"""
import json
import logging

import azure.functions as func

from shared.refresh import run_refresh


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        data = run_refresh()
        return func.HttpResponse(json.dumps(data), mimetype="application/json", status_code=200)
    except Exception as exc:
        logging.exception("RefreshNow failed")
        return func.HttpResponse(
            json.dumps({"error": "Refresh failed", "detail": str(exc)}),
            mimetype="application/json",
            status_code=500,
        )
